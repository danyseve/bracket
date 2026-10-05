"""Coordinador transaccional de idempotencia de escrituras F3 (S3.3c-4, LAB ONLY).

Pieza de laboratorio: **no publica ninguna ruta**. Ordena, dentro de una sola transaccion, las
tareas que hasta ahora vivian sueltas:

1. validacion de la identidad y del estado **actual** del actor (``users.active``);
2. autorizacion vigente por tenant y, si la operacion lo exige, por rol;
3. validacion del ``Idempotency-Key`` recibido;
4. huella HMAC de la peticion (metodo, ruta y cuerpo);
5. reserva de la clave o recuperacion de la reserva que ya existe;
6. ejecucion de la operacion de dominio **solo** con una reserva nueva;
7. finalizacion de la idempotencia en la misma transaccion que la operacion y su auditoria;
8. resultado permitido (lista cerrada de metadatos).

Limites del contrato:

* El coordinador **no** construye respuestas HTTP: eso es del adaptador
  (``bracket/routes/domain_idempotency.py``).
* Tenant, actor y estado de idempotencia **nunca** los elige el cliente: el tenant y el actor llegan
  dentro del :class:`ActorContext` ya resuelto en servidor, y el estado lo escribe solo este modulo.
* La operacion de dominio se ejecuta **dentro** de la transaccion del coordinador (su propia
  ``database.transaction()`` anida como ``SAVEPOINT``, no como transaccion independiente) y no puede
  hacer llamadas externas mientras el bloqueo esta tomado.
* Orden de bloqueos: ``idempotency_key -> competitor -> academy -> registration``. La fila de la
  clave se toma siempre primero y quien espera por ella no tiene ningun otro bloqueo, asi que no
  puede cerrar un ciclo (RS-9/RS-10 intactos).
* Semantica de caducidad (S3.3c-4, conservadora): una reserva caducada **no** se reutiliza, no se
  pisa y **no** autoriza a volver a ejecutar; el protocolo responde un rechazo determinista. La
  caducidad es una retencion de almacenamiento, no un permiso de reejecucion.
* Un fallo de la operacion de dominio revierte tambien la reserva: no queda una clave
  ``IN_PROGRESS`` huerfana y el reintento con la misma clave vuelve a empezar de cero.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import asyncpg  # type: ignore[import-untyped]
from heliclockter import datetime_utc

from bracket.database import database
from bracket.logic.idempotency import (
    IdempotencyConfigurationError,
    IdempotencyDecision,
    IdempotencyError,
    classify_reservation,
    compute_request_fingerprint,
    default_expires_at,
    validate_idempotency_key,
    validate_request_method,
    validate_request_path,
    validate_response_metadata,
)
from bracket.models.db.domain import ActorContext
from bracket.models.db.idempotency import (
    IdempotencyCompletion,
    IdempotencyKey,
    IdempotencyReservationInsert,
)
from bracket.models.db.user_x_club import UserXClubRelation
from bracket.sql.idempotency import (
    is_idempotency_unique_violation,
    sql_complete_idempotency_reservation,
    sql_insert_idempotency_reservation,
    sql_read_idempotency_reservation,
)
from bracket.sql.users import get_user_by_id, get_user_relation_to_club
from bracket.utils.id_types import DomainIdempotencyKeyId

#: Tiempo maximo de espera por un bloqueo antes de rechazar la peticion (sin reintento automatico).
LOCK_TIMEOUT_MS = 2000

#: Tiempo maximo de la transaccion completa: ninguna peticion puede quedarse colgada.
STATEMENT_TIMEOUT_MS = 5000

#: Segundos que se anuncian en ``Retry-After`` para un rechazo reintentable.
RETRY_AFTER_SECONDS = 1

_OPERATION_FAILED_RETRY_HINT = " reintente con la misma clave cuando el recurso este disponible"


class IdempotencyProtocolError(Exception):
    """Fallo del protocolo de idempotencia.

    ``code`` es un identificador estable y **no** lleva datos del usuario: el mensaje nunca
    repite la clave, la huella ni el contenido de la peticion.
    """

    code: str = "IDEMPOTENCY_PROTOCOL_ERROR"
    retryable: bool = False


class IdempotencyRequestInvalidError(IdempotencyProtocolError):
    """Clave, metodo o ruta invalidos (o clave ausente): la peticion no se puede registrar."""

    code = "IDEMPOTENCY_REQUEST_INVALID"


class IdempotencyNotConfiguredError(IdempotencyProtocolError):
    """Falta la clave HMAC del servidor: sin huella no se ejecuta nada (fallo cerrado)."""

    code = "IDEMPOTENCY_NOT_CONFIGURED"


class IdempotencyActorInactiveError(IdempotencyProtocolError):
    """El actor ya no esta activo en el momento de la transaccion."""

    code = "ACTOR_INACTIVE"


class IdempotencyTenantNotAuthorizedError(IdempotencyProtocolError):
    """El actor no tiene relacion vigente con el tenant de la operacion."""

    code = "TENANT_NOT_AUTHORIZED"


class IdempotencyRoleNotAllowedError(IdempotencyProtocolError):
    """El actor tiene acceso al tenant, pero su rol actual no permite esta operacion."""

    code = "ROLE_NOT_ALLOWED"


class IdempotencyKeyReusedError(IdempotencyProtocolError):
    """La misma clave llega con otra peticion (huella o version de clave distintas).

    No se ejecuta nada y la reserva existente **no** se modifica.
    """

    code = "IDEMPOTENCY_KEY_REUSED"


class IdempotencyOperationInProgressError(IdempotencyProtocolError):
    """Otra transaccion tiene la clave reservada y todavia no ha confirmado."""

    code = "OPERATION_IN_PROGRESS"
    retryable = True


class IdempotencyResultExpiredError(IdempotencyProtocolError):
    """La reserva caduco: no se puede verificar la operacion anterior.

    Rechazo conservador: no se reejecuta y no se reutiliza la clave (la caducidad es retencion de
    almacenamiento, nunca permiso de reejecucion).
    """

    code = "IDEMPOTENCY_RESULT_EXPIRED"


class IdempotencyLockTimeoutError(IdempotencyProtocolError):
    """Se agoto ``lock_timeout`` esperando un bloqueo de PostgreSQL."""

    code = "LOCK_TIMEOUT"
    retryable = True


class IdempotencyStatementTimeoutError(IdempotencyProtocolError):
    """La transaccion supero ``statement_timeout``."""

    code = "STATEMENT_TIMEOUT"
    retryable = True


class IdempotencyStateError(IdempotencyProtocolError):
    """Invariante roto: la reserva o el resultado no encajan con el protocolo."""

    code = "IDEMPOTENCY_STATE_ERROR"


class _ReservationTakenError(Exception):
    """Senal interna: la clave ya estaba reservada; se resuelve con una lectura nueva.

    PostgreSQL aborta la transaccion completa cuando una sentencia falla, asi que el conflicto se
    captura fuera del ``SAVEPOINT`` de la reserva y se decide con una lectura posterior al
    ``ROLLBACK TO SAVEPOINT``.
    """

    def __init__(self, existing: IdempotencyKey) -> None:
        super().__init__("la clave de idempotencia ya esta reservada")
        self.existing = existing


@dataclass(frozen=True)
class ProtocolRequest:
    """Lo que identifica la peticion del cliente: no incluye identidad ni tenant."""

    method: str
    path: str
    payload: bytes
    idempotency_key: str | None = None


@dataclass(frozen=True)
class OperationOutcome:
    """Lo que una operacion de dominio permite repetir: codigo y metadatos de la lista cerrada.

    ``metadata`` se valida con :func:`validate_response_metadata`: lista cerrada de claves, valores
    escalares y tamano acotado. No se almacena la respuesta HTTP ni el cuerpo de la peticion.
    """

    response_status: int
    metadata: dict[str, Any] | None = None
    resource_type: str | None = None
    resource_id: int | None = None


@dataclass(frozen=True)
class ProtocolResult:
    """Resultado permitido: la operacion se ejecuto ahora o se repite lo ya confirmado."""

    decision: IdempotencyDecision
    response_status: int
    response_body: dict[str, Any] | None
    resource_type: str | None
    resource_id: int | None
    reservation_id: DomainIdempotencyKeyId


def _invalid(message: str) -> IdempotencyRequestInvalidError:
    return IdempotencyRequestInvalidError(message)


def _validated_key(request: ProtocolRequest) -> str:
    """Clave obligatoria y valida, con el error del catalogo del protocolo (400).

    El mensaje de :class:`IdempotencyError` nunca repite el valor recibido; aun asi se traduce a un
    error del protocolo para que la capa HTTP tenga un unico catalogo de codigos.
    """
    try:
        return validate_idempotency_key(request.idempotency_key)
    except IdempotencyError as error:
        raise _invalid(
            "el Idempotency-Key es obligatorio y debe cumplir el formato admitido"
        ) from error


def _validated_request_target(request: ProtocolRequest) -> tuple[str, str]:
    try:
        return (
            validate_request_method(request.method),
            validate_request_path(request.path),
        )
    except IdempotencyError as error:
        raise _invalid("el metodo o la ruta de la peticion no son registrables") from error


async def _apply_timeouts() -> None:
    """Limites por transaccion: sin ellos una peticion bloqueada no tiene techo.

    ``SET LOCAL`` solo afecta a la transaccion en curso y se aplica al principio, antes de tomar
    cualquier bloqueo.
    """
    await database.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_MS}ms'")
    await database.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'")


async def _authorize_actor(context: ActorContext, *, require_owner: bool) -> None:
    """Identidad y autorizacion **actuales**, dentro de la propia transaccion.

    Es una segunda comprobacion deliberada: la del adaptador HTTP ocurre antes, con el token de la
    peticion; esta ocurre con el estado de la base en el momento de ejecutar. Una respuesta
    almacenada nunca sustituye a esta autorizacion.
    """
    user = await get_user_by_id(context.actor_user_id)
    if user is None or not user.active:
        raise IdempotencyActorInactiveError(
            "el actor no esta activo en el momento de ejecutar la operacion"
        )

    relation = await get_user_relation_to_club(context.tenant_club_id, context.actor_user_id)
    if relation is None:
        raise IdempotencyTenantNotAuthorizedError(
            "el actor no tiene relacion vigente con el tenant de la operacion"
        )
    if require_owner and relation is not UserXClubRelation.OWNER:
        raise IdempotencyRoleNotAllowedError(
            "el rol actual del actor no permite esta operacion en el tenant"
        )


def _reservation_insert(
    context: ActorContext,
    request: ProtocolRequest,
    *,
    key: str,
    fingerprint: str,
    key_version: str,
    moment: datetime_utc,
) -> IdempotencyReservationInsert:
    return IdempotencyReservationInsert(
        tenant_club_id=context.tenant_club_id,
        actor_user_id=context.actor_user_id,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        fingerprint_key_version=key_version,
        request_method=request.method,
        request_path=request.path,
        created=moment,
        expires_at=default_expires_at(moment),
    )


async def _reserve(
    context: ActorContext,
    request: ProtocolRequest,
    *,
    key: str,
    fingerprint: str,
    key_version: str,
    moment: datetime_utc,
) -> IdempotencyKey:
    """Reserva la clave dentro de un ``SAVEPOINT``.

    Si otra transaccion gano la carrera por la misma clave, se revierte **solo** el savepoint (la
    transaccion del coordinador sigue viva) y se levanta :class:`_ReservationTakenError` con la
    reserva que hay que resolver.
    """
    reservation = _reservation_insert(
        context,
        request,
        key=key,
        fingerprint=fingerprint,
        key_version=key_version,
        moment=moment,
    )
    try:
        async with database.transaction():
            return await sql_insert_idempotency_reservation(reservation)
    except asyncpg.exceptions.UniqueViolationError as error:
        if not is_idempotency_unique_violation(error):
            raise

    existing = await sql_read_idempotency_reservation(
        tenant_club_id=context.tenant_club_id,
        actor_user_id=context.actor_user_id,
        idempotency_key=key,
    )
    if existing is None:
        raise IdempotencyStateError(
            "el conflicto de la clave unica no se puede releer en esta transaccion"
        )
    raise _ReservationTakenError(existing)


def _resolve_existing(
    existing: IdempotencyKey,
    *,
    fingerprint: str,
    key_version: str,
    moment: datetime_utc,
) -> ProtocolResult:
    """Decide que hacer con la reserva que ya existe, **sin modificarla**.

    * misma huella y ``COMPLETED`` vigente -> se repite el resultado confirmado (nunca se
      reejecuta);
    * misma huella y ``IN_PROGRESS`` vigente -> rechazo reintentable (no se reintenta dentro de la
      transaccion fallida);
    * huella distinta (o calculada con otra version de clave) -> conflicto determinista;
    * caducada -> rechazo conservador: no se reejecuta y no se pisa la reserva.
    """
    decision = classify_reservation(
        existing,
        fingerprint=fingerprint,
        fingerprint_key_version=key_version,
        now=moment,
    )

    if decision is IdempotencyDecision.REPLAY:
        try:
            replay = existing.replay_metadata()
        except IdempotencyError as error:
            raise IdempotencyStateError(
                "la reserva confirmada no tiene un resultado repetible"
            ) from error
        return ProtocolResult(
            decision=IdempotencyDecision.REPLAY,
            response_status=replay.response_status,
            response_body=replay.response_body,
            resource_type=replay.resource_type,
            resource_id=replay.resource_id,
            reservation_id=existing.id,
        )

    if decision is IdempotencyDecision.IN_PROGRESS:
        raise IdempotencyOperationInProgressError(
            "la misma clave tiene una operacion en curso; se puede reintentar mas tarde"
            + _OPERATION_FAILED_RETRY_HINT
        )
    if decision is IdempotencyDecision.FINGERPRINT_CONFLICT:
        raise IdempotencyKeyReusedError(
            "la clave ya se uso con una peticion distinta: no se ejecuta y la reserva no cambia"
        )
    if decision is IdempotencyDecision.EXPIRED:
        raise IdempotencyResultExpiredError(
            "la reserva de esta clave caduco: no se puede verificar la operacion anterior"
            + _OPERATION_FAILED_RETRY_HINT
        )
    raise IdempotencyStateError("la reserva existente no se puede clasificar sin reservarla")


def _completion_from(outcome: OperationOutcome) -> IdempotencyCompletion:
    """Metadatos permitidos del resultado o error de invariante (no se guarda JSON arbitrario)."""
    metadata = outcome.metadata
    if metadata is not None:
        try:
            metadata = validate_response_metadata(metadata)
        except IdempotencyError as error:
            raise IdempotencyStateError(
                "la operacion devolvio metadatos de respuesta no permitidos"
            ) from error

    return IdempotencyCompletion(
        response_status=outcome.response_status,
        response_body=metadata,
        resource_type=outcome.resource_type,
        resource_id=outcome.resource_id,
    )


async def run_idempotent_operation(
    *,
    context: ActorContext,
    request: ProtocolRequest,
    operation: Callable[[], Awaitable[OperationOutcome]],
    require_owner: bool = False,
    now: datetime_utc | None = None,
) -> ProtocolResult:
    """Ejecuta una operacion de dominio bajo el contrato de idempotencia (una sola transaccion).

    ``operation`` es una coroutine que ejecuta la operacion de dominio **sin cambios** y
    devuelve sus metadatos permitidos. Solo se invoca con una reserva nueva; en un reintento con
    la misma clave y la misma peticion se repite el resultado confirmado sin volver a ejecutarla.

    La transaccion del coordinador envuelve a la de la operacion de dominio: si la operacion falla,
    la reserva, el cambio de dominio y su auditoria revierten juntos.
    """
    key = _validated_key(request)
    method, path = _validated_request_target(request)
    moment = now or datetime_utc.now()

    try:
        fingerprint = compute_request_fingerprint(method=method, path=path, payload=request.payload)
    except IdempotencyConfigurationError as error:
        raise IdempotencyNotConfiguredError(
            "el servidor no tiene clave HMAC configurada: no se registra ninguna operacion"
        ) from error
    except IdempotencyError as error:
        raise _invalid("la peticion no se puede resumir en una huella valida") from error

    try:
        async with database.transaction():
            await _apply_timeouts()
            await _authorize_actor(context, require_owner=require_owner)

            try:
                await _reserve(
                    context,
                    request,
                    key=key,
                    fingerprint=fingerprint.fingerprint,
                    key_version=fingerprint.key_version,
                    moment=moment,
                )
            except _ReservationTakenError as taken:
                return _resolve_existing(
                    taken.existing,
                    fingerprint=fingerprint.fingerprint,
                    key_version=fingerprint.key_version,
                    moment=moment,
                )

            outcome = await operation()
            completion = _completion_from(outcome)
            completed = await sql_complete_idempotency_reservation(
                tenant_club_id=context.tenant_club_id,
                actor_user_id=context.actor_user_id,
                idempotency_key=key,
                completion=completion,
            )
            if completed is None:
                raise IdempotencyStateError(
                    "la reserva nueva no se pudo finalizar en su propia transaccion"
                )

            confirmed_status = completed.response_status
            if confirmed_status is None:
                raise IdempotencyStateError(
                    "la reserva confirmada no tiene un codigo de respuesta repetible"
                )

            return ProtocolResult(
                decision=IdempotencyDecision.RESERVED,
                response_status=confirmed_status,
                response_body=completed.response_body,
                resource_type=completed.resource_type,
                resource_id=completed.resource_id,
                reservation_id=completed.id,
            )
    except asyncpg.exceptions.LockNotAvailableError as error:
        raise IdempotencyLockTimeoutError(
            "la operacion no pudo tomar sus bloqueos en el tiempo maximo: no se ejecuto nada"
            + _OPERATION_FAILED_RETRY_HINT
        ) from error
    except asyncpg.exceptions.QueryCanceledError as error:
        raise IdempotencyStatementTimeoutError(
            "la operacion supero el tiempo maximo de transaccion: no se ejecuto nada"
            + _OPERATION_FAILED_RETRY_HINT
        ) from error
