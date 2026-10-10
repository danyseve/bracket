"""Consultas de solo lectura del dominio Competitor/SportsClub/Registration (S1 de F3B).

Contrato (``docs/23`` §5, ``docs/24`` §S1):

* **Solo lectura.** En este modulo no hay ``INSERT``/``UPDATE``/``DELETE``.
  Crear competidores, inscribir, corregir, importar CSV o hacer backfill es S2+.
* **Aislamiento por tenant.** Toda funcion exige el ``tenant_club_id`` del
  llamante y lo aplica *dentro* de la consulta SQL (competidores por
  ``managed_by_club_id``, academias por ``tenant_club_id``, inscripciones por el
  ``club_id`` del torneo). No hay ninguna consulta sin ambito de tenant y no se
  filtra en Python despues de leer de mas.
* **Sin deduplicacion por nombre.** Las busquedas por nombre devuelven *todas*
  las filas que coinciden: un nombre no identifica a nadie. No existe una funcion
  que devuelva "el" competidor de un nombre ni ninguna fusion automatica. Dos
  homonimos del mismo tenant siguen siendo dos competidores.
* **Identidad desconocida permitida.** Una inscripcion con ``competitor_id`` NULL
  es un estado valido y permanente; se puede listar con
  :func:`get_registrations_without_identity`.
* **Revisiones corregidas.** Por defecto se excluyen las inscripciones ya
  sustituidas (``status = 'CORRECTED'`` o ``superseded_by_registration_id`` no
  NULL). Con ``include_superseded=True`` se incluyen, y
  :func:`get_registration_revision_chain` devuelve la cadena completa.
* **Snapshots e historico.** Las inscripciones devuelven el nombre del competidor
  y de la academia tal como estaban al inscribirse; los historiales de nombre se
  consultan con :func:`get_competitor_name_history` y
  :func:`get_sports_club_name_history`.
* **Acceso minimo a PII.** No se seleccionan identificadores de usuario ni notas
  libres (ver ``bracket/models/db/domain.py``).

Estas funciones no autentican por si mismas: el llamante debe haber resuelto
antes el tenant (``user_owner_for_club`` y equivalentes). Ninguna de ellas usa
``user_authenticated_or_public_dashboard`` como control de acceso.
"""

from __future__ import annotations

from bracket.database import database
from bracket.models.db.domain import (
    Competitor,
    CompetitorNameHistory,
    CompetitorXSportsClub,
    SportsClub,
    SportsClubNameHistory,
    TournamentRegistration,
)
from bracket.utils.id_types import (
    ClubId,
    CompetitorId,
    SportsClubId,
    TournamentId,
    TournamentRegistrationId,
)

# Una inscripcion esta "vigente" si no ha sido sustituida por una correccion.
_CURRENT_REGISTRATION = "tr.status <> 'CORRECTED' AND tr.superseded_by_registration_id IS NULL"

_SPORTS_CLUB_COLUMNS = "s.id, s.name, s.active, s.tenant_club_id, s.created, s.updated_at"

_COMPETITOR_COLUMNS = (
    "c.id, c.display_name, c.active, c.managed_by_club_id, c.created, c.updated_at"
)

_REGISTRATION_COLUMNS = """
    tr.id, tr.tournament_id, tr.competitor_id, tr.identity_status, tr.representation,
    tr.sports_club_id, tr.affiliation_id, tr.category_key, tr.category_label,
    tr.competitor_name_snapshot, tr.sports_club_name_snapshot, tr.status, tr.revision,
    tr.corrects_registration_id, tr.superseded_by_registration_id, tr.created, tr.updated_at
"""

_REGISTRATION_TENANT_JOIN = """
    FROM tournament_registrations tr
    JOIN tournaments t ON t.id = tr.tournament_id
"""

