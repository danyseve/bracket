"""Rutas HTTP F3 de inscripciones de torneo (S3.4a/S3.4b/S3.4c, LAB ONLY).

Primera exposicion HTTP de F3: el ciclo de vida de una inscripcion (alta, confirmacion, retirada,
readmision y descalificacion) montado sobre los controles ya certificados en S3.3d, sin anadir
ningun concepto de dominio. S3.4b anade la **lectura**: listado paginado y detalle. S3.4c anade la
**ventana de inscripcion del torneo**: plazo, cierre manual y bloqueo de altas nuevas.

Contrato de esta capa:

* la identidad y el tenant salen **solo** de la sesion (``actor_context_for_tournament``) y del
  torneo de la ruta: el cuerpo no aporta ``actor_user_id``, ``actor_label`` ni ``tenant_club_id``;
* toda escritura exige ``Idempotency-Key`` y pasa por el protocolo de S3.3c-4, que no se
  reimplementa aqui: el router solo traduce peticion, operacion y errores;
* el motivo de auditoria es el vocabulario cerrado ``reason_code``/``reason_note``;
* la respuesta es una proyeccion publica (``RegistrationResponse``/``RegistrationWindowResponse``),
  nunca la fila ni un modelo interno;
* la lectura no exige ``Idempotency-Key`` (no escribe nada) y su ambito se aplica **dentro** del
  SQL: ni el listado mezcla otro tenant o torneo, ni el detalle busca por identificador sin
  acotarlo, de modo que una inscripcion ajena responde igual que una inexistente.

Las dos escrituras de la ventana (plazo y cierre) son administrativas: **OWNER-only**, con el mismo
protocolo de idempotencia que el resto. No se anade ``reopen`` en esta fase: reabrir seria una
operacion con su propio evento de auditoria, no un no-op.

Lo que estas rutas **no** hacen: editar competidores o academias, generar el cuadro, ni relajar la
autorizacion del dominio. Los errores del dominio se traducen al catalogo de S3.3d (§11) **por
codigo**, con detalles fijos que nunca repiten el valor recibido.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette import status

from bracket.config import config
from bracket.logic.bracket_readiness import (
    BracketReadiness,
    CategoryReadiness,
    ReadinessIssue,
    evaluate_bracket_readiness,
)
from bracket.logic.competitors import InsufficientPrivilegesError, TenantNotAuthorizedError
from bracket.logic.idempotency_protocol import OperationOutcome, ProtocolResult
from bracket.logic.registration_core import RegistrationDomainError, TournamentNotFoundError
from bracket.logic.registration_window import (
    RegistrationWindow,
    RegistrationWindowAlreadyClosedError,
    RegistrationWindowClosedError,
    close_registrations,
    registration_window,
    set_registration_deadline,
)
from bracket.logic.registrations import (
    DuplicateRegistrationError,
    InvalidRegistrationDataError,
    InvalidRegistrationStateError,
    RegistrationNotFoundError,
    confirm_registration,
    create_registration,
    disqualify_registration,
    reinstate_registration,
    withdraw_registration,
)
from bracket.models.db.domain import ActorContext, TournamentRegistration
from bracket.models.db.registration_api import (
    BracketReadinessCategory,
    BracketReadinessIssue,
    BracketReadinessResponse,
    BracketReadinessSummary,
    RegistrationCreateBody,
    RegistrationFilterStatus,
    RegistrationLifecycleBody,
    RegistrationListResponse,
    RegistrationResponse,
    RegistrationWindowBody,
    RegistrationWindowCloseBody,
    RegistrationWindowResponse,
)
from bracket.routes.domain_auth import actor_context_for_tournament
from bracket.routes.domain_idempotency import execute_idempotent_operation, idempotency_request
from bracket.sql.bracket_readiness import (
    get_existing_single_elimination_brackets,
    get_registration_readiness_rows,
)
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

#: Las escrituras de la ventana son del **torneo**, no de una inscripcion: mismo criterio que su
#: auditoria (``entity='tournament'``) y sin tocar la lista cerrada de recursos del protocolo.
_WINDOW_RESOURCE_TYPE = "tournament"

# Un rechazo de autorizacion del dominio usa el codigo del catalogo certificado (S3.3d §11), no su
# mensaje interno: el contrato es el codigo.
_TENANT_NOT_AUTHORIZED = "TENANT_NOT_AUTHORIZED"
_ROLE_NOT_ALLOWED = "ROLE_NOT_ALLOWED"
_STATE_ERROR = "STATE_ERROR"

# La ventana cerrada tambien es un codigo estable, distinto del conflicto de estado de una
# inscripcion: el cliente tiene que poder distinguir "no se admiten altas nuevas" de "esa
# inscripcion no admite ese cambio".
_REGISTRATION_CLOSED = "REGISTRATION_CLOSED"
_REGISTRATION_ALREADY_CLOSED = "REGISTRATION_ALREADY_CLOSED"

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
    _ErrorMapping((RegistrationWindowClosedError,), status.HTTP_409_CONFLICT, _REGISTRATION_CLOSED),
    _ErrorMapping(
        (RegistrationWindowAlreadyClosedError,),
        status.HTTP_409_CONFLICT,
        _REGISTRATION_ALREADY_CLOSED,
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


async def _run_protocol(
    *,
    context: ActorContext,
    request: Request,
    operation: Callable[[], Awaitable[OperationOutcome]],
    expected_status: int,
    require_owner: bool = False,
) -> ProtocolResult:
    """Ejecuta la operacion dentro del protocolo de S3.3c-4 y comprueba el codigo declarado.

    El codigo HTTP lo fija la ruta, y el protocolo guarda ese mismo codigo: si la reserva que se
    esta repitiendo declara otro, no se responde un exito que no se ha comprobado. Los errores del
    dominio se traducen al catalogo HTTP; los del protocolo ya vienen traducidos por el.
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

    return result


