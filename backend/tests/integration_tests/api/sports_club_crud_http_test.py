# pylint: disable=redefined-outer-name
"""Contrato HTTP del club deportivo: alta y edicion (S4.8g-2 §7/§8/§14, LAB ONLY).
Fija el contrato del CRUD minimo de la entidad que el producto llama **Club** (tabla historica
``sports_clubs``):

* ``POST /clubs/{club_id}/sports-clubs`` da de alta un club del tenant (201) y publica la misma
  proyeccion que el listado (``id, name, active, logo_path``): ni ids de tenant ni columnas
  internas;
* ``PATCH /clubs/{club_id}/sports-clubs/{sports_club_id}`` renombra y/o activa/desactiva (200);
* el protocolo de S3.3c-4 se respeta: sin ``Idempotency-Key`` no hay operacion, la repeticion con la
  misma clave devuelve la respuesta reservada, y con otra clave se reaplica;
* **autorizacion**: OWNER-only para mutar (401 el anonimo, 403 el COLLABORATOR y el ajeno) y el
  club de otro tenant responde 404 igual que uno inexistente;
* **unicidad**: no hay ``UNIQUE(name)`` global. Se rechaza (409) el duplicado obvio dentro del mismo
  tenant y **el mismo nombre sigue siendo valido en otra organizacion** (§8);
* **nombre**: el historial del club se abre al alta y se cierra/abre al renombrar, con una sola
  entrada abierta y sin rangos invertidos (misma disciplina que el competidor);
* **estado**: desactivar es el flujo normal (no hay borrado fisico) y repetir el valor vigente no
  escribe ni audita;
* **auditoria**: cada cambio confirmado deja un evento de ``sports_club`` con el codigo cerrado que
  le toca (``CLUB_CREATE``, ``CLUB_RENAME``, ``CLUB_ACTIVE_CHANGE``) y los nombres de campo tocados;
* la puerta de escritura de F3 alcanza tambien estas dos rutas (503 con la bandera apagada).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any, Final
from uuid import uuid4

import pytest
from heliclockter import datetime_utc
from starlette import status

from bracket.config import config
from bracket.database import database
from bracket.routes.domain_idempotency import IDEMPOTENCY_KEY_HEADER
from bracket.utils.id_types import SportsClubId
from tests.integration_tests.api.competitor_http_support import (
    CompetitorLab,
    call,
    competitor_lab_context,
)
from tests.integration_tests.api.shared import get_root_uvicorn_url
from tests.integration_tests.registration_fixtures import (
    delete_sports_club,
    insert_sports_club,
)

#: Clave HMAC sintetica del laboratorio: sin ella el protocolo de idempotencia falla cerrado.
SYNTHETIC_SECRET: Final = "s48g2-club-lab-hmac-key"


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


#: Proyeccion publica del club: identidad minima y logo. Nada de tenant ni de columnas internas.
CLUB_FIELDS: Final = {"id", "name", "active", "logo_path"}

#: Codigos de auditoria del ciclo de vida del club (catalogo cerrado de alcance ``sports_club``).
CODE_CREATE: Final = "CLUB_CREATE"
CODE_RENAME: Final = "CLUB_RENAME"
CODE_ACTIVE: Final = "CLUB_ACTIVE_CHANGE"


def sports_clubs_url(lab: CompetitorLab) -> str:
    """Listado de clubes del tenant (misma ruta que certifica el catalogo de lectura)."""
    return f"{get_root_uvicorn_url()}clubs/{lab.club}/sports-clubs"


def club_url(lab: CompetitorLab, sports_club_id: int | None = None) -> str:
    base = sports_clubs_url(lab)
    return base if sports_club_id is None else f"{base}/{sports_club_id}"


@asynccontextmanager
async def club_lab() -> AsyncIterator[tuple[CompetitorLab, list[SportsClubId]]]:
    """Tenant del laboratorio con seguimiento de los clubes creados por HTTP.

    Los clubes creados por las pruebas se retiran al cerrar el contexto.
    """
    created: list[SportsClubId] = []
    async with competitor_lab_context() as lab:
        try:
            yield lab, created
        finally:
            for sports_club_id in created:
                await delete_sports_club(sports_club_id)


async def create_club(
    lab: CompetitorLab,
    *,
    token: str | None,
    key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    return await call(
        "POST",
        club_url(lab),
        token=token,
        key=uuid4().hex if key is None else key,
        payload=payload if payload is not None else {"name": f"club-{uuid4().hex[:8]}"},
    )


async def history_rows(sports_club_id: int) -> list[dict[str, Any]]:
    """Historial de nombre del club, en orden de apertura."""
    rows = await database.fetch_all(
        query=(
            "SELECT name, valid_to FROM sports_clubs_name_history "
            "WHERE sports_club_id = :sports_club_id ORDER BY valid_from, id"
        ),
        values={"sports_club_id": sports_club_id},
    )
    return [dict(row._mapping) for row in rows]


async def audit_rows(sports_club_id: int) -> list[dict[str, Any]]:
    """Eventos de auditoria del club, en orden de escritura."""
    rows = await database.fetch_all(
        query=(
            "SELECT action, changed_fields, reason_code FROM domain_change_log "
            "WHERE entity = 'sports_club' AND entity_id = :sports_club_id ORDER BY id"
        ),
        values={"sports_club_id": sports_club_id},
    )
    return [dict(row._mapping) for row in rows]


def changed_fields(row: dict[str, Any]) -> list[str]:
    """``changed_fields`` viaja como JSON: se normaliza a lista para comparar."""
    raw = row["changed_fields"]
    return json.loads(raw) if isinstance(raw, str) else list(raw)


# ---------------------------------------------------------------------------
# A. Alta
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.asyncio(loop_scope="session")
async def test_owner_creates_a_club_and_gets_the_public_projection(flag_on: None) -> None:
    async with club_lab() as (lab, created):
        name = f"LAB Asturkon {uuid4().hex[:6]}"
        response_status, body = await create_club(
            lab, token=lab.owner_token, payload={"name": name}
        )

        assert response_status == status.HTTP_201_CREATED, body
        assert set(body) == CLUB_FIELDS
        assert body["name"] == name
        assert body["active"] is True
        assert body["logo_path"] is None
        created.append(SportsClubId(body["id"]))

        rows = await audit_rows(body["id"])
        assert [(row["action"], row["reason_code"]) for row in rows] == [("CREATE", CODE_CREATE)]
        assert changed_fields(rows[0]) == ["name", "active"]

        history = await history_rows(body["id"])
        assert [row["name"] for row in history] == [name]
        assert history[0]["valid_to"] is None


@pytest.mark.asyncio
@pytest.mark.asyncio(loop_scope="session")
async def test_created_club_is_listed_in_the_tenant_catalog(flag_on: None) -> None:
    async with club_lab() as (lab, created):
        name = f"LAB Checkmat {uuid4().hex[:6]}"
        _, body = await create_club(lab, token=lab.owner_token, payload={"name": name})
        created.append(SportsClubId(body["id"]))

        list_status, listing = await call("GET", sports_clubs_url(lab), token=lab.owner_token)

        assert list_status == status.HTTP_200_OK
        published = listing["data"] if isinstance(listing, dict) else listing
        assert any(club["id"] == body["id"] and club["name"] == name for club in published)


@pytest.mark.asyncio
@pytest.mark.asyncio(loop_scope="session")
async def test_club_can_be_created_inactive(flag_on: None) -> None:
    async with club_lab() as (lab, created):
        response_status, body = await create_club(
            lab, token=lab.owner_token, payload={"name": f"club-{uuid4().hex[:8]}", "active": False}
        )

        assert response_status == status.HTTP_201_CREATED, body
        assert body["active"] is False
        created.append(SportsClubId(body["id"]))


@pytest.mark.asyncio
@pytest.mark.asyncio(loop_scope="session")
async def test_create_rejects_unknown_fields_instead_of_ignoring_them(flag_on: None) -> None:
    """``extra="forbid"``: la identidad y el tenant no se aceptan por cuerpo."""
    async with club_lab() as (lab, _created):
        response_status, _body = await call(
            "POST",
            club_url(lab),
            token=lab.owner_token,
            key=uuid4().hex,
            payload={"name": f"club-{uuid4().hex[:8]}", "tenant_club_id": lab.club},
        )

        assert response_status == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.asyncio
@pytest.mark.asyncio(loop_scope="session")
async def test_create_rejects_an_empty_or_whitespace_name(flag_on: None) -> None:
    async with club_lab() as (lab, _created):
        response_status, body = await create_club(
            lab, token=lab.owner_token, payload={"name": "   "}
        )

        assert response_status == status.HTTP_400_BAD_REQUEST, body


# ---------------------------------------------------------------------------
# B. Protocolo de idempotencia
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.asyncio(loop_scope="session")
async def test_create_without_idempotency_key_does_not_operate(flag_on: None) -> None:
    async with club_lab() as (lab, _created):
        name = f"sin-clave-{uuid4().hex[:6]}"
        response_status, _body = await call(
            "POST", club_url(lab), token=lab.owner_token, payload={"name": name}
        )

        assert response_status == status.HTTP_400_BAD_REQUEST
        exists = await database.fetch_val(
            query="SELECT count(*) FROM sports_clubs WHERE tenant_club_id = :c AND name = :n",
            values={"c": lab.club, "n": name},
        )
        assert exists == 0


@pytest.mark.asyncio
@pytest.mark.asyncio(loop_scope="session")
async def test_repeating_the_same_key_returns_the_reserved_response(flag_on: None) -> None:
    async with club_lab() as (lab, created):
        key = uuid4().hex
        payload = {"name": f"LAB idempotente {uuid4().hex[:6]}"}
        first_status, first = await create_club(
            lab, token=lab.owner_token, key=key, payload=payload
        )
        second_status, second = await create_club(
            lab, token=lab.owner_token, key=key, payload=payload
        )
        created.append(SportsClubId(first["id"]))

        assert (first_status, second_status) == (
            status.HTTP_201_CREATED,
            status.HTTP_201_CREATED,
        )
        assert first == second
        assert len(await audit_rows(first["id"])) == 1


# ---------------------------------------------------------------------------
# C. Unicidad: por tenant, nunca global (§8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.asyncio(loop_scope="session")
async def test_duplicate_name_in_the_same_tenant_is_rejected(flag_on: None) -> None:
    async with club_lab() as (lab, created):
        name = f"LAB repetido {uuid4().hex[:6]}"
        _, first = await create_club(lab, token=lab.owner_token, payload={"name": name})
        created.append(SportsClubId(first["id"]))

        # El duplicado se detecta con el nombre normalizado: espacios repetidos y extremos no
        # sirven para colar dos veces la misma etiqueta.
        response_status, body = await create_club(
            lab, token=lab.owner_token, payload={"name": f"  {name}  "}
        )

        assert response_status == status.HTTP_409_CONFLICT, body
        assert body["detail"] == "DUPLICATE_CLUB"


@pytest.mark.asyncio
@pytest.mark.asyncio(loop_scope="session")
async def test_the_same_name_is_valid_in_another_organization(flag_on: None) -> None:
    """No hay ``UNIQUE(name)`` global: dos organizaciones pueden tener el mismo nombre de club."""
    async with club_lab() as (lab, created):
        shared = f"Gracie Barra {uuid4().hex[:6]}"
        foreign = await insert_sports_club(
            shared, lab.other_club, active=True, created=datetime_utc.now()
        )

        try:
            response_status, body = await create_club(
                lab, token=lab.owner_token, payload={"name": shared}
            )
            assert response_status == status.HTTP_201_CREATED, body
            created.append(SportsClubId(body["id"]))
        finally:
            await delete_sports_club(foreign)


# ---------------------------------------------------------------------------
# D. Edicion: renombrado y estado
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.asyncio(loop_scope="session")
async def test_rename_closes_the_previous_name_history_entry(flag_on: None) -> None:
    async with club_lab() as (lab, created):
        original = f"LAB Asturkon {uuid4().hex[:6]}"
        renamed = f"LAB Checkmat {uuid4().hex[:6]}"
        _, body = await create_club(lab, token=lab.owner_token, payload={"name": original})
        sports_club_id = SportsClubId(body["id"])
        created.append(sports_club_id)

        response_status, updated = await call(
            "PATCH",
            club_url(lab, sports_club_id),
            token=lab.owner_token,
            key=uuid4().hex,
            payload={"name": renamed},
        )

        assert response_status == status.HTTP_200_OK, updated
        assert updated["name"] == renamed
        assert set(updated) == CLUB_FIELDS

        history = await history_rows(sports_club_id)
        assert [row["name"] for row in history] == [original, renamed]
        assert history[0]["valid_to"] is not None
        assert history[1]["valid_to"] is None

        rows = await audit_rows(sports_club_id)
        assert [(row["action"], row["reason_code"]) for row in rows] == [
            ("CREATE", CODE_CREATE),
            ("UPDATE", CODE_RENAME),
        ]
        assert changed_fields(rows[-1]) == ["name"]


@pytest.mark.asyncio
@pytest.mark.asyncio(loop_scope="session")
async def test_deactivate_then_reactivate_audits_the_state_change(flag_on: None) -> None:
    async with club_lab() as (lab, created):
        _, body = await create_club(
            lab, token=lab.owner_token, payload={"name": f"club-{uuid4().hex[:8]}"}
        )
        sports_club_id = SportsClubId(body["id"])
        created.append(sports_club_id)

        off_status, off = await call(
            "PATCH",
            club_url(lab, sports_club_id),
            token=lab.owner_token,
            key=uuid4().hex,
            payload={"active": False},
        )
        on_status, on = await call(
            "PATCH",
            club_url(lab, sports_club_id),
            token=lab.owner_token,
            key=uuid4().hex,
            payload={"active": True},
        )

        assert (off_status, on_status) == (status.HTTP_200_OK, status.HTTP_200_OK)
        assert (off["active"], on["active"]) == (False, True)

        rows = await audit_rows(sports_club_id)
        assert [(row["action"], row["reason_code"]) for row in rows] == [
            ("CREATE", CODE_CREATE),
            ("UPDATE", CODE_ACTIVE),
            ("UPDATE", CODE_ACTIVE),
        ]
        assert changed_fields(rows[-1]) == ["active"]


@pytest.mark.asyncio
@pytest.mark.asyncio(loop_scope="session")
async def test_repeating_the_current_value_writes_nothing(flag_on: None) -> None:
    """Idempotencia por contenido: reenviar el valor vigente no cambia ni audita."""
    async with club_lab() as (lab, created):
        name = f"club-{uuid4().hex[:8]}"
        _, body = await create_club(lab, token=lab.owner_token, payload={"name": name})
        sports_club_id = SportsClubId(body["id"])
        created.append(sports_club_id)

        response_status, unchanged = await call(
            "PATCH",
            club_url(lab, sports_club_id),
            token=lab.owner_token,
            key=uuid4().hex,
            payload={"name": name, "active": True},
        )

        assert response_status == status.HTTP_200_OK
        assert unchanged["name"] == name
        assert len(await audit_rows(sports_club_id)) == 1
        assert len(await history_rows(sports_club_id)) == 1


@pytest.mark.asyncio
@pytest.mark.asyncio(loop_scope="session")
async def test_patch_without_fields_is_rejected(flag_on: None) -> None:
    async with club_lab() as (lab, created):
        _, body = await create_club(
            lab, token=lab.owner_token, payload={"name": f"club-{uuid4().hex[:8]}"}
        )
        created.append(SportsClubId(body["id"]))

        response_status, _body = await call(
            "PATCH",
            club_url(lab, body["id"]),
            token=lab.owner_token,
            key=uuid4().hex,
            payload={},
        )

        assert response_status == status.HTTP_400_BAD_REQUEST


@pytest.mark.asyncio
@pytest.mark.asyncio(loop_scope="session")
async def test_rename_to_a_name_used_by_another_club_of_the_tenant_is_rejected(
    flag_on: None,
) -> None:
    async with club_lab() as (lab, created):
        taken = f"ocupado-{uuid4().hex[:8]}"
        _, first = await create_club(lab, token=lab.owner_token, payload={"name": taken})
        _, second = await create_club(
            lab, token=lab.owner_token, payload={"name": f"libre-{uuid4().hex[:8]}"}
        )
        created.extend([SportsClubId(first["id"]), SportsClubId(second["id"])])

        response_status, body = await call(
            "PATCH",
            club_url(lab, second["id"]),
            token=lab.owner_token,
            key=uuid4().hex,
            payload={"name": taken},
        )

        assert response_status == status.HTTP_409_CONFLICT, body


@pytest.mark.asyncio
@pytest.mark.asyncio(loop_scope="session")
async def test_renaming_to_the_same_name_is_not_a_duplicate_of_itself(flag_on: None) -> None:
    async with club_lab() as (lab, created):
        name = f"club-{uuid4().hex[:8]}"
        _, body = await create_club(lab, token=lab.owner_token, payload={"name": name})
        created.append(SportsClubId(body["id"]))

        response_status, updated = await call(
            "PATCH",
            club_url(lab, body["id"]),
            token=lab.owner_token,
            key=uuid4().hex,
            payload={"name": f"  {name}  ", "active": False},
        )

        assert response_status == status.HTTP_200_OK, updated
        assert (updated["name"], updated["active"]) == (name, False)


# ---------------------------------------------------------------------------
# E. Autorizacion y aislamiento entre tenants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.asyncio(loop_scope="session")
async def test_mutations_are_owner_only(flag_on: None) -> None:
    async with club_lab() as (lab, created):
        _, body = await create_club(
            lab, token=lab.owner_token, payload={"name": f"club-{uuid4().hex[:8]}"}
        )
        sports_club_id = SportsClubId(body["id"])
        created.append(sports_club_id)

        anonymous_status, _ = await call(
            "POST", club_url(lab), token=None, key=uuid4().hex, payload={"name": "x"}
        )
        collaborator_status, _ = await call(
            "POST",
            club_url(lab),
            token=lab.collaborator_token,
            key=uuid4().hex,
            payload={"name": f"club-{uuid4().hex[:8]}"},
        )
        outsider_status, _ = await call(
            "POST",
            club_url(lab),
            token=lab.outsider_token,
            key=uuid4().hex,
            payload={"name": f"club-{uuid4().hex[:8]}"},
        )
        collaborator_patch, _ = await call(
            "PATCH",
            club_url(lab, sports_club_id),
            token=lab.collaborator_token,
            key=uuid4().hex,
            payload={"active": False},
        )

        # Mismo contrato que el alta de competidores: sin sesion y con un tenant ajeno la
        # denegacion es indistinguible (401); con sesion del tenant pero sin ser OWNER, 403.
        assert anonymous_status == status.HTTP_401_UNAUTHORIZED
        assert collaborator_status == status.HTTP_403_FORBIDDEN
        assert outsider_status == status.HTTP_401_UNAUTHORIZED
        assert collaborator_patch == status.HTTP_403_FORBIDDEN

        # Ni el nombre ni el estado del club han cambiado.
        assert len(await audit_rows(sports_club_id)) == 1
        current = await database.fetch_val(
            query="SELECT active FROM sports_clubs WHERE id = :s",
            values={"s": sports_club_id},
        )
        assert current is True


@pytest.mark.asyncio
@pytest.mark.asyncio(loop_scope="session")
async def test_a_club_of_another_tenant_is_not_found(flag_on: None) -> None:
    async with club_lab() as (lab, _created):
        foreign = await insert_sports_club(
            f"ajeno-{uuid4().hex[:8]}",
            lab.other_club,
            active=True,
            created=datetime_utc.now(),
        )
        try:
            response_status, body = await call(
                "PATCH",
                club_url(lab, foreign),
                token=lab.owner_token,
                key=uuid4().hex,
                payload={"name": f"robado-{uuid4().hex[:6]}"},
            )
        finally:
            await delete_sports_club(foreign)

        assert response_status == status.HTTP_404_NOT_FOUND, body
        assert body["detail"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# F. Logo de la entidad: la ruta ya certificada sigue siendo la misma (§7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.asyncio(loop_scope="session")
async def test_logo_route_and_crud_share_the_same_entity(flag_on: None) -> None:
    """El club creado por el CRUD es el mismo que gestiona su logo (una sola entidad)."""
    async with club_lab() as (lab, created):
        _, body = await create_club(
            lab, token=lab.owner_token, payload={"name": f"club-{uuid4().hex[:8]}"}
        )
        sports_club_id = SportsClubId(body["id"])
        created.append(sports_club_id)

        remove_status, removed = await call(
            "POST",
            f"{club_url(lab, sports_club_id)}/logo",
            token=lab.owner_token,
            key=uuid4().hex,
        )

        assert remove_status == status.HTTP_200_OK, removed
        assert removed["id"] == sports_club_id
        assert removed["logo_path"] is None
        assert IDEMPOTENCY_KEY_HEADER
