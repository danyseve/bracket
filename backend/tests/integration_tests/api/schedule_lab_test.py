# pylint: disable=redefined-outer-name
"""Contrato HTTP del planificador de torneo (S4.5, F3C, LAB ONLY).

Lo que se fija aqui es el contrato observable de las tres rutas que convierten un torneo real en un
plan de tatamis:

* ``POST /tournaments/{id}/schedule/generate`` rellena lo que no tiene tatami ni hora y **no toca**
  la decision explicita (tatami y hora) de un combate ya asignado;
* ``POST /tournaments/{id}/schedule/recalculate`` vuelve a planificar, y solo mueve esa decision si
  el organizador lo pide con ``preserve_assigned=false``;
* ``GET /tournaments/{id}/schedule`` es una lectura: la vista de calendario por tatami, con la hora
  planificada y la estimada ya derivada.

Casos que se fijan aqui:

* que planifica lo planificable y deja fuera lo estructural (bye/walkover no es combate);
* que respeta las dependencias (la final empieza cuando terminan sus alimentadores, mas el descanso)
  y que un tatami nunca aloja dos combates a la vez;
* que es determinista: repetir la generacion con otra clave no mueve ni un combate;
* que la decision manual (tatami **y** hora) sobrevive a la generacion y a un recalculo normal, y
  que solo se mueve cuando se pide expresamente;
* quien puede pedirlo (OWNER-only para escribir), con que forma (``Idempotency-Key`` obligatoria y
  un cuerpo cerrado) y que garantiza (replay estable, resumen de solo escalares);
* que la puerta de F3 corta antes de reservar, bloquear, escribir o auditar;
* que la vista de calendario publica solo datos deportivos y deriva el retraso del tatami.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any, Final, cast
from uuid import uuid4

import aiohttp
import pytest
import pytest_asyncio
from heliclockter import datetime_utc
from starlette import status

from bracket.config import config
from bracket.database import database
from bracket.logic.scheduling.builder import build_matches_for_stage_item
from bracket.models.db.stage_item import StageItem, StageItemWithInputsCreate, StageType
from bracket.models.db.stage_item_inputs import StageItemInputCreateBodyFinal
from bracket.routes.domain_idempotency import IDEMPOTENCY_KEY_HEADER
from bracket.sql.shared import sql_delete_stage_item_with_foreign_keys
from bracket.sql.stage_items import sql_create_stage_item_with_inputs
from bracket.utils.dummy_records import DUMMY_COURT1, DUMMY_RANKING1, DUMMY_STAGE2, DUMMY_TEAM1
from bracket.utils.id_types import CourtId, MatchId, StageItemId, TournamentId
from tests.integration_tests.api.generate_bracket_http_support import (
    SYNTHETIC_SECRET,
    GenerationLab,
    generation_lab_context,
)
from tests.integration_tests.api.shared import get_root_uvicorn_url
from tests.integration_tests.sql import (
    inserted_court,
    inserted_ranking,
    inserted_stage,
    inserted_team,
)

#: Campos que publica el resumen de una generacion o un recalculo. Un campo de mas es un fallo.
SCHEDULE_SUMMARY_FIELDS: Final = {
    "tournament_id",
    "court_count",
    "scheduled_match_count",
    "moved_match_count",
    "manual_match_count",
    "unscheduled_match_count",
    "start_time",
}

#: Campos que publica la vista de calendario del organizador.
SCHEDULE_VIEW_FIELDS: Final = {"tournament", "generated_at", "courts"}

#: Campos de cada tatami en la vista.
COURT_DISPLAY_FIELDS: Final = {
    "court_id",
    "court_name",
    "lag_minutes",
    "current",
    "next",
    "upcoming",
    "completed",
}

#: Codigo estable del rechazo cuando el torneo no se puede planificar.
NOT_PLANNABLE: Final = "SCHEDULE_NOT_PLANNABLE"

#: Hora de inicio explicita del laboratorio: no se depende del ``start_time`` del torneo de prueba.
START_TIME: Final = "2026-06-01T09:00:00+00:00"
START: Final = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)

#: Un combate ocupa ``duration_minutes + margin_minutes`` = 10 + 5 minutos (DUMMY_TOURNAMENT).
SLOT_MINUTES: Final = 15
#: Descanso minimo inicial de LAB (§6 de S4.5).
REST_MINUTES: Final = 15


def schedule_url(tournament_id: TournamentId, operation: str) -> str:
    return f"{get_root_uvicorn_url()}tournaments/{tournament_id}/schedule{operation}"


def generate_url(tournament_id: TournamentId) -> str:
    return schedule_url(tournament_id, "/generate")


def recalculate_url(tournament_id: TournamentId) -> str:
    return schedule_url(tournament_id, "/recalculate")


def view_url(tournament_id: TournamentId) -> str:
    return schedule_url(tournament_id, "")


@dataclass(frozen=True)
class ScheduleLab:
    """Torneo de laboratorio con tatamis, un cuadro de cuatro equipos y sus combates reales."""

    lab: GenerationLab
    tournament: TournamentId
    stage_item_id: StageItemId
    courts: tuple[CourtId, ...]
    match_ids: tuple[MatchId, ...]

    @property
    def semi_ids(self) -> tuple[MatchId, ...]:
        """Los dos combates de la primera ronda (dos equipos cada uno)."""
        return self.match_ids[:2]

    @property
    def final_id(self) -> MatchId:
        """La final: no tiene rivales todavia, solo los alimentadores."""
        return self.match_ids[2]


@pytest.fixture(scope="module", autouse=True)
def idempotency_secret() -> Any:
    """Clave HMAC sintetica del laboratorio: el protocolo falla cerrado sin ella."""
    patcher = pytest.MonkeyPatch()
    patcher.setattr(config, "idempotency_hmac_key", SYNTHETIC_SECRET)
    patcher.setattr(config, "idempotency_hmac_key_version", "v1")
    yield
    patcher.undo()


@asynccontextmanager
async def schedule_lab_context(*, court_count: int = 2) -> AsyncIterator[ScheduleLab]:
    """Cuadro de eliminacion de cuatro equipos con equipos reales en las cuatro ranuras.

    Cuatro equipos con plaza resuelta no dejan ningun hueco muerto: los tres combates son combates
    de verdad (dos semifinales y una final que espera a sus ganadores). ``court_count=0`` deja el
    torneo sin tatamis, que es el caso que no se puede planificar.
    """
    async with generation_lab_context() as lab:
        async with AsyncExitStack() as stack:
            now = datetime_utc.now()
            courts = [
                await stack.enter_async_context(
                    inserted_court(
                        DUMMY_COURT1.model_copy(
                            update={
                                "tournament_id": lab.tournament,
                                "name": f"Tatami {index + 1}",
                                "created": now,
                            }
                        )
                    )
                )
                for index in range(court_count)
            ]
            stage = await stack.enter_async_context(
                inserted_stage(
                    DUMMY_STAGE2.model_copy(
                        update={"tournament_id": lab.tournament, "name": "S4.5"}
                    )
                )
            )
            ranking = await stack.enter_async_context(
                inserted_ranking(
                    DUMMY_RANKING1.model_copy(update={"tournament_id": lab.tournament})
                )
            )
            teams = [
                await stack.enter_async_context(
                    inserted_team(
                        DUMMY_TEAM1.model_copy(
                            update={
                                "tournament_id": lab.tournament,
                                "name": f"Equipo {index + 1}",
                                "created": now,
                            }
                        )
                    )
                )
                for index in range(4)
            ]
            stage_item: StageItem = await sql_create_stage_item_with_inputs(
                lab.tournament,
                StageItemWithInputsCreate(
                    stage_id=stage.id,
                    name="Cuadro S4.5",
                    team_count=4,
                    type=StageType.SINGLE_ELIMINATION,
                    ranking_id=ranking.id,
                    inputs=[
                        StageItemInputCreateBodyFinal(slot=index + 1, team_id=team.id)
                        for index, team in enumerate(teams)
                    ],
                ),
            )
            # Lo ultimo apilado se deshace primero: los combates referencian tatamis y equipos.
            stack.push_async_callback(sql_delete_stage_item_with_foreign_keys, stage_item.id)
            await build_matches_for_stage_item(stage_item, lab.tournament)

            match_ids = tuple(
                MatchId(row["id"]) for row in await match_rows(stage_item_id=stage_item.id)
            )
            assert len(match_ids) == 3, "el cuadro de cuatro equipos tiene tres combates"

            yield ScheduleLab(
                lab=lab,
                tournament=lab.tournament,
                stage_item_id=stage_item.id,
                courts=tuple(CourtId(court.id) for court in courts),
                match_ids=match_ids,
            )


@pytest_asyncio.fixture(loop_scope="session", scope="function")
async def schedule_lab() -> AsyncIterator[ScheduleLab]:
    async with schedule_lab_context() as lab:
        yield lab


async def match_rows(*, stage_item_id: StageItemId) -> list[dict[str, Any]]:
    """Estado persistido de los combates del cuadro, en orden determinista (por id)."""
    rows = await database.fetch_all(
        query=(
            "SELECT matches.id AS id, matches.court_id AS court_id,"
            " matches.start_time AS start_time,"
            " matches.position_in_schedule AS position_in_schedule,"
            " matches.finished_at AS finished_at"
            " FROM matches JOIN rounds ON matches.round_id = rounds.id"
            " WHERE rounds.stage_item_id = :stage_item_id ORDER BY matches.id"
        ),
        values={"stage_item_id": stage_item_id},
    )
    return [dict(row._mapping) for row in rows]


async def persisted_state(lab: ScheduleLab) -> list[tuple[Any, ...]]:
    """Huella comparable del plan persistido: tatami, hora, posicion y fin de cada combate."""
    return [
        (
            row["id"],
            None if row["court_id"] is None else int(row["court_id"]),
            row["start_time"],
            row["position_in_schedule"],
            row["finished_at"],
        )
        for row in await match_rows(stage_item_id=lab.stage_item_id)
    ]


async def set_manual_assignment(
    match_id: MatchId, *, court_id: CourtId, start_time: datetime
) -> None:
    """Deja un combate con la decision explicita del organizador: tatami **y** hora."""
    await database.execute(
        query=(
            "UPDATE matches SET court_id = :court_id, start_time = :start_time,"
            " position_in_schedule = NULL WHERE id = :match_id"
        ),
        values={"match_id": match_id, "court_id": int(court_id), "start_time": start_time},
    )


async def schedule_reservations(tournament_id: TournamentId, operation: str) -> int:
    """Reservas de idempotencia de esta ruta y torneo: la operacion no reutiliza claves."""
    return int(
        await database.fetch_val(
            query="SELECT count(*) FROM domain_idempotency_keys WHERE request_path LIKE :path",
            values={"path": f"%/tournaments/{int(tournament_id)}/schedule/{operation}"},
        )
    )


async def post_operation(
    url: str,
    *,
    key: str | None = None,
    token: str | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Peticion cruda a una escritura del planificador. La sesion es de la peticion."""
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if key is not None:
        headers[IDEMPOTENCY_KEY_HEADER] = key
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, headers=headers, json={} if body is None else body
        ) as response:
            text = await response.text()
            content_type = response.headers.get("content-type", "")
            assert content_type.startswith("application/json"), text
            return response.status, json.loads(text)


