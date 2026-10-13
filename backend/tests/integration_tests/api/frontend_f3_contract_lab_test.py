# pylint: disable=redefined-outer-name
# pylint: disable=too-many-statements  # escenarios de punta a punta, no codigo de produccion
"""Validacion LAB del contrato que consume el frontend de S4.1 (F3 CORE, LAB ONLY).

Este modulo no define un contrato nuevo: **repite por HTTP, contra la app real, la misma
secuencia de llamadas que emite la capa de servicios del frontend** (``services/f3.ts`` sobre las
rutas de ``f3_contract.ts``). Existe para cerrar el punto 18 de S4.1: arrancar el backend
controlado con ``ENVIRONMENT=CI`` y ``F3_WRITE_ENABLED=true`` contra ``bracket-ci-pg`` y confirmar
que los contratos que la UI da por ciertos son los que el backend publica de verdad.

Rutas replicadas (las mismas cadenas que construye ``f3_contract.ts``):

* ``POST/GET clubs/{club_id}/competitors`` y ``POST .../{id}/{rename,activate,deactivate}``
* ``POST tournaments/{id}/registrations`` y ``.../{confirm,withdraw,reinstate,disqualify}``
* ``GET/POST tournaments/{id}/registration-window`` y ``POST .../registrations/close``
* ``GET tournaments/{id}/bracket-readiness`` y ``POST .../generate-bracket``
* lecturas del tablero de tatamis: ``tournaments/{id}/{courts,stages,matches}`` y la unica
  asignacion persistida, ``PUT tournaments/{id}/matches/{match_id}`` con ``court_id``

Toda la evidencia sale del codigo de estado y del cuerpo publicado. Nada de esto toca produccion:
la base es la de CI y la app se levanta en el mismo proceso de pruebas.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from typing import Any, Final
from uuid import uuid4

import aiohttp
import pytest
import pytest_asyncio
from heliclockter import datetime_utc
from starlette import status

from bracket.config import config
from bracket.routes.domain_idempotency import IDEMPOTENCY_KEY_HEADER
from tests.integration_tests.api.competitor_http_support import (
    CompetitorLab,
    competitor_lab_context,
)
from tests.integration_tests.api.generate_bracket_http_support import (
    SYNTHETIC_SECRET,
    GenerationLab,
    bracket_structure,
    call_readiness,
    generate,
    generation_lab_context,
)
from tests.integration_tests.api.shared import get_root_uvicorn_url

#: Campos publicados que la UI consume. El perfil deportivo entra en S4.8a-3 y la lista blanca
#: del frontend (``f3_contract.ts``) lo incorpora en S4.8a-4: hasta entonces el contrato que fija
#: este test es el del backend.
COMPETITOR_FIELDS: Final = {
    "id",
    "display_name",
    "active",
    "created",
    "updated_at",
    "date_of_birth",
    "sex",
    "belt_id",
}
REGISTRATION_FIELDS: Final = {
    "id",
    "tournament_id",
    "competitor_id",
    "identity_status",
    "representation",
    "sports_club_id",
    "category_key",
    "category_label",
    "competitor_name_snapshot",
    "sports_club_name_snapshot",
    "status",
    "revision",
    "created",
    "updated_at",
}
WINDOW_FIELDS: Final = {
    "tournament_id",
    "registration_deadline",
    "registrations_closed_at",
    "state",
    "registrations_open",
}
READINESS_FIELDS: Final = {
    "tournament_id",
    "ready",
    "registration_window",
    "summary",
    "categories",
    "blocking_issues",
}
NOT_READY_FIELDS: Final = {"code", "blocking_issues", "registration_window"}
GENERATION_FIELDS: Final = {"tournament_id", "generated", "stage_items", "skipped_stage_items"}

#: Codigos que ``f3_errors.ts`` traduce a un mensaje de producto.
STATE_CONFLICT_DETAIL: Final = "Registration state does not allow this operation"
WRITE_DISABLED_DETAIL: Final = "F3_WRITE_DISABLED"
NOT_READY_CODE: Final = "BRACKET_NOT_READY"


@pytest.fixture(scope="module", autouse=True)
def idempotency_secret() -> Iterator[None]:
    """Clave HMAC sintetica del laboratorio: sin ella el protocolo de idempotencia falla cerrado."""
    patcher = pytest.MonkeyPatch()
    patcher.setattr(config, "idempotency_hmac_key", SYNTHETIC_SECRET)
    patcher.setattr(config, "idempotency_hmac_key_version", "v1")
    yield
    patcher.undo()


@pytest_asyncio.fixture(loop_scope="session", scope="function")
async def lab() -> AsyncIterator[GenerationLab]:
    async with generation_lab_context() as generation_lab:
        yield generation_lab


@pytest_asyncio.fixture(loop_scope="session", scope="function")
async def clubs() -> AsyncIterator[CompetitorLab]:
    async with competitor_lab_context() as competitor_lab:
        yield competitor_lab


# ---------------------------------------------------------------------------------------------
# Rutas: mismas cadenas que `f3_contract.ts`
# ---------------------------------------------------------------------------------------------


def competitors_url(club_id: int) -> str:
    return f"clubs/{club_id}/competitors"


def competitor_action_url(club_id: int, competitor_id: int, action: str) -> str:
    return f"{competitors_url(club_id)}/{competitor_id}/{action}"


def registrations_url(tournament_id: int) -> str:
    return f"tournaments/{tournament_id}/registrations"


def registration_action_url(tournament_id: int, registration_id: int, action: str) -> str:
    return f"{registrations_url(tournament_id)}/{registration_id}/{action}"


def window_url_path(tournament_id: int) -> str:
    return f"tournaments/{tournament_id}/registration-window"


def close_url_path(tournament_id: int) -> str:
    return f"{registrations_url(tournament_id)}/close"


def readiness_url_path(tournament_id: int) -> str:
    return f"tournaments/{tournament_id}/bracket-readiness"


def generate_url_path(tournament_id: int) -> str:
    return f"tournaments/{tournament_id}/generate-bracket"


async def call(
    method: str,
    path: str,
    *,
    token: str,
    key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Peticion con la misma forma que emite el frontend (Bearer + Idempotency-Key)."""
    headers = {"Authorization": f"Bearer {token}"}
    if key is not None:
        headers[IDEMPOTENCY_KEY_HEADER] = key
    url = get_root_uvicorn_url() + path
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, headers=headers, json=payload) as response:
            text = await response.text()
            assert text, f"{method} {path} devolvio un cuerpo vacio"
            return response.status, json.loads(text)


