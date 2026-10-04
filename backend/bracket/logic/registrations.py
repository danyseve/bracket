"""Logica de dominio del nucleo de inscripciones (S3.1 de F3B).

Tres operaciones internas, **sin HTTP ni UI todavia**:

* :func:`create_registration` — alta en borrador (``DRAFT``);
* :func:`update_registration_draft` — edicion del borrador (reemplazo explicito);
* :func:`confirm_registration` — ``DRAFT -> CONFIRMED`` (congela los snapshots).

Las consultas no se duplican aqui: se usan las de S1
(``bracket/sql/domain_reads.py``), que ya aplican el aislamiento por tenant.

Reglas del contrato (``docs/26`` §2-§9; decisiones aprobadas el 2026-10-04):

* El torneo entra como identificador explicito y se comprueba contra el tenant del
  :class:`ActorContext`. Un torneo de otro tenant y un torneo inexistente dan el
  mismo error: no se filtra su existencia.
* El competidor es una identidad del tenant, o ``None`` (identidad desconocida): en
  ese caso el nombre de origen es obligatorio y queda congelado como snapshot.
* La academia representada se **elige** (jamas se deduce de la afiliacion primaria) y
  tiene que estar activa; una participacion ``INDEPENDENT`` no lleva academia.
* ``category_key`` es una clave estable (slug en minusculas) y ``category_label``
  queda congelada por clave dentro del torneo. Nada de deduplicar por nombre.
* Los snapshots de nombre del competidor y de la academia **se derivan** al escribir;
  solo la inscripcion sin identidad acepta el nombre desde el payload.
* Un borrador es editable; una inscripcion ``CONFIRMED`` no se toca en sitio (la
  correccion auditada es S6). Ninguna escritura cambia el estado en silencio.
* Cada operacion es una transaccion: la fila y su evento de auditoria se confirman o
  se revierten juntos. Contra duplicados manda el indice unico parcial, no un
  ``SELECT`` previo.
* Sin PII: ni los errores ni la auditoria llevan valores, solo nombres de campo.

Permisos (S3.1 §5): OWNER y COLLABORATOR pueden dar de alta, editar borradores y
confirmar. Retirar una inscripcion confirmada, descalificar y corregir exigiran OWNER
en sus propios incrementos; aqui no hay ninguna operacion de ese tipo.

LIMITACIONES PENDIENTES (heredadas de S2, no resueltas aqui):

1. El :class:`ActorContext` lo construye el llamador: mientras no exista la capa HTTP
   no hay sesion que impida a un llamador interno inventarse un ``tenant_club_id``.
2. ``domain_change_log`` no tiene columna de tenant.
3. La autorizacion se apoya en ``users_x_clubs``; no hay roles de dominio ni auditoria
   de intentos denegados.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import asyncpg  # type: ignore[import-untyped]
from heliclockter import datetime_utc

from bracket.database import database
from bracket.logic.competitors import CompetitorNotFoundError, TenantNotAuthorizedError
from bracket.models.db.domain import (
    ActorContext,
    RegistrationDraftData,
    RegistrationIdentityStatus,
    RegistrationRepresentation,
    TournamentRegistration,
)
from bracket.sql import registration_writes
from bracket.sql.domain_reads import get_competitor, get_competitor_affiliations, get_registration
from bracket.sql.domain_writes import sql_insert_domain_change_log
from bracket.sql.users import get_user_relation_to_club
from bracket.utils.id_types import (
    CompetitorId,
    CompetitorXSportsClubId,
    SportsClubId,
    TournamentId,
    TournamentRegistrationId,
    UserId,
)
from bracket.utils.types import assert_some

MAX_COMPETITOR_NAME_LENGTH = 200
MAX_CATEGORY_KEY_LENGTH = 64
MAX_CATEGORY_LABEL_LENGTH = 200
MAX_REASON_LENGTH = 500

_REGISTRATION_ENTITY = "tournament_registration"

# Una clave de categoria estable: minusculas, digitos y guiones simples.
_CATEGORY_KEY_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

_DEFAULT_REASONS: dict[str, str] = {
    "CREATE": "alta de inscripcion",
    "UPDATE": "edicion de borrador de inscripcion",
    "CONFIRM": "confirmacion de inscripcion",
}

# Nombres de campo del alta (jamas valores: sin PII en auditoria).
_CREATE_CHANGED_FIELDS: tuple[str, ...] = (
    "competitor_id",
    "identity_status",
    "representation",
    "sports_club_id",
    "sports_club_name_snapshot",
    "affiliation_id",
    "category_key",
    "category_label",
    "competitor_name_snapshot",
    "status",
)

# Campos que una edicion de borrador puede cambiar y que se auditan por nombre.
_COMPARABLE_FIELDS: tuple[str, ...] = (
    "competitor_id",
    "identity_status",
    "representation",
    "sports_club_id",
    "sports_club_name_snapshot",
    "affiliation_id",
    "category_key",
    "category_label",
    "competitor_name_snapshot",
)

_DUPLICATE_INDEXES: frozenset[str] = frozenset(
    {
        "uq_tournament_registrations_current_category",
        "uq_tournament_registrations_current_uncategorized",
    }
)

_DUPLICATE_MESSAGE = (
    "ya existe una inscripcion vigente de ese competidor en esa categoria del torneo"
)


class RegistrationDomainError(Exception):
    """Base de los errores de la capa interna. Los mensajes no llevan datos personales."""


class TournamentNotFoundError(RegistrationDomainError):
    """El torneo no existe o no es del tenant del contexto (el mismo error a proposito)."""


class RegistrationNotFoundError(RegistrationDomainError):
    """La inscripcion no existe o no es del tenant del contexto."""


class DuplicateRegistrationError(RegistrationDomainError):
    """Ya hay una inscripcion vigente de ese competidor en esa categoria del torneo."""


class InvalidRegistrationDataError(RegistrationDomainError):
    """Datos de entrada invalidos (categoria incoherente, afiliacion que no cuadra, ...)."""


class SportsClubNotSelectableError(InvalidRegistrationDataError):
    """La academia representada no existe o no esta activa: no es elegible."""


class InvalidRegistrationStateError(RegistrationDomainError):
    """La transicion pedida no es valida en el estado actual de la inscripcion."""


@dataclass(frozen=True)
class _ResolvedDraft:  # pylint: disable=too-many-instance-attributes
    """Borrador validado y con los snapshots ya derivados. Todavia no escrito."""

    competitor_id: CompetitorId | None
    identity_status: RegistrationIdentityStatus
    representation: RegistrationRepresentation
    sports_club_id: SportsClubId | None
    sports_club_name_snapshot: str | None
    affiliation_id: CompetitorXSportsClubId | None
    category_key: str | None
    category_label: str | None
    competitor_name_snapshot: str
    verified_by_user_id: UserId | None
    verified_at: datetime_utc | None


def _normalize_text(value: object, *, field: str, max_length: int) -> str:
    """Valida texto libre de entrada. El mensaje nombra el **campo**, nunca el valor."""
    if not isinstance(value, str):
        raise InvalidRegistrationDataError(f"{field} debe ser texto")
    normalized = value.strip()
    if not normalized:
        raise InvalidRegistrationDataError(f"{field} no puede estar vacio")
    if len(normalized) > max_length:
        raise InvalidRegistrationDataError(f"{field} supera la longitud maxima permitida")
    if not normalized.isprintable():
        raise InvalidRegistrationDataError(f"{field} contiene caracteres de control")
    return normalized


def _normalize_reason(reason: object, action: str) -> str:
    if reason is None:
        return _DEFAULT_REASONS[action]
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidRegistrationDataError("reason no puede estar vacio")
    normalized = reason.strip()
    if len(normalized) > MAX_REASON_LENGTH or not normalized.isprintable():
        raise InvalidRegistrationDataError("reason no es valido")
    return normalized


def _is_duplicate_registration(exc: asyncpg.exceptions.UniqueViolationError) -> bool:
    """Solo se traducen los indices de unicidad de inscripciones; el resto se propaga."""
    return exc.as_dict().get("constraint_name") in _DUPLICATE_INDEXES


async def _authorize(context: ActorContext) -> None:
    """El actor tiene que tener relacion con el tenant del contexto.

    No distingue "tenant inexistente" de "tenant no autorizado": no se filtra la
    existencia del tenant. OWNER y COLLABORATOR valen igual para este nucleo.
    """
    relation = await get_user_relation_to_club(context.tenant_club_id, context.actor_user_id)
    if relation is None:
        raise TenantNotAuthorizedError("el actor no tiene acceso a este tenant")


async def _assert_tournament_in_tenant(context: ActorContext, tournament_id: TournamentId) -> None:
    """El torneo forma parte del tenant del contexto, o el error es el mismo que si no existiera."""
    if not await registration_writes.sql_tournament_is_in_tenant(
        tournament_id=tournament_id, tenant_club_id=context.tenant_club_id
    ):
        raise TournamentNotFoundError("torneo no encontrado en este tenant")


def _resolve_category(data: RegistrationDraftData) -> tuple[str | None, str | None]:
    """Clave estable + etiqueta congelada: las dos o ninguna. Nunca se inventa la clave."""
    key, label = data.category_key, data.category_label
    if key is None and label is None:
        return None, None
    if key is None or label is None:
        raise InvalidRegistrationDataError(
            "category_key y category_label van juntos, o no va ninguno de los dos"
        )
    if not isinstance(key, str):
        raise InvalidRegistrationDataError("category_key debe ser texto")
    normalized_key = key.strip().lower()
    if not normalized_key or len(normalized_key) > MAX_CATEGORY_KEY_LENGTH:
        raise InvalidRegistrationDataError("category_key no es valida")
    if _CATEGORY_KEY_PATTERN.match(normalized_key) is None:
        raise InvalidRegistrationDataError(
            "category_key debe ser una clave estable en minusculas (letras, digitos y guiones)"
        )
    return normalized_key, _normalize_text(
        label, field="category_label", max_length=MAX_CATEGORY_LABEL_LENGTH
    )


async def _resolve_representation(
    data: RegistrationDraftData,
) -> tuple[SportsClubId | None, str | None]:
    """Academia representada explicita y activa, o participacion independiente sin academia."""
    if data.representation == "INDEPENDENT":
        if data.sports_club_id is not None:
            raise InvalidRegistrationDataError(
                "una participacion independiente no lleva academia representada"
            )
        return None, None
    if data.sports_club_id is None:
        raise InvalidRegistrationDataError(
            "representar una academia exige indicar cual: no se deduce de la afiliacion"
        )
    sports_club = await registration_writes.sql_selectable_sports_club(data.sports_club_id)
    if sports_club is None:
        raise SportsClubNotSelectableError("la academia representada no existe o no esta activa")
    return sports_club.id, sports_club.name


async def _resolve_identity(
    context: ActorContext, data: RegistrationDraftData
) -> tuple[
    CompetitorId | None, RegistrationIdentityStatus, str, UserId | None, datetime_utc | None
]:
    """Identidad verificada del tenant, o identidad desconocida con nombre de origen."""
    if data.competitor_id is None:
        if data.identity_status == "VERIFIED":
            raise InvalidRegistrationDataError(
                "sin competidor no se puede declarar una identidad verificada"
            )
        if data.competitor_name_snapshot is None:
            raise InvalidRegistrationDataError(
                "una inscripcion sin identidad verificada exige el nombre de origen"
            )
        name_snapshot = _normalize_text(
            data.competitor_name_snapshot,
            field="competitor_name_snapshot",
            max_length=MAX_COMPETITOR_NAME_LENGTH,
        )
        return None, data.identity_status or "UNVERIFIED", name_snapshot, None, None

    if data.identity_status is not None:
        raise InvalidRegistrationDataError(
            "identity_status se deriva del competidor: no se acepta desde el payload"
        )
    if data.competitor_name_snapshot is not None:
        raise InvalidRegistrationDataError(
            "el nombre de un competidor verificado se deriva: no se acepta desde el payload"
        )
    competitor = await get_competitor(data.competitor_id, tenant_club_id=context.tenant_club_id)
    if competitor is None:
        raise CompetitorNotFoundError("competidor no encontrado en este tenant")
    return (
        competitor.id,
        "VERIFIED",
        competitor.display_name,
        context.actor_user_id,
        datetime_utc.now(),
    )


async def _resolve_affiliation(
    context: ActorContext,
    *,
    data: RegistrationDraftData,
    sports_club_id: SportsClubId | None,
) -> CompetitorXSportsClubId | None:
    """La afiliacion, si se indica, tiene que ser del competidor y de la academia representada."""
    if data.affiliation_id is None:
        return None
    if data.competitor_id is None or data.representation != "CLUB" or sports_club_id is None:
        raise InvalidRegistrationDataError(
            "una afiliacion solo tiene sentido con competidor verificado y academia representada"
        )
    affiliations = await get_competitor_affiliations(
        data.competitor_id, tenant_club_id=context.tenant_club_id, include_history=True
    )
    if not any(
        affiliation.id == data.affiliation_id and affiliation.sports_club_id == sports_club_id
        for affiliation in affiliations
    ):
        raise InvalidRegistrationDataError(
            "la afiliacion no pertenece al competidor o no corresponde a la academia representada"
        )
    return data.affiliation_id


async def _assert_category_label_frozen(
    *, tournament_id: TournamentId, category_key: str, category_label: str
) -> None:
    """Una ``category_key`` no puede significar dos cosas distintas en el mismo torneo."""
    labels = await registration_writes.sql_category_labels_for_key(
        tournament_id=tournament_id, category_key=category_key
    )
    if any(label != category_label for label in labels):
        raise InvalidRegistrationDataError(
            "esa category_key ya esta congelada con otra etiqueta en este torneo"
        )


async def _resolve_draft(
    context: ActorContext, *, tournament_id: TournamentId, data: RegistrationDraftData
) -> _ResolvedDraft:
    """Valida el payload y deriva los snapshots. No escribe nada."""
    category_key, category_label = _resolve_category(data)
    sports_club_id, sports_club_name_snapshot = await _resolve_representation(data)
    (
        competitor_id,
        identity_status,
        name_snapshot,
        verified_by_user_id,
        verified_at,
    ) = await _resolve_identity(context, data)
    affiliation_id = await _resolve_affiliation(context, data=data, sports_club_id=sports_club_id)
    if category_key is not None:
        await _assert_category_label_frozen(
            tournament_id=tournament_id,
            category_key=category_key,
            category_label=assert_some(category_label),
        )
    return _ResolvedDraft(
        competitor_id=competitor_id,
        identity_status=identity_status,
        representation=data.representation,
        sports_club_id=sports_club_id,
        sports_club_name_snapshot=sports_club_name_snapshot,
        affiliation_id=affiliation_id,
        category_key=category_key,
        category_label=category_label,
        competitor_name_snapshot=name_snapshot,
        verified_by_user_id=verified_by_user_id,
        verified_at=verified_at,
    )


def _changed_field_names(current: TournamentRegistration, draft: _ResolvedDraft) -> list[str]:
    """Nombres de los campos cuyo valor cambia con el reemplazo (jamas los valores)."""
    return [
        field for field in _COMPARABLE_FIELDS if getattr(current, field) != getattr(draft, field)
    ]


async def create_registration(
    context: ActorContext,
    tournament_id: TournamentId,
    data: RegistrationDraftData,
    *,
    reason: str | None = None,
) -> TournamentRegistration:
    """Alta de una inscripcion en borrador. El tenant sale del contexto, no del payload."""
    normalized_reason = _normalize_reason(reason, "CREATE")

    async with database.transaction():
        await _authorize(context)
        await _assert_tournament_in_tenant(context, tournament_id)
        draft = await _resolve_draft(context, tournament_id=tournament_id, data=data)
        try:
            registration = await registration_writes.sql_insert_registration(
                tournament_id=tournament_id,
                competitor_id=draft.competitor_id,
                identity_status=draft.identity_status,
                representation=draft.representation,
                sports_club_id=draft.sports_club_id,
                affiliation_id=draft.affiliation_id,
                category_key=draft.category_key,
                category_label=draft.category_label,
                competitor_name_snapshot=draft.competitor_name_snapshot,
                sports_club_name_snapshot=draft.sports_club_name_snapshot,
                verified_by_user_id=draft.verified_by_user_id,
                verified_at=draft.verified_at,
            )
        except asyncpg.exceptions.UniqueViolationError as exc:
            if not _is_duplicate_registration(exc):
                raise
            raise DuplicateRegistrationError(_DUPLICATE_MESSAGE) from exc
        await sql_insert_domain_change_log(
            entity=_REGISTRATION_ENTITY,
            entity_id=registration.id,
            action="CREATE",
            changed_fields=list(_CREATE_CHANGED_FIELDS),
            actor_user_id=context.actor_user_id,
            actor_label=context.actor_label,
            reason=normalized_reason,
        )
    return registration


async def update_registration_draft(
    context: ActorContext,
    registration_id: TournamentRegistrationId,
    data: RegistrationDraftData,
    *,
    reason: str | None = None,
) -> TournamentRegistration:
    """Edita un borrador del tenant (reemplazo explicito) y lo audita como ``UPDATE``.

    Solo ``DRAFT``: una inscripcion confirmada no se sobrescribe en sitio, se corrige
    de forma auditada (S6). Si el reemplazo no cambia nada no se escribe auditoria.
    """
    normalized_reason = _normalize_reason(reason, "UPDATE")

    async with database.transaction():
        await _authorize(context)
        current = await get_registration(registration_id, tenant_club_id=context.tenant_club_id)
        if current is None:
            raise RegistrationNotFoundError("inscripcion no encontrada en este tenant")
        if current.status != "DRAFT":
            raise InvalidRegistrationStateError(
                "solo un borrador se edita en sitio; una inscripcion confirmada se corrige "
                "de forma auditada"
            )
        draft = await _resolve_draft(context, tournament_id=current.tournament_id, data=data)
        changed_fields = _changed_field_names(current, draft)
        if not changed_fields:
            return current
        try:
            updated = await registration_writes.sql_update_registration_draft(
                registration_id=registration_id,
                tenant_club_id=context.tenant_club_id,
                competitor_id=draft.competitor_id,
                identity_status=draft.identity_status,
                representation=draft.representation,
                sports_club_id=draft.sports_club_id,
                affiliation_id=draft.affiliation_id,
                category_key=draft.category_key,
                category_label=draft.category_label,
                competitor_name_snapshot=draft.competitor_name_snapshot,
                sports_club_name_snapshot=draft.sports_club_name_snapshot,
                verified_by_user_id=draft.verified_by_user_id,
                verified_at=draft.verified_at,
            )
        except asyncpg.exceptions.UniqueViolationError as exc:
            if not _is_duplicate_registration(exc):
                raise
            raise DuplicateRegistrationError(_DUPLICATE_MESSAGE) from exc
        if updated is None:
            raise InvalidRegistrationStateError(
                "la inscripcion dejo de ser un borrador editable durante la operacion"
            )
        await sql_insert_domain_change_log(
            entity=_REGISTRATION_ENTITY,
            entity_id=updated.id,
            action="UPDATE",
            changed_fields=changed_fields,
            actor_user_id=context.actor_user_id,
            actor_label=context.actor_label,
            reason=normalized_reason,
        )
    return updated


async def _assert_represented_academy_active(registration: TournamentRegistration) -> None:
    """Regla A5 al confirmar: una inscripcion nueva no representa una academia inactiva.

    Se comprueba al escribir el borrador **y** al confirmarlo: una academia puede
    desactivarse entre las dos operaciones. El FK de ``sports_club_id`` es ``RESTRICT``
    (una academia con inscripciones no se borra), asi que desactivarla es la unica
    forma de dejar de ser representable. El error no distingue "no existe" de "no esta
    activa", y el borrador no se pierde: sigue editable.
    """
    if registration.sports_club_id is None:
        return
    if await registration_writes.sql_selectable_sports_club(registration.sports_club_id) is None:
        raise SportsClubNotSelectableError(
            "la academia representada dejo de estar activa: el borrador sigue editable"
        )


async def confirm_registration(
    context: ActorContext,
    registration_id: TournamentRegistrationId,
    *,
    reason: str | None = None,
) -> TournamentRegistration:
    """``DRAFT -> CONFIRMED``: congela los snapshots y deja de ser editable.

    Idempotente: repetir la confirmacion no cambia la fila ni escribe un segundo evento
    de auditoria. Confirmar algo que no es un borrador (retirada, descalificada,
    corregida) es un error de estado, no un exito silencioso.
    """
    normalized_reason = _normalize_reason(reason, "CONFIRM")

    async with database.transaction():
        await _authorize(context)
        current = await get_registration(registration_id, tenant_club_id=context.tenant_club_id)
        if current is None:
            raise RegistrationNotFoundError("inscripcion no encontrada en este tenant")
        if current.status == "CONFIRMED":
            return current
        if current.status != "DRAFT":
            raise InvalidRegistrationStateError("solo un borrador se puede confirmar")
        await _assert_represented_academy_active(current)

        confirmed = await registration_writes.sql_confirm_registration(
            registration_id=registration_id, tenant_club_id=context.tenant_club_id
        )
        if confirmed is None:
            # Confirmacion simultanea: la otra transaccion gano la carrera.
            after = await get_registration(registration_id, tenant_club_id=context.tenant_club_id)
            if after is None:
                raise RegistrationNotFoundError("inscripcion no encontrada en este tenant")
            if after.status == "CONFIRMED":
                return after
            if after.status != "DRAFT":
                raise InvalidRegistrationStateError("solo un borrador se puede confirmar")
            # Sigue siendo un borrador del tenant: lo unico que puede haber impedido la
            # sentencia es que la academia representada dejara de estar activa.
            await _assert_represented_academy_active(after)
            raise InvalidRegistrationStateError(
                "no se pudo confirmar el borrador: vuelve a intentarlo"
            )

        await sql_insert_domain_change_log(
            entity=_REGISTRATION_ENTITY,
            entity_id=confirmed.id,
            action="CONFIRM",
            changed_fields=["status"],
            actor_user_id=context.actor_user_id,
            actor_label=context.actor_label,
            reason=normalized_reason,
        )
    return confirmed
