# pylint: disable=redefined-outer-name  # `domain_data` es el fixture de este modulo (igual que conftest).
"""S1 de F3B — capa de consultas de solo lectura del dominio.

TDD: estas pruebas se escribieron y se ejecutaron **antes** de subir
``bracket/models/db/domain.py`` y ``bracket/sql/domain_reads.py`` (rojo con
``ModuleNotFoundError``) y despues en verde con la implementacion.

Cubren el contrato de S1: aislamiento por tenant, registro no encontrado,
categoria nula, varias categorias, afiliaciones multiples, identidad desconocida,
correcciones historicas, snapshots y ausencia de escritura.

Validacion de base de datos exclusivamente en ``bracket_test`` (F3A aplicada).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta

import pytest
import pytest_asyncio
from databases import Database
from heliclockter import datetime_utc

from bracket.database import database
from bracket.models.db.club import ClubInsertable
from bracket.sql.domain_reads import (
    get_competitor,
    get_competitor_affiliations,
    get_competitor_name_history,
    get_competitors_for_tenant,
    get_platform_sports_clubs,
    get_registration,
    get_registration_revision_chain,
    get_registrations_for_competitor,
    get_registrations_without_identity,
    get_sports_club,
    get_sports_club_name_history,
    get_sports_clubs_for_tenant,
    get_tournament_registrations,
)
from bracket.utils.dummy_records import DUMMY_TOURNAMENT
from bracket.utils.id_types import (
    ClubId,
    CompetitorId,
    CompetitorXSportsClubId,
    SportsClubId,
    TournamentId,
    TournamentRegistrationId,
)
from tests.integration_tests.sql import inserted_club, inserted_tournament

_INSERT_SPORTS_CLUB = """
    INSERT INTO sports_clubs (name, tenant_club_id, active, created)
    VALUES (:name, :tenant_club_id, :active, :created)
    RETURNING id
"""

_INSERT_COMPETITOR = """
    INSERT INTO competitors (display_name, managed_by_club_id, active, created)
    VALUES (:display_name, :managed_by_club_id, :active, :created)
    RETURNING id
"""

_INSERT_AFFILIATION = """
    INSERT INTO competitors_x_sports_clubs
        (competitor_id, sports_club_id, valid_from, valid_to, is_primary, created)
    VALUES (:competitor_id, :sports_club_id, :valid_from, :valid_to, :is_primary, :created)
    RETURNING id
"""

_INSERT_COMPETITOR_NAME_HISTORY = """
    INSERT INTO competitors_name_history (competitor_id, display_name, valid_from, valid_to)
    VALUES (:competitor_id, :display_name, :valid_from, :valid_to)
    RETURNING id
"""

_INSERT_SPORTS_CLUB_NAME_HISTORY = """
    INSERT INTO sports_clubs_name_history (sports_club_id, name, valid_from, valid_to)
    VALUES (:sports_club_id, :name, :valid_from, :valid_to)
    RETURNING id
"""

_INSERT_REGISTRATION = """
    INSERT INTO tournament_registrations (
        tournament_id, competitor_id, identity_status, representation, sports_club_id,
        affiliation_id, category_key, category_label, competitor_name_snapshot,
        sports_club_name_snapshot, status, revision, corrects_registration_id, created
    ) VALUES (
        :tournament_id, :competitor_id,
        CAST(:identity_status AS registration_identity_status),
        CAST(:representation AS registration_representation),
        :sports_club_id, :affiliation_id, :category_key, :category_label,
        :competitor_name_snapshot, :sports_club_name_snapshot,
        CAST(:status AS registration_status), :revision, :corrects_registration_id, :created
    )
    RETURNING id
"""

_SET_SUPERSEDED_BY = """
    UPDATE tournament_registrations SET superseded_by_registration_id = :successor_id
    WHERE id = :registration_id
"""

_DELETE_REGISTRATION_LEAVES = """
    DELETE FROM tournament_registrations tr
    WHERE tr.tournament_id = :tournament_id
    AND NOT EXISTS (
        SELECT 1 FROM tournament_registrations child
        WHERE child.corrects_registration_id = tr.id
    )
