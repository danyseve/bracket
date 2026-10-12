"""Cola de trabajo de un tatami para el marcador (S4.4, F3C, LAB ONLY).

Lectura **de solo lectura** y por tatami. Existe porque hoy cada marcador tendria que reconstruir
la cola desde la lectura completa del torneo, y esa reconstruccion es justo donde se cuelan las
inferencias: reordenar por marcador, decidir "terminado" desde ``score=0`` o tomar el ``court_id``
del combate como si fuera la asignacion del cuadro.

El contrato que fija esta ruta:

* el tatami de la cola es un tatami **real del torneo** (si no existe en ese torneo, 404; da igual
  que sea de otro torneo del mismo tenant o que no exista);
* un cuadro entra en la cola por su asignacion (``stage_items.court_id``, S4.2/S4.3);
* el orden es el del contrato de lectura de cuadros y no se toca;
* cada combate es ``playable`` o ``waiting_on_winners`` con sus pases pendientes;
* ``next_match_id`` es el primer jugable en ese orden;
* **no** se publica combate actual ni terminado: el modelo no tiene estado oficial de combate y no
  se infiere del marcador.

Mismo criterio de sesion que la lectura de cuadros: sin sesion solo la vista publica (sin rondas en
borrador); con sesion, todo. Es lectura pura: no escribe, no audita y no necesita idempotencia.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from starlette import status

from bracket.config import config
from bracket.logic.court_queue import build_court_queue
from bracket.models.db.court_queue import CourtQueueResponse
from bracket.models.db.user import UserPublic
from bracket.routes.auth import user_authenticated_or_public_dashboard
from bracket.sql.courts import get_all_courts_in_tournament
from bracket.sql.stages import get_full_tournament_details
from bracket.sql.tournaments import sql_get_tournament
from bracket.utils.id_types import CourtId, TournamentId

router = APIRouter(prefix=config.api_prefix)


@router.get(
    "/tournaments/{tournament_id}/courts/{court_id}/queue", response_model=CourtQueueResponse
)
async def get_court_queue(
    tournament_id: TournamentId,
    court_id: CourtId,
    user: UserPublic = Depends(user_authenticated_or_public_dashboard),
    no_draft_rounds: bool = False,
) -> CourtQueueResponse:
    """Cola del tatami: cuadros asignados, sus combates y el siguiente jugable."""
    if no_draft_rounds is False and user is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Can't view draft rounds when not authorized",
        )

    courts = await get_all_courts_in_tournament(tournament_id)
    court = next((item for item in courts if item.id == court_id), None)
    if court is None:
        # Tatami inexistente, de otro torneo o de otro tenant: la misma respuesta para los tres.
        # No se distingue cual de los casos es: el cliente no tiene por que poder enumerar torneos.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Could not find court with id {court_id}",
        )

    tournament = await sql_get_tournament(tournament_id)
    stages = await get_full_tournament_details(tournament_id, no_draft_rounds=no_draft_rounds)
    return build_court_queue(tournament=tournament, court=court, stages=stages)
