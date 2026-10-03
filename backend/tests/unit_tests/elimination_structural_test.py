"""
P2.8A - Structural bye/walkover advancement in single elimination trees.

A match that can never receive a second entrant is not a real fight: its only
entrant must advance structurally, without inventing scores or winners.

Terminology (see docs/17-bye-auto-advance.md):

- direct structural bye : one entrant + one empty slot in the first round
- dead / ghost branch   : a match where no entrant can ever appear (empty/empty)
- structural walkover   : a later match whose only possible entrant is determined
                          while the other branch is dead
- pending               : an entrant that will be known later (real match not
                          decided yet, or a tentative input from another stage item)
"""

from bracket.logic.ranking.elimination import (
    get_inputs_to_update_in_subsequent_elimination_rounds,
)
from bracket.models.db.match import Match, MatchWithDetails, MatchWithDetailsDefinitive
from bracket.models.db.stage_item import StageType
from bracket.models.db.stage_item_inputs import (
    StageItemInput,
    StageItemInputEmpty,
    StageItemInputFinal,
)
from bracket.models.db.team import Team
from bracket.models.db.util import RoundWithMatches, StageItemWithRounds
from bracket.utils.dummy_records import DUMMY_MOCK_TIME, DUMMY_TEAM1
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

# Round ids increase with the round, like they do in the database, so that the
# resolver processes the rounds in the right order.
ROUND_1 = RoundId(-3)
ROUND_2 = RoundId(-2)
ROUND_3 = RoundId(-1)


def team(team_id: int) -> Team:
    return Team(**DUMMY_TEAM1.model_dump(), id=TeamId(team_id))


def entrant(input_id: int, slot: int, team_id: int) -> StageItemInputFinal:
    return StageItemInputFinal(
        id=StageItemInputId(input_id),
        slot=slot,
        tournament_id=TOURNAMENT_ID,
        stage_item_id=STAGE_ITEM_ID,
        team_id=TeamId(team_id),
        team=team(team_id),
    )


def empty(input_id: int, slot: int) -> StageItemInputEmpty:
    return StageItemInputEmpty(
        id=StageItemInputId(input_id),
        slot=slot,
        tournament_id=TOURNAMENT_ID,
        stage_item_id=STAGE_ITEM_ID,
    )


def match(
    match_id: int,
    round_id: RoundId,
    input1: StageItemInput | None = None,
    input2: StageItemInput | None = None,
    *,
    from1: int | None = None,
    from2: int | None = None,
    score1: int = 0,
    score2: int = 0,
) -> MatchWithDetails:
    return MatchWithDetails(
        id=MatchId(match_id),
        created=DUMMY_MOCK_TIME,
        round_id=round_id,
        duration_minutes=5,
        margin_minutes=0,
        stage_item_input1=input1,
        stage_item_input2=input2,
        stage_item_input1_id=input1.id if input1 else None,
        stage_item_input2_id=input2.id if input2 else None,
        stage_item_input1_winner_from_match_id=MatchId(from1) if from1 is not None else None,
        stage_item_input2_winner_from_match_id=MatchId(from2) if from2 is not None else None,
        stage_item_input1_score=score1,
        stage_item_input2_score=score2,
        stage_item_input1_conflict=False,
        stage_item_input2_conflict=False,
    )


def round_(
    round_id: RoundId, matches: list[MatchWithDetails | MatchWithDetailsDefinitive]
) -> RoundWithMatches:
    return RoundWithMatches(
        id=round_id,
        matches=matches,
        stage_item_id=STAGE_ITEM_ID,
        created=DUMMY_MOCK_TIME,
        is_draft=False,
        name="",
    )


def stage_item(rounds: list[RoundWithMatches], inputs: list[StageItemInput]) -> StageItemWithRounds:
    return StageItemWithRounds(
        rounds=rounds,
        inputs=inputs,
        type_name="Single Elimination",
        team_count=4,
        ranking_id=None,
        id=STAGE_ITEM_ID,
        stage_id=StageId(-1),
        name="",
        created=DUMMY_MOCK_TIME,
        type=StageType.SINGLE_ELIMINATION,
    )