async def _execute(
    *,
    context: ActorContext,
    request: Request,
    operation: Callable[[], Awaitable[OperationOutcome]],
    expected_status: int,
    require_owner: bool = False,
) -> TournamentRegistration:
    """Ejecuta la operacion dentro del protocolo y devuelve la inscripcion resultante."""
    result = await _run_protocol(
        context=context,
        request=request,
        operation=operation,
        expected_status=expected_status,
        require_owner=require_owner,
    )
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


# --- ventana de inscripcion del torneo (S3.4c) --------------------------------------------------


def _window_response(window: RegistrationWindow) -> RegistrationWindowResponse:
    """Proyeccion publica de la ventana: lista blanca de campos, sin auditoria ni internos.

    ``state`` y ``registrations_open`` viajan juntos para que el cliente que solo necesita saber si
    puede inscribir no tenga que interpretar el vocabulario. El actor del cierre y el motivo no
    salen de la auditoria, asi que no aparecen aqui.
    """
    return RegistrationWindowResponse(
        tournament_id=window.tournament_id,
        registration_deadline=window.registration_deadline,
        registrations_closed_at=window.registrations_closed_at,
        state=window.state,
        registrations_open=window.registrations_open,
    )


def _window_outcome(window: RegistrationWindow) -> OperationOutcome:
    """Lo unico que el protocolo guarda de una escritura de la ventana: su codigo y su recurso.

    La respuesta no se guarda: se reconstruye releyendo la ventana, y esa relectura es justo lo que
    hace que un replay devuelva el mismo cuerpo que la primera vez (el instante del cierre ya
    normalizado por la base de datos, no el reloj del proceso que lo escribio).
    """
    return OperationOutcome(
        response_status=status.HTTP_200_OK,
        metadata={"resource_id": window.tournament_id},
        resource_type=_WINDOW_RESOURCE_TYPE,
        resource_id=window.tournament_id,
    )


async def _reload_window(context: ActorContext, tournament_id: TournamentId) -> RegistrationWindow:
    """Relee la ventana para construir la respuesta, tanto si la operacion se ejecuto como si no."""
    return await registration_window(context, tournament_id)


async def _execute_window(
    *,
    context: ActorContext,
    request: Request,
    operation: Callable[[], Awaitable[OperationOutcome]],
    tournament_id: TournamentId,
) -> RegistrationWindow:
    """Igual que ``_execute`` pero para la ventana: no hay inscripcion que releer, hay una ventana.

    Las dos escrituras de la ventana son administrativas (OWNER-only) y devuelven 200, tambien en
    el replay: la reserva guarda ese mismo codigo.
    """
    await _run_protocol(
        context=context,
        request=request,
        operation=operation,
        expected_status=status.HTTP_200_OK,
        require_owner=True,
    )
    return await _reload_window(context, tournament_id)


@router.get(
    "/tournaments/{tournament_id}/registration-window",
    response_model=RegistrationWindowResponse,
)
async def get_registration_window_endpoint(
    tournament_id: TournamentId,
    context: ActorContext = Depends(actor_context_for_tournament),
) -> RegistrationWindowResponse:
    """Ventana de inscripcion del torneo, con su estado derivado del plazo y del cierre manual.

    Lee cualquier miembro del tenant (OWNER o COLLABORATOR): es la misma informacion que necesita
    quien va a inscribirse, y el detalle de quien cerro y cuando no se publica. No exige
    ``Idempotency-Key`` y no escribe auditoria. Un torneo de otro tenant responde el mismo 404 que
    uno inexistente.
    """
    try:
        window = await registration_window(context, tournament_id)
    except (
        RegistrationDomainError,
        TenantNotAuthorizedError,
        InsufficientPrivilegesError,
    ) as error:
        raise _http_exception(error) from error
    return _window_response(window)


