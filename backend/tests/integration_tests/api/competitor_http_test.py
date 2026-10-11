# pylint: disable=redefined-outer-name
"""Contrato HTTP de competidores (S3.7a, F3B, LAB ONLY).

TDD: este modulo se escribio **antes** de la implementacion y fallo en rojo (no existian ni las
rutas ni el arnes). Fija el contrato descrito en ``docs/26`` §50:

* **Alcance**: el tenant es el ``club`` de la ruta y sale del JWT resuelto en servidor; el cuerpo
  no puede declarar tenant, actor ni estado (``extra="forbid"``);
* **Autorizacion**: alta y renombrado segun la politica de dominio certificada (OWNER y
  COLLABORATOR); baja y reactivacion **OWNER-only**; club ajeno y club inexistente comparten la
  denegacion indistinguible (401) y un competidor de otro tenant no se distingue de uno
  inexistente (404);
* **Banderas**: las cuatro escrituras llevan la puerta de F3 y la lectura sigue publicandose con la
  bandera apagada; el rechazo no consume la ``Idempotency-Key``;
* **Idempotencia**: la clave es obligatoria, el replay es estable y la misma clave con otro
  contenido (u otra operacion) es un conflicto, con un unico efecto y un unico evento de auditoria;
* **Respuesta**: proyeccion cerrada, sin tenant ni estado interno de mas.

Las pruebas de concurrencia viven en ``competitor_http_concurrency_test.py`` y el contrato interno
de la auditoria de la identidad en la suite de S2 (``competitor_write_test.py``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any, Final
from uuid import uuid4

import pytest
import pytest_asyncio
from starlette import status

from bracket.config import config
from bracket.database import database
from bracket.routes.f3_write_gate import F3_WRITE_DISABLED
from bracket.utils.id_types import CompetitorId
from tests.integration_tests.api.competitor_http_support import (
    CompetitorLab,
    action_url,
    call,
    competitor_lab_context,
    competitor_url,
    competitors_url,
)
from tests.integration_tests.api.generate_bracket_http_support import (
    SYNTHETIC_SECRET,
    insert_registration,
)
from tests.integration_tests.api.shared import get_root_uvicorn_url
from tests.integration_tests.registration_fixtures import competitor_audit_rows


def api_url(path: str) -> str:
    """URL absoluta del servidor de pruebas para las rutas que no son de competidores."""
    return f"{get_root_uvicorn_url()}{path.lstrip('/')}"


#: Proyeccion publica del competidor: un campo de mas es un fallo y cierra el contrato.
COMPETITOR_FIELDS: Final = {"id", "display_name", "active", "created", "updated_at"}
#: Proyeccion del listado.
LIST_FIELDS: Final = {"count", "competitors"}
#: Cuerpo con el que un club ajeno y un club inexistente deniegan: indistinguibles.
UNAUTHORISED_DETAIL: Final = "Could not validate credentials"
#: Sin sesion la denegacion es la de la propia capa de credenciales.
NOT_AUTHENTICATED_DETAIL: Final = "Not authenticated"
NOT_FOUND_DETAIL: Final = "Not Found"
FORBIDDEN_DETAIL: Final = "Insufficient role for this organization"
#: Codigos del catalogo cerrado que usan estas rutas.
CREATE_REASON: Final = "PLANNED_ENTRY"
RENAME_REASON: Final = "DATA_CORRECTION"
DEACTIVATE_REASON: Final = "DUPLICATE"
ACTIVATE_REASON: Final = "ADMINISTRATIVE"


@pytest.fixture(scope="module", autouse=True)
def idempotency_secret() -> Iterator[None]:
    """Clave HMAC sintetica del laboratorio: sin ella el protocolo falla cerrado.

    La app corre en el mismo proceso, asi que parchear la configuracion alcanza al servidor de
    pruebas. El valor es sintetico y no sale de aqui.
    """
    patcher = pytest.MonkeyPatch()
    patcher.setattr(config, "idempotency_hmac_key", SYNTHETIC_SECRET)
    patcher.setattr(config, "idempotency_hmac_key_version", "v1")
    yield
    patcher.undo()


@pytest.fixture()
def flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Escritura habilitada de forma explicita: el contrato no depende del entorno de CI."""
    monkeypatch.setattr(config, "f3_write_enabled", True)


