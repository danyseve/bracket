"""S3.4d - reglas y determinismo de la revision de readiness, sin base de datos.

TDD: este modulo se escribio **antes** de la implementacion y fallo en rojo (el modulo
``bracket.logic.bracket_readiness`` y su vocabulario no existian).

La revision es una **funcion pura**: la base de datos no decide nada, solo aporta filas. Aqui se
fija la tabla de decision y el determinismo (mismo estado -> misma respuesta, en cualquier orden de
entrada y sin depender del orden del SQL), que es lo que la ruta y el cliente pueden consumir.
"""

from __future__ import annotations

from typing import Final

import pytest

from bracket.logic.bracket_readiness import (
    BracketReadiness,
    BracketReadinessBlocker,
    ExistingBracket,
    RegistrationReadinessRow,
    evaluate_bracket_readiness,
)
from bracket.logic.scheduling.generation import BracketGenerationBlocker
from bracket.models.db.domain import (
    RegistrationRepresentation,
    RegistrationStatus,
    RegistrationWindowState,
)
from bracket.utils.id_types import (
    ClubId,
    CompetitorId,
    CompetitorXSportsClubId,
    SportsClubId,
    StageItemId,
    TournamentId,
    TournamentRegistrationId,
)

TENANT: Final = ClubId(1)
OTHER_TENANT: Final = ClubId(2)
TOURNAMENT: Final = TournamentId(7)
#: Minimo de participantes de la generacion de cuadro de F2C: menos de dos no es un cuadro.
MINIMUM: Final = 2


def row(  # un constructor por palabra clave, sin posicionales
    registration_id: int,
    *,
    status: RegistrationStatus = "CONFIRMED",
    category_key: str | None = "gi-peso-83",
    category_label: str | None = "Gi -83 kg",
    representation: RegistrationRepresentation = "INDEPENDENT",
    competitor_id: int | None = 100,
    competitor_active: bool | None = True,
    competitor_tenant_club_id: int | None = 1,
    sports_club_id: int | None = None,
    affiliation_id: int | None = None,
    affiliation_competitor_id: int | None = None,
    affiliation_sports_club_id: int | None = None,
    superseded: bool = False,
    classification_blockers: tuple[BracketReadinessBlocker, ...] = (),
    overridden: bool = False,
) -> RegistrationReadinessRow:
    """Fila del torneo con los valores por defecto de una confirmacion valida e independiente."""
    return RegistrationReadinessRow(
        registration_id=TournamentRegistrationId(registration_id),
        status=status,
        superseded=superseded,
        category_key=category_key,
        category_label=category_label,
        representation=representation,
        competitor_id=None if competitor_id is None else CompetitorId(competitor_id),
        competitor_active=competitor_active,
        competitor_tenant_club_id=(
            None if competitor_tenant_club_id is None else ClubId(competitor_tenant_club_id)
        ),
        sports_club_id=None if sports_club_id is None else SportsClubId(sports_club_id),
        affiliation_id=(
            None if affiliation_id is None else CompetitorXSportsClubId(affiliation_id)
        ),
        affiliation_competitor_id=(
            None if affiliation_competitor_id is None else CompetitorId(affiliation_competitor_id)
        ),
        affiliation_sports_club_id=(
            None if affiliation_sports_club_id is None else SportsClubId(affiliation_sports_club_id)
        ),
        classification_blockers=classification_blockers,
        overridden=overridden,
    )


def review(
    rows: list[RegistrationReadinessRow],
    *,
    window: RegistrationWindowState = "CLOSED",
    brackets: list[ExistingBracket] | None = None,
) -> BracketReadiness:
    """Revision con el tenant y el torneo del laboratorio."""
    return evaluate_bracket_readiness(
        tournament_id=TOURNAMENT,
        tenant_club_id=TENANT,
        window_state=window,
        rows=rows,
        existing_brackets=brackets or [],
    )


def codes(issues) -> list[str]:  # type: ignore[no-untyped-def]
    return [issue.code.value for issue in issues]


def bracket(
    stage_item_id: int,
    *,
    entrant_count: int = 4,
    blocker: BracketGenerationBlocker | None = None,
) -> ExistingBracket:
    return ExistingBracket(
        stage_item_id=StageItemId(stage_item_id),
        entrant_count=entrant_count,
        generator_blocker=blocker,
    )