def _readiness_response(readiness: BracketReadiness) -> BracketReadinessResponse:
    """Proyeccion publica de la revision: lista blanca de campos y codigos del vocabulario cerrado.

    Ni el actor ni el tenant salen de aqui, y las filas de inscripcion no viajan: solo recuentos y
    bloqueos. El orden ya viene fijado por la capa de dominio, asi que la proyeccion no reordena
    nada: dos revisiones del mismo estado devuelven exactamente lo mismo.
    """

    def issue(entry: ReadinessIssue) -> BracketReadinessIssue:
        return BracketReadinessIssue(
            code=entry.code,
            count=entry.count,
            category_key=entry.category_key,
            required_minimum=entry.required_minimum,
        )

    def category(entry: CategoryReadiness) -> BracketReadinessCategory:
        return BracketReadinessCategory(
            category_key=entry.category_key,
            category_label=entry.category_label,
            confirmed_count=entry.confirmed_count,
            required_minimum=entry.required_minimum,
            ready=entry.ready,
            blocking_issues=[issue(item) for item in entry.issues],
        )

    return BracketReadinessResponse(
        tournament_id=readiness.tournament_id,
        ready=readiness.ready,
        registration_window=readiness.window_state,
        summary=BracketReadinessSummary(
            total=readiness.summary.total,
            confirmed=readiness.summary.confirmed,
            draft=readiness.summary.draft,
            withdrawn=readiness.summary.withdrawn,
            disqualified=readiness.summary.disqualified,
        ),
        categories=[category(entry) for entry in readiness.categories],
        blocking_issues=[issue(entry) for entry in readiness.issues],
    )


@router.get(
    "/tournaments/{tournament_id}/bracket-readiness",
    response_model=BracketReadinessResponse,
)
async def get_bracket_readiness_endpoint(
    tournament_id: TournamentId,
    context: ActorContext = Depends(actor_context_for_tournament),
) -> BracketReadinessResponse:
    """Revision final del estado de las inscripciones antes de generar cuadro (solo lectura).

    Responde a "esta listo este torneo para generar brackets?" y, si no, a "que problemas concretos
    lo impiden". OWNER y COLLABORATOR pueden leer; el tenant lo resuelve el servidor y nunca el
    cliente, y un torneo de otro tenant responde el mismo 404 que uno inexistente.

    No exige ``Idempotency-Key`` porque no hay ninguna escritura que repetir, no escribe auditoria y
    no altera estado. La ventana se lee **primero**: es la que autoriza el torneo y la que acota la
    lectura de la estructura competitiva a un torneo que el actor puede ver. No genera cuadros:
    solo revisa si se puede.
    """
    try:
        window = await registration_window(context, tournament_id)
        rows = await get_registration_readiness_rows(
            tournament_id, tenant_club_id=context.tenant_club_id
        )
        brackets = await get_existing_single_elimination_brackets(tournament_id)
    except (
        RegistrationDomainError,
        TenantNotAuthorizedError,
        InsufficientPrivilegesError,
    ) as error:
        raise _http_exception(error) from error

    return _readiness_response(
        evaluate_bracket_readiness(
            tournament_id=tournament_id,
            tenant_club_id=context.tenant_club_id,
            window_state=window.state,
            rows=rows,
            existing_brackets=brackets,
        )
    )


@router.post(
    "/tournaments/{tournament_id}/registration-window",
    response_model=RegistrationWindowResponse,
)
async def set_registration_window_endpoint(
    tournament_id: TournamentId,
    body: RegistrationWindowBody,
    request: Request,
    context: ActorContext = Depends(actor_context_for_tournament),
) -> RegistrationWindowResponse:
    """Configura el plazo de inscripcion del torneo. ``registration_deadline`` nula lo limpia.

    OWNER-only y con ``Idempotency-Key``, por el mismo protocolo que el resto de escrituras F3. El
    cuerpo describe el estado deseado: no puede traer identidad, tenant ni el estado de cierre. Un
    plazo ya vencido cierra la ventana por reloj (``DEADLINE_PASSED``) sin escribir nada mas, y
    limpiar el plazo no reabre un cierre manual.
    """
    deadline = body.registration_deadline

    async def operation() -> OperationOutcome:
        window = await set_registration_deadline(
            context, tournament_id, registration_deadline=deadline
        )
        return _window_outcome(window)

    window = await _execute_window(
        context=context,
        request=request,
        operation=operation,
        tournament_id=tournament_id,
    )
    return _window_response(window)


@router.post(
    "/tournaments/{tournament_id}/registrations/close",
    response_model=RegistrationWindowResponse,
)
async def close_registrations_endpoint(
    tournament_id: TournamentId,
    request: Request,
    body: RegistrationWindowCloseBody | None = None,
    context: ActorContext = Depends(actor_context_for_tournament),
) -> RegistrationWindowResponse:
    """Cierre manual de la ventana de inscripcion del torneo: deja de admitir altas nuevas.

    OWNER-only y con ``Idempotency-Key``. El cuerpo esta vacio a proposito (un campo de mas es un
    422) porque cerrar no lleva datos: el actor y el instante quedan en la auditoria. Repetir el
    cierre con la misma clave repite la misma respuesta; con otra clave es un conflicto de estado
    (``409 REGISTRATION_ALREADY_CLOSED``), no un no-op silencioso. No altera el ciclo de vida de las
    inscripciones ya existentes.
    """

    async def operation() -> OperationOutcome:
        window = await close_registrations(context, tournament_id)
        return _window_outcome(window)

    window = await _execute_window(
        context=context,
        request=request,
        operation=operation,
        tournament_id=tournament_id,
    )
    return _window_response(window)
