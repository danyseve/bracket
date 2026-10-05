"""Adaptador HTTP de la idempotencia (S3.3c-4, LAB ONLY).

Este modulo **no declara rutas**: expone lo que necesitara cualquier endpoint F3 para entrar en el
protocolo de idempotencia, y mantiene separado lo que la orden exige separar:

* la cabecera y el cuerpo crudo los lee :func:`idempotency_request` (no lee identidad ni tenant);
* la identidad y el tenant llegan desde ``routes/domain_auth.py`` (S3.3b) y se pasan tal cual;
* la operacion de dominio y la construccion de la respuesta HTTP las pone la ruta que lo use;
* la traduccion de errores del protocolo a HTTP vive aqui, en un unico catalogo de codigos.

Sin endpoint publicado no hay superficie nueva: el modulo solo se importa desde pruebas hasta que
S3.3c-5/S3.3d decidan publicar rutas reales.

Pendiente conocido para cuando se publiquen rutas: el manejador global de ``app.py`` responde
``{"detail": ...}`` y **no** reenvia las cabeceras de la excepcion, de modo que el
``Retry-After`` de los rechazos reintentables (RS-11) se perderia. Aqui se emite en la excepcion
para que la capa que publique las rutas solo tenga que reenviar ``exc.headers``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request, status

from bracket.logic.idempotency_protocol import (
    RETRY_AFTER_SECONDS,
    IdempotencyProtocolError,
    OperationOutcome,
    ProtocolRequest,
    ProtocolResult,
    run_idempotent_operation,
)
from bracket.models.db.domain import ActorContext

#: Cabecera que transporta la clave (una por operacion logica del cliente).
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"

#: Catalogo cerrado de codigos del protocolo -> (codigo HTTP, segundos de ``Retry-After``).
_ERROR_HTTP: dict[str, tuple[int, int | None]] = {
    "IDEMPOTENCY_REQUEST_INVALID": (status.HTTP_400_BAD_REQUEST, None),
    "IDEMPOTENCY_NOT_CONFIGURED": (status.HTTP_503_SERVICE_UNAVAILABLE, None),
    "ACTOR_INACTIVE": (status.HTTP_401_UNAUTHORIZED, None),
    "TENANT_NOT_AUTHORIZED": (status.HTTP_403_FORBIDDEN, None),
    "ROLE_NOT_ALLOWED": (status.HTTP_403_FORBIDDEN, None),
    "IDEMPOTENCY_KEY_REUSED": (status.HTTP_409_CONFLICT, None),
    "OPERATION_IN_PROGRESS": (status.HTTP_409_CONFLICT, RETRY_AFTER_SECONDS),
    "IDEMPOTENCY_RESULT_EXPIRED": (status.HTTP_409_CONFLICT, None),
    "LOCK_TIMEOUT": (status.HTTP_503_SERVICE_UNAVAILABLE, RETRY_AFTER_SECONDS),
    "STATEMENT_TIMEOUT": (status.HTTP_503_SERVICE_UNAVAILABLE, RETRY_AFTER_SECONDS),
    "IDEMPOTENCY_STATE_ERROR": (status.HTTP_500_INTERNAL_SERVER_ERROR, None),
}


def protocol_http_exception(error: IdempotencyProtocolError) -> HTTPException:
    """Traduce un error del protocolo a la respuesta HTTP del contrato (nunca 2xx).

    Un codigo que no este en el catalogo se responde como 500: la lista es cerrada y un codigo nuevo
    sin decision explicita no puede convertirse en un exito.
    """
    status_code, retry_after = _ERROR_HTTP.get(
        error.code, (status.HTTP_500_INTERNAL_SERVER_ERROR, None)
    )
    headers = None if retry_after is None else {"Retry-After": str(retry_after)}
    return HTTPException(status_code=status_code, detail=error.code, headers=headers)


async def idempotency_request(request: Request) -> ProtocolRequest:
    """Metodo, ruta, cuerpo crudo y cabecera de la peticion en curso.

    El cuerpo se lee **tal cual** (bytes): la huella se calcula sobre el payload canonico del
    cliente, no sobre un modelo re-serializado. No se leen identidad, tenant ni rol.
    """
    return ProtocolRequest(
        method=request.method,
        path=request.url.path,
        payload=await request.body(),
        idempotency_key=request.headers.get(IDEMPOTENCY_KEY_HEADER),
    )


async def execute_idempotent_operation(
    *,
    context: ActorContext,
    request: ProtocolRequest,
    operation: Callable[[], Awaitable[OperationOutcome]],
    require_owner: bool = False,
) -> ProtocolResult:
    """Ejecuta la operacion en el protocolo y traduce sus errores a HTTP.

    Es el unico punto donde el protocolo y HTTP se tocan: la operacion de dominio que recibe sigue
    siendo la misma que usaria el servicio interno, sin cambios de firma ni de semantica.
    """
    try:
        return await run_idempotent_operation(
            context=context,
            request=request,
            operation=operation,
            require_owner=require_owner,
        )
    except IdempotencyProtocolError as error:
        raise protocol_http_exception(error) from error
