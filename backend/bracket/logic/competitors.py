"""Logica de dominio de competidores (S2 de F3B): identidad administrada por tenant.

Cuatro operaciones internas, **sin HTTP todavia**:

* :func:`create_competitor` — alta de identidad (puede existir sin academia);
* :func:`update_competitor_display_name` — cambio controlado de datos basicos;
* :func:`deactivate_competitor` — baja logica (``competitors.active = false``);
* :func:`activate_competitor` — reactivacion de una identidad dada de baja
  (``competitors.active = true``). La baja logica **no** es irreversible.

Reglas del contrato (docs/21 v3, S2):

* El tenant sale **siempre** del :class:`ActorContext`: ninguna funcion acepta
  ``managed_by_club_id`` desde fuera.
* El actor debe tener acceso al tenant (``users_x_clubs``). Si no lo tiene se
  levanta :class:`TenantNotAuthorizedError` sin distinguir entre "tenant
  inexistente" y "tenant no autorizado": asi no se filtra su existencia.
* Politica de permisos (S2): OWNER y COLLABORATOR pueden dar de alta y editar datos
  basicos; la **baja logica** y su **reactivacion** exigen OWNER
  (:class:`InsufficientPrivilegesError`). No se modifica ningun permiso *legacy*: la
  politica vive solo en esta capa.
* Sin deduplicacion automatica: dos altas con el mismo nombre son dos identidades.
* Cada escritura confirmada deja un evento en ``domain_change_log`` con entidad,
  accion, actor, motivo y **nombres de campo** (jamas valores: sin PII en auditoria).
* Competidor, historial de nombre y evento de auditoria se confirman o se revierten
  juntos: una unica transaccion por operacion.
* Sin logs: ni los errores ni el modulo registran nombres ni identificadores.
  ``reason`` es texto libre del llamador y no debe llevar datos personales.
* Idempotencia: si una operacion no cambia nada (mismo nombre, baja ya aplicada,
  reactivacion ya aplicada) no se escribe historial ni auditoria.
* Baja y reactivacion escriben la **misma** columna (``active``) sobre la **misma**
  fila filtrada por tenant: toman por tanto el mismo bloqueo de fila y compiten en el
  mismo orden (competidor -> inscripcion) con el ``FOR SHARE`` de la confirmacion de
  la inscripcion (RS-9). Ninguna de las dos toca ``tournament_registrations``,
  snapshots ni inscripciones historicas: no introducen un bloqueo nuevo ni un ciclo.

LIMITACIONES DE AUTORIZACION PENDIENTES (documentadas, no resueltas aqui):

1. El contexto lo construye el llamador: mientras no exista la capa HTTP, no hay
   sesion que impida a un llamador interno inventarse un ``tenant_club_id``. El
   endpoint tendra que derivar el contexto de la sesion autenticada y **nunca** del
   payload, y no debe reutilizar ``user_authenticated_or_public_dashboard``.
2. ``domain_change_log`` (esquema F3A) no tiene columna de tenant: el tenant de la
   operacion queda implicito en la entidad auditada. Registrar el tenant explicito
   exigiria una migracion aditiva que debe evaluarse antes del despliegue.
3. La autorizacion se apoya en la relacion ``users_x_clubs`` ya existente; no hay
   todavia roles de dominio ni auditoria de intentos denegados.
"""

from __future__ import annotations

from bracket.database import database
from bracket.models.db.domain import ActorContext, Competitor, CompetitorBasicDataUpdate
from bracket.models.db.user_x_club import UserXClubRelation
from bracket.sql.domain_reads import get_competitor
from bracket.sql.domain_writes import (
    sql_activate_competitor,
    sql_close_open_competitor_name_history,
    sql_deactivate_competitor,
    sql_insert_competitor,
    sql_insert_competitor_name_history,
    sql_insert_domain_change_log,
    sql_update_competitor_display_name,
)
from bracket.sql.users import get_user_relation_to_club
from bracket.utils.id_types import CompetitorId

MAX_DISPLAY_NAME_LENGTH = 200
MAX_REASON_LENGTH = 500

_COMPETITOR_ENTITY = "competitor"

_DEFAULT_REASONS: dict[str, str] = {
    "CREATE": "alta de competidor",
    "UPDATE": "actualizacion de datos basicos",
    "DEACTIVATE": "baja logica de competidor",
    "ACTIVATE": "reactivacion de competidor",
}


