# pylint: disable=redefined-outer-name  # los fixtures vienen del modulo de contrato de asignacion.
"""Contrato HTTP de la cola de un tatami para el marcador (S4.4, F3C, LAB ONLY).

TDD: este modulo se escribio **antes** de la implementacion y fallo en rojo (la ruta
``GET /tournaments/{id}/courts/{id}/queue`` no existia: 404 en lugar del contrato aqui fijado).

Lo que se fija es la **unica lectura por tatami** que el marcador necesita: los cuadros que el
organizador asigno a ese tatami (S4.2/S4.3), sus combates en el orden del contrato de lectura del
backend, y por combate si es jugable o espera ganadores.

Decisiones que este contrato cierra y que no se pueden relajar sin cambiarlo:

* el tatami del cuadro (``stage_items.court_id``) es la autoridad de pertenencia a la cola; el
  ``court_id`` del combate se publica como dato, no como criterio de seleccion;
* el orden es **el del contrato de lectura del backend** (``stages`` -> ``stage_items`` ->
  ``rounds`` -> ``matches``), no ``position_in_schedule`` y nunca el marcador: no se reordena;
* ``next_match_id`` es el primer combate **jugable** en ese orden, y ``null`` si no hay ninguno;
* **no** se publica "combate actual" ni "terminado": el modelo no tiene estado oficial de combate
  y no se infiere del marcador (``score=0`` no significa nada, y en datos reales los combates sin
  jugar traen marcadores distintos de cero). El estado vivo lo tiene el marcador, no el backend.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final

import aiohttp
import pytest
import pytest_asyncio
from heliclockter import datetime_utc
from starlette import status

from bracket.database import database
from bracket.models.db.match import MatchInsertable
from bracket.models.db.round import RoundInsertable
from bracket.models.db.stage_item_inputs import StageItemInputInsertable
from bracket.utils.id_types import CourtId, MatchId, StageItemId, TournamentId
from tests.integration_tests.api.generate_bracket_http_support import GenerationLab
from tests.integration_tests.api.shared import get_root_uvicorn_url
from tests.integration_tests.api.stage_item_court_lab_test import (  # noqa: F401
    TatamiLab,
    assign_ok,
    idempotency_secret,
    lab,
    tatami,
)
from tests.integration_tests.sql import (
    inserted_match,
    inserted_round,
    inserted_stage_item_input,
)

#: Campos que publica la respuesta de la cola. Un campo de mas es un fallo: se cierra.
QUEUE_FIELDS: Final = {
    "tournament_id",
    "tournament_name",
    "court_id",
    "court_name",
    "stage_items",
    "next_match_id",
}
#: Campos de cada cuadro de la cola.
QUEUE_STAGE_ITEM_FIELDS: Final = {"stage_item_id", "name", "type_name", "matches"}
#: Campos de cada combate. Ni scores internos, ni conflictos, ni banderas de borrador sueltas.
QUEUE_MATCH_FIELDS: Final = {
    "match_id",
    "round_id",
    "round_name",
    "round_is_draft",
    "position_in_schedule",
    "start_time",
    "duration_seconds",
    "court_id",
    "fighter_a",
    "fighter_b",
    "score_a",
    "score_b",
    "state",
    "waiting_for",
}
#: Campos de cada luchador: los mismos que usa el contrato normalizado del Bridge.
QUEUE_FIGHTER_FIELDS: Final = {"stage_item_input_id", "team_id", "name"}
#: Campos del motivo de espera de un pase sin resolver.
QUEUE_WAITING_FIELDS: Final = {"side", "from_stage_item_id", "winner_position", "from_match_id"}

#: Estados posibles de un combate en la cola. No hay un tercero: no se inventa "terminado".
PLAYABLE: Final = "playable"
WAITING: Final = "waiting_on_winners"


@dataclass(frozen=True)
class WaitingCase:
    """Cuadro asignado al Tatami 1 con una segunda ronda que espera y una posicion desordenada."""

    stage_item_id: StageItemId
    court_a: CourtId
    court_b: CourtId
    playable_match_id: MatchId
    waiting_match_id: MatchId
    waiting_round_match_id: MatchId


def queue_url(tournament_id: TournamentId, court_id: CourtId, *, no_draft_rounds: bool = False) -> str:
    """Ruta exacta del contrato: la cola del tatami, no del combate ni del cuadro."""
    suffix = "?no_draft_rounds=true" if no_draft_rounds else ""
    return f"{get_root_uvicorn_url()}tournaments/{tournament_id}/courts/{court_id}/queue{suffix}"


def stages_url(tournament_id: TournamentId) -> str:
    """Lectura de la que la cola toma el orden: sirve para comparar orden contra orden."""
    return f"{get_root_uvicorn_url()}tournaments/{tournament_id}/stages?no_draft_rounds=true"


async def read_queue(
    *,
    tournament: TournamentId,
    court_id: CourtId,
    token: str | None = None,
    no_draft_rounds: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Peticion cruda a la cola. La sesion es de la peticion, no del caso."""
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    async with aiohttp.ClientSession() as session:
        async with session.get(
            queue_url(tournament, court_id, no_draft_rounds=no_draft_rounds), headers=headers
        ) as response:
            text = await response.text()
            content_type = response.headers.get("content-type", "")
            assert content_type.startswith("application/json"), text
            return response.status, json.loads(text)


