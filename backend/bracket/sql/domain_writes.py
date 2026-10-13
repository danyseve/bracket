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

from datetime import date

from bracket.database import database
from bracket.models.db.domain import (
    Competitor,
    CompetitorSex,
    CompetitorXSportsClub,
    DomainChangeLogAction,
    DomainWriteScopeError,
    SportsClub,
)
from bracket.utils.id_types import (
    BeltId,
    ClubId,
    CompetitorId,
    CompetitorXSportsClubId,
    SportsClubId,
    UserId,
)
from bracket.utils.types import assert_some

_COMPETITOR_COLUMNS = (
    "id, display_name, active, managed_by_club_id, created, updated_at, date_of_birth, sex, belt_id"
)


async def sql_insert_competitor(
    *,
    display_name: str,
    managed_by_club_id: ClubId,
    active: bool = True,
    date_of_birth: date | None = None,
    sex: CompetitorSex | None = None,
    belt_id: BeltId | None = None,
) -> Competitor:
    """Crea la identidad. ``managed_by_club_id`` sale del contexto autorizado, no del payload.

    El perfil deportivo (S4.8a-3 §2) entra por parametro ya validado por la capa de dominio: la
    fecha de nacimiento, el sexo y el cinturon son datos de la identidad, no del contexto.
    """
    query = f"""
        INSERT INTO competitors
            (display_name, managed_by_club_id, active, created, date_of_birth, sex, belt_id)
        VALUES (:display_name, :managed_by_club_id, :active, NOW(), :date_of_birth, :sex, :belt_id)
        RETURNING {_COMPETITOR_COLUMNS}
        """
    result = await database.fetch_one(
        query=query,
        values={
            "display_name": display_name,
            "managed_by_club_id": managed_by_club_id,
            "active": active,
            "date_of_birth": date_of_birth,
            "sex": sex,
            "belt_id": belt_id,
        },
    )
    return Competitor.model_validate(dict(assert_some(result)._mapping))


