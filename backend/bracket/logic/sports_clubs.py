"""Club deportiva (``sports_clubs``): logo, alta y edicion (S4.8a-3 §4 y S4.8g-2 §14, LAB ONLY).

En producto esta entidad se llama **Club** (S4.8g-2 §1/§2); ``sports_clubs`` es solo su nombre
tecnico historico. La organizacion/tenant (``clubs``) es otra cosa: aqui solo se gestiona el club
deportivo del competidor.

El club (``sports_clubs``) puede llevar un logo opcional. En la base solo se guarda la
**ruta relativa** (``sports_clubs.logo_path``, columna aditiva nullable); el fichero vive en disco
y lo escribe la capa de rutas. Este modulo solo persiste la referencia y la audita; nunca toca el
disco.

Reglas:

* El tenant sale **siempre** del :class:`ActorContext`: el club tiene que pertenecer al tenant.
* La operacion es **sensible**: exige relacion ``OWNER`` con el tenant
  (:class:`InsufficientPrivilegesError`); la ruta deja fuera al anonimo con 401.
* Si la ruta resultante es identica a la vigente no cambia nada (sin auditoria): idempotencia por
  contenido.
* Cada cambio confirmado deja un evento en ``domain_change_log`` con entidad ``sports_club``,
  accion ``UPDATE`` y codigo cerrado ``CLUB_LOGO_CHANGE``, anotando el **nombre** de campo
  ``logo_path`` (jamas la ruta ni el contenido).
* La validacion del fichero (tipo real, extension, tamano) y el borrado del logo anterior tras
  persistir viven en la capa de rutas.

S4.8g-2 §7/§8/§14 anade al mismo modulo el ciclo de vida del club: **alta**, **renombrado** y
**cambio de estado**, sin borrado fisico (desactivar es el flujo normal). El nombre se guarda
normalizado y su historial se abre/cierra en ``sports_clubs_name_history`` (mecanismo que ya existia
en el esquema, igual que el del competidor). **No hay ``UNIQUE(name)`` global** (§8): dos clubes
distintos pueden llamarse igual en organizaciones distintas —y "Gracie Barra Madrid" y "Gracie Barra
Oviedo" son clubes distintos— asi que la identidad real es ``sports_club_id``; lo unico que se
comprueba es un duplicado **obvio dentro del mismo tenant**, con una consulta acotada al tenant, sin
constraint nueva (no hay migracion en esta fase).
"""

from __future__ import annotations

import re
import unicodedata

from bracket.database import database
from bracket.logic.audit_reasons import SCOPE_SPORTS_CLUB, build_audit_reason
from bracket.models.db.domain import ActorContext, DomainChangeLogAction, SportsClub
from bracket.models.db.user_x_club import UserXClubRelation
from bracket.sql.domain_reads import (
    find_sports_club_by_normalized_name,
    get_sports_club,
)
from bracket.sql.domain_writes import (
    sql_close_open_sports_club_name_history,
    sql_insert_domain_change_log,
    sql_insert_sports_club,
    sql_insert_sports_club_name_history,
    sql_update_sports_club_logo,
    sql_update_sports_club_profile,
)
from bracket.sql.users import get_user_relation_to_club
from bracket.utils.id_types import SportsClubId

_ENTITY = "sports_club"
_ACTION: DomainChangeLogAction = "UPDATE"
_CREATE: DomainChangeLogAction = "CREATE"

#: Codigos cerrados por operacion (catalogo de S3.3c-2, alcance ``sports_club``).
_CODE_CREATE = "CLUB_CREATE"
_CODE_RENAME = "CLUB_RENAME"
_CODE_ACTIVE = "CLUB_ACTIVE_CHANGE"

#: Longitud maxima del nombre visible del club. El nombre es una etiqueta de producto, no texto
#: libre: se normaliza (NFC, sin espacios repetidos ni extremos) antes de medir.
MAX_CLUB_NAME_LENGTH = 120

_WHITESPACE = re.compile(r"\s+")


class SportsClubDomainError(Exception):
    """Base de los errores del club. Los mensajes no llevan datos personales."""


class SportsClubNotFoundError(SportsClubDomainError):
    """El club no existe o no pertenece al tenant del contexto."""


class TenantNotAuthorizedError(SportsClubDomainError):
    """El actor no tiene acceso al tenant (o el tenant no existe)."""


class InsufficientPrivilegesError(SportsClubDomainError):
    """El actor tiene acceso al tenant, pero no es OWNER."""


class SportsClubValidationError(SportsClubDomainError):
    """La peticion no describe un cambio aplicable (nombre invalido o cuerpo sin cambios)."""


class DuplicateSportsClubNameError(SportsClubDomainError):
    """Ya hay un club con ese nombre (normalizado) en el mismo tenant."""


def _normalize_club_name(name: str) -> str:
    """Normaliza el nombre visible: NFC, sin espacios repetidos ni extremos, acotado.

    No se aplica ninguna regla de negocio sobre el contenido (no se prohiben abreviaturas ni
    mayusculas): solo se descarta lo que no puede ser un nombre legible.
    """
    normalized = _WHITESPACE.sub(" ", unicodedata.normalize("NFC", name)).strip()
    if not normalized:
        raise SportsClubValidationError("el nombre del club no puede estar vacio")
    if len(normalized) > MAX_CLUB_NAME_LENGTH:
        raise SportsClubValidationError("el nombre del club es demasiado largo")
    if any(character.isprintable() is False for character in normalized):
        raise SportsClubValidationError("el nombre del club contiene caracteres no imprimibles")
    return normalized


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
    """Fija (o limpia con ``logo_path=None``) el logo del club del tenant.

    Devuelve ``(club_actualizada, logo_path_anterior)``. El segundo valor es la ruta del logo
    vigente antes del cambio (``None`` si no habia): la ruta lo usa para **borrar el fichero
    anterior solo despues** de que el nuevo este validado y persistido. Si la ruta no cambia, no se
    escribe auditoria y se devuelve ``(club, None)``.
    """
    async with database.transaction():
        await _authorize(context)
        current = await get_sports_club(sports_club_id, tenant_club_id=context.tenant_club_id)
        if current is None:
            raise SportsClubNotFoundError("club no encontrada")
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
            raise SportsClubNotFoundError("club no encontrada")
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