# Cadena de revisiones. El vinculo documentado es `corrects_registration_id`
# (F3A dejo la coherencia con `superseded_by_registration_id` como invariante de
# aplicacion). `UNION` en vez de `UNION ALL` para deduplicar y no colgarse ante un
# ciclo. La tenencia se comprueba en cada paso del recorrido, no solo en el ancla.
_CHAIN_ANCESTORS = f"""
    WITH RECURSIVE chain AS (
        SELECT {_REGISTRATION_COLUMNS}
        {_REGISTRATION_TENANT_JOIN}
        WHERE tr.id = :registration_id AND t.club_id = :tenant_club_id
        UNION
        SELECT {_REGISTRATION_COLUMNS}
        {_REGISTRATION_TENANT_JOIN}
        JOIN chain c ON c.corrects_registration_id = tr.id
        WHERE t.club_id = :tenant_club_id
    )
    SELECT * FROM chain
"""

_CHAIN_DESCENDANTS = f"""
    WITH RECURSIVE chain AS (
        SELECT {_REGISTRATION_COLUMNS}
        {_REGISTRATION_TENANT_JOIN}
        WHERE tr.id = :registration_id AND t.club_id = :tenant_club_id
        UNION
        SELECT {_REGISTRATION_COLUMNS}
        {_REGISTRATION_TENANT_JOIN}
        JOIN chain c ON tr.corrects_registration_id = c.id
        WHERE t.club_id = :tenant_club_id
    )
    SELECT * FROM chain
"""

_MAX_CHAIN_HOPS = 20


