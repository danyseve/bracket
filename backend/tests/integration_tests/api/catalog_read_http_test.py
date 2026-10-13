# pylint: disable=redefined-outer-name
"""Contrato HTTP de lectura de los catalogos de club (S4.8a-4a, LAB ONLY).

TDD: este modulo se escribio **antes** de la implementacion y fallo en rojo (las tres rutas no
existian). Fija el contrato de solo lectura que el frontend de a-4 necesita para cargar el catalogo
real de cinturones, listar las academias (sports clubs) y leer la afiliacion vigente de un
competidor:

* ``GET clubs/{club_id}/belts``: catalogo de la tabla ``belts`` (S4.8a-1), de **plataforma** (no
  pertenece a ninguna academia); se publica ``id, code, label, rank, scope, active, created``;
* ``GET clubs/{club_id}/sports-clubs``: academias del tenant, con ``id, name, active, logo_path``
  (la misma proyeccion ``SportsClubResponse`` que hoy solo viaja en la respuesta del logo);
* ``GET clubs/{club_id}/competitors/{competitor_id}/affiliations``: afiliaciones del competidor,
  con la proyeccion ``CompetitorAffiliationsResponse`` que hoy solo devuelven las rutas POST de
  afiliacion; sirve para que la UI precargue la academia vigente.

Ninguna de las tres escribe: no hay ``Idempotency-Key``, no hay auditoria y se publican con la
puerta de escritura F3 apagada. La autorizacion es la de los GET ya existentes bajo ``/clubs/...``:
el tenant de la ruta sale de la sesion (``actor_context_for_club``) y cualquier relacion con el club
puede leer (OWNER y COLLABORATOR -> 200; anonimo -> 401; actor de otro club -> 401, indistinguible
de "club inexistente"). El ambito se aplica **dentro** del SQL, nunca despues de leer.

Nota sobre los datos: el arnes de CI construye el esquema con ``metadata.create_all`` (no corre la
migracion S4.8a-1), asi que la tabla ``belts`` llega **vacia**. Este modulo siembra su propio
catalogo de prueba (codigos ``T-*``) para poder fijar forma y orden, y lo retira al terminar.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final
from uuid import uuid4

import pytest
from heliclockter import datetime_utc
from starlette import status

from bracket.database import database
from bracket.utils.id_types import CompetitorId, CompetitorXSportsClubId, SportsClubId
from tests.integration_tests.api.competitor_http_support import (
    CompetitorLab,
    call,
    competitor_lab_context,
)
from tests.integration_tests.api.shared import get_root_uvicorn_url
from tests.integration_tests.registration_fixtures import (
    delete_affiliations,
    delete_sports_club,
    insert_affiliation,
    insert_sports_club,
)

#: Proyeccion publica de un cinturon: un campo de mas cierra el contrato.
BELT_FIELDS: Final = {"id", "code", "label", "rank", "scope", "active", "created"}
#: Proyeccion publica de una academia (``SportsClubResponse``).
SPORTS_CLUB_FIELDS: Final = {"id", "name", "active", "logo_path"}
#: Proyeccion publica de una afiliacion.
AFFILIATION_FIELDS: Final = {
    "id",
    "competitor_id",
    "sports_club_id",
    "is_primary",
    "valid_from",
    "valid_to",
}

#: (scope, code, rank, active) del catalogo de prueba. Los ``rank`` altos y los codigos ``T-`` no
#: chocan con el seed de la migracion; la escala enum se castea en el SQL.
_TEST_BELTS: Final[tuple[tuple[str, str, int, bool], ...]] = (
    ("ADULT", "T-ADULT-WHITE", 101, True),
    ("ADULT", "T-ADULT-BLUE", 102, True),
    ("ADULT", "T-ADULT-RETIRED", 103, False),
    ("JUVENILE", "T-JUV-WHITE", 101, True),
)
#: Los vigentes de prueba, en el orden total que debe publicar la ruta. ``ORDER BY scope`` usa el
#: orden de declaracion del enum ``belt_scope`` (``JUVENILE`` antes que ``ADULT``), no alfabetico.
_ACTIVE_BELTS_ORDERED: Final = (
    ("JUVENILE", 101, "T-JUV-WHITE"),
    ("ADULT", 101, "T-ADULT-WHITE"),
    ("ADULT", 102, "T-ADULT-BLUE"),
)

_INSERT_BELT = """
    INSERT INTO belts (code, label, rank, scope, active, created)
    VALUES (:code, :label, :rank, CAST(:scope AS belt_scope), :active, :created)
    RETURNING id
