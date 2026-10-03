"""P2.8B F2C - Explicit bracket generation through the API, as the standard UI flow uses it.

A stage item is created with empty slots (no inputs in the body), the teams are then assigned to
those slots one by one, and only then is the bracket generated explicitly. These tests cover that
whole flow end to end: the bye-aware distribution, the structural advancement of the byes, the
idempotency of a second call, and every situation in which generating is refused.
"""

from contextlib import AsyncExitStack

import pytest

from bracket.database import database
from bracket.models.db.stage_item import StageType
from bracket.models.db.stage_item_inputs import StageItemInput
from bracket.models.db.tournament import TournamentStatus
from bracket.models.db.util import StageItemWithRounds
from bracket.schema import (
    matches,
    rounds,
    stage_item_inputs,
    stage_items,
    stages,
    tournaments,
)
from bracket.sql.stage_items import get_stage_item
from bracket.utils.dummy_records import DUMMY_COURT1, DUMMY_MOCK_TIME, DUMMY_STAGE2, DUMMY_TEAM1
from bracket.utils.http import HTTPMethod
from bracket.utils.id_types import StageId, StageItemId, TeamId
from bracket.utils.types import JsonDict
from tests.integration_tests.api.shared import SUCCESS_RESPONSE, send_tournament_request
from tests.integration_tests.models import AuthContext
from tests.integration_tests.sql import (
    assert_row_count_and_clear,
    inserted_court,
    inserted_stage,
    inserted_team,
)

BLOCKED_PREFIX = "generate_bracket_blocked"


async def send(
    method: HTTPMethod, endpoint: str, auth_context: AuthContext, json: JsonDict | None = None
) -> JsonDict:
    return await send_tournament_request(method, endpoint, auth_context, json=json)


async def create_stage_item(
    auth_context: AuthContext,
    stage_id: StageId,
    team_count: int,
    stage_type: StageType = StageType.SINGLE_ELIMINATION,
) -> StageItemId:
    """Create a stage item with empty slots, exactly like the standard UI flow does."""
    assert (
        await send(
            HTTPMethod.POST,
            "stage_items",
            auth_context,
            json={
                "type": stage_type.value,
                "team_count": team_count,
                "stage_id": stage_id,
            },
        )
        == SUCCESS_RESPONSE
    )
    rows = await database.fetch_all(query=stage_items.select().order_by(stage_items.c.id))
    return StageItemId(int(rows[-1]["id"]))


async def assign_entrants(
    auth_context: AuthContext, stage_item_id: StageItemId, team_ids: list[TeamId]
) -> None:
    """Assign the teams to the empty slots in order, one by one, as the UI does."""
    rows = await database.fetch_all(
        query=stage_item_inputs.select()
        .where(stage_item_inputs.c.stage_item_id == stage_item_id)
        .order_by(stage_item_inputs.c.slot)
    )
    assert len(rows) >= len(team_ids)
    for row, team_id in zip(rows, team_ids, strict=False):
        assert (
            await send(
                HTTPMethod.PUT,
                f"stage_items/{stage_item_id}/inputs/{row['id']}",
                auth_context,
                json={
                    "team_id": team_id,
                    "winner_from_stage_item_id": None,
                    "winner_position": None,
                },
            )
            == SUCCESS_RESPONSE
        )


def is_occupied(stage_item_input: StageItemInput | None) -> bool:
    return stage_item_input is not None and stage_item_input.team_id is not None


def slot_occupancy(stage_item: StageItemWithRounds) -> list[tuple[bool, bool]]:
    """Per first round match: whether its first and second slot hold a team."""
    return [
        (is_occupied(match.stage_item_input1), is_occupied(match.stage_item_input2))
        for match in stage_item.rounds[0].matches
    ]


def ghost_matches(occupancy: list[tuple[bool, bool]]) -> int:
    """First round matches with no entrant at all: nobody can ever play them."""
    return sum(1 for first, second in occupancy if not first and not second)


def bye_matches(occupancy: list[tuple[bool, bool]]) -> int:
    """First round matches with exactly one entrant: a direct bye."""
    return sum(1 for first, second in occupancy if first != second)


