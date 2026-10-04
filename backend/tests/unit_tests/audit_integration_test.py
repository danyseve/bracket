"""S3.3c-2 — integracion del catalogo cerrado de motivos con la auditoria.

TDD: este modulo se escribio y ejecuto **antes** de la integracion y fallo en rojo (las diez
operaciones de auditoria todavia aceptaban ``reason`` de texto libre, los modulos de dominio
conservaban sus motivos por defecto propios y ``domain_change_log`` no tenia ``reason_code`` ni
``reason_note``); la implementacion posterior lo lleva a verde.

Contrato que se protege:

* **cobertura**: el catalogo cubre *exactamente* las diez parejas (entidad, accion) que las
  operaciones escriben, ni una mas ni una menos;
* **vocabulario cerrado**: las operaciones reciben ``reason_code`` (y ``reason_note``) y ya no
  aceptan texto libre del llamador;
* **texto canonico**: el unico texto que se conserva como explicacion es el derivado del codigo,
  y ninguno contiene formas de dato personal;
* **motivos historicos intactos**: los textos canonicos reproducen literalmente los motivos por
  defecto de S2 y S3.2, de modo que ningun evento anterior cambia de texto.
"""

from __future__ import annotations

import inspect
import re
from typing import Any

import pytest
from sqlalchemy import CheckConstraint, String, Text

from bracket import schema
from bracket.logic import audit_reasons, competitors, registrations
from bracket.sql import domain_writes

# (modulo, funcion, entidad, accion, exige codigo explicito). Las diez operaciones que escriben
# auditoria hoy: cuatro en competidores y seis en inscripciones.
AUDIT_OPERATIONS: tuple[tuple[Any, str, str, str, bool], ...] = (
    (competitors, "create_competitor", audit_reasons.SCOPE_COMPETITOR, "CREATE", False),
    (
        competitors,
        "update_competitor_display_name",
        audit_reasons.SCOPE_COMPETITOR,
        "UPDATE",
        False,
    ),
    (competitors, "deactivate_competitor", audit_reasons.SCOPE_COMPETITOR, "DEACTIVATE", False),
    (competitors, "activate_competitor", audit_reasons.SCOPE_COMPETITOR, "ACTIVATE", False),
    (registrations, "create_registration", audit_reasons.SCOPE_REGISTRATION, "CREATE", False),
    (registrations, "update_registration_draft", audit_reasons.SCOPE_REGISTRATION, "UPDATE", False),
    (registrations, "confirm_registration", audit_reasons.SCOPE_REGISTRATION, "CONFIRM", False),
    (registrations, "withdraw_registration", audit_reasons.SCOPE_REGISTRATION, "WITHDRAW", True),
    (registrations, "reinstate_registration", audit_reasons.SCOPE_REGISTRATION, "REINSTATE", True),
    (
        registrations,
        "disqualify_registration",
        audit_reasons.SCOPE_REGISTRATION,
        "DISQUALIFY",
        True,
    ),
)

# Motivos por defecto historicos (S2 y S3.2), congelados aqui: son los textos que ya estan
# escritos en el historico de laboratorio y que la integracion no puede cambiar.
HISTORICAL_DEFAULTS: dict[tuple[str, str], str] = {
    (audit_reasons.SCOPE_COMPETITOR, "CREATE"): "alta de competidor",
    (audit_reasons.SCOPE_COMPETITOR, "UPDATE"): "actualizacion de datos basicos",
    (audit_reasons.SCOPE_COMPETITOR, "DEACTIVATE"): "baja logica de competidor",
    (audit_reasons.SCOPE_COMPETITOR, "ACTIVATE"): "reactivacion de competidor",
    (audit_reasons.SCOPE_REGISTRATION, "CREATE"): "alta de inscripcion",
    (audit_reasons.SCOPE_REGISTRATION, "UPDATE"): "edicion de borrador de inscripcion",
    (audit_reasons.SCOPE_REGISTRATION, "CONFIRM"): "confirmacion de inscripcion",
}

REASON_CODE_COLUMN = "reason_code"
REASON_NOTE_COLUMN = "reason_note"
REASON_CODE_CATALOG_CHECK = "ck_domain_change_log_reason_code_catalog"
REASON_CODE_ACTION_CHECK = "ck_domain_change_log_reason_code_action"
REASON_NOTE_CHECK = "ck_domain_change_log_reason_note"
WRITER_PARAMETERS = [
    "entity",
    "entity_id",
    "action",
    "changed_fields",
    "actor_user_id",
    "actor_label",
    "reason",
    "reason_code",
    "reason_note",
    "tenant_club_id",
]


def _check_constraints() -> dict[str, CheckConstraint]:
    return {
        str(constraint.name): constraint
        for constraint in schema.domain_change_log.constraints
        if isinstance(constraint, CheckConstraint)
    }


def _quoted_upper_tokens(sql: str) -> set[str]:
    """Codigos entrecomillados en mayusculas de una expresion CHECK."""
    return set(re.findall(r"'([A-Z][A-Z_]{2,31})'", sql))


def _triples(sql: str) -> set[tuple[str, str, str]]:
    """Ternas (entidad, accion, codigo) de una expresion CHECK."""
    return set(re.findall(r"\(\s*'(\w+)',\s*'(\w+)',\s*'([A-Z_]+)'\s*\)", sql))


# --- cobertura del catalogo ---------------------------------------------------------------------


