# pylint: disable=redefined-outer-name
"""Contrato HTTP del logo de la academia (S4.8a-3 §4, LAB ONLY).

TDD: este modulo se escribe **antes** de la implementacion y falla en rojo (la ruta y su columna no
existen). Fija el contrato, siguiendo el patron del logo de torneo pero endurecido:

* solo PNG, JPEG y WEBP; el tipo se decide por el **contenido real** (firma), no por el
  ``Content-Type`` ni por la extension declarados: un SVG (o cualquier otra cosa) se rechaza;
* hay un **tamano maximo** (constante del modulo, 2 MB) y un rechazo explicito si se supera;
* el **nombre del fichero lo genera el servidor** (uuid): la extension y el nombre del cliente no
  llegan al disco y no hay forma de salir de la carpeta del logo (path traversal imposible);
* en la base solo vive la **ruta relativa** (``sports_clubs.logo_path``, nullable); el fichero, en
  disco;
* soporta **upload**, **replace** y **remove** (sin fichero). El logo anterior **no** se borra hasta
  que el nuevo esta validado y persistido: si el nuevo se rechaza, el anterior sobrevive intacto;
* **auditoria**: cada cambio confirmado deja un evento de la academia con el codigo cerrado
  ``CLUB_LOGO_CHANGE``;
* **autorizacion**: OWNER-only; el anonimo recibe 401 y el COLLABORATOR 403.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import aiohttp
import pytest
from heliclockter import datetime_utc
from starlette import status

from bracket.config import config
from bracket.database import database
from bracket.routes.domain_sports_clubs import MAX_LOGO_BYTES
from bracket.utils.id_types import SportsClubId
from tests.integration_tests.api.competitor_http_support import (
    CompetitorLab,
    competitor_lab_context,
)
from tests.integration_tests.api.shared import get_root_uvicorn_url
from tests.integration_tests.registration_fixtures import (
    delete_sports_club,
    insert_sports_club,
)

#: Proyeccion publica del club: la ruta del logo es lo unico que se publica de mas.
CLUB_FIELDS: Final = {"id", "name", "active", "logo_path"}
LOGO_DIR: Final = Path("static/sports-club-logos")

PNG_BYTES: Final = b"\x89PNG\r\n\x1a\n" + b"logo-png-payload" * 4
JPEG_BYTES: Final = b"\xff\xd8\xff\xe0" + b"logo-jpeg-payload" * 4
WEBP_BYTES: Final = b"RIFF" + (24).to_bytes(4, "little") + b"WEBP" + b"logo-webp-payload" * 4
SVG_BYTES: Final = b"<svg xmlns='http://www.w3.org/2000/svg'><rect/></svg>"


@pytest.fixture()
def flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Escritura habilitada de forma explicita: el contrato no depende del entorno de CI."""
    monkeypatch.setattr(config, "f3_write_enabled", True)


@asynccontextmanager
async def logo_lab() -> AsyncIterator[tuple[CompetitorLab, SportsClubId, list[str]]]:
    """Tenant del laboratorio con una academia propia y seguimiento de ficheros creados."""
    async with competitor_lab_context() as lab:
        created_files: list[str] = []
        sports_club_id = await insert_sports_club(
            f"acad-logo-{uuid4().hex[:8]}", lab.club, active=True, created=datetime_utc.now()
        )
        try:
            yield lab, sports_club_id, created_files
        finally:
            for relative in created_files:
                Path(relative).unlink(missing_ok=True)
            await delete_sports_club(sports_club_id)


def logo_url(lab: CompetitorLab, sports_club_id: int) -> str:
    return f"{get_root_uvicorn_url()}clubs/{lab.club}/sports-clubs/{sports_club_id}/logo"


async def post_logo(
    lab: CompetitorLab,
    sports_club_id: int,
    *,
    token: str | None,
    filename: str | None = None,
    content: bytes | None = None,
    content_type: str = "image/png",
) -> tuple[int, Any]:
    """Sube (o quita, sin fichero) el logo; el cuerpo es multipart/form-data."""
    form = aiohttp.FormData()
    if filename is not None and content is not None:
        form.add_field("file", content, filename=filename, content_type=content_type)
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    async with aiohttp.ClientSession() as session:
        async with session.post(logo_url(lab, sports_club_id), data=form, headers=headers) as resp:
            text = await resp.text()
            return resp.status, (json.loads(text) if text else {})


def track(created_files: list[str], body: dict[str, Any]) -> str:
    """Registra el fichero recien creado para retirarlo al cerrar el laboratorio."""
    relative = body["logo_path"]
    created_files.append(relative)
    return relative


async def persisted_logo_path(sports_club_id: int) -> str | None:
    value = await database.fetch_val(
        query="SELECT logo_path FROM sports_clubs WHERE id = :s", values={"s": sports_club_id}
    )
    return None if value is None else str(value)


# --- 1. formatos validos -----------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("filename", "content", "content_type", "suffix"),
    [
        ("logo.png", PNG_BYTES, "image/png", ".png"),
        ("logo.jpg", JPEG_BYTES, "image/jpeg", ".jpg"),
        ("logo.webp", WEBP_BYTES, "image/webp", ".webp"),
    ],
)
async def test_supported_formats_are_accepted(
    flag_on: None,
    filename: str,
    content: bytes,
    content_type: str,
    suffix: str,
) -> None:
    """PNG, JPEG y WEBP se aceptan; el nombre lo genera el servidor y el fichero va a disco."""
    async with logo_lab() as (lab, sports_club_id, created_files):
        status_code, body = await post_logo(
            lab,
            sports_club_id,
            token=lab.owner_token,
            filename=filename,
            content=content,
            content_type=content_type,
        )

        assert status_code == status.HTTP_200_OK, body
        assert set(body) == CLUB_FIELDS
        logo_path = track(created_files, body)
        assert logo_path.startswith("static/sports-club-logos/")
        assert logo_path.endswith(suffix)
        assert Path(logo_path).name != filename
        assert Path(logo_path).exists()
        assert await persisted_logo_path(sports_club_id) == logo_path


