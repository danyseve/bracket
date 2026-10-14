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

Lo que estas rutas **no** hacen: editar competidores o clubes, generar el cuadro, ni relajar la
autorizacion del dominio. Los errores del dominio se traducen al catalogo de S3.3d (§11) **por
codigo**, con detalles fijos que nunca repiten el valor recibido.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette import status

from bracket.config import config
from bracket.logic.bracket_generation import (
    BracketNotReadyError,
    TournamentBracketGeneration,
    generate_bracket_for_tournament,
    read_tournament_bracket_generation,
)
from bracket.logic.bracket_readiness import (
    BracketReadiness,
    CategoryReadiness,
    ReadinessIssue,
    evaluate_bracket_readiness,
)
from bracket.logic.classification.engine import classify_input
from bracket.logic.classification.rules import CategoryVerdict, RegistrationClassification
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
    assign_registration_category,
    confirm_registration,
    create_registration,
    disqualify_registration,
    reinstate_registration,
    withdraw_registration,
)
from bracket.logic.scheduling.generation import BracketGenerationPlan
from bracket.models.db.domain import ActorContext, TournamentRegistration
from bracket.models.db.registration_api import (
    BracketReadinessCategory,
    BracketReadinessIssue,
    BracketReadinessResponse,
    BracketReadinessSummary,
    ClassificationCategoryVerdict,
    GenerateBracketStageItemResponse,
    GenerateBracketTournamentBody,
    GenerateBracketTournamentResponse,
    RegistrationCategoryBody,
    RegistrationClassificationResponse,
    RegistrationCreateBody,
    RegistrationFilterStatus,
    RegistrationLifecycleBody,
    RegistrationListResponse,
    RegistrationResponse,
    RegistrationWindowBody,
    RegistrationWindowCloseBody,
    RegistrationWindowResponse,
)
from bracket.models.db.tournament import Tournament
from bracket.routes.domain_auth import actor_context_for_tournament
from bracket.routes.domain_idempotency import execute_idempotent_operation, idempotency_request
from bracket.routes.f3_write_gate import require_f3_write_enabled
from bracket.routes.util import disallow_archived_tournament
from bracket.sql.bracket_readiness import (
    get_existing_single_elimination_brackets,
    get_registration_readiness_rows,
)
from bracket.sql.classification import get_registration_classification_input
from bracket.sql.domain_reads import (
    count_tournament_registrations,
    get_registration,
    list_tournament_registrations,
)
from bracket.utils.id_types import (
    CompetitorId,
    StageItemId,
    TournamentId,
    TournamentRegistrationId,
)
from bracket.utils.pagination import PaginationRegistrations

router = APIRouter(prefix=config.api_prefix)

#: ``resource_type`` de la reserva de idempotencia (el mismo valor que la entidad de auditoria).
_RESOURCE_TYPE = "tournament_registration"

