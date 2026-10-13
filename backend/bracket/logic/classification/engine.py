"""Motor **puro** de clasificacion de competidores e inscripciones (S4.8a-2, LAB).

Responde a una sola pregunta por vez y sin efectos: dadas las reglas de una categoria y los datos de
una inscripcion, si esa categoria admite a ese competidor. No escribe, no audita, no resuelve HTTP y
**no modifica** la categoria (una regla incoherente se reporta, no se corrige).

Regla de oro del bloque: no se confunde "el dato no esta" con "el dato no encaja".

* ``MISSING_*`` significa que la categoria **exige** un dato que no existe todavia;
* ``CATEGORY_INCOMPATIBLE`` significa que el dato existe y **no** cumple la restriccion;
* una restriccion ``NULL`` no exige nada: no se pide un dato que la categoria no restringe, de modo
  que los cuadros legacy (sin reglas) siguen admitiendo exactamente lo que admitian.

La edad no se persiste: se calcula contra la fecha del torneo (``age_on_date``).
"""

from __future__ import annotations

from datetime import date

from bracket.logic.classification.rules import (
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
)
from bracket.logic.classification.vocabulary import (
    ClassificationBlocker,
    ClassificationWarning,
    order_blockers,
    order_warnings,
)

_MALE = "MALE"
_FEMALE = "FEMALE"


def age_on_date(date_of_birth: date, on_date: date) -> int:
    """Anios **cumplidos** en ``on_date``.

    El aniversario se compara por (mes, dia): el mismo dia del cumpleanos ya cuenta, y el dia
    anterior no. Quien nace un 29 de febrero no adelanta el aniversario al 28 en los anios no
    bisiestos: lo cumple el 1 de marzo (convencion de la aritmetica de fechas habitual).
    """
    years = on_date.year - date_of_birth.year
    if (on_date.month, on_date.day) < (date_of_birth.month, date_of_birth.day):
        years -= 1
    return years


def _belt_blocker(rules: CategoryRules, competitor: CompetitorFacts) -> ClassificationBlocker | None:
    """Motivo por el que el cinturon no encaja, o ``None`` si encaja o no hay restriccion."""
    if rules.belt_min is None and rules.belt_max is None:
        return None
    belt: BeltLevel | None = competitor.belt
    if belt is None:
        return ClassificationBlocker.MISSING_BELT
    if rules.belt_scope is not None and belt.scope != rules.belt_scope:
        return ClassificationBlocker.CATEGORY_INCOMPATIBLE
    if rules.belt_min is not None and belt.rank < rules.belt_min.rank:
        return ClassificationBlocker.CATEGORY_INCOMPATIBLE
    if rules.belt_max is not None and belt.rank > rules.belt_max.rank:
        return ClassificationBlocker.CATEGORY_INCOMPATIBLE
    return None


def _sex_blocker(rules: CategoryRules, competitor: CompetitorFacts) -> ClassificationBlocker | None:
    """Motivo por el que el sexo no encaja.

    ``MIXED`` no restringe: una categoria mixta admite cualquier sexo y tambien el desconocido, de
    modo que no exige un dato que no necesita.
    """
    constraint = rules.sex_constraint
    if constraint not in (_MALE, _FEMALE):
        return None
    if competitor.sex is None:
        return ClassificationBlocker.MISSING_SEX
    if competitor.sex != constraint:
        return ClassificationBlocker.CATEGORY_INCOMPATIBLE
    return None


def _age_blocker(
    rules: CategoryRules, competitor: CompetitorFacts, on_date: date
) -> ClassificationBlocker | None:
    """Motivo por el que la edad no encaja (o falta la fecha de nacimiento)."""
    if rules.age_min is None and rules.age_max is None:
        return None
    if competitor.date_of_birth is None:
        return ClassificationBlocker.MISSING_DATE_OF_BIRTH
    age = age_on_date(competitor.date_of_birth, on_date)
    if rules.age_min is not None and age < rules.age_min:
        return ClassificationBlocker.CATEGORY_INCOMPATIBLE
    if rules.age_max is not None and age > rules.age_max:
        return ClassificationBlocker.CATEGORY_INCOMPATIBLE
    return None


def _weight_blocker(
    rules: CategoryRules, registration: RegistrationFacts
) -> ClassificationBlocker | None:
    """Motivo por el que el peso declarado no encaja (limites **inclusivos**)."""
    if rules.weight_min_kg is None and rules.weight_max_kg is None:
        return None
    weight = registration.declared_weight_kg
    if weight is None:
        return ClassificationBlocker.MISSING_WEIGHT
    if rules.weight_min_kg is not None and weight < rules.weight_min_kg:
        return ClassificationBlocker.CATEGORY_INCOMPATIBLE
    if rules.weight_max_kg is not None and weight > rules.weight_max_kg:
        return ClassificationBlocker.CATEGORY_INCOMPATIBLE
    return None


def _modality_blocker(
    rules: CategoryRules, registration: RegistrationFacts
) -> ClassificationBlocker | None:
    """Motivo por el que la modalidad no encaja (``GI`` solo con ``GI``)."""
    if rules.modality is None:
        return None
    if registration.modality is None:
        return ClassificationBlocker.MISSING_MODALITY
    if registration.modality != rules.modality:
        return ClassificationBlocker.CATEGORY_INCOMPATIBLE
    return None