def test_catalog_covers_exactly_the_ten_audit_operations() -> None:
    written = {(scope, action) for _, _, scope, action, _ in AUDIT_OPERATIONS}

    assert len(AUDIT_OPERATIONS) == 10
    assert set(audit_reasons.CATALOG) == written, (
        "el catalogo no cubre exactamente esas diez parejas"
    )
    for _, _, scope, action, _ in AUDIT_OPERATIONS:
        assert audit_reasons.allowed_codes(scope=scope, action=action)


@pytest.mark.parametrize(
    ("module", "function", "scope", "action", "code_required"),
    AUDIT_OPERATIONS,
    ids=[f"{scope}-{action}" for _, _, scope, action, _ in AUDIT_OPERATIONS],
)
def test_operations_require_a_code_when_the_catalog_has_no_default(
    module: Any, function: str, scope: str, action: str, code_required: bool
) -> None:
    parameters = inspect.signature(getattr(module, function)).parameters

    assert REASON_CODE_COLUMN in parameters, "la operacion recibe el codigo cerrado"
    assert "reason" not in parameters, "la operacion ya no acepta texto libre"
    if code_required:
        assert parameters[REASON_CODE_COLUMN].default is inspect.Parameter.empty, (
            "las acciones sin codigo por defecto exigen el codigo explicitamente"
        )
    else:
        assert parameters[REASON_CODE_COLUMN].default is None
    has_default = (scope, action) in audit_reasons.DEFAULT_CODES
    assert has_default is not code_required, "obligatoriedad del codigo y del catalogo coinciden"


@pytest.mark.parametrize(
    ("module", "function", "scope", "action", "code_required"),
    AUDIT_OPERATIONS,
    ids=[f"{scope}-{action}" for _, _, scope, action, _ in AUDIT_OPERATIONS],
)
def test_operations_accept_an_optional_note(
    module: Any, function: str, scope: str, action: str, code_required: bool
) -> None:
    parameters = inspect.signature(getattr(module, function)).parameters

    assert REASON_NOTE_COLUMN in parameters
    assert parameters[REASON_NOTE_COLUMN].default is None


@pytest.mark.parametrize(
    ("module", "names"),
    [
        (competitors, ("_DEFAULT_REASONS", "_normalize_reason", "MAX_REASON_LENGTH")),
        (
            registrations,
            (
                "_DEFAULT_REASONS",
                "_normalize_reason",
                "_normalize_required_reason",
                "MAX_REASON_LENGTH",
            ),
        ),
    ],
    ids=["competitors", "registrations"],
)
def test_domain_modules_no_longer_keep_free_text_reason_paths(
    module: Any, names: tuple[str, ...]
) -> None:
    for name in names:
        assert not hasattr(module, name), f"{module.__name__} conserva la via de texto libre {name}"


def test_writer_carries_the_closed_reason_columns() -> None:
    signature = inspect.signature(domain_writes.sql_insert_domain_change_log)
    parameters = list(signature.parameters)

    assert parameters == WRITER_PARAMETERS
    for name in ("reason_code", "reason_note"):
        assert signature.parameters[name].default is inspect.Parameter.empty, (
            "el escritor exige declarar el codigo y la nota en cada evento"
        )


# --- esquema ------------------------------------------------------------------------------------


def test_table_declares_the_two_nullable_columns() -> None:
    code = schema.domain_change_log.c[REASON_CODE_COLUMN]
    note = schema.domain_change_log.c[REASON_NOTE_COLUMN]

    assert isinstance(code.type, String) and code.type.length == 32
    assert code.nullable is True, "NULL = evento anterior a la migracion, nunca 'sin motivo'"
    assert isinstance(note.type, Text)
    assert note.nullable is True


def test_table_declares_the_catalog_coherence_and_note_checks() -> None:
    constraints = _check_constraints()

    assert set(constraints) >= {
        REASON_CODE_CATALOG_CHECK,
        REASON_CODE_ACTION_CHECK,
        REASON_NOTE_CHECK,
    }
    assert _quoted_upper_tokens(str(constraints[REASON_CODE_CATALOG_CHECK].sqltext)) == set(
        audit_reasons.REASON_CODES
    )

    expected_pairs = {
        (scope, action, code)
        for (scope, action), codes in audit_reasons.CATALOG.items()
        for code in codes
    }
    assert _triples(str(constraints[REASON_CODE_ACTION_CHECK].sqltext)) == expected_pairs

    note_sql = str(constraints[REASON_NOTE_CHECK].sqltext)
    assert _quoted_upper_tokens(note_sql) == set(audit_reasons.NOTE_ALLOWED_CODES)
    assert str(audit_reasons.MAX_REASON_NOTE_LENGTH) in note_sql


# --- textos canonicos ---------------------------------------------------------------------------


def test_canonical_texts_reproduce_the_historical_defaults() -> None:
    for (scope, action), text in HISTORICAL_DEFAULTS.items():
        code = audit_reasons.DEFAULT_CODES[(scope, action)]

        assert audit_reasons.canonical_text(scope=scope, action=action, code=code) == text, (
            f"{scope}/{action} cambio de texto respecto al historico"
        )


def test_every_canonical_text_is_poor_in_data_shaped_tokens() -> None:
    for (scope, action, code), text in audit_reasons.CANONICAL_TEXTS.items():
        assert not audit_reasons.find_personal_data(text), f"{scope}/{action}/{code}"
        assert not re.search(r"\d", text), f"{scope}/{action}/{code} contiene digitos"
        assert "@" not in text and "://" not in text
