# pylint: disable=redefined-outer-name
"""E2E realista de S4.8a-5 (TROZO C1, §10-§12): correccion de blockers, asignacion de categoria y
override OWNER, por el API HTTP real.

Pruebas **negras** sobre el escenario del TROZO A (24 competidores, 3 academias, 5 categorias
estructuradas + 1 legacy) y la superficie de clasificacion del TROZO B, contra la base de
laboratorio ``bracket_ci``. No hay mocks del motor, del protocolo ni del RBAC: se ejercitan las
rutas reales (perfil de competidor, asignacion de categoria, relectura de la inscripcion y
readiness del cuadro) y se lee lo que el backend escribe (``domain_change_log``).

Que fija este modulo:

* §10 CORREGIR un blocker por API real y **revalidar**: anadir el peso (``b2``), el cinturon
  (``b6``), la fecha de nacimiento (``a7``) y la modalidad (``c3``), comprobando en cada caso que
  desaparece **exactamente** el blocker corregido —y los derivados de "ninguna compatible"—, que
  **no aparece ninguno nuevo** y que las clasificaciones de los demas casos **no cambian**;
* §11 ASIGNAR una categoria **compatible** sin ``override``: OWNER puede y COLLABORATOR tambien
  (contrato real), con idempotencia (misma clave + mismo cuerpo -> replay sin duplicar efecto;
  misma clave + cuerpo distinto -> ``409``), relectura de la inscripcion y efecto en el readiness;
* §12 OVERRIDE OWNER sobre una categoria deliberadamente **incompatible**: sin ``override`` -> 400,
  COLLABORATOR con ``override=true`` -> ``403``, OWNER con ``override=true`` y motivo -> 200 (sin
  motivo -> 400), validando el evento ``CATEGORY_CHANGE`` de la auditoria con actor, motivo y la
  categoria anterior/recomendada/elegida, y que **no existe camino silencioso** a una incompatible.

§25: al cerrar el modulo se vuelca la evidencia en ``/home/ubuntu/s48a5-evidence/trozo-c1.json``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio

from bracket.config import config
from bracket.logic.classification.vocabulary import ClassificationBlocker
from tests.integration_tests.api.generate_bracket_http_support import SYNTHETIC_SECRET
from tests.integration_tests.api.s48a5_e2e_trozo_a_support import fresh_key
from tests.integration_tests.api.s48a5_e2e_trozo_b_support import (
    classify_http,
    create_registration_http,
    representation_of,
)
from tests.integration_tests.api.s48a5_e2e_trozo_c1_support import (
    C1Scenario,
    actor_relation,
    assign_category_http,
    category_group,
    confirm_registration_http,
    count_category_change_events,
    get_registration_http,
    issue_codes,
    latest_category_change_event,
    readiness_http,
    record_assignment,
    record_correction,
    record_override_event,
    record_validation,
    surface_of,
    trozo_c1_scenario,
    update_profile_http,
    write_evidence_c1,
)

MISSING_DATE_OF_BIRTH = str(ClassificationBlocker.MISSING_DATE_OF_BIRTH.value)
MISSING_BELT = str(ClassificationBlocker.MISSING_BELT.value)
MISSING_WEIGHT = str(ClassificationBlocker.MISSING_WEIGHT.value)
MISSING_MODALITY = str(ClassificationBlocker.MISSING_MODALITY.value)
CATEGORY_NOT_RESOLVED = str(ClassificationBlocker.CATEGORY_NOT_RESOLVED.value)
CATEGORY_INCOMPATIBLE = str(ClassificationBlocker.CATEGORY_INCOMPATIBLE.value)
NO_COMPATIBLE_CATEGORY = str(ClassificationBlocker.NO_COMPATIBLE_CATEGORY.value)

#: Los dos blockers de "ninguna compatible" que acompanan a un ``MISSING_*`` y que, al corregir el
#: dato, desaparecen con el (el caso pasa a tener una unica compatible y solo queda la etiqueta sin
#: resolver). Se documentan como derivados, no como el blocker corregido.
_DERIVED = frozenset({CATEGORY_INCOMPATIBLE, NO_COMPATIBLE_CATEGORY})


@pytest.fixture(scope="module", autouse=True)
def e2e_write_gate() -> Iterator[None]:
    """Escritura F3 y secreto de idempotencia habilitados durante todo el modulo.

    El perfil de competidor, la asignacion de categoria y la confirmacion estan detras de la puerta de
    escritura y del protocolo de idempotencia. No se depende del entorno de CI: el modulo lo fija y lo
    restaura.
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
) -> AsyncIterator[C1Scenario]:
    """Escenario A + superficie ``before`` + superficie ``after``; al cerrar vuelca §25."""
    async with trozo_c1_scenario() as scenario_:
        try:
            yield scenario_
        finally:
            write_evidence_c1()


