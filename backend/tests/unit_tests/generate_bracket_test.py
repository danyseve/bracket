"""P2.8B F2C - Explicit generation of a bye-aware bracket for a stage item.

The standard UI flow creates a stage item with empty slots and assigns the teams to those slots
afterwards. Those teams end up in the first slots, so the empty ones are paired with each other and
the bracket contains matches nobody can ever play. Generating the bracket spreads the byes over the
whole first round instead. These tests cover the pure planning: which bracket a stage item should
have, which slots have to move, and when generating is refused.
"""

from typing import Any

import pytest
from fastapi import HTTPException

from bracket.logic.scheduling.generation import (
    BracketGenerationBlocker,
    get_bracket_generation_blocker,
    plan_bracket_generation,
)
from bracket.logic.scheduling.seeding import distribute_entrants_into_slots
from bracket.models.db.match import MatchWithDetails, MatchWithDetailsDefinitive
from bracket.models.db.stage_item import StageType
from bracket.models.db.stage_item_inputs import (
    StageItemInput,
    StageItemInputEmpty,
    StageItemInputFinal,
    StageItemInputTentative,
)
from bracket.models.db.team import Team
from bracket.models.db.util import RoundWithMatches, StageItemWithRounds
from bracket.utils.dummy_records import DUMMY_MOCK_TIME, DUMMY_TEAM1
from bracket.utils.id_types import (
    CourtId,
    MatchId,
    RoundId,
    StageId,
    StageItemId,
    StageItemInputId,
    TeamId,
    TournamentId,
)


def make_input(input_id: int, slot: int, team_id: int | None) -> StageItemInput:
    if team_id is None:
        return StageItemInputEmpty(
            id=StageItemInputId(input_id), slot=slot, tournament_id=TournamentId(-1)
        )
    return StageItemInputFinal(
        id=StageItemInputId(input_id),
        slot=slot,
        tournament_id=TournamentId(-1),
        team_id=TeamId(team_id),
        team=Team(**DUMMY_TEAM1.model_dump(), id=TeamId(team_id)),
    )


def make_stage_item(
    slot_count: int,
    entrant_count: int,
    *,
    type_: StageType = StageType.SINGLE_ELIMINATION,
    rounds: list[RoundWithMatches] | None = None,
    tentative_slots: int = 0,
) -> StageItemWithRounds:
    """A stage item with the entrants in the first slots: the layout the UI flow produces."""
    inputs: list[StageItemInput] = []
    for slot in range(1, slot_count + 1):
        if slot <= entrant_count:
            inputs.append(make_input(-slot, slot, -slot))
        elif slot <= entrant_count + tentative_slots:
            inputs.append(
                StageItemInputTentative(
                    id=StageItemInputId(-slot),
                    slot=slot,
                    tournament_id=TournamentId(-1),
                    winner_from_stage_item_id=StageItemId(-99),
                    winner_position=slot,
                )
            )
        else:
            inputs.append(make_input(-slot, slot, None))

    return StageItemWithRounds(
        id=StageItemId(-1),
        stage_id=StageId(-1),
        name="",
        created=DUMMY_MOCK_TIME,
        type=type_,
        team_count=slot_count,
        ranking_id=None,
        rounds=rounds if rounds is not None else [],
        inputs=inputs,
        type_name=str(type_.value),
    )


def make_generated_stage_item(slot_count: int, entrant_count: int) -> StageItemWithRounds:
    """A stage item that already has the bye-aware layout that generating produces."""
    distribution = distribute_entrants_into_slots(list(range(1, entrant_count + 1)), slot_count)
    inputs = [
        make_input(-slot, slot, team_id) for slot, team_id in enumerate(distribution, start=1)
    ]
    return StageItemWithRounds(
        id=StageItemId(-1),
        stage_id=StageId(-1),
        name="",
        created=DUMMY_MOCK_TIME,
        type=StageType.SINGLE_ELIMINATION,
        team_count=slot_count,
        ranking_id=None,
        rounds=[],
        inputs=inputs,
        type_name=str(StageType.SINGLE_ELIMINATION.value),
    )


def make_round(match_count: int, **match_updates: Any) -> list[RoundWithMatches]:
    """One first round with the first round matches, empty slots unless overridden."""
    inputs = [make_input(-2 * index, 2 * index - 1, None) for index in range(1, match_count + 1)]
    matches: list[MatchWithDetailsDefinitive | MatchWithDetails] = []
    for index, input_ in enumerate(inputs, start=1):
        matches.append(
            MatchWithDetails.model_validate(
                {
                    "id": MatchId(-index),
                    "created": DUMMY_MOCK_TIME,
                    "duration_minutes": 5,
                    "margin_minutes": 1,
                    "round_id": RoundId(-1),
                    "stage_item_input1_score": 0,
                    "stage_item_input2_score": 0,
                    "stage_item_input1_conflict": False,
                    "stage_item_input2_conflict": False,
                    "stage_item_input1_id": input_.id,
                    "stage_item_input2_id": input_.id,
                    "stage_item_input1": input_,
                    "stage_item_input2": input_,
                }
                | match_updates
            )
        )

    return [
        RoundWithMatches(
            id=RoundId(-1),
            created=DUMMY_MOCK_TIME,
            name="",
            is_draft=True,
            stage_item_id=StageItemId(-1),
            matches=matches,
        )
    ]


