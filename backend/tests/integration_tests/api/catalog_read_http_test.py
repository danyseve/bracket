# pylint: disable=redefined-outer-name
"""Contrato HTTP de lectura de los catalogos de club (S4.8a-4a, LAB ONLY).

TDD: este modulo se escribe **antes** de la implementacion y falla en rojo (las tres rutas no
existen). Fija el contrato de solo lectura que el frontend de a-4 necesita para cargar el catalogo
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
#: Cinturones del seed de la migracion S4.8a-1 (``BELT_SEED``): dos escalas, cinco colores cada una.
EXPECTED_SCOPES: Final = {"JUVENILE", "ADULT"}
EXPECTED_CODES: Final = {"WHITE", "GREY", "YELLOW", "ORANGE", "GREEN", "BLUE", "PURPLE", "BROWN", "BLACK"}


@dataclass
class CatalogLab:
    """Tenant del laboratorio de competidores mas una academia activa, una inactiva y una ajena."""

    lab: CompetitorLab
    active_club: SportsClubId
    inactive_club: SportsClubId
    foreign_club: SportsClubId
    primary_affiliation: CompetitorXSportsClubId
    affiliated_competitor: CompetitorId


@asynccontextmanager
async def catalog_lab() -> AsyncIterator[CatalogLab]:
    """Laboratorio de lecturas de catalogo: academias del tenant y una afiliacion vigente."""
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
        try:
            yield CatalogLab(
                lab=lab,
                active_club=active_club,
                inactive_club=inactive_club,
                foreign_club=foreign_club,
                primary_affiliation=primary,
                affiliated_competitor=affiliated,
            )
        finally:
            # De hojas a raices: afiliaciones -> academias -> (competidores y clubes del arnes).
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


# --- 1. catalogo de cinturones -----------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_belts_publica_el_catalogo_de_plataforma() -> None:
    """El catalogo real de ``belts``: solo los campos publicos y ordenado por escala y rango."""
    async with catalog_lab() as catalog:
        status_code, body = await call("GET", belts_url(catalog.lab), token=catalog.lab.owner_token)

        assert status_code == status.HTTP_200_OK, body
        assert isinstance(body, list), body
        assert body, body
        assert all(set(row) == BELT_FIELDS for row in body), body
        # El catalogo del seed: dos escalas con los mismos cinco colores, todos activos.
        assert {row["scope"] for row in body} == EXPECTED_SCOPES
        assert EXPECTED_CODES <= {row["code"] for row in body}
        assert all(row["active"] is True for row in body), body
        # Orden total: por escala y, dentro de ella, por ``rank`` ascendente.
        for scope in EXPECTED_SCOPES:
            ranks = [row["rank"] for row in body if row["scope"] == scope]
            assert ranks == sorted(ranks), body


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
