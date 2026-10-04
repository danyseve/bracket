"""Sentencias de escritura y guardas del nucleo de inscripciones (S3.1 de F3B).

Solo SQL: la validacion de negocio, la autorizacion del contexto, la transaccion y
la auditoria viven en ``bracket/logic/registrations.py``. Aqui no hay ni una decision
de permisos ni un ``SELECT`` que sirva de candado contra duplicados.

Tres reglas heredadas de S2:

* **Ninguna funcion abre transaccion.** La inscripcion y su evento de auditoria se
  confirman o se revierten juntos, y eso lo decide quien orquesta.
* **El tenant se aplica en el SQL.** Las mutaciones llevan el ``tenant_club_id`` del
  contexto en el ``WHERE`` (por el ``club_id`` del torneo); no se filtra en Python
  despues de leer de mas.
* **Contra duplicados manda el indice.** Los indices unicos parciales de F3A
  (``uq_tournament_registrations_current_category`` y
  ``uq_tournament_registrations_current_uncategorized``) son la unica proteccion
  real. ``DRAFT``, ``CONFIRMED`` y ``WITHDRAWN`` ocupan el mismo hueco; solo las
  filas ``CORRECTED`` quedan fuera del indice.

Las guardas de lectura de este modulo (torneo del tenant, academia representable,
etiqueta ya congelada para una ``category_key``) existen para que la capa de dominio
pueda dar un error de negocio en vez de un fallo de clave foranea.

Proyeccion de retorno: la misma que la capa de lectura de S1, declarada aqui de forma
explicita para no acoplar la escritura a un simbolo privado de ``domain_reads``.
"""

from __future__ import annotations

from heliclockter import datetime_utc

from bracket.database import database
from bracket.models.db.domain import (
    Competitor,
    RegistrationIdentityStatus,
    RegistrationRepresentation,
    SportsClub,
    TournamentRegistration,
)
from bracket.utils.id_types import (
    ClubId,
    CompetitorId,
    CompetitorXSportsClubId,
    SportsClubId,
    TournamentId,
    TournamentRegistrationId,
    UserId,
)
from bracket.utils.types import assert_some

_REGISTRATION_COLUMNS = """
        id, tournament_id, competitor_id, identity_status, representation,
        sports_club_id, affiliation_id, category_key, category_label,
        competitor_name_snapshot, sports_club_name_snapshot, status, revision,
        corrects_registration_id, superseded_by_registration_id, created, updated_at
"""

# Misma proyeccion con el alias de la tabla: las sentencias de UPDATE devuelven la
# fila actualizada desde un ``UPDATE ... RETURNING``.
_REGISTRATION_COLUMNS_ALIASED = """
        tr.id, tr.tournament_id, tr.competitor_id, tr.identity_status, tr.representation,
        tr.sports_club_id, tr.affiliation_id, tr.category_key, tr.category_label,
        tr.competitor_name_snapshot, tr.sports_club_name_snapshot, tr.status, tr.revision,
        tr.corrects_registration_id, tr.superseded_by_registration_id, tr.created, tr.updated_at
"""

_SPORTS_CLUB_COLUMNS = "id, name, active, tenant_club_id, created, updated_at"

# Misma proyeccion que la lectura de S1 para un competidor, con alias de tabla.
_COMPETITOR_COLUMNS = (
    "c.id, c.display_name, c.active, c.managed_by_club_id, c.created, c.updated_at"
)

# Alcance del tenant, dentro de la propia sentencia. El torneo es el unico punto por
# el que una inscripcion puede pertenecer a un tenant.
_REGISTRATION_IN_TENANT = (
    "EXISTS (SELECT 1 FROM tournaments t "
    "WHERE t.id = tr.tournament_id AND t.club_id = :tenant_club_id)"
)


async def sql_insert_registration(
    *,
    tournament_id: TournamentId,
    competitor_id: CompetitorId | None,
    identity_status: RegistrationIdentityStatus,
    representation: RegistrationRepresentation,
    sports_club_id: SportsClubId | None,
    affiliation_id: CompetitorXSportsClubId | None,
    category_key: str | None,
    category_label: str | None,
    competitor_name_snapshot: str,
    sports_club_name_snapshot: str | None,
    verified_by_user_id: UserId | None,
    verified_at: datetime_utc | None,
) -> TournamentRegistration:
    """Alta de una inscripcion en borrador (``DRAFT``, ``revision = 1``).

    El estado, la revision y los enlaces de correccion no se aceptan desde fuera: el
    alta no puede fabricar una revision historica ni una fila ya confirmada.
    """
    query = f"""
        INSERT INTO tournament_registrations (
            tournament_id, competitor_id, identity_status, representation,
            sports_club_id, affiliation_id, category_key, category_label,
            competitor_name_snapshot, sports_club_name_snapshot,
            status, revision, verified_by_user_id, verified_at, created
        ) VALUES (
            :tournament_id, :competitor_id,
            CAST(:identity_status AS registration_identity_status),
            CAST(:representation AS registration_representation),
            :sports_club_id, :affiliation_id, :category_key, :category_label,
            :competitor_name_snapshot, :sports_club_name_snapshot,
            CAST('DRAFT' AS registration_status), 1, :verified_by_user_id, :verified_at, NOW()
        )
        RETURNING {_REGISTRATION_COLUMNS}
        """
    result = await database.fetch_one(
        query=query,
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
            "verified_by_user_id": verified_by_user_id,
            "verified_at": verified_at,
        },
    )
    return TournamentRegistration.model_validate(dict(assert_some(result)._mapping))


