"""Ruta HTTP de la asignacion explicita de tatami de un cuadro (S4.2, F3C, LAB ONLY).

Una sola escritura, una sola decision: ``PUT /tournaments/{tournament_id}/stage-items/
{stage_item_id}/court``. ``PUT`` porque fija el estado del cuadro (idempotente por naturaleza) y
porque el mismo cuerpo dice "este cuadro va a este tatami" o "este cuadro se queda sin tatami"
(``court_id: null``).

Lo que la ruta **no** hace: tocar combates uno a uno (la propagacion es parte de la misma operacion
de dominio), inventar un estado de combate para decidir, ni relajar la autorizacion. La escritura es
**OWNER-only** y pasa por la puerta de F3 (``503 F3_WRITE_DISABLED`` antes de reservar, bloquear,
escribir o auditar), con el mismo protocolo de idempotencia que el resto de F3.

Los errores del dominio se traducen al contrato HTTP **por codigo** y con un mapeo cerrado: un error
no contemplado no se convierte en un 4xx adivinado, sale como 500. El unico rechazo con cuerpo
estructurado es el de actividad (``STAGE_ITEM_IN_USE``), porque el recuento de combates que bloquean
es informacion util y no un interno.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final, NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette import status

from bracket.config import config
from bracket.logic import stage_item_court
from bracket.logic.competitors import InsufficientPrivilegesError, TenantNotAuthorizedError
from bracket.logic.idempotency import IdempotencyDecision
from bracket.logic.idempotency_protocol import OperationOutcome, ProtocolResult
from bracket.logic.stage_item_court import (
    CourtNotFoundError,
    StageItemCourtAssignment,
    StageItemCourtError,
    StageItemInUseError,
    StageItemNotFoundError,
)
from bracket.models.db.domain import ActorContext
from bracket.models.db.tatami_api import StageItemCourtBody, StageItemCourtResponse
from bracket.routes.domain_auth import actor_context_for_tournament
from bracket.routes.domain_idempotency import execute_idempotent_operation, idempotency_request
from bracket.routes.f3_write_gate import require_f3_write_enabled
from bracket.utils.id_types import StageItemId, TournamentId

router = APIRouter(prefix=config.api_prefix)

#: Tipo de recurso del protocolo de idempotencia. El protocolo no tiene lista cerrada de tipos: lo
#: que cierra es la coherencia tipo/identificador y el juego de claves de la respuesta.
_RESOURCE_TYPE: Final = "stage_item"

_NOT_FOUND_DETAIL: Final = "Not Found"
_STATE_ERROR: Final = "State error"
_ROLE_NOT_ALLOWED: Final = "ROLE_NOT_ALLOWED"
_TENANT_NOT_AUTHORIZED: Final = "TENANT_NOT_AUTHORIZED"

#: Codigo estable del rechazo por actividad real en el cuadro.
STAGE_ITEM_IN_USE: Final = "STAGE_ITEM_IN_USE"


class _ErrorMapping(NamedTuple):
    """Fila del mapeo de errores de dominio al contrato HTTP de tatami."""

    error_types: tuple[type[Exception], ...]
    status_code: int
    detail: str


#: Mapeo cerrado de errores de dominio al contrato HTTP. El orden importa: el primer tipo que
#: coincide decide. Un error no contemplado sale como 500, no como un 4xx adivinado.
_ERROR_MAP: tuple[_ErrorMapping, ...] = (
    _ErrorMapping(
        (StageItemNotFoundError, CourtNotFoundError),
        status.HTTP_404_NOT_FOUND,
        _NOT_FOUND_DETAIL,
    ),
    _ErrorMapping((TenantNotAuthorizedError,), status.HTTP_403_FORBIDDEN, _TENANT_NOT_AUTHORIZED),
    _ErrorMapping((InsufficientPrivilegesError,), status.HTTP_403_FORBIDDEN, _ROLE_NOT_ALLOWED),
)


def _http_exception(error: Exception) -> HTTPException:
    """Traduce un rechazo del dominio al contrato HTTP de tatami.

    El rechazo por actividad se traduce aparte: es el unico cuyo cuerpo no es solo el codigo, porque
    lleva el recuento de combates con actividad que el organizador necesita para saber por que no
    puede mover el cuadro. El codigo sigue siendo cerrado y va primero.
    """
    if isinstance(error, StageItemInUseError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": STAGE_ITEM_IN_USE, "activity_match_count": error.activity_match_count},
        )

    for mapping in _ERROR_MAP:
        if isinstance(error, mapping.error_types):
            return HTTPException(status_code=mapping.status_code, detail=mapping.detail)
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_STATE_ERROR)


async def _run_protocol(
    *,
    context: ActorContext,
    request: Request,
    operation: Callable[[], Awaitable[OperationOutcome]],
    expected_status: int,
) -> ProtocolResult:
    """Ejecuta la operacion dentro del protocolo de idempotencia y comprueba el codigo declarado.

    La escritura es administrativa, asi que el OWNER lo exige el protocolo (``require_owner=True``):
    es la misma puerta que usan las escrituras de la ventana, para que un COLLAB reciba el mismo
    ``403 ROLE_NOT_ALLOWED`` en toda la superficie de escritura de F3. El codigo HTTP lo fija la
    ruta y el protocolo guarda ese mismo codigo: una reserva que declare otro no se responde como
    exito.
    """
    try:
        result = await execute_idempotent_operation(
            context=context,
            request=await idempotency_request(request),
            operation=operation,
            require_owner=True,
        )
    except (
        StageItemCourtError,
        TenantNotAuthorizedError,
        InsufficientPrivilegesError,
    ) as error:
        raise _http_exception(error) from error

    if result.response_status != expected_status:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_STATE_ERROR)
    return result


@router.put(
    "/tournaments/{tournament_id}/stage-items/{stage_item_id}/court",
    dependencies=[Depends(require_f3_write_enabled)],
    response_model=StageItemCourtResponse,
)
async def assign_stage_item_court_endpoint(
    tournament_id: TournamentId,
    stage_item_id: StageItemId,
    body: StageItemCourtBody,
    request: Request,
    context: ActorContext = Depends(actor_context_for_tournament),
) -> StageItemCourtResponse:
    """Fija (o quita) el tatami de un cuadro entero y lo propaga a sus combates.

    La operacion de dominio hace todo el trabajo en una transaccion; esta ruta solo la ejecuta bajo
    el protocolo de idempotencia y publica el estado resultante. La respuesta se construye con el
    efecto de la operacion **si se ha ejecutado ahora**; si lo que llega es la repeticion de una
    operacion ya confirmada, no se re-ejecuta nada y se publica el estado vigente (con efecto 0,
    porque no se ha movido ningun combate en esta peticion).
    """
    executed: list[StageItemCourtAssignment] = []

    async def operation() -> OperationOutcome:
        """Aplica la asignacion y guarda el efecto para publicarlo en la respuesta."""
        assignment = await stage_item_court.assign_stage_item_court(
            context=context,
            tournament_id=tournament_id,
            stage_item_id=stage_item_id,
            court_id=body.court_id,
        )
        executed.append(assignment)
        return OperationOutcome(
            response_status=status.HTTP_200_OK,
            metadata={"resource_id": int(stage_item_id)},
            resource_type=_RESOURCE_TYPE,
            resource_id=int(stage_item_id),
        )

    result = await _run_protocol(
        context=context,
        request=request,
        operation=operation,
        expected_status=status.HTTP_200_OK,
    )

    if result.decision is IdempotencyDecision.RESERVED and executed:
        assignment = executed[-1]
    else:
        assignment = await stage_item_court.read_stage_item_court_assignment(
            context=context, tournament_id=tournament_id, stage_item_id=stage_item_id
        )

    return StageItemCourtResponse(
        stage_item_id=assignment.stage_item_id,
        court_id=assignment.court_id,
        court_name=assignment.court_name,
        affected_match_count=assignment.affected_match_count,
    )
