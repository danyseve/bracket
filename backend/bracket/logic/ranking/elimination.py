from bracket.models.db.match import Match
from bracket.models.db.stage_item_inputs import StageItemInput, StageItemInputTentative
from bracket.models.db.util import StageItemWithRounds
from bracket.sql.matches import (
    sql_set_input_ids_for_match,
)
from bracket.utils.id_types import (
    MatchId,
    RoundId,
)


def get_dead_slots(stage_item: StageItemWithRounds) -> dict[MatchId, set[int]]:
    """
    Determine per match which input slots can never receive an entrant.

    A slot is dead when it holds no team and nothing else can ever fill it: neither the
    winner of an earlier match (its whole branch is dead), nor a tentative input coming
    from another stage item. Slots are identified by index: 0 is the first input, 1 the
    second one.

    This is derived from the tree topology only, so it does not depend on scores and is
    stable over calls. It is used to tell a structural match (a bye or a walkover, which
    can only ever hold one entrant) apart from a fight that simply has not happened yet.
    """
    possible_entrants: dict[MatchId, int] = {}
    dead_slots: dict[MatchId, set[int]] = {}

    for round_ in sorted(stage_item.rounds, key=lambda round_: round_.id):
        for match in round_.matches:
            dead: set[int] = set()
            count = 0
            slot_feeds = (
                (match.stage_item_input1, match.stage_item_input1_winner_from_match_id),
                (match.stage_item_input2, match.stage_item_input2_winner_from_match_id),
            )
            for slot, (input_, winner_from_match_id) in enumerate(slot_feeds):
                if input_ is not None and input_.team_id is not None:
                    count += 1
                elif isinstance(input_, StageItemInputTentative):
                    count += 1  # will be filled in by a previous stage item
                elif winner_from_match_id is not None:
                    if possible_entrants.get(winner_from_match_id, 0) > 0:
                        count += 1
                    else:
                        dead.add(slot)
                else:
                    dead.add(slot)

            possible_entrants[match.id] = count
            dead_slots[match.id] = dead

    return dead_slots


def get_advancing_input(match: Match, dead_slots: dict[MatchId, set[int]]) -> StageItemInput | None:
    """
    Determine which input advances from a match.

    - a competitive winner (decided by the scores) always advances,
    - a match that can only ever hold one entrant advances that entrant structurally, as
      long as the other slot is dead: a bye or a walkover is not a fight, so it gets no
      score, no duration and no winner of its own,
    - anything else (undecided fight, entrant still pending) advances nobody yet.
    """
    match_winner = match.get_winner()
    if match_winner is not None:
        return match_winner

    inputs = (match.stage_item_input1, match.stage_item_input2)
    occupied_slots = [
        slot
        for slot, input_ in enumerate(inputs)
        if input_ is not None and input_.team_id is not None
    ]
    if len(occupied_slots) != 1:
        return None

    occupied_slot = occupied_slots[0]
    if dead_slots.get(match.id, set()) != {1 - occupied_slot}:
        return None

    return inputs[occupied_slot]


def get_inputs_to_update_in_subsequent_elimination_rounds(
    current_round_id: RoundId,
    stage_item: StageItemWithRounds,
    match_ids: set[MatchId] | None = None,
) -> dict[MatchId, Match]:
    """
    Determine the updates of stage item input IDs in the elimination tree.

    Crucial aspect is that entering a winner for a match will influence matches of subsequent
    rounds, because of the tree-like structure of elimination stage items.
    """
    current_round = next(round_ for round_ in stage_item.rounds if round_.id == current_round_id)
    affected_matches: dict[MatchId, Match] = {
        match.id: match
        for match in current_round.matches
        if match_ids is None or match.id in match_ids
    }
    subsequent_rounds = [round_ for round_ in stage_item.rounds if round_.id > current_round.id]
    subsequent_rounds.sort(key=lambda round_: round_.id)
    subsequent_matches = [match for round_ in subsequent_rounds for match in round_.matches]

    # A single-entrant match is not a fight: it advances its entrant structurally, without
    # scores, duration or a winner of its own. Which slots can never be filled follows from
    # the tree topology, so it can be determined once up front.
    dead_slots = get_dead_slots(stage_item)
    updated_match_ids: list[MatchId] = []

    for subsequent_match in subsequent_matches:
        updated_inputs: list[StageItemInput | None] = [
            subsequent_match.stage_item_input1,
            subsequent_match.stage_item_input2,
        ]
        original_inputs = updated_inputs.copy()

        if subsequent_match.stage_item_input1_winner_from_match_id is not None and (
            affected_match1 := affected_matches.get(
                subsequent_match.stage_item_input1_winner_from_match_id
            )
        ):
            updated_inputs[0] = get_advancing_input(affected_match1, dead_slots)

        if subsequent_match.stage_item_input2_winner_from_match_id is not None and (
            affected_match2 := affected_matches.get(
                subsequent_match.stage_item_input2_winner_from_match_id
            )
        ):
            updated_inputs[1] = get_advancing_input(affected_match2, dead_slots)

        if original_inputs != updated_inputs:
            input_ids = [input_.id if input_ else None for input_ in updated_inputs]

            affected_matches[subsequent_match.id] = subsequent_match.model_copy(
                update={
                    "stage_item_input1_id": input_ids[0],
                    "stage_item_input2_id": input_ids[1],
                    "stage_item_input1": updated_inputs[0],
                    "stage_item_input2": updated_inputs[1],
                }
            )
            updated_match_ids.append(subsequent_match.id)

    # Only the matches that this pass actually changed have to be written. Writing the
    # untouched ones back would send the input ids of this snapshot to the database again,
    # erasing what an earlier round of the same complete pass just advanced.
    return {match_id: affected_matches[match_id] for match_id in updated_match_ids}


async def update_inputs_in_subsequent_elimination_rounds(
    current_round_id: RoundId,
    stage_item: StageItemWithRounds,
    match_ids: set[MatchId] | None = None,
) -> None:
    updates = get_inputs_to_update_in_subsequent_elimination_rounds(
        current_round_id, stage_item, match_ids
    )
    for _, match in updates.items():
        await sql_set_input_ids_for_match(
            match.round_id, match.id, [match.stage_item_input1_id, match.stage_item_input2_id]
        )


async def update_inputs_in_complete_elimination_stage_item(
    stage_item: StageItemWithRounds,
) -> None:
    for round_ in stage_item.rounds:
        await update_inputs_in_subsequent_elimination_rounds(round_.id, stage_item)
