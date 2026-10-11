"""Arnes del contrato HTTP de la generacion explicita del cuadro (S3.5a, F3B, LAB ONLY).

El laboratorio (tenants, sesiones, cuadros) y sus ayudas HTTP viven aqui para que el modulo de
tests se lea como el contrato que fija y no como su utileria.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final
from uuid import uuid4

import aiohttp
from heliclockter import datetime_utc
from starlette import status

from bracket.database import database
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
from bracket.utils.id_types import CompetitorId, StageItemId, TournamentId
from tests.integration_tests.api.shared import get_root_uvicorn_url
from tests.integration_tests.mocks import get_mock_token, get_mock_user
from tests.integration_tests.registration_fixtures import (
    delete_competitor,
    delete_registration_audit,
    delete_registrations,
    insert_competitor,
)
from tests.integration_tests.sql import (
    inserted_club,
    inserted_match,
    inserted_ranking,
    inserted_round,
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

LOCK_TOURNAMENT_SHARE = "SELECT id FROM tournaments WHERE id = :tournament_id FOR SHARE"
LOCK_TOURNAMENT_UPDATE = "SELECT id FROM tournaments WHERE id = :tournament_id FOR NO KEY UPDATE"

#: Clave HMAC sintetica del laboratorio: sin ella las escrituras del arnes no firman.
SYNTHETIC_SECRET: Final = "synthetic-laboratory-secret-not-for-deployment"


@dataclass(frozen=True)
class GenerationLab:
    """Tenant con dos torneos propios, uno ajeno, cuatro competidores y las sesiones del arnes."""

    tournament: TournamentId
    other_tournament: TournamentId
    foreign_tournament: TournamentId
    competitors: tuple[CompetitorId, ...]
    owner_token: str
    collaborator_token: str
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

        # Un generador asincrono no vale para tuple(): cuatro altas seguidas, una a una.
        competitors: list[CompetitorId] = []
        for index in range(4):
            competitors.append(
                await insert_competitor(f"competidora-s35a-{suffix}-{index}", club.id, created=now)
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
            tournament=tournaments[0].id,
            other_tournament=tournaments[1].id,
            foreign_tournament=foreign_tournament.id,
            competitors=tuple(competitors),
            owner_token=get_mock_token(owner.email),
            collaborator_token=get_mock_token(collaborator.email),
            inactive_token=get_mock_token(inactive.email),
        )


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
    registration_status: str = "CONFIRMED",
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
            "status": registration_status,
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
            payload = await response.json()
            assert isinstance(payload, dict), payload
            return payload


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
        query=("UPDATE tournaments SET status = CAST(:status AS tournament_status) WHERE id = :id"),
        values={"status": "ARCHIVED" if archived else "OPEN", "id": tournament_id},
    )


def pairs_of(layout_values: list[int | None]) -> list[tuple[int | None, int | None]]:
    """Parejas de la primera ronda: dos ranuras consecutivas son una eliminatoria."""
    return list(zip(layout_values[::2], layout_values[1::2], strict=True))
