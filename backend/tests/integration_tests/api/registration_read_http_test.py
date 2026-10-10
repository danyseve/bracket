# pylint: disable=redefined-outer-name
"""Contrato HTTP de lectura de inscripciones F3 (S3.4b, LAB ONLY).

Pruebas **negras** contra la aplicacion del producto (``bracket/app.py``): se levanta el servidor
de pruebas real y se llama por HTTP con una sesion del producto (``Bearer`` emitido por el mismo
camino que en produccion).

Lo que se demuestra aqui, y contra que capa:

* el listado y el detalle devuelven la **proyeccion publica** de una inscripcion (la misma que las
  rutas de escritura de S3.4a): sin enlaces de correccion, sin metadatos de idempotencia y sin PII
  interna;
* el ambito es del servidor: el torneo de la ruta tiene que ser del tenant del JWT y el listado
  nunca mezcla inscripciones de otro tenant ni de otro torneo;
* el detalle de una inscripcion de otro tenant responde igual que una inexistente: el mismo 404
  sin revelar existencia, y sin ninguna consulta global por identificador;
* el orden del listado es determinista y la paginacion (``limit``/``offset``) es estable, con el
  total del filtro aparte de la pagina;
* los filtros son ``status`` y ``competitor_id``, con vocabulario cerrado para el estado.

Solo usa la base de CI (``bracket_ci``): no hay mocks del dominio ni de la capa SQL. Cada caso
atraviesa la ruta, la dependencia de contexto y PostgreSQL.
"""

