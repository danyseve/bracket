# pylint: disable=redefined-outer-name
"""Contrato HTTP de las rutas F3 de inscripciones (S3.4a, LAB ONLY).

Pruebas **negras** contra la aplicacion del producto (``bracket/app.py``), que en esta fase ya
monta el router ``bracket/routes/domain_registrations.py``: se levanta el servidor de pruebas real
y se llama por HTTP con una sesion del producto (``Bearer`` emitido por el mismo camino que en
produccion).

Lo que se demuestra aqui, y contra que capa:

* el ciclo de vida completo (alta, confirmacion, retirada, readmision y descalificacion) responde
  con la proyeccion publica y sin campos internos;
* identidad y tenant salen del JWT y del torneo de la ruta: el cuerpo no puede aportarlos (se
  rechaza, no se ignora) y el torneo ajeno no se distingue del inexistente;
* toda escritura pasa por el protocolo de idempotencia certificado (S3.3c-4): replay, conflicto de
  huella, ejecucion unica, ``Retry-After`` y revalidacion de autorizacion antes del replay;
* el motivo de auditoria es el vocabulario cerrado ``reason_code``/``reason_note``.

Solo usa la base de CI (``bracket_ci``): no hay mocks del protocolo ni del dominio. Cada caso
atraviesa la ruta, la dependencia de contexto, el protocolo, la operacion de dominio y PostgreSQL.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import aiohttp
import pytest
import pytest_asyncio
from heliclockter import datetime_utc

from bracket.config import config
from bracket.database import database
from bracket.logic.idempotency_protocol import RETRY_AFTER_SECONDS
from bracket.models.db.club import ClubInsertable
from bracket.models.db.user_x_club import UserXClubInsertable, UserXClubRelation
from bracket.routes.domain_idempotency import IDEMPOTENCY_KEY_HEADER
from bracket.sql.idempotency import sql_insert_idempotency_reservation
from bracket.sql.users import update_user_active
from bracket.utils.dummy_records import DUMMY_TOURNAMENT
from bracket.utils.http import HTTPMethod
from bracket.utils.id_types import (
    ClubId,
    CompetitorId,
    CompetitorXSportsClubId,
    SportsClubId,
    TournamentId,
    TournamentRegistrationId,
    UserId,
)
from tests.integration_tests.api.shared import get_root_uvicorn_url
from tests.integration_tests.idempotency_fixtures import lab_reservation
from tests.integration_tests.mocks import get_mock_token, get_mock_user
from tests.integration_tests.registration_fixtures import (
    audit_rows,
    count_current_registrations,
    delete_affiliations,
    delete_competitor,
    delete_registration_audit,
    delete_sports_club,
    insert_affiliation,
    insert_competitor,
    insert_sports_club,
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

#: Lista blanca de la respuesta: cualquier campo nuevo tiene que ser una decision explicita.
PUBLIC_REGISTRATION_FIELDS = frozenset(
    {
        "id",
        "tournament_id",
        "competitor_id",
        "identity_status",
        "representation",
        "sports_club_id",
        "category_key",
        "category_label",
        "competitor_name_snapshot",
        "sports_club_name_snapshot",
        "status",
        "revision",
        "created",
        "updated_at",
    }
)


@dataclass(frozen=True)
class HttpLab:  # pylint: disable=too-many-instance-attributes
    """Tenant con torneo propio, torneo ajeno y sesiones reales (OWNER, COLLABORATOR, ajenos).

    Arnes de test: agrupa las 13 piezas del laboratorio en un unico objeto inmutable.
    """

    tenant: ClubId
    tournament: TournamentId
    other_tournament: TournamentId
    foreign_tournament: TournamentId
    sports_club: SportsClubId
    competitor: CompetitorId
    affiliation: CompetitorXSportsClubId
    owner_id: UserId
    owner_token: str
    collaborator_id: UserId
    collaborator_token: str
    outsider_token: str
    inactive_token: str


@asynccontextmanager
async def http_lab_context() -> AsyncIterator[HttpLab]:
    """Datos y sesiones del laboratorio HTTP; la limpieza se apila de hojas a raices."""
    now = datetime_utc.now()
    suffix = uuid4().hex[:12]

    async with AsyncExitStack() as stack:
        club = await stack.enter_async_context(
            inserted_club(ClubInsertable(name=f"f3b-s34a-{suffix}", created=now))
        )
        other_club = await stack.enter_async_context(
            inserted_club(ClubInsertable(name=f"f3b-s34a-other-{suffix}", created=now))
        )
        tournament = await stack.enter_async_context(
            inserted_tournament(
                DUMMY_TOURNAMENT.model_copy(
                    update={"club_id": club.id, "dashboard_endpoint": f"f3b-s34a-{suffix}-a"}
                )
            )
        )
        other_tournament = await stack.enter_async_context(
            inserted_tournament(
                DUMMY_TOURNAMENT.model_copy(
                    update={"club_id": club.id, "dashboard_endpoint": f"f3b-s34a-{suffix}-b"}
                )
            )
        )
        foreign_tournament = await stack.enter_async_context(
            inserted_tournament(
                DUMMY_TOURNAMENT.model_copy(
                    update={"club_id": other_club.id, "dashboard_endpoint": f"f3b-s34a-{suffix}-c"}
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

        sports_club = await insert_sports_club(
            f"Academia {suffix}", club.id, active=True, created=now
        )
        stack.push_async_callback(delete_sports_club, sports_club)

        competitor = await insert_competitor(f"Competidora {suffix}", club.id, created=now)
        stack.push_async_callback(delete_competitor, competitor)

        affiliation = await insert_affiliation(
            competitor_id=competitor,
            sports_club_id=sports_club,
            is_primary=True,
            created=now,
        )
        stack.push_async_callback(delete_affiliations, competitor)

        # Lo ultimo apilado se deshace primero: inscripciones y auditoria antes que el resto.
        stack.push_async_callback(
            delete_registration_audit, (tournament.id, other_tournament.id), club.id
        )

        yield HttpLab(
            tenant=club.id,
            tournament=tournament.id,
            other_tournament=other_tournament.id,
            foreign_tournament=foreign_tournament.id,
            sports_club=sports_club,
            competitor=competitor,
            affiliation=affiliation,
            owner_id=owner.id,
            owner_token=get_mock_token(owner.email),
            collaborator_id=collaborator.id,
            collaborator_token=get_mock_token(collaborator.email),
            outsider_token=get_mock_token(outsider.email),
            inactive_token=get_mock_token(inactive.email),
        )


@pytest_asyncio.fixture(loop_scope="session", scope="function")
async def lab() -> AsyncIterator[HttpLab]:
    """Laboratorio por caso: tenant, torneos y sesiones propios, asi los recuentos son absolutos."""
    async with http_lab_context() as http_lab:
        yield http_lab


@pytest.fixture(scope="module", autouse=True)
def idempotency_secret() -> Iterator[None]:
    """Clave HMAC sintetica del laboratorio (el protocolo falla cerrado sin ella)."""
    patcher = pytest.MonkeyPatch()
    patcher.setattr(config, "idempotency_hmac_key", SYNTHETIC_SECRET)
    patcher.setattr(config, "idempotency_hmac_key_version", "v1")
    yield
    patcher.undo()


# --- utilidades HTTP ---------------------------------------------------------------------------


def new_key() -> str:
    """Clave de idempotencia valida y unica por llamada (min. 8, alfabeto permitido)."""
    return f"k-{uuid4().hex}"


def create_url(tournament_id: TournamentId) -> str:
    return f"tournaments/{tournament_id}/registrations"


def create_path(tournament_id: TournamentId) -> str:
    """Ruta tal como la ve el servidor (con barra inicial): asi se calcula la huella."""
    return f"/tournaments/{tournament_id}/registrations"


def lifecycle_url(tournament_id: TournamentId, registration_id: int, action: str) -> str:
    return f"tournaments/{tournament_id}/registrations/{registration_id}/{action}"


async def call(
    method: HTTPMethod,
    url: str,
    *,
    token: str | None = None,
    key: str | None = None,
    payload: bytes | None = None,
) -> tuple[int, dict[str, str], str]:
    """Peticion cruda: devuelve estado, cabeceras (en minusculas) y texto.

    El cuerpo se envia **tal cual** (bytes) y no como ``json=`` para que la huella del protocolo
    sea reproducible en las pruebas; las cabeceras se devuelven para poder mirar ``retry-after``.
    """
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


def draft_payload(lab: HttpLab, **overrides: object) -> bytes:
    """Cuerpo de alta valido: competidor verificado del tenant, participacion independiente."""
    body: dict[str, object] = {
        "competitor_id": lab.competitor,
        "identity_status": None,
        "representation": "INDEPENDENT",
        "sports_club_id": None,
        "affiliation_id": None,
        "category_key": f"cat-{uuid4().hex[:10]}",
        "category_label": "Categoria de prueba",
        "competitor_name_snapshot": None,
    }
    body.update(overrides)
    return json.dumps(body).encode()


def reason_payload(reason_code: str | None = None, reason_note: str | None = None) -> bytes:
    body: dict[str, object] = {}
    if reason_code is not None:
        body["reason_code"] = reason_code
    if reason_note is not None:
        body["reason_note"] = reason_note
    return json.dumps(body).encode()


async def create_registration(
    lab: HttpLab,
    *,
    token: str | None = None,
    key: str | None = None,
    payload: bytes | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    return await call_json(
        HTTPMethod.POST,
        create_url(lab.tournament),
        token=token or lab.owner_token,
        key=key or new_key(),
        payload=payload or draft_payload(lab),
    )


async def confirm_registration(
    lab: HttpLab, registration_id: int
) -> tuple[int, dict[str, str], dict[str, Any]]:
    return await call_json(
        HTTPMethod.POST,
        lifecycle_url(lab.tournament, registration_id, "confirm"),
        token=lab.owner_token,
        key=new_key(),
        payload=reason_payload("READY"),
    )


async def create_and_confirm(lab: HttpLab) -> TournamentRegistrationId:
    assert (created := await create_registration(lab))[0] == 201
    registration_id = TournamentRegistrationId(int(created[2]["id"]))
    assert (confirmed := await confirm_registration(lab, registration_id))[0] == 200
    assert confirmed[2]["status"] == "CONFIRMED"
    return registration_id


async def audit_actions(registration_id: TournamentRegistrationId) -> list[str]:
    return [str(row["action"]) for row in await audit_rows(registration_id)]


async def count_registrations(lab: HttpLab, tournament_id: TournamentId | None = None) -> int:
    return await count_current_registrations(
        tournament_id=tournament_id or lab.tournament, competitor_id=lab.competitor
    )


# --- 1. ciclo de vida ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_create_returns_a_draft_with_a_public_projection(lab: HttpLab) -> None:
    key = new_key()
    status, headers, body = await create_registration(lab, key=key)

    assert status == 201
    assert headers["content-type"].startswith(JSON_CONTENT_TYPE)
    assert set(body) == PUBLIC_REGISTRATION_FIELDS
    assert body["status"] == "DRAFT"
    assert body["tournament_id"] == lab.tournament
    assert body["competitor_id"] == lab.competitor
    assert body["identity_status"] == "VERIFIED"
    assert body["representation"] == "INDEPENDENT"
    assert body["sports_club_id"] is None
    assert body["revision"] == 1
    assert body["updated_at"] is None
    # ni la clave de idempotencia ni el actor viajan al cliente
    assert key not in json.dumps(body)
    assert await count_registrations(lab) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_create_with_a_represented_academy_freezes_its_name(lab: HttpLab) -> None:
    status, _, body = await create_registration(
        lab,
        payload=draft_payload(
            lab,
            representation="CLUB",
            sports_club_id=lab.sports_club,
            affiliation_id=lab.affiliation,
        ),
    )

    assert status == 201
    assert body["representation"] == "CLUB"
    assert body["sports_club_id"] == lab.sports_club
    assert body["sports_club_name_snapshot"] is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_confirm_turns_the_draft_into_confirmed(lab: HttpLab) -> None:
    assert (created := await create_registration(lab))[0] == 201
    registration_id = TournamentRegistrationId(int(created[2]["id"]))

    status, _, body = await confirm_registration(lab, registration_id)

    assert status == 200
    assert body["status"] == "CONFIRMED"
    # la confirmacion no reescribe el borrador: la revision nace en el alta y el ciclo no la toca
    assert body["revision"] == 1
    assert await audit_actions(registration_id) == ["CREATE", "CONFIRM"]
    rows = await audit_rows(registration_id)
    assert rows[0]["reason_code"] == "PLANNED_ENTRY"
    assert rows[1]["reason_code"] == "READY"
    assert rows[1]["actor_user_id"] == lab.owner_id


@pytest.mark.asyncio(loop_scope="session")
async def test_withdraw_turns_the_draft_into_withdrawn(lab: HttpLab) -> None:
    assert (created := await create_registration(lab))[0] == 201
    registration_id = TournamentRegistrationId(int(created[2]["id"]))

    status, _, body = await call_json(
        HTTPMethod.POST,
        lifecycle_url(lab.tournament, registration_id, "withdraw"),
        token=lab.owner_token,
        key=new_key(),
        payload=reason_payload("WITHDRAWAL_REQUEST"),
    )

    assert status == 200
    assert body["status"] == "WITHDRAWN"
    assert await audit_actions(registration_id) == ["CREATE", "WITHDRAW"]


@pytest.mark.asyncio(loop_scope="session")
async def test_reinstate_confirms_the_withdrawn_registration(lab: HttpLab) -> None:
    assert (created := await create_registration(lab))[0] == 201
    registration_id = TournamentRegistrationId(int(created[2]["id"]))
    for action, reason in (("withdraw", "DUPLICATE"), ("reinstate", "MISTAKEN_WITHDRAWAL")):
        status, _, body = await call_json(
            HTTPMethod.POST,
            lifecycle_url(lab.tournament, registration_id, action),
            token=lab.owner_token,
            key=new_key(),
            payload=reason_payload(reason),
        )
        assert status == 200

    assert body["status"] == "CONFIRMED"
    assert await audit_actions(registration_id) == ["CREATE", "WITHDRAW", "REINSTATE"]


@pytest.mark.asyncio(loop_scope="session")
async def test_disqualify_records_the_closed_reason(lab: HttpLab) -> None:
    registration_id = await create_and_confirm(lab)

    status, _, body = await call_json(
        HTTPMethod.POST,
        lifecycle_url(lab.tournament, registration_id, "disqualify"),
        token=lab.owner_token,
        key=new_key(),
        payload=reason_payload("RULE_VIOLATION", "hecho probado en la mesa"),
    )

    assert status == 200
    assert body["status"] == "DISQUALIFIED"
    rows = await audit_rows(registration_id)
    assert rows[-1]["action"] == "DISQUALIFY"
    assert rows[-1]["reason_code"] == "RULE_VIOLATION"
    assert rows[-1]["reason_note"] == "hecho probado en la mesa"


@pytest.mark.asyncio(loop_scope="session")
async def test_transition_from_a_wrong_state_is_409(lab: HttpLab) -> None:
    assert (created := await create_registration(lab))[0] == 201
    registration_id = TournamentRegistrationId(int(created[2]["id"]))

    status, _, body = await call_json(
        HTTPMethod.POST,
        lifecycle_url(lab.tournament, registration_id, "disqualify"),
        token=lab.owner_token,
        key=new_key(),
        payload=reason_payload("RULE_VIOLATION", "un borrador no se descalifica"),
    )

    assert status == 409
    assert body == {"detail": "Registration state does not allow this operation"}
    assert await audit_actions(registration_id) == ["CREATE"]


# --- 2. identidad, tenant y rol -----------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_all_endpoints_require_authentication(lab: HttpLab) -> None:
    assert (created := await create_registration(lab))[0] == 201
    registration_id = TournamentRegistrationId(int(created[2]["id"]))
    urls = [create_url(lab.tournament)] + [
        lifecycle_url(lab.tournament, registration_id, action)
        for action in ("confirm", "withdraw", "reinstate", "disqualify")
    ]

    for url in urls:
        status, _, body = await call_json(
            HTTPMethod.POST, url, key=new_key(), payload=reason_payload("READY")
        )
        assert status == 401
        assert body == NOT_AUTHENTICATED


@pytest.mark.asyncio(loop_scope="session")
async def test_inactive_actor_is_rejected(lab: HttpLab) -> None:
    status, _, body = await create_registration(lab, token=lab.inactive_token)

    assert status == 401
    assert body == UNAUTHORIZED
    assert await count_registrations(lab) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_foreign_tournament_is_404(lab: HttpLab) -> None:
    """Un torneo de otro tenant es indistinguible de uno inexistente (404 del contrato S3.3b)."""
    status, _, body = await call_json(
        HTTPMethod.POST,
        create_url(lab.foreign_tournament),
        token=lab.owner_token,
        key=new_key(),
        payload=draft_payload(lab),
    )

    assert status == 404
    assert body == NOT_FOUND
    assert await count_registrations(lab) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_registration_of_another_tournament_is_404(lab: HttpLab) -> None:
    """La ruta no puede mentir sobre el torneo: la inscripcion tiene que ser de ese torneo."""
    assert (created := await create_registration(lab))[0] == 201
    registration_id = TournamentRegistrationId(int(created[2]["id"]))

    status, _, body = await call_json(
        HTTPMethod.POST,
        lifecycle_url(lab.other_tournament, registration_id, "confirm"),
        token=lab.owner_token,
        key=new_key(),
        payload=reason_payload("READY"),
    )

    assert status == 404
    assert body == NOT_FOUND
    assert await audit_actions(registration_id) == ["CREATE"]


@pytest.mark.asyncio(loop_scope="session")
async def test_collaborator_cannot_disqualify(lab: HttpLab) -> None:
    registration_id = await create_and_confirm(lab)

    status, _, body = await call_json(
        HTTPMethod.POST,
        lifecycle_url(lab.tournament, registration_id, "disqualify"),
        token=lab.collaborator_token,
        key=new_key(),
        payload=reason_payload("RULE_VIOLATION", "un colaborador no descalifica"),
    )

    assert status == 403
    assert body == {"detail": "ROLE_NOT_ALLOWED"}
    assert await audit_actions(registration_id) == ["CREATE", "CONFIRM"]


@pytest.mark.asyncio(loop_scope="session")
async def test_collaborator_can_create_and_withdraw_a_draft(lab: HttpLab) -> None:
    assert (created := await create_registration(lab, token=lab.collaborator_token))[0] == 201
    registration_id = TournamentRegistrationId(int(created[2]["id"]))

    status, _, body = await call_json(
        HTTPMethod.POST,
        lifecycle_url(lab.tournament, registration_id, "withdraw"),
        token=lab.collaborator_token,
        key=new_key(),
        payload=reason_payload("WITHDRAWAL_REQUEST"),
    )

    assert status == 200
    assert body["status"] == "WITHDRAWN"
    rows = await audit_rows(registration_id)
    assert {str(row["actor_user_id"]) for row in rows} == {str(lab.collaborator_id)}


# --- 3. idempotencia ----------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_missing_idempotency_key_is_400(lab: HttpLab) -> None:
    status, _, body = await call_json(
        HTTPMethod.POST,
        create_url(lab.tournament),
        token=lab.owner_token,
        payload=draft_payload(lab),
    )

    assert status == 400
    assert body == {"detail": "IDEMPOTENCY_REQUEST_INVALID"}
    assert await count_registrations(lab) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_invalid_idempotency_key_is_400(lab: HttpLab) -> None:
    status, _, body = await call_json(
        HTTPMethod.POST,
        create_url(lab.tournament),
        token=lab.owner_token,
        key="corta",
        payload=draft_payload(lab),
    )

    assert status == 400
    assert body == {"detail": "IDEMPOTENCY_REQUEST_INVALID"}
    assert await count_registrations(lab) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_replay_returns_the_same_registration_without_duplicating(lab: HttpLab) -> None:
    """Respuesta perdida y reintento: mismo recurso, una sola escritura y un solo evento."""
    key = new_key()
    payload = draft_payload(lab)

    first = await create_registration(lab, key=key, payload=payload)
    second = await create_registration(lab, key=key, payload=payload)

    assert first[0] == 201
    assert second[0] == 201
    assert first[2] == second[2]
    assert await count_registrations(lab) == 1
    assert await audit_actions(TournamentRegistrationId(int(first[2]["id"]))) == ["CREATE"]


@pytest.mark.asyncio(loop_scope="session")
async def test_double_click_on_confirm_executes_once(lab: HttpLab) -> None:
    assert (created := await create_registration(lab))[0] == 201
    registration_id = TournamentRegistrationId(int(created[2]["id"]))
    key = new_key()
    payload = reason_payload("READY")
    url = lifecycle_url(lab.tournament, registration_id, "confirm")

    first = await call_json(HTTPMethod.POST, url, token=lab.owner_token, key=key, payload=payload)
    second = await call_json(HTTPMethod.POST, url, token=lab.owner_token, key=key, payload=payload)

    assert first[0] == 200
    assert second[0] == 200
    assert first[2] == second[2]
    assert await audit_actions(registration_id) == ["CREATE", "CONFIRM"]


@pytest.mark.asyncio(loop_scope="session")
async def test_same_key_with_a_different_payload_is_409(lab: HttpLab) -> None:
    key = new_key()
    assert (first := await create_registration(lab, key=key))[0] == 201

    status, headers, body = await create_registration(
        lab, key=key, payload=draft_payload(lab, category_key="otra-categoria")
    )

    assert status == 409
    assert body == {"detail": "IDEMPOTENCY_KEY_REUSED"}
    assert headers.get("retry-after") is None
    assert await count_registrations(lab) == 1
    assert await audit_rows(TournamentRegistrationId(int(first[2]["id"]))) != []


@pytest.mark.asyncio(loop_scope="session")
async def test_same_key_on_another_resource_is_409(lab: HttpLab) -> None:
    key = new_key()
    payload = draft_payload(lab)
    assert (first := await create_registration(lab, key=key, payload=payload))[0] == 201

    status, _, body = await call_json(
        HTTPMethod.POST,
        create_url(lab.other_tournament),
        token=lab.owner_token,
        key=key,
        payload=payload,
    )

    assert status == 409
    assert body == {"detail": "IDEMPOTENCY_KEY_REUSED"}
    assert await count_registrations(lab, lab.other_tournament) == 0
    assert await count_registrations(lab, lab.tournament) == 1
    assert int(first[2]["id"]) > 0


async def last_registration_id(lab: HttpLab) -> TournamentRegistrationId:
    """Identificador de la ultima inscripcion del competidor en el torneo del laboratorio."""
    registration_id = await database.fetch_val(
        query=(
            "SELECT id FROM tournament_registrations "
            "WHERE tournament_id = :t AND competitor_id = :c ORDER BY id DESC LIMIT 1"
        ),
        values={"t": lab.tournament, "c": lab.competitor},
    )
    return TournamentRegistrationId(int(registration_id))


@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_same_key_executes_once(lab: HttpLab) -> None:
    """Dos peticiones a la vez con la misma clave: una ejecuta y la otra espera o replica."""
    key = new_key()
    payload = draft_payload(lab)

    first, second = await asyncio.gather(
        create_registration(lab, key=key, payload=payload),
        create_registration(lab, key=key, payload=payload),
    )

    assert await count_registrations(lab) == 1
    assert await audit_rows(await last_registration_id(lab)) != []
    successful = [result for result in (first, second) if result[0] == 201]
    assert len(successful) >= 1
    assert len({result[2]["id"] for result in successful}) == 1

    for status, headers, body in (first, second):
        if status == 409:
            assert body == {"detail": "OPERATION_IN_PROGRESS"}
            assert headers["retry-after"] == str(RETRY_AFTER_SECONDS)
        else:
            assert status == 201


@pytest.mark.asyncio(loop_scope="session")
async def test_operation_in_progress_is_409_with_retry_after(lab: HttpLab) -> None:
    """La reserva en curso del mismo actor y clave responde 409 con ``Retry-After``.

    Es el caso RED→GREEN del arreglo del manejador global: la excepcion ya traia la cabecera, pero
    el manejador la descartaba al construir la respuesta.
    """
    key = new_key()
    payload = draft_payload(lab)
    await sql_insert_idempotency_reservation(
        lab_reservation(
            lab.tenant,
            lab.owner_id,
            key=key,
            method="POST",
            path=create_path(lab.tournament),
            payload=payload,
        )
    )

    status, headers, body = await create_registration(lab, key=key, payload=payload)

    assert status == 409
    assert body == {"detail": "OPERATION_IN_PROGRESS"}
    assert headers["retry-after"] == str(RETRY_AFTER_SECONDS)
    assert await count_registrations(lab) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_lock_timeout_is_503_with_retry_after(lab: HttpLab) -> None:
    """Con la clave bloqueada por otra transaccion, la reserva agota ``lock_timeout``."""
    key = new_key()
    payload = draft_payload(lab)
    transaction = await database.transaction()
    try:
        await sql_insert_idempotency_reservation(
            lab_reservation(
                lab.tenant,
                lab.owner_id,
                key=key,
                method="POST",
                path=create_path(lab.tournament),
                payload=payload,
            )
        )
        status, headers, body = await create_registration(lab, key=key, payload=payload)
    finally:
        await transaction.rollback()

    assert status == 503
    assert body == {"detail": "LOCK_TIMEOUT"}
    assert headers["retry-after"] == str(RETRY_AFTER_SECONDS)
    assert await count_registrations(lab) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_replay_does_not_bypass_a_revoked_actor(lab: HttpLab) -> None:
    """Reintentar con la misma clave no revive una sesion ya invalidada."""
    key = new_key()
    payload = draft_payload(lab)
    assert (first := await create_registration(lab, key=key, payload=payload))[0] == 201

    await update_user_active(lab.owner_id, False)
    try:
        status, _, body = await create_registration(lab, key=key, payload=payload)
    finally:
        await update_user_active(lab.owner_id, True)

    assert status == 401
    assert body == UNAUTHORIZED
    assert await count_registrations(lab) == 1
    assert int(first[2]["id"]) > 0


# --- 4. motivos de auditoria --------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_unknown_reason_code_is_400(lab: HttpLab) -> None:
    assert (created := await create_registration(lab))[0] == 201
    registration_id = TournamentRegistrationId(int(created[2]["id"]))

    status, _, body = await call_json(
        HTTPMethod.POST,
        lifecycle_url(lab.tournament, registration_id, "withdraw"),
        token=lab.owner_token,
        key=new_key(),
        payload=reason_payload("PORQUE_SI"),
    )

    assert status == 400
    assert body == {"detail": "Registration request is invalid"}
    assert await audit_actions(registration_id) == ["CREATE"]


@pytest.mark.asyncio(loop_scope="session")
async def test_reason_note_on_a_code_that_does_not_allow_it_is_400(lab: HttpLab) -> None:
    """La nota solo cabe en los motivos que la contemplan; el catalogo lo decide, no el cliente."""
    assert (created := await create_registration(lab))[0] == 201
    registration_id = TournamentRegistrationId(int(created[2]["id"]))

    status, _, body = await call_json(
        HTTPMethod.POST,
        lifecycle_url(lab.tournament, registration_id, "withdraw"),
        token=lab.owner_token,
        key=new_key(),
        payload=reason_payload("WITHDRAWAL_REQUEST", "esto no cabe aqui"),
    )

    assert status == 400
    assert body == {"detail": "Registration request is invalid"}
    assert await audit_actions(registration_id) == ["CREATE"]


@pytest.mark.asyncio(loop_scope="session")
async def test_missing_reason_code_on_withdraw_is_400(lab: HttpLab) -> None:
    assert (created := await create_registration(lab))[0] == 201
    registration_id = TournamentRegistrationId(int(created[2]["id"]))

    status, _, body = await call_json(
        HTTPMethod.POST,
        lifecycle_url(lab.tournament, registration_id, "withdraw"),
        token=lab.owner_token,
        key=new_key(),
        payload=None,
    )

    assert status == 400
    assert body == {"detail": "Registration request is invalid"}
    assert await audit_actions(registration_id) == ["CREATE"]


# --- 5. superficie publica y errores ------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_actor_and_tenant_spoofing_in_the_body_is_rejected(lab: HttpLab) -> None:
    """Identidad y tenant no se aceptan desde el cuerpo: se rechazan, no se ignoran."""
    payload = draft_payload(
        lab,
        actor_user_id=lab.owner_id,
        actor_label="Suplantadora",
        tenant_club_id=lab.tenant,
        role="OWNER",
        status="CONFIRMED",
        revision=99,
    )

    status, _, body = await create_registration(lab, payload=payload)

    assert status == 422
    assert await count_registrations(lab) == 0
    # el error describe el campo sobrante, no datos del servidor
    assert "extra_forbidden" in json.dumps(body) or "Unprocessable Entity" in json.dumps(body)


@pytest.mark.asyncio(loop_scope="session")
async def test_malformed_payload_is_a_controlled_error(lab: HttpLab) -> None:
    status, _, body = await create_registration(lab, payload=b"{no es json")

    assert status == 422
    assert "detail" in body
    assert await count_registrations(lab) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_errors_do_not_leak_internal_state(lab: HttpLab) -> None:
    """Un conflicto de estado responde solo con la etiqueta del contrato, sin identificadores."""
    assert (created := await create_registration(lab))[0] == 201
    registration_id = TournamentRegistrationId(int(created[2]["id"]))

    # descalificar un borrador no es una transicion valida: 409 de estado
    conflict = await call_json(
        HTTPMethod.POST,
        lifecycle_url(lab.tournament, registration_id, "disqualify"),
        token=lab.owner_token,
        key=new_key(),
        payload=reason_payload("RULE_VIOLATION", "nota de prueba"),
    )

    assert conflict[0] == 409
    assert set(conflict[2]) == {"detail"}
    # ni identificadores, ni huellas, ni rastro de la excepcion
    assert not any(character.isdigit() for character in str(conflict[2]["detail"]))
    assert "Traceback" not in json.dumps(conflict[2])


@pytest.mark.asyncio(loop_scope="session")
async def test_not_found_does_not_reveal_the_tournament(lab: HttpLab) -> None:
    """Un tenant ajeno y un torneo inexistente responden exactamente igual."""
    foreign_tenant = await call_json(
        HTTPMethod.POST,
        create_url(lab.foreign_tournament),
        token=lab.owner_token,
        key=new_key(),
        payload=draft_payload(lab),
    )
    missing = await call_json(
        HTTPMethod.POST,
        create_url(TournamentId(999_999_999)),
        token=lab.owner_token,
        key=new_key(),
        payload=draft_payload(lab),
    )

    assert foreign_tenant[0] == missing[0] == 404
    assert foreign_tenant[2] == missing[2] == NOT_FOUND
    assert not any(character.isdigit() for character in str(missing[2]["detail"]))


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_records_field_names_and_never_values(lab: HttpLab) -> None:
    """La auditoria del alta es un solo evento del tenant, con nombres de campo, sin valores."""
    assert (created := await create_registration(lab))[0] == 201
    registration_id = TournamentRegistrationId(int(created[2]["id"]))
    rows = await audit_rows(registration_id)

    assert len(rows) == 1
    assert rows[0]["entity"] == "tournament_registration"
    assert rows[0]["entity_id"] == registration_id
    assert rows[0]["action"] == "CREATE"
    assert rows[0]["actor_user_id"] == lab.owner_id
    changed_fields = rows[0]["changed_fields"]
    assert "competitor_id" in str(changed_fields)
    assert "status" in str(changed_fields)
    # ni el nombre del competidor ni el de la academia aparecen en la auditoria
    assert "Competidora" not in json.dumps(rows, default=str)
    assert "Academia" not in json.dumps(rows, default=str)
    assert await count_registrations(lab, lab.other_tournament) == 0
