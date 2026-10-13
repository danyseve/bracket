# pylint: disable=redefined-outer-name
"""Contrato HTTP de la bandera de escritura de F3 (S3.6b, F3B, LAB ONLY).

TDD: este modulo se escribio **antes** de la implementacion y fallo en rojo (la puerta y su codigo
no existian). Fija el contrato descrito en ``docs/26`` §47:

* apagada: la lectura sigue publicandose (ventana, listado, detalle y readiness) y **las ocho**
  familias de escritura de F3 responden ``503 F3_WRITE_DISABLED`` sin reservar la
  ``Idempotency-Key``, sin bloquear filas, sin escribir dominio y sin auditar;
* encendida: el comportamiento certificado de S3.4/S3.5 no cambia, incluida la autorizacion de
  cada operacion (OWNER-only donde lo era, 401 del actor inactivo, 404 del tenant ajeno);
* la puerta alcanza **exactamente** esas ocho rutas y ninguna otra: ni la lectura de F3 ni ninguna
  ruta legacy la llevan.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any, Final
from uuid import uuid4

import aiohttp
import pytest
import pytest_asyncio
from fastapi.routing import APIRoute
from starlette import status

from bracket.app import app
from bracket.config import config
from bracket.database import database
from bracket.routes.domain_idempotency import IDEMPOTENCY_KEY_HEADER
from bracket.routes.f3_write_gate import F3_WRITE_DISABLED, require_f3_write_enabled
from bracket.utils.id_types import TournamentId
from tests.integration_tests.api.generate_bracket_http_support import (
    SYNTHETIC_SECRET,
    GenerationLab,
    generation_lab_context,
    insert_registration,
)
from tests.integration_tests.api.shared import get_root_uvicorn_url

#: Las familias de escritura de F3 (inscripciones, generacion, competidores, afiliaciones,
#: logo de academia y tatami).
EXPECTED_GUARDED: Final = (
    "POST /tournaments/{tournament_id}/generate-bracket",
    "PUT /tournaments/{tournament_id}/stage-items/{stage_item_id}/court",
    "POST /tournaments/{tournament_id}/schedule/generate",
    "POST /tournaments/{tournament_id}/schedule/recalculate",
    "POST /tournaments/{tournament_id}/registration-window",
    "POST /tournaments/{tournament_id}/registrations",
    "POST /tournaments/{tournament_id}/registrations/close",
    "POST /tournaments/{tournament_id}/registrations/{registration_id}/category",
    "POST /tournaments/{tournament_id}/registrations/{registration_id}/confirm",
    "POST /tournaments/{tournament_id}/registrations/{registration_id}/disqualify",
    "POST /tournaments/{tournament_id}/registrations/{registration_id}/reinstate",
    "POST /tournaments/{tournament_id}/registrations/{registration_id}/withdraw",
    "POST /clubs/{club_id}/competitors",
    "POST /clubs/{club_id}/competitors/{competitor_id}/activate",
    "POST /clubs/{club_id}/competitors/{competitor_id}/deactivate",
    "POST /clubs/{club_id}/competitors/{competitor_id}/rename",
    "POST /clubs/{club_id}/competitors/{competitor_id}/profile",
    "POST /clubs/{club_id}/competitors/{competitor_id}/affiliations",
    "POST /clubs/{club_id}/competitors/{competitor_id}/affiliations/{affiliation_id}/close",
    "POST /clubs/{club_id}/competitors/{competitor_id}/affiliations/{affiliation_id}/primary",
    "POST /clubs/{club_id}/competitors/{competitor_id}/independence",
    "POST /clubs/{club_id}/sports-clubs/{sports_club_id}/logo",
)

#: Campos publicados de la ventana, del listado, del readiness y del detalle (lista blanca).
WINDOW_FIELDS: Final = {
    "tournament_id",
    "registration_deadline",
    "registrations_closed_at",
    "state",
    "registrations_open",
}
LIST_FIELDS: Final = {"count", "registrations"}
READINESS_FIELDS: Final = {
    "tournament_id",
    "ready",
    "registration_window",
    "summary",
    "categories",
    "blocking_issues",
}


@pytest.fixture(scope="module", autouse=True)
def idempotency_secret() -> Iterator[None]:
    """Clave HMAC sintetica del laboratorio: sin ella el protocolo falla cerrado.

    La app corre en el mismo proceso, asi que parchear la configuracion alcanza al servidor de
    pruebas. El valor es sintetico y no sale de aqui.
    """
    patcher = pytest.MonkeyPatch()
    patcher.setattr(config, "idempotency_hmac_key", SYNTHETIC_SECRET)
    patcher.setattr(config, "idempotency_hmac_key_version", "v1")
    yield
    patcher.undo()


@pytest_asyncio.fixture(loop_scope="session", scope="function")
async def lab() -> AsyncIterator[GenerationLab]:
    async with generation_lab_context() as generation_lab:
        yield generation_lab


async def call(
    method: str,
    path: str,
    *,
    token: str | None = None,
    key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Peticion cruda contra el servidor de pruebas; la sesion es de la peticion."""
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if key is not None:
        headers[IDEMPOTENCY_KEY_HEADER] = key
    async with aiohttp.ClientSession() as session:
        async with session.request(
            method, get_root_uvicorn_url() + path, headers=headers, json=payload
        ) as response:
            text = await response.text()
            content_type = response.headers.get("content-type", "")
            assert content_type.startswith("application/json"), text
            return response.status, json.loads(text)