# ---------------------------------------------------------------- helpers del modulo


async def register(
    scenario_: C1Scenario,
    which: str,
    competitor_key: str,
    *,
    weight: Decimal | None,
    modality: str | None,
    key: str | None = None,
) -> int:
    """Alta real de una inscripcion en la superficie indicada; devuelve el id."""
    dataset = scenario_.dataset
    surface = surface_of(scenario_, which)
    representation = representation_of(dataset, competitor_key)
    body = await create_registration_http(
        tournament_id=int(surface.tournament),
        token=dataset.owner_token,
        competitor_id=int(dataset.competitors[competitor_key]),
        declared_weight_kg=weight,
        modality=modality,
        key=key,
        **representation,
    )
    return int(body["id"])


async def classify(scenario_: C1Scenario, which: str, registration_id: int) -> dict[str, Any]:
    """Clasificacion real de una inscripcion de la superficie indicada."""
    surface = surface_of(scenario_, which)
    return await classify_http(
        tournament_id=int(surface.tournament),
        registration_id=registration_id,
        token=scenario_.dataset.owner_token,
    )


def codes(body: dict[str, Any]) -> list[str]:
    """Codigos literales de los blockers, en el orden del contrato."""
    return [str(code) for code in body["blockers"]]


async def reread(scenario_: C1Scenario, which: str, registration_id: int) -> dict[str, Any]:
    """Relectura por HTTP de la proyeccion publica de la inscripcion."""
    surface = surface_of(scenario_, which)
    status_code, body = await get_registration_http(
        tournament_id=int(surface.tournament),
        registration_id=registration_id,
        token=scenario_.dataset.owner_token,
    )
    assert status_code == 200, body
    return body


async def assert_correction(
    scenario_: C1Scenario,
    *,
    case: str,
    competitor_key: str,
    field: str,
    corrected_value: Any,
    blocker_code: str,
    before_body: dict[str, Any],
    before_registration_id: int,
    after_body: dict[str, Any],
    after_registration_id: int,
    control_registration_id: int,
    control_before: dict[str, Any],
) -> None:
    """Revalida una correccion: mide el antes/despues y comprueba que solo cae el blocker corregido.

    El control es **otro** caso de la superficie ``after``, clasificado antes y despues de la
    correccion: demuestra que corregir un dato no perturba las clasificaciones de los demas
    competidores.
    """
    before_tournament = int(surface_of(scenario_, "before").tournament)
    after_tournament = int(surface_of(scenario_, "after").tournament)

    bl_before = codes(before_body)
    bl_after = codes(after_body)

    assert blocker_code in bl_before, (case, "blocker ausente en el antes", bl_before)
    assert blocker_code not in bl_after, (case, "el blocker corregido sigue presente", bl_after)

    removed = [code for code in bl_before if code not in bl_after]
    added = [code for code in bl_after if code not in bl_before]
    assert set(removed) == {blocker_code} | _DERIVED, (case, "desaparece de mas", removed)
    assert added == [], (case, "aparece un blocker nuevo", added)
    # Tras corregir, la unica compatible hace que quede la etiqueta sin resolver.
    assert bl_after == [CATEGORY_NOT_RESOLVED], (case, bl_after)
    assert len(after_body["compatible_categories"]) == 1, (case, after_body["compatible_categories"])

    control_after = await classify(scenario_, "after", control_registration_id)
    other_unchanged = control_after == control_before
    assert other_unchanged, (case, "cambio la clasificacion de otro caso")

    record_correction(
        case=case,
        competitor_key=competitor_key,
        field=field,
        corrected_value=corrected_value,
        before_registration_id=before_registration_id,
        before_tournament_id=before_tournament,
        after_registration_id=after_registration_id,
        after_tournament_id=after_tournament,
        blockers_before=bl_before,
        blockers_after=bl_after,
        removed=removed,
        added=added,
        other_cases_unchanged=other_unchanged,
    )
    record_validation(
        f"§10.{case}",
        True,
        {"blocker": blocker_code, "antes": bl_before, "despues": bl_after, "removed": removed},
    )


