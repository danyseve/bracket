"""Proyeccion de la cola de un tatami a partir de la lectura de cuadros (S4.4, F3C, LAB ONLY).

Logica **pura**: recibe la lectura que ya existe (``get_full_tournament_details``) y el tatami, y
devuelve el contrato de la cola. No consulta la base de datos y no escribe nada.

Reglas que aplica, y que son las unicas que puede aplicar:

* **Pertenencia**: un cuadro esta en la cola de un tatami si su asignacion (``court_id``) apunta a
  ese tatami. Es la autoridad de S4.2/S4.3; el ``court_id`` del combate se publica como dato.
* **Orden**: se conserva el del contrato de lectura del backend (cuadros y combates tal como los
  devuelve ``stages``: ``ORDER BY rounds.id, matches.id`` en ``bracket/sql/stages.py``). No se
  reordena por ``position_in_schedule``, ni por marcador, ni por estado.
* **Jugable**: los dos pases tienen equipo (``StageItemInputFinal``). Cualquier otro caso
  (tentativo, apuntando al ganador de otro cuadro, o vacio) se publica como espera con su
  procedencia. No se infiere nada del marcador.
"""

from __future__ import annotations

from typing import Final, Literal

from bracket.models.db.court import Court
from bracket.models.db.court_queue import (
    CourtQueueResponse,
    QueueFighter,
    QueueMatch,
    QueueStageItem,
    QueueState,
    QueueWaiting,
)
from bracket.models.db.match import MatchWithDetails, MatchWithDetailsDefinitive
from bracket.models.db.round import Round
from bracket.models.db.stage_item_inputs import (
    StageItemInput,
    StageItemInputFinal,
    StageItemInputTentative,
)
from bracket.models.db.tournament import Tournament
from bracket.models.db.util import StageWithStageItems

#: Los dos unicos estados publicables y los dos lados de un combate.
PLAYABLE: Final[QueueState] = "playable"
WAITING_ON_WINNERS: Final[QueueState] = "waiting_on_winners"
COMPLETED: Final[QueueState] = "completed"
Side = Literal["a", "b"]

MatchWithRounds = MatchWithDetails | MatchWithDetailsDefinitive


def _fighter(input_: StageItemInput | None) -> QueueFighter | None:
    """Luchador de un pase resuelto, o ``None`` si el pase todavia no tiene equipo."""
    if not isinstance(input_, StageItemInputFinal):
        return None

    return QueueFighter(
        stage_item_input_id=input_.id, team_id=input_.team_id, name=input_.team.name
    )


def _waiting(match: MatchWithRounds, side: Side) -> QueueWaiting | None:
    """Motivo por el que un lado no se puede jugar todavia, o ``None`` si si se puede."""
    input_ = match.stage_item_input1 if side == "a" else match.stage_item_input2
    if isinstance(input_, StageItemInputFinal):
        return None

    from_match_id = (
        match.stage_item_input1_winner_from_match_id
        if side == "a"
        else match.stage_item_input2_winner_from_match_id
    )
    if isinstance(input_, StageItemInputTentative):
        return QueueWaiting(
            side=side,
            from_stage_item_id=input_.winner_from_stage_item_id,
            winner_position=input_.winner_position,
            from_match_id=from_match_id,
        )

    return QueueWaiting(side=side, from_match_id=from_match_id)


def _duration_seconds(match: MatchWithRounds) -> int | None:
    """Duracion efectiva del combate en segundos, la misma unidad que usa el Bridge."""
    minutes = (
        match.custom_duration_minutes
        if match.custom_duration_minutes is not None
        else match.duration_minutes
    )
    if minutes <= 0:
        return None

    return minutes * 60


def _queue_match(match: MatchWithRounds, round_: Round) -> QueueMatch:
    """Proyeccion de un combate: sin conflictos, sin banderas internas y sin estado inventado."""
    waiting = [item for item in (_waiting(match, "a"), _waiting(match, "b")) if item is not None]
    return QueueMatch(
        match_id=match.id,
        round_id=round_.id,
        round_name=round_.name,
        round_is_draft=round_.is_draft,
        position_in_schedule=match.position_in_schedule,
        start_time=match.start_time,
        duration_seconds=_duration_seconds(match),
        court_id=match.court_id,
        fighter_a=_fighter(match.stage_item_input1),
        fighter_b=_fighter(match.stage_item_input2),
        score_a=match.stage_item_input1_score,
        score_b=match.stage_item_input2_score,
        winner_stage_item_input_id=match.winner_stage_item_input_id,
        result_method=match.result_method,
        state=(
            COMPLETED
            if match.get_winner() is not None
            else (WAITING_ON_WINNERS if waiting else PLAYABLE)
        ),
        waiting_for=waiting,
    )


def build_court_queue(
    *, tournament: Tournament, court: Court, stages: list[StageWithStageItems]
) -> CourtQueueResponse:
    """Cola del tatami: los cuadros asignados y sus combates, en el orden del backend."""
    stage_items: list[QueueStageItem] = []
    for stage in stages:
        for stage_item in stage.stage_items:
            if stage_item.court_id != court.id:
                continue

            matches = [
                _queue_match(match, round_)
                for round_ in stage_item.rounds
                for match in round_.matches
            ]
            stage_items.append(
                QueueStageItem(
                    stage_item_id=stage_item.id,
                    name=stage_item.name,
                    type_name=stage_item.type_name,
                    matches=matches,
                )
            )

    # "Actual" y "siguiente" no son el id menor ni el primero de la lista cruda: son los dos
    # primeros combates JUGABLES en el orden de lectura, que ya implica dependencias resueltas y
    # participantes conocidos. Un combate resuelto no vuelve a ser ni actual ni siguiente.
    playable_match_ids = [
        match.match_id
        for stage_item in stage_items
        for match in stage_item.matches
        if match.state == PLAYABLE
    ]
    current_match_id = playable_match_ids[0] if playable_match_ids else None
    next_match_id = playable_match_ids[1] if len(playable_match_ids) > 1 else None
    return CourtQueueResponse(
        tournament_id=tournament.id,
        tournament_name=tournament.name,
        court_id=court.id,
        court_name=court.name,
        stage_items=stage_items,
        current_match_id=current_match_id,
        next_match_id=next_match_id,
    )
