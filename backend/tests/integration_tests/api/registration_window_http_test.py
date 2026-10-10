# pylint: disable=redefined-outer-name
"""Contrato HTTP de la ventana de inscripcion F3 (S3.4c, LAB ONLY).

TDD: este modulo se escribio **antes** de la implementacion y fallo en rojo (los dos endpoints
nuevos no existian: ``404``/``405`` en vez del contrato; el alta no miraba la ventana).

Pruebas **negras** contra la aplicacion del producto (``bracket/app.py``): servidor real y sesion
del producto (``Bearer`` emitido por el mismo camino que en produccion).

Que se demuestra aqui, y contra que capa:

* la ventana es del **servidor**: se lee y se configura bajo el tenant del JWT, y el cuerpo nunca
  aporta identidad, tenant ni estado de cierre;
* la configuracion del plazo y el cierre manual son **administrativos** (OWNER) y toda escritura
  pasa por el protocolo de idempotencia de S3.3c-4, con replay estable;
* el alta certificada en S3.4a **rechaza** con un codigo estable (``409 REGISTRATION_CLOSED``)
  cuando el plazo vencio o hay cierre manual, y el rechazo no deja inscripcion ni auditoria propia;
* el **cierre manual no bloquea** el ciclo de vida de las inscripciones ya existentes: es una
  decision analizada y documentada, no un efecto colateral;
* la lectura no exige ``Idempotency-Key`` y no escribe auditoria.

Solo usa la base de CI (``bracket_ci``): sin mocks del dominio ni de la capa SQL.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import aiohttp
import pytest
import pytest_asyncio
from heliclockter import datetime_utc

from bracket.config import config
from bracket.database import database
from bracket.models.db.club import ClubInsertable
from bracket.models.db.user_x_club import UserXClubInsertable, UserXClubRelation
from bracket.routes.domain_idempotency import IDEMPOTENCY_KEY_HEADER
from bracket.sql.users import update_user_active
from bracket.utils.dummy_records import DUMMY_TOURNAMENT
from bracket.utils.http import HTTPMethod
from bracket.utils.id_types import ClubId, CompetitorId, TournamentId
from tests.integration_tests.api.shared import get_root_uvicorn_url
from tests.integration_tests.mocks import get_mock_token, get_mock_user
from tests.integration_tests.registration_fixtures import (
    delete_competitor,
    delete_registration_audit,
    insert_competitor,
)
from tests.integration_tests.sql import (
    inserted_club,
    inserted_tournament,
    inserted_user,
    inserted_user_x_club,
)

# Secreto HMAC **sintetico** de laboratorio (nunca uno de despliegue), como en S3.3c-3.
SYNTHETIC_SECRET = "synthetic-laboratory-secret-not-for-deployment"

JSON_CONTENT_TYPE = "application/json"
NOT_FOUND = {"detail": "Not Found"}
NOT_AUTHENTICATED = {"detail": "Not authenticated"}
UNAUTHORIZED = {"detail": "Could not validate credentials"}
ROLE_NOT_ALLOWED = {"detail": "ROLE_NOT_ALLOWED"}
IDEMPOTENCY_REQUEST_INVALID = {"detail": "IDEMPOTENCY_REQUEST_INVALID"}
REGISTRATION_CLOSED = {"detail": "REGISTRATION_CLOSED"}
REGISTRATION_ALREADY_CLOSED = {"detail": "REGISTRATION_ALREADY_CLOSED"}
REQUEST_INVALID = {"detail": "Registration request is invalid"}

#: Lista blanca de la respuesta: cualquier campo nuevo tiene que ser una decision explicita.
PUBLIC_WINDOW_FIELDS = frozenset(
    {
        "tournament_id",
        "registration_deadline",
        "registrations_closed_at",
        "state",
        "registrations_open",
    }
)

#: Campos que no pueden aparecer en la respuesta de la ventana.
INTERNAL_WINDOW_FIELDS = frozenset(
    {
        "actor_user_id",
        "actor_label",
        "tenant_club_id",
        "closed_by_user_id",
        "idempotency_key",
        "reason_code",
        "reason_note",
        "audit_event_id",
    }
)

#: Auditoria de la ventana: se guarda por **nombre de campo**, nunca por valor.
SELECT_TOURNAMENT_AUDIT = """
SELECT action, changed_fields, actor_user_id, actor_label, reason, reason_code, reason_note,
       tenant_club_id, entity, entity_id
