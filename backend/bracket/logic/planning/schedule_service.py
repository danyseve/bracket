"""S4.5 — capa de datos del planificador de torneo (LAB ONLY).

Convierte un torneo real en lo que el nucleo del planificador necesita saber, escribe el plan
decidido y calcula lo derivado (retraso por tatami y estimacion). No decide: decidir es de
:mod:`bracket.logic.planning.scheduler`; aqui solo se lee, se adapta y se escribe.

Decisiones que esta capa respeta y no reabre (§2, §3, §4, §8, §9 de S4.5):

* los tatamis activos son los ``courts`` del torneo (o el subconjunto que pida el organizador): no
  se inventa ningun tatami ni se asume que sean seis;
* ``stage_item.court_id`` es planificacion de alto nivel y ``match.court_id`` es ejecucion: el plan
  escribe en el combate, nunca en el cuadro;
* el plan es **solo** de lo que todavia se puede jugar. Un combate ya jugado no se mueve nunca (se
  queda donde esta, con su hora), asi que entra con ``playable=False`` y su hueco no se reserva:
  ese hueco es pasado;
* ``estimated_start`` no se persiste: se deriva del plan mas el retraso observado del tatami.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

from heliclockter import datetime_utc

from bracket.logic.planning.scheduler import (
    MatchToSchedule,
    SchedulePlan,
    build_schedule,
    observed_lag_minutes,
)
from bracket.logic.scheduling.public_schedule import PublicMatch
from bracket.logic.scheduling.structural import get_playable_match_ids
from bracket.models.db.court import Court
from bracket.models.db.match import Match, MatchWithDetails, MatchWithDetailsDefinitive
from bracket.models.db.round import Round
from bracket.models.db.stage_item_inputs import StageItemInputFinal, StageItemInputTentative
from bracket.models.db.tournament import Tournament
from bracket.models.db.util import StageItemWithRounds, StageWithStageItems
from bracket.sql.courts import get_all_courts_in_tournament
from bracket.sql.matches import sql_reschedule_match_and_determine_duration_and_margin
from bracket.sql.stages import get_full_tournament_details
from bracket.sql.tournaments import sql_get_tournament
from bracket.utils.id_types import CourtId, TournamentId

MatchWithRounds = MatchWithDetails | MatchWithDetailsDefinitive

#: Un combate del torneo con el cuadro y la ronda donde vive.
MatchRow = tuple[StageItemWithRounds, Round, int, MatchWithRounds]

#: ``generate`` solo rellena lo que no tiene tatami ni hora; ``recalculate`` vuelve a planificar lo
#: pendiente (y solo mueve una asignacion explicita si el organizador lo pide con
#: ``preserve_assigned=False``).
ScheduleMode = Literal["generate", "recalculate"]


class ScheduleGenerationError(Exception):
    """El torneo no se puede planificar con lo que hay: sin tatamis activos o sin combates."""


@dataclass
class TournamentPlan:
    """El plan calculado junto con lo necesario para escribirlo y publicarlo."""

    tournament: Tournament
    court_ids: tuple[int, ...]
    plan: SchedulePlan
    matches_by_id: dict[int, MatchWithRounds] = field(default_factory=dict)
    lag_minutes_by_court: dict[int, int] = field(default_factory=dict)

    @property
    def scheduled_match_count(self) -> int:
        return len(self.plan.slots)


def match_duration_and_margin(match: Match, tournament: Tournament) -> tuple[int, int]:
    """Duracion prevista y transicion de un combate (§4): mandan ``custom_*`` si existen.

    El slot que ocupa el tatami es la suma de ambos, el mismo contrato que ya usaba
    ``Match.end_time``: no se inventa una duracion fija de cinco minutos.
    """
    duration = (
        tournament.duration_minutes
        if match.custom_duration_minutes is None
        else match.custom_duration_minutes
    )
    margin = (
        tournament.margin_minutes
        if match.custom_margin_minutes is None
        else match.custom_margin_minutes
    )
    return max(1, int(duration)), max(0, int(margin))


def _participant_keys(match: MatchWithRounds) -> tuple[str, ...]:
    """Quien puede jugar el combate, sin saber todavia quien jugara.

    Solo cuenta un pase ya resuelto con equipo: un pase que espera a un ganador no es una persona
    todavia, asi que no fija descansos de nadie.
    """
    keys: list[str] = []
    for input_ in (match.stage_item_input1, match.stage_item_input2):
        if isinstance(input_, StageItemInputFinal):
            keys.append(f"team:{int(input_.team_id)}")
    return tuple(sorted(keys))


def _feeder_ids(match: MatchWithRounds) -> tuple[int, ...]:
    """Combates cuyo ganador alimenta a este: su final manda sobre la hora de este (§5)."""
    feeders = [
        int(feeder_id)
        for feeder_id in (
            match.stage_item_input1_winner_from_match_id,
            match.stage_item_input2_winner_from_match_id,
        )
        if feeder_id is not None
    ]
    return tuple(sorted(feeders))


def _fighter_name(input_: object) -> str | None:
    """Nombre deportivo del pase, o ``None`` si todavia no se sabe quien pelea."""
    return input_.team.name if isinstance(input_, StageItemInputFinal) else None


def _waiting_labels(match: MatchWithRounds) -> tuple[str, ...]:
    """Que falta para poder jugar el combate, en texto legible para la pantalla (§5)."""
    labels: list[str] = []
    for side, input_ in (("a", match.stage_item_input1), ("b", match.stage_item_input2)):
        if isinstance(input_, StageItemInputFinal):
            continue
        feeder = (
            match.stage_item_input1_winner_from_match_id
            if side == "a"
            else match.stage_item_input2_winner_from_match_id
        )
        if isinstance(input_, StageItemInputTentative):
            labels.append(f"Ganador del cuadro {int(input_.winner_from_stage_item_id)}")
        elif feeder is not None:
            labels.append(f"Ganador del combate {int(feeder)}")
        else:
            labels.append("Ganador pendiente")
    return tuple(labels)


def match_rows(stages: list[StageWithStageItems]) -> list[MatchRow]:
    """Combates del torneo con su cuadro y su ronda, en orden determinista (por id)."""
    rows: list[MatchRow] = []
    for stage in stages:
        for stage_item in stage.stage_items:
            for round_order, round_ in enumerate(
                sorted(stage_item.rounds, key=lambda item: int(item.id)), start=1
            ):
                for match in round_.matches:
                    rows.append((stage_item, round_, round_order, match))
    return rows


def build_core_matches(
    *, stages: list[StageWithStageItems], tournament: Tournament
) -> dict[int, MatchToSchedule]:
    """Lo que el nucleo necesita saber, combate a combate."""
    core: dict[int, MatchToSchedule] = {}
    for stage_item, _, round_order, match in match_rows(stages):
        duration, margin = match_duration_and_margin(match, tournament)
        core[int(match.id)] = MatchToSchedule(
            match_id=int(match.id),
            court_id=None if match.court_id is None else int(match.court_id),
            start_time=match.start_time,
            round_order=round_order,
            position_hint=match.position_in_schedule,
            duration_minutes=duration,
            margin_minutes=margin,
            participant_keys=_participant_keys(match),
            feeder_match_ids=_feeder_ids(match),
            # Un combate jugado no se planifica (no se mueve nunca) y un bye no es un combate.
            playable=int(match.id) in get_playable_match_ids(stage_item)
            and match.finished_at is None,
        )
    return core


def matches_by_id(*, stages: list[StageWithStageItems]) -> dict[int, MatchWithRounds]:
    """Los combates crudos por id, para escribir el plan sobre el modelo real."""
    return {int(match.id): match for _, _, _, match in match_rows(stages)}


def resolve_active_court_ids(
    *, courts: list[Court], requested: list[int] | None
) -> tuple[int, ...]:
    """Tatamis activos del evento: los del torneo, o el subconjunto explicito del organizador.

    Nunca se anade un tatami que el torneo no tenga: pedir un tatami ajeno es un error de uso, no
    una excusa para planificar en el vacio.
    """
    available = {int(court.id) for court in courts}
    if requested is None:
        return tuple(sorted(available))
    unknown = sorted({int(court) for court in requested} - available)
    if unknown:
        raise ScheduleGenerationError(f"tatamis que no son del torneo: {unknown}")
    return tuple(sorted({int(court) for court in requested}))


def lag_minutes_by_court(
    *, matches: Iterable[MatchWithRounds], tournament: Tournament
) -> dict[int, int]:
    """Retraso observado por tatami (§9), en minutos y nunca negativo.

    Es el mayor desvio de lo ya terminado: que un combate acabe antes no adelanta el resto, porque
    un tatami no recupera tiempo por si solo.
    """
    lag: dict[int, int] = {}
    for match in matches:
        if match.court_id is None or match.start_time is None or match.finished_at is None:
            continue
        duration, margin = match_duration_and_margin(match, tournament)
        planned_end = match.start_time + timedelta(minutes=duration + margin)
        court = int(match.court_id)
        observed = observed_lag_minutes(planned_end=planned_end, finished_at=match.finished_at)
        lag[court] = max(lag.get(court, 0), observed)
    return lag


def public_matches(
    *, stages: list[StageWithStageItems], tournament: Tournament, lag_by_court: dict[int, int]
) -> list[PublicMatch]:
    """Proyeccion publica de los combates: lo deportivo, con ``estimated_start`` ya derivado.

    Una ronda en borrador no se publica: todavia no es un horario del evento.
    """
    result: list[PublicMatch] = []
    for stage_item, round_, _, match in match_rows(stages):
        if round_.is_draft:
            continue
        duration, margin = match_duration_and_margin(match, tournament)
        court_id = None if match.court_id is None else int(match.court_id)
        lag = 0 if court_id is None else max(0, int(lag_by_court.get(court_id, 0)))
        result.append(
            PublicMatch(
                match_id=int(match.id),
                court_id=court_id,
                category=stage_item.name,
                round_name=round_.name,
                fighter_a=_fighter_name(match.stage_item_input1),
                fighter_b=_fighter_name(match.stage_item_input2),
                scheduled_start=match.start_time,
                estimated_start=(
                    None if match.start_time is None else match.start_time + timedelta(minutes=lag)
                ),
                duration_minutes=duration,
                margin_minutes=margin,
                waiting_for=_waiting_labels(match),
                finished_at=match.finished_at,
                position_in_schedule=match.position_in_schedule,
                playable=int(match.id) in get_playable_match_ids(stage_item),
            )
        )
    return result


async def load_tournament_plan(
    *,
    tournament_id: TournamentId,
    start_time: datetime_utc | None = None,
    minimum_rest_minutes: int = 15,
    court_ids: list[int] | None = None,
    preserve_assigned: bool = True,
    mode: ScheduleMode = "recalculate",
) -> TournamentPlan:
    """Lee el torneo, decide el plan y devuelve todo lo necesario para escribirlo.

    ``mode="generate"`` no toca nada que ya tenga tatami y hora: solo rellena lo que falta.
    ``mode="recalculate"`` vuelve a planificar lo pendiente, y una asignacion explicita se mueve
    si el organizador lo pide (``preserve_assigned=False``).
    """
    tournament = await sql_get_tournament(tournament_id)
    courts = await get_all_courts_in_tournament(tournament_id)
    active_courts = resolve_active_court_ids(courts=courts, requested=court_ids)
    if not active_courts:
        raise ScheduleGenerationError("el torneo no tiene tatamis que planificar")

    stages = await get_full_tournament_details(tournament_id)
    core = build_core_matches(stages=stages, tournament=tournament)
    if not core:
        raise ScheduleGenerationError("el torneo no tiene combates que planificar")

    effective_start = start_time if start_time is not None else tournament.start_time
    if effective_start is None:
        raise ScheduleGenerationError(
            "el torneo no tiene hora de inicio: indica una para planificar"
        )

    effective_preserve = True if mode == "generate" else preserve_assigned
    plan = build_schedule(
        matches=core.values(),
        courts=list(active_courts),
        start_time=effective_start,
        minimum_rest_minutes=minimum_rest_minutes,
        preserve_assigned=effective_preserve,
    )
    return TournamentPlan(
        tournament=tournament,
        court_ids=active_courts,
        plan=plan,
        matches_by_id=matches_by_id(stages=stages),
        lag_minutes_by_court=lag_minutes_by_court(
            matches=[match for _, _, _, match in match_rows(stages)], tournament=tournament
        ),
    )


async def apply_plan(*, plan: TournamentPlan) -> int:
    """Escribe el plan sobre los combates y devuelve cuantos se movieron.

    Solo escribe lo que cambia. La escritura es determinista: si algo se cortase a medias, repetir
    la operacion con la misma clave deja el mismo plan, porque decidir no depende del azar.
    """
    moved = 0
    for slot in plan.plan.slots:
        match = plan.matches_by_id.get(slot.match_id)
        if match is None:
            continue
        already_there = (
            match.court_id == slot.court_id
            and match.start_time == slot.start_time
            and match.position_in_schedule == slot.position_in_schedule
        )
        if already_there:
            continue
        await sql_reschedule_match_and_determine_duration_and_margin(
            court_id=CourtId(slot.court_id),
            start_time=slot.start_time,
            position_in_schedule=slot.position_in_schedule,
            match=match,
            tournament=plan.tournament,
        )
        moved += 1
    return moved
