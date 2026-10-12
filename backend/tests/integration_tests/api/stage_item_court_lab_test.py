# pylint: disable=redefined-outer-name
"""Contrato HTTP de la asignacion explicita de tatami (S4.2, F3C, LAB ONLY).

TDD: este modulo se escribio **antes** de la implementacion y fallo en rojo (la ruta
``PUT /tournaments/{id}/stage-items/{id}/court`` no existia: 404 en lugar del contrato aqui fijado).

Lo que se fija es el contrato observable de una operacion de negocio que **escribe** y que hasta
ahora no existia: asignar, reasignar y desasignar el tatami de un cuadro entero, sin
tocar combate a combate.

* quien puede pedirla (OWNER-only) y con que forma (``Idempotency-Key`` y un cuerpo cerrado);
* que valida antes de tocar nada (torneo, cuadro y tatami del mismo torneo y del mismo tenant);
* que propaga (el ``court_id`` de los combates del cuadro, para que el modelo legado
  ``matches.court_id`` siga siendo la verdad de cada combate);
* que respeta (un cuadro con actividad real no se reasigna: no se inventa estado de combate);
* que garantiza (una transaccion, un evento de auditoria por efecto, replay estable, y 4xx/5xx sin
  500 para estados de dominio esperables);
* que el planner de F2C **no** sobrescribe una asignacion explicita: la respeta y rechaza la
  generacion en lugar de pisarla.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import aiohttp
import pytest
import pytest_asyncio
from heliclockter import datetime_utc
from starlette import status

from bracket.config import config
from bracket.database import database
from bracket.logic.idempotency_protocol import LOCK_TIMEOUT_MS
from bracket.routes.domain_idempotency import IDEMPOTENCY_KEY_HEADER
from bracket.utils.dummy_records import DUMMY_COURT1
from bracket.utils.id_types import CourtId, StageItemId, TournamentId
from tests.integration_tests.api.generate_bracket_http_support import (
    SYNTHETIC_SECRET,
    GenerationLab,
    bracket_structure,
    call_readiness,
    generate,
    generation_lab_context,
    ready_tournament,
)
from tests.integration_tests.api.shared import get_root_uvicorn_url
from tests.integration_tests.registration_fixtures import wait_for_tuple_contention
from tests.integration_tests.sql import inserted_court

#: Campos que publica la respuesta de la asignacion. Un campo de mas es un fallo: se cierra.
ASSIGNMENT_FIELDS = {"stage_item_id", "court_id", "court_name", "affected_match_count"}

#: Codigos estables de los rechazos de esta operacion.
STAGE_ITEM_IN_USE = "STAGE_ITEM_IN_USE"

#: Bloqueo de fila que usa la operacion sobre el cuadro: el test lo toma para forzar contencion.
LOCK_STAGE_ITEM_UPDATE = "SELECT id FROM stage_items WHERE id = :stage_item_id FOR NO KEY UPDATE"

#: Cuadro de otro torneo del mismo tenant: lo usa el caso de validacion cruzada.
OTHER_TOURNAMENT_COURT_NAME = "Tatami ajeno"


def assignment_url(tournament_id: TournamentId, stage_item_id: StageItemId) -> str:
    """Ruta exacta del contrato: el tatami del cuadro, no del combate."""
    return f"{get_root_uvicorn_url()}tournaments/{tournament_id}/stage-items/{stage_item_id}/court"


def stages_url(tournament_id: TournamentId) -> str:
    """Ruta de lectura que tiene que publicar la asignacion de forma explicita."""
    return f"{get_root_uvicorn_url()}tournaments/{tournament_id}/stages"


@dataclass(frozen=True)
class TatamiLab:
    """Cuadro con dos tatamis del torneo, uno ajeno y un segundo cuadro para aislar el alcance."""

    stage_item_id: StageItemId
    other_stage_item_id: StageItemId
    court_a: CourtId
    court_b: CourtId
    other_tournament_court: CourtId


@pytest.fixture(scope="module", autouse=True)
def idempotency_secret() -> Iterator[None]:
    """Clave HMAC sintetica del laboratorio: el protocolo falla cerrado sin ella.

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