def bye_input_ids(stage_item: StageItemWithRounds) -> set[int]:
    """The slots that win their first round match without playing: the direct byes."""
    found: set[int] = set()
    for match in stage_item.rounds[0].matches:
        first, second = match.stage_item_input1, match.stage_item_input2
        if first is not None and first.team_id is not None and not is_occupied(second):
            found.add(int(first.id))
        elif second is not None and second.team_id is not None and not is_occupied(first):
            found.add(int(second.id))
    return found


async def generated_stage_item(
    auth_context: AuthContext, stage_item_id: StageItemId
) -> StageItemWithRounds:
    return await get_stage_item(auth_context.tournament.id, stage_item_id)


async def clear_tournament_tables() -> None:
    await assert_row_count_and_clear(matches, 0)
    await assert_row_count_and_clear(rounds, 0)
    await assert_row_count_and_clear(stage_item_inputs, 0)
    await assert_row_count_and_clear(stage_items, 0)
    await assert_row_count_and_clear(stages, 0)


async def make_inserted_stage(auth_context: AuthContext, stack: AsyncExitStack) -> StageId:
    stage = await stack.enter_async_context(
        inserted_stage(
            DUMMY_STAGE2.model_copy(update={"tournament_id": auth_context.tournament.id})
        )
    )
    return StageId(stage.id)


async def make_inserted_teams(
    auth_context: AuthContext, stack: AsyncExitStack, count: int
) -> list[TeamId]:
    team_ids: list[TeamId] = []
    for _ in range(count):
        team = await stack.enter_async_context(
            inserted_team(
                DUMMY_TEAM1.model_copy(update={"tournament_id": auth_context.tournament.id})
            )
        )
        team_ids.append(TeamId(team.id))
    return team_ids


# A. the gap of the UI flow, and what generating the bracket does about it
@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_six_entrants(
    startup_and_shutdown_uvicorn_server: None, auth_context: AuthContext
) -> None:
    async with AsyncExitStack() as stack:
        stage_id = await make_inserted_stage(auth_context, stack)
        team_ids = await make_inserted_teams(auth_context, stack, 6)
        stage_item_id = await create_stage_item(auth_context, stage_id, 8)
        await assign_entrants(auth_context, stage_item_id, team_ids)

        # The UI flow leaves the empty slots at the end: one match nobody can ever play.
        before = slot_occupancy(await generated_stage_item(auth_context, stage_item_id))
        assert before == [(True, True), (True, True), (True, True), (False, False)]
        assert ghost_matches(before) == 1

        response = await send(
            HTTPMethod.POST, f"stage_items/{stage_item_id}/generate_bracket", auth_context
        )
        assert response == {
            "stage_item_id": stage_item_id,
            "entrant_count": 6,
            "bracket_size": 8,
            "bye_count": 2,
            "ghost_count": 0,
            "changed": True,
        }

        after_stage_item = await generated_stage_item(auth_context, stage_item_id)
        after = slot_occupancy(after_stage_item)
        assert ghost_matches(after) == 0
        assert bye_matches(after) == 2
        assert sum(1 for first, second in after if first and second) == 2

        # The two byes advance structurally: their entrant is already in a later round.
        advancing_input_ids = {
            input_id
            for round_ in after_stage_item.rounds[1:]
            for match in round_.matches
            for input_id in (match.stage_item_input1_id, match.stage_item_input2_id)
            if input_id is not None
        }
        byes_advancing = bye_input_ids(after_stage_item)
        assert byes_advancing
        assert byes_advancing <= advancing_input_ids

        await clear_tournament_tables()


# B. generating twice does not change anything the second time
@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_is_idempotent(
    startup_and_shutdown_uvicorn_server: None, auth_context: AuthContext
) -> None:
    async with AsyncExitStack() as stack:
        stage_id = await make_inserted_stage(auth_context, stack)
        team_ids = await make_inserted_teams(auth_context, stack, 6)
        stage_item_id = await create_stage_item(auth_context, stage_id, 8)
        await assign_entrants(auth_context, stage_item_id, team_ids)

        first = await send(
            HTTPMethod.POST, f"stage_items/{stage_item_id}/generate_bracket", auth_context
        )
        assert first["changed"] is True
        layout_after_first = [
            (input_.slot, input_.team_id)
            for input_ in (await generated_stage_item(auth_context, stage_item_id)).inputs
        ]

        second = await send(
            HTTPMethod.POST, f"stage_items/{stage_item_id}/generate_bracket", auth_context
        )
        assert second == first | {"changed": False}
        layout_after_second = [
            (input_.slot, input_.team_id)
            for input_ in (await generated_stage_item(auth_context, stage_item_id)).inputs
        ]
        assert layout_after_first == layout_after_second

        await clear_tournament_tables()


