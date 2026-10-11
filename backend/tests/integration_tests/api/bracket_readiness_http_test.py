# pylint: disable=redefined-outer-name
"""Contrato HTTP de la revision de readiness (S3.4d, F3B, LAB ONLY).

TDD: este modulo se escribio **antes** de la implementacion y fallo en rojo (la ruta no existia:
404 en lugar del contrato descrito en ``docs/26`` §42).

La revision es de **solo lectura**: no hay ``Idempotency-Key``, no hay escritura y no hay auditoria.
Lo que se fija aqui es el contrato observable: quien puede leer, que se publica, que bloqueos se
emiten con que datos, el orden determinista y la ausencia de efectos secundarios.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
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

from bracket.config import config
from bracket.database import database
from bracket.logic.bracket_readiness import BracketReadinessBlocker
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
from bracket.utils.id_types import ClubId, CompetitorId, TournamentId
from tests.integration_tests.api.shared import get_root_uvicorn_url
from tests.integration_tests.mocks import get_mock_token, get_mock_user
from tests.integration_tests.registration_fixtures import (
    delete_competitor,
    delete_registration_audit,
    delete_registrations,
    insert_competitor,
    set_competitor_active,
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

#: Campos que la respuesta publica. Cualquier campo de mas es un fallo: la proyeccion se cierra.
READINESS_FIELDS: Final = {
    "tournament_id",
    "ready",
    "registration_window",
    "summary",
    "categories",
    "blocking_issues",
}
#: Campos de una categoria. ``required_minimum`` se publica para no obligar al cliente a conocerlo.
CATEGORY_FIELDS: Final = {
    "category_key",
    "category_label",
    "confirmed_count",
    "required_minimum",
    "ready",
    "blocking_issues",
}
#: Campos de un bloqueo. Ni ``message`` libre ni identificadores internos.
ISSUE_FIELDS: Final = {"code", "count", "category_key", "required_minimum"}
#: Campos del recuento.
SUMMARY_FIELDS: Final = {"total", "confirmed", "draft", "withdrawn", "disqualified"}

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

#: Clave HMAC sintetica del laboratorio: las escrituras del arnes pasan por el protocolo.
SYNTHETIC_SECRET: Final = "synthetic-laboratory-secret-not-for-deployment"

#: Huella de las inscripciones del torneo: si la revision escribiera, cambiaria.
REGISTRATIONS_FINGERPRINT = """
    SELECT count(*) AS rows, coalesce(sum(id), 0) AS id_sum,
           coalesce(max(updated_at), 'epoch'::timestamptz) AS last_update
    FROM tournament_registrations WHERE tournament_id = :t