"""


@dataclass
class DomainData:
    tenant_a: ClubId
    tenant_b: ClubId
    tournament_a: TournamentId
    tournament_b: TournamentId
    sports_club_a: SportsClubId
    sports_club_b: SportsClubId
    sports_club_a_inactive: SportsClubId
    sports_club_platform: SportsClubId
    competitor_a1: CompetitorId
    competitor_a2: CompetitorId
    competitor_b: CompetitorId
    competitor_unmanaged: CompetitorId
    affiliation_primary: CompetitorXSportsClubId
    affiliation_secondary: CompetitorXSportsClubId
    affiliation_historical: CompetitorXSportsClubId
    registrations_multicategory: tuple[TournamentRegistrationId, ...]
    registration_original: TournamentRegistrationId
    registration_revision: TournamentRegistrationId
    registration_unknown_identity: TournamentRegistrationId
    registration_other_tenant: TournamentRegistrationId


async def _insert_sports_club(
    name: str, tenant_club_id: ClubId | None, *, active: bool, created: datetime_utc
) -> SportsClubId:
    sports_club_id = await database.fetch_val(
        query=_INSERT_SPORTS_CLUB,
        values={
            "name": name,
            "tenant_club_id": tenant_club_id,
            "active": active,
            "created": created,
        },
    )
    return SportsClubId(sports_club_id)


async def _insert_competitor(
    display_name: str, managed_by_club_id: ClubId | None, *, created: datetime_utc
) -> CompetitorId:
    competitor_id = await database.fetch_val(
        query=_INSERT_COMPETITOR,
        values={
            "display_name": display_name,
            "managed_by_club_id": managed_by_club_id,
            "active": True,
            "created": created,
        },
    )
    return CompetitorId(competitor_id)


async def _insert_affiliation(
    *,
    competitor_id: CompetitorId,
    sports_club_id: SportsClubId,
    valid_from: datetime_utc,
    valid_to: datetime_utc | None,
    is_primary: bool,
    created: datetime_utc,
) -> CompetitorXSportsClubId:
    affiliation_id = await database.fetch_val(
        query=_INSERT_AFFILIATION,
        values={
            "competitor_id": competitor_id,
            "sports_club_id": sports_club_id,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "is_primary": is_primary,
            "created": created,
        },
    )
    return CompetitorXSportsClubId(affiliation_id)


async def _insert_registration(
    *,
    tournament_id: TournamentId,
    competitor_id: CompetitorId | None,
    identity_status: str,
    representation: str,
    competitor_name_snapshot: str,
    status: str,
    revision: int = 1,
    category_key: str | None = None,
    category_label: str | None = None,
    sports_club_id: SportsClubId | None = None,
    sports_club_name_snapshot: str | None = None,
    affiliation_id: CompetitorXSportsClubId | None = None,
    corrects_registration_id: TournamentRegistrationId | None = None,
    created: datetime_utc,
) -> TournamentRegistrationId:
    registration_id = await database.fetch_val(
        query=_INSERT_REGISTRATION,
        values={
            "tournament_id": tournament_id,
            "competitor_id": competitor_id,
            "identity_status": identity_status,
            "representation": representation,
            "sports_club_id": sports_club_id,
            "affiliation_id": affiliation_id,
            "category_key": category_key,
            "category_label": category_label,
            "competitor_name_snapshot": competitor_name_snapshot,
            "sports_club_name_snapshot": sports_club_name_snapshot,
            "status": status,
            "revision": revision,
            "corrects_registration_id": corrects_registration_id,
            "created": created,
        },
    )
    return TournamentRegistrationId(registration_id)


async def _delete_registrations(tournament_id: TournamentId) -> None:
    """Borra las inscripciones del torneo empezando por las hojas (FK RESTRICT)."""
    for _ in range(5):
        remaining = await database.fetch_val(
            query="SELECT count(*) FROM tournament_registrations WHERE tournament_id = :t",
            values={"t": tournament_id},
        )
        if not remaining:
            break
        await database.execute(
            query=_DELETE_REGISTRATION_LEAVES, values={"tournament_id": tournament_id}
        )


async def _delete_affiliations(competitor_id: CompetitorId) -> None:
    await database.execute(
        query="DELETE FROM competitors_x_sports_clubs WHERE competitor_id = :c",
        values={"c": competitor_id},
    )


async def _delete_competitor(competitor_id: CompetitorId) -> None:
    """El historial de nombres cae en cascada con el competidor."""
    await database.execute(
        query="DELETE FROM competitors WHERE id = :c", values={"c": competitor_id}
    )


async def _delete_sports_club(sports_club_id: SportsClubId) -> None:
    """El historial de nombres cae en cascada con la academia."""
    await database.execute(
        query="DELETE FROM sports_clubs WHERE id = :s", values={"s": sports_club_id}
    )


@pytest_asyncio.fixture(loop_scope="session")
async def domain_data(reinit_database: Database) -> AsyncIterator[DomainData]:
    """Dos tenants con homonimos, academias, afiliaciones, correcciones y desconocidos."""
    now = datetime_utc.now()
    two_days_ago = now - timedelta(days=2)
    yesterday = now - timedelta(days=1)

    async with AsyncExitStack() as stack:
        club_a = await stack.enter_async_context(
            inserted_club(ClubInsertable(name="Tenant A (F3B-S1)", created=now))
        )
        club_b = await stack.enter_async_context(
            inserted_club(ClubInsertable(name="Tenant B (F3B-S1)", created=now))
        )
        tournament_a = await stack.enter_async_context(
            inserted_tournament(
                DUMMY_TOURNAMENT.model_copy(
                    update={"club_id": club_a.id, "dashboard_endpoint": "f3b-s1-tenant-a"}
                )
            )
        )
        tournament_b = await stack.enter_async_context(
            inserted_tournament(
                DUMMY_TOURNAMENT.model_copy(
                    update={"club_id": club_b.id, "dashboard_endpoint": "f3b-s1-tenant-b"}
                )
            )
        )

        sports_club_a = await _insert_sports_club(
            "Academia Vecina A", club_a.id, active=True, created=now
        )
        sports_club_b = await _insert_sports_club(
            "Academia Vecina B", club_b.id, active=True, created=now
        )
        sports_club_a_inactive = await _insert_sports_club(
            "Academia Antigua A", club_a.id, active=False, created=now
        )
        sports_club_platform = await _insert_sports_club(
            "Academia de Plataforma", None, active=True, created=now
        )
        # El registro en la pila se hace al crear cada fila: si el fixture falla a
        # medias, el teardown borra lo creado en orden seguro de FK.
        for sports_club_id in (
            sports_club_a,
            sports_club_b,
            sports_club_a_inactive,
            sports_club_platform,
        ):
            stack.push_async_callback(_delete_sports_club, sports_club_id)

        competitor_a1 = await _insert_competitor("Ana Gomez", club_a.id, created=now)
        competitor_a2 = await _insert_competitor("Ana Gomez", club_a.id, created=now)
        competitor_b = await _insert_competitor("Ana Gomez", club_b.id, created=now)
        competitor_unmanaged = await _insert_competitor("Sin Gestor", None, created=now)
        for competitor_id in (
            competitor_a1,
            competitor_a2,
            competitor_b,
            competitor_unmanaged,
        ):
            stack.push_async_callback(_delete_competitor, competitor_id)

        affiliation_primary = await _insert_affiliation(
            competitor_id=competitor_a1,
            sports_club_id=sports_club_a,
            valid_from=two_days_ago,
            valid_to=None,
            is_primary=True,
            created=now,
        )
        affiliation_secondary = await _insert_affiliation(
            competitor_id=competitor_a1,
            sports_club_id=sports_club_platform,
            valid_from=two_days_ago,
            valid_to=None,
            is_primary=False,
            created=now,
        )
        affiliation_historical = await _insert_affiliation(
            competitor_id=competitor_a1,
            sports_club_id=sports_club_b,
            valid_from=two_days_ago,
            valid_to=yesterday,
            is_primary=False,
            created=now,
        )
        # Una sola retirada por competidor: el DELETE borra todas sus afiliaciones.
        stack.push_async_callback(_delete_affiliations, competitor_a1)

        await database.execute(
            query=_INSERT_COMPETITOR_NAME_HISTORY,
            values={
                "competitor_id": competitor_a1,
                "display_name": "Ana Gomez",
                "valid_from": two_days_ago,
                "valid_to": yesterday,
            },
        )
        await database.execute(
            query=_INSERT_COMPETITOR_NAME_HISTORY,
            values={
                "competitor_id": competitor_a1,
                "display_name": "Ana Gomez Lopez",
                "valid_from": yesterday,
                "valid_to": None,
            },
        )
        await database.execute(
            query=_INSERT_SPORTS_CLUB_NAME_HISTORY,
            values={
                "sports_club_id": sports_club_a,
                "name": "Academia Vecina A",
                "valid_from": two_days_ago,
                "valid_to": None,
            },
        )

        registration_club = await _insert_registration(
            tournament_id=tournament_a.id,
            competitor_id=competitor_a1,
            identity_status="VERIFIED",
            representation="CLUB",
            sports_club_id=sports_club_a,
            sports_club_name_snapshot="Academia Vecina A",
            affiliation_id=affiliation_primary,
            category_key="abs-f",
            category_label="Absoluto femenino",
            competitor_name_snapshot="Ana Gomez",
            status="CONFIRMED",
            created=now,
        )
        registration_independent = await _insert_registration(
            tournament_id=tournament_a.id,
            competitor_id=competitor_a1,
            identity_status="VERIFIED",
            representation="INDEPENDENT",
            category_key="abs-m",
            category_label="Absoluto masculino",
            competitor_name_snapshot="Ana Gomez",
            status="CONFIRMED",
            created=now,
        )
        registration_uncategorized = await _insert_registration(
            tournament_id=tournament_a.id,
            competitor_id=competitor_a1,
            identity_status="VERIFIED",
            representation="INDEPENDENT",
            competitor_name_snapshot="Ana Gomez",
            status="DRAFT",
            created=now,
        )
        registration_original = await _insert_registration(
            tournament_id=tournament_a.id,
            competitor_id=competitor_a2,
            identity_status="VERIFIED",
            representation="INDEPENDENT",
            category_key="vet-m",
            category_label="Veteranos",
            competitor_name_snapshot="Nombre Original",
            status="CORRECTED",
            created=now,
        )
        registration_revision = await _insert_registration(
            tournament_id=tournament_a.id,
            competitor_id=competitor_a2,
            identity_status="VERIFIED",
            representation="INDEPENDENT",
            category_key="vet-m",
            category_label="Veteranos",
            competitor_name_snapshot="Nombre Corregido",
            status="CONFIRMED",
            revision=2,
            corrects_registration_id=registration_original,
            created=now,
        )
        await database.execute(
            query=_SET_SUPERSEDED_BY,
            values={
                "registration_id": registration_original,
                "successor_id": registration_revision,
            },
        )
        registration_unknown_identity = await _insert_registration(
            tournament_id=tournament_a.id,
            competitor_id=None,
            identity_status="UNVERIFIED",
            representation="INDEPENDENT",
            competitor_name_snapshot="Persona Sin Ficha",
            status="DRAFT",
            created=now,
        )
        registration_other_tenant = await _insert_registration(
            tournament_id=tournament_b.id,
            competitor_id=competitor_b,
            identity_status="VERIFIED",
            representation="INDEPENDENT",
            category_key="abs-f",
            competitor_name_snapshot="Ana Gomez",
            status="CONFIRMED",
            created=now,
        )

        # Ultimo en entrar, primero en salir: las inscripciones se borran antes que
        # competidores y academias (FK RESTRICT), respetando el orden revision->original.
        stack.push_async_callback(_delete_registrations, tournament_a.id)
        stack.push_async_callback(_delete_registrations, tournament_b.id)

        data = DomainData(
            tenant_a=club_a.id,
            tenant_b=club_b.id,
            tournament_a=tournament_a.id,
            tournament_b=tournament_b.id,
            sports_club_a=sports_club_a,
            sports_club_b=sports_club_b,
            sports_club_a_inactive=sports_club_a_inactive,
            sports_club_platform=sports_club_platform,
            competitor_a1=competitor_a1,
            competitor_a2=competitor_a2,
            competitor_b=competitor_b,
            competitor_unmanaged=competitor_unmanaged,
            affiliation_primary=affiliation_primary,
            affiliation_secondary=affiliation_secondary,
            affiliation_historical=affiliation_historical,
            registrations_multicategory=(
                registration_club,
                registration_independent,
                registration_uncategorized,
            ),
            registration_original=registration_original,
            registration_revision=registration_revision,
            registration_unknown_identity=registration_unknown_identity,
            registration_other_tenant=registration_other_tenant,
        )

        yield data


@pytest.mark.asyncio(loop_scope="session")
async def test_sports_clubs_are_scoped_to_the_tenant(domain_data: DomainData) -> None:
    sports_clubs = await get_sports_clubs_for_tenant(domain_data.tenant_a)

    assert [sports_club.id for sports_club in sports_clubs] == [domain_data.sports_club_a]
    assert domain_data.sports_club_b not in {sports_club.id for sports_club in sports_clubs}
    assert domain_data.sports_club_platform not in {sports_club.id for sports_club in sports_clubs}

    with_inactive = await get_sports_clubs_for_tenant(domain_data.tenant_a, include_inactive=True)
    assert {sports_club.id for sports_club in with_inactive} == {
        domain_data.sports_club_a,
        domain_data.sports_club_a_inactive,
    }


@pytest.mark.asyncio(loop_scope="session")
async def test_platform_sports_clubs_are_readable_but_not_tenant_owned(
    domain_data: DomainData,
) -> None:
    platform_sports_clubs = {sports_club.id for sports_club in await get_platform_sports_clubs()}

    assert domain_data.sports_club_platform in platform_sports_clubs
    assert domain_data.sports_club_a not in platform_sports_clubs
    assert domain_data.sports_club_b not in platform_sports_clubs


@pytest.mark.asyncio(loop_scope="session")
async def test_sports_club_lookup_is_tenant_scoped_and_unknown_is_none(
    domain_data: DomainData,
) -> None:
    assert (
        await get_sports_club(domain_data.sports_club_a, tenant_club_id=domain_data.tenant_a)
        is not None
    )
    assert (
        await get_sports_club(domain_data.sports_club_a, tenant_club_id=domain_data.tenant_b)
        is None
    )
    assert await get_sports_club(SportsClubId(999_999), tenant_club_id=domain_data.tenant_a) is None


@pytest.mark.asyncio(loop_scope="session")
async def test_sports_club_name_history_is_tenant_scoped(domain_data: DomainData) -> None:
    history = await get_sports_club_name_history(
        domain_data.sports_club_a, tenant_club_id=domain_data.tenant_a
    )

    assert [entry.name for entry in history] == ["Academia Vecina A"]
    assert (
        await get_sports_club_name_history(
            domain_data.sports_club_a, tenant_club_id=domain_data.tenant_b
        )
        == []
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_competitors_are_scoped_by_tenant_and_unknown_is_none(
    domain_data: DomainData,
) -> None:
    competitors = await get_competitors_for_tenant(domain_data.tenant_a)
    competitor_ids = {competitor.id for competitor in competitors}

    assert competitor_ids == {domain_data.competitor_a1, domain_data.competitor_a2}
    assert domain_data.competitor_b not in competitor_ids
    assert domain_data.competitor_unmanaged not in competitor_ids

    assert (
        await get_competitor(domain_data.competitor_b, tenant_club_id=domain_data.tenant_a) is None
    )
    assert await get_competitor(CompetitorId(999_999), tenant_club_id=domain_data.tenant_a) is None
    assert (
        await get_competitor(domain_data.competitor_a1, tenant_club_id=domain_data.tenant_a)
        is not None
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_name_search_returns_every_homonym_without_deduplication(
    domain_data: DomainData,
) -> None:
    matches = await get_competitors_for_tenant(domain_data.tenant_a, display_name="Ana Gomez")

    assert {competitor.id for competitor in matches} == {
        domain_data.competitor_a1,
        domain_data.competitor_a2,
    }
    assert len(matches) == 2
    assert domain_data.competitor_b not in {competitor.id for competitor in matches}
    assert domain_data.competitor_unmanaged not in {competitor.id for competitor in matches}

    no_matches = await get_competitors_for_tenant(
        domain_data.tenant_a, display_name="zzz-inexistente"
    )
    assert no_matches == []


@pytest.mark.asyncio(loop_scope="session")
async def test_affiliations_current_by_default_and_history_on_demand(
    domain_data: DomainData,
) -> None:
    current = await get_competitor_affiliations(
        domain_data.competitor_a1, tenant_club_id=domain_data.tenant_a
    )
    assert {affiliation.id for affiliation in current} == {
        domain_data.affiliation_primary,
        domain_data.affiliation_secondary,
    }
    assert current[0].id == domain_data.affiliation_primary
    assert current[0].is_primary is True

    with_history = await get_competitor_affiliations(
        domain_data.competitor_a1, tenant_club_id=domain_data.tenant_a, include_history=True
    )
    assert {affiliation.id for affiliation in with_history} == {
        domain_data.affiliation_primary,
        domain_data.affiliation_secondary,
        domain_data.affiliation_historical,
    }

    assert (
        await get_competitor_affiliations(
            domain_data.competitor_a1, tenant_club_id=domain_data.tenant_b
        )
        == []
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_competitor_name_history_is_ordered_oldest_first(domain_data: DomainData) -> None:
    history = await get_competitor_name_history(
        domain_data.competitor_a1, tenant_club_id=domain_data.tenant_a
    )

    assert [entry.display_name for entry in history] == ["Ana Gomez", "Ana Gomez Lopez"]
    assert history[0].valid_to is not None
    assert history[1].valid_to is None


@pytest.mark.asyncio(loop_scope="session")
async def test_registrations_cover_multiple_categories_and_null_category(
    domain_data: DomainData,
) -> None:
    registrations = await get_registrations_for_competitor(
        domain_data.competitor_a1,
        tournament_id=domain_data.tournament_a,
        tenant_club_id=domain_data.tenant_a,
    )

    assert {registration.id for registration in registrations} == set(
        domain_data.registrations_multicategory
    )
    assert len(registrations) == 3
    assert [registration.category_key for registration in registrations] == [None, "abs-f", "abs-m"]

    club_registration = next(
        registration
        for registration in registrations
        if registration.id == domain_data.registrations_multicategory[0]
    )
    assert club_registration.representation == "CLUB"
    assert club_registration.sports_club_name_snapshot == "Academia Vecina A"


@pytest.mark.asyncio(loop_scope="session")
async def test_registration_lookup_is_tenant_scoped_and_unknown_is_none(
    domain_data: DomainData,
) -> None:
    assert (
        await get_registration(
            domain_data.registration_other_tenant, tenant_club_id=domain_data.tenant_a
        )
        is None
    )
    assert (
        await get_registration(
            TournamentRegistrationId(999_999), tenant_club_id=domain_data.tenant_a
        )
        is None
    )
    assert (
        await get_registration(
            domain_data.registration_other_tenant, tenant_club_id=domain_data.tenant_b
        )
        is not None
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_superseded_registrations_are_excluded_by_default(domain_data: DomainData) -> None:
    current = await get_tournament_registrations(
        domain_data.tournament_a, tenant_club_id=domain_data.tenant_a
    )
    current_ids = {registration.id for registration in current}

    assert domain_data.registration_revision in current_ids
    assert domain_data.registration_original not in current_ids
    assert domain_data.registration_unknown_identity in current_ids
    assert len(current) == 5

    with_superseded = await get_tournament_registrations(
        domain_data.tournament_a, tenant_club_id=domain_data.tenant_a, include_superseded=True
    )
    assert {registration.id for registration in with_superseded} == current_ids | {
        domain_data.registration_original
    }
    assert len(with_superseded) == 6


@pytest.mark.asyncio(loop_scope="session")
async def test_revision_chain_returns_original_and_correction(domain_data: DomainData) -> None:
    from_revision = await get_registration_revision_chain(
        domain_data.registration_revision, tenant_club_id=domain_data.tenant_a
    )
    from_original = await get_registration_revision_chain(
        domain_data.registration_original, tenant_club_id=domain_data.tenant_a
    )

    assert [registration.id for registration in from_revision] == [
        domain_data.registration_original,
        domain_data.registration_revision,
    ]
    assert [registration.revision for registration in from_revision] == [1, 2]
    assert [registration.id for registration in from_original] == [
        domain_data.registration_original,
        domain_data.registration_revision,
    ]

    assert (
        await get_registration_revision_chain(
            domain_data.registration_other_tenant, tenant_club_id=domain_data.tenant_a
        )
        == []
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_unknown_identity_registrations_are_listed(domain_data: DomainData) -> None:
    unknown = await get_registrations_without_identity(
        domain_data.tournament_a, tenant_club_id=domain_data.tenant_a
    )

    assert [registration.id for registration in unknown] == [
        domain_data.registration_unknown_identity
    ]
    assert unknown[0].competitor_id is None
    assert unknown[0].identity_status == "UNVERIFIED"
    assert unknown[0].competitor_name_snapshot == "Persona Sin Ficha"


@pytest.mark.asyncio(loop_scope="session")
async def test_registration_snapshots_survive_renaming(domain_data: DomainData) -> None:
    [competitor] = [
        candidate
        for candidate in await get_competitors_for_tenant(domain_data.tenant_a)
        if candidate.id == domain_data.competitor_a2
    ]
    [original] = [
        registration
        for registration in await get_tournament_registrations(
            domain_data.tournament_a, tenant_club_id=domain_data.tenant_a, include_superseded=True
        )
        if registration.id == domain_data.registration_original
    ]

    assert competitor.display_name == "Ana Gomez"
    assert original.competitor_name_snapshot == "Nombre Original"
    assert original.status == "CORRECTED"
    assert original.superseded_by_registration_id == domain_data.registration_revision


@pytest.mark.asyncio(loop_scope="session")
async def test_other_tenant_tournament_is_not_readable(domain_data: DomainData) -> None:
    assert (
        await get_tournament_registrations(
            domain_data.tournament_b, tenant_club_id=domain_data.tenant_a
        )
        == []
    )
    assert (
        await get_registrations_for_competitor(
            domain_data.competitor_b,
            tournament_id=domain_data.tournament_b,
            tenant_club_id=domain_data.tenant_a,
        )
        == []
    )
    assert (
        await get_registrations_without_identity(
            domain_data.tournament_b, tenant_club_id=domain_data.tenant_a
        )
        == []
    )
    assert (
        await get_tournament_registrations(
            domain_data.tournament_b, tenant_club_id=domain_data.tenant_b
        )
        != []
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_reads_never_write(domain_data: DomainData) -> None:
    async def counts() -> dict[str, int]:
        tables = (
            "sports_clubs",
            "competitors",
            "competitors_x_sports_clubs",
            "competitors_name_history",
            "tournament_registrations",
        )
        return {
            table: await database.fetch_val(query=f"SELECT count(*) FROM {table}")
            for table in tables
        }

    before = await counts()

    await get_sports_clubs_for_tenant(domain_data.tenant_a)
    await get_platform_sports_clubs()
    await get_sports_club(domain_data.sports_club_a, tenant_club_id=domain_data.tenant_a)
    await get_competitors_for_tenant(domain_data.tenant_a, display_name="Ana")
    await get_competitor(domain_data.competitor_a1, tenant_club_id=domain_data.tenant_a)
    await get_competitor_affiliations(
        domain_data.competitor_a1, tenant_club_id=domain_data.tenant_a, include_history=True
    )
    await get_competitor_name_history(
        domain_data.competitor_a1, tenant_club_id=domain_data.tenant_a
    )
    await get_sports_club_name_history(
        domain_data.sports_club_a, tenant_club_id=domain_data.tenant_a
    )
    await get_tournament_registrations(
        domain_data.tournament_a, tenant_club_id=domain_data.tenant_a, include_superseded=True
    )
    await get_registrations_for_competitor(
        domain_data.competitor_a1,
        tournament_id=domain_data.tournament_a,
        tenant_club_id=domain_data.tenant_a,
    )
    await get_registration(domain_data.registration_revision, tenant_club_id=domain_data.tenant_a)
    await get_registrations_without_identity(
        domain_data.tournament_a, tenant_club_id=domain_data.tenant_a
    )
    await get_registration_revision_chain(
        domain_data.registration_original, tenant_club_id=domain_data.tenant_a
    )

    assert await counts() == before
