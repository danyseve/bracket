"""Lecturas READ-ONLY de la revision de readiness (S3.4d, F3B, LAB ONLY).

Dos lecturas, ninguna escritura y ninguna auditoria:

* las filas de inscripcion del torneo, con lo que hace falta para juzgarlas y **en una sola
  consulta** (los enlaces a competidor y afiliacion se resuelven con ``LEFT JOIN``): no hay N+1 y
  la decision de la capa pura ve un unico estado coherente;
* la estructura competitiva que el torneo ya tenga, reutilizando la lectura completa del torneo y
  las funciones **puras** de F2C (``count_entrants`` / ``get_bracket_generation_blocker``): el
  algoritmo de generacion no se duplica.

**Clasificacion integrada (S4.8a-3 §9).** La revision reutiliza el motor de a-2
(``bracket.logic.classification``) y su cargador SQL: las categorias del torneo se leen **una vez**
(``get_classification_categories``) y cada inscripcion confirmada se clasifica con el motor puro,
sin reimplementar ninguna regla. Los bloqueos del motor viajan en la fila
(``classification_blockers``) y la capa pura decide el conteo. El codigo del motor se traduce al
vocabulario del readiness -- que lo amplia al final, §9 -- por **valor** (los dos vocabularios
comparten la cadena canonica), de modo que no hay una tabla de traduccion que pueda divergir.

El motor **solo se aplica si el torneo define al menos una categoria con reglas**: un torneo sin
categorias con reglas no tiene nada que discriminar y su recuento sigue siendo el heredado (no se
cambia el comportamiento de los cuadros legacy). Es una decision deliberada y acotada: el criterio
solo puede estrechar el conteo de un torneo que **ya** declara reglas deportivas.

El *override* (§8) se reconoce por la auditoria: un evento de categoria (``UPDATE`` /
``CATEGORY_CHANGE``) ejecutado por un **OWNER** del club. La fila lo expone como ``overridden``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, Final

from bracket.database import database
from bracket.logic.bracket_readiness import (
    BracketReadinessBlocker,
    ExistingBracket,
    RegistrationReadinessRow,
)
from bracket.logic.classification.engine import classify_registration
from bracket.logic.classification.rules import (
    BeltLevel,
    CategoryRules,
    CompetitorFacts,
    RegistrationFacts,
)
from bracket.logic.scheduling.generation import count_entrants, get_bracket_generation_blocker
from bracket.models.db.domain import BeltScope
from bracket.models.db.stage_item import StageType
from bracket.sql.classification import get_classification_categories
from bracket.sql.stages import get_full_tournament_details
from bracket.utils.id_types import ClubId, TournamentId

# Todas las filas del torneo, **incluidas las corregidas y las sustituidas**: la coherencia de
# categoria es una invariante del torneo (la etiqueta se congela por clave) y mira todas las filas,
# como la comprobacion del alta. El criterio de "vigente" lo aplica la capa pura.
#
# ``overridden`` reconoce el override de categoria (§8): existe un evento de auditoria de categoria
# ejecutado por un OWNER del club del torneo. Se resuelve en la misma consulta, sin N+1.
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
        c.date_of_birth AS competitor_date_of_birth,
        c.sex AS competitor_sex,
        cb.scope AS competitor_belt_scope,
        cb.rank AS competitor_belt_rank,
        cb.code AS competitor_belt_code,
        tr.declared_weight_kg,
        tr.modality AS registration_modality,
        t.start_time,
        tr.sports_club_id,
        tr.affiliation_id,
        aff.competitor_id AS affiliation_competitor_id,
        aff.sports_club_id AS affiliation_sports_club_id,
        EXISTS (
            SELECT 1
            FROM domain_change_log dcl
            JOIN users_x_clubs uxc
              ON uxc.user_id = dcl.actor_user_id AND uxc.club_id = t.club_id
            WHERE dcl.entity = 'tournament_registration'
              AND dcl.entity_id = tr.id
              AND dcl.action = 'UPDATE'
              AND dcl.reason_code = 'CATEGORY_CHANGE'
              AND uxc.relation = 'OWNER'
        ) AS overridden
    FROM tournament_registrations tr
    JOIN tournaments t ON t.id = tr.tournament_id
    LEFT JOIN competitors c ON c.id = tr.competitor_id
    LEFT JOIN belts cb ON cb.id = c.belt_id
    LEFT JOIN competitors_x_sports_clubs aff ON aff.id = tr.affiliation_id
    WHERE tr.tournament_id = :tournament_id AND t.club_id = :tenant_club_id
    ORDER BY tr.id
"""


