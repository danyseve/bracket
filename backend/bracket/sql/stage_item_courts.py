"""Consultas de la asignacion explicita de tatami de un cuadro (S4.2, F3C, LAB ONLY).

La decision vive en ``stage_items.court_id`` (1 cuadro -> 1 tatami, nullable) y **propaga** a
``matches.court_id``, que sigue siendo la verdad de cada combate para el modelo legado de F2C. Estas
consultas son las dos mitades de la operacion: leer y bloquear el cuadro y el tatami destino, y
escribir la propagacion contando **exactamente** cuantos combates cambian.

El bloqueo se toma en el orden cuadro -> tatami con ``FOR NO KEY UPDATE`` (el mismo que usa la
ventana de inscripcion): serializa dos asignaciones del mismo cuadro sin bloquear las lecturas. Un
cuadro de otro torneo, un torneo de otro tenant y un cuadro inexistente dan todos ``None``: no se
filtra la existencia de recursos de otro tenant.
"""

from __future__ import annotations

from dataclasses import dataclass

from bracket.database import database
from bracket.utils.id_types import ClubId, CourtId, StageItemId, TournamentId


@dataclass(frozen=True)
class StageItemCourtRow:
    """Fila minima del cuadro para decidir la transicion, con el tatami actual."""

    stage_item_id: StageItemId
    court_id: CourtId | None


@dataclass(frozen=True)
class CourtRow:
    """Fila minima del tatami destino: identidad y nombre, sin internos."""

    court_id: CourtId
    name: str


#: Cuadro del torneo **y del tenant**, con la fila del cuadro bloqueada. El bloqueo alcanza solo a
#: ``stage_items``: ``stages`` y ``tournaments`` se leen para validar pertenencia, no se bloquean.
_STAGE_ITEM_FOR_UPDATE = """
    SELECT stage_items.id, stage_items.court_id
    FROM stage_items
    JOIN stages ON stages.id = stage_items.stage_id
    JOIN tournaments ON tournaments.id = stages.tournament_id
    WHERE stage_items.id = :stage_item_id
      AND stages.tournament_id = :tournament_id
      AND tournaments.club_id = :tenant_club_id
    FOR NO KEY UPDATE OF stage_items
    """

#: Tatami **del torneo** de la ruta, con su fila bloqueada: dos asignaciones al mismo tatami se
#: serializan y el nombre publicado sale de la misma transaccion que la escritura.
_COURT_FOR_UPDATE = """
    SELECT courts.id, courts.name
    FROM courts
    WHERE courts.id = :court_id AND courts.tournament_id = :tournament_id
    FOR NO KEY UPDATE OF courts
    """

#: Combates del cuadro con actividad **real**. El estado de combate que el backend no tiene
#: (``matches`` no guarda "en curso" ni "finalizado") no se inventa: aqui solo cuenta la evidencia
#: fiable de que ya ha pasado algo, un tanteo distinto de cero o una hora de inicio.
_ACTIVITY_COUNT = """
    SELECT count(*)
    FROM matches
    JOIN rounds ON rounds.id = matches.round_id
    WHERE rounds.stage_item_id = :stage_item_id
      AND (
        matches.stage_item_input1_score <> 0
        OR matches.stage_item_input2_score <> 0
        OR matches.start_time IS NOT NULL
      )
    """

#: Propagacion al modelo legado: los combates del cuadro pasan al tatami del cuadro. Devuelve los
#: identificadores tocados, no un contador del driver, para poder publicar el efecto exacto.
_PROPAGATE_COURT = """
    UPDATE matches
    SET court_id = :court_id
    WHERE matches.court_id IS DISTINCT FROM :court_id
      AND matches.round_id IN (
        SELECT rounds.id FROM rounds WHERE rounds.stage_item_id = :stage_item_id
      )
    RETURNING matches.id
    """

#: Escritura de la decision en el cuadro. El bloqueo de fila lo tomo antes el llamador.
_SET_STAGE_ITEM_COURT = """
    UPDATE stage_items SET court_id = :court_id WHERE stage_items.id = :stage_item_id
    """

