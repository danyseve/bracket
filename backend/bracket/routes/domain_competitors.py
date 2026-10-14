"""Rutas HTTP F3 de competidores (S3.7a, LAB ONLY).

Primera exposicion HTTP de la capa interna de identidad de competidores certificada en S2/S2-bis:
alta, consulta (listado y detalle), renombrado, baja logica y reactivacion. No se anade ningun
concepto de dominio nuevo: cada ruta es la traduccion de una operacion ya certificada.

Contrato de esta capa:

* la identidad y el tenant salen **solo** de la sesion (``actor_context_for_club``) y del club de la
  ruta: el cuerpo no aporta ``actor_user_id``, ``actor_label`` ni ``tenant_club_id``;
* toda escritura exige ``Idempotency-Key`` y pasa por el protocolo de S3.3c-4, que no se
  reimplementa aqui: el router solo traduce peticion, operacion y errores;
* el motivo de auditoria es el vocabulario cerrado ``reason_code``/``reason_note`` del alcance
  ``competitor`` (S3.3c-2): no hay texto libre;
* la respuesta es una proyeccion publica (``CompetitorResponse``), nunca la fila ni un modelo
  interno;
* la lectura no exige ``Idempotency-Key`` (no escribe nada) y su ambito se aplica **dentro** del
  SQL: ni el listado mezcla otro tenant, ni el detalle busca por identificador sin acotarlo, de modo
  que un competidor ajeno responde igual que uno inexistente.

Autorizacion (la del dominio certificado, sin relajarla): cualquier relacion con el club puede
listar, ver, crear y renombrar; la **baja logica** y la **reactivacion** exigen ``OWNER``, igual que
en la capa interna. El 403 de rol se emite despues de demostrar acceso al tenant, nunca antes.

Las escrituras quedan detras de la puerta de S3.6b (``F3_WRITE_ENABLED``): con la bandera apagada
responden ``503 F3_WRITE_DISABLED`` antes de reservar la clave de idempotencia, bloquear filas,
escribir dominio o auditar. Las lecturas siguen publicandose con la bandera apagada.

Lo que estas rutas **no** hacen: gestionar academias ni afiliaciones, ni tocar el ciclo de vida de
la inscripcion. Los errores del dominio se traducen al catalogo de S3.3d (§11) **por codigo**, con
detalles fijos que nunca repiten el valor recibido.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette import status

from bracket.config import config
from bracket.logic.competitor_affiliations import (
    assign_affiliation,
    close_affiliation,
    go_independent,
    promote_affiliation,
)
from bracket.logic.competitors import (
    CompetitorDomainError,
    CompetitorNotFoundError,
    InsufficientPrivilegesError,
    InvalidCompetitorDataError,
    TenantNotAuthorizedError,
    activate_competitor,
    create_competitor,
    deactivate_competitor,
    update_competitor_display_name,
    update_competitor_profile,
)
from bracket.logic.idempotency_protocol import OperationOutcome, ProtocolResult
from bracket.models.db.competitor_api import (
    CompetitorAffiliationBody,
    CompetitorAffiliationResponse,
    CompetitorAffiliationsResponse,
    CompetitorCreateBody,
    CompetitorLifecycleBody,
    CompetitorListItem,
    CompetitorListResponse,
    CompetitorProfileBody,
    CompetitorRenameBody,
    CompetitorResponse,
)
from bracket.models.db.domain import (
    ActorContext,
    Competitor,
    CompetitorBasicDataUpdate,
    CompetitorSportsProfileUpdate,
    CompetitorXSportsClub,
)
from bracket.routes.domain_auth import actor_context_for_club
from bracket.routes.domain_idempotency import execute_idempotent_operation, idempotency_request
from bracket.routes.f3_write_gate import require_f3_write_enabled
from bracket.sql.domain_reads import (
    count_competitors,
    get_affiliation,
    get_competitor,
    get_competitor_affiliations,
    get_open_primary_affiliations,
    list_competitors,
)
from bracket.utils.id_types import ClubId, CompetitorId, CompetitorXSportsClubId
from bracket.utils.pagination import PaginationCompetitors

router = APIRouter(prefix=config.api_prefix)

#: ``resource_type`` de la reserva de idempotencia (el mismo valor que la entidad de auditoria).
_RESOURCE_TYPE = "competitor"

# Un rechazo de autorizacion del dominio usa el codigo del catalogo certificado (S3.3d §11), no su
# mensaje interno: el contrato es el codigo.
_TENANT_NOT_AUTHORIZED = "TENANT_NOT_AUTHORIZED"
_ROLE_NOT_ALLOWED = "ROLE_NOT_ALLOWED"
_STATE_ERROR = "STATE_ERROR"

# Detalles fijos: estables y sin datos personales. El mensaje del dominio no se reenvia nunca
# porque nombra campos y podria crecer sin revision.
_NOT_FOUND_DETAIL = "Not Found"
_REQUEST_INVALID_DETAIL = "Competitor request is invalid"


class _ErrorMapping(NamedTuple):
    """Fila del mapeo de errores de dominio al contrato HTTP (S3.3d §11)."""

    error_types: tuple[type[Exception], ...]
    status_code: int
    detail: str


#: Mapeo cerrado de errores de dominio al contrato HTTP de S3.3d (§11). El orden importa: el primer
#: tipo que coincide decide. Un error no contemplado **no** se convierte en un 4xx adivinado: sale
#: como 500, igual que un codigo de idempotencia desconocido.
_ERROR_MAP: tuple[_ErrorMapping, ...] = (
    _ErrorMapping((CompetitorNotFoundError,), status.HTTP_404_NOT_FOUND, _NOT_FOUND_DETAIL),
    _ErrorMapping(
        (InvalidCompetitorDataError,), status.HTTP_400_BAD_REQUEST, _REQUEST_INVALID_DETAIL
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


def _assert_context_matches_route(context: ActorContext, club_id: ClubId) -> None:
    """El tenant autorizado es el club de la ruta.

    La dependencia ya deriva el contexto del club del camino; esta comprobacion es la garantia de
    que la ruta no puede operar sobre un tenant distinto del club que declara, ni siquiera si esa
    dependencia cambiara de forma. Si no coinciden es un error del servidor, nunca un 4xx: el
    cliente no ha hecho nada mal.
    """
    if context.tenant_club_id != club_id:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_STATE_ERROR)


async def _load_competitor(context: ActorContext, competitor_id: CompetitorId) -> Competitor:
    """Competidor del tenant del contexto; un competidor ajeno responde como uno inexistente.

    La busqueda va acotada al tenant **dentro** del SQL (no hay consulta global por identificador),
    asi que no se filtra que el identificador exista en otro tenant.
    """
    competitor = await get_competitor(competitor_id, tenant_club_id=context.tenant_club_id)
    if competitor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)
    return competitor


def _outcome(response_status: int, competitor: Competitor) -> OperationOutcome:
    """Lo unico que el protocolo guarda: el codigo y datos tecnicos de su lista cerrada.

    Ni el cuerpo de la peticion ni la respuesta HTTP entran aqui. La respuesta se reconstruye
    despues desde el recurso, que se vuelve a autorizar antes de un replay.
    """
    return OperationOutcome(
        response_status=response_status,
        metadata={"resource_id": competitor.id, "competitor_id": competitor.id},
        resource_type=_RESOURCE_TYPE,
        resource_id=competitor.id,
    )


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
        CompetitorDomainError,
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
) -> Competitor:
    """Ejecuta la operacion dentro del protocolo y devuelve el competidor resultante."""
    result = await _run_protocol(
        context=context,
        request=request,
        operation=operation,
        expected_status=expected_status,
        require_owner=require_owner,
    )
    if result.resource_id is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_STATE_ERROR)
    return await _load_competitor(context, CompetitorId(result.resource_id))


@router.post(
    "/clubs/{club_id}/competitors",
    dependencies=[Depends(require_f3_write_enabled)],
    response_model=CompetitorResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_competitor_endpoint(
    club_id: ClubId,
    body: CompetitorCreateBody,
    request: Request,
    context: ActorContext = Depends(actor_context_for_club),
) -> CompetitorResponse:
    """Alta de identidad en el club de la ruta. Exige ``Idempotency-Key``.

    Motivo por defecto del alta: ``PLANNED_ENTRY`` (lo resuelve el catalogo, no el router). No hay
    deduplicacion: dos altas con el mismo nombre son dos identidades (contrato S2).
    """
    _assert_context_matches_route(context, club_id)

    async def operation() -> OperationOutcome:
        competitor = await create_competitor(
            context,
            display_name=body.display_name,
            date_of_birth=body.date_of_birth,
            sex=body.sex,
            belt_id=body.belt_id,
            reason_code=body.reason_code,
            reason_note=body.reason_note,
        )
        return _outcome(status.HTTP_201_CREATED, competitor)

    competitor = await _execute(
        context=context,
        request=request,
        operation=operation,
        expected_status=status.HTTP_201_CREATED,
    )
    return CompetitorResponse.from_competitor(competitor)


@router.get("/clubs/{club_id}/competitors", response_model=CompetitorListResponse)
async def list_competitors_endpoint(
    club_id: ClubId,
    display_name: str | None = Query(
        None,
        description="Solo los competidores cuyo nombre contiene este texto (coincidencia literal).",
    ),
    include_inactive: bool = Query(
        False, description="Incluir tambien las identidades dadas de baja."
    ),
    pagination: PaginationCompetitors = Depends(),
    context: ActorContext = Depends(actor_context_for_club),
) -> CompetitorListResponse:
    """Competidores del club de la ruta, con filtros, paginacion y el total del filtro.

    El ambito es del servidor: el club tiene que ser el tenant del contexto (si no, ya lo ha
    rechazado la dependencia) y la consulta se acota a el dentro del SQL, nunca despues de leer. La
    respuesta es la proyeccion publica, no la fila.

    Leer no exige ``Idempotency-Key``: no hay reserva ni operacion de dominio que repetir.
    """
    _assert_context_matches_route(context, club_id)

    competitors = await list_competitors(
        context.tenant_club_id,
        display_name=display_name,
        include_inactive=include_inactive,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    count = await count_competitors(
        context.tenant_club_id,
        display_name=display_name,
        include_inactive=include_inactive,
    )
    # Afiliacion primaria abierta de **toda la pagina** en una sola consulta: la lista publica el
    # estado actual de cada competidor sin N+1 (S4.8d §3). Un competidor que no aparece ahi no tiene
    # afiliacion abierta y se proyecta como INDEPENDENT sin academia: no se adivina ninguna.
    affiliations = {
        row.competitor_id: row
        for row in await get_open_primary_affiliations(
            [row.id for row in competitors], tenant_club_id=context.tenant_club_id
        )
    }
    return CompetitorListResponse(
        count=count,
        competitors=[
            CompetitorListItem.from_competitor(row, affiliations.get(row.id)) for row in competitors
        ],
    )


@router.get("/clubs/{club_id}/competitors/{competitor_id}", response_model=CompetitorResponse)
async def get_competitor_endpoint(
    club_id: ClubId,
    competitor_id: CompetitorId,
    context: ActorContext = Depends(actor_context_for_club),
) -> CompetitorResponse:
    """Competidor concreto del club, con su estado real.

    El identificador se busca **siempre** acotado al tenant del contexto: un competidor de otro
    tenant no existe para esta ruta y responde el mismo 404 que uno inexistente.
    """
    _assert_context_matches_route(context, club_id)
    competitor = await _load_competitor(context, competitor_id)
    return CompetitorResponse.from_competitor(competitor)


@router.get(
    "/clubs/{club_id}/competitors/{competitor_id}/affiliations",
    response_model=CompetitorAffiliationsResponse,
)
async def list_competitor_affiliations_endpoint(
    club_id: ClubId,
    competitor_id: CompetitorId,
    include_history: bool = Query(
        False, description="Incluir tambien las afiliaciones ya cerradas (historial completo)."
    ),
    context: ActorContext = Depends(actor_context_for_club),
) -> CompetitorAffiliationsResponse:
    """Afiliaciones del competidor (S4.8a-3 §3), de solo lectura.

    Por defecto devuelve **solo las vigentes** (``valid_to`` NULL) para que la UI precargue la
    academia actual; con ``include_history=True`` se anade el historial ya cerrado. El competidor se
    busca **acotado al tenant** (``_load_competitor``), asi que un competidor de otro tenant
    responde el mismo 404 que uno inexistente. Leer no exige ``Idempotency-Key``: no hay reserva ni
    operacion de dominio que repetir.

    La proyeccion es la **existente** ``CompetitorAffiliationsResponse``, la misma que devuelve el
    paso a independiente: aqui solo se publica. No se toca la proyeccion certificada S3.7a
    (``CompetitorResponse``).
    """
    _assert_context_matches_route(context, club_id)
    await _load_competitor(context, competitor_id)
    affiliations = await get_competitor_affiliations(
        competitor_id,
        tenant_club_id=context.tenant_club_id,
        include_history=include_history,
    )
    return CompetitorAffiliationsResponse(
        competitor_id=competitor_id,
        affiliations=[
            CompetitorAffiliationResponse.from_affiliation(affiliation)
            for affiliation in affiliations
        ],
    )


@router.post(
    "/clubs/{club_id}/competitors/{competitor_id}/rename",
    dependencies=[Depends(require_f3_write_enabled)],
    response_model=CompetitorResponse,
)
async def rename_competitor_endpoint(
    club_id: ClubId,
    competitor_id: CompetitorId,
    request: Request,
    body: CompetitorRenameBody,
    context: ActorContext = Depends(actor_context_for_club),
) -> CompetitorResponse:
    """Renombrado controlado: cierra la entrada de nombre vigente y abre la nueva.

    Un nombre identico al vigente no cambia nada (sin historial ni auditoria). El competidor de otro
    tenant responde 404 antes de ejecutar nada.
    """
    _assert_context_matches_route(context, club_id)
    await _load_competitor(context, competitor_id)

    async def operation() -> OperationOutcome:
        competitor = await update_competitor_display_name(
            context,
            competitor_id,
            CompetitorBasicDataUpdate(display_name=body.display_name),
            reason_code=body.reason_code,
            reason_note=body.reason_note,
        )
        return _outcome(status.HTTP_200_OK, competitor)

    competitor = await _execute(
        context=context, request=request, operation=operation, expected_status=status.HTTP_200_OK
    )
    return CompetitorResponse.from_competitor(competitor)


@router.post(
    "/clubs/{club_id}/competitors/{competitor_id}/deactivate",
    dependencies=[Depends(require_f3_write_enabled)],
    response_model=CompetitorResponse,
)
async def deactivate_competitor_endpoint(
    club_id: ClubId,
    competitor_id: CompetitorId,
    request: Request,
    body: CompetitorLifecycleBody | None = None,
    context: ActorContext = Depends(actor_context_for_club),
) -> CompetitorResponse:
    """Baja logica del competidor. Exige relacion ``OWNER`` con el club.

    Idempotente: una segunda baja no audita de nuevo. No borra la identidad ni su historial, y no
    toca inscripciones ni snapshots (RS-9).
    """
    _assert_context_matches_route(context, club_id)
    await _load_competitor(context, competitor_id)
    reason_code = None if body is None else body.reason_code
    reason_note = None if body is None else body.reason_note

    async def operation() -> OperationOutcome:
        competitor = await deactivate_competitor(
            context, competitor_id, reason_code=reason_code, reason_note=reason_note
        )
        return _outcome(status.HTTP_200_OK, competitor)

    competitor = await _execute(
        context=context,
        request=request,
        operation=operation,
        expected_status=status.HTTP_200_OK,
        require_owner=True,
    )
    return CompetitorResponse.from_competitor(competitor)


@router.post(
    "/clubs/{club_id}/competitors/{competitor_id}/activate",
    dependencies=[Depends(require_f3_write_enabled)],
    response_model=CompetitorResponse,
)
async def activate_competitor_endpoint(
    club_id: ClubId,
    competitor_id: CompetitorId,
    request: Request,
    body: CompetitorLifecycleBody | None = None,
    context: ActorContext = Depends(actor_context_for_club),
) -> CompetitorResponse:
    """Reactivacion de una identidad dada de baja. Exige relacion ``OWNER`` con el club.

    Idempotente: si ya estaba activo no audita de nuevo. No toca el historial de nombres.
    """
    _assert_context_matches_route(context, club_id)
    await _load_competitor(context, competitor_id)
    reason_code = None if body is None else body.reason_code
    reason_note = None if body is None else body.reason_note

    async def operation() -> OperationOutcome:
        competitor = await activate_competitor(
            context, competitor_id, reason_code=reason_code, reason_note=reason_note
        )
        return _outcome(status.HTTP_200_OK, competitor)

    competitor = await _execute(
        context=context,
        request=request,
        operation=operation,
        expected_status=status.HTTP_200_OK,
        require_owner=True,
    )
    return CompetitorResponse.from_competitor(competitor)


@router.post(
    "/clubs/{club_id}/competitors/{competitor_id}/profile",
    dependencies=[Depends(require_f3_write_enabled)],
    response_model=CompetitorResponse,
)
async def update_competitor_profile_endpoint(
    club_id: ClubId,
    competitor_id: CompetitorId,
    body: CompetitorProfileBody,
    request: Request,
    context: ActorContext = Depends(actor_context_for_club),
) -> CompetitorResponse:
    """Reemplaza el perfil deportivo del competidor (S4.8a-3 §2).

    La politica de permisos es la de los datos basicos del contrato certificado (OWNER y
    COLLABORATOR): este endpoint no introduce ninguna regla de autorizacion nueva, y la puerta de
    escritura de S3.6b sigue siendo el unico interruptor. Sin cambios reales no hay evento de
    auditoria y la respuesta es la identidad tal cual estaba.
    """
    _assert_context_matches_route(context, club_id)

    async def operation() -> OperationOutcome:
        competitor = await update_competitor_profile(
            context,
            competitor_id,
            CompetitorSportsProfileUpdate(
                date_of_birth=body.date_of_birth, sex=body.sex, belt_id=body.belt_id
            ),
            reason_code=body.reason_code,
            reason_note=body.reason_note,
        )
        return _outcome(status.HTTP_200_OK, competitor)

    competitor = await _execute(
        context=context,
        request=request,
        operation=operation,
        expected_status=status.HTTP_200_OK,
    )
    return CompetitorResponse.from_competitor(competitor)


# --- S4.8a-3 §3: afiliacion del competidor ------------------------------------------------------
#
# Cuatro rutas de accion **OWNER-only** (sensibles): asignar, cambiar la primaria, cerrar una
# afiliacion y pasar a independiente. Todas pasan por el protocolo de idempotencia de S3.3c-4 con
# ``require_owner=True`` (COLLABORATOR -> 403 ROLE_NOT_ALLOWED; anonimo -> 401) y quedan detras de
# la puerta de escritura F3 (bandera apagada -> 503 antes de reservar).

_AFFILIATION_RESOURCE_TYPE = "competitor_affiliation"


def _affiliation_outcome(
    response_status: int, affiliation: CompetitorXSportsClub
) -> OperationOutcome:
    return OperationOutcome(
        response_status=response_status,
        metadata={
            "resource_id": affiliation.id,
            "competitor_id": affiliation.competitor_id,
        },
        resource_type=_AFFILIATION_RESOURCE_TYPE,
        resource_id=affiliation.id,
    )


def _independence_outcome(response_status: int, competitor_id: CompetitorId) -> OperationOutcome:
    return OperationOutcome(
        response_status=response_status,
        metadata={"resource_id": competitor_id, "competitor_id": competitor_id},
        resource_type=_AFFILIATION_RESOURCE_TYPE,
        resource_id=competitor_id,
    )


async def _load_affiliation(context: ActorContext, affiliation_id: int) -> CompetitorXSportsClub:
    affiliation = await get_affiliation(
        CompetitorXSportsClubId(affiliation_id), tenant_club_id=context.tenant_club_id
    )
    if affiliation is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_STATE_ERROR)
    return affiliation


async def _execute_affiliation(
    *,
    context: ActorContext,
    request: Request,
    operation: Callable[[], Awaitable[OperationOutcome]],
    expected_status: int,
) -> CompetitorXSportsClub:
    """Ejecuta una accion de afiliacion dentro del protocolo y devuelve la afiliacion resultante."""
    result = await _run_protocol(
        context=context,
        request=request,
        operation=operation,
        expected_status=expected_status,
        require_owner=True,
    )
    if result.resource_id is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_STATE_ERROR)
    return await _load_affiliation(context, result.resource_id)


@router.post(
    "/clubs/{club_id}/competitors/{competitor_id}/affiliations",
    dependencies=[Depends(require_f3_write_enabled)],
    response_model=CompetitorAffiliationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def assign_affiliation_endpoint(
    club_id: ClubId,
    competitor_id: CompetitorId,
    body: CompetitorAffiliationBody,
    request: Request,
    context: ActorContext = Depends(actor_context_for_club),
) -> CompetitorAffiliationResponse:
    """Abre una afiliacion vigente con una academia del tenant (OWNER-only)."""
    _assert_context_matches_route(context, club_id)

    async def operation() -> OperationOutcome:
        affiliation = await assign_affiliation(
            context, competitor_id, sports_club_id=body.sports_club_id
        )
        return _affiliation_outcome(status.HTTP_201_CREATED, affiliation)

    affiliation = await _execute_affiliation(
        context=context,
        request=request,
        operation=operation,
        expected_status=status.HTTP_201_CREATED,
    )
    return CompetitorAffiliationResponse.from_affiliation(affiliation)


@router.post(
    "/clubs/{club_id}/competitors/{competitor_id}/affiliations/{affiliation_id}/primary",
    dependencies=[Depends(require_f3_write_enabled)],
    response_model=CompetitorAffiliationResponse,
)
async def promote_affiliation_endpoint(
    club_id: ClubId,
    competitor_id: CompetitorId,
    affiliation_id: CompetitorXSportsClubId,
    request: Request,
    context: ActorContext = Depends(actor_context_for_club),
) -> CompetitorAffiliationResponse:
    """Convierte en primaria una afiliacion vigente, cerrando la anterior (OWNER-only)."""
    _assert_context_matches_route(context, club_id)

    async def operation() -> OperationOutcome:
        affiliation = await promote_affiliation(context, competitor_id, affiliation_id)
        return _affiliation_outcome(status.HTTP_200_OK, affiliation)

    affiliation = await _execute_affiliation(
        context=context,
        request=request,
        operation=operation,
        expected_status=status.HTTP_200_OK,
    )
    return CompetitorAffiliationResponse.from_affiliation(affiliation)


@router.post(
    "/clubs/{club_id}/competitors/{competitor_id}/affiliations/{affiliation_id}/close",
    dependencies=[Depends(require_f3_write_enabled)],
    response_model=CompetitorAffiliationResponse,
)
async def close_affiliation_endpoint(
    club_id: ClubId,
    competitor_id: CompetitorId,
    affiliation_id: CompetitorXSportsClubId,
    request: Request,
    context: ActorContext = Depends(actor_context_for_club),
) -> CompetitorAffiliationResponse:
    """Cierra una afiliacion vigente escribiendo ``valid_to`` (OWNER-only)."""
    _assert_context_matches_route(context, club_id)

    async def operation() -> OperationOutcome:
        affiliation = await close_affiliation(context, competitor_id, affiliation_id)
        return _affiliation_outcome(status.HTTP_200_OK, affiliation)

    affiliation = await _execute_affiliation(
        context=context,
        request=request,
        operation=operation,
        expected_status=status.HTTP_200_OK,
    )
    return CompetitorAffiliationResponse.from_affiliation(affiliation)


@router.post(
    "/clubs/{club_id}/competitors/{competitor_id}/independence",
    dependencies=[Depends(require_f3_write_enabled)],
    response_model=CompetitorAffiliationsResponse,
)
async def go_independent_endpoint(
    club_id: ClubId,
    competitor_id: CompetitorId,
    request: Request,
    context: ActorContext = Depends(actor_context_for_club),
) -> CompetitorAffiliationsResponse:
    """Cierra todas las afiliaciones vigentes del competidor (OWNER-only)."""
    _assert_context_matches_route(context, club_id)

    async def operation() -> OperationOutcome:
        await go_independent(context, competitor_id)
        return _independence_outcome(status.HTTP_200_OK, competitor_id)

    result = await _run_protocol(
        context=context,
        request=request,
        operation=operation,
        expected_status=status.HTTP_200_OK,
        require_owner=True,
    )
    if result.resource_id is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_STATE_ERROR)
    competitor = await _load_competitor(context, CompetitorId(result.resource_id))
    affiliations = await get_competitor_affiliations(
        competitor.id, tenant_club_id=context.tenant_club_id, include_history=True
    )
    return CompetitorAffiliationsResponse(
        competitor_id=competitor.id,
        affiliations=[CompetitorAffiliationResponse.from_affiliation(a) for a in affiliations],
    )
