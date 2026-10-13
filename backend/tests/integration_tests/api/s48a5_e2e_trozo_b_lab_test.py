# pylint: disable=redefined-outer-name
"""E2E realista de S4.8a-5 (TROZO B, §8-§9): inscripciones y clasificacion contra el API HTTP real.

Pruebas **negras** sobre el escenario del TROZO A (24 competidores, 3 academias, 4 categorias
estructuradas + 1 legacy) y la base de laboratorio ``bracket_ci``. No hay mocks del motor ni del
protocolo: se dan de alta inscripciones por el camino HTTP real (puerta F3 + idempotencia) y se
inspecciona la clasificacion con ``GET /tournaments/{t}/registrations/{r}/classification``.

Que fija este modulo:

* §8  alta de inscripciones con ``declared_weight_kg``, ``modality``, ``representation`` y la
      afiliacion historica del trozo A, dejando ``category_key`` **sin resolver** en todos los casos
      salvo el de "categoria incompatible", donde se parte de una categoria **asignada**;
* §9  los ocho casos obligatorios de clasificacion, con los codigos **exactos** del vocabulario del
      motor: 1 compatible -> es la recomendada; >1 -> recomendada nula + ``MULTIPLE_COMPATIBLE_CATEGORIES``;
      0 -> ``NO_COMPATIBLE_CATEGORY``; y los blockers ``MISSING_*`` / ``CATEGORY_INCOMPATIBLE``;
* la **no seleccion arbitraria** (con varias compatibles nunca se recomienda una) y que la **lectura
  no escribe**: ni reserva claves de idempotencia, ni audita, ni mueve la inscripcion.

§25: al cerrar el modulo se vuelca el resumen en ``/home/ubuntu/s48a5-evidence/trozo-b.json``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio

from bracket.config import config
from bracket.logic.classification.vocabulary import ClassificationBlocker, ClassificationWarning
from tests.integration_tests.api.generate_bracket_http_support import SYNTHETIC_SECRET
from tests.integration_tests.api.s48a5_e2e_trozo_a_support import (
    LEGACY_CATEGORY,
    E2EDataset,
    registration_row,
)
from tests.integration_tests.api.s48a5_e2e_trozo_b_support import (
    ClassificationScenario,
    blocker,
    classify_http,
    count_audit_rows,
    count_idempotency_keys,
    create_registration_http,
    record_b,
    record_case,
    representation_of,
    set_registration_category_sql,
    trozo_b_scenario,
    warning,
    write_evidence_b,
)

MISSING_DATE_OF_BIRTH = blocker(ClassificationBlocker.MISSING_DATE_OF_BIRTH)
MISSING_BELT = blocker(ClassificationBlocker.MISSING_BELT)
MISSING_WEIGHT = blocker(ClassificationBlocker.MISSING_WEIGHT)
MISSING_MODALITY = blocker(ClassificationBlocker.MISSING_MODALITY)
CATEGORY_NOT_RESOLVED = blocker(ClassificationBlocker.CATEGORY_NOT_RESOLVED)
CATEGORY_INCOMPATIBLE = blocker(ClassificationBlocker.CATEGORY_INCOMPATIBLE)
NO_COMPATIBLE_CATEGORY = blocker(ClassificationBlocker.NO_COMPATIBLE_CATEGORY)
MULTIPLE = warning(ClassificationWarning.MULTIPLE_COMPATIBLE_CATEGORIES)


@pytest.fixture(scope="module", autouse=True)
def e2e_write_gate() -> Iterator[None]:
    """Escritura F3 y secreto de idempotencia habilitados durante todo el modulo.

    El alta de inscripciones (``POST .../registrations``) esta detras de la puerta de escritura y del
    protocolo de idempotencia. No se depende del entorno de CI: el modulo lo fija y lo restaura.
    """
    previous = {
        "f3_write_enabled": config.f3_write_enabled,
        "idempotency_hmac_key": config.idempotency_hmac_key,
        "idempotency_hmac_key_version": config.idempotency_hmac_key_version,
    }
    config.f3_write_enabled = True
    config.idempotency_hmac_key = SYNTHETIC_SECRET
    config.idempotency_hmac_key_version = "v1"
    try:
        yield
    finally:
        for name, value in previous.items():
            setattr(config, name, value)


@pytest_asyncio.fixture(loop_scope="session", scope="module")
async def scenario(
    startup_and_shutdown_uvicorn_server: None,  # noqa: F811 - fixture autouse del conftest de api
    e2e_write_gate: None,  # noqa: F811 - fixture de modulo definida arriba
) -> AsyncIterator[ClassificationScenario]:
    """Escenario del trozo A + superficie de clasificacion; al cerrar vuelca la evidencia de §25."""
    async with trozo_b_scenario() as scenario_:
        try:
            yield scenario_
        finally:
            write_evidence_b()


# ---------------------------------------------------------------- helpers de asercion


def compatible_keys(body: dict[str, Any]) -> list[str]:
    return [str(category["category_key"]) for category in body["compatible_categories"]]


def recommended_key(body: dict[str, Any]) -> str | None:
    recommended = body["recommended_category"]
    return None if recommended is None else str(recommended["category_key"])


async def register(
    scenario_: ClassificationScenario,
    *,
    tournament_id: int,
    competitor_key: str,
    declared_weight_kg: Decimal | None,
    modality: str | None,
    category_key: str | None = None,
    category_label: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Alta real de la inscripcion del caso, con la representacion/afiliacion del competidor."""
    dataset = scenario_.dataset
    representation = representation_of(dataset, competitor_key)
    body = await create_registration_http(
        tournament_id=tournament_id,
        token=dataset.owner_token,
        competitor_id=int(dataset.competitors[competitor_key]),
        declared_weight_kg=declared_weight_kg,
        modality=modality,
        category_key=category_key,
        category_label=category_label,
        **representation,
    )
    return int(body["id"]), body