def draft_payload(lab: GenerationLab) -> dict[str, Any]:
    """Alta valida para el modelo: competidor del tenant, independiente y con categoria."""
    return {
        "competitor_id": int(lab.competitors[0]),
        "representation": "INDEPENDENT",
        "category_key": "gi-peso-83",
        "category_label": "Gi -83 kg",
        "reason_code": "PLANNED_ENTRY",
    }


def write_requests(
    lab: GenerationLab, registration_id: int
) -> list[tuple[str, str, dict[str, Any] | None]]:
    """Las ocho familias de escritura, cada una con un cuerpo valido para su modelo."""
    root = f"tournaments/{lab.tournament}"
    return [
        ("alta", f"{root}/registrations", draft_payload(lab)),
        ("confirmar", f"{root}/registrations/{registration_id}/confirm", {"reason_code": "READY"}),
        (
            "retirar",
            f"{root}/registrations/{registration_id}/withdraw",
            {"reason_code": "WITHDRAWAL_REQUEST"},
        ),
        (
            "readmitir",
            f"{root}/registrations/{registration_id}/reinstate",
            {"reason_code": "READY"},
        ),
        (
            "descalificar",
            f"{root}/registrations/{registration_id}/disqualify",
            {"reason_code": "RULE_VIOLATION", "reason_note": "nota de laboratorio"},
        ),
        ("plazo", f"{root}/registration-window", {"registration_deadline": None}),
        ("cierre", f"{root}/registrations/close", None),
        ("generacion", f"{root}/generate-bracket", None),
    ]


async def reservation_count(paths: list[str]) -> int:
    return int(
        await database.fetch_val(
            query="SELECT count(*) FROM domain_idempotency_keys WHERE request_path = ANY(:paths)",
            values={"paths": paths},
        )
    )


async def registration_count(tournament_id: TournamentId) -> int:
    return int(
        await database.fetch_val(
            query="SELECT count(*) FROM tournament_registrations WHERE tournament_id = :id",
            values={"id": tournament_id},
        )
    )


async def audit_count(tournament_id: TournamentId) -> int:
    return int(
        await database.fetch_val(
            query=(
                "SELECT count(*) FROM domain_change_log WHERE tenant_club_id = "
                "(SELECT club_id FROM tournaments WHERE id = :id)"
            ),
            values={"id": tournament_id},
        )
    )


