"""Lectura de la estructura competitiva para la generacion explicita (S3.5a, F3B, LAB ONLY).

Una sola lectura y sin escritura: los cuadros de eliminacion directa del torneo, tal y como los lee
la generacion certificada de F2C (``get_full_tournament_details``), en orden determinista. El ambito
por tenant **no** se aplica aqui porque no hay ninguna decision que tomar: quien llama ya ha
autorizado el torneo leyendo antes su ventana (la que responde el 404 de otro tenant), igual que en
la revision de S3.4d. Este modulo no escribe ni bloquea nada.
"""

from __future__ import annotations

from bracket.models.db.stage_item import StageType
from bracket.models.db.util import StageItemWithRounds
from bracket.sql.stages import get_full_tournament_details
from bracket.utils.id_types import TournamentId


async def get_single_elimination_stage_items(
    tournament_id: TournamentId,
) -> list[StageItemWithRounds]:
    """Cuadros de eliminacion directa del torneo, en orden determinista por identificador.

    El orden no depende del plan de ejecucion de la base de datos: la generacion reparte ranuras y
    escribe auditoria, asi que dos llamadas con el mismo estado tienen que producir lo mismo.
    """
    stage_items = [
        stage_item
        for stage in await get_full_tournament_details(tournament_id)
        for stage_item in stage.stage_items
        if stage_item.type == StageType.SINGLE_ELIMINATION
    ]
    return sorted(stage_items, key=lambda stage_item: stage_item.id)
