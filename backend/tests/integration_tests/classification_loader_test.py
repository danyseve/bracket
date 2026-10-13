"""S4.8a-2 — cargador de clasificacion contra la base real (integracion, LAB).

La capa pura se prueba sin base de datos; aqui se prueba lo que solo la base puede demostrar: que las
reglas, el cinturon resuelto y la entrada de una inscripcion se leen como el motor espera, y que el
aislamiento por club se respeta en las dos lecturas.

Solo lee y escribe las filas que crea y retira el propio modulo (no toca datos de nadie).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest_asyncio
from heliclockter import datetime_utc

from bracket.database import database
from bracket.logic.classification import (
    BeltLevel,
    ClassificationBlocker,
    ClassificationWarning,
    CompetitorFacts,
    classify_input,
)
from bracket.sql.classification import (
    get_classification_categories,
    get_registration_classification_input,
)
from bracket.utils.id_types import (
    ClubId,
    CompetitorId,
    StageId,
    StageItemId,
    TournamentId,
    TournamentRegistrationId,
)

_START_TIME = datetime_utc(2026, 10, 13, 9, 0, 0, tzinfo=ZoneInfo("UTC"))
_CREATED = datetime_utc(2026, 1, 1, 0, 0, 0, tzinfo=ZoneInfo("UTC"))

_INSERT_CLUB = """
    INSERT INTO clubs (name, created) VALUES (:name, :created) RETURNING id
"""
_INSERT_TOURNAMENT = """
    INSERT INTO tournaments (name, start_time, club_id, dashboard_public)
    VALUES (:name, :start_time, :club_id, false) RETURNING id
"""
_INSERT_STAGE = """
    INSERT INTO stages (tournament_id, name, created, is_active)
    VALUES (:tournament_id, :name, :created, true) RETURNING id
"""
_INSERT_STAGE_ITEM = """
    INSERT INTO stage_items (
        name, stage_id, team_count, ranking_id, type, sex_constraint, age_min, age_max,
        weight_min_kg, weight_max_kg, modality, belt_min_id, belt_max_id
    ) VALUES (
        :name, :stage_id, 2, NULL, CAST('ROUND_ROBIN' AS stage_type),
        CAST(:sex_constraint AS sex_constraint), :age_min, :age_max,
        :weight_min_kg, :weight_max_kg, CAST(:modality AS modality), :belt_min_id, :belt_max_id
    ) RETURNING id
"""
_INSERT_COMPETITOR = """
    INSERT INTO competitors (
        display_name, managed_by_club_id, active, created, date_of_birth, sex, belt_id
    ) VALUES (
        :display_name, :managed_by_club_id, true, :created, :date_of_birth,
        CAST(:sex AS competitor_sex), :belt_id
    ) RETURNING id
"""
_INSERT_REGISTRATION = """
    INSERT INTO tournament_registrations (
        tournament_id, competitor_id, identity_status, representation, sports_club_id,
        affiliation_id, category_key, category_label, competitor_name_snapshot,
        sports_club_name_snapshot, status, revision, corrects_registration_id, created,
        declared_weight_kg, modality
    ) VALUES (
        :tournament_id, :competitor_id, CAST('VERIFIED' AS registration_identity_status),
        CAST('INDEPENDENT' AS registration_representation), NULL, NULL, :category_key,
        :category_label, :competitor_name_snapshot, NULL, CAST('CONFIRMED' AS registration_status),
        1, NULL, :created, :declared_weight_kg, CAST(:modality AS modality)
    ) RETURNING id
