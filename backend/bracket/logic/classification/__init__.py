"""Clasificacion de competidores e inscripciones contra las categorias del torneo (S4.8a-2).

API publica del bloque. El motor es puro (``engine``) y sus datos son inmutables (``rules``); la
carga desde la base vive en ``bracket/sql/classification.py``.
"""

from bracket.logic.classification.engine import (
    age_on_date,
    check_chosen_category,
    check_override,
    classify,
    classify_input,
    classify_registration,
    evaluate_category,
)
from bracket.logic.classification.rules import (
    CATEGORY_KEY_PREFIX,
    BeltLevel,
    CategoryRules,
    CategoryVerdict,
    ChosenCategoryCheck,
    ClassificationInput,
    ClassificationResult,
    CompetitorFacts,
    OverrideCheck,
    RegistrationClassification,
    RegistrationFacts,
    category_key_for,
    stage_item_id_from_key,
)
from bracket.logic.classification.vocabulary import (
    ClassificationBlocker,
    ClassificationWarning,
    order_blockers,
    order_warnings,
)

__all__ = [
    "CATEGORY_KEY_PREFIX",
    "BeltLevel",
    "CategoryRules",
    "CategoryVerdict",
    "ChosenCategoryCheck",
    "ClassificationBlocker",
    "ClassificationInput",
    "ClassificationResult",
    "ClassificationWarning",
    "CompetitorFacts",
    "OverrideCheck",
    "RegistrationClassification",
    "RegistrationFacts",
    "age_on_date",
    "category_key_for",
    "check_chosen_category",
    "check_override",
    "classify",
    "classify_input",
    "classify_registration",
    "evaluate_category",
    "order_blockers",
    "order_warnings",
    "stage_item_id_from_key",
]