# A. the brackets the endpoint has to produce: byes are spread, no ghost match is left
@pytest.mark.parametrize(
    ("slot_count", "entrant_count", "byes", "changed"),
    [
        (2, 2, 0, False),
        (4, 3, 1, False),
        (8, 5, 3, True),
        (8, 6, 2, True),
        (8, 7, 1, True),
    ],
)
def test_plan_spreads_the_byes(
    slot_count: int, entrant_count: int, byes: int, changed: bool
) -> None:
    plan = plan_bracket_generation(make_stage_item(slot_count, entrant_count))

    assert plan.entrant_count == entrant_count
    assert plan.bracket_size == slot_count
    assert plan.bye_count == byes
    assert plan.ghost_count == 0
    assert plan.changed == changed


# B. a bracket that already has the bye-aware layout is not touched again
@pytest.mark.parametrize(
    ("slot_count", "entrant_count"),
    [(2, 2), (4, 3), (8, 5), (8, 6), (8, 7), (8, 8)],
)
def test_plan_is_idempotent(slot_count: int, entrant_count: int) -> None:
    plan = plan_bracket_generation(make_generated_stage_item(slot_count, entrant_count))

    assert plan.entrant_count == entrant_count
    assert plan.bye_count == slot_count - entrant_count
    assert plan.ghost_count == 0
    assert not plan.changed
    assert not plan.changes


# C. only the slots whose occupant changes are reported
def test_plan_only_reports_the_slots_that_change() -> None:
    stage_item = make_stage_item(4, 3)
    plan = plan_bracket_generation(stage_item)

    by_input_id = {input_.id: input_ for input_ in stage_item.inputs}
    for input_id, team_id in plan.changes:
        assert by_input_id[input_id].team_id != team_id
    assert len(plan.changes) == len({input_id for input_id, _ in plan.changes})


# D. a slot count that does not match the bracket size is refused
def test_plan_rejects_an_inconsistent_slot_count() -> None:
    stage_item = make_stage_item(8, 6).model_copy(update={"team_count": 4})

    with pytest.raises(HTTPException) as exc_info:
        plan_bracket_generation(stage_item)
    assert exc_info.value.status_code == 409
    assert str(exc_info.value.detail).endswith(BracketGenerationBlocker.INCONSISTENT_SLOTS.value)


# E. an untouched bracket can be generated
def test_blocker_is_none_for_a_fresh_bracket() -> None:
    assert get_bracket_generation_blocker(make_stage_item(8, 6)) is None


# F. a bracket with a score, a court or a schedule position is not regenerated
@pytest.mark.parametrize(
    ("match_updates", "blocker"),
    [
        (
            {"stage_item_input1_score": 2, "stage_item_input2_score": 0},
            BracketGenerationBlocker.SCORES,
        ),
        ({"court_id": CourtId(-1)}, BracketGenerationBlocker.PLANNING),
        ({"start_time": DUMMY_MOCK_TIME}, BracketGenerationBlocker.PLANNING),
        ({"position_in_schedule": 1}, BracketGenerationBlocker.PLANNING),
    ],
)
def test_blocker_reports_activity(match_updates: dict[str, Any], blocker: str) -> None:
    stage_item = make_stage_item(8, 6, rounds=make_round(4, **match_updates))

    assert get_bracket_generation_blocker(stage_item) == blocker


# G. other stage types, tentative inputs and impossible brackets are refused
def test_blocker_reports_a_non_elimination_stage_item() -> None:
    stage_item = make_stage_item(8, 6, type_=StageType.ROUND_ROBIN)

    assert (
        get_bracket_generation_blocker(stage_item)
        == BracketGenerationBlocker.NOT_SINGLE_ELIMINATION
    )


def test_blocker_reports_tentative_inputs() -> None:
    stage_item = make_stage_item(8, 6, tentative_slots=2)

    assert get_bracket_generation_blocker(stage_item) == BracketGenerationBlocker.TENTATIVE_INPUTS


def test_blocker_reports_too_few_entrants() -> None:
    assert (
        get_bracket_generation_blocker(make_stage_item(8, 1))
        == BracketGenerationBlocker.TOO_FEW_ENTRANTS
    )


def test_blocker_reports_a_bracket_that_is_too_large() -> None:
    assert (
        get_bracket_generation_blocker(make_stage_item(8, 3))
        == BracketGenerationBlocker.BRACKET_TOO_LARGE
    )


# H. structural advancements of an earlier generation do not block a new one
def test_blocker_ignores_materialized_structural_advancements() -> None:
    stage_item = make_generated_stage_item(8, 6)
    rounds = make_round(4)
    rounds[0].matches[3] = (
        rounds[0]
        .matches[3]
        .model_copy(update={"stage_item_input1_winner_from_match_id": MatchId(-1)})
    )

    assert get_bracket_generation_blocker(stage_item.model_copy(update={"rounds": rounds})) is None
