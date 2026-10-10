# pylint: disable=redefined-outer-name  # `registration_data` es el fixture de este modulo.
"""S3.4c - concurrencia del alta contra el cierre de la ventana de inscripcion.

La carrera que hay que resolver es explicita: el alta no puede decidir con un estado de ventana
leido antes de que otro cierre la ventana. Aqui se demuestra que la decision la serializa la **fila
del torneo**, con el orden de bloqueo ``idempotencia -> torneo -> competidor -> academia ->
inscripcion``:

* el alta toma el torneo en modo **compartido** (``FOR SHARE``): dos altas concurrentes del mismo
  torneo **no** se estorban entre si;
* el cierre y la configuracion del plazo lo toman en modo **exclusivo** (``FOR NO KEY UPDATE``):
  mientras un cierre esta en vuelo, un alta **espera** y decide despues (nunca inserta con la
  ventana cerrada);
* mientras un alta esta en vuelo (ya con el torneo tomado), el cierre **espera**: el alta termina y
  el cierre se aplica despues, sin estado intermedio;
* dos cierres concurrentes se serializan y dejan **un unico** evento de auditoria.

Las dos carreras se montan con las operaciones **reales** en vuelo, sin *sleeps*: el alta se detiene
con un bloqueo sobre la fila del competidor (que el alta toma despues del torneo) y el cierre con un
bloqueo sobre la tabla de auditoria (donde escribe despues de tomar el torneo). La espera se
comprueba leyendo ``pg_stat_activity`` (mismo mecanismo que RS-9/S3.2), no con tiempos.

Validacion exclusivamente contra la base de laboratorio (``bracket_ci`` con ``ENVIRONMENT=CI``, o
``bracket_test``); ``bracket_dev`` no se usa para escrituras.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from databases import Database

from bracket.database import database
from bracket.logic.registrations import (
    RegistrationWindowAlreadyClosedError,
    RegistrationWindowClosedError,
    close_registrations,
    create_registration,
)
from bracket.models.db.domain import RegistrationDraftData, TournamentRegistration
from tests.integration_tests.registration_fixtures import (
    WAITING_TIMEOUT_SECONDS,
    RegistrationData,
    build_draft,
    registration_data_context,
    wait_for_waiting_backends,
)

#: Pausa el alta **despues** de tomar el torneo y antes de terminar: el alta real espera aqui.
SELECT_COMPETITOR_ROW_FOR_UPDATE = """
    SELECT id FROM competitors WHERE id = :competitor_id FOR UPDATE
