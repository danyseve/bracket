"""Logo de la academia deportiva (S4.8a-3 §4, LAB ONLY).

La academia (``sports_clubs``) puede llevar un logo opcional. En la base solo se guarda la
**ruta relativa** (``sports_clubs.logo_path``, columna aditiva nullable); el fichero vive en disco
y lo escribe la capa de rutas. Este modulo solo persiste la referencia y la audita; nunca toca el
disco.

Reglas:

* El tenant sale **siempre** del :class:`ActorContext`: la academia tiene que pertenecer al tenant.
* La operacion es **sensible**: exige relacion ``OWNER`` con el tenant
  (:class:`InsufficientPrivilegesError`); la ruta deja fuera al anonimo con 401.
* Si la ruta resultante es identica a la vigente no cambia nada (sin auditoria): idempotencia por
  contenido.
* Cada cambio confirmado deja un evento en ``domain_change_log`` con entidad ``sports_club``,
  accion ``UPDATE`` y codigo cerrado ``CLUB_LOGO_CHANGE``, anotando el **nombre** de campo
  ``logo_path`` (jamas la ruta ni el contenido).
* La validacion del fichero (tipo real, extension, tamano) y el borrado del logo anterior tras
  persistir viven en la capa de rutas.
"""

from __future__ import annotations

from bracket.database import database
from bracket.logic.audit_reasons import SCOPE_SPORTS_CLUB, build_audit_reason
from bracket.models.db.domain import ActorContext, DomainChangeLogAction, SportsClub
from bracket.models.db.user_x_club import UserXClubRelation
from bracket.sql.domain_reads import get_sports_club
from bracket.sql.domain_writes import sql_insert_domain_change_log, sql_update_sports_club_logo
from bracket.sql.users import get_user_relation_to_club
from bracket.utils.id_types import SportsClubId

_ENTITY = "sports_club"
_ACTION: DomainChangeLogAction = "UPDATE"


class SportsClubDomainError(Exception):
    """Base de los errores de la academia. Los mensajes no llevan datos personales."""


class SportsClubNotFoundError(SportsClubDomainError):
    """La academia no existe o no pertenece al tenant del contexto."""


class TenantNotAuthorizedError(SportsClubDomainError):
    """El actor no tiene acceso al tenant (o el tenant no existe)."""


class InsufficientPrivilegesError(SportsClubDomainError):
    """El actor tiene acceso al tenant, pero no es OWNER."""


async def _authorize(context: ActorContext) -> None:
    relation = await get_user_relation_to_club(context.tenant_club_id, context.actor_user_id)
    if relation is None:
        raise TenantNotAuthorizedError("el actor no tiene acceso a este tenant")
    if relation is not UserXClubRelation.OWNER:
        raise InsufficientPrivilegesError("esta operacion exige relacion OWNER con el tenant")


async def set_sports_club_logo(
    context: ActorContext,
    sports_club_id: SportsClubId,
    *,
    logo_path: str | None,
    reason_code: str,
    reason_note: str | None = None,
) -> tuple[SportsClub, str | None]:
    """Fija (o limpia con ``logo_path=None``) el logo de la academia del tenant.

    Devuelve ``(academia_actualizada, logo_path_anterior)``. El segundo valor es la ruta del logo
    vigente antes del cambio (``None`` si no habia): la ruta lo usa para **borrar el fichero
    anterior solo despues** de que el nuevo este validado y persistido. Si la ruta no cambia, no se
    escribe auditoria y se devuelve ``(academia, None)``.
    """
    async with database.transaction():
        await _authorize(context)
        current = await get_sports_club(sports_club_id, tenant_club_id=context.tenant_club_id)
        if current is None:
            raise SportsClubNotFoundError("academia no encontrada")
        previous = current.logo_path
        if previous == logo_path:
            return current, None
        reason = build_audit_reason(
            scope=SCOPE_SPORTS_CLUB, action=_ACTION, code=reason_code, note=reason_note
        )
        updated = await sql_update_sports_club_logo(
            sports_club_id=sports_club_id,
            tenant_club_id=context.tenant_club_id,
            logo_path=logo_path,
        )
        if updated is None:
            raise SportsClubNotFoundError("academia no encontrada")
        await sql_insert_domain_change_log(
            entity=_ENTITY,
            entity_id=sports_club_id,
            action=_ACTION,
            changed_fields=["logo_path"],
            actor_user_id=context.actor_user_id,
            actor_label=context.actor_label,
            reason=reason.text,
            reason_code=reason.code,
            reason_note=reason.note,
            tenant_club_id=context.tenant_club_id,
        )
    return updated, previous