FROM domain_change_log
WHERE entity = 'tournament' AND entity_id = :tournament_id
ORDER BY id
"""


@dataclass(frozen=True)
class WindowLab:  # pylint: disable=too-many-instance-attributes
    """Tenant con dos torneos propios (y un torneo ajeno) y las cuatro sesiones del arnes."""

    tenant: ClubId
    tournament: TournamentId
    other_tournament: TournamentId
    foreign_tournament: TournamentId
    competitor: CompetitorId
    owner_token: str
    collaborator_token: str
    outsider_token: str
    inactive_token: str


@asynccontextmanager
async def window_lab_context() -> AsyncIterator[WindowLab]:
    """Datos y sesiones del laboratorio de la ventana; la limpieza se apila de hojas a raices."""
    now = datetime_utc.now()
    suffix = uuid4().hex[:12]

    async with AsyncExitStack() as stack:
        club = await stack.enter_async_context(
            inserted_club(ClubInsertable(name=f"f3b-s34c-{suffix}", created=now))
        )
        other_club = await stack.enter_async_context(
            inserted_club(ClubInsertable(name=f"f3b-s34c-other-{suffix}", created=now))
        )
        tournaments = [
            await stack.enter_async_context(
                inserted_tournament(
                    DUMMY_TOURNAMENT.model_copy(
                        update={
                            "club_id": club.id,
                            "dashboard_endpoint": f"f3b-s34c-{suffix}-{letter}",
                        }
                    )
                )
            )
            for letter in ("a", "b")
        ]
        foreign_tournament = await stack.enter_async_context(
            inserted_tournament(
                DUMMY_TOURNAMENT.model_copy(
                    update={"club_id": other_club.id, "dashboard_endpoint": f"f3b-s34c-{suffix}-c"}
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

        competitor = await insert_competitor(f"competidora-s34c-{suffix}", club.id, created=now)
        stack.push_async_callback(delete_competitor, competitor)

        # Lo ultimo apilado se deshace primero: inscripciones y auditoria antes que el resto.
        stack.push_async_callback(
            delete_registration_audit,
            (tournaments[0].id, tournaments[1].id, foreign_tournament.id),
            club.id,
        )

        yield WindowLab(
            tenant=club.id,
            tournament=tournaments[0].id,
            other_tournament=tournaments[1].id,
            foreign_tournament=foreign_tournament.id,
            competitor=competitor,
            owner_token=get_mock_token(owner.email),
            collaborator_token=get_mock_token(collaborator.email),
            outsider_token=get_mock_token(outsider.email),
            inactive_token=get_mock_token(inactive.email),
        )


@pytest_asyncio.fixture(loop_scope="session", scope="function")
async def lab() -> AsyncIterator[WindowLab]:
    """Laboratorio por caso: datos propios, de modo que los recuentos son absolutos."""
    async with window_lab_context() as window_lab:
        yield window_lab


@pytest.fixture(scope="module", autouse=True)
def idempotency_secret() -> Iterator[None]:
    """Clave HMAC sintetica del laboratorio (las escrituras del arnes pasan por el protocolo)."""
    patcher = pytest.MonkeyPatch()
    patcher.setattr(config, "idempotency_hmac_key", SYNTHETIC_SECRET)
    patcher.setattr(config, "idempotency_hmac_key_version", "v1")
    yield
    patcher.undo()


# --- utilidades HTTP ---------------------------------------------------------------------------


def new_key() -> str:
    """Clave de idempotencia valida y unica por llamada."""
    return f"k-{uuid4().hex}"


def window_url(tournament_id: TournamentId) -> str:
    return f"tournaments/{tournament_id}/registration-window"


def close_url(tournament_id: TournamentId) -> str:
    return f"tournaments/{tournament_id}/registrations/close"


def registrations_url(tournament_id: TournamentId) -> str:
    return f"tournaments/{tournament_id}/registrations"


async def call(
    method: HTTPMethod,
    url: str,
    *,
    token: str | None = None,
    key: str | None = None,
    payload: bytes | None = None,
) -> tuple[int, dict[str, str], str]:
    """Peticion cruda: devuelve estado, cabeceras (en minusculas) y texto."""
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if key is not None:
        headers[IDEMPOTENCY_KEY_HEADER] = key
    if payload is not None:
        headers["Content-Type"] = JSON_CONTENT_TYPE
    async with aiohttp.ClientSession() as session:
        async with session.request(
            method=str(method.value),
            url=get_root_uvicorn_url() + url,
            data=payload,
            headers=headers,
        ) as response:
            return (
                response.status,
                {name.lower(): value for name, value in response.headers.items()},
                await response.text(),
            )


async def call_json(
    method: HTTPMethod,
    url: str,
    *,
    token: str | None = None,
    key: str | None = None,
    payload: bytes | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    status, headers, text = await call(method, url, token=token, key=key, payload=payload)
    return status, headers, (json.loads(text) if text else {})


def deadline_payload(value: Any, **overrides: object) -> bytes:
    body: dict[str, object] = {"registration_deadline": value}
    body.update(overrides)
    return json.dumps(body).encode()


def draft_payload(competitor_id: CompetitorId, category_key: str, **overrides: object) -> bytes:
    """Cuerpo de alta valido: competidor verificado del tenant, participacion independiente."""
    body: dict[str, object] = {
        "competitor_id": competitor_id,
        "identity_status": None,
        "representation": "INDEPENDENT",
        "sports_club_id": None,
        "affiliation_id": None,
        "category_key": category_key,
        "category_label": f"Etiqueta {category_key}",
        "competitor_name_snapshot": None,
    }
    body.update(overrides)
    return json.dumps(body).encode()


async def get_window(
    lab: WindowLab, *, tournament_id: TournamentId | None = None, token: str | None = None
) -> tuple[int, dict[str, str], dict[str, Any]]:
    return await call_json(
        HTTPMethod.GET,
        window_url(tournament_id or lab.tournament),
        token=token if token is not None else lab.owner_token,
    )


async def post_window(
    lab: WindowLab,
    payload: bytes,
    *,
    tournament_id: TournamentId | None = None,
    token: str | None = None,
    key: str | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    return await call_json(
        HTTPMethod.POST,
        window_url(tournament_id or lab.tournament),
        token=token if token is not None else lab.owner_token,
        key=key,
        payload=payload,
    )


async def close_registrations(
    lab: WindowLab,
    *,
    tournament_id: TournamentId | None = None,
    token: str | None = None,
    key: str | None = None,
    payload: bytes | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    return await call_json(
        HTTPMethod.POST,
        close_url(tournament_id or lab.tournament),
        token=token if token is not None else lab.owner_token,
        key=key,
        payload=payload if payload is not None else b"{}",
    )


async def create_registration(
    lab: WindowLab,
    category_key: str,
    *,
    tournament_id: TournamentId | None = None,
    token: str | None = None,
    key: str | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    return await call_json(
        HTTPMethod.POST,
        registrations_url(tournament_id or lab.tournament),
        token=token if token is not None else lab.owner_token,
        key=key if key is not None else new_key(),
        payload=draft_payload(lab.competitor, category_key),
    )


async def lifecycle(
    lab: WindowLab, registration_id: int, action: str, reason: str
) -> tuple[int, dict[str, str], dict[str, Any]]:
    return await call_json(
        HTTPMethod.POST,
        f"{registrations_url(lab.tournament)}/{registration_id}/{action}",
        token=lab.owner_token,
        key=new_key(),
        payload=json.dumps({"reason_code": reason}).encode(),
    )


async def tournament_audit(tournament_id: TournamentId) -> list[dict[str, Any]]:
    """Eventos de auditoria del torneo, en orden de escritura."""
    records = await database.fetch_all(
        query=SELECT_TOURNAMENT_AUDIT, values={"tournament_id": tournament_id}
    )
    return [dict(record._mapping) for record in records]


def parse_instant(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def published_date(response_body: dict[str, Any], field: str) -> datetime | None:
    value = response_body[field]
    return None if value is None else parse_instant(str(value))


# --- lectura -----------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_get_window_returns_the_public_projection_when_nothing_is_configured(
    lab: WindowLab,
) -> None:
    status, _, body = await get_window(lab)

    assert status == 200, body
    assert set(body) == PUBLIC_WINDOW_FIELDS
    assert body["tournament_id"] == lab.tournament
    assert body["registration_deadline"] is None
    assert body["registrations_closed_at"] is None
    assert body["state"] == "OPEN"
    assert body["registrations_open"] is True


@pytest.mark.asyncio(loop_scope="session")
async def test_get_window_requires_a_session(lab: WindowLab) -> None:
    status, _, body = await call_json(HTTPMethod.GET, window_url(lab.tournament))

    assert status == 401, body
    assert body == NOT_AUTHENTICATED


@pytest.mark.asyncio(loop_scope="session")
async def test_inactive_and_unknown_sessions_are_indistinguishable(lab: WindowLab) -> None:
    inactive_status, _, inactive_body = await get_window(lab, token=lab.inactive_token)
    unknown_status, _, unknown_body = await get_window(
        lab, token=get_mock_token("nadie@example.org")
    )

    assert inactive_status == unknown_status == 401
    assert inactive_body == unknown_body == UNAUTHORIZED


@pytest.mark.asyncio(loop_scope="session")
async def test_foreign_and_missing_tournament_windows_are_the_same_404(lab: WindowLab) -> None:
    foreign_status, _, foreign_body = await get_window(lab, tournament_id=lab.foreign_tournament)
    missing_status, _, missing_body = await get_window(lab, tournament_id=TournamentId(10**9))

    assert foreign_status == missing_status == 404
    assert foreign_body == missing_body == NOT_FOUND
    assert not any(character.isdigit() for character in str(foreign_body))


@pytest.mark.asyncio(loop_scope="session")
async def test_owner_and_collaborator_can_read_the_window(lab: WindowLab) -> None:
    for token in (lab.owner_token, lab.collaborator_token):
        status, _, body = await get_window(lab, token=token)
        assert status == 200, body
        assert set(body) == PUBLIC_WINDOW_FIELDS


@pytest.mark.asyncio(loop_scope="session")
async def test_reading_the_window_writes_no_audit_and_needs_no_key(lab: WindowLab) -> None:
    await get_window(lab)
    await get_window(lab, token=lab.collaborator_token)

    assert await tournament_audit(lab.tournament) == []


@pytest.mark.asyncio(loop_scope="session")
async def test_the_window_response_never_exposes_internal_fields(lab: WindowLab) -> None:
    await close_registrations(lab, key=new_key())

    status, _, body = await get_window(lab)

    assert status == 200, body
    assert set(body).isdisjoint(INTERNAL_WINDOW_FIELDS)


# --- configuracion del plazo -------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_owner_can_set_a_future_deadline_and_the_window_stays_open(lab: WindowLab) -> None:
    deadline = (datetime_utc.now() + timedelta(days=30)).replace(microsecond=0)

    status, _, body = await post_window(lab, deadline_payload(deadline.isoformat()), key=new_key())

    assert status == 200, body
    assert set(body) == PUBLIC_WINDOW_FIELDS
    assert published_date(body, "registration_deadline") == deadline
    assert body["state"] == "OPEN" and body["registrations_open"] is True

    _, _, reread = await get_window(lab)
    assert published_date(reread, "registration_deadline") == deadline


@pytest.mark.asyncio(loop_scope="session")
async def test_the_deadline_lands_at_the_platform_timestamp_resolution(lab: WindowLab) -> None:
    """El plazo se guarda sin fraccion: es la resolucion con la que el proyecto lee instantes.

    El codec de ``bracket.database`` descarta la fraccion al leer, asi que un plazo con
    microsegundos no podria volver a compararse consigo mismo: configurar dos veces el mismo
    plazo volveria a auditar cada vez.
    """
    deadline = (datetime_utc.now() + timedelta(days=30)).replace(microsecond=123456)

    status, _, body = await post_window(lab, deadline_payload(deadline.isoformat()), key=new_key())

    assert status == 200, body
    assert published_date(body, "registration_deadline") == deadline.replace(microsecond=0)


@pytest.mark.asyncio(loop_scope="session")
async def test_the_deadline_is_stored_as_an_instant_whatever_the_offset(lab: WindowLab) -> None:
    """El mismo instante escrito con otro desplazamiento horario se guarda y se devuelve igual."""
    deadline = (datetime_utc.now() + timedelta(days=7)).replace(microsecond=0)
    # El **mismo instante** con otra hora local (no otro instante): 19:00+02:00 es 17:00Z.
    other_offset = datetime.fromtimestamp(deadline.timestamp(), tz=timezone(timedelta(hours=2)))
    assert other_offset.utcoffset() == timedelta(hours=2)
    assert other_offset.isoformat() != deadline.isoformat()

    status, _, body = await post_window(
        lab, deadline_payload(other_offset.isoformat()), key=new_key()
    )

    assert status == 200, body
    published = published_date(body, "registration_deadline")
    assert published is not None
    assert published.timestamp() == deadline.timestamp()


@pytest.mark.asyncio(loop_scope="session")
async def test_owner_can_clear_the_deadline(lab: WindowLab) -> None:
    await post_window(
        lab, deadline_payload((datetime_utc.now() + timedelta(days=1)).isoformat()), key=new_key()
    )

    status, _, body = await post_window(lab, deadline_payload(None), key=new_key())

    assert status == 200, body
    assert body["registration_deadline"] is None
    assert body["state"] == "OPEN"


@pytest.mark.asyncio(loop_scope="session")
async def test_setting_a_past_deadline_closes_the_window_derived_from_the_clock(
    lab: WindowLab,
) -> None:
    past = datetime_utc.now() - timedelta(minutes=5)

    status, _, body = await post_window(lab, deadline_payload(past.isoformat()), key=new_key())

    assert status == 200, body
    assert body["state"] == "DEADLINE_PASSED"
    assert body["registrations_open"] is False


@pytest.mark.asyncio(loop_scope="session")
async def test_an_empty_deadline_body_rejects_unknown_fields(lab: WindowLab) -> None:
    status, _, body = await post_window(
        lab, deadline_payload(None, registrations_closed_at="2026-01-01T00:00:00Z"), key=new_key()
    )

    assert status == 422, body


@pytest.mark.parametrize(
    "spoofed",
    [
        {"actor_user_id": 1},
        {"actor_label": "otro"},
        {"tenant_club_id": 3},
        {"closed_by_user_id": 7},
        {"registrations_closed_at": "2026-01-01T00:00:00Z"},
    ],
)
@pytest.mark.asyncio(loop_scope="session")
async def test_setting_the_deadline_rejects_identity_and_state_spoofing(
    lab: WindowLab, spoofed: dict[str, object]
) -> None:
    """El cuerpo no puede traer identidad, tenant ni el estado de cierre."""
    status, _, _ = await post_window(lab, deadline_payload(None, **spoofed), key=new_key())

    assert status == 422


@pytest.mark.asyncio(loop_scope="session")
async def test_setting_the_deadline_requires_owner(lab: WindowLab) -> None:
    deadline = datetime_utc.now() + timedelta(days=3)

    status, _, body = await post_window(
        lab, deadline_payload(deadline.isoformat()), token=lab.collaborator_token, key=new_key()
    )

    assert status == 403, body
    assert body == ROLE_NOT_ALLOWED
    _, _, window = await get_window(lab)
    assert window["registration_deadline"] is None


@pytest.mark.asyncio(loop_scope="session")
async def test_setting_the_deadline_requires_an_idempotency_key(lab: WindowLab) -> None:
    deadline = datetime_utc.now() + timedelta(days=3)

    status, _, body = await post_window(lab, deadline_payload(deadline.isoformat()))

    assert status == 400, body
    assert body == IDEMPOTENCY_REQUEST_INVALID
    _, _, window = await get_window(lab)
    assert window["registration_deadline"] is None


@pytest.mark.asyncio(loop_scope="session")
async def test_a_foreign_tenant_cannot_set_the_deadline(lab: WindowLab) -> None:
    deadline = datetime_utc.now() + timedelta(days=3)

    status, _, body = await post_window(
        lab,
        deadline_payload(deadline.isoformat()),
        tournament_id=lab.foreign_tournament,
        key=new_key(),
    )

    assert status == 404, body
    assert body == NOT_FOUND


@pytest.mark.asyncio(loop_scope="session")
async def test_an_inactive_actor_cannot_set_the_deadline(lab: WindowLab) -> None:
    deadline = datetime_utc.now() + timedelta(days=3)

    status, _, body = await post_window(
        lab, deadline_payload(deadline.isoformat()), token=lab.inactive_token, key=new_key()
    )

    assert status == 401, body
    assert body == UNAUTHORIZED


@pytest.mark.asyncio(loop_scope="session")
async def test_setting_the_deadline_writes_one_audit_event_with_field_names_only(
    lab: WindowLab,
) -> None:
    deadline = datetime_utc.now() + timedelta(days=9)

    await post_window(lab, deadline_payload(deadline.isoformat()), key=new_key())

    events = await tournament_audit(lab.tournament)
    assert len(events) == 1, events
    event = events[0]
    assert event["action"] == "SET_REGISTRATION_WINDOW"
    assert event["changed_fields"] == ["registration_deadline"]
    assert event["tenant_club_id"] == lab.tenant
    assert event["entity"] == "tournament" and event["entity_id"] == lab.tournament
    assert event["reason_code"] is None, "el catalogo no se amplia en esta fase"
    assert event["reason_note"] is None
    assert str(deadline.year) not in str(event["reason"]), "la auditoria no guarda valores"


@pytest.mark.asyncio(loop_scope="session")
async def test_setting_the_same_deadline_again_writes_no_second_event(lab: WindowLab) -> None:
    """El mismo plazo declarado dos veces (con fraccion) no reaudita ni cambia el estado.

    El plazo se normaliza a la resolucion temporal del proyecto (segundo): sin normalizar, la
    fraccion que el codec descarta al leer haria que cada POST pareciese un plazo distinto y se
    anotase un evento falso por repeticion.
    """
    deadline = (datetime_utc.now() + timedelta(days=9)).replace(microsecond=654321)

    first_status, _, first_body = await post_window(
        lab, deadline_payload(deadline.isoformat()), key=new_key()
    )
    status, _, body = await post_window(lab, deadline_payload(deadline.isoformat()), key=new_key())

    assert first_status == 200, first_body
    assert status == 200, body
    assert published_date(body, "registration_deadline") == deadline.replace(microsecond=0)
    assert body["state"] == first_body["state"] == "OPEN", "la repeticion no cambia la ventana"
    assert len(await tournament_audit(lab.tournament)) == 1, "un solo evento: repetir no audita"


@pytest.mark.asyncio(loop_scope="session")
async def test_clearing_the_deadline_writes_its_own_event_and_keeps_the_history(
    lab: WindowLab,
) -> None:
    deadline = datetime_utc.now() + timedelta(days=4)
    await post_window(lab, deadline_payload(deadline.isoformat()), key=new_key())

    await post_window(lab, deadline_payload(None), key=new_key())

    events = await tournament_audit(lab.tournament)
    assert [event["changed_fields"] for event in events] == [
        ["registration_deadline"],
        ["registration_deadline"],
    ]


# --- cierre manual -----------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_owner_can_close_registrations_and_reads_it_back_closed(lab: WindowLab) -> None:
    status, _, body = await close_registrations(lab, key=new_key())

    assert status == 200, body
    assert set(body) == PUBLIC_WINDOW_FIELDS
    assert body["state"] == "CLOSED"
    assert body["registrations_open"] is False
    assert published_date(body, "registrations_closed_at") is not None

    _, _, reread = await get_window(lab)
    assert reread["state"] == "CLOSED"


@pytest.mark.asyncio(loop_scope="session")
async def test_closing_without_a_deadline_closes_the_derived_state(lab: WindowLab) -> None:
    """Sin plazo, el unico motivo del cierre es el cierre manual."""
    _, _, body = await close_registrations(lab, key=new_key())

    assert body["registration_deadline"] is None
    assert body["state"] == "CLOSED"


@pytest.mark.asyncio(loop_scope="session")
async def test_collaborator_cannot_close_registrations(lab: WindowLab) -> None:
    status, _, body = await close_registrations(lab, token=lab.collaborator_token, key=new_key())

    assert status == 403, body
    assert body == ROLE_NOT_ALLOWED
    _, _, window = await get_window(lab)
    assert window["state"] == "OPEN"


@pytest.mark.asyncio(loop_scope="session")
async def test_closing_requires_an_idempotency_key(lab: WindowLab) -> None:
    status, _, body = await close_registrations(lab)

    assert status == 400, body
    assert body == IDEMPOTENCY_REQUEST_INVALID


@pytest.mark.asyncio(loop_scope="session")
async def test_closing_requires_owner_and_not_just_a_valid_session(lab: WindowLab) -> None:
    status, _, body = await close_registrations(lab, token=lab.outsider_token, key=new_key())

    assert status == 404, body
    assert body == NOT_FOUND


@pytest.mark.asyncio(loop_scope="session")
async def test_an_inactive_actor_cannot_close_registrations(lab: WindowLab) -> None:
    status, _, body = await close_registrations(lab, token=lab.inactive_token, key=new_key())

    assert status == 401, body
    assert body == UNAUTHORIZED


@pytest.mark.asyncio(loop_scope="session")
async def test_closing_rejects_fields_outside_the_whitelist(lab: WindowLab) -> None:
    status, _, body = await close_registrations(
        lab, key=new_key(), payload=json.dumps({"actor_label": "otro"}).encode()
    )

    assert status == 422, body


@pytest.mark.asyncio(loop_scope="session")
async def test_closing_writes_one_audit_event_with_field_names_only(lab: WindowLab) -> None:
    await close_registrations(lab, key=new_key())

    events = await tournament_audit(lab.tournament)
    assert len(events) == 1, events
    event = events[0]
    assert event["action"] == "CLOSE_REGISTRATIONS"
    assert event["changed_fields"] == ["registrations_closed_at"]
    assert event["tenant_club_id"] == lab.tenant
    assert event["reason_code"] is None and event["reason_note"] is None
    assert event["actor_user_id"] is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_closing_twice_with_the_same_key_replays_the_same_response(lab: WindowLab) -> None:
    key = new_key()

    first_status, _, first_body = await close_registrations(lab, key=key)
    second_status, _, second_body = await close_registrations(lab, key=key)

    assert first_status == second_status == 200
    assert first_body == second_body
    assert len(await tournament_audit(lab.tournament)) == 1, "el replay no escribe otra vez"


@pytest.mark.asyncio(loop_scope="session")
async def test_closing_again_with_another_key_is_a_state_conflict_without_double_audit(
    lab: WindowLab,
) -> None:
    await close_registrations(lab, key=new_key())

    status, _, body = await close_registrations(lab, key=new_key())

    assert status == 409, body
    assert body == REGISTRATION_ALREADY_CLOSED
    assert len(await tournament_audit(lab.tournament)) == 1, "un unico cierre auditado"


@pytest.mark.asyncio(loop_scope="session")
async def test_closing_a_foreign_tournament_is_a_404(lab: WindowLab) -> None:
    status, _, body = await close_registrations(
        lab, tournament_id=lab.foreign_tournament, key=new_key()
    )

    assert status == 404, body
    assert body == NOT_FOUND


@pytest.mark.asyncio(loop_scope="session")
async def test_the_window_is_per_tournament(lab: WindowLab) -> None:
    await close_registrations(lab, key=new_key())
    await post_window(
        lab,
        deadline_payload((datetime_utc.now() - timedelta(minutes=1)).isoformat()),
        tournament_id=lab.other_tournament,
        key=new_key(),
    )

    _, _, closed = await get_window(lab)
    _, _, deadline_passed = await get_window(lab, tournament_id=lab.other_tournament)

    assert closed["state"] == "CLOSED"
    assert deadline_passed["state"] == "DEADLINE_PASSED"


# --- el alta mira la ventana -------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_create_is_allowed_while_the_window_is_open(lab: WindowLab) -> None:
    status, _, body = await create_registration(lab, "cat-1")

    assert status == 201, body


@pytest.mark.asyncio(loop_scope="session")
async def test_create_is_rejected_with_a_stable_code_when_manually_closed(lab: WindowLab) -> None:
    await close_registrations(lab, key=new_key())

    status, _, body = await create_registration(lab, "cat-1")

    assert status == 409, body
    assert body == REGISTRATION_CLOSED


@pytest.mark.asyncio(loop_scope="session")
async def test_create_is_rejected_with_a_stable_code_when_the_deadline_passed(
    lab: WindowLab,
) -> None:
    await post_window(
        lab,
        deadline_payload((datetime_utc.now() - timedelta(minutes=1)).isoformat()),
        key=new_key(),
    )

    status, _, body = await create_registration(lab, "cat-1")

    assert status == 409, body
    assert body == REGISTRATION_CLOSED


@pytest.mark.asyncio(loop_scope="session")
async def test_a_rejected_create_leaves_no_registration_and_no_audit_of_its_own(
    lab: WindowLab,
) -> None:
    await close_registrations(lab, key=new_key())

    await create_registration(lab, "cat-1")

    status, _, listing = await call_json(
        HTTPMethod.GET, registrations_url(lab.tournament), token=lab.owner_token
    )
    assert status == 200, listing
    assert listing["count"] == 0
    assert [event["action"] for event in await tournament_audit(lab.tournament)] == [
        "CLOSE_REGISTRATIONS"
    ]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_collaborator_cannot_create_either_once_closed(lab: WindowLab) -> None:
    await close_registrations(lab, key=new_key())

    status, _, body = await create_registration(lab, "cat-1", token=lab.collaborator_token)

    assert status == 409, body
    assert body == REGISTRATION_CLOSED


@pytest.mark.asyncio(loop_scope="session")
async def test_closing_does_not_block_the_lifecycle_of_existing_registrations(
    lab: WindowLab,
) -> None:
    """Decision explicita: la ventana gobierna el **alta**, no el ciclo de vida ya abierto.

    Confirmar, retirar, readmitir y descalificar siguen funcionando con la ventana cerrada: la
    ventana no es un estado de la inscripcion ni un candado de la competicion.
    """
    status, _, created = await create_registration(lab, "cat-1")
    assert status == 201, created
    registration_id = int(created["id"])

    await close_registrations(lab, key=new_key())

    confirm_status, _, _ = await lifecycle(lab, registration_id, "confirm", "READY")
    withdraw_status, _, _ = await lifecycle(lab, registration_id, "withdraw", "WITHDRAWAL_REQUEST")
    reinstate_status, _, _ = await lifecycle(
        lab, registration_id, "reinstate", "MISTAKEN_WITHDRAWAL"
    )
    disqualify_status, _, _ = await lifecycle(lab, registration_id, "disqualify", "ADMINISTRATIVE")

    assert confirm_status == 200
    assert withdraw_status == 200
    assert reinstate_status == 200
    assert disqualify_status == 200
    assert await tournament_audit(lab.tournament) != [], "la auditoria del ciclo de vida sigue"