async def queue_ok(*, tournament: TournamentId, court_id: CourtId) -> dict[str, Any]:
    """Cola con la sesion OWNER y forma publicada cerrada."""
    status_code, body = await read_queue(tournament=tournament, court_id=court_id)
    assert status_code == status.HTTP_200_OK, body
    assert set(body) == QUEUE_FIELDS, body
    return body


async def stage_item_input_ids(stage_item_id: StageItemId) -> dict[int, int]:
    """Ranuras del cuadro, por numero de ranura: la cola no las publica, el test si las necesita."""
    rows = await database.fetch_all(
        query="SELECT id, slot FROM stage_item_inputs WHERE stage_item_id = :stage_item_id",
        values={"stage_item_id": stage_item_id},
    )
    return {int(row["slot"]): int(row["id"]) for row in rows}


async def fighter_name(stage_item_input_id: int) -> str:
    """Nombre del equipo de una ranura, leido de la tabla: el test no escribe nombres a mano."""
    value = await database.fetch_val(
        query=(
            "SELECT teams.name FROM stage_item_inputs "
            "JOIN teams ON teams.id = stage_item_inputs.team_id "
            "WHERE stage_item_inputs.id = :input_id"
        ),
        values={"input_id": stage_item_input_id},
    )
    assert value is not None
    return str(value)


async def stage_item_matches(stage_item_id: StageItemId) -> list[int]:
    """Combates del cuadro en el orden del contrato de lectura (rondas por id, combates por id)."""
    rows = await database.fetch_all(
        query=(
            "SELECT matches.id FROM matches JOIN rounds ON matches.round_id = rounds.id "
            "WHERE rounds.stage_item_id = :stage_item_id ORDER BY rounds.id, matches.id"
        ),
        values={"stage_item_id": stage_item_id},
    )
    return [int(row["id"]) for row in rows]


