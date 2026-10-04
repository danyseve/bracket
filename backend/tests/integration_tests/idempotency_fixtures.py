"""S3.3c-3 — laboratorio del almacen de claves de idempotencia.

Aporta lo que comparten las pruebas de almacenamiento y de concurrencia:

* dos tenants y dos actores **reales** (claves ajenas de verdad, no ids inventados salvo en el
  caso explicito de referencia inexistente);
* un secreto HMAC **sintetico** inyectado por configuracion (nunca un secreto de despliegue);
* fabricas de reserva y de finalizacion que pasan por los validadores reales;
* lectura directa de la tabla para comprobar el estado persistido.

Las sondas de espera por bloqueo (``wait_for_waiting_backends``) son las mismas que usa S2-bis:
esperan a que PostgreSQL registre el bloqueo real en ``pg_stat_activity``; no son ``sleep``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from heliclockter import datetime_utc

from bracket.config import config
from bracket.database import database
from bracket.logic.idempotency import (
    compute_request_fingerprint,
    default_expires_at,
    validate_idempotency_key,
)
from bracket.models.db.club import ClubInsertable
from bracket.models.db.idempotency import IdempotencyCompletion, IdempotencyReservationInsert
from bracket.utils.id_types import ClubId, UserId
from tests.integration_tests.mocks import get_mock_user
from tests.integration_tests.sql import inserted_club, inserted_user

SYNTHETIC_SECRET = "synthetic-laboratory-secret-not-for-deployment"
LAB_KEY = "lab-idempotency-key-0001"
LAB_PATH = "/clubs/1/competitors"
LAB_PAYLOAD = b'{"display_name":"Ana"}'
# Identificador que no existe en ninguna tabla: para el caso de referencia inexistente.
ABSENT_ID = 999_999_999

SELECT_RESERVATION = """
    SELECT id, tenant_club_id, actor_user_id, idempotency_key, request_fingerprint,
        fingerprint_key_version, request_method, request_path, state, resource_type, resource_id,
        response_status, response_body, created, expires_at, completed_at
    FROM domain_idempotency_keys
    WHERE tenant_club_id = :tenant_club_id AND actor_user_id = :actor_user_id
        AND idempotency_key = :idempotency_key
"""

SELECT_COUNT_IN_TENANT = """
    SELECT count(*) FROM domain_idempotency_keys
    WHERE tenant_club_id = :tenant_club_id AND actor_user_id = :actor_user_id
"""

SELECT_COUNT_ALL = "SELECT count(*) FROM domain_idempotency_keys"


@dataclass(frozen=True)
class IdempotencyLab:
    """Dos tenants y dos actores reales para probar aislamiento."""

    club_a: ClubId
    club_b: ClubId
    user_a: UserId
    user_b: UserId


@pytest_asyncio.fixture(loop_scope="session")
async def idempotency_lab(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[IdempotencyLab]:
    monkeypatch.setattr(config, "idempotency_hmac_key", SYNTHETIC_SECRET)
    monkeypatch.setattr(config, "idempotency_hmac_key_version", "v1")

    async with (
        inserted_club(ClubInsertable(name="idempotency-lab-a", created=datetime_utc.now())) as club_a,
        inserted_club(ClubInsertable(name="idempotency-lab-b", created=datetime_utc.now())) as club_b,
        inserted_user(get_mock_user()) as user_a,
        inserted_user(get_mock_user()) as user_b,
    ):
        yield IdempotencyLab(
            club_a=ClubId(club_a.id),
            club_b=ClubId(club_b.id),
            user_a=UserId(user_a.id),
            user_b=UserId(user_b.id),
        )


def lab_reservation(
    tenant_club_id: ClubId,
    actor_user_id: UserId,
    *,
    key: str = LAB_KEY,
    method: str = "POST",
    path: str = LAB_PATH,
    payload: bytes = LAB_PAYLOAD,
    created: datetime_utc | None = None,
    lifetime: timedelta = timedelta(hours=1),
) -> IdempotencyReservationInsert:
    """Reserva valida construida con los validadores reales (nunca a mano)."""
    fingerprint = compute_request_fingerprint(method=method, path=path, payload=payload)
    moment = created or datetime_utc.now()
    return IdempotencyReservationInsert(
        tenant_club_id=tenant_club_id,
        actor_user_id=actor_user_id,
        idempotency_key=validate_idempotency_key(key),
        request_fingerprint=fingerprint.fingerprint,
        fingerprint_key_version=fingerprint.key_version,
        request_method=method,
        request_path=path,
        created=moment,
        expires_at=moment + lifetime,
    )


def lab_completion(
    *,
    status: int = 201,
    metadata: dict[str, Any] | None = None,
    resource_type: str | None = "tournament_registration",
    resource_id: int | None = 7,
) -> IdempotencyCompletion:
    return IdempotencyCompletion(
        response_status=status,
        response_body=metadata if metadata is not None else {"resource_id": 7},
        resource_type=resource_type,
        resource_id=resource_id,
    )


async def stored_reservation(
    tenant_club_id: ClubId, actor_user_id: UserId, key: str = LAB_KEY
) -> dict[str, Any] | None:
    """Fila persistida (vista como diccionario) o ``None`` si no existe en ese tenant/actor."""
    record = await database.fetch_one(
        query=SELECT_RESERVATION,
        values={
            "tenant_club_id": tenant_club_id,
            "actor_user_id": actor_user_id,
            "idempotency_key": key,
        },
    )
    return dict(record._mapping) if record is not None else None


async def reservations_in_tenant(tenant_club_id: ClubId, actor_user_id: UserId) -> int:
    return int(
        await database.fetch_val(
            query=SELECT_COUNT_IN_TENANT,
            values={"tenant_club_id": tenant_club_id, "actor_user_id": actor_user_id},
        )
    )


async def reservations_total() -> int:
    return int(await database.fetch_val(query=SELECT_COUNT_ALL))


def provisional_expiry(created: datetime_utc) -> datetime_utc:
    return default_expires_at(created)
