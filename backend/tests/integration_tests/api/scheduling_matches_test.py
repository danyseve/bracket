import pytest

from bracket.logic.scheduling.builder import build_matches_for_stage_item
from bracket.models.db.stage_item import StageItem, StageItemWithInputsCreate
from bracket.models.db.stage_item_inputs import (
    StageItemInputCreateBodyFinal,
    StageItemInputCreateBodyTentative,
)
from bracket.models.db.util import StageItemWithRounds, StageWithStageItems
from bracket.sql.shared import sql_delete_stage_item_with_foreign_keys
from bracket.sql.stage_items import sql_create_stage_item_with_inputs
from bracket.sql.stages import get_full_tournament_details
from bracket.utils.dummy_records import (
    DUMMY_COURT1,
    DUMMY_STAGE2,
    DUMMY_STAGE_ITEM1,
    DUMMY_STAGE_ITEM3,
    DUMMY_TEAM1,
)
from bracket.utils.http import HTTPMethod
from tests.integration_tests.api.shared import (
    SUCCESS_RESPONSE,
    send_tournament_request,
)
from tests.integration_tests.models import AuthContext
from tests.integration_tests.sql import (
    inserted_court,
    inserted_stage,
    inserted_team,
)


def stage_items_of(stages: list[StageWithStageItems]) -> list[StageItemWithRounds]:
    return [stage_item for stage in stages for stage_item in stage.stage_items]


def stage_item_by_name(stage_items: list[StageItemWithRounds], name: str) -> StageItemWithRounds:
    """Identifica el stage item por identidad (nombre), nunca por su posicion en el JSON."""
    matches = [stage_item for stage_item in stage_items if stage_item.name == name]
    assert len(matches) == 1, f"se esperaba un unico stage item {name!r}, hay {len(matches)}"
    return matches[0]


