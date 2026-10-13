# pylint: disable=redefined-outer-name
"""E2E realista de S4.8a-5 (TROZO C2, §13-§15): historico de club, readiness y generacion del
cuadro de eliminacion directa, por el API HTTP real.

Pruebas **negras** sobre el escenario del TROZO A (24 competidores, 3 academias, 5 categorias
estructuradas + 1 legacy) y las superficies de clasificacion del TROZO B, contra la base de
laboratorio ``bracket_ci``. No hay mocks del motor, del protocolo ni del RBAC: se ejercitan las
rutas reales (inscripcion F3, ciclo de vida, afiliaciones, asignacion de categoria, readiness y
generacion del cuadro) y se lee lo que el backend escribe (``tournament_registrations``,
``competitors_x_sports_clubs``, ``stage_item_inputs`` y ``domain_change_log``).

Que fija este modulo:

* §13 HISTORICO DE CLUB: un competidor **afiliado a la academia A** se inscribe representando a su
  academia (con su snapshot) y se confirma; despues **cambia de academia a B** por los endpoints
  reales de afiliacion (alta + ``primary``); la inscripcion historica sigue mostrando **Club A** y su
  snapshot original, y la inscripcion congelada del escenario A tampoco se reescribe;
* §14 READINESS: con la ventana **abierta** y por debajo del minimo de entrants,
  ``ready=false`` con los **blockers reales por categoria** (no un booleano a secas); al cerrar la
  ventana y resolver las clasificaciones (asignar la sin-resolver y alcanzar el minimo) pasa a
  ``ready=true``;
* §15 GENERATE BRACKET: cerrar la ventana; intentar generar cuando readiness **no** lo permite y
  validar el rechazo real (``409 BRACKET_NOT_READY`` con sus bloqueos); generar cuando ``ready=true``
  y validar, **por grupo**, ``category_key``/``entrant_count``/``bracket_size``, que no se mezclan
  categorias distintas, que los **independientes** entran bien, que representar un club **no**
  influye en la clasificacion deportiva, que la representacion historica de cada entrant se preserva
  y que, tras generar, el cuadro queda **congelado** (una segunda generacion es un no-op sin
  auditoria).

El escenario es de modulo y **los casos se ejecutan en orden de definicion**: §13 y la comparacion
club/independiente son independientes, pero §14 y §15 comparten el torneo de cuadros y avanzan juntos
(antes -> rechazo -> despues -> generacion).

§25: al cerrar el modulo se vuelca la evidencia en ``/home/ubuntu/s48a5-evidence/trozo-c2.json``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio

from bracket.config import config
from bracket.logic.bracket_readiness import BracketReadinessBlocker
from tests.integration_tests.api.generate_bracket_http_support import SYNTHETIC_SECRET
from tests.integration_tests.api.s48a5_e2e_trozo_a_support import (
    affiliation_action_url,
    affiliation_history,
    assign_affiliation_http,
    call,
    fresh_key,
    open_primary_count,
    registration_row,
)
from tests.integration_tests.api.s48a5_e2e_trozo_c2_support import (
    CATEGORY_HEAVY,
    CATEGORY_LIGHT,
    DECLARED_HEAVY,
    DECLARED_LIGHT,
    HISTORICAL_COMPETITOR,
    HISTORICAL_TARGET_ACADEMY,
    C2Scenario,
    assign_category_http,
    bracket_layout,
    category_group,
    classify_http,
    close_window_http,
    confirm_registration_http,
    count_generate_audit,
    create_registration_http,
    current_registration,
    generate_blocking_issues,
    generate_http,
    generate_not_ready_code,
    get_registration_http,
    issue_codes,
    key_name_map,
    occupied_team_ids,
    readiness_http,
    record_c2,
    record_generation,
    record_historical,
    record_readiness,
    representation_of,
    sports_club_name,
    team_name,
    trozo_c2_scenario,
    window_http,
    write_evidence_c2,
)

REGISTRATION_WINDOW_OPEN = str(BracketReadinessBlocker.REGISTRATION_WINDOW_OPEN.value)
CATEGORY_NOT_ENOUGH_COMPETITORS = str(
    BracketReadinessBlocker.CATEGORY_NOT_ENOUGH_COMPETITORS.value
)
CATEGORY_NOT_RESOLVED = str(BracketReadinessBlocker.CATEGORY_NOT_RESOLVED.value)

#: Estado mutable compartido entre los casos ordenados de §14/§15: ids de inscripcion por competidor
#: del torneo de cuadros (se rellenan a medida que el modulo avanza).
BRACKET_REGISTRATIONS: dict[str, int] = {}


@pytest.fixture(scope="module", autouse=True)
def e2e_write_gate() -> Iterator[None]:
    """Escritura F3 y secreto de idempotencia habilitados durante todo el modulo.

    La inscripcion, la confirmacion, las afiliaciones, la asignacion de categoria y la generacion
    estan detras de la puerta de escritura y del protocolo de idempotencia. No se depende del entorno
    de CI: el modulo lo fija y lo restaura.
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
) -> AsyncIterator[C2Scenario]:
    """Escenario A + superficies + torneo de cuadros; al cerrar vuelca §25."""
    async with trozo_c2_scenario() as scenario_:
        try:
            yield scenario_
        finally:
            write_evidence_c2()


