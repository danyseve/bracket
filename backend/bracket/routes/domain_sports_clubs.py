"""Rutas HTTP F3 de la academia deportiva: logo, alta y edicion (S4.8a-3 §4 y S4.8g-2 §14).

En producto la entidad se llama **Club** (S4.8g-2 §1/§2); la tabla sigue siendo ``sports_clubs``.

Sigue el patron del logo de torneo, pero **endurecido**:

* solo PNG, JPEG y WEBP: el tipo se decide por el **contenido real** (magic bytes), no por lo que
  declara el cliente; SVG (y cualquier otro tipo) se rechaza aunque venga como ``image/png``;
* la **extension** declarada tiene que existir y **coincidir** con el tipo real detectado;
* tamano maximo ``MAX_LOGO_BYTES`` (2 MB);
* el nombre del fichero lo **genera el servidor** (``uuid4``) con una extension canonica derivada
  del tipo real: el nombre que envia el cliente nunca toca el disco, de modo que un
  ``../../`` o un nombre absoluto no puede escapar del directorio de logos;
* en la base solo se guarda la **ruta relativa** (``sports_clubs.logo_path``); el fichero, en disco;
* soporta **upload** (sin logo previo), **replace** (reemplazo) y **remove** (sin fichero);
* el logo anterior **no se borra** hasta que el nuevo este validado y persistido: si el nuevo se
  rechaza, el anterior sobrevive intacto.

Contrato de la capa:

* la identidad y el tenant salen **solo** de la sesion (``owner_actor_context_for_club``): el cuerpo
  no aporta ``actor_user_id`` ni ``tenant_club_id``, y la operacion es **OWNER-only** (un
  COLLABORATOR recibe 403 y el anonimo 401);
* la escritura queda detras de la puerta de S3.6b (``F3_WRITE_ENABLED``): con la bandera apagada
  responde 503 antes de escribir en disco o en la base;
* la respuesta es una proyeccion publica (``SportsClubResponse``) que publica ``logo_path``; no se
  devuelve la fila interna;
* la auditoria es un codigo cerrado, entidad ``sports_club``, accion ``UPDATE``, codigo
  ``CLUB_LOGO_CHANGE``.

S4.8g-2 §14 anade el **CRUD minimo** de la entidad Club, con el mismo protocolo que las rutas de
competidores (S3.3c-4: toda escritura exige ``Idempotency-Key`` y pasa por el protocolo):

* ``POST /clubs/{club_id}/sports-clubs`` (201) da de alta un club del tenant (OWNER);
* ``PATCH /clubs/{club_id}/sports-clubs/{sports_club_id}`` (200) renombra y/o activa/desactiva;
* el tenant de la ruta tiene que ser el del contexto, la identidad la resuelve el servidor y no hay
  borrado fisico: desactivar es el flujo normal (§14).
"""

from __future__ import annotations

import pathlib
from collections.abc import Awaitable, Callable
from uuid import uuid4

import aiofiles
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from starlette import status

from bracket.config import config
from bracket.logic.idempotency_protocol import OperationOutcome, ProtocolResult
from bracket.logic.sports_clubs import (
    DuplicateSportsClubNameError,
    InsufficientPrivilegesError,
    SportsClubDomainError,
    SportsClubNotFoundError,
    SportsClubValidationError,
    TenantNotAuthorizedError,
    create_sports_club,
    set_sports_club_logo,
    update_sports_club,
)
from bracket.models.db.domain import ActorContext, SportsClub
from bracket.models.db.sports_club_api import (
    SportsClubCreateBody,
    SportsClubResponse,
    SportsClubUpdateBody,
)
from bracket.routes.domain_auth import actor_context_for_club, owner_actor_context_for_club
from bracket.routes.domain_idempotency import execute_idempotent_operation, idempotency_request
from bracket.routes.f3_write_gate import require_f3_write_enabled
from bracket.sql.domain_reads import get_sports_club
from bracket.utils.id_types import ClubId, SportsClubId

router = APIRouter(prefix=config.api_prefix)