def resolve(
    stage_item_: StageItemWithRounds,
    round_id: RoundId = ROUND_1,
    match_ids: set[MatchId] | None = None,
) -> dict[MatchId, Match]:
    return get_inputs_to_update_in_subsequent_elimination_rounds(round_id, stage_item_, match_ids)


def apply_updates(
    stage_item_: StageItemWithRounds, updates: dict[MatchId, Match]
) -> StageItemWithRounds:
    """Rebuild the stage item as it would look after the updates are persisted."""
    return stage_item_.model_copy(
        update={
            "rounds": [
                round_.model_copy(
                    update={
                        "matches": [updates.get(match_.id, match_) for match_ in round_.matches]
                    }
                )
                for round_ in stage_item_.rounds
            ]
        }
    )


def later_round_updates(
    updates: dict[MatchId, Match],
    stage_item_: StageItemWithRounds,
    round_id: RoundId,
) -> dict[MatchId, Match]:
    """Matches of rounds after `round_id` that the resolver wants to change."""
    first_round_match_ids = {
        match_.id
        for round_ in stage_item_.rounds
        if round_.id == round_id
        for match_ in round_.matches
    }
    return {id_: match_ for id_, match_ in updates.items() if id_ not in first_round_match_ids}


# A. real/real (not decided) -> normal fight, no auto-advance
def test_real_match_is_not_auto_advanced() -> None:
    input1, input2 = entrant(-1, 1, 1), entrant(-2, 2, 2)
    match1 = match(-1, ROUND_1, input1, input2)
    match2 = match(-2, ROUND_2, from1=-1)

    updates = resolve(stage_item([round_(ROUND_1, [match1]), round_(ROUND_2, [match2])], [input1]))

    assert MatchId(-2) not in updates
    assert match1.get_winner() is None


# B. entrant/empty -> direct structural bye -> the entrant advances
def test_direct_bye_advances_its_only_entrant() -> None:
    input1, empty1 = entrant(-1, 1, 1), empty(-2, 2)
    match1 = match(-1, ROUND_1, input1, empty1)
    match2 = match(-2, ROUND_2, from1=-1)
    stage_item_ = stage_item([round_(ROUND_1, [match1]), round_(ROUND_2, [match2])], [input1])

    updates = resolve(stage_item_)

    assert updates[MatchId(-2)].stage_item_input1_id == input1.id
    assert updates[MatchId(-2)].stage_item_input1 == input1
    assert updates[MatchId(-2)].stage_item_input2_id is None
    # no fictitious score or winner on the structural match
    assert match1.stage_item_input1_score == 0
    assert match1.stage_item_input2_score == 0
    assert match1.get_winner() is None


# C. empty/empty -> dead branch -> no winner, no artificial entrant
def test_dead_branch_produces_no_entrant() -> None:
    match1 = match(-1, ROUND_1, empty(-1, 1), empty(-2, 2))
    match2 = match(-2, ROUND_2, from1=-1)
    stage_item_ = stage_item([round_(ROUND_1, [match1]), round_(ROUND_2, [match2])], [empty(-1, 1)])

    assert later_round_updates(resolve(stage_item_), stage_item_, ROUND_1) == {}
    assert match1.get_winner() is None


# D. determined/dead -> structural walkover (also chained through dead branches)
def test_structural_walkover_propagates_through_dead_branch() -> None:
    input1, empty1 = entrant(-1, 1, 1), empty(-2, 2)
    match1 = match(-1, ROUND_1, input1, empty1)  # direct bye
    match2 = match(-2, ROUND_2, from1=-1)  # dead second slot
    match3 = match(-3, ROUND_3, from1=-2)  # dead second slot
    stage_item_ = stage_item(
        [
            round_(ROUND_1, [match1]),
            round_(ROUND_2, [match2]),
            round_(ROUND_3, [match3]),
        ],
        [input1],
    )

    updates = resolve(stage_item_)

    assert updates[MatchId(-2)].stage_item_input1_id == input1.id
    assert updates[MatchId(-3)].stage_item_input1_id == input1.id
    assert match2.get_winner() is None
    assert match3.get_winner() is None
    assert match1.stage_item_input1_score == 0


