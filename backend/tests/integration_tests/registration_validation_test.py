# pylint: disable=redefined-outer-name  # `registration_data` es un fixture de modulo.
"""S3.1 de F3B - nucleo de inscripciones: validacion de entrada y control de acceso.

TDD: rojo (``ModuleNotFoundError``) y verde, igual que ``registration_write_test.py``.
Cubren la forma de los datos y quien puede escribir: identidad desconocida y verificada,
academia representada explicita y activa, afiliacion coherente, categoria estable con
etiqueta congelada, torneo y competidor del tenant, y autorizacion OWNER/COLLABORATOR.

Datos, *fixture* y utilidades en ``registration_fixtures.py``. Base de datos
exclusivamente ``bracket_test``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from databases import Database

from bracket.logic.competitors import (
    CompetitorNotFoundError,
    TenantNotAuthorizedError,
)
from bracket.logic.registrations import (
    InvalidRegistrationDataError,
    SportsClubNotSelectableError,
    TournamentNotFoundError,
    confirm_registration,
    create_registration,
    update_registration_draft,
)
from bracket.models.db.domain import (
    ActorContext,
)
from bracket.utils.id_types import (
    TournamentId,
    TournamentRegistrationId,
    UserId,
)
from tests.integration_tests.registration_fixtures import (
    RegistrationData,
    audit_rows,
    build_draft,
    count_current_registrations,
    registration_data_context,
)


@pytest_asyncio.fixture(loop_scope="session")
async def registration_data(reinit_database: Database) -> AsyncIterator[RegistrationData]:
    """Datos de S3.1: envuelve ``registration_data_context`` y garantiza la limpieza."""
    async with registration_data_context(reinit_database) as data:
        yield data


@pytest.mark.asyncio(loop_scope="session")
async def test_unknown_identity_registration_keeps_the_source_name(
    registration_data: RegistrationData,
) -> None:
    """Identidad desconocida: competitor_id NULL es legitimo y el nombre de origen se conserva."""
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(
            identity_status="UNVERIFIED",
            competitor_name_snapshot="  Sin Ficha Historica  ",
            category_key="gi-absoluto",
            category_label="Gi Absoluto",
        ),
    )

    assert registration.competitor_id is None
    assert registration.identity_status == "UNVERIFIED"
    assert registration.competitor_name_snapshot == "Sin Ficha Historica"
    assert registration.status == "DRAFT"


@pytest.mark.asyncio(loop_scope="session")
async def test_unknown_identity_can_be_declared_ambiguous(
    registration_data: RegistrationData,
) -> None:
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(
            identity_status="AMBIGUOUS",
            competitor_name_snapshot="Homonimo Sin Resolver",
        ),
    )
    assert registration.identity_status == "AMBIGUOUS"
    assert registration.competitor_id is None


@pytest.mark.asyncio(loop_scope="session")
async def test_unknown_identity_requires_a_source_name(
    registration_data: RegistrationData,
) -> None:
    with pytest.raises(InvalidRegistrationDataError):
        await create_registration(
            registration_data.context_owner_a,
            registration_data.tournament_a,
            build_draft(competitor_name_snapshot=None),
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_verified_identity_rejects_a_name_from_the_payload(
    registration_data: RegistrationData,
) -> None:
    """El nombre de un competidor verificado se deriva: no se acepta desde el payload."""
    with pytest.raises(InvalidRegistrationDataError):
        await create_registration(
            registration_data.context_owner_a,
            registration_data.tournament_a,
            build_draft(
                competitor_id=registration_data.competitor_a,
                competitor_name_snapshot="Otro Nombre",
            ),
        )

    with pytest.raises(InvalidRegistrationDataError):
        await create_registration(
            registration_data.context_owner_a,
            registration_data.tournament_a,
            build_draft(competitor_id=registration_data.competitor_a, identity_status="UNVERIFIED"),
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_inactive_sports_club_is_not_selectable(
    registration_data: RegistrationData,
) -> None:
    """Academia inactiva: no es elegible para una inscripcion nueva."""
    with pytest.raises(SportsClubNotSelectableError):
        await create_registration(
            registration_data.context_owner_a,
            registration_data.tournament_a,
            build_draft(
                competitor_id=registration_data.competitor_a,
                representation="CLUB",
                sports_club_id=registration_data.sports_club_a_inactive,
            ),
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_independent_registration_rejects_an_academy(
    registration_data: RegistrationData,
) -> None:
    with pytest.raises(InvalidRegistrationDataError):
        await create_registration(
            registration_data.context_owner_a,
            registration_data.tournament_a,
            build_draft(
                competitor_id=registration_data.competitor_a,
                representation="INDEPENDENT",
                sports_club_id=registration_data.sports_club_a,
            ),
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_representing_a_club_requires_it_explicitly(
    registration_data: RegistrationData,
) -> None:
    """Prohibido deducir la academia: sin club explicito no hay inferencia."""
    with pytest.raises(InvalidRegistrationDataError):
        await create_registration(
            registration_data.context_owner_a,
            registration_data.tournament_a,
            build_draft(competitor_id=registration_data.competitor_a, representation="CLUB"),
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_affiliation_must_match_the_represented_academy(
    registration_data: RegistrationData,
) -> None:
    # Afiliacion a la academia ajena, representando la propia.
    with pytest.raises(InvalidRegistrationDataError):
        await create_registration(
            registration_data.context_owner_a,
            registration_data.tournament_a,
            build_draft(
                competitor_id=registration_data.competitor_a,
                representation="CLUB",
                sports_club_id=registration_data.sports_club_a,
                affiliation_id=registration_data.affiliation_other_club,
            ),
        )

    # Afiliacion a un competidor sin identidad verificada.
    with pytest.raises(InvalidRegistrationDataError):
        await create_registration(
            registration_data.context_owner_a,
            registration_data.tournament_a,
            build_draft(
                competitor_name_snapshot="Sin Ficha",
                representation="CLUB",
                sports_club_id=registration_data.sports_club_a,
                affiliation_id=registration_data.affiliation_a,
            ),
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_academy_of_another_tenant_can_be_represented(
    registration_data: RegistrationData,
) -> None:
    """Representar una academia ajena es legitimo: se elige, y solo se lee su nombre."""
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(
            competitor_id=registration_data.competitor_a,
            representation="CLUB",
            sports_club_id=registration_data.sports_club_b,
            affiliation_id=registration_data.affiliation_other_club,
        ),
    )

    assert registration.sports_club_id == registration_data.sports_club_b
    assert registration.sports_club_name_snapshot == "Academia Ajena B"


@pytest.mark.asyncio(loop_scope="session")
async def test_unknown_competitor_is_rejected(registration_data: RegistrationData) -> None:
    """Un competidor que no es del tenant no existe para esta operacion."""
    with pytest.raises(CompetitorNotFoundError):
        await create_registration(
            registration_data.context_owner_a,
            registration_data.tournament_a,
            build_draft(competitor_id=registration_data.competitor_b),
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_tournament_of_another_tenant_is_indistinguishable_from_a_missing_one(
    registration_data: RegistrationData,
) -> None:
    """Torneo ajeno y torneo inexistente dan el mismo error: no se filtra su existencia."""
    with pytest.raises(TournamentNotFoundError) as foreign:
        await create_registration(
            registration_data.context_owner_a,
            registration_data.tournament_b,
            build_draft(competitor_id=registration_data.competitor_a),
        )
    with pytest.raises(TournamentNotFoundError) as missing:
        await create_registration(
            registration_data.context_owner_a,
            TournamentId(999_999_999),
            build_draft(competitor_id=registration_data.competitor_a),
        )
    assert str(foreign.value) == str(missing.value)

    # Y no se ha creado nada en el torneo ajeno.
    assert (
        await count_current_registrations(
            tournament_id=registration_data.tournament_b,
            competitor_id=registration_data.competitor_a,
        )
        == 0
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_actor_without_access_to_the_tenant_is_rejected(
    registration_data: RegistrationData,
) -> None:
    """El tenant sale del contexto, y el actor tiene que pertenecer a ese tenant."""
    unknown_user = ActorContext(
        tenant_club_id=registration_data.tenant_a,
        actor_user_id=UserId(999_999_999),
        actor_label="fantasma",
    )

    for context in (registration_data.context_outsider_a, unknown_user):
        with pytest.raises(TenantNotAuthorizedError):
            await create_registration(
                context,
                registration_data.tournament_a,
                build_draft(competitor_id=registration_data.competitor_a),
            )
        with pytest.raises(TenantNotAuthorizedError):
            await update_registration_draft(
                context,
                TournamentRegistrationId(999_999_998),
                build_draft(competitor_id=registration_data.competitor_a),
            )

    assert (
        await count_current_registrations(
            tournament_id=registration_data.tournament_a,
            competitor_id=registration_data.competitor_a,
        )
        == 0
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_collaborator_can_create_edit_and_confirm(
    registration_data: RegistrationData,
) -> None:
    """OWNER y COLLABORATOR pueden operar altas, borradores y confirmaciones."""
    registration = await create_registration(
        registration_data.context_collaborator_a,
        registration_data.tournament_a,
        build_draft(competitor_id=registration_data.competitor_a),
    )
    edited = await update_registration_draft(
        registration_data.context_collaborator_a,
        registration.id,
        build_draft(
            competitor_id=registration_data.competitor_a,
            category_key="gi-peso-83",
            category_label="Gi Peso -83 kg",
        ),
    )
    confirmed = await confirm_registration(registration_data.context_collaborator_a, edited.id)

    assert confirmed.status == "CONFIRMED"
    assert len(await audit_rows(registration.id)) == 3


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("category_key", "category_label"),
    [
        ("gi-peso-83", None),
        (None, "Gi Peso -83 kg"),
        ("Gi Peso 83", "Gi Peso -83 kg"),
        ("gi--peso", "Gi Peso -83 kg"),
        ("", "Gi Peso -83 kg"),
        ("gi-peso-83", "   "),
    ],
)
async def test_category_key_and_label_must_be_coherent(
    registration_data: RegistrationData, category_key: str | None, category_label: str | None
) -> None:
    """Clave estable y etiqueta: las dos o ninguna, y la clave con formato de clave."""
    with pytest.raises(InvalidRegistrationDataError):
        await create_registration(
            registration_data.context_owner_a,
            registration_data.tournament_a,
            build_draft(
                competitor_id=registration_data.competitor_a,
                category_key=category_key,
                category_label=category_label,
            ),
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_category_key_is_normalized_to_a_stable_slug(
    registration_data: RegistrationData,
) -> None:
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(
            competitor_id=registration_data.competitor_a,
            category_key="  GI-Peso-83  ",
            category_label="Gi Peso -83 kg",
        ),
    )
    assert registration.category_key == "gi-peso-83"
    # La etiqueta se conserva tal cual (solo se recorta): es la que ve la gente.
    assert registration.category_label == "Gi Peso -83 kg"


@pytest.mark.asyncio(loop_scope="session")
async def test_category_label_is_frozen_per_key_within_the_tournament(
    registration_data: RegistrationData,
) -> None:
    """Una misma clave no puede significar dos cosas distintas en el mismo torneo."""
    await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(
            competitor_id=registration_data.competitor_a,
            category_key="gi-peso-83",
            category_label="Gi Peso -83 kg",
        ),
    )

    # La misma etiqueta, otra identidad sin verificar: vale.
    ok = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(
            identity_status="UNVERIFIED",
            competitor_name_snapshot="Otra Sin Ficha",
            category_key="gi-peso-83",
            category_label="Gi Peso -83 kg",
        ),
    )
    assert ok.category_key == "gi-peso-83"

    # Otra etiqueta para la misma clave: rechazado.
    with pytest.raises(InvalidRegistrationDataError):
        await create_registration(
            registration_data.context_owner_a,
            registration_data.tournament_a,
            build_draft(
                identity_status="UNVERIFIED",
                competitor_name_snapshot="Otro Sin Ficha",
                category_key="gi-peso-83",
                category_label="Gi 83 kg (otra etiqueta)",
            ),
        )
