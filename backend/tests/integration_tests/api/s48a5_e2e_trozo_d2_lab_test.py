# pylint: disable=redefined-outer-name
r"""E2E realista de S4.8a-5 (TROZO D2, §20-§21): categoria legacy y contrato OpenAPI.

Segunda mitad del TROZO D, separada solo por el limite de ``max-module-lines`` de pylint. Son los
dos tramos **independientes** del trozo: no dependen de las escrituras de §17/§18 ni de su orden.

* §20 LEGACY: una categoria **sin reglas** no rompe la clasificacion/readiness/generacion: solo
  aporta compatibilidad **adicional** con el aviso ``CATEGORY_RULES_PARTIAL``, separada del caso
  principal de categoria estructurada (que no lleva ese aviso).
* §21 OPENAPI: el esquema generado en proceso coincide byte a byte con
  ``backend/openapi/openapi.json`` (sin deriva), los endpoints de a-3/a-4a y del E2E estan
  presentes con las schemas del contrato del organizador, y el cliente/types del frontend contiene
  las mismas fichas.

El escenario es de modulo (``trozo_c2_scenario``) y §25 vuelca la evidencia compartida en
``EVIDENCE_D_PATH`` (directorio temporal del sistema, o ``S48A5_EVIDENCE_DIR``).
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal

import pytest
import pytest_asyncio

from bracket.config import config
from tests.integration_tests.api.generate_bracket_http_support import SYNTHETIC_SECRET
from tests.integration_tests.api.s48a5_e2e_paths import BACKEND_ROOT, REPO_ROOT
from tests.integration_tests.api.s48a5_e2e_trozo_b_support import (
    create_registration_http,
    representation_of,
)
from tests.integration_tests.api.s48a5_e2e_trozo_c1_support import (
    assign_category_http,
)
from tests.integration_tests.api.s48a5_e2e_trozo_c2_support import (
    C2Scenario,
    classify_http,
    key_name_map,
    readiness_http,
    trozo_c2_scenario,
)
from tests.integration_tests.api.s48a5_e2e_trozo_d_helpers import (
    compat_keys,
    verdict_of,
    warnings_of,
)
from tests.integration_tests.api.s48a5_e2e_trozo_d_support import (
    EVIDENCE_D,
    record_d,
    write_evidence_d,
)

CATEGORY_RULES_PARTIAL = "CATEGORY_RULES_PARTIAL"

#: el import de ``openapi`` aplica el monkeypatch de pydantic que marca todos los campos
#: requeridos).
_OPENAPI_DUMP = (
    "import json;"
    "from bracket.app import app;"
    "from openapi import openapi;"
    "print(json.dumps(app.openapi(), indent=2, sort_keys=True))"
)


@pytest.fixture(scope="module", autouse=True)
def e2e_write_gate() -> Iterator[None]:
    """Escritura F3 y secreto de idempotencia habilitados durante todo el modulo.

    Perfil, afiliaciones, inscripcion, categoria, override y generacion estan detras de la puerta de
    escritura y del protocolo de idempotencia. No se depende del entorno de CI: el modulo lo fija y
    lo
    restaura (mismo patron que los trozos A/B/C).
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
    startup_and_shutdown_uvicorn_server: None,
    e2e_write_gate: None,
) -> AsyncIterator[C2Scenario]:
    """Escenario A + superficies + torneo de cuadros; al cerrar vuelca la evidencia de §25."""
    async with trozo_c2_scenario() as scenario_:
        try:
            yield scenario_
        finally:
            write_evidence_d()


# ---------------------------------------------------------------- §20 LEGACY