# ---------------------------------------------------------------- §8/§9 caso (a)


@pytest.mark.asyncio(loop_scope="session")
async def test_caso_a_exactamente_una_compatible_es_la_recomendada(
    scenario: ClassificationScenario,
) -> None:
    """§9(a): una sola categoria admite la inscripcion -> esa es la recomendada, sin bloqueos de motor."""
    dataset = scenario.dataset
    tournament = int(scenario.surface.tournament)
    registration_id, created = await register(
        scenario, tournament_id=tournament, competitor_key="a2", declared_weight_kg=Decimal("110.00"), modality="GI"
    )

    # §8: la inscripcion es CLUB con su academia y su afiliacion historica, y nace sin categoria.
    assert created["representation"] == "CLUB"
    assert int(created["sports_club_id"]) == int(dataset.clubs["A"])
    assert created["affiliation_id"] == int(dataset.affiliations["a2"])
    assert created["category_key"] is None and created["category_label"] is None

    body = await classify_http(tournament_id=tournament, registration_id=registration_id, token=dataset.owner_token)

    heavy = scenario.surface.key("cat-male-gi-heavy")
    assert compatible_keys(body) == [heavy]
    assert len(body["compatible_categories"]) == 1
    assert recommended_key(body) == heavy
    assert body["blockers"] == [CATEGORY_NOT_RESOLVED]
    assert body["warnings"] == []

    record_case(
        case="a",
        label="exactamente una compatible -> recomendada",
        competitor_key="a2",
        registration_id=registration_id,
        tournament_id=tournament,
        declared_weight_kg="110.00",
        modality="GI",
        representation="CLUB",
        classification=body,
    )
    record_b("§9.a.una_compatible", True, {"recommended": recommended_key(body)})


# ---------------------------------------------------------------- §9 caso (b)


@pytest.mark.asyncio(loop_scope="session")
async def test_caso_b_multiples_compatibles_no_recomienda_y_avisa(
    scenario: ClassificationScenario,
) -> None:
    """§9(b): >1 categoria admite la inscripcion -> recomendada nula y aviso, nunca una elegida sola."""
    dataset = scenario.dataset
    # El torneo del escenario A trae 4 categorias estructuradas + 1 legacy; el cuadro legacy admite
    # cualquier inscripcion, asi que una compatible estructurada da DOS compatibles.
    tournament = int(dataset.tournament)
    registration_id, _ = await register(
        scenario, tournament_id=tournament, competitor_key="c2", declared_weight_kg=Decimal("55.00"), modality="GI"
    )

    body = await classify_http(tournament_id=tournament, registration_id=registration_id, token=dataset.owner_token)

    female = f"si:{dataset.categories['cat-female-gi']}"
    legacy = f"si:{dataset.categories[LEGACY_CATEGORY]}"
    assert set(compatible_keys(body)) == {female, legacy}
    assert len(body["compatible_categories"]) == 2
    assert recommended_key(body) is None
    assert MULTIPLE in body["warnings"]
    assert body["blockers"] == [CATEGORY_NOT_RESOLVED]

    record_case(
        case="b",
        label="multiples compatibles -> recomendada nula + MULTIPLE",
        competitor_key="c2",
        registration_id=registration_id,
        tournament_id=tournament,
        declared_weight_kg="55.00",
        modality="GI",
        representation="CLUB",
        classification=body,
    )
    record_b("§9.b.multiples", True, {"compatible": compatible_keys(body)})


# ---------------------------------------------------------------- §9 caso (c)