"""

#: Pausa el cierre **despues** de tomar el torneo y antes de confirmar: escribe la auditoria al final.
LOCK_DOMAIN_CHANGE_LOG = "LOCK TABLE domain_change_log IN ACCESS EXCLUSIVE MODE"


@pytest_asyncio.fixture(loop_scope="session")
async def registration_data(reinit_database: Database) -> AsyncIterator[RegistrationData]:
    """Datos de laboratorio (mismo soporte que S3.1/S3.2), con limpieza garantizada."""
    async with registration_data_context(reinit_database) as data:
        yield data


def _draft(registration_data: RegistrationData, key: str) -> RegistrationDraftData:
    """Borrador valido sin academia: estas carreras solo dependen del competidor."""
    return build_draft(
        competitor_id=registration_data.competitor_a,
        category_key=key,
        category_label=key.replace("-", " ").title(),
    )


async def _hold_competitor_row(
    registration_data: RegistrationData, ready: asyncio.Event, release: asyncio.Event
) -> None:
    """Retiene la fila del competidor hasta ``release``, para detener un alta en vuelo."""
    async with database.transaction():
        await database.execute(
            query=SELECT_COMPETITOR_ROW_FOR_UPDATE,
            values={"competitor_id": registration_data.competitor_a},
        )
        ready.set()
        await release.wait()


async def _hold_audit_table(ready: asyncio.Event, release: asyncio.Event) -> None:
    """Retiene la tabla de auditoria hasta ``release``, para detener un cierre en vuelo."""
    async with database.transaction():
        await database.execute(query=LOCK_DOMAIN_CHANGE_LOG)
        ready.set()
        await release.wait()


@pytest.mark.asyncio(loop_scope="session")
async def test_a_create_in_flight_makes_the_close_wait_and_then_close(
    registration_data: RegistrationData,
) -> None:
    """Con un alta en vuelo (ya con el torneo tomado), el cierre espera; despues cierra."""
    ready, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold_competitor_row(registration_data, ready, release))
    await asyncio.wait_for(ready.wait(), WAITING_TIMEOUT_SECONDS)

    create = asyncio.create_task(
        create_registration(
            registration_data.context_owner_a,
            registration_data.tournament_a,
            _draft(registration_data, "s34c-in-flight"),
        )
    )
    # El alta ya tomo el torneo en compartido y espera la fila del competidor.
    await wait_for_waiting_backends("%FOR SHARE%", expected=1)

    close = asyncio.create_task(
        close_registrations(registration_data.context_owner_a, registration_data.tournament_a)
    )
    # El cierre **espera** al alta en vuelo: no puede decidir con la fila del torneo tomada.
    await wait_for_waiting_backends("%FOR NO KEY UPDATE%", expected=1)

    release.set()
    registration = await asyncio.wait_for(create, WAITING_TIMEOUT_SECONDS)
    window = await asyncio.wait_for(close, WAITING_TIMEOUT_SECONDS)
    await asyncio.wait_for(holder, WAITING_TIMEOUT_SECONDS)

    assert isinstance(registration, TournamentRegistration)
    assert window.state == "CLOSED"
    # El alta empezo antes: quedo inscrita. La ventana cerro despues, sin estado intermedio.
    assert await _is_registered(registration.id)


@pytest.mark.asyncio(loop_scope="session")
async def test_a_close_in_flight_makes_the_create_wait_and_then_reject(
    registration_data: RegistrationData,
) -> None:
    """Con un cierre en vuelo, el alta espera y despues **rechaza**: no inserta con la ventana cerrada."""
    ready, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold_audit_table(ready, release))
    await asyncio.wait_for(ready.wait(), WAITING_TIMEOUT_SECONDS)

    close = asyncio.create_task(
        close_registrations(registration_data.context_owner_a, registration_data.tournament_a)
    )
    # El cierre ya tomo el torneo en exclusivo y espera para escribir su auditoria.
    await wait_for_waiting_backends("%ACCESS EXCLUSIVE%", expected=1)

    create = asyncio.create_task(
        create_registration(
            registration_data.context_owner_a,
            registration_data.tournament_a,
            _draft(registration_data, "s34c-waits"),
        )
    )
    # El alta espera el torneo: la decision no se puede tomar con un estado anterior al cierre.
    await wait_for_waiting_backends("%FOR SHARE%", expected=1)

    release.set()
    window = await asyncio.wait_for(close, WAITING_TIMEOUT_SECONDS)
    await asyncio.wait_for(holder, WAITING_TIMEOUT_SECONDS)

    with pytest.raises(RegistrationWindowClosedError):
        await asyncio.wait_for(create, WAITING_TIMEOUT_SECONDS)

    assert window.state == "CLOSED"
    assert not await _any_registration(registration_data.tournament_a)


@pytest.mark.asyncio(loop_scope="session")
async def test_two_creates_in_flight_do_not_block_each_other(
    registration_data: RegistrationData,
) -> None:
    """El torneo se toma en compartido: dos altas del mismo torneo conviven."""
    first_status, second_status = await asyncio.gather(
        create_registration(
            registration_data.context_owner_a,
            registration_data.tournament_a,
            _draft(registration_data, "s34c-pair-a"),
        ),
        create_registration(
            registration_data.context_owner_a,
            registration_data.tournament_a,
            _draft(registration_data, "s34c-pair-b"),
        ),
    )

    assert first_status.id != second_status.id
    assert await _is_registered(first_status.id) and await _is_registered(second_status.id)


@pytest.mark.asyncio(loop_scope="session")
async def test_two_concurrent_closes_serialize_and_leave_a_single_audit_event(
    registration_data: RegistrationData,
) -> None:
    """Dos cierres a la vez: uno cierra y el otro se encuentra el cierre hecho (sin doble evento)."""
    outcomes = await asyncio.gather(
        close_registrations(registration_data.context_owner_a, registration_data.tournament_a),
        close_registrations(registration_data.context_owner_a, registration_data.tournament_a),
        return_exceptions=True,
    )

    closed = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    conflicts = [
        outcome
        for outcome in outcomes
        if isinstance(outcome, RegistrationWindowAlreadyClosedError)
    ]
    assert len(closed) == 1, outcomes
    assert len(conflicts) == 1, outcomes

    assert await _close_audit_events(registration_data.tournament_a) == 1, (
        "un unico cierre auditado"
    )


async def _is_registered(registration_id: int) -> bool:
    count = await database.fetch_val(
        query="SELECT count(*) FROM tournament_registrations WHERE id = :registration_id",
        values={"registration_id": registration_id},
    )
    return int(count) == 1


async def _any_registration(tournament_id: int) -> bool:
    count = await database.fetch_val(
        query="SELECT count(*) FROM tournament_registrations WHERE tournament_id = :tournament_id",
        values={"tournament_id": tournament_id},
    )
    return int(count) > 0


async def _close_audit_events(tournament_id: int) -> int:
    """Eventos de cierre auditados para ese torneo: la prueba de que no hay doble auditoria."""
    count = await database.fetch_val(
        query=(
            "SELECT count(*) FROM domain_change_log"
            " WHERE entity = 'tournament' AND action = 'CLOSE_REGISTRATIONS'"
            " AND entity_id = :tournament_id"
        ),
        values={"tournament_id": tournament_id},
    )
    return int(count)