def _mapped_blocker(blocker: object) -> BracketReadinessBlocker:
    """Traduce un codigo del motor al vocabulario del readiness por valor (cadenas canonicas)."""
    return BracketReadinessBlocker(str(blocker))


def _belt_level(scope: BeltScope | None, rank: int | None, code: str | None) -> BeltLevel | None:
    """Cinturon resuelto a partir de las columnas del catalogo (``None`` si no hay cinturon)."""
    if scope is None or rank is None:
        return None
    return BeltLevel(scope=scope, rank=rank, code=code or "")


def _classification_blockers(
    record: Mapping[str, Any], categories: tuple[CategoryRules, ...], on_date: date
) -> tuple[BracketReadinessBlocker, ...]:
    """Bloqueos de clasificacion de una inscripcion **confirmada**, segun el motor de a-2.

    Solo se clasifican las confirmadas: un borrador ya bloquea por si mismo y su clasificacion no
    decide nada. Se traduce el veredicto del motor sin reimplementar reglas.
    """
    assessment = classify_registration(
        categories,
        CompetitorFacts(
            date_of_birth=record["competitor_date_of_birth"],
            sex=record["competitor_sex"],
            belt=_belt_level(
                record["competitor_belt_scope"],
                record["competitor_belt_rank"],
                record["competitor_belt_code"],
            ),
        ),
        RegistrationFacts(
            declared_weight_kg=record["declared_weight_kg"],
            modality=record["registration_modality"],
            category_key=record["category_key"],
            status=record["status"],
            superseded=bool(record["superseded"]),
            representation=record["representation"],
        ),
        on_date,
    )
    return tuple(_mapped_blocker(blocker) for blocker in assessment.blockers)


def _row_from_record(
    record: Mapping[str, Any],
    *,
    classification_blockers: tuple[BracketReadinessBlocker, ...] = (),
) -> RegistrationReadinessRow:
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
        classification_blockers=classification_blockers,
        overridden=bool(record["overridden"]),
    )


async def get_registration_readiness_rows(
    tournament_id: TournamentId, *, tenant_club_id: ClubId
) -> list[RegistrationReadinessRow]:
    """Filas de inscripcion del torneo del tenant, en orden determinista por identificador.

    Un torneo de otro tenant no devuelve filas (la consulta esta acotada), igual que un torneo
    inexistente: la tenencia se aplica **dentro** del SQL y no depende de una comprobacion previa.

    Las categorias del torneo se leen **una vez** y la clasificacion se aplica en memoria (§9),
    solo si hay al menos una categoria con reglas. Sin categorias con reglas no se clasifica nada y
    el recuento es el heredado.
    """
    records = await database.fetch_all(
        query=_SELECT_READINESS_ROWS,
        values={"tournament_id": tournament_id, "tenant_club_id": tenant_club_id},
    )
    categories = await get_classification_categories(tournament_id, tenant_club_id)
    classifying = any(rule.has_any_rule for rule in categories)
    rows: list[RegistrationReadinessRow] = []
    for record in records:
        row_mapping = record._mapping
        blockers: tuple[BracketReadinessBlocker, ...] = ()
        if classifying and row_mapping["status"] == "CONFIRMED":
            blockers = _classification_blockers(
                row_mapping, categories, row_mapping["start_time"].date()
            )
        rows.append(_row_from_record(row_mapping, classification_blockers=blockers))
    return rows


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