async def sql_update_registration_draft(
    *,
    registration_id: TournamentRegistrationId,
    tenant_club_id: ClubId,
    competitor_id: CompetitorId | None,
    identity_status: RegistrationIdentityStatus,
    representation: RegistrationRepresentation,
    sports_club_id: SportsClubId | None,
    affiliation_id: CompetitorXSportsClubId | None,
    category_key: str | None,
    category_label: str | None,
    competitor_name_snapshot: str,
    sports_club_name_snapshot: str | None,
    verified_by_user_id: UserId | None,
    verified_at: datetime_utc | None,
) -> TournamentRegistration | None:
    """Edita el borrador del tenant. ``None`` si no existe, es de otro tenant o ya no es ``DRAFT``.

    El ``status = 'DRAFT'`` va en el ``WHERE`` a proposito: entre la lectura previa y
    esta sentencia puede colarse una confirmacion.
    """
    query = f"""
        UPDATE tournament_registrations tr
        SET competitor_id = :competitor_id,
            identity_status = CAST(:identity_status AS registration_identity_status),
            representation = CAST(:representation AS registration_representation),
            sports_club_id = :sports_club_id,
            affiliation_id = :affiliation_id,
            category_key = :category_key,
            category_label = :category_label,
            competitor_name_snapshot = :competitor_name_snapshot,
            sports_club_name_snapshot = :sports_club_name_snapshot,
            verified_by_user_id = :verified_by_user_id,
            verified_at = :verified_at,
            updated_at = NOW()
        WHERE tr.id = :registration_id
            AND tr.status = CAST('DRAFT' AS registration_status)
            AND {_REGISTRATION_IN_TENANT}
        RETURNING {_REGISTRATION_COLUMNS_ALIASED}
        """
    result = await database.fetch_one(
        query=query,
        values={
            "registration_id": registration_id,
            "tenant_club_id": tenant_club_id,
            "competitor_id": competitor_id,
            "identity_status": identity_status,
            "representation": representation,
            "sports_club_id": sports_club_id,
            "affiliation_id": affiliation_id,
            "category_key": category_key,
            "category_label": category_label,
            "competitor_name_snapshot": competitor_name_snapshot,
            "sports_club_name_snapshot": sports_club_name_snapshot,
            "verified_by_user_id": verified_by_user_id,
            "verified_at": verified_at,
        },
    )
    return None if result is None else TournamentRegistration.model_validate(dict(result._mapping))


async def sql_confirm_registration(
    *, registration_id: TournamentRegistrationId, tenant_club_id: ClubId
) -> TournamentRegistration | None:
    """``DRAFT -> CONFIRMED``. ``None`` si ya no es un borrador del tenant.

    La transicion se decide en la propia sentencia (``status = 'DRAFT'`` en el
    ``WHERE``), no en un ``SELECT`` previo: una confirmacion repetida o simultanea
    afecta a cero filas en vez de a dos. No toca ningun snapshot.

    La segunda condicion cierra la carrera de la regla A5: si la academia representada
    se desactiva entre la lectura del dominio y esta sentencia, la confirmacion no
    ocurre. Un independiente (``sports_club_id IS NULL``) no depende de ninguna
    academia.

    La tercera hace lo mismo con la regla S3.1a: un competidor dado de baja entre el
    borrador y la confirmacion impide la transicion. Un alta sin identidad
    (``competitor_id IS NULL``) es un caso valido y no depende de ningun competidor.
    """
    query = f"""
        UPDATE tournament_registrations tr
        SET status = CAST('CONFIRMED' AS registration_status), updated_at = NOW()
        WHERE tr.id = :registration_id
            AND tr.status = CAST('DRAFT' AS registration_status)
            AND {_REGISTRATION_IN_TENANT}
            AND (
                tr.sports_club_id IS NULL
                OR EXISTS (
                    SELECT 1 FROM sports_clubs sc
                    WHERE sc.id = tr.sports_club_id AND sc.active IS TRUE
                )
            )
            AND (
                tr.competitor_id IS NULL
                OR EXISTS (
                    SELECT 1 FROM competitors c
                    WHERE c.id = tr.competitor_id AND c.active IS TRUE
                )
            )
        RETURNING {_REGISTRATION_COLUMNS_ALIASED}
        """
    result = await database.fetch_one(
        query=query,
        values={"registration_id": registration_id, "tenant_club_id": tenant_club_id},
    )
    return None if result is None else TournamentRegistration.model_validate(dict(result._mapping))


