# pylint: disable=redefined-outer-name  # `registration_data` es un fixture de modulo.
"""S3.1a de F3B - RS-9: serializacion de la elegibilidad del competidor al confirmar.

Concurrencia **controlada**: en vez de lanzar dos operaciones y dejar que la suerte decida el
orden, cada prueba mantiene una transaccion abierta y espera **evidencia positiva** del propio
PostgreSQL (un backend con ``wait_event_type = 'Lock'`` ejecutando la sentencia esperada, y una
entrada de bloqueo de fila en ``pg_locks``, que solo existe mientras hay conflicto de fila)
antes de concluir nada. No hay ``sleep`` como mecanismo de sincronizacion, no hay dobles del
bloqueo de PostgreSQL y se usan las sentencias reales de S2 (baja logica) y de la confirmacion.

Ordenes cubiertos:

* **A**: la baja logica ya tiene la fila del competidor cuando llega la confirmacion.
* **B**: la confirmacion tiene el ``FOR SHARE`` y la baja logica espera a que termine.

Datos y *fixture* en ``registration_fixtures.py``; base de datos exclusivamente de laboratorio.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from databases import Database

from bracket.database import database
from bracket.logic.competitors import deactivate_competitor
from bracket.logic.registrations import (
    CompetitorNotSelectableError,
    confirm_registration,
    create_registration,
)
from bracket.models.db.domain import Competitor, TournamentRegistration
from bracket.sql.domain_reads import get_competitor, get_registration
from bracket.sql.domain_writes import sql_deactivate_competitor
from tests.integration_tests.registration_fixtures import (
    RegistrationData,
    audit_rows,
    build_draft,
    count_current_registrations,
    registration_data_context,
)

# Pax (solo pruebas): bloquea la fila de la inscripcion para que la confirmacion se detenga
# dentro de su transaccion, despues de haber tomado el bloqueo del competidor.
SELECT_REGISTRATION_ROW_FOR_UPDATE = """
    SELECT id FROM tournament_registrations WHERE id = :registration_id FOR UPDATE
"""

# Evidencia de espera: un backend detenido por un bloqueo ejecutando esa sentencia.
SELECT_WAITING_BACKENDS = """
    SELECT count(*) FROM pg_stat_activity
    WHERE datname = current_database()
        AND wait_event_type = 'Lock'
        AND query LIKE :pattern
"""

# Evidencia de conflicto de fila: los bloqueos de fila solo aparecen en `pg_locks` mientras
# hay contencion (el que espera y el que ya lo tiene), asi que su existencia es la senal.
SELECT_TUPLE_CONTENTION = """
    SELECT count(*) FROM pg_locks
    WHERE locktype = 'tuple' AND relation = to_regclass(:relation)