from __future__ import annotations

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
from bracket.models.db.club import ClubInsertable
from bracket.models.db.user_x_club import UserXClubInsertable, UserXClubRelation
from bracket.routes.domain_idempotency import IDEMPOTENCY_KEY_HEADER
from bracket.sql.users import update_user_active
from bracket.utils.dummy_records import DUMMY_TOURNAMENT
from bracket.utils.http import HTTPMethod
from bracket.utils.id_types import (
    ClubId,
    CompetitorId,
    TournamentId,
    TournamentRegistrationId,
)
from tests.integration_tests.api.shared import get_root_uvicorn_url
from tests.integration_tests.mocks import get_mock_token, get_mock_user
from tests.integration_tests.registration_fixtures import (
    audit_rows,
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

#: Campos que **no** pueden aparecer ni en el detalle ni en el listado.
INTERNAL_FIELDS = frozenset(
    {
        "corrects_registration_id",
        "superseded_by_registration_id",
        "verified_by_user_id",
        "verified_at",
        "affiliation_id",
        "idempotency_key",
        "actor_user_id",
        "actor_label",
        "tenant_club_id",
    }
)


@dataclass(frozen=True)
class ReadLab:  # pylint: disable=too-many-instance-attributes
    """Tenant con torneo propio, segundo torneo del mismo tenant y torneo ajeno.

    Arnes de test: agrupa las piezas del laboratorio de lectura en un unico objeto inmutable.
    """

    tenant: ClubId
    tournament: TournamentId
    other_tournament: TournamentId
    foreign_tournament: TournamentId
    competitor_a: CompetitorId
    competitor_b: CompetitorId
    foreign_competitor: CompetitorId
    owner_token: str
    collaborator_token: str
    outsider_token: str
    inactive_token: str


@asynccontextmanager
async def read_lab_context() -> AsyncIterator[ReadLab]:
    """Datos y sesiones del laboratorio de lectura; la limpieza se apila de hojas a raices."""
    now = datetime_utc.now()
    suffix = uuid4().hex[:12]

    async with AsyncExitStack() as stack:
        club = await stack.enter_async_context(
            inserted_club(ClubInsertable(name=f"f3b-s34b-{suffix}", created=now))
        )
        other_club = await stack.enter_async_context(
            inserted_club(ClubInsertable(name=f"f3b-s34b-other-{suffix}", created=now))
        )
        tournament = await stack.enter_async_context(
            inserted_tournament(
                DUMMY_TOURNAMENT.model_copy(
                    update={"club_id": club.id, "dashboard_endpoint": f"f3b-s34b-{suffix}-a"}
                )
            )
        )
        other_tournament = await stack.enter_async_context(
            inserted_tournament(
                DUMMY_TOURNAMENT.model_copy(
                    update={"club_id": club.id, "dashboard_endpoint": f"f3b-s34b-{suffix}-b"}
                )
            )
        )
        foreign_tournament = await stack.enter_async_context(
            inserted_tournament(
                DUMMY_TOURNAMENT.model_copy(
                    update={"club_id": other_club.id, "dashboard_endpoint": f"f3b-s34b-{suffix}-c"}
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

        # Nombres ordenables a proposito: el orden del listado es parte del contrato.
        competitor_a = await insert_competitor(f"A-competidora-{suffix}", club.id, created=now)
        stack.push_async_callback(delete_competitor, competitor_a)
        competitor_b = await insert_competitor(f"B-competidora-{suffix}", club.id, created=now)
        stack.push_async_callback(delete_competitor, competitor_b)
        foreign_competitor = await insert_competitor(
            f"C-competidora-ajena-{suffix}", other_club.id, created=now
        )
        stack.push_async_callback(delete_competitor, foreign_competitor)

        # Lo ultimo apilado se deshace primero: inscripciones y auditoria antes que el resto.
        stack.push_async_callback(
            delete_registration_audit,
            (tournament.id, other_tournament.id, foreign_tournament.id),
            club.id,
        )

        yield ReadLab(
            tenant=club.id,
            tournament=tournament.id,
            other_tournament=other_tournament.id,
            foreign_tournament=foreign_tournament.id,
            competitor_a=competitor_a,
            competitor_b=competitor_b,
            foreign_competitor=foreign_competitor,
            owner_token=get_mock_token(owner.email),
            collaborator_token=get_mock_token(collaborator.email),
            outsider_token=get_mock_token(outsider.email),
            inactive_token=get_mock_token(inactive.email),
        )


@pytest_asyncio.fixture(loop_scope="session", scope="function")
async def lab() -> AsyncIterator[ReadLab]:
    """Laboratorio por caso: datos propios, de modo que los recuentos son absolutos."""
    async with read_lab_context() as read_lab:
        yield read_lab


@pytest.fixture(scope="module", autouse=True)
def idempotency_secret() -> Iterator[None]:
    """Clave HMAC sintetica del laboratorio (las altas del arnes pasan por el protocolo)."""
    patcher = pytest.MonkeyPatch()
    patcher.setattr(config, "idempotency_hmac_key", SYNTHETIC_SECRET)
    patcher.setattr(config, "idempotency_hmac_key_version", "v1")
    yield
    patcher.undo()


# --- utilidades HTTP ---------------------------------------------------------------------------


def new_key() -> str:
    """Clave de idempotencia valida y unica por llamada."""
    return f"k-{uuid4().hex}"


def registrations_url(tournament_id: TournamentId, query: str = "") -> str:
    return f"tournaments/{tournament_id}/registrations{query}"


def registration_url(tournament_id: TournamentId, registration_id: int) -> str:
    return f"tournaments/{tournament_id}/registrations/{registration_id}"


def lifecycle_url(tournament_id: TournamentId, registration_id: int, action: str) -> str:
    return f"{registration_url(tournament_id, registration_id)}/{action}"


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


def reason_payload(reason_code: str) -> bytes:
    return json.dumps({"reason_code": reason_code}).encode()


async def create_registration(
    lab: ReadLab,
    competitor_id: CompetitorId,
    category_key: str,
    *,
    tournament_id: TournamentId | None = None,
    token: str | None = None,
) -> int:
    """Alta de una inscripcion en borrador; devuelve su identificador."""
    status, _, body = await call_json(
        HTTPMethod.POST,
        registrations_url(tournament_id or lab.tournament),
        token=token or lab.owner_token,
        key=new_key(),
        payload=draft_payload(competitor_id, category_key),
    )
    assert status == 201, body
    return int(body["id"])


async def lifecycle(lab: ReadLab, registration_id: int, action: str, reason: str) -> dict[str, Any]:
    status, _, body = await call_json(
        HTTPMethod.POST,
        lifecycle_url(lab.tournament, registration_id, action),
        token=lab.owner_token,
        key=new_key(),
        payload=reason_payload(reason),
    )
    assert status == 200, body
    return body


async def list_registrations(
    lab: ReadLab,
    *,
    tournament_id: TournamentId | None = None,
    query: str = "",
    token: str | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    return await call_json(
        HTTPMethod.GET,
        registrations_url(tournament_id or lab.tournament, query),
        token=token if token is not None else lab.owner_token,
    )


async def get_registration(
    lab: ReadLab,
    registration_id: int,
    *,
    tournament_id: TournamentId | None = None,
    token: str | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    return await call_json(
        HTTPMethod.GET,
        registration_url(tournament_id or lab.tournament, registration_id),
        token=token if token is not None else lab.owner_token,
    )


def page(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Elementos de una pagina del listado."""
    return list(body["registrations"])


def keys_of(body: dict[str, Any]) -> list[tuple[str, str | None]]:
    """Clave de orden visible de cada elemento: (nombre del competidor, categoria)."""
    return [(item["competitor_name_snapshot"], item["category_key"]) for item in page(body)]


# --- 1. listado --------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_list_returns_the_tournament_registrations_with_the_public_projection(
    lab: ReadLab,
) -> None:
    """El listado trae las inscripciones del torneo con la proyeccion publica y el total."""
    await create_registration(lab, lab.competitor_a, "cat-1")
    await create_registration(lab, lab.competitor_a, "cat-2")

    status, headers, body = await list_registrations(lab)

    assert status == 200
    assert headers["content-type"].startswith(JSON_CONTENT_TYPE)
    assert body["count"] == 2
    assert len(page(body)) == 2
    for item in page(body):
        assert set(item) == PUBLIC_REGISTRATION_FIELDS
        assert item["tournament_id"] == lab.tournament
        assert INTERNAL_FIELDS.isdisjoint(item)


@pytest.mark.asyncio(loop_scope="session")
async def test_list_is_ordered_deterministically_and_repeats_the_same_order(lab: ReadLab) -> None:
    """Orden por nombre, categoria e id: la misma consulta devuelve siempre la misma secuencia."""
    await create_registration(lab, lab.competitor_b, "cat-1")
    await create_registration(lab, lab.competitor_a, "cat-2")
    await create_registration(lab, lab.competitor_a, "cat-1")

    first = await list_registrations(lab)
    second = await list_registrations(lab)

    keys = keys_of(first[2])
    assert keys == sorted(keys, key=lambda item: (item[0], item[1] or ""))
    assert len({item["id"] for item in page(first[2])}) == 3
    assert [item["id"] for item in page(first[2])] == [item["id"] for item in page(second[2])]


@pytest.mark.asyncio(loop_scope="session")
async def test_pagination_returns_disjoint_pages_and_the_total_of_the_filter(lab: ReadLab) -> None:
    """``count`` es el total del filtro, no el de la pagina; las paginas no se solapan."""
    for competitor_id, category_key in (
        (lab.competitor_a, "cat-1"),
        (lab.competitor_a, "cat-2"),
        (lab.competitor_b, "cat-1"),
    ):
        await create_registration(lab, competitor_id, category_key)

    first = await list_registrations(lab, query="?limit=2&offset=0")
    second = await list_registrations(lab, query="?limit=2&offset=2")

    assert first[2]["count"] == second[2]["count"] == 3
    assert len(page(first[2])) == 2
    assert len(page(second[2])) == 1
    assert {item["id"] for item in page(first[2])}.isdisjoint(
        {item["id"] for item in page(second[2])}
    )
    assert [item["id"] for item in page(first[2]) + page(second[2])] == [
        item["id"] for item in page((await list_registrations(lab))[2])
    ]


@pytest.mark.asyncio(loop_scope="session")
async def test_offset_beyond_the_total_returns_an_empty_page_with_the_total(lab: ReadLab) -> None:
    await create_registration(lab, lab.competitor_a, "cat-1")

    status, _, body = await list_registrations(lab, query="?limit=10&offset=50")

    assert status == 200
    assert body["count"] == 1
    assert not page(body)


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("query", ["?limit=0", "?limit=101", "?offset=-1"])
async def test_pagination_limits_are_enforced(lab: ReadLab, query: str) -> None:
    """``limit`` fuera de [1, 100] y ``offset`` negativo se rechazan, no se recortan en silencio."""
    status, _, body = await list_registrations(lab, query=query)

    assert status == 422
    assert "detail" in body


# --- 2. filtros --------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_list_filters_by_status(lab: ReadLab) -> None:
    pending = await create_registration(lab, lab.competitor_a, "cat-1")
    confirmed = await create_registration(lab, lab.competitor_a, "cat-2")
    await lifecycle(lab, confirmed, "confirm", "READY")

    status, _, body = await list_registrations(lab, query="?status=CONFIRMED")

    assert status == 200
    assert body["count"] == 1
    assert [item["id"] for item in page(body)] == [confirmed]
    assert [item["status"] for item in page(body)] == ["CONFIRMED"]

    draft = await list_registrations(lab, query="?status=DRAFT")

    assert [item["id"] for item in page(draft[2])] == [pending]


@pytest.mark.asyncio(loop_scope="session")
async def test_list_filters_by_competitor(lab: ReadLab) -> None:
    await create_registration(lab, lab.competitor_a, "cat-1")
    from_b = await create_registration(lab, lab.competitor_b, "cat-1")

    status, _, body = await list_registrations(lab, query=f"?competitor_id={lab.competitor_a}")

    assert status == 200
    assert body["count"] == 1
    assert page(body)[0]["competitor_id"] == lab.competitor_a

    only_b = await list_registrations(lab, query=f"?competitor_id={lab.competitor_b}")

    assert [item["id"] for item in page(only_b[2])] == [from_b]


@pytest.mark.asyncio(loop_scope="session")
async def test_list_combines_status_and_competitor_filters(lab: ReadLab) -> None:
    kept = await create_registration(lab, lab.competitor_a, "cat-1")
    dropped = await create_registration(lab, lab.competitor_a, "cat-2")
    await lifecycle(lab, dropped, "confirm", "READY")

    status, _, body = await list_registrations(
        lab, query=f"?status=DRAFT&competitor_id={lab.competitor_a}"
    )

    assert status == 200
    assert body["count"] == 1
    assert [item["id"] for item in page(body)] == [kept]


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("query", ["?status=CORRECTED", "?status=UNKNOWN"])
async def test_unknown_or_corrected_status_filter_is_rejected(lab: ReadLab, query: str) -> None:
    """``CORRECTED`` no es un valor del filtro: las corregidas no forman parte del listado."""
    status, _, body = await list_registrations(lab, query=query)

    assert status == 422
    assert "detail" in body


@pytest.mark.asyncio(loop_scope="session")
async def test_superseded_registrations_do_not_appear_in_the_list(lab: ReadLab) -> None:
    """Una inscripcion sustituida por una correccion no es parte del listado vigente.

    La operacion de correccion es posterior a esta fase, asi que la sustitucion se marca
    directamente en la base de datos, igual que ``set_competitor_active`` hace con la baja logica
    que todavia no tiene API.
    """
    original = await create_registration(lab, lab.competitor_a, "cat-1")
    replacement = await create_registration(lab, lab.competitor_a, "cat-2")
    await database.execute(
        query=(
            "UPDATE tournament_registrations SET status = 'CORRECTED', "
            "superseded_by_registration_id = :replacement WHERE id = :original"
        ),
        values={"replacement": replacement, "original": original},
    )

    status, _, body = await list_registrations(lab)

    assert status == 200
    assert body["count"] == 1
    assert [item["id"] for item in page(body)] == [replacement]
    # la sustituida no se lista, pero su detalle por identificador sigue siendo accesible
    assert (await get_registration(lab, original))[0] == 200


# --- 3. autorizacion y aislamiento -------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_owner_and_collaborator_can_read(lab: ReadLab) -> None:
    registration_id = await create_registration(lab, lab.competitor_a, "cat-1")

    for token in (lab.owner_token, lab.collaborator_token):
        status, _, body = await list_registrations(lab, token=token)
        assert status == 200
        assert body["count"] == 1

        status, _, detail = await get_registration(lab, registration_id, token=token)
        assert status == 200
        assert detail["id"] == registration_id


@pytest.mark.asyncio(loop_scope="session")
async def test_read_without_token_is_401(lab: ReadLab) -> None:
    status, _, body = await call_json(HTTPMethod.GET, registrations_url(lab.tournament))

    assert status == 401
    assert body == NOT_AUTHENTICATED


@pytest.mark.asyncio(loop_scope="session")
async def test_inactive_and_unknown_sessions_are_indistinguishable_401(lab: ReadLab) -> None:
    for token in (lab.inactive_token, "not-a-jwt"):
        status, _, body = await list_registrations(lab, token=token)
        assert status == 401
        assert body == UNAUTHORIZED


@pytest.mark.asyncio(loop_scope="session")
async def test_foreign_tournament_is_404_like_a_missing_one(lab: ReadLab) -> None:
    """Un torneo de otro tenant y uno inexistente responden igual, en listado y en detalle."""
    foreign = await list_registrations(lab, tournament_id=lab.foreign_tournament)
    missing = await list_registrations(lab, tournament_id=TournamentId(999_999_999))

    assert foreign[0] == missing[0] == 404
    assert foreign[2] == missing[2] == NOT_FOUND

    foreign_detail = await get_registration(
        lab, 1, tournament_id=lab.foreign_tournament, token=lab.outsider_token
    )
    assert foreign_detail[0] == 404
    assert foreign_detail[2] == NOT_FOUND


@pytest.mark.asyncio(loop_scope="session")
async def test_list_never_exposes_another_tenants_registrations(lab: ReadLab) -> None:
    """Las inscripciones de otro tenant no aparecen ni cuentan en el listado propio."""
    await create_registration(lab, lab.competitor_a, "cat-1")
    foreign_id = await create_registration(
        lab,
        lab.foreign_competitor,
        "cat-ajena",
        tournament_id=lab.foreign_tournament,
        token=lab.outsider_token,
    )

    status, _, body = await list_registrations(lab)

    assert status == 200
    assert body["count"] == 1
    assert foreign_id not in {item["id"] for item in page(body)}

    own = await call_json(
        HTTPMethod.GET,
        registration_url(lab.foreign_tournament, foreign_id),
        token=lab.outsider_token,
    )
    assert own[0] == 200
    assert own[2]["id"] == foreign_id


@pytest.mark.asyncio(loop_scope="session")
async def test_list_only_covers_its_own_tournament(lab: ReadLab) -> None:
    """Otro torneo del mismo tenant es otro ambito: no se mezcla ni se cuenta."""
    await create_registration(lab, lab.competitor_a, "cat-1")
    await create_registration(lab, lab.competitor_a, "cat-otro", tournament_id=lab.other_tournament)

    status, _, body = await list_registrations(lab)

    assert status == 200
    assert body["count"] == 1
    assert all(item["tournament_id"] == lab.tournament for item in page(body))


# --- 4. detalle --------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_detail_returns_the_same_projection_as_creation(lab: ReadLab) -> None:
    created = await call_json(
        HTTPMethod.POST,
        registrations_url(lab.tournament),
        token=lab.owner_token,
        key=new_key(),
        payload=draft_payload(lab.competitor_a, "cat-1"),
    )
    registration_id = int(created[2]["id"])

    status, headers, detail = await get_registration(lab, registration_id)

    assert status == 200
    assert headers["content-type"].startswith(JSON_CONTENT_TYPE)
    assert detail == created[2]
    assert set(detail) == PUBLIC_REGISTRATION_FIELDS
    assert INTERNAL_FIELDS.isdisjoint(detail)


@pytest.mark.asyncio(loop_scope="session")
async def test_detail_of_another_tenants_registration_is_404(lab: ReadLab) -> None:
    """El detalle de una inscripcion ajena no se distingue de una inexistente."""
    foreign_id = await create_registration(
        lab,
        lab.foreign_competitor,
        "cat-ajena",
        tournament_id=lab.foreign_tournament,
        token=lab.outsider_token,
    )

    foreign = await get_registration(lab, foreign_id)
    missing = await get_registration(lab, 999_999_999)

    assert foreign[0] == missing[0] == 404
    assert foreign[2] == missing[2] == NOT_FOUND
    assert not any(character.isdigit() for character in str(foreign[2]["detail"]))


@pytest.mark.asyncio(loop_scope="session")
async def test_detail_of_another_tournament_of_the_same_tenant_is_404(lab: ReadLab) -> None:
    """La inscripcion existe en el tenant, pero no en el torneo de la ruta: mismo 404."""
    other_id = await create_registration(
        lab, lab.competitor_a, "cat-otro", tournament_id=lab.other_tournament
    )

    same_tenant_other_tournament = await get_registration(
        lab, other_id, tournament_id=lab.tournament
    )
    missing = await get_registration(lab, 999_999_999)

    assert same_tenant_other_tournament[0] == missing[0] == 404
    assert same_tenant_other_tournament[2] == missing[2] == NOT_FOUND


@pytest.mark.asyncio(loop_scope="session")
async def test_detail_reflects_the_lifecycle_state(lab: ReadLab) -> None:
    registration_id = await create_registration(lab, lab.competitor_a, "cat-1")
    await lifecycle(lab, registration_id, "confirm", "READY")

    status, _, detail = await get_registration(lab, registration_id)

    assert status == 200
    assert detail["status"] == "CONFIRMED"


# --- 5. lectura pura ----------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_reads_are_pure_and_do_not_need_an_idempotency_key(lab: ReadLab) -> None:
    """Leer no exige ``Idempotency-Key``, no escribe auditoria y no reserva nada."""
    registration_id = await create_registration(lab, lab.competitor_a, "cat-1")
    await lifecycle(lab, registration_id, "confirm", "READY")
    before = await audit_rows(TournamentRegistrationId(registration_id))

    # sin clave de idempotencia: si la lectura la exigiera, seria 400
    status, _, body = await call_json(
        HTTPMethod.GET, registrations_url(lab.tournament), token=lab.owner_token
    )
    detail = await call_json(
        HTTPMethod.GET, registration_url(lab.tournament, registration_id), token=lab.owner_token
    )

    assert status == 200
    assert detail[0] == 200
    assert body["count"] == 1
    # la lectura no anade eventos: el ciclo de vida sigue siendo el unico que audita
    later = await audit_rows(TournamentRegistrationId(registration_id))
    assert [row["action"] for row in later] == [row["action"] for row in before]


@pytest.mark.asyncio(loop_scope="session")
async def test_reads_do_not_leak_actor_identity(lab: ReadLab) -> None:
    """Ni el nombre del usuario ni identificadores de sesion viajan al cliente."""
    await create_registration(lab, lab.competitor_a, "cat-1")

    status, _, body = await list_registrations(lab)
    text = json.dumps(body)

    assert status == 200
    assert lab.owner_token not in text
    assert "actor" not in text
    assert "idempotency" not in text