# --- ventana -----------------------------------------------------------------------------------


def test_open_window_blocks_the_review() -> None:
    """Con altas todavia abiertas no hay nada que revisar: el censo se moveria bajo los pies."""
    result = review([row(1), row(2, competitor_id=101)], window="OPEN")

    assert result.ready is False
    assert result.window_state == "OPEN"
    assert codes(result.issues) == [BracketReadinessBlocker.REGISTRATION_WINDOW_OPEN.value]
    assert result.issues[0].count is None


@pytest.mark.parametrize("window", ["CLOSED", "DEADLINE_PASSED"])
def test_closed_window_does_not_block_but_is_reported(window: RegistrationWindowState) -> None:
    """El plazo vencido basta: el cierre manual no es un requisito de la revision."""
    rows = [
        row(1),
        row(2, competitor_id=101),
        row(3, category_key="nogi-absoluto", competitor_id=102),
        row(4, category_key="nogi-absoluto", competitor_id=103),
    ]

    result = review(rows, window=window)

    assert result.ready is True
    assert result.window_state == window
    assert not result.issues


# --- recuento ----------------------------------------------------------------------------------


def test_summary_counts_only_current_registrations() -> None:
    """Corregidas y sustituidas no cuentan dos veces; retiradas y descalificadas no participan."""
    rows = [
        row(1),
        row(2, competitor_id=101),
        row(3, status="DRAFT", competitor_id=102),
        row(4, status="WITHDRAWN", competitor_id=103),
        row(5, status="DISQUALIFIED", competitor_id=104),
        row(6, status="CORRECTED", competitor_id=105),
        row(7, competitor_id=106, superseded=True),
    ]

    result = review(rows)

    assert result.summary.total == 5
    assert result.summary.confirmed == 2
    assert result.summary.draft == 1
    assert result.summary.withdrawn == 1
    assert result.summary.disqualified == 1


def test_draft_registrations_block_with_their_count() -> None:
    rows = [
        row(1),
        row(2, competitor_id=101),
        row(3, status="DRAFT", competitor_id=102),
        row(4, status="DRAFT", competitor_id=103),
    ]

    result = review(rows)

    assert result.ready is False
    pending = next(
        issue
        for issue in result.issues
        if issue.code is BracketReadinessBlocker.REGISTRATIONS_PENDING
    )
    assert pending.count == 2
    assert pending.category_key is None


def test_withdrawn_and_disqualified_do_not_count_as_participants() -> None:
    """Una categoria con una confirmada y una retirada no llega al minimo."""
    rows = [
        row(1),
        row(2, competitor_id=101, status="WITHDRAWN"),
        row(3, competitor_id=102, status="DISQUALIFIED"),
    ]

    result = review(rows)

    assert result.summary.confirmed == 1
    assert result.categories[0].confirmed_count == 1
    not_enough = next(
        issue
        for issue in result.categories[0].issues
        if issue.code is BracketReadinessBlocker.CATEGORY_NOT_ENOUGH_COMPETITORS
    )
    assert not_enough.count == 1
    assert not_enough.required_minimum == MINIMUM
    assert not_enough.category_key == "gi-peso-83"


# --- categorias --------------------------------------------------------------------------------


def test_a_category_that_reaches_the_minimum_is_ready() -> None:
    result = review([row(1), row(2, competitor_id=101)])

    assert result.ready is True
    [category] = result.categories
    assert category.category_key == "gi-peso-83"
    assert category.category_label == "Gi -83 kg"
    assert category.confirmed_count == 2
    assert category.required_minimum == MINIMUM
    assert category.ready is True
    assert category.issues == ()


def test_categories_are_ordered_by_key_with_uncategorized_first() -> None:
    """``category_key`` nulas primero y despues por clave: el orden del listado de S1."""
    rows = [
        row(1, category_key="nogi-absoluto", category_label="No-Gi Absoluto"),
        row(2, category_key="nogi-absoluto", category_label="No-Gi Absoluto", competitor_id=101),
        row(3, category_key="gi-peso-83"),
        row(4, category_key="gi-peso-83", competitor_id=102),
        row(5, category_key=None, category_label=None, competitor_id=103),
        row(6, category_key=None, category_label=None, competitor_id=104),
        row(7, category_key="gi-absoluto", category_label="Gi Absoluto", competitor_id=105),
        row(8, category_key="gi-absoluto", category_label="Gi Absoluto", competitor_id=106),
    ]

    result = review(rows)

    assert [category.category_key for category in result.categories] == [
        None,
        "gi-absoluto",
        "gi-peso-83",
        "nogi-absoluto",
    ]
    assert result.categories[0].category_label is None
    assert result.ready is True


