"""Revision final del estado de las inscripciones antes de generar cuadro (S3.4d, F3B, LAB ONLY).

Responde a una unica pregunta -- "esta listo este torneo para generar brackets?" -- y, cuando la
respuesta es no, a "que problemas concretos lo impiden". No genera cuadros, no escribe y no audita:
es una **revision derivada**, sin estado persistente ``READY`` y sin duplicar informacion que ya
vive en el torneo y en sus inscripciones.

Reglas (``docs/26`` §42):

* el resultado se calcula del estado real y es determinista para el mismo estado: no hay columna ni
  cache que pueda desincronizarse del dominio;
* el minimo por categoria **no se inventa**: es el mismo que exige la generacion de cuadro de F2C
  (``bracket.logic.scheduling.generation``), importado como constante en vez de copiado;
* la ventana bloquea **solo** en ``OPEN``: ``DEADLINE_PASSED`` y ``CLOSED`` permiten revisar y el
  cierre manual no es un requisito. La fase que genere cuadros exigira la misma condicion ("no
  ``OPEN``"), asi que no hay dos definiciones que puedan divergir;
* solo ``CONFIRMED`` cuenta como participante; ``WITHDRAWN`` y ``DISQUALIFIED`` no participan, y
  una inscripcion sustituida por una correccion **no se cuenta dos veces**: "vigente" es la misma
  definicion que usa el listado de S1 (``status <> 'CORRECTED'`` y sin sustituta);
* solo se emiten codigos con una condicion **alcanzable** en el modelo actual. Las invariantes que
  la base de datos ya garantiza no se reportan (seria codigo muerto): se documentan como "no
  aplica" en ``docs/26`` §42.6, con el ``CHECK``/indice que las sostiene. Las que solo garantiza la
  aplicacion si se comprueban, porque un dato heredado o manipulado fuera de la aplicacion las
  puede incumplir.

La lista global de bloqueos es la vista completa: incluye los bloqueos de cada categoria ademas de
los del torneo. ``ready`` es cierto solo si esa lista esta vacia, de modo que "sin bloqueos
globales y todas las categorias listas" y "sin bloqueos" no pueden discrepar.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, NamedTuple

from bracket.logic.scheduling.generation import MINIMUM_ENTRANTS, BracketGenerationBlocker
from bracket.models.db.domain import (
    RegistrationRepresentation,
    RegistrationStatus,
    RegistrationWindowState,
)
from bracket.utils.id_types import (
    ClubId,
    CompetitorId,
    CompetitorXSportsClubId,
    SportsClubId,
    StageItemId,
    TournamentId,
    TournamentRegistrationId,
)


class BracketReadinessBlocker(StrEnum):
    """Vocabulario **cerrado** de bloqueos del readiness.

    Los codigos son estables y son el contrato: un mensaje libre no lo es. Un cliente traduce el
    codigo, asi que no se renombra uno en su sitio y no se emite ninguno sin una condicion real
    detras (``docs/26`` §42.6).
    """

    #: La ventana de inscripcion sigue admitiendo altas nuevas.
    REGISTRATION_WINDOW_OPEN = "REGISTRATION_WINDOW_OPEN"
    #: Hay inscripciones en borrador sin resolver.
    REGISTRATIONS_PENDING = "REGISTRATIONS_PENDING"
    #: Una categoria no llega al minimo de participantes confirmados de la generacion de F2C.
    CATEGORY_NOT_ENOUGH_COMPETITORS = "CATEGORY_NOT_ENOUGH_COMPETITORS"
    #: Una inscripcion confirmada apunta a un competidor dado de baja.
    COMPETITOR_INACTIVE = "COMPETITOR_INACTIVE"
    #: Clave y etiqueta de categoria no van juntas, o una clave significa dos cosas.
    INVALID_CATEGORY = "INVALID_CATEGORY"
    #: La fila guardada no cumple una invariante que la aplicacion solo garantiza al escribir.
    REGISTRATION_INCONSISTENT = "REGISTRATION_INCONSISTENT"
    #: El torneo ya tiene un cuadro generado que **no** se puede regenerar (actividad encima).
    BRACKET_ALREADY_EXISTS = "BRACKET_ALREADY_EXISTS"
    # --- S4.8a-3 §9: codigos del motor de clasificacion, **anadidos al final** para no
    # reordenar los bloqueos existentes (el orden del vocabulario es el de la respuesta). ---
    #: La categoria exige edad y el competidor no tiene fecha de nacimiento.
    MISSING_DATE_OF_BIRTH = "MISSING_DATE_OF_BIRTH"
    #: La categoria exige sexo y el competidor no lo tiene declarado.
    MISSING_SEX = "MISSING_SEX"
    #: La categoria exige cinturon y el competidor no tiene cinturon.
    MISSING_BELT = "MISSING_BELT"
    #: La categoria exige peso y la inscripcion no declara peso.
    MISSING_WEIGHT = "MISSING_WEIGHT"
    #: La categoria exige modalidad y la inscripcion no la declara.
    MISSING_MODALITY = "MISSING_MODALITY"
    #: Ninguna categoria del torneo admite esta inscripcion.
    NO_COMPATIBLE_CATEGORY = "NO_COMPATIBLE_CATEGORY"
    #: La clave de categoria elegida no resuelve a una categoria del torneo.
    CATEGORY_NOT_RESOLVED = "CATEGORY_NOT_RESOLVED"
    #: Un dato disponible incumple una restriccion explicita de la categoria.
    CATEGORY_INCOMPATIBLE = "CATEGORY_INCOMPATIBLE"
    #: Configuracion incoherente de la propia categoria (rango de cinturon imposible).
    INVALID_CATEGORY_BELT_RANGE = "INVALID_CATEGORY_BELT_RANGE"


#: Orden canonico de la respuesta: el de la declaracion del vocabulario, nunca el del SQL.
#: Desempata por ``category_key`` (``NULLS FIRST``, como el listado de S1) y por ``count``.
_BLOCKER_ORDER: Final[dict[BracketReadinessBlocker, int]] = {
    blocker: position for position, blocker in enumerate(BracketReadinessBlocker)
}

_CORRECTED: Final[RegistrationStatus] = "CORRECTED"


@dataclass(frozen=True)
class RegistrationReadinessRow:  # pylint: disable=too-many-instance-attributes
    """Una fila del torneo tal como la lee la revision, con lo que hace falta para juzgarla.

    Los enlaces (competidor, afiliacion) vienen resueltos del ``LEFT JOIN``: ``None`` significa que
    la fila apunta a algo que no existe o que no es ubicable. Una sola consulta, sin N+1, y la
    decision queda en una funcion pura.
    """

    registration_id: TournamentRegistrationId
    status: RegistrationStatus
    superseded: bool
    category_key: str | None
    category_label: str | None
    representation: RegistrationRepresentation
    competitor_id: CompetitorId | None
    competitor_active: bool | None
    competitor_tenant_club_id: ClubId | None
    sports_club_id: SportsClubId | None
    affiliation_id: CompetitorXSportsClubId | None
    affiliation_competitor_id: CompetitorId | None
    affiliation_sports_club_id: SportsClubId | None
    #: Bloqueos del motor de clasificacion (§9). Vacio significa que la categoria elegida es
    #: resoluble y compatible (o que la inscripcion no tiene nada que clasificar todavia).
    classification_blockers: tuple[BracketReadinessBlocker, ...] = ()
    #: Hubo un override de categoria OWNER valido y auditado (§8): el bloqueo no cuenta.
    overridden: bool = False

    @property
    def is_current(self) -> bool:
        """Vigente: ni corregida ni sustituida por una correccion (definicion de S1)."""
        return self.status != _CORRECTED and not self.superseded

    @property
    def counts_toward_minimum(self) -> bool:
        """Cuenta para el minimo de la categoria (§9): clasificable o con override OWNER.

        Una confirmada con bloqueos de clasificacion no es un participante clasificable; un
        override OWNER valido y auditado la vuelve contable aunque la categoria no encaje.
        """
        return not self.classification_blockers or self.overridden


@dataclass(frozen=True)
class ExistingBracket:
    """Cuadro que el torneo **ya** tiene, con el veredicto de generacion de F2C ya aplicado.

    ``generator_blocker`` es el bloqueo que devuelve la funcion pura de F2C
    (``get_bracket_generation_blocker``): ``None`` significa que el cuadro se puede regenerar (la
    generacion es idempotente), y cualquier valor significa que algo depende ya de su reparto.
    """

    stage_item_id: StageItemId
    entrant_count: int
    generator_blocker: BracketGenerationBlocker | None

    @property
    def is_generated(self) -> bool:
        """Hay cuadro de verdad: al menos el minimo de entrantes que exige F2C."""
        return self.entrant_count >= MINIMUM_ENTRANTS

    @property
    def is_frozen(self) -> bool:
        """Cuadro generado que ya no se puede rehacer: alguien depende de su reparto."""
        return self.is_generated and self.generator_blocker is not None


@dataclass(frozen=True)
class ReadinessIssue:
    """Bloqueo concreto: codigo estable y, cuando cuenta algo, cuantas cosas cuenta."""

    code: BracketReadinessBlocker
    count: int | None = None
    category_key: str | None = None
    required_minimum: int | None = None


@dataclass(frozen=True)
class RegistrationSummary:
    """Recuento de inscripciones vigentes por estado. ``total`` es la suma de los cuatro."""

    total: int
    confirmed: int
    draft: int
    withdrawn: int
    disqualified: int


@dataclass(frozen=True)
class CategoryReadiness:
    """Una categoria (clave + etiqueta) con sus confirmados y sus bloqueos propios."""

    category_key: str | None
    category_label: str | None
    confirmed_count: int
    required_minimum: int
    issues: tuple[ReadinessIssue, ...]

    @property
    def ready(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class BracketReadiness:
    """Resultado: la respuesta a "esta listo" y el detalle de lo que lo impide."""

    tournament_id: TournamentId
    ready: bool
    window_state: RegistrationWindowState
    summary: RegistrationSummary
    categories: tuple[CategoryReadiness, ...]
    issues: tuple[ReadinessIssue, ...]


class _CategoryGroup(NamedTuple):
    """Grupo de inscripciones confirmadas vigentes con la misma ``category_key``."""

    rows: tuple[RegistrationReadinessRow, ...]
    labels: frozenset[str]


def _category_sort_key(category_key: str | None) -> tuple[bool, str]:
    """``category_key`` nulas primero y despues por clave, como el orden del listado de S1."""
    return (category_key is not None, category_key or "")


def _issue_sort_key(issue: ReadinessIssue) -> tuple[int, bool, str, int]:
    return (
        _BLOCKER_ORDER[issue.code],
        issue.category_key is not None,
        issue.category_key or "",
        issue.count or 0,
    )


def _summary_of(current: Sequence[RegistrationReadinessRow]) -> RegistrationSummary:
    """Recuento por estado de las vigentes. Los estados son un vocabulario cerrado."""

    def count(status: RegistrationStatus) -> int:
        return sum(1 for row in current if row.status == status)

    return RegistrationSummary(
        total=len(current),
        confirmed=count("CONFIRMED"),
        draft=count("DRAFT"),
        withdrawn=count("WITHDRAWN"),
        disqualified=count("DISQUALIFIED"),
    )


def _category_blocker(row: RegistrationReadinessRow) -> BracketReadinessBlocker | None:
    """Clave y etiqueta van juntas o ninguna, y la clave es un slug estable en minusculas.

    Lo exige el alta (``registrations._resolve_category``) y **no** hay ``CHECK`` que lo garantice:
    es la comprobacion que si aporta algo sobre datos heredados. Se mira en todas las filas, no
    solo en las vigentes: la etiqueta se congela por clave y por torneo.
    """
    if (row.category_key is None) != (row.category_label is None):
        return BracketReadinessBlocker.INVALID_CATEGORY
    if row.category_key is None:
        return None
    if not row.category_key or row.category_key != row.category_key.strip().lower():
        return BracketReadinessBlocker.INVALID_CATEGORY
    return None


def _row_blockers(
    row: RegistrationReadinessRow, *, tenant_club_id: ClubId
) -> tuple[BracketReadinessBlocker, ...]:
    """Invariantes de una inscripcion **confirmada** vigente.

    Son las que la aplicacion solo garantiza al escribir:

    Un borrador no se juzga aqui: ya bloquea por si mismo mientras este pendiente. Lo que la base
    de datos ya garantiza con un ``CHECK`` (identidad verificada sin competidor, representacion de
    club sin club o sin snapshot) **no** se repite aqui: no se puede incumplir, y comprobarlo
    seria codigo muerto (``docs/26`` §42.6).
    """
    blockers: list[BracketReadinessBlocker] = []

    if row.competitor_id is not None:
        # La elegibilidad del competidor (del tenant y activo, S3.1a) la garantiza el alta, no la
        # base: la baja logica es posterior a la confirmacion y otro tenant no pasa por el alta.
        if row.competitor_active is None or row.competitor_tenant_club_id != tenant_club_id:
            blockers.append(BracketReadinessBlocker.REGISTRATION_INCONSISTENT)
        elif not row.competitor_active:
            blockers.append(BracketReadinessBlocker.COMPETITOR_INACTIVE)

    # Afiliacion: exige representacion de club y apuntar a ese club y a ese competidor
    # (``registrations._resolve_affiliation``). El FK solo comprueba que la fila existe.
    if row.affiliation_id is not None and (
        row.representation != "CLUB"
        or row.sports_club_id is None
        or row.affiliation_competitor_id != row.competitor_id
        or row.affiliation_sports_club_id != row.sports_club_id
    ):
        blockers.append(BracketReadinessBlocker.REGISTRATION_INCONSISTENT)

    category_blocker = _category_blocker(row)
    if category_blocker is not None:
        blockers.append(category_blocker)

    # §9: los bloqueos del motor de clasificacion pesan como los demas, salvo que un override
    # OWNER valido y auditado los haya legitimado.
    if row.classification_blockers and not row.overridden:
        blockers.extend(row.classification_blockers)

    return tuple(sorted(set(blockers), key=lambda blocker: _BLOCKER_ORDER[blocker]))


def _counted_issues(
    counts: dict[BracketReadinessBlocker, int], *, category_key: str | None
) -> list[ReadinessIssue]:
    """Un bloqueo por codigo con su recuento, en el orden canonico del vocabulario."""
    return [
        ReadinessIssue(code=code, count=count, category_key=category_key)
        for code, count in counts.items()
        if count
    ]


def _category_readiness(group: _CategoryGroup, *, tenant_club_id: ClubId) -> CategoryReadiness:
    """Lista de una categoria: minimo de confirmados y coherencia de sus confirmadas."""
    category_key = group.rows[0].category_key
    counts: dict[BracketReadinessBlocker, int] = {}
    for row in group.rows:
        for blocker in _row_blockers(row, tenant_club_id=tenant_club_id):
            counts[blocker] = counts.get(blocker, 0) + 1

    # §9: solo cuentan las confirmadas clasificables (o con override OWNER). Una confirmada
    # cuya clasificacion no resuelve no es un participante del cuadro.
    counted = tuple(row for row in group.rows if row.counts_toward_minimum)
    issues: list[ReadinessIssue] = []
    if len(counted) < MINIMUM_ENTRANTS:
        issues.append(
            ReadinessIssue(
                code=BracketReadinessBlocker.CATEGORY_NOT_ENOUGH_COMPETITORS,
                count=len(counted),
                category_key=category_key,
                required_minimum=MINIMUM_ENTRANTS,
            )
        )
    issues.extend(_counted_issues(counts, category_key=category_key))

    label = next(iter(group.labels)) if len(group.labels) == 1 else None
    return CategoryReadiness(
        category_key=category_key,
        category_label=label,
        confirmed_count=len(counted),
        required_minimum=MINIMUM_ENTRANTS,
        issues=tuple(sorted(issues, key=_issue_sort_key)),
    )


def _frozen_category_issues(rows: Iterable[RegistrationReadinessRow]) -> list[ReadinessIssue]:
    """Una clave no puede significar dos cosas en el mismo torneo (etiqueta congelada).

    Se miran todas las filas -- incluidas las corregidas --, como hace la comprobacion del alta:
    la etiqueta se congela por torneo, no por inscripcion vigente. Orden determinista por clave.
    """
    labels: dict[str, set[str]] = {}
    for row in rows:
        if row.category_key is not None and row.category_label is not None:
            labels.setdefault(row.category_key, set()).add(row.category_label)
    return [
        ReadinessIssue(code=BracketReadinessBlocker.INVALID_CATEGORY, count=1, category_key=key)
        for key in sorted(key for key, values in labels.items() if len(values) > 1)
    ]


def _bracket_structure_issues(brackets: Sequence[ExistingBracket]) -> list[ReadinessIssue]:
    """Cuadros ya generados que no se pueden regenerar: su reparto ya no esta en nuestras manos.

    Un cuadro sin actividad encima **si** se puede regenerar (F2C es explicito y la generacion es
    idempotente), asi que no bloquea; lo que bloquea es uno con algo encima (resultado, ganador,
    pista u horario) o con entrantes que no admiten reparto.
    """
    frozen = sum(1 for bracket in brackets if bracket.is_frozen)
    return (
        [ReadinessIssue(code=BracketReadinessBlocker.BRACKET_ALREADY_EXISTS, count=frozen)]
        if frozen
        else []
    )


def evaluate_bracket_readiness(
    *,
    tournament_id: TournamentId,
    tenant_club_id: ClubId,
    window_state: RegistrationWindowState,
    rows: Sequence[RegistrationReadinessRow],
    existing_brackets: Sequence[ExistingBracket] = (),
) -> BracketReadiness:
    """Revisa el estado de las inscripciones del torneo y decide si esta listo para generar.

    Funcion **pura**: no toca la base de datos y no depende del orden en que lleguen las filas (las
    ordena ella). El llamante aporta las lecturas y la ruta proyecta el resultado a la respuesta.
    """
    current = [row for row in rows if row.is_current]
    summary = _summary_of(current)

    groups: dict[str | None, list[RegistrationReadinessRow]] = {}
    for row in current:
        if row.status == "CONFIRMED":
            groups.setdefault(row.category_key, []).append(row)

    categories: list[CategoryReadiness] = []
    for category_key in sorted(groups, key=_category_sort_key):
        group_rows = tuple(sorted(groups[category_key], key=lambda row: row.registration_id))
        labels = frozenset(
            row.category_label for row in group_rows if row.category_label is not None
        )
        categories.append(
            _category_readiness(
                _CategoryGroup(rows=group_rows, labels=labels), tenant_club_id=tenant_club_id
            )
        )

    issues: list[ReadinessIssue] = []
    if window_state == "OPEN":
        issues.append(ReadinessIssue(code=BracketReadinessBlocker.REGISTRATION_WINDOW_OPEN))
    if summary.draft:
        issues.append(
            ReadinessIssue(code=BracketReadinessBlocker.REGISTRATIONS_PENDING, count=summary.draft)
        )
    issues.extend(issue for category in categories for issue in category.issues)
    issues.extend(_frozen_category_issues(rows))
    issues.extend(_bracket_structure_issues(existing_brackets))

    return BracketReadiness(
        tournament_id=tournament_id,
        ready=not issues,
        window_state=window_state,
        summary=summary,
        categories=tuple(categories),
        issues=tuple(sorted(issues, key=_issue_sort_key)),
    )
