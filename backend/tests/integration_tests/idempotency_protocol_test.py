# pylint: disable=redefined-outer-name  # `protocol_data` es un fixture de modulo.
"""S3.3c-4 — protocolo de idempotencia sobre PostgreSQL real (LAB ONLY).

Pruebas del coordinador transaccional: reserva, repeticion, conflicto, concurrencia, retirada de
permisos, caducidad y rotacion de la clave HMAC. Todo contra ``bracket_test``, sin mocks del control
transaccional: las transacciones, los bloqueos y los conflictos de clave unica son los de PostgreSQL.

Las sondas ``wait_for_waiting_backends`` esperan a que PostgreSQL registre el bloqueo real en
``pg_stat_activity`` (no son ``sleep``) y dejan la evidencia en el fichero indicado por
``S33C4_BLOCKING_EVIDENCE`` cuando esa variable esta definida.
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
from bracket.logic.competitors import create_competitor, deactivate_competitor, update_competitor_display_name
from bracket.logic.idempotency import IdempotencyDecision
from bracket.logic.idempotency_protocol import (
    IdempotencyActorInactiveError,
    IdempotencyKeyReusedError,
    IdempotencyLockTimeoutError,
    IdempotencyProtocolError,
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
from bracket.models.db.domain import ActorContext, CompetitorBasicDataUpdate
from bracket.sql.idempotency import sql_insert_idempotency_reservation
from bracket.sql.users import update_user_active
from bracket.utils.id_types import (
    ClubId,
    CompetitorId,
    TournamentId,
    TournamentRegistrationId,
)
from tests.integration_tests.idempotency_fixtures import (
    SYNTHETIC_SECRET,
    lab_reservation,
    reservations_total,
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
LAB_PAYLOAD = b'{"display_name":"Ana"}'
BLOCKING_EVIDENCE_ENV = "S33C4_BLOCKING_EVIDENCE"
DEADLOCK_PASSES = 15

SELECT_WAITING_BACKENDS = """
    SELECT pid, wait_event_type, wait_event, state, query
    FROM pg_stat_activity
    WHERE wait_event_type = 'Lock'
        AND pid <> pg_backend_pid()
        AND query LIKE :pattern
"""

DELETE_REGISTRATION_AUDIT = """
    DELETE FROM domain_change_log
    WHERE entity = 'tournament_registration'
        AND entity_id IN (
            SELECT id FROM tournament_registrations WHERE tournament_id = :t
        )
"""
DELETE_COMPETITOR_AUDIT = """
    DELETE FROM domain_change_log
    WHERE entity = 'competitor' AND tenant_club_id = :c
"""
DELETE_NAME_HISTORY = """
    DELETE FROM competitors_name_history
    WHERE competitor_id IN (SELECT id FROM competitors WHERE managed_by_club_id = :t)