# C. the documented bracket sizes, from the API
@pytest.mark.parametrize(
    ("team_count", "entrant_count", "byes"),
    [(2, 2, 0), (4, 3, 1), (8, 5, 3), (8, 7, 1), (8, 8, 0)],
)
@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_sizes(
    startup_and_shutdown_uvicorn_server: None,
    auth_context: AuthContext,
    team_count: int,
    entrant_count: int,
    byes: int,
) -> None:
    async with AsyncExitStack() as stack:
        stage_id = await make_inserted_stage(auth_context, stack)
        team_ids = await make_inserted_teams(auth_context, stack, entrant_count)
        stage_item_id = await create_stage_item(auth_context, stage_id, team_count)
        await assign_entrants(auth_context, stage_item_id, team_ids)

        response = await send(
            HTTPMethod.POST, f"stage_items/{stage_item_id}/generate_bracket", auth_context
        )
        assert response["entrant_count"] == entrant_count
        assert response["bracket_size"] == team_count
        assert response["bye_count"] == byes
        assert response["ghost_count"] == 0

        occupancy = slot_occupancy(await generated_stage_item(auth_context, stage_item_id))
        assert ghost_matches(occupancy) == 0
        assert bye_matches(occupancy) == byes

        await clear_tournament_tables()


# D. a bracket with results, courts or planning is never regenerated
@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_blocked_by_score(
    startup_and_shutdown_uvicorn_server: None, auth_context: AuthContext
) -> None:
    async with AsyncExitStack() as stack:
        stage_id = await make_inserted_stage(auth_context, stack)
        team_ids = await make_inserted_teams(auth_context, stack, 6)
        stage_item_id = await create_stage_item(auth_context, stage_id, 8)
        await assign_entrants(auth_context, stage_item_id, team_ids)

        stage_item = await generated_stage_item(auth_context, stage_item_id)
        match_id = stage_item.rounds[0].matches[0].id
        await database.execute(
            matches.update().where(matches.c.id == match_id).values(stage_item_input1_score=1)
        )

        response = await send(
            HTTPMethod.POST, f"stage_items/{stage_item_id}/generate_bracket", auth_context
        )
        assert response == {"detail": f"{BLOCKED_PREFIX}: scores"}

        await clear_tournament_tables()


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_blocked_by_planning(
    startup_and_shutdown_uvicorn_server: None, auth_context: AuthContext
) -> None:
    async with AsyncExitStack() as stack:
        stage_id = await make_inserted_stage(auth_context, stack)
        team_ids = await make_inserted_teams(auth_context, stack, 6)
        court = await stack.enter_async_context(
            inserted_court(
                DUMMY_COURT1.model_copy(update={"tournament_id": auth_context.tournament.id})
            )
        )
        stage_item_id = await create_stage_item(auth_context, stage_id, 8)
        await assign_entrants(auth_context, stage_item_id, team_ids)

        stage_item = await generated_stage_item(auth_context, stage_item_id)
        match_id = stage_item.rounds[0].matches[0].id
        await database.execute(
            matches.update()
            .where(matches.c.id == match_id)
            .values(court_id=court.id, start_time=DUMMY_MOCK_TIME, position_in_schedule=1)
        )

        response = await send(
            HTTPMethod.POST, f"stage_items/{stage_item_id}/generate_bracket", auth_context
        )
        assert response == {"detail": f"{BLOCKED_PREFIX}: planning"}

        await clear_tournament_tables()


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_blocked_for_archived_tournament(
    startup_and_shutdown_uvicorn_server: None, auth_context: AuthContext
) -> None:
    async with AsyncExitStack() as stack:
        stage_id = await make_inserted_stage(auth_context, stack)
        team_ids = await make_inserted_teams(auth_context, stack, 6)
        stage_item_id = await create_stage_item(auth_context, stage_id, 8)
        await assign_entrants(auth_context, stage_item_id, team_ids)

        await database.execute(
            tournaments.update()
            .where(tournaments.c.id == auth_context.tournament.id)
            .values(status=TournamentStatus.ARCHIVED.value)
        )
        try:
            response = await send(
                HTTPMethod.POST, f"stage_items/{stage_item_id}/generate_bracket", auth_context
            )
            assert response == {"detail": "Can't update archived tournament"}
        finally:
            # Other tests in this module keep using the same tournament.
            await database.execute(
                tournaments.update()
                .where(tournaments.c.id == auth_context.tournament.id)
                .values(status=TournamentStatus.OPEN.value)
            )

        await clear_tournament_tables()


