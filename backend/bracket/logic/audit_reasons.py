"""Catalogo cerrado de motivos de auditoria (S3.3c-1, LAB ONLY).

Modulo **interno y todavia no conectado**: describe que motivo puede acompanar a
cada evento de auditoria y como se valida, pero no escribe auditoria, no conoce
la capa de persistencia y no cambia las firmas de las operaciones O1-O6. La
conexion real (columnas ``reason_code`` / ``reason_note``) es S3.3c-2.

Por que un catalogo cerrado
---------------------------

``reason`` es hoy texto libre de hasta 500 caracteres: es la via por la que un
dato personal acaba en una tabla de auditoria de forma permanente. En vez de
intentar limpiar despues, el contrato pasa a ser:

* un ``reason_code`` **cerrado por accion**: solo se acepta lo que el catalogo
  permite para esa accion concreta;
* un **texto canonico** derivado del codigo: es lo que se conserva como
  explicacion legible y nunca contiene datos personales;
* una ``reason_note`` **opcional y restringida**: solo los codigos de
  :data:`NOTE_ALLOWED_CODES` admiten nota, con limites estrictos.

Los codigos ``INJURY`` y ``DISCIPLINE`` se descartaron por decision expresa:
inducen a registrar salud o disciplina, que no son necesarias para operar el
torneo. La retirada por lesion se registra como retirada solicitada, sin causa.

Que **no** garantiza este modulo
--------------------------------

Las heuristicas de :func:`find_personal_data` (correo, telefono, URL, documento,
IBAN) son proporcionadas, **no** un saneo universal: no reconocen un dato
escrito con palabras, abreviado o partido, y pueden rechazar texto legitimo
(por ejemplo una fecha seguida de otro numero). Ninguna expresion regular
demuestra la ausencia de datos personales: el control fuerte es el vocabulario
cerrado y la nota corta, no el filtro.

Compatibilidad
--------------

El catalogo reproduce **exactamente** los motivos por defecto de S2 y S3.2
(``competitors._DEFAULT_REASONS`` y ``registrations._DEFAULT_REASONS``), de modo
que la integracion posterior no cambia el texto de ningun evento historico. Los
motivos obligatorios de hoy (``WITHDRAW`` / ``REINSTATE`` / ``DISQUALIFY``, sin
valor por defecto) siguen siendo obligatorios: son justo los que dejan de
admitir texto libre.
"""

from __future__ import annotations

import re
import unicodedata
from types import MappingProxyType
from typing import NamedTuple

__all__ = [
    "SCOPE_COMPETITOR",
    "SCOPE_REGISTRATION",
    "MAX_REASON_NOTE_LENGTH",
    "REASON_CODES",
    "CATALOG",
    "CANONICAL_TEXTS",
    "DEFAULT_CODES",
    "NOTE_ALLOWED_CODES",
    "AuditReason",
    "AuditReasonError",
    "UnknownAuditActionError",
    "UnknownReasonCodeError",
    "ReasonCodeNotAllowedError",
    "MissingReasonCodeError",
    "ReasonNoteNotAllowedError",
    "InvalidReasonNoteError",
    "AuditCatalogError",
    "allowed_codes",
    "note_is_allowed",
    "canonical_text",
    "validate_reason_code",
    "validate_reason_note",
    "find_personal_data",
    "build_audit_reason",
]

#: Entidad de auditoria del competidor (``domain_change_log.entity``).
SCOPE_COMPETITOR = "competitor"
#: Entidad de auditoria de la inscripcion (``domain_change_log.entity``).
SCOPE_REGISTRATION = "tournament_registration"

#: Longitud maxima de la nota opcional, medida despues de normalizar a NFC.
MAX_REASON_NOTE_LENGTH = 200