async def generate(
    lab: ScheduleLab, *, key: str | None = None, token: str | None = None, body: Any = None
) -> tuple[int, dict[str, Any]]:
    return await post_operation(generate_url(lab.tournament), key=key, token=token, body=body)


async def recalculate(
    lab: ScheduleLab, *, key: str | None = None, token: str | None = None, body: Any = None
) -> tuple[int, dict[str, Any]]:
    return await post_operation(recalculate_url(lab.tournament), key=key, token=token, body=body)


async def generate_ok(lab: ScheduleLab, *, body: Any = None) -> dict[str, Any]:
    """Generacion con la sesion OWNER y clave nueva, exigiendo 200 y la forma publicada."""
    status_code, response = await generate(
        lab, key=uuid4().hex, token=lab.lab.owner_token, body=body
    )
    assert status_code == status.HTTP_200_OK, response
    assert set(response) == SCHEDULE_SUMMARY_FIELDS, response
    return response


async def read_view(lab: ScheduleLab, *, token: str | None = None) -> tuple[int, dict[str, Any]]:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(view_url(lab.tournament), headers=headers) as response:
            text = await response.text()
            content_type = response.headers.get("content-type", "")
            assert content_type.startswith("application/json"), text
            return response.status, json.loads(text)


def court_display(view: dict[str, Any], court_id: CourtId) -> dict[str, Any]:
    """Bloque de un tatami en la vista, identificado por su id y no por su posicion."""
    blocks = [block for block in view["courts"] if block["court_id"] == int(court_id)]
    assert len(blocks) == 1, view
    return cast("dict[str, Any]", blocks[0])