# E. brackets that cannot be generated as requested
@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_blocked_with_too_few_entrants(
    startup_and_shutdown_uvicorn_server: None, auth_context: AuthContext
) -> None:
    async with AsyncExitStack() as stack:
        stage_id = await make_inserted_stage(auth_context, stack)
        team_ids = await make_inserted_teams(auth_context, stack, 1)
        stage_item_id = await create_stage_item(auth_context, stage_id, 8)
        await assign_entrants(auth_context, stage_item_id, team_ids)

        response = await send(
            HTTPMethod.POST, f"stage_items/{stage_item_id}/generate_bracket", auth_context
        )
        assert response == {"detail": f"{BLOCKED_PREFIX}: too_few_entrants"}

        await clear_tournament_tables()


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_blocked_when_the_bracket_is_too_large(
    startup_and_shutdown_uvicorn_server: None, auth_context: AuthContext
) -> None:
    async with AsyncExitStack() as stack:
        stage_id = await make_inserted_stage(auth_context, stack)
        team_ids = await make_inserted_teams(auth_context, stack, 3)
        stage_item_id = await create_stage_item(auth_context, stage_id, 8)
        await assign_entrants(auth_context, stage_item_id, team_ids)

        response = await send(
            HTTPMethod.POST, f"stage_items/{stage_item_id}/generate_bracket", auth_context
        )
        assert response == {"detail": f"{BLOCKED_PREFIX}: bracket_too_large"}

        await clear_tournament_tables()


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_blocked_for_a_round_robin_stage_item(
    startup_and_shutdown_uvicorn_server: None, auth_context: AuthContext
) -> None:
    async with AsyncExitStack() as stack:
        stage_id = await make_inserted_stage(auth_context, stack)
        team_ids = await make_inserted_teams(auth_context, stack, 4)
        stage_item_id = await create_stage_item(
            auth_context, stage_id, 4, stage_type=StageType.ROUND_ROBIN
        )
        await assign_entrants(auth_context, stage_item_id, team_ids)

        response = await send(
            HTTPMethod.POST, f"stage_items/{stage_item_id}/generate_bracket", auth_context
        )
        assert response == {"detail": f"{BLOCKED_PREFIX}: not_single_elimination"}

        await clear_tournament_tables()


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_blocked_with_tentative_inputs(
    startup_and_shutdown_uvicorn_server: None, auth_context: AuthContext
) -> None:
    async with AsyncExitStack() as stack:
        stage_id = await make_inserted_stage(auth_context, stack)
        team_ids = await make_inserted_teams(auth_context, stack, 4)
        round_robin_stage_item_id = await create_stage_item(
            auth_context, stage_id, 4, stage_type=StageType.ROUND_ROBIN
        )
        await assign_entrants(auth_context, round_robin_stage_item_id, team_ids)
        stage_item_id = await create_stage_item(auth_context, stage_id, 2)

        rows = await database.fetch_all(
            query=stage_item_inputs.select()
            .where(stage_item_inputs.c.stage_item_id == stage_item_id)
            .order_by(stage_item_inputs.c.slot)
        )
        assert (
            await send(
                HTTPMethod.PUT,
                f"stage_items/{stage_item_id}/inputs/{rows[0]['id']}",
                auth_context,
                json={
                    "team_id": None,
                    "winner_from_stage_item_id": round_robin_stage_item_id,
                    "winner_position": 1,
                },
            )
            == SUCCESS_RESPONSE
        )

        response = await send(
            HTTPMethod.POST, f"stage_items/{stage_item_id}/generate_bracket", auth_context
        )
        assert response == {"detail": f"{BLOCKED_PREFIX}: tentative_inputs"}

        await clear_tournament_tables()


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_of_an_unknown_stage_item(
    startup_and_shutdown_uvicorn_server: None, auth_context: AuthContext
) -> None:
    response = await send(HTTPMethod.POST, "stage_items/-1/generate_bracket", auth_context)

    assert response == {"detail": "Stage item doesn't exist"}
