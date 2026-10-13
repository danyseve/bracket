"""Datos que juzga el motor de clasificacion: sin SQL y sin HTTP (S4.8a-2, LAB).

El motor es **puro**: recibe estos datos ya resueltos y devuelve un veredicto. La carga desde la base
vive en ``bracket/sql/classification.py``; aqui no hay acceso a datos, de modo que el juicio es
determinista y se puede probar sin base de datos.

Contratos congelados del bloque (a-2):

* ``category_key`` sigue siendo ``"si:<stage_item_id>"`` y es el identificador contractual; el
  ``stage_item`` se resuelve detras (D2). No se anade clave foranea a ``registrations``.
* ``category_label`` es presentacion (``stage_items.name``), nunca clave logica.
* La afiliacion y el club **no** entran en la compatibilidad deportiva (``representation`` viaja como
  contexto, sin participar en el juicio).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final

from bracket.logic.classification.vocabulary import (
    ClassificationBlocker,
    ClassificationWarning,
)
from bracket.models.db.domain import (
    BeltScope,
    CompetitorSex,
    Modality,
    RegistrationRepresentation,
    RegistrationStatus,
    SexConstraint,
)
from bracket.utils.id_types import StageItemId, TournamentId

#: Prefijo contractual de la clave de categoria (D2).
CATEGORY_KEY_PREFIX: Final[str] = "si:"


def category_key_for(stage_item_id: StageItemId) -> str:
    """Clave contractual de una categoria a partir de su ``stage_item``."""
    return f"{CATEGORY_KEY_PREFIX}{stage_item_id}"


def stage_item_id_from_key(category_key: str) -> StageItemId | None:
    """``stage_item`` detras de una clave contractual; ``None`` si no es una clave del contrato."""
    if not category_key.startswith(CATEGORY_KEY_PREFIX):
        return None
    suffix = category_key[len(CATEGORY_KEY_PREFIX) :]
    if not suffix.isdigit():
        return None
    return StageItemId(int(suffix))


@dataclass(frozen=True)
class BeltLevel:
    """Cinturon **resuelto** del catalogo: su escala y su orden. ``rank`` compara rangos."""

    scope: BeltScope
    rank: int
    code: str = ""


@dataclass(frozen=True)
class CategoryRules:
    """Reglas **estructuradas** de una categoria (``stage_items``). ``None`` = sin restriccion.

    Un ``None`` significa "esta categoria no declara esa restriccion", nunca "admite cualquier
    valor inventado": no se deduce nada del nombre de la categoria (jamas se parsea "-71 kg").
    """

    stage_item_id: StageItemId
    category_label: str | None = None
    sex_constraint: SexConstraint | None = None
    belt_min: BeltLevel | None = None
    belt_max: BeltLevel | None = None
    age_min: int | None = None
    age_max: int | None = None
    weight_min_kg: Decimal | None = None
    weight_max_kg: Decimal | None = None
    modality: Modality | None = None

    @property
    def category_key(self) -> str:
        """Clave contractual de la categoria (``"si:<stage_item_id>"``)."""
        return category_key_for(self.stage_item_id)

    @property
    def has_any_rule(self) -> bool:
        """Cierto si la categoria declara alguna restriccion (las legacy no declaran ninguna)."""
        return any(
            value is not None
            for value in (
                self.sex_constraint,
                self.belt_min,
                self.belt_max,
                self.age_min,
                self.age_max,
                self.weight_min_kg,
                self.weight_max_kg,
                self.modality,
            )
        )

    @property
    def belt_scope(self) -> BeltScope | None:
        """Escala que exige el rango de cinturon (la del extremo declarado), si hay rango."""
        if self.belt_min is not None:
            return self.belt_min.scope
        if self.belt_max is not None:
            return self.belt_max.scope
        return None

    @property
    def belt_range_is_valid(self) -> bool:
        """Coherencia **cruzada** del rango: misma escala y minimo no superior al maximo.

        Un rango incoherente no se corrige ni se ignora: se reporta
        (``INVALID_CATEGORY_BELT_RANGE``) y la categoria no clasifica.
        """
        if self.belt_min is None or self.belt_max is None:
            return True
        return (
            self.belt_min.scope == self.belt_max.scope
            and self.belt_min.rank <= self.belt_max.rank
        )


@dataclass(frozen=True)
class CompetitorFacts:
    """Datos del competidor que participan en la clasificacion (los demas no se miran)."""

    date_of_birth: date | None = None
    sex: CompetitorSex | None = None
    belt: BeltLevel | None = None


@dataclass(frozen=True)
class RegistrationFacts:
    """Datos de **esta** inscripcion: el peso es del torneo, no un atributo permanente (D1/§1)."""

    declared_weight_kg: Decimal | None = None
    modality: Modality | None = None
    category_key: str | None = None
    status: RegistrationStatus | None = None
    superseded: bool = False
    representation: RegistrationRepresentation | None = None

    @property
    def is_current(self) -> bool:
        """Vigente: ni corregida ni sustituida por una correccion (definicion del readiness)."""
        return self.status != "CORRECTED" and not self.superseded

    @property
    def is_confirmed(self) -> bool:
        """Confirmada: solo una confirmada puede contar como participante."""
        return self.status == "CONFIRMED"


@dataclass(frozen=True)
class CategoryVerdict:
    """Veredicto de **una** categoria frente a una inscripcion."""

    category_key: str
    category_label: str | None
    compatible: bool
    blockers: tuple[ClassificationBlocker, ...] = ()
    warnings: tuple[ClassificationWarning, ...] = ()


@dataclass(frozen=True)
class ClassificationResult:
    """Resultado de clasificar una inscripcion contra las categorias del torneo.

    ``blockers`` es el bloqueo **de la inscripcion**: cuando hay alguna categoria compatible esta
    vacio (la inscripcion es clasificable) y los motivos de cada categoria que no encaja siguen
    visibles en ``verdicts``. Cuando no hay ninguna compatible, reune
    ``NO_COMPATIBLE_CATEGORY`` y los motivos concretos.
    """

    verdicts: tuple[CategoryVerdict, ...]
    compatible_categories: tuple[CategoryVerdict, ...]
    recommended_category: CategoryVerdict | None
    blockers: tuple[ClassificationBlocker, ...]
    warnings: tuple[ClassificationWarning, ...]

    @property
    def has_compatible_categories(self) -> bool:
        """Cierto si al menos una categoria del torneo admite la inscripcion."""
        return bool(self.compatible_categories)


@dataclass(frozen=True)
class ChosenCategoryCheck:
    """Veredicto de la categoria **elegida** por la inscripcion (``category_key``)."""

    category_key: str | None
    resolved: bool
    compatible: bool
    blockers: tuple[ClassificationBlocker, ...] = ()
    verdict: CategoryVerdict | None = None


@dataclass(frozen=True)
class RegistrationClassification:
    """Readiness de dominio de una inscripcion (§13). Sin RBAC y sin HTTP."""

    ok: bool
    countable: bool
    chosen: ChosenCategoryCheck
    result: ClassificationResult

    @property
    def blockers(self) -> tuple[ClassificationBlocker, ...]:
        """Bloqueos de la inscripcion + los de la categoria elegida, sin duplicados."""
        return self.result.blockers + tuple(
            blocker for blocker in self.chosen.blockers if blocker not in self.result.blockers
        )

    @property
    def warnings(self) -> tuple[ClassificationWarning, ...]:
        """Avisos del resultado (no se inventa ninguno mas)."""
        return self.result.warnings


@dataclass(frozen=True)
class OverrideCheck:
    """Validacion de dominio del override (§14): recomendada, elegida y desvio. Sin RBAC."""

    recommended_category: CategoryVerdict | None
    chosen: ChosenCategoryCheck
    compatible: bool
    deviation_from_recommendation: bool
    blockers: tuple[ClassificationBlocker, ...]
    warnings: tuple[ClassificationWarning, ...]


@dataclass(frozen=True)
class ClassificationInput:
    """Todo lo que hace falta para clasificar una inscripcion, ya cargado y sin decidir nada."""

    tournament_id: TournamentId
    start_time: datetime
    categories: tuple[CategoryRules, ...]
    competitor: CompetitorFacts
    registration: RegistrationFacts
