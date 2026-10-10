"""Regresion y contrato del borrado de torneos (hotfix DELETE TOURNAMENT 500).

Defecto original: `DELETE /tournaments/{id}` intentaba borrar los rankings antes que los
stage_items que los referencian, la `ForeignKeyViolationError` escapaba de la ruta y la API
respondia HTTP 500 (y con una constraint fuera del enum `ForeignKey` el guardia lanzaba
`AssertionError`, tambien 500). El torneo quedaba intacto.

Contrato que se fija aqui:

- un torneo sin dependencias se borra (comportamiento previo, sin regresion);
- un torneo con ranking, stage_items que referencian ese ranking, stage, stage_item_inputs,
  round, match (con court), team, player y players_x_teams se borra por completo: 0 filas en
  todas esas tablas;
- `stage_items_ranking_id_fkey` (la constraint del fallo original) ya no produce 500;
- todo el borrado ocurre en UNA transaccion: un fallo deja CERO eliminaciones parciales;
- una dependencia que no se puede borrar produce 409 estable con codigo de aplicacion,
  nunca 500 ni 400, y sin filtrar nombres de constraint, SQL ni detalles de PostgreSQL.
"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Final

import pytest

from bracket.database import database
from bracket.models.db.stage_item_inputs import StageItemInputInsertable
from bracket.models.db.tournament import Tournament, TournamentInsertable
from bracket.utils.dummy_records import (
    DUMMY_COURT1,
    DUMMY_MATCH1,
    DUMMY_PLAYER1,
    DUMMY_RANKING1,
    DUMMY_ROUND1,
    DUMMY_STAGE1,
    DUMMY_STAGE_ITEM1,
    DUMMY_TEAM1,
    DUMMY_TOURNAMENT,
)
from bracket.utils.http import HTTPMethod
from bracket.utils.id_types import StageItemId, TournamentId
from tests.integration_tests.api.shared import (
    SUCCESS_RESPONSE,
    send_tournament_request_with_status,
)
from tests.integration_tests.models import AuthContext
from tests.integration_tests.sql import (
    inserted_court,
    inserted_match,
    inserted_player_in_team,
    inserted_ranking,
    inserted_round,
    inserted_stage,
    inserted_stage_item,
    inserted_stage_item_input,
    inserted_team,
    inserted_tournament,
)

#: Recuento por tabla dependiente de un torneo. Un torneo borrado por completo deja cero filas en
#: todas ellas. `players_x_teams` no tiene `tournament_id`: se cuenta a traves de sus players.
ROW_COUNT_QUERIES: Final[dict[str, str]] = {
    "stages": "SELECT count(*) FROM stages WHERE tournament_id = :tournament_id",
    "stage_items": (
        "SELECT count(*) FROM stage_items si JOIN stages s ON s.id = si.stage_id "
        "WHERE s.tournament_id = :tournament_id"
    ),
    "stage_item_inputs": (
        "SELECT count(*) FROM stage_item_inputs WHERE tournament_id = :tournament_id"
    ),
    "rounds": (
        "SELECT count(*) FROM rounds r JOIN stage_items si ON si.id = r.stage_item_id "
        "JOIN stages s ON s.id = si.stage_id WHERE s.tournament_id = :tournament_id"
    ),
    "matches": (
        "SELECT count(*) FROM matches m JOIN rounds r ON r.id = m.round_id "
        "JOIN stage_items si ON si.id = r.stage_item_id JOIN stages s ON s.id = si.stage_id "
        "WHERE s.tournament_id = :tournament_id"
    ),
    "players": "SELECT count(*) FROM players WHERE tournament_id = :tournament_id",
    "players_x_teams": (
        "SELECT count(*) FROM players_x_teams pxt JOIN players p ON p.id = pxt.player_id "
        "WHERE p.tournament_id = :tournament_id"
    ),
    "courts": "SELECT count(*) FROM courts WHERE tournament_id = :tournament_id",
    "teams": "SELECT count(*) FROM teams WHERE tournament_id = :tournament_id",
    "rankings": "SELECT count(*) FROM rankings WHERE tournament_id = :tournament_id",
    "tournaments": "SELECT count(*) FROM tournaments WHERE id = :tournament_id",
}


async def row_counts_of_tournament(tournament_id: TournamentId) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table_name, query in ROW_COUNT_QUERIES.items():
        counts[table_name] = int(
            await database.fetch_val(query=query, values={"tournament_id": tournament_id})
        )
    return counts


def make_tournament_body(auth_context: AuthContext) -> TournamentInsertable:
    """
    Un torneo propio del club autenticado: el torneo de `auth_context` es de ambito de sesion y
    otros tests lo reutilizan, asi que cada test crea y borra el suyo.
    """
    return DUMMY_TOURNAMENT.model_copy(
        update={"club_id": auth_context.club.id, "dashboard_endpoint": None}
    )


def auth_context_for(auth_context: AuthContext, tournament: Tournament) -> AuthContext:
    return auth_context.model_copy(update={"tournament": tournament})


@asynccontextmanager
async def tournament_with_dependencies(auth_context: AuthContext) -> AsyncIterator[Tournament]:
    """
    Torneo con una dependencia de cada tipo: ranking, stage_items que lo referencian, stage,
    stage_item_inputs, round, match (con court), team, player y players_x_teams.
    """
    async with AsyncExitStack() as stack:
        tournament = await stack.enter_async_context(
            inserted_tournament(make_tournament_body(auth_context))
        )
        ranking = await stack.enter_async_context(
            inserted_ranking(DUMMY_RANKING1.model_copy(update={"tournament_id": tournament.id}))
        )
        stage = await stack.enter_async_context(
            inserted_stage(DUMMY_STAGE1.model_copy(update={"tournament_id": tournament.id}))
        )
        stage_item = await stack.enter_async_context(
            inserted_stage_item(
                DUMMY_STAGE_ITEM1.model_copy(
                    update={"stage_id": stage.id, "ranking_id": ranking.id}
                )
            )
        )
        inputs = [
            await stack.enter_async_context(
                inserted_stage_item_input(
                    StageItemInputInsertable(
                        slot=slot, tournament_id=tournament.id, stage_item_id=stage_item.id
                    )
                )
            )
            for slot in (0, 1)
        ]
        round_ = await stack.enter_async_context(
            inserted_round(DUMMY_ROUND1.model_copy(update={"stage_item_id": stage_item.id}))
        )
        court = await stack.enter_async_context(
            inserted_court(DUMMY_COURT1.model_copy(update={"tournament_id": tournament.id}))
        )
        await stack.enter_async_context(
            inserted_match(
                DUMMY_MATCH1.model_copy(
                    update={
                        "round_id": round_.id,
                        "stage_item_input1_id": inputs[0].id,
                        "stage_item_input2_id": inputs[1].id,
                        "court_id": court.id,
                    }
                )
            )
        )
        team = await stack.enter_async_context(
            inserted_team(DUMMY_TEAM1.model_copy(update={"tournament_id": tournament.id}))
        )
        await stack.enter_async_context(
            inserted_player_in_team(
                DUMMY_PLAYER1.model_copy(update={"tournament_id": tournament.id}), team.id
            )
        )
        yield tournament


async def break_stage_item_deletion(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Desactiva el borrado de las relaciones y de los stage_items, con lo que la propia base de
    datos rechaza el borrado del torneo con una `ForeignKeyViolationError` real.
    """

    async def _do_nothing(_: StageItemId) -> None:
        return None

    monkeypatch.setattr("bracket.logic.tournaments.sql_delete_stage_item_relations", _do_nothing)
    monkeypatch.setattr("bracket.logic.tournaments.sql_delete_stage_item", _do_nothing)


