"""Lecturas READ-ONLY de la revision de readiness (S3.4d, F3B, LAB ONLY).

Dos lecturas, ninguna escritura y ninguna auditoria:

* las filas de inscripcion del torneo, con lo que hace falta para juzgarlas y **en una sola
  consulta** (los enlaces a competidor y afiliacion se resuelven con ``LEFT JOIN``): no hay N+1 y
  la decision de la capa pura ve un unico estado coherente;
* la estructura competitiva que el torneo ya tenga, reutilizando la lectura completa del torneo y
  las funciones **puras** de F2C (``count_entrants`` / ``get_bracket_generation_blocker``): el
  algoritmo de generacion no se duplica.

Ambito: la consulta de filas acota por ``tournament_id`` **y** por el tenant del contexto. La
lectura de estructura no se acota (``get_full_tournament_details`` solo entiende de torneo) porque
quien llama ya ha autorizado el torneo: la ruta lee antes la ventana de inscripcion, que es la que
resuelve el 404 de un torneo de otro tenant. Ese orden es un requisito, no un detalle: sin el, la
lectura de estructura no tendria control de tenencia propio.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from bracket.database import database
from bracket.logic.bracket_readiness import ExistingBracket, RegistrationReadinessRow
from bracket.logic.scheduling.generation import count_entrants, get_bracket_generation_blocker
from bracket.models.db.stage_item import StageType
from bracket.sql.stages import get_full_tournament_details
from bracket.utils.id_types import ClubId, TournamentId

# Todas las filas del torneo, **incluidas las corregidas y las sustituidas**: la coherencia de
# categoria es una invariante del torneo (la etiqueta se congela por clave) y mira todas las filas,
# como la comprobacion del alta. El criterio de "vigente" lo aplica la capa pura.
_SELECT_READINESS_ROWS: Final[str] = """
    SELECT
        tr.id AS registration_id,
        tr.status,
        (tr.superseded_by_registration_id IS NOT NULL) AS superseded,
        tr.category_key,
        tr.category_label,
        tr.representation,
        tr.competitor_id,
        c.active AS competitor_active,
        c.managed_by_club_id AS competitor_tenant_club_id,
        tr.sports_club_id,
        tr.affiliation_id,
        aff.competitor_id AS affiliation_competitor_id,
        aff.sports_club_id AS affiliation_sports_club_id
    FROM tournament_registrations tr
    JOIN tournaments t ON t.id = tr.tournament_id
    LEFT JOIN competitors c ON c.id = tr.competitor_id
    LEFT JOIN competitors_x_sports_clubs aff ON aff.id = tr.affiliation_id
    WHERE tr.tournament_id = :tournament_id AND t.club_id = :tenant_club_id
    ORDER BY tr.id
"""


def _row_from_record(record: Mapping[str, Any]) -> RegistrationReadinessRow:
    """Proyeccion de la fila leida al dato que juzga la capa pura (sin modelos SQL crudos)."""
    return RegistrationReadinessRow(
        registration_id=record["registration_id"],
        status=record["status"],
        superseded=bool(record["superseded"]),
        category_key=record["category_key"],
        category_label=record["category_label"],
        representation=record["representation"],
        competitor_id=record["competitor_id"],
        competitor_active=record["competitor_active"],
        competitor_tenant_club_id=record["competitor_tenant_club_id"],
        sports_club_id=record["sports_club_id"],
        affiliation_id=record["affiliation_id"],
        affiliation_competitor_id=record["affiliation_competitor_id"],
        affiliation_sports_club_id=record["affiliation_sports_club_id"],
    )


async def get_registration_readiness_rows(
    tournament_id: TournamentId, *, tenant_club_id: ClubId
) -> list[RegistrationReadinessRow]:
    """Filas de inscripcion del torneo del tenant, en orden determinista por identificador.

    Un torneo de otro tenant no devuelve filas (la consulta esta acotada), igual que un torneo
    inexistente: la tenencia se aplica **dentro** del SQL y no depende de una comprobacion previa.
    """
    records = await database.fetch_all(
        query=_SELECT_READINESS_ROWS,
        values={"tournament_id": tournament_id, "tenant_club_id": tenant_club_id},
    )
    return [_row_from_record(record._mapping) for record in records]


async def get_existing_single_elimination_brackets(
    tournament_id: TournamentId,
) -> list[ExistingBracket]:
    """Cuadros de eliminacion directa que el torneo ya tiene, con su veredicto de F2C.

    Solo eliminacion directa: la generacion explicita de F2C es la que admite regeneracion y la
    unica para la que existe un bloqueo que preguntar. Otros formatos (liguilla, etc.) quedan fuera
    del alcance de esta revision y se documentan como "no aplica" (``docs/26`` §42.6).

    Orden determinista por identificador de ``stage_item``: dos llamadas con el mismo estado
    devuelven lo mismo.
    """
    brackets: list[ExistingBracket] = []
    for stage in await get_full_tournament_details(tournament_id):
        for stage_item in stage.stage_items:
            if stage_item.type != StageType.SINGLE_ELIMINATION:
                continue
            brackets.append(
                ExistingBracket(
                    stage_item_id=stage_item.id,
                    entrant_count=count_entrants(stage_item),
                    generator_blocker=get_bracket_generation_blocker(stage_item),
                )
            )
    return sorted(brackets, key=lambda bracket: bracket.stage_item_id)
