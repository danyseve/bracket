# pylint: disable=redefined-outer-name
"""Contrato HTTP de la afiliacion del competidor (S4.8a-3 §3, LAB ONLY).

TDD: este modulo se escribe **antes** de la implementacion y falla en rojo (rutas, codigo cerrado y
reglas no existen). Fija el contrato:

* **Asignar** una afiliacion vigente con una academia del tenant; la primera es primaria;
* **Cambiar la primaria**: promover una afiliacion cierra la primaria vigente anterior, en la misma
  transaccion; nunca hay **dos** primarias abiertas;
* **Cerrar** es poner ``valid_to``: el pasado no se reescribe (ni una afiliacion ya cerrada se vuelve
  a cerrar con otro instante);
* **Pasar a independiente**: cierra todas las afiliaciones vigentes del competidor;
* las **inscripciones historicas** (``affiliation_id`` y ``sports_club_name_snapshot``) quedan
  congeladas: ninguna de las cuatro operaciones las toca;
* se valida la academia: inexistente, inactiva o de otro tenant -> rechazo de datos;
* **auditoria**: cada escritura confirmada deja un evento del competidor con el codigo cerrado
  ``AFFILIATION_CHANGE``; sin cambios reales no hay evento;
* **autorizacion**: OWNER-only (sensible); COLLABORATOR 403 y anonimo 401;
* **puerta e idempotencia**: puerta de S3.6b (503 antes de reservar) y protocolo de S3.3c-4.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final
from uuid import uuid4

import pytest
import pytest_asyncio
from heliclockter import datetime_utc
from starlette import status

from bracket.config import config
from bracket.database import database
from bracket.routes.f3_write_gate import F3_WRITE_DISABLED
from bracket.utils.id_types import CompetitorId, SportsClubId
from tests.integration_tests.api.competitor_http_support import (
    CompetitorLab,
    call,
    competitor_lab_context,
)
from tests.integration_tests.api.generate_bracket_http_support import SYNTHETIC_SECRET
from tests.integration_tests.api.shared import get_root_uvicorn_url
from tests.integration_tests.registration_fixtures import (
    competitor_audit_rows,
    delete_sports_club,
    insert_sports_club,
)

#: Proyeccion publica de una afiliacion: un campo de mas cierra el contrato.
AFFILIATION_FIELDS: Final = {
    "id",
    "competitor_id",
    "sports_club_id",
    "is_primary",
    "valid_from",
    "valid_to",
}
#: Detalle fijo del rechazo por datos invalidos (no repite el valor recibido).
INVALID_DETAIL: Final = "Competitor request is invalid"
#: Detalle del 403 cuando la relacion con el tenant no basta.
ROLE_DETAIL: Final = "ROLE_NOT_ALLOWED"
#: Codigo cerrado de auditoria de las cuatro operaciones.
REASON_CODE: Final = "AFFILIATION_CHANGE"

_SELECT_AFFILIATION = """
    SELECT id, competitor_id, sports_club_id, is_primary, valid_from, valid_to
    FROM competitors_x_sports_clubs WHERE id = :affiliation_id
"""
_COUNT_OPEN_PRIMARY = """
    SELECT count(*) FROM competitors_x_sports_clubs
    WHERE competitor_id = :competitor_id AND valid_to IS NULL AND is_primary
"""
_COUNT_OPEN = """
    SELECT count(*) FROM competitors_x_sports_clubs
    WHERE competitor_id = :competitor_id AND valid_to IS NULL
"""
_INSERT_FROZEN_REGISTRATION = """
    INSERT INTO tournament_registrations (
        tournament_id, competitor_id, identity_status, representation,
        sports_club_id, competitor_name_snapshot, sports_club_name_snapshot, affiliation_id,
        status, revision, created
    ) VALUES (
        :tournament_id, :competitor_id,
        CAST('VERIFIED' AS registration_identity_status),
        CAST('CLUB' AS registration_representation),
        :sports_club_id, :name_snapshot, :club_snapshot, :affiliation_id,
        CAST('CONFIRMED' AS registration_status), 1, NOW()
    )
    RETURNING id
"""
_SELECT_FROZEN_REGISTRATION = """
    SELECT affiliation_id, sports_club_name_snapshot
    FROM tournament_registrations WHERE id = :registration_id
"""
_RESERVATION_COUNT = (
    "SELECT count(*) FROM domain_idempotency_keys WHERE request_path = ANY(:paths)"
)


@pytest.fixture(scope="module", autouse=True)
def idempotency_secret() -> Iterator[None]:
    """Clave HMAC sintetica del laboratorio: sin ella el protocolo falla cerrado."""
    patcher = pytest.MonkeyPatch()
    patcher.setattr(config, "idempotency_hmac_key", SYNTHETIC_SECRET)
    patcher.setattr(config, "idempotency_hmac_key_version", "v1")
    yield
    patcher.undo()


@pytest.fixture()
def flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Escritura habilitada de forma explicita: el contrato no depende del entorno de CI."""
    monkeypatch.setattr(config, "f3_write_enabled", True)


