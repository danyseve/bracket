"""Sentencias de escritura del dominio Competitor (S2 de F3B).

Solo SQL: la validacion, la autorizacion del contexto, la transaccion y la
auditoria viven en la capa de dominio (``bracket/logic/competitors.py``).

**Ninguna de estas funciones abre su propia transaccion.** El caso de uso debe
poder confirmar o revertir juntas la fila del competidor, su historial de nombres
y el evento de auditoria (requisito S2 §4): por eso la transaccion la abre quien
orquesta, y aqui se ejecutan sentencias sueltas.

Proyeccion de retorno: los mismos campos que la capa de lectura de S1, declarados
aqui de forma explicita para no acoplar la escritura a un simbolo privado de
``domain_reads``.
"""

from __future__ import annotations

from bracket.database import database
from bracket.models.db.domain import Competitor, DomainChangeLogAction, DomainWriteScopeError
from bracket.utils.id_types import ClubId, CompetitorId, UserId
from bracket.utils.types import assert_some

_COMPETITOR_COLUMNS = "id, display_name, active, managed_by_club_id, created, updated_at"


async def sql_insert_competitor(
    *, display_name: str, managed_by_club_id: ClubId, active: bool = True
) -> Competitor:
    """Crea la identidad. ``managed_by_club_id`` sale del contexto autorizado, no del payload."""
    query = f"""
        INSERT INTO competitors (display_name, managed_by_club_id, active, created)
        VALUES (:display_name, :managed_by_club_id, :active, NOW())
        RETURNING {_COMPETITOR_COLUMNS}
        """
    result = await database.fetch_one(
        query=query,
        values={
            "display_name": display_name,
            "managed_by_club_id": managed_by_club_id,
            "active": active,
        },
    )
    return Competitor.model_validate(dict(assert_some(result)._mapping))


async def sql_update_competitor_display_name(
    *, competitor_id: CompetitorId, tenant_club_id: ClubId, display_name: str
) -> Competitor | None:
    """Renombra un competidor del tenant. ``None`` si no existe o no es de ese tenant."""
    query = f"""
        UPDATE competitors
        SET display_name = :display_name, updated_at = NOW()
        WHERE id = :competitor_id AND managed_by_club_id = :tenant_club_id
        RETURNING {_COMPETITOR_COLUMNS}
        """
    result = await database.fetch_one(
        query=query,
        values={
            "competitor_id": competitor_id,
            "tenant_club_id": tenant_club_id,
            "display_name": display_name,
        },
    )
    return Competitor.model_validate(dict(result._mapping)) if result is not None else None


async def sql_deactivate_competitor(
    *, competitor_id: CompetitorId, tenant_club_id: ClubId
) -> Competitor | None:
    """Baja logica del tenant. ``None`` si no existe, no es de ese tenant o ya estaba inactivo."""
    query = f"""
        UPDATE competitors
        SET active = false, updated_at = NOW()
        WHERE id = :competitor_id
        AND managed_by_club_id = :tenant_club_id
        AND active IS TRUE
        RETURNING {_COMPETITOR_COLUMNS}
        """
    result = await database.fetch_one(
        query=query,
        values={"competitor_id": competitor_id, "tenant_club_id": tenant_club_id},
    )
    return Competitor.model_validate(dict(result._mapping)) if result is not None else None


async def sql_activate_competitor(
    *, competitor_id: CompetitorId, tenant_club_id: ClubId
) -> Competitor | None:
    """Reactivacion logica del tenant. ``None`` si no existe, no es de ese tenant o ya activo.

    Simetrica de :func:`sql_deactivate_competitor`: mismo filtro de tenant, mismo cambio (la
    columna ``active``) y mismo ``RETURNING``. Al escribir la misma columna, las dos operaciones
    de estado toman el mismo bloqueo de fila (``FOR NO KEY UPDATE``) y por tanto compiten igual
    con el ``FOR SHARE`` de la confirmacion (RS-9), sin anadir un orden de bloqueo nuevo.
    ``competitors.active`` es ``NOT NULL`` (esquema F3A), asi que ``IS FALSE`` es exacto.
    """
    query = f"""
        UPDATE competitors
        SET active = true, updated_at = NOW()
        WHERE id = :competitor_id
        AND managed_by_club_id = :tenant_club_id
        AND active IS FALSE
        RETURNING {_COMPETITOR_COLUMNS}
        """
    result = await database.fetch_one(
        query=query,
        values={"competitor_id": competitor_id, "tenant_club_id": tenant_club_id},
    )
    return Competitor.model_validate(dict(result._mapping)) if result is not None else None


async def sql_close_open_competitor_name_history(
    *, competitor_id: CompetitorId, tenant_club_id: ClubId
) -> int:
    """Cierra la entrada de nombre vigente (``valid_to IS NULL``). Devuelve cuantas cerro.

    S3.3a: el cierre solo alcanza al competidor de ese tenant, asi que un llamador futuro que
    use el helper suelto no puede tocar el historial de otro tenant.
    """
    query = """
        WITH closed AS (
            UPDATE competitors_name_history
            SET valid_to = NOW()
            WHERE competitor_id = :competitor_id
              AND valid_to IS NULL
              AND EXISTS (
                  SELECT 1 FROM competitors
                  WHERE competitors.id = competitors_name_history.competitor_id
                    AND competitors.managed_by_club_id = :tenant_club_id
              )
            RETURNING 1
        )
        SELECT count(*) FROM closed
        """
    return int(
        await database.execute(
            query=query,
            values={"competitor_id": competitor_id, "tenant_club_id": tenant_club_id},
        )
        or 0
    )