async def tournament_state(tournament_id: TournamentId) -> tuple[Any, ...]:
    """Estado de la ventana y del torneo: deadline, cierre y estado publico."""
    row = await database.fetch_one(
        query=(
            "SELECT registration_deadline, registrations_closed_at, status FROM tournaments "
            "WHERE id = :id"
        ),
        values={"id": tournament_id},
    )
    assert row is not None
    return tuple(row._mapping.values())


# --- 1. la puerta alcanza exactamente las rutas de escritura de F3 ----------------------------


def test_gate_is_attached_only_to_the_f3_write_routes() -> None:
    """Cada escritura F3 lleva la puerta; la lectura y las rutas legacy no la llevan."""
    guarded = sorted(
        f"{sorted(route.methods)[0]} {route.path}"
        for route in app.routes
        if isinstance(route, APIRoute)
        and any(
            dependency.dependency is require_f3_write_enabled for dependency in route.dependencies
        )
    )

    assert guarded == sorted(EXPECTED_GUARDED)


# --- 2. apagada: la lectura sigue publicandose ------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_reads_are_not_gated(lab: GenerationLab, monkeypatch: pytest.MonkeyPatch) -> None:
    """Con la escritura apagada, ventana, listado, detalle y readiness siguen respondiendo."""
    monkeypatch.setattr(config, "f3_write_enabled", False)
    registration_id = await insert_registration(lab.tournament, competitor_id=lab.competitors[0])
    root = f"tournaments/{lab.tournament}"

    window_status, window = await call("GET", f"{root}/registration-window", token=lab.owner_token)
    list_status, listing = await call("GET", f"{root}/registrations", token=lab.owner_token)
    detail_status, detail = await call(
        "GET", f"{root}/registrations/{registration_id}", token=lab.owner_token
    )
    readiness_status, readiness = await call(
        "GET", f"{root}/bracket-readiness", token=lab.owner_token
    )

    assert (window_status, list_status, detail_status, readiness_status) == (
        status.HTTP_200_OK,
        status.HTTP_200_OK,
        status.HTTP_200_OK,
        status.HTTP_200_OK,
    )
    assert set(window) == WINDOW_FIELDS and window["tournament_id"] == lab.tournament
    assert set(listing) == LIST_FIELDS and listing["count"] == 1
    assert detail["id"] == registration_id and detail["status"] == "CONFIRMED"
    assert set(readiness) == READINESS_FIELDS and readiness["ready"] is False


# --- 3. apagada: toda escritura se rechaza antes de cualquier efecto ---------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_every_write_family_is_rejected(
    lab: GenerationLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Las ocho familias responden 503 con el codigo estable, sea cual sea el llamante."""
    monkeypatch.setattr(config, "f3_write_enabled", False)
    registration_id = await insert_registration(lab.tournament, competitor_id=lab.competitors[0])

    for label, path, payload in write_requests(lab, registration_id):
        status_code, body = await call(
            "POST", path, token=lab.owner_token, key=uuid4().hex, payload=payload
        )
        assert status_code == status.HTTP_503_SERVICE_UNAVAILABLE, (label, body)
        assert body == {"detail": F3_WRITE_DISABLED}, (label, body)

    collaborator_status, collaborator_body = await call(
        "POST",
        f"tournaments/{lab.tournament}/generate-bracket",
        token=lab.collaborator_token,
        key=uuid4().hex,
    )
    assert collaborator_status == status.HTTP_503_SERVICE_UNAVAILABLE
    assert collaborator_body == {"detail": F3_WRITE_DISABLED}


@pytest.mark.asyncio(loop_scope="session")
async def test_write_disabled_leaves_no_trace(
    lab: GenerationLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ni reserva, ni auditoria, ni cambio de dominio: la puerta se resuelve antes que todo eso."""
    monkeypatch.setattr(config, "f3_write_enabled", False)
    registration_id = await insert_registration(lab.tournament, competitor_id=lab.competitors[0])
    state_before = await tournament_state(lab.tournament)
    registrations_before = await registration_count(lab.tournament)
    audit_before = await audit_count(lab.tournament)

    paths: list[str] = []
    for _, path, payload in write_requests(lab, registration_id):
        paths.append(f"/{path}")
        status_code, _ = await call(
            "POST", path, token=lab.owner_token, key=uuid4().hex, payload=payload
        )
        assert status_code == status.HTTP_503_SERVICE_UNAVAILABLE

    assert await reservation_count(paths) == 0
    assert await registration_count(lab.tournament) == registrations_before
    assert await audit_count(lab.tournament) == audit_before
    assert await tournament_state(lab.tournament) == state_before


