"""P2.8B F2B - Structural classification of matches and the planning it drives.

A single elimination tree that is distributed bye-aware has no empty/empty first-round pair, but it
still holds structural matches: a bye is one entrant over a dead slot, and a walkover over a dead
branch is the same thing one round later. Nobody can play those, so they must never take a court,
a start time or a position in the schedule, and they must never be counted as a fight.

The topologies below are built with the real engine functions (``determine_matches_first_round``
and ``determine_matches_subsequent_round``), so the expected numbers are derived from the tree that
the engine actually builds, not hardcoded.
"""

from typing import cast

import pytest

from bracket.logic.planning.matches import get_matches_to_schedule
from bracket.logic.scheduling.elimination import (
    determine_matches_first_round,
    determine_matches_subsequent_round,
    get_number_of_rounds_to_create_single_elimination,
)
from bracket.logic.scheduling.seeding import (
    bracket_size_for_entrant_count,
    distribute_entrants_into_slots,
    order_bye_aware,
)
from bracket.logic.scheduling.structural import (
    MatchStructure,
    get_match_structures,
    get_playable_match_ids,
)
from bracket.models.db.match import Match, MatchCreateBody, MatchWithDetails
from bracket.models.db.stage_item import StageType
from bracket.models.db.stage_item_inputs import (
    StageItemInput,
    StageItemInputEmpty,
    StageItemInputFinal,
)
from bracket.models.db.team import Team
from bracket.models.db.tournament import Tournament
from bracket.models.db.util import RoundWithMatches, StageItemWithRounds
from bracket.utils.dummy_records import DUMMY_MOCK_TIME, DUMMY_TEAM1, DUMMY_TOURNAMENT
from bracket.utils.id_types import (
    MatchId,
    RoundId,
    StageId,
    StageItemId,
    StageItemInputId,
    TeamId,
    TournamentId,
)

TOURNAMENT_ID = TournamentId(-1)
STAGE_ITEM_ID = StageItemId(-1)
TOURNAMENT = Tournament(**DUMMY_TOURNAMENT.model_dump(), id=TOURNAMENT_ID)


def team(team_id: int) -> Team:
    return Team(**DUMMY_TEAM1.model_dump(), id=TeamId(team_id))


def entrant(team_number: int, slot: int) -> StageItemInputFinal:
    return StageItemInputFinal(
        id=StageItemInputId(-team_number),
        slot=slot,
        tournament_id=TOURNAMENT_ID,
        stage_item_id=STAGE_ITEM_ID,
        team_id=TeamId(team_number),
        team=team(team_number),
    )


def empty(input_number: int, slot: int) -> StageItemInputEmpty:
    return StageItemInputEmpty(
        id=StageItemInputId(-1000 - input_number),
        slot=slot,
        tournament_id=TOURNAMENT_ID,
        stage_item_id=STAGE_ITEM_ID,
    )


def build_inputs(entrant_count: int) -> tuple[list[StageItemInput], int]:
    """Inputs of a stage item, placed with the bye-aware distribution (slot 1 is index 0)."""
    bracket_size = bracket_size_for_entrant_count(entrant_count)
    assert bracket_size is not None
    distributed = distribute_entrants_into_slots(list(range(1, entrant_count + 1)), bracket_size)

    inputs: list[StageItemInput] = []
    empty_number = 0
    for slot_index, placed in enumerate(distributed):
        if placed is None:
            inputs.append(empty(empty_number, slot_index + 1))
            empty_number += 1
        else:
            inputs.append(entrant(placed, slot_index + 1))
    return inputs, bracket_size