MAX_LOGO_BYTES = 2 * 1024 * 1024
_LOGO_DIR = pathlib.Path("static/sports-club-logos")
_REASON_CODE = "CLUB_LOGO_CHANGE"
_INVALID_LOGO = "INVALID_CLUB_LOGO"
_NOT_FOUND = "NOT_FOUND"
_STATE_ERROR = "STATE_ERROR"
_INVALID_REQUEST = "INVALID_REQUEST"
_DUPLICATE = "DUPLICATE_CLUB"
_RESOURCE_TYPE = "sports_club"

# Tipo real (detectado por contenido) -> extension canonica del fichero que se guarda.
_CANONICAL_EXTENSION = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
# Extension declarada -> tipo que deberia acompanarla (validacion de coherencia).
_DECLARED_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _http_exception(error: Exception) -> HTTPException:
    if isinstance(error, SportsClubNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    if isinstance(error, DuplicateSportsClubNameError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_DUPLICATE)
    if isinstance(error, SportsClubValidationError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_INVALID_REQUEST)
    if isinstance(error, (TenantNotAuthorizedError, InsufficientPrivilegesError)):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="FORBIDDEN")
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_STATE_ERROR)


def _sniff_content_type(content: bytes) -> str | None:
    """Tipo real por magic bytes; ``None`` si no es PNG/JPEG/WEBP (incluye SVG)."""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def _validated_relative_path(file: UploadFile, content: bytes) -> str:
    """Valida contenido/extension/tamano y devuelve la ruta relativa generada por el servidor."""
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_INVALID_LOGO)
    if len(content) > MAX_LOGO_BYTES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_INVALID_LOGO)

    sniffed = _sniff_content_type(content)
    if sniffed is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_INVALID_LOGO)

    declared_suffix = pathlib.Path(file.filename or "").suffix.lower()
    if _DECLARED_TYPES.get(declared_suffix) != sniffed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_INVALID_LOGO)

    # Nombre de fichero generado por el servidor: el del cliente jamas se usa en disco.
    return str(_LOGO_DIR / f"{uuid4().hex}{_CANONICAL_EXTENSION[sniffed]}")


def _assert_context_matches_route(context: ActorContext, club_id: ClubId) -> None:
    """La ruta declara el tenant; el contexto autorizado tiene que ser el mismo.

    Mismo criterio que en competidores: si no coinciden es un error del servidor (500), nunca un
    4xx, porque el cliente no ha hecho nada mal.
    """
    if context.tenant_club_id != club_id:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_STATE_ERROR)


def _outcome(response_status: int, sports_club_id: int) -> OperationOutcome:
    """Lo unico que el protocolo guarda: codigo y datos tecnicos de su lista cerrada."""
    return OperationOutcome(
        response_status=response_status,
        metadata={"resource_id": sports_club_id},
        resource_type=_RESOURCE_TYPE,
        resource_id=sports_club_id,
    )


async def _run_protocol(
    *,
    context: ActorContext,
    request: Request,
    operation: Callable[[], Awaitable[OperationOutcome]],
    expected_status: int,
) -> ProtocolResult:
    """Ejecuta la operacion en el protocolo de S3.3c-4 y comprueba el codigo declarado.

    El codigo HTTP lo fija la ruta y el protocolo guarda ese mismo codigo: una reserva repetida que
    declarase otro no se responde como exito. Los errores de dominio se traducen al catalogo HTTP.
    """
    try:
        result = await execute_idempotent_operation(
            context=context,
            request=await idempotency_request(request),
            operation=operation,
            require_owner=True,
        )
    except (
        SportsClubDomainError,
        TenantNotAuthorizedError,
        InsufficientPrivilegesError,
    ) as error:
        raise _http_exception(error) from error

    if result.response_status != expected_status:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_STATE_ERROR)

    return result


async def _load_sports_club(context: ActorContext, sports_club_id: SportsClubId) -> SportsClub:
    """Recarga el club del tenant del contexto para reconstruir la respuesta publica.

    La consulta va acotada al tenant **dentro** del SQL (``get_sports_club``), asi que un club de
    otro tenant responde como inexistente y este camino tampoco filtra su existencia.
    """
    sports_club = await get_sports_club(sports_club_id, tenant_club_id=context.tenant_club_id)
    if sports_club is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    return sports_club


