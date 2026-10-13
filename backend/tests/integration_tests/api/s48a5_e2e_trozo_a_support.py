# pylint: disable=redefined-outer-name
"""Soporte del E2E realista de S4.8a-5 (TROZO A §3-§7): escenario LAB reusable.

Contiene el **constructor del escenario** y las ayudas de peticion que el modulo de pruebas
usa para fijar el contrato de validacion E2E. No contiene pruebas (el nombre no termina en
``_test``): pytest no lo recoge.

Que monta (§3-§7 de la especificacion S4.8a-5):

* un torneo LAB **aislado** (club y torneo propios, nunca los de otro modulo);
* **tres academias** del tenant: A con logo PNG, B con logo JPEG, C sin logo;
* **veinticuatro competidores F3** (creados por el camino HTTP real, puerta F3 + idempotencia),
  repartidos 7/7/7 afiliados a A/B/C y **3 independientes**, con DOB, sexo y cinturon del
  catalogo real ``belts``, y varios deliberadamente **incompletos** (sin DOB, sin cinturon,
  sin sexo) para ejercitar blockers en los trozos B/C/D;
* **cinco categorias estructuradas** con reglas reales en columnas de ``stage_items``
  (sexo / rango de cinturon / rango de edad / rango de peso / modalidad) y **una legacy** sin
  ninguna regla (para §20);
* la **afiliacion primaria** de cada competidor afiliado y una **inscripcion historica
  congelada** de referencia para comprobar que cambiar de club no la toca.

El escenario se apoya en el arnes HTTP ya certificado del repositorio
(``competitor_lab_context``, ``insert_sports_club``) y en la base de laboratorio
``bracket_ci``: no hay mocks del motor ni del protocolo.
"""

from __future__ import annotations

import io
import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final
from uuid import uuid4
from zoneinfo import ZoneInfo

import aiohttp
from heliclockter import datetime_utc

from bracket.config import config
from bracket.database import database
from bracket.routes.domain_idempotency import IDEMPOTENCY_KEY_HEADER
from bracket.utils.id_types import (
    CompetitorId,
    RankingId,
    SportsClubId,
    StageId,
    StageItemId,
    TournamentId,
)
from tests.integration_tests.api.competitor_http_support import (
    CompetitorLab,
    call,
    competitor_lab_context,
)
from tests.integration_tests.api.shared import get_root_uvicorn_url
# Los bytes de imagen validos ya viven (y se prueban) en el contrato del logo de a-3: se reutilizan.
from tests.integration_tests.api.sports_club_logo_http_test import (  # noqa: F401
    JPEG_BYTES,
    PNG_BYTES,
    WEBP_BYTES,
)
from tests.integration_tests.registration_fixtures import (
    delete_sports_club,
    insert_sports_club,
)

#: Momento de creacion comun de las filas del escenario (independiente del reloj real).
CREATED: Final = datetime_utc(2026, 1, 1, 0, 0, 0, tzinfo=ZoneInfo("UTC"))
#: Fecha de inicio del torneo: la edad de clasificacion se calcula contra ella, no contra el reloj.
START_TIME: Final = datetime_utc(2026, 10, 13, 9, 0, 0, tzinfo=ZoneInfo("UTC"))
#: Resumen congelable de §25 (lo escribe el fixture de modulo al cerrar).
EVIDENCE_PATH: Final = Path("/home/ubuntu/s48a5-evidence/trozo-a.json")
#: Prefijo de los nombres del escenario dentro del tenant (permite contar sin ruido).
NAME_PREFIX: Final = "e2e"

#: Cinturones de la escala ADULT que sembramos (codigo -> (etiqueta, rango)).
ADULT_BELTS: Final[dict[str, tuple[str, int]]] = {
    "WHITE": ("White", 1),
    "BLUE": ("Blue", 2),
    "PURPLE": ("Purple", 3),
    "BROWN": ("Brown", 4),
    "BLACK": ("Black", 5),
}

#: Reparto de las tres academias: A (PNG), B (JPEG), C (sin logo).
ACADEMY_LETTERS: Final = ("A", "B", "C")

