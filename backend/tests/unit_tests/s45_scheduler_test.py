"""S4.5 — nucleo del planificador de torneo (LAB ONLY).

Fija las reglas de la primera version y **solo** ellas: determinista, balanceo entre tatamis,
dependencias de ronda, descanso minimo del competidor, nunca dos combates simultaneos del mismo
participante y ninguna asignacion manual tocada por accidente.

El nucleo es puro: no toca base de datos, ni modelos, ni HTTP. Por eso estas pruebas describen el
contrato completo del algoritmo y la capa que habla con la base de datos queda para las pruebas de
integracion.

Reglas de plan (una por prueba, sin combinatoria escondida):

* orden de planificacion = (ronda, posicion sugerida, id) — estable y reproducible;
* un cuadro/combate ocupa un tatami durante ``duration_minutes + margin_minutes``;
* un combate no empieza antes de que terminen sus alimentadores (+descanso);
* un participante no repite antes de ``minimum_rest_minutes`` desde el final de su anterior;
* un combate ya asignado a mano (tatami **y** hora) se respeta tal cual;
* los tatamis se cargan por turnos (el que antes queda libre), con el id como desempate.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from heliclockter import datetime_utc

from bracket.logic.planning.scheduler import (
    MatchToSchedule,
    SchedulePlan,
    build_schedule,
    estimate_starts,
    observed_lag_minutes,
)

UTC = ZoneInfo("UTC")
START = datetime_utc(2026, 10, 12, 9, 0, 0, tzinfo=UTC)


def match(
    match_id: int,
    *,
    court: int | None = None,
    start: datetime_utc | None = None,
    round_order: int = 1,
    position: int | None = None,
    duration: int = 5,
    margin: int = 1,
    participants: tuple[str, ...] = (),
    feeders: tuple[int, ...] = (),
    playable: bool = True,
) -> MatchToSchedule:
    """Un combate con lo minimo que el planificador necesita saber de el."""
    return MatchToSchedule(
        match_id=match_id,
        court_id=court,
        start_time=start,
        round_order=round_order,
        position_hint=position,
        duration_minutes=duration,
        margin_minutes=margin,
        participant_keys=participants,
        feeder_match_ids=feeders,
        playable=playable,
    )


def plan(matches: list[MatchToSchedule], courts: list[int], **kwargs: object) -> SchedulePlan:
    """Planifica con los valores de la primera version salvo lo que diga la prueba."""
    options: dict[str, object] = {"start_time": START, "minimum_rest_minutes": 15}
    options.update(kwargs)
    return build_schedule(matches=matches, courts=courts, **options)  # type: ignore[arg-type]


def starts_by_court(plan_: SchedulePlan) -> dict[int, list[datetime]]:
    # El nucleo trabaja con ``datetime`` puro: no conoce el tipo de la capa de datos.
    result: dict[int, list[datetime]] = {}
    for slot in plan_.slots:
        result.setdefault(slot.court_id, []).append(slot.start_time)
    return result


def test_cuatro_tatamis_activos_de_seis_solo_usan_los_activos() -> None:
    """Con 4 tatamis activos, los combates no pueden caer en el 5 ni en el 6."""
    matches = [match(i, round_order=1) for i in range(1, 13)]
    built = plan(matches, [1, 2, 3, 4])

    assert {slot.court_id for slot in built.slots} == {1, 2, 3, 4}
    assert not built.unscheduled_match_ids
    assert all(slot.court_id not in (5, 6) for slot in built.slots)


def test_planificacion_determinista() -> None:
    """Dos veces el mismo torneo, el mismo plan: sin depender del orden de entrada."""
    matches = [match(i, round_order=1 + i % 3) for i in range(1, 10)]
    first = plan(matches, [1, 2, 3])
    second = plan(list(reversed(matches)), [1, 2, 3])

    assert [
        (s.match_id, s.court_id, s.position_in_schedule, s.start_time) for s in first.slots
    ] == [(s.match_id, s.court_id, s.position_in_schedule, s.start_time) for s in second.slots]
    assert first.slots == second.slots


def test_un_solo_tatami_es_secuencial_y_sin_solape() -> None:
    """En un tatami los combates van uno detras de otro, sin solaparse."""
    matches = [match(i, round_order=1, position=i) for i in range(1, 5)]
    built = plan(matches, [1])

    slots = sorted(built.slots, key=lambda slot: slot.start_time)
    assert [slot.position_in_schedule for slot in slots] == [1, 2, 3, 4]
    for previous, following in zip(slots, slots[1:], strict=False):
        assert following.start_time >= previous.end_time


def test_slot_es_duracion_mas_margen() -> None:
    """Cada combate reserva su duracion prevista mas su transicion (§4: configurable)."""
    matches = [
        match(1, duration=5, margin=1),
        match(2, duration=10, margin=3),
        match(3, duration=7, margin=0),
    ]
    built = plan(matches, [1])

    by_id = {slot.match_id: slot for slot in built.slots}
    assert (by_id[1].duration_minutes, by_id[1].margin_minutes) == (5, 1)
    assert (by_id[2].duration_minutes, by_id[2].margin_minutes) == (10, 3)
    assert (by_id[3].duration_minutes, by_id[3].margin_minutes) == (7, 0)
    assert by_id[1].end_time == START + timedelta(minutes=6)
    assert by_id[2].start_time == START + timedelta(minutes=6)


def test_balanceo_entre_tatamis() -> None:
    """Sin dependencias, la carga se reparte: 4 combates en 2 tatamis son 2 y 2."""
    matches = [match(i, round_order=1, position=i) for i in range(1, 5)]
    built = plan(matches, [1, 2])

    by_court = starts_by_court(built)
    assert {court: len(starts) for court, starts in by_court.items()} == {1: 2, 2: 2}
    assert by_court[1][0] == START
    assert by_court[2][0] == START


def test_dependencia_de_ronda_espera_a_los_alimentadores() -> None:
    """La final no empieza antes de que terminen las dos semifinales que la alimentan."""
    semis = [
        match(10, round_order=1, position=1, participants=("team:1", "team:2")),
        match(11, round_order=1, position=2, participants=("team:3", "team:4")),
    ]
    final = match(12, round_order=2, position=1, feeders=(10, 11), participants=())
    built = plan([*semis, final], [1, 2], minimum_rest_minutes=15)

    by_id = {slot.match_id: slot for slot in built.slots}
    feeders_end = max(by_id[10].end_time, by_id[11].end_time)
    assert by_id[12].start_time >= feeders_end + timedelta(minutes=15)


def test_los_cruces_sin_decidir_tambien_reservan_slot() -> None:
    """§5: el planificador conoce combates futuros aunque no sepa los participantes."""
    semis = [
        match(10, round_order=1, participants=("team:1", "team:2")),
        match(11, round_order=1, participants=("team:3", "team:4")),
    ]
    final = match(12, round_order=2, feeders=(10, 11), participants=())
    built = plan([*semis, final], [1, 2])

    assert built.slot_for(12) is not None
    assert not built.unscheduled_match_ids


def test_descanso_minimo_del_competidor() -> None:
    """Un competidor no repite antes de ``minimum_rest_minutes`` desde el final de su combate."""
    first = match(1, round_order=1, participants=("team:7", "team:8"))
    second = match(2, round_order=2, participants=("team:7", "team:9"), feeders=(1,))
    built = plan([first, second], [1, 2], minimum_rest_minutes=30)

    by_id = {slot.match_id: slot for slot in built.slots}
    assert by_id[2].start_time >= by_id[1].end_time + timedelta(minutes=30)


def test_un_combate_no_arranca_antes_del_inicio_del_torneo() -> None:
    """Nada empieza antes de la hora de inicio del evento."""
    built = plan([match(i, round_order=1) for i in range(1, 4)], [1, 2], start_time=START)

    assert all(slot.start_time >= START for slot in built.slots)


def test_respeta_las_asignaciones_manuales() -> None:
    """§8: una asignacion explicita (tatami y hora) no se sobrescribe en silencio."""
    manual_start = START + timedelta(hours=3)
    manual = match(
        99, court=3, start=manual_start, round_order=1, participants=("team:1", "team:2")
    )
    pending = [match(i, round_order=1) for i in range(1, 4)]
    built = plan([manual, *pending], [1, 2, 3])

    kept = built.slot_for(99)
    assert kept is not None
    assert kept.court_id == 3
    assert kept.start_time == manual_start
    assert built.manual_match_ids == (99,)
    # Y los demas siguen colocados, sin pisar la hora del manual.
    others = [slot for slot in built.slots if slot.match_id != 99]
    assert len(others) == 3
    assert all(not (slot.court_id == 3 and slot.start_time == manual_start) for slot in others)


def test_recalcular_puede_replanificar_lo_asignado_por_el_planificador() -> None:
    """Con ``preserve_assigned=False`` las asignaciones previas se vuelven a calcular (§9)."""
    previous = [
        match(1, court=1, start=START, round_order=1),
        match(2, court=1, start=START + timedelta(minutes=6), round_order=1),
    ]
    built = plan(previous, [1, 2], preserve_assigned=False)

    assert not built.manual_match_ids
    assert {slot.court_id for slot in built.slots} == {1, 2}


def test_los_combates_estructurales_no_ocupan_tatami() -> None:
    """Un bye/walkover no es un combate: no reserva tatami ni posicion (S4.2)."""
    built = plan(
        [
            match(1, round_order=1, playable=True, participants=("team:1", "team:2")),
            match(2, round_order=1, playable=False, participants=("team:1",)),
        ],
        [1],
    )

    assert built.slot_for(2) is None
    assert built.unscheduled_match_ids == (2,)


def test_estimacion_es_el_plan_mas_el_retraso_del_tatami() -> None:
    """§9: ``estimated_start`` se deriva; el plan persistido no se toca."""
    built = plan([match(i, round_order=1, position=i) for i in range(1, 4)], [1])
    estimated = estimate_starts(plan=built, lag_minutes_by_court={1: 12})

    planned = [
        slot.start_time for slot in sorted(built.slots, key=lambda s: s.position_in_schedule)
    ]
    assert [
        estimated[slot.match_id]
        for slot in sorted(built.slots, key=lambda s: s.position_in_schedule)
    ] == [start + timedelta(minutes=12) for start in planned]
    # El plan sigue intacto: la estimacion no escribe sobre el.
    assert [
        slot.start_time for slot in sorted(built.slots, key=lambda s: s.position_in_schedule)
    ] == planned


def test_lag_observado_sale_de_los_combates_terminados() -> None:
    """El retraso de un tatami es el mayor desvio observado en lo ya jugado, nunca negativo."""
    assert (
        observed_lag_minutes(
            planned_end=START + timedelta(minutes=6), finished_at=START + timedelta(minutes=20)
        )
        == 14
    )
    assert (
        observed_lag_minutes(
            planned_end=START + timedelta(minutes=6), finished_at=START + timedelta(minutes=6)
        )
        == 0
    )
    assert observed_lag_minutes(planned_end=START + timedelta(minutes=6), finished_at=None) == 0
    assert (
        observed_lag_minutes(
            planned_end=START + timedelta(minutes=6), finished_at=START + timedelta(minutes=4)
        )
        == 0
    )
