"""P2.8B F2A - Bye-aware distribution of entrants over single elimination slots.

Distributing the entrants over the slots of a bracket must not create matches that nobody can
ever play: pushing every entrant to the front and leaving the empty slots at the back is what
creates empty/empty pairs. The distribution below spreads the byes over the whole first round.
"""

import pytest

from bracket.logic.scheduling.seeding import (
    bracket_size_for_entrant_count,
    bye_aware_bye_pairs,
    distribute_entrants_into_slots,
    order_bye_aware,
)


def byes_and_ghosts(entrant_count: int) -> tuple[int, int, int]:
    """(bracket size, number of byes, number of empty/empty first-round pairs)."""
    bracket_size = bracket_size_for_entrant_count(entrant_count)
    assert bracket_size is not None
    distributed = distribute_entrants_into_slots(list(range(entrant_count)), bracket_size)

    byes = sum(1 for slot in distributed if slot is None)
    ghosts = sum(
        1
        for pair in range(bracket_size // 2)
        if distributed[2 * pair] is None and distributed[2 * pair + 1] is None
    )
    return bracket_size, byes, ghosts


# A. bracket size invariant: the smallest power of two that can hold the entrants
def test_bracket_size_is_the_smallest_power_of_two() -> None:
    assert bracket_size_for_entrant_count(2) == 2
    assert bracket_size_for_entrant_count(3) == 4
    assert bracket_size_for_entrant_count(4) == 4
    assert bracket_size_for_entrant_count(5) == 8
    assert bracket_size_for_entrant_count(6) == 8
    assert bracket_size_for_entrant_count(7) == 8
    assert bracket_size_for_entrant_count(8) == 8
    assert bracket_size_for_entrant_count(9) == 16
    assert bracket_size_for_entrant_count(16) == 16
    assert bracket_size_for_entrant_count(17) == 32


# B. fewer than two entrants cannot make a valid single elimination bracket
def test_bracket_size_rejects_too_few_entrants() -> None:
    assert bracket_size_for_entrant_count(0) is None
    assert bracket_size_for_entrant_count(1) is None


# C. 2 entrants in a bracket of 2: no bye, no ghost
def test_two_entrants_bracket_of_two() -> None:
    assert byes_and_ghosts(2) == (2, 0, 0)


# D. 3 entrants in a bracket of 4: one bye, no ghost
def test_three_entrants_bracket_of_four() -> None:
    assert byes_and_ghosts(3) == (4, 1, 0)


# E. 4 entrants in a bracket of 4: no bye, no ghost
def test_four_entrants_bracket_of_four() -> None:
    assert byes_and_ghosts(4) == (4, 0, 0)


# F. 5 entrants in a bracket of 8: three byes, no ghost
def test_five_entrants_bracket_of_eight() -> None:
    assert byes_and_ghosts(5) == (8, 3, 0)


# G. 6 entrants in a bracket of 8: two byes, no ghost
def test_six_entrants_bracket_of_eight() -> None:
    assert byes_and_ghosts(6) == (8, 2, 0)


# H. 7 entrants in a bracket of 8: one bye, no ghost
def test_seven_entrants_bracket_of_eight() -> None:
    assert byes_and_ghosts(7) == (8, 1, 0)


# I. 8 entrants in a bracket of 8: no bye, no ghost
def test_eight_entrants_bracket_of_eight() -> None:
    assert byes_and_ghosts(8) == (8, 0, 0)


# J. 9 entrants in a bracket of 16: seven byes, no ghost
def test_nine_entrants_bracket_of_sixteen() -> None:
    assert byes_and_ghosts(9) == (16, 7, 0)


# K. 15 entrants in a bracket of 16: one bye, no ghost
def test_fifteen_entrants_bracket_of_sixteen() -> None:
    assert byes_and_ghosts(15) == (16, 1, 0)


# L. every entrant is used exactly once, in the order it was given
def test_all_entrants_appear_exactly_once() -> None:
    for entrant_count in range(2, 40):
        bracket_size = bracket_size_for_entrant_count(entrant_count)
        assert bracket_size is not None
        distributed = distribute_entrants_into_slots(list(range(entrant_count)), bracket_size)
        placed = [slot for slot in distributed if slot is not None]

        assert len(distributed) == bracket_size
        assert placed == list(range(entrant_count))
        assert len(set(placed)) == entrant_count


# M. determinism: the same input always gives the same distribution
def test_distribution_is_deterministic() -> None:
    for entrant_count in range(2, 40):
        bracket_size = bracket_size_for_entrant_count(entrant_count)
        assert bracket_size is not None
        first = distribute_entrants_into_slots(list(range(entrant_count)), bracket_size)
        second = distribute_entrants_into_slots(list(range(entrant_count)), bracket_size)

        assert first == second


# N. byes stay a bounded distance apart: they never share a first-round pair, and there is at most
#    one bye per pair, which is what keeps a first-round pair from being empty twice
def test_byes_never_share_a_first_round_pair() -> None:
    for entrant_count in range(2, 40):
        bracket_size = bracket_size_for_entrant_count(entrant_count)
        assert bracket_size is not None
        pairs = bye_aware_bye_pairs(entrant_count, bracket_size)

        assert len(pairs) == bracket_size - entrant_count
        assert len(set(pairs)) == len(pairs)
        assert pairs == sorted(pairs)
        assert all(0 <= pair < bracket_size // 2 for pair in pairs)


# O. rejecting impossible requests instead of silently building a broken bracket
def test_distribution_rejects_impossible_brackets() -> None:
    with pytest.raises(ValueError, match="at least 2 and even"):
        distribute_entrants_into_slots([1, 2], 3)

    with pytest.raises(ValueError, match="do not fit"):
        distribute_entrants_into_slots([1, 2, 3, 4, 5], 4)

    with pytest.raises(ValueError, match="more byes than first-round pairs"):
        distribute_entrants_into_slots([1, 2], 8)


# P. ordering an existing slot list moves the empty slots, keeps the objects and is idempotent
def test_order_bye_aware_moves_byes_and_is_idempotent() -> None:
    # entrants first, empties at the end: the shape the old generator produced for 6 in 8
    slots: list[int | None] = [1, 2, 3, 4, 5, 6, None, None]

    ordered = order_bye_aware(slots, 8)

    assert [slot for slot in ordered if slot is not None] == [1, 2, 3, 4, 5, 6]
    assert ordered.count(None) == 2
    assert all(
        not (ordered[2 * pair] is None and ordered[2 * pair + 1] is None) for pair in range(4)
    )
    assert order_bye_aware(ordered, 8) == ordered


# Q. nothing to move: a full bracket and an empty bracket stay untouched
def test_order_bye_aware_is_a_no_op_without_byes() -> None:
    full: list[int | None] = [1, 2, 3, 4]
    empty: list[int | None] = [None, None, None, None]

    assert order_bye_aware(full, 4) == full
    assert order_bye_aware(empty, 4) == empty