@pytest_asyncio.fixture(loop_scope="session", scope="function")
async def tatami(lab: GenerationLab) -> AsyncIterator[TatamiLab]:
    """Dos cuadros sin actividad y tres tatamis: dos del torneo y uno de otro torneo del tenant."""
    now = datetime_utc.now()
    async with inserted_court(
        DUMMY_COURT1.model_copy(
            update={"tournament_id": lab.tournament, "name": "Tatami 1", "created": now}
        )
    ) as court_a:
        async with inserted_court(
            DUMMY_COURT1.model_copy(
                update={"tournament_id": lab.tournament, "name": "Tatami 2", "created": now}
            )
        ) as court_b:
            async with inserted_court(
                DUMMY_COURT1.model_copy(
                    update={
                        "tournament_id": lab.other_tournament,
                        "name": OTHER_TOURNAMENT_COURT_NAME,
                        "created": now,
                    }
                )
            ) as foreign_court:
                # Los cuadros se apilan DESPUES: su desmontaje va antes que el de los tatamis y
                # ningun combate puede quedar apuntando a un tatami que ya no existe.
                async with bracket_structure(lab) as stage_item_id:
                    async with bracket_structure(lab) as other_stage_item_id:
                        yield TatamiLab(
                            stage_item_id=stage_item_id,
                            other_stage_item_id=other_stage_item_id,
                            court_a=CourtId(court_a.id),
                            court_b=CourtId(court_b.id),
                            other_tournament_court=CourtId(foreign_court.id),
                        )


def error_detail(body: dict[str, Any]) -> dict[str, Any]:
    """Detalle estructurado de un rechazo, ya estrechado para poder leerlo sin indexar `object`."""
    detail = body["detail"]
    assert isinstance(detail, dict), body
    return detail