@pytest.mark.asyncio(loop_scope="session")
async def test_schedule_all_matches(
    startup_and_shutdown_uvicorn_server: None, auth_context: AuthContext
) -> None:
    async with (
        inserted_court(
            DUMMY_COURT1.model_copy(update={"tournament_id": auth_context.tournament.id})
        ),
        inserted_stage(
            DUMMY_STAGE2.model_copy(update={"tournament_id": auth_context.tournament.id})
        ) as stage_inserted_1,
        inserted_team(
            DUMMY_TEAM1.model_copy(update={"tournament_id": auth_context.tournament.id})
        ) as team_inserted_1,
        inserted_team(
            DUMMY_TEAM1.model_copy(update={"tournament_id": auth_context.tournament.id})
        ) as team_inserted_2,
        inserted_team(
            DUMMY_TEAM1.model_copy(update={"tournament_id": auth_context.tournament.id})
        ) as team_inserted_3,
        inserted_team(
            DUMMY_TEAM1.model_copy(update={"tournament_id": auth_context.tournament.id})
        ) as team_inserted_4,
    ):
        tournament_id = auth_context.tournament.id
        stage_item_1 = await sql_create_stage_item_with_inputs(
            tournament_id,
            StageItemWithInputsCreate(
                stage_id=stage_inserted_1.id,
                name=DUMMY_STAGE_ITEM1.name,
                team_count=DUMMY_STAGE_ITEM1.team_count,
                type=DUMMY_STAGE_ITEM1.type,
                inputs=[
                    StageItemInputCreateBodyFinal(
                        slot=1,
                        team_id=team_inserted_1.id,
                    ),
                    StageItemInputCreateBodyFinal(
                        slot=2,
                        team_id=team_inserted_2.id,
                    ),
                    StageItemInputCreateBodyFinal(
                        slot=3,
                        team_id=team_inserted_3.id,
                    ),
                    StageItemInputCreateBodyFinal(
                        slot=4,
                        team_id=team_inserted_4.id,
                    ),
                ],
            ),
        )
        stage_item_2 = await sql_create_stage_item_with_inputs(
            tournament_id,
            StageItemWithInputsCreate(
                stage_id=stage_inserted_1.id,
                name=DUMMY_STAGE_ITEM3.name,
                team_count=2,
                type=DUMMY_STAGE_ITEM3.type,
                inputs=[
                    StageItemInputCreateBodyTentative(
                        slot=1,
                        winner_from_stage_item_id=stage_item_1.id,
                        winner_position=1,
                    ),
                    StageItemInputCreateBodyTentative(
                        slot=2,
                        winner_from_stage_item_id=stage_item_1.id,
                        winner_position=2,
                    ),
                ],
            ),
        )
        await build_matches_for_stage_item(stage_item_1, tournament_id)
        await build_matches_for_stage_item(stage_item_2, tournament_id)

        response = await send_tournament_request(
            HTTPMethod.POST,
            "schedule_matches",
            auth_context,
        )
        stages = await get_full_tournament_details(tournament_id)

        await sql_delete_stage_item_with_foreign_keys(stage_item_2.id)
        await sql_delete_stage_item_with_foreign_keys(stage_item_1.id)

    assert response == SUCCESS_RESPONSE

    # La identidad del stage item es su nombre: el grupo es el round robin, no la posicion 0.
    stage_item = stage_item_by_name(stage_items_of(stages), DUMMY_STAGE_ITEM1.name)
    assert len(stage_item.rounds) == 3
    for round_ in stage_item.rounds:
        assert len(round_.matches) == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_the_stage_items_keep_the_contractual_order(auth_context: AuthContext) -> None:
    """El orden de ``stage_items`` y ``rounds`` es contractual (``ORDER BY id``), no del plan.

    Los nombres elegidos no coinciden ni con el orden de creacion ni con su inverso: la asercion
    no puede cumplirse por casualidad con un plan de ejecucion concreto.
    """
    tournament_id = auth_context.tournament.id
    async with (
        inserted_court(DUMMY_COURT1.model_copy(update={"tournament_id": tournament_id})),
        inserted_stage(
            DUMMY_STAGE2.model_copy(update={"tournament_id": tournament_id})
        ) as stage_inserted,
        inserted_team(
            DUMMY_TEAM1.model_copy(update={"tournament_id": tournament_id})
        ) as team_inserted_1,
        inserted_team(
            DUMMY_TEAM1.model_copy(update={"tournament_id": tournament_id})
        ) as team_inserted_2,
        inserted_team(
            DUMMY_TEAM1.model_copy(update={"tournament_id": tournament_id})
        ) as team_inserted_3,
        inserted_team(
            DUMMY_TEAM1.model_copy(update={"tournament_id": tournament_id})
        ) as team_inserted_4,
    ):
        created: list[StageItem] = []
        try:
            for name in ("Media Orden", "Alfa Orden", "Zulu Orden"):
                stage_item = await sql_create_stage_item_with_inputs(
                    tournament_id,
                    StageItemWithInputsCreate(
                        stage_id=stage_inserted.id,
                        name=name,
                        team_count=DUMMY_STAGE_ITEM1.team_count,
                        type=DUMMY_STAGE_ITEM1.type,
                        inputs=[
                            StageItemInputCreateBodyFinal(
                                slot=1,
                                team_id=team_inserted_1.id,
                            ),
                            StageItemInputCreateBodyFinal(
                                slot=2,
                                team_id=team_inserted_2.id,
                            ),
                            StageItemInputCreateBodyFinal(
                                slot=3,
                                team_id=team_inserted_3.id,
                            ),
                            StageItemInputCreateBodyFinal(
                                slot=4,
                                team_id=team_inserted_4.id,
                            ),
                        ],
                    ),
                )
                await build_matches_for_stage_item(stage_item, tournament_id)
                created.append(stage_item)

            stages = await get_full_tournament_details(tournament_id)
            created_ids = {int(stage_item.id) for stage_item in created}
            delivered = [
                stage_item
                for stage_item in stage_items_of(stages)
                if int(stage_item.id) in created_ids
            ]

            assert [int(stage_item.id) for stage_item in delivered] == sorted(created_ids)
            for stage_item in delivered:
                round_ids = [int(round_.id) for round_ in stage_item.rounds]
                assert round_ids == sorted(round_ids)
        finally:
            for stage_item in reversed(created):
                await sql_delete_stage_item_with_foreign_keys(stage_item.id)
