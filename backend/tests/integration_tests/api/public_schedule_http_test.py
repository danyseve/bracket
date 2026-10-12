# pylint: disable=redefined-outer-name  # `public_lab` y `two_court_lab` son fixtures.
"""Pantalla publica del planificador: quien entra, que cuerpo sale y que no toca (S4.5, LAB ONLY).

``GET /public/tournaments/{id}/schedule`` es la unica puerta de la grada. La sirve el mismo cargador
que la vista del organizador (mismo cuerpo) y quien puede leerla lo decide el panel publico del
torneo. Aqui se fija, contra el servidor de verdad: que la pantalla solo lleva datos deportivos, que
los estados los decide el servidor, que leerla no mueve el plan y que no necesita la puerta de
escritura de F3 (la grada se lee aunque la escritura este apagada).
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any, Final

import aiohttp
import pytest
import pytest_asyncio
from starlette import status

from bracket.config import config
from bracket.database import database
from bracket.logic.scheduling.public_schedule import PUBLIC_MATCH_KEYS
from bracket.utils.id_types import TournamentId
from tests.integration_tests.api.schedule_lab_test import (
    COURT_DISPLAY_FIELDS,
    SCHEDULE_VIEW_FIELDS,
    START_TIME,
    ScheduleLab,
    court_display,
    generate_ok,
    persisted_state,
    read_view,
    schedule_lab_context,
)
from tests.integration_tests.api.shared import get_root_uvicorn_url
from tests.unit_tests.idempotency_test import SYNTHETIC_SECRET

#: Campos de la ficha publica de un combate: los del contrato, ni uno mas.
DISPLAY_MATCH_FIELDS: Final = set(PUBLIC_MATCH_KEYS)

#: Campos del bloque del torneo en el cuerpo publico.
PUBLIC_TOURNAMENT_FIELDS: Final = {"name", "start_time"}

#: Nada de esto puede aparecer en el cuerpo publico, a ninguna profundidad.
FORBIDDEN_KEYS: Final = frozenset(
    {
        "email",
        "date_of_birth",
        "jwt",
        "token",
        "password",
        "audit",
        "audit_log",
        "user_id",
        "club_id",
        "created_by",
        "idempotency_key",
        "competitor_id",
        "player_id",
        "registration",
    }
)

#: Vocabulario publico de estados (§11).
STATES: Final = {"NOW", "NEXT", "UPCOMING", "WAITING", "COMPLETED", "DELAYED"}

#: Una ficha de combate en el cuerpo publico, por su posicion en el arbol del JSON.
MATCH_ENTRY_PATH: Final = re.compile(
    r"^\$\.courts\[\d+\]\.(current|next|upcoming\[\d+\]|completed\[\d+\])$"
)

#: Un combate ocupa 10 + 5 minutos, asi que acabar a los 45 minutos deja 30 de retraso (§9).
LATE_FINISH_MINUTES: Final = 45
EXPECTED_LAG_MINUTES: Final = 30


def public_url(tournament_id: TournamentId) -> str:
    return f"{get_root_uvicorn_url()}public/tournaments/{int(tournament_id)}/schedule"


async def read_public(
    tournament_id: TournamentId, *, token: str | None = None
) -> tuple[int, dict[str, Any]]:
    """Peticion a la pantalla publica: sin sesion salvo que se pida una."""
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(public_url(tournament_id), headers=headers) as response:
            if response.status != status.HTTP_200_OK:
                return response.status, {}
            text = await response.text()
            assert response.headers.get("content-type", "").startswith("application/json"), text
            return response.status, json.loads(text)


async def set_dashboard_public(tournament_id: TournamentId, *, public: bool) -> None:
    """Enciende o apaga el panel publico del torneo: es la unica puerta de la grada."""
    await database.execute(
        query="UPDATE tournaments SET dashboard_public = :public WHERE id = :tournament_id",
        values={"public": public, "tournament_id": int(tournament_id)},
    )


async def public_reservations(tournament_id: TournamentId) -> int:
    """Reservas de idempotencia de la pantalla publica: tiene que ser siempre cero."""
    return int(
        await database.fetch_val(
            query="SELECT count(*) FROM domain_idempotency_keys WHERE request_path LIKE :path",
            values={"path": f"%/public/tournaments/{int(tournament_id)}/schedule%"},
        )
    )


def walk_public_payload(node: object, path: str) -> None:
    """Revisa el cuerpo entero: fichas exactas y ninguna clave privada a ninguna profundidad."""
    if isinstance(node, dict):
        if MATCH_ENTRY_PATH.match(path):
            assert set(node) == DISPLAY_MATCH_FIELDS, f"{path}: {sorted(node)}"
        for key, value in node.items():
            assert str(key).lower() not in FORBIDDEN_KEYS, f"{path}.{key} es una clave privada"
            walk_public_payload(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            walk_public_payload(item, f"{path}[{index}]")


@pytest.fixture(scope="module", autouse=True)
def idempotency_secret() -> Any:
    """Clave HMAC sintetica del laboratorio: el protocolo de escritura falla cerrado sin ella."""
    patcher = pytest.MonkeyPatch()
    patcher.setattr(config, "idempotency_hmac_key", SYNTHETIC_SECRET)
    patcher.setattr(config, "idempotency_hmac_key_version", "v1")
    yield
    patcher.undo()


@pytest_asyncio.fixture(loop_scope="session", scope="function")
async def public_lab() -> AsyncIterator[ScheduleLab]:
    """Un solo tatami: los tres combates van en la misma cola y se ven los estados en orden."""
    async with schedule_lab_context(court_count=1) as lab:
        yield lab


@pytest_asyncio.fixture(loop_scope="session", scope="function")
async def two_court_lab() -> AsyncIterator[ScheduleLab]:
    """Dos tatamis: el caso normal del laboratorio, para comparar con la vista del organizador."""
    async with schedule_lab_context(court_count=2) as lab:
        yield lab


# --- 1. la pantalla solo lleva datos deportivos -------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_the_public_screen_only_carries_sporting_data(public_lab: ScheduleLab) -> None:
    """El cuerpo publico es exactamente el contrato: nada de cuentas, tokens ni auditoria."""
    await generate_ok(public_lab, body={"start_time": START_TIME})

    status_code, payload = await read_public(public_lab.tournament)

    assert status_code == status.HTTP_200_OK
    assert set(payload) == SCHEDULE_VIEW_FIELDS
    assert set(payload["tournament"]) == PUBLIC_TOURNAMENT_FIELDS
    assert len(payload["courts"]) == 1
    for block in payload["courts"]:
        assert set(block) == COURT_DISPLAY_FIELDS, block

    # El guardian de la prueba es propio: recorre el JSON entero, no confia en el del servidor.
    walk_public_payload(payload, "$")

    # Y los datos deportivos si estan: los rivales de las semifinales tienen nombre.
    block = court_display(payload, public_lab.courts[0])
    assert block["current"]["fighter_a"] is not None
    assert block["current"]["fighter_b"] is not None
    assert payload["tournament"]["name"] == "Some Cool Tournament"


@pytest.mark.asyncio(loop_scope="session")
async def test_the_public_screen_hides_the_internal_write_state(public_lab: ScheduleLab) -> None:
    """La pantalla no expone el estado interno de escritura: ni una pista de idempotencia."""
    await generate_ok(public_lab, body={"start_time": START_TIME})

    _, payload = await read_public(public_lab.tournament)
    raw = json.dumps(payload)

    assert "idempotency" not in raw.lower()
    assert "audit" not in raw.lower()
    assert SYNTHETIC_SECRET not in raw
    assert await public_reservations(public_lab.tournament) == 0


# --- 2. quien puede leer la pantalla ------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_with_the_public_panel_off_nobody_gets_in(public_lab: ScheduleLab) -> None:
    """Con el panel publico apagado la ruta responde 401, y un torneo inexistente tambien."""
    await set_dashboard_public(public_lab.tournament, public=False)

    status_code, _ = await read_public(public_lab.tournament)
    assert status_code == status.HTTP_401_UNAUTHORIZED

    status_code, _ = await read_public(TournamentId(10**9))
    assert status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.asyncio(loop_scope="session")
async def test_with_the_public_panel_on_anybody_reads_and_the_organizer_too(
    public_lab: ScheduleLab,
) -> None:
    """Con el panel publico encendido entra cualquiera, y el organizador ve exactamente lo mismo."""
    await generate_ok(public_lab, body={"start_time": START_TIME})

    anonymous_status, anonymous = await read_public(public_lab.tournament)
    organizer_status, organizer = await read_public(
        public_lab.tournament, token=public_lab.lab.owner_token
    )

    assert anonymous_status == status.HTTP_200_OK
    assert organizer_status == status.HTTP_200_OK
    assert anonymous["courts"] == organizer["courts"]


@pytest.mark.asyncio(loop_scope="session")
async def test_the_public_screen_shares_the_body_with_the_organizer_view(
    two_court_lab: ScheduleLab,
) -> None:
    """La pantalla publica y la vista del organizador sirven el mismo cuerpo (§12)."""
    await generate_ok(two_court_lab, body={"start_time": START_TIME})

    _, public = await read_public(two_court_lab.tournament)
    organizer_status, organizer = await read_view(
        two_court_lab, token=two_court_lab.lab.owner_token
    )

    assert organizer_status == status.HTTP_200_OK
    assert set(public) == set(organizer) == SCHEDULE_VIEW_FIELDS
    assert public["courts"] == organizer["courts"]
    assert public["tournament"] == organizer["tournament"]


# --- 3. el servidor decide los estados ----------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_the_screen_receives_the_states_decided_by_the_server(
    public_lab: ScheduleLab,
) -> None:
    """AHORA, SIGUIENTE y lo que espera rivales: decidido en el servidor, sin derivarlo en la TV."""
    await generate_ok(public_lab, body={"start_time": START_TIME})

    _, payload = await read_public(public_lab.tournament)
    block = court_display(payload, public_lab.courts[0])

    assert block["lag_minutes"] == 0
    assert {block["current"]["match_id"], block["next"]["match_id"]} == {
        int(match_id) for match_id in public_lab.semi_ids
    }
    assert block["current"]["state"] == "NOW"
    assert block["next"]["state"] == "NEXT"
    assert block["current"]["scheduled_start"] == START_TIME
    assert block["current"]["estimated_start"] == START_TIME

    # La final tiene hora y tatami pero todavia no rivales: espera y dice a quien espera.
    waiting = block["upcoming"][0]
    assert waiting["match_id"] == int(public_lab.final_id)
    assert waiting["state"] == "WAITING"
    assert waiting["fighter_a"] is None
    assert set(waiting["waiting_for"]) == {
        f"Ganador del combate {int(match_id)}" for match_id in public_lab.semi_ids
    }

    shown = [block["current"], block["next"], *block["upcoming"]]
    assert {entry["state"] for entry in shown} <= STATES


@pytest.mark.asyncio(loop_scope="session")
async def test_the_screen_derives_completed_and_delayed_from_what_finished(
    public_lab: ScheduleLab,
) -> None:
    """Lo terminado pasa a COMPLETADO; lo que espera en ese tatami, a DELAYED con la hora nueva."""
    await generate_ok(public_lab, body={"start_time": START_TIME})
    # Se lee el plan antes de tocarlo: el primer combate del tatami es el que se termina tarde.
    _, planned = await read_public(public_lab.tournament)
    planned_block = court_display(planned, public_lab.courts[0])
    first_match = int(planned_block["current"]["match_id"])
    other_match = int(planned_block["next"]["match_id"])
    first_start = datetime.fromisoformat(str(planned_block["current"]["scheduled_start"]))
    await database.execute(
        query="UPDATE matches SET finished_at = :finished_at WHERE id = :match_id",
        values={
            "match_id": first_match,
            "finished_at": first_start + timedelta(minutes=LATE_FINISH_MINUTES),
        },
    )

    status_code, payload = await read_public(public_lab.tournament)

    assert status_code == status.HTTP_200_OK
    block = court_display(payload, public_lab.courts[0])
    assert block["lag_minutes"] == EXPECTED_LAG_MINUTES

    # Lo terminado: ultimo resultado del tatami, con su estado.
    assert block["completed"][0]["match_id"] == first_match
    assert block["completed"][0]["state"] == "COMPLETED"

    # Lo que sigue: el tatami va 30 minutos tarde; el combate que puede pelear sale retrasado.
    assert block["current"]["match_id"] == other_match
    assert block["current"]["state"] == "DELAYED"
    scheduled = datetime.fromisoformat(str(block["current"]["scheduled_start"]))
    estimated = datetime.fromisoformat(str(block["current"]["estimated_start"]))
    assert estimated - scheduled == timedelta(minutes=EXPECTED_LAG_MINUTES)

    # Y el que sigue esperando rivales sigue siendo WAITING, aunque el tatami vaya tarde.
    assert block["next"]["match_id"] == int(public_lab.final_id)
    assert block["next"]["state"] == "WAITING"

    walk_public_payload(payload, "$")


# --- 4. leer la pantalla no cambia nada ---------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_reading_the_public_screen_moves_nothing_and_reserves_nothing(
    public_lab: ScheduleLab,
) -> None:
    """Diez lecturas de la grada no mueven un combate, no reservan idempotencia y no escriben."""
    await generate_ok(public_lab, body={"start_time": START_TIME})
    before = await persisted_state(public_lab)
    reservations_before = await public_reservations(public_lab.tournament)

    for _ in range(10):
        status_code, _ = await read_public(public_lab.tournament)
        assert status_code == status.HTTP_200_OK

    assert await persisted_state(public_lab) == before
    assert await public_reservations(public_lab.tournament) == reservations_before == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_the_public_screen_is_readable_while_writing_is_disabled(
    public_lab: ScheduleLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Con ``F3_WRITE_ENABLED=false`` la grada sigue leyendo: el despliegue va sin escritura."""
    await generate_ok(public_lab, body={"start_time": START_TIME})
    monkeypatch.setattr(config, "f3_write_enabled", False)

    status_code, payload = await read_public(public_lab.tournament)

    assert status_code == status.HTTP_200_OK
    assert set(payload) == SCHEDULE_VIEW_FIELDS
    assert court_display(payload, public_lab.courts[0])["current"] is not None
    # La lectura no depende de la clave de idempotencia ni de la puerta de escritura.
    assert await public_reservations(public_lab.tournament) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_two_screens_of_the_same_tournament_see_the_same_plan(
    public_lab: ScheduleLab,
) -> None:
    """Dos pantallas a la vez leen el mismo plan: la lectura no compite con la lectura."""
    await generate_ok(public_lab, body={"start_time": START_TIME})

    first = await read_public(public_lab.tournament)
    second = await read_public(public_lab.tournament)

    assert first[0] == second[0] == status.HTTP_200_OK
    assert first[1]["courts"] == second[1]["courts"]
    assert await public_reservations(public_lab.tournament) == 0
    assert await persisted_state(public_lab) == await persisted_state(public_lab)


