"""Bye-aware distribution of entrants over the slots of a single elimination bracket.

A single elimination bracket is played on ``bracket_size`` slots that are paired two by two in the
first round. If the entrants are pushed to the front of the slot list and the empty slots are left
at the back, the last first-round matches pair two empty slots: ghost matches, which nobody can
ever play and which would still take a court and a time slot away from a real fight.

The functions below spread the empty slots over the whole first round instead, so that every bye
is matched against a real entrant whenever that is mathematically possible
(``bracket_size <= 2 * entrant_count``). They are pure, deterministic and depend only on the number
of entrants and the bracket size, never on the order in which rows happen to come out of the
database.
"""

from __future__ import annotations

from collections.abc import Sequence


def bracket_size_for_entrant_count(entrant_count: int) -> int | None:
    """
    Smallest power of two that can hold ``entrant_count`` entrants.

    This is the bracket size invariant: 2 -> 2, 3 and 4 -> 4, 5 to 8 -> 8, 9 to 16 -> 16. A
    single elimination bracket is never oversized beyond that power of two. ``None`` means that no
    valid single elimination bracket can be built (fewer than two entrants).
    """
    if entrant_count < 2:
        return None
    size = 2
    while size < entrant_count:
        size *= 2
    return size


def bye_aware_bye_pairs(entrant_count: int, bracket_size: int) -> list[int]:
    """
    First-round pairs (zero based) that receive exactly one entrant, i.e. one bye.

    With ``pair_count = bracket_size // 2`` first-round pairs and ``byes = bracket_size -
    entrant_count`` empty slots, the pairs are picked at ``(2 * i + 1) * pair_count // (2 * byes)``.
    Consecutive picks differ by at least ``pair_count / byes >= 1``, so the picks are distinct and
    sorted: the byes are spread over the whole round instead of being clustered at the end.
    """
    pair_count = bracket_size // 2
    byes = bracket_size - entrant_count
    if byes == 0:
        return []
    return [(2 * index + 1) * pair_count // (2 * byes) for index in range(byes)]


def distribute_entrants_into_slots[T](entrants: Sequence[T], bracket_size: int) -> list[T | None]:
    """
    Place ``entrants`` into the ``bracket_size`` first-round slots, spreading the byes out.

    Returns a list of length ``bracket_size`` where index ``i`` is slot ``i + 1``; the positions
    that stay empty are ``None``. Entrants keep their relative order.

    Exactly ``len(entrants)`` positions are occupied and ``bracket_size - len(entrants)`` are empty,
    and no first-round pair is empty twice as long as ``bracket_size <= 2 * len(entrants)``.
    """
    entrant_count = len(entrants)
    if entrant_count == 0:
        return [None] * bracket_size
    if bracket_size < 2 or bracket_size % 2 != 0:
        raise ValueError(f"Bracket size must be at least 2 and even, got {bracket_size}")
    if entrant_count > bracket_size:
        raise ValueError(f"{entrant_count} entrants do not fit in a bracket of {bracket_size}")
    if bracket_size > 2 * entrant_count:
        raise ValueError(
            f"A bracket of {bracket_size} would need more byes than first-round pairs for "
            f"{entrant_count} entrants; use the smallest power of two instead"
        )

    bye_pairs = set(bye_aware_bye_pairs(entrant_count, bracket_size))
    slots: list[T | None] = [None] * bracket_size
    entrant_index = 0

    for pair_index in range(bracket_size // 2):
        first_slot = 2 * pair_index
        slots[first_slot] = entrants[entrant_index]
        entrant_index += 1
        if pair_index not in bye_pairs:
            slots[first_slot + 1] = entrants[entrant_index]
            entrant_index += 1

    assert entrant_index == entrant_count
    return slots


def order_bye_aware[T](slots: Sequence[T | None], bracket_size: int) -> list[T | None]:
    """
    Reorder an already built slot list so that its empty positions are spread bye-aware.

    The same objects are returned, only their position changes: non-empty entries keep their
    relative order, and so do the empty ones. It is a no-op for a full bracket, for an empty one,
    and for a bracket that is already distributed, which makes it safe to run before building the
    first round.
    """
    occupied = [slot for slot in slots if slot is not None]
    if len(occupied) == 0 or len(occupied) == len(slots):
        return list(slots)

    distributed = distribute_entrants_into_slots(occupied, bracket_size)
    empties = (slot for slot in slots if slot is None)
    return [slot if slot is not None else next(empties) for slot in distributed]