async def assign(
    lab: GenerationLab,
    *,
    stage_item_id: StageItemId,
    court_id: CourtId | None,
    key: str | None = None,
    token: str | None = None,
    tournament: TournamentId | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    """Peticion cruda a la ruta. La sesion es de la peticion, no del caso."""
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if key is not None:
        headers[IDEMPOTENCY_KEY_HEADER] = key
    target = lab.tournament if tournament is None else tournament
    payload = {"court_id": court_id} if body is None else body
    async with aiohttp.ClientSession() as session:
        async with session.put(
            assignment_url(target, stage_item_id), headers=headers, json=payload
        ) as response:
            text = await response.text()
            content_type = response.headers.get("content-type", "")
            assert content_type.startswith("application/json"), text
            return response.status, dict(response.headers), json.loads(text)


async def assign_ok(
    lab: GenerationLab, *, stage_item_id: StageItemId, court_id: CourtId | None
) -> dict[str, Any]:
    """Asignacion con la sesion OWNER y clave nueva, exigiendo 200 y la forma publicada."""
    status_code, _, body = await assign(
        lab, stage_item_id=stage_item_id, court_id=court_id, key=uuid4().hex, token=lab.owner_token
    )
    assert status_code == status.HTTP_200_OK, body
    assert set(body) == ASSIGNMENT_FIELDS, body
    return body


async def stage_item_court(stage_item_id: StageItemId) -> int | None:
    """Tatami persistido del cuadro, leido de la tabla (no de la respuesta)."""
    value = await database.fetch_val(
        query="SELECT court_id FROM stage_items WHERE id = :stage_item_id",
        values={"stage_item_id": stage_item_id},
    )
    return None if value is None else int(value)


async def match_courts(stage_item_id: StageItemId) -> list[int | None]:
    """Tatami de cada combate del cuadro, en orden determinista."""
    rows = await database.fetch_all(
        query=(
            "SELECT matches.court_id FROM matches "
            "JOIN rounds ON matches.round_id = rounds.id "
            "WHERE rounds.stage_item_id = :stage_item_id ORDER BY matches.id"
        ),
        values={"stage_item_id": stage_item_id},
    )
    return [None if row["court_id"] is None else int(row["court_id"]) for row in rows]


async def match_count(stage_item_id: StageItemId) -> int:
    """Numero de combates del cuadro: el alcance real de la propagacion."""
    value = await database.fetch_val(
        query=(
            "SELECT count(*) FROM matches JOIN rounds ON matches.round_id = rounds.id "
            "WHERE rounds.stage_item_id = :stage_item_id"
        ),
        values={"stage_item_id": stage_item_id},
    )
    return int(value)


async def set_match_activity(
    stage_item_id: StageItemId, *, score: int = 0, started: bool = False
) -> None:
    """Marca actividad real en el primer combate del cuadro: tanteo o combate ya empezado."""
    await database.execute(
        query=(
            "UPDATE matches SET stage_item_input1_score = :score, "
            "start_time = CASE WHEN :started THEN now() ELSE NULL END "
            "WHERE id = (SELECT matches.id FROM matches JOIN rounds "
            "ON matches.round_id = rounds.id "
            "WHERE rounds.stage_item_id = :stage_item_id ORDER BY matches.id LIMIT 1)"
        ),
        values={"score": score, "started": started, "stage_item_id": stage_item_id},
    )


async def tatami_audit(action: str | None = None) -> list[dict[str, Any]]:
    """Eventos de auditoria de tatami, en orden determinista, con los campos publicados."""
    filters = "WHERE action LIKE 'TATAMI_%'"
    values: dict[str, Any] = {}
    if action is not None:
        filters += " AND action = :action"
        values["action"] = action
    rows = await database.fetch_all(
        query=(
            "SELECT entity, entity_id, action, actor_user_id, actor_label, reason, reason_code, "
            "reason_note, changed_fields, tenant_club_id FROM domain_change_log "
            f"{filters} ORDER BY id"
        ),
        values=values,
    )
    return [dict(row._mapping) for row in rows]


async def assignment_reservations(stage_item_id: StageItemId) -> int:
    """Reservas de idempotencia de esta ruta y cuadro: la operacion no reutiliza claves."""
    return int(
        await database.fetch_val(
            query=("SELECT count(*) FROM domain_idempotency_keys WHERE request_path LIKE :path"),
            values={"path": f"%/stage-items/{int(stage_item_id)}/court"},
        )
    )


# --- 1. asignar, reasignar y desasignar ---------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_assign_persists_and_propagates_to_every_match_of_the_stage_item(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """Asignar escribe el cuadro y propaga el tatami a sus combates, en una sola operacion."""
    expected_matches = await match_count(tatami.stage_item_id)
    assert expected_matches > 0
    assert await match_courts(tatami.stage_item_id) == [None] * expected_matches

    body = await assign_ok(lab, stage_item_id=tatami.stage_item_id, court_id=tatami.court_a)

    assert body == {
        "stage_item_id": int(tatami.stage_item_id),
        "court_id": int(tatami.court_a),
        "court_name": "Tatami 1",
        "affected_match_count": expected_matches,
    }
    assert await stage_item_court(tatami.stage_item_id) == int(tatami.court_a)
    assert await match_courts(tatami.stage_item_id) == [int(tatami.court_a)] * expected_matches
    assert [row["action"] for row in await tatami_audit()] == ["TATAMI_ASSIGNED"]


@pytest.mark.asyncio(loop_scope="session")
async def test_reassign_moves_stage_item_and_every_match(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """Reasignar cambia el tatami del cuadro y el de sus combates: no deja mitad en cada uno."""
    await assign_ok(lab, stage_item_id=tatami.stage_item_id, court_id=tatami.court_a)
    expected_matches = await match_count(tatami.stage_item_id)

    body = await assign_ok(lab, stage_item_id=tatami.stage_item_id, court_id=tatami.court_b)

    assert body["court_id"] == int(tatami.court_b)
    assert body["court_name"] == "Tatami 2"
    assert body["affected_match_count"] == expected_matches
    assert await stage_item_court(tatami.stage_item_id) == int(tatami.court_b)
    assert await match_courts(tatami.stage_item_id) == [int(tatami.court_b)] * expected_matches
    assert [row["action"] for row in await tatami_audit()] == [
        "TATAMI_ASSIGNED",
        "TATAMI_REASSIGNED",
    ]


@pytest.mark.asyncio(loop_scope="session")
async def test_unassign_clears_the_stage_item_and_its_matches(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """``court_id=null`` desasigna: el cuadro y sus combates vuelven a "sin tatami"."""
    await assign_ok(lab, stage_item_id=tatami.stage_item_id, court_id=tatami.court_a)
    expected_matches = await match_count(tatami.stage_item_id)

    body = await assign_ok(lab, stage_item_id=tatami.stage_item_id, court_id=None)

    assert body == {
        "stage_item_id": int(tatami.stage_item_id),
        "court_id": None,
        "court_name": None,
        "affected_match_count": expected_matches,
    }
    assert await stage_item_court(tatami.stage_item_id) is None
    assert await match_courts(tatami.stage_item_id) == [None] * expected_matches
    assert [row["action"] for row in await tatami_audit()] == [
        "TATAMI_ASSIGNED",
        "TATAMI_UNASSIGNED",
    ]


@pytest.mark.asyncio(loop_scope="session")
async def test_assigning_the_same_court_again_is_a_noop_without_a_new_event(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """CASO B: clave nueva sobre el tatami ya asignado es un no-op real, sin evento nuevo.

    No es un replay (la clave es nueva), asi que la operacion se ejecuta y comprueba el estado: no
    mueve ningun combate y no audita nada, porque no hay efecto. El unico cambio es la reserva.
    """
    first = await assign_ok(lab, stage_item_id=tatami.stage_item_id, court_id=tatami.court_a)
    events_after_first = await tatami_audit()

    second = await assign_ok(lab, stage_item_id=tatami.stage_item_id, court_id=tatami.court_a)

    assert first["affected_match_count"] >= 1
    assert second["affected_match_count"] == 0
    assert second["court_id"] == first["court_id"] == int(tatami.court_a)
    assert await stage_item_court(tatami.stage_item_id) == int(tatami.court_a)
    assert await tatami_audit() == events_after_first
    assert [row["action"] for row in events_after_first] == ["TATAMI_ASSIGNED"]
    assert await assignment_reservations(tatami.stage_item_id) == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_unassigning_an_already_unassigned_stage_item_is_a_noop(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """Desasignar lo que ya estaba sin tatami no escribe: ni evento, ni combates, ni error."""
    body = await assign_ok(lab, stage_item_id=tatami.stage_item_id, court_id=None)

    assert body["court_id"] is None
    assert body["affected_match_count"] == 0
    assert await tatami_audit() == []


@pytest.mark.asyncio(loop_scope="session")
async def test_assignment_scope_is_the_stage_item_only(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """Asignar un cuadro no toca al otro: el alcance es el cuadro y sus combates, no el torneo."""
    await assign_ok(lab, stage_item_id=tatami.stage_item_id, court_id=tatami.court_a)

    other_matches = await match_count(tatami.other_stage_item_id)
    assert other_matches > 0
    assert await stage_item_court(tatami.other_stage_item_id) is None
    assert await match_courts(tatami.other_stage_item_id) == [None] * other_matches


# --- 2. validacion de torneo, cuadro y tatami ---------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_court_of_another_tournament_is_not_found_and_writes_nothing(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """Un tatami de otro torneo del mismo tenant responde 404 y no se escribe nada."""
    status_code, _, body = await assign(
        lab,
        stage_item_id=tatami.stage_item_id,
        court_id=tatami.other_tournament_court,
        key=uuid4().hex,
        token=lab.owner_token,
    )

    assert status_code == status.HTTP_404_NOT_FOUND
    assert body == {"detail": "Not Found"}
    assert await stage_item_court(tatami.stage_item_id) is None
    assert await tatami_audit() == []


@pytest.mark.asyncio(loop_scope="session")
async def test_stage_item_of_another_tournament_is_not_found(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """Un cuadro de otro torneo del mismo tenant responde 404: el torneo de la ruta manda."""
    status_code, _, body = await assign(
        lab,
        stage_item_id=tatami.stage_item_id,
        court_id=tatami.other_tournament_court,
        key=uuid4().hex,
        token=lab.owner_token,
        tournament=lab.other_tournament,
    )

    assert status_code == status.HTTP_404_NOT_FOUND
    assert body == {"detail": "Not Found"}
    assert await tatami_audit() == []


@pytest.mark.asyncio(loop_scope="session")
async def test_unknown_stage_item_and_unknown_court_are_not_found(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """Cuadro y tatami inexistentes responden el mismo 404, sin escribir ni auditar."""
    unknown = StageItemId(2_000_000_001)

    missing_stage_item, _, body_a = await assign(
        lab,
        stage_item_id=unknown,
        court_id=tatami.court_a,
        key=uuid4().hex,
        token=lab.owner_token,
    )
    missing_court, _, body_b = await assign(
        lab,
        stage_item_id=tatami.stage_item_id,
        court_id=CourtId(2_000_000_002),
        key=uuid4().hex,
        token=lab.owner_token,
    )

    assert missing_stage_item == missing_court == status.HTTP_404_NOT_FOUND
    assert body_a == body_b == {"detail": "Not Found"}
    assert await stage_item_court(tatami.stage_item_id) is None
    assert await tatami_audit() == []


@pytest.mark.asyncio(loop_scope="session")
async def test_tournament_of_another_tenant_is_not_found(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """Un torneo de otro club responde el mismo 404 que uno inexistente: no revela existencia."""
    status_code, _, body = await assign(
        lab,
        stage_item_id=tatami.stage_item_id,
        court_id=tatami.court_a,
        key=uuid4().hex,
        token=lab.owner_token,
        tournament=lab.foreign_tournament,
    )

    assert status_code == status.HTTP_404_NOT_FOUND
    assert body == {"detail": "Not Found"}
    assert await tatami_audit() == []


# --- 3. quien puede pedirla ---------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_collaborator_cannot_assign(lab: GenerationLab, tatami: TatamiLab) -> None:
    """COLLAB lee el tablero, pero no mueve cuadros de tatami: la escritura es OWNER-only."""
    key = uuid4().hex
    status_code, _, body = await assign(
        lab,
        stage_item_id=tatami.stage_item_id,
        court_id=tatami.court_a,
        key=key,
        token=lab.collaborator_token,
    )

    assert status_code == status.HTTP_403_FORBIDDEN
    assert body == {"detail": "ROLE_NOT_ALLOWED"}
    assert await stage_item_court(tatami.stage_item_id) is None
    assert await tatami_audit() == []
    assert await assignment_reservations(tatami.stage_item_id) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_unauthenticated_and_inactive_actors_are_rejected(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """Sin sesion es 401; con la relacion revocada (usuario inactivo) tambien, sin cambios."""
    anonymous, _, body_a = await assign(
        lab, stage_item_id=tatami.stage_item_id, court_id=tatami.court_a, key=uuid4().hex
    )
    inactive, _, body_b = await assign(
        lab,
        stage_item_id=tatami.stage_item_id,
        court_id=tatami.court_a,
        key=uuid4().hex,
        token=lab.inactive_token,
    )

    assert anonymous == inactive == status.HTTP_401_UNAUTHORIZED
    assert body_a == {"detail": "Not authenticated"}
    assert body_b == {"detail": "Could not validate credentials"}
    assert await stage_item_court(tatami.stage_item_id) is None
    assert await tatami_audit() == []


@pytest.mark.asyncio(loop_scope="session")
async def test_missing_idempotency_key_is_rejected_before_writing(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """Sin ``Idempotency-Key`` no hay operacion: el protocolo rechaza antes de tocar la base."""
    status_code, _, body = await assign(
        lab,
        stage_item_id=tatami.stage_item_id,
        court_id=tatami.court_a,
        token=lab.owner_token,
    )

    assert status_code == status.HTTP_400_BAD_REQUEST
    assert body == {"detail": "IDEMPOTENCY_REQUEST_INVALID"}
    assert await stage_item_court(tatami.stage_item_id) is None
    assert await tatami_audit() == []


# --- 4. la puerta de escritura de F3 ------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_write_disabled_returns_503_before_any_change(
    lab: GenerationLab, tatami: TatamiLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Con ``F3_WRITE_ENABLED=false`` la ruta responde 503 y no reserva, ni bloquea, ni audita."""
    monkeypatch.setattr(config, "f3_write_enabled", False)

    status_code, _, body = await assign(
        lab,
        stage_item_id=tatami.stage_item_id,
        court_id=tatami.court_a,
        key=uuid4().hex,
        token=lab.owner_token,
    )

    assert status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert body == {"detail": "F3_WRITE_DISABLED"}
    assert await stage_item_court(tatami.stage_item_id) is None
    assert await tatami_audit() == []
    assert await assignment_reservations(tatami.stage_item_id) == 0


# --- 5. actividad real: el limite que no se inventa ---------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_scored_match_blocks_the_assignment(lab: GenerationLab, tatami: TatamiLab) -> None:
    """Con un combate ya con tanteo el cuadro tiene actividad: ni asignar ni reasignar."""
    await set_match_activity(tatami.stage_item_id, score=1)

    status_code, _, body = await assign(
        lab,
        stage_item_id=tatami.stage_item_id,
        court_id=tatami.court_a,
        key=uuid4().hex,
        token=lab.owner_token,
    )

    assert status_code == status.HTTP_409_CONFLICT
    assert body == {"detail": {"code": STAGE_ITEM_IN_USE, "activity_match_count": 1}}
    assert await stage_item_court(tatami.stage_item_id) is None
    assert await match_courts(tatami.stage_item_id) == [None]
    assert await tatami_audit() == []
    assert await assignment_reservations(tatami.stage_item_id) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_started_match_blocks_the_reassignment(lab: GenerationLab, tatami: TatamiLab) -> None:
    """Un combate con hora de inicio bloquea tambien el cambio de tatami de un cuadro asignado."""
    await assign_ok(lab, stage_item_id=tatami.stage_item_id, court_id=tatami.court_a)
    await set_match_activity(tatami.stage_item_id, started=True)

    status_code, _, body = await assign(
        lab,
        stage_item_id=tatami.stage_item_id,
        court_id=tatami.court_b,
        key=uuid4().hex,
        token=lab.owner_token,
    )

    assert status_code == status.HTTP_409_CONFLICT
    assert error_detail(body)["code"] == STAGE_ITEM_IN_USE
    assert await stage_item_court(tatami.stage_item_id) == int(tatami.court_a)
    assert await match_courts(tatami.stage_item_id) == [int(tatami.court_a)]
    assert [row["action"] for row in await tatami_audit()] == ["TATAMI_ASSIGNED"]


@pytest.mark.asyncio(loop_scope="session")
async def test_activity_blocks_the_unassignment_too(lab: GenerationLab, tatami: TatamiLab) -> None:
    """Con actividad, el tatami queda congelado: desasignar tampoco es un atajo."""
    await assign_ok(lab, stage_item_id=tatami.stage_item_id, court_id=tatami.court_a)
    await set_match_activity(tatami.stage_item_id, score=3)

    status_code, _, body = await assign(
        lab,
        stage_item_id=tatami.stage_item_id,
        court_id=None,
        key=uuid4().hex,
        token=lab.owner_token,
    )

    assert status_code == status.HTTP_409_CONFLICT
    assert error_detail(body)["code"] == STAGE_ITEM_IN_USE
    assert await stage_item_court(tatami.stage_item_id) == int(tatami.court_a)
    assert await match_courts(tatami.stage_item_id) == [int(tatami.court_a)]


# --- 6. idempotencia ----------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_replay_with_the_same_key_writes_once_and_answers_the_same(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """Repetir la misma operacion con la misma clave devuelve la misma respuesta y no reescribe."""
    key = uuid4().hex
    first_status, _, first_body = await assign(
        lab,
        stage_item_id=tatami.stage_item_id,
        court_id=tatami.court_a,
        key=key,
        token=lab.owner_token,
    )

    second_status, _, second_body = await assign(
        lab,
        stage_item_id=tatami.stage_item_id,
        court_id=tatami.court_a,
        key=key,
        token=lab.owner_token,
    )

    assert first_status == second_status == status.HTTP_200_OK
    # Repeticion de una operacion ya confirmada (CASO A): se devuelve **la respuesta original** tal
    # cual, con el mismo efecto. El cliente que reintenta ve exactamente lo mismo, incluido cuantos
    # combates movio la operacion entonces.
    assert first_body["affected_match_count"] >= 1
    assert second_body == first_body
    assert await assignment_reservations(tatami.stage_item_id) == 1
    assert [row["action"] for row in await tatami_audit()] == ["TATAMI_ASSIGNED"]


@pytest.mark.asyncio(loop_scope="session")
async def test_same_key_with_a_different_court_is_a_reused_key_conflict(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """La misma clave con otro cuerpo es un conflicto de clave reutilizada, no una reasignacion."""
    key = uuid4().hex
    await assign_ok_key(lab, tatami, key=key, court_id=tatami.court_a)

    status_code, _, body = await assign(
        lab,
        stage_item_id=tatami.stage_item_id,
        court_id=tatami.court_b,
        key=key,
        token=lab.owner_token,
    )

    assert status_code == status.HTTP_409_CONFLICT
    assert body == {"detail": "IDEMPOTENCY_KEY_REUSED"}
    assert await stage_item_court(tatami.stage_item_id) == int(tatami.court_a)
    assert await match_courts(tatami.stage_item_id) == [int(tatami.court_a)]
    assert [row["action"] for row in await tatami_audit()] == ["TATAMI_ASSIGNED"]


async def assign_ok_key(
    lab: GenerationLab, tatami: TatamiLab, *, key: str, court_id: CourtId | None
) -> dict[str, Any]:
    """Asignacion con clave fijada por el caso (para el replay y el conflicto de reutilizacion)."""
    status_code, _, body = await assign(
        lab,
        stage_item_id=tatami.stage_item_id,
        court_id=court_id,
        key=key,
        token=lab.owner_token,
    )
    assert status_code == status.HTTP_200_OK, body
    return body


# --- 7. concurrencia y bloqueo ------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_same_key_while_in_flight_is_a_safe_replay(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """Misma clave y mismo cuerpo en vuelo: la segunda espera y reproduce, no escribe dos veces."""
    key = uuid4().hex
    async with database.transaction():
        await database.execute(
            query=LOCK_STAGE_ITEM_UPDATE, values={"stage_item_id": tatami.stage_item_id}
        )
        first = asyncio.create_task(
            assign(
                lab,
                stage_item_id=tatami.stage_item_id,
                court_id=tatami.court_a,
                key=key,
                token=lab.owner_token,
            )
        )
        await wait_for_tuple_contention("stage_items")

        second = asyncio.create_task(
            assign(
                lab,
                stage_item_id=tatami.stage_item_id,
                court_id=tatami.court_a,
                key=key,
                token=lab.owner_token,
            )
        )
        await asyncio.sleep(0.2)

    first_status, _, first_body = await first
    second_status, _, second_body = await second

    assert first_status == second_status == status.HTTP_200_OK, (first_body, second_body)
    # La repeticion en vuelo espera a la confirmada y **reproduce su respuesta** (CASO A): no la
    # recalcula ni la completa con el estado actual.
    assert second_body == first_body
    assert [row["action"] for row in await tatami_audit()] == ["TATAMI_ASSIGNED"]
    assert await assignment_reservations(tatami.stage_item_id) == 1
    assert await match_courts(tatami.stage_item_id) == [int(tatami.court_a)]


@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_assignments_leave_a_consistent_state(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """Dos asignaciones a la vez de distinta clave se serializan: cuadro y combates coinciden."""
    first, second = await asyncio.gather(
        assign(
            lab,
            stage_item_id=tatami.stage_item_id,
            court_id=tatami.court_a,
            key=uuid4().hex,
            token=lab.owner_token,
        ),
        assign(
            lab,
            stage_item_id=tatami.stage_item_id,
            court_id=tatami.court_b,
            key=uuid4().hex,
            token=lab.owner_token,
        ),
    )

    assert first[0] == second[0] == status.HTTP_200_OK
    persisted = await stage_item_court(tatami.stage_item_id)
    assert persisted in (int(tatami.court_a), int(tatami.court_b))
    assert await match_courts(tatami.stage_item_id) == [persisted]
    events = await tatami_audit()
    assert events[-1]["action"] in ("TATAMI_ASSIGNED", "TATAMI_REASSIGNED")
    assert len(events) == len({first[2]["court_id"], second[2]["court_id"]})


@pytest.mark.asyncio(loop_scope="session")
async def test_lock_timeout_rolls_back_the_whole_operation(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """Si el cuadro esta bloqueado mas alla del limite, la operacion falla entera: nada a medias."""
    async with database.transaction():
        await database.execute(
            query=LOCK_STAGE_ITEM_UPDATE, values={"stage_item_id": tatami.stage_item_id}
        )
        blocked = asyncio.create_task(
            assign(
                lab,
                stage_item_id=tatami.stage_item_id,
                court_id=tatami.court_a,
                key=uuid4().hex,
                token=lab.owner_token,
            )
        )
        await wait_for_tuple_contention("stage_items")
        await asyncio.sleep(LOCK_TIMEOUT_MS / 1000 + 0.5)

    status_code, _, body = await blocked

    assert status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert body == {"detail": "LOCK_TIMEOUT"}
    assert await stage_item_court(tatami.stage_item_id) is None
    assert await match_courts(tatami.stage_item_id) == [None]
    assert await tatami_audit() == []


# --- 8. auditoria -------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_event_carries_the_closed_catalog_and_no_payload(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """El evento publica entidad, accion, actor, tenant y campos cambiados; nunca valores crudos."""
    body = await assign_ok(lab, stage_item_id=tatami.stage_item_id, court_id=tatami.court_a)

    events = await tatami_audit()
    assert len(events) == 1
    event = events[0]
    assert event["entity"] == "stage_item"
    assert event["entity_id"] == int(tatami.stage_item_id)
    assert event["action"] == "TATAMI_ASSIGNED"
    assert event["changed_fields"] == ["court_id"]
    assert event["reason_code"] is None
    assert event["reason_note"] is None
    assert event["tenant_club_id"] is not None
    assert event["actor_user_id"] is not None
    assert str(tatami.court_a) not in str(event["reason"])
    assert str(body["court_name"]) not in str(event["reason"])


@pytest.mark.asyncio(loop_scope="session")
async def test_each_effect_has_exactly_one_event(lab: GenerationLab, tatami: TatamiLab) -> None:
    """Asignar, reasignar y desasignar dejan tres eventos, uno por efecto real."""
    await assign_ok(lab, stage_item_id=tatami.stage_item_id, court_id=tatami.court_a)
    await assign_ok(lab, stage_item_id=tatami.stage_item_id, court_id=tatami.court_b)
    await assign_ok(lab, stage_item_id=tatami.stage_item_id, court_id=None)

    events = await tatami_audit()
    assert [row["action"] for row in events] == [
        "TATAMI_ASSIGNED",
        "TATAMI_REASSIGNED",
        "TATAMI_UNASSIGNED",
    ]
    assert {row["entity"] for row in events} == {"stage_item"}
    assert all(row["changed_fields"] == ["court_id"] for row in events)


# --- 9. el planner respeta la asignacion explicita ----------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_planner_refuses_to_overwrite_an_explicit_court(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """Con tatami asignado, el planner rechaza la generacion en lugar de pisar la decision."""
    await ready_tournament(lab)
    async with inserted_court(
        DUMMY_COURT1.model_copy(
            update={
                "tournament_id": lab.tournament,
                "name": "Tatami del planner",
                "created": datetime_utc.now(),
            }
        )
    ) as court:
        court_id = CourtId(court.id)
        async with bracket_structure(lab) as stage_item_id:
            await assign_ok(lab, stage_item_id=stage_item_id, court_id=court_id)

            status_code, _, body = await generate(lab, key=uuid4().hex, token=lab.owner_token)

            assert status_code == status.HTTP_409_CONFLICT, body
            # El planner no pisa la decision: el cuadro con tatami queda "congelado" para la
            # generacion (BRACKET_ALREADY_EXISTS) y la generacion se rechaza en bloque.
            assert body["detail"]["code"] == "BRACKET_NOT_READY"
            assert [issue["code"] for issue in body["detail"]["blocking_issues"]] == [
                "BRACKET_ALREADY_EXISTS"
            ]
            assert await stage_item_court(stage_item_id) == int(court_id)
            assert await match_courts(stage_item_id) == [int(court_id)]


# --- 10. contrato de lectura --------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_read_api_exposes_the_assignment_explicitly(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """``GET /stages`` publica el tatami del cuadro con nombre, sin inferirlo de los combates."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            stages_url(lab.tournament), headers={"Authorization": f"Bearer {lab.owner_token}"}
        ) as response:
            assert response.status == status.HTTP_200_OK
            payload = json.loads(await response.text())

    stage_items = [
        stage_item
        for stage in payload["data"]
        for stage_item in stage["stage_items"]
        if stage_item["id"] == int(tatami.stage_item_id)
    ]
    assert len(stage_items) == 1
    assert stage_items[0]["court_id"] is None
    assert stage_items[0]["court"] is None

    await assign_ok(lab, stage_item_id=tatami.stage_item_id, court_id=tatami.court_a)

    async with aiohttp.ClientSession() as session:
        async with session.get(
            stages_url(lab.tournament), headers={"Authorization": f"Bearer {lab.owner_token}"}
        ) as response:
            assert response.status == status.HTTP_200_OK
            payload = json.loads(await response.text())

    stage_items = [
        stage_item
        for stage in payload["data"]
        for stage_item in stage["stage_items"]
        if stage_item["id"] == int(tatami.stage_item_id)
    ]
    assert len(stage_items) == 1
    assert stage_items[0]["court_id"] == int(tatami.court_a)
    assert stage_items[0]["court"]["id"] == int(tatami.court_a)
    assert stage_items[0]["court"]["name"] == "Tatami 1"


@pytest.mark.asyncio(loop_scope="session")
async def test_readiness_still_reads_with_the_assignment_in_place(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """El readiness sigue siendo de lectura y publica el cuadro congelado por la asignacion."""
    await ready_tournament(lab)
    async with inserted_court(
        DUMMY_COURT1.model_copy(
            update={
                "tournament_id": lab.tournament,
                "name": "Tatami del readiness",
                "created": datetime_utc.now(),
            }
        )
    ) as court:
        async with bracket_structure(lab) as stage_item_id:
            await assign_ok(lab, stage_item_id=stage_item_id, court_id=CourtId(court.id))

            readiness = await call_readiness(lab)

    assert readiness["registration_window"] == "CLOSED"
    assert readiness["ready"] is False
    assert [issue["code"] for issue in readiness["blocking_issues"]] == ["BRACKET_ALREADY_EXISTS"]