_CATALOG: dict[tuple[str, str], tuple[str, ...]] = {
    (SCOPE_COMPETITOR, "CREATE"): ("PLANNED_ENTRY",),
    (SCOPE_COMPETITOR, "UPDATE"): ("DATA_CORRECTION",),
    (SCOPE_COMPETITOR, "DEACTIVATE"): (
        "REQUESTED_BY_ATHLETE",
        "DUPLICATE",
        "DATA_ERROR",
        "ADMINISTRATIVE",
    ),
    (SCOPE_COMPETITOR, "ACTIVATE"): (
        "REQUESTED_BY_ATHLETE",
        "DUPLICATE",
        "DATA_ERROR",
        "ADMINISTRATIVE",
    ),
    (SCOPE_REGISTRATION, "CREATE"): ("PLANNED_ENTRY",),
    (SCOPE_REGISTRATION, "UPDATE"): (
        "DATA_CORRECTION",
        "CATEGORY_CHANGE",
        "REPRESENTATION_CHANGE",
    ),
    (SCOPE_REGISTRATION, "CONFIRM"): ("READY", "DATA_VERIFIED"),
    (SCOPE_REGISTRATION, "WITHDRAW"): (
        "WITHDRAWAL_REQUEST",
        "DUPLICATE",
        "ELIGIBILITY_LOST",
        "ADMINISTRATIVE",
    ),
    (SCOPE_REGISTRATION, "REINSTATE"): (
        "MISTAKEN_WITHDRAWAL",
        "ELIGIBILITY_REGAINED",
        "ADMINISTRATIVE",
    ),
    (SCOPE_REGISTRATION, "DISQUALIFY"): (
        "SCHEDULING_NO_SHOW",
        "RULE_VIOLATION",
        "ELIGIBILITY",
        "ADMINISTRATIVE",
    ),
}

#: Codigos admitidos por par (entidad, accion). Solo lectura.
CATALOG: MappingProxyType[tuple[str, str], tuple[str, ...]] = MappingProxyType(_CATALOG)

#: Todos los codigos del catalogo.
REASON_CODES: frozenset[str] = frozenset(code for codes in _CATALOG.values() for code in codes)

#: Texto canonico que se conserva como explicacion legible del evento.
CANONICAL_TEXTS: MappingProxyType[tuple[str, str, str], str] = MappingProxyType(
    {
        (SCOPE_COMPETITOR, "CREATE", "PLANNED_ENTRY"): "alta de competidor",
        (SCOPE_COMPETITOR, "UPDATE", "DATA_CORRECTION"): "actualizacion de datos basicos",
        (SCOPE_COMPETITOR, "DEACTIVATE", "REQUESTED_BY_ATHLETE"): (
            "baja solicitada por el competidor"
        ),
        (SCOPE_COMPETITOR, "DEACTIVATE", "DUPLICATE"): "baja por duplicado",
        (SCOPE_COMPETITOR, "DEACTIVATE", "DATA_ERROR"): "baja por error de datos",
        (SCOPE_COMPETITOR, "DEACTIVATE", "ADMINISTRATIVE"): "baja logica de competidor",
        (SCOPE_COMPETITOR, "ACTIVATE", "REQUESTED_BY_ATHLETE"): (
            "reactivacion solicitada por el competidor"
        ),
        (SCOPE_COMPETITOR, "ACTIVATE", "DUPLICATE"): "reactivacion por duplicado",
        (SCOPE_COMPETITOR, "ACTIVATE", "DATA_ERROR"): "reactivacion por error de datos",
        (SCOPE_COMPETITOR, "ACTIVATE", "ADMINISTRATIVE"): "reactivacion de competidor",
        (SCOPE_REGISTRATION, "CREATE", "PLANNED_ENTRY"): "alta de inscripcion",
        (SCOPE_REGISTRATION, "UPDATE", "DATA_CORRECTION"): ("edicion de borrador de inscripcion"),
        (SCOPE_REGISTRATION, "UPDATE", "CATEGORY_CHANGE"): ("cambio de categoria en el borrador"),
        (SCOPE_REGISTRATION, "UPDATE", "REPRESENTATION_CHANGE"): (
            "cambio de representacion en el borrador"
        ),
        (SCOPE_REGISTRATION, "CONFIRM", "READY"): "confirmacion de inscripcion",
        (SCOPE_REGISTRATION, "CONFIRM", "DATA_VERIFIED"): ("confirmacion con datos verificados"),
        (SCOPE_REGISTRATION, "WITHDRAW", "WITHDRAWAL_REQUEST"): "retirada solicitada",
        (SCOPE_REGISTRATION, "WITHDRAW", "DUPLICATE"): ("retirada por inscripcion duplicada"),
        (SCOPE_REGISTRATION, "WITHDRAW", "ELIGIBILITY_LOST"): (
            "retirada por perdida de elegibilidad"
        ),
        (SCOPE_REGISTRATION, "WITHDRAW", "ADMINISTRATIVE"): "retirada administrativa",
        (SCOPE_REGISTRATION, "REINSTATE", "MISTAKEN_WITHDRAWAL"): (
            "readmision por retirada erronea"
        ),
        (SCOPE_REGISTRATION, "REINSTATE", "ELIGIBILITY_REGAINED"): (
            "readmision por elegibilidad recuperada"
        ),
        (SCOPE_REGISTRATION, "REINSTATE", "ADMINISTRATIVE"): "readmision administrativa",
        (SCOPE_REGISTRATION, "DISQUALIFY", "SCHEDULING_NO_SHOW"): (
            "descalificacion por incomparecencia"
        ),
        (SCOPE_REGISTRATION, "DISQUALIFY", "RULE_VIOLATION"): (
            "descalificacion por incumplimiento de reglas"
        ),
        (SCOPE_REGISTRATION, "DISQUALIFY", "ELIGIBILITY"): ("descalificacion por inelegibilidad"),
        (SCOPE_REGISTRATION, "DISQUALIFY", "ADMINISTRATIVE"): ("descalificacion administrativa"),
    }
)