#: Estado actual del cuadro **del torneo y del tenant** para publicar la respuesta. La usa la ruta
#: tambien al repetir una operacion, asi que lee el estado vigente y no el cuerpo guardado.
_ASSIGNMENT = """
    SELECT stage_items.court_id, courts.name AS court_name
    FROM stage_items
    JOIN stages ON stages.id = stage_items.stage_id
    JOIN tournaments ON tournaments.id = stages.tournament_id
    LEFT JOIN courts ON courts.id = stage_items.court_id
    WHERE stage_items.id = :stage_item_id
      AND stages.tournament_id = :tournament_id
      AND tournaments.club_id = :tenant_club_id
    """


async def sql_lock_stage_item_for_court(
    *, stage_item_id: StageItemId, tournament_id: TournamentId, tenant_club_id: ClubId
) -> StageItemCourtRow | None:
    """Cuadro del torneo del tenant, **con el bloqueo de fila ya tomado**.

    ``None`` unifica los tres casos que no deben distinguirse hacia fuera: el cuadro no existe, el
    cuadro es de otro torneo, o el torneo no es de este tenant. El bloqueo de fila es lo que
    serializa dos asignaciones del mismo cuadro.
    """
    result = await database.fetch_one(
        query=_STAGE_ITEM_FOR_UPDATE,
        values={
            "stage_item_id": stage_item_id,
            "tournament_id": tournament_id,
            "tenant_club_id": tenant_club_id,
        },
    )
    if result is None:
        return None
    court_id = result["court_id"]
    return StageItemCourtRow(
        stage_item_id=StageItemId(int(result["id"])),
        court_id=None if court_id is None else CourtId(int(court_id)),
    )


async def sql_lock_court(*, court_id: CourtId, tournament_id: TournamentId) -> CourtRow | None:
    """Tatami del torneo de la ruta, **con el bloqueo de fila ya tomado**.

    Un tatami de otro torneo da ``None``: la asignacion es siempre dentro del mismo torneo y el
    rechazo no revela si el tatami existe en otro sitio.
    """
    result = await database.fetch_one(
        query=_COURT_FOR_UPDATE, values={"court_id": court_id, "tournament_id": tournament_id}
    )
    if result is None:
        return None
    return CourtRow(court_id=CourtId(int(result["id"])), name=str(result["name"]))


async def sql_count_stage_item_activity(stage_item_id: StageItemId) -> int:
    """Combates del cuadro con actividad real (tanteo o hora de inicio), no planificacion."""
    value = await database.fetch_val(query=_ACTIVITY_COUNT, values={"stage_item_id": stage_item_id})
    return int(value or 0)


async def sql_propagate_court_to_stage_item_matches(
    *, stage_item_id: StageItemId, court_id: CourtId | None
) -> int:
    """Lleva el tatami del cuadro a sus combates y devuelve cuantos combates cambian de verdad.

    No cuenta los que ya estaban en ese tatami (asignar dos veces no multiplica el efecto) ni toca
    los de ningun otro cuadro.
    """
    rows = await database.fetch_all(
        query=_PROPAGATE_COURT, values={"stage_item_id": stage_item_id, "court_id": court_id}
    )
    return len(rows)


async def sql_set_stage_item_court(*, stage_item_id: StageItemId, court_id: CourtId | None) -> None:
    """Fija el tatami del cuadro. No comprueba pertenencia: el bloqueo lo tomo el llamador."""
    await database.execute(
        query=_SET_STAGE_ITEM_COURT,
        values={"stage_item_id": stage_item_id, "court_id": court_id},
    )


async def sql_stage_item_court_assignment(
    *, stage_item_id: StageItemId, tournament_id: TournamentId, tenant_club_id: ClubId
) -> tuple[CourtId | None, str | None] | None:
    """Tatami vigente del cuadro y su nombre; ``None`` si el cuadro no es del torneo del tenant."""
    result = await database.fetch_one(
        query=_ASSIGNMENT,
        values={
            "stage_item_id": stage_item_id,
            "tournament_id": tournament_id,
            "tenant_club_id": tenant_club_id,
        },
    )
    if result is None:
        return None
    court_id = result["court_id"]
    name = result["court_name"]
    return (
        None if court_id is None else CourtId(int(court_id)),
        None if name is None else str(name),
    )