@dataclass
class AffiliationLab:
    """Laboratorio del competidor con las academias del tenant y de fuera."""

    lab: CompetitorLab
    club_a: SportsClubId
    club_inactive: SportsClubId
    club_foreign: SportsClubId


@asynccontextmanager
async def affiliation_lab() -> AsyncIterator[AffiliationLab]:
    """Tenant del laboratorio de competidores mas las academias propias y la ajena."""
    async with competitor_lab_context() as lab:
        async with AsyncExitStack() as stack:
            now = datetime_utc.now()
            suffix = uuid4().hex[:8]
            club_a = await insert_sports_club(f"acad-a-{suffix}", lab.club, active=True, created=now)
            club_inactive = await insert_sports_club(
                f"acad-old-{suffix}", lab.club, active=False, created=now
            )
            club_foreign = await insert_sports_club(
                f"acad-b-{suffix}", lab.other_club, active=True, created=now
            )
            for sports_club_id in (club_a, club_inactive, club_foreign):
                stack.push_async_callback(delete_sports_club, sports_club_id)

            async def delete_lab_rows() -> None:
                """Retira auditoria, inscripciones y afiliaciones del laboratorio, por tenant."""
                tenants = [lab.club, lab.other_club]
                await database.execute(
                    query="DELETE FROM domain_change_log WHERE entity = 'competitor' AND "
                    "entity_id IN (SELECT id FROM competitors WHERE managed_by_club_id = ANY(:ids))",
                    values={"ids": tenants},
                )
                await database.execute(
                    query="DELETE FROM tournament_registrations WHERE tournament_id = :t",
                    values={"t": lab.tournament},
                )
                await database.execute(
                    query="DELETE FROM competitors_x_sports_clubs WHERE sports_club_id IN "
                    "(SELECT id FROM sports_clubs WHERE tenant_club_id = ANY(:ids))",
                    values={"ids": tenants},
                )

            # Se apila al final: se ejecuta primero, antes de retirar academias y competidores.
            stack.push_async_callback(delete_lab_rows)
            yield AffiliationLab(
                lab=lab, club_a=club_a, club_inactive=club_inactive, club_foreign=club_foreign
            )


def affiliations_url(lab: CompetitorLab, competitor_id: int) -> str:
    return f"{get_root_uvicorn_url()}clubs/{lab.club}/competitors/{competitor_id}/affiliations"


def affiliation_action_url(
    lab: CompetitorLab, competitor_id: int, affiliation_id: int, action: str
) -> str:
    return f"{affiliations_url(lab, competitor_id)}/{affiliation_id}/{action}"


def independence_url(lab: CompetitorLab, competitor_id: int) -> str:
    return f"{get_root_uvicorn_url()}clubs/{lab.club}/competitors/{competitor_id}/independence"


async def assign(
    lab: CompetitorLab,
    competitor_id: int,
    sports_club_id: int,
    *,
    token: str | None,
    key: str,
) -> tuple[int, Any]:
    """Asignacion por HTTP; el cuerpo solo lleva la academia destino."""
    return await call(
        "POST",
        affiliations_url(lab, competitor_id),
        token=token,
        key=key,
        payload={"sports_club_id": sports_club_id},
    )


async def affiliation_row(affiliation_id: int) -> dict[str, Any]:
    record = await database.fetch_one(
        query=_SELECT_AFFILIATION, values={"affiliation_id": affiliation_id}
    )
    assert record is not None
    return dict(record._mapping)


async def open_primary_count(competitor_id: int) -> int:
    return int(
        await database.fetch_val(
            query=_COUNT_OPEN_PRIMARY, values={"competitor_id": competitor_id}
        )
    )


async def open_count(competitor_id: int) -> int:
    return int(
        await database.fetch_val(query=_COUNT_OPEN, values={"competitor_id": competitor_id})
    )


def first_competitor(lab: CompetitorLab) -> CompetitorId:
    """Identidad propia activa del laboratorio."""
    return lab.active_competitors[0]


# --- 1. asignar --------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_owner_assigns_first_affiliation_as_primary(flag_on: None) -> None:
    """La primera afiliacion vigente es la primaria y deja auditoria con el codigo nuevo."""
    async with affiliation_lab() as fixture:
        lab, competitor_id = fixture.lab, first_competitor(fixture.lab)
        status_code, body = await assign(
            lab, competitor_id, fixture.club_a, token=lab.owner_token, key=f"k-{uuid4().hex}"
        )

        assert status_code == status.HTTP_201_CREATED, body
        assert set(body) == AFFILIATION_FIELDS
        assert body["competitor_id"] == competitor_id
        assert body["sports_club_id"] == fixture.club_a
        assert body["is_primary"] is True and body["valid_to"] is None

        row = await affiliation_row(body["id"])
        assert row["is_primary"] is True and row["valid_to"] is None
        assert await open_primary_count(competitor_id) == 1

        audit = await competitor_audit_rows(competitor_id)
        assert audit[-1]["action"] == "UPDATE"
        assert audit[-1]["reason_code"] == REASON_CODE