"""


def lab_key(suffix: str) -> str:
    """Clave con forma valida y unica por caso."""
    return f"protocol-lab-key-{suffix}"


def lab_request(*, key: str, payload: bytes = LAB_PAYLOAD, method: str = "POST") -> ProtocolRequest:
    return ProtocolRequest(method=method, path=LAB_PATH, payload=payload, idempotency_key=key)


async def delete_protocol_rows(tenant_club_id: ClubId, tournament_id: TournamentId) -> None:
    """Retira lo que el protocolo y las operaciones de dominio hayan escrito en el tenant."""
    values = {"t": tournament_id, "c": tenant_club_id}
    await database.execute(query=DELETE_REGISTRATION_AUDIT, values=values)
    await database.execute(
        query="DELETE FROM tournament_registrations WHERE tournament_id = :t", values=values
    )
    await database.execute(query=DELETE_COMPETITOR_AUDIT, values=values)
    await database.execute(query=DELETE_NAME_HISTORY, values=values)
    await database.execute(
        query="DELETE FROM competitors WHERE managed_by_club_id = :c", values=values
    )
    await database.execute(
        query="DELETE FROM domain_idempotency_keys WHERE tenant_club_id = :c", values=values
    )


async def capture_blocking_evidence(pattern: str) -> list[dict[str, Any]]:
    """Evidencia del bloqueo real de PostgreSQL, si la ejecucion pidio conservarla."""
    records = await database.fetch_all(
        query=SELECT_WAITING_BACKENDS, values={"pattern": pattern}
    )
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
            stack.push_async_callback(delete_protocol_rows, data.tenant_a, data.tournament_a)
            yield data


def competitor_outcome(competitor_id: CompetitorId, *, status: int = 201) -> OperationOutcome:
    return OperationOutcome(
        response_status=status,
        metadata={"resource_id": int(competitor_id), "resource_type": "competitor"},
        resource_type="competitor",
        resource_id=int(competitor_id),
    )


def competitor_operation(
    context: ActorContext, *, display_name: str, gate: asyncio.Event | None = None
) -> Callable[[], Awaitable[OperationOutcome]]:
    """Operacion O1-O2 real, igual que la usaria un endpoint: sin cambios de firma ni semantica."""

    async def operation() -> OperationOutcome:
        if gate is not None:
            await gate.wait()
        competitor = await create_competitor(context, display_name=display_name)
        return competitor_outcome(CompetitorId(competitor.id))

    return operation


async def _hold_competitor_row(
    competitor_id: CompetitorId, ready: asyncio.Event, release: asyncio.Event
) -> None:
    """Mantiene un bloqueo de fila en el competidor desde otra conexion."""
    async with database.transaction():
        await database.fetch_one(
            query="SELECT id FROM competitors WHERE id = :c FOR UPDATE",
            values={"c": competitor_id},
        )
        ready.set()
        await release.wait()


async def test_primera_peticion_reserva_ejecuta_y_confirma(protocol_data: RegistrationData) -> None:
    key = lab_key("primera")
    result = await run_idempotent_operation(
        context=protocol_data.context_owner_a,
        request=lab_request(key=key),
        operation=competitor_operation(protocol_data.context_owner_a, display_name="Ana Primera"),
    )

    assert result.decision is IdempotencyDecision.RESERVED
    assert result.response_status == 201
    assert result.resource_id is not None
    stored = await stored_reservation(protocol_data.tenant_a, protocol_data.context_owner_a.actor_user_id, key)
    assert stored is not None
    assert stored["state"] == "COMPLETED"
    assert stored["completed_at"] is not None
    assert await reservations_total() == 1


async def test_misma_clave_misma_peticion_repite_sin_ejecutar(protocol_data: RegistrationData) -> None:
    key = lab_key("replay")
    context = protocol_data.context_owner_a
    first = await run_idempotent_operation(
        context=context,
        request=lab_request(key=key),
        operation=competitor_operation(context, display_name="Ana Replay"),
    )
    second = await run_idempotent_operation(
        context=context,
        request=lab_request(key=key),
        operation=competitor_operation(context, display_name="Ana Replay"),
    )

    assert first.decision is IdempotencyDecision.RESERVED
    assert second.decision is IdempotencyDecision.REPLAY
    # Respuesta perdida: la repeticion devuelve el resultado confirmado, identico.
    assert second.response_status == first.response_status
    assert second.response_body == first.response_body
    assert second.resource_id == first.resource_id
    assert await reservations_total() == 1
    assert await _competitors_in_tenant(protocol_data.tenant_a) == 1


async def test_misma_clave_otra_peticion_es_conflicto_determinista(
    protocol_data: RegistrationData,
) -> None:
    key = lab_key("conflicto")
    context = protocol_data.context_owner_a
    await run_idempotent_operation(
        context=context,
        request=lab_request(key=key),
        operation=competitor_operation(context, display_name="Ana Conflicto"),
    )
    before = await stored_reservation(protocol_data.tenant_a, context.actor_user_id, key)

    with pytest.raises(IdempotencyKeyReusedError) as rejected:
        await run_idempotent_operation(
            context=context,
            request=lab_request(key=key, payload=b'{"display_name":"Otra"}'),
            operation=competitor_operation(context, display_name="Otra"),
        )

    assert rejected.value.code == "IDEMPOTENCY_KEY_REUSED"
    assert await stored_reservation(protocol_data.tenant_a, context.actor_user_id, key) == before
    assert await _competitors_in_tenant(protocol_data.tenant_a) == 1


async def test_peticiones_concurrentes_identicas_un_solo_efecto(
    protocol_data: RegistrationData, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Con el bloqueo observado a proposito, el techo de espera se amplia para no depender del reloj.
    monkeypatch.setattr(protocol, "LOCK_TIMEOUT_MS", 10_000)
    key = lab_key("concurrente")
    context = protocol_data.context_owner_a
    gate = asyncio.Event()

    winner = asyncio.create_task(
        run_idempotent_operation(
            context=context,
            request=lab_request(key=key),
            operation=competitor_operation(context, display_name="Ana Concurrente", gate=gate),
        )
    )
    await wait_for_waiting_backends("%INSERT INTO domain_idempotency_keys%")
    evidence = await capture_blocking_evidence("%domain_idempotency_keys%")
    loser = asyncio.create_task(
        run_idempotent_operation(
            context=context,
            request=lab_request(key=key),
            operation=competitor_operation(context, display_name="Ana Concurrente"),
        )
    )
    gate.set()
    results = await asyncio.gather(winner, loser)

    decisions = sorted(result.decision.value for result in results)
    assert decisions == ["REPLAY", "RESERVED"], decisions
    executed = next(result for result in results if result.decision is IdempotencyDecision.RESERVED)
    replayed = next(result for result in results if result.decision is IdempotencyDecision.REPLAY)
    assert replayed.response_body == executed.response_body
    assert await reservations_total() == 1
    assert await _competitors_in_tenant(protocol_data.tenant_a) == 1
    assert await _audit_events(protocol_data.tenant_a, "competitor") == 1
    if os.environ.get(BLOCKING_EVIDENCE_ENV):
        assert evidence, "no se pudo documentar el bloqueo real de PostgreSQL"


async def test_el_perdedor_espera_un_bloqueo_de_clave_unica(
    protocol_data: RegistrationData, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La espera es un bloqueo real de ``domain_idempotency_keys``, no una espera activa."""
    monkeypatch.setattr(protocol, "LOCK_TIMEOUT_MS", 10_000)
    key = lab_key("bloqueo")
    context = protocol_data.context_owner_a
    reservation = lab_reservation(context.tenant_club_id, context.actor_user_id, key=key)

    async with database.transaction():
        await sql_insert_idempotency_reservation(reservation)
        waiter = asyncio.create_task(
            run_idempotent_operation(
                context=context,
                request=lab_request(key=key),
                operation=competitor_operation(context, display_name="Ana Bloqueo"),
            )
        )
        await wait_for_waiting_backends("%INSERT INTO domain_idempotency_keys%")
        await capture_blocking_evidence("%domain_idempotency_keys%")

    with pytest.raises(IdempotencyProtocolError) as rejected:
        await waiter

    # La reserva ajena sigue IN_PROGRESS: el segundo proceso obtiene una decision coherente.
    assert rejected.value.code == "OPERATION_IN_PROGRESS"
    assert await _competitors_in_tenant(protocol_data.tenant_a) == 0