async def sql_insert_competitor_name_history(
    *,
    competitor_id: CompetitorId,
    tenant_club_id: ClubId,
    display_name: str,
    changed_by_user_id: UserId,
) -> None:
    """Abre una entrada de nombre. ``changed_by_user_id`` es el actor, no se proyecta en lectura.

    S3.3a: solo abre historial para el competidor de ese tenant. Si el competidor no pertenece
    al tenant no se escribe nada y se levanta :class:`DomainWriteScopeError` (la operacion
    revierte entera).
    """
    query = """
        WITH inserted AS (
            INSERT INTO competitors_name_history
                (competitor_id, display_name, valid_from, valid_to, changed_by_user_id)
            SELECT :competitor_id, :display_name, NOW(), NULL, :changed_by_user_id
            WHERE EXISTS (
                SELECT 1 FROM competitors
                WHERE competitors.id = :competitor_id
                  AND competitors.managed_by_club_id = :tenant_club_id
            )
            RETURNING 1
        )
        SELECT count(*) FROM inserted
        """
    inserted = int(
        await database.execute(
            query=query,
            values={
                "competitor_id": competitor_id,
                "tenant_club_id": tenant_club_id,
                "display_name": display_name,
                "changed_by_user_id": changed_by_user_id,
            },
        )
        or 0
    )
    if inserted != 1:
        raise DomainWriteScopeError("el historial no corresponde al tenant de su competidor")


# Coherencia entidad <-> tenant, expresada en la propia sentencia de auditoria (S3.3a).
# Una referencia valida a ``clubs(id)`` no demuestra que el evento corresponda a su entidad:
# esto si. El nombre de entidad es un valor cerrado del dominio, no texto libre.
_ENTITY_TENANT_PREDICATE: dict[str, str] = {
    "competitor": (
        "SELECT 1 FROM competitors "
        "WHERE competitors.id = :entity_id "
        "AND competitors.managed_by_club_id = :tenant_club_id"
    ),
    "sports_club": (
        "SELECT 1 FROM sports_clubs "
        "WHERE sports_clubs.id = :entity_id "
        "AND sports_clubs.tenant_club_id = :tenant_club_id"
    ),
    "tournament_registration": (
        "SELECT 1 FROM tournament_registrations "
        "JOIN tournaments ON tournaments.id = tournament_registrations.tournament_id "
        "WHERE tournament_registrations.id = :entity_id "
        "AND tournaments.club_id = :tenant_club_id"
    ),
}


async def sql_insert_domain_change_log(
    *,
    entity: str,
    entity_id: int,
    action: DomainChangeLogAction,
    changed_fields: list[str],
    actor_user_id: UserId,
    actor_label: str | None,
    reason: str,
    reason_code: str | None,
    reason_note: str | None,
    tenant_club_id: ClubId,
) -> None:
    """Evento de auditoria **atribuido al tenant de su entidad**.

    ``reason`` es el texto canonico legible del evento y ``reason_code``/``reason_note`` su motivo
    estructurado (S3.3c-2): el codigo pertenece a un catalogo cerrado y la nota es opcional y solo
    en los codigos que la admiten; ambos los valida el catalogo **antes** de llegar aqui y los
    vuelve a cerrar la base con sus CHECK. El escritor acepta ``reason_code=None`` para el unico
    caso legitimo: entidades de plataforma sin catalogo propio (``sports_club``) y filas anteriores
    a la migracion; ninguna de las diez operaciones de auditoria de F3 usa esa forma.

    ``changed_fields`` guarda NOMBRES de campo, nunca valores (sin PII). El evento se escribe
    solo si la entidad pertenece de verdad a ``tenant_club_id``; si no, no se escribe nada y se
    levanta :class:`DomainWriteScopeError`, de modo que no queda una operacion sin evento ni un
    evento sin operacion. Entidades de plataforma (tenant NULL) no tienen escritor autorizado
    todavia: ninguna operacion de F3 audita una entidad sin tenant.
    """
    predicate = _ENTITY_TENANT_PREDICATE.get(entity)
    if predicate is None:
        raise DomainWriteScopeError("entidad de auditoria no soportada")

    query = f"""
        WITH inserted AS (
            INSERT INTO domain_change_log
                (entity, entity_id, action, changed_fields, actor_user_id, actor_label, reason,
                 reason_code, reason_note, tenant_club_id, created)
            SELECT :entity, :entity_id, :action, :changed_fields, :actor_user_id, :actor_label,
                   :reason, :reason_code, :reason_note, :tenant_club_id, NOW()
            WHERE EXISTS ({predicate})
            RETURNING 1
        )
        SELECT count(*) FROM inserted
        """
    inserted = int(
        await database.execute(
            query=query,
            values={
                "entity": entity,
                "entity_id": entity_id,
                "action": action,
                "changed_fields": changed_fields,
                "actor_user_id": actor_user_id,
                "actor_label": actor_label,
                "reason": reason,
                "reason_code": reason_code,
                "reason_note": reason_note,
                "tenant_club_id": tenant_club_id,
            },
        )
        or 0
    )
    if inserted != 1:
        raise DomainWriteScopeError("el evento no corresponde al tenant de su entidad")