#: Detalle fijo del rechazo por datos invalidos (no repite el valor recibido).
INVALID_DETAIL: Final = "Competitor request is invalid"

_INSERT_STAGE = """
    INSERT INTO stages (tournament_id, name, created, is_active)
    VALUES (:tournament_id, :name, :created, true) RETURNING id
"""
_INSERT_RANKING = """
    INSERT INTO rankings (
        created, tournament_id, position, win_points, draw_points, loss_points, add_score_points
    ) VALUES (:created, :tournament_id, 1, 3, 1, 0, false) RETURNING id
"""
_UPSERT_BELT = """
    INSERT INTO belts (code, label, rank, scope, active)
    VALUES (:code, :label, :rank, CAST(:scope AS belt_scope), true)
    ON CONFLICT (scope, code) DO NOTHING
"""
_SELECT_BELT = """
    SELECT id FROM belts WHERE scope = CAST(:scope AS belt_scope) AND code = :code
"""
_INSERT_STAGE_ITEM = """
    INSERT INTO stage_items (
        name, stage_id, team_count, ranking_id, type, sex_constraint, age_min, age_max,
        weight_min_kg, weight_max_kg, modality, belt_min_id, belt_max_id
    ) VALUES (
        :name, :stage_id, 2, :ranking_id, CAST('ROUND_ROBIN' AS stage_type),
        CAST(:sex_constraint AS sex_constraint), :age_min, :age_max,
        :weight_min_kg, :weight_max_kg, CAST(:modality AS modality), :belt_min_id, :belt_max_id
    ) RETURNING id
"""
_INSERT_FROZEN_REGISTRATION = """
    INSERT INTO tournament_registrations (
        tournament_id, competitor_id, identity_status, representation,
        sports_club_id, competitor_name_snapshot, sports_club_name_snapshot, affiliation_id,
        status, revision, created
    ) VALUES (
        :tournament_id, :competitor_id,
        CAST('VERIFIED' AS registration_identity_status),
        CAST('CLUB' AS registration_representation),
        :sports_club_id, :name_snapshot, :club_snapshot, :affiliation_id,
        CAST('CONFIRMED' AS registration_status), 1, NOW()
    )
    RETURNING id
"""
_DELETE_FROZEN_REGISTRATIONS = "DELETE FROM tournament_registrations WHERE tournament_id = :t"
_DELETE_AFFILIATIONS = """
    DELETE FROM competitors_x_sports_clubs
    WHERE competitor_id IN (SELECT id FROM competitors WHERE managed_by_club_id = :t)
"""
_NULL_TENANT_BELTS = "UPDATE competitors SET belt_id = NULL WHERE managed_by_club_id = :t"
_DELETE_STAGE_ITEMS = "DELETE FROM stage_items WHERE id = ANY(:ids)"
_DELETE_RANKING = "DELETE FROM rankings WHERE id = :id"
_DELETE_STAGE = "DELETE FROM stages WHERE id = :id"
_DELETE_SPORTS_CLUBS = "DELETE FROM sports_clubs WHERE id = ANY(:ids)"
_DELETE_BELTS = "DELETE FROM belts WHERE id = ANY(:ids)"

_COUNT_COMPETITORS_BY_PREFIX = (
    "SELECT count(*) FROM competitors WHERE display_name LIKE :prefix"
)
_COUNT_OPEN_PRIMARY = """
    SELECT count(*) FROM competitors_x_sports_clubs
    WHERE competitor_id = :competitor_id AND valid_to IS NULL AND is_primary
"""
_COUNT_OPEN = """
    SELECT count(*) FROM competitors_x_sports_clubs
    WHERE competitor_id = :competitor_id AND valid_to IS NULL
"""
_SELECT_STAGE_ITEM = """
    SELECT name, sex_constraint, belt_min_id, belt_max_id, age_min, age_max,
           weight_min_kg, weight_max_kg, modality
    FROM stage_items WHERE id = :id
"""
_SELECT_PROFILE = """
    SELECT date_of_birth, sex, belt_id FROM competitors WHERE id = :competitor_id
"""
_SELECT_REGISTRATION = """
    SELECT competitor_id, sports_club_id, affiliation_id, sports_club_name_snapshot,
           competitor_name_snapshot, status, representation
    FROM tournament_registrations WHERE id = :id
"""
_SELECT_AFFILIATION_HISTORY = """
    SELECT id, sports_club_id, is_primary, valid_from, valid_to
    FROM competitors_x_sports_clubs
    WHERE competitor_id = :competitor_id
    ORDER BY id
"""


