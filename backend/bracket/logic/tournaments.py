import aiofiles.os
import asyncpg  # type: ignore[import-untyped]

from bracket.database import database
from bracket.sql.courts import sql_delete_courts_of_tournament
from bracket.sql.players import sql_delete_players_of_tournament
from bracket.sql.rankings import get_all_rankings_in_tournament, sql_delete_ranking
from bracket.sql.shared import sql_delete_stage_item_matches, sql_delete_stage_item_relations
from bracket.sql.stage_items import sql_delete_stage_item
from bracket.sql.stages import get_full_tournament_details, sql_delete_stage
from bracket.sql.teams import sql_delete_teams_of_tournament
from bracket.sql.tournaments import sql_delete_tournament, sql_get_tournament
from bracket.utils.errors import TournamentDeleteConflictError
from bracket.utils.id_types import TournamentId
from bracket.utils.logging import logger


async def get_tournament_logo_path(tournament_id: TournamentId) -> str | None:
    tournament = await sql_get_tournament(tournament_id)
    logo_path = f"static/tournament-logos/{tournament.logo_path}" if tournament.logo_path else None
    return logo_path if logo_path is not None and await aiofiles.os.path.exists(logo_path) else None


async def delete_tournament_logo(tournament_id: TournamentId) -> None:
    logo_path = await get_tournament_logo_path(tournament_id)
    if logo_path is not None:
        await aiofiles.os.remove(logo_path)


async def delete_tournament_rows(tournament_id: TournamentId) -> None:
    """
    Elimina las filas del torneo en orden seguro (hojas primero), auditado contra las FK reales del
    esquema con `tests/integration_tests/tournament_delete_fk_coverage_test.py`:

    matches -> relations (rounds y stage_item_inputs) -> stage_items -> stages -> rankings ->
    players -> courts -> teams -> tournaments.

    Requiere una transaccion abierta por el llamante: `sql_delete_tournament_completely` lo hace.
    """
    stages = await get_full_tournament_details(tournament_id)

    for stage in stages:
        for stage_item in stage.stage_items:
            await sql_delete_stage_item_matches(stage_item.id)

    for stage in stages:
        for stage_item in stage.stage_items:
            await sql_delete_stage_item_relations(stage_item.id)

    for stage in stages:
        for stage_item in stage.stage_items:
            await sql_delete_stage_item(stage_item.id)

        await sql_delete_stage(tournament_id, stage.id)

    for ranking in await get_all_rankings_in_tournament(tournament_id):
        await sql_delete_ranking(tournament_id, ranking.id)

    await sql_delete_players_of_tournament(tournament_id)
    await sql_delete_courts_of_tournament(tournament_id)
    await sql_delete_teams_of_tournament(tournament_id)
    await sql_delete_tournament(tournament_id)


async def sql_delete_tournament_completely(tournament_id: TournamentId) -> None:
    """
    Borra un torneo y todas sus dependencias en UNA transaccion.

    Si alguna dependencia no se puede borrar (FK protegida o no contemplada por el contrato), la
    transaccion se revierte completa y se lanza `TournamentDeleteConflictError` (HTTP 409 con codigo
    estable). Nunca se propaga un 500 por una violacion de clave ajena esperable.
    """
    await delete_tournament_logo(tournament_id)

    try:
        async with database.transaction():
            await delete_tournament_rows(tournament_id)
    except asyncpg.exceptions.ForeignKeyViolationError as exc:
        logger.warning(
            "Tournament delete blocked by a database constraint",
            extra={
                "tournament_id": tournament_id,
                "constraint_name": exc.as_dict().get("constraint_name"),
            },
        )
        raise TournamentDeleteConflictError() from exc