@asynccontextmanager
async def tentative_input(
    *,
    tournament_id: TournamentId,
    stage_item_id: StageItemId,
    slot: int,
    from_stage_item_id: StageItemId,
    winner_position: int,
) -> AsyncIterator[int]:
    """Pase que espera al ganador de otro cuadro: el hecho real de "no jugable todavia".

    El ayudante del arnes solo inserta pases con equipo o vacios; el pase *tentativo* (el que
    apunta al ganador de un cuadro) se inserta aqui porque es justo el estado que hay que fijar.
    """
    input_id = await database.fetch_val(
        query=(
            "INSERT INTO stage_item_inputs "
            "(slot, tournament_id, stage_item_id, winner_from_stage_item_id, winner_position) "
            "VALUES (:slot, :tournament_id, :stage_item_id, :from_stage_item_id, :winner_position) "
            "RETURNING id"
        ),
        values={
            "slot": slot,
            "tournament_id": tournament_id,
            "stage_item_id": stage_item_id,
            "from_stage_item_id": from_stage_item_id,
            "winner_position": winner_position,
        },
    )
    assert input_id is not None
    try:
        yield int(input_id)
    finally:
        await database.execute(
            query="DELETE FROM stage_item_inputs WHERE id = :input_id", values={"input_id": input_id}
        )


@pytest_asyncio.fixture(loop_scope="session", scope="function")
async def waiting_case(lab: GenerationLab, tatami: TatamiLab) -> AsyncIterator[WaitingCase]:
    """Cuadro en el Tatami 1 con: un combate jugable, otro que espera pases y una ronda posterior.

    El combate original del arnes (equipo en las dos ranuras) queda con ``position_in_schedule``
    = 2 y el combate que se anade con 1, para que el orden de la cola **no** pueda coincidir con
    el de la planificacion: si la cola reordenase por posicion, este test lo veria.
    """
    now = datetime_utc.now()
    await assign_ok(lab, stage_item_id=tatami.stage_item_id, court_id=tatami.court_a)
    inputs = await stage_item_input_ids(tatami.stage_item_id)
    await database.execute(
        query=(
            "UPDATE matches SET position_in_schedule = 2 WHERE round_id IN "
            "(SELECT id FROM rounds WHERE stage_item_id = :stage_item_id)"
        ),
        values={"stage_item_id": tatami.stage_item_id},
    )

    async with inserted_round(
        RoundInsertable(
            created=now, stage_item_id=tatami.stage_item_id, is_draft=False, name="Ronda 2"
        )
    ) as second_round:
        async with tentative_input(
            tournament_id=lab.tournament,
            stage_item_id=tatami.stage_item_id,
            slot=4,
            from_stage_item_id=tatami.stage_item_id,
            winner_position=1,
        ) as tentative_a:
            async with tentative_input(
                tournament_id=lab.tournament,
                stage_item_id=tatami.stage_item_id,
                slot=5,
                from_stage_item_id=tatami.stage_item_id,
                winner_position=2,
            ) as tentative_b:
                async with inserted_match(
                    MatchInsertable(
                        created=now,
                        duration_minutes=10,
                        margin_minutes=5,
                        round_id=second_round.id,
                        stage_item_input1_id=tentative_a,
                        stage_item_input2_id=tentative_b,
                        stage_item_input1_score=0,
                        stage_item_input2_score=0,
                        stage_item_input1_conflict=False,
                        stage_item_input2_conflict=False,
                    )
                ) as final_match:
                    async with inserted_match(
                        MatchInsertable(
                            created=now,
                            duration_minutes=10,
                            margin_minutes=5,
                            position_in_schedule=1,
                            round_id=second_round.id,
                            stage_item_input1_id=inputs[2],
                            stage_item_input2_id=inputs[3],
                            stage_item_input1_score=0,
                            stage_item_input2_score=0,
                            stage_item_input1_conflict=False,
                            stage_item_input2_conflict=False,
                        )
                    ) as empty_match:
                        matches = await stage_item_matches(tatami.stage_item_id)
                        yield WaitingCase(
                            stage_item_id=tatami.stage_item_id,
                            court_a=tatami.court_a,
                            court_b=tatami.court_b,
                            playable_match_id=MatchId(matches[0]),
                            waiting_match_id=MatchId(empty_match.id),
                            waiting_round_match_id=MatchId(final_match.id),
                        )