@dataclass(frozen=True)
class CompetitorSpec:
    """Un competidor del escenario: clave, academia destino (o ``None`` = independiente) y perfil.

    Los campos de perfil pueden ser ``None`` a proposito: el E2E necesita casos incompletos.
    """

    key: str
    academy: str | None
    date_of_birth: date | None
    sex: str | None
    belt_code: str | None


#: Los veinticuatro competidores (§5): 7 en A, 7 en B, 7 en C y 3 independientes.
#: Casos incompletos deliberados: ``a7`` sin DOB, ``b6`` sin cinturon, ``b7`` sin sexo,
#: ``c7`` sin ninguno de los tres.
COMPETITOR_SPECS: Final[tuple[CompetitorSpec, ...]] = (
    CompetitorSpec("a1", "A", date(1990, 5, 17), "MALE", "WHITE"),
    CompetitorSpec("a2", "A", date(1988, 2, 29), "MALE", "BLUE"),
    CompetitorSpec("a3", "A", date(2001, 11, 3), "FEMALE", "PURPLE"),
    CompetitorSpec("a4", "A", date(1995, 7, 21), "MALE", "BROWN"),
    CompetitorSpec("a5", "A", date(1999, 1, 9), "FEMALE", "BLUE"),
    CompetitorSpec("a6", "A", date(1993, 3, 30), "MALE", "BLACK"),
    CompetitorSpec("a7", "A", None, "MALE", "WHITE"),
    CompetitorSpec("b1", "B", date(1992, 6, 11), "FEMALE", "WHITE"),
    CompetitorSpec("b2", "B", date(2000, 9, 25), "MALE", "PURPLE"),
    CompetitorSpec("b3", "B", date(1985, 12, 1), "MALE", "BLUE"),
    CompetitorSpec("b4", "B", date(1997, 4, 18), "FEMALE", "BROWN"),
    CompetitorSpec("b5", "B", date(2003, 8, 8), "FEMALE", "WHITE"),
    CompetitorSpec("b6", "B", date(1991, 10, 10), "MALE", None),
    CompetitorSpec("b7", "B", date(1994, 2, 14), None, "WHITE"),
    CompetitorSpec("c1", "C", date(1989, 1, 19), "MALE", "BLACK"),
    CompetitorSpec("c2", "C", date(1996, 5, 5), "FEMALE", "BLUE"),
    CompetitorSpec("c3", "C", date(2002, 3, 3), "MALE", "WHITE"),
    CompetitorSpec("c4", "C", date(1987, 11, 11), "FEMALE", "PURPLE"),
    CompetitorSpec("c5", "C", date(1998, 7, 7), "MALE", "BROWN"),
    CompetitorSpec("c6", "C", date(1986, 6, 6), "FEMALE", "BLACK"),
    CompetitorSpec("c7", "C", None, None, None),
    CompetitorSpec("i1", None, date(1990, 10, 10), "MALE", "BLUE"),
    CompetitorSpec("i2", None, date(2001, 1, 1), "FEMALE", "WHITE"),
    CompetitorSpec("i3", None, date(1984, 4, 4), "MALE", "BLACK"),
)

#: Nombre de la categoria legacy del escenario (sin reglas, solo para §20).
LEGACY_CATEGORY: Final = "legacy-open"

