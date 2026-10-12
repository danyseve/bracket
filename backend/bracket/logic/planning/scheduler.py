"""S4.5 — planificador de torneo (LAB ONLY).

Reglas de la primera version, explicitas y sin optimizacion matematica (§8):

* el plan es **determinista**: mismo torneo, mismo plan, sin depender del orden de entrada;
* un combate ocupa su tatami durante ``duration_minutes + margin_minutes``;
* un combate espera a que terminen sus alimentadores (mas el descanso de sus ganadores);
* un participante no repite antes de ``minimum_rest_minutes`` desde el final de su combate;
* un tatami no aloja dos combates a la vez;
* los tatamis se eligen por turnos: el que antes pueda alojar el combate, con el id como desempate
  (asi la carga se reparte sola y el resultado sigue siendo reproducible);
* una asignacion explicita (tatami **y** hora) no se toca salvo que el organizador lo pida.

Este modulo no toca la base de datos, ni los modelos, ni HTTP: solo decide. Quien lee el torneo y
quien escribe el plan viven en la capa de arriba.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class MatchToSchedule:  # pylint: disable=too-many-instance-attributes
    """Lo minimo que el planificador necesita saber de un combate."""

    match_id: int
    court_id: int | None
    start_time: datetime | None
    round_order: int
    position_hint: int | None
    duration_minutes: int
    margin_minutes: int
    #: Identidad deportiva de quien puede jugarlo (``team:<id>``). Vacio si aun no se sabe quien.
    participant_keys: tuple[str, ...] = ()
    #: Combates cuyo ganador alimenta a este. Su final manda sobre la hora de este.
    feeder_match_ids: tuple[int, ...] = ()
    #: Un combate estructural (bye/walkover) no es un combate: no reserva tatami.
    playable: bool = True

    @property
    def slot_minutes(self) -> int:
        return self.duration_minutes + self.margin_minutes


@dataclass(frozen=True)
class ScheduledSlot:
    match_id: int
    court_id: int
    position_in_schedule: int
    start_time: datetime
    duration_minutes: int
    margin_minutes: int
    #: Verdadero si la asignacion venia puesta y se ha conservado tal cual.
    is_manual: bool = False

    @property
    def end_time(self) -> datetime:
        return self.start_time + timedelta(minutes=self.duration_minutes + self.margin_minutes)


@dataclass(frozen=True)
class SchedulePlan:
    slots: tuple[ScheduledSlot, ...]
    #: Combates que no pueden ocupar tatami (estructurales). No es un error: no son combates.
    unscheduled_match_ids: tuple[int, ...] = ()
    #: Asignaciones explicitas que se han respetado.
    manual_match_ids: tuple[int, ...] = ()

    def slot_for(self, match_id: int) -> ScheduledSlot | None:
        return next((slot for slot in self.slots if slot.match_id == match_id), None)


def _first_free(
    busy: Sequence[tuple[datetime, datetime]], earliest: datetime, minutes: int
) -> datetime:
    """Primer hueco del tatami a partir de ``earliest`` que quepa en ``minutes``."""
    start = earliest
    for busy_start, busy_end in sorted(busy):
        if busy_end <= start:
            continue
        if busy_start >= start + timedelta(minutes=minutes):
            break
        start = busy_end
    return start


def observed_lag_minutes(*, planned_end: datetime, finished_at: datetime | None) -> int:
    """Desvio observado de un combate ya terminado, en minutos y nunca negativo (§9)."""
    if finished_at is None:
        return 0
    return max(0, int((finished_at - planned_end).total_seconds() // 60))


# 13 ramas, una por regla del plan (descanso, alimentadores, tatami libre, asignacion
# manual). Partirlas en funciones mas pequenas cambiaria el algoritmo sin ganar claridad.
def build_schedule(  # pylint: disable=too-many-branches
    *,
    matches: Iterable[MatchToSchedule],
    courts: Sequence[int],
    start_time: datetime,
    minimum_rest_minutes: int = 15,
    preserve_assigned: bool = True,
) -> SchedulePlan:
    """Construye el plan de un torneo sobre los tatamis activos que se le pasen.

    ``courts`` son los tatamis activos del evento (§1: los courts del torneo). No se inventa
    ninguno: fuera de esa lista no se planifica.
    """
    court_ids = list(dict.fromkeys(int(court) for court in courts))
    if not court_ids:
        raise ValueError("no hay tatamis activos: sin tatamis no hay planificacion")

    rest = timedelta(minutes=max(0, minimum_rest_minutes))
    by_id = {match.match_id: match for match in matches}

    manual: list[ScheduledSlot] = []
    pending: list[MatchToSchedule] = []
    unscheduled: list[int] = []
    for match in by_id.values():
        if not match.playable:
            unscheduled.append(match.match_id)
            continue
        already_assigned = match.court_id in court_ids and match.start_time is not None
        if already_assigned and preserve_assigned:
            manual.append(
                ScheduledSlot(
                    match_id=match.match_id,
                    court_id=int(match.court_id),  # type: ignore[arg-type]
                    position_in_schedule=0,
                    start_time=match.start_time,  # type: ignore[arg-type]
                    duration_minutes=match.duration_minutes,
                    margin_minutes=match.margin_minutes,
                    is_manual=True,
                )
            )
        else:
            pending.append(match)

    court_busy: dict[int, list[tuple[datetime, datetime]]] = defaultdict(list)
    participant_busy: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    placed: dict[int, ScheduledSlot] = {}
    for slot in manual:
        court_busy[slot.court_id].append((slot.start_time, slot.end_time))
        for key in by_id[slot.match_id].participant_keys:
            participant_busy[key].append((slot.start_time, slot.end_time))
        placed[slot.match_id] = slot

    order = sorted(
        pending,
        key=lambda match: (
            match.round_order,
            match.position_hint if match.position_hint is not None else 10**9,
            match.match_id,
        ),
    )

    for match in order:
        earliest = start_time
        for feeder_id in match.feeder_match_ids:
            feeder = placed.get(feeder_id)
            if feeder is not None:
                earliest = max(earliest, feeder.end_time + rest)
        for key in match.participant_keys:
            for _busy_start, busy_end in participant_busy.get(key, []):
                earliest = max(earliest, busy_end + rest)

        court_start = [
            (_first_free(court_busy[court], earliest, match.slot_minutes), court)
            for court in court_ids
        ]
        start, court = min(court_start, key=lambda item: (item[0], item[1]))
        slot = ScheduledSlot(
            match_id=match.match_id,
            court_id=court,
            position_in_schedule=0,
            start_time=start,
            duration_minutes=match.duration_minutes,
            margin_minutes=match.margin_minutes,
        )
        court_busy[court].append((start, slot.end_time))
        for key in match.participant_keys:
            participant_busy[key].append((start, slot.end_time))
        placed[match.match_id] = slot

    ordered = sorted(placed.values(), key=lambda s: (s.start_time, s.court_id, s.match_id))
    numbered = tuple(
        ScheduledSlot(
            match_id=slot.match_id,
            court_id=slot.court_id,
            position_in_schedule=position,
            start_time=slot.start_time,
            duration_minutes=slot.duration_minutes,
            margin_minutes=slot.margin_minutes,
            is_manual=slot.is_manual,
        )
        for position, slot in enumerate(ordered, start=1)
    )

    return SchedulePlan(
        slots=numbered,
        unscheduled_match_ids=tuple(sorted(unscheduled)),
        manual_match_ids=tuple(sorted(slot.match_id for slot in numbered if slot.is_manual)),
    )


def estimate_starts(
    *, plan: SchedulePlan, lag_minutes_by_court: Mapping[int, int]
) -> dict[int, datetime]:
    """``estimated_start`` derivado (§9): el plan del tatami mas su retraso observado.

    No escribe nada: es una lectura. El horario planificado se queda donde esta.
    """
    return {
        slot.match_id: slot.start_time
        + timedelta(minutes=max(0, int(lag_minutes_by_court.get(slot.court_id, 0))))
        for slot in plan.slots
    }
