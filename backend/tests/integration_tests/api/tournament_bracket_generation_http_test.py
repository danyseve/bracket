# pylint: disable=redefined-outer-name
"""Contrato HTTP de la generacion explicita del cuadro del torneo (S3.5a, F3B, LAB ONLY).

TDD: este modulo se escribio **antes** de la implementacion y fallo en rojo (la ruta no existia:
404 en lugar del contrato descrito en ``docs/26`` §44).

Lo que se fija aqui es el contrato observable de una operacion que **escribe**: quien puede pedirla
(OWNER-only), que exige (``Idempotency-Key`` y ningun cuerpo), que revalida antes de tocar nada (el
readiness de S3.4d recalculado dentro de la transaccion, nunca el que el cliente pudo leer antes),
que reutiliza (la generacion certificada de F2C, sin duplicar su algoritmo) y que garantiza
(atomicidad, una sola generacion y un solo evento de auditoria por efecto, replay estable, y
desenlaces 4xx/5xx sin 500 para estados de dominio esperables).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final
from uuid import uuid4

import aiohttp
import pytest
import pytest_asyncio
from heliclockter import datetime_utc
from starlette import status

from bracket.database import database
from bracket.logic.idempotency_protocol import RETRY_AFTER_SECONDS
from bracket.logic.scheduling.generation import BLOCKER_PREFIX
from bracket.models.db.club import ClubInsertable
from bracket.models.db.match import MatchInsertable
from bracket.models.db.round import RoundInsertable
from bracket.models.db.stage_item import StageItemInsertable, StageType
from bracket.models.db.stage_item_inputs import StageItemInputInsertable
from bracket.models.db.user_x_club import UserXClubInsertable, UserXClubRelation
from bracket.routes.domain_idempotency import IDEMPOTENCY_KEY_HEADER
from bracket.sql.users import update_user_active
from bracket.utils.dummy_records import (
    DUMMY_RANKING1,
    DUMMY_STAGE2,
    DUMMY_TEAM1,
    DUMMY_TEAM2,
    DUMMY_TOURNAMENT,
)
from bracket.utils.id_types import ClubId, CompetitorId, StageItemId, TournamentId
from tests.integration_tests.api.shared import get_root_uvicorn_url
from tests.integration_tests.mocks import get_mock_token, get_mock_user
from tests.integration_tests.registration_fixtures import (
    delete_competitor,
    delete_registration_audit,
    delete_registrations,
    insert_competitor,
    set_competitor_active,
    wait_for_waiting_backends,
)
from tests.integration_tests.sql import (
    inserted_club,
    inserted_match,
    inserted_ranking,
    inserted_stage,
    inserted_stage_item,
    inserted_stage_item_input,
    inserted_team,
    inserted_tournament,
    inserted_user,
    inserted_user_x_club,
)

#: Campos que publica la respuesta de la generacion. Un campo de mas es un fallo: se cierra.
GENERATION_FIELDS: Final = {"tournament_id", "generated", "stage_items", "skipped_stage_items"}
#: Campos de cada cuadro: los de reparto que ya publica la ruta de F2C, ni uno mas.
STAGE_ITEM_FIELDS: Final = {
    "stage_item_id",
    "entrant_count",
    "bracket_size",
    "bye_count",
    "ghost_count",
}
#: Campos del rechazo por readiness: codigo estable primero, bloqueos derivados despues.
NOT_READY_FIELDS: Final = {"code", "blocking_issues", "registration_window"}

INSERT_REGISTRATION = """
    INSERT INTO tournament_registrations (
        tournament_id, competitor_id, identity_status, representation, sports_club_id,
        affiliation_id, category_key, category_label, competitor_name_snapshot,
        sports_club_name_snapshot, status, revision, superseded_by_registration_id, created
    ) VALUES (
        :tournament_id, :competitor_id,
        CAST(:identity_status AS registration_identity_status),
        CAST(:representation AS registration_representation),
        :sports_club_id, :affiliation_id, :category_key, :category_label,
        :competitor_name_snapshot, :sports_club_name_snapshot,
        CAST(:status AS registration_status), 1, :superseded_by, NOW()
    )
    RETURNING id
