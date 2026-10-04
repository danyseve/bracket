# pylint: disable=redefined-outer-name  # `registration_data` es un fixture de modulo.
"""S3.2 de F3B - ciclo de vida de la inscripcion: O5 retirada, O5b readmision, O6 descalificacion.

TDD: estas pruebas se escribieron y se ejecutaron **antes** de que existieran las tres
operaciones en ``bracket/logic/registrations.py`` (rojo por importacion: ``withdraw_registration``,
``reinstate_registration`` y ``disqualify_registration`` no existian) y despues en verde con la
implementacion.

Cubren la matriz completa de transiciones y permisos (OWNER/COLLABORATOR), el aislamiento por
tenant, la auditoria (un evento por transicion efectiva, motivo obligatorio y no vacio, sin PII),
la idempotencia, la defensa del indice unico, la inmutabilidad de la clave de categoria, el
rollback integral y el caso que el contrato obliga a mirar de frente:

    readmitir una inscripcion que se retiro **siendo borrador** (nunca confirmada) no puede
    convertirse en una confirmacion silenciosa: la readmision aplica exactamente las mismas
    comprobaciones de elegibilidad que la confirmacion inicial (competidor y academia, con
    ``FOR SHARE`` y en el mismo orden de bloqueo) y conserva los snapshots ya materializados en
    la fila, de modo que la fila resultante es la misma que habria dejado ``confirm_registration``.

Datos, *fixture* y utilidades en ``registration_fixtures.py``. Base de datos exclusivamente de
laboratorio (``bracket_test``; con ``ENVIRONMENT=CI`` la base es ``bracket_ci``).
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from databases import Database

import bracket.logic.registrations as registrations_module
from bracket.database import database
from bracket.logic.competitors import InsufficientPrivilegesError, TenantNotAuthorizedError
from bracket.logic.registrations import (
    CompetitorNotSelectableError,
    DuplicateRegistrationError,
    InvalidRegistrationDataError,
    InvalidRegistrationStateError,
    RegistrationNotFoundError,
    SportsClubNotSelectableError,
    confirm_registration,
    create_registration,
    disqualify_registration,
    reinstate_registration,
    update_registration_draft,
    withdraw_registration,
)
from bracket.models.db.domain import RegistrationDraftData, TournamentRegistration
from bracket.sql.domain_reads import get_registration
from bracket.utils.id_types import TournamentRegistrationId
from tests.integration_tests.registration_fixtures import (
    INSERT_RAW_REGISTRATION,
    RegistrationData,
    audit_rows,
    build_draft,
    count_current_registrations,
    fetch_all_audit_rows_as_text,
    registration_data_context,
    set_competitor_active,
    set_sports_club_active,
)

UNKNOWN_REGISTRATION_ID = TournamentRegistrationId(999_999_999)


@pytest_asyncio.fixture(loop_scope="session")
async def registration_data(reinit_database: Database) -> AsyncIterator[RegistrationData]:
    """Datos de S3.1: envuelve ``registration_data_context`` y garantiza la limpieza."""
    async with registration_data_context(reinit_database) as data:
        yield data


def _categorized(
    registration_data: RegistrationData, key: str, **overrides: object
) -> RegistrationDraftData:
    """Borrador valido del competidor del tenant A, con clave de categoria unica por prueba."""
    return build_draft(
        competitor_id=registration_data.competitor_a,
        category_key=key,
        category_label=key.replace("-", " ").title(),
        **overrides,
    )


async def _raw_registration_in_status(registration_data: RegistrationData, status: str) -> int:
    """Fila cruda en un estado de origen.

    Solo se usa para ``CORRECTED``, que no tiene operacion de alta: una fila corregida nace de la
    correccion auditada (S6). Sin ``competitor_id`` queda fuera de los indices unicos parciales.
    """
    values: dict[str, object] = {
        "tournament_id": registration_data.tournament_a,
        "competitor_id": None,
        "identity_status": "UNVERIFIED",
        "representation": "INDEPENDENT",
        "competitor_name_snapshot": f"Inscripcion cruda {status}",
        "status": status,
    }
    return int(await database.fetch_val(query=INSERT_RAW_REGISTRATION, values=values))


async def _registration_in_status(
    registration_data: RegistrationData, status: str, *, key: str
) -> TournamentRegistration:
    """Inscripcion real en ese estado de origen, encadenando las operaciones del contrato."""
    context = registration_data.context_owner_a
    if status == "CORRECTED":
        registration_id = TournamentRegistrationId(
            await _raw_registration_in_status(registration_data, status)
        )
        row = await get_registration(registration_id, tenant_club_id=registration_data.tenant_a)
        assert row is not None
        return row

    registration = await create_registration(
        context, registration_data.tournament_a, _categorized(registration_data, key)
    )
    if status == "DRAFT":
        return registration
    if status == "CONFIRMED":
        return await confirm_registration(context, registration.id, reason="confirmacion de matriz")
    if status == "WITHDRAWN":
        return await withdraw_registration(context, registration.id, reason="retirada de matriz")
    assert status == "DISQUALIFIED"
    await confirm_registration(context, registration.id, reason="confirmacion de matriz")
    return await disqualify_registration(
        context, registration.id, reason="descalificacion de matriz"
    )


async def _confirmed(registration_data: RegistrationData, key: str) -> TournamentRegistration:
    """Inscripcion confirmada del competidor A (sin academia, categorizada)."""
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        _categorized(registration_data, key),
    )
    return await confirm_registration(registration_data.context_owner_a, registration.id)


async def _actions(registration_id: TournamentRegistrationId) -> list[str]:
    return [str(row["action"]) for row in await audit_rows(registration_id)]


# --- Matriz completa de transiciones ------------------------------------------------------------


_LIFECYCLE_MATRIX: tuple[tuple[str, str, str | None], ...] = (
    # (estado de origen, operacion, estado resultante; None = InvalidRegistrationStateError)
    ("DRAFT", "withdraw", "WITHDRAWN"),
    ("CONFIRMED", "withdraw", "WITHDRAWN"),
    ("WITHDRAWN", "withdraw", "WITHDRAWN"),
    ("DISQUALIFIED", "withdraw", None),
    ("CORRECTED", "withdraw", None),
    ("CONFIRMED", "disqualify", "DISQUALIFIED"),
    ("DISQUALIFIED", "disqualify", "DISQUALIFIED"),
    ("DRAFT", "disqualify", None),
    ("WITHDRAWN", "disqualify", None),
    ("CORRECTED", "disqualify", None),
    ("WITHDRAWN", "reinstate", "CONFIRMED"),
    ("DRAFT", "reinstate", None),
    ("CONFIRMED", "reinstate", None),
    ("DISQUALIFIED", "reinstate", None),
    ("CORRECTED", "reinstate", None),
)


@pytest.mark.asyncio(loop_scope="session")
async def test_the_full_transition_matrix(registration_data: RegistrationData) -> None:
    """Matriz completa: cada origen, cada operacion, un unico resultado admitido (docs/26 §3.3)."""
    operations = {
        "withdraw": withdraw_registration,
        "reinstate": reinstate_registration,
        "disqualify": disqualify_registration,
    }
    for index, (source, operation_name, expected) in enumerate(_LIFECYCLE_MATRIX):
        label = f"{source} --{operation_name}--> {expected}"
        registration = await _registration_in_status(
            registration_data, source, key=f"matriz-{index:02d}"
        )
        operation = operations[operation_name]
        if expected is None:
            with pytest.raises(InvalidRegistrationStateError):
                await operation(
                    registration_data.context_owner_a, registration.id, reason="prueba de matriz"
                )
        else:
            result = await operation(
                registration_data.context_owner_a, registration.id, reason="prueba de matriz"
            )
            assert result.status == expected, label
        after = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
        assert after is not None, label
        assert after.status == (expected or source), label


# --- O5: retirar --------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_withdraw_a_draft_audits_one_event_and_keeps_the_snapshots(
    registration_data: RegistrationData,
) -> None:
    """``DRAFT -> WITHDRAWN`` con OWNER: un evento, motivo normalizado y snapshots intactos."""
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(
            competitor_id=registration_data.competitor_a,
            representation="CLUB",
            sports_club_id=registration_data.sports_club_a,
            category_key="gi-absoluto",
            category_label="Gi Absoluto",
        ),
    )

    withdrawn = await withdraw_registration(
        registration_data.context_owner_a, registration.id, reason="  lesion en el calentamiento  "
    )

    assert withdrawn.id == registration.id
    assert withdrawn.status == "WITHDRAWN"
    assert withdrawn.updated_at is not None
    assert withdrawn.competitor_name_snapshot == "Ana Gomez"
    assert withdrawn.sports_club_name_snapshot == "Academia Propia A"
    assert withdrawn.category_key == "gi-absoluto"
    rows = await audit_rows(registration.id)
    assert [row["action"] for row in rows] == ["CREATE", "WITHDRAW"]
    assert rows[-1]["changed_fields"] == ["status"]
    assert rows[-1]["reason"] == "lesion en el calentamiento"
    assert rows[-1]["actor_label"] == "owner-a"
    assert rows[-1]["entity"] == "tournament_registration"
    assert rows[-1]["entity_id"] == registration.id


@pytest.mark.asyncio(loop_scope="session")
async def test_withdraw_a_draft_is_allowed_for_a_collaborator(
    registration_data: RegistrationData,
) -> None:
    """Un ``DRAFT`` no esta comprometido: OWNER y COLLABORATOR lo retiran por igual."""
    registration = await create_registration(
        registration_data.context_collaborator_a,
        registration_data.tournament_a,
        _categorized(registration_data, "no-gi-absoluto"),
    )

    withdrawn = await withdraw_registration(
        registration_data.context_collaborator_a, registration.id, reason="baja por el club"
    )

    assert withdrawn.status == "WITHDRAWN"
    assert await _actions(registration.id) == ["CREATE", "WITHDRAW"]


@pytest.mark.asyncio(loop_scope="session")
async def test_withdrawing_a_draft_does_not_require_eligible_competitor_or_academy(
    registration_data: RegistrationData,
) -> None:
    """Salir de un borrador bloqueado: retirar no exige competidor ni academia activos.

    El borrador se crea con la academia activa y despues se dan de baja la academia y el
    competidor: es exactamente el estado del que la retirada tiene que poder sacar al club.
    """
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(
            competitor_id=registration_data.competitor_a,
            representation="CLUB",
            sports_club_id=registration_data.sports_club_a,
            category_key="gi-peso-pesado",
            category_label="Gi Peso Pesado",
        ),
    )
    await set_competitor_active(registration_data.competitor_a, active=False)
    await set_sports_club_active(registration_data.sports_club_a, active=False)
    try:
        # Con la elegibilidad rota la confirmacion no pasa...
        with pytest.raises(CompetitorNotSelectableError):
            await confirm_registration(registration_data.context_owner_a, registration.id)
        # ...pero la retirada si: es la salida del borrador bloqueado.
        withdrawn = await withdraw_registration(
            registration_data.context_owner_a, registration.id, reason="salida del borrador"
        )
    finally:
        await set_competitor_active(registration_data.competitor_a, active=True)
        await set_sports_club_active(registration_data.sports_club_a, active=True)

    assert withdrawn.status == "WITHDRAWN"
    assert await _actions(registration.id) == ["CREATE", "WITHDRAW"]


@pytest.mark.asyncio(loop_scope="session")
async def test_withdraw_is_idempotent_and_writes_a_single_event(
    registration_data: RegistrationData,
) -> None:
    """``WITHDRAWN -> WITHDRAWN``: no-op, sin segundo evento y sin tocar la fila."""
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        _categorized(registration_data, "gi-master"),
    )
    first = await withdraw_registration(
        registration_data.context_owner_a, registration.id, reason="primera"
    )

    second = await withdraw_registration(
        registration_data.context_owner_a, registration.id, reason="reintento"
    )

    assert second.status == "WITHDRAWN"
    assert second.updated_at == first.updated_at
    assert await _actions(registration.id) == ["CREATE", "WITHDRAW"]


@pytest.mark.asyncio(loop_scope="session")
async def test_withdrawing_a_confirmed_registration_requires_the_owner(
    registration_data: RegistrationData,
) -> None:
    """``CONFIRMED -> WITHDRAWN`` es una retirada de algo comprometido: solo OWNER (A3/D-5)."""
    owned = await _confirmed(registration_data, "gi-absoluto-owner")
    by_collaborator = await _confirmed(registration_data, "gi-absoluto-collab")

    with pytest.raises(InsufficientPrivilegesError):
        await withdraw_registration(
            registration_data.context_collaborator_a, by_collaborator.id, reason="intento"
        )
    untouched = await get_registration(
        by_collaborator.id, tenant_club_id=registration_data.tenant_a
    )
    assert untouched is not None
    assert untouched.status == "CONFIRMED"
    assert await _actions(by_collaborator.id) == ["CREATE", "CONFIRM"]

    withdrawn = await withdraw_registration(
        registration_data.context_owner_a, owned.id, reason="baja de la competicion"
    )
    assert withdrawn.status == "WITHDRAWN"
    assert await _actions(owned.id) == ["CREATE", "CONFIRM", "WITHDRAW"]


@pytest.mark.asyncio(loop_scope="session")
async def test_withdrawn_from_confirmed_keeps_the_historic_and_the_unique_slot(
    registration_data: RegistrationData,
) -> None:
    """Retirar un ``CONFIRMED`` conserva snapshots y sigue ocupando la clave unica."""
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        _categorized(registration_data, "gi-absoluto-historico"),
    )
    confirmed = await confirm_registration(
        registration_data.context_owner_a, registration.id, reason="confirmada"
    )

    withdrawn = await withdraw_registration(
        registration_data.context_owner_a, registration.id, reason="baja posterior"
    )

    assert withdrawn.competitor_name_snapshot == confirmed.competitor_name_snapshot
    assert withdrawn.sports_club_id == confirmed.sports_club_id
    assert withdrawn.revision == confirmed.revision
    assert (
        await count_current_registrations(
            tournament_id=registration_data.tournament_a,
            competitor_id=registration_data.competitor_a,
        )
        == 1
    )
    with pytest.raises(DuplicateRegistrationError):
        await create_registration(
            registration_data.context_owner_a,
            registration_data.tournament_a,
            _categorized(registration_data, "gi-absoluto-historico"),
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_withdraw_rejects_a_registration_without_an_operation_to_leave(
    registration_data: RegistrationData,
) -> None:
    """``DISQUALIFIED`` y ``CORRECTED`` son terminales para la retirada."""
    disqualified = await _registration_in_status(
        registration_data, "DISQUALIFIED", key="gi-terminal"
    )
    corrected = await _registration_in_status(registration_data, "CORRECTED", key="gi-corregida")

    for registration in (disqualified, corrected):
        with pytest.raises(InvalidRegistrationStateError):
            await withdraw_registration(
                registration_data.context_owner_a, registration.id, reason="intento"
            )
        after = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
        assert after is not None
        assert after.status == registration.status


# --- O6: descalificar ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_disqualify_a_confirmed_registration_requires_the_owner(
    registration_data: RegistrationData,
) -> None:
    """``CONFIRMED -> DISQUALIFIED``: solo OWNER, un evento y el historico dentro."""
    registration = await _confirmed(registration_data, "gi-descalificable")

    with pytest.raises(InsufficientPrivilegesError):
        await disqualify_registration(
            registration_data.context_collaborator_a, registration.id, reason="intento"
        )
    untouched = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
    assert untouched is not None
    assert untouched.status == "CONFIRMED"
    assert await _actions(registration.id) == ["CREATE", "CONFIRM"]

    disqualified = await disqualify_registration(
        registration_data.context_owner_a, registration.id, reason="conducta antideportiva"
    )

    assert disqualified.status == "DISQUALIFIED"
    assert disqualified.competitor_name_snapshot == registration.competitor_name_snapshot
    assert disqualified.category_key == registration.category_key
    rows = await audit_rows(registration.id)
    assert [row["action"] for row in rows] == ["CREATE", "CONFIRM", "DISQUALIFY"]
    assert rows[-1]["changed_fields"] == ["status"]
    assert rows[-1]["reason"] == "conducta antideportiva"


@pytest.mark.asyncio(loop_scope="session")
async def test_disqualify_is_idempotent_and_a_disqualified_row_still_occupies_the_slot(
    registration_data: RegistrationData,
) -> None:
    """``DISQUALIFIED -> DISQUALIFIED``: no-op sin segundo evento, y la clave sigue ocupada."""
    registration = await _registration_in_status(
        registration_data, "DISQUALIFIED", key="gi-descalificada-idem"
    )

    second = await disqualify_registration(
        registration_data.context_owner_a, registration.id, reason="reintento"
    )

    assert second.status == "DISQUALIFIED"
    assert await _actions(registration.id) == ["CREATE", "CONFIRM", "DISQUALIFY"]
    assert (
        await count_current_registrations(
            tournament_id=registration_data.tournament_a,
            competitor_id=registration_data.competitor_a,
        )
        == 1
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_disqualify_rejects_a_registration_that_is_not_confirmed(
    registration_data: RegistrationData,
) -> None:
    """Descalificar un borrador (o algo ya retirado) es un error de estado, no un atajo."""
    draft = await _registration_in_status(registration_data, "DRAFT", key="gi-borrador")
    withdrawn = await _registration_in_status(registration_data, "WITHDRAWN", key="gi-retirada")

    for registration in (draft, withdrawn):
        with pytest.raises(InvalidRegistrationStateError):
            await disqualify_registration(
                registration_data.context_owner_a, registration.id, reason="intento"
            )


# --- O5b: readmitir -----------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_reinstate_a_withdrawal_that_never_was_confirmed_equals_a_first_confirmation(
    registration_data: RegistrationData,
) -> None:
    """El caso obligado: retirado en ``DRAFT``, readmitido a ``CONFIRMED`` sin atajos.

    La fila readmitida tiene que quedar como la habria dejado una confirmacion inicial: mismas
    comprobaciones de elegibilidad (competidor y academia activos, con ``FOR SHARE`` y en el orden
    de bloqueo del contrato) y los snapshots ya materializados en el borrador, que ni la
    confirmacion ni la readmision reescriben. Se compara campo a campo con una confirmacion directa.
    """
    direct = await _confirmed(registration_data, "comparacion-directa")
    draft = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        _categorized(registration_data, "comparacion-ciclo"),
    )
    withdrawn = await withdraw_registration(
        registration_data.context_owner_a, draft.id, reason="retirada previa a la confirmacion"
    )
    assert withdrawn.status == "WITHDRAWN"

    reinstated = await reinstate_registration(
        registration_data.context_owner_a, draft.id, reason="subsanado el problema"
    )

    assert reinstated.id == draft.id
    assert reinstated.status == "CONFIRMED"
    for field in (
        "tournament_id",
        "competitor_id",
        "identity_status",
        "representation",
        "sports_club_id",
        "affiliation_id",
        "competitor_name_snapshot",
        "sports_club_name_snapshot",
        "status",
        "revision",
        "corrects_registration_id",
        "superseded_by_registration_id",
    ):
        assert getattr(reinstated, field) == getattr(direct, field), field
    assert await _actions(draft.id) == ["CREATE", "WITHDRAW", "REINSTATE"]
    assert await _actions(direct.id) == ["CREATE", "CONFIRM"]


@pytest.mark.asyncio(loop_scope="session")
async def test_reinstate_revalidates_the_competitor_and_keeps_the_row_withdrawn(
    registration_data: RegistrationData,
) -> None:
    """Readmitir exige el competidor activo: no se confirma a nadie dado de baja."""
    draft = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        _categorized(registration_data, "gi-readmision-competidor"),
    )
    await withdraw_registration(registration_data.context_owner_a, draft.id, reason="retirada")
    await set_competitor_active(registration_data.competitor_a, active=False)
    try:
        with pytest.raises(CompetitorNotSelectableError):
            await reinstate_registration(
                registration_data.context_owner_a, draft.id, reason="intento de readmision"
            )
        after = await get_registration(draft.id, tenant_club_id=registration_data.tenant_a)
        assert after is not None
        assert after.status == "WITHDRAWN"
        assert await _actions(draft.id) == ["CREATE", "WITHDRAW"]

        # Reactivar la identidad (S2-bis) abre la readmision sin tocar la fila retirada.
        await set_competitor_active(registration_data.competitor_a, active=True)
        reinstated = await reinstate_registration(
            registration_data.context_owner_a, draft.id, reason="identidad reactivada"
        )
    finally:
        await set_competitor_active(registration_data.competitor_a, active=True)

    assert reinstated.status == "CONFIRMED"
    assert await _actions(draft.id) == ["CREATE", "WITHDRAW", "REINSTATE"]


@pytest.mark.asyncio(loop_scope="session")
async def test_reinstate_revalidates_the_represented_academy(
    registration_data: RegistrationData,
) -> None:
    """Readmitir una inscripcion que representa una academia exige esa academia activa (A5)."""
    draft = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(
            competitor_id=registration_data.competitor_a,
            representation="CLUB",
            sports_club_id=registration_data.sports_club_a,
            category_key="gi-readmision-academia",
            category_label="Gi Readmision Academia",
        ),
    )
    await withdraw_registration(registration_data.context_owner_a, draft.id, reason="retirada")
    await set_sports_club_active(registration_data.sports_club_a, active=False)
    try:
        with pytest.raises(SportsClubNotSelectableError):
            await reinstate_registration(
                registration_data.context_owner_a, draft.id, reason="intento de readmision"
            )
    finally:
        await set_sports_club_active(registration_data.sports_club_a, active=True)

    after = await get_registration(draft.id, tenant_club_id=registration_data.tenant_a)
    assert after is not None
    assert after.status == "WITHDRAWN"
    assert await _actions(draft.id) == ["CREATE", "WITHDRAW"]

    reinstated = await reinstate_registration(
        registration_data.context_owner_a, draft.id, reason="academia reactivada"
    )
    assert reinstated.status == "CONFIRMED"


@pytest.mark.asyncio(loop_scope="session")
async def test_reinstate_is_allowed_for_a_collaborator(
    registration_data: RegistrationData,
) -> None:
    """Readmitir no es una operacion sensible: OWNER y COLLABORATOR."""
    draft = await create_registration(
        registration_data.context_collaborator_a,
        registration_data.tournament_a,
        _categorized(registration_data, "gi-readmision-colaborador"),
    )
    await withdraw_registration(
        registration_data.context_collaborator_a, draft.id, reason="retirada del colaborador"
    )

    reinstated = await reinstate_registration(
        registration_data.context_collaborator_a, draft.id, reason="readmision del colaborador"
    )

    assert reinstated.status == "CONFIRMED"
    assert await _actions(draft.id) == ["CREATE", "WITHDRAW", "REINSTATE"]


@pytest.mark.asyncio(loop_scope="session")
async def test_reinstate_of_a_withdrawn_confirmed_registration_keeps_the_frozen_snapshots(
    registration_data: RegistrationData,
) -> None:
    """Readmitir algo que si se confirmo conserva el snapshot congelado por la confirmacion."""
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        _categorized(registration_data, "gi-readmision-historico"),
    )
    confirmed = await confirm_registration(
        registration_data.context_owner_a, registration.id, reason="confirmada"
    )
    await withdraw_registration(
        registration_data.context_owner_a, registration.id, reason="retirada posterior"
    )

    reinstated = await reinstate_registration(
        registration_data.context_owner_a, registration.id, reason="vuelve al torneo"
    )

    assert reinstated.competitor_name_snapshot == confirmed.competitor_name_snapshot
    assert reinstated.status == "CONFIRMED"
    assert await _actions(registration.id) == ["CREATE", "CONFIRM", "WITHDRAW", "REINSTATE"]


@pytest.mark.asyncio(loop_scope="session")
async def test_reinstate_rejects_a_registration_that_is_not_withdrawn(
    registration_data: RegistrationData,
) -> None:
    """Solo ``WITHDRAWN`` se readmite: un ``CONFIRMED`` ya esta confirmado (no es un no-op)."""
    draft = await _registration_in_status(registration_data, "DRAFT", key="gi-readmision-draft")
    confirmed = await _registration_in_status(
        registration_data, "CONFIRMED", key="gi-readmision-confirmed"
    )
    disqualified = await _registration_in_status(
        registration_data, "DISQUALIFIED", key="gi-readmision-disqualified"
    )

    for registration in (draft, confirmed, disqualified):
        with pytest.raises(InvalidRegistrationStateError):
            await reinstate_registration(
                registration_data.context_owner_a, registration.id, reason="intento"
            )
        after = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
        assert after is not None
        assert after.status == registration.status


@pytest.mark.asyncio(loop_scope="session")
async def test_a_registration_with_unknown_identity_travels_the_whole_lifecycle(
    registration_data: RegistrationData,
) -> None:
    """Sin identidad verificada no hay elegibilidad que revalidar, pero el ciclo se cumple."""
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(
            competitor_name_snapshot="Inscripcion sin identidad",
            category_key="gi-sin-identidad",
            category_label="Gi Sin Identidad",
        ),
    )
    withdrawn = await withdraw_registration(
        registration_data.context_owner_a, registration.id, reason="retirada"
    )
    reinstated = await reinstate_registration(
        registration_data.context_owner_a, registration.id, reason="readmision"
    )

    assert withdrawn.competitor_id is None
    assert reinstated.status == "CONFIRMED"
    assert reinstated.identity_status == "UNVERIFIED"
    assert reinstated.competitor_name_snapshot == "Inscripcion sin identidad"
    assert await _actions(registration.id) == ["CREATE", "WITHDRAW", "REINSTATE"]


# --- Permisos, tenant y entradas invalidas ------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_lifecycle_operations_are_invisible_from_another_tenant(
    registration_data: RegistrationData,
) -> None:
    """Un actor de otro tenant no ve la inscripcion: mismo error que si no existiera."""
    registration = await _confirmed(registration_data, "gi-otro-tenant")

    for operation in (withdraw_registration, reinstate_registration, disqualify_registration):
        with pytest.raises(RegistrationNotFoundError):
            await operation(
                registration_data.context_owner_b, registration.id, reason="intento ajeno"
            )
    after = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
    assert after is not None
    assert after.status == "CONFIRMED"
    assert await _actions(registration.id) == ["CREATE", "CONFIRM"]


@pytest.mark.asyncio(loop_scope="session")
async def test_lifecycle_operations_reject_an_actor_without_relation_to_the_tenant(
    registration_data: RegistrationData,
) -> None:
    """Sin relacion con el tenant no hay acceso, ni siquiera para retirar un borrador."""
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        _categorized(registration_data, "gi-sin-relacion"),
    )

    for operation in (withdraw_registration, reinstate_registration, disqualify_registration):
        with pytest.raises(TenantNotAuthorizedError):
            await operation(registration_data.context_outsider_a, registration.id, reason="intento")
    after = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
    assert after is not None
    assert after.status == "DRAFT"
    assert await _actions(registration.id) == ["CREATE"]


@pytest.mark.asyncio(loop_scope="session")
async def test_lifecycle_operations_reject_an_unknown_registration(
    registration_data: RegistrationData,
) -> None:
    """Una inscripcion inexistente da el mismo error que una de otro tenant."""
    for operation in (withdraw_registration, reinstate_registration, disqualify_registration):
        with pytest.raises(RegistrationNotFoundError):
            await operation(registration_data.context_owner_a, UNKNOWN_REGISTRATION_ID, reason="x")


@pytest.mark.asyncio(loop_scope="session")
async def test_the_reason_is_mandatory_and_cannot_be_blank(
    registration_data: RegistrationData,
) -> None:
    """El motivo es obligatorio en las tres operaciones (docs/26 §3) y nunca admite vacios."""
    for operation in (withdraw_registration, reinstate_registration, disqualify_registration):
        parameters = inspect.signature(operation).parameters
        assert list(parameters) == ["context", "registration_id", "reason"]
        assert parameters["reason"].default is inspect.Parameter.empty

    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        _categorized(registration_data, "gi-motivo"),
    )
    for blank in ("", "   ", "\t"):
        with pytest.raises(InvalidRegistrationDataError):
            await withdraw_registration(
                registration_data.context_owner_a, registration.id, reason=blank
            )
    after = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
    assert after is not None
    assert after.status == "DRAFT"
    assert await _actions(registration.id) == ["CREATE"]


# --- Integridad y rollback ----------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_the_category_key_cannot_change_once_the_draft_is_left(
    registration_data: RegistrationData,
) -> None:
    """Retirada o confirmada, la inscripcion deja de ser editable: la clave queda congelada."""
    a = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        _categorized(registration_data, "gi-congelada"),
    )
    await withdraw_registration(registration_data.context_owner_a, a.id, reason="retirada")
    reinstated = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        _categorized(registration_data, "gi-congelada-confirmada"),
    )
    await confirm_registration(
        registration_data.context_owner_a, reinstated.id, reason="confirmada"
    )

    for registration in (a, reinstated):
        with pytest.raises(InvalidRegistrationStateError):
            await update_registration_draft(
                registration_data.context_owner_a,
                registration.id,
                _categorized(registration_data, "gi-congelada-nueva"),
                reason="intento de reescritura",
            )
        after = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
        assert after is not None
        assert after.category_key == registration.category_key


@pytest.mark.asyncio(loop_scope="session")
async def test_an_audit_failure_rolls_back_the_transition(
    registration_data: RegistrationData, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rollback integral: sin evento de auditoria la transicion no ocurre."""

    async def _boom(**_kwargs: object) -> None:
        raise RuntimeError("auditoria no disponible")

    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        _categorized(registration_data, "gi-rollback"),
    )
    await withdraw_registration(
        registration_data.context_owner_a, registration.id, reason="retirada"
    )
    monkeypatch.setattr(registrations_module, "sql_insert_domain_change_log", _boom)

    with pytest.raises(RuntimeError):
        await reinstate_registration(
            registration_data.context_owner_a, registration.id, reason="readmision"
        )

    after = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
    assert after is not None
    assert after.status == "WITHDRAWN"
    assert await _actions(registration.id) == ["CREATE", "WITHDRAW"]


