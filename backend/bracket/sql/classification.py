"""Lecturas READ-ONLY de clasificacion (S4.8a-2, LAB).

Dos lecturas, ninguna escritura y ninguna auditoria:

* las **categorias** del torneo (``stage_items``) con sus reglas estructuradas y el cinturon ya
  resuelto a escala y orden, en **una sola consulta**;
* la **entrada de clasificacion** de una inscripcion: su torneo, el competidor y la propia
  inscripcion, tambien sin N+1 (los enlaces se resuelven con ``LEFT JOIN``).

Ambito: la clave se acota por ``tournament_id``/``tenant_club_id``, de modo que una inscripcion o
unas categorias de otro club no se pueden leer. Ninguna de las dos decide: solo cargan datos, y el
juicio queda en el motor puro (``bracket/logic/classification``).

El cinturon no se guarda en el competidor: se guarda su ``belt_id``, y aqui se resuelven su escala
y su orden contra el catalogo, lo que permite comparar rangos sin parsear el nombre de la categoria.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Final

from bracket.database import database
from bracket.logic.classification.rules import (
    BeltLevel,
    CategoryRules,
    ClassificationInput,
    CompetitorFacts,
    RegistrationFacts,
)
from bracket.models.db.domain import BeltScope, Modality
from bracket.utils.id_types import ClubId, CompetitorId, TournamentId, TournamentRegistrationId

_CATEGORY_COLUMNS: Final[str] = """
        si.id AS stage_item_id,
        si.name AS category_label,
        si.sex_constraint,
        si.age_min,
        si.age_max,
        si.weight_min_kg,
        si.weight_max_kg,
        si.modality,
        bmin.scope AS belt_min_scope,
        bmin.rank AS belt_min_rank,
        bmin.code AS belt_min_code,
        bmax.scope AS belt_max_scope,
        bmax.rank AS belt_max_rank,
        bmax.code AS belt_max_code
"""

#: Categorias del torneo: **todas** sus ``stage_items``, activas o no, en orden estable por id.
_SELECT_CATEGORIES: Final[str] = f"""
    SELECT {_CATEGORY_COLUMNS}
    FROM stage_items si
    JOIN stages s ON s.id = si.stage_id
    JOIN tournaments t ON t.id = s.tournament_id
    LEFT JOIN belts bmin ON bmin.id = si.belt_min_id
    LEFT JOIN belts bmax ON bmax.id = si.belt_max_id
    WHERE s.tournament_id = :tournament_id AND t.club_id = :tenant_club_id
    ORDER BY si.id
"""

_SELECT_REGISTRATION_INPUT: Final[str] = """
    SELECT
        tr.tournament_id,
        tr.category_key,
        tr.declared_weight_kg,
        tr.modality AS registration_modality,
        tr.status AS registration_status,
        (tr.superseded_by_registration_id IS NOT NULL) AS superseded,
        tr.representation,
        tr.competitor_id,
        t.start_time,
        c.date_of_birth,
        c.sex,
        cb.scope AS belt_scope,
        cb.rank AS belt_rank,
        cb.code AS belt_code
    FROM tournament_registrations tr
    JOIN tournaments t ON t.id = tr.tournament_id
    LEFT JOIN competitors c ON c.id = tr.competitor_id
    LEFT JOIN belts cb ON cb.id = c.belt_id
    WHERE tr.id = :registration_id AND t.club_id = :tenant_club_id