@pytest.mark.asyncio(loop_scope="session")
async def test_caso_c_ninguna_compatible_bloquea(scenario: ClassificationScenario) -> None:
    """§9(c): ninguna categoria admite la inscripcion -> lista vacia + NO_COMPATIBLE_CATEGORY."""
    dataset = scenario.dataset
    tournament = int(scenario.surface.tournament)
    registration_id, created = await register(
        scenario, tournament_id=tournament, competitor_key="i3", declared_weight_kg=Decimal("70.00"), modality="GI"
    )
    # §8: representacion real del competidor sin academia.
    assert created["representation"] == "INDEPENDENT"
    assert created["sports_club_id"] is None and created["affiliation_id"] is None

    body = await classify_http(tournament_id=tournament, registration_id=registration_id, token=dataset.owner_token)

    assert body["compatible_categories"] == []
    assert recommended_key(body) is None
    assert body["blockers"] == [CATEGORY_NOT_RESOLVED, CATEGORY_INCOMPATIBLE, NO_COMPATIBLE_CATEGORY]
    assert body["warnings"] == []

    record_case(
        case="c",
        label="ninguna compatible -> NO_COMPATIBLE_CATEGORY",
        competitor_key="i3",
        registration_id=registration_id,
        tournament_id=tournament,
        declared_weight_kg="70.00",
        modality="GI",
        representation="INDEPENDENT",
        classification=body,
    )
    record_b("§9.c.ninguna", True, {"blockers": body["blockers"]})


# ---------------------------------------------------------------- §9 casos (d)-(g): datos que faltan


@pytest.mark.asyncio(loop_scope="session")
async def test_caso_d_sin_peso_bloquea(scenario: ClassificationScenario) -> None:
    """§9(d): la inscripcion no declara peso y las categorias lo restringen -> MISSING_WEIGHT."""
    dataset = scenario.dataset
    tournament = int(scenario.surface.tournament)
    registration_id, created = await register(
        scenario, tournament_id=tournament, competitor_key="b2", declared_weight_kg=None, modality="GI"
    )
    assert created["declared_weight_kg"] is None

    body = await classify_http(tournament_id=tournament, registration_id=registration_id, token=dataset.owner_token)

    assert body["compatible_categories"] == [] and recommended_key(body) is None
    assert body["blockers"] == [MISSING_WEIGHT, CATEGORY_NOT_RESOLVED, CATEGORY_INCOMPATIBLE, NO_COMPATIBLE_CATEGORY]
    record_case(
        case="d", label="sin peso -> MISSING_WEIGHT", competitor_key="b2",
        registration_id=registration_id, tournament_id=tournament, declared_weight_kg=None,
        modality="GI", representation="CLUB", classification=body,
    )
    record_b("§9.d.sin_peso", True, {"blockers": body["blockers"]})


@pytest.mark.asyncio(loop_scope="session")
async def test_caso_e_sin_cinturon_bloquea(scenario: ClassificationScenario) -> None:
    """§9(e): el competidor no tiene cinturon y la categoria lo restringe -> MISSING_BELT."""
    dataset = scenario.dataset
    tournament = int(scenario.surface.tournament)
    registration_id, _ = await register(
        scenario, tournament_id=tournament, competitor_key="b6", declared_weight_kg=Decimal("74.50"), modality="GI"
    )

    body = await classify_http(tournament_id=tournament, registration_id=registration_id, token=dataset.owner_token)

    assert body["compatible_categories"] == [] and recommended_key(body) is None
    assert body["blockers"] == [MISSING_BELT, CATEGORY_NOT_RESOLVED, CATEGORY_INCOMPATIBLE, NO_COMPATIBLE_CATEGORY]
    record_case(
        case="e", label="sin cinturon -> MISSING_BELT", competitor_key="b6",
        registration_id=registration_id, tournament_id=tournament, declared_weight_kg="74.50",
        modality="GI", representation="CLUB", classification=body,
    )
    record_b("§9.e.sin_cinturon", True, {"blockers": body["blockers"]})


@pytest.mark.asyncio(loop_scope="session")
async def test_caso_f_sin_fecha_de_nacimiento_bloquea(scenario: ClassificationScenario) -> None:
    """§9(f): el competidor no tiene fecha de nacimiento y la categoria restringe edad -> MISSING_DATE_OF_BIRTH."""
    dataset = scenario.dataset
    tournament = int(scenario.surface.tournament)
    registration_id, _ = await register(
        scenario, tournament_id=tournament, competitor_key="a7", declared_weight_kg=Decimal("74.50"), modality="GI"
    )

    body = await classify_http(tournament_id=tournament, registration_id=registration_id, token=dataset.owner_token)

    assert body["compatible_categories"] == [] and recommended_key(body) is None
    assert body["blockers"] == [
        MISSING_DATE_OF_BIRTH, CATEGORY_NOT_RESOLVED, CATEGORY_INCOMPATIBLE, NO_COMPATIBLE_CATEGORY
    ]
    record_case(
        case="f", label="sin DOB -> MISSING_DATE_OF_BIRTH", competitor_key="a7",
        registration_id=registration_id, tournament_id=tournament, declared_weight_kg="74.50",
        modality="GI", representation="CLUB", classification=body,
    )
    record_b("§9.f.sin_dob", True, {"blockers": body["blockers"]})