@pytest.mark.asyncio(loop_scope="session")
async def test_the_public_screen_reports_its_own_generation_time(public_lab: ScheduleLab) -> None:
    """El cuerpo dice cuando se genero: la pantalla puede avisar de que esta al dia."""
    await generate_ok(public_lab, body={"start_time": START_TIME})

    _, payload = await read_public(public_lab.tournament)
    generated_at = datetime.fromisoformat(str(payload["generated_at"]))

    assert generated_at.tzinfo is not None
    assert generated_at.utcoffset() == timedelta(0)


@pytest.mark.asyncio(loop_scope="session")
async def test_a_tournament_without_a_plan_still_serves_an_empty_screen(
    public_lab: ScheduleLab,
) -> None:
    """Sin plan todavia la pantalla responde: tatamis sin combates, no un error."""
    await generate_ok(public_lab, body={"start_time": START_TIME})
    await database.execute(
        query=(
            "UPDATE matches SET court_id = NULL, start_time = NULL, position_in_schedule = NULL"
            " WHERE round_id IN (SELECT id FROM rounds WHERE stage_item_id = :stage_item_id)"
        ),
        values={"stage_item_id": public_lab.stage_item_id},
    )

    status_code, payload = await read_public(public_lab.tournament)

    assert status_code == status.HTTP_200_OK
    block = court_display(payload, public_lab.courts[0])
    assert block["current"] is None
    assert block["next"] is None
    assert block["upcoming"] == []
    assert block["completed"] == []
    assert block["lag_minutes"] == 0
    assert await public_reservations(public_lab.tournament) == 0