"""


def _belt_level(scope: BeltScope | None, rank: int | None, code: str | None) -> BeltLevel | None:
    """Cinturon resuelto a partir de las columnas del catalogo (``None`` si no hay cinturon)."""
    if scope is None or rank is None:
        return None
    return BeltLevel(scope=scope, rank=rank, code=code or "")


def _category_rules_from_record(record: Mapping[str, Any]) -> CategoryRules:
    """Proyeccion de la fila leida a las reglas que juzga el motor, sin modelos SQL crudos."""
    return CategoryRules(
        stage_item_id=record["stage_item_id"],
        category_label=record["category_label"],
        sex_constraint=record["sex_constraint"],
        belt_min=_belt_level(
            record["belt_min_scope"], record["belt_min_rank"], record["belt_min_code"]
        ),
        belt_max=_belt_level(
            record["belt_max_scope"], record["belt_max_rank"], record["belt_max_code"]
        ),
        age_min=record["age_min"],
        age_max=record["age_max"],
        weight_min_kg=record["weight_min_kg"],
        weight_max_kg=record["weight_max_kg"],
        modality=record["modality"],
    )


async def get_classification_categories(
    tournament_id: TournamentId, tenant_club_id: ClubId
) -> tuple[CategoryRules, ...]:
    """Categorias del torneo con sus reglas, en orden estable por ``stage_item_id``."""
    records = await database.fetch_all(
        query=_SELECT_CATEGORIES,
        values={"tournament_id": tournament_id, "tenant_club_id": tenant_club_id},
    )
    return tuple(_category_rules_from_record(record._mapping) for record in records)


async def get_registration_classification_input(
    registration_id: TournamentRegistrationId, tenant_club_id: ClubId
) -> ClassificationInput | None:
    """Entrada de clasificacion de una inscripcion, o ``None`` si no es de este club.

    Un competidor ausente (inscripcion en borrador sin competidor) se devuelve con hechos vacios:
    el motor decidira entonces los ``MISSING_*`` que correspondan. No se lanza excepcion por un dato
    que falta: falta de dato no es inscripcion inexistente.
    """
    record = await database.fetch_one(
        query=_SELECT_REGISTRATION_INPUT,
        values={"registration_id": registration_id, "tenant_club_id": tenant_club_id},
    )
    if record is None:
        return None
    row = record._mapping
    categories = await get_classification_categories(row["tournament_id"], tenant_club_id)
    return ClassificationInput(
        tournament_id=row["tournament_id"],
        start_time=row["start_time"],
        categories=categories,
        competitor=CompetitorFacts(
            date_of_birth=row["date_of_birth"],
            sex=row["sex"],
            belt=_belt_level(row["belt_scope"], row["belt_rank"], row["belt_code"]),
        ),
        registration=RegistrationFacts(
            declared_weight_kg=row["declared_weight_kg"],
            modality=row["registration_modality"],
            category_key=row["category_key"],
            status=row["registration_status"],
            superseded=bool(row["superseded"]),
            representation=row["representation"],
        ),
    )


#: Torneo del tenant + competidor **vigente** del tenant con su cinturon resuelto, en una consulta.
#: El competidor se une con sus tres condiciones (id, tenant y ``active``) dentro del ``ON``: asi
#: una identidad de otro club o dada de baja deja la columna en ``NULL`` sin desaparecer la fila, y
#: el torneo inexistente o de otro tenant se distingue del competidor no elegible.
_SELECT_PREVIEW_FACTS: Final[str] = """
    SELECT
        t.start_time,
        c.id AS eligible_competitor_id,
        c.date_of_birth,
        c.sex,
        cb.scope AS belt_scope,
        cb.rank AS belt_rank,
        cb.code AS belt_code
    FROM tournaments t
    LEFT JOIN competitors c
        ON c.id = :competitor_id
        AND c.managed_by_club_id = :tenant_club_id
        AND c.active IS TRUE
    LEFT JOIN belts cb ON cb.id = c.belt_id
    WHERE t.id = :tournament_id AND t.club_id = :tenant_club_id
"""


async def get_preview_classification_input(
    tournament_id: TournamentId,
    tenant_club_id: ClubId,
    *,
    competitor_id: CompetitorId,
    declared_weight_kg: Decimal | None,
    modality: Modality | None,
) -> ClassificationInput | None:
    """Entrada de clasificacion **antes de inscribir** (S4.8d §2), o ``None`` si no es clasificable.

    Reutiliza las mismas piezas que la clasificacion de una inscripcion ya existente: las categorias
    salen de :func:`get_classification_categories` y el cinturon se resuelve contra el catalogo, asi
    que el motor puro juzga exactamente lo mismo en los dos caminos. Lo unico que cambia es de donde
    viene lo declarado: aqui del cuerpo de la peticion (peso y modalidad), porque todavia no hay
    fila de la que leerlo.

    ``None`` unifica tres cosas que no se distinguen desde fuera: torneo inexistente, torneo de otro
    tenant y competidor que no es una identidad **vigente** de ese tenant (la misma condicion que
    :func:`~bracket.sql.registration_writes.sql_selectable_competitor` exige para un alta). El error
    de la capa HTTP no revela cual de las tres pasa.

    ``status`` viaja en ``None`` y ``category_key`` tambien: no hay inscripcion y la previa es
    precisamente **antes** de elegir categoria. El motor responde con eso los ``MISSING_*`` que
    correspondan, incluidos los del propio competidor, que aqui si son reales.

    Es una lectura: no crea inscripcion, no escribe auditoria y no reserva idempotencia.
    """
    record = await database.fetch_one(
        query=_SELECT_PREVIEW_FACTS,
        values={
            "tournament_id": tournament_id,
            "tenant_club_id": tenant_club_id,
            "competitor_id": competitor_id,
        },
    )
    if record is None or record["eligible_competitor_id"] is None:
        return None
    row = record._mapping
    categories = await get_classification_categories(tournament_id, tenant_club_id)
    return ClassificationInput(
        tournament_id=tournament_id,
        start_time=row["start_time"],
        categories=categories,
        competitor=CompetitorFacts(
            date_of_birth=row["date_of_birth"],
            sex=row["sex"],
            belt=_belt_level(row["belt_scope"], row["belt_rank"], row["belt_code"]),
        ),
        registration=RegistrationFacts(
            declared_weight_kg=declared_weight_kg,
            modality=modality,
            category_key=None,
            status=None,
            superseded=False,
            representation=None,
        ),
    )