#: Codigo que se aplica cuando la operacion admite omitir el motivo. Reproduce
#: los motivos por defecto historicos. Las acciones que **no** aparecen aqui
#: exigen motivo explicito, igual que hoy.
DEFAULT_CODES: MappingProxyType[tuple[str, str], str] = MappingProxyType(
    {
        (SCOPE_COMPETITOR, "CREATE"): "PLANNED_ENTRY",
        (SCOPE_COMPETITOR, "UPDATE"): "DATA_CORRECTION",
        (SCOPE_COMPETITOR, "DEACTIVATE"): "ADMINISTRATIVE",
        (SCOPE_COMPETITOR, "ACTIVATE"): "ADMINISTRATIVE",
        (SCOPE_REGISTRATION, "CREATE"): "PLANNED_ENTRY",
        (SCOPE_REGISTRATION, "UPDATE"): "DATA_CORRECTION",
        (SCOPE_REGISTRATION, "CONFIRM"): "READY",
    }
)

#: Unicos codigos que admiten nota libre. El resto no acepta texto del llamador.
NOTE_ALLOWED_CODES: frozenset[str] = frozenset({"ADMINISTRATIVE", "RULE_VIOLATION", "DATA_ERROR"})

_CODE_SHAPE = re.compile(r"[A-Z][A-Z_]{2,31}")

# Heuristicas **proporcionadas** de dato personal evidente: solo la forma
# reconocible. El orden importa: define el orden del informe de
# :func:`find_personal_data`.
_PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"[^\s@]+@[^\s@]+\.[A-Za-z]{2,}")),
    ("telefono", re.compile(r"(?:(?:\+|00)\d{1,3}[\s.-]?)?(?:\d[\s.-]?){8,}\d")),
    ("url", re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)),
    ("documento", re.compile(r"\b(?:\d{8}[A-HJ-NP-TV-Z]|[XYZ]\d{7}[A-HJ-NP-TV-Z])\b")),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}(?:[ -]?[A-Z0-9]{3,4}){3,7}\b")),
)


class AuditReasonError(ValueError):
    """Entrada de motivo invalida. Nunca deja el evento escrito."""


class UnknownAuditActionError(AuditReasonError):
    """La pareja (entidad, accion) no pertenece al catalogo."""


class UnknownReasonCodeError(AuditReasonError):
    """El codigo no existe en el catalogo."""


class ReasonCodeNotAllowedError(AuditReasonError):
    """El codigo existe, pero no para esta accion."""


class MissingReasonCodeError(AuditReasonError):
    """La accion exige motivo explicito y no se ha dado."""


class ReasonNoteNotAllowedError(AuditReasonError):
    """El codigo no admite nota libre."""


class InvalidReasonNoteError(AuditReasonError):
    """La nota no cumple el contrato (forma, longitud o contenido)."""


class AuditCatalogError(RuntimeError):
    """Inconsistencia interna del catalogo: es un fallo de programacion."""


class AuditReason(NamedTuple):
    """Motivo resuelto: codigo cerrado, texto canonico y nota opcional."""

    code: str
    text: str
    note: str | None