"""


async def _insert_belt(code: str, rank: int, scope: str, *, active: bool, created: Any) -> int:
    """Siembra un cinturon de prueba: la tabla del arnes de CI no trae el catalogo de plataforma."""
    belt_id: int = await database.fetch_val(
        query=_INSERT_BELT,
        values={
            "code": code,
            "label": code,
            "rank": rank,
            "scope": scope,
            "active": active,
            "created": created,
        },
    )
    return belt_id


async def _delete_belt(belt_id: int) -> None:
    await database.execute(
        query="DELETE FROM belts WHERE id = :belt_id", values={"belt_id": belt_id}
    )


@dataclass
class CatalogLab:
    """Tenant del laboratorio de competidores mas academias propias/ajenas y cinturones."""

    lab: CompetitorLab
    active_club: SportsClubId
    inactive_club: SportsClubId
    foreign_club: SportsClubId
    primary_affiliation: CompetitorXSportsClubId
    affiliated_competitor: CompetitorId
    belt_ids: tuple[int, ...]


@asynccontextmanager
async def catalog_lab() -> AsyncIterator[CatalogLab]:
    """Laboratorio de lecturas de catalogo: academias del tenant, afiliacion y cinturones."""
    async with competitor_lab_context() as lab:
        now = datetime_utc.now()
        suffix = uuid4().hex[:8]
        active_club = await insert_sports_club(
            f"acad-on-{suffix}", lab.club, active=True, created=now
        )
        inactive_club = await insert_sports_club(
            f"acad-off-{suffix}", lab.club, active=False, created=now
        )
        foreign_club = await insert_sports_club(
            f"acad-foreign-{suffix}", lab.other_club, active=True, created=now
        )
        affiliated = lab.active_competitors[0]
        primary = await insert_affiliation(
            competitor_id=affiliated,
            sports_club_id=active_club,
            is_primary=True,
            created=now,
        )
        # Una comprension con ``await`` como generador seria un generador asincrono (no vale para
        # ``tuple()``): altas seguidas, una a una, como el resto del arnes.
        belt_ids_list: list[int] = []
        for scope, code, rank, active in _TEST_BELTS:
            belt_ids_list.append(await _insert_belt(code, rank, scope, active=active, created=now))
        belt_ids = tuple(belt_ids_list)
        try:
            yield CatalogLab(
                lab=lab,
                active_club=active_club,
                inactive_club=inactive_club,
                foreign_club=foreign_club,
                primary_affiliation=primary,
                affiliated_competitor=affiliated,
                belt_ids=belt_ids,
            )
        finally:
            # De hojas a raices: cinturones/afiliaciones -> academias -> competidores y clubes.
            for belt_id in belt_ids:
                await _delete_belt(belt_id)
            for competitor_id in lab.active_competitors:
                await delete_affiliations(competitor_id)
            for sports_club_id in (active_club, inactive_club, foreign_club):
                await delete_sports_club(sports_club_id)


def belts_url(lab: CompetitorLab) -> str:
    return f"{get_root_uvicorn_url()}clubs/{lab.club}/belts"


def sports_clubs_url(lab: CompetitorLab) -> str:
    return f"{get_root_uvicorn_url()}clubs/{lab.club}/sports-clubs"


def affiliations_url(lab: CompetitorLab, competitor_id: int) -> str:
    club = lab.club
    return f"{get_root_uvicorn_url()}clubs/{club}/competitors/{competitor_id}/affiliations"


def _my_belt_sequence(body: list[Any]) -> list[tuple[Any, Any, Any]]:
    """El subconjunto de cinturones de prueba, en el orden en que la ruta los publico."""
    return [
        (row["scope"], row["rank"], row["code"]) for row in body if row["code"].startswith("T-")
    ]


# --- 1. catalogo de cinturones -----------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_belts_publica_el_catalogo_de_la_tabla_belts() -> None:
    """El catalogo real de ``belts``: solo los campos publicos y ordenado por escala y rango."""
    async with catalog_lab() as catalog:
        status_code, body = await call("GET", belts_url(catalog.lab), token=catalog.lab.owner_token)

        assert status_code == status.HTTP_200_OK, body
        assert isinstance(body, list), body
        assert all(set(row) == BELT_FIELDS for row in body), body
        # Solo los vigentes, en el orden total (escala y, dentro de ella, ``rank`` ascendente).
        assert _my_belt_sequence(body) == list(_ACTIVE_BELTS_ORDERED), body
        assert all(row["active"] is True for row in body if row["code"].startswith("T-")), body


@pytest.mark.asyncio(loop_scope="session")
async def test_belts_incluye_los_retirados_si_se_pide() -> None:
    """``include_inactive=true`` anade los retirados (``active = false``), omitidos por defecto."""
    async with catalog_lab() as catalog:
        url = f"{belts_url(catalog.lab)}?include_inactive=true"
        status_code, body = await call("GET", url, token=catalog.lab.owner_token)

        assert status_code == status.HTTP_200_OK, body
        retired = {row["code"]: row for row in body if row["code"] == "T-ADULT-RETIRED"}
        assert retired, body
        assert retired["T-ADULT-RETIRED"]["active"] is False


@pytest.mark.asyncio(loop_scope="session")
async def test_belts_exige_pertenencia_al_club() -> None:
    """Cualquier relacion con el club lee; el anonimo y el actor de otro club no."""
    async with catalog_lab() as catalog:
        lab = catalog.lab
        collaborator_status, collaborator_body = await call(
            "GET", belts_url(lab), token=lab.collaborator_token
        )
        anonymous_status, _ = await call("GET", belts_url(lab), token=None)
        outsider_status, _ = await call("GET", belts_url(lab), token=lab.outsider_token)

        assert collaborator_status == status.HTTP_200_OK, collaborator_body
        assert anonymous_status == status.HTTP_401_UNAUTHORIZED
        assert outsider_status == status.HTTP_401_UNAUTHORIZED


# --- 2. academias (sports clubs) del tenant ----------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_sports_clubs_lista_solo_las_del_tenant_activas() -> None:
    """Publica las academias activas del tenant; ni la de baja ni la ajena aparecen."""
    async with catalog_lab() as catalog:
        status_code, body = await call(
            "GET", sports_clubs_url(catalog.lab), token=catalog.lab.owner_token
        )

        assert status_code == status.HTTP_200_OK, body
        assert isinstance(body, list), body
        ids = {row["id"] for row in body}
        assert catalog.active_club in ids, body
        assert catalog.inactive_club not in ids, body
        assert catalog.foreign_club not in ids, body
        assert all(set(row) == SPORTS_CLUB_FIELDS for row in body), body
        fresh = next(row for row in body if row["id"] == catalog.active_club)
        assert fresh["active"] is True
        assert fresh["logo_path"] is None


@pytest.mark.asyncio(loop_scope="session")
async def test_sports_clubs_incluye_las_de_baja_si_se_pide() -> None:
    """``include_inactive=true`` anade la de baja; la de otro tenant jamas entra."""
    async with catalog_lab() as catalog:
        url = f"{sports_clubs_url(catalog.lab)}?include_inactive=true"
        status_code, body = await call("GET", url, token=catalog.lab.owner_token)

        assert status_code == status.HTTP_200_OK, body
        ids = {row["id"] for row in body}
        assert catalog.active_club in ids, body
        assert catalog.inactive_club in ids, body
        assert catalog.foreign_club not in ids, body


@pytest.mark.asyncio(loop_scope="session")
async def test_sports_clubs_exige_pertenencia_al_club() -> None:
    async with catalog_lab() as catalog:
        lab = catalog.lab
        collaborator_status, collaborator_body = await call(
            "GET", sports_clubs_url(lab), token=lab.collaborator_token
        )
        anonymous_status, _ = await call("GET", sports_clubs_url(lab), token=None)
        outsider_status, _ = await call("GET", sports_clubs_url(lab), token=lab.outsider_token)

        assert collaborator_status == status.HTTP_200_OK, collaborator_body
        assert anonymous_status == status.HTTP_401_UNAUTHORIZED
        assert outsider_status == status.HTTP_401_UNAUTHORIZED


# --- 3. afiliaciones vigentes de un competidor -------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_affiliations_devuelve_la_vigente() -> None:
    """La afiliacion vigente (``valid_to`` NULL) con la proyeccion publica existente."""
    async with catalog_lab() as catalog:
        lab = catalog.lab
        status_code, body = await call(
            "GET",
            affiliations_url(lab, catalog.affiliated_competitor),
            token=lab.owner_token,
        )

        assert status_code == status.HTTP_200_OK, body
        assert set(body) == {"competitor_id", "affiliations"}, body
        assert body["competitor_id"] == catalog.affiliated_competitor
        assert len(body["affiliations"]) == 1, body
        affiliation: dict[str, Any] = body["affiliations"][0]
        assert set(affiliation) == AFFILIATION_FIELDS, affiliation
        assert affiliation["id"] == catalog.primary_affiliation
        assert affiliation["sports_club_id"] == catalog.active_club
        assert affiliation["is_primary"] is True
        assert affiliation["valid_to"] is None


@pytest.mark.asyncio(loop_scope="session")
async def test_affiliations_de_competidor_ajeno_es_404() -> None:
    """Un competidor de otro tenant no existe para la ruta: 404, no una lista vacia."""
    async with catalog_lab() as catalog:
        lab = catalog.lab
        status_code, _body = await call(
            "GET", affiliations_url(lab, lab.foreign_competitor), token=lab.owner_token
        )

        assert status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio(loop_scope="session")
async def test_affiliations_exige_pertenencia_al_club() -> None:
    async with catalog_lab() as catalog:
        lab = catalog.lab
        url = affiliations_url(lab, catalog.affiliated_competitor)
        collaborator_status, collaborator_body = await call(
            "GET", url, token=lab.collaborator_token
        )
        anonymous_status, _ = await call("GET", url, token=None)
        outsider_status, _ = await call("GET", url, token=lab.outsider_token)

        assert collaborator_status == status.HTTP_200_OK, collaborator_body
        assert anonymous_status == status.HTTP_401_UNAUTHORIZED
        assert outsider_status == status.HTTP_401_UNAUTHORIZED
