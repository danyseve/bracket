# pylint: disable=redefined-outer-name
"""E2E realista de S4.8a-5 (TROZO A, §3-§7): escenario LAB completo contra el API HTTP real.

Pruebas **negras** contra la aplicacion real (servidor uvicorn del arnes de ``api``) sobre la base
de laboratorio ``bracket_ci``. El escenario se construye una vez por modulo con el camino HTTP real
(puerta F3 + idempotencia) y se valida aqui; los trozos B/C/D (inscripciones, clasificacion,
asignacion, override, readiness, bracket, privacidad, idempotencia, auditoria) pueden reutilizar el
mismo constructor (``e2e_dataset_context``) o seguir este patron.

Que fija este modulo (§3-§7 de la especificacion S4.8a-5):

* §3  torneo LAB **aislado** con 3 academias, 24 competidores y 5 categorias estructuradas + 1
legacy;
* §4  catalogo/listado de academias, ``logo_path`` por academia, **fallback** sin logo y ciclo
      upload/replace/remove del logo;
* §5  24 competidores F3 repartidos 7/7/7 + 3 independientes, con DOB/sexo/cinturon variados y
      varios deliberadamente **incompletos**;
* §6  afiliacion real: A) con club, B) independiente, C) cambio de club, D) cierre, E) vuelta a
      independiente, con **una sola primaria abierta** por competidor, historico conservado
      (``valid_from``/``valid_to``) y la **inscripcion historica congelada** al cambiar de club;
* §7  cinco categorias **estructuradas** con reglas reales (sexo / rango de cinturon / rango de
      edad / rango de peso / modalidad) y una **legacy** sin reglas.

§25: al cerrar el modulo se vuelca un resumen con los ids reales y el resultado de cada validacion
en ``/home/ubuntu/s48a5-evidence/trozo-a.json``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import AsyncIterator, Iterator
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio

from bracket.config import config
from bracket.database import database
from bracket.utils.id_types import ClubId
from tests.integration_tests.api.generate_bracket_http_support import SYNTHETIC_SECRET
from tests.integration_tests.api.s48a5_e2e_trozo_a_support import (
    ADULT_BELTS,
    CATEGORY_SPECS,
    CREATED,
    LEGACY_CATEGORY,
    NAME_PREFIX,
    PNG_BYTES,
    WEBP_BYTES,
    E2EDataset,
    affiliation_action_url,
    affiliation_history,
    affiliations_url,
    assign_affiliation_http,
    belts_url,
    call,
    competitors_url,
    count_competitors,
    create_competitor_http,
    e2e_dataset_context,
    fresh_key,
    independence_url,
    insert_frozen_registration,
    open_count,
    open_primary_count,
    post_logo,
    profile_row,
    record,
    registration_row,
    sports_clubs_url,
    stage_item_row,
    write_evidence,
)
from tests.integration_tests.registration_fixtures import (
    delete_sports_club,
    insert_sports_club,
)

#: Campos publicos de una academia (``SportsClubResponse``): un campo de mas cierra el contrato.
SPORTS_CLUB_FIELDS = {"id", "name", "active", "logo_path"}
#: Campos publicos de un cinturon (``BeltResponse``).
BELT_FIELDS = {"id", "code", "label", "rank", "scope", "active", "created"}
#: Etiquetas de validacion de §25 (una por area del trozo).
VALIDATION_SECTIONS = ("§3", "§4", "§5", "§6", "§7")


@pytest.fixture(scope="module", autouse=True)
def e2e_write_gate() -> Iterator[None]:
    """Escritura F3 y secreto de idempotencia habilitados durante todo el modulo.

    El constructor del escenario da de alta competidores y afiliaciones por el camino HTTP real, que
    esta detras de la puerta de escritura y del protocolo de idempotencia. No se depende del entorno
    de CI: el modulo lo fija de forma explicita y lo restaura al cerrar.
    """
    previous = {
        "f3_write_enabled": config.f3_write_enabled,
        "idempotency_hmac_key": config.idempotency_hmac_key,
        "idempotency_hmac_key_version": config.idempotency_hmac_key_version,
    }
    config.f3_write_enabled = True
    config.idempotency_hmac_key = SYNTHETIC_SECRET
    config.idempotency_hmac_key_version = "v1"
    try:
        yield
    finally:
        for name, value in previous.items():
            setattr(config, name, value)


@pytest_asyncio.fixture(loop_scope="session", scope="module")
async def dataset(
    startup_and_shutdown_uvicorn_server: None,
    e2e_write_gate: None,
) -> AsyncIterator[E2EDataset]:
    """Escenario LAB unico del modulo; al cerrar vuelca la evidencia de §25."""
    async with e2e_dataset_context() as scenario:
        try:
            yield scenario
        finally:
            write_evidence(scenario)


# ---------------------------------------------------------------- §3 y §5: forma del escenario


@pytest.mark.asyncio(loop_scope="session")
async def test_e2e_dataset_has_three_academies_and_twentyfour_competitors(
    dataset: E2EDataset,
) -> None:
    """§3: 3 academias, 24 competidores y reparto 7/7/7 + 3 independientes, comprobado por HTTP."""
    assert len(dataset.clubs) == 3
    assert await count_competitors(NAME_PREFIX) == 24

    split = Counter(dataset.academy_of.values())
    assert split["A"] == 7 and split["B"] == 7 and split["C"] == 7 and split[None] == 3

    status_code, body = await call(
        "GET",
        f"{competitors_url(dataset.club)}?display_name={NAME_PREFIX}-",
        token=dataset.owner_token,
    )
    assert status_code == 200, body
    assert set(body) == {"count", "competitors"}
    assert body["count"] == 24
    assert len(body["competitors"]) == 24
    assert {int(row["id"]) for row in body["competitors"]} == {
        int(cid) for cid in dataset.competitors.values()
    }

    record(
        "§3.dataset_shape",
        True,
        {
            "academies": len(dataset.clubs),
            "competitors": body["count"],
            "split": {"A": 7, "B": 7, "C": 7, "independent": 3},
        },
    )


# ---------------------------------------------------------------- §4: academias y logo


@pytest.mark.asyncio(loop_scope="session")
async def test_e2e_academy_catalog_lists_three_academies_with_logo_and_fallback(
    dataset: E2EDataset,
) -> None:
    """§4: el listado sirve las tres academias, el ``logo_path`` real y el fallback sin logo."""
    status_code, body = await call("GET", sports_clubs_url(dataset.club), token=dataset.owner_token)
    assert status_code == 200, body
    rows = {int(row["id"]): row for row in body}
    assert set(dataset.clubs.values()) <= set(rows)
    for row in rows.values():
        assert set(row) == SPORTS_CLUB_FIELDS

    academy_a = rows[int(dataset.clubs["A"])]
    academy_b = rows[int(dataset.clubs["B"])]
    academy_c = rows[int(dataset.clubs["C"])]

    assert academy_a["logo_path"] == dataset.logo_paths["A"]
    assert academy_a["logo_path"].endswith(".png")
    assert Path(academy_a["logo_path"]).exists()

    assert academy_b["logo_path"] == dataset.logo_paths["B"]
    assert Path(academy_b["logo_path"]).suffix in {".jpg", ".jpeg", ".webp"}
    assert Path(academy_b["logo_path"]).exists()

    # Fallback: la academia sin logo se publica igual, con ``logo_path`` nulo.
    assert academy_c["logo_path"] is None

    record(
        "§4.academy_catalog_and_fallback",
        True,
        {
            "academy_a_logo": academy_a["logo_path"],
            "academy_b_logo": academy_b["logo_path"],
            "academy_c_logo": None,
        },
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_e2e_logo_upload_replace_and_remove_cycle(dataset: E2EDataset) -> None:
    """§4: el ciclo real de logo (subir -> reemplazar -> quitar) deja la columna y el disco
    coherentes.

    Se hace sobre una academia propia del caso (no toca las tres del escenario) para no alterar el
    dataset que los trozos B/C/D van a congelar.
    """
    suffix = uuid4().hex[:8]
    sports_club_id = await insert_sports_club(
        f"{NAME_PREFIX}-cycle-{suffix}", ClubId(dataset.club), active=True, created=CREATED
    )
    created_files: list[Path] = []
    try:
        status_code, body = await post_logo(
            dataset.club,
            int(sports_club_id),
            token=dataset.owner_token,
            filename="logo.png",
            content=PNG_BYTES,
            content_type="image/png",
        )
        assert status_code == 200, body
        first = Path(str(body["logo_path"]))
        created_files.append(first)
        assert first.suffix == ".png" and first.exists()

        status_code, body = await post_logo(
            dataset.club,
            int(sports_club_id),
            token=dataset.owner_token,
            filename="logo.webp",
            content=WEBP_BYTES,
            content_type="image/webp",
        )
        assert status_code == 200, body
        second = Path(str(body["logo_path"]))
        created_files.append(second)
        assert second != first
        assert second.suffix == ".webp" and second.exists()
        assert not first.exists()  # el anterior se retira del disco

        # Quitar el logo: sin fichero, la columna queda en NULL y el fichero se borra.
        status_code, body = await post_logo(
            dataset.club, int(sports_club_id), token=dataset.owner_token
        )
        assert status_code == 200, body
        assert body["logo_path"] is None
        assert not second.exists()

        record(
            "§4.logo_cycle",
            True,
            {"uploaded": str(first), "replaced": str(second), "removed": True},
        )
    finally:
        for path in created_files:
            path.unlink(missing_ok=True)
        await delete_sports_club(sports_club_id)


# ---------------------------------------------------------------- §7: categorias


@pytest.mark.asyncio(loop_scope="session")
async def test_e2e_five_structured_categories_and_one_legacy_without_rules(
    dataset: E2EDataset,
) -> None:
    """§7: cinco categorias con reglas reales y una legacy sin ninguna regla."""
    total = await database.fetch_val(
        query="SELECT count(*) FROM stage_items WHERE stage_id = :id", values={"id": dataset.stage}
    )
    assert int(total) == len(CATEGORY_SPECS) + 1

    for spec in CATEGORY_SPECS:
        row = await stage_item_row(int(dataset.categories[spec["name"]]))
        assert row["sex_constraint"] == spec["sex_constraint"]
        assert row["age_min"] == spec["age_min"] and row["age_max"] == spec["age_max"]
        assert row["weight_min_kg"] == spec["weight_min_kg"]
        assert row["weight_max_kg"] == spec["weight_max_kg"]
        assert row["modality"] == spec["modality"]
        belt_min = None if spec["belt_min"] is None else dataset.belts[spec["belt_min"]]
        belt_max = None if spec["belt_max"] is None else dataset.belts[spec["belt_max"]]
        assert row["belt_min_id"] == belt_min and row["belt_max_id"] == belt_max

    legacy = await stage_item_row(int(dataset.categories[LEGACY_CATEGORY]))
    rule_columns = (
        "sex_constraint",
        "belt_min_id",
        "belt_max_id",
        "age_min",
        "age_max",
        "weight_min_kg",
        "weight_max_kg",
        "modality",
    )
    assert all(legacy[column] is None for column in rule_columns)

    # El catalogo real de cinturones se sirve por HTTP con su forma publica.
    status_code, belts = await call("GET", belts_url(dataset.club), token=dataset.owner_token)
    assert status_code == 200, belts
    codes = {row["code"] for row in belts}
    assert set(ADULT_BELTS) <= codes
    for row in belts:
        assert set(row) == BELT_FIELDS

    record(
        "§7.categories",
        True,
        {
            "structured": {
                spec["name"]: int(dataset.categories[spec["name"]]) for spec in CATEGORY_SPECS
            },
            "legacy": int(dataset.categories[LEGACY_CATEGORY]),
            "total": int(total),
        },
    )


# ---------------------------------------------------------------- §5: perfiles


@pytest.mark.asyncio(loop_scope="session")
async def test_e2e_competitors_vary_their_profile_and_include_deliberate_gaps(
    dataset: E2EDataset,
) -> None:
    """§5: DOB, sexo y cinturon variados y casos incompletos para ejercitar blockers despues."""
    profiles: dict[str, dict[str, Any]] = {
        key: await profile_row(int(cid)) for key, cid in dataset.competitors.items()
    }

    sexes = {profile["sex"] for profile in profiles.values()}
    belts = {profile["belt_id"] for profile in profiles.values() if profile["belt_id"] is not None}
    assert {"MALE", "FEMALE"} <= sexes
    assert len(belts) >= 3

    # Perfil exacto de una muestra concreta.
    assert profiles["a1"]["date_of_birth"] == date(1990, 5, 17)
    assert profiles["a1"]["sex"] == "MALE"
    assert profiles["a1"]["belt_id"] == dataset.belts["WHITE"]

    # Huecos deliberados: sin DOB (a7), sin cinturon (b6), sin sexo (b7) y sin nada (c7).
    assert profiles["a7"]["date_of_birth"] is None
    assert profiles["b6"]["belt_id"] is None
    assert profiles["b7"]["sex"] is None
    assert all(profiles["c7"][field] is None for field in ("date_of_birth", "sex", "belt_id"))

    record(
        "§5.profiles",
        True,
        {
            "sexes": sorted(sex for sex in sexes if sex is not None),
            "profiles_without_sex": sum(
                1 for profile in profiles.values() if profile["sex"] is None
            ),
            "distinct_belts": len(belts),
            "gaps": {"a7": "no_dob", "b6": "no_belt", "b7": "no_sex", "c7": "no_profile"},
        },
    )


# ---------------------------------------------------------------- §6: afiliacion


@pytest.mark.asyncio(loop_scope="session")
async def test_e2e_each_affiliated_competitor_has_a_single_open_primary(
    dataset: E2EDataset,
) -> None:
    """§6 A/B: cada competidor con academia tiene una sola primaria abierta; el independiente,
    ninguna."""
    for key, academy in dataset.academy_of.items():
        competitor_id = int(dataset.competitors[key])
        if academy is None:
            assert await open_count(competitor_id) == 0
        else:
            assert await open_primary_count(competitor_id) == 1
            assert await open_count(competitor_id) == 1

    # La vista vigente por HTTP sirve exactamente la primaria del competidor.
    a1 = int(dataset.competitors["a1"])
    status_code, body = await call(
        "GET", affiliations_url(dataset.club, a1), token=dataset.owner_token
    )
    assert status_code == 200, body
    assert body["competitor_id"] == a1
    assert len(body["affiliations"]) == 1
    current = body["affiliations"][0]
    assert current["is_primary"] is True and current["valid_to"] is None
    assert current["sports_club_id"] == int(dataset.clubs["A"])
    assert current["valid_from"] is not None

    record("§6.single_open_primary", True, {"affiliated": 21, "independent": 3})


@pytest.mark.asyncio(loop_scope="session")
async def test_e2e_affiliation_change_close_and_independence_preserve_history(
    dataset: E2EDataset,
) -> None:
    """§6 C/D/E: cambio de club, cierre y vuelta a independiente conservan el historico y una sola
    primaria."""
    suffix = uuid4().hex[:8]
    competitor_id = await create_competitor_http(
        dataset.club,
        token=dataset.owner_token,
        name=f"{NAME_PREFIX}-lifecycle-{suffix}",
        date_of_birth=date(1990, 1, 1),
        sex="MALE",
        belt_id=dataset.belts["BLUE"],
    )
    club_a, club_b, club_c = (int(dataset.clubs[letter]) for letter in ("A", "B", "C"))

    # A) con club: la primera afiliacion nace primaria y vigente.
    first = await assign_affiliation_http(
        dataset.club, competitor_id, club_a, token=dataset.owner_token
    )
    assert first["is_primary"] is True and first["valid_to"] is None
    assert await open_primary_count(competitor_id) == 1

    # Inscripcion historica congelada que fotografia la academia y la afiliacion de ese momento.
    registration_id = await insert_frozen_registration(
        tournament_id=int(dataset.tournament),
        competitor_id=competitor_id,
        sports_club_id=club_a,
        name_snapshot=f"{NAME_PREFIX}-lifecycle-{suffix}",
        club_snapshot="Academia LAB A",
        affiliation_id=int(first["id"]),
    )

    # C) cambio de club: la segunda afiliacion NO nace primaria; promoverla cierra la anterior.
    second = await assign_affiliation_http(
        dataset.club, competitor_id, club_b, token=dataset.owner_token
    )
    assert second["is_primary"] is False and second["valid_to"] is None
    status_code, promoted = await call(
        "POST",
        affiliation_action_url(dataset.club, competitor_id, int(second["id"]), "primary"),
        token=dataset.owner_token,
        key=fresh_key(),
    )
    assert status_code == 200, promoted
    assert promoted["is_primary"] is True and promoted["valid_to"] is None
    assert await open_primary_count(competitor_id) == 1

    history = await affiliation_history(competitor_id)
    previous = next(row for row in history if int(row["id"]) == int(first["id"]))
    assert previous["valid_to"] is not None  # historico conservado
    assert previous["valid_from"] is not None
    # La primaria vigente es unica y es la promovida: el historico cerrado no compite por el indice.
    open_rows = [row for row in history if row["valid_to"] is None]
    assert len(open_rows) == 1
    assert int(open_rows[0]["id"]) == int(second["id"]) and open_rows[0]["is_primary"] is True

    # D) cierre de la afiliacion vigente.
    status_code, closed = await call(
        "POST",
        affiliation_action_url(dataset.club, competitor_id, int(second["id"]), "close"),
        token=dataset.owner_token,
        key=fresh_key(),
    )
    assert status_code == 200, closed
    assert closed["valid_to"] is not None
    assert await open_count(competitor_id) == 0

    # E) vuelta a independiente: cierra todo lo vigente.
    third = await assign_affiliation_http(
        dataset.club, competitor_id, club_c, token=dataset.owner_token
    )
    status_code, body = await call(
        "POST",
        independence_url(dataset.club, competitor_id),
        token=dataset.owner_token,
        key=fresh_key(),
    )
    assert status_code == 200, body
    assert await open_count(competitor_id) == 0
    history = await affiliation_history(competitor_id)
    assert len(history) == 3
    assert all(row["valid_to"] is not None for row in history)
    newest = next(row for row in history if int(row["id"]) == int(third["id"]))
    assert newest["valid_to"] is not None

    # La inscripcion historica NO se mueve al cambiar de club.
    registration = await registration_row(registration_id)
    assert int(registration["affiliation_id"]) == int(first["id"])
    assert int(registration["sports_club_id"]) == club_a
    assert registration["representation"] == "CLUB"

    record(
        "§6.lifecycle",
        True,
        {
            "competitor_id": competitor_id,
            "affiliations": [int(first["id"]), int(second["id"]), int(third["id"])],
            "frozen_registration_id": registration_id,
            "frozen_club_still": club_a,
        },
    )