@pytest.mark.asyncio(loop_scope="session")
async def test_deleting_a_tournament_without_dependencies_succeeds(
    startup_and_shutdown_uvicorn_server: None, auth_context: AuthContext
) -> None:
    """Sin dependencias el borrado sigue funcionando: 200 y cero filas."""
    async with inserted_tournament(make_tournament_body(auth_context)) as tournament:
        status, body = await send_tournament_request_with_status(
            HTTPMethod.DELETE, "", auth_context_for(auth_context, tournament)
        )

        assert status == 200, body
        assert body == SUCCESS_RESPONSE
        assert (await row_counts_of_tournament(tournament.id))["tournaments"] == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_deleting_a_tournament_with_dependencies_removes_all_of_them(
    startup_and_shutdown_uvicorn_server: None, auth_context: AuthContext
) -> None:
    """Con dependencias completas: 200, cero filas en todas las tablas del torneo."""
    async with tournament_with_dependencies(auth_context) as tournament:
        before = await row_counts_of_tournament(tournament.id)
        assert all(count > 0 for count in before.values()), before

        status, body = await send_tournament_request_with_status(
            HTTPMethod.DELETE, "", auth_context_for(auth_context, tournament)
        )

        assert status == 200, body
        after = await row_counts_of_tournament(tournament.id)
        assert after == dict.fromkeys(after, 0), after


