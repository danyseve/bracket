"""Afiliacion del competidor con un club (S4.8a-3 §3, LAB ONLY).

Opera sobre la tabla ya existente ``competitors_x_sports_clubs`` (modelo
:class:`CompetitorXSportsClub`): no se crea esquema nuevo de afiliacion.

Cuatro operaciones internas, **sin HTTP todavia**:

* :func:`assign_affiliation` — abre una afiliacion vigente con un club del tenant;
* :func:`promote_affiliation` — cambia la afiliacion primaria (cierra la anterior y abre esta);
* :func:`close_affiliation` — cierra una afiliacion poniendo ``valid_to``;
* :func:`go_independent` — cierra **todas** las afiliaciones vigentes (el competidor pasa a
  independiente).

Reglas del contrato:

* Como maximo **una** primaria **abierta** por competidor: lo garantiza, ademas de la logica, el
  indice unico parcial ``uq_competitors_x_sports_clubs_primary``. ``promote_affiliation`` cierra la
  primaria vigente antes de promover la nueva, **en la misma transaccion**.
* Cerrar es **poner ``valid_to``**: nunca se reescribe ni se borra el pasado. Una afiliacion ya
  cerrada no se vuelve a cerrar (idempotencia por contenido, sin nueva auditoria).
* El club destino tiene que **existir, estar activo y ser del tenant** del contexto: se valida
  antes de escribir. Un club de otro tenant o inactivo es :class:`InvalidCompetitorDataError`.
* Las inscripciones historicas (``tournament_registrations.affiliation_id`` y
  ``sports_club_name_snapshot``) **no se tocan**: son instantaneas congeladas y este modulo no
  escribe en ``tournament_registrations``.
* Cada escritura confirmada deja **un** evento en ``domain_change_log`` (entidad ``competitor``,
  accion ``UPDATE``, codigo cerrado ``AFFILIATION_CHANGE``) con nombres de campo, jamas valores.

AUTORIZACION: las cuatro operaciones son **sensibles** y exigen relacion ``OWNER`` con el tenant
(:class:`InsufficientPrivilegesError`); la ruta lo traduce a 403 y la dependencia de sesion deja
fuera al anonimo con 401. El tenant sale **siempre** del :class:`ActorContext`, nunca del payload.
"""

from __future__ import annotations

from bracket.database import database
from bracket.logic.audit_reasons import SCOPE_COMPETITOR, build_audit_reason
from bracket.logic.competitors import (
    CompetitorNotFoundError,
    InsufficientPrivilegesError,
    InvalidCompetitorDataError,
    TenantNotAuthorizedError,
)
from bracket.models.db.domain import ActorContext, CompetitorXSportsClub, DomainChangeLogAction
from bracket.models.db.user_x_club import UserXClubRelation
from bracket.sql.domain_reads import (
    get_affiliation,
    get_competitor,
    get_competitor_affiliations,
    get_sports_club,
)
from bracket.sql.domain_writes import (
    sql_close_affiliation,
    sql_close_all_open_affiliations,
    sql_close_open_primary_affiliation,
    sql_insert_affiliation,
    sql_insert_domain_change_log,
    sql_set_primary_affiliation,
)
from bracket.sql.users import get_user_relation_to_club
from bracket.utils.id_types import CompetitorId, CompetitorXSportsClubId, SportsClubId

_ENTITY = "competitor"
_ACTION: DomainChangeLogAction = "UPDATE"
_REASON_CODE = "AFFILIATION_CHANGE"
_AFFILIATION_NOT_FOUND = "la afiliacion no existe para este competidor"
_CLUB_NOT_SELECTABLE = "el club no existe, no esta activa o no es del tenant"


async def _authorize(context: ActorContext) -> None:
    """Exige relacion ``OWNER`` con el tenant, sin filtrar la existencia del tenant."""
    relation = await get_user_relation_to_club(context.tenant_club_id, context.actor_user_id)
    if relation is None:
        raise TenantNotAuthorizedError("el actor no tiene acceso a este tenant")
    if relation is not UserXClubRelation.OWNER:
        raise InsufficientPrivilegesError("esta operacion exige relacion OWNER con el tenant")


async def _require_competitor(context: ActorContext, competitor_id: CompetitorId) -> None:
    if await get_competitor(competitor_id, tenant_club_id=context.tenant_club_id) is None:
        raise CompetitorNotFoundError("competidor no encontrado")


async def _validate_club(context: ActorContext, sports_club_id: SportsClubId) -> None:
    """El club tiene que existir, estar activa y ser del tenant del contexto."""
    club = await get_sports_club(sports_club_id, tenant_club_id=context.tenant_club_id)
    if club is None or not club.active:
        raise InvalidCompetitorDataError(_CLUB_NOT_SELECTABLE)


