# pylint: disable=redefined-outer-name  # los fixtures vienen del modulo de contrato
"""Contrato RED/GREEN del resultado de combate con ganador y metodo (S4.4b, F3C, LAB ONLY).

El backend no tenia hecho durable de "combate jugado": solo dos puntuaciones, y el ganador se
deducia comparandolas. Con eso, un combate decidido por sumision, decision, descalificacion,
walkover o parada del arbitro —o por puntos empatados— no avanzaba a nadie, y la cola no podia
publicar "terminado" ni elegir el siguiente combate sin inventarse el estado.

Este modulo fija el contrato minimo que cierra el gap:

* el resultado se declara con **ganador explicito** y **metodo** de un catalogo cerrado;
* el ganador avanza con **cualquier** metodo, no solo por puntos;
* el perdedor no avanza;
* la cola publica el combate como ``completed`` y elige ``current``/``next`` saltandolo;
* repetir el envio converge (misma respuesta, sin doble avance);
* el fin del cuadro deja la cola sin combates pendientes.

Es el mismo contrato que ya usa el marcador en su maquina de estados local
(``ready -> running <-> paused -> awaiting_result -> finished``, con ``winner_team_id`` y
``method``); aqui solo se persiste en el backend, que es la autoridad.
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
    assign_ok,
    idempotency_secret,
    lab,
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

#: Catalogo cerrado, compartido con la maquina de estados del marcador (state.js).
METHODS = (
    "points",
    "submission",
    "decision",
    "disqualification",
    "walkover",
    "referee_stoppage",
    "other",
)

TEAM_NAMES = ("Metodo A", "Metodo B", "Metodo C", "Metodo D")


@dataclass(frozen=True)
class ResultExecutionLab:  # pylint: disable=too-many-instance-attributes
    """Cuadro de eliminacion directa de 4 competidores en un tatami, sin jugar todavia."""

    stage_item_id: StageItemId
    court_id: CourtId
    round_1: RoundId
    final_round: RoundId
    semi_a: MatchId
    semi_b: MatchId
    final: MatchId
    input_ids: tuple[StageItemInputId, StageItemInputId, StageItemInputId, StageItemInputId]


@pytest_asyncio.fixture(loop_scope="session", scope="function")
async def execution(
    lab: GenerationLab,  # noqa: F811
) -> AsyncIterator[ResultExecutionLab]:
    """Cuadro de 4 en un tatami: dos semifinales y una final enlazada a sus ganadores.

    La final se crea con el enlace que usa el propio backend (``winner_from_match_id`` sobre cada
    semifinal), de modo que el avance lo decide la logica del backend. El tatami se crea antes que
    los combates: su desmontaje ocurre despues, cuando ningun combate lo referencia ya.
    """
    now = datetime_utc.now()
    async with AsyncExitStack() as stack:
        ranking = await stack.enter_async_context(
            inserted_ranking(DUMMY_RANKING1.model_copy(update={"tournament_id": lab.tournament}))
        )
        stage = await stack.enter_async_context(
            inserted_stage(
                DUMMY_STAGE2.model_copy(update={"tournament_id": lab.tournament, "name": "Metodos"})
            )
        )
        stage_item = await stack.enter_async_context(
            inserted_stage_item(
                StageItemInsertable(
                    stage_id=stage.id,
                    name="Cuadro metodos",
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
                    update={
                        "tournament_id": lab.tournament,
                        "name": "Tatami metodos",
                        "created": now,
                    }
                )
            )
        )
        round_1 = await stack.enter_async_context(
            inserted_round(
                RoundInsertable(
                    created=now, stage_item_id=stage_item.id, is_draft=False, name="Semifinales"
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

        def match(**kwargs: Any) -> MatchInsertable:
            return MatchInsertable(
                created=now,
                duration_minutes=5,
                margin_minutes=5,
                court_id=court.id,
                stage_item_input1_score=0,
                stage_item_input2_score=0,
                stage_item_input1_conflict=False,
                stage_item_input2_conflict=False,
                **kwargs,
            )

        semi_a = await stack.enter_async_context(
            inserted_match(
                match(
                    round_id=round_1.id,
                    stage_item_input1_id=inputs[0].id,
                    stage_item_input2_id=inputs[1].id,
                )
            )
        )
        semi_b = await stack.enter_async_context(
            inserted_match(
                match(
                    round_id=round_1.id,
                    stage_item_input1_id=inputs[2].id,
                    stage_item_input2_id=inputs[3].id,
                )
            )
        )
        final = await stack.enter_async_context(
            inserted_match(
                match(
                    round_id=final_round.id,
                    stage_item_input1_id=None,
                    stage_item_input2_id=None,
                    stage_item_input1_winner_from_match_id=semi_a.id,
                    stage_item_input2_winner_from_match_id=semi_b.id,
                )
            )
        )
        await assign_ok(lab, stage_item_id=stage_item.id, court_id=court.id)
        yield ResultExecutionLab(
            stage_item_id=stage_item.id,
            court_id=court.id,
            round_1=round_1.id,
            final_round=final_round.id,
            semi_a=semi_a.id,
            semi_b=semi_b.id,
            final=final.id,
            input_ids=(inputs[0].id, inputs[1].id, inputs[2].id, inputs[3].id),
        )


COMPLETED = "completed"
PLAYABLE = "playable"
WAITING = "waiting_on_winners"


async def read_queue(tournament_id: TournamentId, court_id: CourtId) -> dict[str, Any]:
    """Lee la cola del tatami por la via publica, sin sesion y sin borradores."""
    url = f"{get_root_uvicorn_url()}tournaments/{tournament_id}/courts/{court_id}/queue"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params={"no_draft_rounds": "true"}) as response:
            body = await response.text()
            assert response.status == status.HTTP_200_OK, body
            return cast("dict[str, Any]", json.loads(body))


def published(queue: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Todos los combates publicados del tatami, indexados por id."""
    return {
        match["match_id"]: match
        for stage_item in queue["stage_items"]
        for match in stage_item["matches"]
    }