def build_topology(entrant_count: int) -> StageItemWithRounds:
    """A stage item whose rounds and matches come from the real first/subsequent round builders."""
    inputs, bracket_size = build_inputs(entrant_count)
    rounds_count = get_number_of_rounds_to_create_single_elimination(bracket_size)
    inputs_by_id = {input_.id: input_ for input_ in inputs}

    stage_item_ = StageItemWithRounds(
        rounds=[],
        inputs=inputs,
        type_name="Single Elimination",
        team_count=bracket_size,
        ranking_id=None,
        id=STAGE_ITEM_ID,
        stage_id=StageId(-1),
        name="",
        created=DUMMY_MOCK_TIME,
        type=StageType.SINGLE_ELIMINATION,
    )

    rounds: list[RoundWithMatches] = []
    next_match_id = 0
    previous_matches: list[Match] = []

    for round_index in range(rounds_count):
        round_id = RoundId(-(rounds_count - round_index))
        round_ = RoundWithMatches(
            id=round_id,
            matches=[],
            stage_item_id=STAGE_ITEM_ID,
            created=DUMMY_MOCK_TIME,
            is_draft=False,
            name="",
        )
        bodies: list[MatchCreateBody]
        if round_index == 0:
            bodies = determine_matches_first_round(round_, stage_item_, TOURNAMENT)
        else:
            bodies = determine_matches_subsequent_round(previous_matches, round_, TOURNAMENT)

        matches = [
            to_match(next_match_id + index, body, inputs_by_id) for index, body in enumerate(bodies)
        ]
        next_match_id += len(matches)
        previous_matches = [cast("Match", match) for match in matches]
        rounds.append(round_.model_copy(update={"matches": matches}))

    return stage_item_.model_copy(update={"rounds": rounds})


def to_match(
    match_number: int, body: MatchCreateBody, inputs_by_id: dict[StageItemInputId, StageItemInput]
) -> MatchWithDetails:
    input1 = inputs_by_id.get(body.stage_item_input1_id) if body.stage_item_input1_id else None
    input2 = inputs_by_id.get(body.stage_item_input2_id) if body.stage_item_input2_id else None
    return MatchWithDetails(
        id=MatchId(-match_number),
        created=DUMMY_MOCK_TIME,
        round_id=body.round_id,
        duration_minutes=body.duration_minutes,
        margin_minutes=body.margin_minutes,
        stage_item_input1=input1,
        stage_item_input2=input2,
        stage_item_input1_id=body.stage_item_input1_id,
        stage_item_input2_id=body.stage_item_input2_id,
        stage_item_input1_winner_from_match_id=body.stage_item_input1_winner_from_match_id,
        stage_item_input2_winner_from_match_id=body.stage_item_input2_winner_from_match_id,
        stage_item_input1_score=0,
        stage_item_input2_score=0,
        stage_item_input1_conflict=False,
        stage_item_input2_conflict=False,
    )


def expected_playable_matches(entrant_count: int, bracket_size: int) -> int:
    """A bracket of B plays B - 1 matches, minus the B - N structural byes."""
    return (bracket_size - 1) - (bracket_size - entrant_count)


# A. the bye-aware distribution leaves no dead (empty/empty) match anywhere in the tree
def test_no_dead_matches_for_any_entrant_count() -> None:
    for entrant_count in (2, 3, 4, 5, 6, 7, 8, 9, 13, 15, 16):
        stage_item_ = build_topology(entrant_count)
        structures = get_match_structures(stage_item_)
        dead = [id_ for id_, structure in structures.items() if structure is MatchStructure.DEAD]

        assert not dead, f"{entrant_count} entrants created dead matches {dead}"


# B. the number of structural byes is exactly B - N: one per bye, and no more
def test_number_of_structural_byes_is_the_bye_count() -> None:
    for entrant_count in (3, 5, 6, 7, 9, 13, 15):
        stage_item_ = build_topology(entrant_count)
        bracket_size = bracket_size_for_entrant_count(entrant_count)
        assert bracket_size is not None
        structures = get_match_structures(stage_item_)

        byes = sum(1 for value in structures.values() if value is MatchStructure.STRUCTURAL_BYE)
        assert byes == bracket_size - entrant_count