@pytest.mark.asyncio(loop_scope="session")
async def test_second_affiliation_is_not_primary(flag_on: None) -> None:
    """Con una primaria vigente, la siguiente afiliacion entra como no primaria."""
    async with affiliation_lab() as fixture:
        lab, competitor_id = fixture.lab, first_competitor(fixture.lab)
        await assign(lab, competitor_id, fixture.club_a, token=lab.owner_token, key="k-1")
        status_code, body = await assign(
            lab, competitor_id, fixture.club_a, token=lab.owner_token, key="k-2"
        )

        assert status_code == status.HTTP_201_CREATED, body
        assert body["is_primary"] is False
        assert await open_primary_count(competitor_id) == 1


# --- 2. cambiar la primaria --------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_promoting_an_affiliation_closes_the_previous_primary(flag_on: None) -> None:
    """Promover cierra la primaria anterior y deja una sola primaria abierta."""
    async with affiliation_lab() as fixture:
        lab, competitor_id = fixture.lab, first_competitor(fixture.lab)
        _, first = await assign(lab, competitor_id, fixture.club_a, token=lab.owner_token, key="k-1")
        _, second = await assign(
            lab, competitor_id, fixture.club_a, token=lab.owner_token, key="k-2"
        )
        assert second["is_primary"] is False

        status_code, promoted = await call(
            "POST",
            affiliation_action_url(lab, competitor_id, second["id"], "primary"),
            token=lab.owner_token,
            key="k-3",
        )

        assert status_code == status.HTTP_200_OK, promoted
        assert promoted["id"] == second["id"] and promoted["is_primary"] is True
        closed = await affiliation_row(first["id"])
        assert closed["valid_to"] is not None and closed["is_primary"] is True
        assert await open_primary_count(competitor_id) == 1


# --- 3. cerrar ---------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_closing_sets_valid_to_and_never_rewrites_it(flag_on: None) -> None:
    """Cerrar pone ``valid_to``; repetir la operacion no cambia el instante ya escrito."""
    async with affiliation_lab() as fixture:
        lab, competitor_id = fixture.lab, first_competitor(fixture.lab)
        _, created = await assign(
            lab, competitor_id, fixture.club_a, token=lab.owner_token, key="k-1"
        )
        status_code, closed = await call(
            "POST",
            affiliation_action_url(lab, competitor_id, created["id"], "close"),
            token=lab.owner_token,
            key="k-2",
        )
        assert status_code == status.HTTP_200_OK, closed
        assert closed["valid_to"] is not None
        first_instant = closed["valid_to"]

        _, closed_again = await call(
            "POST",
            affiliation_action_url(lab, competitor_id, created["id"], "close"),
            token=lab.owner_token,
            key="k-3",
        )

        assert closed_again["valid_to"] == first_instant
        assert await open_count(competitor_id) == 0


# --- 4. pasar a independiente ------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_independence_closes_every_open_affiliation(flag_on: None) -> None:
    """Ir a independiente cierra todas las afiliaciones vigentes del competidor."""
    async with affiliation_lab() as fixture:
        lab, competitor_id = fixture.lab, first_competitor(fixture.lab)
        await assign(lab, competitor_id, fixture.club_a, token=lab.owner_token, key="k-1")
        await assign(lab, competitor_id, fixture.club_a, token=lab.owner_token, key="k-2")
        assert await open_count(competitor_id) == 2

        status_code, body = await call(
            "POST",
            independence_url(lab, competitor_id),
            token=lab.owner_token,
            key="k-3",
        )

        assert status_code == status.HTTP_200_OK, body
        assert body["competitor_id"] == competitor_id
        assert all(item["valid_to"] is not None for item in body["affiliations"])
        assert await open_count(competitor_id) == 0