#: Las cinco categorias estructuradas (§7), con reglas reales. Los cinturones se resuelven por
#: codigo despues de sembrar el catalogo. ``None`` = sin restriccion en ese criterio.
CATEGORY_SPECS: Final[tuple[dict[str, Any], ...]] = (
    {
        "name": "cat-male-gi-light",
        "sex_constraint": "MALE",
        "age_min": 18,
        "age_max": 39,
        "weight_min_kg": Decimal("71.00"),
        "weight_max_kg": Decimal("77.00"),
        "modality": "GI",
        "belt_min": "WHITE",
        "belt_max": "PURPLE",
    },
    {
        "name": "cat-male-gi-heavy",
        "sex_constraint": "MALE",
        "age_min": None,
        "age_max": None,
        "weight_min_kg": Decimal("77.01"),
        "weight_max_kg": Decimal("120.00"),
        "modality": "GI",
        "belt_min": None,
        "belt_max": None,
    },
    {
        "name": "cat-female-gi",
        "sex_constraint": "FEMALE",
        "age_min": None,
        "age_max": None,
        "weight_min_kg": Decimal("48.00"),
        "weight_max_kg": Decimal("64.00"),
        "modality": "GI",
        "belt_min": None,
        "belt_max": None,
    },
    {
        "name": "cat-open-nogi",
        "sex_constraint": "MIXED",
        "age_min": None,
        "age_max": None,
        "weight_min_kg": Decimal("60.00"),
        "weight_max_kg": Decimal("100.00"),
        "modality": "NO_GI",
        "belt_min": None,
        "belt_max": None,
    },
    # Categoria estructurada anadida para demostrar, con reglas REALES, el caso obligatorio
    # "multiples categorias compatibles" (§8): la especificacion (§3) prohibe apoyarlo en una
    # categoria legacy sin reglas. Es la clase master masculina de GI. El solape con
    # ``cat-male-gi-heavy`` esta disenado para que exista SOLO para el titular del caso (b), ``b3``:
    #   * ``age_min=40`` deja fuera a ``a2`` (38 anios, caso a) y a ``a6`` (33 anios, caso h);
    #   * ``belt_max=BROWN`` deja fuera a ``i3`` (negro, caso c) y ``belt_min=WHITE`` mantiene el
    #     cinturon azul de ``b3`` dentro del rango;
    #   * ``weight 77.01-120.00`` coincide con ``cat-male-gi-heavy``, de modo que ``b3`` (85.00 kg)
    #     cae a la vez en heavy y en esta master, y en ninguna otra (light corta en 39 anios).
    # Ningun competidor que usen los casos (a), (c)-(i) entra aqui: su resultado no cambia.
    {
        "name": "cat-male-gi-master",
        "sex_constraint": "MALE",
        "age_min": 40,
        "age_max": None,
        "weight_min_kg": Decimal("77.01"),
        "weight_max_kg": Decimal("120.00"),
        "modality": "GI",
        "belt_min": "WHITE",
        "belt_max": "BROWN",
    },
)


@dataclass
class E2EDataset:  # pylint: disable=too-many-instance-attributes
    """Escenario LAB completo: ids reales y datos para reutilizar en los trozos B/C/D."""

    lab: CompetitorLab
    stage: StageId
    ranking: RankingId
    belts: dict[str, int]
    clubs: dict[str, SportsClubId]
    logo_paths: dict[str, str | None]
    competitors: dict[str, CompetitorId] = field(default_factory=dict)
    academy_of: dict[str, str | None] = field(default_factory=dict)
    categories: dict[str, StageItemId] = field(default_factory=dict)
    affiliations: dict[str, int] = field(default_factory=dict)
    frozen_registration: int = 0
    name_prefix: str = NAME_PREFIX

    @property
    def club(self) -> int:
        """Tenant del escenario (el club de la ruta)."""
        return int(self.lab.club)

    @property
    def tournament(self) -> TournamentId:
        return self.lab.tournament

    @property
    def owner_token(self) -> str:
        return self.lab.owner_token

    def name(self, key: str) -> str:
        """Nombre HTTP del competidor de esa clave dentro del prefijo del escenario."""
        return f"{self.name_prefix}-{key}"


# ---------------------------------------------------------------- HTTP helpers


