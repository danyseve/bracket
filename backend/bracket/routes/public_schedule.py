"""S4.5 — lectura publica del planificador: la pantalla de la grada (LAB ONLY).

Una sola ruta, de lectura y sin sesion: ``GET /public/tournaments/{tournament_id}/schedule``
devuelve **el mismo cuerpo** que la vista del organizador (§12). La pantalla TV/tablet no deriva
nada por su cuenta: ``NOW``, ``NEXT``, ``WAITING``, ``COMPLETED`` y ``DELAYED`` los decide el
servidor, y el cuerpo pasa por el guardian de privacidad antes de salir (§11).

Quien puede leerla: cualquiera, si el torneo tiene activado el panel publico
(``user_authenticated_or_public_dashboard``); si no, 401. No hay version publica del cuadro: esto es
el calendario por tatami, no el arbol de combates.

Es una lectura y no tiene efectos: no reserva idempotencia, no mueve ningun combate y no escribe
auditoria. No lleva la puerta de escritura de F3 porque no escribe nada.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from bracket.config import config
from bracket.models.db.schedule_api import TournamentScheduleResponse
from bracket.models.db.user import UserPublic
from bracket.routes.auth import user_authenticated_or_public_dashboard
from bracket.routes.domain_schedule import load_schedule_view
from bracket.utils.id_types import TournamentId

router = APIRouter(prefix=config.api_prefix)


@router.get(
    "/public/tournaments/{tournament_id}/schedule",
    response_model=TournamentScheduleResponse,
)
async def get_public_tournament_schedule_endpoint(
    tournament_id: TournamentId,
    _: UserPublic = Depends(user_authenticated_or_public_dashboard),
) -> TournamentScheduleResponse:
    """Calendario publico del torneo: por tatami, AHORA, SIGUIENTE y los siguientes (§12)."""
    return await load_schedule_view(tournament_id=tournament_id)