def allowed_codes(*, scope: str, action: str) -> tuple[str, ...]:
    """Codigos admitidos para una accion, en el orden del catalogo."""

    codes = CATALOG.get((scope, action))
    if codes is None:
        raise UnknownAuditActionError(f"accion de auditoria desconocida: {scope}/{action}")
    return codes


def note_is_allowed(code: str) -> bool:
    """Indica si el codigo admite nota libre."""

    return code in NOTE_ALLOWED_CODES


def find_personal_data(text: str) -> tuple[str, ...]:
    """Categorias de dato personal evidente presentes en el texto.

    Devuelve **nombres de categoria**, nunca el valor encontrado, de modo que el
    mensaje de error no reproduce el dato que se pretende rechazar.
    """

    return tuple(kind for kind, pattern in _PII_PATTERNS if pattern.search(text))


def validate_reason_code(*, scope: str, action: str, code: object) -> str:
    """Valida el codigo contra el catalogo de su accion."""

    allowed = allowed_codes(scope=scope, action=action)
    if not isinstance(code, str) or code not in REASON_CODES:
        raise UnknownReasonCodeError(_describe_rejected_code(code))
    if code not in allowed:
        raise ReasonCodeNotAllowedError(f"el motivo {code} no esta permitido para {scope}/{action}")
    return code


def validate_reason_note(*, code: str, note: object) -> str | None:
    """Valida la nota opcional: permiso del codigo, forma y contenido.

    Devuelve la nota normalizada a NFC y sin espacios exteriores, o ``None`` si
    no se ha dado nota. Nunca devuelve la nota cuando es invalida.
    """

    if note is None:
        return None
    if code not in NOTE_ALLOWED_CODES:
        raise ReasonNoteNotAllowedError(f"el motivo {code} no admite nota")
    if not isinstance(note, str):
        raise InvalidReasonNoteError("la nota debe ser texto")
    normalized = unicodedata.normalize("NFC", note).strip()
    if not normalized:
        raise InvalidReasonNoteError("la nota no puede estar vacia")
    if not normalized.isprintable():
        raise InvalidReasonNoteError("la nota contiene caracteres no imprimibles")
    if len(normalized) > MAX_REASON_NOTE_LENGTH:
        raise InvalidReasonNoteError(f"la nota supera {MAX_REASON_NOTE_LENGTH} caracteres")
    kinds = find_personal_data(normalized)
    if kinds:
        raise InvalidReasonNoteError(
            f"la nota parece incluir datos personales evidentes: {', '.join(kinds)}"
        )
    return normalized


def canonical_text(*, scope: str, action: str, code: str) -> str:
    """Texto legible que se conserva del codigo, dentro de su accion."""

    allowed = validate_reason_code(scope=scope, action=action, code=code)
    text = CANONICAL_TEXTS.get((scope, action, allowed))
    if text is None:
        raise AuditCatalogError(f"catalogo inconsistente: {scope}/{action}/{allowed}")
    return text


def build_audit_reason(
    *,
    scope: str,
    action: str,
    code: object = None,
    note: object = None,
) -> AuditReason:
    """Resuelve el motivo completo de un evento, o rechaza la entrada.

    Si la accion admite omitir el motivo se aplica su codigo por defecto; si
    exige motivo explicito y no se ha dado, la operacion no puede continuar.
    """

    allowed_codes(scope=scope, action=action)
    if code is None:
        default = DEFAULT_CODES.get((scope, action))
        if default is None:
            raise MissingReasonCodeError(f"la accion {action} exige un motivo explicito")
        code = default
    resolved = validate_reason_code(scope=scope, action=action, code=code)
    return AuditReason(
        code=resolved,
        text=canonical_text(scope=scope, action=action, code=resolved),
        note=validate_reason_note(code=resolved, note=note),
    )


def _describe_rejected_code(code: object) -> str:
    """Mensaje que nunca reproduce el valor recibido si no tiene forma de codigo.

    Un valor sin forma de codigo puede ser cualquier cosa (incluso una nota con
    datos personales enviada en el campo equivocado): no se repite en el error.
    """

    if isinstance(code, str) and _CODE_SHAPE.fullmatch(code):
        return f"motivo de auditoria desconocido: {code}"
    return "motivo de auditoria desconocido: valor sin forma de codigo"