"""

WAITING_TIMEOUT_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 0.01


@pytest_asyncio.fixture(loop_scope="session")
async def registration_data(reinit_database: Database) -> AsyncIterator[RegistrationData]:
    """Datos de S3.1: envuelve ``registration_data_context`` y garantiza la limpieza."""
    async with registration_data_context(reinit_database) as data:
        yield data


async def _wait_until_true(query: str, values: dict[str, object], *, message: str) -> None:
    """Espera acotada a que la consulta devuelva algo; si no, falla con ese mensaje.

    El limite no es el mecanismo de sincronizacion (para eso estan los eventos): es la red
    que convierte "nunca llego a bloquearse" en un fallo explicito en vez de un cuelgue.
    """
    deadline = asyncio.get_running_loop().time() + WAITING_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        if await database.fetch_val(query=query, values=values):
            return
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(message)


async def _wait_for_waiting_backend(pattern: str) -> None:
    """Espera a que haya un backend detenido por un bloqueo ejecutando esa sentencia."""
    await _wait_until_true(
        SELECT_WAITING_BACKENDS,
        {"pattern": pattern},
        message=f"ningun backend esperaba un bloqueo con la sentencia {pattern!r}",
    )


async def _wait_for_tuple_contention(relation: str) -> None:
    """Espera a que haya contencion de bloqueo de fila (row lock) en esa tabla."""
    await _wait_until_true(
        SELECT_TUPLE_CONTENTION,
        {"relation": relation},
        message=f"no aparecio contencion de bloqueo de fila en {relation!r}",
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_a_deactivation_in_flight_makes_the_confirmation_wait(
    registration_data: RegistrationData,
) -> None:
    """RS-9 orden A: la baja logica tiene la fila; la confirmacion espera y no confirma.

    En rojo (sin el ``FOR SHARE``) la confirmacion no esperaba a nadie: confirmaba la
    inscripcion con la baja todavia en vuelo y la prueba fallaba en la evidencia de espera.
    """
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(competitor_id=registration_data.competitor_a),
    )
    deactivation_ready = asyncio.Event()
    release_deactivation = asyncio.Event()

    async def _deactivate_and_hold() -> None:
        async with database.transaction():
            await sql_deactivate_competitor(
                competitor_id=registration_data.competitor_a,
                tenant_club_id=registration_data.tenant_a,
            )
            deactivation_ready.set()
            await release_deactivation.wait()

    holder = asyncio.create_task(_deactivate_and_hold())
    confirmation: asyncio.Task[TournamentRegistration] | None = None
    try:
        await asyncio.wait_for(deactivation_ready.wait(), WAITING_TIMEOUT_SECONDS)
        confirmation = asyncio.create_task(
            confirm_registration(registration_data.context_owner_a, registration.id)
        )
        # La confirmacion espera al bloqueo de la baja: sentencia de dominio esperando.
        await _wait_for_waiting_backend("%FOR SHARE%")
        await _wait_for_tuple_contention("competitors")
        assert not confirmation.done()
    finally:
        release_deactivation.set()
        await asyncio.gather(holder, return_exceptions=True)

    assert confirmation is not None
    with pytest.raises(CompetitorNotSelectableError):
        await confirmation

    after = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
    assert after is not None
    assert after.status == "DRAFT"
    assert [row["action"] for row in await audit_rows(registration.id)] == ["CREATE"]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_confirmation_in_flight_makes_the_deactivation_wait(
    registration_data: RegistrationData,
) -> None:
    """RS-9 orden B: la confirmacion tiene el ``FOR SHARE`` y la baja espera sin romper nada.

    Con la confirmacion detenida dentro de su transaccion (pax sobre la fila de la
    inscripcion, tomado **despues** de su bloqueo del competidor), la baja logica no puede
    avanzar; al terminar la confirmacion, la baja se aplica y el historico se conserva.
    """
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(competitor_id=registration_data.competitor_a),
    )
    registration_ready = asyncio.Event()
    release_registration = asyncio.Event()

    async def _hold_registration_row() -> None:
        async with database.transaction():
            await database.fetch_val(
                query=SELECT_REGISTRATION_ROW_FOR_UPDATE,
                values={"registration_id": registration.id},
            )
            registration_ready.set()
            await release_registration.wait()

    holder = asyncio.create_task(_hold_registration_row())
    confirmation: asyncio.Task[TournamentRegistration] | None = None
    deactivation: asyncio.Task[Competitor] | None = None
    try:
        await asyncio.wait_for(registration_ready.wait(), WAITING_TIMEOUT_SECONDS)
        confirmation = asyncio.create_task(
            confirm_registration(registration_data.context_owner_a, registration.id)
        )
        # La confirmacion esta dentro de su transaccion: espera al pax sobre la inscripcion.
        await _wait_for_waiting_backend("%UPDATE tournament_registrations%")
        deactivation = asyncio.create_task(
            deactivate_competitor(registration_data.context_owner_a, registration_data.competitor_a)
        )
        # La baja espera al bloqueo de fila que la confirmacion ya tiene.
        await _wait_for_waiting_backend("%UPDATE competitors%")
        await _wait_for_tuple_contention("competitors")
        assert not deactivation.done()
    finally:
        release_registration.set()
        await asyncio.gather(holder, return_exceptions=True)

    assert confirmation is not None and deactivation is not None
    confirmed = await confirmation
    assert confirmed.status == "CONFIRMED"
    await deactivation

    after = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
    assert after is not None
    assert after.status == "CONFIRMED"
    assert after.competitor_id == registration_data.competitor_a
    assert after.competitor_name_snapshot == confirmed.competitor_name_snapshot
    competitor = await get_competitor(
        registration_data.competitor_a, tenant_club_id=registration_data.tenant_a
    )
    assert competitor is not None
    assert competitor.active is False
    assert [row["action"] for row in await audit_rows(registration.id)] == ["CREATE", "CONFIRM"]
    assert (
        await count_current_registrations(
            tournament_id=registration_data.tournament_a,
            competitor_id=registration_data.competitor_a,
        )
        == 1
    )