def test_the_same_state_gives_the_same_answer_in_any_input_order() -> None:
    """Determinismo: ni el orden de entrada ni el de la consulta pueden cambiar la respuesta."""
    rows = [
        row(1),
        row(2, competitor_id=101, category_key="nogi-absoluto", category_label="No-Gi Absoluto"),
        row(3, competitor_id=102, status="DRAFT"),
        row(4, competitor_id=103, category_key="gi-absoluto", category_label="Gi Absoluto"),
        row(5, competitor_id=104, competitor_active=False),
    ]
    brackets = [bracket(9, blocker=BracketGenerationBlocker.SCORES), bracket(3)]

    forward = review(rows, window="OPEN", brackets=brackets)
    backward = review(list(reversed(rows)), window="OPEN", brackets=list(reversed(brackets)))

    assert forward == backward
    assert [category.category_key for category in forward.categories] == [
        "gi-absoluto",
        "gi-peso-83",
        "nogi-absoluto",
    ]


def test_zero_registrations_is_ready_with_no_categories() -> None:
    """Sin inscripciones vigentes no hay ninguna condicion de dominio que incumplir."""
    result = review([])

    assert result.ready is True
    assert result.summary.total == 0
    assert not result.categories
    assert not result.issues


# --- coherencia de la fila ---------------------------------------------------------------------


def test_inactive_competitor_blocks_the_category_and_the_tournament() -> None:
    rows = [
        row(1),
        row(2, competitor_id=101, competitor_active=False),
        row(3, competitor_id=102),
    ]

    result = review(rows)

    assert result.ready is False
    global_issue = next(
        issue
        for issue in result.issues
        if issue.code is BracketReadinessBlocker.COMPETITOR_INACTIVE
    )
    assert global_issue.count == 1
    assert global_issue.category_key == "gi-peso-83"
    [category] = result.categories
    assert category.confirmed_count == 3
    assert category.ready is False
    assert codes(category.issues) == [BracketReadinessBlocker.COMPETITOR_INACTIVE.value]


def test_competitor_from_another_tenant_is_inconsistent() -> None:
    """Un competidor que no es de este tenant no es ubicable en un cuadro de este torneo."""
    rows = [row(1), row(2, competitor_id=101, competitor_tenant_club_id=OTHER_TENANT)]

    result = review(rows)

    inconsistent = next(
        issue
        for issue in result.issues
        if issue.code is BracketReadinessBlocker.REGISTRATION_INCONSISTENT
    )
    assert inconsistent.count == 1
    assert result.categories[0].ready is False


def test_missing_competitor_row_is_inconsistent() -> None:
    rows = [row(1), row(2, competitor_id=101, competitor_active=None)]

    result = review(rows)

    assert BracketReadinessBlocker.REGISTRATION_INCONSISTENT.value in codes(result.issues)


def test_category_key_and_label_go_together() -> None:
    rows = [row(1, category_label=None), row(2, competitor_id=101)]

    result = review(rows)

    invalid = next(
        issue for issue in result.issues if issue.code is BracketReadinessBlocker.INVALID_CATEGORY
    )
    assert invalid.count == 1
    assert invalid.category_key == "gi-peso-83"


def test_a_key_that_is_not_a_slug_is_an_invalid_category() -> None:
    rows = [
        row(1, category_key="Gi-Peso-83", category_label="Gi -83 kg"),
        row(2, competitor_id=101),
    ]

    result = review(rows)

    assert BracketReadinessBlocker.INVALID_CATEGORY.value in codes(result.issues)