async def submit_result(
    *,
    tournament_id: TournamentId,
    match_id: MatchId,
    round_id: RoundId,
    court_id: CourtId,
    token: str,
    score_a: int = 0,
    score_b: int = 0,
    winner_input_id: StageItemInputId | None = None,
    method: str | None = None,
) -> int:
    """Escribe el resultado y devuelve el codigo de estado (para los casos de rechazo)."""
    url = f"{get_root_uvicorn_url()}tournaments/{tournament_id}/matches/{match_id}"
    payload: dict[str, Any] = {
        "round_id": int(round_id),
        "stage_item_input1_score": score_a,
        "stage_item_input2_score": score_b,
        "court_id": int(court_id),
    }
    if winner_input_id is not None:
        payload["winner_stage_item_input_id"] = int(winner_input_id)
    if method is not None:
        payload["result_method"] = method
    async with aiohttp.ClientSession() as session:
        async with session.put(
            url, json=payload, headers={"Authorization": f"Bearer {token}"}
        ) as response:
            await response.text()
            return response.status


def owner_token(generation_lab: GenerationLab) -> str:
    return generation_lab.owner_token


@pytest.mark.asyncio(loop_scope="session")
async def test_a_submission_win_advances_the_winner(
    lab: GenerationLab,  # noqa: F811
    execution: ResultExecutionLab,
) -> None:
    """Con 0-0 y ganador declarado por sumision, el ganador avanza igual que por puntos."""
    status_code = await submit_result(
        tournament_id=lab.tournament,
        match_id=execution.semi_a,
        round_id=execution.round_1,
        court_id=execution.court_id,
        token=owner_token(lab),
        score_a=0,
        score_b=0,
        winner_input_id=execution.input_ids[0],
        method="submission",
    )
    assert status_code == status.HTTP_200_OK

    queue = await read_queue(lab.tournament, execution.court_id)
    final = published(queue)[execution.final]
    assert final["state"] == WAITING
    assert final["fighter_a"] is not None
    assert final["fighter_a"]["name"] == TEAM_NAMES[0]
    assert final["fighter_b"] is None