# C. every candidate match of a full bracket is playable, as before
def test_full_bracket_keeps_every_match_playable() -> None:
    for entrant_count in (2, 4, 8, 16):
        stage_item_ = build_topology(entrant_count)
        structures = get_match_structures(stage_item_)

        assert set(structures.values()) == {MatchStructure.PLAYABLE}
        assert len(structures) == entrant_count - 1


# D. only playable matches are handed to the scheduler, derived from the tree
def test_only_playable_matches_are_scheduled() -> None:
    for entrant_count in (2, 3, 4, 5, 6, 7, 8, 9, 15):
        stage_item_ = build_topology(entrant_count)
        bracket_size = bracket_size_for_entrant_count(entrant_count)
        assert bracket_size is not None

        to_schedule = get_matches_to_schedule(stage_item_)
        structures = get_match_structures(stage_item_)

        assert len(to_schedule) == expected_playable_matches(entrant_count, bracket_size)
        assert {match.id for match in to_schedule} == get_playable_match_ids(stage_item_)
        for match in to_schedule:
            assert structures[match.id] is MatchStructure.PLAYABLE
        for match_id, structure in structures.items():
            if structure is not MatchStructure.PLAYABLE:
                assert match_id not in {match.id for match in to_schedule}


# E. a structural bye is one entrant over a dead slot: it never holds two entrants
def test_structural_byes_hold_one_entrant_at_most() -> None:
    for entrant_count in (3, 5, 6, 7, 15):
        stage_item_ = build_topology(entrant_count)
        structures = get_match_structures(stage_item_)

        for round_ in stage_item_.rounds:
            for match in round_.matches:
                if structures[match.id] is not MatchStructure.STRUCTURAL_BYE:
                    continue
                populated = [
                    input_
                    for input_ in (match.stage_item_input1, match.stage_item_input2)
                    if input_ is not None and input_.team_id is not None
                ]
                assert len(populated) == 1
                assert match.get_winner() is None
                assert match.start_time is None and match.court_id is None


# F. the engine pairs a bye with a real entrant instead of pairing two empty slots
def test_first_round_never_has_two_empty_slots_for_six_in_eight() -> None:
    stage_item_ = build_topology(6)
    first_round = stage_item_.rounds[0]

    for match in first_round.matches:
        empty_slots = sum(
            1
            for input_ in (match.stage_item_input1, match.stage_item_input2)
            if input_ is None or input_.team_id is None
        )
        assert empty_slots <= 1
        assert match.start_time is None and match.position_in_schedule is None


# G. the old "entrants first, empties last" slot order did create a ghost: the distribution is what
#    removes it, and the engine no longer builds that shape for a new bracket
def test_naive_slot_order_is_what_created_the_ghost() -> None:
    naive_slots: list[int | None] = [1, 2, 3, 4, 5, 6, None, None]
    distributed = distribute_entrants_into_slots([1, 2, 3, 4, 5, 6], 8)

    def ghosts(slots: list[int | None]) -> int:
        return sum(
            1
            for pair in range(len(slots) // 2)
            if slots[2 * pair] is None and slots[2 * pair + 1] is None
        )

    assert ghosts(naive_slots) == 1
    assert ghosts(distributed) == 0
    assert order_bye_aware(naive_slots, 8) == distributed


# H. undecided future rounds stay planifiable: exactly the first round of a bye-aware bracket holds
#    structural matches, every later match can still become a real fight
def test_later_rounds_have_no_structural_matches() -> None:
    for entrant_count in (5, 6, 7):
        stage_item_ = build_topology(entrant_count)
        structures = get_match_structures(stage_item_)
        first_round_id = stage_item_.rounds[0].id

        for round_ in stage_item_.rounds:
            if round_.id == first_round_id:
                continue
            for match in round_.matches:
                assert structures[match.id] is MatchStructure.PLAYABLE


# I. impossible brackets are still rejected by the size invariant
def test_bracket_size_guard() -> None:
    with pytest.raises(ValueError):
        distribute_entrants_into_slots([1, 2], 8)