# E. pending/dead -> wait, never advance prematurely
def test_pending_entrant_with_dead_branch_waits() -> None:
    input1, input2 = entrant(-1, 1, 1), entrant(-2, 2, 2)
    match1 = match(-1, ROUND_1, input1, input2)  # real fight, not decided yet
    match2 = match(-2, ROUND_1, empty(-3, 3), empty(-4, 4))  # dead branch
    match3 = match(-3, ROUND_2, from1=-1, from2=-2)
    stage_item_ = stage_item(
        [round_(ROUND_1, [match1, match2]), round_(ROUND_2, [match3])], [input1]
    )

    assert later_round_updates(resolve(stage_item_), stage_item_, ROUND_1) == {}


# F. determined/pending -> wait, no auto-advance either
def test_determined_entrant_with_pending_branch_waits() -> None:
    input1, empty1 = entrant(-1, 1, 1), empty(-2, 2)
    pending1, pending2 = entrant(-3, 3, 3), entrant(-4, 4, 4)
    match1 = match(-1, ROUND_1, input1, empty1)  # direct bye
    match2 = match(-2, ROUND_1, pending1, pending2)  # real fight, not decided yet
    match3 = match(-3, ROUND_2, from1=-1, from2=-2)
    match4 = match(-4, ROUND_3, from1=-3)
    stage_item_ = stage_item(
        [round_(ROUND_1, [match1, match2]), round_(ROUND_2, [match3]), round_(ROUND_3, [match4])],
        [input1],
    )

    updates = resolve(stage_item_)

    assert updates[MatchId(-3)].stage_item_input1_id == input1.id
    assert updates[MatchId(-3)].stage_item_input2_id is None
    assert MatchId(-4) not in updates


# G. 3 entrants in a bracket of 4: one direct bye, its entrant reaches the final
def test_three_entrants_bracket_of_four() -> None:
    input1, input2 = entrant(-1, 1, 1), entrant(-2, 2, 2)
    input3, empty4 = entrant(-3, 3, 3), empty(-4, 4)
    match78 = match(-78, ROUND_1, input1, input2)
    match79 = match(-79, ROUND_1, input3, empty4)
    match80 = match(-80, ROUND_2, from1=-78, from2=-79)
    stage_item_ = stage_item(
        [round_(ROUND_1, [match78, match79]), round_(ROUND_2, [match80])], [input1]
    )

    updates = resolve(stage_item_)

    assert updates[MatchId(-80)].stage_item_input2_id == input3.id
    assert updates[MatchId(-80)].stage_item_input1_id is None