async def sql_update_competitor_profile(
    *,
    competitor_id: CompetitorId,
    tenant_club_id: ClubId,
    date_of_birth: date | None,
    sex: CompetitorSex | None,
    belt_id: BeltId | None,
) -> Competitor | None:
    """Reemplaza el perfil deportivo de un competidor del tenant.

    ``None`` si no existe o no es de ese tenant. Escribe **los tres** campos: el estado deseado
    es completo y ``NULL`` es un valor, no una omision.
    """
    query = f"""
        UPDATE competitors
        SET date_of_birth = :date_of_birth, sex = :sex, belt_id = :belt_id, updated_at = NOW()
        WHERE id = :competitor_id AND managed_by_club_id = :tenant_club_id
        RETURNING {_COMPETITOR_COLUMNS}
        """
    result = await database.fetch_one(
        query=query,
        values={
            "competitor_id": competitor_id,
            "tenant_club_id": tenant_club_id,
            "date_of_birth": date_of_birth,
            "sex": sex,
            "belt_id": belt_id,
        },
    )
    return Competitor.model_validate(dict(result._mapping)) if result is not None else None


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

    S3.7b: el cierre usa ``clock_timestamp()`` y no ``NOW()``. ``NOW()`` es el inicio de la
    transaccion: un renombrado que espera el lock de la fila del competidor puede haber
    arrancado antes de que la transaccion previa insertase su ``valid_from`` y cerraria con un
    ``valid_to`` anterior, chocando con ``ck_competitors_name_history_range``. El suelo de un
    microsegundo sobre el ``valid_from`` de la propia fila cierra la ultima rendija (que dos
    renombrados cayeran en el mismo instante) y deja la restriccion satisfecha por
    construccion.
    """
    query = """
        WITH closed AS (
            UPDATE competitors_name_history
            SET valid_to = GREATEST(
                clock_timestamp(), valid_from + interval '1 microsecond'
            )
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

    S3.7b: ``valid_from`` se toma con ``clock_timestamp()`` por el mismo motivo que el cierre:
    garantiza que la entrada nueva abre despues del cierre de la anterior aunque la transaccion
    haya arrancado antes, sin solapar rangos.
    """
    query = """
        WITH inserted AS (
            INSERT INTO competitors_name_history
                (competitor_id, display_name, valid_from, valid_to, changed_by_user_id)
            SELECT :competitor_id, :display_name, clock_timestamp(), NULL, :changed_by_user_id
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
    "tournament": (
        "SELECT 1 FROM tournaments "
        "WHERE tournaments.id = :entity_id "
        "AND tournaments.club_id = :tenant_club_id"
    ),
    "stage_item": (
        "SELECT 1 FROM stage_items "
        "JOIN stages ON stages.id = stage_items.stage_id "
        "JOIN tournaments ON tournaments.id = stages.tournament_id "
        "WHERE stage_items.id = :entity_id "
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
    vuelve a cerrar la base con sus CHECK. El escritor acepta ``reason_code=None`` para los unicos
    casos legitimos: entidades de plataforma sin catalogo propio (``sports_club``), las filas
    anteriores a la migracion y los eventos de **ventana del torneo** de S3.4c
    (``SET_REGISTRATION_WINDOW`` y ``CLOSE_REGISTRATIONS``), que no son transiciones de una
    inscripcion y por eso no llevan codigo del catalogo; las seis operaciones del ciclo de vida
    (S3.1/S3.2) si lo llevan.

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


# --- S4.8a-3 §3: afiliaciones del competidor ---------------------------------------------------

_AFFILIATION_COLUMNS = (
    "id, competitor_id, sports_club_id, valid_from, valid_to, is_primary, created"
)


async def sql_insert_affiliation(
    *,
    competitor_id: CompetitorId,
    tenant_club_id: ClubId,
    sports_club_id: SportsClubId,
    is_primary: bool,
) -> CompetitorXSportsClub:
    """Abre una afiliacion vigente del competidor con la academia.

    El competidor tiene que pertenecer al tenant (lo cierra el propio SQL); si no, se levanta
    :class:`DomainWriteScopeError` y la operacion revierte entera. ``valid_from`` usa
    ``clock_timestamp()``, no ``NOW()``: la afiliacion abre en el instante real de la escritura.
    """
    query = f"""
        INSERT INTO competitors_x_sports_clubs
            (competitor_id, sports_club_id, valid_from, valid_to, is_primary, created)
        SELECT :competitor_id, :sports_club_id, clock_timestamp(), NULL, :is_primary, NOW()
        WHERE EXISTS (
            SELECT 1 FROM competitors
            WHERE competitors.id = :competitor_id
              AND competitors.managed_by_club_id = :tenant_club_id
        )
        RETURNING {_AFFILIATION_COLUMNS}
        """
    result = await database.fetch_one(
        query=query,
        values={
            "competitor_id": competitor_id,
            "tenant_club_id": tenant_club_id,
            "sports_club_id": sports_club_id,
            "is_primary": is_primary,
        },
    )
    if result is None:
        raise DomainWriteScopeError("la afiliacion no corresponde al tenant de su competidor")
    return CompetitorXSportsClub.model_validate(dict(result._mapping))


async def sql_close_open_primary_affiliation(
    *, competitor_id: CompetitorId, tenant_club_id: ClubId, except_id: int | None
) -> int:
    """Cierra la afiliacion primaria vigente (``valid_to IS NULL AND is_primary``).

    ``except_id`` permite excluir la fila que se va a promover en el mismo paso, de modo que el
    indice unico parcial ``uq_competitors_x_sports_clubs_primary`` nunca vea dos primarias abiertas.
    Devuelve cuantas cerro; el cierre solo alcanza al competidor de ese tenant.
    """
    query = """
        WITH closed AS (
            UPDATE competitors_x_sports_clubs
            SET valid_to = GREATEST(clock_timestamp(), valid_from + interval '1 microsecond')
            WHERE competitor_id = :competitor_id
              AND valid_to IS NULL
              AND is_primary
              AND (
                  CAST(:except_id AS bigint) IS NULL
                  OR id <> CAST(:except_id AS bigint)
              )
              AND EXISTS (
                  SELECT 1 FROM competitors
                  WHERE competitors.id = competitors_x_sports_clubs.competitor_id
                    AND competitors.managed_by_club_id = :tenant_club_id
              )
            RETURNING 1
        )
        SELECT count(*) FROM closed
        """
    return int(
        await database.execute(
            query=query,
            values={
                "competitor_id": competitor_id,
                "tenant_club_id": tenant_club_id,
                "except_id": except_id,
            },
        )
        or 0
    )


async def sql_set_primary_affiliation(
    *,
    affiliation_id: CompetitorXSportsClubId,
    competitor_id: CompetitorId,
    tenant_club_id: ClubId,
) -> CompetitorXSportsClub | None:
    """Marca como primaria una afiliacion vigente del competidor del tenant.

    ``None`` si la afiliacion no existe, no es de ese competidor o ya esta cerrada.
    """
    query = f"""
        UPDATE competitors_x_sports_clubs
        SET is_primary = true
        WHERE id = :affiliation_id
          AND competitor_id = :competitor_id
          AND valid_to IS NULL
          AND EXISTS (
              SELECT 1 FROM competitors
              WHERE competitors.id = competitors_x_sports_clubs.competitor_id
                AND competitors.managed_by_club_id = :tenant_club_id
          )
        RETURNING {_AFFILIATION_COLUMNS}
        """
    result = await database.fetch_one(
        query=query,
        values={
            "affiliation_id": affiliation_id,
            "competitor_id": competitor_id,
            "tenant_club_id": tenant_club_id,
        },
    )
    return (
        CompetitorXSportsClub.model_validate(dict(result._mapping)) if result is not None else None
    )


async def sql_close_affiliation(
    *,
    affiliation_id: CompetitorXSportsClubId,
    competitor_id: CompetitorId,
    tenant_club_id: ClubId,
) -> CompetitorXSportsClub | None:
    """Cierra una afiliacion vigente poniendo ``valid_to`` (nunca reescribe el pasado).

    ``None`` si la afiliacion no existe, no es de ese competidor o ya estaba cerrada.
    """
    query = f"""
        UPDATE competitors_x_sports_clubs
        SET valid_to = GREATEST(clock_timestamp(), valid_from + interval '1 microsecond')
        WHERE id = :affiliation_id
          AND competitor_id = :competitor_id
          AND valid_to IS NULL
          AND EXISTS (
              SELECT 1 FROM competitors
              WHERE competitors.id = competitors_x_sports_clubs.competitor_id
                AND competitors.managed_by_club_id = :tenant_club_id
          )
        RETURNING {_AFFILIATION_COLUMNS}
        """
    result = await database.fetch_one(
        query=query,
        values={
            "affiliation_id": affiliation_id,
            "competitor_id": competitor_id,
            "tenant_club_id": tenant_club_id,
        },
    )
    return (
        CompetitorXSportsClub.model_validate(dict(result._mapping)) if result is not None else None
    )


async def sql_close_all_open_affiliations(
    *, competitor_id: CompetitorId, tenant_club_id: ClubId
) -> int:
    """Cierra todas las afiliaciones vigentes del competidor (pasar a independiente).

    Devuelve cuantas cerro. Solo alcanza al competidor de ese tenant.
    """
    query = """
        WITH closed AS (
            UPDATE competitors_x_sports_clubs
            SET valid_to = GREATEST(clock_timestamp(), valid_from + interval '1 microsecond')
            WHERE competitor_id = :competitor_id
              AND valid_to IS NULL
              AND EXISTS (
                  SELECT 1 FROM competitors
                  WHERE competitors.id = competitors_x_sports_clubs.competitor_id
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


# --- S4.8a-3 §4: logo de la academia -----------------------------------------------------------

_SPORTS_CLUB_WRITE_COLUMNS = "id, name, active, tenant_club_id, created, updated_at, logo_path"


async def sql_update_sports_club_logo(
    *,
    sports_club_id: SportsClubId,
    tenant_club_id: ClubId,
    logo_path: str | None,
) -> SportsClub | None:
    """Fija o limpia la ruta relativa del logo de una academia del tenant.

    ``None`` si la academia no existe o no pertenece a ese tenant. ``logo_path=None`` borra la
    referencia (quitar el logo); el fichero en disco lo retira la capa de rutas despues.
    """
    query = f"""
        UPDATE sports_clubs
        SET logo_path = :logo_path, updated_at = NOW()
        WHERE id = :sports_club_id AND tenant_club_id = :tenant_club_id
        RETURNING {_SPORTS_CLUB_WRITE_COLUMNS}
        """
    result = await database.fetch_one(
        query=query,
        values={
            "sports_club_id": sports_club_id,
            "tenant_club_id": tenant_club_id,
            "logo_path": logo_path,
        },
    )
    return SportsClub.model_validate(dict(result._mapping)) if result is not None else None