@pytest.mark.asyncio(loop_scope="session")
async def test_20_categoria_legacy_solo_aporta_compatibilidad_parcial(scenario: C2Scenario) -> None:
    """Una categoria legacy sin reglas no rompe el flujo: solo aporta compatibilidad **parcial**.

    En el torneo que **si** tiene la categoria legacy (torneo A), la clasificacion de un competidor
    valido devuelve la categoria estructurada **y** la legacy; la legacy lleva el aviso
    ``CATEGORY_RULES_PARTIAL`` y la estructurada no. En la superficie limpia (sin legacy) el mismo
    competidor solo clasifica en la estructurada, sin ese aviso: el caso legacy queda **separado**
    del
    principal. Se comprueba ademas que la readiness sigue respondiendo (no rompe el flujo).
    """
    dataset = scenario.dataset
    structured = f"si:{dataset.categories['cat-male-gi-light']}"
    legacy = f"si:{dataset.categories['legacy-open']}"

    # Caso con legacy (torneo A).
    reg_legacy_tournament = await create_registration_http(
        tournament_id=int(dataset.tournament),
        token=dataset.owner_token,
        competitor_id=int(dataset.competitors["a2"]),
        representation=representation_of(dataset, "a2")["representation"],
        sports_club_id=representation_of(dataset, "a2")["sports_club_id"],
        affiliation_id=representation_of(dataset, "a2")["affiliation_id"],
        declared_weight_kg=Decimal("76.50"),
        modality="GI",
    )
    classification = await classify_http(
        tournament_id=int(dataset.tournament),
        registration_id=int(reg_legacy_tournament["id"]),
        token=dataset.owner_token,
    )
    keys = compat_keys(classification)
    assert structured in keys, keys
    assert legacy in keys, keys
    assert CATEGORY_RULES_PARTIAL in warnings_of(verdict_of(classification, legacy)), classification
    assert CATEGORY_RULES_PARTIAL not in warnings_of(verdict_of(classification, structured)), (
        classification
    )

    # Solo aporta compatibilidad adicional: la legacy se puede asignar (no rompe) y la readiness
    # responde.
    status_assign, assigned = await assign_category_http(
        tournament_id=int(dataset.tournament),
        registration_id=int(reg_legacy_tournament["id"]),
        token=dataset.owner_token,
        category_key=legacy,
    )
    assert status_assign == 200, assigned
    readiness = await readiness_http(
        tournament_id=int(dataset.tournament), token=dataset.owner_token
    )
    assert isinstance(readiness.get("ready"), bool), readiness

    # Caso principal (sin legacy): el mismo competidor no recibe el aviso parcial.
    reg_clean = await create_registration_http(
        tournament_id=int(scenario.club_surface.tournament),
        token=dataset.owner_token,
        competitor_id=int(dataset.competitors["a2"]),
        representation=representation_of(dataset, "a2")["representation"],
        sports_club_id=representation_of(dataset, "a2")["sports_club_id"],
        affiliation_id=representation_of(dataset, "a2")["affiliation_id"],
        declared_weight_kg=Decimal("76.50"),
        modality="GI",
    )
    clean_classification = await classify_http(
        tournament_id=int(scenario.club_surface.tournament),
        registration_id=int(reg_clean["id"]),
        token=dataset.owner_token,
    )
    clean_names = key_name_map(scenario.club_surface)
    clean_compatible = sorted(
        clean_names[item["category_key"]] for item in clean_classification["compatible_categories"]
    )
    all_clean_warnings = [
        w for item in clean_classification["compatible_categories"] for w in warnings_of(item)
    ]
    assert clean_compatible == ["cat-male-gi-light"], clean_compatible
    assert CATEGORY_RULES_PARTIAL not in all_clean_warnings, all_clean_warnings

    EVIDENCE_D["legacy"] = {
        "torneo_con_legacy": {
            "compatible": sorted(keys),
            "legacy_warnings": warnings_of(verdict_of(classification, legacy)),
            "estructurada_warnings": warnings_of(verdict_of(classification, structured)),
            "readiness_responde": isinstance(readiness.get("ready"), bool),
        },
        "torneo_sin_legacy": {
            "compatible": clean_compatible,
            "warnings": all_clean_warnings,
        },
    }
    record_d("20_categoria_legacy_parcial", True, EVIDENCE_D["legacy"])


# ---------------------------------------------------------------- §21 OPENAPI / CONTRATO