@pytest.mark.asyncio(loop_scope="session")
async def test_no_pii_in_the_lifecycle_audit_or_errors(
    registration_data: RegistrationData,
) -> None:
    """Ni valores personales en la auditoria del ciclo ni datos personales en los errores."""
    sensitive_tokens = ("Ana", "Gomez", "Academia Propia A", "Absoluto")
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(
            competitor_id=registration_data.competitor_a,
            representation="CLUB",
            sports_club_id=registration_data.sports_club_a,
            category_key="gi-absoluto-pii",
            category_label="Gi Absoluto Pii",
        ),
    )
    await confirm_registration(
        registration_data.context_owner_a, registration.id, reason="confirmada"
    )
    with pytest.raises(InsufficientPrivilegesError) as exc_info:
        await disqualify_registration(
            registration_data.context_collaborator_a, registration.id, reason="intento"
        )
    assert all(token not in str(exc_info.value) for token in sensitive_tokens)

    await withdraw_registration(
        registration_data.context_owner_a, registration.id, reason="baja por lesion"
    )
    await set_competitor_active(registration_data.competitor_a, active=False)
    try:
        with pytest.raises(CompetitorNotSelectableError) as exc_info:
            await reinstate_registration(
                registration_data.context_owner_a, registration.id, reason="intento invalido"
            )
    finally:
        await set_competitor_active(registration_data.competitor_a, active=True)
    assert all(token not in str(exc_info.value) for token in sensitive_tokens)

    dump = str(await audit_rows(registration.id))
    assert all(token not in dump for token in sensitive_tokens)
    assert all(token not in str(await fetch_all_audit_rows_as_text()) for token in sensitive_tokens)