def evaluate_category(
    rules: CategoryRules,
    competitor: CompetitorFacts,
    registration: RegistrationFacts,
    on_date: date,
) -> CategoryVerdict:
    """Admite, o no, esta categoria a esta inscripcion: veredicto con sus motivos, sin corregir."""
    blockers: list[ClassificationBlocker] = []
    warnings: list[ClassificationWarning] = []
    if not rules.belt_range_is_valid:
        blockers.append(ClassificationBlocker.INVALID_CATEGORY_BELT_RANGE)
    else:
        for blocker in (
            _sex_blocker(rules, competitor),
            _age_blocker(rules, competitor, on_date),
            _belt_blocker(rules, competitor),
            _weight_blocker(rules, registration),
            _modality_blocker(rules, registration),
        ):
            if blocker is not None:
                blockers.append(blocker)
    if not rules.has_any_rule:
        warnings.append(ClassificationWarning.CATEGORY_RULES_PARTIAL)
    ordered = order_blockers(blockers)
    return CategoryVerdict(
        category_key=rules.category_key,
        category_label=rules.category_label,
        compatible=not ordered,
        blockers=ordered,
        warnings=order_warnings(warnings),
    )


def classify(
    categories: tuple[CategoryRules, ...],
    competitor: CompetitorFacts,
    registration: RegistrationFacts,
    on_date: date,
) -> ClassificationResult:
    """Clasifica una inscripcion contra las categorias del torneo, en el orden recibido.

    La recomendacion **no se inventa**: con una sola compatible es esa; con varias no hay
    recomendacion (se devuelven todas y se avisa) y con ninguna tampoco. No se ordena por id para
    elegir, y no se elige "la primera".
    """
    verdicts = tuple(
        evaluate_category(rules, competitor, registration, on_date) for rules in categories
    )
    compatible = tuple(verdict for verdict in verdicts if verdict.compatible)
    recommended = compatible[0] if len(compatible) == 1 else None
    warnings: list[ClassificationWarning] = []
    if len(compatible) > 1:
        warnings.append(ClassificationWarning.MULTIPLE_COMPATIBLE_CATEGORIES)
    if any(ClassificationWarning.CATEGORY_RULES_PARTIAL in v.warnings for v in verdicts):
        warnings.append(ClassificationWarning.CATEGORY_RULES_PARTIAL)
    blockers: list[ClassificationBlocker] = []
    if not compatible:
        blockers.append(ClassificationBlocker.NO_COMPATIBLE_CATEGORY)
        for verdict in verdicts:
            blockers.extend(verdict.blockers)
    return ClassificationResult(
        verdicts=verdicts,
        compatible_categories=compatible,
        recommended_category=recommended,
        blockers=order_blockers(blockers),
        warnings=order_warnings(warnings),
    )


def check_chosen_category(
    category_key: str | None, result: ClassificationResult
) -> ChosenCategoryCheck:
    """Comprueba la categoria **elegida**: resuelta contra el torneo y compatible.

    No resuelta (sin clave, clave de otro torneo o clave que no existe) es
    ``CATEGORY_NOT_RESOLVED``; resuelta pero no admitida es ``CATEGORY_INCOMPATIBLE``. El veredicto
    de la categoria viaja entero, de modo que un dato que falta sigue leyendose como ``MISSING_*``.
    """
    if category_key is None:
        return ChosenCategoryCheck(
            category_key=None,
            resolved=False,
            compatible=False,
            blockers=order_blockers([ClassificationBlocker.CATEGORY_NOT_RESOLVED]),
        )
    for verdict in result.verdicts:
        if verdict.category_key == category_key:
            return ChosenCategoryCheck(
                category_key=category_key,
                resolved=True,
                compatible=verdict.compatible,
                blockers=verdict.blockers,
                verdict=verdict,
            )
    return ChosenCategoryCheck(
        category_key=category_key,
        resolved=False,
        compatible=False,
        blockers=order_blockers([ClassificationBlocker.CATEGORY_NOT_RESOLVED]),
    )


def classify_registration(
    categories: tuple[CategoryRules, ...],
    competitor: CompetitorFacts,
    registration: RegistrationFacts,
    on_date: date,
) -> RegistrationClassification:
    """Readiness de dominio: decide si esta inscripcion cuenta como participante clasificable (§13).

    ``countable`` es lo que necesita el readiness: vigente, **confirmada** y con una categoria
    elegida, resuelta y compatible. Una confirmada con bloqueos no cuenta, y no se toca ninguna otra
    regla del readiness (ventana, minimo por categoria, cuadro ya generado).
    """
    result = classify(categories, competitor, registration, on_date)
    chosen = check_chosen_category(registration.category_key, result)
    return RegistrationClassification(
        ok=chosen.compatible,
        countable=chosen.compatible and registration.is_current and registration.is_confirmed,
        chosen=chosen,
        result=result,
    )


def classify_input(data: ClassificationInput) -> RegistrationClassification:
    """Clasifica una entrada ya cargada (torneo + competidor + inscripcion + categorias)."""
    return classify_registration(
        data.categories, data.competitor, data.registration, data.start_time.date()
    )


def check_override(
    categories: tuple[CategoryRules, ...],
    competitor: CompetitorFacts,
    registration: RegistrationFacts,
    on_date: date,
) -> OverrideCheck:
    """Validacion de dominio del override (§14): recomendada, elegida y desvio. Sin RBAC.

    El desvio se mide contra una recomendacion **existente**: cuando hay varias categorias
    compatibles (y por tanto ninguna recomendacion determinista) elegir cualquiera de ellas no es
    desviarse de nada. Quien exige ``OWNER`` + motivo, y quien audita, es a-3.
    """
    assessment = classify_registration(categories, competitor, registration, on_date)
    recommended = assessment.result.recommended_category
    deviation = (
        recommended is not None
        and assessment.chosen.resolved
        and assessment.chosen.category_key != recommended.category_key
    )
    return OverrideCheck(
        recommended_category=recommended,
        chosen=assessment.chosen,
        compatible=assessment.ok,
        deviation_from_recommendation=deviation,
        blockers=assessment.blockers,
        warnings=assessment.warnings,
    )