@pytest.mark.asyncio(loop_scope="session")
async def test_the_queue_publishes_the_result_and_its_method(
    lab: GenerationLab,  # noqa: F811
    execution: ResultExecutionLab,
) -> None:
    """El combate jugado se publica como ``completed`` con ganador y metodo (no se infiere)."""
    await submit_result(
        tournament_id=lab.tournament,
        match_id=execution.semi_a,
        round_id=execution.round_1,
        court_id=execution.court_id,
        token=owner_token(lab),
        winner_input_id=execution.input_ids[0],
        method="submission",
    )
    queue = await read_queue(lab.tournament, execution.court_id)
    match = published(queue)[execution.semi_a]
    assert match["state"] == COMPLETED
    assert match["winner_stage_item_input_id"] == execution.input_ids[0]
    assert match["result_method"] == "submission"


@pytest.mark.asyncio(loop_scope="session")
async def test_the_loser_does_not_advance(
    lab: GenerationLab,  # noqa: F811
    execution: ResultExecutionLab,
) -> None:
    """El perdedor no aparece en el combate siguiente en ningun caso."""
    await submit_result(
        tournament_id=lab.tournament,
        match_id=execution.semi_a,
        round_id=execution.round_1,
        court_id=execution.court_id,
        token=owner_token(lab),
        winner_input_id=execution.input_ids[0],
        method="decision",
    )
    queue = await read_queue(lab.tournament, execution.court_id)
    final = published(queue)[execution.final]
    assert final["fighter_a"] is not None
    assert final["fighter_a"]["name"] == TEAM_NAMES[0]
    assert final["fighter_a"]["stage_item_input_id"] != execution.input_ids[1]
    assert final["fighter_b"] is None


@pytest.mark.asyncio(loop_scope="session")
async def test_current_and_next_skip_the_completed_match(
    lab: GenerationLab,  # noqa: F811
    execution: ResultExecutionLab,
) -> None:
    """``current`` es el primer jugable (no el id menor) y ``next`` el siguiente."""
    queue = await read_queue(lab.tournament, execution.court_id)
    assert queue["current_match_id"] == execution.semi_a
    assert queue["next_match_id"] == execution.semi_b

    await submit_result(
        tournament_id=lab.tournament,
        match_id=execution.semi_a,
        round_id=execution.round_1,
        court_id=execution.court_id,
        token=owner_token(lab),
        winner_input_id=execution.input_ids[0],
        method="points",
        score_a=7,
        score_b=0,
    )
    after = await read_queue(lab.tournament, execution.court_id)
    assert after["current_match_id"] == execution.semi_b
    assert after["next_match_id"] is None


@pytest.mark.asyncio(loop_scope="session")
async def test_an_unknown_method_is_rejected(
    lab: GenerationLab,  # noqa: F811
    execution: ResultExecutionLab,
) -> None:
    """El catalogo de metodos es cerrado: un metodo inventado no escribe nada."""
    status_code = await submit_result(
        tournament_id=lab.tournament,
        match_id=execution.semi_a,
        round_id=execution.round_1,
        court_id=execution.court_id,
        token=owner_token(lab),
        winner_input_id=execution.input_ids[0],
        method="kata",
    )
    assert status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    queue = await read_queue(lab.tournament, execution.court_id)
    assert published(queue)[execution.semi_a]["state"] == PLAYABLE