# ---------------------------------------------------------------- helpers del modulo


async def _register(
    scenario_: C2Scenario,
    competitor_key: str,
    *,
    representation: dict[str, Any],
    declared: tuple[Decimal, str],
    tournament_id: int | None = None,
) -> int:
    """Alta real de un borrador; la categoria se fija despues por la ruta real (§7)."""
    target = int(scenario_.bracket.tournament) if tournament_id is None else int(tournament_id)
    body = await create_registration_http(
        tournament_id=target,
        token=scenario_.dataset.owner_token,
        competitor_id=int(scenario_.dataset.competitors[competitor_key]),
        declared_weight_kg=declared[0],
        modality=declared[1],
        **representation,
    )
    return int(body["id"])


async def _register_and_confirm(
    scenario_: C2Scenario,
    competitor_key: str,
    *,
    representation: dict[str, Any],
    declared: tuple[Decimal, str],
    category_key: str | None = None,
    category_name: str | None = None,
    tournament_id: int | None = None,
) -> int:
    """Alta + ``DRAFT -> CONFIRMED`` (la readiness solo cuenta confirmadas). Devuelve el id."""
    target = int(scenario_.bracket.tournament) if tournament_id is None else int(tournament_id)
    registration_id = await _register(
        scenario_,
        competitor_key,
        representation=representation,
        declared=declared,
        tournament_id=target,
    )
    status_code, body = await confirm_registration_http(
        tournament_id=target,
        registration_id=registration_id,
        token=scenario_.dataset.owner_token,
    )
    assert status_code == 200, body
    if category_key is not None:
        status_code, body = await assign_category_http(
            tournament_id=target,
            registration_id=registration_id,
            token=scenario_.dataset.owner_token,
            category_key=category_key,
        )
        assert status_code == 200, body
    return registration_id


def _group_codes(group: dict[str, Any] | None) -> list[str]:
    assert group is not None
    return [str(issue["code"]) for issue in group["blocking_issues"]]


# ---------------------------------------------------------------- §13 HISTORICO DE CLUB