"""

LOCK_TOURNAMENT_SHARE = (
    "SELECT id FROM tournaments WHERE id = :tournament_id FOR SHARE"
)


@dataclass(frozen=True)
class GenerationLab:
    """Tenant con dos torneos propios, uno ajeno, cuatro competidores y las sesiones del arnes."""

    tenant: ClubId
    foreign_tenant: ClubId
    tournament: TournamentId
    other_tournament: TournamentId
    foreign_tournament: TournamentId
    competitors: tuple[CompetitorId, ...]
    foreign_competitor: CompetitorId
    owner_token: str
    collaborator_token: str
    outsider_token: str
    inactive_token: str


async def delete_generation_audit(tenant_ids: tuple[int, ...]) -> None:
    """Retira TODA la auditoria del laboratorio, sea cual sea la entidad que la escribio."""
    await database.execute(
        query="DELETE FROM domain_change_log WHERE tenant_club_id = ANY(:ids)",
        values={"ids": list(tenant_ids)},
    )


@asynccontextmanager
async def generation_lab_context() -> AsyncIterator[GenerationLab]:
    """Datos y sesiones del laboratorio; la limpieza se apila de hojas a raices."""
    now = datetime_utc.now()
    suffix = uuid4().hex[:12]

    async with AsyncExitStack() as stack:
        club = await stack.enter_async_context(
            inserted_club(ClubInsertable(name=f"f3b-s35a-{suffix}", created=now))
        )
        other_club = await stack.enter_async_context(
            inserted_club(ClubInsertable(name=f"f3b-s35a-other-{suffix}", created=now))
        )
        tournaments = [
            await stack.enter_async_context(
                inserted_tournament(
                    DUMMY_TOURNAMENT.model_copy(
                        update={
                            "club_id": club.id,
                            "dashboard_endpoint": f"f3b-s35a-{suffix}-{letter}",
                        }
                    )
                )
            )
            for letter in ("a", "b")
        ]
        foreign_tournament = await stack.enter_async_context(
            inserted_tournament(
                DUMMY_TOURNAMENT.model_copy(
                    update={"club_id": other_club.id, "dashboard_endpoint": f"f3b-s35a-{suffix}-c"}
                )
            )
        )

        owner = await stack.enter_async_context(inserted_user(get_mock_user()))
        collaborator = await stack.enter_async_context(inserted_user(get_mock_user()))
        outsider = await stack.enter_async_context(inserted_user(get_mock_user()))
        inactive = await stack.enter_async_context(inserted_user(get_mock_user()))
        for user, target_club, relation in (
            (owner, club, UserXClubRelation.OWNER),
            (collaborator, club, UserXClubRelation.COLLABORATOR),
            (outsider, other_club, UserXClubRelation.OWNER),
            (inactive, club, UserXClubRelation.OWNER),
        ):
            await stack.enter_async_context(
                inserted_user_x_club(
                    UserXClubInsertable(user_id=user.id, club_id=target_club.id, relation=relation)
                )
            )
        await update_user_active(inactive.id, False)

        competitors = tuple(
            [
                await insert_competitor(f"competidora-s35a-{suffix}-{index}", club.id, created=now)
                for index in range(4)
            ]
        )
        for competitor in competitors:
            stack.push_async_callback(delete_competitor, competitor)
        foreign_competitor = await insert_competitor(
            f"competidora-s35a-foreign-{suffix}", other_club.id, created=now
        )
        stack.push_async_callback(delete_competitor, foreign_competitor)

        # Lo ultimo apilado se deshace primero: inscripciones y auditoria antes que el resto.
        stack.push_async_callback(delete_generation_audit, (club.id, other_club.id))
        for tournament in (*tournaments, foreign_tournament):
            stack.push_async_callback(delete_registration_audit, (tournament.id,), club.id)
            stack.push_async_callback(delete_registrations, tournament.id)

        yield GenerationLab(
            tenant=club.id,
            foreign_tenant=other_club.id,
            tournament=tournaments[0].id,
            other_tournament=tournaments[1].id,
            foreign_tournament=foreign_tournament.id,
            competitors=competitors,
            foreign_competitor=foreign_competitor,
            owner_token=get_mock_token(owner.email),
            collaborator_token=get_mock_token(collaborator.email),
            outsider_token=get_mock_token(outsider.email),
            inactive_token=get_mock_token(inactive.email),
        )


@pytest_asyncio.fixture(loop_scope="session", scope="function")
async def lab() -> AsyncIterator[GenerationLab]:
    async with generation_lab_context() as generation_lab:
        yield generation_lab


def generation_url(tournament_id: TournamentId) -> str:
    return f"{get_root_uvicorn_url()}tournaments/{tournament_id}/generate-bracket"


def readiness_url(tournament_id: TournamentId) -> str:
    return f"{get_root_uvicorn_url()}tournaments/{tournament_id}/bracket-readiness"


def window_url(tournament_id: TournamentId) -> str:
    return f"{get_root_uvicorn_url()}tournaments/{tournament_id}/registration-window"


def close_url(tournament_id: TournamentId) -> str:
    return f"{get_root_uvicorn_url()}tournaments/{tournament_id}/registrations/close"


async def insert_registration(
    tournament_id: TournamentId,
    *,
    competitor_id: CompetitorId | None = None,
    status: str = "CONFIRMED",
    category_key: str | None = "gi-peso-83",
    category_label: str | None = "Gi -83 kg",
) -> int:
    """Inserta una fila con la forma exacta que necesita el caso, sin pasar por el ciclo de vida."""
    registration_id = await database.fetch_val(
        query=INSERT_REGISTRATION,
        values={
            "tournament_id": tournament_id,
            "competitor_id": competitor_id,
            "identity_status": "VERIFIED" if competitor_id is not None else "AMBIGUOUS",
            "representation": "INDEPENDENT",
            "sports_club_id": None,
            "affiliation_id": None,
            "category_key": category_key,
            "category_label": category_label,
            "competitor_name_snapshot": f"snapshot-{uuid4().hex[:8]}",
            "sports_club_name_snapshot": None,
            "status": status,
            "superseded_by": None,
        },
    )
    return int(registration_id)


async def close_registrations(lab: GenerationLab) -> None:
    """Cierra la ventana del torneo principal por la ruta de S3.4c. La sesion es de la peticion."""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            close_url(lab.tournament),
            headers={
                "Authorization": f"Bearer {lab.owner_token}",
                IDEMPOTENCY_KEY_HEADER: uuid4().hex,
            },
        ) as response:
            assert response.status == status.HTTP_200_OK, await response.text()


async def set_deadline(lab: GenerationLab, deadline: datetime_utc | None) -> None:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            window_url(lab.tournament),
            json={"registration_deadline": None if deadline is None else deadline.isoformat()},
            headers={
                "Authorization": f"Bearer {lab.owner_token}",
                IDEMPOTENCY_KEY_HEADER: uuid4().hex,
            },
        ) as response:
            assert response.status == status.HTTP_200_OK, await response.text()


async def ready_tournament(lab: GenerationLab) -> None:
    """Deja el torneo principal listo para generar: dos confirmadas y la ventana cerrada."""
    await insert_registration(lab.tournament, competitor_id=lab.competitors[0])
    await insert_registration(lab.tournament, competitor_id=lab.competitors[1])
    await close_registrations(lab)


async def generate(
    lab: GenerationLab,
    *,
    key: str | None = None,
    tournament: TournamentId | None = None,
    token: str | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    """Peticion cruda a la ruta. La sesion es de la peticion, no del caso."""
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if key is not None:
        headers[IDEMPOTENCY_KEY_HEADER] = key
    target = lab.tournament if tournament is None else tournament
    async with aiohttp.ClientSession() as session:
        async with session.post(generation_url(target), headers=headers, json=body) as response:
            text = await response.text()
            content_type = response.headers.get("content-type", "")
            assert content_type.startswith("application/json"), text
            return response.status, dict(response.headers), json.loads(text)


async def call_generation(lab: GenerationLab) -> dict[str, Any]:
    """Generacion del torneo principal con la sesion OWNER, exigiendo 200 y forma publicada."""
    status_code, _, body = await generate(lab, key=uuid4().hex, token=lab.owner_token)
    assert status_code == status.HTTP_200_OK, body
    assert set(body) == GENERATION_FIELDS, body
    for stage_item in body["stage_items"]:
        assert set(stage_item) == STAGE_ITEM_FIELDS, stage_item
    return body


async def call_readiness(lab: GenerationLab) -> dict[str, Any]:
    """Revision del torneo principal con la sesion OWNER, para componer los casos."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            readiness_url(lab.tournament),
            headers={"Authorization": f"Bearer {lab.owner_token}"},
        ) as response:
            assert response.status == status.HTTP_200_OK, await response.text()
            return await response.json()