def competitors_url(club: int) -> str:
    return f"{get_root_uvicorn_url()}clubs/{club}/competitors"


def competitor_url(club: int, competitor_id: int) -> str:
    return f"{competitors_url(club)}/{competitor_id}"


def affiliations_url(club: int, competitor_id: int) -> str:
    return f"{competitor_url(club, competitor_id)}/affiliations"


def affiliation_action_url(club: int, competitor_id: int, affiliation_id: int, action: str) -> str:
    return f"{affiliations_url(club, competitor_id)}/{affiliation_id}/{action}"


def independence_url(club: int, competitor_id: int) -> str:
    return f"{competitor_url(club, competitor_id)}/independence"


def sports_clubs_url(club: int) -> str:
    return f"{get_root_uvicorn_url()}clubs/{club}/sports-clubs"


def belts_url(club: int) -> str:
    return f"{get_root_uvicorn_url()}clubs/{club}/belts"


def logo_url(club: int, sports_club_id: int) -> str:
    return f"{sports_clubs_url(club)}/{sports_club_id}/logo"


def fresh_key() -> str:
    """Clave de idempotencia valida (>= 8 caracteres) y distinta por llamada."""
    return f"k-{uuid4().hex}"


async def create_competitor_http(
    club: int,
    *,
    token: str,
    name: str,
    date_of_birth: date | None,
    sex: str | None,
    belt_id: int | None,
) -> int:
    """Alta F3 por HTTP: puerta de escritura + idempotencia + perfil deportivo opcional."""
    payload: dict[str, Any] = {
        "display_name": name,
        "date_of_birth": None if date_of_birth is None else date_of_birth.isoformat(),
        "sex": sex,
        "belt_id": belt_id,
        "reason_code": "PLANNED_ENTRY",
    }
    status_code, body = await call(
        "POST", competitors_url(club), token=token, key=fresh_key(), payload=payload
    )
    assert status_code == 201, body
    return int(body["id"])


async def assign_affiliation_http(
    club: int, competitor_id: int, sports_club_id: int, *, token: str
) -> dict[str, Any]:
    """Abre una afiliacion vigente por HTTP (OWNER-only)."""
    status_code, body = await call(
        "POST",
        affiliations_url(club, competitor_id),
        token=token,
        key=fresh_key(),
        payload={"sports_club_id": sports_club_id},
    )
    assert status_code == 201, body
    return body


async def post_logo(
    club: int,
    sports_club_id: int,
    *,
    token: str,
    filename: str | None = None,
    content: bytes | None = None,
    content_type: str = "image/png",
) -> tuple[int, dict[str, Any]]:
    """Sube (o quita, sin fichero) el logo de la academia; ``multipart/form-data`` real."""
    form = aiohttp.FormData()
    if filename is not None and content is not None:
        form.add_field("file", io.BytesIO(content), filename=filename, content_type=content_type)
    headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as session:
        async with session.post(logo_url(club, sports_club_id), data=form, headers=headers) as resp:
            text = await resp.text()
            return resp.status, (json.loads(text) if text else {})


# ---------------------------------------------------------------- DB helpers


async def insert_belt(scope: str, code: str, label: str, rank: int) -> int:
    """Siembra un cinturon del catalogo (el arnes de CI crea ``belts`` vacia) y devuelve su id."""
    await database.execute(
        query=_UPSERT_BELT, values={"code": code, "label": label, "rank": rank, "scope": scope}
    )
    return int(
        await database.fetch_val(query=_SELECT_BELT, values={"scope": scope, "code": code})
    )


async def insert_category(
    dataset: E2EDataset, spec: dict[str, Any]
) -> StageItemId:
    """Siembra una categoria con reglas reales en ``stage_items`` y devuelve su id."""
    belt_min = None if spec["belt_min"] is None else dataset.belts[spec["belt_min"]]
    belt_max = None if spec["belt_max"] is None else dataset.belts[spec["belt_max"]]
    stage_item_id = await database.fetch_val(
        query=_INSERT_STAGE_ITEM,
        values={
            "name": spec["name"],
            "stage_id": dataset.stage,
            "ranking_id": dataset.ranking,
            "sex_constraint": spec["sex_constraint"],
            "age_min": spec["age_min"],
            "age_max": spec["age_max"],
            "weight_min_kg": spec["weight_min_kg"],
            "weight_max_kg": spec["weight_max_kg"],
            "modality": spec["modality"],
            "belt_min_id": belt_min,
            "belt_max_id": belt_max,
        },
    )
    return StageItemId(int(stage_item_id))