@pytest.mark.asyncio(loop_scope="session")
async def test_queue_of_a_court_without_stage_items_is_empty(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """Un tatami sin cuadros asignados devuelve una cola vacia, no un error ni un combate inventado."""
    body = await queue_ok(tournament=lab.tournament, court_id=tatami.court_a)

    assert body["tournament_id"] == lab.tournament
    assert body["court_id"] == tatami.court_a
    assert body["court_name"] == "Tatami 1"
    assert body["stage_items"] == []
    assert body["next_match_id"] is None


@pytest.mark.asyncio(loop_scope="session")
async def test_queue_publishes_the_assigned_stage_item_and_its_matches(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """La cola publica el cuadro asignado con lo justo para pintar el combate."""
    await assign_ok(lab, stage_item_id=tatami.stage_item_id, court_id=tatami.court_a)

    body = await queue_ok(tournament=lab.tournament, court_id=tatami.court_a)

    assert body["court_name"] == "Tatami 1"
    assert [item["stage_item_id"] for item in body["stage_items"]] == [tatami.stage_item_id]
    [stage_item] = body["stage_items"]
    assert set(stage_item) == QUEUE_STAGE_ITEM_FIELDS, stage_item
    assert stage_item["name"] == "Cuadro S3.5a"
    [match] = stage_item["matches"]
    assert set(match) == QUEUE_MATCH_FIELDS, match
    assert match["state"] == PLAYABLE
    assert match["waiting_for"] == []
    assert match["court_id"] == tatami.court_a
    assert match["round_name"] == "Ronda 1"
    assert match["round_is_draft"] is False
    assert match["duration_seconds"] == 600
    assert match["score_a"] == 0 and match["score_b"] == 0
    assert set(match["fighter_a"]) == QUEUE_FIGHTER_FIELDS, match["fighter_a"]
    assert set(match["fighter_b"]) == QUEUE_FIGHTER_FIELDS, match["fighter_b"]
    assert match["fighter_a"]["name"] == await fighter_name(
        match["fighter_a"]["stage_item_input_id"]
    )
    assert match["fighter_b"]["name"] == await fighter_name(
        match["fighter_b"]["stage_item_input_id"]
    )
    assert match["fighter_a"]["name"] != match["fighter_b"]["name"]
    assert match["fighter_a"]["team_id"] != match["fighter_b"]["team_id"]
    assert body["next_match_id"] == match["match_id"]


@pytest.mark.asyncio(loop_scope="session")
async def test_queue_of_a_court_without_the_stage_item_does_not_show_it(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """El cuadro pertenece al tatami que dice su asignacion: en otro tatami no aparece."""
    await assign_ok(lab, stage_item_id=tatami.stage_item_id, court_id=tatami.court_a)

    body = await queue_ok(tournament=lab.tournament, court_id=tatami.court_b)

    assert body["court_name"] == "Tatami 2"
    assert body["stage_items"] == []


@pytest.mark.asyncio(loop_scope="session")
async def test_queue_of_an_unassigned_stage_item_is_in_no_court(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """Sin asignacion no hay tatami: el cuadro no se cuela en la cola de nadie."""
    await assign_ok(lab, stage_item_id=tatami.stage_item_id, court_id=tatami.court_a)

    body = await queue_ok(tournament=lab.tournament, court_id=tatami.court_b)

    assert all(
        item["stage_item_id"] != tatami.other_stage_item_id for item in body["stage_items"]
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_queue_of_a_court_of_another_tournament_is_not_found(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """El tatami se valida contra su torneo: el de otro torneo del mismo tenant no existe aqui."""
    status_code, body = await read_queue(
        tournament=lab.tournament, court_id=tatami.other_tournament_court
    )

    assert status_code == status.HTTP_404_NOT_FOUND, body
    assert str(tatami.other_tournament_court) in json.dumps(body)


@pytest.mark.asyncio(loop_scope="session")
async def test_queue_of_an_unknown_court_is_not_found(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """Un tatami inexistente es 404 y no una cola vacia: no se confunde "no existe" con "sin nada"."""
    status_code, body = await read_queue(tournament=lab.tournament, court_id=CourtId(10_000_000))

    assert status_code == status.HTTP_404_NOT_FOUND, body
    assert "10000000" in json.dumps(body)


@pytest.mark.asyncio(loop_scope="session")
async def test_queue_of_a_tournament_that_does_not_exist_is_not_found(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """Un torneo inexistente tampoco tiene tatamis: 404 sin filtrar la existencia de otros."""
    status_code, body = await read_queue(
        tournament=TournamentId(10_000_001), court_id=tatami.court_a
    )

    assert status_code == status.HTTP_404_NOT_FOUND, body


@pytest.mark.asyncio(loop_scope="session")
async def test_queue_needs_a_session_unless_the_drafts_are_excluded(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """Mismo criterio que la lectura de cuadros: sin sesion, solo la vista publica."""
    await assign_ok(lab, stage_item_id=tatami.stage_item_id, court_id=tatami.court_a)

    anonymous_status, anonymous_body = await read_queue(
        tournament=lab.tournament, court_id=tatami.court_a
    )
    public_status, public_body = await read_queue(
        tournament=lab.tournament, court_id=tatami.court_a, no_draft_rounds=True
    )
    owner_status, _ = await read_queue(
        tournament=lab.tournament, court_id=tatami.court_a, token=lab.owner_token
    )

    assert anonymous_status == status.HTTP_403_FORBIDDEN, anonymous_body
    assert public_status == status.HTTP_200_OK, public_body
    assert owner_status == status.HTTP_200_OK
    assert set(public_body) == QUEUE_FIELDS, public_body
    assert [item["stage_item_id"] for item in public_body["stage_items"]] == [
        tatami.stage_item_id
    ]


@pytest.mark.asyncio(loop_scope="session")
async def test_queue_keeps_the_backend_order_and_next_is_the_first_playable(
    lab: GenerationLab, waiting_case: WaitingCase
) -> None:
    """Orden = contrato de lectura del backend; ``next_match_id`` = primer jugable de ese orden.

    El combate anadido tiene ``position_in_schedule`` 1 y espera pases; el original tiene 2 y es
    jugable. Si la cola ordenase por posicion, ``next_match_id`` seria el que espera (y no lo es)
    y el orden de los combates seria el de la planificacion (y tampoco).
    """
    body = await queue_ok(tournament=lab.tournament, court_id=waiting_case.court_a)

    [stage_item] = body["stage_items"]
    published = [match["match_id"] for match in stage_item["matches"]]
    persisted = await stage_item_matches(waiting_case.stage_item_id)

    assert published == persisted
    assert published.index(waiting_case.waiting_match_id) < published.index(
        waiting_case.playable_match_id
    )
    assert body["next_match_id"] == waiting_case.playable_match_id


@pytest.mark.asyncio(loop_scope="session")
async def test_queue_reports_waiting_matches_with_their_pending_passes(
    lab: GenerationLab, waiting_case: WaitingCase
) -> None:
    """Un pase sin resolver se publica como espera, con su procedencia; no se omite ni se inventa."""
    body = await queue_ok(tournament=lab.tournament, court_id=waiting_case.court_a)

    [stage_item] = body["stage_items"]
    by_id = {match["match_id"]: match for match in stage_item["matches"]}

    empty = by_id[waiting_case.waiting_match_id]
    assert empty["state"] == WAITING
    assert empty["fighter_a"] is None and empty["fighter_b"] is None
    assert [set(waiting) for waiting in empty["waiting_for"]] == [
        QUEUE_WAITING_FIELDS,
        QUEUE_WAITING_FIELDS,
    ]
    assert [waiting["side"] for waiting in empty["waiting_for"]] == ["a", "b"]
    assert all(waiting["from_stage_item_id"] is None for waiting in empty["waiting_for"])

    pending = by_id[waiting_case.waiting_round_match_id]
    assert pending["state"] == WAITING
    assert [waiting["side"] for waiting in pending["waiting_for"]] == ["a", "b"]
    assert [waiting["winner_position"] for waiting in pending["waiting_for"]] == [1, 2]
    assert all(
        waiting["from_stage_item_id"] == waiting_case.stage_item_id
        for waiting in pending["waiting_for"]
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_queue_follows_the_tatami_reassignment(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """La cola consume la asignacion de S4.3: al reasignar, el cuadro cambia de cola y de ninguna otra."""
    await assign_ok(lab, stage_item_id=tatami.stage_item_id, court_id=tatami.court_a)
    await assign_ok(lab, stage_item_id=tatami.stage_item_id, court_id=tatami.court_b)

    first = await queue_ok(tournament=lab.tournament, court_id=tatami.court_a)
    second = await queue_ok(tournament=lab.tournament, court_id=tatami.court_b)

    assert first["stage_items"] == []
    assert [item["stage_item_id"] for item in second["stage_items"]] == [tatami.stage_item_id]
    [stage_item] = second["stage_items"]
    assert all(match["court_id"] == tatami.court_b for match in stage_item["matches"])


@pytest.mark.asyncio(loop_scope="session")
async def test_queue_hides_draft_rounds_when_asked(
    lab: GenerationLab, tatami: TatamiLab
) -> None:
    """Una ronda en borrador no se publica a quien pide la vista publica."""
    now = datetime_utc.now()
    await assign_ok(lab, stage_item_id=tatami.stage_item_id, court_id=tatami.court_a)
    inputs = await stage_item_input_ids(tatami.stage_item_id)
    async with inserted_round(
        RoundInsertable(
            created=now, stage_item_id=tatami.stage_item_id, is_draft=True, name="Ronda oculta"
        )
    ) as draft_round:
        async with inserted_match(
            MatchInsertable(
                created=now,
                duration_minutes=10,
                margin_minutes=5,
                round_id=draft_round.id,
                stage_item_input1_id=inputs[0],
                stage_item_input2_id=inputs[1],
                stage_item_input1_score=0,
                stage_item_input2_score=0,
                stage_item_input1_conflict=False,
                stage_item_input2_conflict=False,
            )
        ) as draft_match:
            public = await read_queue(
                tournament=lab.tournament, court_id=tatami.court_a, no_draft_rounds=True
            )
            private = await read_queue(
                tournament=lab.tournament, court_id=tatami.court_a, token=lab.owner_token
            )

    public_rounds = [
        match["round_id"] for item in public["stage_items"] for match in item["matches"]
    ]
    private_rounds = [
        match["round_id"] for item in private["stage_items"] for match in item["matches"]
    ]
    private_matches = [
        match["match_id"] for item in private["stage_items"] for match in item["matches"]
    ]
    assert draft_round.id not in public_rounds
    assert draft_round.id in private_rounds
    assert draft_match.id in private_matches


@pytest.mark.asyncio(loop_scope="session")
async def test_queue_matches_the_order_of_the_stages_read(
    lab: GenerationLab, waiting_case: WaitingCase
) -> None:
    """El orden de la cola es el de ``/stages`` para el mismo cuadro: una sola autoridad de orden."""
    queue_body = await queue_ok(tournament=lab.tournament, court_id=waiting_case.court_a)

    async with aiohttp.ClientSession() as session:
        async with session.get(stages_url(lab.tournament)) as response:
            assert response.status == status.HTTP_200_OK
            stages_body = json.loads(await response.text())

    stages: list[dict[str, Any]] = stages_body["data"]
    stage_item = next(
        item
        for stage in stages
        for item in stage["stage_items"]
        if item["id"] == waiting_case.stage_item_id
    )
    from_stages = [
        match["id"] for round_ in stage_item["rounds"] for match in round_["matches"]
    ]
    from_queue = [
        match["match_id"]
        for item in queue_body["stage_items"]
        for match in item["matches"]
    ]

    assert from_queue == from_stages