# --- 1. planificar lo planificable --------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_plans_every_match_and_respects_dependencies_and_courts(
    schedule_lab: ScheduleLab,
) -> None:
    """Los tres combates quedan planificados: sin solape y con la final tras el descanso."""
    body = await generate_ok(schedule_lab, body={"start_time": START_TIME})

    assert body == {
        "tournament_id": int(schedule_lab.tournament),
        "court_count": 2,
        "scheduled_match_count": 3,
        "moved_match_count": 3,
        "manual_match_count": 0,
        "unscheduled_match_count": 0,
        "start_time": START_TIME,
    }

    rows = {row["id"]: row for row in await match_rows(stage_item_id=schedule_lab.stage_item_id)}
    assert all(row["start_time"] is not None for row in rows.values())
    assert {int(row["court_id"]) for row in rows.values()} == {int(c) for c in schedule_lab.courts}
    # Las dos semifinales empiezan a la vez, cada una en un tatami distinto: el plan las reparte.
    semis = {int(match_id): rows[int(match_id)]["start_time"] for match_id in schedule_lab.semi_ids}
    assert set(semis.values()) == {START}
    assert {int(rows[int(match_id)]["court_id"]) for match_id in schedule_lab.semi_ids} == {
        int(c) for c in schedule_lab.courts
    }
    # La final espera a que terminen sus alimentadores y a que descansen sus ganadores (§5).
    semi_end = START + timedelta(minutes=SLOT_MINUTES)
    assert rows[int(schedule_lab.final_id)]["start_time"] == semi_end + timedelta(
        minutes=REST_MINUTES
    )
    # Un tatami no aloja dos combates a la vez: los huecos por tatami no se solapan.
    for court_id in schedule_lab.courts:
        windows = sorted(
            (
                row["start_time"],
                row["start_time"] + timedelta(minutes=SLOT_MINUTES),
            )
            for row in rows.values()
            if int(row["court_id"]) == int(court_id)
        )
        assert all(previous[1] <= following[0] for previous, following in pairwise(windows)), (
            windows
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_a_second_generation_with_a_new_key_moves_nothing(
    schedule_lab: ScheduleLab,
) -> None:
    """El plan es determinista: con todo ya planificado, otra generacion no mueve ni un combate."""
    await generate_ok(schedule_lab, body={"start_time": START_TIME})
    before = await persisted_state(schedule_lab)

    body = await generate_ok(schedule_lab, body={"start_time": START_TIME})

    assert body["moved_match_count"] == 0
    assert body["scheduled_match_count"] == 3
    assert body["manual_match_count"] == 3
    assert await persisted_state(schedule_lab) == before


# --- 2. la decision explicita del organizador ---------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_does_not_touch_an_explicit_assignment(schedule_lab: ScheduleLab) -> None:
    """Una asignacion manual (tatami y hora) sobrevive a la generacion, tal cual se decidio."""
    manual_start = START + timedelta(hours=10)
    await set_manual_assignment(
        schedule_lab.final_id, court_id=schedule_lab.courts[0], start_time=manual_start
    )

    body = await generate_ok(schedule_lab, body={"start_time": START_TIME})

    assert body["manual_match_count"] == 1
    rows = {row["id"]: row for row in await match_rows(stage_item_id=schedule_lab.stage_item_id)}
    final = rows[int(schedule_lab.final_id)]
    assert final["start_time"] == manual_start
    assert int(final["court_id"]) == int(schedule_lab.courts[0])
    # Los otros dos combates si se planificaron.
    assert all(rows[int(match_id)]["start_time"] is not None for match_id in schedule_lab.semi_ids)


@pytest.mark.asyncio(loop_scope="session")
async def test_recalculate_keeps_the_explicit_assignment_by_default(
    schedule_lab: ScheduleLab,
) -> None:
    """Un recalculo tampoco mueve la decision manual: ``preserve_assigned`` es por defecto."""
    manual_start = START + timedelta(hours=10)
    await set_manual_assignment(
        schedule_lab.final_id, court_id=schedule_lab.courts[0], start_time=manual_start
    )

    status_code, body = await recalculate(
        schedule_lab,
        key=uuid4().hex,
        token=schedule_lab.lab.owner_token,
        body={"start_time": START_TIME},
    )

    assert status_code == status.HTTP_200_OK, body
    rows = {row["id"]: row for row in await match_rows(stage_item_id=schedule_lab.stage_item_id)}
    assert rows[int(schedule_lab.final_id)]["start_time"] == manual_start
    assert int(rows[int(schedule_lab.final_id)]["court_id"]) == int(schedule_lab.courts[0])


@pytest.mark.asyncio(loop_scope="session")
async def test_recalculate_moves_the_explicit_assignment_only_when_asked(
    schedule_lab: ScheduleLab,
) -> None:
    """``preserve_assigned=false`` replanifica la manual: la final vuelve tras sus semis."""
    manual_start = START + timedelta(hours=10)
    await set_manual_assignment(
        schedule_lab.final_id, court_id=schedule_lab.courts[0], start_time=manual_start
    )

    status_code, body = await recalculate(
        schedule_lab,
        key=uuid4().hex,
        token=schedule_lab.lab.owner_token,
        body={"start_time": START_TIME, "preserve_assigned": False},
    )

    assert status_code == status.HTTP_200_OK, body
    assert set(body) == SCHEDULE_SUMMARY_FIELDS, body
    assert body["moved_match_count"] >= 1
    rows = {row["id"]: row for row in await match_rows(stage_item_id=schedule_lab.stage_item_id)}
    final = rows[int(schedule_lab.final_id)]
    assert final["start_time"] != manual_start
    assert final["start_time"] == START + timedelta(minutes=SLOT_MINUTES + REST_MINUTES)


# --- 3. idempotencia ----------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_replay_with_the_same_key_answers_the_same_and_writes_once(
    schedule_lab: ScheduleLab,
) -> None:
    """Repetir la misma operacion con la misma clave devuelve el mismo resumen y no reescribe."""
    key = uuid4().hex
    first_status, first_body = await generate(
        schedule_lab, key=key, token=schedule_lab.lab.owner_token, body={"start_time": START_TIME}
    )
    after_first = await persisted_state(schedule_lab)

    second_status, second_body = await generate(
        schedule_lab, key=key, token=schedule_lab.lab.owner_token, body={"start_time": START_TIME}
    )

    assert first_status == second_status == status.HTTP_200_OK
    assert second_body == first_body
    assert await persisted_state(schedule_lab) == after_first
    assert await schedule_reservations(schedule_lab.tournament, "generate") == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_same_key_with_a_different_body_is_a_reused_key_conflict(
    schedule_lab: ScheduleLab,
) -> None:
    """La misma clave con otro cuerpo es un conflicto de clave reutilizada, no otra generacion."""
    key = uuid4().hex
    await generate(
        schedule_lab, key=key, token=schedule_lab.lab.owner_token, body={"start_time": START_TIME}
    )
    after_first = await persisted_state(schedule_lab)

    status_code, body = await generate(
        schedule_lab,
        key=key,
        token=schedule_lab.lab.owner_token,
        body={"start_time": "2026-06-01T10:00:00+00:00"},
    )

    assert status_code == status.HTTP_409_CONFLICT
    assert body == {"detail": "IDEMPOTENCY_KEY_REUSED"}
    assert await persisted_state(schedule_lab) == after_first
    assert await schedule_reservations(schedule_lab.tournament, "generate") == 1