async def create_sports_club(
    context: ActorContext,
    *,
    name: str,
    active: bool = True,
    reason_code: str | None = None,
    reason_note: str | None = None,
) -> SportsClub:
    """Da de alta un club del tenant (OWNER). Audita ``CREATE`` con ``CLUB_CREATE``.

    El club nace con su historial de nombre abierto, igual que el competidor: el renombrado cierra
    esa entrada y abre otra, de modo que el nombre vigente siempre tiene una fila. No hay logo: se
    sube despues por su propia ruta.
    """
    normalized_name = _normalize_club_name(name)
    reason = build_audit_reason(
        scope=SCOPE_SPORTS_CLUB, action=_CREATE, code=reason_code or _CODE_CREATE, note=reason_note
    )

    async with database.transaction():
        await _authorize(context)
        duplicate = await find_sports_club_by_normalized_name(
            tenant_club_id=context.tenant_club_id, name=normalized_name
        )
        if duplicate is not None:
            raise DuplicateSportsClubNameError(
                "ya existe un club con ese nombre en esta organizacion"
            )
        sports_club = await sql_insert_sports_club(
            name=normalized_name, tenant_club_id=context.tenant_club_id, active=active
        )
        await sql_insert_sports_club_name_history(
            sports_club_id=sports_club.id,
            tenant_club_id=context.tenant_club_id,
            name=normalized_name,
            changed_by_user_id=context.actor_user_id,
        )
        await sql_insert_domain_change_log(
            entity=_ENTITY,
            entity_id=sports_club.id,
            action=_CREATE,
            changed_fields=["name", "active"],
            actor_user_id=context.actor_user_id,
            actor_label=context.actor_label,
            reason=reason.text,
            reason_code=reason.code,
            reason_note=reason.note,
            tenant_club_id=context.tenant_club_id,
        )
    return sports_club


async def update_sports_club(
    context: ActorContext,
    sports_club_id: SportsClubId,
    *,
    name: str | None = None,
    active: bool | None = None,
    reason_code: str | None = None,
    reason_note: str | None = None,
) -> SportsClub:
    """Renombra y/o cambia el estado de un club del tenant (OWNER). Sin borrado fisico.

    Solo se aplica lo que viaja. Si el valor enviado coincide con el vigente no hay escritura ni
    auditoria (idempotencia por contenido); si no viaja ningun campo, o ninguno de los enviados
    cambia nada, es una peticion invalida y no un no-op silencioso. El renombrado cierra la entrada
    abierta del historial y abre la nueva **antes** de confirmar, igual que el competidor.

    Cuando el cuerpo cambia nombre y estado a la vez se emite **un** evento, con los dos campos
    anotados, y el codigo del cambio de nombre (``CLUB_RENAME``): es el cambio identificativo.
    """
    if name is None and active is None:
        raise SportsClubValidationError("no hay ningun campo que actualizar")
    normalized_name = _normalize_club_name(name) if name is not None else None

    async with database.transaction():
        await _authorize(context)
        current = await get_sports_club(sports_club_id, tenant_club_id=context.tenant_club_id)
        if current is None:
            raise SportsClubNotFoundError("club no encontrada")

        changed_fields = []
        if normalized_name is not None and normalized_name != current.name:
            changed_fields.append("name")
        if active is not None and active != current.active:
            changed_fields.append("active")
        if not changed_fields:
            return current

        reason = build_audit_reason(
            scope=SCOPE_SPORTS_CLUB,
            action=_ACTION,
            code=reason_code or (_CODE_RENAME if "name" in changed_fields else _CODE_ACTIVE),
            note=reason_note,
        )

        if "name" in changed_fields and normalized_name is not None:
            duplicate = await find_sports_club_by_normalized_name(
                tenant_club_id=context.tenant_club_id,
                name=normalized_name,
                exclude_sports_club_id=sports_club_id,
            )
            if duplicate is not None:
                raise DuplicateSportsClubNameError(
                    "ya existe un club con ese nombre en esta organizacion"
                )

        updated = await sql_update_sports_club_profile(
            sports_club_id=sports_club_id,
            tenant_club_id=context.tenant_club_id,
            name=normalized_name,
            active=active,
        )
        if updated is None:
            raise SportsClubNotFoundError("club no encontrada")

        if "name" in changed_fields and normalized_name is not None:
            await sql_close_open_sports_club_name_history(
                sports_club_id=sports_club_id, tenant_club_id=context.tenant_club_id
            )
            await sql_insert_sports_club_name_history(
                sports_club_id=sports_club_id,
                tenant_club_id=context.tenant_club_id,
                name=normalized_name,
                changed_by_user_id=context.actor_user_id,
            )

        await sql_insert_domain_change_log(
            entity=_ENTITY,
            entity_id=sports_club_id,
            action=_ACTION,
            changed_fields=changed_fields,
            actor_user_id=context.actor_user_id,
            actor_label=context.actor_label,
            reason=reason.text,
            reason_code=reason.code,
            reason_note=reason.note,
            tenant_club_id=context.tenant_club_id,
        )
    return updated