def test_a_frozen_label_that_means_two_things_is_invalid() -> None:
    """La etiqueta se congela por clave y por torneo, tambien a traves de filas corregidas."""
    rows = [
        row(1),
        row(2, competitor_id=101),
        row(3, competitor_id=102, category_label="Gi -83 kilos", status="CORRECTED"),
    ]

    result = review(rows)

    invalid = [
        issue for issue in result.issues if issue.code is BracketReadinessBlocker.INVALID_CATEGORY
    ]
    assert [(issue.count, issue.category_key) for issue in invalid] == [(1, "gi-peso-83")]
    # La etiqueta publicada es la de las filas vigentes (la correccion no manda sobre el presente).
    assert result.categories[0].category_label == "Gi -83 kg"


def test_an_affiliation_that_does_not_match_the_category_of_club_is_inconsistent() -> None:
    """La afiliacion exige representacion de club y apuntar a ese club y a ese competidor."""
    rows = [
        row(1),
        row(
            2,
            competitor_id=101,
            representation="CLUB",
            sports_club_id=5,
            affiliation_id=9,
            affiliation_competitor_id=999,
            affiliation_sports_club_id=5,
        ),
    ]

    result = review(rows)

    inconsistent = next(
        issue
        for issue in result.issues
        if issue.code is BracketReadinessBlocker.REGISTRATION_INCONSISTENT
    )
    assert inconsistent.count == 1


# --- estructura competitiva existente -----------------------------------------------------------


def test_a_generated_bracket_with_activity_on_it_blocks() -> None:
    """Con un cuadro ya generado y actividad encima, el reparto ya no esta en nuestras manos."""
    rows = [row(1), row(2, competitor_id=101)]
    brackets = [bracket(4, blocker=BracketGenerationBlocker.SCORES)]

    result = review(rows, brackets=brackets)

    existing = next(
        issue
        for issue in result.issues
        if issue.code is BracketReadinessBlocker.BRACKET_ALREADY_EXISTS
    )
    assert existing.count == 1
    assert result.ready is False


def test_a_regenerable_bracket_does_not_block() -> None:
    """Sin nada encima, F2C puede regenerar el cuadro (la generacion es idempotente)."""
    result = review([row(1), row(2, competitor_id=101)], brackets=[bracket(4)])

    assert result.ready is True


def test_a_stage_item_without_a_bracket_does_not_block() -> None:
    """Un ``stage_item`` vacio o con un solo entrante no es un cuadro: no hay nada que perder."""
    result = review(
        [row(1), row(2, competitor_id=101)],
        brackets=[
            bracket(4, entrant_count=0, blocker=BracketGenerationBlocker.TOO_FEW_ENTRANTS),
            bracket(5, entrant_count=1, blocker=BracketGenerationBlocker.TOO_FEW_ENTRANTS),
        ],
    )

    assert result.ready is True


# --- orden de la lista global -------------------------------------------------------------------


def test_the_global_list_is_the_complete_view_in_canonical_order() -> None:
    """La lista global incluye los bloqueos de cada categoria, en el orden del vocabulario."""
    rows = [
        row(1),
        row(2, competitor_id=101, competitor_active=False),
        row(3, competitor_id=102, status="DRAFT"),
        row(4, competitor_id=103, category_key="nogi-absoluto", category_label="No-Gi Absoluto"),
    ]
    brackets = [bracket(4, blocker=BracketGenerationBlocker.WINNER)]

    result = review(rows, window="OPEN", brackets=brackets)

    assert codes(result.issues) == [
        BracketReadinessBlocker.REGISTRATION_WINDOW_OPEN.value,
        BracketReadinessBlocker.REGISTRATIONS_PENDING.value,
        BracketReadinessBlocker.CATEGORY_NOT_ENOUGH_COMPETITORS.value,
        BracketReadinessBlocker.COMPETITOR_INACTIVE.value,
        BracketReadinessBlocker.BRACKET_ALREADY_EXISTS.value,
    ]
    assert result.ready is False
    # La vista por categoria es la proyeccion: la de la categoria afectada esta marcada.
    by_key = {category.category_key: category for category in result.categories}
    assert by_key["nogi-absoluto"].ready is False
    assert by_key["gi-peso-83"].ready is False


# --- S4.8a-3 §9: clasificacion integrada en el readiness ----------------------------------------