class CompetitorDomainError(Exception):
    """Base de los errores de la capa interna. Los mensajes no llevan datos personales."""


class TenantNotAuthorizedError(CompetitorDomainError):
    """El actor no tiene acceso al tenant indicado (o el tenant no existe)."""


class CompetitorNotFoundError(CompetitorDomainError):
    """El competidor no existe o no pertenece al tenant del contexto."""


class InvalidCompetitorDataError(CompetitorDomainError):
    """Datos de entrada invalidos (nombre vacio, demasiado largo o con caracteres de control)."""


class InsufficientPrivilegesError(CompetitorDomainError):
    """El actor tiene acceso al tenant, pero su relacion no permite esta operacion.

    Politica S2: OWNER y COLLABORATOR pueden dar de alta y editar datos basicos;
    la baja logica y su reactivacion (operaciones historicamente sensibles) exigen
    OWNER.
    """


def _normalize_display_name(display_name: object) -> str:
    if not isinstance(display_name, str):
        raise InvalidCompetitorDataError("display_name debe ser texto")
    normalized = display_name.strip()
    if not normalized:
        raise InvalidCompetitorDataError("display_name no puede estar vacio")
    if len(normalized) > MAX_DISPLAY_NAME_LENGTH:
        raise InvalidCompetitorDataError("display_name supera la longitud maxima permitida")
    if not normalized.isprintable():
        raise InvalidCompetitorDataError("display_name contiene caracteres de control")
    return normalized


def _normalize_reason(reason: object, action: str) -> str:
    if reason is None:
        return _DEFAULT_REASONS[action]
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidCompetitorDataError("reason no puede estar vacio")
    normalized = reason.strip()
    if len(normalized) > MAX_REASON_LENGTH or not normalized.isprintable():
        raise InvalidCompetitorDataError("reason no es valido")
    return normalized


async def _authorize(context: ActorContext, *, require_owner: bool = False) -> None:
    """Comprueba el acceso del actor al tenant y, si procede, que sea OWNER.

    No distingue "tenant inexistente" de "tenant no autorizado": no se filtra la
    existencia del tenant. Tampoco distingue COLLABORATOR de "sin permiso" en el
    mensaje de `InsufficientPrivilegesError`.
    """
    relation = await get_user_relation_to_club(context.tenant_club_id, context.actor_user_id)
    if relation is None:
        raise TenantNotAuthorizedError("el actor no tiene acceso a este tenant")
    if require_owner and relation is not UserXClubRelation.OWNER:
        raise InsufficientPrivilegesError("esta operacion exige relacion OWNER con el tenant")


async def create_competitor(
    context: ActorContext, *, display_name: str, reason: str | None = None
) -> Competitor:
    """Alta de identidad. El competidor puede no tener academia y se audita como ``CREATE``."""
    normalized_name = _normalize_display_name(display_name)
    normalized_reason = _normalize_reason(reason, "CREATE")

    async with database.transaction():
        await _authorize(context)
        competitor = await sql_insert_competitor(
            display_name=normalized_name, managed_by_club_id=context.tenant_club_id
        )
        await sql_insert_competitor_name_history(
            competitor_id=competitor.id,
            display_name=normalized_name,
            changed_by_user_id=context.actor_user_id,
        )
        await sql_insert_domain_change_log(
            entity=_COMPETITOR_ENTITY,
            entity_id=competitor.id,
            action="CREATE",
            changed_fields=["display_name"],
            actor_user_id=context.actor_user_id,
            actor_label=context.actor_label,
            reason=normalized_reason,
        )

    return competitor