# --- 4. encendida: el comportamiento certificado no cambia -------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_write_enabled_keeps_the_certified_behaviour(
    lab: GenerationLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Encendida, la escritura funciona igual que en S3.4/S3.5: alta, replay, ventana y rechazo."""
    monkeypatch.setattr(config, "f3_write_enabled", True)
    root = f"tournaments/{lab.tournament}"
    payload = draft_payload(lab)
    key = uuid4().hex

    create_status, created = await call(
        "POST", f"{root}/registrations", token=lab.owner_token, key=key, payload=payload
    )
    assert create_status == status.HTTP_201_CREATED, created
    registration_id = created["id"]

    replay_status, replayed = await call(
        "POST", f"{root}/registrations", token=lab.owner_token, key=key, payload=payload
    )
    assert replay_status == status.HTTP_201_CREATED and replayed == created

    confirm_status, _ = await call(
        "POST",
        f"{root}/registrations/{registration_id}/confirm",
        token=lab.owner_token,
        key=uuid4().hex,
        payload={"reason_code": "READY"},
    )
    window_status, _ = await call(
        "POST",
        f"{root}/registration-window",
        token=lab.owner_token,
        key=uuid4().hex,
        payload={"registration_deadline": None},
    )
    close_status, _ = await call(
        "POST", f"{root}/registrations/close", token=lab.owner_token, key=uuid4().hex
    )
    readiness_status, readiness = await call(
        "GET", f"{root}/bracket-readiness", token=lab.owner_token
    )
    generate_status, generated = await call(
        "POST", f"{root}/generate-bracket", token=lab.owner_token, key=uuid4().hex
    )

    assert confirm_status == status.HTTP_200_OK
    assert window_status == status.HTTP_200_OK
    assert close_status == status.HTTP_200_OK
    assert readiness_status == status.HTTP_200_OK and readiness["ready"] is False
    assert generate_status == status.HTTP_409_CONFLICT
    assert generated["detail"]["code"] == "BRACKET_NOT_READY"


@pytest.mark.asyncio(loop_scope="session")
async def test_authorisation_still_applies_when_enabled(
    lab: GenerationLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La bandera no sustituye a la autorizacion: COLLAB 403, actor inactivo 401, ajeno 404."""
    monkeypatch.setattr(config, "f3_write_enabled", True)
    path = f"tournaments/{lab.tournament}/generate-bracket"

    collaborator_status, collaborator_body = await call(
        "POST", path, token=lab.collaborator_token, key=uuid4().hex
    )
    inactive_status, _ = await call("POST", path, token=lab.inactive_token, key=uuid4().hex)
    foreign_status, _ = await call(
        "POST",
        f"tournaments/{lab.foreign_tournament}/generate-bracket",
        token=lab.owner_token,
        key=uuid4().hex,
    )

    assert collaborator_status == status.HTTP_403_FORBIDDEN
    assert collaborator_body == {"detail": "ROLE_NOT_ALLOWED"}
    assert inactive_status == status.HTTP_401_UNAUTHORIZED
    assert foreign_status == status.HTTP_404_NOT_FOUND
    assert await reservation_count([f"/{path}"]) == 0