"""


@dataclass(frozen=True)
class ReadinessLab:  # pylint: disable=too-many-instance-attributes
    """Tenant con dos torneos propios, uno ajeno, cuatro competidores y las sesiones del arnes."""

    tenant: ClubId
    tournament: TournamentId
    other_tournament: TournamentId
    foreign_tournament: TournamentId
    competitors: tuple[CompetitorId, ...]
    foreign_competitor: CompetitorId
    owner_token: str
    collaborator_token: str
    outsider_token: str
    inactive_token: str


@asynccontextmanager
async def readiness_lab_context() -> AsyncIterator[ReadinessLab]:
    """Datos y sesiones del laboratorio; la limpieza se apila de hojas a raices."""
    now = datetime_utc.now()
    suffix = uuid4().hex[:12]

    async with AsyncExitStack() as stack:
        club = await stack.enter_async_context(
            inserted_club(ClubInsertable(name=f"f3b-s34d-{suffix}", created=now))
        )
        other_club = await stack.enter_async_context(
            inserted_club(ClubInsertable(name=f"f3b-s34d-other-{suffix}", created=now))
        )
        tournaments = [
            await stack.enter_async_context(
                inserted_tournament(
                    DUMMY_TOURNAMENT.model_copy(
                        update={
                            "club_id": club.id,
                            "dashboard_endpoint": f"f3b-s34d-{suffix}-{letter}",
                        }
                    )
                )
            )
            for letter in ("a", "b")
        ]
        foreign_tournament = await stack.enter_async_context(
            inserted_tournament(
                DUMMY_TOURNAMENT.model_copy(
                    update={"club_id": other_club.id, "dashboard_endpoint": f"f3b-s34d-{suffix}-c"}
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

        competitor_list = [
            await insert_competitor(f"competidora-s34d-{suffix}-{index}", club.id, created=now)
            for index in range(4)
        ]
        competitors = tuple(competitor_list)
        for competitor in competitors:
            stack.push_async_callback(delete_competitor, competitor)
        foreign_competitor = await insert_competitor(
            f"competidora-s34d-foreign-{suffix}", other_club.id, created=now
        )
        stack.push_async_callback(delete_competitor, foreign_competitor)

        # Lo ultimo apilado se deshace primero: inscripciones y auditoria antes que el resto.
        stack.push_async_callback(delete_audit_for_tenants, (club.id, other_club.id))
        for tournament in (*tournaments, foreign_tournament):
            stack.push_async_callback(delete_registration_audit, (tournament.id,), club.id)
            stack.push_async_callback(delete_registrations, tournament.id)

        yield ReadinessLab(
            tenant=club.id,
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
async def lab() -> AsyncIterator[ReadinessLab]:
    async with readiness_lab_context() as readiness_lab:
        yield readiness_lab


def readiness_url(tournament_id: TournamentId) -> str:
    return f"{get_root_uvicorn_url()}tournaments/{tournament_id}/bracket-readiness"


async def insert_registration(
    tournament_id: TournamentId,
    *,
    competitor_id: CompetitorId | None = None,
    status: str = "CONFIRMED",
    category_key: str | None = "gi-peso-83",
    category_label: str | None = "Gi -83 kg",
    representation: str = "INDEPENDENT",
    superseded_by: int | None = None,
) -> int:
    """Inserta una fila con la forma exacta que necesita el caso, sin pasar por el ciclo de vida.

    Es la via para los estados heredados o manipulados: el alta no permite ni una etiqueta que no
    cuadre con la clave ni un competidor de otro tenant, y aqui hay que poder representarlos.
    """
    registration_id = await database.fetch_val(
        query=INSERT_REGISTRATION,
        values={
            "tournament_id": tournament_id,
            "competitor_id": competitor_id,
            "identity_status": "VERIFIED" if competitor_id is not None else "AMBIGUOUS",
            "representation": representation,
            "sports_club_id": None,
            "affiliation_id": None,
            "category_key": category_key,
            "category_label": category_label,
            "competitor_name_snapshot": f"snapshot-{uuid4().hex[:8]}",
            "sports_club_name_snapshot": None,
            "status": status,
            "superseded_by": superseded_by,
        },
    )
    return int(registration_id)


async def set_registration_competitor(registration_id: int, competitor_id: CompetitorId) -> None:
    """Reapunta una inscripcion a otro competidor directamente en la base de datos."""
    await database.execute(
        query=(
            "UPDATE tournament_registrations SET competitor_id = :c, identity_status = 'VERIFIED' "
            "WHERE id = :id"
        ),
        values={"c": competitor_id, "id": registration_id},
    )


async def fingerprint(tournament_id: TournamentId) -> tuple[int, int, Any]:
    record = await database.fetch_one(query=REGISTRATIONS_FINGERPRINT, values={"t": tournament_id})
    assert record is not None
    mapping = record._mapping
    return (mapping["rows"], mapping["id_sum"], mapping["last_update"])


async def delete_audit_for_tenants(tenant_ids: tuple[int, ...]) -> None:
    """Retira TODA la auditoria del laboratorio, sea cual sea la entidad que la escribio.

    La limpieza estandar cubre inscripciones, torneo y competidores; las operaciones de ventana
    dejan filas de otras entidades que tambien referencian al club, y el club no se puede borrar
    mientras existan.
    """
    await database.execute(
        query="DELETE FROM domain_change_log WHERE tenant_club_id = ANY(:ids)",
        values={"ids": list(tenant_ids)},
    )


async def audit_count() -> int:
    return int(await database.fetch_val(query="SELECT count(*) FROM domain_change_log"))


async def get_readiness(
    tournament_id: TournamentId, *, token: str | None
) -> tuple[int, dict[str, Any]]:
    """Peticion cruda a la ruta. La sesion es de la peticion, no del caso."""
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(readiness_url(tournament_id), headers=headers) as response:
            return response.status, await response.json()


async def call_readiness(lab: ReadinessLab) -> dict[str, Any]:
    """Revision del torneo principal con la sesion OWNER, exigiendo 200 y forma publicada."""
    status_code, body = await get_readiness(lab.tournament, token=lab.owner_token)
    assert status_code == status.HTTP_200_OK, body
    assert set(body) == READINESS_FIELDS, body
    return body


async def set_deadline(lab: ReadinessLab, deadline: datetime_utc | None) -> None:
    async with (
        aiohttp.ClientSession() as session,
        session.post(
            f"{get_root_uvicorn_url()}tournaments/{lab.tournament}/registration-window",
            json={"registration_deadline": None if deadline is None else deadline.isoformat()},
            headers={
                "Authorization": f"Bearer {lab.owner_token}",
                IDEMPOTENCY_KEY_HEADER: uuid4().hex,
            },
        ) as response,
    ):
        assert response.status == status.HTTP_200_OK, await response.text()


async def close_registrations(lab: ReadinessLab) -> None:
    async with (
        aiohttp.ClientSession() as session,
        session.post(
            f"{get_root_uvicorn_url()}tournaments/{lab.tournament}/registrations/close",
            json={},
            headers={
                "Authorization": f"Bearer {lab.owner_token}",
                IDEMPOTENCY_KEY_HEADER: uuid4().hex,
            },
        ) as response,
    ):
        assert response.status == status.HTTP_200_OK, await response.text()


async def create_draft(lab: ReadinessLab, competitor_id: CompetitorId) -> int:
    async with (
        aiohttp.ClientSession() as session,
        session.post(
            f"{get_root_uvicorn_url()}tournaments/{lab.tournament}/registrations",
            json={
                "competitor_id": competitor_id,
                "representation": "INDEPENDENT",
                "category_key": "gi-peso-83",
                "category_label": "Gi -83 kg",
            },
            headers={
                "Authorization": f"Bearer {lab.owner_token}",
                IDEMPOTENCY_KEY_HEADER: uuid4().hex,
            },
        ) as response,
    ):
        assert response.status == status.HTTP_201_CREATED, await response.text()
        return int((await response.json())["id"])


async def confirm(lab: ReadinessLab, registration_id: int) -> None:
    async with (
        aiohttp.ClientSession() as session,
        session.post(
            f"{get_root_uvicorn_url()}tournaments/{lab.tournament}/registrations/"
            f"{registration_id}/confirm",
            json={},
            headers={
                "Authorization": f"Bearer {lab.owner_token}",
                IDEMPOTENCY_KEY_HEADER: uuid4().hex,
            },
        ) as response,
    ):
        assert response.status == status.HTTP_200_OK, await response.text()


def issues_by_code(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {issue["code"]: issue for issue in body["blocking_issues"]}


# --- autorizacion y ambito ---------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("token", [None, "no-es-un-token"])
async def test_only_a_valid_session_can_read(lab: ReadinessLab, token: str | None) -> None:
    status_code, body = await get_readiness(lab.tournament, token=token)
    assert status_code == status.HTTP_401_UNAUTHORIZED, body


@pytest.mark.asyncio(loop_scope="session")
async def test_an_inactive_session_is_indistinguishable_from_an_unknown_one(
    lab: ReadinessLab,
) -> None:
    """Un usuario dado de baja y un token invalido no se distinguen hacia fuera."""
    inactive_status, inactive_body = await get_readiness(lab.tournament, token=lab.inactive_token)
    unknown_status, unknown_body = await get_readiness(lab.tournament, token="invalido")

    assert inactive_status == unknown_status == status.HTTP_401_UNAUTHORIZED
    assert inactive_body == unknown_body


@pytest.mark.asyncio(loop_scope="session")
async def test_a_tournament_of_another_tenant_is_not_found(lab: ReadinessLab) -> None:
    """Misma semantica de 404 para otro tenant que para un torneo que no existe."""
    foreign_status, foreign_body = await get_readiness(
        lab.foreign_tournament, token=lab.owner_token
    )
    missing_status, missing_body = await get_readiness(TournamentId(10**9), token=lab.owner_token)

    assert foreign_status == missing_status == status.HTTP_404_NOT_FOUND
    assert foreign_body == missing_body


@pytest.mark.asyncio(loop_scope="session")
async def test_a_user_of_another_tenant_cannot_read(lab: ReadinessLab) -> None:
    status_code, _ = await get_readiness(lab.tournament, token=lab.outsider_token)
    assert status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("role", ["owner", "collaborator"])
async def test_owner_and_collaborator_can_read(lab: ReadinessLab, role: str) -> None:
    token = lab.owner_token if role == "owner" else lab.collaborator_token
    status_code, body = await get_readiness(lab.tournament, token=token)

    assert status_code == status.HTTP_200_OK, body
    assert body["tournament_id"] == lab.tournament


@pytest.mark.asyncio(loop_scope="session")
async def test_the_review_is_not_scoped_to_the_requested_tournament(lab: ReadinessLab) -> None:
    """El mismo tenant con otro torneo no ve las inscripciones del primero."""
    await insert_registration(lab.tournament, competitor_id=lab.competitors[0])

    status_code, body = await get_readiness(lab.other_tournament, token=lab.owner_token)

    assert status_code == status.HTTP_200_OK, body
    assert body["summary"]["total"] == 0
    assert body["tournament_id"] == lab.other_tournament


# --- ventana -----------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_an_open_window_blocks_the_tournament(lab: ReadinessLab) -> None:
    """Sin plazo y sin cierre manual la ventana admite altas: no hay nada que revisar."""
    body = await call_readiness(lab)

    assert body["ready"] is False
    assert body["registration_window"] == "OPEN"
    assert body["summary"] == {
        "total": 0,
        "confirmed": 0,
        "draft": 0,
        "withdrawn": 0,
        "disqualified": 0,
    }
    assert body["categories"] == []
    assert issues_by_code(body)["REGISTRATION_WINDOW_OPEN"] == {
        "code": "REGISTRATION_WINDOW_OPEN",
        "count": None,
        "category_key": None,
        "required_minimum": None,
    }


@pytest.mark.asyncio(loop_scope="session")
async def test_a_window_past_its_deadline_allows_the_review(lab: ReadinessLab) -> None:
    await set_deadline(lab, datetime_utc.now() - timedelta(minutes=1))
    for competitor in lab.competitors[:2]:
        await insert_registration(lab.tournament, competitor_id=competitor)

    body = await call_readiness(lab)

    assert body["registration_window"] == "DEADLINE_PASSED"
    assert body["ready"] is True
    assert body["blocking_issues"] == []


@pytest.mark.asyncio(loop_scope="session")
async def test_a_manual_close_allows_the_review(lab: ReadinessLab) -> None:
    await close_registrations(lab)
    await insert_registration(lab.tournament, competitor_id=lab.competitors[0])
    await insert_registration(lab.tournament, competitor_id=lab.competitors[1])

    body = await call_readiness(lab)

    assert body["registration_window"] == "CLOSED"
    assert body["ready"] is True


# --- recuento y ciclo de vida ------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_pending_drafts_block_with_their_count(lab: ReadinessLab) -> None:
    await close_registrations(lab)
    for competitor in lab.competitors[:2]:
        await insert_registration(lab.tournament, competitor_id=competitor)
    await insert_registration(lab.tournament, competitor_id=lab.competitors[2], status="DRAFT")

    body = await call_readiness(lab)

    assert body["ready"] is False
    assert body["summary"]["draft"] == 1
    pending = issues_by_code(body)["REGISTRATIONS_PENDING"]
    assert pending["count"] == 1
    assert pending["category_key"] is None


@pytest.mark.asyncio(loop_scope="session")
async def test_the_real_lifecycle_is_respected(lab: ReadinessLab) -> None:
    """Alta y confirmacion por la API de verdad: dos confirmadas en la misma categoria.

    Las altas se crean con la ventana abierta -- cerrarla bloquea las altas nuevas, no las
    confirmaciones de lo que ya estaba dentro -- y el cierre llega despues, como en la operacion.
    """
    for competitor in lab.competitors[:2]:
        registration_id = await create_draft(lab, competitor)
        await confirm(lab, registration_id)
    await close_registrations(lab)

    body = await call_readiness(lab)

    assert body["ready"] is True
    assert body["summary"] == {
        "total": 2,
        "confirmed": 2,
        "draft": 0,
        "withdrawn": 0,
        "disqualified": 0,
    }
    assert [category["confirmed_count"] for category in body["categories"]] == [2]


@pytest.mark.asyncio(loop_scope="session")
async def test_withdrawn_and_disqualified_do_not_participate(lab: ReadinessLab) -> None:
    await close_registrations(lab)
    await insert_registration(lab.tournament, competitor_id=lab.competitors[0])
    await insert_registration(lab.tournament, competitor_id=lab.competitors[1], status="WITHDRAWN")
    await insert_registration(
        lab.tournament, competitor_id=lab.competitors[2], status="DISQUALIFIED"
    )

    body = await call_readiness(lab)

    assert body["summary"] == {
        "total": 3,
        "confirmed": 1,
        "draft": 0,
        "withdrawn": 1,
        "disqualified": 1,
    }
    assert body["ready"] is False
    assert issues_by_code(body)["CATEGORY_NOT_ENOUGH_COMPETITORS"]["count"] == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_a_corrected_registration_is_not_counted_twice(lab: ReadinessLab) -> None:
    """Corregida o sustituida: fuera del recuento, ni como participante ni en el total vigente."""
    await close_registrations(lab)
    original = await insert_registration(lab.tournament, competitor_id=lab.competitors[0])
    await insert_registration(lab.tournament, competitor_id=lab.competitors[1])
    corrected = await insert_registration(
        lab.tournament, competitor_id=lab.competitors[2], status="CORRECTED"
    )
    superseded = await insert_registration(
        lab.tournament, competitor_id=lab.competitors[3], superseded_by=original
    )

    body = await call_readiness(lab)

    assert corrected != superseded
    assert body["summary"]["total"] == 2
    assert body["summary"]["confirmed"] == 2
    assert body["ready"] is True


# --- categorias --------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_categories_come_out_in_a_deterministic_order(lab: ReadinessLab) -> None:
    """``category_key`` nulas primero y despues por clave; el orden no depende del SQL."""
    await close_registrations(lab)
    await insert_registration(lab.tournament, competitor_id=lab.competitors[0])
    await insert_registration(lab.tournament, competitor_id=lab.competitors[1])
    for competitor, (key, label) in zip(
        lab.competitors[2:],
        [("nogi-absoluto", "No-Gi Absoluto"), ("gi-absoluto", "Gi Absoluto")],
        strict=True,
    ):
        await insert_registration(
            lab.tournament, competitor_id=competitor, category_key=key, category_label=label
        )

    body = await call_readiness(lab)

    assert [category["category_key"] for category in body["categories"]] == [
        "gi-absoluto",
        "gi-peso-83",
        "nogi-absoluto",
    ]
    assert set(body["categories"][0]) == CATEGORY_FIELDS
    assert await call_readiness(lab) == body


@pytest.mark.asyncio(loop_scope="session")
async def test_an_uncategorized_registration_is_its_own_group(lab: ReadinessLab) -> None:
    """Sin categoria no es un dato invalido (el modelo lo admite), pero no forma cuadro."""
    await close_registrations(lab)
    await insert_registration(
        lab.tournament,
        competitor_id=lab.competitors[0],
        category_key=None,
        category_label=None,
    )

    body = await call_readiness(lab)

    [category] = body["categories"]
    assert category["category_key"] is None
    assert category["category_label"] is None
    assert category["confirmed_count"] == 1
    assert category["ready"] is False
    assert [issue["code"] for issue in category["blocking_issues"]] == [
        "CATEGORY_NOT_ENOUGH_COMPETITORS"
    ]
    assert issues_by_code(body)["CATEGORY_NOT_ENOUGH_COMPETITORS"]["required_minimum"] == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_a_category_below_the_minimum_publishes_count_and_minimum(lab: ReadinessLab) -> None:
    await close_registrations(lab)
    await insert_registration(lab.tournament, competitor_id=lab.competitors[0])

    body = await call_readiness(lab)

    issue = issues_by_code(body)["CATEGORY_NOT_ENOUGH_COMPETITORS"]
    assert issue == {
        "code": "CATEGORY_NOT_ENOUGH_COMPETITORS",
        "count": 1,
        "category_key": "gi-peso-83",
        "required_minimum": 2,
    }
    assert body["ready"] is False
    assert body["categories"][0]["ready"] is False


# --- coherencia de participantes ---------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_an_inactive_competitor_blocks_its_category(lab: ReadinessLab) -> None:
    """La baja logica del competidor es posterior a la confirmacion y se tiene que ver."""
    await close_registrations(lab)
    await insert_registration(lab.tournament, competitor_id=lab.competitors[0])
    await insert_registration(lab.tournament, competitor_id=lab.competitors[1])
    await set_competitor_active(lab.competitors[1], active=False)

    body = await call_readiness(lab)

    issue = issues_by_code(body)["COMPETITOR_INACTIVE"]
    assert issue["count"] == 1
    assert issue["category_key"] == "gi-peso-83"
    assert body["categories"][0]["confirmed_count"] == 2
    assert body["categories"][0]["ready"] is False
    assert body["ready"] is False


@pytest.mark.asyncio(loop_scope="session")
async def test_a_competitor_from_another_tenant_is_inconsistent(lab: ReadinessLab) -> None:
    """Un competidor ajeno no es ubicable: se reporta como incoherencia, no como baja."""
    await close_registrations(lab)
    registration_id = await insert_registration(lab.tournament, competitor_id=lab.competitors[0])
    await insert_registration(lab.tournament, competitor_id=lab.competitors[1])
    await set_registration_competitor(registration_id, lab.foreign_competitor)

    body = await call_readiness(lab)

    assert issues_by_code(body)["REGISTRATION_INCONSISTENT"]["count"] == 1
    assert [issue["code"] for issue in body["categories"][0]["blocking_issues"]] == [
        "REGISTRATION_INCONSISTENT"
    ]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_category_key_that_means_two_things_is_invalid(lab: ReadinessLab) -> None:
    """La etiqueta se congela por clave y torneo: dos etiquetas para una clave no es valido."""
    await close_registrations(lab)
    await insert_registration(lab.tournament, competitor_id=lab.competitors[0])
    await insert_registration(lab.tournament, competitor_id=lab.competitors[1])
    await insert_registration(
        lab.tournament,
        competitor_id=lab.competitors[2],
        category_label="Gi -83 kilos",
        status="CORRECTED",
    )

    body = await call_readiness(lab)

    issue = issues_by_code(body)["INVALID_CATEGORY"]
    assert issue["count"] == 1
    assert issue["category_key"] == "gi-peso-83"
    # La etiqueta publicada es la de las inscripciones vigentes; el conflicto vive en el bloqueo.
    assert body["categories"][0]["category_label"] == "Gi -83 kg"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_category_key_without_a_label_is_invalid(lab: ReadinessLab) -> None:
    await close_registrations(lab)
    await insert_registration(lab.tournament, competitor_id=lab.competitors[0], category_label=None)
    await insert_registration(lab.tournament, competitor_id=lab.competitors[1])

    body = await call_readiness(lab)

    assert issues_by_code(body)["INVALID_CATEGORY"]["count"] == 1


# --- estructura competitiva existente ----------------------------------------------------------


@asynccontextmanager
async def generated_bracket(
    lab: ReadinessLab, *, scores: tuple[int, int]
) -> AsyncIterator[dict[str, int]]:
    """Un ``stage_item`` de eliminacion directa con dos entrantes y una eliminatoria con marcador.

    Se monta con los datos minimos que lee la generacion de F2C: ``stage``, ``stage_item``, dos
    entrantes con equipo y una eliminatoria. El marcador es lo que hace que el cuadro ya no se pueda
    regenerar; con ``0-0`` no hay nada encima.
    """
    now = datetime_utc.now()
    async with AsyncExitStack() as stack:
        ranking = await stack.enter_async_context(
            inserted_ranking(DUMMY_RANKING1.model_copy(update={"tournament_id": lab.tournament}))
        )
        stage = await stack.enter_async_context(
            inserted_stage(
                DUMMY_STAGE2.model_copy(update={"tournament_id": lab.tournament, "name": "S3.4d"})
            )
        )
        stage_item = await stack.enter_async_context(
            inserted_stage_item(
                StageItemInsertable(
                    stage_id=stage.id,
                    name="Cuadro S3.4d",
                    created=now,
                    type=StageType.SINGLE_ELIMINATION,
                    team_count=4,
                    ranking_id=ranking.id,
                )
            )
        )
        inputs = []
        for index, dummy_team in enumerate((DUMMY_TEAM1, DUMMY_TEAM2)):
            team = await stack.enter_async_context(
                inserted_team(dummy_team.model_copy(update={"tournament_id": lab.tournament}))
            )
            inputs.append(
                await stack.enter_async_context(
                    inserted_stage_item_input(
                        StageItemInputInsertable(
                            slot=index,
                            team_id=team.id,
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
                    stage_item_input1_score=scores[0],
                    stage_item_input2_score=scores[1],
                    stage_item_input1_conflict=False,
                    stage_item_input2_conflict=False,
                )
            )
        )
        yield {"stage_item_id": int(stage_item.id)}


@pytest.mark.asyncio(loop_scope="session")
async def test_an_existing_bracket_with_activity_blocks_generation(lab: ReadinessLab) -> None:
    """Cuadro ya generado y con marcador encima: el reparto no se puede rehacer."""
    await close_registrations(lab)
    await insert_registration(lab.tournament, competitor_id=lab.competitors[0])
    await insert_registration(lab.tournament, competitor_id=lab.competitors[1])

    async with generated_bracket(lab, scores=(1, 0)):
        body = await call_readiness(lab)
        assert body["ready"] is False
        assert issues_by_code(body)["BRACKET_ALREADY_EXISTS"]["count"] == 1

    # Sin la estructura, la misma revision vuelve a estar lista: nada se quedo pegado.
    assert (await call_readiness(lab))["ready"] is True


@pytest.mark.asyncio(loop_scope="session")
async def test_an_existing_regenerable_bracket_does_not_block(lab: ReadinessLab) -> None:
    """Sin nada encima la generacion de F2C puede rehacer el cuadro: no bloquea."""
    await close_registrations(lab)
    await insert_registration(lab.tournament, competitor_id=lab.competitors[0])
    await insert_registration(lab.tournament, competitor_id=lab.competitors[1])

    async with generated_bracket(lab, scores=(0, 0)):
        body = await call_readiness(lab)

    assert body["ready"] is True
    assert body["blocking_issues"] == []


# --- solo lectura y vocabulario ----------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_the_review_writes_nothing_and_audits_nothing(lab: ReadinessLab) -> None:
    """Una revision no deja rastro: ni filas tocadas ni un solo evento de auditoria."""
    await close_registrations(lab)
    registration_id = await insert_registration(lab.tournament, competitor_id=lab.competitors[0])
    await insert_registration(lab.tournament, competitor_id=lab.competitors[1], status="WITHDRAWN")
    before_registrations = await fingerprint(lab.tournament)
    before_audit = await audit_count()

    first = await call_readiness(lab)
    second = await call_readiness(lab)

    assert first == second
    assert await fingerprint(lab.tournament) == before_registrations
    assert await audit_count() == before_audit
    assert registration_id  # la fila sigue ahi, intacta


@pytest.mark.asyncio(loop_scope="session")
async def test_the_blocker_vocabulary_is_closed(lab: ReadinessLab) -> None:
    """Todo codigo emitido es del vocabulario cerrado, y ningun bloqueo trae datos de mas."""
    await close_registrations(lab)
    await insert_registration(lab.tournament, competitor_id=lab.competitors[0])
    await insert_registration(lab.tournament, competitor_id=lab.competitors[1], status="DRAFT")

    body = await call_readiness(lab)

    known = {blocker.value for blocker in BracketReadinessBlocker}
    assert body["blocking_issues"]
    for issue in body["blocking_issues"]:
        assert set(issue) == ISSUE_FIELDS
        assert issue["code"] in known
    assert body["ready"] is (not body["blocking_issues"])
    for category in body["categories"]:
        assert set(category) == CATEGORY_FIELDS
        assert category["ready"] is (not category["blocking_issues"])


@pytest.fixture(scope="module", autouse=True)
def idempotency_secret() -> Iterator[None]:
    """Clave HMAC sintetica del laboratorio: sin ella las escrituras del arnes no firman."""
    patcher = pytest.MonkeyPatch()
    patcher.setattr(config, "idempotency_hmac_key", SYNTHETIC_SECRET)
    patcher.setattr(config, "idempotency_hmac_key_version", "v1")
    yield
    patcher.undo()
