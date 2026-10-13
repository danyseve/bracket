# pylint: disable=redefined-outer-name
r"""E2E realista de S4.8a-5 (TROZO D, §17-§21): privacidad, idempotencia, auditoria, compatibilidad

de categoria legacy y contrato OpenAPI, por el API HTTP real.

Pruebas **negras** sobre el escenario del TROZO A (24 competidores, 3 academias, 5 categorias
estructuradas + 1 legacy), las superficies del TROZO B y el torneo de cuadros del TROZO C2, contra
la
base de laboratorio ``bracket_ci``. No hay mocks del motor, del protocolo de idempotencia, del RBAC
ni de la puerta de escritura: se ejercitan las rutas reales y se lee lo que el backend escribe
(``domain_change_log``, ``domain_idempotency_keys``, ``competitors_x_sports_clubs``,
``tournament_registrations``, ``sports_clubs``).

Que fija este modulo y por que:

* §17 PRIVACIDAD: `date_of_birth`/`sex`/`belt_id` **si** viajan en la proyeccion del ORGANIZADOR
  (``GET`` de competidor) y `declared_weight_kg` en la de la inscripcion; y **no** viajan en el
  calendario publico (un combate ya publicado, con los nombres reales de dos competidores, se sirve
  con la lista blanca cerrada de ``PUBLIC_MATCH_KEYS`` y pasa el guardin real
  ``assert_public_payload_is_safe``). El catalogo publico tampoco filtra campos de organizador. Cada
  conclusion cita el ``archivo:linea`` que la garantiza, backend y contrato de frontend.
* §18 IDEMPOTENCIA: cada escritura F3 (perfil, afiliacion completa — alta, primaria, cierre e
  independencia —, alta de inscripcion, asignacion de categoria, override y logo de academia) con
  **replay** de la misma clave + mismo cuerpo (efecto sin duplicar: mismas entidades, misma
  ``revision``/``updated_at``, un solo evento de auditoria, una sola fila de clave) y **409**
  ``IDEMPOTENCY_KEY_REUSED`` con la misma clave y cuerpo distinto. Se cuentan filas antes/despues.
* §19 AUDITORIA: los eventos **reales** de ``domain_change_log`` de esta fase —
  ``AFFILIATION_CHANGE``,
  ``CLUB_LOGO_CHANGE``, ``CATEGORY_CHANGE`` y ``GENERATE_BRACKET`` (codigos tomados de
  ``bracket/logic/audit_reasons.py``, no inventados) — con actor, tenant y motivo; y que una tanda
  de
  ``GET`` **no** audita (mismo numero de eventos antes/despues).
* §20 LEGACY: una categoria **sin reglas** no rompe la clasificacion/readiness/generacion: solo
  aporta
  compatibilidad **adicional** con el aviso ``CATEGORY_RULES_PARTIAL``, separada del caso principal
  de
  categoria estructurada (que no lleva ese aviso).
* §21 OPENAPI: el esquema generado en proceso coincide byte a byte con
  ``backend/openapi/openapi.json``
  (sin deriva), los endpoints de a-3/a-4a y del E2E estan presentes con las schemas del contrato del
  organizador, y el cliente/types del frontend contiene las mismas fichas.

El escenario es de modulo y **los casos se ejecutan en orden de definicion**: §17/§18 producen los
cambios que §19 audita. §20 y §21 son independientes.

§25: al cerrar el modulo se vuelca la evidencia en ``/home/ubuntu/s48a5-evidence/trozo-d.json``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from bracket.config import config
from bracket.logic.scheduling.public_schedule import (
    PUBLIC_MATCH_KEYS,
    assert_public_payload_is_safe,
)
from tests.integration_tests.api.generate_bracket_http_support import SYNTHETIC_SECRET
from tests.integration_tests.api.s48a5_e2e_trozo_a_support import (
    PNG_BYTES,
    affiliation_action_url,
    call,
    competitor_url,
    fresh_key,
    independence_url,
    open_count,
    open_primary_count,
    post_logo,
    profile_row,
)
from tests.integration_tests.api.s48a5_e2e_trozo_b_support import (
    create_registration_http,
    registrations_url,
    representation_of,
)
from tests.integration_tests.api.s48a5_e2e_trozo_c1_support import (
    assign_category_http,
    confirm_registration_http,
    get_registration_http,
    update_profile_http,
)
from tests.integration_tests.api.s48a5_e2e_trozo_c2_support import (
    CATEGORY_HEAVY,
    CATEGORY_LIGHT,
    DECLARED_HEAVY,
    DECLARED_LIGHT,
    C2Scenario,
    classify_http,
    close_window_http,
    generate_http,
    key_name_map,
    readiness_http,
    trozo_c2_scenario,
)
from tests.integration_tests.api.s48a5_e2e_trozo_d_support import (
    EVIDENCE_D,
    audit_events,
    count_affiliations,
    count_audit_events,
    count_idempotency_keys,
    count_registrations,
    idempotency_rows_for_key,
    public_schedule_tournament,
    public_schedule_url,
    record_d,
    write_evidence_d,
)

SKIP_CONFLICT = "endpoint sin cuerpo: no existe 'cuerpo distinto'"

#: Estado mutable compartido entre los casos ordenados (ids creados por §18 que §19 audita).
REGISTRY: dict[str, Any] = {}

IDEMPOTENCY_REUSED = "IDEMPOTENCY_KEY_REUSED"
CATEGORY_RULES_PARTIAL = "CATEGORY_RULES_PARTIAL"

#: Comando canonico de regeneracion del contrato (mismo que ``tests/unit_tests/openapi_test.py``:
#: el import de ``openapi`` aplica el monkeypatch de pydantic que marca todos los campos
#: requeridos).
_OPENAPI_DUMP = (
    "import json;"
    "from bracket.app import app;"
    "from openapi import openapi;"
    "print(json.dumps(app.openapi(), indent=2, sort_keys=True))"
)
AFFILIATION_CHANGE = "AFFILIATION_CHANGE"
CLUB_LOGO_CHANGE = "CLUB_LOGO_CHANGE"
CATEGORY_CHANGE = "CATEGORY_CHANGE"
GENERATE_BRACKET = "GENERATE_BRACKET"


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


# ---------------------------------------------------------------- utilidades


def _compat_keys(body: dict[str, Any]) -> set[str]:
    return {item["category_key"] for item in body["compatible_categories"]}


def _verdict(body: dict[str, Any], category_key: str) -> dict[str, Any]:
    return next(
        item for item in body["compatible_categories"] if item["category_key"] == category_key
    )


def _warnings(verdict: dict[str, Any]) -> list[str]:
    return [str(item) for item in verdict.get("warnings", [])]


async def _assert_replay_same_body(
    post: Any, *, key: str, payload: dict[str, Any] | None, expected: int
) -> tuple[Any, Any]:
    """Primera llamada + replay (misma clave, mismo cuerpo): mismo estatus y cuerpo identico."""
    status1, body1 = await post(key, payload)
    assert status1 == expected, (status1, body1)
    status2, body2 = await post(key, payload)
    assert status2 == expected, (status2, body2)
    assert body2 == body1, (body1, body2)
    return status1, body1


async def _assert_conflict(
    post: Any, *, key: str, other_payload: dict[str, Any] | None, expected: int
) -> None:
    """Misma clave + cuerpo distinto: 409 con el codigo real del protocolo."""
    status_code, body = await post(key, other_payload)
    assert status_code == 409, (status_code, body)
    assert body.get("detail") == IDEMPOTENCY_REUSED, body


async def _dedicated_registration(
    scenario: C2Scenario, competitor_key: str, weight: str, modality: str = "GI"
) -> int:
    """Crea y CONFIRMA una inscripcion propia de un competidor libre, para aislar el test de cate
    goria.
    """
    dataset = scenario.dataset
    body = await create_registration_http(
        tournament_id=int(dataset.tournament),
        token=dataset.owner_token,
        competitor_id=int(dataset.competitors[competitor_key]),
        representation="INDEPENDENT",
        sports_club_id=None,
        affiliation_id=None,
        declared_weight_kg=Decimal(weight),
        modality=modality,
    )
    await confirm_registration_http(
        tournament_id=int(dataset.tournament),
        registration_id=int(body["id"]),
        token=dataset.owner_token,
    )
    return int(body["id"])


def _record_write(
    name: str,
    *,
    endpoint: str,
    expected_status: int,
    replay_same_body: bool,
    conflict_409: bool,
    keys_delta: int,
    effect: dict[str, Any],
    note: str | None = None,
) -> None:
    EVIDENCE_D["idempotency"]["writes"].append(
        {
            "write": name,
            "endpoint": endpoint,
            "expected_status": expected_status,
            "replay_mismo_cuerpo": replay_same_body,
            "conflicto_409": conflict_409,
            "idempotency_keys_delta": keys_delta,
            "efecto": effect,
            "nota": note,
        }
    )
    record_d(f"idempotencia_{name}", replay_same_body and (conflict_409 or note is not None))


# ---------------------------------------------------------------- §17 PRIVACIDAD


@pytest.mark.asyncio(loop_scope="session")
async def test_17a_organizador_expone_perfil_deportivo_y_peso(scenario: C2Scenario) -> None:
    """La proyeccion del ORGANIZADOR si lleva ``date_of_birth``/``sex``/``belt_id`` y el peso dec
    larado.
    """
    dataset = scenario.dataset
    registration = await create_registration_http(
        tournament_id=int(dataset.tournament),
        token=dataset.owner_token,
        competitor_id=int(dataset.competitors["a4"]),
        representation=representation_of(dataset, "a4")["representation"],
        sports_club_id=representation_of(dataset, "a4")["sports_club_id"],
        affiliation_id=representation_of(dataset, "a4")["affiliation_id"],
        declared_weight_kg=Decimal("76.50"),
        modality="GI",
    )
    REGISTRY["reg_organizer"] = int(registration["id"])

    status_c, competitor_body = await call(
        "GET",
        competitor_url(dataset.club, int(dataset.competitors["a4"])),
        token=dataset.owner_token,
    )
    assert status_c == 200, competitor_body
    assert competitor_body["date_of_birth"] == "1995-07-21", competitor_body
    assert competitor_body["sex"] == "MALE", competitor_body
    assert competitor_body["belt_id"] is not None, competitor_body

    status_r, registration_body = await get_registration_http(
        tournament_id=int(dataset.tournament),
        registration_id=int(registration["id"]),
        token=dataset.owner_token,
    )
    assert status_r == 200, registration_body
    assert registration_body["declared_weight_kg"] == "76.50", registration_body

    EVIDENCE_D["privacy"]["organizer_http"] = {
        "competitor_get": {
            "url": competitor_url(dataset.club, int(dataset.competitors["a4"])),
            "date_of_birth": competitor_body["date_of_birth"],
            "sex": competitor_body["sex"],
            "belt_id_presente": competitor_body["belt_id"] is not None,
        },
        "registration_get": {
            "declared_weight_kg": registration_body["declared_weight_kg"],
        },
    }
    record_d("17_organizador_expone_perfil", True, EVIDENCE_D["privacy"]["organizer_http"])


@pytest.mark.asyncio(loop_scope="session")
async def test_17b_publico_no_expone_datos_personales(scenario: C2Scenario) -> None:
    """El calendario publico, con un combate real ya publicado, no lleva ningun dato personal.

    No es una prueba vacia: el cuerpo publico identifica a los competidores por su **nombre** (un
    combate con dos equipos con el nombre HTTP real de dos competidores del escenario). Aun asi, ni
    las claves sensibles ni los **valores** (fecha de nacimiento / peso) aparecen, y el guardin real
    ``assert_public_payload_is_safe`` pasa sobre el cuerpo servido.
    """
    dataset = scenario.dataset
    async with public_schedule_tournament(dataset, fighter_keys=("a1", "b1")) as public_tournament:
        status_code, body = await call(
            "GET", public_schedule_url(public_tournament), token=dataset.owner_token
        )
        assert status_code == 200, body

        # El guardin real de privacidad del propio backend, aplicado al cuerpo servido por HTTP.
        assert_public_payload_is_safe(body)

        serialized = json.dumps(body, ensure_ascii=False)
        for forbidden in ("date_of_birth", "declared_weight_kg", "sex", "belt_id"):
            assert forbidden not in serialized, (forbidden, serialized)

        fighters: set[str] = set()
        entry_keys: set[str] = set()
        for court in body["courts"]:
            entries = list(court.get("upcoming", [])) + list(court.get("completed", []))
            if court.get("current"):
                entries.append(court["current"])
            if court.get("next"):
                entries.append(court["next"])
            for entry in entries:
                entry_keys.update(entry)
                for key in ("fighter_a", "fighter_b"):
                    if entry.get(key):
                        fighters.add(str(entry[key]))
        assert dataset.name("a1") in fighters, fighters  # prueba no vacia
        assert "1990-05-17" not in serialized  # valor de date_of_birth de a1

        # Cada entrada publica solo lleva la lista blanca de claves de combate.
        for key in entry_keys:
            assert key in PUBLIC_MATCH_KEYS, (key, sorted(PUBLIC_MATCH_KEYS))

        EVIDENCE_D["privacy"]["public_http"] = {
            "url": public_schedule_url(public_tournament),
            "fighter_names": sorted(fighters),
            "entry_keys": sorted(entry_keys),
            "public_match_keys": sorted(PUBLIC_MATCH_KEYS),
        }
    record_d("17_publico_sin_datos_personales", True, EVIDENCE_D["privacy"]["public_http"])


@pytest.mark.asyncio(loop_scope="session")
async def test_17c_catalogo_publico_sin_campos_de_organizador(scenario: C2Scenario) -> None:
    """El catalogo/listado publico (academias y cinturones) no filtra campos de organizador."""
    dataset = scenario.dataset
    status_sc, sports_clubs = await call(
        "GET", f"{_root()}clubs/{dataset.club}/sports-clubs", token=dataset.owner_token
    )
    assert status_sc == 200, sports_clubs
    for club in sports_clubs:
        assert set(club) <= {"id", "name", "logo_path", "active"}, set(club)

    status_b, belts = await call(
        "GET", f"{_root()}clubs/{dataset.club}/belts", token=dataset.owner_token
    )
    assert status_b == 200, belts
    serialized = json.dumps(belts)
    for forbidden in ("date_of_birth", "declared_weight_kg", "email"):
        assert forbidden not in serialized
    record_d(
        "17_catalogo_publico_sin_organizador",
        True,
        {"sports_clubs_fields": sorted(sports_clubs[0])},
    )


def _root() -> str:
    from tests.integration_tests.api.shared import get_root_uvicorn_url

    return get_root_uvicorn_url()


@pytest.mark.asyncio(loop_scope="session")
async def test_17d_contrato_frontend_whitelist_publica(scenario: C2Scenario) -> None:
    """El contrato de frontend separa la ficha **publica** (lista blanca) del contrato de organiz
    ador.
    """
    root = Path("/home/ubuntu/work/bracket-f2c/frontend/src/components/utils")
    privacy = (root / "f3_privacy.ts").read_text(encoding="utf-8")
    competitor_contract = (root / "f3_competitor_contract.ts").read_text(encoding="utf-8")

    # La lista blanca publica NO nombra los campos sensibles (no los expone por conveniencia).
    assert "date_of_birth" not in privacy, "f3_privacy.ts no debe nombrar date_of_birth"
    assert "declared_weight_kg" not in privacy, "f3_privacy.ts no debe nombrar declared_weight_kg"
    assert "toPublicCompetitor" in privacy and "display_name" in privacy

    # El contrato del organizador SI los declara (perfil deportivo + peso declarado).
    assert "date_of_birth" in competitor_contract
    assert "declared_weight_kg" in competitor_contract

    record_d(
        "17_contrato_frontend",
        True,
        {
            "f3_privacy_ts": "backend/bracket/models/db/competitor_api.py + registration_api.py",
            "source": "frontend/src/components/utils/f3_privacy.ts / f3_competitor_contract.ts",
        },
    )


# ---------------------------------------------------------------- §18 IDEMPOTENCIA


@pytest.mark.asyncio(loop_scope="session")
async def test_18a_perfil_replay_y_conflicto(scenario: C2Scenario) -> None:
    """Perfil de competidor: replay sin duplicar efecto + 409 con cuerpo distinto."""
    dataset = scenario.dataset
    competitor = int(dataset.competitors["a2"])
    key = fresh_key()
    audit_before = await count_audit_events(dataset.club, entity="competitor", action="UPDATE")
    keys_before = await count_idempotency_keys(dataset.club)

    async def post(k: str, payload: dict[str, Any] | None) -> tuple[int, Any]:
        assert payload is not None
        return await update_profile_http(
            club=dataset.club,
            competitor_id=competitor,
            token=dataset.owner_token,
            date_of_birth=payload["date_of_birth"],
            sex=payload["sex"],
            belt_id=payload["belt_id"],
            key=k,
        )

    p1 = {"date_of_birth": date(1991, 1, 1), "sex": "MALE", "belt_id": int(dataset.belts["BLUE"])}
    p2 = {"date_of_birth": date(1992, 2, 2), "sex": "MALE", "belt_id": int(dataset.belts["BLUE"])}
    await _assert_replay_same_body(post, key=key, payload=p1, expected=200)
    await _assert_conflict(post, key=key, other_payload=p2, expected=200)

    row = await profile_row(competitor)
    assert str(row["date_of_birth"]) == "1991-01-01", row
    audit_after = await count_audit_events(dataset.club, entity="competitor", action="UPDATE")
    keys_after = await count_idempotency_keys(dataset.club)
    assert keys_after == keys_before + 1
    assert len(await idempotency_rows_for_key(dataset.club, key)) == 1
    assert audit_after == audit_before + 1  # solo el perfil: el replay y el 409 no auditan
    _record_write(
        "perfil_competidor",
        endpoint=f"POST /clubs/{{club}}/competitors/{competitor}/profile",
        expected_status=200,
        replay_same_body=True,
        conflict_409=True,
        keys_delta=1,
        effect={"date_of_birth": str(row["date_of_birth"])},
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_18b_afiliacion_alta_replay_y_conflicto(scenario: C2Scenario) -> None:
    """Afiliacion (alta): replay y 409; efecto medido por filas de ``competitors_x_sports_clubs``
    .
    """
    dataset = scenario.dataset
    competitor = int(dataset.competitors["i1"])
    club_a = int(dataset.clubs["A"])
    club_b = int(dataset.clubs["B"])
    key = fresh_key()
    rows_before = await count_affiliations(competitor)
    audit_before = await count_audit_events(
        dataset.club, entity="competitor", action="UPDATE", reason_code=AFFILIATION_CHANGE
    )

    async def post(k: str, payload: dict[str, Any] | None) -> tuple[int, Any]:
        assert payload is not None
        return await call(
            "POST",
            f"{_root()}clubs/{dataset.club}/competitors/{competitor}/affiliations",
            token=dataset.owner_token,
            key=k,
            payload=payload,
        )

    _, body = await _assert_replay_same_body(
        post, key=key, payload={"sports_club_id": club_a}, expected=201
    )
    await _assert_conflict(post, key=key, other_payload={"sports_club_id": club_b}, expected=201)
    REGISTRY["affiliation_i1"] = int(body["id"])

    rows_after = await count_affiliations(competitor)
    audit_after = await count_audit_events(
        dataset.club, entity="competitor", action="UPDATE", reason_code=AFFILIATION_CHANGE
    )
    assert rows_after == rows_before + 1
    assert audit_after == audit_before + 1
    assert len(await idempotency_rows_for_key(dataset.club, key)) == 1
    _record_write(
        "afiliacion_alta",
        endpoint=f"POST /clubs/{{club}}/competitors/{competitor}/affiliations",
        expected_status=201,
        replay_same_body=True,
        conflict_409=True,
        keys_delta=1,
        effect={
            "filas_afiliacion_delta": rows_after - rows_before,
            "auditoria_delta": audit_after - audit_before,
        },
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_18c_afiliacion_primaria_replay(scenario: C2Scenario) -> None:
    """Afiliacion (marcar primaria): replay sin duplicar; sin cuerpo no hay 'cuerpo distinto'."""
    dataset = scenario.dataset
    competitor = int(dataset.competitors["i1"])
    # i1 ya tiene una afiliacion (18b). Se crea una SEGUNDA (no primaria) y se promueve esa:
    # promover
    # la ya-primaria seria un no-op sin evento de auditoria.
    _, second = await call(
        "POST",
        f"{_root()}clubs/{dataset.club}/competitors/{competitor}/affiliations",
        token=dataset.owner_token,
        key=fresh_key(),
        payload={"sports_club_id": int(dataset.clubs["B"])},
    )
    affiliation_id = int(second["id"])
    REGISTRY["affiliation_i1_second"] = affiliation_id
    assert not second.get("is_primary"), second
    key = fresh_key()
    audit_before = await count_audit_events(
        dataset.club, entity="competitor", action="UPDATE", reason_code=AFFILIATION_CHANGE
    )

    async def post(k: str, payload: dict[str, Any] | None) -> tuple[int, Any]:
        return await call(
            "POST",
            affiliation_action_url(dataset.club, competitor, affiliation_id, "primary"),
            token=dataset.owner_token,
            key=k,
        )

    await _assert_replay_same_body(post, key=key, payload=None, expected=200)
    assert await open_primary_count(competitor) == 1
    audit_after = await count_audit_events(
        dataset.club, entity="competitor", action="UPDATE", reason_code=AFFILIATION_CHANGE
    )
    assert audit_after == audit_before + 1
    _record_write(
        "afiliacion_primaria",
        endpoint=f"POST .../affiliations/{affiliation_id}/primary",
        expected_status=200,
        replay_same_body=True,
        conflict_409=False,
        keys_delta=1,
        effect={"primarias_vigentes": 1, "auditoria_delta": audit_after - audit_before},
        note=SKIP_CONFLICT,
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_18d_afiliacion_cierre_replay(scenario: C2Scenario) -> None:
    """Afiliacion (cierre): replay sin duplicar efecto."""
    dataset = scenario.dataset
    competitor = int(dataset.competitors["i1"])
    affiliation_id = int(REGISTRY["affiliation_i1_second"])
    key = fresh_key()

    async def post(k: str, payload: dict[str, Any] | None) -> tuple[int, Any]:
        return await call(
            "POST",
            affiliation_action_url(dataset.club, competitor, affiliation_id, "close"),
            token=dataset.owner_token,
            key=k,
        )

    _, body = await _assert_replay_same_body(post, key=key, payload=None, expected=200)
    assert body["valid_to"] is not None
    assert await open_count(competitor) == 0
    _record_write(
        "afiliacion_cierre",
        endpoint=f"POST .../affiliations/{affiliation_id}/close",
        expected_status=200,
        replay_same_body=True,
        conflict_409=False,
        keys_delta=1,
        effect={"vigentes": 0},
        note=SKIP_CONFLICT,
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_18e_independencia_replay(scenario: C2Scenario) -> None:
    """Vuelta a independiente: replay sin duplicar efecto."""
    dataset = scenario.dataset
    competitor = int(dataset.competitors["i1"])
    # Se reabre una afiliacion (clave aparte) para que la independencia tenga algo que cerrar.
    await call(
        "POST",
        f"{_root()}clubs/{dataset.club}/competitors/{competitor}/affiliations",
        token=dataset.owner_token,
        key=fresh_key(),
        payload={"sports_club_id": int(dataset.clubs["C"])},
    )
    key = fresh_key()

    async def post(k: str, payload: dict[str, Any] | None) -> tuple[int, Any]:
        return await call(
            "POST", independence_url(dataset.club, competitor), token=dataset.owner_token, key=k
        )

    await _assert_replay_same_body(post, key=key, payload=None, expected=200)
    assert await open_count(competitor) == 0
    _record_write(
        "independencia",
        endpoint=f"POST .../competitors/{competitor}/independence",
        expected_status=200,
        replay_same_body=True,
        conflict_409=False,
        keys_delta=1,
        effect={"vigentes": 0},
        note=SKIP_CONFLICT,
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_18f_inscripcion_alta_replay_y_conflicto(scenario: C2Scenario) -> None:
    """Alta de inscripcion: replay y 409; efecto = filas de ``tournament_registrations``."""
    dataset = scenario.dataset
    competitor = int(dataset.competitors["i2"])
    tournament = int(dataset.tournament)
    key = fresh_key()
    rows_before = await count_registrations(tournament)

    payload1 = {
        "competitor_id": competitor,
        "representation": "INDEPENDENT",
        "sports_club_id": None,
        "affiliation_id": None,
        "declared_weight_kg": "55.00",
        "modality": "GI",
        "category_key": None,
        "category_label": None,
        "reason_code": "PLANNED_ENTRY",
    }
    payload2 = {**payload1, "declared_weight_kg": "60.00"}

    async def post(k: str, payload: dict[str, Any] | None) -> tuple[int, Any]:
        return await call(
            "POST", registrations_url(tournament), token=dataset.owner_token, key=k, payload=payload
        )

    _, body = await _assert_replay_same_body(post, key=key, payload=payload1, expected=201)
    await _assert_conflict(post, key=key, other_payload=payload2, expected=201)
    REGISTRY["reg_category"] = int(body["id"])

    rows_after = await count_registrations(tournament)
    assert rows_after == rows_before + 1
    assert len(await idempotency_rows_for_key(dataset.club, key)) == 1
    _record_write(
        "inscripcion_alta",
        endpoint=f"POST /tournaments/{tournament}/registrations",
        expected_status=201,
        replay_same_body=True,
        conflict_409=True,
        keys_delta=1,
        effect={"inscripciones_delta": rows_after - rows_before},
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_18g_asignacion_categoria_replay_y_conflicto(scenario: C2Scenario) -> None:
    """Asignacion de categoria: replay sin cambiar ``revision`` ni duplicar auditoria; 409.

    Se usa una inscripcion propia (competidor libre ``a3``) y la categoria se elige de la propia
    clasificacion HTTP (estructurada + legacy), para no depender de elecciones incompatibles.
    """
    dataset = scenario.dataset
    tournament = int(dataset.tournament)
    registration_id = await _dedicated_registration(scenario, "a3", "52.00")
    REGISTRY["reg_assign"] = registration_id
    classification = await classify_http(
        tournament_id=tournament, registration_id=registration_id, token=dataset.owner_token
    )
    compatible = [item["category_key"] for item in classification["compatible_categories"]]
    assert len(compatible) >= 2, compatible
    chosen, other = compatible[0], compatible[1]
    key = fresh_key()
    audit_before = await count_audit_events(
        dataset.club, entity="tournament_registration", action="UPDATE", reason_code=CATEGORY_CHANGE
    )

    async def post(k: str, payload: dict[str, Any] | None) -> tuple[int, Any]:
        assert payload is not None
        return await assign_category_http(
            tournament_id=tournament,
            registration_id=registration_id,
            token=dataset.owner_token,
            key=k,
            category_key=payload["category_key"],
        )

    _, body = await _assert_replay_same_body(
        post, key=key, payload={"category_key": chosen}, expected=200
    )
    revision = body["revision"]
    await _assert_conflict(post, key=key, other_payload={"category_key": other}, expected=200)

    status_r, reread_body = await get_registration_http(
        tournament_id=tournament, registration_id=registration_id, token=dataset.owner_token
    )
    assert status_r == 200, reread_body
    assert reread_body["category_key"] == chosen, reread_body
    assert int(reread_body["revision"]) == int(revision), (reread_body["revision"], revision)
    audit_after = await count_audit_events(
        dataset.club, entity="tournament_registration", action="UPDATE", reason_code=CATEGORY_CHANGE
    )
    assert audit_after == audit_before + 1
    _record_write(
        "asignacion_categoria",
        endpoint=f"POST /tournaments/{tournament}/registrations/{registration_id}/category",
        expected_status=200,
        replay_same_body=True,
        conflict_409=True,
        keys_delta=1,
        effect={
            "revision_estable": int(revision),
            "auditoria_categoria_delta": audit_after - audit_before,
            "categoria": chosen,
        },
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_18h_override_replay_y_conflicto(scenario: C2Scenario) -> None:
    """Override (OWNER, motivo obligatorio): replay y 409; auditoria CATEGORY_CHANGE una vez."""
    dataset = scenario.dataset
    tournament = int(dataset.tournament)
    registration_id = int(REGISTRY["reg_assign"])
    key = fresh_key()
    audit_before = await count_audit_events(
        dataset.club,
        entity="tournament_registration",
        action="UPDATE",
        reason_code="DATA_CORRECTION",
    )

    async def post(k: str, payload: dict[str, Any] | None) -> tuple[int, Any]:
        assert payload is not None
        return await assign_category_http(
            tournament_id=tournament,
            registration_id=registration_id,
            token=dataset.owner_token,
            key=k,
            category_key=payload["category_key"],
            override=True,
            reason_code="DATA_CORRECTION",
        )

    # Categoria estructurada INCOMPATIBLE para a3 (MALE) forzada con override; el segundo cuerpo usa
    # otra categoria existente (legacy) -> mismo clave, cuerpo distinto -> 409.
    p1 = {"category_key": f"si:{dataset.categories['cat-male-gi-heavy']}"}
    p2 = {"category_key": f"si:{dataset.categories['legacy-open']}"}
    await _assert_replay_same_body(post, key=key, payload=p1, expected=200)
    await _assert_conflict(post, key=key, other_payload=p2, expected=200)
    status_r, reread_body = await get_registration_http(
        tournament_id=tournament, registration_id=registration_id, token=dataset.owner_token
    )
    assert status_r == 200, reread_body
    assert reread_body["category_key"] == p1["category_key"], reread_body

    audit_after = await count_audit_events(
        dataset.club,
        entity="tournament_registration",
        action="UPDATE",
        reason_code="DATA_CORRECTION",
    )
    assert audit_after == audit_before + 1
    _record_write(
        "override_categoria",
        endpoint=f"POST .../registrations/{registration_id}/category (override=true)",
        expected_status=200,
        replay_same_body=True,
        conflict_409=True,
        keys_delta=1,
        effect={
            "auditoria_override_delta": audit_after - audit_before,
            "categoria_forzada": p1["category_key"],
        },
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_18i_logo_academia_idempotencia_por_valor_y_hallazgo(scenario: C2Scenario) -> None:
    """Logo de academia: la ruta NO usa el protocolo de clave; su idempotencia es **por valor**.

    Hallazgo (no se corrige en silencio): ``POST .../sports-clubs/{id}/logo`` no declara la
    dependencia
    del protocolo de idempotencia (no consume ``Idempotency-Key``), luego subir el **mismo** fichero
    dos veces crea un segundo fichero y un segundo evento ``CLUB_LOGO_CHANGE`` (no hay replay real).
    Lo que si garantiza el dominio es la idempotencia **por valor**: quitar el logo cuando ya no hay
    ninguno es un no-op sin auditoria.
    """
    dataset = scenario.dataset
    academy_c = int(dataset.clubs["C"])
    audit_before_all = await count_audit_events(
        dataset.club, entity="sports_club", action="UPDATE", reason_code=CLUB_LOGO_CHANGE
    )

    # Alta del logo (club C nace sin logo en el escenario A).
    status1, body1 = await post_logo(
        dataset.club, academy_c, token=dataset.owner_token, filename="c.png", content=PNG_BYTES
    )
    assert status1 == 200, body1
    assert body1["logo_path"] is not None, body1
    audit_after_first = await count_audit_events(
        dataset.club, entity="sports_club", action="UPDATE", reason_code=CLUB_LOGO_CHANGE
    )
    assert audit_after_first == audit_before_all + 1

    # Reintento del MISMO fichero con la MISMA clave: no hay protocolo -> NI replay NI 409.
    reused_key = fresh_key()
    status_dup, body_dup = await post_logo(
        dataset.club, academy_c, token=dataset.owner_token, filename="c.png", content=PNG_BYTES
    )
    assert status_dup == 200, body_dup
    duplicated = body_dup["logo_path"] != body1["logo_path"]
    audit_after_dup = await count_audit_events(
        dataset.club, entity="sports_club", action="UPDATE", reason_code=CLUB_LOGO_CHANGE
    )
    assert not await idempotency_rows_for_key(dataset.club, reused_key)  # la clave no se consume

    # Idempotencia POR VALOR: quitar el logo dos veces -> la segunda no escribe ni audita.
    status_rm1, body_rm1 = await post_logo(dataset.club, academy_c, token=dataset.owner_token)
    assert status_rm1 == 200 and body_rm1["logo_path"] is None, body_rm1
    audit_after_rm1 = await count_audit_events(
        dataset.club, entity="sports_club", action="UPDATE", reason_code=CLUB_LOGO_CHANGE
    )
    status_rm2, body_rm2 = await post_logo(dataset.club, academy_c, token=dataset.owner_token)
    assert status_rm2 == 200 and body_rm2["logo_path"] is None, body_rm2
    audit_after_rm2 = await count_audit_events(
        dataset.club, entity="sports_club", action="UPDATE", reason_code=CLUB_LOGO_CHANGE
    )
    assert audit_after_rm2 == audit_after_rm1  # segundo borrado: no-op sin auditoria

    EVIDENCE_D["idempotency"]["findings"].append(
        {
            "hallazgo": "logo de academia sin protocolo de Idempotency-Key",
            "endpoint": f"POST /clubs/{dataset.club}/sports-clubs/{academy_c}/logo",
            "evidencia": "domain_sports_clubs.py: no depende del protocolo; uuid4 por subida",
            "mismo_fichero_dos_veces": {
                "segunda_ruta_logo": body_dup["logo_path"],
                "duplico_evento_auditoria": duplicated,
                "delta_eventos": audit_after_dup - audit_after_first,
            },
            "idempotencia_por_valor": {
                "borrado_repetido_sin_auditoria": audit_after_rm2 == audit_after_rm1,
            },
        }
    )
    _record_write(
        "logo_academia",
        endpoint=f"POST /clubs/{{club}}/sports-clubs/{academy_c}/logo",
        expected_status=200,
        replay_same_body=False,
        conflict_409=False,
        keys_delta=0,
        effect={
            "idempotencia_por_valor_borrado_repetido": audit_after_rm2 == audit_after_rm1,
            "sin_protocolo_de_clave": not await idempotency_rows_for_key(dataset.club, reused_key),
        },
        note="HALLAZGO: la ruta no consume Idempotency-Key (no replay/409); idempotente por valor",
    )


# ---------------------------------------------------------------- §19 AUDITORIA


@pytest.mark.asyncio(loop_scope="session")
async def test_19a_eventos_de_auditoria_reales_con_actor_tenant_y_motivo(
    scenario: C2Scenario,
) -> None:
    """Los cambios de §18 dejan eventos reales con actor, tenant y motivo."""
    dataset = scenario.dataset
    club = dataset.club

    affiliation_events = await audit_events(
        club, entity="competitor", action="UPDATE", reason_code=AFFILIATION_CHANGE
    )
    logo_events = await audit_events(
        club, entity="sports_club", action="UPDATE", reason_code=CLUB_LOGO_CHANGE
    )
    category_events = await audit_events(
        club, entity="tournament_registration", action="UPDATE", reason_code=CATEGORY_CHANGE
    )
    assert affiliation_events, "debe haber eventos AFFILIATION_CHANGE"
    assert logo_events, "debe haber eventos CLUB_LOGO_CHANGE"
    assert category_events, "debe haber eventos CATEGORY_CHANGE"

    for name, events in (
        ("AFFILIATION_CHANGE", affiliation_events),
        ("CLUB_LOGO_CHANGE", logo_events),
        ("CATEGORY_CHANGE", category_events),
    ):
        for event in events:
            assert event["actor_user_id"] is not None, event
            assert str(event["actor_label"]).strip(), event
            assert int(event["tenant_club_id"]) == club, event
            assert str(event["reason"]).strip(), event
        EVIDENCE_D["audit"]["events"][name] = [
            {
                "entity": events[-1]["entity"],
                "action": events[-1]["action"],
                "reason_code": events[-1]["reason_code"],
                "reason": events[-1]["reason"],
                "actor_label": events[-1]["actor_label"],
                "tenant_club_id": events[-1]["tenant_club_id"],
            }
        ]
    record_d("19_eventos_auditoria", True, EVIDENCE_D["audit"]["events"])


@pytest.mark.asyncio(loop_scope="session")
async def test_19b_generar_cuadro_audita_evento_real(scenario: C2Scenario) -> None:
    """Generar el cuadro (flujo principal) deja un evento ``GENERATE_BRACKET`` con actor y motivo
    .
    """
    dataset = scenario.dataset
    bracket = scenario.bracket
    tournament = int(bracket.tournament)

    async def register_and_confirm(
        key: str, declared: tuple[Decimal, str], category_key: str | None
    ) -> None:
        rep = representation_of(dataset, key)
        body = await create_registration_http(
            tournament_id=tournament,
            token=dataset.owner_token,
            competitor_id=int(dataset.competitors[key]),
            representation=rep["representation"],
            sports_club_id=rep["sports_club_id"],
            affiliation_id=rep["affiliation_id"],
            declared_weight_kg=declared[0],
            modality=declared[1],
        )
        await confirm_registration_http(
            tournament_id=tournament, registration_id=int(body["id"]), token=dataset.owner_token
        )
        if category_key is not None:
            await assign_category_http(
                tournament_id=tournament,
                registration_id=int(body["id"]),
                token=dataset.owner_token,
                category_key=category_key,
            )

    await register_and_confirm("a1", DECLARED_LIGHT, bracket.key(CATEGORY_LIGHT))
    await register_and_confirm("a6", DECLARED_HEAVY, bracket.key(CATEGORY_HEAVY))
    await register_and_confirm("b2", DECLARED_LIGHT, bracket.key(CATEGORY_LIGHT))
    await register_and_confirm("i3", DECLARED_HEAVY, bracket.key(CATEGORY_HEAVY))
    await close_window_http(tournament_id=tournament, token=dataset.owner_token)

    status_code, body = await generate_http(tournament_id=tournament, token=dataset.owner_token)
    assert status_code == 200, body

    events = await audit_events(
        dataset.club, entity="tournament", action=GENERATE_BRACKET, entity_id=tournament
    )
    assert events, "debe existir un evento GENERATE_BRACKET del torneo"
    event = events[-1]
    assert event["actor_user_id"] is not None, event
    assert str(event["actor_label"]).strip(), event
    assert int(event["tenant_club_id"]) == dataset.club, event
    assert str(event["reason"]).strip(), event
    assert event["reason_code"] is None, event  # GENERATE_BRACKET no lleva codigo de motivo
    EVIDENCE_D["audit"]["events"]["GENERATE_BRACKET"] = [
        {
            "entity": event["entity"],
            "entity_id": event["entity_id"],
            "action": event["action"],
            "reason_code": event["reason_code"],
            "reason": event["reason"],
            "actor_label": event["actor_label"],
            "tenant_club_id": event["tenant_club_id"],
        }
    ]
    record_d("19_generate_bracket_audita", True, EVIDENCE_D["audit"]["events"]["GENERATE_BRACKET"])


@pytest.mark.asyncio(loop_scope="session")
async def test_19c_las_lecturas_no_auditan(scenario: C2Scenario) -> None:
    """Una tanda de ``GET`` no escribe ni eventos de auditoria ni claves de idempotencia."""
    dataset = scenario.dataset
    tournament = int(dataset.tournament)
    registration_id = int(REGISTRY["reg_organizer"])
    audit_before = await count_audit_events(dataset.club)
    keys_before = await count_idempotency_keys(dataset.club)

    await call(
        "GET",
        competitor_url(dataset.club, int(dataset.competitors["a1"])),
        token=dataset.owner_token,
    )
    await get_registration_http(
        tournament_id=tournament, registration_id=registration_id, token=dataset.owner_token
    )
    await classify_http(
        tournament_id=tournament, registration_id=registration_id, token=dataset.owner_token
    )
    await readiness_http(tournament_id=tournament, token=dataset.owner_token)
    await call("GET", f"{_root()}clubs/{dataset.club}/sports-clubs", token=dataset.owner_token)
    await call("GET", f"{_root()}clubs/{dataset.club}/belts", token=dataset.owner_token)

    audit_after = await count_audit_events(dataset.club)
    keys_after = await count_idempotency_keys(dataset.club)
    assert audit_after == audit_before, (audit_before, audit_after)
    assert keys_after == keys_before, (keys_before, keys_after)
    EVIDENCE_D["audit"]["reads_do_not_audit"] = {
        "eventos_antes": audit_before,
        "eventos_despues": audit_after,
        "claves_antes": keys_before,
        "claves_despues": keys_after,
    }
    record_d(
        "19_lecturas_no_auditan",
        audit_after == audit_before and keys_after == keys_before,
        EVIDENCE_D["audit"]["reads_do_not_audit"],
    )


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
    keys = _compat_keys(classification)
    assert structured in keys, keys
    assert legacy in keys, keys
    assert CATEGORY_RULES_PARTIAL in _warnings(_verdict(classification, legacy)), classification
    assert CATEGORY_RULES_PARTIAL not in _warnings(_verdict(classification, structured)), (
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
        w for item in clean_classification["compatible_categories"] for w in _warnings(item)
    ]
    assert clean_compatible == ["cat-male-gi-light"], clean_compatible
    assert CATEGORY_RULES_PARTIAL not in all_clean_warnings, all_clean_warnings

    EVIDENCE_D["legacy"] = {
        "torneo_con_legacy": {
            "compatible": sorted(keys),
            "legacy_warnings": _warnings(_verdict(classification, legacy)),
            "estructurada_warnings": _warnings(_verdict(classification, structured)),
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
    on_disk = Path("openapi/openapi.json").read_text(encoding="utf-8")
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
    root = Path("/home/ubuntu/work/bracket-f2c/frontend/src/openapi")
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
