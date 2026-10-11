"""Reparto de responsabilidades de la generacion explicita del cuadro (S3.5a, F3B, LAB ONLY).

Sin base de datos y sin HTTP: lo que se fija aqui es que la generacion **no** reimplementa nada de
lo certificado. Que cuadro se puede generar lo dice el bloqueo de F2C, el reparto lo dice su
planificador, el minimo de participantes es su unico minimo y la revision es entera la de S3.4d. Si
alguien duplicara una de esas reglas aqui, estos tests lo dejan en evidencia antes que la
suite larga.

Los constructores de cuadros son los del test certificado de F2C (``tests.unit_tests.
generate_bracket_test``): fabricar otros seria probar contra una estructura distinta de la que la
generacion recibe de verdad.
"""

from __future__ import annotations

import pathlib

from bracket.logic.bracket_generation import (
    GeneratedBracket,
    TournamentBracketGeneration,
    generatable_stage_items,
    plan_generatable_brackets,
)
from bracket.logic.scheduling.generation import (
    MINIMUM_ENTRANTS,
    get_bracket_generation_blocker,
    plan_bracket_generation,
)
from bracket.models.db.util import StageItemWithRounds
from bracket.utils.id_types import StageItemId, TournamentId
from tests.unit_tests.generate_bracket_test import make_generated_stage_item, make_stage_item


def _with_id(stage_item: StageItemWithRounds, stage_item_id: int) -> StageItemWithRounds:
    """El constructor de F2C usa siempre el mismo identificador; el orden necesita varios."""
    return stage_item.model_copy(update={"id": StageItemId(stage_item_id)})


def test_generatable_stage_items_is_the_single_filter() -> None:
    """Leer y escribir comparten el mismo filtro: no hay dos listas de "que se puede generar"."""
    stage_items = [_with_id(make_stage_item(4, 1), 1), _with_id(make_stage_item(4, 2), 2)]

    generatable, skipped = generatable_stage_items(stage_items)

    assert [stage_item.id for stage_item in generatable] == [StageItemId(2)]
    assert skipped == 1
    assert plan_generatable_brackets(stage_items)[0][0].stage_item_id == StageItemId(2)


def test_plan_generatable_brackets_keeps_the_read_order() -> None:
    """El orden es el de la lectura, no el del planificador: dos llamadas escriben lo mismo."""
    first = _with_id(make_stage_item(4, 2), 3)
    second = _with_id(make_stage_item(8, 8), 5)

    brackets, skipped = plan_generatable_brackets([first, second])

    assert [bracket.stage_item_id for bracket in brackets] == [StageItemId(3), StageItemId(5)]
    assert skipped == 0


def test_plan_generatable_brackets_reuses_f2c_blockers() -> None:
    """Un cuadro bloqueado por F2C no se toca: no se inventa un bloqueo ni se fuerza el plan."""
    too_few = _with_id(make_stage_item(4, 1), 1)
    tentative = _with_id(make_stage_item(4, 2, tentative_slots=2), 2)
    playable = _with_id(make_stage_item(4, 2), 3)

    brackets, skipped = plan_generatable_brackets([too_few, tentative, playable])

    assert [bracket.stage_item_id for bracket in brackets] == [StageItemId(3)]
    assert skipped == 2
    assert get_bracket_generation_blocker(too_few) is not None
    assert get_bracket_generation_blocker(tentative) is not None
    assert get_bracket_generation_blocker(playable) is None


def test_plan_generatable_brackets_uses_the_certified_planner() -> None:
    """El plan es el de F2C, campo a campo: no hay una segunda version del reparto."""
    stage_item = _with_id(make_stage_item(4, 2), 1)

    brackets, _ = plan_generatable_brackets([stage_item])
    (bracket,) = brackets

    assert bracket.plan == plan_bracket_generation(stage_item)
    assert bracket.plan.entrant_count == 2
    assert bracket.plan.bracket_size == 4
    assert bracket.plan.bye_count == 2
    assert bracket.plan.changed is True


def test_plan_generatable_brackets_reports_an_already_generated_bracket_as_unchanged() -> None:
    """Repetir la generacion es un no-op y no hay que inventar una prohibicion para saberlo."""
    stage_item = _with_id(make_generated_stage_item(8, 6), 1)

    brackets, skipped = plan_generatable_brackets([stage_item])
    (bracket,) = brackets

    assert bracket.plan.changed is False
    assert bracket.plan.entrant_count == 6
    assert skipped == 0


def test_generation_flags_separate_the_tournament_state_from_this_call() -> None:
    """``generated`` es la post-condicion del torneo; ``changed`` es lo que hizo esta llamada."""
    stage_item = _with_id(make_generated_stage_item(8, 6), 1)
    brackets, _ = plan_generatable_brackets([stage_item])

    generation = TournamentBracketGeneration(
        tournament_id=TournamentId(1), brackets=tuple(brackets), skipped_stage_items=0
    )

    assert generation.generated is True
    assert generation.changed is False


def test_generation_at_the_minimum_is_reported_as_generated() -> None:
    """El minimo es el de F2C: justo en el limite, el cuadro cuenta como generado."""
    plan = plan_bracket_generation(make_generated_stage_item(4, MINIMUM_ENTRANTS))
    generation = TournamentBracketGeneration(
        tournament_id=TournamentId(1),
        brackets=(GeneratedBracket(stage_item_id=StageItemId(1), plan=plan),),
        skipped_stage_items=0,
    )

    assert MINIMUM_ENTRANTS == 2
    assert generation.generated is True


def test_generation_does_not_define_the_minimum_or_the_planning_algorithms() -> None:
    """Guardia: ni el minimo ni el planificador ni el recuento se definen en la generacion."""
    import bracket.logic.bracket_generation as module  # pylint: disable=import-outside-toplevel

    source = pathlib.Path(module.__file__).read_text()

    assert "MINIMUM_ENTRANTS = " not in source
    assert "def plan_bracket_generation" not in source
    assert "def count_entrants" not in source
    assert "def distribute_entrants_into_slots" not in source
