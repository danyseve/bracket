"""Contrato HTTP del contexto de actor autenticado (S3.3b, LAB ONLY).

Pruebas negras sobre ``bracket/routes/domain_auth.py``: se levanta una aplicacion FastAPI
**de prueba** con rutas internas que devuelven el :class:`ActorContext` resuelto por las
dependencias reales, y se llama por HTTP contra el servidor de pruebas
(``tests/integration_tests/api/shared.py``). La autenticacion es la del producto: token
HS256 validado por ``check_jwt_and_get_user``, obtenido con ``POST /token`` o emitido por
``create_access_token``. **No se anade ninguna ruta al router productivo.**

Los valores del contexto se contrastan con los de las filas insertadas (club, usuario y
relacion ``users_x_clubs``), de modo que las pruebas demuestran que el contexto sale del
JWT y de consultas autorizadas, no de valores construidos a mano por el test.

La app de prueba tampoco traduce las excepciones del dominio: la revalidacion se prueba
llamando a la operacion real de ``bracket/logic/competitors.py``.

Solo usa la base de CI (``bracket_ci``/``bracket_test``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import aiohttp
import jwt
import pytest
import pytest_asyncio
from bracket.routes.domain_auth import (
    actor_context_for_club,
    actor_context_for_tournament,
    owner_actor_context_for_club,
    owner_actor_context_for_tournament,
)
from fastapi import Depends, FastAPI
from heliclockter import timedelta

from bracket.app import app
from bracket.database import database
from bracket.logic.competitors import TenantNotAuthorizedError, create_competitor
from bracket.models.db.club import Club, ClubInsertable
from bracket.models.db.domain import ActorContext
from bracket.models.db.tournament import Tournament, TournamentInsertable
from bracket.models.db.user import UserInDB
from bracket.models.db.user_x_club import UserXClubInsertable, UserXClubRelation
from bracket.routes.auth import ACCESS_TOKEN_EXPIRE_MINUTES, create_access_token
from bracket.sql.users import update_user_active
from bracket.utils.dummy_records import DUMMY_CLUB, DUMMY_TOURNAMENT
from bracket.utils.http import HTTPMethod
from bracket.utils.id_types import ClubId, TournamentId
from bracket.utils.types import JsonDict
from tests.integration_tests.api.shared import (
    UvicornTestServer,
    find_free_port,
    send_request,
)
from tests.integration_tests.mocks import get_mock_token, get_mock_user
from tests.integration_tests.sql import (
    inserted_club,
    inserted_tournament,
    inserted_user,
    inserted_user_x_club,
)

UNAUTHORIZED: JsonDict = {"detail": "Could not validate credentials"}
NOT_FOUND: JsonDict = {"detail": "Not Found"}
MOCK_PASSWORD = "mypassword"


def build_probe_app() -> FastAPI:
    """Aplicacion de prueba con rutas internas que exponen el contexto resuelto.

    Estas rutas viven solo aqui: el router productivo (``bracket/app.py``) no declara
    ninguna ruta F3 en esta fase.
    """
    probe = FastAPI()

    @probe.get("/probe/club/{club_id}")
    async def probe_club(
        club_id: ClubId, context: ActorContext = Depends(actor_context_for_club)
    ) -> JsonDict:
        return context.model_dump(mode="json")

    @probe.get("/probe/club/{club_id}/owner")
    async def probe_club_owner(
        club_id: ClubId, context: ActorContext = Depends(owner_actor_context_for_club)
    ) -> JsonDict:
        return context.model_dump(mode="json")

    @probe.get("/probe/tournament/{tournament_id}")
    async def probe_tournament(
        tournament_id: TournamentId, context: ActorContext = Depends(actor_context_for_tournament)
    ) -> JsonDict:
        return context.model_dump(mode="json")

    @probe.get("/probe/tournament/{tournament_id}/owner")
    async def probe_tournament_owner(
        tournament_id: TournamentId,
        context: ActorContext = Depends(owner_actor_context_for_tournament),
    ) -> JsonDict:
        return context.model_dump(mode="json")

    @probe.post("/probe/club/{club_id}")
    async def probe_club_with_body(
        club_id: ClubId,
        payload: JsonDict,
        context: ActorContext = Depends(actor_context_for_club),
    ) -> JsonDict:
        return context.model_dump(mode="json") | {"_payload": payload}

    return probe


@asynccontextmanager
async def probe_server() -> AsyncIterator[str]:
    port = find_free_port()
    server = UvicornTestServer(build_probe_app(), port=port)
    try:
        await server.up()
        yield f"http://127.0.0.1:{port}/"
    finally:
        await server.down()


@pytest_asyncio.fixture(loop_scope="session", scope="module")
async def probe_url() -> AsyncIterator[str]:
    async with probe_server() as url:
        yield url


async def call_probe(
    method: HTTPMethod,
    base_url: str,
    path: str,
    token: str | None = None,
    json_body: JsonDict | None = None,
) -> tuple[int, JsonDict]:
    """Llamada HTTP real contra la app de prueba: el mismo camino que un endpoint funcional."""
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as session:
        async with session.request(
            method=str(method.value), url=base_url + path, json=json_body, headers=headers
        ) as response:
            return response.status, JsonDict(await response.json())


class Tenant:
    """Club (organizador) + usuario + relacion + token de sesion, tal como los inserta el gate."""

    def __init__(self, club: Club, user: UserInDB, relation: UserXClubRelation, token: str) -> None:
        self.club = club
        self.user = user
        self.relation = relation
        self.token = token
        self.headers = {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def inserted_tenant(
    relation: UserXClubRelation = UserXClubRelation.OWNER, *, active: bool = True
) -> AsyncIterator[Tenant]:
    mock_user = get_mock_user()
    async with (
        inserted_user(mock_user) as user_inserted,
        inserted_club(DUMMY_CLUB) as club_inserted,
        inserted_user_x_club(
            UserXClubInsertable(
                user_id=user_inserted.id, club_id=club_inserted.id, relation=relation
            )
        ),
    ):
        if not active:
            await update_user_active(user_inserted.id, False)

        yield Tenant(
            club=club_inserted,
            user=user_inserted,
            relation=relation,
            token=get_mock_token(mock_user.email),
        )


@asynccontextmanager
async def inserted_named_club(name: str) -> AsyncIterator[Club]:
    async with inserted_club(ClubInsertable(name=name, created=DUMMY_CLUB.created)) as club:
        yield club


@asynccontextmanager
async def inserted_tournament_of(club: Club) -> AsyncIterator[Tournament]:
    """Torneo del club con ``dashboard_endpoint`` unico, para no chocar con otros tests."""
    tournament = TournamentInsertable.model_validate(
        DUMMY_TOURNAMENT.model_dump() | {"club_id": club.id, "dashboard_endpoint": f"p-{uuid4()}"}
    )
    async with inserted_tournament(tournament) as tournament_inserted:
        yield tournament_inserted


@asynccontextmanager
async def inserted_sports_club(club: Club, name: str) -> AsyncIterator[int]:
    """Academia deportiva (``sports_clubs``) del tenant indicado; devuelve su id."""
    record = await database.fetch_one(
        query="""
            INSERT INTO sports_clubs (name, tenant_club_id, active, created)
            VALUES (:name, :club_id, true, NOW())
            RETURNING id
            """,
        values={"name": name, "club_id": club.id},
    )
    assert record is not None
    sports_club_id: int = record["id"]
    try:
        yield sports_club_id
    finally:
        await database.execute(
            query="DELETE FROM sports_clubs WHERE id = :id", values={"id": sports_club_id}
        )


def expired_token(email: str) -> str:
    return create_access_token(data={"user": email}, expires_delta=timedelta(minutes=-5))


def forged_token(email: str) -> str:
    """JWT bien formado pero firmado con otro secreto."""
    return jwt.encode(
        {"user": email, "exp": 7258723200},
        "definitely-not-the-configured-secret",
        algorithm="HS256",
    )


# --- 1. sesion autenticada ---------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_context_comes_from_a_real_login_session(
    startup_and_shutdown_uvicorn_server: None, probe_url: str
) -> None:
    """Token obtenido con ``POST /token`` de la app productiva, no inyectado en el test."""
    mock_user = get_mock_user()
    async with inserted_tenant() as tenant:
        login = JsonDict(
            await send_request(
                HTTPMethod.POST,
                "token",
                body={"username": mock_user.email, "password": MOCK_PASSWORD},
            )
        )
        assert "access_token" in login

        status, body = await call_probe(
            HTTPMethod.GET,
            probe_url,
            f"probe/club/{tenant.club.id}",
            token=str(login["access_token"]),
        )

    assert status == 200
    assert body == {
        "tenant_club_id": tenant.club.id,
        "actor_user_id": tenant.user.id,
        "actor_label": tenant.user.name,
    }


@pytest.mark.asyncio(loop_scope="session")
async def test_without_jwt(probe_url: str) -> None:
    async with inserted_tenant() as tenant:
        response = await call_probe(HTTPMethod.GET, probe_url, f"probe/club/{tenant.club.id}")
    assert response == (401, UNAUTHORIZED)


@pytest.mark.asyncio(loop_scope="session")
async def test_invalid_jwt(probe_url: str) -> None:
    async with inserted_tenant() as tenant:
        response = await call_probe(
            HTTPMethod.GET, probe_url, f"probe/club/{tenant.club.id}", token="some.invalid.token"
        )
    assert response == (401, UNAUTHORIZED)


@pytest.mark.asyncio(loop_scope="session")
async def test_expired_jwt(probe_url: str) -> None:
    async with inserted_tenant() as tenant:
        response = await call_probe(
            HTTPMethod.GET,
            probe_url,
            f"probe/club/{tenant.club.id}",
            token=expired_token(tenant.user.email),
        )
    assert response == (401, UNAUTHORIZED)


@pytest.mark.asyncio(loop_scope="session")
async def test_forged_signature(probe_url: str) -> None:
    async with inserted_tenant() as tenant:
        response = await call_probe(
            HTTPMethod.GET,
            probe_url,
            f"probe/club/{tenant.club.id}",
            token=forged_token(tenant.user.email),
        )
    assert response == (401, UNAUTHORIZED)


@pytest.mark.asyncio(loop_scope="session")
async def test_unknown_user(probe_url: str) -> None:
    """Firma correcta para un usuario que no existe en la base."""
    async with inserted_tenant() as tenant:
        response = await call_probe(
            HTTPMethod.GET,
            probe_url,
            f"probe/club/{tenant.club.id}",
            token=get_mock_token(f"ghost-{tenant.user.email}"),
        )
    assert response == (401, UNAUTHORIZED)


@pytest.mark.asyncio(loop_scope="session")
async def test_inactive_user(probe_url: str) -> None:
    """Token valido, usuario real, relacion vigente... pero ``users.active = false``."""
    async with inserted_tenant(active=False) as tenant:
        response = await call_probe(
            HTTPMethod.GET, probe_url, f"probe/club/{tenant.club.id}", token=tenant.token
        )
    assert response == (401, UNAUTHORIZED)


# --- 2. ambito por club ------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_owner_gets_context(probe_url: str) -> None:
    async with inserted_tenant(UserXClubRelation.OWNER) as tenant:
        status, body = await call_probe(
            HTTPMethod.GET, probe_url, f"probe/club/{tenant.club.id}", token=tenant.token
        )
    assert status == 200
    assert body == {
        "tenant_club_id": tenant.club.id,
        "actor_user_id": tenant.user.id,
        "actor_label": tenant.user.name,
    }


@pytest.mark.asyncio(loop_scope="session")
async def test_collaborator_gets_context(probe_url: str) -> None:
    async with inserted_tenant(UserXClubRelation.COLLABORATOR) as tenant:
        status, body = await call_probe(
            HTTPMethod.GET, probe_url, f"probe/club/{tenant.club.id}", token=tenant.token
        )
    assert status == 200
    assert body["tenant_club_id"] == tenant.club.id
    assert body["actor_user_id"] == tenant.user.id


@pytest.mark.asyncio(loop_scope="session")
async def test_foreign_and_unknown_club_are_indistinguishable(probe_url: str) -> None:
    """Club de otro tenant y club inexistente: misma respuesta, sin revelar existencia."""
    async with inserted_tenant() as tenant:
        async with inserted_named_club("Other Club") as other:
            foreign = await call_probe(
                HTTPMethod.GET, probe_url, f"probe/club/{other.id}", token=tenant.token
            )
        unknown = await call_probe(
            HTTPMethod.GET, probe_url, "probe/club/987654321", token=tenant.token
        )
        own = await call_probe(
            HTTPMethod.GET, probe_url, f"probe/club/{tenant.club.id}", token=tenant.token
        )
    assert foreign == unknown == (401, UNAUTHORIZED)
    assert own[0] == 200
    assert "Other" not in str(foreign)


@pytest.mark.asyncio(loop_scope="session")
async def test_user_in_multiple_clubs_resolves_tenant_of_the_route(probe_url: str) -> None:
    mock_user = get_mock_user()
    async with (
        inserted_user(mock_user) as user_inserted,
        inserted_named_club("Club A") as club_a,
        inserted_named_club("Club B") as club_b,
        inserted_named_club("Club C") as club_c,
        inserted_user_x_club(
            UserXClubInsertable(
                user_id=user_inserted.id, club_id=club_a.id, relation=UserXClubRelation.OWNER
            )
        ),
        inserted_user_x_club(
            UserXClubInsertable(
                user_id=user_inserted.id,
                club_id=club_b.id,
                relation=UserXClubRelation.COLLABORATOR,
            )
        ),
    ):
        token = get_mock_token(mock_user.email)
        in_a = await call_probe(HTTPMethod.GET, probe_url, f"probe/club/{club_a.id}", token=token)
        in_b = await call_probe(HTTPMethod.GET, probe_url, f"probe/club/{club_b.id}", token=token)
        in_c = await call_probe(HTTPMethod.GET, probe_url, f"probe/club/{club_c.id}", token=token)

    assert in_a[1]["tenant_club_id"] == club_a.id
    assert in_b[1]["tenant_club_id"] == club_b.id
    assert in_c == (401, UNAUTHORIZED)


@pytest.mark.asyncio(loop_scope="session")
async def test_owner_gate_403_only_after_tenant_access(probe_url: str) -> None:
    async with inserted_tenant(UserXClubRelation.COLLABORATOR) as tenant:
        collaborator = await call_probe(
            HTTPMethod.GET, probe_url, f"probe/club/{tenant.club.id}/owner", token=tenant.token
        )
        unknown = await call_probe(
            HTTPMethod.GET, probe_url, "probe/club/987654321/owner", token=tenant.token
        )
    assert collaborator[0] == 403
    assert str(tenant.club.id) not in str(collaborator[1])
    # Sin acceso al tenant no se llega al 403 de rol: la respuesta es la denegacion indistinguible.
    assert unknown == (401, UNAUTHORIZED)


@pytest.mark.asyncio(loop_scope="session")
async def test_owner_gate_allows_owner(probe_url: str) -> None:
    async with inserted_tenant(UserXClubRelation.OWNER) as tenant:
        status, body = await call_probe(
            HTTPMethod.GET, probe_url, f"probe/club/{tenant.club.id}/owner", token=tenant.token
        )
    assert status == 200
    assert body["tenant_club_id"] == tenant.club.id


# --- 3. ambito por torneo ----------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_tournament_resolves_organizer_club(probe_url: str) -> None:
    async with inserted_tenant() as tenant:
        async with inserted_tournament_of(tenant.club) as tournament:
            status, body = await call_probe(
                HTTPMethod.GET, probe_url, f"probe/tournament/{tournament.id}", token=tenant.token
            )
    assert status == 200
    assert body["tenant_club_id"] == tenant.club.id
    assert body["actor_user_id"] == tenant.user.id


@pytest.mark.asyncio(loop_scope="session")
async def test_foreign_tournament_and_unknown_tournament_are_404(probe_url: str) -> None:
    """Cambiar el ``tournament_id`` no da acceso y no revela existencia: 404 identico."""
    async with inserted_tenant() as tenant:
        async with inserted_named_club("Other Club") as other_club:
            async with inserted_tournament_of(other_club) as other_tournament:
                foreign = await call_probe(
                    HTTPMethod.GET,
                    probe_url,
                    f"probe/tournament/{other_tournament.id}",
                    token=tenant.token,
                )
        unknown = await call_probe(
            HTTPMethod.GET, probe_url, "probe/tournament/987654321", token=tenant.token
        )
    assert foreign == unknown == (404, NOT_FOUND)


@pytest.mark.asyncio(loop_scope="session")
async def test_tournament_owner_gate(probe_url: str) -> None:
    async with inserted_tenant(UserXClubRelation.COLLABORATOR) as tenant:
        async with inserted_tournament_of(tenant.club) as tournament:
            collaborator = await call_probe(
                HTTPMethod.GET,
                probe_url,
                f"probe/tournament/{tournament.id}/owner",
                token=tenant.token,
            )
            unknown = await call_probe(
                HTTPMethod.GET, probe_url, "probe/tournament/987654321/owner", token=tenant.token
            )
    assert collaborator[0] == 403
    assert unknown == (404, NOT_FOUND)


@pytest.mark.asyncio(loop_scope="session")
async def test_tournament_requires_authentication(probe_url: str) -> None:
    async with inserted_tenant() as tenant:
        async with inserted_tournament_of(tenant.club) as tournament:
            response = await call_probe(
                HTTPMethod.GET, probe_url, f"probe/tournament/{tournament.id}"
            )
    assert response == (401, UNAUTHORIZED)


# --- 4. manipulacion del payload ---------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_payload_identifiers_are_ignored(probe_url: str) -> None:
    """El contexto no sale del payload: se envian otro tenant, otro actor y un rol, y se ignoran."""
    async with inserted_tenant() as tenant:
        async with inserted_named_club("Other Club") as other:
            status, body = await call_probe(
                HTTPMethod.POST,
                probe_url,
                f"probe/club/{tenant.club.id}",
                token=tenant.token,
                json_body={
                    "tenant_club_id": other.id,
                    "actor_user_id": 987654321,
                    "actor_label": "Mallory",
                    "role": "OWNER",
                },
            )
    assert status == 200
    assert body["tenant_club_id"] == tenant.club.id
    assert body["actor_user_id"] == tenant.user.id
    assert body["actor_label"] == tenant.user.name


@pytest.mark.asyncio(loop_scope="session")
async def test_payload_cannot_claim_a_foreign_tenant(probe_url: str) -> None:
    async with inserted_tenant() as tenant:
        async with inserted_named_club("Other Club") as other:
            response = await call_probe(
                HTTPMethod.POST,
                probe_url,
                f"probe/club/{other.id}",
                token=tenant.token,
                json_body={
                    "tenant_club_id": other.id,
                    "actor_user_id": tenant.user.id,
                    "actor_label": tenant.user.name,
                    "role": "OWNER",
                },
            )
    assert response == (401, UNAUTHORIZED)


# --- 5. academia frente a organizador ----------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_sports_club_is_not_a_tenant(probe_url: str) -> None:
    """``sports_clubs`` (academia) y ``clubs`` (organizador) no se confunden."""
    async with inserted_tenant() as tenant:
        async with inserted_sports_club(tenant.club, "Academia del tenant") as sports_club_id:
            assert sports_club_id != tenant.club.id
            as_tenant = await call_probe(
                HTTPMethod.GET, probe_url, f"probe/club/{sports_club_id}", token=tenant.token
            )
            as_owner = await call_probe(
                HTTPMethod.GET, probe_url, f"probe/club/{sports_club_id}/owner", token=tenant.token
            )
            organizer = await call_probe(
                HTTPMethod.GET, probe_url, f"probe/club/{tenant.club.id}", token=tenant.token
            )
    assert as_tenant == as_owner == (401, UNAUTHORIZED)
    assert organizer[0] == 200


@pytest.mark.asyncio(loop_scope="session")
async def test_academy_of_another_tenant_is_not_a_tenant(probe_url: str) -> None:
    async with inserted_tenant() as tenant:
        async with inserted_named_club("Other Club") as other_club:
            async with inserted_sports_club(other_club, "Academia ajena") as sports_club_id:
                response = await call_probe(
                    HTTPMethod.GET, probe_url, f"probe/club/{sports_club_id}", token=tenant.token
                )
    assert response == (401, UNAUTHORIZED)


# --- 6. revalidacion por el dominio y rutas publicas -------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_domain_revalidates_revoked_relation(probe_url: str) -> None:
    """El dominio no confia en el contexto: si la relacion se revoca, la operacion se rechaza."""
    async with inserted_tenant(UserXClubRelation.OWNER) as tenant:
        status, body = await call_probe(
            HTTPMethod.GET, probe_url, f"probe/club/{tenant.club.id}", token=tenant.token
        )
        assert status == 200
        context = ActorContext.model_validate(body)

        await database.execute(
            query="DELETE FROM users_x_clubs WHERE user_id = :user_id AND club_id = :club_id",
            values={"user_id": tenant.user.id, "club_id": tenant.club.id},
        )
        with pytest.raises(TenantNotAuthorizedError):
            await create_competitor(context, display_name="Relacion revocada")


@pytest.mark.asyncio(loop_scope="session")
async def test_public_dashboard_behaviour_is_preserved(
    startup_and_shutdown_uvicorn_server: None, probe_url: str
) -> None:
    """El dashboard publico sigue abierto y **no** sirve para construir contexto F3."""
    async with inserted_tenant() as tenant:
        async with inserted_tournament_of(tenant.club) as tournament:
            public = JsonDict(await send_request(HTTPMethod.GET, f"tournaments/{tournament.id}"))
            without_token = await call_probe(
                HTTPMethod.GET, probe_url, f"probe/tournament/{tournament.id}"
            )
            with_public_dashboard = JsonDict(await send_request(HTTPMethod.GET, "users/me"))
    assert public["data"]["id"] == tournament.id
    assert without_token == (401, UNAUTHORIZED)
    assert with_public_dashboard == {"detail": "Not authenticated"}


def test_probe_routes_are_not_in_the_product_app() -> None:
    """Las rutas internas del gate no se han anadido al router productivo."""
    product_paths = {route.path for route in app.routes}
    assert not [path for path in product_paths if path.startswith("/probe")]
    assert ACCESS_TOKEN_EXPIRE_MINUTES == 7 * 24 * 60
    assert ClubId(1) == 1
    assert TournamentId(1) == 1