@pytest.fixture()
def flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Escritura cerrada de forma explicita, tambien fuera de CI."""
    monkeypatch.setattr(config, "f3_write_enabled", False)


@pytest_asyncio.fixture(loop_scope="session", scope="function")
async def lab() -> AsyncIterator[CompetitorLab]:
    async with competitor_lab_context() as competitor_lab:
        yield competitor_lab


def new_key() -> str:
    return uuid4().hex


def create_payload(name: str, reason_code: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"display_name": name}
    if reason_code is not None:
        payload["reason_code"] = reason_code
    return payload


async def name_history(competitor_id: int) -> list[dict[str, Any]]:
    """Historial de nombres en orden de apertura, con el cierre de cada entrada."""
    records = await database.fetch_all(
        query="""
            SELECT display_name, valid_to
            FROM competitors_name_history
            WHERE competitor_id = :competitor_id
            ORDER BY valid_from, id
        """,
        values={"competitor_id": competitor_id},
    )
    return [dict(record._mapping) for record in records]


async def stored_competitor(competitor_id: int) -> dict[str, Any]:
    record = await database.fetch_one(
        query="SELECT id, display_name, active FROM competitors WHERE id = :id",
        values={"id": competitor_id},
    )
    assert record is not None
    return dict(record._mapping)


def actions(rows: list[dict[str, Any]]) -> list[str]:
    return [str(row["action"]) for row in rows]


# --- Alta -----------------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_owner_creates_the_identity_with_the_closed_projection(
    lab: CompetitorLab, flag_on: None
) -> None:
    name = f"{lab.root_name}-nueva"
    response_status, body = await call(
        "POST",
        competitors_url(lab),
        token=lab.owner_token,
        key=new_key(),
        payload=create_payload(name, CREATE_REASON),
    )

    assert response_status == status.HTTP_201_CREATED
    assert set(body) == COMPETITOR_FIELDS
    assert body["display_name"] == name
    assert body["active"] is True
    assert body["id"] > 0

    stored = await stored_competitor(body["id"])
    assert stored == {"id": body["id"], "display_name": name, "active": True}

    audit = await competitor_audit_rows(body["id"])
    assert actions(audit) == ["CREATE"]
    assert audit[0]["entity"] == "competitor"
    assert audit[0]["changed_fields"] == ["display_name"]
    assert audit[0]["reason_code"] == CREATE_REASON
    assert audit[0]["actor_user_id"] is not None

    history = await name_history(body["id"])
    assert [(row["display_name"], row["valid_to"]) for row in history] == [(name, None)]


@pytest.mark.asyncio(loop_scope="session")
async def test_collaborator_can_create_as_the_domain_policy_says(
    lab: CompetitorLab, flag_on: None
) -> None:
    response_status, body = await call(
        "POST",
        competitors_url(lab),
        token=lab.collaborator_token,
        key=new_key(),
        payload=create_payload(f"{lab.root_name}-colaboradora", CREATE_REASON),
    )

    assert response_status == status.HTTP_201_CREATED
    assert body["active"] is True


@pytest.mark.asyncio(loop_scope="session")
async def test_the_tenant_is_the_club_of_the_route_and_never_the_body(
    lab: CompetitorLab, flag_on: None
) -> None:
    # El club ajeno, el club inexistente y el actor inactivo comparten la denegacion
    # indistinguible de la capa de credenciales; sin cabecera de sesion la respuesta es la
    # propia de esa ausencia.
    for label, token, club, detail in (
        ("club ajeno", lab.outsider_token, lab.club, UNAUTHORISED_DETAIL),
        ("club inexistente", lab.owner_token, 99999999, UNAUTHORISED_DETAIL),
        ("actor inactivo", lab.inactive_user_token, lab.club, UNAUTHORISED_DETAIL),
        ("sin sesion", None, lab.club, NOT_AUTHENTICATED_DETAIL),
    ):
        response_status, body = await call(
            "POST",
            competitors_url(lab).replace(f"clubs/{lab.club}/", f"clubs/{club}/"),
            token=token,
            key=new_key(),
            payload=create_payload(f"{lab.root_name}-ajena", CREATE_REASON),
        )
        assert response_status == status.HTTP_401_UNAUTHORIZED, label
        assert body == {"detail": detail}, label

    # Y en el club propio del actor ajeno el alta si cae en su tenant.
    response_status, body = await call(
        "POST",
        competitors_url(lab).replace(f"clubs/{lab.club}/", f"clubs/{lab.other_club}/"),
        token=lab.outsider_token,
        key=new_key(),
        payload=create_payload(f"{lab.root_name}-propia-del-ajeno", CREATE_REASON),
    )
    assert response_status == status.HTTP_201_CREATED
    assert body["id"] > 0


@pytest.mark.asyncio(loop_scope="session")
async def test_the_body_cannot_declare_tenant_or_state(lab: CompetitorLab, flag_on: None) -> None:
    for extra in (
        {"managed_by_club_id": int(lab.other_club)},
        {"tenant_club_id": int(lab.other_club)},
        {"actor_user_id": 1},
        {"active": False},
    ):
        response_status, body = await call(
            "POST",
            competitors_url(lab),
            token=lab.owner_token,
            key=new_key(),
            payload={**create_payload(f"{lab.root_name}-inyectada", CREATE_REASON), **extra},
        )
        assert response_status == status.HTTP_422_UNPROCESSABLE_CONTENT, (extra, body)


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("name", ["", "   ", "x" * 201])
async def test_invalid_names_are_rejected_with_the_domain_error(
    lab: CompetitorLab, flag_on: None, name: str
) -> None:
    response_status, body = await call(
        "POST",
        competitors_url(lab),
        token=lab.owner_token,
        key=new_key(),
        payload={"display_name": name, "reason_code": CREATE_REASON},
    )

    assert response_status == status.HTTP_400_BAD_REQUEST
    assert set(body) == {"detail"}


@pytest.mark.asyncio(loop_scope="session")
async def test_a_missing_idempotency_key_is_rejected_without_creating(
    lab: CompetitorLab, flag_on: None
) -> None:
    response_status, body = await call(
        "POST",
        competitors_url(lab),
        token=lab.owner_token,
        payload=create_payload(f"{lab.root_name}-sin-clave", CREATE_REASON),
    )

    assert response_status == status.HTTP_400_BAD_REQUEST
    assert body["detail"] == "IDEMPOTENCY_REQUEST_INVALID"


@pytest.mark.asyncio(loop_scope="session")
async def test_replay_returns_the_same_identity_and_writes_once(
    lab: CompetitorLab, flag_on: None
) -> None:
    key = new_key()
    payload = create_payload(f"{lab.root_name}-repetida", CREATE_REASON)

    first_status, first = await call(
        "POST", competitors_url(lab), token=lab.owner_token, key=key, payload=payload
    )
    second_status, second = await call(
        "POST", competitors_url(lab), token=lab.owner_token, key=key, payload=payload
    )

    assert (first_status, second_status) == (status.HTTP_201_CREATED, status.HTTP_201_CREATED)
    assert first == second
    audit = await competitor_audit_rows(first["id"])
    assert actions(audit) == ["CREATE"]
    assert len(await name_history(first["id"])) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_the_same_key_with_another_payload_is_a_conflict(
    lab: CompetitorLab, flag_on: None
) -> None:
    key = new_key()
    first_status, first = await call(
        "POST",
        competitors_url(lab),
        token=lab.owner_token,
        key=key,
        payload=create_payload(f"{lab.root_name}-conflicto-a", CREATE_REASON),
    )
    assert first_status == status.HTTP_201_CREATED

    second_status, second = await call(
        "POST",
        competitors_url(lab),
        token=lab.owner_token,
        key=key,
        payload=create_payload(f"{lab.root_name}-conflicto-b", CREATE_REASON),
    )

    assert second_status == status.HTTP_409_CONFLICT
    assert second["detail"] == "IDEMPOTENCY_KEY_REUSED"
    # Sin doble efecto: la segunda identidad no existe.
    records = await database.fetch_val(
        query="SELECT count(*) FROM competitors WHERE managed_by_club_id = :c"
        " AND display_name = :n",
        values={"c": lab.club, "n": f"{lab.root_name}-conflicto-b"},
    )
    assert records == 0
    assert first["id"] > 0


@pytest.mark.asyncio(loop_scope="session")
async def test_a_key_cannot_be_reused_for_another_operation(
    lab: CompetitorLab, flag_on: None
) -> None:
    key = new_key()
    created_status, created = await call(
        "POST",
        competitors_url(lab),
        token=lab.owner_token,
        key=key,
        payload=create_payload(f"{lab.root_name}-clave-unica", CREATE_REASON),
    )
    assert created_status == status.HTTP_201_CREATED

    rename_status, rename_body = await call(
        "POST",
        action_url(lab, created["id"], "rename"),
        token=lab.owner_token,
        key=key,
        payload={"display_name": f"{lab.root_name}-otra-cosa", "reason_code": RENAME_REASON},
    )

    assert rename_status == status.HTTP_409_CONFLICT
    assert rename_body["detail"] == "IDEMPOTENCY_KEY_REUSED"
    stored = await stored_competitor(int(created["id"]))
    assert stored["display_name"] == f"{lab.root_name}-clave-unica"


# --- Lectura --------------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_the_list_is_tenant_scoped_deterministic_and_counted(
    lab: CompetitorLab, flag_off: None
) -> None:
    response_status, body = await call("GET", competitors_url(lab), token=lab.owner_token)

    assert response_status == status.HTTP_200_OK
    assert set(body) == LIST_FIELDS
    assert body["count"] == len(lab.active_names)
    assert [row["display_name"] for row in body["competitors"]] == list(lab.active_names)
    assert all(set(row) == COMPETITOR_FIELDS for row in body["competitors"])
    # La identidad dada de baja y la del otro tenant no aparecen.
    names = [row["display_name"] for row in body["competitors"]]
    assert f"{lab.root_name}-baja" not in names
    assert f"{lab.root_name}-ajena" not in names


@pytest.mark.asyncio(loop_scope="session")
async def test_pagination_neither_repeats_nor_skips(lab: CompetitorLab, flag_off: None) -> None:
    first_status, first = await call(
        "GET", f"{competitors_url(lab)}?limit=2&offset=0", token=lab.owner_token
    )
    second_status, second = await call(
        "GET", f"{competitors_url(lab)}?limit=2&offset=2", token=lab.owner_token
    )

    assert (first_status, second_status) == (status.HTTP_200_OK, status.HTTP_200_OK)
    assert first["count"] == second["count"] == len(lab.active_names)
    assert [row["display_name"] for row in first["competitors"]] == list(lab.active_names[:2])
    assert [row["display_name"] for row in second["competitors"]] == list(lab.active_names[2:])


@pytest.mark.asyncio(loop_scope="session")
async def test_the_search_is_literal_and_inactive_identities_are_opt_in(
    lab: CompetitorLab, flag_off: None
) -> None:
    matched_status, matched = await call(
        "GET", f"{competitors_url(lab)}?display_name=media", token=lab.owner_token
    )
    wildcard_status, wildcard = await call(
        "GET", f"{competitors_url(lab)}?display_name=%25", token=lab.owner_token
    )
    inactive_status, inactive = await call(
        "GET", f"{competitors_url(lab)}?include_inactive=true", token=lab.owner_token
    )

    assert matched_status == status.HTTP_200_OK
    assert [row["display_name"] for row in matched["competitors"]] == [f"{lab.root_name}-media"]
    # ``%`` es un caracter literal, no un patron: no devuelve el listado entero.
    assert wildcard_status == status.HTTP_200_OK
    assert wildcard["count"] == 0
    assert inactive_status == status.HTTP_200_OK
    assert inactive["count"] == len(lab.active_names) + 1
    assert f"{lab.root_name}-baja" in [row["display_name"] for row in inactive["competitors"]]


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1"])
async def test_out_of_range_pages_are_rejected(
    lab: CompetitorLab, flag_off: None, query: str
) -> None:
    response_status, _ = await call("GET", f"{competitors_url(lab)}?{query}", token=lab.owner_token)

    assert response_status == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.asyncio(loop_scope="session")
async def test_the_detail_returns_the_closed_projection(lab: CompetitorLab, flag_off: None) -> None:
    competitor_id = int(lab.active_competitors[0])
    response_status, body = await call(
        "GET", competitor_url(lab, competitor_id), token=lab.collaborator_token
    )

    assert response_status == status.HTTP_200_OK
    assert set(body) == COMPETITOR_FIELDS
    assert body["id"] == competitor_id


@pytest.mark.asyncio(loop_scope="session")
async def test_a_foreign_or_unknown_identity_is_indistinguishable_from_missing(
    lab: CompetitorLab, flag_off: None
) -> None:
    foreign_status, foreign = await call(
        "GET", competitor_url(lab, int(lab.foreign_competitor)), token=lab.owner_token
    )
    unknown_status, unknown = await call(
        "GET", competitor_url(lab, 99999999), token=lab.owner_token
    )

    assert foreign_status == unknown_status == status.HTTP_404_NOT_FOUND
    assert foreign == unknown == {"detail": NOT_FOUND_DETAIL}


@pytest.mark.asyncio(loop_scope="session")
async def test_the_club_of_the_route_still_needs_access(lab: CompetitorLab, flag_off: None) -> None:
    response_status, body = await call(
        "GET",
        competitor_url(lab, int(lab.foreign_competitor)).replace(
            f"clubs/{lab.club}/", f"clubs/{lab.other_club}/"
        ),
        token=lab.owner_token,
    )

    assert response_status == status.HTTP_401_UNAUTHORIZED
    assert body == {"detail": UNAUTHORISED_DETAIL}


# --- Renombrado -----------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_the_rename_keeps_both_names_in_the_history(
    lab: CompetitorLab, flag_on: None
) -> None:
    old_name = f"{lab.root_name}-antes"
    new_name = f"{lab.root_name}-renombrada"
    created_status, created = await call(
        "POST",
        competitors_url(lab),
        token=lab.owner_token,
        key=new_key(),
        payload=create_payload(old_name, CREATE_REASON),
    )
    assert created_status == status.HTTP_201_CREATED
    competitor_id = created["id"]

    response_status, body = await call(
        "POST",
        action_url(lab, competitor_id, "rename"),
        token=lab.collaborator_token,
        key=new_key(),
        payload={"display_name": new_name, "reason_code": RENAME_REASON},
    )

    assert response_status == status.HTTP_200_OK
    assert body["display_name"] == new_name
    assert body["active"] is True

    history = await name_history(competitor_id)
    assert [row["display_name"] for row in history] == [old_name, new_name]
    assert history[0]["valid_to"] is not None
    assert history[1]["valid_to"] is None

    audit = await competitor_audit_rows(CompetitorId(competitor_id))
    assert actions(audit) == ["CREATE", "UPDATE"]
    assert audit[1]["changed_fields"] == ["display_name"]
    assert audit[1]["reason_code"] == RENAME_REASON


@pytest.mark.asyncio(loop_scope="session")
async def test_renaming_to_the_same_name_writes_nothing(lab: CompetitorLab, flag_on: None) -> None:
    same_name = f"{lab.root_name}-igual"
    _, created = await call(
        "POST",
        competitors_url(lab),
        token=lab.owner_token,
        key=new_key(),
        payload=create_payload(same_name, CREATE_REASON),
    )
    competitor_id = created["id"]

    response_status, body = await call(
        "POST",
        action_url(lab, competitor_id, "rename"),
        token=lab.owner_token,
        key=new_key(),
        payload={"display_name": same_name, "reason_code": RENAME_REASON},
    )

    assert response_status == status.HTTP_200_OK
    assert body["display_name"] == same_name
    assert len(await name_history(competitor_id)) == 1
    assert actions(await competitor_audit_rows(CompetitorId(competitor_id))) == ["CREATE"]


@pytest.mark.asyncio(loop_scope="session")
async def test_the_rename_replay_is_stable(lab: CompetitorLab, flag_on: None) -> None:
    payload = {"display_name": f"{lab.root_name}-replay", "reason_code": RENAME_REASON}
    _, created = await call(
        "POST",
        competitors_url(lab),
        token=lab.owner_token,
        key=new_key(),
        payload=create_payload(f"{lab.root_name}-replay-antes", CREATE_REASON),
    )
    competitor_id = created["id"]
    key = new_key()

    first_status, first = await call(
        "POST",
        action_url(lab, competitor_id, "rename"),
        token=lab.owner_token,
        key=key,
        payload=payload,
    )
    second_status, second = await call(
        "POST",
        action_url(lab, competitor_id, "rename"),
        token=lab.owner_token,
        key=key,
        payload=payload,
    )

    assert (first_status, second_status) == (status.HTTP_200_OK, status.HTTP_200_OK)
    assert first == second
    assert len(await name_history(competitor_id)) == 2
    assert actions(await competitor_audit_rows(CompetitorId(competitor_id))) == ["CREATE", "UPDATE"]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_foreign_or_unknown_identity_cannot_be_renamed(
    lab: CompetitorLab, flag_on: None
) -> None:
    for competitor_id in (int(lab.foreign_competitor), 99999999):
        response_status, body = await call(
            "POST",
            action_url(lab, competitor_id, "rename"),
            token=lab.owner_token,
            key=new_key(),
            payload={"display_name": f"{lab.root_name}-imposible", "reason_code": RENAME_REASON},
        )
        assert response_status == status.HTTP_404_NOT_FOUND
        assert body == {"detail": NOT_FOUND_DETAIL}


@pytest.mark.asyncio(loop_scope="session")
async def test_the_rename_validates_name_and_reason(lab: CompetitorLab, flag_on: None) -> None:
    competitor_id = int(lab.active_competitors[0])

    missing_status, _ = await call(
        "POST",
        action_url(lab, competitor_id, "rename"),
        token=lab.owner_token,
        key=new_key(),
        payload={"reason_code": RENAME_REASON},
    )
    reason_status, reason_body = await call(
        "POST",
        action_url(lab, competitor_id, "rename"),
        token=lab.owner_token,
        key=new_key(),
        payload={"display_name": f"{lab.root_name}-x", "reason_code": "INVENTADO"},
    )
    name_status, _ = await call(
        "POST",
        action_url(lab, competitor_id, "rename"),
        token=lab.owner_token,
        key=new_key(),
        payload={"display_name": "   ", "reason_code": RENAME_REASON},
    )

    assert missing_status == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert reason_status == status.HTTP_400_BAD_REQUEST
    assert set(reason_body) == {"detail"}
    assert name_status == status.HTTP_400_BAD_REQUEST


@pytest.mark.asyncio(loop_scope="session")
async def test_renaming_a_deactivated_identity_does_not_reactivate_it(
    lab: CompetitorLab, flag_on: None
) -> None:
    competitor_id = int(lab.inactive_competitor)

    response_status, body = await call(
        "POST",
        action_url(lab, competitor_id, "rename"),
        token=lab.owner_token,
        key=new_key(),
        payload={"display_name": f"{lab.root_name}-baja-renombrada", "reason_code": RENAME_REASON},
    )

    assert response_status == status.HTTP_200_OK
    assert body["active"] is False
    assert (await stored_competitor(competitor_id))["active"] is False


# --- Baja y reactivacion --------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_the_owner_deactivates_without_deleting_the_identity(
    lab: CompetitorLab, flag_on: None
) -> None:
    name = f"{lab.root_name}-baja-http"
    _, created = await call(
        "POST",
        competitors_url(lab),
        token=lab.owner_token,
        key=new_key(),
        payload=create_payload(name, CREATE_REASON),
    )
    competitor_id = created["id"]

    response_status, body = await call(
        "POST",
        action_url(lab, competitor_id, "deactivate"),
        token=lab.owner_token,
        key=new_key(),
        payload={"reason_code": DEACTIVATE_REASON},
    )

    assert response_status == status.HTTP_200_OK
    assert body["active"] is False
    assert (await stored_competitor(competitor_id))["active"] is False
    # La identidad y su historial siguen ahi: no se borra nada.
    assert len(await name_history(competitor_id)) == 1
    audit = await competitor_audit_rows(CompetitorId(competitor_id))
    assert actions(audit) == ["CREATE", "DEACTIVATE"]
    assert audit[1]["changed_fields"] == ["active"]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_collaborator_cannot_deactivate_nor_reactivate(
    lab: CompetitorLab, flag_on: None
) -> None:
    competitor_id = int(lab.active_competitors[1])

    deactivate_status, deactivate_body = await call(
        "POST",
        action_url(lab, competitor_id, "deactivate"),
        token=lab.collaborator_token,
        key=new_key(),
        payload={"reason_code": DEACTIVATE_REASON},
    )
    activate_status, activate_body = await call(
        "POST",
        action_url(lab, int(lab.inactive_competitor), "activate"),
        token=lab.collaborator_token,
        key=new_key(),
        payload={"reason_code": ACTIVATE_REASON},
    )

    assert deactivate_status == activate_status == status.HTTP_403_FORBIDDEN
    assert deactivate_body["detail"] == activate_body["detail"] == "ROLE_NOT_ALLOWED"
    assert (await stored_competitor(competitor_id))["active"] is True


@pytest.mark.asyncio(loop_scope="session")
async def test_the_owner_reactivates_the_identity(lab: CompetitorLab, flag_on: None) -> None:
    competitor_id = int(lab.inactive_competitor)
    name = f"{lab.root_name}-baja"

    response_status, body = await call(
        "POST",
        action_url(lab, competitor_id, "activate"),
        token=lab.owner_token,
        key=new_key(),
        payload={"reason_code": ACTIVATE_REASON},
    )

    assert response_status == status.HTTP_200_OK
    assert body["active"] is True
    assert body["display_name"] == name
    audit = await competitor_audit_rows(CompetitorId(competitor_id))
    assert actions(audit) == ["ACTIVATE"]
    assert audit[0]["changed_fields"] == ["active"]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_transition_that_changes_nothing_is_a_noop(
    lab: CompetitorLab, flag_on: None
) -> None:
    competitor_id = int(lab.active_competitors[2])

    deactivate_status, deactivated = await call(
        "POST",
        action_url(lab, competitor_id, "deactivate"),
        token=lab.owner_token,
        key=new_key(),
        payload={"reason_code": DEACTIVATE_REASON},
    )
    again_status, again = await call(
        "POST",
        action_url(lab, competitor_id, "deactivate"),
        token=lab.owner_token,
        key=new_key(),
        payload={"reason_code": DEACTIVATE_REASON},
    )

    assert (deactivate_status, again_status) == (status.HTTP_200_OK, status.HTTP_200_OK)
    assert deactivated == again
    assert actions(await competitor_audit_rows(CompetitorId(competitor_id))) == ["DEACTIVATE"]


@pytest.mark.asyncio(loop_scope="session")
async def test_the_state_change_replay_is_stable(lab: CompetitorLab, flag_on: None) -> None:
    competitor_id = int(lab.active_competitors[0])
    key = new_key()
    payload = {"reason_code": DEACTIVATE_REASON}

    first_status, first = await call(
        "POST",
        action_url(lab, competitor_id, "deactivate"),
        token=lab.owner_token,
        key=key,
        payload=payload,
    )
    second_status, second = await call(
        "POST",
        action_url(lab, competitor_id, "deactivate"),
        token=lab.owner_token,
        key=key,
        payload=payload,
    )

    assert (first_status, second_status) == (status.HTTP_200_OK, status.HTTP_200_OK)
    assert first == second
    assert actions(await competitor_audit_rows(CompetitorId(competitor_id))) == ["DEACTIVATE"]


@pytest.mark.asyncio(loop_scope="session")
async def test_state_changes_reject_unknown_foreign_and_invalid_reason(
    lab: CompetitorLab, flag_on: None
) -> None:
    for competitor_id, action in (
        (int(lab.foreign_competitor), "deactivate"),
        (99999999, "deactivate"),
        (int(lab.foreign_competitor), "activate"),
        (99999999, "activate"),
    ):
        response_status, body = await call(
            "POST",
            action_url(lab, competitor_id, action),
            token=lab.owner_token,
            key=new_key(),
            payload={"reason_code": DEACTIVATE_REASON},
        )
        assert response_status == status.HTTP_404_NOT_FOUND, (action, competitor_id)
        assert body == {"detail": NOT_FOUND_DETAIL}

    invalid_status, _ = await call(
        "POST",
        action_url(lab, int(lab.active_competitors[0]), "deactivate"),
        token=lab.owner_token,
        key=new_key(),
        payload={"reason_code": "INVENTADO"},
    )
    assert invalid_status == status.HTTP_400_BAD_REQUEST


@pytest.mark.asyncio(loop_scope="session")
async def test_a_deactivated_identity_leaves_the_default_listing(
    lab: CompetitorLab, flag_on: None
) -> None:
    competitor_id = int(lab.active_competitors[1])
    await call(
        "POST",
        action_url(lab, competitor_id, "deactivate"),
        token=lab.owner_token,
        key=new_key(),
        payload={"reason_code": DEACTIVATE_REASON},
    )

    _, default_list = await call("GET", competitors_url(lab), token=lab.owner_token)
    _, full_list = await call(
        "GET", f"{competitors_url(lab)}?include_inactive=true", token=lab.owner_token
    )

    assert default_list["count"] == len(lab.active_names) - 1
    assert full_list["count"] == len(lab.active_names) + 1


@pytest.mark.asyncio(loop_scope="session")
async def test_the_deactivation_shows_up_in_the_bracket_readiness(
    lab: CompetitorLab, flag_on: None
) -> None:
    competitor_id = int(lab.active_competitors[0])
    await insert_registration(lab.tournament, competitor_id=CompetitorId(competitor_id))

    _, before = await call(
        "GET", api_url(f"tournaments/{lab.tournament}/bracket-readiness"), token=lab.owner_token
    )
    before_blockers = [issue["code"] for issue in before["blocking_issues"]]

    response_status, _ = await call(
        "POST",
        action_url(lab, competitor_id, "deactivate"),
        token=lab.owner_token,
        key=new_key(),
        payload={"reason_code": DEACTIVATE_REASON},
    )
    _, after = await call(
        "GET", api_url(f"tournaments/{lab.tournament}/bracket-readiness"), token=lab.owner_token
    )
    after_blockers = [issue["code"] for issue in after["blocking_issues"]]

    assert response_status == status.HTTP_200_OK
    assert "COMPETITOR_INACTIVE" not in before_blockers
    assert "COMPETITOR_INACTIVE" in after_blockers
    assert after["ready"] is False
    # La inscripcion no se toca: la baja es de la identidad, no del historico.
    assert after["summary"]["total"] == before["summary"]["total"] == 1


# --- Bandera de escritura -------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_writes_are_closed_and_reads_stay_open_with_the_flag_off(
    lab: CompetitorLab, flag_off: None
) -> None:
    competitor_id = int(lab.active_competitors[0])
    write_requests = (
        ("POST", competitors_url(lab), create_payload(f"{lab.root_name}-cerrada", CREATE_REASON)),
        (
            "POST",
            action_url(lab, competitor_id, "rename"),
            {"display_name": f"{lab.root_name}-cerrada", "reason_code": RENAME_REASON},
        ),
        (
            "POST",
            action_url(lab, competitor_id, "deactivate"),
            {"reason_code": DEACTIVATE_REASON},
        ),
        (
            "POST",
            action_url(lab, competitor_id, "activate"),
            {"reason_code": ACTIVATE_REASON},
        ),
    )

    for method, url, payload in write_requests:
        response_status, body = await call(
            method, url, token=lab.owner_token, key=new_key(), payload=payload
        )
        assert response_status == status.HTTP_503_SERVICE_UNAVAILABLE, url
        assert body == {"detail": F3_WRITE_DISABLED}

    # Lectura intacta con la bandera apagada.
    read_status, read_body = await call("GET", competitors_url(lab), token=lab.owner_token)
    detail_status, _ = await call("GET", competitor_url(lab, competitor_id), token=lab.owner_token)
    assert (read_status, detail_status) == (status.HTTP_200_OK, status.HTTP_200_OK)
    assert read_body["count"] == len(lab.active_names)

    # Cero efectos: ni identidad nueva, ni cambio de estado, ni auditoria.
    assert (await stored_competitor(competitor_id))["active"] is True
    assert (await stored_competitor(competitor_id))["display_name"] == lab.active_names[0]
    assert await competitor_audit_rows(CompetitorId(competitor_id)) == []
    assert await name_history(competitor_id) == []


@pytest.mark.asyncio(loop_scope="session")
async def test_the_blocked_write_does_not_consume_the_idempotency_key(
    lab: CompetitorLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = new_key()
    payload = create_payload(f"{lab.root_name}-clave-intacta", CREATE_REASON)

    monkeypatch.setattr(config, "f3_write_enabled", False)
    blocked_status, blocked_body = await call(
        "POST", competitors_url(lab), token=lab.owner_token, key=key, payload=payload
    )

    monkeypatch.setattr(config, "f3_write_enabled", True)
    created_status, created = await call(
        "POST", competitors_url(lab), token=lab.owner_token, key=key, payload=payload
    )

    assert blocked_status == status.HTTP_503_SERVICE_UNAVAILABLE
    assert blocked_body == {"detail": F3_WRITE_DISABLED}
    assert created_status == status.HTTP_201_CREATED
    assert created["display_name"] == f"{lab.root_name}-clave-intacta"


@pytest.mark.asyncio(loop_scope="session")
async def test_the_flag_does_not_replace_the_authorisation(
    lab: CompetitorLab, flag_off: None
) -> None:
    response_status, body = await call(
        "POST",
        competitors_url(lab),
        token=lab.collaborator_token,
        key=new_key(),
        payload=create_payload(f"{lab.root_name}-sin-permiso", CREATE_REASON),
    )

    assert response_status == status.HTTP_503_SERVICE_UNAVAILABLE
    assert body == {"detail": F3_WRITE_DISABLED}


# --- Regresion ------------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_the_legacy_club_routes_are_untouched(lab: CompetitorLab, flag_off: None) -> None:
    response_status, body = await call("GET", api_url("clubs"), token=lab.owner_token)

    assert response_status == status.HTTP_200_OK
    assert int(lab.club) in [club["id"] for club in body["data"]]


@pytest.mark.asyncio(loop_scope="session")
async def test_the_registration_read_api_still_sees_the_tenant(
    lab: CompetitorLab, flag_on: None
) -> None:
    created_status, created = await call(
        "POST",
        competitors_url(lab),
        token=lab.owner_token,
        key=new_key(),
        payload=create_payload(f"{lab.root_name}-inscrita", CREATE_REASON),
    )
    assert created_status == status.HTTP_201_CREATED
    await insert_registration(lab.tournament, competitor_id=created["id"])

    list_status, listing = await call(
        "GET",
        api_url(f"tournaments/{lab.tournament}/registrations"),
        token=lab.owner_token,
    )

    assert list_status == status.HTTP_200_OK
    assert listing["count"] == 1
    assert listing["registrations"][0]["competitor_id"] == created["id"]
    assert created["id"] in {row["competitor_id"] for row in listing["registrations"]}