async def sql_tournament_is_in_tenant(
    *, tournament_id: TournamentId, tenant_club_id: ClubId
) -> bool:
    """``True`` solo si el torneo existe **y** es del tenant del contexto."""
    query = """
        SELECT EXISTS (
            SELECT 1 FROM tournaments
            WHERE id = :tournament_id AND club_id = :tenant_club_id
        )
        """
    return bool(
        await database.fetch_val(
            query=query, values={"tournament_id": tournament_id, "tenant_club_id": tenant_club_id}
        )
    )


async def sql_selectable_sports_club(sports_club_id: SportsClubId) -> SportsClub | None:
    """Academia representable: existe y esta activa.

    **No se filtra por tenant a proposito**: una inscripcion puede representar a una
    academia de otro tenant o de plataforma (``docs/21`` v3 §6.2/§12.4), y de la
    academia solo se lee el nombre para el snapshot. ``None`` unifica "no existe" y
    "no esta activa": el error de dominio no revela cual de las dos cosas pasa.
    """
    query = f"""
        SELECT {_SPORTS_CLUB_COLUMNS}
        FROM sports_clubs
        WHERE id = :sports_club_id AND active IS TRUE
        """
    result = await database.fetch_one(query=query, values={"sports_club_id": sports_club_id})
    return None if result is None else SportsClub.model_validate(dict(result._mapping))


async def sql_selectable_competitor(
    *, competitor_id: CompetitorId, tenant_club_id: ClubId, for_share: bool = False
) -> Competitor | None:
    """Competidor elegible como identidad: del tenant **y** activo (S3.1a).

    A diferencia de la academia representada (que puede ser de otro tenant), la identidad
    personal solo puede salir del tenant del contexto, y la baja logica la deshabilita para
    altas nuevas. Pertenencia y estado se comprueban en la propia sentencia, no con un
    ``SELECT`` previo. ``None`` unifica "no existe", "es de otro club" y "esta dado de baja":
    el error de dominio no revela cual de las tres cosas pasa.

    ``for_share`` añade ``FOR SHARE`` (RS-9): es el bloqueo que toma la confirmacion para que
    la elegibilidad quede serializada con la baja logica **dentro de su transaccion**. La baja
    (``UPDATE competitors SET active = false``) toma ``FOR NO KEY UPDATE``, que si conflictua
    con ``FOR SHARE``; cuando el ``FOR SHARE`` espera a que la baja comitee, PostgreSQL
    reevalua la condicion sobre la version actualizada y la fila deja de aparecer (cero filas,
    sin ventana). ``FOR KEY SHARE`` no serviria: es compatible con ``FOR NO KEY UPDATE``. El
    alta y la edicion de borrador no bloquean: solo comprueban.
    """
    lock = " FOR SHARE" if for_share else ""
    query = f"""
        SELECT {_COMPETITOR_COLUMNS}
        FROM competitors c
        WHERE c.id = :competitor_id
            AND c.managed_by_club_id = :tenant_club_id
            AND c.active IS TRUE{lock}
        """
    result = await database.fetch_one(
        query=query, values={"competitor_id": competitor_id, "tenant_club_id": tenant_club_id}
    )
    return None if result is None else Competitor.model_validate(dict(result._mapping))


async def sql_category_labels_for_key(
    *, tournament_id: TournamentId, category_key: str
) -> list[str]:
    """Etiquetas ya usadas en el torneo para esa ``category_key`` (incluye ``CORRECTED``).

    Sirve para congelar la etiqueta: la misma clave no puede significar dos cosas
    distintas dentro del mismo torneo. Se miran todas las filas, no solo las vigentes.
    """
    query = """
        SELECT DISTINCT category_label
        FROM tournament_registrations
        WHERE tournament_id = :tournament_id
            AND category_key = :category_key
            AND category_label IS NOT NULL
        """
    records = await database.fetch_all(
        query=query, values={"tournament_id": tournament_id, "category_key": category_key}
    )
    return [str(record["category_label"]) for record in records]
