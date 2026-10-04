# pylint: disable=redefined-outer-name  # los fixtures de este modulo son los del laboratorio.
"""S3.3c-3 — transacciones independientes y bloqueos reales de PostgreSQL sobre la reserva.

Estas pruebas **no** usan ``sleep`` como mecanismo: sincronizan con ``asyncio.Event`` y esperan a
que PostgreSQL registre el bloqueo en ``pg_stat_activity`` (misma sonda que S2-bis). Cada
``database.transaction()`` toma su propia conexion del pool, asi que son backends distintos de
verdad.

Lo que se demuestra:

* dos reservas simultaneas de la misma clave **serializan** en el indice unico: la segunda espera y,
  cuando la primera confirma, choca con la restriccion;
* mientras la primera no confirma, la fila **no es visible** para nadie: no se puede afirmar que
  otra transaccion "vera IN_PROGRESS" (READ COMMITTED no muestra escrituras no confirmadas);
* si la primera revierte, la clave queda libre y la segunda reserva se queda con ella;
* la finalizacion tambien serializa: de dos finalizaciones concurrentes solo una escribe y la otra
  no encuentra su reserva pendiente.

No hay reintentos automaticos: la clasificacion de un choque de unicidad es decision del llamador
(protocolo HTTP de S3.3c-4).
"""

from __future__ import annotations

import asyncio

import asyncpg  # type: ignore[import-untyped]
import pytest
from heliclockter import datetime_utc

from bracket.database import database
from bracket.logic.idempotency import IDEMPOTENCY_STATE_IN_PROGRESS
from bracket.sql.idempotency import (
    sql_complete_idempotency_reservation,
    sql_insert_idempotency_reservation,
    violation_constraint_name,
)
from tests.integration_tests.idempotency_fixtures import (
    LAB_KEY,
    IdempotencyLab,
    lab_completion,
    lab_reservation,
    reservations_total,
    stored_reservation,
)
from tests.integration_tests.registration_fixtures import (
    WAITING_TIMEOUT_SECONDS,
    wait_for_waiting_backends,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

UNIQUE_KEY = "uq_domain_idempotency_keys_tenant_actor_key"
INSERT_PATTERN = "%INSERT INTO domain_idempotency_keys%"
UPDATE_PATTERN = "%UPDATE domain_idempotency_keys%"


class _Rollback(RuntimeError):
    """Marca la transaccion que debe revertir, sin ser un error del sistema."""


async def test_two_simultaneous_reservations_of_the_same_key_serialize(
    idempotency_lab: IdempotencyLab,
) -> None:
    lab = idempotency_lab
    reservation = lab_reservation(lab.club_a, lab.user_a)
    first_holds_the_key = asyncio.Event()
    release_first = asyncio.Event()

    async def _hold_the_key() -> None:
        async with database.transaction():
            await sql_insert_idempotency_reservation(reservation)
            first_holds_the_key.set()
            await release_first.wait()

    holder = asyncio.create_task(_hold_the_key())
    try:
        await asyncio.wait_for(first_holds_the_key.wait(), WAITING_TIMEOUT_SECONDS)

        contender = asyncio.create_task(sql_insert_idempotency_reservation(reservation))
        await wait_for_waiting_backends(INSERT_PATTERN)

        assert not contender.done(), "la segunda reserva no puede pasar por delante"
        assert await reservations_total() == 0, (
            "READ COMMITTED: la reserva sin confirmar no es visible para nadie"
        )

        release_first.set()
        await asyncio.gather(holder, return_exceptions=True)

        with pytest.raises(asyncpg.exceptions.UniqueViolationError) as error:
            await contender

        assert violation_constraint_name(error.value) == UNIQUE_KEY
        assert await reservations_total() == 1, "solo queda la reserva que confirmo"
    finally:
        release_first.set()
        await asyncio.gather(holder, return_exceptions=True)


async def test_a_rolled_back_reservation_lets_the_contender_take_the_key(
    idempotency_lab: IdempotencyLab,
) -> None:
    lab = idempotency_lab
    reservation = lab_reservation(lab.club_a, lab.user_a)
    first_holds_the_key = asyncio.Event()
    release_first = asyncio.Event()

    async def _hold_and_rollback() -> None:
        try:
            async with database.transaction():
                await sql_insert_idempotency_reservation(reservation)
                first_holds_the_key.set()
                await release_first.wait()
                raise _Rollback
        except _Rollback:
            return

    holder = asyncio.create_task(_hold_and_rollback())
    try:
        await asyncio.wait_for(first_holds_the_key.wait(), WAITING_TIMEOUT_SECONDS)
        contender = asyncio.create_task(sql_insert_idempotency_reservation(reservation))
        await wait_for_waiting_backends(INSERT_PATTERN)
        assert not contender.done()

        release_first.set()
        await asyncio.gather(holder, return_exceptions=True)

        stored = await asyncio.wait_for(contender, WAITING_TIMEOUT_SECONDS)
        assert stored.state == IDEMPOTENCY_STATE_IN_PROGRESS
        assert await reservations_total() == 1, "la clave quedo libre y el segundo la tomo"
    finally:
        release_first.set()
        await asyncio.gather(holder, return_exceptions=True)


async def test_only_one_of_two_simultaneous_completions_writes(
    idempotency_lab: IdempotencyLab,
) -> None:
    lab = idempotency_lab
    await sql_insert_idempotency_reservation(lab_reservation(lab.club_a, lab.user_a))
    first_completion_holds_the_row = asyncio.Event()
    release_first = asyncio.Event()

    async def _complete_and_hold() -> None:
        async with database.transaction():
            completed = await sql_complete_idempotency_reservation(
                tenant_club_id=lab.club_a,
                actor_user_id=lab.user_a,
                idempotency_key=LAB_KEY,
                completion=lab_completion(status=201, metadata={"resource_id": 7}),
            )
            assert completed is not None
            first_completion_holds_the_row.set()
            await release_first.wait()

    holder = asyncio.create_task(_complete_and_hold())
    try:
        await asyncio.wait_for(first_completion_holds_the_row.wait(), WAITING_TIMEOUT_SECONDS)

        contender = asyncio.create_task(
            sql_complete_idempotency_reservation(
                tenant_club_id=lab.club_a,
                actor_user_id=lab.user_a,
                idempotency_key=LAB_KEY,
                completion=lab_completion(status=500, metadata={"resource_id": 8}),
            )
        )
        await wait_for_waiting_backends(UPDATE_PATTERN)
        assert not contender.done(), "la segunda finalizacion espera el bloqueo de fila"

        release_first.set()
        await asyncio.gather(holder, return_exceptions=True)

        assert await asyncio.wait_for(contender, WAITING_TIMEOUT_SECONDS) is None, (
            "cuando la fila deja de estar pendiente, la segunda finalizacion no escribe nada"
        )

        row = await stored_reservation(lab.club_a, lab.user_a)
        assert row is not None
        assert row["response_status"] == 201, "gana la finalizacion que confirmo primero"
        assert row["response_body"] == {"resource_id": 7}
        assert await reservations_total() == 1
    finally:
        release_first.set()
        await asyncio.gather(holder, return_exceptions=True)
