"""S4.5 — contrato de la pantalla publica (RED: la implementacion llega despues).

Lo que se fija aqui es lo que la grada, la API publica y el organizador tienen que contar igual:
quien es AHORA y quien es SIGUIENTE en cada tatami, que estado tiene cada combate y que el cuerpo
publico no lleva nada que no sea deportivo.
"""

from datetime import timedelta
from zoneinfo import ZoneInfo

from heliclockter import datetime_utc

from bracket.logic.scheduling.public_schedule import (
    STATE_COMPLETED,
    STATE_DELAYED,
    STATE_NEXT,
    STATE_NOW,
    STATE_UPCOMING,
    STATE_WAITING,
    PublicMatch,
    assert_public_payload_is_safe,
    build_court_display,
    to_public_payload,
)

UTC = ZoneInfo("UTC")
START = datetime_utc(2026, 10, 12, 9, 0, 0, tzinfo=UTC)


def match(
    match_id: int,
    *,
    court: int = 1,
    minutes: int = 0,
    category: str = "Senior -70kg",
    round_name: str = "Ronda 1",
    fighter_a: str | None = "Ane",
    fighter_b: str | None = "Bea",
    waiting_for: tuple[str, ...] = (),
    finished: datetime_utc | None = None,
    position: int | None = None,
    playable: bool = True,
) -> PublicMatch:
    planned = START + timedelta(minutes=minutes)
    return PublicMatch(
        match_id=match_id,
        court_id=court,
        category=category,
        round_name=round_name,
        fighter_a=fighter_a,
        fighter_b=fighter_b,
        scheduled_start=planned,
        estimated_start=planned,
        duration_minutes=5,
        margin_minutes=1,
        waiting_for=waiting_for,
        finished_at=finished,
        position_in_schedule=position,
        playable=playable,
    )


def test_ahora_y_siguiente_por_tatami() -> None:
    """El primer combate pendiente es AHORA, el segundo SIGUIENTE y el resto los siguientes."""
    display = build_court_display(
        court_id=1,
        court_name="Tatami 1",
        matches=[match(1), match(2, minutes=6), match(3, minutes=12), match(4, minutes=18)],
    )

    assert display.current is not None and display.current.match_id == 1
    assert display.current.state == STATE_NOW
    assert display.next is not None and display.next.match_id == 2
    assert display.next.state == STATE_NEXT
    assert [entry.match_id for entry in display.upcoming] == [3, 4]
    assert all(entry.state == STATE_UPCOMING for entry in display.upcoming)


def test_los_terminados_no_son_ahora_y_se_listan_apartes() -> None:
    """Un combate terminado no puede aparecer como AHORA: pasa a lo ya jugado."""
    display = build_court_display(
        court_id=1,
        court_name="Tatami 1",
        matches=[
            match(1, minutes=0, finished=START + timedelta(minutes=7)),
            match(2, minutes=6),
        ],
    )

    assert display.current is not None and display.current.match_id == 2
    assert [entry.match_id for entry in display.completed] == [1]
    assert display.completed[0].state == STATE_COMPLETED


def test_combate_sin_rivales_es_pendiente_de_ganador() -> None:
    """§5: mientras no se sepa quien pelea, la pantalla dice que espera a un ganador."""
    display = build_court_display(
        court_id=1,
        court_name="Tatami 1",
        matches=[
            match(
                9,
                fighter_a=None,
                fighter_b=None,
                waiting_for=("Ganador combate 10", "Ganador combate 11"),
            )
        ],
    )

    assert display.current is not None
    assert display.current.state == STATE_WAITING
    assert display.current.waiting_for == ("Ganador combate 10", "Ganador combate 11")
    assert display.current.fighter_a is None


