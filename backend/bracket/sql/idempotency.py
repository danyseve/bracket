"""Sentencias del almacen de claves de idempotencia (S3.3c-3).

Solo SQL: las reglas puras (clave, huella, metadatos y clasificacion) viven en
``bracket/logic/idempotency.py`` y el esquema en ``bracket/schema.py``.

**Ninguna de estas funciones abre ni confirma su propia transaccion.** La reserva, la operacion de
dominio y su finalizacion tienen que confirmarse juntas o revertirse juntas: la transaccion la abre
quien orquesta (capa de dominio / capa HTTP de S3.3c-4). Aqui se ejecutan sentencias sueltas.

Consecuencia importante y verificada en el laboratorio: si dos transacciones reservan la **misma**
clave a la vez, la segunda **espera** en el indice unico y, cuando la primera confirma, recibe un
``UniqueViolationError``; mientras la primera no confirma, la segunda no ve ninguna fila (no ve
"IN_PROGRESS"). Por eso la traduccion a una decision no se hace dentro de la transaccion fallida:
se revierte primero y se vuelve a leer la reserva.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg  # type: ignore[import-untyped]

from bracket.database import database
from bracket.models.db.idempotency import (
    IdempotencyCompletion,
    IdempotencyKey,
    IdempotencyReservationInsert,
)
from bracket.utils.id_types import ClubId, UserId
from bracket.utils.types import assert_some

_RESERVATION_COLUMNS = (
    "id, tenant_club_id, actor_user_id, idempotency_key, request_fingerprint, "
    "fingerprint_key_version, request_method, request_path, state, resource_type, resource_id, "
    "response_status, response_body, created, expires_at, completed_at"
)

UNIQUE_KEY_CONSTRAINT = "uq_domain_idempotency_keys_tenant_actor_key"
FOREIGN_KEY_TENANT_CONSTRAINT = "fk_domain_idempotency_keys_tenant_club_id"
FOREIGN_KEY_ACTOR_CONSTRAINT = "fk_domain_idempotency_keys_actor_user_id"

#: Estados que puede escribir una sentencia: el vocabulario lo cierra el CHECK de la tabla.
IN_PROGRESS_STATE_SQL = "IN_PROGRESS"
COMPLETED_STATE_SQL = "COMPLETED"


def _to_idempotency_key(row: Any) -> IdempotencyKey:
    """Convierte una fila en modelo. asyncpg devuelve jsonb como texto: se decodifica aqui.

    La base guarda response_body como jsonb (y asi lo valida su CHECK); la capa SQL es el
    unico punto donde el texto del driver se convierte en el diccionario que ve el dominio.
    """
    data = dict(row._mapping)
    body = data.get("response_body")
    if isinstance(body, str):
        data["response_body"] = json.loads(body)
    return IdempotencyKey.model_validate(data)


async def sql_insert_idempotency_reservation(
    reservation: IdempotencyReservationInsert,
) -> IdempotencyKey:
    """Reserva la clave dentro de la transaccion en curso (primera sentencia de la operacion).

    No captura el conflicto de unicidad a proposito: en PostgreSQL una sentencia fallida aborta la
    transaccion completa, asi que el ``UniqueViolationError`` se deja escapar para que el llamador
    revierta y decida con una lectura nueva.
    """
    query = f"""
        INSERT INTO domain_idempotency_keys (
            tenant_club_id, actor_user_id, idempotency_key, request_fingerprint,
            fingerprint_key_version, request_method, request_path, state, created, expires_at
        )
        VALUES (
            :tenant_club_id, :actor_user_id, :idempotency_key, :request_fingerprint,
            :fingerprint_key_version, :request_method, :request_path, '{IN_PROGRESS_STATE_SQL}',
            :created, :expires_at
        )
        RETURNING {_RESERVATION_COLUMNS}
        """
    result = await database.fetch_one(
        query=query,
        values={
            "tenant_club_id": reservation.tenant_club_id,
            "actor_user_id": reservation.actor_user_id,
            "idempotency_key": reservation.idempotency_key,
            "request_fingerprint": reservation.request_fingerprint,
            "fingerprint_key_version": reservation.fingerprint_key_version,
            "request_method": reservation.request_method,
            "request_path": reservation.request_path,
            "created": reservation.created,
            "expires_at": reservation.expires_at,
        },
    )
    return _to_idempotency_key(assert_some(result))


async def sql_read_idempotency_reservation(
    *, tenant_club_id: ClubId, actor_user_id: UserId, idempotency_key: str
) -> IdempotencyKey | None:
    """Lee la reserva **dentro de su tenant y de su actor**: la clave no cruza ese ambito."""
    query = f"""
        SELECT {_RESERVATION_COLUMNS} FROM domain_idempotency_keys
        WHERE tenant_club_id = :tenant_club_id AND actor_user_id = :actor_user_id
          AND idempotency_key = :idempotency_key
        """
    result = await database.fetch_one(
        query=query,
        values={
            "tenant_club_id": tenant_club_id,
            "actor_user_id": actor_user_id,
            "idempotency_key": idempotency_key,
        },
    )
    return None if result is None else _to_idempotency_key(result)


async def sql_complete_idempotency_reservation(
    *,
    tenant_club_id: ClubId,
    actor_user_id: UserId,
    idempotency_key: str,
    completion: IdempotencyCompletion,
) -> IdempotencyKey | None:
    """Finaliza **una sola vez** una reserva pendiente, dentro de la transaccion en curso.

    El ``WHERE ... state = 'IN_PROGRESS'`` es la garantia atomica: dos finalizaciones simultaneas no
    pueden escribir las dos (la segunda espera el bloqueo de fila y, al reevaluar, ya no encaja).
    ``None`` significa: no existe en este tenant/actor, o ya estaba confirmada.
    """
    query = f"""
        UPDATE domain_idempotency_keys
        SET state = '{COMPLETED_STATE_SQL}', completed_at = NOW(),
            response_status = :response_status,
            response_body = CAST(:response_body AS jsonb),
            resource_type = :resource_type, resource_id = :resource_id
        WHERE tenant_club_id = :tenant_club_id AND actor_user_id = :actor_user_id
          AND idempotency_key = :idempotency_key AND state = '{IN_PROGRESS_STATE_SQL}'
        RETURNING {_RESERVATION_COLUMNS}
        """
    result = await database.fetch_one(
        query=query,
        values={
            "tenant_club_id": tenant_club_id,
            "actor_user_id": actor_user_id,
            "idempotency_key": idempotency_key,
            "response_status": completion.response_status,
            "response_body": (
                None
                if completion.response_body is None
                else json.dumps(completion.response_body, sort_keys=True, separators=(",", ":"))
            ),
            "resource_type": completion.resource_type,
            "resource_id": completion.resource_id,
        },
    )
    return None if result is None else _to_idempotency_key(result)


def violation_constraint_name(error: BaseException) -> str | None:
    """Nombre de la restriccion violada, tal como lo informa el motor (o ``None``)."""
    if not isinstance(
        error,
        (
            asyncpg.exceptions.UniqueViolationError,
            asyncpg.exceptions.ForeignKeyViolationError,
            asyncpg.exceptions.CheckViolationError,
            asyncpg.exceptions.IntegrityConstraintViolationError,
        ),
    ):
        return None
    name = error.as_dict().get("constraint_name")
    return name if isinstance(name, str) else None


def is_idempotency_unique_violation(error: BaseException) -> bool:
    """``True`` solo si el conflicto es la clave unica de idempotencia (no otra restriccion)."""
    return (
        isinstance(error, asyncpg.exceptions.UniqueViolationError)
        and violation_constraint_name(error) == UNIQUE_KEY_CONSTRAINT
    )
