"""Structural classification of the matches of an elimination stage item.

A single elimination tree holds matches of three different kinds:

- ``PLAYABLE``      : two entrants can still show up, so it is a real fight. It may need a court.
- ``STRUCTURAL_BYE``: only one entrant can ever show up, because the other slot is dead (a bye in
                      the first round, or a walkover over a dead branch). Not a fight.
- ``DEAD``          : no entrant can ever show up (an empty/empty match). Nothing is played here.

A fight whose feeders have not been decided yet is still ``PLAYABLE``: two entrants can show up, so
it is a future fight that may be planned. The classification is derived from the tree topology
alone, so it does not depend on scores and it is stable over calls.
"""

from __future__ import annotations

from enum import auto

from bracket.logic.ranking.elimination import get_dead_slots
from bracket.models.db.util import StageItemWithRounds
from bracket.utils.id_types import MatchId
from bracket.utils.types import EnumAutoStr


class MatchStructure(EnumAutoStr):
    PLAYABLE = auto()
    STRUCTURAL_BYE = auto()
    DEAD = auto()


def _classify(dead_slot_count: int) -> MatchStructure:
    if dead_slot_count >= 2:
        return MatchStructure.DEAD
    if dead_slot_count == 1:
        return MatchStructure.STRUCTURAL_BYE
    return MatchStructure.PLAYABLE


def get_match_structures(stage_item: StageItemWithRounds) -> dict[MatchId, MatchStructure]:
    """
    Classify every match of a stage item, keyed by match id.

    A slot that can never receive an entrant is dead; a match with two dead slots can never be
    played, and a match with one dead slot can only ever hold one entrant.
    """
    return {
        match_id: _classify(len(dead_slots))
        for match_id, dead_slots in get_dead_slots(stage_item).items()
    }


def get_playable_match_ids(stage_item: StageItemWithRounds) -> set[MatchId]:
    """Ids of the matches that can still become a real fight."""
    return {
        match_id
        for match_id, structure in get_match_structures(stage_item).items()
        if structure is MatchStructure.PLAYABLE
    }
