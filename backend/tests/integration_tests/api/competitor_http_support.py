"""Arnes del contrato HTTP de competidores (S3.7a, F3B, LAB ONLY).

El laboratorio (dos tenants, cuatro sesiones y seis identidades) y las ayudas de peticion viven
aqui para que el modulo de pruebas se lea como el contrato que fija y no como su utileria.

Los nombres del laboratorio son unicos por ejecucion y comparten prefijo, para poder comprobar el
orden determinista del listado sin depender de lo que ya hubiera en la base de datos.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import aiohttp
from heliclockter import datetime_utc

from bracket.database import database
from bracket.models.db.club import ClubInsertable
from bracket.models.db.user_x_club import UserXClubInsertable, UserXClubRelation
from bracket.routes.domain_idempotency import IDEMPOTENCY_KEY_HEADER
from bracket.sql.users import update_user_active
from bracket.utils.dummy_records import DUMMY_TOURNAMENT
from bracket.utils.id_types import ClubId, CompetitorId, TournamentId
from tests.integration_tests.api.shared import get_root_uvicorn_url
from tests.integration_tests.mocks import get_mock_token, get_mock_user
from tests.integration_tests.registration_fixtures import (
    delete_competitor,
    delete_registration_audit,
    insert_competitor,
    set_competitor_active,
)
from tests.integration_tests.sql import (
    inserted_club,
    inserted_tournament,
    inserted_user,
    inserted_user_x_club,
)

#: Nombres del laboratorio: tres identidades activas, una ya de baja y una del otro tenant.
ORDERED_SUFFIXES = ("alfa", "media", "zeta")


@dataclass(frozen=True)
class CompetitorLab:  # pylint: disable=too-many-instance-attributes
    """Tenant con identidades propias, una de baja, una ajena y las cuatro sesiones del arnes."""

    club: ClubId
    other_club: ClubId
    tournament: TournamentId
    root_name: str
    active_competitors: tuple[CompetitorId, ...]
    inactive_competitor: CompetitorId
    foreign_competitor: CompetitorId
    owner_token: str
    collaborator_token: str
    inactive_user_token: str
    outsider_token: str

    @property
    def active_names(self) -> tuple[str, ...]:
        """Nombres activos en el orden determinista del contrato (nombre, id)."""
        return tuple(f"{self.root_name}-{suffix}" for suffix in ORDERED_SUFFIXES)


async def delete_competitor_audit(tenant_ids: tuple[int, ...]) -> None:
    """Retira la auditoria del laboratorio: ``domain_change_log`` tiene FK al tenant."""
    await database.execute(
        query="DELETE FROM domain_change_log WHERE tenant_club_id = ANY(:ids)",
        values={"ids": list(tenant_ids)},
    )


async def delete_all_competitors(tenant_id: int) -> None:
    """Retira todas las identidades del tenant, historial de nombres incluido.

    Se listan primero y se dan de baja una a una con el ayudante certificado: cubre tambien lo
    que la prueba crea por HTTP, que el laboratorio no conoce de antemano.
    """
    rows = await database.fetch_all(
        query="SELECT id FROM competitors WHERE managed_by_club_id = :tenant_id ORDER BY id",
        values={"tenant_id": tenant_id},
    )
    for row in rows:
        await database.execute(
            query="DELETE FROM competitors_name_history WHERE competitor_id = :c",
            values={"c": row["id"]},
        )
        await delete_competitor(CompetitorId(row["id"]))


@asynccontextmanager
async def competitor_lab_context() -> AsyncIterator[CompetitorLab]:
    """Datos y sesiones del laboratorio; la limpieza se apila de hojas a raices."""
    now = datetime_utc.now()
    suffix = uuid4().hex[:12]
    root_name = f"c{suffix}"

    async with AsyncExitStack() as stack:
        club = await stack.enter_async_context(
            inserted_club(ClubInsertable(name=f"f3b-s37a-{suffix}", created=now))
        )
        other_club = await stack.enter_async_context(
            inserted_club(ClubInsertable(name=f"f3b-s37a-other-{suffix}", created=now))
        )
        tournament = await stack.enter_async_context(
            inserted_tournament(
                DUMMY_TOURNAMENT.model_copy(
                    update={"club_id": club.id, "dashboard_endpoint": f"f3b-s37a-{suffix}"}
                )
            )
        )

        owner = await stack.enter_async_context(inserted_user(get_mock_user()))
        collaborator = await stack.enter_async_context(inserted_user(get_mock_user()))
        inactive_user = await stack.enter_async_context(inserted_user(get_mock_user()))
        outsider = await stack.enter_async_context(inserted_user(get_mock_user()))
        for user, target_club, relation in (
            (owner, club, UserXClubRelation.OWNER),
            (collaborator, club, UserXClubRelation.COLLABORATOR),
            (inactive_user, club, UserXClubRelation.OWNER),
            (outsider, other_club, UserXClubRelation.OWNER),
        ):
            await stack.enter_async_context(
                inserted_user_x_club(
                    UserXClubInsertable(user_id=user.id, club_id=target_club.id, relation=relation)
                )
            )
        await update_user_active(inactive_user.id, False)

        # Un generador asincrono no vale para tuple(): altas seguidas, una a una.
        active: list[CompetitorId] = []
        for name_suffix in ORDERED_SUFFIXES:
            active.append(
                await insert_competitor(f"{root_name}-{name_suffix}", club.id, created=now)
            )
        inactive = await insert_competitor(f"{root_name}-baja", club.id, created=now)
        await set_competitor_active(inactive, active=False)
        foreign = await insert_competitor(f"{root_name}-ajena", other_club.id, created=now)

        # Lo ultimo apilado se deshace primero. Las identidades se apilan antes que sus
        # dependencias para que, al deshacer, caigan despues: inscripciones -> competidores ->
        # auditoria -> clubes.
        stack.push_async_callback(delete_all_competitors, club.id)
        stack.push_async_callback(delete_all_competitors, other_club.id)
        stack.push_async_callback(delete_registration_audit, (tournament.id,), club.id)
        stack.push_async_callback(delete_competitor_audit, (club.id, other_club.id))

        yield CompetitorLab(
            club=club.id,
            other_club=other_club.id,
            tournament=tournament.id,
            root_name=root_name,
            active_competitors=tuple(active),
            inactive_competitor=inactive,
            foreign_competitor=foreign,
            owner_token=get_mock_token(owner.email),
            collaborator_token=get_mock_token(collaborator.email),
            inactive_user_token=get_mock_token(inactive_user.email),
            outsider_token=get_mock_token(outsider.email),
        )


def competitors_url(lab: CompetitorLab) -> str:
    """Coleccion de competidores del tenant del laboratorio."""
    return f"{get_root_uvicorn_url()}clubs/{lab.club}/competitors"


def competitor_url(lab: CompetitorLab, competitor_id: int) -> str:
    return f"{competitors_url(lab)}/{competitor_id}"


def action_url(lab: CompetitorLab, competitor_id: int, action: str) -> str:
    return f"{competitor_url(lab, competitor_id)}/{action}"


async def call(
    method: str,
    url: str,
    *,
    token: str | None = None,
    key: str | None = None,
    payload: dict[str, Any] | None = None,
    raw_body: str | None = None,
) -> tuple[int, Any]:
    """Peticion cruda contra el servidor de pruebas; la sesion es de la peticion.

    Con ``raw_body`` se envia un cuerpo **tal cual** (bytes): asi se puede firmar la huella de un
    cuerpo que no es un modelo valido sin que el cliente lo reescriba.
    """
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if key is not None:
        headers[IDEMPOTENCY_KEY_HEADER] = key
    async with aiohttp.ClientSession() as session:
        async with session.request(
            method,
            url,
            headers=headers,
            json=payload,
            data=None if raw_body is None else raw_body.encode(),
        ) as response:
            text = await response.text()
            return response.status, json.loads(text) if text else {}
