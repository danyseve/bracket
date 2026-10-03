"""Explicit generation of a single elimination bracket from the entrants of a stage item.

Creating a stage item through the standard UI does not know its entrants yet: the stage item is
created together with empty slots (``sql_create_stage_item_with_empty_inputs``) and the teams are
assigned to those slots afterwards. Pushing the teams into the first slots and leaving the empty
ones at the end pairs empty slots with each other, which creates matches nobody can ever play.
``distribute_entrants_into_slots`` spreads the byes over the whole first round instead of
clustering them at the end.

This module applies that distribution, followed by the structural resolver of P2.8A, to an
existing stage item, so the standard UI flow can generate the same bracket as the
create-with-inputs flow. Generation is always explicit: it never runs as a side effect of
assigning, removing or editing a team. Nothing here writes scores, winners, courts or planning.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from fastapi import HTTPException
from starlette import status

from bracket.database import database
from bracket.logic.ranking.calculation import recalculate_ranking_for_stage_item
from bracket.logic.ranking.elimination import update_inputs_in_complete_elimination_stage_item
from bracket.logic.scheduling.seeding import distribute_entrants_into_slots
from bracket.models.db.match import MatchWithDetails, MatchWithDetailsDefinitive
from bracket.models.db.stage_item import StageType
from bracket.models.db.stage_item_inputs import StageItemInputTentative
from bracket.models.db.util import StageItemWithRounds
from bracket.sql.stage_item_inputs import sql_set_team_id_for_stage_item_input
from bracket.sql.stage_items import get_stage_item
from bracket.utils.id_types import StageItemInputId, TeamId, TournamentId

# Machine readable reason of a blocked generation, returned as `generate_bracket_blocked: <value>`
# so that clients can translate it. The value is part of the API: do not rename one in place.
BLOCKER_PREFIX = "generate_bracket_blocked"


class BracketGenerationBlocker(StrEnum):
    """Reason why a bracket cannot be (re)generated right now."""

    NOT_SINGLE_ELIMINATION = "not_single_elimination"
    TENTATIVE_INPUTS = "tentative_inputs"
    SCORES = "scores"
    WINNER = "winner"
    PLANNING = "planning"
    TOO_FEW_ENTRANTS = "too_few_entrants"
    BRACKET_TOO_LARGE = "bracket_too_large"
    INCONSISTENT_SLOTS = "inconsistent_slots"


@dataclass(frozen=True)
class BracketGenerationPlan:
    """The bracket a stage item should have, and the slots that have to move to get there."""

    entrant_count: int
    bracket_size: int
    ghost_count: int
    changes: tuple[tuple[StageItemInputId, TeamId | None], ...]

    @property
    def bye_count(self) -> int:
        """Number of first-round slots that stay empty, i.e. direct byes."""
        return self.bracket_size - self.entrant_count

    @property
    def changed(self) -> bool:
        """Whether applying the plan writes anything; False means it is already generated."""
        return len(self.changes) > 0


def _get_match_blocker(
    match: MatchWithDetails | MatchWithDetailsDefinitive,
) -> BracketGenerationBlocker | None:
    """The reason this match cannot be redistributed, if there is one."""
    if match.stage_item_input1_score != 0 or match.stage_item_input2_score != 0:
        return BracketGenerationBlocker.SCORES
    if match.get_winner() is not None:
        return BracketGenerationBlocker.WINNER
    return None


def _get_activity_blocker(
    stage_item: StageItemWithRounds,
) -> BracketGenerationBlocker | None:
    """
    Whether something already depends on the current layout of the bracket.

    A score or a winner fixes which slot advances; a court or a schedule position fixes when the
    match is played. From that moment on the layout is no longer ours to change.
    """
    for round_ in stage_item.rounds:
        for match in round_.matches:
            blocker = _get_match_blocker(match)
            if blocker is not None:
                return blocker
            if (
                match.court_id is not None
                or match.start_time is not None
                or match.position_in_schedule is not None
            ):
                return BracketGenerationBlocker.PLANNING
    return None


def _get_entrants_blocker(
    stage_item: StageItemWithRounds,
) -> BracketGenerationBlocker | None:
    """Whether the stage item holds a number of entrants this bracket can be built with."""
    entrant_count = count_entrants(stage_item)
    if entrant_count < 2:
        return BracketGenerationBlocker.TOO_FEW_ENTRANTS
    if stage_item.team_count > 2 * entrant_count:
        return BracketGenerationBlocker.BRACKET_TOO_LARGE
    return None


def get_bracket_generation_blocker(
    stage_item: StageItemWithRounds,
) -> BracketGenerationBlocker | None:
    """
    Determine whether this stage item may be generated, and if not, why.

    A bracket is only generated once it is the sole thing that decides where its entrants go: as
    soon as there is a score, a winner, a court, a start time or a schedule position, something
    else depends on the current layout. Tentative inputs (winners of another stage item) are
    entrant placeholders that belong to a stage item on their own, so they are not redistributed.
    """
    if stage_item.type != StageType.SINGLE_ELIMINATION:
        return BracketGenerationBlocker.NOT_SINGLE_ELIMINATION
    if any(isinstance(input_, StageItemInputTentative) for input_ in stage_item.inputs):
        return BracketGenerationBlocker.TENTATIVE_INPUTS
    return _get_activity_blocker(stage_item) or _get_entrants_blocker(stage_item)


def count_entrants(stage_item: StageItemWithRounds) -> int:
    """The number of slots that already hold a team, i.e. the entrants of the bracket."""
    return sum(1 for input_ in stage_item.inputs if input_.team_id is not None)


def plan_bracket_generation(stage_item: StageItemWithRounds) -> BracketGenerationPlan:
    """
    Determine the bye-aware layout of a stage item, without touching the database.

    The entrants keep their relative order and the bracket size stays exactly the ``team_count``
    of the stage item, which is already the smallest power of two that holds them. Only the slots
    whose occupant changes are reported, so generating twice is a no-op.
    """
    inputs = sorted(stage_item.inputs, key=lambda input_: input_.slot)
    if len(inputs) != stage_item.team_count:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{BLOCKER_PREFIX}: {BracketGenerationBlocker.INCONSISTENT_SLOTS.value}",
        )

    entrants = [input_ for input_ in inputs if input_.team_id is not None]
    distribution = distribute_entrants_into_slots(entrants, stage_item.team_count)

    changes: list[tuple[StageItemInputId, TeamId | None]] = []
    for input_, target in zip(inputs, distribution, strict=True):
        target_team_id = target.team_id if target is not None else None
        if input_.team_id != target_team_id:
            changes.append((input_.id, target_team_id))

    ghost_count = sum(
        1
        for index in range(0, len(distribution), 2)
        if distribution[index] is None and distribution[index + 1] is None
    )

    return BracketGenerationPlan(
        entrant_count=len(entrants),
        bracket_size=stage_item.team_count,
        ghost_count=ghost_count,
        changes=tuple(changes),
    )


async def generate_bracket_for_stage_item(
    tournament_id: TournamentId, stage_item: StageItemWithRounds
) -> BracketGenerationPlan:
    """
    Generate the bye-aware bracket of a stage item and materialize its structural advancements.

    Everything happens in a single transaction, so a failure leaves the bracket as it was. The
    structural resolver runs on the freshly read state: a bye does not depend on anybody entering
    a fictitious result by hand.
    """
    blocker = get_bracket_generation_blocker(stage_item)
    if blocker is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{BLOCKER_PREFIX}: {blocker.value}",
        )

    plan = plan_bracket_generation(stage_item)

    async with database.transaction():
        # A team can only be in one slot of a stage item, so moving teams around has to go
        # through an empty slot: first release every slot that changes, then fill them in.
        # Otherwise a swap would briefly put the same team in two slots of the stage item.
        for stage_item_input_id, _ in plan.changes:
            await sql_set_team_id_for_stage_item_input(tournament_id, stage_item_input_id, None)

        for stage_item_input_id, team_id in plan.changes:
            if team_id is not None:
                await sql_set_team_id_for_stage_item_input(
                    tournament_id, stage_item_input_id, team_id
                )

        refreshed_stage_item = await get_stage_item(tournament_id, stage_item.id)
        await recalculate_ranking_for_stage_item(tournament_id, refreshed_stage_item)
        await update_inputs_in_complete_elimination_stage_item(refreshed_stage_item)

    return plan
