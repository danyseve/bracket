# pylint: disable=redefined-outer-name  # `protocol_data` es un fixture de modulo.
"""S3.3c-4 — protocolo de idempotencia sobre PostgreSQL real (LAB ONLY).

Pruebas del coordinador transaccional: reserva y confirmacion atomicas, repeticion de una
respuesta perdida, conflicto determinista por huella distinta, concurrencia real (una sola
ejecucion efectiva), rollback cuando falla el dominio o la auditoria, revalidacion de identidad,
autorizacion y rol en la repeticion, caducidad conservadora, rotacion de HMAC, alcance por
tenant/actor, y orden de bloqueos sin deadlock.

Nada de esto publica rutas: el protocolo se ejercita como lo hara la capa HTTP de F3.

Se ejecuta contra ``bracket_test`` (PostgreSQL real de laboratorio). Los conteos se toman por
tenant, por torneo o por nombre, y siempre con una linea base previa a la llamada, para que lo
que se mida sea el efecto de la peticion y no los residuos de otras pruebas de la sesion.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from databases import Database
from heliclockter import datetime_utc

import bracket.logic.idempotency_protocol as protocol
from bracket.config import config
from bracket.database import database
from bracket.logic.competitors import (
    activate_competitor,
    create_competitor,
    deactivate_competitor,
)
from bracket.logic.idempotency import IdempotencyDecision, compute_request_fingerprint
from bracket.logic.idempotency_protocol import (
    IdempotencyActorInactiveError,
    IdempotencyKeyReusedError,
    IdempotencyLockTimeoutError,
    IdempotencyOperationInProgressError,
    IdempotencyResultExpiredError,
    IdempotencyRoleNotAllowedError,
    IdempotencyTenantNotAuthorizedError,
    OperationOutcome,
    ProtocolRequest,
    run_idempotent_operation,
)
from bracket.logic.registrations import (
    InvalidRegistrationDataError,
    InvalidRegistrationStateError,
    create_registration,
    reinstate_registration,
    withdraw_registration,
)
from bracket.models.db.domain import ActorContext
from bracket.sql.domain_reads import get_competitor
from bracket.sql.domain_writes import sql_insert_domain_change_log as original_audit
from bracket.sql.idempotency import (
    sql_complete_idempotency_reservation,
    sql_insert_idempotency_reservation,
)
from bracket.sql.users import update_user_active
from bracket.utils.id_types import (
    ClubId,
    CompetitorId,
    TournamentId,
    TournamentRegistrationId,
)
from tests.integration_tests.idempotency_fixtures import (
    SYNTHETIC_SECRET,
    lab_completion,
    lab_reservation,
    stored_reservation,
)
from tests.integration_tests.registration_fixtures import (
    RegistrationData,
    audit_rows,
    build_draft,
    registration_data_context,
    wait_for_waiting_backends,
)

LAB_PATH = "/clubs/1/competitors"
LAB_PAYLOAD = b'{"display_name":"Ana Protocolo"}'
BLOCKING_EVIDENCE_ENV = "S33C4_BLOCKING_EVIDENCE"
DEADLOCK_PASSES = 15
COMPETITOR_NAME = "Ana Protocolo"

COUNT_KEYS = "SELECT count(*) FROM domain_idempotency_keys WHERE tenant_club_id = :tenant_club_id"
COUNT_COMPETITORS = """
    SELECT count(*) FROM competitors
    WHERE managed_by_club_id = :tenant_club_id AND display_name = :name
"""
COUNT_REGISTRATIONS = "SELECT count(*) FROM tournament_registrations WHERE tournament_id = :id"
COUNT_AUDIT = """
    SELECT count(*) FROM domain_change_log
    WHERE tenant_club_id = :tenant_club_id AND entity = :entity AND action = :action
"""

SELECT_WAITING_BACKENDS = """
    SELECT pid, wait_event_type, wait_event, state, query
    FROM pg_stat_activity
    WHERE wait_event_type = 'Lock'
        AND pid <> pg_backend_pid()
        AND query LIKE :pattern
"""

DELETE_REGISTRATION_AUDIT = """
    DELETE FROM domain_change_log
    WHERE entity = 'tournament_registration' AND tenant_club_id = :tenant_club_id
"""
DELETE_REGISTRATIONS = "DELETE FROM tournament_registrations WHERE tournament_id = :tournament_id"
DELETE_COMPETITOR_AUDIT = """
    DELETE FROM domain_change_log
    WHERE entity = 'competitor' AND tenant_club_id = :tenant_club_id