@pytest.mark.asyncio(loop_scope="session")
async def test_13_historico_de_club_conserva_club_y_snapshot_al_cambiar_de_academia(
    scenario: C2Scenario,
) -> None:
    dataset = scenario.dataset
    competitor = int(dataset.competitors[HISTORICAL_COMPETITOR])
    representation = representation_of(dataset, HISTORICAL_COMPETITOR)
    assert representation["representation"] == "CLUB"
    assert representation["sports_club_id"] == int(dataset.clubs["A"])
    assert representation["affiliation_id"] == int(dataset.affiliations[HISTORICAL_COMPETITOR])

    # (1)(2) Inscripcion confirmada que representa la academia A, con su snapshot.
    registration_id = await _register_and_confirm(
        scenario,
        HISTORICAL_COMPETITOR,
        representation=representation,
        declared=DECLARED_LIGHT,
        tournament_id=int(dataset.tournament),
    )
    academy_a = await sports_club_name(int(dataset.clubs["A"]))
    before = await registration_row(registration_id)
    assert before["sports_club_id"] == int(dataset.clubs["A"]), before
    assert before["sports_club_name_snapshot"] == academy_a, before
    assert before["competitor_name_snapshot"] == dataset.name(HISTORICAL_COMPETITOR), before
    assert before["representation"] == "CLUB", before
    assert before["affiliation_id"] == int(dataset.affiliations[HISTORICAL_COMPETITOR]), before

    status_code, http_before = await get_registration_http(
        tournament_id=int(dataset.tournament),
        registration_id=registration_id,
        token=dataset.owner_token,
    )
    assert status_code == 200, http_before
    assert http_before["sports_club_name_snapshot"] == academy_a, http_before
    assert http_before["competitor_name_snapshot"] == dataset.name(HISTORICAL_COMPETITOR)

    frozen_before = await registration_row(int(dataset.frozen_registration))

    # (3) El competidor cambia de academia a B por los endpoints reales: alta + promocion a primaria.
    new_affiliation = await assign_affiliation_http(
        dataset.club, competitor, int(dataset.clubs["B"]), token=dataset.owner_token
    )
    new_affiliation_id = int(new_affiliation["id"])
    status_code, body = await call(
        "POST",
        affiliation_action_url(dataset.club, competitor, new_affiliation_id, "primary"),
        token=dataset.owner_token,
        key=fresh_key(),
    )
    assert status_code == 200, body

    history = await affiliation_history(competitor)
    b_row = next(row for row in history if row["id"] == new_affiliation_id)
    assert b_row["is_primary"] is True, b_row
    assert b_row["valid_to"] is None, b_row
    a_rows = [row for row in history if row["sports_club_id"] == int(dataset.clubs["A"])]
    assert a_rows and all(row["valid_to"] is not None for row in a_rows), a_rows
    assert await open_primary_count(competitor) == 1

    # (4) La inscripcion historica NO se reescribe: misma foto exacta que antes del cambio.
    after = await registration_row(registration_id)
    assert after == before, (before, after)
    status_code, http_after = await get_registration_http(
        tournament_id=int(dataset.tournament),
        registration_id=registration_id,
        token=dataset.owner_token,
    )
    assert status_code == 200, http_after
    assert http_after["sports_club_name_snapshot"] == academy_a, http_after
    assert http_after["sports_club_id"] == int(dataset.clubs["A"]), http_after

    # La referencia congelada del escenario A tampoco se mueve con el cambio.
    frozen_after = await registration_row(int(dataset.frozen_registration))
    assert frozen_after == frozen_before, (frozen_before, frozen_after)

    record_historical(
        before=dict(before),
        after=dict(after),
    )
    record_c2(
        "13_historico_de_club",
        after == before and frozen_after == frozen_before,
        {
            "registration_id": registration_id,
            "competitor": HISTORICAL_COMPETITOR,
            "academy_before": "A",
            "academy_after": HISTORICAL_TARGET_ACADEMY,
            "snapshot_before": before["sports_club_name_snapshot"],
            "snapshot_after": after["sports_club_name_snapshot"],
        },
    )


# ---------------------------------------------------------------- §15 el club no clasifica


@pytest.mark.asyncio(loop_scope="session")
async def test_15_el_club_no_influye_en_la_clasificacion_deportiva(scenario: C2Scenario) -> None:
    """El mismo competidor, con los mismos datos, clasifica igual como CLUB y como INDEPENDENT."""
    dataset = scenario.dataset
    competitor = int(dataset.competitors["i1"])
    declared = (Decimal("74.50"), "GI")

    # CLUB: una academia real (C) y su afiliacion, dadas de alta por el camino real.
    affiliation = await assign_affiliation_http(
        dataset.club, competitor, int(dataset.clubs["C"]), token=dataset.owner_token
    )
    club_body = await create_registration_http(
        tournament_id=int(scenario.club_surface.tournament),
        token=dataset.owner_token,
        competitor_id=competitor,
        declared_weight_kg=declared[0],
        modality=declared[1],
        representation="CLUB",
        sports_club_id=int(dataset.clubs["C"]),
        affiliation_id=int(affiliation["id"]),
    )
    independent_body = await create_registration_http(
        tournament_id=int(scenario.independent_surface.tournament),
        token=dataset.owner_token,
        competitor_id=competitor,
        declared_weight_kg=declared[0],
        modality=declared[1],
        representation="INDEPENDENT",
        sports_club_id=None,
        affiliation_id=None,
    )

    club_classification = await classify_http(
        tournament_id=int(scenario.club_surface.tournament),
        registration_id=int(club_body["id"]),
        token=dataset.owner_token,
    )
    independent_classification = await classify_http(
        tournament_id=int(scenario.independent_surface.tournament),
        registration_id=int(independent_body["id"]),
        token=dataset.owner_token,
    )

    club_names = key_name_map(scenario.club_surface)
    independent_names = key_name_map(scenario.independent_surface)
    club_compatible = sorted(
        club_names[item["category_key"]] for item in club_classification["compatible_categories"]
    )
    independent_compatible = sorted(
        independent_names[item["category_key"]]
        for item in independent_classification["compatible_categories"]
    )
    club_recommended = club_names[club_classification["recommended_category"]["category_key"]]
    independent_recommended = independent_names[
        independent_classification["recommended_category"]["category_key"]
    ]

    assert club_compatible == independent_compatible, (club_compatible, independent_compatible)
    assert club_recommended == independent_recommended, (club_recommended, independent_recommended)
    assert club_compatible, "el competidor debe tener alguna categoria compatible"

    record_c2(
        "15_club_no_influye_en_clasificacion",
        club_compatible == independent_compatible and club_recommended == independent_recommended,
        {
            "compatible_club": club_compatible,
            "compatible_independent": independent_compatible,
            "recommended": club_recommended,
        },
    )