def test_retraso_del_tatami_marca_los_pendientes() -> None:
    """§16: con el tatami tarde, los pendientes salen como DELAYED y con sus minutos de retraso."""
    display = build_court_display(
        court_id=1,
        court_name="Tatami 1",
        matches=[match(1), match(2, minutes=6)],
        lag_minutes=12,
    )

    assert display.lag_minutes == 12
    assert display.current is not None and display.current.state == STATE_DELAYED
    assert display.current.delay_minutes == 12
    assert display.next is not None and display.next.state == STATE_DELAYED


def test_cada_court_ve_solo_sus_combates() -> None:
    """Cada tatami ve lo suyo: la pantalla por tatami no mezcla combates de otros."""
    matches = [match(1, court=1), match(2, court=2), match(3, court=1, minutes=6)]
    first = build_court_display(court_id=1, court_name="Tatami 1", matches=matches)
    second = build_court_display(court_id=2, court_name="Tatami 2", matches=matches)

    assert [entry.match_id for entry in (first.current, first.next) if entry] == [1, 3]
    assert second.current is not None and second.current.match_id == 2
    assert second.next is None


def test_orden_por_hora_planificada_y_posicion() -> None:
    """La pantalla ordena por plan (hora y posicion): no inventa su propio orden."""
    display = build_court_display(
        court_id=1,
        court_name="Tatami 1",
        matches=[
            match(1, minutes=6, position=2),
            match(2, minutes=0, position=1),
        ],
    )

    assert display.current is not None and display.current.match_id == 2
    assert display.next is not None and display.next.match_id == 1


def test_tatami_sin_combates_no_revienta() -> None:
    """Un tatami vacio se muestra vacio, sin fallar."""
    display = build_court_display(court_id=4, court_name="Tatami 4", matches=[])

    assert display.current is None
    assert display.next is None
    assert display.upcoming == ()
    assert display.completed == ()


def test_los_combates_estructurales_no_se_publican() -> None:
    """Un bye/walkover no es un combate y no aparece en la pantalla."""
    display = build_court_display(
        court_id=1,
        court_name="Tatami 1",
        matches=[match(1, playable=False), match(2, minutes=6)],
    )

    assert display.current is not None and display.current.match_id == 2


def test_cuerpo_publico_solo_lleva_datos_deportivos() -> None:
    """§11: el cuerpo publico lleva nombres de pila deportivos y horas, y nada de cuentas."""
    display = build_court_display(
        court_id=1,
        court_name="Tatami 1",
        matches=[match(1), match(2, minutes=6), match(3, minutes=12)],
        lag_minutes=0,
    )
    payload = to_public_payload(
        tournament_name="Open Vetusta",
        tournament_start_time=START,
        courts=[display],
        generated_at=START,
    )

    assert_public_payload_is_safe(payload)
    assert set(payload) == {"tournament", "generated_at", "courts"}
    court = payload["courts"][0]  # type: ignore[index]
    assert set(court) == {
        "court_id",
        "court_name",
        "lag_minutes",
        "current",
        "next",
        "upcoming",
        "completed",
    }
    current = court["current"]
    assert set(current) == {
        "match_id",
        "category",
        "round_name",
        "fighter_a",
        "fighter_b",
        "scheduled_start",
        "estimated_start",
        "state",
        "position_in_schedule",
        "waiting_for",
    }
    assert current["fighter_a"] == "Ane" and current["fighter_b"] == "Bea"
    assert current["category"] == "Senior -70kg"
    assert current["scheduled_start"].startswith("2026-10-12T09:00:00")


def test_el_guardian_de_privacidad_detecta_fugas() -> None:
    """El guardian tiene que fallar si alguien cuela un dato de cuenta en el cuerpo publico."""
    for leak in ({"email": "a@b"}, {"fighters": [{"date_of_birth": "1990-01-01"}]}, {"jwt": "x"}):
        payload: dict[str, object] = {"tournament": {"name": "x"}, "courts": [{"current": leak}]}
        try:
            assert_public_payload_is_safe(payload)
        except AssertionError:
            continue
        raise AssertionError(f"no se detecto la fuga: {leak}")