async def layout(stage_item_id: StageItemId) -> list[int | None]:
    """Ocupantes de las ranuras del cuadro, por numero de ranura."""
    rows = await database.fetch_all(
        query=(
            "SELECT slot, team_id FROM stage_item_inputs WHERE stage_item_id = :stage_item_id "
            "ORDER BY slot"
        ),
        values={"stage_item_id": stage_item_id},
    )
    return [None if row["team_id"] is None else int(row["team_id"]) for row in rows]


async def audit_rows(action: str = "GENERATE_BRACKET") -> list[dict[str, Any]]:
    """Filas de auditoria de una accion, en orden determinista, con los campos publicados."""
    rows = await database.fetch_all(
        query=(
            "SELECT entity, entity_id, action, actor_user_id, reason_code, reason_note, "
            "changed_fields FROM domain_change_log WHERE action = :action ORDER BY id"
        ),
        values={"action": action},
    )
    return [dict(row._mapping) for row in rows]


async def reserved_paths() -> list[str]:
    """Caminos de las reservas de idempotencia que quedaron registradas."""
    rows = await database.fetch_all(
        query="SELECT request_path FROM domain_idempotency_keys ORDER BY id"
    )
    return [str(row["request_path"]) for row in rows]


async def generation_reservations() -> int:
    return int(
        await database.fetch_val(
            query=(
                "SELECT count(*) FROM domain_idempotency_keys "
                "WHERE request_path LIKE '%/generate-bracket'"
            )
        )
    )