def test_the_readiness_vocabulary_appends_the_engine_codes_without_reordering() -> None:
    """Los nueve codigos del motor se anaden **al final**: el orden de los existentes no cambia."""
    codes_in_order = [blocker.value for blocker in BracketReadinessBlocker]

    assert codes_in_order[:7] == [
        "REGISTRATION_WINDOW_OPEN",
        "REGISTRATIONS_PENDING",
        "CATEGORY_NOT_ENOUGH_COMPETITORS",
        "COMPETITOR_INACTIVE",
        "INVALID_CATEGORY",
        "REGISTRATION_INCONSISTENT",
        "BRACKET_ALREADY_EXISTS",
    ]
    assert codes_in_order[7:] == [
        "MISSING_DATE_OF_BIRTH",
        "MISSING_SEX",
        "MISSING_BELT",
        "MISSING_WEIGHT",
        "MISSING_MODALITY",
        "NO_COMPATIBLE_CATEGORY",
        "CATEGORY_NOT_RESOLVED",
        "CATEGORY_INCOMPATIBLE",
        "INVALID_CATEGORY_BELT_RANGE",
    ]


def test_a_classification_blocker_does_not_count_toward_the_minimum() -> None:
    """Una confirmada cuya clasificacion no resuelve no es un participante del cuadro."""
    rows = [
        row(1, classification_blockers=(BracketReadinessBlocker.CATEGORY_NOT_RESOLVED,)),
        row(
            2,
            competitor_id=101,
            classification_blockers=(BracketReadinessBlocker.CATEGORY_NOT_RESOLVED,),
        ),
    ]

    result = review(rows, window="CLOSED")

    assert result.ready is False
    assert codes(result.issues) == [
        BracketReadinessBlocker.CATEGORY_NOT_ENOUGH_COMPETITORS.value,
        BracketReadinessBlocker.CATEGORY_NOT_RESOLVED.value,
    ]
    [category] = result.categories
    assert category.confirmed_count == 0
    assert category.required_minimum == MINIMUM


def test_a_classifiable_pair_reaches_the_minimum() -> None:
    result = review([row(1), row(2, competitor_id=101)], window="CLOSED")

    assert result.ready is True
    assert result.categories[0].confirmed_count == 2


def test_an_owner_override_makes_an_incompatible_row_count() -> None:
    """Un override OWNER valido y auditado legitima el bloqueo: la fila vuelve a contar."""
    rows = [
        row(
            1,
            classification_blockers=(BracketReadinessBlocker.CATEGORY_INCOMPATIBLE,),
            overridden=True,
        ),
        row(
            2,
            competitor_id=101,
            classification_blockers=(BracketReadinessBlocker.CATEGORY_INCOMPATIBLE,),
            overridden=True,
        ),
    ]

    result = review(rows, window="CLOSED")

    assert result.ready is True
    assert BracketReadinessBlocker.CATEGORY_INCOMPATIBLE.value not in codes(result.issues)
    assert result.categories[0].confirmed_count == 2


def test_a_missing_datum_blocks_the_count_with_its_own_code() -> None:
    """Sin peso declarado la categoria con restriccion de peso no admite la inscripcion."""
    rows = [
        row(1, classification_blockers=(BracketReadinessBlocker.MISSING_WEIGHT,)),
        row(
            2, competitor_id=101, classification_blockers=(BracketReadinessBlocker.MISSING_WEIGHT,)
        ),
    ]

    result = review(rows, window="CLOSED")

    assert BracketReadinessBlocker.MISSING_WEIGHT.value in codes(result.issues)
    assert BracketReadinessBlocker.CATEGORY_NOT_ENOUGH_COMPETITORS.value in codes(result.issues)


def test_the_window_minimum_and_frozen_bracket_still_decide() -> None:
    """§9 no cambia la ventana, el minimo ni el cuadro congelado."""
    rows = [row(1), row(2, competitor_id=101)]

    assert codes(review(rows, window="OPEN").issues) == [
        BracketReadinessBlocker.REGISTRATION_WINDOW_OPEN.value
    ]
    assert codes(review([row(1)], window="CLOSED").issues) == [
        BracketReadinessBlocker.CATEGORY_NOT_ENOUGH_COMPETITORS.value
    ]
    frozen = [bracket(5, blocker=BracketGenerationBlocker.SCORES)]
    assert codes(review(rows, window="CLOSED", brackets=frozen).issues) == [
        BracketReadinessBlocker.BRACKET_ALREADY_EXISTS.value
    ]
