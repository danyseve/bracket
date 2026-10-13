"""S4.8a-2 — motor de clasificacion: pruebas **puras** de dominio (sin base de datos).

TDD: escritas y ejecutadas antes de la implementacion (rojo con ``ModuleNotFoundError``).

Cubren edad (incluido el 29 de febrero), sexo, cinturon (rango, extremos abiertos, escala, rango
incoherente), peso (limites inclusivos), modalidad, reglas nulas del cuadro legacy, catalogos de
blockers y avisos, recomendacion determinista, readiness de dominio y validacion del override.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from bracket.logic.classification import (
    BeltLevel,
    CategoryRules,
    ClassificationBlocker,
    ClassificationWarning,
    CompetitorFacts,
    RegistrationFacts,
    age_on_date,
    category_key_for,
    check_override,
    classify,
    classify_registration,
    evaluate_category,
    stage_item_id_from_key,
)
from bracket.utils.id_types import StageItemId

#: Fecha del torneo con la que se calcula la edad en toda la bateria.
ON_DATE = date(2026, 10, 13)

ADULT_BLUE = BeltLevel(scope="ADULT", rank=2, code="BLUE")
ADULT_PURPLE = BeltLevel(scope="ADULT", rank=3, code="PURPLE")
ADULT_BLACK = BeltLevel(scope="ADULT", rank=5, code="BLACK")
ADULT_WHITE = BeltLevel(scope="ADULT", rank=1, code="WHITE")
JUVENILE_GREEN = BeltLevel(scope="JUVENILE", rank=5, code="GREEN")


def rules(stage_item_id: int = 1, **overrides: object) -> CategoryRules:
    """Categoria con solo las reglas que la prueba declara (el resto, sin restriccion)."""
    return CategoryRules(
        stage_item_id=StageItemId(stage_item_id),
        category_label=f"categoria-{stage_item_id}",
        **overrides,  # type: ignore[arg-type]
    )


def competitor(**overrides: object) -> CompetitorFacts:
    """Competidor sin datos declarados salvo los que indique la prueba."""
    return CompetitorFacts(**overrides)  # type: ignore[arg-type]


def registration(**overrides: object) -> RegistrationFacts:
    """Inscripcion confirmada y sin categoria elegida salvo que la prueba diga otra cosa."""
    values: dict[str, object] = {"status": "CONFIRMED"}
    values.update(overrides)
    return RegistrationFacts(**values)  # type: ignore[arg-type]


def verdict_of(category: CategoryRules, person: CompetitorFacts, entry: RegistrationFacts):
    """Veredicto de una sola categoria (atajo de la bateria)."""
    return evaluate_category(category, person, entry, ON_DATE)


# ---------------------------------------------------------------- edad


@pytest.mark.parametrize(
    ("date_of_birth", "expected"),
    [
        (date(2000, 1, 1), 26),
        (date(2000, 12, 31), 25),
        (date(2000, 10, 13), 26),
    ],
)
def test_age_is_years_completed_on_the_tournament_date(date_of_birth: date, expected: int) -> None:
    assert age_on_date(date_of_birth, ON_DATE) == expected


@pytest.mark.parametrize(
    ("on_date", "expected"),
    [
        (date(2023, 2, 28), 22),
        (date(2023, 3, 1), 23),
        (date(2024, 2, 28), 23),
        (date(2024, 2, 29), 24),
    ],
)
def test_leap_day_birthday_does_not_advance_to_february_28(on_date: date, expected: int) -> None:
    assert age_on_date(date(2000, 2, 29), on_date) == expected


def test_age_restriction_without_date_of_birth_is_missing_data() -> None:
    verdict = verdict_of(rules(age_min=18), competitor(), registration())
    assert verdict.compatible is False
    assert verdict.blockers == (ClassificationBlocker.MISSING_DATE_OF_BIRTH,)


def test_without_age_restriction_a_missing_date_of_birth_does_not_block() -> None:
    verdict = verdict_of(rules(), competitor(), registration())
    assert verdict.compatible is True
    assert verdict.blockers == ()


def test_age_below_the_minimum_is_incompatible() -> None:
    verdict = verdict_of(rules(age_min=30), competitor(date_of_birth=date(2000, 5, 5)), registration())
    assert verdict.blockers == (ClassificationBlocker.CATEGORY_INCOMPATIBLE,)


def test_age_above_the_maximum_is_incompatible() -> None:
    verdict = verdict_of(rules(age_max=30), competitor(date_of_birth=date(1990, 5, 5)), registration())
    assert verdict.blockers == (ClassificationBlocker.CATEGORY_INCOMPATIBLE,)


def test_age_limits_are_inclusive() -> None:
    born = date(1996, 10, 13)
    verdict = verdict_of(rules(age_min=30, age_max=30), competitor(date_of_birth=born), registration())
    assert verdict.compatible is True


# ---------------------------------------------------------------- sexo


@pytest.mark.parametrize(
    ("sex", "constraint", "expected"),
    [
        ("MALE", "MALE", ()),
        ("MALE", "MIXED", ()),
        ("FEMALE", "FEMALE", ()),
        ("FEMALE", "MIXED", ()),
        ("MALE", "FEMALE", (ClassificationBlocker.CATEGORY_INCOMPATIBLE,)),
        ("FEMALE", "MALE", (ClassificationBlocker.CATEGORY_INCOMPATIBLE,)),
        (None, "MALE", (ClassificationBlocker.MISSING_SEX,)),
        (None, "MIXED", ()),
        (None, None, ()),
        ("FEMALE", None, ()),
    ],
)
def test_sex_compatibility(sex, constraint, expected) -> None:
    verdict = verdict_of(rules(sex_constraint=constraint), competitor(sex=sex), registration())
    assert verdict.blockers == expected


# ---------------------------------------------------------------- cinturon


def test_belt_inside_the_range_is_compatible() -> None:
    verdict = verdict_of(
        rules(belt_min=ADULT_BLUE, belt_max=ADULT_PURPLE), competitor(belt=ADULT_PURPLE), registration()
    )
    assert verdict.compatible is True


@pytest.mark.parametrize(
    ("minimum", "maximum", "belt", "expected"),
    [
        (ADULT_BLUE, None, ADULT_BLACK, ()),
        (ADULT_WHITE, None, ADULT_WHITE, ()),
        (None, ADULT_BLUE, ADULT_WHITE, ()),
        (None, ADULT_BLUE, ADULT_PURPLE, (ClassificationBlocker.CATEGORY_INCOMPATIBLE,)),
        (ADULT_BLUE, None, ADULT_WHITE, (ClassificationBlocker.CATEGORY_INCOMPATIBLE,)),
        (ADULT_BLUE, ADULT_PURPLE, None, (ClassificationBlocker.MISSING_BELT,)),
        (ADULT_BLUE, ADULT_PURPLE, JUVENILE_GREEN, (ClassificationBlocker.CATEGORY_INCOMPATIBLE,)),
        (None, None, None, ()),
    ],
)
def test_belt_range_and_scope(minimum, maximum, belt, expected) -> None:
    verdict = verdict_of(
        rules(belt_min=minimum, belt_max=maximum), competitor(belt=belt), registration()
    )
    assert verdict.blockers == expected


def test_a_range_whose_minimum_is_above_its_maximum_is_invalid_configuration() -> None:
    verdict = verdict_of(
        rules(belt_min=ADULT_PURPLE, belt_max=ADULT_BLUE), competitor(belt=ADULT_BLUE), registration()
    )
    assert verdict.compatible is False
    assert verdict.blockers == (ClassificationBlocker.INVALID_CATEGORY_BELT_RANGE,)


def test_a_range_mixing_two_scales_is_invalid_configuration() -> None:
    verdict = verdict_of(
        rules(belt_min=JUVENILE_GREEN, belt_max=ADULT_PURPLE),
        competitor(belt=ADULT_PURPLE),
        registration(),
    )
    assert verdict.blockers == (ClassificationBlocker.INVALID_CATEGORY_BELT_RANGE,)


# ---------------------------------------------------------------- peso


@pytest.mark.parametrize(
    ("minimum", "maximum", "weight", "expected"),
    [
        (Decimal("71.00"), Decimal("77.00"), Decimal("71.00"), ()),
        (Decimal("71.00"), Decimal("77.00"), Decimal("77.00"), ()),
        (Decimal("71.00"), Decimal("77.00"), Decimal("74.50"), ()),
        (Decimal("71.00"), Decimal("77.00"), Decimal("70.99"), (ClassificationBlocker.CATEGORY_INCOMPATIBLE,)),
        (Decimal("71.00"), Decimal("77.00"), Decimal("77.01"), (ClassificationBlocker.CATEGORY_INCOMPATIBLE,)),
        (None, Decimal("71.00"), Decimal("60.00"), ()),
        (Decimal("71.00"), None, Decimal("120.00"), ()),
        (Decimal("71.00"), Decimal("77.00"), None, (ClassificationBlocker.MISSING_WEIGHT,)),
        (None, None, None, ()),
    ],
)
def test_weight_compatibility_with_inclusive_limits(minimum, maximum, weight, expected) -> None:
    verdict = verdict_of(
        rules(weight_min_kg=minimum, weight_max_kg=maximum),
        competitor(),
        registration(declared_weight_kg=weight),
    )
    assert verdict.blockers == expected


# ---------------------------------------------------------------- modalidad


@pytest.mark.parametrize(
    ("constraint", "modality", "expected"),
    [
        ("GI", "GI", ()),
        ("NO_GI", "NO_GI", ()),
        ("GI", "NO_GI", (ClassificationBlocker.CATEGORY_INCOMPATIBLE,)),
        ("NO_GI", "GI", (ClassificationBlocker.CATEGORY_INCOMPATIBLE,)),
        ("GI", None, (ClassificationBlocker.MISSING_MODALITY,)),
        (None, "NO_GI", ()),
        (None, None, ()),
    ],
)
def test_modality_compatibility(constraint, modality, expected) -> None:
    verdict = verdict_of(
        rules(modality=constraint), competitor(), registration(modality=modality)
    )
    assert verdict.blockers == expected


# ---------------------------------------------------------------- combinado


def complete_category(stage_item_id: int = 1) -> CategoryRules:
    """Categoria con **todas** las reglas declaradas."""
    return rules(
        stage_item_id,
        sex_constraint="MALE",
        belt_min=ADULT_BLUE,
        belt_max=ADULT_PURPLE,
        age_min=18,
        age_max=39,
        weight_min_kg=Decimal("71.00"),
        weight_max_kg=Decimal("77.00"),
        modality="GI",
    )


def complete_competitor() -> CompetitorFacts:
    """Competidor que cumple todas las reglas de ``complete_category``."""
    return competitor(date_of_birth=date(1995, 5, 5), sex="MALE", belt=ADULT_BLUE)


def complete_registration() -> RegistrationFacts:
    """Inscripcion que cumple todas las reglas de ``complete_category``."""
    return registration(declared_weight_kg=Decimal("74.50"), modality="GI")


def test_every_rule_holding_makes_the_category_compatible() -> None:
    result = classify((complete_category(),), complete_competitor(), complete_registration(), ON_DATE)
    assert result.compatible_categories == result.verdicts
    assert result.blockers == ()
    assert result.recommended_category.category_label == "categoria-1"


def test_one_single_failing_rule_blocks_the_category() -> None:
    person = complete_competitor()
    entry = registration(declared_weight_kg=Decimal("78.50"), modality="GI")
    result = classify((complete_category(),), person, entry, ON_DATE)
    assert result.compatible_categories == ()
    assert result.recommended_category is None
    assert result.blockers == (
        ClassificationBlocker.CATEGORY_INCOMPATIBLE,
        ClassificationBlocker.NO_COMPATIBLE_CATEGORY,
    )


def test_several_compatible_categories_are_all_returned_without_recommendation() -> None:
    result = classify(
        (complete_category(1), complete_category(2)), complete_competitor(), complete_registration(), ON_DATE
    )
    assert len(result.compatible_categories) == 2
    assert result.recommended_category is None
    assert result.warnings == (ClassificationWarning.MULTIPLE_COMPATIBLE_CATEGORIES,)
    assert result.blockers == ()


def test_no_compatible_category_is_reported_with_its_reasons() -> None:
    result = classify(
        (rules(1, modality="NO_GI"), rules(2, weight_max_kg=Decimal("66.00"))),
        complete_competitor(),
        complete_registration(),
        ON_DATE,
    )
    assert result.compatible_categories == ()
    assert result.blockers == (
        ClassificationBlocker.CATEGORY_INCOMPATIBLE,
        ClassificationBlocker.NO_COMPATIBLE_CATEGORY,
    )


def test_exactly_one_compatible_category_is_recommended() -> None:
    result = classify(
        (rules(1, sex_constraint="FEMALE"), complete_category(2)),
        complete_competitor(),
        complete_registration(),
        ON_DATE,
    )
    assert len(result.compatible_categories) == 1
    assert result.recommended_category.category_key == "si:2"


# ---------------------------------------------------------------- reglas nulas (legacy)


def test_a_legacy_category_without_rules_still_accepts_any_registration() -> None:
    result = classify((rules(1),), candidate_without_data(), registration(), ON_DATE)
    assert result.compatible_categories[0].compatible is True
    assert result.warnings == (ClassificationWarning.CATEGORY_RULES_PARTIAL,)


def candidate_without_data() -> CompetitorFacts:
    """Competidor sin ningun dato declarado (el caso de los competidores existentes)."""
    return competitor()


# ---------------------------------------------------------------- catalogos


def test_the_blocker_vocabulary_is_closed_and_ordered() -> None:
    assert tuple(blocker.value for blocker in ClassificationBlocker) == (
        "MISSING_DATE_OF_BIRTH",
        "MISSING_SEX",
        "MISSING_BELT",
        "MISSING_WEIGHT",
        "MISSING_MODALITY",
        "CATEGORY_NOT_RESOLVED",
        "CATEGORY_INCOMPATIBLE",
        "NO_COMPATIBLE_CATEGORY",
        "INVALID_CATEGORY_BELT_RANGE",
    )
    assert tuple(warning.value for warning in ClassificationWarning) == (
        "CATEGORY_RULES_PARTIAL",
        "MULTIPLE_COMPATIBLE_CATEGORIES",
    )


def test_the_category_key_contract_is_reversible() -> None:
    assert category_key_for(StageItemId(7)) == "si:7"
    assert stage_item_id_from_key("si:7") == 7
    assert stage_item_id_from_key("ADULT_BLUE") is None
    assert stage_item_id_from_key("si:x") is None


# ---------------------------------------------------------------- readiness de dominio


def test_a_confirmed_registration_with_a_resolved_category_is_countable() -> None:
    assessment = classify_registration(
        (complete_category(),),
        complete_competitor(),
        registration(declared_weight_kg=Decimal("74.50"), modality="GI", category_key="si:1"),
        ON_DATE,
    )
    assert assessment.ok is True
    assert assessment.countable is True
    assert assessment.chosen.blockers == ()
    assert assessment.blockers == ()


def test_a_confirmed_registration_without_the_required_weight_does_not_count() -> None:
    assessment = classify_registration(
        (complete_category(),),
        complete_competitor(),
        registration(modality="GI", category_key="si:1"),
        ON_DATE,
    )
    assert assessment.ok is False
    assert assessment.countable is False
    assert assessment.blockers == (
        ClassificationBlocker.MISSING_WEIGHT,
        ClassificationBlocker.NO_COMPATIBLE_CATEGORY,
    )
    assert ClassificationBlocker.CATEGORY_INCOMPATIBLE not in assessment.blockers


def test_a_confirmed_registration_with_an_incompatible_category_does_not_count() -> None:
    assessment = classify_registration(
        (rules(1, sex_constraint="FEMALE"),),
        complete_competitor(),
        registration(category_key="si:1"),
        ON_DATE,
    )
    assert assessment.countable is False
    assert assessment.chosen.blockers == (ClassificationBlocker.CATEGORY_INCOMPATIBLE,)


def test_a_confirmed_legacy_registration_keeps_counting() -> None:
    assessment = classify_registration((rules(1),), competitor(), registration(category_key="si:1"), ON_DATE)
    assert assessment.countable is True


def test_a_confirmed_registration_without_a_resolved_category_does_not_count() -> None:
    assessment = classify_registration(
        (complete_category(),), complete_competitor(), complete_registration(), ON_DATE
    )
    assert assessment.ok is False
    assert assessment.countable is False
    assert assessment.chosen.blockers == (ClassificationBlocker.CATEGORY_NOT_RESOLVED,)


def test_a_category_key_from_another_tournament_does_not_resolve() -> None:
    assessment = classify_registration(
        (complete_category(),),
        complete_competitor(),
        registration(declared_weight_kg=Decimal("74.50"), modality="GI", category_key="si:99"),
        ON_DATE,
    )
    assert assessment.chosen.resolved is False
    assert assessment.chosen.blockers == (ClassificationBlocker.CATEGORY_NOT_RESOLVED,)


@pytest.mark.parametrize("status", ["WITHDRAWN", "DISQUALIFIED", "DRAFT"])
def test_a_registration_that_is_not_confirmed_does_not_count(status: str) -> None:
    assessment = classify_registration(
        (complete_category(),),
        complete_competitor(),
        registration(declared_weight_kg=Decimal("74.50"), modality="GI", category_key="si:1", status=status),
        ON_DATE,
    )
    assert assessment.ok is True
    assert assessment.countable is False


def test_a_superseded_registration_does_not_count() -> None:
    assessment = classify_registration(
        (complete_category(),),
        complete_competitor(),
        registration(
            declared_weight_kg=Decimal("74.50"), modality="GI", category_key="si:1", superseded=True
        ),
        ON_DATE,
    )
    assert assessment.countable is False


# ---------------------------------------------------------------- override


def test_the_recommended_category_is_not_a_deviation() -> None:
    over = check_override(
        (rules(1, sex_constraint="FEMALE"), complete_category(2)),
        complete_competitor(),
        registration(declared_weight_kg=Decimal("74.50"), modality="GI", category_key="si:2"),
        ON_DATE,
    )
    assert over.recommended_category.category_key == "si:2"
    assert over.compatible is True
    assert over.deviation_from_recommendation is False


def test_choosing_another_compatible_category_is_not_a_deviation_without_recommendation() -> None:
    over = check_override(
        (complete_category(1), complete_category(2)),
        complete_competitor(),
        registration(declared_weight_kg=Decimal("74.50"), modality="GI", category_key="si:2"),
        ON_DATE,
    )
    assert over.recommended_category is None
    assert over.compatible is True
    assert over.deviation_from_recommendation is False


def test_choosing_a_category_other_than_the_recommendation_is_a_deviation() -> None:
    over = check_override(
        (complete_category(1), rules(2, modality="NO_GI")),
        complete_competitor(),
        registration(declared_weight_kg=Decimal("74.50"), modality="GI", category_key="si:2"),
        ON_DATE,
    )
    assert over.recommended_category.category_key == "si:1"
    assert over.chosen.resolved is True
    assert over.compatible is False
    assert over.deviation_from_recommendation is True


def test_an_unresolved_choice_is_not_a_deviation() -> None:
    over = check_override(
        (complete_category(1),),
        complete_competitor(),
        registration(declared_weight_kg=Decimal("74.50"), modality="GI", category_key="si:99"),
        ON_DATE,
    )
    assert over.recommended_category.category_key == "si:1"
    assert over.chosen.resolved is False
    assert over.deviation_from_recommendation is False


def test_choosing_an_incompatible_category_is_not_compatible() -> None:
    over = check_override(
        (complete_category(1),),
        complete_competitor(),
        registration(modality="NO_GI", category_key="si:1"),
        ON_DATE,
    )
    assert over.compatible is False
    assert ClassificationBlocker.CATEGORY_INCOMPATIBLE in over.blockers


def test_the_club_representation_does_not_change_the_sporting_verdict() -> None:
    independent = classify(
        (complete_category(),),
        complete_competitor(),
        registration(declared_weight_kg=Decimal("74.50"), modality="GI", representation="INDEPENDENT"),
        ON_DATE,
    )
    club = classify(
        (complete_category(),),
        complete_competitor(),
        registration(declared_weight_kg=Decimal("74.50"), modality="GI", representation="CLUB"),
        ON_DATE,
    )
    assert independent.compatible_categories == club.compatible_categories
    assert independent.blockers == club.blockers == ()