def issues_by_code(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Bloqueos derivados del rechazo, indexados por codigo."""
    return {issue["code"]: issue for issue in body["detail"]["blocking_issues"]}


@asynccontextmanager
async def bracket_structure(
    lab: GenerationLab,
    *,
    team_count: int = 4,
    entrant_slots: tuple[int, ...] = (0, 1),
    slot_override: int | None = None,
) -> AsyncIterator[StageItemId]:
    """Cuadro de eliminacion directa con equipo en las ranuras pedidas y sin nada encima.

    ``slot_override`` fuerza un numero de ranuras distinto del ``team_count``: es la estructura
    incoherente que la generacion de F2C rechaza (409) y la que demuestra el rollback completo.
    """
    now = datetime_utc.now()
    async with AsyncExitStack() as stack:
        ranking = await stack.enter_async_context(
            inserted_ranking(DUMMY_RANKING1.model_copy(update={"tournament_id": lab.tournament}))
        )
        stage = await stack.enter_async_context(
            inserted_stage(
                DUMMY_STAGE2.model_copy(update={"tournament_id": lab.tournament, "name": "S3.5a"})
            )
        )
        stage_item = await stack.enter_async_context(
            inserted_stage_item(
                StageItemInsertable(
                    stage_id=stage.id,
                    name="Cuadro S3.5a",
                    created=now,
                    type=StageType.SINGLE_ELIMINATION,
                    team_count=team_count,
                    ranking_id=ranking.id,
                )
            )
        )
        inputs = []
        for slot in range(team_count if slot_override is None else slot_override):
            team = None
            if slot in entrant_slots:
                dummy = DUMMY_TEAM1 if slot == entrant_slots[0] else DUMMY_TEAM2
                team = await stack.enter_async_context(
                    inserted_team(dummy.model_copy(update={"tournament_id": lab.tournament}))
                )
            inputs.append(
                await stack.enter_async_context(
                    inserted_stage_item_input(
                        StageItemInputInsertable(
                            slot=slot,
                            team_id=None if team is None else team.id,
                            tournament_id=lab.tournament,
                            stage_item_id=stage_item.id,
                        )
                    )
                )
            )
        round_ = await stack.enter_async_context(
            inserted_round(
                RoundInsertable(
                    created=now, stage_item_id=stage_item.id, is_draft=False, name="Ronda 1"
                )
            )
        )
        await stack.enter_async_context(
            inserted_match(
                MatchInsertable(
                    created=now,
                    duration_minutes=10,
                    margin_minutes=5,
                    round_id=round_.id,
                    stage_item_input1_id=inputs[0].id,
                    stage_item_input2_id=inputs[1].id,
                    stage_item_input1_score=0,
                    stage_item_input2_score=0,
                    stage_item_input1_conflict=False,
                    stage_item_input2_conflict=False,
                )
            )
        )
        yield stage_item.id


async def set_tournament_archived(tournament_id: TournamentId, *, archived: bool) -> None:
    await database.execute(
        query=(
            "UPDATE tournaments SET status = CAST(:status AS tournament_status) WHERE id = :id"
        ),
        values={"status": "ARCHIVED" if archived else "OPEN", "id": tournament_id},
    )


def pairs_of(layout_values: list[int | None]) -> list[tuple[int | None, int | None]]:
    """Parejas de la primera ronda: dos ranuras consecutivas son una eliminatoria."""
    return list(zip(layout_values[::2], layout_values[1::2], strict=True))


# --- 1. quien puede pedirla y con que forma ---------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_is_owner_only(lab: GenerationLab) -> None:
    """COLLAB revisa el readiness, pero no puede disparar la generacion: es OWNER-only."""
    await ready_tournament(lab)

    async with bracket_structure(lab):
        assert (await call_readiness(lab))["ready"] is True
        status_code, _, body = await generate(
            lab, key=uuid4().hex, token=lab.collaborator_token
        )

    assert status_code == status.HTTP_403_FORBIDDEN
    assert body == {"detail": "ROLE_NOT_ALLOWED"}
    assert await audit_rows() == []
    assert await generation_reservations() == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_rejects_an_inactive_actor(lab: GenerationLab) -> None:
    """Un actor dado de baja no dispara una escritura: 401 y ni una reserva."""
    await ready_tournament(lab)

    status_code, _, _ = await generate(lab, key=uuid4().hex, token=lab.inactive_token)

    assert status_code == status.HTTP_401_UNAUTHORIZED
    assert await audit_rows() == []
    assert await generation_reservations() == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_hides_a_foreign_tournament(lab: GenerationLab) -> None:
    """Un torneo de otro tenant responde como uno inexistente y no deja rastro."""
    status_code, _, _ = await generate(
        lab, key=uuid4().hex, tournament=lab.foreign_tournament, token=lab.owner_token
    )

    assert status_code == status.HTTP_404_NOT_FOUND
    assert await audit_rows() == []
    assert await generation_reservations() == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_hides_the_state_of_a_foreign_tournament(
    lab: GenerationLab,
) -> None:
    """Ni el estado del torneo ajeno se filtra: 404, no el 400 de \"archivado\"."""
    await set_tournament_archived(lab.foreign_tournament, archived=True)

    status_code, _, _ = await generate(
        lab, key=uuid4().hex, tournament=lab.foreign_tournament, token=lab.owner_token
    )

    assert status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_rejects_an_archived_tournament(lab: GenerationLab) -> None:
    """Regla que ya existia en la generacion de F2C: un torneo archivado no se genera."""
    await ready_tournament(lab)
    await set_tournament_archived(lab.tournament, archived=True)

    status_code, _, body = await generate(lab, key=uuid4().hex, token=lab.owner_token)

    assert status_code == status.HTTP_400_BAD_REQUEST
    assert body == {"detail": "Can't update archived tournament"}
    assert await audit_rows() == []
    assert await generation_reservations() == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_requires_an_idempotency_key(lab: GenerationLab) -> None:
    """Sin ``Idempotency-Key`` no hay operacion: no se reserva nada ni se escribe nada."""
    await ready_tournament(lab)

    status_code, _, body = await generate(lab, token=lab.owner_token)

    assert status_code == status.HTTP_400_BAD_REQUEST
    assert body == {"detail": "IDEMPOTENCY_REQUEST_INVALID"}
    assert await audit_rows() == []
    assert await generation_reservations() == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_rejects_an_unexpected_body(lab: GenerationLab) -> None:
    """La generacion no lleva datos: un campo de mas es un 422, no un campo ignorado."""
    await ready_tournament(lab)

    status_code, _, _ = await generate(
        lab, key=uuid4().hex, token=lab.owner_token, body={"reason_note": "porque si"}
    )

    assert status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert await generation_reservations() == 0


# --- 2. revalidacion del readiness --------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_is_rejected_while_the_window_is_open(lab: GenerationLab) -> None:
    """Con la ventana abierta no se genera y no se escribe ni una ranura."""
    await insert_registration(lab.tournament, competitor_id=lab.competitors[0])
    await insert_registration(lab.tournament, competitor_id=lab.competitors[1])

    async with bracket_structure(lab) as stage_item_id:
        before = await layout(stage_item_id)
        status_code, _, body = await generate(lab, key=uuid4().hex, token=lab.owner_token)
        assert status_code == status.HTTP_409_CONFLICT
        assert set(body["detail"]) == NOT_READY_FIELDS
        assert body["detail"]["code"] == "BRACKET_NOT_READY"
        assert body["detail"]["registration_window"] == "OPEN"
        assert list(issues_by_code(body)) == ["REGISTRATION_WINDOW_OPEN"]
        assert await layout(stage_item_id) == before

    assert await audit_rows() == []
    assert await generation_reservations() == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_is_rejected_with_a_pending_draft(lab: GenerationLab) -> None:
    """Una inscripcion en borrador bloquea aunque la ventana ya este cerrada."""
    await ready_tournament(lab)
    await insert_registration(
        lab.tournament,
        competitor_id=lab.competitors[2],
        status="DRAFT",
        category_key="gi-peso-92",
        category_label="Gi -92 kg",
    )

    async with bracket_structure(lab):
        status_code, _, body = await generate(lab, key=uuid4().hex, token=lab.owner_token)

    assert status_code == status.HTTP_409_CONFLICT
    assert "REGISTRATIONS_PENDING" in issues_by_code(body)
    assert await audit_rows() == []


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_is_rejected_with_a_category_below_the_minimum(
    lab: GenerationLab,
) -> None:
    """Una categoria con una sola confirmada bloquea: el minimo es el de F2C."""
    await insert_registration(lab.tournament, competitor_id=lab.competitors[0])
    await insert_registration(
        lab.tournament,
        competitor_id=lab.competitors[1],
        category_key="gi-peso-92",
        category_label="Gi -92 kg",
    )
    await close_registrations(lab)

    status_code, _, body = await generate(lab, key=uuid4().hex, token=lab.owner_token)

    assert status_code == status.HTTP_409_CONFLICT
    issues = issues_by_code(body)
    assert set(issues) == {"CATEGORY_NOT_ENOUGH_COMPETITORS"}
    assert issues["CATEGORY_NOT_ENOUGH_COMPETITORS"]["count"] == 1
    assert issues["CATEGORY_NOT_ENOUGH_COMPETITORS"]["required_minimum"] == 2
    assert await audit_rows() == []


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_is_rejected_with_an_inactive_confirmed_competitor(
    lab: GenerationLab,
) -> None:
    """Una confirmada cuyo competidor esta de baja bloquea: no se genera con quien no puede competir."""
    await ready_tournament(lab)
    await set_competitor_active(lab.competitors[1], active=False)

    async with bracket_structure(lab) as stage_item_id:
        before = await layout(stage_item_id)
        status_code, _, body = await generate(lab, key=uuid4().hex, token=lab.owner_token)
        assert status_code == status.HTTP_409_CONFLICT
        assert set(issues_by_code(body)) == {"COMPETITOR_INACTIVE"}
        assert await layout(stage_item_id) == before

    assert await audit_rows() == []


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_lists_every_derived_blocker(lab: GenerationLab) -> None:
    """Cuando hay varios bloqueos, el rechazo los publica todos: no se queda en el primero."""
    await insert_registration(lab.tournament, competitor_id=lab.competitors[0])
    await insert_registration(
        lab.tournament,
        competitor_id=lab.competitors[1],
        status="DRAFT",
        category_key="gi-peso-92",
        category_label="Gi -92 kg",
    )

    status_code, _, body = await generate(lab, key=uuid4().hex, token=lab.owner_token)

    assert status_code == status.HTTP_409_CONFLICT
    assert set(issues_by_code(body)) == {
        "REGISTRATION_WINDOW_OPEN",
        "REGISTRATIONS_PENDING",
        "CATEGORY_NOT_ENOUGH_COMPETITORS",
    }


# --- 3. generacion ------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_generates_the_bye_aware_layout_once(lab: GenerationLab) -> None:
    """El caso normal: reparto con despedidas repartidas y un solo evento de auditoria."""
    await ready_tournament(lab)

    async with bracket_structure(lab) as stage_item_id:
        before = await layout(stage_item_id)
        body = await call_generation(lab)
        after = await layout(stage_item_id)

        assert sorted(value for value in after if value is not None) == sorted(
            value for value in before if value is not None
        )
        assert after != before
        assert all(
            not (left is None and right is None) for left, right in pairs_of(after)
        ), after

    assert body["tournament_id"] == lab.tournament
    assert body["generated"] is True
    assert body["skipped_stage_items"] == 0
    (stage_item,) = body["stage_items"]
    assert stage_item["stage_item_id"] == stage_item_id
    assert stage_item["entrant_count"] == 2
    assert stage_item["bracket_size"] == 4
    assert stage_item["bye_count"] == 2

    rows = await audit_rows()
    assert len(rows) == 1
    assert rows[0]["entity"] == "tournament"
    assert rows[0]["entity_id"] == lab.tournament
    assert rows[0]["reason_code"] is None
    assert rows[0]["reason_note"] is None
    assert rows[0]["changed_fields"] == ["stage_item_inputs.team_id"]
    assert await generation_reservations() == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_generates_with_a_passed_deadline(lab: GenerationLab) -> None:
    """DEADLINE_PASSED permite generar si la revision global esta lista, y no toca el plazo."""
    await set_deadline(lab, datetime_utc.now() - timedelta(hours=1))
    await insert_registration(lab.tournament, competitor_id=lab.competitors[0])
    await insert_registration(lab.tournament, competitor_id=lab.competitors[1])

    async with bracket_structure(lab):
        body = await call_generation(lab)
        after = await call_readiness(lab)

    assert body["generated"] is True
    assert after["registration_window"] == "DEADLINE_PASSED"
    assert len(await audit_rows()) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_generates_with_closed_registrations(lab: GenerationLab) -> None:
    """CLOSED permite generar si la revision global esta lista, y sigue cerrada despues."""
    await ready_tournament(lab)

    async with bracket_structure(lab):
        body = await call_generation(lab)
        after = await call_readiness(lab)

    assert body["generated"] is True
    assert after["registration_window"] == "CLOSED"
    assert after["ready"] is True


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_without_brackets_is_an_empty_success(lab: GenerationLab) -> None:
    """Sin cuadros de eliminacion directa no hay nada que repartir: 200 vacio y sin evento."""
    await ready_tournament(lab)

    body = await call_generation(lab)

    assert body == {
        "tournament_id": lab.tournament,
        "generated": False,
        "stage_items": [],
        "skipped_stage_items": 0,
    }
    assert await audit_rows() == []


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_skips_a_bracket_that_f2c_blocks(lab: GenerationLab) -> None:
    """Un cuadro que F2C bloquea no se toca ni se inventa un error: se cuenta aparte."""
    await ready_tournament(lab)

    async with bracket_structure(lab, team_count=4, entrant_slots=()) as empty_item:
        body = await call_generation(lab)
        assert await layout(empty_item) == [None, None, None, None]

    assert body["stage_items"] == []
    assert body["skipped_stage_items"] == 1
    assert body["generated"] is False
    assert await audit_rows() == []


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_rolls_everything_back_when_a_bracket_cannot_be_planned(
    lab: GenerationLab,
) -> None:
    """Atomicidad: un cuadro imposible de planificar deshace los anteriores y no deja reserva."""
    await ready_tournament(lab)

    async with bracket_structure(lab) as first_item:
        async with bracket_structure(lab, slot_override=3) as inconsistent_item:
            before_first = await layout(first_item)
            before_inconsistent = await layout(inconsistent_item)
            key = uuid4().hex

            status_code, _, body = await generate(lab, key=key, token=lab.owner_token)

            assert status_code == status.HTTP_409_CONFLICT
            assert BLOCKER_PREFIX in body["detail"]
            assert await layout(first_item) == before_first
            assert await layout(inconsistent_item) == before_inconsistent
            assert await audit_rows() == []
            assert await generation_reservations() == 0

            again, _, _ = await generate(lab, key=key, token=lab.owner_token)
            assert again == status.HTTP_409_CONFLICT


# --- 4. idempotencia ----------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_replays_the_same_key(lab: GenerationLab) -> None:
    """Misma clave: mismo cuerpo y mismo reparto, sin segunda generacion ni segundo evento."""
    await ready_tournament(lab)

    async with bracket_structure(lab) as stage_item_id:
        key = uuid4().hex
        first_status, _, first_body = await generate(lab, key=key, token=lab.owner_token)
        after_first = await layout(stage_item_id)
        second_status, _, second_body = await generate(lab, key=key, token=lab.owner_token)
        after_second = await layout(stage_item_id)

    assert first_status == second_status == status.HTTP_200_OK
    assert second_body == first_body
    assert after_second == after_first
    assert len(await audit_rows()) == 1
    assert await generation_reservations() == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_a_second_call_with_another_key_does_not_generate_twice(lab: GenerationLab) -> None:
    """Doble clic (dos claves): un solo reparto y un solo evento, sin doble auditoria."""
    await ready_tournament(lab)

    async with bracket_structure(lab) as stage_item_id:
        first = await call_generation(lab)
        after_first = await layout(stage_item_id)
        second = await call_generation(lab)
        after_second = await layout(stage_item_id)

    assert second == first
    assert after_second == after_first
    assert len(await audit_rows()) == 1
    assert await generation_reservations() == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_rejects_a_key_already_used_for_another_tournament(
    lab: GenerationLab,
) -> None:
    """La misma clave con otro contenido (otro torneo) es un conflicto, no un replay."""
    await ready_tournament(lab)

    key = uuid4().hex
    first_status, _, _ = await generate(lab, key=key, token=lab.owner_token)
    second_status, _, body = await generate(
        lab, key=key, tournament=lab.other_tournament, token=lab.owner_token
    )

    assert first_status == status.HTTP_200_OK
    assert second_status == status.HTTP_409_CONFLICT
    assert body["detail"] == "IDEMPOTENCY_KEY_REUSED"
    assert len(await audit_rows()) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_regenerates_when_the_layout_changed(lab: GenerationLab) -> None:
    """Regeneracion segun el contrato de F2C: si el reparto cambio, la nueva generacion lo rehace."""
    await ready_tournament(lab)

    async with bracket_structure(lab) as stage_item_id:
        await call_generation(lab)
        generated_layout = await layout(stage_item_id)
        # El organizador mueve un participante a mano: el reparto se queda fuera de su sitio.
        await database.execute(
            query=(
                "UPDATE stage_item_inputs SET team_id = NULL WHERE stage_item_id = :stage_item_id "
                "AND slot = (SELECT min(slot) FROM stage_item_inputs WHERE stage_item_id = :stage_item_id)"
            ),
            values={"stage_item_id": stage_item_id},
        )
        moved = await layout(stage_item_id)
        body = await call_generation(lab)

        assert moved != generated_layout
        assert await layout(stage_item_id) == generated_layout
        assert body["generated"] is True

    assert len(await audit_rows()) == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_is_blocked_when_a_bracket_has_activity(lab: GenerationLab) -> None:
    """Un cuadro ya repartido con actividad encima bloquea la operacion entera (regla de F2C)."""
    await ready_tournament(lab)

    async with bracket_structure(lab) as stage_item_id:
        await call_generation(lab)
        # Un resultado ya puntuado es actividad: F2C deja de admitir la regeneracion del cuadro.
        await database.execute(
            query=(
                "UPDATE matches SET stage_item_input1_score = 6 WHERE round_id IN "
                "(SELECT id FROM rounds WHERE stage_item_id = :stage_item_id)"
            ),
            values={"stage_item_id": stage_item_id},
        )

        status_code, _, body = await generate(lab, key=uuid4().hex, token=lab.owner_token)

        assert status_code == status.HTTP_409_CONFLICT
        assert body["detail"]["code"] == "BRACKET_NOT_READY"
        assert "BRACKET_ALREADY_EXISTS" in issues_by_code(body)

    assert len(await audit_rows()) == 1


# --- 5. concurrencia ----------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_a_generation_that_cannot_take_the_lock_answers_503(lab: GenerationLab) -> None:
    """Si el bloqueo del torneo no se consigue en el plazo: 503 con ``Retry-After``, no un 500."""
    await ready_tournament(lab)

    async with bracket_structure(lab) as stage_item_id:
        before = await layout(stage_item_id)
        async with database.transaction():
            # Otro actor (aqui, el propio test) mantiene la fila del torneo bloqueada.
            await database.execute(
                query="SELECT id FROM tournaments WHERE id = :tournament_id FOR NO KEY UPDATE",
                values={"tournament_id": lab.tournament},
            )
            status_code, headers, body = await generate(
                lab, key=uuid4().hex, token=lab.owner_token
            )

        retry_after = {key.lower(): value for key, value in headers.items()}.get("retry-after")
        assert status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert retry_after == str(RETRY_AFTER_SECONDS)
        assert body["detail"] == "LOCK_TIMEOUT"
        assert await layout(stage_item_id) == before

    assert await audit_rows() == []
    assert await generation_reservations() == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_a_generation_in_flight_makes_the_next_one_wait(lab: GenerationLab) -> None:
    """Generate vs Generate: el segundo espera el bloqueo del torneo y no reparte dos veces."""
    await ready_tournament(lab)

    async with bracket_structure(lab) as stage_item_id:
        async with database.transaction():
            await database.execute(
                query="LOCK TABLE domain_change_log IN ACCESS EXCLUSIVE MODE"
            )
            first = asyncio.create_task(generate(lab, key=uuid4().hex, token=lab.owner_token))
            await wait_for_waiting_backends("%domain_change_log%")

            second = asyncio.create_task(generate(lab, key=uuid4().hex, token=lab.owner_token))
            await wait_for_tuple_contention("tournaments")

        first_status, _, first_body = await first
        second_status, _, second_body = await second
        first_layout = await layout(stage_item_id)
        second_layout = await layout(stage_item_id)

    assert first_status == second_status == status.HTTP_200_OK
    assert second_body == first_body
    assert first_layout == second_layout
    assert len(await audit_rows()) == 1
    assert await generation_reservations() == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_a_generation_waits_for_a_registration_in_flight_and_revalidates(
    lab: GenerationLab,
) -> None:
    """Generate vs alta: la generacion espera y revisa **despues** del alta, no antes.

    El cliente ya habia visto ``ready: true``; mientras el alta se confirma, la generacion espera su
    bloqueo. Al desbloquearse recalcula la revision: aparece el borrador y se rechaza. Es la prueba
    de que el veredicto no viaja con la peticion.
    """
    await ready_tournament(lab)

    async with bracket_structure(lab) as stage_item_id:
        before = await layout(stage_item_id)
        assert (await call_readiness(lab))["ready"] is True

        async with database.transaction():
            await database.execute(
                query=LOCK_TOURNAMENT_SHARE, values={"tournament_id": lab.tournament}
            )
            await insert_registration(
                lab.tournament,
                competitor_id=lab.competitors[2],
                status="DRAFT",
                category_key="gi-peso-92",
                category_label="Gi -92 kg",
            )
            pending = asyncio.create_task(generate(lab, key=uuid4().hex, token=lab.owner_token))
            await wait_for_tuple_contention("tournaments")

        status_code, _, body = await pending

        assert status_code == status.HTTP_409_CONFLICT
        assert "REGISTRATIONS_PENDING" in issues_by_code(body)
        assert await layout(stage_item_id) == before

    assert await audit_rows() == []
    assert await generation_reservations() == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_a_generation_waits_for_the_close_and_then_generates(lab: GenerationLab) -> None:
    """Generate vs cierre: se pide con la ventana abierta y sale bien porque revalida al desbloquear.

    La revision previa diria \"no\" (ventana ``OPEN``); la operacion espera su bloqueo, el cierre se
    confirma y la revision que decide es la de despues. Ni cierra la ventana ni la toca.
    """
    await insert_registration(lab.tournament, competitor_id=lab.competitors[0])
    await insert_registration(lab.tournament, competitor_id=lab.competitors[1])

    async with bracket_structure(lab) as stage_item_id:
        assert (await call_readiness(lab))["ready"] is False

        async with database.transaction():
            await database.execute(
                query="LOCK TABLE domain_change_log IN ACCESS EXCLUSIVE MODE"
            )
            closing = asyncio.create_task(close_registrations(lab))
            await wait_for_waiting_backends("%domain_change_log%")

            pending = asyncio.create_task(generate(lab, key=uuid4().hex, token=lab.owner_token))
            await wait_for_tuple_contention("tournaments")

        await closing
        status_code, _, body = await pending
        after = await call_readiness(lab)
        after_layout = await layout(stage_item_id)

    assert status_code == status.HTTP_200_OK
    assert body["generated"] is True
    assert after["registration_window"] == "CLOSED"
    assert all(not (left is None and right is None) for left, right in pairs_of(after_layout))


@pytest.mark.asyncio(loop_scope="session")
async def test_a_generation_waits_for_a_lifecycle_change_and_revalidates(
    lab: GenerationLab,
) -> None:
    """Generate vs ciclo de vida: la baja del competidor se ve porque se espera y se revalida."""
    await ready_tournament(lab)

    async with bracket_structure(lab) as stage_item_id:
        before = await layout(stage_item_id)
        assert (await call_readiness(lab))["ready"] is True

        async with database.transaction():
            await database.execute(
                query=LOCK_TOURNAMENT_SHARE, values={"tournament_id": lab.tournament}
            )
            await set_competitor_active(lab.competitors[1], active=False)
            pending = asyncio.create_task(generate(lab, key=uuid4().hex, token=lab.owner_token))
            await wait_for_tuple_contention("tournaments")

        status_code, _, body = await pending

        assert status_code == status.HTTP_409_CONFLICT
        assert set(issues_by_code(body)) == {"COMPETITOR_INACTIVE"}
        assert await layout(stage_item_id) == before

    assert await audit_rows() == []
    assert await generation_reservations() == 0