async def count_competitors(prefix: str) -> int:
    return int(
        await database.fetch_val(
            query=_COUNT_COMPETITORS_BY_PREFIX, values={"prefix": f"{prefix}-%"}
        )
    )


async def open_primary_count(competitor_id: int) -> int:
    return int(
        await database.fetch_val(
            query=_COUNT_OPEN_PRIMARY, values={"competitor_id": competitor_id}
        )
    )


async def open_count(competitor_id: int) -> int:
    return int(await database.fetch_val(query=_COUNT_OPEN, values={"competitor_id": competitor_id}))


async def stage_item_row(stage_item_id: int) -> dict[str, Any]:
    record = await database.fetch_one(
        query=_SELECT_STAGE_ITEM, values={"id": stage_item_id}
    )
    assert record is not None
    return dict(record._mapping)


async def profile_row(competitor_id: int) -> dict[str, Any]:
    record = await database.fetch_one(
        query=_SELECT_PROFILE, values={"competitor_id": competitor_id}
    )
    assert record is not None
    return dict(record._mapping)


async def insert_frozen_registration(
    *,
    tournament_id: int,
    competitor_id: int,
    sports_club_id: int,
    name_snapshot: str,
    club_snapshot: str,
    affiliation_id: int,
) -> int:
    """Inscribe y confirma un competidor con su academia y afiliacion **fotografiadas**.

    Es la referencia de §6: demuestra que una inscripcion historica no cambia cuando el
    competidor cambia de academia despues.
    """
    return int(
        await database.fetch_val(
            query=_INSERT_FROZEN_REGISTRATION,
            values={
                "tournament_id": tournament_id,
                "competitor_id": competitor_id,
                "sports_club_id": sports_club_id,
                "name_snapshot": name_snapshot,
                "club_snapshot": club_snapshot,
                "affiliation_id": affiliation_id,
            },
        )
    )


async def registration_row(registration_id: int) -> dict[str, Any]:
    """Fila de una inscripcion (para comprobar que la foto historica no se mueve)."""
    record = await database.fetch_one(
        query=_SELECT_REGISTRATION, values={"id": registration_id}
    )
    assert record is not None
    return dict(record._mapping)


async def affiliation_history(competitor_id: int) -> list[dict[str, Any]]:
    """Todas las afiliaciones del competidor (vigentes y cerradas), en orden de insercion."""
    records = await database.fetch_all(
        query=_SELECT_AFFILIATION_HISTORY, values={"competitor_id": competitor_id}
    )
    return [dict(record._mapping) for record in records]


# ---------------------------------------------------------------- builder


async def _cleanup(dataset: E2EDataset) -> None:
    """Deshace el escenario de hojas a raices, antes de que cierre el laboratorio de base."""
    await database.execute(
        query=_DELETE_FROZEN_REGISTRATIONS, values={"t": dataset.tournament}
    )
    await database.execute(
        query=_DELETE_AFFILIATIONS, values={"t": dataset.club}
    )
    # Los competidores referencian los cinturones con FK RESTRICT: se sueltan antes de borrarlos.
    await database.execute(query=_NULL_TENANT_BELTS, values={"t": dataset.club})
    await database.execute(
        query=_DELETE_STAGE_ITEMS, values={"ids": [int(c) for c in dataset.categories.values()]}
    )
    await database.execute(query=_DELETE_RANKING, values={"id": dataset.ranking})
    await database.execute(query=_DELETE_STAGE, values={"id": dataset.stage})
    await database.execute(
        query=_DELETE_SPORTS_CLUBS, values={"ids": [int(c) for c in dataset.clubs.values()]}
    )
    await database.execute(query=_DELETE_BELTS, values={"ids": list(dataset.belts.values())})
    for relative in dataset.logo_paths.values():
        if relative is not None:
            Path(relative).unlink(missing_ok=True)


