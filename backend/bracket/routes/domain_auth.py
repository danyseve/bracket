"""Contexto de actor autenticado para las operaciones de dominio F3 (S3.3b, LAB ONLY).

Este modulo **no declara rutas**: expone dependencias de FastAPI que construyen el
``ActorContext`` (``bracket/models/db/domain.py``) exclusivamente a partir de una sesion
autenticada y de una organizacion autorizada. La capa HTTP funcional (fases posteriores)
sera la unica que traduzca las excepciones del dominio a respuestas.

Reutiliza el mecanismo de autenticacion existente (``bracket/routes/auth.py``): JWT HS256,
``oauth2_scheme`` y ``check_jwt_and_get_user`` (firma, expiracion, usuario real y
``users.active``). No se crea un segundo mecanismo ni se modifica el formato del token.

Contrato HTTP (docs/26 §23):

* **401** ``{"detail": "Not authenticated"}``: peticion sin cabecera ``Authorization``
  (lo emite ``oauth2_scheme``, igual que el resto de rutas protegidas del producto).
* **401** ``{"detail": "Could not validate credentials"}``: JWT invalido, caducado o
  manipulado, usuario inexistente, usuario inactivo y usuario sin relacion con el club de la
  ruta. Es la denegacion indistinguible de ``user_authenticated_for_club``
  (``routes/auth.py:132``): "club ajeno" y "club inexistente" responden exactamente igual.
* **404** ``{"detail": "Not Found"}``: torneo de otro tenant y torneo inexistente (mismo
  cuerpo y mismo codigo, sin revelar existencia) en el ambito por torneo.
* **403** ``{"detail": "Insufficient role for this organization"}``: rol insuficiente
  **despues** de validar el acceso al tenant; nunca antes, para no revelar existencia.

Reglas del contrato:

* ``tenant_club_id``, ``actor_user_id`` y ``actor_label`` se resuelven en servidor desde el
  JWT y la base de datos; jamas se aceptan desde el payload, el rol del payload ni cabeceras
  manipulables que identifiquen usuario u organizacion.
* ``clubs`` es el organizador (tenant); ``sports_clubs`` son las academias deportivas y
  **no** son tenants: una academia puede aparecer como inscrita y seguir siendo ajena al
  tenant del torneo.
* Ambito por torneo: el tenant es ``tournaments.club_id`` leido de la base de datos, de modo
  que cambiar el ``tournament_id`` no permite acceso cruzado.
* Sin cache de roles ni de pertenencias: cada peticion vuelve a resolver la relacion en la
  base de datos, de forma que revocar una relacion se aplica de inmediato. El dominio
  (``logic/competitors.py``) revalida ademas la relacion a partir del contexto.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status

from bracket.models.db.domain import ActorContext
from bracket.models.db.user import UserPublic
from bracket.models.db.user_x_club import UserXClubRelation
from bracket.routes.auth import check_jwt_and_get_user, oauth2_scheme
from bracket.sql.tournaments import sql_get_tournament_club_id
from bracket.sql.users import get_user_relation_to_club
from bracket.utils.id_types import ClubId, TournamentId

_UNAUTHENTICATED_DETAIL = "Could not validate credentials"
_FORBIDDEN_DETAIL = "Insufficient role for this organization"
# Mismo cuerpo para "recurso de otro tenant" y "recurso inexistente": no revela existencia.
_NOT_FOUND_DETAIL = "Not Found"


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=_UNAUTHENTICATED_DETAIL,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden() -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_FORBIDDEN_DETAIL)


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)


async def _authenticated_user(token: str) -> UserPublic:
    """Usuario real y activo a partir del JWT; cualquier fallo es la denegacion indistinguible."""
    user = await check_jwt_and_get_user(token)
    if user is None:
        raise _unauthorized()

    return user


def _context(user: UserPublic, tenant_club_id: ClubId) -> ActorContext:
    """Contexto construido en servidor: la etiqueta es el nombre del usuario autenticado."""
    return ActorContext(
        tenant_club_id=tenant_club_id,
        actor_user_id=user.id,
        actor_label=user.name,
    )


async def _actor_context_for_club(
    club_id: ClubId, token: str, *, require_owner: bool
) -> ActorContext:
    user = await _authenticated_user(token)
    relation = await get_user_relation_to_club(club_id, user.id)
    if relation is None:
        # El club es el ambito de autorizacion, no un recurso de la peticion: "club ajeno" y
        # "club inexistente" comparten la denegacion indistinguible del contrato existente.
        raise _unauthorized()

    if require_owner and relation is not UserXClubRelation.OWNER:
        # Ya se ha demostrado acceso al tenant: aqui el 403 no revela existencia.
        raise _forbidden()

    return _context(user, club_id)


async def _actor_context_for_tournament(
    tournament_id: TournamentId, token: str, *, require_owner: bool
) -> ActorContext:
    user = await _authenticated_user(token)
    club_id = await sql_get_tournament_club_id(tournament_id)
    if club_id is None:
        raise _not_found()

    relation = await get_user_relation_to_club(club_id, user.id)
    if relation is None:
        # Torneo de otro tenant: mismo 404 que un torneo inexistente.
        raise _not_found()

    if require_owner and relation is not UserXClubRelation.OWNER:
        raise _forbidden()

    return _context(user, club_id)


async def actor_context_for_club(
    club_id: ClubId, token: str = Depends(oauth2_scheme)
) -> ActorContext:
    """Contexto del tenant ``club_id`` de la ruta, con cualquier relacion autorizada."""
    return await _actor_context_for_club(club_id, token, require_owner=False)


async def owner_actor_context_for_club(
    club_id: ClubId, token: str = Depends(oauth2_scheme)
) -> ActorContext:
    """Igual que ``actor_context_for_club``, pero exige relacion ``OWNER``."""
    return await _actor_context_for_club(club_id, token, require_owner=True)


async def actor_context_for_tournament(
    tournament_id: TournamentId, token: str = Depends(oauth2_scheme)
) -> ActorContext:
    """Contexto del tenant organizador de ``tournament_id``, con cualquier relacion autorizada."""
    return await _actor_context_for_tournament(tournament_id, token, require_owner=False)


async def owner_actor_context_for_tournament(
    tournament_id: TournamentId, token: str = Depends(oauth2_scheme)
) -> ActorContext:
    """Igual que ``actor_context_for_tournament``, pero exige relacion ``OWNER``."""
    return await _actor_context_for_tournament(tournament_id, token, require_owner=True)
