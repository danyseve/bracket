"""Modelos del almacen de claves de idempotencia (S3.3c-3).

Tres piezas:

* ``IdempotencyReservationInsert`` — lo que se reserva antes de operar (nunca lleva el cuerpo);
* ``IdempotencyCompletion`` — lo que se escribe al confirmar: estado final, codigo de respuesta y
  metadatos permitidos, en una sola sentencia;
* ``IdempotencyKey`` — la fila leida: estado, referencia al recurso y lo que se puede repetir.
"""

from __future__ import annotations

from typing import Any

from heliclockter import datetime_utc

from bracket.logic.idempotency import (
    IDEMPOTENCY_STATE_COMPLETED,
    IdempotencyError,
)
from bracket.models.db.shared import BaseModelORM
from bracket.utils.id_types import ClubId, DomainIdempotencyKeyId, UserId


class IdempotencyReservationInsert(BaseModelORM):
    """Reserva inicial: identifica la peticion sin guardarla. No incluye el cuerpo."""

    tenant_club_id: ClubId
    actor_user_id: UserId
    idempotency_key: str
    request_fingerprint: str
    fingerprint_key_version: str
    request_method: str
    request_path: str
    created: datetime_utc
    expires_at: datetime_utc


class IdempotencyCompletion(BaseModelORM):
    """Finalizacion atomica: la operacion confirmada y lo que se podra repetir."""

    response_status: int
    response_body: dict[str, Any] | None = None
    resource_type: str | None = None
    resource_id: int | None = None


class IdempotencyReplay(BaseModelORM):
    """Lo unico que se repite de una respuesta anterior: codigo y metadatos permitidos.

    No es una respuesta HTTP: volver a emitirla exige **reautorizar** la operacion (S3.3c-4).
    """

    response_status: int
    response_body: dict[str, Any] | None = None
    resource_type: str | None = None
    resource_id: int | None = None


class IdempotencyKey(BaseModelORM):
    """Fila de ``domain_idempotency_keys``."""

    id: DomainIdempotencyKeyId
    tenant_club_id: ClubId
    actor_user_id: UserId
    idempotency_key: str
    request_fingerprint: str
    fingerprint_key_version: str
    request_method: str
    request_path: str
    state: str
    resource_type: str | None = None
    resource_id: int | None = None
    response_status: int | None = None
    response_body: dict[str, Any] | None = None
    created: datetime_utc
    expires_at: datetime_utc
    completed_at: datetime_utc | None = None

    def is_completed(self) -> bool:
        return self.state == IDEMPOTENCY_STATE_COMPLETED

    def replay_metadata(self) -> IdempotencyReplay:
        """Respuesta registrada. Una reserva pendiente no tiene nada que repetir."""
        if not self.is_completed() or self.response_status is None:
            raise IdempotencyError(
                "la reserva de idempotencia sigue pendiente: no hay respuesta que repetir"
            )
        return IdempotencyReplay(
            response_status=self.response_status,
            response_body=self.response_body,
            resource_type=self.resource_type,
            resource_id=self.resource_id,
        )