@asynccontextmanager
async def e2e_dataset_context() -> AsyncIterator[E2EDataset]:
    """Construye el escenario LAB completo (§3-§7) y lo limpia de hojas a raices al cerrar."""
    async with AsyncExitStack() as stack:
        lab = await stack.enter_async_context(competitor_lab_context())
        suffix = uuid4().hex[:8]

        # Fecha de inicio fija: la edad se calcula contra ella, no contra el reloj.
        await database.execute(
            query="UPDATE tournaments SET start_time = :start WHERE id = :id",
            values={"start": START_TIME, "id": lab.tournament},
        )

        stage = StageId(
            int(
                await database.fetch_val(
                    query=_INSERT_STAGE,
                    values={"tournament_id": lab.tournament, "name": f"{NAME_PREFIX}-stage", "created": CREATED},
                )
            )
        )
        ranking = RankingId(
            int(
                await database.fetch_val(
                    query=_INSERT_RANKING,
                    values={"tournament_id": lab.tournament, "created": CREATED},
                )
            )
        )

        belts: dict[str, int] = {}
        for code, (label, rank) in ADULT_BELTS.items():
            belts[code] = await insert_belt("ADULT", code, label, rank)

        clubs: dict[str, SportsClubId] = {}
        for letter in ACADEMY_LETTERS:
            sports_club_id = await insert_sports_club(
                f"{NAME_PREFIX}-academy-{letter}-{suffix}", lab.club, active=True, created=CREATED
            )
            clubs[letter] = sports_club_id
            stack.push_async_callback(delete_sports_club, sports_club_id)

        dataset = E2EDataset(
            lab=lab,
            stage=stage,
            ranking=ranking,
            belts=belts,
            clubs=clubs,
            logo_paths={"A": None, "B": None, "C": None},
            name_prefix=NAME_PREFIX,
        )

        # Logos: A con PNG, B con JPEG; C se queda sin logo (fallback).
        for letter, filename, content, content_type, key in (
            ("A", "logo.png", PNG_BYTES, "image/png", "a"),
            ("B", "logo.jpg", JPEG_BYTES, "image/jpeg", "b"),
        ):
            status_code, body = await post_logo(
                dataset.club,
                int(clubs[letter]),
                token=lab.owner_token,
                filename=filename,
                content=content,
                content_type=content_type,
            )
            assert status_code == 200, (letter, key, body)
            dataset.logo_paths[letter] = str(body["logo_path"])

        # Veinticuatro competidores F3 por el camino HTTP real.
        for spec in COMPETITOR_SPECS:
            belt_id = None if spec.belt_code is None else belts[spec.belt_code]
            competitor_id = await create_competitor_http(
                dataset.club,
                token=lab.owner_token,
                name=dataset.name(spec.key),
                date_of_birth=spec.date_of_birth,
                sex=spec.sex,
                belt_id=belt_id,
            )
            dataset.competitors[spec.key] = CompetitorId(competitor_id)
            dataset.academy_of[spec.key] = spec.academy

        # Afiliacion primaria de los 21 competidores con academia (los 3 independientes, sin ninguna).
        for spec in COMPETITOR_SPECS:
            if spec.academy is None:
                continue
            body = await assign_affiliation_http(
                dataset.club,
                int(dataset.competitors[spec.key]),
                int(clubs[spec.academy]),
                token=lab.owner_token,
            )
            dataset.affiliations[spec.key] = int(body["id"])

        # Categorias: cinco estructuradas con reglas reales + una legacy sin ninguna regla.
        for spec in CATEGORY_SPECS:
            dataset.categories[spec["name"]] = await insert_category(dataset, spec)
        dataset.categories[LEGACY_CATEGORY] = await insert_category(
            dataset,
            {
                "name": LEGACY_CATEGORY,
                "sex_constraint": None,
                "age_min": None,
                "age_max": None,
                "weight_min_kg": None,
                "weight_max_kg": None,
                "modality": None,
                "belt_min": None,
                "belt_max": None,
            },
        )

        # Inscripcion historica congelada de un competidor afiliado (referencia para §6).
        frozen_key = "a1"
        dataset.frozen_registration = int(
            await database.fetch_val(
                query=_INSERT_FROZEN_REGISTRATION,
                values={
                    "tournament_id": lab.tournament,
                    "competitor_id": int(dataset.competitors[frozen_key]),
                    "sports_club_id": int(clubs["A"]),
                    "name_snapshot": dataset.name(frozen_key),
                    "club_snapshot": "Academia Congelada A",
                    "affiliation_id": dataset.affiliations[frozen_key],
                },
            )
        )

        try:
            yield dataset
        finally:
            await _cleanup(dataset)