"""
_DELETE_REGISTRATIONS = "DELETE FROM tournament_registrations WHERE id = ANY(:ids)"
_DELETE_COMPETITORS = "DELETE FROM competitors WHERE id = ANY(:ids)"
_DELETE_STAGE_ITEMS = "DELETE FROM stage_items WHERE id = ANY(:ids)"
_DELETE_STAGES = "DELETE FROM stages WHERE id = ANY(:ids)"
_DELETE_TOURNAMENTS = "DELETE FROM tournaments WHERE id = ANY(:ids)"
_DELETE_CLUBS = "DELETE FROM clubs WHERE id = ANY(:ids)"


@dataclass
class LoadedData:
    """Filas creadas por el modulo de pruebas (se retiran al terminar la bateria)."""

    tenant: ClubId
    other_tenant: ClubId
    tournament: TournamentId
    other_tournament: TournamentId
    category_blue: StageItemId
    category_feather: StageItemId
    category_legacy: StageItemId
    other_category: StageItemId
    competitor: CompetitorId
    registration: TournamentRegistrationId
    other_registration: TournamentRegistrationId


async def _insert(query: str, **values: object) -> int:
    """Ejecuta un ``INSERT ... RETURNING id`` y lo confirma (una conexion suelta no persiste)."""
    async with database.transaction():
        return int(await database.fetch_val(query=query, values=values))


async def _belt_id(scope: str, code: str) -> int:
    """Identificador del cinturon sembrado por la migracion (no se crea ninguno)."""
    return int(
        await database.fetch_val(
            query="SELECT id FROM belts WHERE scope = :scope AND code = :code",
            values={"scope": scope, "code": code},
        )
    )


@pytest_asyncio.fixture
async def loaded() -> AsyncIterator[LoadedData]:
    """Escenario completo: dos clubes, tres categorias con reglas y una inscripcion confirmada."""
    tenant = ClubId(await _insert(_INSERT_CLUB, name="club-clasificacion", created=_CREATED))
    other_tenant = ClubId(await _insert(_INSERT_CLUB, name="club-ajeno", created=_CREATED))
    tournament = TournamentId(
        await _insert(
            _INSERT_TOURNAMENT,
            name="torneo-clasificacion",
            start_time=_START_TIME,
            club_id=tenant,
        )
    )
    other_tournament = TournamentId(
        await _insert(
            _INSERT_TOURNAMENT,
            name="torneo-ajeno",
            start_time=_START_TIME,
            club_id=other_tenant,
        )
    )
    stage = StageId(
        await _insert(_INSERT_STAGE, tournament_id=tournament, name="fase", created=_CREATED)
    )
    other_stage = StageId(
        await _insert(
            _INSERT_STAGE, tournament_id=other_tournament, name="fase", created=_CREATED
        )
    )
    blue = await _belt_id("ADULT", "BLUE")
    purple = await _belt_id("ADULT", "PURPLE")

    category_blue = StageItemId(
        await _insert(
            _INSERT_STAGE_ITEM,
            name="-77 kg azul",
            stage_id=stage,
            sex_constraint="MALE",
            age_min=18,
            age_max=39,
            weight_min_kg=Decimal("71.00"),
            weight_max_kg=Decimal("77.00"),
            modality="GI",
            belt_min_id=blue,
            belt_max_id=purple,
        )
    )
    category_feather = StageItemId(
        await _insert(
            _INSERT_STAGE_ITEM,
            name="-66 kg",
            stage_id=stage,
            sex_constraint=None,
            age_min=None,
            age_max=None,
            weight_min_kg=Decimal("62.00"),
            weight_max_kg=Decimal("66.00"),
            modality=None,
            belt_min_id=None,
            belt_max_id=None,
        )
    )
    category_legacy = StageItemId(
        await _insert(
            _INSERT_STAGE_ITEM,
            name="categoria heredada",
            stage_id=stage,
            sex_constraint=None,
            age_min=None,
            age_max=None,
            weight_min_kg=None,
            weight_max_kg=None,
            modality=None,
            belt_min_id=None,
            belt_max_id=None,
        )
    )
    other_category = StageItemId(
        await _insert(
            _INSERT_STAGE_ITEM,
            name="categoria ajena",
            stage_id=other_stage,
            sex_constraint=None,
            age_min=None,
            age_max=None,
            weight_min_kg=None,
            weight_max_kg=None,
            modality=None,
            belt_min_id=None,
            belt_max_id=None,
        )
    )
    competitor = CompetitorId(
        await _insert(
            _INSERT_COMPETITOR,
            display_name="competidor-clasificacion",
            managed_by_club_id=tenant,
            created=_CREATED,
            date_of_birth=date(1995, 5, 5),
            sex="MALE",
            belt_id=blue,
        )
    )
    registration = TournamentRegistrationId(
        await _insert(
            _INSERT_REGISTRATION,
            tournament_id=tournament,
            competitor_id=competitor,
            category_key=f"si:{category_blue}",
            category_label="-77 kg azul",
            competitor_name_snapshot="competidor-clasificacion",
            created=_CREATED,
            declared_weight_kg=Decimal("74.50"),
            modality="GI",
        )
    )
    other_registration = TournamentRegistrationId(
        await _insert(
            _INSERT_REGISTRATION,
            tournament_id=other_tournament,
            competitor_id=None,
            category_key=f"si:{other_category}",
            category_label="categoria ajena",
            competitor_name_snapshot="sin competidor",
            created=_CREATED,
            declared_weight_kg=None,
            modality=None,
        )
    )
    data = LoadedData(
        tenant=tenant,
        other_tenant=other_tenant,
        tournament=tournament,
        other_tournament=other_tournament,
        category_blue=category_blue,
        category_feather=category_feather,
        category_legacy=category_legacy,
        other_category=other_category,
        competitor=competitor,
        registration=registration,
        other_registration=other_registration,
    )
    try:
        yield data
    finally:
        await database.execute(
            query=_DELETE_REGISTRATIONS, values={"ids": [registration, other_registration]}
        )
        await database.execute(query=_DELETE_COMPETITORS, values={"ids": [competitor]})
        await database.execute(
            query=_DELETE_STAGE_ITEMS,
            values={
                "ids": [category_blue, category_feather, category_legacy, other_category]
            },
        )
        await database.execute(query=_DELETE_STAGES, values={"ids": [stage, other_stage]})
        await database.execute(
            query=_DELETE_TOURNAMENTS, values={"ids": [tournament, other_tournament]}
        )
        await database.execute(query=_DELETE_CLUBS, values={"ids": [tenant, other_tenant]})


async def test_the_categories_are_loaded_with_their_rules_and_resolved_belt(
    loaded: LoadedData,
) -> None:
    """Las reglas llegan tipadas y el cinturon llega resuelto a escala y orden."""
    categories = await get_classification_categories(loaded.tournament, loaded.tenant)
    assert [category.stage_item_id for category in categories] == [
        loaded.category_blue,
        loaded.category_feather,
        loaded.category_legacy,
    ]
    blue, feather, legacy = categories
    assert blue.category_key == f"si:{loaded.category_blue}"
    assert blue.category_label == "-77 kg azul"
    assert blue.sex_constraint == "MALE"
    assert (blue.age_min, blue.age_max) == (18, 39)
    assert (blue.weight_min_kg, blue.weight_max_kg) == (Decimal("71.00"), Decimal("77.00"))
    assert blue.modality == "GI"
    assert blue.belt_min == BeltLevel(scope="ADULT", rank=2, code="BLUE")
    assert blue.belt_max == BeltLevel(scope="ADULT", rank=3, code="PURPLE")
    assert blue.belt_range_is_valid is True
    assert feather.sex_constraint is None
    assert feather.belt_min is None and feather.belt_max is None
    assert feather.modality is None
    assert legacy.has_any_rule is False


async def test_the_loaded_registration_is_classified_end_to_end(loaded: LoadedData) -> None:
    """Torneo, competidor e inscripcion se leen y el motor decide sobre datos reales."""
    data = await get_registration_classification_input(loaded.registration, loaded.tenant)
    assert data is not None
    assert data.start_time.date() == date(2026, 10, 13)
    assert data.competitor.date_of_birth == date(1995, 5, 5)
    assert data.competitor.sex == "MALE"
    assert data.competitor.belt == BeltLevel(scope="ADULT", rank=2, code="BLUE")
    assert data.registration.declared_weight_kg == Decimal("74.50")
    assert data.registration.modality == "GI"
    assert data.registration.status == "CONFIRMED"

    assessment = classify_input(data)
    compatible = {verdict.category_key for verdict in assessment.result.compatible_categories}
    assert compatible == {f"si:{loaded.category_blue}", f"si:{loaded.category_legacy}"}
    assert assessment.chosen.compatible is True
    assert assessment.ok is True
    assert assessment.countable is True
    # dos categorias admiten la inscripcion: no hay recomendacion determinista
    assert assessment.result.recommended_category is None
    assert ClassificationWarning.MULTIPLE_COMPATIBLE_CATEGORIES in assessment.warnings
    feather = next(
        verdict
        for verdict in assessment.result.verdicts
        if verdict.category_key == f"si:{loaded.category_feather}"
    )
    assert feather.blockers == (ClassificationBlocker.CATEGORY_INCOMPATIBLE,)


async def test_a_missing_declared_weight_is_reported_as_missing_data(loaded: LoadedData) -> None:
    """Falta el peso: ``MISSING_WEIGHT`` (dato que no esta), no ``CATEGORY_INCOMPATIBLE``."""
    await database.execute(
        query="UPDATE tournament_registrations SET declared_weight_kg = NULL WHERE id = :id",
        values={"id": loaded.registration},
    )
    data = await get_registration_classification_input(loaded.registration, loaded.tenant)
    assert data is not None
    assessment = classify_input(data)
    assert ClassificationBlocker.MISSING_WEIGHT in assessment.blockers
    assert ClassificationBlocker.CATEGORY_INCOMPATIBLE not in assessment.blockers
    assert assessment.ok is False
    assert assessment.countable is False
    # la categoria heredada, que no exige peso, sigue admitiendo la inscripcion
    assert f"si:{loaded.category_legacy}" in {
        verdict.category_key for verdict in assessment.result.compatible_categories
    }


async def test_another_club_cannot_read_the_categories_nor_the_registration(
    loaded: LoadedData,
) -> None:
    """Aislamiento por club en las dos lecturas: nada de otro tenant se lee."""
    assert await get_classification_categories(loaded.tournament, loaded.other_tenant) == ()
    assert (
        await get_registration_classification_input(loaded.registration, loaded.other_tenant)
        is None
    )


async def test_a_registration_without_competitor_is_loaded_as_empty_facts(
    loaded: LoadedData,
) -> None:
    """Un borrador sin competidor no es un error de lectura: llega con hechos vacios."""
    data = await get_registration_classification_input(
        loaded.other_registration, loaded.other_tenant
    )
    assert data is not None
    assert data.competitor == CompetitorFacts()
    assert data.registration.category_key == f"si:{loaded.other_category}"
    assert data.registration.is_confirmed is True
    assert data.registration.is_current is True