"""
DELETE_NAME_HISTORY = """
    DELETE FROM competitors_name_history
    WHERE competitor_id IN (
        SELECT id FROM competitors
        WHERE managed_by_club_id = :tenant_club_id
            AND id <> :keep_competitor_id AND id <> :keep_other_competitor_id
    )
"""
DELETE_COMPETITORS = """
    DELETE FROM competitors
    WHERE managed_by_club_id = :tenant_club_id
        AND id <> :keep_competitor_id AND id <> :keep_other_competitor_id
"""
DELETE_KEYS = "DELETE FROM domain_idempotency_keys WHERE tenant_club_id = :tenant_club_id"


def lab_key(suffix: str) -> str:
    """Clave con forma valida y distinta por caso."""
    return f"protocol-lab-key-{suffix}"


def lab_request(*, key: str, payload: bytes = LAB_PAYLOAD, method: str = "POST") -> ProtocolRequest:
    """Peticion cruda tal y como la entregaria la capa HTTP."""
    return ProtocolRequest(method=method, path=LAB_PATH, payload=payload, idempotency_key=key)


async def count_keys(tenant_club_id: ClubId) -> int:
    return int(
        await database.fetch_val(query=COUNT_KEYS, values={"tenant_club_id": tenant_club_id})
    )


async def count_competitors(tenant_club_id: ClubId, name: str) -> int:
    return int(
        await database.fetch_val(
            query=COUNT_COMPETITORS, values={"tenant_club_id": tenant_club_id, "name": name}
        )
    )


async def count_registrations(tournament_id: TournamentId) -> int:
    return int(await database.fetch_val(query=COUNT_REGISTRATIONS, values={"id": tournament_id}))


async def count_audit_events(tenant_club_id: ClubId, entity: str, action: str) -> int:
    return int(
        await database.fetch_val(
            query=COUNT_AUDIT,
            values={"tenant_club_id": tenant_club_id, "entity": entity, "action": action},
        )
    )


async def delete_protocol_rows(
    tenants: tuple[ClubId, ...],
    tournament_id: TournamentId,
    keep_competitors: tuple[CompetitorId, ...],
) -> None:
    """Retira lo escrito por el protocolo y por las operaciones de dominio del propio test."""
    keep_competitor_id, keep_other_competitor_id = keep_competitors
    await database.execute(query=DELETE_REGISTRATION_AUDIT, values={"tenant_club_id": tenants[0]})
    await database.execute(query=DELETE_REGISTRATIONS, values={"tournament_id": tournament_id})
    keep = {
        "keep_competitor_id": keep_competitor_id,
        "keep_other_competitor_id": keep_other_competitor_id,
    }
    for tenant_club_id in tenants:
        await database.execute(
            query=DELETE_COMPETITOR_AUDIT, values={"tenant_club_id": tenant_club_id}
        )
        await database.execute(
            query=DELETE_NAME_HISTORY, values={"tenant_club_id": tenant_club_id, **keep}
        )
        await database.execute(
            query=DELETE_COMPETITORS, values={"tenant_club_id": tenant_club_id, **keep}
        )
        await database.execute(query=DELETE_KEYS, values={"tenant_club_id": tenant_club_id})


async def capture_blocking_evidence(pattern: str) -> list[dict[str, Any]]:
    """Evidencia del bloqueo real de PostgreSQL (se conserva si el lanzador lo pide)."""
    records = await database.fetch_all(query=SELECT_WAITING_BACKENDS, values={"pattern": pattern})
    captured = [dict(record._mapping) for record in records]
    destination = os.environ.get(BLOCKING_EVIDENCE_ENV)
    if destination:
        with Path(destination).open("a", encoding="utf-8") as evidence:
            evidence.write(json.dumps({"pattern": pattern, "waiting": captured}) + "\n")
    return captured


@pytest_asyncio.fixture(loop_scope="session")
async def protocol_data(
    monkeypatch: pytest.MonkeyPatch, reinit_database: Database
) -> AsyncIterator[RegistrationData]:
    """Datos reales de S3.1 (dos tenants, torneos, actores con relacion) y limpieza del protocolo.

    El secreto HMAC es **sintetico** y se inyecta por configuracion: nunca un secreto de despliegue.
    """
    monkeypatch.setattr(config, "idempotency_hmac_key", SYNTHETIC_SECRET)
    monkeypatch.setattr(config, "idempotency_hmac_key_version", "v1")

    async with registration_data_context(reinit_database) as data:
        async with AsyncExitStack() as stack:
            stack.push_async_callback(
                delete_protocol_rows,
                (data.tenant_a, data.tenant_b),
                data.tournament_a,
                (data.competitor_a, data.competitor_b),
            )
            yield data


def competitor_outcome(competitor_id: CompetitorId, *, status: int = 201) -> OperationOutcome:
    return OperationOutcome(
        response_status=status,
        metadata={"resource_id": int(competitor_id), "competitor_id": int(competitor_id)},
        resource_type="competitor",
        resource_id=int(competitor_id),
    )


def registration_outcome(registration: Any, *, status: int) -> OperationOutcome:
    return OperationOutcome(
        response_status=status,
        metadata={
            "resource_id": int(registration.id),
            "resource_type": "tournament_registration",
            "registration_status": registration.status,
        },
        resource_type="tournament_registration",
        resource_id=int(registration.id),
    )


def competitor_operation(
    context: ActorContext, *, display_name: str = COMPETITOR_NAME, status: int = 201
) -> Callable[[], Awaitable[OperationOutcome]]:
    """Operacion de dominio de referencia: O1 (alta de competidor)."""

    async def operation() -> OperationOutcome:
        competitor = await create_competitor(context, display_name=display_name)
        return competitor_outcome(competitor.id, status=status)

    return operation


def registration_operation(
    context: ActorContext, tournament_id: TournamentId, **overrides: object
) -> Callable[[], Awaitable[OperationOutcome]]:
    """Operacion de dominio de referencia: CREATE de inscripcion (sin identidad verificada)."""

    async def operation() -> OperationOutcome:
        registration = await create_registration(context, tournament_id, build_draft(**overrides))
        return registration_outcome(registration, status=201)

    return operation


def withdraw_operation(
    context: ActorContext, registration_id: TournamentRegistrationId
) -> Callable[[], Awaitable[OperationOutcome]]:
    async def operation() -> OperationOutcome:
        registration = await withdraw_registration(
            context, registration_id, reason_code="WITHDRAWAL_REQUEST"
        )
        return registration_outcome(registration, status=200)

    return operation


def reinstate_operation(
    context: ActorContext, registration_id: TournamentRegistrationId
) -> Callable[[], Awaitable[OperationOutcome]]:
    async def operation() -> OperationOutcome:
        registration = await reinstate_registration(
            context, registration_id, reason_code="MISTAKEN_WITHDRAWAL"
        )
        return registration_outcome(registration, status=200)

    return operation


def deactivate_operation(
    context: ActorContext, competitor_id: CompetitorId
) -> Callable[[], Awaitable[OperationOutcome]]:
    async def operation() -> OperationOutcome:
        await deactivate_competitor(context, competitor_id)
        return competitor_outcome(competitor_id, status=200)

    return operation


def activate_operation(
    context: ActorContext, competitor_id: CompetitorId
) -> Callable[[], Awaitable[OperationOutcome]]:
    async def operation() -> OperationOutcome:
        await activate_competitor(context, competitor_id)
        return competitor_outcome(competitor_id, status=200)

    return operation


@pytest.mark.asyncio(loop_scope="session")
async def test_primera_peticion_reserva_ejecuta_y_confirma(protocol_data: RegistrationData) -> None:
    key = lab_key("primera")
    baseline = await count_competitors(protocol_data.tenant_a, COMPETITOR_NAME)
    audit_baseline = await count_audit_events(protocol_data.tenant_a, "competitor", "CREATE")

    result = await run_idempotent_operation(
        context=protocol_data.context_owner_a,
        request=lab_request(key=key),
        operation=competitor_operation(protocol_data.context_owner_a),
    )

    assert result.decision is IdempotencyDecision.RESERVED
    assert result.response_status == 201
    assert result.response_body is not None
    assert result.resource_type == "competitor"

    stored = await stored_reservation(
        protocol_data.tenant_a, protocol_data.context_owner_a.actor_user_id, key
    )
    assert stored is not None
    assert stored["state"] == "COMPLETED"
    assert stored["response_status"] == 201
    assert stored["fingerprint_key_version"] == "v1"
    assert (
        stored["request_fingerprint"]
        == compute_request_fingerprint(
            method="POST", path=LAB_PATH, payload=LAB_PAYLOAD
        ).fingerprint
    )

    assert await count_competitors(protocol_data.tenant_a, COMPETITOR_NAME) == baseline + 1
    assert (
        await count_audit_events(protocol_data.tenant_a, "competitor", "CREATE")
        == audit_baseline + 1
    )
    assert await count_keys(protocol_data.tenant_a) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_misma_clave_misma_peticion_repite_sin_ejecutar(
    protocol_data: RegistrationData,
) -> None:
    key = lab_key("perdida")
    baseline = await count_competitors(protocol_data.tenant_a, COMPETITOR_NAME)
    audit_baseline = await count_audit_events(protocol_data.tenant_a, "competitor", "CREATE")
    operation = competitor_operation(protocol_data.context_owner_a)

    first = await run_idempotent_operation(
        context=protocol_data.context_owner_a, request=lab_request(key=key), operation=operation
    )
    replay = await run_idempotent_operation(
        context=protocol_data.context_owner_a, request=lab_request(key=key), operation=operation
    )

    assert first.decision is IdempotencyDecision.RESERVED
    assert replay.decision is IdempotencyDecision.REPLAY
    assert replay.response_status == first.response_status
    assert replay.response_body == first.response_body
    assert replay.resource_id == first.resource_id
    assert replay.reservation_id == first.reservation_id

    assert await count_competitors(protocol_data.tenant_a, COMPETITOR_NAME) == baseline + 1
    assert (
        await count_audit_events(protocol_data.tenant_a, "competitor", "CREATE")
        == audit_baseline + 1
    )
    assert await count_keys(protocol_data.tenant_a) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_misma_clave_otra_peticion_es_conflicto_determinista(
    protocol_data: RegistrationData,
) -> None:
    key = lab_key("conflicto")
    baseline = await count_competitors(protocol_data.tenant_a, COMPETITOR_NAME)
    context = protocol_data.context_owner_a

    await run_idempotent_operation(
        context=context, request=lab_request(key=key), operation=competitor_operation(context)
    )
    before = await stored_reservation(protocol_data.tenant_a, context.actor_user_id, key)

    with pytest.raises(IdempotencyKeyReusedError) as conflict:
        await run_idempotent_operation(
            context=context,
            request=lab_request(key=key, payload=b'{"display_name":"Otra Persona"}'),
            operation=competitor_operation(context, display_name="Otra Persona"),
        )

    assert conflict.value.code == "IDEMPOTENCY_KEY_REUSED"
    assert conflict.value.retryable is False
    after = await stored_reservation(protocol_data.tenant_a, context.actor_user_id, key)
    assert after == before
    assert await count_competitors(protocol_data.tenant_a, COMPETITOR_NAME) == baseline + 1
    assert await count_competitors(protocol_data.tenant_a, "Otra Persona") == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_peticiones_concurrentes_identicas_un_solo_efecto(
    protocol_data: RegistrationData,
) -> None:
    key = lab_key("concurrente")
    baseline = await count_competitors(protocol_data.tenant_a, COMPETITOR_NAME)
    audit_baseline = await count_audit_events(protocol_data.tenant_a, "competitor", "CREATE")
    context = protocol_data.context_owner_a
    gate = asyncio.Event()

    reserved = asyncio.Event()

    async def slow_operation() -> OperationOutcome:
        reserved.set()
        await gate.wait()
        competitor = await create_competitor(context, display_name=COMPETITOR_NAME)
        return competitor_outcome(competitor.id)

    first = asyncio.create_task(
        run_idempotent_operation(
            context=context, request=lab_request(key=key), operation=slow_operation
        )
    )
    await reserved.wait()
    second = asyncio.create_task(
        run_idempotent_operation(
            context=context, request=lab_request(key=key), operation=competitor_operation(context)
        )
    )
    await wait_for_waiting_backends("%INSERT INTO domain_idempotency_keys%")
    assert await capture_blocking_evidence("%INSERT INTO domain_idempotency_keys%") != []

    gate.set()
    results = await asyncio.gather(first, second)

    decisions = sorted(result.decision.value for result in results)
    assert decisions == ["REPLAY", "RESERVED"]
    winner = next(result for result in results if result.decision is IdempotencyDecision.RESERVED)
    loser = next(result for result in results if result.decision is IdempotencyDecision.REPLAY)
    assert loser.response_body == winner.response_body
    assert loser.response_status == winner.response_status

    assert await count_competitors(protocol_data.tenant_a, COMPETITOR_NAME) == baseline + 1
    assert (
        await count_audit_events(protocol_data.tenant_a, "competitor", "CREATE")
        == audit_baseline + 1
    )
    assert await count_keys(protocol_data.tenant_a) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_el_perdedor_espera_un_bloqueo_real_y_recibe_una_decision_coherente(
    protocol_data: RegistrationData, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(protocol, "LOCK_TIMEOUT_MS", 10_000)
    key = lab_key("bloqueo")
    context = protocol_data.context_owner_a
    baseline = await count_competitors(protocol_data.tenant_a, COMPETITOR_NAME)

    ready = asyncio.Event()
    release = asyncio.Event()

    async def hold_reservation() -> None:
        """Mantiene la reserva sin confirmar: bloqueo real sobre la clave unica."""
        async with database.transaction():
            await sql_insert_idempotency_reservation(
                lab_reservation(
                    protocol_data.tenant_a,
                    context.actor_user_id,
                    key=key,
                    method="POST",
                    path=LAB_PATH,
                    payload=LAB_PAYLOAD,
                )
            )
            ready.set()
            await release.wait()

    holder = asyncio.create_task(hold_reservation())
    await ready.wait()
    waiter = asyncio.create_task(
        run_idempotent_operation(
            context=context,
            request=lab_request(key=key),
            operation=competitor_operation(context),
        )
    )
    await wait_for_waiting_backends("%INSERT INTO domain_idempotency_keys%")
    waiting = await capture_blocking_evidence("%INSERT INTO domain_idempotency_keys%")
    assert waiting != []

    release.set()
    await holder
    with pytest.raises(IdempotencyOperationInProgressError) as decision:
        await waiter

    assert decision.value.code == "OPERATION_IN_PROGRESS"
    assert decision.value.retryable is True
    assert await count_competitors(protocol_data.tenant_a, COMPETITOR_NAME) == baseline
    assert await count_keys(protocol_data.tenant_a) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_fallo_de_dominio_revierte_reserva_y_efecto(protocol_data: RegistrationData) -> None:
    key = lab_key("fallo-dominio")
    context = protocol_data.context_owner_a
    baseline = await count_registrations(protocol_data.tournament_a)

    with pytest.raises(InvalidRegistrationDataError):
        await run_idempotent_operation(
            context=context,
            request=lab_request(key=key),
            operation=registration_operation(
                context, protocol_data.tournament_a, competitor_name_snapshot=None
            ),
        )

    assert await stored_reservation(protocol_data.tenant_a, context.actor_user_id, key) is None
    assert await count_registrations(protocol_data.tournament_a) == baseline

    retry = await run_idempotent_operation(
        context=context,
        request=lab_request(key=key),
        operation=registration_operation(
            context, protocol_data.tournament_a, competitor_name_snapshot="Sin Ficha"
        ),
    )
    assert retry.decision is IdempotencyDecision.RESERVED
    assert await count_registrations(protocol_data.tournament_a) == baseline + 1


@pytest.mark.asyncio(loop_scope="session")
async def test_fallo_de_auditoria_revierte_todo(
    protocol_data: RegistrationData, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = lab_key("fallo-auditoria")
    context = protocol_data.context_owner_a
    baseline = await count_competitors(protocol_data.tenant_a, COMPETITOR_NAME)

    async def broken_audit(*args: object, **kwargs: object) -> None:
        raise RuntimeError("fallo simulado de auditoria")

    monkeypatch.setattr("bracket.logic.competitors.sql_insert_domain_change_log", broken_audit)
    with pytest.raises(RuntimeError):
        await run_idempotent_operation(
            context=context,
            request=lab_request(key=key),
            operation=competitor_operation(context),
        )

    assert await count_competitors(protocol_data.tenant_a, COMPETITOR_NAME) == baseline
    assert await count_audit_events(protocol_data.tenant_a, "competitor", "CREATE") == 0
    assert await stored_reservation(protocol_data.tenant_a, context.actor_user_id, key) is None

    monkeypatch.setattr("bracket.logic.competitors.sql_insert_domain_change_log", original_audit)

    retry = await run_idempotent_operation(
        context=context, request=lab_request(key=key), operation=competitor_operation(context)
    )
    assert retry.decision is IdempotencyDecision.RESERVED
    assert await count_competitors(protocol_data.tenant_a, COMPETITOR_NAME) == baseline + 1


@pytest.mark.asyncio(loop_scope="session")
async def test_actor_desactivado_se_rechaza_antes_de_ejecutar(
    protocol_data: RegistrationData,
) -> None:
    key = lab_key("inactivo")
    context = protocol_data.context_owner_a
    baseline = await count_competitors(protocol_data.tenant_a, COMPETITOR_NAME)
    await update_user_active(context.actor_user_id, False)

    with pytest.raises(IdempotencyActorInactiveError) as rejected:
        await run_idempotent_operation(
            context=context, request=lab_request(key=key), operation=competitor_operation(context)
        )

    assert rejected.value.code == "ACTOR_INACTIVE"
    assert await count_competitors(protocol_data.tenant_a, COMPETITOR_NAME) == baseline
    assert await stored_reservation(protocol_data.tenant_a, context.actor_user_id, key) is None
    assert await count_keys(protocol_data.tenant_a) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_actor_ajeno_al_tenant_se_rechaza(protocol_data: RegistrationData) -> None:
    key = lab_key("ajeno")
    intruder = ActorContext(
        tenant_club_id=protocol_data.tenant_b,
        actor_user_id=protocol_data.context_owner_a.actor_user_id,
        actor_label="Owner A en tenant B",
    )

    with pytest.raises(IdempotencyTenantNotAuthorizedError) as rejected:
        await run_idempotent_operation(
            context=intruder,
            request=lab_request(key=key),
            operation=competitor_operation(intruder),
        )

    assert rejected.value.code == "TENANT_NOT_AUTHORIZED"
    assert await count_keys(protocol_data.tenant_b) == 0
    assert await stored_reservation(protocol_data.tenant_b, intruder.actor_user_id, key) is None


@pytest.mark.asyncio(loop_scope="session")
async def test_cambio_de_rol_no_devuelve_el_resultado_guardado(
    protocol_data: RegistrationData,
) -> None:
    key = lab_key("rol")
    context = protocol_data.context_collaborator_a

    first = await run_idempotent_operation(
        context=context, request=lab_request(key=key), operation=competitor_operation(context)
    )
    assert first.decision is IdempotencyDecision.RESERVED
    before = await stored_reservation(protocol_data.tenant_a, context.actor_user_id, key)

    with pytest.raises(IdempotencyRoleNotAllowedError) as rejected:
        await run_idempotent_operation(
            context=context,
            request=lab_request(key=key),
            operation=competitor_operation(context),
            require_owner=True,
        )

    assert rejected.value.code == "ROLE_NOT_ALLOWED"
    assert await stored_reservation(protocol_data.tenant_a, context.actor_user_id, key) == before


@pytest.mark.asyncio(loop_scope="session")
async def test_repeticion_tras_revocar_permisos_se_deniega(protocol_data: RegistrationData) -> None:
    key = lab_key("revocacion")
    context = protocol_data.context_owner_a

    first = await run_idempotent_operation(
        context=context, request=lab_request(key=key), operation=competitor_operation(context)
    )
    assert first.decision is IdempotencyDecision.RESERVED
    before = await stored_reservation(protocol_data.tenant_a, context.actor_user_id, key)

    await database.execute(
        query="DELETE FROM users_x_clubs WHERE user_id = :user_id AND club_id = :club_id",
        values={"user_id": context.actor_user_id, "club_id": protocol_data.tenant_a},
    )

    with pytest.raises(IdempotencyTenantNotAuthorizedError) as rejected:
        await run_idempotent_operation(
            context=context, request=lab_request(key=key), operation=competitor_operation(context)
        )

    assert rejected.value.code == "TENANT_NOT_AUTHORIZED"
    assert await stored_reservation(protocol_data.tenant_a, context.actor_user_id, key) == before


@pytest.mark.asyncio(loop_scope="session")
async def test_clave_caducada_se_rechaza_de_forma_conservadora(
    protocol_data: RegistrationData, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(protocol, "LOCK_TIMEOUT_MS", 10_000)
    key = lab_key("caducada")
    context = protocol_data.context_owner_a
    baseline = await count_competitors(protocol_data.tenant_a, COMPETITOR_NAME)
    created = datetime_utc.now() - timedelta(hours=30)

    await sql_insert_idempotency_reservation(
        lab_reservation(
            protocol_data.tenant_a,
            context.actor_user_id,
            key=key,
            created=created,
            lifetime=timedelta(hours=1),
        )
    )
    await sql_complete_idempotency_reservation(
        tenant_club_id=protocol_data.tenant_a,
        actor_user_id=context.actor_user_id,
        idempotency_key=key,
        completion=lab_completion(),
    )
    before = await stored_reservation(protocol_data.tenant_a, context.actor_user_id, key)
    assert before is not None
    assert before["state"] == "COMPLETED"

    with pytest.raises(IdempotencyResultExpiredError) as rejected:
        await run_idempotent_operation(
            context=context, request=lab_request(key=key), operation=competitor_operation(context)
        )

    assert rejected.value.code == "IDEMPOTENCY_RESULT_EXPIRED"
    assert rejected.value.retryable is False
    assert await count_competitors(protocol_data.tenant_a, COMPETITOR_NAME) == baseline
    assert await stored_reservation(protocol_data.tenant_a, context.actor_user_id, key) == before


@pytest.mark.asyncio(loop_scope="session")
async def test_hmac_rotado_es_conflicto_y_no_reejecuta(
    protocol_data: RegistrationData, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = lab_key("rotacion")
    context = protocol_data.context_owner_a
    baseline = await count_competitors(protocol_data.tenant_a, COMPETITOR_NAME)

    await run_idempotent_operation(
        context=context, request=lab_request(key=key), operation=competitor_operation(context)
    )
    before = await stored_reservation(protocol_data.tenant_a, context.actor_user_id, key)
    assert before is not None
    assert before["fingerprint_key_version"] == "v1"

    monkeypatch.setattr(config, "idempotency_hmac_key_version", "v2")
    with pytest.raises(IdempotencyKeyReusedError) as rejected:
        await run_idempotent_operation(
            context=context, request=lab_request(key=key), operation=competitor_operation(context)
        )

    assert rejected.value.code == "IDEMPOTENCY_KEY_REUSED"
    assert await count_competitors(protocol_data.tenant_a, COMPETITOR_NAME) == baseline + 1
    assert await stored_reservation(protocol_data.tenant_a, context.actor_user_id, key) == before


@pytest.mark.asyncio(loop_scope="session")
async def test_la_clave_es_por_tenant_y_actor(protocol_data: RegistrationData) -> None:
    key = lab_key("compartida")
    owner_a = protocol_data.context_owner_a
    owner_b = protocol_data.context_owner_b

    first = await run_idempotent_operation(
        context=owner_a, request=lab_request(key=key), operation=competitor_operation(owner_a)
    )
    second = await run_idempotent_operation(
        context=owner_b, request=lab_request(key=key), operation=competitor_operation(owner_b)
    )

    assert first.decision is IdempotencyDecision.RESERVED
    assert second.decision is IdempotencyDecision.RESERVED
    assert await count_keys(protocol_data.tenant_a) == 1
    assert await count_keys(protocol_data.tenant_b) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_create_sin_competidor_es_idempotente(protocol_data: RegistrationData) -> None:
    key = lab_key("create-sin-ficha")
    context = protocol_data.context_owner_a
    baseline = await count_registrations(protocol_data.tournament_a)
    audit_baseline = await count_audit_events(
        protocol_data.tenant_a, "tournament_registration", "CREATE"
    )
    operation = registration_operation(
        context, protocol_data.tournament_a, competitor_name_snapshot="Sin Ficha"
    )

    first = await run_idempotent_operation(
        context=context, request=lab_request(key=key), operation=operation
    )
    replay = await run_idempotent_operation(
        context=context, request=lab_request(key=key), operation=operation
    )

    assert first.decision is IdempotencyDecision.RESERVED
    assert replay.decision is IdempotencyDecision.REPLAY
    assert replay.resource_id == first.resource_id
    assert await count_registrations(protocol_data.tournament_a) == baseline + 1
    assert (
        await count_audit_events(protocol_data.tenant_a, "tournament_registration", "CREATE")
        == audit_baseline + 1
    )
    rows = await audit_rows(TournamentRegistrationId(first.resource_id or 0))
    assert [row["action"] for row in rows] == ["CREATE"]


@pytest.mark.asyncio(loop_scope="session")
async def test_reinstate_repetido_no_duplica_auditoria(protocol_data: RegistrationData) -> None:
    context = protocol_data.context_owner_a
    created = await run_idempotent_operation(
        context=context,
        request=lab_request(key=lab_key("reinstate-crea")),
        operation=registration_operation(
            context, protocol_data.tournament_a, competitor_name_snapshot="Sin Ficha"
        ),
    )
    registration_id = TournamentRegistrationId(created.resource_id or 0)

    withdrawn = await run_idempotent_operation(
        context=context,
        request=lab_request(key=lab_key("reinstate-baja")),
        operation=withdraw_operation(context, registration_id),
    )
    assert withdrawn.response_status == 200

    key = lab_key("reinstate-vuelve")
    first = await run_idempotent_operation(
        context=context,
        request=lab_request(key=key),
        operation=reinstate_operation(context, registration_id),
    )
    replay = await run_idempotent_operation(
        context=context,
        request=lab_request(key=key),
        operation=reinstate_operation(context, registration_id),
    )

    assert first.decision is IdempotencyDecision.RESERVED
    assert replay.decision is IdempotencyDecision.REPLAY
    assert replay.response_body == first.response_body
    rows = await audit_rows(registration_id)
    assert [row["action"] for row in rows] == ["CREATE", "WITHDRAW", "REINSTATE"]
    assert (
        await count_audit_events(protocol_data.tenant_a, "tournament_registration", "REINSTATE")
        == 1
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_reinstate_con_otra_clave_no_deja_reserva_huerfana(
    protocol_data: RegistrationData,
) -> None:
    context = protocol_data.context_owner_a
    created = await run_idempotent_operation(
        context=context,
        request=lab_request(key=lab_key("huerfana-crea")),
        operation=registration_operation(
            context, protocol_data.tournament_a, competitor_name_snapshot="Sin Ficha"
        ),
    )
    registration_id = TournamentRegistrationId(created.resource_id or 0)
    await run_idempotent_operation(
        context=context,
        request=lab_request(key=lab_key("huerfana-baja")),
        operation=withdraw_operation(context, registration_id),
    )
    await run_idempotent_operation(
        context=context,
        request=lab_request(key=lab_key("huerfana-vuelve")),
        operation=reinstate_operation(context, registration_id),
    )

    key = lab_key("huerfana-extra")
    with pytest.raises(InvalidRegistrationStateError):
        await run_idempotent_operation(
            context=context,
            request=lab_request(key=key),
            operation=reinstate_operation(context, registration_id),
        )

    assert await stored_reservation(protocol_data.tenant_a, context.actor_user_id, key) is None
    assert (
        await count_audit_events(protocol_data.tenant_a, "tournament_registration", "REINSTATE")
        == 1
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_lock_timeout_no_deja_efecto_ni_reserva(
    protocol_data: RegistrationData, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(protocol, "LOCK_TIMEOUT_MS", 250)
    key = lab_key("timeout")
    context = protocol_data.context_owner_a
    ready = asyncio.Event()
    release = asyncio.Event()

    async def hold_competitor_row() -> None:
        async with database.transaction():
            await database.fetch_one(
                query="SELECT id FROM competitors WHERE id = :competitor_id FOR UPDATE",
                values={"competitor_id": protocol_data.competitor_a},
            )
            ready.set()
            await release.wait()

    holder = asyncio.create_task(hold_competitor_row())
    await ready.wait()
    try:
        with pytest.raises(IdempotencyLockTimeoutError) as rejected:
            await run_idempotent_operation(
                context=context,
                request=lab_request(key=key),
                operation=deactivate_operation(context, protocol_data.competitor_a),
            )
        assert rejected.value.code == "LOCK_TIMEOUT"
        assert rejected.value.retryable is True
    finally:
        release.set()
        await holder

    competitor = await get_competitor(
        protocol_data.competitor_a, tenant_club_id=protocol_data.tenant_a
    )
    assert competitor is not None
    assert competitor.active is True
    assert await stored_reservation(protocol_data.tenant_a, context.actor_user_id, key) is None


@pytest.mark.asyncio(loop_scope="session")
async def test_orden_de_bloqueo_sin_deadlock_en_quince_pasadas(
    protocol_data: RegistrationData,
) -> None:
    context = protocol_data.context_owner_a
    competitor_id = protocol_data.competitor_a
    audit_baseline = await count_audit_events(protocol_data.tenant_a, "competitor", "ACTIVATE")

    for index in range(DEADLOCK_PASSES):
        await deactivate_competitor(context, competitor_id)
        left = run_idempotent_operation(
            context=context,
            request=lab_request(key=lab_key(f"orden-{index}-a")),
            operation=activate_operation(context, competitor_id),
        )
        right = run_idempotent_operation(
            context=context,
            request=lab_request(key=lab_key(f"orden-{index}-b")),
            operation=activate_operation(context, competitor_id),
        )
        results = await asyncio.wait_for(asyncio.gather(left, right), timeout=10)
        assert all(result.decision is IdempotencyDecision.RESERVED for result in results)

    assert await count_keys(protocol_data.tenant_a) == 2 * DEADLOCK_PASSES
    assert (
        await count_audit_events(protocol_data.tenant_a, "competitor", "ACTIVATE")
        == audit_baseline + DEADLOCK_PASSES
    )