def _escape_like(value: str) -> str:
    """Escapa comodines para que la busqueda sea literal (no patron del usuario)."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def get_sports_clubs_for_tenant(
    tenant_club_id: ClubId, *, include_inactive: bool = False
) -> list[SportsClub]:
    """Academias del tenant. No incluye las de plataforma (``tenant_club_id`` NULL)."""
    query = (
        f"SELECT {_SPORTS_CLUB_COLUMNS} FROM sports_clubs s"
        " WHERE s.tenant_club_id = :tenant_club_id"
    )
    if not include_inactive:
        query += " AND s.active IS TRUE"
    query += " ORDER BY s.name, s.id"

    records = await database.fetch_all(query=query, values={"tenant_club_id": tenant_club_id})
    return [SportsClub.model_validate(dict(record._mapping)) for record in records]


async def get_platform_sports_clubs(*, include_inactive: bool = False) -> list[SportsClub]:
    """Academias de plataforma (``tenant_club_id`` NULL): catalogo compartido de lectura."""
    query = f"SELECT {_SPORTS_CLUB_COLUMNS} FROM sports_clubs s WHERE s.tenant_club_id IS NULL"
    if not include_inactive:
        query += " AND s.active IS TRUE"
    query += " ORDER BY s.name, s.id"

    records = await database.fetch_all(query=query)
    return [SportsClub.model_validate(dict(record._mapping)) for record in records]


async def get_sports_club(
    sports_club_id: SportsClubId, *, tenant_club_id: ClubId
) -> SportsClub | None:
    """Academia del tenant por id. Devuelve ``None`` si es de otro tenant o no existe."""
    query = f"""
        SELECT {_SPORTS_CLUB_COLUMNS} FROM sports_clubs s
        WHERE s.id = :sports_club_id AND s.tenant_club_id = :tenant_club_id
    """
    record = await database.fetch_one(
        query=query, values={"sports_club_id": sports_club_id, "tenant_club_id": tenant_club_id}
    )
    return SportsClub.model_validate(dict(record._mapping)) if record is not None else None


async def get_sports_club_name_history(
    sports_club_id: SportsClubId, *, tenant_club_id: ClubId
) -> list[SportsClubNameHistory]:
    """Historial de nombres de la academia, de mas antiguo a mas reciente."""
    query = """
        SELECT h.id, h.sports_club_id, h.name, h.valid_from, h.valid_to
        FROM sports_clubs_name_history h
        JOIN sports_clubs s ON s.id = h.sports_club_id
        WHERE h.sports_club_id = :sports_club_id AND s.tenant_club_id = :tenant_club_id
        ORDER BY h.valid_from, h.id
    """
    records = await database.fetch_all(
        query=query, values={"sports_club_id": sports_club_id, "tenant_club_id": tenant_club_id}
    )
    return [SportsClubNameHistory.model_validate(dict(record._mapping)) for record in records]


async def get_competitors_for_tenant(
    tenant_club_id: ClubId,
    *,
    display_name: str | None = None,
    include_inactive: bool = False,
) -> list[Competitor]:
    """Competidores administrados por el tenant.

    Con ``display_name`` devuelve **todas** las coincidencias parciales: son
    candidatos, nunca una identidad. Deliberadamente no existe una variante que
    devuelva una sola fila por nombre.
    """
    query = f"""
        SELECT {_COMPETITOR_COLUMNS}
        FROM competitors c
        WHERE c.managed_by_club_id = :tenant_club_id
    """
    values: dict[str, str | ClubId] = {"tenant_club_id": tenant_club_id}
    if display_name is not None:
        query += " AND c.display_name ILIKE :display_name ESCAPE '\\'"
        values["display_name"] = f"%{_escape_like(display_name)}%"
    if not include_inactive:
        query += " AND c.active IS TRUE"
    query += " ORDER BY c.display_name, c.id"

    records = await database.fetch_all(query=query, values=values)
    return [Competitor.model_validate(dict(record._mapping)) for record in records]


async def get_competitor(
    competitor_id: CompetitorId, *, tenant_club_id: ClubId
) -> Competitor | None:
    """Competidor del tenant por id. Devuelve ``None`` si es de otro tenant o no existe."""
    query = f"""
        SELECT {_COMPETITOR_COLUMNS}
        FROM competitors c
        WHERE c.id = :competitor_id AND c.managed_by_club_id = :tenant_club_id
    """
    record = await database.fetch_one(
        query=query, values={"competitor_id": competitor_id, "tenant_club_id": tenant_club_id}
    )
    return Competitor.model_validate(dict(record._mapping)) if record is not None else None


async def get_competitor_name_history(
    competitor_id: CompetitorId, *, tenant_club_id: ClubId
) -> list[CompetitorNameHistory]:
    """Historial de nombres del competidor, de mas antiguo a mas reciente."""
    query = """
        SELECT h.id, h.competitor_id, h.display_name, h.valid_from, h.valid_to
        FROM competitors_name_history h
        JOIN competitors c ON c.id = h.competitor_id
        WHERE h.competitor_id = :competitor_id AND c.managed_by_club_id = :tenant_club_id
        ORDER BY h.valid_from, h.id
    """
    records = await database.fetch_all(
        query=query, values={"competitor_id": competitor_id, "tenant_club_id": tenant_club_id}
    )
    return [CompetitorNameHistory.model_validate(dict(record._mapping)) for record in records]


async def get_competitor_affiliations(
    competitor_id: CompetitorId, *, tenant_club_id: ClubId, include_history: bool = False
) -> list[CompetitorXSportsClub]:
    """Afiliaciones del competidor (solo vigentes salvo ``include_history=True``)."""
    query = """
        SELECT a.id, a.competitor_id, a.sports_club_id, a.valid_from, a.valid_to,
               a.is_primary, a.created
        FROM competitors_x_sports_clubs a
        JOIN competitors c ON c.id = a.competitor_id
        WHERE a.competitor_id = :competitor_id AND c.managed_by_club_id = :tenant_club_id
    """
    if not include_history:
        query += " AND a.valid_to IS NULL"
    query += " ORDER BY a.is_primary DESC, a.valid_from, a.id"

    records = await database.fetch_all(
        query=query, values={"competitor_id": competitor_id, "tenant_club_id": tenant_club_id}
    )
    return [CompetitorXSportsClub.model_validate(dict(record._mapping)) for record in records]


async def get_tournament_registrations(
    tournament_id: TournamentId, *, tenant_club_id: ClubId, include_superseded: bool = False
) -> list[TournamentRegistration]:
    """Inscripciones del torneo (el torneo debe ser del tenant).

    Una fila por inscripcion: varias categorias del mismo competidor son varias
    filas. Por defecto se excluyen las sustituidas por una correccion.
    """
    query = f"SELECT {_REGISTRATION_COLUMNS} {_REGISTRATION_TENANT_JOIN}"
    query += "WHERE tr.tournament_id = :tournament_id AND t.club_id = :tenant_club_id"
    if not include_superseded:
        query += f" AND {_CURRENT_REGISTRATION}"
    query += " ORDER BY tr.competitor_name_snapshot, tr.category_key NULLS FIRST, tr.id"

    records = await database.fetch_all(
        query=query, values={"tournament_id": tournament_id, "tenant_club_id": tenant_club_id}
    )
    return [TournamentRegistration.model_validate(dict(record._mapping)) for record in records]


def _registration_filters(
    tournament_id: TournamentId,
    tenant_club_id: ClubId,
    status: str | None,
    competitor_id: CompetitorId | None,
) -> tuple[str, dict[str, object]]:
    """Clausula ``WHERE`` compartida por el listado paginado y su recuento.

    Compartirla es lo que impide que el total y la pagina se desincronicen al anadir un filtro:
    los dos leen exactamente las mismas condiciones, y siempre acotadas al tenant del llamante.
    """
    where = "WHERE tr.tournament_id = :tournament_id AND t.club_id = :tenant_club_id"
    values: dict[str, object] = {"tournament_id": tournament_id, "tenant_club_id": tenant_club_id}
    if status is not None:
        where += " AND tr.status = :status"
        values["status"] = status
    if competitor_id is not None:
        where += " AND tr.competitor_id = :competitor_id"
        values["competitor_id"] = competitor_id
    return f"{where} AND {_CURRENT_REGISTRATION}", values


async def list_tournament_registrations(
    tournament_id: TournamentId,
    *,
    tenant_club_id: ClubId,
    status: str | None = None,
    competitor_id: CompetitorId | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[TournamentRegistration]:
    """Pagina del listado de inscripciones vigentes del torneo (el torneo debe ser del tenant).

    Orden total y estable: competidor, categoria e identificador, el mismo que usa
    :func:`get_tournament_registrations`. Al ser total, paginar con ``limit``/``offset`` no repite
    ni se salta filas. Sin ``limit`` devuelve todas las del filtro.
    """
    where, values = _registration_filters(tournament_id, tenant_club_id, status, competitor_id)
    query = f"SELECT {_REGISTRATION_COLUMNS} {_REGISTRATION_TENANT_JOIN} {where}"
    query += " ORDER BY tr.competitor_name_snapshot, tr.category_key NULLS FIRST, tr.id"
    if limit is not None:
        query += " LIMIT :limit"
        values["limit"] = limit
    if offset is not None:
        query += " OFFSET :offset"
        values["offset"] = offset

    records = await database.fetch_all(query=query, values=values)
    return [TournamentRegistration.model_validate(dict(record._mapping)) for record in records]


async def count_tournament_registrations(
    tournament_id: TournamentId,
    *,
    tenant_club_id: ClubId,
    status: str | None = None,
    competitor_id: CompetitorId | None = None,
) -> int:
    """Total de inscripciones vigentes que cumplen los filtros, sin paginar.

    Es el total del **filtro**, no el de la pagina: el cliente puede saber si quedan mas paginas
    sin pedir una de mas.
    """
    where, values = _registration_filters(tournament_id, tenant_club_id, status, competitor_id)
    count = await database.fetch_val(
        query=f"SELECT COUNT(*) AS count {_REGISTRATION_TENANT_JOIN} {where}", values=values
    )
    return int(count or 0)


async def get_registrations_for_competitor(
    competitor_id: CompetitorId,
    *,
    tournament_id: TournamentId,
    tenant_club_id: ClubId,
    include_superseded: bool = False,
) -> list[TournamentRegistration]:
    """Inscripciones (multicategoria) de un competidor en un torneo del tenant."""
    query = f"SELECT {_REGISTRATION_COLUMNS} {_REGISTRATION_TENANT_JOIN}"
    query += """
        WHERE tr.competitor_id = :competitor_id
        AND tr.tournament_id = :tournament_id AND t.club_id = :tenant_club_id
    """
    if not include_superseded:
        query += f" AND {_CURRENT_REGISTRATION}"
    query += " ORDER BY tr.category_key NULLS FIRST, tr.id"

    records = await database.fetch_all(
        query=query,
        values={
            "competitor_id": competitor_id,
            "tournament_id": tournament_id,
            "tenant_club_id": tenant_club_id,
        },
    )
    return [TournamentRegistration.model_validate(dict(record._mapping)) for record in records]


async def get_registration(
    registration_id: TournamentRegistrationId, *, tenant_club_id: ClubId
) -> TournamentRegistration | None:
    """Inscripcion por id, con su estado real (incluidas las sustituidas)."""
    query = f"""
        SELECT {_REGISTRATION_COLUMNS} {_REGISTRATION_TENANT_JOIN}
        WHERE tr.id = :registration_id AND t.club_id = :tenant_club_id
    """
    record = await database.fetch_one(
        query=query, values={"registration_id": registration_id, "tenant_club_id": tenant_club_id}
    )
    return (
        TournamentRegistration.model_validate(dict(record._mapping)) if record is not None else None
    )


async def get_registrations_without_identity(
    tournament_id: TournamentId, *, tenant_club_id: ClubId
) -> list[TournamentRegistration]:
    """Inscripciones vigentes sin competidor asociado (identidad desconocida)."""
    query = f"""
        SELECT {_REGISTRATION_COLUMNS} {_REGISTRATION_TENANT_JOIN}
        WHERE tr.tournament_id = :tournament_id AND t.club_id = :tenant_club_id
        AND tr.competitor_id IS NULL AND {_CURRENT_REGISTRATION}
        ORDER BY tr.competitor_name_snapshot, tr.id
    """
    records = await database.fetch_all(
        query=query, values={"tournament_id": tournament_id, "tenant_club_id": tenant_club_id}
    )
    return [TournamentRegistration.model_validate(dict(record._mapping)) for record in records]


async def _fetch_chain(
    query: str, registration_id: TournamentRegistrationId, tenant_club_id: ClubId
) -> list[TournamentRegistration]:
    records = await database.fetch_all(
        query=query, values={"registration_id": registration_id, "tenant_club_id": tenant_club_id}
    )
    return [TournamentRegistration.model_validate(dict(record._mapping)) for record in records]


async def get_registration_revision_chain(
    registration_id: TournamentRegistrationId, *, tenant_club_id: ClubId
) -> list[TournamentRegistration]:
    """Cadena de revisiones completa (originales y correcciones), ordenada.

    Recorre ascendentes y descendentes por ``corrects_registration_id`` y sigue
    ademas los punteros ``superseded_by_registration_id`` que aun no esten en la
    cadena (red de seguridad, acotada a ``_MAX_CHAIN_HOPS`` saltos). Devuelve
    ``[]`` si la inscripcion no existe o es de otro tenant.
    """
    chain: dict[TournamentRegistrationId, TournamentRegistration] = {}
    for query in (_CHAIN_ANCESTORS, _CHAIN_DESCENDANTS):
        for row in await _fetch_chain(query, registration_id, tenant_club_id):
            chain[row.id] = row

    for _ in range(_MAX_CHAIN_HOPS):
        missing = {
            row.superseded_by_registration_id
            for row in chain.values()
            if row.superseded_by_registration_id is not None
            and row.superseded_by_registration_id not in chain
        }
        if not missing:
            break
        for missing_id in sorted(missing):
            successor = await get_registration(missing_id, tenant_club_id=tenant_club_id)
            if successor is not None:
                chain[successor.id] = successor

    return sorted(chain.values(), key=lambda row: (row.revision, row.id))
