"""Vocabulario **cerrado** del motor de clasificacion (S4.8a-2, LAB).

Los codigos son el contrato: un cliente los traduce y un mensaje libre no. No se renombra ninguno en
su sitio y no se emite ninguno sin una condicion real detras. El orden de la declaracion es el orden
canonico de la respuesta: nunca el de la consulta ni el de la iteracion.

Los codigos no duplican los del readiness del cuadro (``BracketReadinessBlocker``): aquel responde
"puede generarse el cuadro" y este "puede clasificarse esta inscripcion". Se mantienen separados
mientras la clasificacion no se exponga por HTTP (a-3 decidira como se surfacean).
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Final


class ClassificationBlocker(StrEnum):
    """Motivos por los que una inscripcion no tiene categoria resoluble."""

    #: La categoria restringe edad y el competidor no tiene fecha de nacimiento.
    MISSING_DATE_OF_BIRTH = "MISSING_DATE_OF_BIRTH"
    #: La categoria restringe sexo y el competidor no lo tiene declarado.
    MISSING_SEX = "MISSING_SEX"
    #: La categoria restringe cinturon y el competidor no tiene cinturon.
    MISSING_BELT = "MISSING_BELT"
    #: La categoria restringe peso y la inscripcion no declara peso.
    MISSING_WEIGHT = "MISSING_WEIGHT"
    #: La categoria restringe modalidad y la inscripcion no declara modalidad.
    MISSING_MODALITY = "MISSING_MODALITY"
    #: La clave de la categoria elegida no resuelve a una categoria del torneo.
    CATEGORY_NOT_RESOLVED = "CATEGORY_NOT_RESOLVED"
    #: Un dato **disponible** incumple una restriccion explicita (no es un dato que falte).
    CATEGORY_INCOMPATIBLE = "CATEGORY_INCOMPATIBLE"
    #: Ninguna categoria del torneo admite esta inscripcion.
    NO_COMPATIBLE_CATEGORY = "NO_COMPATIBLE_CATEGORY"
    #: Configuracion incoherente de la propia categoria (rango de cinturon imposible).
    INVALID_CATEGORY_BELT_RANGE = "INVALID_CATEGORY_BELT_RANGE"


class ClassificationWarning(StrEnum):
    """Situaciones **no** bloqueantes que el organizador deberia conocer."""

    #: La categoria no declara ninguna restriccion: admite cualquier inscripcion (cuadro legacy).
    CATEGORY_RULES_PARTIAL = "CATEGORY_RULES_PARTIAL"
    #: Mas de una categoria admite la inscripcion: no hay recomendacion determinista.
    MULTIPLE_COMPATIBLE_CATEGORIES = "MULTIPLE_COMPATIBLE_CATEGORIES"


_BLOCKER_ORDER: Final[dict[ClassificationBlocker, int]] = {
    blocker: position for position, blocker in enumerate(ClassificationBlocker)
}
_WARNING_ORDER: Final[dict[ClassificationWarning, int]] = {
    warning: position for position, warning in enumerate(ClassificationWarning)
}


def order_blockers(
    blockers: Iterable[ClassificationBlocker],
) -> tuple[ClassificationBlocker, ...]:
    """Orden canonico y **sin duplicados**: el de la declaracion del vocabulario."""
    return tuple(sorted(set(blockers), key=lambda blocker: _BLOCKER_ORDER[blocker]))


def order_warnings(
    warnings: Iterable[ClassificationWarning],
) -> tuple[ClassificationWarning, ...]:
    """Orden canonico y **sin duplicados** del vocabulario de avisos."""
    return tuple(sorted(set(warnings), key=lambda warning: _WARNING_ORDER[warning]))
