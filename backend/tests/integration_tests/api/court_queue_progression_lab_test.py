# pylint: disable=redefined-outer-name  # los fixtures vienen del modulo de contrato.
"""E2E del avance del bracket visto desde la cola de un tatami (S4.4, F3C, LAB ONLY).

Cadena que se valida, con el backend como unica autoridad y sin estado local:

    cuadro asignado al tatami (S4.2/S4.3)
      -> la cola publica sus combates en el orden del contrato de lectura
      -> resultado escrito con el contrato de escritura de combates (PUT .../matches/{id})
      -> el backend deja avanzado al ganador (recalcula los pases de la ronda siguiente)
      -> la cola lo refleja: el pase que esperaba aparece resuelto y, con los dos resueltos,
         el combate de la ronda siguiente pasa a jugable

Lo que NO se puede afirmar, y por eso se fija aqui de forma explicita: el modelo no tiene un hecho
durable de "combate jugado", asi que un combate con resultado se sigue publicando como jugable.
Se documenta el gap; no se infiere del marcador.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, cast

import aiohttp
import pytest
import pytest_asyncio
from heliclockter import datetime_utc
from starlette import status

from bracket.models.db.match import MatchInsertable
from bracket.models.db.round import RoundInsertable
from bracket.models.db.stage_item import StageItemInsertable, StageType
from bracket.models.db.stage_item_inputs import StageItemInputInsertable
from bracket.models.db.team import TeamInsertable
from bracket.utils.dummy_records import DUMMY_COURT1, DUMMY_RANKING1, DUMMY_STAGE2
from bracket.utils.id_types import (
    CourtId,
    MatchId,
    RoundId,
    StageItemId,
    StageItemInputId,
    TournamentId,
)
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
    inserted_court,
    inserted_match,
    inserted_ranking,
    inserted_round,
    inserted_stage,
    inserted_stage_item,
    inserted_stage_item_input,
    inserted_team,
)

#: Campos que publica un combate de la cola: un campo de mas es un fallo.
QUEUE_MATCH_FIELDS = {
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

PLAYABLE = "playable"
WAITING = "waiting_on_winners"
TEAM_NAMES = ("Equipo S4.4 A", "Equipo S4.4 B", "Equipo S4.4 C", "Equipo S4.4 D")


@dataclass(frozen=True)
class ProgressionLab:
    """Cuadro de dos rondas asignado a un tatami: dos semifinales y una final enlazada."""

    stage_item_id: StageItemId
    court_id: CourtId
    semi_a: MatchId
    semi_b: MatchId
    final: MatchId
    first_round_id: int
    final_round_id: int


def queue_url(tournament_id: TournamentId, court_id: CourtId) -> str:
    """Ruta de la cola del tatami, por la via publica: la que consume un marcador."""
    return f"{get_root_uvicorn_url()}tournaments/{tournament_id}/courts/{court_id}/queue"


def match_url(tournament_id: TournamentId, match_id: MatchId) -> str:
    """Contrato de escritura de un combate: el que usa el bridge para publicar un resultado."""
    return f"{get_root_uvicorn_url()}tournaments/{tournament_id}/matches/{match_id}"


@pytest_asyncio.fixture(loop_scope="session", scope="function")
async def progression(
    lab: GenerationLab,  # noqa: F811  (fixtures importados del modulo de contrato)
) -> AsyncIterator[ProgressionLab]:
    """Cuadro de eliminacion directa de dos rondas, y su tatami, listos para jugar.

    La final se crea con el enlace que usa el propio backend (``winner_from_match_id`` sobre cada
    semifinal), no con un pase inventado: asi el avance lo decide la logica del backend. El tatami
    se crea antes que los combates a proposito: su desmontaje ocurre despues, cuando ningun combate
    lo referencia ya.
    """
    now = datetime_utc.now()
    async with AsyncExitStack() as stack:
        ranking = await stack.enter_async_context(
            inserted_ranking(DUMMY_RANKING1.model_copy(update={"tournament_id": lab.tournament}))
        )
        stage = await stack.enter_async_context(
            inserted_stage(
                DUMMY_STAGE2.model_copy(update={"tournament_id": lab.tournament, "name": "S4.4"})
            )
        )
        stage_item = await stack.enter_async_context(
            inserted_stage_item(
                StageItemInsertable(
                    stage_id=stage.id,
                    name="Cuadro S4.4",
                    created=now,
                    type=StageType.SINGLE_ELIMINATION,
                    team_count=4,
                    ranking_id=ranking.id,
                )
            )
        )
        inputs = []
        for slot, name in enumerate(TEAM_NAMES):
            team = await stack.enter_async_context(
                inserted_team(
                    TeamInsertable(
                        created=now, name=name, tournament_id=lab.tournament, active=True
                    )
                )
            )
            inputs.append(
                await stack.enter_async_context(
                    inserted_stage_item_input(
                        StageItemInputInsertable(
                            slot=slot,
                            team_id=team.id,
                            tournament_id=lab.tournament,
                            stage_item_id=stage_item.id,
                        )
                    )
                )
            )
        court = await stack.enter_async_context(
            inserted_court(
                DUMMY_COURT1.model_copy(
                    update={"tournament_id": lab.tournament, "name": "Tatami 1", "created": now}
                )
            )
        )

        first_round = await stack.enter_async_context(
            inserted_round(
                RoundInsertable(
                    created=now, stage_item_id=stage_item.id, is_draft=False, name="Ronda 1"
                )
            )
        )
        final_round = await stack.enter_async_context(
            inserted_round(
                RoundInsertable(
                    created=now, stage_item_id=stage_item.id, is_draft=False, name="Final"
                )
            )
        )

        def match(
            round_id: RoundId,
            first: StageItemInputId | None,
            second: StageItemInputId | None,
            winner_from_first: MatchId | None = None,
            winner_from_second: MatchId | None = None,
        ) -> MatchInsertable:
            return MatchInsertable(
                created=now,
                duration_minutes=5,
                margin_minutes=5,
                round_id=round_id,
                stage_item_input1_id=first,
                stage_item_input2_id=second,
                stage_item_input1_score=0,
                stage_item_input2_score=0,
                stage_item_input1_conflict=False,
                stage_item_input2_conflict=False,
                stage_item_input1_winner_from_match_id=winner_from_first,
                stage_item_input2_winner_from_match_id=winner_from_second,
            )

        semi_a = await stack.enter_async_context(
            inserted_match(match(first_round.id, inputs[0].id, inputs[1].id))
        )
        semi_b = await stack.enter_async_context(
            inserted_match(match(first_round.id, inputs[2].id, inputs[3].id))
        )
        final = await stack.enter_async_context(
            inserted_match(
                match(
                    final_round.id,
                    None,
                    None,
                    winner_from_first=semi_a.id,
                    winner_from_second=semi_b.id,
                )
            )
        )
        # La asignacion la hace el organizador (S4.3): aqui solo se consume.
        await assign_ok(lab, stage_item_id=stage_item.id, court_id=court.id)

        yield ProgressionLab(
            stage_item_id=stage_item.id,
            court_id=court.id,
            semi_a=semi_a.id,
            semi_b=semi_b.id,
            final=final.id,
            first_round_id=first_round.id,
            final_round_id=final_round.id,
        )


async def read_queue(tournament_id: TournamentId, court_id: CourtId) -> dict[str, Any]:
    """Lee la cola del tatami por la via publica (sin sesion y sin borradores)."""
    url = f"{queue_url(tournament_id, court_id)}?no_draft_rounds=true"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            body = await response.text()
            assert response.status == status.HTTP_200_OK, body
            return cast("dict[str, Any]", json.loads(body))


def matches_of(body: dict[str, Any], stage_item_id: StageItemId) -> list[dict[str, Any]]:
    """Combates publicados del cuadro, tal cual los da la cola (sin reordenar nada)."""
    for stage_item in body["stage_items"]:
        if stage_item["stage_item_id"] == stage_item_id:
            return list(stage_item["matches"])
    raise AssertionError(f"el cuadro {stage_item_id} no esta en la cola: {body}")


def match_by_id(matches: list[dict[str, Any]], match_id: MatchId) -> dict[str, Any]:
    for match in matches:
        if match["match_id"] == match_id:
            return match
    raise AssertionError(f"el combate {match_id} no esta en la cola: {matches}")


async def put_result(
    *,
    tournament_id: TournamentId,
    match_id: MatchId,
    round_id: int,
    court_id: CourtId,
    score_a: int,
    score_b: int,
    token: str,
) -> None:
    """Publica un resultado con el mismo contrato de escritura que usa el bridge."""
    payload = {
        "round_id": round_id,
        "stage_item_input1_score": score_a,
        "stage_item_input2_score": score_b,
        "court_id": int(court_id),
    }
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as session:
        async with session.put(
            match_url(tournament_id, match_id), json=payload, headers=headers
        ) as response:
            body = await response.text()
            assert response.status == status.HTTP_200_OK, body


def waiting_sides(match: dict[str, Any]) -> list[str]:
    return [waited["side"] for waited in match["waiting_for"]]


@pytest.mark.asyncio(loop_scope="session")
async def test_queue_publishes_the_two_first_fights_and_the_final_waiting(
    lab: GenerationLab,  # noqa: F811  (fixtures importados del modulo de contrato)
    progression: ProgressionLab,
) -> None:
    """De un cuadro de dos rondas: dos combates jugables y la final esperando a los ganadores."""
    body = await read_queue(lab.tournament, progression.court_id)
    matches = matches_of(body, progression.stage_item_id)

    assert [match["match_id"] for match in matches] == [
        progression.semi_a,
        progression.semi_b,
        progression.final,
    ]
    for match in matches:
        assert set(match) == QUEUE_MATCH_FIELDS, match
        assert match["court_id"] == progression.court_id, match

    semi_a = match_by_id(matches, progression.semi_a)
    semi_b = match_by_id(matches, progression.semi_b)
    final = match_by_id(matches, progression.final)

    assert semi_a["state"] == PLAYABLE and semi_b["state"] == PLAYABLE
    assert semi_a["fighter_a"]["name"] == TEAM_NAMES[0]
    assert semi_a["fighter_b"]["name"] == TEAM_NAMES[1]
    assert semi_b["fighter_a"]["name"] == TEAM_NAMES[2]

    # La final no esta jugable y dice por que: espera a los ganadores de los dos combates previos.
    assert final["state"] == WAITING
    assert final["fighter_a"] is None and final["fighter_b"] is None
    assert final["waiting_for"] == [
        {
            "side": "a",
            "from_stage_item_id": None,
            "winner_position": None,
            "from_match_id": progression.semi_a,
        },
        {
            "side": "b",
            "from_stage_item_id": None,
            "winner_position": None,
            "from_match_id": progression.semi_b,
        },
    ]

    # El primero jugable en el orden del contrato, y sin estado de combate que inventar.
    assert body["next_match_id"] == progression.semi_a


@pytest.mark.asyncio(loop_scope="session")
async def test_result_resolves_only_that_pass_of_the_final(
    lab: GenerationLab,  # noqa: F811  (fixtures importados del modulo de contrato)
    progression: ProgressionLab,
) -> None:
    """Un resultado resuelve el pase correspondiente de la final; el otro sigue pendiente."""
    await put_result(
        tournament_id=lab.tournament,
        match_id=progression.semi_a,
        round_id=progression.first_round_id,
        court_id=progression.court_id,
        score_a=2,
        score_b=0,
        token=lab.owner_token,
    )
    matches = matches_of(
        await read_queue(lab.tournament, progression.court_id), progression.stage_item_id
    )
    final = match_by_id(matches, progression.final)

    assert final["state"] == WAITING
    assert final["fighter_a"]["name"] == TEAM_NAMES[0]
    assert final["fighter_b"] is None
    assert waiting_sides(final) == ["b"]

    # GAP DOCUMENTADO: el modelo no tiene hecho durable de "jugado", asi que el combate con
    # resultado se sigue publicando como jugable. No se infiere del marcador ni se filtra aqui.
    semi_a = match_by_id(matches, progression.semi_a)
    assert semi_a["state"] == PLAYABLE
    assert semi_a["score_a"] == 2 and semi_a["score_b"] == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_second_result_makes_the_final_playable(
    lab: GenerationLab,  # noqa: F811  (fixtures importados del modulo de contrato)
    progression: ProgressionLab,
) -> None:
    """Con los dos pases resueltos, el combate de la ronda siguiente pasa a jugable."""
    for match_id, score_a, score_b in (
        (progression.semi_a, 3, 1),
        (progression.semi_b, 0, 4),
    ):
        await put_result(
            tournament_id=lab.tournament,
            match_id=match_id,
            round_id=progression.first_round_id,
            court_id=progression.court_id,
            score_a=score_a,
            score_b=score_b,
            token=lab.owner_token,
        )

    matches = matches_of(
        await read_queue(lab.tournament, progression.court_id), progression.stage_item_id
    )
    final = match_by_id(matches, progression.final)

    assert final["state"] == PLAYABLE
    assert final["waiting_for"] == []
    assert final["fighter_a"]["name"] == TEAM_NAMES[0]
    assert final["fighter_b"]["name"] == TEAM_NAMES[3]


@pytest.mark.asyncio(loop_scope="session")
async def test_replaying_a_result_does_not_advance_twice(
    lab: GenerationLab,  # noqa: F811  (fixtures importados del modulo de contrato)
    progression: ProgressionLab,
) -> None:
    """Reenviar el mismo resultado (reintento de red) deja la cola exactamente igual."""
    for _ in range(2):
        for match_id, score_a, score_b in ((progression.semi_a, 5, 2), (progression.semi_b, 1, 0)):
            await put_result(
                tournament_id=lab.tournament,
                match_id=match_id,
                round_id=progression.first_round_id,
                court_id=progression.court_id,
                score_a=score_a,
                score_b=score_b,
                token=lab.owner_token,
            )

    body = await read_queue(lab.tournament, progression.court_id)
    matches = matches_of(body, progression.stage_item_id)
    final = match_by_id(matches, progression.final)

    # El avance es una asignacion de estado, no un incremento: repetirlo converge al mismo sitio.
    assert final["state"] == PLAYABLE
    assert final["fighter_a"]["name"] == TEAM_NAMES[0]
    assert final["fighter_b"]["name"] == TEAM_NAMES[2]
    assert len(matches) == 3, matches