async def control_registration(
    scenario_: C1Scenario, competitor_key: str, weight: Decimal, modality: str
) -> tuple[int, dict[str, Any]]:
    """Registra y clasifica un caso de control en la superficie ``after``."""
    control_id = await register(
        scenario_, "after", competitor_key, weight=weight, modality=modality
    )
    return control_id, await classify(scenario_, "after", control_id)


# ================================================================ §10 CORREGIR blockers


@pytest.mark.asyncio(loop_scope="session")
async def test_c10_b2_peso_corregido_quita_missing_weight(scenario: C1Scenario) -> None:
    """§10.peso: ``b2`` sin peso bloquea por ``MISSING_WEIGHT``; al declarar 74.50 kg, se revalida."""
    control_id, control_before = await control_registration(
        scenario, "a2", Decimal("110.00"), "GI"
    )

    before_id = await register(scenario, "before", "b2", weight=None, modality="GI")
    before = await classify(scenario, "before", before_id)
    assert MISSING_WEIGHT in codes(before), codes(before)

    after_id = await register(scenario, "after", "b2", weight=Decimal("74.50"), modality="GI")
    after = await classify(scenario, "after", after_id)

    await assert_correction(
        scenario,
        case="peso",
        competitor_key="b2",
        field="declared_weight_kg",
        corrected_value="74.50",
        blocker_code=MISSING_WEIGHT,
        before_body=before,
        before_registration_id=before_id,
        after_body=after,
        after_registration_id=after_id,
        control_registration_id=control_id,
        control_before=control_before,
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_c10_b6_cinturon_corregido_quita_missing_belt(scenario: C1Scenario) -> None:
    """§10.cinturon: ``b6`` sin cinturon bloquea por ``MISSING_BELT``; se corrige por el perfil real."""
    dataset = scenario.dataset
    control_id, control_before = await control_registration(
        scenario, "b3", Decimal("85.00"), "GI"
    )

    before_id = await register(scenario, "before", "b6", weight=Decimal("74.50"), modality="GI")
    before = await classify(scenario, "before", before_id)
    assert MISSING_BELT in codes(before), codes(before)

    status_code, body = await update_profile_http(
        club=dataset.club,
        competitor_id=int(dataset.competitors["b6"]),
        token=dataset.owner_token,
        date_of_birth=date(1991, 10, 10),
        sex="MALE",
        belt_id=int(dataset.belts["PURPLE"]),
    )
    assert status_code == 200, body

    after_id = await register(scenario, "after", "b6", weight=Decimal("74.50"), modality="GI")
    after = await classify(scenario, "after", after_id)

    await assert_correction(
        scenario,
        case="cinturon",
        competitor_key="b6",
        field="belt_id",
        corrected_value="PURPLE",
        blocker_code=MISSING_BELT,
        before_body=before,
        before_registration_id=before_id,
        after_body=after,
        after_registration_id=after_id,
        control_registration_id=control_id,
        control_before=control_before,
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_c10_a7_fecha_nacimiento_corregida_quita_missing_dob(scenario: C1Scenario) -> None:
    """§10.DOB: ``a7`` sin fecha bloquea por ``MISSING_DATE_OF_BIRTH``; se corrige por el perfil real."""
    dataset = scenario.dataset
    control_id, control_before = await control_registration(
        scenario, "b4", Decimal("55.00"), "GI"
    )

    before_id = await register(scenario, "before", "a7", weight=Decimal("74.50"), modality="GI")
    before = await classify(scenario, "before", before_id)
    assert MISSING_DATE_OF_BIRTH in codes(before), codes(before)

    status_code, body = await update_profile_http(
        club=dataset.club,
        competitor_id=int(dataset.competitors["a7"]),
        token=dataset.owner_token,
        date_of_birth=date(1990, 5, 17),
        sex="MALE",
        belt_id=int(dataset.belts["WHITE"]),
    )
    assert status_code == 200, body

    after_id = await register(scenario, "after", "a7", weight=Decimal("74.50"), modality="GI")
    after = await classify(scenario, "after", after_id)

    await assert_correction(
        scenario,
        case="dob",
        competitor_key="a7",
        field="date_of_birth",
        corrected_value="1990-05-17",
        blocker_code=MISSING_DATE_OF_BIRTH,
        before_body=before,
        before_registration_id=before_id,
        after_body=after,
        after_registration_id=after_id,
        control_registration_id=control_id,
        control_before=control_before,
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_c10_c3_modalidad_corregida_quita_missing_modality(scenario: C1Scenario) -> None:
    """§10.modalidad: ``c3`` sin modalidad bloquea por ``MISSING_MODALITY``; se corrige al declarar GI."""
    control_id, control_before = await control_registration(
        scenario, "c2", Decimal("55.00"), "GI"
    )

    before_id = await register(scenario, "before", "c3", weight=Decimal("74.50"), modality=None)
    before = await classify(scenario, "before", before_id)
    assert MISSING_MODALITY in codes(before), codes(before)

    after_id = await register(scenario, "after", "c3", weight=Decimal("74.50"), modality="GI")
    after = await classify(scenario, "after", after_id)

    await assert_correction(
        scenario,
        case="modalidad",
        competitor_key="c3",
        field="modality",
        corrected_value="GI",
        blocker_code=MISSING_MODALITY,
        before_body=before,
        before_registration_id=before_id,
        after_body=after,
        after_registration_id=after_id,
        control_registration_id=control_id,
        control_before=control_before,
    )


# ================================================================ §11 ASIGNAR categoria


@pytest.mark.asyncio(loop_scope="session")
async def test_c11_owner_asigna_recomendada_sin_override(scenario: C1Scenario) -> None:
    """§11.OWNER: asigna la recomendada sin ``override``, la relee y la clasificacion se resuelve."""
    dataset = scenario.dataset
    surface = scenario.after_surface
    light = surface.key("cat-male-gi-light")
    registration_id = await register(
        scenario, "after", "a1", weight=Decimal("74.50"), modality="GI"
    )

    before = await classify(scenario, "after", registration_id)
    assert codes(before) == [CATEGORY_NOT_RESOLVED], codes(before)
    assert str(before["recommended_category"]["category_key"]) == light

    status_code, body = await assign_category_http(
        tournament_id=int(surface.tournament),
        registration_id=registration_id,
        token=dataset.owner_token,
    )
    assert status_code == 200, body
    assert body["category_key"] == light and body["category_label"] == "cat-male-gi-light"

    reread_body = await reread(scenario, "after", registration_id)
    assert reread_body["category_key"] == light
    assert reread_body["category_label"] == "cat-male-gi-light"

    after = await classify(scenario, "after", registration_id)
    assert codes(after) == [], codes(after)
    assert str(after["recommended_category"]["category_key"]) == light

    event = await latest_category_change_event(registration_id)
    assert event is not None, "el cambio de categoria debe auditarse"
    assert event["reason_code"] == "CATEGORY_CHANGE"
    assert await actor_relation(int(event["actor_user_id"]), dataset.club) == "OWNER"

    record_assignment(
        name="§11.owner.sin_override",
        registration_id=registration_id,
        tournament_id=int(surface.tournament),
        actor="OWNER",
        request={"category_key": None, "override": False},
        status=status_code,
        detail={"category_key": body["category_key"], "category_label": body["category_label"]},
        category_key_after=body["category_key"],
        category_label_after=body["category_label"],
    )
    record_validation("§11.owner.sin_override", True, {"event": event})


@pytest.mark.asyncio(loop_scope="session")
async def test_c11_collaborator_tambien_asigna_sin_override(scenario: C1Scenario) -> None:
    """§11.COLLABORATOR: el contrato real permite la asignacion normal (sin ``override``)."""
    dataset = scenario.dataset
    surface = scenario.after_surface
    light = surface.key("cat-male-gi-light")
    registration_id = await register(
        scenario, "after", "i1", weight=Decimal("74.50"), modality="GI"
    )

    status_code, body = await assign_category_http(
        tournament_id=int(surface.tournament),
        registration_id=registration_id,
        token=dataset.lab.collaborator_token,
    )
    assert status_code == 200, body
    assert body["category_key"] == light

    event = await latest_category_change_event(registration_id)
    assert event is not None and event["reason_code"] == "CATEGORY_CHANGE"
    assert await actor_relation(int(event["actor_user_id"]), dataset.club) == "COLLABORATOR"

    record_assignment(
        name="§11.collaborator.sin_override",
        registration_id=registration_id,
        tournament_id=int(surface.tournament),
        actor="COLLABORATOR",
        request={"category_key": None, "override": False},
        status=status_code,
        detail={"category_key": body["category_key"]},
        category_key_after=body["category_key"],
        category_label_after=body["category_label"],
    )
    record_validation("§11.collaborator.sin_override", True, {"event": event})


@pytest.mark.asyncio(loop_scope="session")
async def test_c11_idempotencia_replay_y_conflicto(scenario: C1Scenario) -> None:
    """§11.idempotencia: misma clave + mismo cuerpo -> replay; misma clave + cuerpo distinto -> 409."""
    dataset = scenario.dataset
    surface = scenario.after_surface
    female = surface.key("cat-female-gi")
    registration_id = await register(
        scenario, "after", "a5", weight=Decimal("55.00"), modality="GI"
    )
    key = fresh_key()

    first_status, first_body = await assign_category_http(
        tournament_id=int(surface.tournament),
        registration_id=registration_id,
        token=dataset.owner_token,
        key=key,
    )
    assert first_status == 200, first_body
    assert first_body["category_key"] == female
    events_after_first = await count_category_change_events(registration_id)
    assert events_after_first == 1

    replay_status, replay_body = await assign_category_http(
        tournament_id=int(surface.tournament),
        registration_id=registration_id,
        token=dataset.owner_token,
        key=key,
    )
    assert replay_status == 200, replay_body
    assert replay_body["id"] == first_body["id"]
    assert replay_body["category_key"] == female
    assert replay_body["revision"] == first_body["revision"]
    assert await count_category_change_events(registration_id) == events_after_first, "el replay audita"

    conflict_status, conflict_body = await assign_category_http(
        tournament_id=int(surface.tournament),
        registration_id=registration_id,
        token=dataset.owner_token,
        key=key,
        reason_note="cuerpo distinto con la misma clave",
    )
    assert conflict_status == 409, conflict_body
    assert conflict_body["detail"] == "IDEMPOTENCY_KEY_REUSED"

    reread_body = await reread(scenario, "after", registration_id)
    assert reread_body["category_key"] == female

    record_assignment(
        name="§11.idempotencia.replay",
        registration_id=registration_id,
        tournament_id=int(surface.tournament),
        actor="OWNER",
        request={"category_key": None, "override": False, "key": key},
        status=replay_status,
        detail={"replay_de": first_status, "category_key": replay_body["category_key"]},
        category_key_after=replay_body["category_key"],
        note="misma clave + mismo cuerpo",
    )
    record_assignment(
        name="§11.idempotencia.conflicto",
        registration_id=registration_id,
        tournament_id=int(surface.tournament),
        actor="OWNER",
        request={"category_key": None, "override": False, "key": key, "reason_note": "distinto"},
        status=conflict_status,
        detail=conflict_body,
        note="misma clave + cuerpo distinto",
    )
    record_validation(
        "§11.idempotencia",
        True,
        {"replay": replay_status, "conflicto": conflict_status, "detalle": conflict_body},
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_c11_readiness_refleja_la_asignacion(scenario: C1Scenario) -> None:
    """§11.readiness: una confirmada sin clasificar bloquea; tras asignar, deja de bloquear."""
    dataset = scenario.dataset
    surface = scenario.after_surface
    female = surface.key("cat-female-gi")
    registration_id = await register(
        scenario, "after", "a3", weight=Decimal("55.00"), modality="GI"
    )

    confirmed_status, confirmed_body = await confirm_registration_http(
        tournament_id=int(surface.tournament),
        registration_id=registration_id,
        token=dataset.owner_token,
    )
    assert confirmed_status == 200, confirmed_body

    before = await readiness_http(tournament_id=int(surface.tournament), token=dataset.owner_token)
    assert CATEGORY_NOT_RESOLVED in issue_codes(before), issue_codes(before)
    assert category_group(before, female) is None

    status_code, body = await assign_category_http(
        tournament_id=int(surface.tournament),
        registration_id=registration_id,
        token=dataset.owner_token,
    )
    assert status_code == 200, body
    assert body["category_key"] == female

    after = await readiness_http(tournament_id=int(surface.tournament), token=dataset.owner_token)
    assert CATEGORY_NOT_RESOLVED not in issue_codes(after), issue_codes(after)
    group = category_group(after, female)
    assert group is not None and group["confirmed_count"] >= 1, group

    record_assignment(
        name="§11.readiness",
        registration_id=registration_id,
        tournament_id=int(surface.tournament),
        actor="OWNER",
        request={"category_key": None, "override": False},
        status=status_code,
        detail={"category_key": body["category_key"]},
        category_key_after=body["category_key"],
    )
    record_validation(
        "§11.readiness",
        True,
        {
            "issues_antes": issue_codes(before),
            "issues_despues": issue_codes(after),
            "grupo": {
                "category_key": group["category_key"],
                "confirmed_count": group["confirmed_count"],
            },
        },
    )


# ================================================================ §12 OVERRIDE OWNER


@pytest.mark.asyncio(loop_scope="session")
async def test_c12_incompatible_sin_override_es_rechazada(scenario: C1Scenario) -> None:
    """§12.sin_override: fijar una categoria incompatible sin ``override`` -> 400 del contrato."""
    dataset = scenario.dataset
    surface = scenario.after_surface
    female = surface.key("cat-female-gi")
    heavy = surface.key("cat-male-gi-heavy")
    registration_id = await register(
        scenario, "after", "a6", weight=Decimal("100.00"), modality="GI"
    )

    before = await classify(scenario, "after", registration_id)
    assert [str(c["category_key"]) for c in before["compatible_categories"]] == [heavy]
    assert str(before["recommended_category"]["category_key"]) == heavy

    status_code, body = await assign_category_http(
        tournament_id=int(surface.tournament),
        registration_id=registration_id,
        token=dataset.owner_token,
        category_key=female,
        override=False,
    )
    assert status_code == 400, body
    assert body["detail"] == "Registration request is invalid"

    reread_body = await reread(scenario, "after", registration_id)
    assert reread_body["category_key"] is None
    assert await count_category_change_events(registration_id) == 0

    record_assignment(
        name="§12.sin_override",
        registration_id=registration_id,
        tournament_id=int(surface.tournament),
        actor="OWNER",
        request={"category_key": female, "override": False},
        status=status_code,
        detail=body,
        category_key_after=reread_body["category_key"],
        note="sin override -> rechazo",
    )
    record_validation("§12.sin_override", True, {"status": status_code, "detail": body})


@pytest.mark.asyncio(loop_scope="session")
async def test_c12_collaborator_con_override_es_403(scenario: C1Scenario) -> None:
    """§12.COLLABORATOR: ``override=true`` es OWNER-only -> 403 con el codigo real del backend."""
    dataset = scenario.dataset
    surface = scenario.after_surface
    female = surface.key("cat-female-gi")
    registration_id = await register(
        scenario, "after", "c1", weight=Decimal("100.00"), modality="GI"
    )

    status_code, body = await assign_category_http(
        tournament_id=int(surface.tournament),
        registration_id=registration_id,
        token=dataset.lab.collaborator_token,
        category_key=female,
        override=True,
        reason_code="CATEGORY_CHANGE",
    )
    assert status_code == 403, body
    assert body["detail"] == "ROLE_NOT_ALLOWED"

    reread_body = await reread(scenario, "after", registration_id)
    assert reread_body["category_key"] is None
    assert await count_category_change_events(registration_id) == 0

    record_assignment(
        name="§12.collaborator.override",
        registration_id=registration_id,
        tournament_id=int(surface.tournament),
        actor="COLLABORATOR",
        request={"category_key": female, "override": True},
        status=status_code,
        detail=body,
        category_key_after=reread_body["category_key"],
        note="override OWNER-only",
    )
    record_validation("§12.collaborator.override", True, {"status": status_code, "detail": body})


@pytest.mark.asyncio(loop_scope="session")
async def test_c12_owner_override_exige_motivo_y_audita(scenario: C1Scenario) -> None:
    """§12.OWNER: sin motivo -> 400; con motivo -> 200, auditado y reflejado en readiness."""
    dataset = scenario.dataset
    surface = scenario.after_surface
    female = surface.key("cat-female-gi")
    heavy = surface.key("cat-male-gi-heavy")
    registration_id = await register(
        scenario, "after", "c5", weight=Decimal("100.00"), modality="GI"
    )
    confirmed_status, confirmed_body = await confirm_registration_http(
        tournament_id=int(surface.tournament),
        registration_id=registration_id,
        token=dataset.owner_token,
    )
    assert confirmed_status == 200, confirmed_body

    # (a) override sin motivo -> rechazo del contrato; la inscripcion no cambia.
    no_reason_status, no_reason_body = await assign_category_http(
        tournament_id=int(surface.tournament),
        registration_id=registration_id,
        token=dataset.owner_token,
        category_key=female,
        override=True,
        reason_code=None,
    )
    assert no_reason_status == 400, no_reason_body
    assert no_reason_body["detail"] == "Registration request is invalid"
    assert (await reread(scenario, "after", registration_id))["category_key"] is None
    record_assignment(
        name="§12.owner.override.sin_motivo",
        registration_id=registration_id,
        tournament_id=int(surface.tournament),
        actor="OWNER",
        request={"category_key": female, "override": True, "reason_code": None},
        status=no_reason_status,
        detail=no_reason_body,
        category_key_after=None,
        note="motivo obligatorio",
    )

    # (b) override con motivo -> aceptado y auditado. El codigo CATEGORY_CHANGE no admite nota libre
    # (NOTE_ALLOWED_CODES), asi que el "motivo" es el propio reason_code.
    ok_status, ok_body = await assign_category_http(
        tournament_id=int(surface.tournament),
        registration_id=registration_id,
        token=dataset.owner_token,
        category_key=female,
        override=True,
        reason_code="CATEGORY_CHANGE",
    )
    assert ok_status == 200, ok_body
    assert ok_body["category_key"] == female and ok_body["category_label"] == "cat-female-gi"

    event = await latest_category_change_event(registration_id)
    assert event is not None, "el override debe auditarse"
    assert event["entity"] == "tournament_registration"
    assert event["action"] == "UPDATE"
    assert event["reason_code"] == "CATEGORY_CHANGE"
    assert event["reason"] == "cambio de categoria en el borrador"
    assert event["reason_note"] is None
    assert "category_key" in event["changed_fields"]
    assert "category_label" in event["changed_fields"]
    assert await actor_relation(int(event["actor_user_id"]), dataset.club) == "OWNER"
    record_override_event(event)

    # La clasificacion sigue diciendo que la elegida es incompatible, pero el readiness la legitimiza.
    after = await classify(scenario, "after", registration_id)
    assert str(after["recommended_category"]["category_key"]) == heavy
    assert CATEGORY_INCOMPATIBLE in codes(after), codes(after)

    readiness = await readiness_http(
        tournament_id=int(surface.tournament), token=dataset.owner_token
    )
    group = category_group(readiness, female)
    assert group is not None and group["confirmed_count"] >= 1, group
    assert CATEGORY_INCOMPATIBLE not in issue_codes(readiness), issue_codes(readiness)

    record_assignment(
        name="§12.owner.override.con_motivo",
        registration_id=registration_id,
        tournament_id=int(surface.tournament),
        actor="OWNER",
        request={"category_key": female, "override": True, "reason_code": "CATEGORY_CHANGE"},
        status=ok_status,
        detail={
            "category_key": ok_body["category_key"],
            "category_label": ok_body["category_label"],
        },
        category_key_after=ok_body["category_key"],
        category_label_after=ok_body["category_label"],
        note="aceptado y auditado",
    )
    record_validation(
        "§12.owner.override",
        True,
        {"sin_motivo": no_reason_status, "con_motivo": ok_status, "evento": event},
    )