async def update_competitor_display_name(
    context: ActorContext,
    competitor_id: CompetitorId,
    data: CompetitorBasicDataUpdate,
    *,
    reason: str | None = None,
) -> Competitor:
    """Renombrado controlado. Cierra la entrada de nombre vigente y abre la nueva.

    Si el nombre normalizado coincide con el vigente no cambia nada (sin historial ni
    auditoria) y devuelve la identidad tal cual.
    """
    normalized_name = _normalize_display_name(data.display_name)
    normalized_reason = _normalize_reason(reason, "UPDATE")

    async with database.transaction():
        await _authorize(context)
        current = await get_competitor(competitor_id, tenant_club_id=context.tenant_club_id)
        if current is None:
            raise CompetitorNotFoundError("competidor no encontrado")
        if current.display_name == normalized_name:
            return current

        updated = await sql_update_competitor_display_name(
            competitor_id=competitor_id,
            tenant_club_id=context.tenant_club_id,
            display_name=normalized_name,
        )
        if updated is None:
            raise CompetitorNotFoundError("competidor no encontrado")

        await sql_close_open_competitor_name_history(competitor_id=competitor_id)
        await sql_insert_competitor_name_history(
            competitor_id=competitor_id,
            display_name=normalized_name,
            changed_by_user_id=context.actor_user_id,
        )
        await sql_insert_domain_change_log(
            entity=_COMPETITOR_ENTITY,
            entity_id=competitor_id,
            action="UPDATE",
            changed_fields=["display_name"],
            actor_user_id=context.actor_user_id,
            actor_label=context.actor_label,
            reason=normalized_reason,
        )

    return updated


async def deactivate_competitor(
    context: ActorContext, competitor_id: CompetitorId, *, reason: str | None = None
) -> Competitor:
    """Baja logica (``active = false``). Idempotente: una segunda baja no audita de nuevo.

    Operacion historicamente sensible: exige relacion OWNER con el tenant.
    """
    normalized_reason = _normalize_reason(reason, "DEACTIVATE")

    async with database.transaction():
        await _authorize(context, require_owner=True)
        current = await get_competitor(competitor_id, tenant_club_id=context.tenant_club_id)
        if current is None:
            raise CompetitorNotFoundError("competidor no encontrado")
        if not current.active:
            return current

        deactivated = await sql_deactivate_competitor(
            competitor_id=competitor_id, tenant_club_id=context.tenant_club_id
        )
        if deactivated is None:
            raise CompetitorNotFoundError("competidor no encontrado")

        await sql_insert_domain_change_log(
            entity=_COMPETITOR_ENTITY,
            entity_id=competitor_id,
            action="DEACTIVATE",
            changed_fields=["active"],
            actor_user_id=context.actor_user_id,
            actor_label=context.actor_label,
            reason=normalized_reason,
        )

    return deactivated


async def activate_competitor(
    context: ActorContext, competitor_id: CompetitorId, *, reason: str | None = None
) -> Competitor:
    """Reactivacion (``active = true``). Idempotente: si ya estaba activo no audita de nuevo.

    Operacion simetrica de la baja logica y del mismo calibre: exige relacion OWNER.

    Concurrencia (RS-9): la sentencia solo cambia la columna ``active`` de la fila del
    competidor del tenant, asi que toma el mismo bloqueo de fila que la baja y compite en
    el mismo orden con el ``FOR SHARE`` de la confirmacion. El estado se lee dentro de la
    transaccion, pero la proteccion no es esa lectura previa: la sentencia vuelve a filtrar
    por ``active IS FALSE``. Si otra reactivacion gano la carrera, la sentencia no escribe
    nada y la operacion se limita a devolver el estado ya reactivado, sin duplicar auditoria.
    """
    normalized_reason = _normalize_reason(reason, "ACTIVATE")

    async with database.transaction():
        await _authorize(context, require_owner=True)
        current = await get_competitor(competitor_id, tenant_club_id=context.tenant_club_id)
        if current is None:
            raise CompetitorNotFoundError("competidor no encontrado")
        if current.active:
            return current

        activated = await sql_activate_competitor(
            competitor_id=competitor_id, tenant_club_id=context.tenant_club_id
        )
        if activated is None:
            # Carrera: otra reactivacion aplico el cambio entre la lectura y la sentencia.
            # Se relee sin escribir auditoria; si ya esta activa, el resultado efectivo es
            # el mismo y no se duplica el evento.
            after = await get_competitor(competitor_id, tenant_club_id=context.tenant_club_id)
            if after is None or not after.active:
                raise CompetitorNotFoundError("competidor no encontrado")
            return after

        await sql_insert_domain_change_log(
            entity=_COMPETITOR_ENTITY,
            entity_id=competitor_id,
            action="ACTIVATE",
            changed_fields=["active"],
            actor_user_id=context.actor_user_id,
            actor_label=context.actor_label,
            reason=normalized_reason,
        )

    return activated