@pytest.mark.asyncio(loop_scope="session")
async def test_21a_openapi_sin_deriva_y_endpoints_presentes(scenario: C2Scenario) -> None:
    """El esquema generado coincide con ``backend/openapi/openapi.json`` y cubre los endpoints F3
    .
    """
    # Regeneracion con el comando real del repo (importando ``openapi`` para aplicar el monkeypatch
    # pydantic de campos requeridos) en un PROCESO LIMPIO, y comparacion con el fichero versionado.
    proc = subprocess.run(
        [sys.executable, "-c", _OPENAPI_DUMP], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    on_disk = (BACKEND_ROOT / "openapi" / "openapi.json").read_text(encoding="utf-8")
    regenerated = proc.stdout.strip()
    drift = on_disk.strip() != regenerated
    assert not drift, "DERIVA REAL: openapi/openapi.json no coincide con la regeneracion del repo"
    schema = json.loads(on_disk)

    paths = schema["paths"]
    required = (
        "/clubs/{club_id}/competitors",
        "/clubs/{club_id}/competitors/{competitor_id}",
        "/clubs/{club_id}/competitors/{competitor_id}/profile",
        "/clubs/{club_id}/competitors/{competitor_id}/affiliations",
        "/clubs/{club_id}/competitors/{competitor_id}/affiliations/{affiliation_id}/primary",
        "/clubs/{club_id}/competitors/{competitor_id}/affiliations/{affiliation_id}/close",
        "/clubs/{club_id}/competitors/{competitor_id}/independence",
        "/clubs/{club_id}/sports-clubs",
        "/clubs/{club_id}/sports-clubs/{sports_club_id}/logo",
        "/clubs/{club_id}/belts",
        "/tournaments/{tournament_id}/registrations",
        "/tournaments/{tournament_id}/registrations/{registration_id}",
        "/tournaments/{tournament_id}/registrations/{registration_id}/classification",
        "/tournaments/{tournament_id}/registrations/{registration_id}/category",
        "/tournaments/{tournament_id}/generate-bracket",
        "/public/tournaments/{tournament_id}/schedule",
    )
    missing = [path for path in required if path not in paths]
    assert not missing, missing

    schemas = schema["components"]["schemas"]
    assert "date_of_birth" in schemas["CompetitorResponse"]["properties"]
    assert "declared_weight_kg" in schemas["RegistrationResponse"]["properties"]
    assert "date_of_birth" not in json.dumps(schemas["TournamentScheduleResponse"])

    EVIDENCE_D["openapi"] = {
        "drift": drift,
        "paths_requeridos_presentes": len(required),
        "paths_totales": len(paths),
        "CompetitorResponse_incluye_date_of_birth": True,
        "RegistrationResponse_incluye_declared_weight_kg": True,
        "schedule_publico_sin_date_of_birth": True,
    }
    record_d("21_openapi_sin_deriva", not drift, EVIDENCE_D["openapi"])


@pytest.mark.asyncio(loop_scope="session")
async def test_21b_cliente_frontend_corresponde_al_contrato(scenario: C2Scenario) -> None:
    """El cliente/types generados del frontend contienen las mismas fichas que el backend de HEAD
    .
    """
    root = REPO_ROOT / "frontend" / "src" / "openapi"
    types = (root / "types.gen.ts").read_text(encoding="utf-8")
    sdk = (root / "sdk.gen.ts").read_text(encoding="utf-8")

    assert "CompetitorResponse" in types and "date_of_birth" in types
    assert "RegistrationResponse" in types and "declared_weight_kg" in types
    assert "TournamentScheduleResponse" in types

    # Operaciones F3 del E2E presentes en el cliente generado.
    for operation in (
        "getPublicTournamentScheduleEndpointPublicTournamentsTournamentIdScheduleGet",
        "getTournamentScheduleEndpointTournamentsTournamentIdScheduleGet",
    ):
        assert operation in sdk, operation

    EVIDENCE_D["openapi"]["cliente_frontend"] = {
        "types_contienen": [
            "CompetitorResponse",
            "RegistrationResponse",
            "TournamentScheduleResponse",
        ],
        "sdk_operaciones": [
            "getPublicTournamentScheduleEndpointPublicTournamentsTournamentIdScheduleGet",
        ],
        "fuente": "frontend/src/openapi/types.gen.ts + sdk.gen.ts",
    }
    record_d("21_cliente_frontend", True, EVIDENCE_D["openapi"]["cliente_frontend"])