async def _audit(
    context: ActorContext, *, competitor_id: CompetitorId, changed_fields: list[str]
) -> None:
    reason = build_audit_reason(scope=SCOPE_COMPETITOR, action=_ACTION, code=_REASON_CODE)
    await sql_insert_domain_change_log(
        entity=_ENTITY,
        entity_id=competitor_id,
        action=_ACTION,
        changed_fields=changed_fields,
        actor_user_id=context.actor_user_id,
        actor_label=context.actor_label,
        reason=reason.text,
        reason_code=reason.code,
        reason_note=reason.note,
        tenant_club_id=context.tenant_club_id,
    )


async def assign_affiliation(
    context: ActorContext, competitor_id: CompetitorId, *, sports_club_id: SportsClubId
) -> CompetitorXSportsClub:
    """Abre una afiliacion vigente con el club indicada.

    La primera afiliacion vigente del competidor queda **primaria**; las siguientes nacen no
    primarias. El club se valida (existe, activo, del tenant) y el competidor tiene que ser del
    tenant. Todo en una transaccion; la auditoria es ``AFFILIATION_CHANGE``.
    """
    async with database.transaction():
        await _authorize(context)
        await _require_competitor(context, competitor_id)
        await _validate_club(context, sports_club_id)
        open_affiliations = await get_competitor_affiliations(
            competitor_id, tenant_club_id=context.tenant_club_id
        )
        is_primary = not open_affiliations
        affiliation = await sql_insert_affiliation(
            competitor_id=competitor_id,
            tenant_club_id=context.tenant_club_id,
            sports_club_id=sports_club_id,
            is_primary=is_primary,
        )
        changed_fields = ["sports_club_id"] + (["is_primary"] if is_primary else [])
        await _audit(context, competitor_id=competitor_id, changed_fields=changed_fields)
    return affiliation


async def promote_affiliation(
    context: ActorContext, competitor_id: CompetitorId, affiliation_id: CompetitorXSportsClubId
) -> CompetitorXSportsClub:
    """Convierte en primaria una afiliacion vigente del competidor.

    Cierra la primaria vigente (si la hay) antes de promover esta, en la misma transaccion: nunca
    hay dos primarias abiertas. Promover la que ya es primaria no cambia nada (sin auditoria).
    """
    async with database.transaction():
        await _authorize(context)
        current = await get_affiliation(affiliation_id, tenant_club_id=context.tenant_club_id)
        if current is None or current.competitor_id != competitor_id:
            raise CompetitorNotFoundError(_AFFILIATION_NOT_FOUND)
        if current.valid_to is not None:
            raise InvalidCompetitorDataError("la afiliacion no esta vigente")
        if current.is_primary:
            return current
        await sql_close_open_primary_affiliation(
            competitor_id=competitor_id,
            tenant_club_id=context.tenant_club_id,
            except_id=affiliation_id,
        )
        updated = await sql_set_primary_affiliation(
            affiliation_id=affiliation_id,
            competitor_id=competitor_id,
            tenant_club_id=context.tenant_club_id,
        )
        if updated is None:
            raise CompetitorNotFoundError(_AFFILIATION_NOT_FOUND)
        await _audit(context, competitor_id=competitor_id, changed_fields=["is_primary"])
    return updated


async def close_affiliation(
    context: ActorContext, competitor_id: CompetitorId, affiliation_id: CompetitorXSportsClubId
) -> CompetitorXSportsClub:
    """Cierra una afiliacion vigente escribiendo ``valid_to`` (nunca reescribe el pasado).

    Cerrar una afiliacion ya cerrada no cambia nada (sin auditoria) y devuelve la afiliacion tal
    cual: la operacion es idempotente por contenido.
    """
    async with database.transaction():
        await _authorize(context)
        current = await get_affiliation(affiliation_id, tenant_club_id=context.tenant_club_id)
        if current is None or current.competitor_id != competitor_id:
            raise CompetitorNotFoundError(_AFFILIATION_NOT_FOUND)
        if current.valid_to is not None:
            return current
        closed = await sql_close_affiliation(
            affiliation_id=affiliation_id,
            competitor_id=competitor_id,
            tenant_club_id=context.tenant_club_id,
        )
        if closed is None:
            raise CompetitorNotFoundError(_AFFILIATION_NOT_FOUND)
        await _audit(context, competitor_id=competitor_id, changed_fields=["valid_to"])
    return closed


async def go_independent(
    context: ActorContext, competitor_id: CompetitorId
) -> list[CompetitorXSportsClub]:
    """Cierra **todas** las afiliaciones vigentes del competidor (pasa a independiente).

    Devuelve el historial completo de afiliaciones del competidor (cerradas incluidas). Sin
    afiliaciones vigentes no cambia nada (sin auditoria).
    """
    async with database.transaction():
        await _authorize(context)
        await _require_competitor(context, competitor_id)
        closed = await sql_close_all_open_affiliations(
            competitor_id=competitor_id, tenant_club_id=context.tenant_club_id
        )
        if closed:
            await _audit(context, competitor_id=competitor_id, changed_fields=["valid_to"])
    return await get_competitor_affiliations(
        competitor_id, tenant_club_id=context.tenant_club_id, include_history=True
    )