# --- 2. tipos no soportados --------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("filename", "content", "content_type"),
    [
        ("logo.svg", SVG_BYTES, "image/svg+xml"),
        ("logo.svg", SVG_BYTES, "image/png"),
        ("logo.png", b"not really an image", "image/png"),
        ("logo.gif", b"GIF89a", "image/gif"),
        ("logo.txt", b"texto plano", "text/plain"),
    ],
)
async def test_unsupported_content_is_rejected(
    flag_on: None, filename: str, content: bytes, content_type: str
) -> None:
    """Un SVG (o cualquier contenido no PNG/JPEG/WEBP) se rechaza; nada se persiste."""
    async with logo_lab() as (lab, sports_club_id, _files):
        status_code, _body = await post_logo(
            lab,
            sports_club_id,
            token=lab.owner_token,
            filename=filename,
            content=content,
            content_type=content_type,
        )

        assert status_code == status.HTTP_400_BAD_REQUEST
        assert await persisted_logo_path(sports_club_id) is None


# --- 3. tamano maximo --------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_oversized_logo_is_rejected(flag_on: None) -> None:
    """Un fichero por encima del maximo del modulo se rechaza sin escribir nada."""
    async with logo_lab() as (lab, sports_club_id, _files):
        oversized = b"\x89PNG\r\n\x1a\n" + b"\x00" * MAX_LOGO_BYTES
        status_code, _body = await post_logo(
            lab,
            sports_club_id,
            token=lab.owner_token,
            filename="logo.png",
            content=oversized,
            content_type="image/png",
        )

        assert status_code == status.HTTP_400_BAD_REQUEST
        assert await persisted_logo_path(sports_club_id) is None


# --- 4. replace --------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_replace_swaps_the_file_and_removes_the_previous_one(flag_on: None) -> None:
    """Reemplazar cambia la ruta, borra el fichero anterior y conserva el nuevo."""
    async with logo_lab() as (lab, sports_club_id, created_files):
        _, first = await post_logo(
            lab,
            sports_club_id,
            token=lab.owner_token,
            filename="logo.png",
            content=PNG_BYTES,
        )
        old_path = track(created_files, first)
        _, second = await post_logo(
            lab,
            sports_club_id,
            token=lab.owner_token,
            filename="logo.jpg",
            content=JPEG_BYTES,
            content_type="image/jpeg",
        )
        new_path = track(created_files, second)

        assert new_path != old_path
        assert new_path.endswith(".jpg")
        assert Path(new_path).exists()
        assert not Path(old_path).exists()
        assert await persisted_logo_path(sports_club_id) == new_path


# --- 5. remove ---------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_remove_clears_the_column_and_the_file(flag_on: None) -> None:
    """Sin fichero, la ruta queda en ``NULL`` y el logo anterior se retira del disco."""
    async with logo_lab() as (lab, sports_club_id, created_files):
        _, uploaded = await post_logo(
            lab,
            sports_club_id,
            token=lab.owner_token,
            filename="logo.png",
            content=PNG_BYTES,
        )
        uploaded_path = track(created_files, uploaded)

        status_code, body = await post_logo(lab, sports_club_id, token=lab.owner_token)

        assert status_code == status.HTTP_200_OK, body
        assert body["logo_path"] is None
        assert not Path(uploaded_path).exists()
        assert await persisted_logo_path(sports_club_id) is None


# --- 6. el anterior sobrevive al rechazo -------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_previous_logo_survives_a_rejected_upload(flag_on: None) -> None:
    """Si el nuevo logo se rechaza, el anterior sigue en la base y en disco."""
    async with logo_lab() as (lab, sports_club_id, created_files):
        _, uploaded = await post_logo(
            lab,
            sports_club_id,
            token=lab.owner_token,
            filename="logo.png",
            content=PNG_BYTES,
        )
        keep_path = track(created_files, uploaded)

        status_code, _body = await post_logo(
            lab,
            sports_club_id,
            token=lab.owner_token,
            filename="evil.svg",
            content=SVG_BYTES,
            content_type="image/svg+xml",
        )

        assert status_code == status.HTTP_400_BAD_REQUEST
        assert await persisted_logo_path(sports_club_id) == keep_path
        assert Path(keep_path).exists()


# --- 7. autorizacion OWNER-only ----------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_logo_is_owner_only(flag_on: None) -> None:
    """COLLABORATOR recibe 403 y el anonimo 401; no se escribe ni se guarda fichero."""
    async with logo_lab() as (lab, sports_club_id, _files):
        collaborator_status, _ = await post_logo(
            lab,
            sports_club_id,
            token=lab.collaborator_token,
            filename="logo.png",
            content=PNG_BYTES,
        )
        anonymous_status, _ = await post_logo(
            lab,
            sports_club_id,
            token=None,
            filename="logo.png",
            content=PNG_BYTES,
        )

        assert collaborator_status == status.HTTP_403_FORBIDDEN
        assert anonymous_status == status.HTTP_401_UNAUTHORIZED
        assert await persisted_logo_path(sports_club_id) is None