# ---------------------------------------------------------------- §14 READINESS (antes)


@pytest.mark.asyncio(loop_scope="session")
async def test_14_readiness_no_listo_expone_blockers_reales_por_categoria(
    scenario: C2Scenario,
) -> None:
    """Sin resolver las clasificaciones y por debajo del minimo: ``ready=false`` con los blockers."""
    bracket = scenario.bracket
    dataset = scenario.dataset

    # Estado de partida: ventana abierta; light con un solo confirmado; heavy con un solo
    # confirmado; y una inscripcion confirmada sin categoria (por resolver).
    BRACKET_REGISTRATIONS["a1"] = await _register_and_confirm(
        scenario,
        "a1",
        representation=representation_of(dataset, "a1"),
        declared=DECLARED_LIGHT,
        category_key=bracket.key(CATEGORY_LIGHT),
        category_name=CATEGORY_LIGHT,
    )
    BRACKET_REGISTRATIONS["a6"] = await _register_and_confirm(
        scenario,
        "a6",
        representation=representation_of(dataset, "a6"),
        declared=DECLARED_HEAVY,
        category_key=bracket.key(CATEGORY_HEAVY),
        category_name=CATEGORY_HEAVY,
    )
    BRACKET_REGISTRATIONS["b2"] = await _register_and_confirm(
        scenario,
        "b2",
        representation=representation_of(dataset, "b2"),
        declared=DECLARED_LIGHT,
        category_key=None,
    )

    window = await window_http(tournament_id=int(bracket.tournament), token=dataset.owner_token)
    assert window["state"] == "OPEN", window

    readiness = await readiness_http(
        tournament_id=int(bracket.tournament), token=dataset.owner_token
    )
    assert readiness["ready"] is False, readiness
    codes = issue_codes(readiness)
    assert REGISTRATION_WINDOW_OPEN in codes, codes
    assert CATEGORY_NOT_ENOUGH_COMPETITORS in codes, codes
    assert CATEGORY_NOT_RESOLVED in codes, codes

    light = category_group(readiness, bracket.key(CATEGORY_LIGHT))
    heavy = category_group(readiness, bracket.key(CATEGORY_HEAVY))
    unresolved = category_group(readiness, None)
    assert light is not None and heavy is not None and unresolved is not None

    assert light["confirmed_count"] == 1, light
    assert light["required_minimum"] == 2, light
    assert CATEGORY_NOT_ENOUGH_COMPETITORS in _group_codes(light), light
    assert heavy["confirmed_count"] == 1, heavy
    assert heavy["required_minimum"] == 2, heavy
    assert CATEGORY_NOT_ENOUGH_COMPETITORS in _group_codes(heavy), heavy
    assert CATEGORY_NOT_RESOLVED in _group_codes(unresolved), unresolved

    record_readiness("before", readiness)
    record_c2(
        "14_readiness_antes",
        not readiness["ready"] and REGISTRATION_WINDOW_OPEN in codes,
        {
            "codes": codes,
            "light": {"confirmed": light["confirmed_count"],
                       "required": light["required_minimum"],
                       "issues": _group_codes(light)},
            "heavy": {"confirmed": heavy["confirmed_count"],
                      "required": heavy["required_minimum"],
                      "issues": _group_codes(heavy)},
            "unresolved": _group_codes(unresolved),
        },
    )