# ---------------------------------------------------------------- §25 evidence


#: Resultados de validacion acumulados por los tests del modulo (se vuelcan al cerrar).
EVIDENCE: dict[str, Any] = {"validations": []}


def record(name: str, passed: bool, detail: dict[str, Any] | None = None) -> None:
    """Anota el resultado de una validacion para el resumen congelable de §25."""
    EVIDENCE["validations"].append({"name": name, "passed": bool(passed), "detail": detail or {}})


def dataset_summary(dataset: E2EDataset) -> dict[str, Any]:
    """Resumen con ids reales del escenario (para congelar en §25)."""
    return {
        "tenant_club_id": dataset.club,
        "tournament_id": int(dataset.tournament),
        "stage_id": int(dataset.stage),
        "ranking_id": int(dataset.ranking),
        "belts": {code: int(belt) for code, belt in dataset.belts.items()},
        "academies": {letter: int(club) for letter, club in dataset.clubs.items()},
        "logo_paths": dict(dataset.logo_paths),
        "competitors": {key: int(cid) for key, cid in dataset.competitors.items()},
        "academy_of": dict(dataset.academy_of),
        "categories": {name: int(cid) for name, cid in dataset.categories.items()},
        "affiliations": {key: int(aid) for key, aid in dataset.affiliations.items()},
        "frozen_registration_id": int(dataset.frozen_registration),
        "counts": {
            "competitors": len(dataset.competitors),
            "affiliated": sum(1 for v in dataset.academy_of.values() if v is not None),
            "independent": sum(1 for v in dataset.academy_of.values() if v is None),
            "structured_categories": len(CATEGORY_SPECS),
            "legacy_categories": 1,
        },
    }


def write_evidence(dataset: E2EDataset) -> Path:
    """Vuelca el resumen de §25 (dataset + validaciones) en ``EVIDENCE_PATH``."""
    EVIDENCE["dataset"] = dataset_summary(dataset)
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(
        json.dumps(EVIDENCE, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
    )
    return EVIDENCE_PATH


#: Reexportados para que el modulo de pruebas los use sin conocer el arnes por dentro.
__all__ = [
    "ADULT_BELTS",
    "CATEGORY_SPECS",
    "COMPETITOR_SPECS",
    "CREATED",
    "E2EDataset",
    "EVIDENCE",
    "EVIDENCE_PATH",
    "INVALID_DETAIL",
    "LEGACY_CATEGORY",
    "NAME_PREFIX",
    "PNG_BYTES",
    "JPEG_BYTES",
    "WEBP_BYTES",
    "affiliation_action_url",
    "affiliation_history",
    "affiliations_url",
    "assign_affiliation_http",
    "belts_url",
    "call",
    "competitor_url",
    "competitors_url",
    "count_competitors",
    "create_competitor_http",
    "dataset_summary",
    "e2e_dataset_context",
    "fresh_key",
    "independence_url",
    "insert_belt",
    "insert_frozen_registration",
    "logo_url",
    "open_count",
    "open_primary_count",
    "post_logo",
    "profile_row",
    "record",
    "registration_row",
    "sports_clubs_url",
    "stage_item_row",
    "write_evidence",
]