#: Las escrituras administrativas del torneo (ventana de S3.4c y generacion del cuadro de S3.5a)
#: son del **torneo**, no de una inscripcion: mismo criterio que su auditoria
#: (``entity='tournament'``) y sin tocar la lista cerrada de recursos del protocolo.
_TOURNAMENT_RESOURCE_TYPE = "tournament"

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
_BRACKET_NOT_READY = "BRACKET_NOT_READY"


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

    El rechazo por revision se traduce aparte porque es el **unico** cuyo cuerpo no es solo el
    codigo: lleva los bloqueos derivados. El codigo sigue siendo el primero y sigue siendo cerrado.
    """
    if isinstance(error, BracketNotReadyError):
        return _not_ready_exception(error)

    for mapping in _ERROR_MAP:
        if isinstance(error, mapping.error_types):
            return HTTPException(status_code=mapping.status_code, detail=mapping.detail)
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_STATE_ERROR)


def _readiness_issue(entry: ReadinessIssue) -> BracketReadinessIssue:
    """Bloqueo de la revision en su forma publica: codigo del vocabulario cerrado y recuentos."""
    return BracketReadinessIssue(
        code=entry.code,
        count=entry.count,
        category_key=entry.category_key,
        required_minimum=entry.required_minimum,
    )


def _not_ready_exception(error: BracketNotReadyError) -> HTTPException:
    """Rechazo por revision: ``409 BRACKET_NOT_READY`` con los bloqueos **derivados**.

    Sin ellos el cliente no puede actuar y tendria que repetir la revision; son la misma proyeccion
    publica y segura que devuelve la revision GET (codigos del vocabulario cerrado y recuentos), sin
    filas, sin identidades, sin el actor y sin el tenant. La revision que viaja es la que se
    **recalculo** dentro de la transaccion, no la que el cliente pudo leer antes.
    """
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": _BRACKET_NOT_READY,
            "blocking_issues": [
                _readiness_issue(issue).model_dump(mode="json") for issue in error.readiness.issues
            ],
            "registration_window": error.readiness.window_state,
        },
    )


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
    dependencies=[Depends(require_f3_write_enabled)],
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
    dependencies=[Depends(require_f3_write_enabled)],
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
    dependencies=[Depends(require_f3_write_enabled)],
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
    dependencies=[Depends(require_f3_write_enabled)],
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
    dependencies=[Depends(require_f3_write_enabled)],
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


# --- clasificacion de la inscripcion (S4.8a-3 §6/§7/§8) -----------------------------------------


def _classification_verdict(verdict: CategoryVerdict) -> ClassificationCategoryVerdict:
    """Proyeccion publica del veredicto del motor: clave, etiqueta, compatibilidad y motivos."""
    return ClassificationCategoryVerdict(
        category_key=verdict.category_key,
        category_label=verdict.category_label,
        compatible=verdict.compatible,
        blockers=list(verdict.blockers),
        warnings=list(verdict.warnings),
    )


def _classification_response(
    assessment: RegistrationClassification,
    *,
    with_chosen_category: bool = True,
) -> RegistrationClassificationResponse:
    """Contrato de §6: compatibles, recomendada (solo con una unica), bloqueos y avisos.

    ``with_chosen_category=False`` proyecta el resultado **sin** el veredicto de la categoria
    elegida: lo usa la clasificacion **previa** al alta (S4.8d §2), donde todavia no hay categoria
    declarada y su ausencia no es un defecto de los datos declarados. No oculta ningun otro bloqueo:
    los que salen del motor y de los hechos declarados se publican igual. La clasificacion de una
    inscripcion existente no cambia porque el defecto es ``True``.
    """
    recommended = assessment.result.recommended_category
    blockers = assessment.result.blockers if not with_chosen_category else assessment.blockers
    return RegistrationClassificationResponse(
        compatible_categories=[
            _classification_verdict(verdict) for verdict in assessment.result.compatible_categories
        ],
        recommended_category=None if recommended is None else _classification_verdict(recommended),
        blockers=list(blockers),
        warnings=list(assessment.warnings),
    )


@router.get(
    "/tournaments/{tournament_id}/registrations/{registration_id}/classification",
    response_model=RegistrationClassificationResponse,
)
async def get_registration_classification_endpoint(
    tournament_id: TournamentId,
    registration_id: TournamentRegistrationId,
    context: ActorContext = Depends(actor_context_for_tournament),
) -> RegistrationClassificationResponse:
    """Clasificacion de una inscripcion contra las categorias de su torneo (§6, solo lectura).

    Reutiliza **exactamente** el motor ya validado de a-2 (``bracket/logic/classification``) y su
    cargador SQL: no reimplementa reglas. No escribe, no audita, no reserva idempotencia y no exige
    ``Idempotency-Key``. La inscripcion se acota al torneo de la ruta y al tenant del contexto: un
    torneo o una inscripcion de otro tenant responden el mismo 404 que uno inexistente.
    """
    await _load_registration_in_tournament(context, tournament_id, registration_id)
    data = await get_registration_classification_input(
        registration_id, tenant_club_id=context.tenant_club_id
    )
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)
    return _classification_response(classify_input(data))


@router.post(
    "/tournaments/{tournament_id}/registrations/{registration_id}/category",
    dependencies=[Depends(require_f3_write_enabled)],
    response_model=RegistrationResponse,
)
async def assign_registration_category_endpoint(
    tournament_id: TournamentId,
    registration_id: TournamentRegistrationId,
    body: RegistrationCategoryBody,
    request: Request,
    context: ActorContext = Depends(actor_context_for_tournament),
) -> RegistrationResponse:
    """Asigna la categoria de una inscripcion vigente (§7) y audita el cambio (§8).

    Escritura F3 con ``Idempotency-Key``. Sin ``category_key`` usa la **recomendada**, que solo
    existe cuando hay **exactamente una** categoria compatible; con varias se rechaza sin elegir por
    el cliente. ``override=true`` permite fijar una categoria no compatible, es **OWNER-only**
    (COLLABORATOR ``403``, anonimo ``401``) y exige motivo; el RBAC general del resto de operaciones
    no cambia.
    """
    await _load_registration_in_tournament(context, tournament_id, registration_id)

    async def operation() -> OperationOutcome:
        registration = await assign_registration_category(
            context,
            registration_id,
            category_key=body.category_key,
            override=body.override,
            reason_code=body.reason_code,
            reason_note=body.reason_note,
        )
        return _outcome(status.HTTP_200_OK, registration)

    registration = await _execute(
        context=context,
        request=request,
        operation=operation,
        expected_status=status.HTTP_200_OK,
        require_owner=body.override,
    )
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
        resource_type=_TOURNAMENT_RESOURCE_TYPE,
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

    def category(entry: CategoryReadiness) -> BracketReadinessCategory:
        return BracketReadinessCategory(
            category_key=entry.category_key,
            category_label=entry.category_label,
            confirmed_count=entry.confirmed_count,
            required_minimum=entry.required_minimum,
            ready=entry.ready,
            blocking_issues=[_readiness_issue(item) for item in entry.issues],
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
        blocking_issues=[_readiness_issue(entry) for entry in readiness.issues],
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
    dependencies=[Depends(require_f3_write_enabled)],
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
    dependencies=[Depends(require_f3_write_enabled)],
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


# --- generacion explicita del cuadro del torneo (S3.5a) -----------------------------------------


def _generation_stage_item(
    stage_item_id: StageItemId, plan: BracketGenerationPlan
) -> GenerateBracketStageItemResponse:
    """Un cuadro en su forma publica: solo el reparto, nunca el identificador de sus equipos."""
    return GenerateBracketStageItemResponse(
        stage_item_id=stage_item_id,
        entrant_count=plan.entrant_count,
        bracket_size=plan.bracket_size,
        bye_count=plan.bye_count,
        ghost_count=plan.ghost_count,
    )


def _generation_response(
    generation: TournamentBracketGeneration,
) -> GenerateBracketTournamentResponse:
    """Proyeccion publica de la generacion: el reparto de cada cuadro y el recuento de los fuera.

    No hay campos nuevos que inventar: cada cuadro viaja como el reparto que ya publica la ruta de
    F2C (participantes, tamano, despedidas y huecos) y el torneo con su recuento. Ni el actor, ni el
    tenant, ni las ranuras, ni los equipos salen de aqui.
    """
    return GenerateBracketTournamentResponse(
        tournament_id=generation.tournament_id,
        generated=generation.generated,
        stage_items=[
            _generation_stage_item(bracket.stage_item_id, bracket.plan)
            for bracket in generation.brackets
        ],
        skipped_stage_items=generation.skipped_stage_items,
    )


def _generation_outcome(tournament_id: TournamentId) -> OperationOutcome:
    """Lo unico que el protocolo guarda de una generacion: su codigo y su torneo.

    La respuesta no se guarda: se reconstruye releyendo el reparto, y esa relectura es justo lo que
    hace que un replay devuelva el mismo cuerpo sin volver a generar nada.
    """
    return OperationOutcome(
        response_status=status.HTTP_200_OK,
        metadata={"resource_id": tournament_id},
        resource_type=_TOURNAMENT_RESOURCE_TYPE,
        resource_id=tournament_id,
    )


@router.post(
    "/tournaments/{tournament_id}/generate-bracket",
    dependencies=[Depends(require_f3_write_enabled)],
    response_model=GenerateBracketTournamentResponse,
)
async def generate_bracket_endpoint(
    tournament_id: TournamentId,
    request: Request,
    body: GenerateBracketTournamentBody | None = None,
    context: ActorContext = Depends(actor_context_for_tournament),
    _: Tournament = Depends(disallow_archived_tournament),
) -> GenerateBracketTournamentResponse:
    """Genera el cuadro de eliminacion directa del torneo por accion **explicita** del organizador.

    OWNER-only y con ``Idempotency-Key`` obligatoria: no hay ningun camino que genere al cerrar la
    ventana, ni el cliente puede pedirla sin clave. El cuerpo esta vacio a proposito (un
    campo de mas
    es un 422): el actor, el instante y el porque quedan en la auditoria, no en la peticion.

    La peticion **no** se fia de la revision que el cliente haya leido antes: dentro de la misma
    transaccion —y con la fila del torneo ya bloqueada— la revision de S3.4d se recalcula.
    Si no esta
    lista no se escribe nada y se responde ``409 BRACKET_NOT_READY`` con los bloqueos derivados. Una
    ventana ``OPEN`` nunca genera; ``DEADLINE_PASSED`` y ``CLOSED`` si, siempre que la revision
    global este lista. No se cierra la ventana ni se toca su plazo.

    La generacion es la de F2C, sin reimplementar su algoritmo: reparador de despedidas,
    determinista y con su bloqueo propio. Un cuadro que F2C no admite generar (sin participantes,
    alimentado por otro, o con actividad encima) no se toca y se cuenta aparte; el reparto de los
    demas se escribe entero o no se escribe nada, porque todo ocurre en una sola transaccion. Un
    torneo archivado responde el mismo 400 que la generacion de F2C (regla que ya existia) y uno de
    otro tenant el mismo 404 que uno inexistente, antes de mirar su estado.
    """

    async def operation() -> OperationOutcome:
        await generate_bracket_for_tournament(context, tournament_id)
        return _generation_outcome(tournament_id)

    await _run_protocol(
        context=context,
        request=request,
        operation=operation,
        expected_status=status.HTTP_200_OK,
        require_owner=True,
    )
    return _generation_response(await read_tournament_bracket_generation(context, tournament_id))
