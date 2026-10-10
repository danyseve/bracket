"""Rutas HTTP F3 de inscripciones de torneo (S3.4a/S3.4b, LAB ONLY).

Primera exposicion HTTP de F3: el ciclo de vida de una inscripcion (alta, confirmacion, retirada,
readmision y descalificacion) montado sobre los controles ya certificados en S3.3d, sin anadir
ningun concepto de dominio. S3.4b anade la **lectura**: listado paginado y detalle.

Contrato de esta capa:

* la identidad y el tenant salen **solo** de la sesion (``actor_context_for_tournament``) y del
  torneo de la ruta: el cuerpo no aporta ``actor_user_id``, ``actor_label`` ni ``tenant_club_id``;
* toda escritura exige ``Idempotency-Key`` y pasa por el protocolo de S3.3c-4, que no se
  reimplementa aqui: el router solo traduce peticion, operacion y errores;
* el motivo de auditoria es el vocabulario cerrado ``reason_code``/``reason_note``;
* la respuesta es una proyeccion publica (``RegistrationResponse``), nunca la fila ni un modelo
  interno;
* la lectura no exige ``Idempotency-Key`` (no escribe nada) y su ambito se aplica **dentro** del
  SQL: ni el listado mezcla otro tenant o torneo, ni el detalle busca por identificador sin
  acotarlo, de modo que una inscripcion ajena responde igual que una inexistente.

Lo que estas rutas **no** hacen: editar competidores o academias, generar el cuadro, cerrar
inscripciones, ni relajar la autorizacion del dominio. Los errores del dominio se traducen al
catalogo de S3.3d (§11) **por codigo**, con detalles fijos que nunca repiten el valor recibido.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette import status

from bracket.config import config
from bracket.logic.competitors import InsufficientPrivilegesError, TenantNotAuthorizedError
from bracket.logic.idempotency_protocol import OperationOutcome
from bracket.logic.registrations import (
    DuplicateRegistrationError,
    InvalidRegistrationDataError,
    InvalidRegistrationStateError,
    RegistrationDomainError,
    RegistrationNotFoundError,
    TournamentNotFoundError,
    confirm_registration,
    create_registration,
    disqualify_registration,
    reinstate_registration,
    withdraw_registration,
)
from bracket.models.db.domain import ActorContext, TournamentRegistration
from bracket.models.db.registration_api import (
    RegistrationCreateBody,
    RegistrationFilterStatus,
    RegistrationLifecycleBody,
    RegistrationListResponse,
    RegistrationResponse,
)
from bracket.routes.domain_auth import actor_context_for_tournament
from bracket.routes.domain_idempotency import execute_idempotent_operation, idempotency_request
from bracket.sql.domain_reads import (
    count_tournament_registrations,
    get_registration,
    list_tournament_registrations,
)
from bracket.utils.id_types import CompetitorId, TournamentId, TournamentRegistrationId
from bracket.utils.pagination import PaginationRegistrations

router = APIRouter(prefix=config.api_prefix)

#: ``resource_type`` de la reserva de idempotencia (el mismo valor que la entidad de auditoria).
_RESOURCE_TYPE = "tournament_registration"

# Un rechazo de autorizacion del dominio usa el codigo del catalogo certificado (S3.3d §11), no su
# mensaje interno: el contrato es el codigo.
_TENANT_NOT_AUTHORIZED = "TENANT_NOT_AUTHORIZED"
_ROLE_NOT_ALLOWED = "ROLE_NOT_ALLOWED"
_STATE_ERROR = "STATE_ERROR"

# Detalles fijos: estables y sin datos personales. El mensaje del dominio no se reenvia nunca
# porque nombra campos y podria crecer sin revision.
_NOT_FOUND_DETAIL = "Not Found"
_REQUEST_INVALID_DETAIL = "Registration request is invalid"
_STATE_CONFLICT_DETAIL = "Registration state does not allow this operation"
_DUPLICATE_DETAIL = "Registration already exists"


class _ErrorMapping(NamedTuple):
    """Fila del mapeo de errores de dominio al contrato HTTP (S3.3d §11)."""

    error_types: tuple[type[Exception], ...]
    status_code: int
    detail: str


#: Mapeo cerrado de errores de dominio al contrato HTTP de S3.3d (§11). El orden importa: el primer
#: tipo que coincide decide. Un error no contemplado **no** se convierte en un 4xx adivinado: sale
#: como 500, igual que un codigo de idempotencia desconocido.
_ERROR_MAP: tuple[_ErrorMapping, ...] = (
    _ErrorMapping(
        (TournamentNotFoundError, RegistrationNotFoundError),
        status.HTTP_404_NOT_FOUND,
        _NOT_FOUND_DETAIL,
    ),
    _ErrorMapping((DuplicateRegistrationError,), status.HTTP_409_CONFLICT, _DUPLICATE_DETAIL),
    _ErrorMapping(
        (InvalidRegistrationStateError,), status.HTTP_409_CONFLICT, _STATE_CONFLICT_DETAIL
    ),
    _ErrorMapping(
        (InvalidRegistrationDataError,), status.HTTP_400_BAD_REQUEST, _REQUEST_INVALID_DETAIL
    ),
    _ErrorMapping((TenantNotAuthorizedError,), status.HTTP_403_FORBIDDEN, _TENANT_NOT_AUTHORIZED),
    _ErrorMapping((InsufficientPrivilegesError,), status.HTTP_403_FORBIDDEN, _ROLE_NOT_ALLOWED),
)


def _http_exception(error: Exception) -> HTTPException:
    """Traduce un error del dominio al contrato HTTP de S3.3d (§11).

    La lista es cerrada y el orden de ``_ERROR_MAP`` decide. Un error de la familia que no este
    contemplado **no** se convierte en un 4xx adivinado: sale como 500, igual que un codigo de
    idempotencia desconocido.
    """
    for mapping in _ERROR_MAP:
        if isinstance(error, mapping.error_types):
            return HTTPException(status_code=mapping.status_code, detail=mapping.detail)
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_STATE_ERROR)


def _assert_registration_is_in_tournament(
    registration: TournamentRegistration | None, tournament_id: TournamentId
) -> TournamentRegistration:
    """La inscripcion de la ruta tiene que existir **en ese torneo** y en el tenant del contexto.

    El mismo 404 que una inscripcion inexistente: no se filtra que el identificador exista en otro
    torneo del mismo tenant. Se comprueba antes de ejecutar la operacion para que la ruta no pueda
    mentir sobre el torneo del recurso (una inscripcion no cambia de torneo, asi que la
    comprobacion previa sigue siendo cierta despues).
    """
    if registration is None or registration.tournament_id != tournament_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)
    return registration


async def _load_registration_in_tournament(
    context: ActorContext, tournament_id: TournamentId, registration_id: TournamentRegistrationId
) -> TournamentRegistration:
    """Lectura autorizada y acotada al tenant, para comprobar el ambito del recurso."""
    registration = await get_registration(registration_id, tenant_club_id=context.tenant_club_id)
    return _assert_registration_is_in_tournament(registration, tournament_id)


def _outcome(response_status: int, registration: TournamentRegistration) -> OperationOutcome:
    """Lo unico que el protocolo guarda: el codigo y datos tecnicos de su lista cerrada.

    Ni el cuerpo de la peticion ni la respuesta HTTP entran aqui. La respuesta se reconstruye
    despues desde el recurso, que el protocolo vuelve a autorizar antes de un replay.
    """
    return OperationOutcome(
        response_status=response_status,
        metadata={"resource_id": registration.id, "registration_status": registration.status},
        resource_type=_RESOURCE_TYPE,
        resource_id=registration.id,
    )


async def _reload_registration(
    context: ActorContext, registration_id: int | None
) -> TournamentRegistration:
    """Relee la inscripcion confirmada (o guardada) para construir la respuesta."""
    if registration_id is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_STATE_ERROR)
    registration = await get_registration(
        TournamentRegistrationId(registration_id), tenant_club_id=context.tenant_club_id
    )
    if registration is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_STATE_ERROR)
    return registration


async def _execute(
    *,
    context: ActorContext,
    request: Request,
    operation: Callable[[], Awaitable[OperationOutcome]],
    expected_status: int,
    require_owner: bool = False,
) -> TournamentRegistration:
    """Ejecuta la operacion dentro del protocolo y devuelve la inscripcion resultante.

    El codigo HTTP lo fija la ruta, y el protocolo guarda ese mismo codigo: si la reserva que se
    esta repitiendo declara otro, no se responde un exito que no se ha comprobado.
    """
    try:
        result = await execute_idempotent_operation(
            context=context,
            request=await idempotency_request(request),
            operation=operation,
            require_owner=require_owner,
        )
    except (
        RegistrationDomainError,
        TenantNotAuthorizedError,
        InsufficientPrivilegesError,
    ) as error:
        raise _http_exception(error) from error

    if result.response_status != expected_status:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_STATE_ERROR)

    return await _reload_registration(context, result.resource_id)


@router.post(
    "/tournaments/{tournament_id}/registrations",
    response_model=RegistrationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_registration_endpoint(
    tournament_id: TournamentId,
    body: RegistrationCreateBody,
    request: Request,
    context: ActorContext = Depends(actor_context_for_tournament),
) -> RegistrationResponse:
    """Alta de una inscripcion en borrador. Exige ``Idempotency-Key`` y motivo ``PLANNED_ENTRY``."""
    draft = body.to_draft()

    async def operation() -> OperationOutcome:
        registration = await create_registration(
            context,
            tournament_id,
            draft,
            reason_code=body.reason_code,
            reason_note=body.reason_note,
        )
        return _outcome(status.HTTP_201_CREATED, registration)

    registration = await _execute(
        context=context,
        request=request,
        operation=operation,
        expected_status=status.HTTP_201_CREATED,
    )
    return RegistrationResponse.from_registration(registration)


@router.post(
    "/tournaments/{tournament_id}/registrations/{registration_id}/confirm",
    response_model=RegistrationResponse,
)
async def confirm_registration_endpoint(
    tournament_id: TournamentId,
    registration_id: TournamentRegistrationId,
    request: Request,
    body: RegistrationLifecycleBody | None = None,
    context: ActorContext = Depends(actor_context_for_tournament),
) -> RegistrationResponse:
    """``DRAFT -> CONFIRMED``. Motivo opcional (``READY`` o ``DATA_VERIFIED``)."""
    await _load_registration_in_tournament(context, tournament_id, registration_id)
    reason_code = None if body is None else body.reason_code
    reason_note = None if body is None else body.reason_note

    async def operation() -> OperationOutcome:
        registration = await confirm_registration(
            context, registration_id, reason_code=reason_code, reason_note=reason_note
        )
        return _outcome(status.HTTP_200_OK, registration)

    registration = await _execute(
        context=context, request=request, operation=operation, expected_status=status.HTTP_200_OK
    )
    return RegistrationResponse.from_registration(registration)


@router.post(
    "/tournaments/{tournament_id}/registrations/{registration_id}/withdraw",
    response_model=RegistrationResponse,
)
async def withdraw_registration_endpoint(
    tournament_id: TournamentId,
    registration_id: TournamentRegistrationId,
    request: Request,
    body: RegistrationLifecycleBody | None = None,
    context: ActorContext = Depends(actor_context_for_tournament),
) -> RegistrationResponse:
    """``DRAFT|CONFIRMED -> WITHDRAWN``. Motivo obligatorio; retirar una confirmada exige OWNER."""
    await _load_registration_in_tournament(context, tournament_id, registration_id)
    reason_code = None if body is None else body.reason_code
    reason_note = None if body is None else body.reason_note

    async def operation() -> OperationOutcome:
        registration = await withdraw_registration(
            context, registration_id, reason_code=reason_code, reason_note=reason_note
        )
        return _outcome(status.HTTP_200_OK, registration)

    registration = await _execute(
        context=context, request=request, operation=operation, expected_status=status.HTTP_200_OK
    )
    return RegistrationResponse.from_registration(registration)


@router.post(
    "/tournaments/{tournament_id}/registrations/{registration_id}/reinstate",
    response_model=RegistrationResponse,
)
async def reinstate_registration_endpoint(
    tournament_id: TournamentId,
    registration_id: TournamentRegistrationId,
    request: Request,
    body: RegistrationLifecycleBody | None = None,
    context: ActorContext = Depends(actor_context_for_tournament),
) -> RegistrationResponse:
    """``WITHDRAWN -> CONFIRMED`` con la misma elegibilidad que confirmar. Motivo obligatorio."""
    await _load_registration_in_tournament(context, tournament_id, registration_id)
    reason_code = None if body is None else body.reason_code
    reason_note = None if body is None else body.reason_note

    async def operation() -> OperationOutcome:
        registration = await reinstate_registration(
            context, registration_id, reason_code=reason_code, reason_note=reason_note
        )
        return _outcome(status.HTTP_200_OK, registration)

    registration = await _execute(
        context=context, request=request, operation=operation, expected_status=status.HTTP_200_OK
    )
    return RegistrationResponse.from_registration(registration)


@router.post(
    "/tournaments/{tournament_id}/registrations/{registration_id}/disqualify",
    response_model=RegistrationResponse,
)
async def disqualify_registration_endpoint(
    tournament_id: TournamentId,
    registration_id: TournamentRegistrationId,
    request: Request,
    body: RegistrationLifecycleBody | None = None,
    context: ActorContext = Depends(actor_context_for_tournament),
) -> RegistrationResponse:
    """``CONFIRMED -> DISQUALIFIED``. Motivo obligatorio y relacion OWNER con el tenant."""
    await _load_registration_in_tournament(context, tournament_id, registration_id)
    reason_code = None if body is None else body.reason_code
    reason_note = None if body is None else body.reason_note

    async def operation() -> OperationOutcome:
        registration = await disqualify_registration(
            context, registration_id, reason_code=reason_code, reason_note=reason_note
        )
        return _outcome(status.HTTP_200_OK, registration)

    registration = await _execute(
        context=context,
        request=request,
        operation=operation,
        expected_status=status.HTTP_200_OK,
        require_owner=True,
    )
    return RegistrationResponse.from_registration(registration)


@router.get(
    "/tournaments/{tournament_id}/registrations",
    response_model=RegistrationListResponse,
)
async def list_registrations_endpoint(
    tournament_id: TournamentId,
    registration_status: RegistrationFilterStatus | None = Query(
        None, alias="status", description="Solo las inscripciones en este estado vigente."
    ),
    competitor_id: CompetitorId | None = Query(
        None, description="Solo las inscripciones de este competidor."
    ),
    pagination: PaginationRegistrations = Depends(),
    context: ActorContext = Depends(actor_context_for_tournament),
) -> RegistrationListResponse:
    """Inscripciones vigentes del torneo, con filtros, paginacion y el total del filtro.

    El ambito es del servidor: el torneo tiene que ser del tenant del contexto (si no, ya lo ha
    rechazado la dependencia) y la consulta se acota a el dentro del SQL, nunca despues de leer. La
    respuesta es la proyeccion publica, no la fila.

    Leer no exige ``Idempotency-Key``: no hay reserva ni operacion de dominio que repetir.
    """
    registrations = await list_tournament_registrations(
        tournament_id,
        tenant_club_id=context.tenant_club_id,
        status=registration_status,
        competitor_id=competitor_id,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    count = await count_tournament_registrations(
        tournament_id,
        tenant_club_id=context.tenant_club_id,
        status=registration_status,
        competitor_id=competitor_id,
    )
    return RegistrationListResponse(
        count=count,
        registrations=[RegistrationResponse.from_registration(row) for row in registrations],
    )


@router.get(
    "/tournaments/{tournament_id}/registrations/{registration_id}",
    response_model=RegistrationResponse,
)
async def get_registration_endpoint(
    tournament_id: TournamentId,
    registration_id: TournamentRegistrationId,
    context: ActorContext = Depends(actor_context_for_tournament),
) -> RegistrationResponse:
    """Inscripcion concreta del torneo, con su estado real.

    El identificador se busca **siempre** acotado al tenant del contexto (no hay consulta global por
    identificador): una inscripcion de otro tenant no existe para esta ruta y responde el mismo 404
    que una inexistente. Tampoco se confirma su existencia en otro torneo del mismo tenant, porque
    ademas tiene que estar en el torneo de la ruta.
    """
    registration = await _load_registration_in_tournament(context, tournament_id, registration_id)
    return RegistrationResponse.from_registration(registration)