def issue_codes(issues: list[dict[str, Any]]) -> set[str]:
    """Codigos tecnicos de un listado de bloqueos, tal y como los consume la UI."""
    return {str(issue["code"]) for issue in issues}


def reason(reason_code: str | None = None, reason_note: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if reason_code is not None:
        body["reason_code"] = reason_code
    if reason_note is not None:
        body["reason_note"] = reason_note
    return body


# ---------------------------------------------------------------------------------------------
# 8. COMPETIDORES: list / create / rename / deactivate / activate tal y como los emite la UI
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_competidores_ciclo_de_vida_por_http(clubs: CompetitorLab) -> None:
    club_id = clubs.club
    token = clubs.owner_token
    display_name = f"s4-1-competidor-{uuid4().hex[:8]}"

    created_status, created = await call(
        "POST",
        competitors_url(club_id),
        token=token,
        key=uuid4().hex,
        payload={"display_name": display_name},
    )
    assert created_status == status.HTTP_201_CREATED, created
    assert set(created) == COMPETITOR_FIELDS
    assert created["display_name"] == display_name
    assert created["active"] is True
    competitor_id: int = created["id"]

    listed_status, listed = await call(
        "GET", f"{competitors_url(club_id)}?limit=100&offset=0", token=token
    )
    assert listed_status == status.HTTP_200_OK, listed
    assert set(listed) == {"count", "competitors"}
    assert any(row["id"] == competitor_id for row in listed["competitors"])

    renamed_status, renamed = await call(
        "POST",
        competitor_action_url(club_id, competitor_id, "rename"),
        token=token,
        key=uuid4().hex,
        payload={"display_name": f"{display_name}-2"},
    )
    assert renamed_status == status.HTTP_200_OK, renamed
    assert renamed["display_name"] == f"{display_name}-2"

    down_status, down = await call(
        "POST",
        competitor_action_url(club_id, competitor_id, "deactivate"),
        token=token,
        key=uuid4().hex,
        payload=reason("ADMINISTRATIVE"),
    )
    assert down_status == status.HTTP_200_OK, down
    assert down["active"] is False

    _, active_list = await call(
        "GET", f"{competitors_url(club_id)}?limit=100&offset=0", token=token
    )
    assert not any(row["id"] == competitor_id for row in active_list["competitors"])
    _, all_list = await call(
        "GET",
        f"{competitors_url(club_id)}?include_inactive=true&limit=100&offset=0",
        token=token,
    )
    assert any(row["id"] == competitor_id for row in all_list["competitors"])

    up_status, up = await call(
        "POST",
        competitor_action_url(club_id, competitor_id, "activate"),
        token=token,
        key=uuid4().hex,
        payload=reason("ADMINISTRATIVE"),
    )
    assert up_status == status.HTTP_200_OK, up
    assert up["active"] is True


@pytest.mark.asyncio(loop_scope="session")
async def test_alta_de_competidor_solo_por_nombre_y_sin_academia(clubs: CompetitorLab) -> None:
    """Gap de academia: el alta de la UI no lleva ``sports_club_id`` y el backend la acepta."""
    club_id = clubs.club
    display_name = f"s4-1-independiente-{uuid4().hex[:8]}"

    created_status, created = await call(
        "POST",
        competitors_url(club_id),
        token=clubs.owner_token,
        key=uuid4().hex,
        payload={"display_name": display_name},
    )
    assert created_status == status.HTTP_201_CREATED, created
    assert created["display_name"] == display_name


# ---------------------------------------------------------------------------------------------
# 9. INSCRIPCIONES: ciclo completo y rechazo de transiciones invalidas
# ---------------------------------------------------------------------------------------------


async def create_registration(
    lab: GenerationLab, competitor_id: int, *, key: str | None = None
) -> tuple[int, dict[str, Any]]:
    return await call(
        "POST",
        registrations_url(lab.tournament),
        token=lab.owner_token,
        key=key or uuid4().hex,
        payload={
            "competitor_id": competitor_id,
            "representation": "INDEPENDENT",
            "category_key": "gi-peso-83",
            "category_label": "Gi -83 kg",
            "reason_code": "PLANNED_ENTRY",
        },
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_inscripciones_ciclo_completo_y_fsm(lab: GenerationLab) -> None:
    competitor_id = lab.competitors[0]

    created_status, created = await create_registration(lab, competitor_id)
    assert created_status == status.HTTP_201_CREATED, created
    assert set(created) == REGISTRATION_FIELDS
    assert created["competitor_id"] == competitor_id
    assert created["representation"] == "INDEPENDENT"
    assert created["category_key"] == "gi-peso-83"
    registration_id: int = created["id"]

    listed_status, listed = await call(
        "GET", f"{registrations_url(lab.tournament)}?limit=100&offset=0", token=lab.owner_token
    )
    assert listed_status == status.HTTP_200_OK, listed
    assert set(listed) == {"count", "registrations"}
    assert any(row["id"] == registration_id for row in listed["registrations"])

    confirm_status, confirmed = await call(
        "POST",
        registration_action_url(lab.tournament, registration_id, "confirm"),
        token=lab.owner_token,
        key=uuid4().hex,
        payload=reason("READY"),
    )
    assert confirm_status == status.HTTP_200_OK, confirmed
    assert confirmed["status"] == "CONFIRMED"

    withdraw_status, withdrawn = await call(
        "POST",
        registration_action_url(lab.tournament, registration_id, "withdraw"),
        token=lab.owner_token,
        key=uuid4().hex,
        payload=reason("ADMINISTRATIVE"),
    )
    assert withdraw_status == status.HTTP_200_OK, withdrawn
    assert withdrawn["status"] == "WITHDRAWN"

    # Repetir la retirada es idempotente en el backend (200): la UI puede reintentar sin miedo y
    # el estado no cambia.
    repeat_status, repeat = await call(
        "POST",
        registration_action_url(lab.tournament, registration_id, "withdraw"),
        token=lab.owner_token,
        key=uuid4().hex,
        payload=reason("ADMINISTRATIVE"),
    )
    assert repeat_status == status.HTTP_200_OK, repeat
    assert repeat["status"] == "WITHDRAWN"

    reinstate_status, reinstated = await call(
        "POST",
        registration_action_url(lab.tournament, registration_id, "reinstate"),
        token=lab.owner_token,
        key=uuid4().hex,
        payload=reason("ADMINISTRATIVE"),
    )
    assert reinstate_status == status.HTTP_200_OK, reinstated
    assert reinstated["status"] == "CONFIRMED"

    disqualify_status, disqualified = await call(
        "POST",
        registration_action_url(lab.tournament, registration_id, "disqualify"),
        token=lab.owner_token,
        key=uuid4().hex,
        payload=reason("RULE_VIOLATION"),
    )
    assert disqualify_status == status.HTTP_200_OK, disqualified
    assert disqualified["status"] == "DISQUALIFIED"

    # Descalificar es terminal: la UI no ofrece readmision y el backend la rechaza con 409.
    terminal_status, terminal = await call(
        "POST",
        registration_action_url(lab.tournament, registration_id, "reinstate"),
        token=lab.owner_token,
        key=uuid4().hex,
        payload=reason("ADMINISTRATIVE"),
    )
    assert terminal_status == status.HTTP_409_CONFLICT, terminal
    assert terminal.get("detail") == STATE_CONFLICT_DETAIL, terminal


# ---------------------------------------------------------------------------------------------
# 10. VENTANA: OPEN, DEADLINE_PASSED, CLOSED y "cerrar no genera bracket"
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_ventana_estados_y_cierre_no_genera_bracket(lab: GenerationLab) -> None:
    get_status, window = await call("GET", window_url_path(lab.tournament), token=lab.owner_token)
    assert get_status == status.HTTP_200_OK, window
    assert set(window) == WINDOW_FIELDS
    assert window["state"] == "OPEN"
    assert window["registrations_open"] is True
    assert window["registration_deadline"] is None

    past = datetime_utc.now() - timedelta(hours=1)
    deadline_status, deadline = await call(
        "POST",
        window_url_path(lab.tournament),
        token=lab.owner_token,
        key=uuid4().hex,
        payload={"registration_deadline": past.isoformat()},
    )
    assert deadline_status == status.HTTP_200_OK, deadline
    assert deadline["state"] == "DEADLINE_PASSED"
    assert deadline["registrations_open"] is False

    clear_status, cleared = await call(
        "POST",
        window_url_path(lab.tournament),
        token=lab.owner_token,
        key=uuid4().hex,
        payload={"registration_deadline": None},
    )
    assert clear_status == status.HTTP_200_OK, cleared
    assert cleared["state"] == "OPEN"

    stages_before_status, stages_before = await call(
        "GET", f"tournaments/{lab.tournament}/stages", token=lab.owner_token
    )
    assert stages_before_status == status.HTTP_200_OK, stages_before

    close_status, closed = await call(
        "POST",
        close_url_path(lab.tournament),
        token=lab.owner_token,
        key=uuid4().hex,
        payload={},
    )
    assert close_status == status.HTTP_200_OK, closed
    assert closed["state"] == "CLOSED"
    assert closed["registrations_open"] is False
    assert closed["registrations_closed_at"] is not None

    # Cerrar la ventana NO genera bracket: la estructura de stages no cambia.
    _, stages_after = await call(
        "GET", f"tournaments/{lab.tournament}/stages", token=lab.owner_token
    )
    before_ids = sorted(item["id"] for item in stages_before["data"])
    after_ids = sorted(item["id"] for item in stages_after["data"])
    assert before_ids == after_ids


# ---------------------------------------------------------------------------------------------
# 11. READINESS: ready/no ready, resumen, categorias y bloqueos con su codigo tecnico
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_readiness_publica_bloqueos(lab: GenerationLab) -> None:
    empty = await call_readiness(lab)
    assert set(empty) == READINESS_FIELDS
    assert empty["ready"] is False
    assert set(empty["summary"]) == {
        "total",
        "confirmed",
        "draft",
        "withdrawn",
        "disqualified",
    }
    assert "REGISTRATION_WINDOW_OPEN" in issue_codes(empty["blocking_issues"]), empty

    first_status, _ = await create_registration(lab, lab.competitors[0])
    assert first_status == status.HTTP_201_CREATED
    second_status, _ = await create_registration(lab, lab.competitors[1])
    assert second_status == status.HTTP_201_CREATED

    pending = await call_readiness(lab)
    pending_codes = issue_codes(pending["blocking_issues"])
    assert "REGISTRATIONS_PENDING" in pending_codes, pending
    assert "REGISTRATION_WINDOW_OPEN" in pending_codes, pending
    assert pending["ready"] is False


# ---------------------------------------------------------------------------------------------
# 12. GENERAR BRACKETS: 409 BRACKET_NOT_READY, generacion real y derivacion del tablero
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_409_no_ready_y_flujo_real(lab: GenerationLab) -> None:
    not_ready_status, not_ready_body = await call(
        "POST",
        generate_url_path(lab.other_tournament),
        token=lab.owner_token,
        key=uuid4().hex,
        payload={},
    )
    assert not_ready_status == status.HTTP_409_CONFLICT, not_ready_body
    not_ready = not_ready_body["detail"]
    assert set(not_ready) == NOT_READY_FIELDS, not_ready_body
    assert not_ready["code"] == NOT_READY_CODE
    assert "REGISTRATION_WINDOW_OPEN" in issue_codes(not_ready["blocking_issues"])

    # Camino feliz: dos confirmadas en la misma categoria y la ventana cerrada.
    for competitor_id in (lab.competitors[0], lab.competitors[1]):
        create_status, created = await create_registration(lab, competitor_id)
        assert create_status == status.HTTP_201_CREATED, created
        confirm_status, confirmed = await call(
            "POST",
            registration_action_url(lab.tournament, created["id"], "confirm"),
            token=lab.owner_token,
            key=uuid4().hex,
            payload=reason("READY"),
        )
        assert confirm_status == status.HTTP_200_OK, confirmed
        assert confirmed["status"] == "CONFIRMED"

    close_status, closed = await call(
        "POST", close_url_path(lab.tournament), token=lab.owner_token, key=uuid4().hex, payload={}
    )
    assert close_status == status.HTTP_200_OK, closed
    assert closed["state"] == "CLOSED"

    ready = await call_readiness(lab)
    assert ready["ready"] is True, ready
    assert ready["blocking_issues"] == []

    # La generacion de F3 **reparte** las inscripciones en los cuadros ya existentes (F2C),
    # asi que el escenario del organizador incluye la estructura de fases del torneo.
    async with bracket_structure(lab):
        generated_status, _, generated = await generate(lab, key=uuid4().hex, token=lab.owner_token)
        assert generated_status == status.HTTP_200_OK, generated
        assert set(generated) == GENERATION_FIELDS
        assert generated["generated"] is True, generated
        assert generated["stage_items"], generated

        # El tablero de tatamis se deriva de courts + stages(con sus combates): la unica asociacion
        # persistida es match -> court, asi que sin asignar los combates llegan con court_id nulo.
        courts_status, courts = await call(
            "GET", f"tournaments/{lab.tournament}/courts", token=lab.owner_token
        )
        assert courts_status == status.HTTP_200_OK, courts
        stages_status, stages = await call(
            "GET", f"tournaments/{lab.tournament}/stages", token=lab.owner_token
        )
        assert stages_status == status.HTTP_200_OK, stages

        # No hay `GET /tournaments/{id}/matches`: la UI deriva los combates de la lectura de fases
        # (`stage_items[].rounds[].matches`), que es exactamente el anidamiento que aplana
        # `matchesOf` en el frontend.
        stage_items = [item for stage in stages["data"] for item in stage["stage_items"]]
        assert stage_items, stages
        match_rows = [
            match
            for item in stage_items
            for round_ in item["rounds"]
            for match in round_["matches"]
        ]
        assert match_rows, stages
        unassigned = [row for row in match_rows if row["court_id"] is None]
        assert unassigned, stages

        # Alta de tatami por la via que ya existe en la UI (POST tournaments/{id}/courts).
        court_status, court = await call(
            "POST",
            f"tournaments/{lab.tournament}/courts",
            token=lab.owner_token,
            key=uuid4().hex,
            payload={"name": f"tatami-s41-{uuid4().hex[:6]}"},
        )
        assert court_status == status.HTTP_200_OK, court
        # `SingleCourtResponse` anida la fila creada en `data`.
        court_id = court["data"]["id"]
        _, courts_after = await call(
            "GET", f"tournaments/{lab.tournament}/courts", token=lab.owner_token
        )
        assert any(item["id"] == court_id for item in courts_after["data"]), courts_after

        # La asignacion soportada hoy es combate a combate: no hay endpoint por stage-item.
        target = unassigned[0]
        assign_status, assigned = await call(
            "PUT",
            f"tournaments/{lab.tournament}/matches/{target['id']}",
            token=lab.owner_token,
            payload={
                "court_id": court_id,
                "custom_duration_minutes": target["custom_duration_minutes"],
                "custom_margin_minutes": target["custom_margin_minutes"],
                "round_id": target["round_id"],
                "stage_item_input1_score": target["stage_item_input1_score"],
                "stage_item_input2_score": target["stage_item_input2_score"],
            },
        )
        assert assign_status == status.HTTP_200_OK, assigned
        _, stages_after = await call(
            "GET", f"tournaments/{lab.tournament}/stages", token=lab.owner_token
        )
        rows_after = [
            match
            for stage in stages_after["data"]
            for item in stage["stage_items"]
            for round_ in item["rounds"]
            for match in round_["matches"]
        ]
        row = next(item for item in rows_after if item["id"] == target["id"])
        assert row["court_id"] == court_id

        # Un tatami con combates encima no se borra (`400 Could not delete court since it's used
        # by 1 matches`), asi que el flujo real primero libera el combate.
        release_status, released = await call(
            "PUT",
            f"tournaments/{lab.tournament}/matches/{target['id']}",
            token=lab.owner_token,
            key=uuid4().hex,
            payload={
                "court_id": None,
                "custom_duration_minutes": target["custom_duration_minutes"],
                "custom_margin_minutes": target["custom_margin_minutes"],
                "round_id": target["round_id"],
                "stage_item_input1_score": target["stage_item_input1_score"],
                "stage_item_input2_score": target["stage_item_input2_score"],
            },
        )
        assert release_status == status.HTTP_200_OK, released
        _, freed = await call("GET", f"tournaments/{lab.tournament}/stages", token=lab.owner_token)
        freed_row = next(
            match
            for stage in freed["data"]
            for item in stage["stage_items"]
            for round_ in item["rounds"]
            for match in round_["matches"]
            if match["id"] == target["id"]
        )
        assert freed_row["court_id"] is None, freed_row

        # El laboratorio borra el torneo al desmontar: dejar el tatami creado rompe esa limpieza,
        # asi que se retira por la via que ya existe en la UI.
        delete_status, deleted = await call(
            "DELETE",
            f"tournaments/{lab.tournament}/courts/{court_id}",
            token=lab.owner_token,
            key=uuid4().hex,
            payload={},
        )
        assert delete_status == status.HTTP_200_OK, deleted
        _, courts_final = await call(
            "GET", f"tournaments/{lab.tournament}/courts", token=lab.owner_token
        )
        assert all(item["id"] != court_id for item in courts_final["data"]), courts_final


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_sin_estructura_es_exito_vacio(lab: GenerationLab) -> None:
    """Sin cuadros en el torneo la generacion no falla: responde 200 sin repartir nada.

    La UI no debe pintarlo como error (ni como reparto): se muestra el aviso de que todavia no hay
    cuadros que repartir.
    """
    for competitor_id in (lab.competitors[0], lab.competitors[1]):
        create_status, created = await create_registration(lab, competitor_id)
        assert create_status == status.HTTP_201_CREATED, created
        confirm_status, confirmed = await call(
            "POST",
            registration_action_url(lab.tournament, created["id"], "confirm"),
            token=lab.owner_token,
            key=uuid4().hex,
            payload=reason("READY"),
        )
        assert confirm_status == status.HTTP_200_OK, confirmed

    close_status, closed = await call(
        "POST", close_url_path(lab.tournament), token=lab.owner_token, key=uuid4().hex, payload={}
    )
    assert close_status == status.HTTP_200_OK, closed
    assert (await call_readiness(lab))["ready"] is True

    empty_status, _, empty = await generate(lab, key=uuid4().hex, token=lab.owner_token)
    assert empty_status == status.HTTP_200_OK, empty
    assert empty["generated"] is False, empty
    assert empty["stage_items"] == [], empty
    assert empty["skipped_stage_items"] == 0, empty


# ---------------------------------------------------------------------------------------------
# 13. F3_WRITE_DISABLED: 503 en escritura, lectura intacta
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_write_disabled_responde_503_y_la_lectura_sigue(
    lab: GenerationLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "f3_write_enabled", False)
    try:
        write_status, write_body = await call(
            "POST",
            registrations_url(lab.tournament),
            token=lab.owner_token,
            key=uuid4().hex,
            payload={
                "competitor_id": lab.competitors[0],
                "representation": "INDEPENDENT",
                "category_key": "gi-peso-83",
                "category_label": "Gi -83 kg",
            },
        )
        assert write_status == status.HTTP_503_SERVICE_UNAVAILABLE, write_body
        assert write_body.get("detail") == WRITE_DISABLED_DETAIL, write_body

        read_status, window = await call(
            "GET", window_url_path(lab.tournament), token=lab.owner_token
        )
        assert read_status == status.HTTP_200_OK, window
        readiness_status, _ = await call(
            "GET", readiness_url_path(lab.tournament), token=lab.owner_token
        )
        assert readiness_status == status.HTTP_200_OK
    finally:
        monkeypatch.setattr(config, "f3_write_enabled", True)


# ---------------------------------------------------------------------------------------------
# 14. IDEMPOTENCIA: misma clave -> mismo resultado; clave nueva -> accion nueva
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_idempotencia_misma_clave_mismo_resultado(lab: GenerationLab) -> None:
    competitor_id = lab.competitors[2]
    key = uuid4().hex

    first_status, first = await create_registration(lab, competitor_id, key=key)
    assert first_status == status.HTTP_201_CREATED, first
    second_status, second = await create_registration(lab, competitor_id, key=key)
    assert second_status == status.HTTP_201_CREATED, second
    assert second["id"] == first["id"], (first, second)

    _, listed = await call(
        "GET",
        f"{registrations_url(lab.tournament)}?limit=100&offset=0",
        token=lab.owner_token,
    )
    matching = [
        row
        for row in listed["registrations"]
        if row["competitor_id"] == competitor_id and row["status"] != "DISQUALIFIED"
    ]
    assert len(matching) == 1, matching

    # Clave nueva sobre **otra** identidad: accion nueva, fila nueva. La misma identidad no admite
    # una segunda inscripcion viva (409 `Registration already exists`, que la UI marca duplicado).
    other_competitor = lab.competitors[3]
    third_status, third = await create_registration(lab, other_competitor)
    assert third_status == status.HTTP_201_CREATED, third
    assert third["id"] != first["id"]

    repeat_status, repeat = await create_registration(lab, competitor_id)
    assert repeat_status == status.HTTP_409_CONFLICT, repeat
    assert repeat.get("detail") == "Registration already exists", repeat


# ---------------------------------------------------------------------------------------------
# 15. AUTORIZACION: la UI no inventa el rol, se apoya en el 403 del backend
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_403_del_actor_sin_permiso_en_generacion(lab: GenerationLab) -> None:
    forbidden_status, forbidden = await call(
        "POST",
        generate_url_path(lab.tournament),
        token=lab.collaborator_token,
        key=uuid4().hex,
        payload={},
    )
    assert forbidden_status == status.HTTP_403_FORBIDDEN, forbidden
    assert forbidden.get("detail") in {"ROLE_NOT_ALLOWED", "TENANT_NOT_AUTHORIZED"}, forbidden

    unauthorized_status, unauthorized = await call(
        "POST",
        generate_url_path(lab.tournament),
        token=lab.inactive_token,
        key=uuid4().hex,
        payload={},
    )
    assert unauthorized_status == status.HTTP_401_UNAUTHORIZED, unauthorized