# H. 6 entrants in a bracket of 8: the current generator creates ONE ghost match,
#    not two direct byes. Nothing may be invented; the propagation must be correct
#    once the real feeder is decided.
def test_six_entrants_bracket_of_eight_respects_current_seeding() -> None:
    e1, e2, e3, e4 = (entrant(-i, i, i) for i in range(1, 5))
    e5, e6 = entrant(-5, 5, 5), entrant(-6, 6, 6)
    match82 = match(-82, ROUND_1, e1, e2)
    match83 = match(-83, ROUND_1, e3, e4)
    match84 = match(-84, ROUND_1, e5, e6)
    match85 = match(-85, ROUND_1, empty(-7, 7), empty(-8, 8))  # ghost (empty/empty)
    match86 = match(-86, ROUND_2, from1=-82, from2=-83)
    match87 = match(-87, ROUND_2, from1=-84, from2=-85)
    match88 = match(-88, ROUND_3, from1=-86, from2=-87)
    rounds = [
        round_(ROUND_1, [match82, match83, match84, match85]),
        round_(ROUND_2, [match86, match87]),
        round_(ROUND_3, [match88]),
    ]
    stage_item_ = stage_item(rounds, [e1])

    # nothing to resolve yet: no direct bye, no decided entrant
    assert later_round_updates(resolve(stage_item_), stage_item_, ROUND_1) == {}

    # the real feeder -84 is decided: -84's winner advances and, because the other
    # branch of -87 is dead, it must also cross -87 without a fictitious fight
    decided_match84 = match(-84, ROUND_1, e5, e6, score1=3, score2=0)
    stage_item_decided = stage_item_.model_copy(
        update={
            "rounds": [
                round_(ROUND_1, [match82, match83, decided_match84, match85]),
                round_(ROUND_2, [match86, match87]),
                round_(ROUND_3, [match88]),
            ]
        }
    )

    updates = resolve(stage_item_decided, ROUND_1, {MatchId(-84)})

    assert updates[MatchId(-87)].stage_item_input1_id == e5.id
    assert updates[MatchId(-87)].stage_item_input2_id is None
    assert updates[MatchId(-88)].stage_item_input2_id == e5.id
    assert match85.get_winner() is None
    assert match85.stage_item_input1_score == 0 and match85.stage_item_input2_score == 0


# I. 7 entrants in a bracket of 8: one direct bye
def test_seven_entrants_bracket_of_eight() -> None:
    e = [entrant(-i, i, i) for i in range(1, 8)]
    match89, match90 = match(-89, ROUND_1, e[0], e[1]), match(-90, ROUND_1, e[2], e[3])
    match91, match92 = match(-91, ROUND_1, e[4], e[5]), match(-92, ROUND_1, e[6], empty(-8, 8))
    match93 = match(-93, ROUND_2, from1=-89, from2=-90)
    match94 = match(-94, ROUND_2, from1=-91, from2=-92)
    match95 = match(-95, ROUND_3, from1=-93, from2=-94)
    stage_item_ = stage_item(
        [
            round_(ROUND_1, [match89, match90, match91, match92]),
            round_(ROUND_2, [match93, match94]),
            round_(ROUND_3, [match95]),
        ],
        [e[0]],
    )

    updates = resolve(stage_item_)

    assert updates[MatchId(-94)].stage_item_input2_id == e[6].id
    assert MatchId(-95) not in updates


# J. 5 entrants in a bracket of 8: one direct bye + one ghost, chained propagation
def test_five_entrants_bracket_of_eight() -> None:
    e = [entrant(-i, i, i) for i in range(1, 6)]
    match97, match98 = match(-97, ROUND_1, e[0], e[1]), match(-98, ROUND_1, e[2], e[3])
    match99 = match(-99, ROUND_1, e[4], empty(-6, 6))
    match100 = match(-100, ROUND_1, empty(-7, 7), empty(-8, 8))  # ghost
    match101 = match(-101, ROUND_2, from1=-97, from2=-98)
    match102 = match(-102, ROUND_2, from1=-99, from2=-100)
    match103 = match(-103, ROUND_3, from1=-101, from2=-102)
    stage_item_ = stage_item(
        [
            round_(ROUND_1, [match97, match98, match99, match100]),
            round_(ROUND_2, [match101, match102]),
            round_(ROUND_3, [match103]),
        ],
        [e[0]],
    )

    updates = resolve(stage_item_)

    assert updates[MatchId(-102)].stage_item_input1_id == e[4].id
    assert updates[MatchId(-102)].stage_item_input2_id is None
    assert updates[MatchId(-103)].stage_item_input2_id == e[4].id
    assert updates[MatchId(-103)].stage_item_input1_id is None
    assert match100.get_winner() is None


