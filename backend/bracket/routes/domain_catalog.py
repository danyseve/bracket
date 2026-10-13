"""Rutas HTTP F3 de lectura de los catalogos del club (S4.8a-4a, LAB ONLY).

Lecturas que faltaban para que el frontend de a-4 pueda cargar el **catalogo real de cinturones**,
listar las **academias** (sports clubs) del tenant y (en ``domain_competitors.py``) leer la
afiliacion vigente de un competidor. No se anade ningun concepto de dominio nuevo: cada ruta es la
traduccion de un lector ya existente de ``bracket/sql/domain_reads.py``.

Contrato de esta capa:

* la identidad y el tenant salen **solo** de la sesion y del club de la ruta
  (``actor_context_for_club``): el cuerpo no aporta ``actor_user_id`` ni ``tenant_club_id`` y estas
  rutas **no** aceptan cuerpo;
* la autorizacion es la de los GET ya existentes bajo ``/clubs/...``: cualquier relacion con el
  club puede leer (OWNER y COLLABORATOR -> 200; anonimo -> 401; actor de otro club -> 401, que es
  la denegacion indistinguible de ``actor_context_for_club``). No se relaja ni se cambia el RBAC de
  ninguna otra ruta;
* el ambito del tenant se aplica **dentro** del SQL (academias por ``tenant_club_id``), nunca
  despues de leer. El catalogo de cinturones es de **plataforma** y no lleva ambito de tenant: la
  ruta exige pertenecer al club por coherencia con el resto de lecturas bajo ``/clubs/...``;
* la respuesta es una proyeccion publica (``BeltResponse``, ``SportsClubResponse``), nunca la fila
  interna;
* son lecturas: no exigen ``Idempotency-Key``, no auditan y se publican con la puerta de escritura
  F3 (``F3_WRITE_ENABLED``) apagada.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from starlette import status

from bracket.config import config
from bracket.models.db.catalog_api import BeltResponse
from bracket.models.db.domain import ActorContext
from bracket.models.db.sports_club_api import SportsClubResponse
from bracket.routes.domain_auth import actor_context_for_club
from bracket.sql.domain_reads import get_sports_clubs_for_tenant, list_belts
from bracket.utils.id_types import ClubId

router = APIRouter(prefix=config.api_prefix)

_STATE_ERROR = "STATE_ERROR"


def _assert_context_matches_route(context: ActorContext, club_id: ClubId) -> None:
    """El tenant autorizado es el club de la ruta.

    La dependencia ya deriva el contexto del club del camino; esta comprobacion es la garantia de
    que la ruta no puede leer un tenant distinto del club que declara, ni siquiera si esa
    dependencia cambiara de forma (mismo criterio que ``domain_competitors.py``). Si no coinciden es
    un error del servidor, nunca un 4xx: el cliente no ha hecho nada mal.
    """
    if context.tenant_club_id != club_id:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_STATE_ERROR)


@router.get("/clubs/{club_id}/belts", response_model=list[BeltResponse])
async def list_belts_endpoint(
    club_id: ClubId,
    include_inactive: bool = Query(
        False, description="Incluir tambien los cinturones retirados del catalogo."
    ),
    context: ActorContext = Depends(actor_context_for_club),
) -> list[BeltResponse]:
    """Catalogo de cinturones de la tabla ``belts`` (S4.8a-1).

    Es un catalogo de **plataforma**: no se acota al tenant (lo comparten todos), pero la ruta
    exige pertenecer al club, igual que el resto de lecturas bajo ``/clubs/...``. El orden es el
    del lector (escala y rango). Leer no exige ``Idempotency-Key``: no hay nada que repetir.
    """
    _assert_context_matches_route(context, club_id)
    belts = await list_belts(include_inactive=include_inactive)
    return [BeltResponse.from_belt(belt) for belt in belts]


@router.get("/clubs/{club_id}/sports-clubs", response_model=list[SportsClubResponse])
async def list_sports_clubs_endpoint(
    club_id: ClubId,
    include_inactive: bool = Query(
        False, description="Incluir tambien las academias dadas de baja."
    ),
    context: ActorContext = Depends(actor_context_for_club),
) -> list[SportsClubResponse]:
    """Academias del club del tenant de la ruta, con su logo ya persistido.

    La consulta se acota al tenant **dentro** del SQL (``tenant_club_id``) y no incluye las de
    plataforma (``tenant_club_id`` NULL): una academia de otro tenant no existe para esta ruta. Por
    defecto solo las activas; con ``include_inactive=True`` se anaden las dadas de baja.
    """
    _assert_context_matches_route(context, club_id)
    sports_clubs = await get_sports_clubs_for_tenant(
        context.tenant_club_id, include_inactive=include_inactive
    )
    return [SportsClubResponse.from_sports_club(sports_club) for sports_club in sports_clubs]