# ---------------------------------------------------------------- §15 GENERATE BRACKET (rechazo)


@pytest.mark.asyncio(loop_scope="session")
async def test_15_generacion_rechazada_cuando_readiness_no_lo_permite(scenario: C2Scenario) -> None:
    """Con la ventana abierta y categorias por debajo del minimo, generar se rechaza de verdad."""
    bracket = scenario.bracket
    dataset = scenario.dataset

    audit_before = await count_generate_audit(int(bracket.tournament))
    status_code, body = await generate_http(
        tournament_id=int(bracket.tournament), token=dataset.owner_token
    )
    assert status_code == 409, body
    assert generate_not_ready_code(body) == "BRACKET_NOT_READY", body
    blocking = generate_blocking_issues(status_code, body)
    assert REGISTRATION_WINDOW_OPEN in blocking, blocking
    assert CATEGORY_NOT_ENOUGH_COMPETITORS in blocking, blocking
    # Nada se escribio: no hay evento de generacion ni se movieron las ranuras.
    assert await count_generate_audit(int(bracket.tournament)) == audit_before == 0
    assert len(await occupied_team_ids(bracket.categories[CATEGORY_LIGHT])) == 2

    record_c2(
        "15_generacion_rechazada",
        status_code == 409 and REGISTRATION_WINDOW_OPEN in blocking,
        {"status": status_code, "blocking_issues": blocking},
    )


# ---------------------------------------------------------------- §14 READINESS (despues)


@pytest.mark.asyncio(loop_scope="session")
async def test_14_readiness_listo_tras_resolver_clasificaciones(scenario: C2Scenario) -> None:
    """Cerrada la ventana y resueltas las clasificaciones, con el minimo: ``ready=true``."""
    bracket = scenario.bracket
    dataset = scenario.dataset

    # Resolver las clasificaciones: asignar la sin-clasificar a su categoria compatible...
    status_code, body = await assign_category_http(
        tournament_id=int(bracket.tournament),
        registration_id=BRACKET_REGISTRATIONS["b2"],
        token=dataset.owner_token,
        category_key=bracket.key(CATEGORY_LIGHT),
    )
    assert status_code == 200, body
    # ...y alcanzar el minimo de entrants en la categoria que se quedo corta (la ventana sigue
    # abierta: el alta solo se cierra mas abajo).
    BRACKET_REGISTRATIONS["i3"] = await _register_and_confirm(
        scenario,
        "i3",
        representation=representation_of(dataset, "i3"),
        declared=DECLARED_HEAVY,
        category_key=bracket.key(CATEGORY_HEAVY),
        category_name=CATEGORY_HEAVY,
    )

    # Con todo inscrito, se cierra la ventana: es la ultima regla de la readiness.
    status_code, body = await close_window_http(
        tournament_id=int(bracket.tournament), token=dataset.owner_token
    )
    assert status_code == 200, body
    assert body["state"] == "CLOSED", body

    readiness = await readiness_http(
        tournament_id=int(bracket.tournament), token=dataset.owner_token
    )
    assert readiness["ready"] is True, readiness
    assert issue_codes(readiness) == [], readiness["blocking_issues"]
    assert readiness["registration_window"] == "CLOSED", readiness
    assert readiness["summary"]["confirmed"] == 4, readiness["summary"]

    light = category_group(readiness, bracket.key(CATEGORY_LIGHT))
    heavy = category_group(readiness, bracket.key(CATEGORY_HEAVY))
    assert light is not None and light["ready"] is True and light["confirmed_count"] == 2, light
    assert heavy is not None and heavy["ready"] is True and heavy["confirmed_count"] == 2, heavy
    assert category_group(readiness, None) is None, readiness["categories"]

    record_readiness("after", readiness)
    record_c2(
        "14_readiness_despues",
        readiness["ready"] is True and issue_codes(readiness) == [],
        {
            "ready": readiness["ready"],
            "window": readiness["registration_window"],
            "confirmed": readiness["summary"]["confirmed"],
            "light": light["confirmed_count"],
            "heavy": heavy["confirmed_count"],
        },
    )


# ---------------------------------------------------------------- §15 GENERATE BRACKET (exito)


