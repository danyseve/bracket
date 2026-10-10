"""S3.4c - estado derivado de la ventana de inscripcion, con reloj controlado.

TDD: este modulo se escribio **antes** de la implementacion y fallo en rojo (la funcion
``registration_window_state`` y su vocabulario no existian).

La ventana **no** es una columna de estado: se deriva de dos instantes, ``registration_deadline``
(plazo) y ``registrations_closed_at`` (cierre manual). Aqui se fija la tabla de decision que
consumen la ruta de lectura, la de cierre y el alta de inscripciones:

* el **cierre manual manda**: cerrado a mano sigue cerrado aunque el plazo no haya vencido, aunque
  el plazo ya haya vencido y aunque despues se retire el plazo;
* sin cierre manual, el **plazo vencido** cierra (``now >= deadline``: en el instante exacto del
  plazo ya no se admite una inscripcion nueva);
* sin cierre manual y sin plazo, la ventana esta **abierta**.

Se prueba con instantes concretos inyectados (reloj controlado), no con ``sleep``. La comparacion
es por instante y no por representacion local, asi que los mismos casos se comprueban con el reloj
del laboratorio en UTC y en una zona con desplazamiento distinto.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Final
from zoneinfo import ZoneInfo

import pytest
from heliclockter import datetime_utc

from bracket.logic.registration_window import (
    REGISTRATION_WINDOW_STATES,
    registration_window_state,
)
from bracket.models.db.domain import RegistrationWindowState

#: Reloj del laboratorio: un instante fijo, en UTC y aware (nunca naive).
NOW: Final = datetime_utc(2026, 10, 11, 10, 0, 0, tzinfo=ZoneInfo("UTC"))
#: El mismo instante en UTC puro, para reescribirlo con otro desplazamiento local sin ambiguedad.
NOW_STD: Final = datetime(2026, 10, 11, 10, 0, 0, tzinfo=UTC)
BEFORE: Final = NOW - timedelta(seconds=1)
AFTER: Final = NOW + timedelta(seconds=1)
CLOSED: Final = NOW - timedelta(days=1)


def test_the_vocabulary_is_closed() -> None:
    """El estado derivado tiene tres valores y ninguno mas: es un contrato publico."""
    assert set(REGISTRATION_WINDOW_STATES) == {"OPEN", "CLOSED", "DEADLINE_PASSED"}


def test_without_deadline_and_without_closure_the_window_is_open() -> None:
    assert (
        registration_window_state(registration_deadline=None, registrations_closed_at=None, now=NOW)
        == "OPEN"
    )


def test_a_future_deadline_leaves_the_window_open() -> None:
    assert (
        registration_window_state(
            registration_deadline=AFTER, registrations_closed_at=None, now=NOW
        )
        == "OPEN"
    )


def test_a_deadline_one_second_before_now_no_longer_admits_registrations() -> None:
    """El limite es el instante exacto y sin periodo de gracia: un segundo despues del plazo
    ya no entra una inscripcion nueva."""
    assert (
        registration_window_state(
            registration_deadline=BEFORE, registrations_closed_at=None, now=NOW
        )
        == "DEADLINE_PASSED"
    )


def test_the_exact_deadline_moment_is_already_closed() -> None:
    """En el instante del plazo ya no entra una inscripcion nueva: el limite es inclusivo."""
    assert (
        registration_window_state(registration_deadline=NOW, registrations_closed_at=None, now=NOW)
        == "DEADLINE_PASSED"
    )


def test_a_past_deadline_closes_the_window() -> None:
    assert (
        registration_window_state(
            registration_deadline=CLOSED, registrations_closed_at=None, now=NOW
        )
        == "DEADLINE_PASSED"
    )


def test_a_manual_closure_without_deadline_is_closed() -> None:
    assert (
        registration_window_state(
            registration_deadline=None, registrations_closed_at=CLOSED, now=NOW
        )
        == "CLOSED"
    )


def test_a_manual_closure_before_the_deadline_wins() -> None:
    """Cerrar a mano cierra ahora, sin esperar al plazo."""
    assert (
        registration_window_state(
            registration_deadline=AFTER, registrations_closed_at=CLOSED, now=NOW
        )
        == "CLOSED"
    )


def test_a_manual_closure_outranks_a_passed_deadline() -> None:
    """Con las dos cosas, el estado es el cierre manual: es la accion explicita del tenant."""
    assert (
        registration_window_state(
            registration_deadline=CLOSED, registrations_closed_at=CLOSED, now=NOW
        )
        == "CLOSED"
    )


@pytest.mark.parametrize("offset", [timedelta(0), timedelta(hours=2), timedelta(hours=-7)])
def test_the_comparison_is_by_instant_and_not_by_local_representation(offset: timedelta) -> None:
    """El plazo puede venir escrito con otra hora local: el veredicto no cambia.

    ``local`` es *el mismo instante* que ``NOW`` con otro desplazamiento local (por eso su
    ``utcoffset`` es ``offset`` y su reloj de pared ya no marca las 10:00). Comparar por
    representacion local daria otro veredicto; comparar por instante da el mismo.
    """
    local = NOW_STD.astimezone(timezone(offset))
    assert local.utcoffset() == offset
    assert local == NOW, "es el mismo instante, escrito de otra forma"

    # Tal y como llega del cable: el mismo instante con otro desplazamiento declarado.
    deadline = datetime_utc.fromisoformat(local.isoformat())
    assert deadline == NOW

    assert (
        registration_window_state(
            registration_deadline=deadline, registrations_closed_at=None, now=NOW
        )
        == "DEADLINE_PASSED"
    ), "el instante del plazo y el del reloj son el mismo, vengan como vengan"


def test_the_state_is_a_value_of_the_declared_vocabulary() -> None:
    state: RegistrationWindowState = registration_window_state(
        registration_deadline=BEFORE, registrations_closed_at=None, now=NOW
    )
    assert state in REGISTRATION_WINDOW_STATES