# --- 5. las inscripciones historicas quedan congeladas -----------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_historical_registration_is_frozen_by_affiliation_changes(flag_on: None) -> None:
    """Cerrar o ir a independiente no toca ``affiliation_id`` ni el nombre de academia congelado."""
    async with affiliation_lab() as fixture:
        lab, competitor_id = fixture.lab, first_competitor(fixture.lab)
        _, created = await assign(
            lab, competitor_id, fixture.club_a, token=lab.owner_token, key="k-1"
        )
        registration_id = await database.fetch_val(
            query=_INSERT_FROZEN_REGISTRATION,
            values={
                "tournament_id": lab.tournament,
                "competitor_id": competitor_id,
                "sports_club_id": fixture.club_a,
                "name_snapshot": "Nombre Congelado",
                "club_snapshot": "Academia Congelada",
                "affiliation_id": created["id"],
            },
        )
        await call(
            "POST",
            affiliation_action_url(lab, competitor_id, created["id"], "close"),
            token=lab.owner_token,
            key="k-2",
        )
        await call(
            "POST", independence_url(lab, competitor_id), token=lab.owner_token, key="k-3"
        )

        frozen = await database.fetch_one(
            query=_SELECT_FROZEN_REGISTRATION, values={"registration_id": registration_id}
        )
        assert frozen is not None
        assert frozen["affiliation_id"] == created["id"]
        assert frozen["sports_club_name_snapshot"] == "Academia Congelada"


# --- 6. validacion de la academia --------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_unknown_inactive_or_foreign_club_is_rejected(flag_on: None) -> None:
    """Academia inexistente, inactiva o de otro tenant: rechazo de datos, sin afiliacion."""
    async with affiliation_lab() as fixture:
        lab, competitor_id = fixture.lab, first_competitor(fixture.lab)
        for sports_club_id in (
            2_000_000_000,
            fixture.club_inactive,
            fixture.club_foreign,
        ):
            status_code, body = await assign(
                lab, competitor_id, sports_club_id, token=lab.owner_token, key=f"k-{uuid4().hex}"
            )
            assert status_code == status.HTTP_400_BAD_REQUEST, (sports_club_id, body)
            assert body == {"detail": INVALID_DETAIL}
        assert await open_count(competitor_id) == 0


# --- 7. autorizacion OWNER-only ----------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_affiliation_is_owner_only(flag_on: None) -> None:
    """COLLABORATOR recibe 403 ROLE_NOT_ALLOWED y el anonimo 401; nada se escribe."""
    async with affiliation_lab() as fixture:
        lab, competitor_id = fixture.lab, first_competitor(fixture.lab)

        collaborator_status, collaborator_body = await assign(
            lab, competitor_id, fixture.club_a, token=lab.collaborator_token, key="k-collab"
        )
        anonymous_status, _ = await assign(
            lab, competitor_id, fixture.club_a, token=None, key="k-anon"
        )

        assert collaborator_status == status.HTTP_403_FORBIDDEN
        assert collaborator_body == {"detail": ROLE_DETAIL}
        assert anonymous_status == status.HTTP_401_UNAUTHORIZED
        assert await open_count(competitor_id) == 0


# --- 8. idempotencia ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_same_key_and_body_is_a_replay(flag_on: None) -> None:
    """Misma clave y mismo cuerpo: se repite la misma respuesta sin abrir otra afiliacion."""
    async with affiliation_lab() as fixture:
        lab, competitor_id = fixture.lab, first_competitor(fixture.lab)
        key = f"k-{uuid4().hex}"
        first_status, first = await assign(
            lab, competitor_id, fixture.club_a, token=lab.owner_token, key=key
        )
        second_status, second = await assign(
            lab, competitor_id, fixture.club_a, token=lab.owner_token, key=key
        )

        assert first_status == status.HTTP_201_CREATED
        assert (second_status, second) == (first_status, first)
        assert await open_count(competitor_id) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_same_key_with_another_body_conflicts(flag_on: None) -> None:
    """Misma clave y otro cuerpo: 409, sin abrir una segunda afiliacion."""
    async with affiliation_lab() as fixture:
        lab, competitor_id = fixture.lab, first_competitor(fixture.lab)
        key = f"k-{uuid4().hex}"
        await assign(lab, competitor_id, fixture.club_a, token=lab.owner_token, key=key)
        conflict_status, _ = await assign(
            lab, competitor_id, fixture.club_foreign, token=lab.owner_token, key=key
        )

        assert conflict_status == status.HTTP_409_CONFLICT
        assert await open_count(competitor_id) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_write_gate_disabled_is_503_before_reserving(flag_on: None) -> None:
    """Con la puerta apagada la afiliacion responde 503 sin reservar la clave."""
    async with affiliation_lab() as fixture:
        lab, competitor_id = fixture.lab, first_competitor(fixture.lab)
        config.f3_write_enabled = False
        try:
            path = f"/clubs/{lab.club}/competitors/{competitor_id}/affiliations"
            status_code, body = await assign(
                lab, competitor_id, fixture.club_a, token=lab.owner_token, key=f"k-{uuid4().hex}"
            )
        finally:
            config.f3_write_enabled = True

        assert status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert body == {"detail": F3_WRITE_DISABLED}
        reserved = int(
            await database.fetch_val(query=_RESERVATION_COUNT, values={"paths": [path]})
        )
        assert reserved == 0
        assert await open_count(competitor_id) == 0