async def test_fallo_de_dominio_revierte_reserva_y_efecto(protocol_data: RegistrationData) -> None:
    key = lab_key("fallo-dominio")
    context = protocol_data.context_owner_a

    async def failing_operation() -> OperationOutcome:
        await create_competitor(context, display_name="Ana Fallo")
        raise InvalidRegistrationDataError("fallo de dominio posterior a la escritura")

    with pytest.raises(InvalidRegistrationDataError):
        await run_idempotent_operation(
            context=context, request=lab_request(key=key), operation=failing_operation
        )

    # Nada sobrevive: ni competidor, ni reserva huerfana, ni evento de auditoria.
    assert await _competitors_in_tenant(protocol_data.tenant_a) == 0
    assert await stored_reservation(protocol_data.tenant_a, context.actor_user_id, key) is None
    assert await reservations_total() == 0
    assert await _audit_events(protocol_data.tenant_a, "competitor") == 0


async def test_fallo_de_auditoria_revierte_todo(
    protocol_data: RegistrationData, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = lab_key("fallo-auditoria")
    context = protocol_data.context_owner_a

    async def failing_audit(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("fallo simulado al escribir la auditoria")

    monkeypatch.setattr("bracket.logic.competitors.sql_insert_domain_change_log", failing_audit)
    with pytest.raises(RuntimeError):
        await run_idempotent_operation(
            context=context,
            request=lab_request(key=key),
            operation=competitor_operation(context, display_name="Ana Auditoria"),
        )

    assert await _competitors_in_tenant(protocol_data.tenant_a) == 0
    assert await stored_reservation(protocol_data.tenant_a, context.actor_user_id, key) is None
    assert await _audit_events(protocol_data.tenant_a, "competitor") == 0


async def test_actor_desactivado_se_rechaza_antes_de_ejecutar(
    protocol_data: RegistrationData,
) -> None:
    context = protocol_data.context_owner_a
    await update_user_active(context.actor_user_id, active=False)
    try:
        with pytest.raises(IdempotencyActorInactiveError) as rejected:
            await run_idempotent_operation(
                context=context,
                request=lab_request(key=lab_key("inactivo")),
                operation=competitor_operation(context, display_name="Ana Inactiva"),
            )
        assert rejected.value.code == "ACTOR_INACTIVE"
        assert await _competitors_in_tenant(protocol_data.tenant_a) == 0
        assert await reservations_total() == 0
    finally:
        await update_user_active(context.actor_user_id, active=True)


async def test_actor_ajeno_al_tenant_se_rechaza(protocol_data: RegistrationData) -> None:
    with pytest.raises(IdempotencyTenantNotAuthorizedError) as rejected:
        await run_idempotent_operation(
            context=protocol_data.context_outsider_a,
            request=lab_request(key=lab_key("ajeno")),
            operation=competitor_operation(protocol_data.context_outsider_a, display_name="Ajeno"),
        )

    assert rejected.value.code == "TENANT_NOT_AUTHORIZED"
    assert await _competitors_in_tenant(protocol_data.tenant_a) == 0
    assert await reservations_total() == 0


async def test_cambio_de_rol_no_devuelve_el_resultado_guardado(
    protocol_data: RegistrationData,
) -> None:
    """La respuesta almacenada nunca sustituye a la autorizacion actual."""
    key = lab_key("rol")
    collaborator = protocol_data.context_collaborator_a
    await run_idempotent_operation(
        context=collaborator,
        request=lab_request(key=key),
        operation=competitor_operation(collaborator, display_name="Ana Rol"),
    )

    with pytest.raises(IdempotencyRoleNotAllowedError) as rejected:
        await run_idempotent_operation(
            context=collaborator,
            request=lab_request(key=key),
            operation=competitor_operation(collaborator, display_name="Ana Rol"),
            require_owner=True,
        )

    assert rejected.value.code == "ROLE_NOT_ALLOWED"
    assert await _competitors_in_tenant(protocol_data.tenant_a) == 1


async def test_repeticion_tras_revocar_permisos_se_deniega(protocol_data: RegistrationData) -> None:
    key = lab_key("revocacion")
    context = protocol_data.context_owner_a
    await run_idempotent_operation(
        context=context,
        request=lab_request(key=key),
        operation=competitor_operation(context, display_name="Ana Revocada"),
    )

    await database.execute(
        query="DELETE FROM users_x_clubs WHERE user_id = :u AND club_id = :c",
        values={"u": context.actor_user_id, "c": context.tenant_club_id},
    )
    with pytest.raises(IdempotencyTenantNotAuthorizedError):
        await run_idempotent_operation(
            context=context,
            request=lab_request(key=key),
            operation=competitor_operation(context, display_name="Ana Revocada"),
        )

    # La reserva confirmada no se toca: la denegacion es de autorizacion, no de estado.
    stored = await stored_reservation(protocol_data.tenant_a, context.actor_user_id, key)
    assert stored is not None and stored["state"] == "COMPLETED"


async def test_clave_caducada_se_rechaza_de_forma_conservadora(
    protocol_data: RegistrationData,
) -> None:
    key = lab_key("caducada")
    context = protocol_data.context_owner_a
    # Reserva real que vencio hace 29 h: la caducidad es retencion, no permiso de reejecucion.
    expired = lab_reservation(
        context.tenant_club_id,
        context.actor_user_id,
        key=key,
        created=datetime_utc.now() - timedelta(hours=30),
        lifetime=timedelta(hours=1),
    )
    await sql_insert_idempotency_reservation(expired)

    with pytest.raises(IdempotencyResultExpiredError) as rejected:
        await run_idempotent_operation(
            context=context,
            request=lab_request(key=key),
            operation=competitor_operation(context, display_name="Ana Caducada"),
        )

    assert rejected.value.code == "IDEMPOTENCY_RESULT_EXPIRED"
    # No se reejecuta y la reserva caducada no se pisa ni se libera.
    assert await _competitors_in_tenant(protocol_data.tenant_a) == 0
    stored = await stored_reservation(protocol_data.tenant_a, context.actor_user_id, key)
    assert stored is not None
    assert stored["state"] == "IN_PROGRESS"


async def test_hmac_rotado_es_conflicto_y_no_reejecuta(
    protocol_data: RegistrationData, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = lab_key("rotacion")
    context = protocol_data.context_owner_a
    await run_idempotent_operation(
        context=context,
        request=lab_request(key=key),
        operation=competitor_operation(context, display_name="Ana Rotada"),
    )

    monkeypatch.setattr(config, "idempotency_hmac_key_version", "v2")
    with pytest.raises(IdempotencyKeyReusedError):
        await run_idempotent_operation(
            context=context,
            request=lab_request(key=key),
            operation=competitor_operation(context, display_name="Ana Rotada"),
        )
    assert await _competitors_in_tenant(protocol_data.tenant_a) == 1


async def test_la_clave_es_por_tenant_y_actor(protocol_data: RegistrationData) -> None:
    key = lab_key("aislamiento")
    first = await run_idempotent_operation(
        context=protocol_data.context_owner_a,
        request=lab_request(key=key),
        operation=competitor_operation(protocol_data.context_owner_a, display_name="Ana A"),
    )
    second = await run_idempotent_operation(
        context=protocol_data.context_owner_b,
        request=lab_request(key=key),
        operation=competitor_operation(protocol_data.context_owner_b, display_name="Ana B"),
    )

    assert first.decision is IdempotencyDecision.RESERVED
    assert second.decision is IdempotencyDecision.RESERVED
    assert await reservations_total() == 2
    assert await _competitors_in_tenant(protocol_data.tenant_a) == 1
    assert await _competitors_in_tenant(protocol_data.tenant_b) == 1


async def test_create_sin_competidor_es_idempotente(protocol_data: RegistrationData) -> None:
    key = lab_key("create-sin-competidor")
    context = protocol_data.context_collaborator_a
    payload = b'{"representation":"INDEPENDENT"}'

    async def operation() -> OperationOutcome:
        registration = await create_registration(
            context, protocol_data.tournament_a, build_draft()
        )
        return OperationOutcome(
            response_status=201,
            metadata={"resource_id": int(registration.id), "registration_status": "DRAFT"},
            resource_type="tournament_registration",
            resource_id=int(registration.id),
        )

    first = await run_idempotent_operation(
        context=context, request=lab_request(key=key, payload=payload), operation=operation
    )
    second = await run_idempotent_operation(
        context=context, request=lab_request(key=key, payload=payload), operation=operation
    )

    assert first.decision is IdempotencyDecision.RESERVED
    assert second.decision is IdempotencyDecision.REPLAY
    assert await _registrations_in_tournament(protocol_data.tournament_a) == 1
    assert await _audit_events(protocol_data.tenant_a, "tournament_registration") == 1


async def test_reinstate_repetido_no_duplica_auditoria(protocol_data: RegistrationData) -> None:
    context = protocol_data.context_owner_a
    tournament_id = protocol_data.tournament_a

    async def create_operation() -> OperationOutcome:
        registration = await create_registration(context, tournament_id, build_draft())
        return OperationOutcome(
            response_status=201,
            metadata={"resource_id": int(registration.id), "registration_status": "DRAFT"},
            resource_type="tournament_registration",
            resource_id=int(registration.id),
        )

    created = await run_idempotent_operation(
        context=context,
        request=lab_request(key=lab_key("reinstate-create")),
        operation=create_operation,
    )
    assert created.resource_id is not None
    registration_id = TournamentRegistrationId(created.resource_id)

    async def withdraw_operation() -> OperationOutcome:
        await withdraw_registration(
            context, registration_id, reason_code="WITHDRAWAL_REQUEST"
        )
        return OperationOutcome(
            response_status=200,
            metadata={"resource_id": registration_id, "registration_status": "WITHDRAWN"},
            resource_type="tournament_registration",
            resource_id=registration_id,
        )

    await run_idempotent_operation(
        context=context,
        request=lab_request(key=lab_key("reinstate-withdraw"), method="DELETE"),
        operation=withdraw_operation,
    )

    async def reinstate_operation() -> OperationOutcome:
        await reinstate_registration(
            context, registration_id, reason_code="MISTAKEN_WITHDRAWAL"
        )
        return OperationOutcome(
            response_status=200,
            metadata={"resource_id": registration_id, "registration_status": "CONFIRMED"},
            resource_type="tournament_registration",
            resource_id=registration_id,
        )

    request = lab_request(key=lab_key("reinstate"), method="PATCH")
    first = await run_idempotent_operation(
        context=context, request=request, operation=reinstate_operation
    )
    second = await run_idempotent_operation(
        context=context, request=request, operation=reinstate_operation
    )

    assert first.decision is IdempotencyDecision.RESERVED
    assert second.decision is IdempotencyDecision.REPLAY
    events = [
        row
        for row in await audit_rows(registration_id)
        if str(row.get("action")) == "REINSTATE"
    ]
    assert len(events) == 1, events


async def test_reinstate_con_otra_clave_no_deja_reserva_huerfana(
    protocol_data: RegistrationData,
) -> None:
    context = protocol_data.context_owner_a
    tournament_id = protocol_data.tournament_a
    registration = await create_registration(context, tournament_id, build_draft())
    await withdraw_registration(context, registration.id, reason_code="WITHDRAWAL_REQUEST")
    await reinstate_registration(context, registration.id, reason_code="MISTAKEN_WITHDRAWAL")

    key = lab_key("reinstate-repetido-otra-clave")

    async def operation() -> OperationOutcome:
        await reinstate_registration(
            context, registration.id, reason_code="MISTAKEN_WITHDRAWAL"
        )
        return OperationOutcome(
            response_status=200,
            metadata={"resource_id": int(registration.id), "registration_status": "CONFIRMED"},
            resource_type="tournament_registration",
            resource_id=int(registration.id),
        )

    with pytest.raises(InvalidRegistrationStateError):
        await run_idempotent_operation(
            context=context, request=lab_request(key=key, method="PATCH"), operation=operation
        )

    assert await stored_reservation(protocol_data.tenant_a, context.actor_user_id, key) is None


async def test_lock_timeout_no_deja_efecto_ni_reserva(
    protocol_data: RegistrationData, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(protocol, "LOCK_TIMEOUT_MS", 250)
    key = lab_key("lock-timeout")
    context = protocol_data.context_owner_a
    ready = asyncio.Event()
    release = asyncio.Event()
    holder = asyncio.create_task(
        _hold_competitor_row(CompetitorId(protocol_data.competitor_a), ready, release)
    )
    await ready.wait()

    try:
        with pytest.raises(IdempotencyLockTimeoutError) as rejected:
            await run_idempotent_operation(
                context=context,
                request=lab_request(key=key),
                operation=_deactivate_operation(context, CompetitorId(protocol_data.competitor_a)),
            )
        assert rejected.value.code == "LOCK_TIMEOUT"
        assert rejected.value.retryable is True
        assert await stored_reservation(protocol_data.tenant_a, context.actor_user_id, key) is None
        active = await database.fetch_val(
            query="SELECT active FROM competitors WHERE id = :c",
            values={"c": protocol_data.competitor_a},
        )
        assert active is True
    finally:
        release.set()
        await holder


async def test_orden_de_bloqueo_sin_deadlock_15_pasadas(protocol_data: RegistrationData) -> None:
    """Idempotency-Key -> competitor: 15 pasadas seguidas sin deadlock ni espera agotada."""
    context = protocol_data.context_owner_a
    competitor_id = CompetitorId(protocol_data.competitor_a)

    for attempt in range(DEADLOCK_PASSES):
        left = asyncio.create_task(
            run_idempotent_operation(
                context=context,
                request=lab_request(key=lab_key(f"orden-{attempt}-a")),
                operation=_rename_operation(context, competitor_id, f"Ana {attempt} A"),
            )
        )
        right = asyncio.create_task(
            run_idempotent_operation(
                context=context,
                request=lab_request(key=lab_key(f"orden-{attempt}-b")),
                operation=_rename_operation(context, competitor_id, f"Ana {attempt} B"),
            )
        )
        results = await asyncio.wait_for(asyncio.gather(left, right), timeout=10)
        assert all(
            result.decision is IdempotencyDecision.RESERVED for result in results
        ), results


def _rename_operation(
    context: ActorContext, competitor_id: CompetitorId, display_name: str
) -> Callable[[], Awaitable[OperationOutcome]]:
    async def operation() -> OperationOutcome:
        await update_competitor_display_name(
            context, competitor_id, CompetitorBasicDataUpdate(display_name=display_name)
        )
        return competitor_outcome(competitor_id, status=200)

    return operation


def _deactivate_operation(
    context: ActorContext, competitor_id: CompetitorId
) -> Callable[[], Awaitable[OperationOutcome]]:
    async def operation() -> OperationOutcome:
        await deactivate_competitor(context, competitor_id)
        return competitor_outcome(competitor_id, status=200)

    return operation


async def _competitors_in_tenant(tenant_club_id: ClubId) -> int:
    return int(
        await database.fetch_val(
            query="SELECT count(*) FROM competitors WHERE managed_by_club_id = :c",
            values={"c": tenant_club_id},
        )
    )


async def _registrations_in_tournament(tournament_id: TournamentId) -> int:
    return int(
        await database.fetch_val(
            query="SELECT count(*) FROM tournament_registrations WHERE tournament_id = :t",
            values={"t": tournament_id},
        )
    )


async def _audit_events(tenant_club_id: ClubId, entity: str) -> int:
    return int(
        await database.fetch_val(
            query=(
                "SELECT count(*) FROM domain_change_log "
                "WHERE tenant_club_id = :c AND entity = :e"
            ),
            values={"c": tenant_club_id, "e": entity},
        )
    )
