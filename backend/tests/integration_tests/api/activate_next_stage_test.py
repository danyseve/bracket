import pytest

from bracket.logic.scheduling.builder import build_matches_for_stage_item
from bracket.models.db.match import MatchBody, MatchWithDetailsDefinitive
from bracket.models.db.stage_item import StageItemWithInputsCreate
from bracket.models.db.stage_item_inputs import (
    StageItemInputCreateBodyFinal,
    StageItemInputCreateBodyTentative,
)
from bracket.models.db.util import StageWithStageItems
from bracket.sql.matches import sql_update_match
from bracket.sql.shared import sql_delete_stage_item_with_foreign_keys
from bracket.sql.stage_items import sql_create_stage_item_with_inputs
from bracket.sql.stages import get_full_tournament_details
from bracket.utils.dummy_records import (
    DUMMY_COURT1,
    DUMMY_STAGE1,
    DUMMY_STAGE2,
    DUMMY_STAGE_ITEM1,
    DUMMY_STAGE_ITEM3,
    DUMMY_TEAM1,
)
from bracket.utils.http import HTTPMethod
from tests.integration_tests.api.shared import SUCCESS_RESPONSE, send_tournament_request
from tests.integration_tests.models import AuthContext
from tests.integration_tests.sql import (
    inserted_court,
    inserted_stage,
    inserted_team,
)


@pytest.mark.asyncio(loop_scope="session")
async def test_activate_next_stage(
    startup_and_shutdown_uvicorn_server: None, auth_context: AuthContext
) -> None:
    async with (
        inserted_court(
            DUMMY_COURT1.model_copy(update={"tournament_id": auth_context.tournament.id})
        ),
        inserted_stage(
            DUMMY_STAGE1.model_copy(update={"tournament_id": auth_context.tournament.id})
        ) as stage_inserted_1,
        inserted_stage(
            DUMMY_STAGE2.model_copy(update={"tournament_id": auth_context.tournament.id})
        ) as stage_inserted_2,
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
                stage_id=stage_inserted_2.id,
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

        # Set match score to get a winner (team 2) that goes to the next round
        # Identidad, no posicion: el contrato del JSON fija el contenido, no el orden de las
        # rondas ni de los partidos, asi que se localiza la fase y el partido por su identidad.
        stages_before = await get_full_tournament_details(auth_context.tournament.id)
        prev_stage = next(
            (stage for stage in stages_before if stage.id == stage_inserted_1.id), None
        )
        assert prev_stage is not None, "no se encontro la fase previa del torneo"
        matches_before = [
            match
            for stage_item in prev_stage.stage_items
            for round_ in stage_item.rounds
            for match in round_.matches
            if isinstance(match, MatchWithDetailsDefinitive)
        ]
        winner_matches = [
            match
            for match in matches_before
            if match.stage_item_input2.team_id == team_inserted_2.id
        ]
        assert winner_matches, "no hay ningun partido con el equipo 2 en el segundo slot"
        # Una sola victoria basta para que el equipo 2 lidere la clasificacion; se elige de forma
        # determinista el partido de menor id.
        match1 = min(winner_matches, key=lambda match: match.id)
        await sql_update_match(
            match1.id,
            MatchBody(**match1.model_copy(update={"stage_item_input2_score": 42}).model_dump()),
            auth_context.tournament,
        )

        response = await send_tournament_request(
            HTTPMethod.POST, "stages/activate?direction=next", auth_context, json={}
        )
        stages_after = await get_full_tournament_details(auth_context.tournament.id)
        next_stage = next(
            (stage for stage in stages_after if stage.id == stage_inserted_2.id), None
        )

        await sql_delete_stage_item_with_foreign_keys(stage_item_2.id)
        await sql_delete_stage_item_with_foreign_keys(stage_item_1.id)

    assert response == SUCCESS_RESPONSE

    assert isinstance(next_stage, StageWithStageItems)
    # assert isinstance(next_stage.stage_items[0].rounds[0].matches[0], MatchWithDetailsDefinitive)
    # assert (
    #     next_stage.stage_items[0].rounds[0].matches[0].stage_item_input1.team_id
    #     == team_inserted_2.id
    # )