@router.post(
    "/clubs/{club_id}/sports-clubs",
    dependencies=[Depends(require_f3_write_enabled)],
    response_model=SportsClubResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_sports_club_endpoint(
    club_id: ClubId,
    body: SportsClubCreateBody,
    request: Request,
    context: ActorContext = Depends(actor_context_for_club),
) -> SportsClubResponse:
    """Alta de un club del tenant de la ruta. Exige ``Idempotency-Key`` y relacion OWNER.

    No hay deduplicacion por nombre global (§8): solo se rechaza (409) un duplicado obvio **dentro
    del mismo tenant**. Dos organizaciones distintas pueden tener clubes con el mismo nombre.
    """
    _assert_context_matches_route(context, club_id)

    async def operation() -> OperationOutcome:
        sports_club = await create_sports_club(
            context,
            name=body.name,
            active=body.active,
            reason_code=body.reason_code,
            reason_note=body.reason_note,
        )
        return _outcome(status.HTTP_201_CREATED, sports_club.id)

    result = await _run_protocol(
        context=context,
        request=request,
        operation=operation,
        expected_status=status.HTTP_201_CREATED,
    )
    if result.resource_id is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_STATE_ERROR)
    return SportsClubResponse.from_sports_club(
        await _load_sports_club(context, SportsClubId(result.resource_id))
    )


@router.patch(
    "/clubs/{club_id}/sports-clubs/{sports_club_id}",
    dependencies=[Depends(require_f3_write_enabled)],
    response_model=SportsClubResponse,
)
async def update_sports_club_endpoint(
    club_id: ClubId,
    sports_club_id: SportsClubId,
    body: SportsClubUpdateBody,
    request: Request,
    context: ActorContext = Depends(actor_context_for_club),
) -> SportsClubResponse:
    """Renombra y/o activa/desactiva un club del tenant. Exige ``Idempotency-Key`` y OWNER.

    Un club de otro tenant responde 404 igual que uno inexistente, y no hay borrado fisico:
    desactivar (``active=false``) es el flujo normal de retirada.
    """
    _assert_context_matches_route(context, club_id)

    async def operation() -> OperationOutcome:
        sports_club = await update_sports_club(
            context,
            sports_club_id,
            name=body.name,
            active=body.active,
            reason_code=body.reason_code,
            reason_note=body.reason_note,
        )
        return _outcome(status.HTTP_200_OK, sports_club.id)

    result = await _run_protocol(
        context=context,
        request=request,
        operation=operation,
        expected_status=status.HTTP_200_OK,
    )
    if result.resource_id is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_STATE_ERROR)
    return SportsClubResponse.from_sports_club(
        await _load_sports_club(context, SportsClubId(result.resource_id))
    )


@router.post(
    "/clubs/{club_id}/sports-clubs/{sports_club_id}/logo",
    dependencies=[Depends(require_f3_write_enabled)],
    response_model=SportsClubResponse,
)
async def set_sports_club_logo_endpoint(
    club_id: ClubId,
    sports_club_id: SportsClubId,
    context: ActorContext = Depends(owner_actor_context_for_club),
    file: UploadFile | None = File(None),
) -> SportsClubResponse:
    """Sube, reemplaza o quita (sin fichero) el logo de la academia del tenant."""
    if context.tenant_club_id != club_id:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_STATE_ERROR)

    written_path: pathlib.Path | None = None
    try:
        if file is None:
            new_logo_path: str | None = None
        else:
            content = await file.read()
            new_logo_path = _validated_relative_path(file, content)
            _LOGO_DIR.mkdir(parents=True, exist_ok=True)
            written_path = pathlib.Path(new_logo_path)
            async with aiofiles.open(written_path, "wb") as handle:
                await handle.write(content)

        updated, previous = await set_sports_club_logo(
            context,
            sports_club_id,
            logo_path=new_logo_path,
            reason_code=_REASON_CODE,
        )
    except SportsClubDomainError as error:
        if written_path is not None:
            written_path.unlink(missing_ok=True)
        raise _http_exception(error) from error

    # El logo anterior solo se retira despues de que el nuevo este persistido.
    if previous is not None and previous != new_logo_path:
        pathlib.Path(previous).unlink(missing_ok=True)

    return SportsClubResponse.from_sports_club(updated)