# --- 4. la puerta de escritura de F3 y quien puede pedirlo --------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_write_disabled_returns_503_before_any_change(
    schedule_lab: ScheduleLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Con ``F3_WRITE_ENABLED=false`` la ruta responde 503 y no reserva ni escribe."""
    monkeypatch.setattr(config, "f3_write_enabled", False)

    status_code, body = await generate(
        schedule_lab,
        key=uuid4().hex,
        token=schedule_lab.lab.owner_token,
        body={"start_time": START_TIME},
    )

    assert status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert body == {"detail": "F3_WRITE_DISABLED"}
    assert all(
        row["start_time"] is None
        for row in await match_rows(stage_item_id=schedule_lab.stage_item_id)
    )
    assert await schedule_reservations(schedule_lab.tournament, "generate") == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_collaborator_cannot_plan_but_can_read(schedule_lab: ScheduleLab) -> None:
    """COLLAB lee el calendario, pero no planifica: la escritura es OWNER-only."""
    status_code, body = await generate(
        schedule_lab,
        key=uuid4().hex,
        token=schedule_lab.lab.collaborator_token,
        body={"start_time": START_TIME},
    )

    assert status_code == status.HTTP_403_FORBIDDEN
    assert body == {"detail": "ROLE_NOT_ALLOWED"}
    assert await schedule_reservations(schedule_lab.tournament, "generate") == 0

    read_status, view = await read_view(schedule_lab, token=schedule_lab.lab.collaborator_token)
    assert read_status == status.HTTP_200_OK
    assert set(view) == SCHEDULE_VIEW_FIELDS


@pytest.mark.asyncio(loop_scope="session")
async def test_unauthenticated_and_inactive_actors_are_rejected(schedule_lab: ScheduleLab) -> None:
    """Sin sesion es 401; con la relacion revocada (usuario inactivo) tambien, sin planificar."""
    anonymous, body_a = await generate(
        schedule_lab, key=uuid4().hex, body={"start_time": START_TIME}
    )
    inactive, body_b = await generate(
        schedule_lab,
        key=uuid4().hex,
        token=schedule_lab.lab.inactive_token,
        body={"start_time": START_TIME},
    )

    assert anonymous == inactive == status.HTTP_401_UNAUTHORIZED
    assert body_a == {"detail": "Not authenticated"}
    assert body_b == {"detail": "Could not validate credentials"}
    assert all(
        row["start_time"] is None
        for row in await match_rows(stage_item_id=schedule_lab.stage_item_id)
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_missing_idempotency_key_is_rejected_before_writing(
    schedule_lab: ScheduleLab,
) -> None:
    """Sin ``Idempotency-Key`` no hay operacion: el protocolo rechaza antes de tocar la base."""
    status_code, body = await generate(
        schedule_lab, token=schedule_lab.lab.owner_token, body={"start_time": START_TIME}
    )

    assert status_code == status.HTTP_400_BAD_REQUEST
    assert body == {"detail": "IDEMPOTENCY_REQUEST_INVALID"}
    assert all(
        row["start_time"] is None
        for row in await match_rows(stage_item_id=schedule_lab.stage_item_id)
    )
    assert await schedule_reservations(schedule_lab.tournament, "generate") == 0


# --- 5. lo que no se puede planificar -----------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_a_tournament_without_courts_is_not_plannable() -> None:
    """Sin tatamis activos no se inventa planificacion: 400 con codigo estable y nada escrito."""
    async with schedule_lab_context(court_count=0) as lab:
        status_code, body = await generate(
            lab, key=uuid4().hex, token=lab.lab.owner_token, body={"start_time": START_TIME}
        )

        assert status_code == status.HTTP_400_BAD_REQUEST
        assert body["detail"]["code"] == NOT_PLANNABLE
        assert await schedule_reservations(lab.tournament, "generate") == 0
        assert all(
            row["start_time"] is None for row in await match_rows(stage_item_id=lab.stage_item_id)
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_a_court_of_another_tournament_is_not_plannable(schedule_lab: ScheduleLab) -> None:
    """Pedir un tatami que no es del torneo es un error de uso, no una excusa para planificar."""
    now = datetime_utc.now()
    async with inserted_court(
        DUMMY_COURT1.model_copy(
            update={
                "tournament_id": schedule_lab.lab.other_tournament,
                "name": "Tatami ajeno",
                "created": now,
            }
        )
    ) as foreign_court:
        status_code, body = await generate(
            schedule_lab,
            key=uuid4().hex,
            token=schedule_lab.lab.owner_token,
            body={"start_time": START_TIME, "court_ids": [int(foreign_court.id)]},
        )

    assert status_code == status.HTTP_400_BAD_REQUEST
    assert body["detail"]["code"] == NOT_PLANNABLE
    assert all(
        row["start_time"] is None
        for row in await match_rows(stage_item_id=schedule_lab.stage_item_id)
    )


# --- 6. la vista de calendario ------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_view_publishes_now_and_next_per_court_without_private_data(
    schedule_lab: ScheduleLab,
) -> None:
    """La lectura publica AHORA y SIGUIENTE con nombres deportivos y sin nada de cuenta."""
    await generate_ok(schedule_lab, body={"start_time": START_TIME})

    status_code, view = await read_view(schedule_lab, token=schedule_lab.lab.owner_token)

    assert status_code == status.HTTP_200_OK
    assert set(view) == SCHEDULE_VIEW_FIELDS
    # El torneo de prueba arranca en la hora fija del laboratorio (``DUMMY_MOCK_TIME``).
    assert view["tournament"]["start_time"] == "2022-01-11T04:32:11+00:00"
    assert len(view["courts"]) == 2
    for block in view["courts"]:
        assert set(block) == COURT_DISPLAY_FIELDS, block
        assert block["lag_minutes"] == 0

    # Cada semifinal es AHORA en su tatami: tiene rivales y esta planificada a la misma hora.
    for court_id, match_id in zip(schedule_lab.courts, schedule_lab.semi_ids, strict=True):
        block = court_display(view, court_id)
        assert block["current"]["match_id"] == int(match_id)
        assert block["current"]["state"] == "NOW"
        assert block["current"]["scheduled_start"] == START_TIME
        assert block["current"]["estimated_start"] == START_TIME
        assert block["current"]["fighter_a"] is not None
        assert block["current"]["fighter_b"] is not None
        assert block["current"]["waiting_for"] == []

    # La final ya tiene tatami y hora, pero todavia no rivales: no es AHORA, es SIGUIENTE y espera.
    final = next(
        entry
        for block in view["courts"]
        for entry in [block["current"], block["next"], *block["upcoming"]]
        if entry is not None and entry["match_id"] == int(schedule_lab.final_id)
    )
    assert final["state"] == "WAITING"
    assert final["fighter_a"] is None
    assert final["fighter_b"] is None
    assert set(final["waiting_for"]) == {
        f"Ganador del combate {int(match_id)}" for match_id in schedule_lab.semi_ids
    }
    # Y no se adelanta a un combate que si tiene rivales.
    for block in view["courts"]:
        assert block["current"]["state"] == "NOW"


@pytest.mark.asyncio(loop_scope="session")
async def test_view_derives_the_delay_of_a_court(schedule_lab: ScheduleLab) -> None:
    """El retraso del tatami se deriva de lo ya terminado y no se persiste (§9)."""
    late_finish = START + timedelta(minutes=45)
    await set_manual_assignment(
        schedule_lab.semi_ids[0], court_id=schedule_lab.courts[0], start_time=START
    )
    await database.execute(
        query="UPDATE matches SET finished_at = :finished_at WHERE id = :match_id",
        values={"match_id": int(schedule_lab.semi_ids[0]), "finished_at": late_finish},
    )
    await set_manual_assignment(
        schedule_lab.semi_ids[1],
        court_id=schedule_lab.courts[0],
        start_time=START + timedelta(minutes=30),
    )

    status_code, view = await read_view(schedule_lab, token=schedule_lab.lab.owner_token)

    assert status_code == status.HTTP_200_OK
    # El combate termino a los 45 minutos cuando su hueco eran 15: 30 minutos de retraso.
    block = court_display(view, schedule_lab.courts[0])
    assert block["lag_minutes"] == 30
    assert block["current"]["match_id"] == int(schedule_lab.semi_ids[1])
    assert block["current"]["state"] == "DELAYED"
    # La hora planificada no se mueve: la estimada es la planificada mas el retraso derivado (§9).
    assert block["current"]["scheduled_start"] == "2026-06-01T09:30:00+00:00"
    assert block["current"]["estimated_start"] == "2026-06-01T10:00:00+00:00"
    # El tatami de al lado no arrastra el retraso del otro.
    assert court_display(view, schedule_lab.courts[1])["lag_minutes"] == 0
    completed = block["completed"]
    assert completed[0]["match_id"] == int(schedule_lab.semi_ids[0])
    assert completed[0]["state"] == "COMPLETED"