@pytest.mark.asyncio(loop_scope="session")
async def test_15_generacion_por_grupo_y_cuadro_congelado(scenario: C2Scenario) -> None:
    """``ready=true``: el cuadro se genera por grupo y, una vez generado, queda congelado."""
    bracket = scenario.bracket
    dataset = scenario.dataset

    # Foto de la representacion de cada entrant ANTES de generar (debe preservarse).
    before = {
        key: await current_registration(int(bracket.tournament), int(dataset.competitors[key]))
        for key in ("a1", "b2", "a6", "i3")
    }
    audit_before = await count_generate_audit(int(bracket.tournament))

    status_code, generated = await generate_http(
        tournament_id=int(bracket.tournament), token=dataset.owner_token
    )
    assert status_code == 200, generated
    assert generated["generated"] is True, generated
    assert generated["skipped_stage_items"] == 0, generated

    name_by_stage_item = {int(bracket.categories[name]): name for name in bracket.entrants}
    assert len(generated["stage_items"]) == 2, generated["stage_items"]

    groups: list[dict[str, Any]] = []
    seen_team_sets: list[set[int]] = []
    ordered_names: list[str] = []
    for item in generated["stage_items"]:
        stage_item_id = int(item["stage_item_id"])
        name = name_by_stage_item[stage_item_id]
        ordered_names.append(name)
        expected_teams = {bracket.teams[key] for key in bracket.entrants[name]}
        actual_teams = set(await occupied_team_ids(stage_item_id))

        assert item["entrant_count"] == len(bracket.entrants[name]) == 2, item
        assert item["bracket_size"] == bracket.team_count == 4, item
        assert item["bye_count"] == 2, item
        # ``category_key`` correcto por grupo: el cuadro es la propia categoria.
        assert f"si:{stage_item_id}" == bracket.key(name), (stage_item_id, name)
        # No se mezclan categorias distintas: el cuadro contiene exactamente sus entrantes.
        assert actual_teams == expected_teams, (name, actual_teams, expected_teams)
        seen_team_sets.append(actual_teams)
        groups.append(
            {
                "category_key": bracket.key(name),
                "category_name": name,
                "stage_item_id": stage_item_id,
                "entrant_count": item["entrant_count"],
                "bracket_size": item["bracket_size"],
                "bye_count": item["bye_count"],
                "entrants": list(bracket.entrants[name]),
                "occupied_slots": (await bracket_layout(stage_item_id)),
            }
        )
    assert seen_team_sets[0].isdisjoint(seen_team_sets[1]), seen_team_sets

    # Los independientes entran correctamente: el entrante independiente ocupa su ranura real.
    independent_key = "i3"
    independent_competitor = await current_registration(
        int(bracket.tournament), int(dataset.competitors[independent_key])
    )
    assert independent_competitor["representation"] == "INDEPENDENT"
    assert independent_competitor["sports_club_id"] is None
    independent_name = next(
        name for name in bracket.entrants if independent_key in bracket.entrants[name]
    )
    assert (
        bracket.teams[independent_key]
        in seen_team_sets[ordered_names.index(independent_name)]
    )
    assert await team_name(bracket.teams[independent_key]) == f"{dataset.name_prefix}-team-{independent_key}"

    # La representacion historica de cada entrant se preserva tras generar.
    after = {
        key: await current_registration(int(bracket.tournament), int(dataset.competitors[key]))
        for key in ("a1", "b2", "a6", "i3")
    }
    assert after == before, (before, after)

    # Congelado: una segunda generacion con otra clave es un no-op (mismo reparto, sin auditoria).
    status_code, regenerated = await generate_http(
        tournament_id=int(bracket.tournament), token=dataset.owner_token
    )
    assert status_code == 200, regenerated
    assert regenerated["stage_items"] == generated["stage_items"], regenerated
    audit_after = await count_generate_audit(int(bracket.tournament))
    assert audit_after == audit_before + 1 == 1, audit_after

    record_generation(
        groups=groups,
        frozen={
            "audit_events": audit_after,
            "second_generation_same": regenerated["stage_items"] == generated["stage_items"],
        },
    )
    record_c2(
        "15_generacion_por_grupo",
        len(groups) == 2 and audit_after == 1 and after == before,
        {"groups": [group["category_name"] for group in groups], "audit_events": audit_after},
    )