@pytest.mark.asyncio(loop_scope="session")
async def test_a_winner_that_is_not_a_participant_is_rejected(
    lab: GenerationLab,  # noqa: F811
    execution: ResultExecutionLab,
) -> None:
    """Un ganador que no es ninguno de los dos pases del combate no se acepta."""
    status_code = await submit_result(
        tournament_id=lab.tournament,
        match_id=execution.semi_a,
        round_id=execution.round_1,
        court_id=execution.court_id,
        token=owner_token(lab),
        winner_input_id=execution.input_ids[2],
        method="points",
        score_a=1,
        score_b=0,
    )
    assert status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.asyncio(loop_scope="session")
async def test_a_winner_without_a_method_is_rejected(
    lab: GenerationLab,  # noqa: F811
    execution: ResultExecutionLab,
) -> None:
    """Los dos campos del resultado van juntos: ganador sin metodo tampoco se acepta."""
    status_code = await submit_result(
        tournament_id=lab.tournament,
        match_id=execution.semi_a,
        round_id=execution.round_1,
        court_id=execution.court_id,
        token=owner_token(lab),
        winner_input_id=execution.input_ids[0],
    )
    assert status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.asyncio(loop_scope="session")
async def test_replaying_the_same_result_does_not_advance_twice(
    lab: GenerationLab,  # noqa: F811
    execution: ResultExecutionLab,
) -> None:
    """Reenviar el mismo resultado converge: la cola queda identica (sin doble avance)."""
    for _ in range(2):
        status_code = await submit_result(
            tournament_id=lab.tournament,
            match_id=execution.semi_a,
            round_id=execution.round_1,
            court_id=execution.court_id,
            token=owner_token(lab),
            winner_input_id=execution.input_ids[1],
            method="submission",
        )
        assert status_code == status.HTTP_200_OK

    queue = await read_queue(lab.tournament, execution.court_id)
    matches = published(queue)
    assert matches[execution.semi_a]["winner_stage_item_input_id"] == execution.input_ids[1]
    final = matches[execution.final]
    assert final["fighter_a"] is not None
    assert final["fighter_a"]["stage_item_input_id"] == execution.input_ids[1]
    assert final["fighter_b"] is None


@pytest.mark.asyncio(loop_scope="session")
async def test_the_end_of_the_bracket_leaves_no_pending_match(
    lab: GenerationLab,  # noqa: F811
    execution: ResultExecutionLab,
) -> None:
    """Con las dos semifinales y la final resueltas, la cola no tiene nada pendiente."""
    for match_id, winner, method in (
        (execution.semi_a, execution.input_ids[0], "submission"),
        (execution.semi_b, execution.input_ids[2], "walkover"),
    ):
        status_code = await submit_result(
            tournament_id=lab.tournament,
            match_id=match_id,
            round_id=execution.round_1,
            court_id=execution.court_id,
            token=owner_token(lab),
            winner_input_id=winner,
            method=method,
        )
        assert status_code == status.HTTP_200_OK

    ready = await read_queue(lab.tournament, execution.court_id)
    final = published(ready)[execution.final]
    assert final["state"] == PLAYABLE
    assert final["fighter_a"]["stage_item_input_id"] == execution.input_ids[0]
    assert final["fighter_b"]["stage_item_input_id"] == execution.input_ids[2]
    assert ready["current_match_id"] == execution.final

    status_code = await submit_result(
        tournament_id=lab.tournament,
        match_id=execution.final,
        round_id=execution.final_round,
        court_id=execution.court_id,
        token=owner_token(lab),
        winner_input_id=execution.input_ids[0],
        method="points",
        score_a=2,
        score_b=0,
    )
    assert status_code == status.HTTP_200_OK

    closed = await read_queue(lab.tournament, execution.court_id)
    matches = published(closed)
    assert all(match["state"] == COMPLETED for match in matches.values())
    assert closed["current_match_id"] is None
    assert closed["next_match_id"] is None
    assert matches[execution.final]["winner_stage_item_input_id"] == execution.input_ids[0]