@pytest.mark.asyncio(loop_scope="session")
async def test_caso_g_sin_modalidad_bloquea(scenario: ClassificationScenario) -> None:
    """§9(g): la inscripcion no declara modalidad y la categoria la restringe -> MISSING_MODALITY."""
    dataset = scenario.dataset
    tournament = int(scenario.surface.tournament)
    registration_id, created = await register(
        scenario, tournament_id=tournament, competitor_key="c3", declared_weight_kg=Decimal("74.50"), modality=None
    )
    assert created["modality"] is None

    body = await classify_http(tournament_id=tournament, registration_id=registration_id, token=dataset.owner_token)

    assert body["compatible_categories"] == [] and recommended_key(body) is None
    assert body["blockers"] == [MISSING_MODALITY, CATEGORY_NOT_RESOLVED, CATEGORY_INCOMPATIBLE, NO_COMPATIBLE_CATEGORY]
    record_case(
        case="g", label="sin modalidad -> MISSING_MODALITY", competitor_key="c3",
        registration_id=registration_id, tournament_id=tournament, declared_weight_kg="74.50",
        modality=None, representation="CLUB", classification=body,
    )
    record_b("§9.g.sin_modalidad", True, {"blockers": body["blockers"]})


# ---------------------------------------------------------------- §9 caso (h): categoria asignada incompatible


@pytest.mark.asyncio(loop_scope="session")
async def test_caso_h_categoria_asignada_incompatible_bloquea(scenario: ClassificationScenario) -> None:
    """§9(h): una categoria **asignada** cuyas reglas no encajan -> CATEGORY_INCOMPATIBLE.

    La recomendacion sigue siendo la unica compatible; la categoria fijada se senala como incompatible
    (no se "arregla" la inscripcion ni se elige otra por el cliente).
    """
    dataset = scenario.dataset
    tournament = int(scenario.surface.tournament)
    registration_id, _ = await register(
        scenario, tournament_id=tournament, competitor_key="a6", declared_weight_kg=Decimal("100.00"), modality="GI"
    )

    # Estado de partida del caso: categoria asignada (por SQL; la asignacion por API es §7, otra unidad).
    await set_registration_category_sql(
        registration_id, scenario.surface.key("cat-female-gi"), "cat-female-gi"
    )

    body = await classify_http(tournament_id=tournament, registration_id=registration_id, token=dataset.owner_token)

    heavy = scenario.surface.key("cat-male-gi-heavy")
    assert compatible_keys(body) == [heavy]
    assert recommended_key(body) == heavy
    assert body["blockers"] == [CATEGORY_INCOMPATIBLE]
    assert body["warnings"] == []

    record_case(
        case="h", label="categoria asignada incompatible -> CATEGORY_INCOMPATIBLE", competitor_key="a6",
        registration_id=registration_id, tournament_id=tournament, declared_weight_kg="100.00",
        modality="GI", representation="CLUB", classification=body,
    )
    record_b("§9.h.incompatible", True, {"assigned": scenario.surface.key("cat-female-gi"), "blockers": body["blockers"]})


# ---------------------------------------------------------------- §9 solo lectura


@pytest.mark.asyncio(loop_scope="session")
async def test_lectura_de_clasificacion_no_escribe(scenario: ClassificationScenario) -> None:
    """§9: la lectura de clasificacion no reserva idempotencia, no audita y no mueve la inscripcion."""
    dataset = scenario.dataset
    tournament = int(scenario.surface.tournament)
    registration_id, _ = await register(
        scenario, tournament_id=tournament, competitor_key="a4", declared_weight_kg=Decimal("74.50"), modality="GI"
    )

    keys_before = await count_idempotency_keys(dataset.club)
    audit_before = await count_audit_rows(dataset.club)
    row_before = await registration_row(registration_id)

    first = await classify_http(tournament_id=tournament, registration_id=registration_id, token=dataset.owner_token)
    second = await classify_http(tournament_id=tournament, registration_id=registration_id, token=dataset.owner_token)

    assert first == second
    assert await count_idempotency_keys(dataset.club) == keys_before
    assert await count_audit_rows(dataset.club) == audit_before
    assert await registration_row(registration_id) == row_before

    record_b(
        "§9.lectura_no_escribe",
        True,
        {"idempotency_keys": keys_before, "audit_rows": audit_before},
    )