# K. idempotency: running the resolver twice changes nothing and duplicates nothing
def test_resolver_is_idempotent() -> None:
    e = [entrant(-i, i, i) for i in range(1, 6)]
    match97, match98 = match(-97, ROUND_1, e[0], e[1]), match(-98, ROUND_1, e[2], e[3])
    match99 = match(-99, ROUND_1, e[4], empty(-6, 6))
    match100 = match(-100, ROUND_1, empty(-7, 7), empty(-8, 8))
    match101 = match(-101, ROUND_2, from1=-97, from2=-98)
    match102 = match(-102, ROUND_2, from1=-99, from2=-100)
    match103 = match(-103, ROUND_3, from1=-101, from2=-102)
    stage_item_ = stage_item(
        [
            round_(ROUND_1, [match97, match98, match99, match100]),
            round_(ROUND_2, [match101, match102]),
            round_(ROUND_3, [match103]),
        ],
        [e[0]],
    )

    first = resolve(stage_item_)
    applied = apply_updates(stage_item_, first)
    second = resolve(applied)

    assert later_round_updates(second, applied, ROUND_1) == {}
    # the advanced entrant is still the very same input row, not a duplicate
    assert applied.rounds[1].matches[1].stage_item_input1_id == e[4].id


# L. structural matches are never real candidates: no fictitious winner or score,
#    and never two entrants on the same structural match
def test_structural_matches_are_not_competitive() -> None:
    input1, empty1 = entrant(-1, 1, 1), empty(-2, 2)
    match1 = match(-1, ROUND_1, input1, empty1)
    match2 = match(-2, ROUND_2, from1=-1)
    stage_item_ = stage_item([round_(ROUND_1, [match1]), round_(ROUND_2, [match2])], [input1])

    updates = resolve(stage_item_)

    populated = [
        input_
        for input_ in (match1.stage_item_input1, match1.stage_item_input2)
        if input_ is not None and input_.team_id is not None
    ]
    assert len(populated) == 1  # a candidate needs two entrants; this match has one
    assert match1.get_winner() is None
    assert (match1.stage_item_input1_score, match1.stage_item_input2_score) == (0, 0)
    assert updates[MatchId(-2)].stage_item_input1_score == 0
    assert updates[MatchId(-2)].stage_item_input2_score == 0


# M. real results keep working exactly as before
def test_real_results_still_propagate_by_scores() -> None:
    input1, input2 = entrant(-1, 1, 1), entrant(-2, 2, 2)
    input3, input4 = entrant(-3, 3, 3), entrant(-4, 4, 4)
    match1 = match(-1, ROUND_1, input1, input2, score1=2, score2=0)
    match2 = match(-2, ROUND_1, input3, input4, score1=1, score2=3)
    match3 = match(-3, ROUND_2, from1=-1, from2=-2)
    stage_item_ = stage_item(
        [round_(ROUND_1, [match1, match2]), round_(ROUND_2, [match3])], [input1]
    )

    updates = resolve(stage_item_)

    assert updates[MatchId(-3)].stage_item_input1_id == input1.id
    assert updates[MatchId(-3)].stage_item_input2_id == input4.id
    assert match1.get_winner() == input1
    assert match2.get_winner() == input4


# N. a pass over a later round must not hand back the matches it did not change: writing
#    those back would send stale input ids to the database and erase what an earlier round
#    of the same complete pass has just advanced.
def test_pass_does_not_rewrite_untouched_matches() -> None:
    e = [entrant(-i, i, i) for i in range(1, 4)]
    match78 = match(-78, ROUND_1, e[0], e[1])
    match79 = match(-79, ROUND_1, e[2], empty(-4, 4))
    match80 = match(-80, ROUND_2, from1=-78, from2=-79)
    stage_item_ = stage_item(
        [round_(ROUND_1, [match78, match79]), round_(ROUND_2, [match80])], [e[0]]
    )

    first_pass = resolve(stage_item_, ROUND_1)
    second_pass = resolve(stage_item_, ROUND_2)

    assert first_pass[MatchId(-80)].stage_item_input2_id == e[2].id
    assert second_pass == {}