@pytest.mark.asyncio(loop_scope="session")
async def test_stage_items_ranking_fkey_no_longer_produces_a_500(
    startup_and_shutdown_uvicorn_server: None, auth_context: AuthContext
) -> None:
    """
    Regresion explicita del fallo original: ranking + stage_items que lo referencian + stage.
    Antes: `stage_items_ranking_id_fkey` -> HTTP 500. Ahora: 200 y cero filas.
    """
    async with AsyncExitStack() as stack:
        tournament = await stack.enter_async_context(
            inserted_tournament(make_tournament_body(auth_context))
        )
        ranking = await stack.enter_async_context(
            inserted_ranking(DUMMY_RANKING1.model_copy(update={"tournament_id": tournament.id}))
        )
        stage = await stack.enter_async_context(
            inserted_stage(DUMMY_STAGE1.model_copy(update={"tournament_id": tournament.id}))
        )
        await stack.enter_async_context(
            inserted_stage_item(
                DUMMY_STAGE_ITEM1.model_copy(
                    update={"stage_id": stage.id, "ranking_id": ranking.id}
                )
            )
        )

        status, body = await send_tournament_request_with_status(
            HTTPMethod.DELETE, "", auth_context_for(auth_context, tournament)
        )

        assert status == 200, body
        after = await row_counts_of_tournament(tournament.id)
        assert after["rankings"] == 0 and after["stage_items"] == 0, after


@pytest.mark.asyncio(loop_scope="session")
async def test_a_failure_during_the_delete_rolls_everything_back(
    startup_and_shutdown_uvicorn_server: None,
    auth_context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Atomicidad: con un fallo a mitad del borrado no puede persistir ninguna eliminacion parcial,
    y la API debe responder 409 estable (no 500, no 400).
    """
    await break_stage_item_deletion(monkeypatch)

    async with tournament_with_dependencies(auth_context) as tournament:
        before = await row_counts_of_tournament(tournament.id)

        status, body = await send_tournament_request_with_status(
            HTTPMethod.DELETE, "", auth_context_for(auth_context, tournament)
        )

        assert status == 409, body
        assert body["code"] == "TOURNAMENT_DELETE_CONFLICT"
        assert isinstance(body["detail"], str) and body["detail"], body

        after = await row_counts_of_tournament(tournament.id)
        assert after == before, after


@pytest.mark.asyncio(loop_scope="session")
async def test_the_conflict_response_is_safe(
    startup_and_shutdown_uvicorn_server: None,
    auth_context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La respuesta de conflicto no filtra constraint, SQL, stack trace ni detalles internos."""
    await break_stage_item_deletion(monkeypatch)

    async with tournament_with_dependencies(auth_context) as tournament:
        status, body = await send_tournament_request_with_status(
            HTTPMethod.DELETE, "", auth_context_for(auth_context, tournament)
        )

        assert status == 409, body
        serialized = str(body).lower()
        for forbidden in ("fkey", "constraint", "select", "traceback", "asyncpg", "psycopg"):
            assert forbidden not in serialized, body


@pytest.mark.asyncio(loop_scope="session")
async def test_deleting_a_tournament_does_not_touch_other_tournaments(
    startup_and_shutdown_uvicorn_server: None, auth_context: AuthContext
) -> None:
    """El borrado no puede alcanzar datos de otros torneos del mismo club."""
    async with tournament_with_dependencies(auth_context) as foreign_tournament:
        async with tournament_with_dependencies(auth_context) as tournament:
            foreign_before = await row_counts_of_tournament(foreign_tournament.id)

            status, body = await send_tournament_request_with_status(
                HTTPMethod.DELETE, "", auth_context_for(auth_context, tournament)
            )

            assert status == 200, body
            assert await row_counts_of_tournament(foreign_tournament.id) == foreign_before
