"""Contrato del catalogo cerrado de motivos de auditoria (S3.3c-1, LAB ONLY).

Modulo **interno y no conectado** todavia: no escribe auditoria, no toca
``domain_change_log``, no cambia las firmas de O1-O6 y no publica ningun endpoint.
Estas pruebas fijan el contrato que consumira S3.3c-2.

Cubre:

A. invariantes y contenido exacto del catalogo aprobado;
B. reglas por accion (un codigo de otra accion se rechaza);
C. codigos desconocidos y acciones desconocidas;
D. notas: solo en codigos autorizados;
E. limites de la nota (200 exactos, 201, vacia, solo espacios, ausente);
F. normalizacion Unicode NFC;
G. caracteres de control;
H. datos personales evidentes (correo, telefono, URL, documento, IBAN) y los
   casos legitimos que **no** deben rechazarse;
I. motivos obligatorios frente a los que conservan valor por defecto;
J. compatibilidad con los motivos por defecto historicos de S2 y S3.2;
K. ausencia de conexion con la auditoria y de cambios en contratos existentes;
L. construccion completa y ausencia de fugas en los mensajes de error.

**No se afirma** que estas heuristicas eliminen la PII de forma universal: solo
rechazan las formas mas evidentes y tienen falsos positivos y negativos
documentados.
"""

from __future__ import annotations

import inspect
import re
import unicodedata
from pathlib import Path

import pytest

from bracket.logic import audit_reasons, competitors, registrations

CATALOG_PAIRS = tuple(audit_reasons.CATALOG)

APPROVED_CATALOG: dict[tuple[str, str], tuple[str, ...]] = {
    ("competitor", "CREATE"): ("PLANNED_ENTRY",),
    ("competitor", "UPDATE"): ("DATA_CORRECTION",),
    ("competitor", "DEACTIVATE"): (
        "REQUESTED_BY_ATHLETE",
        "DUPLICATE",
        "DATA_ERROR",
        "ADMINISTRATIVE",
    ),
    ("competitor", "ACTIVATE"): (
        "REQUESTED_BY_ATHLETE",
        "DUPLICATE",
        "DATA_ERROR",
        "ADMINISTRATIVE",
    ),
    ("tournament_registration", "CREATE"): ("PLANNED_ENTRY",),
    ("tournament_registration", "UPDATE"): (
        "DATA_CORRECTION",
        "CATEGORY_CHANGE",
        "REPRESENTATION_CHANGE",
    ),
    ("tournament_registration", "CONFIRM"): ("READY", "DATA_VERIFIED"),
    ("tournament_registration", "WITHDRAW"): (
        "WITHDRAWAL_REQUEST",
        "DUPLICATE",
        "ELIGIBILITY_LOST",
        "ADMINISTRATIVE",
    ),
    ("tournament_registration", "REINSTATE"): (
        "MISTAKEN_WITHDRAWAL",
        "ELIGIBILITY_REGAINED",
        "ADMINISTRATIVE",
    ),
    ("tournament_registration", "DISQUALIFY"): (
        "SCHEDULING_NO_SHOW",
        "RULE_VIOLATION",
        "ELIGIBILITY",
        "ADMINISTRATIVE",
    ),
}


# ---------------------------------------------------------------------------
# A. Invariantes y contenido exacto del catalogo
# ---------------------------------------------------------------------------


def test_catalog_matches_the_approved_table() -> None:
    assert audit_reasons.CATALOG == APPROVED_CATALOG
    assert set(audit_reasons.REASON_CODES) == {
        code for codes in APPROVED_CATALOG.values() for code in codes
    }


def test_catalog_is_not_empty() -> None:
    assert CATALOG_PAIRS
    assert audit_reasons.REASON_CODES


@pytest.mark.parametrize(("scope", "action"), CATALOG_PAIRS)
def test_every_catalog_entry_has_a_canonical_text(scope: str, action: str) -> None:
    for code in audit_reasons.CATALOG[(scope, action)]:
        text = audit_reasons.canonical_text(scope=scope, action=action, code=code)
        assert text
        assert text == text.strip()


@pytest.mark.parametrize(("scope", "action"), CATALOG_PAIRS)
def test_every_allowed_code_is_accepted(scope: str, action: str) -> None:
    for code in audit_reasons.CATALOG[(scope, action)]:
        assert audit_reasons.validate_reason_code(scope=scope, action=action, code=code) == code


def test_catalog_has_no_duplicate_codes_per_entry() -> None:
    for entry in audit_reasons.CATALOG.values():
        assert len(entry) == len(set(entry))


def test_canonical_texts_are_unique_per_entry() -> None:
    for scope, action in CATALOG_PAIRS:
        texts = [
            audit_reasons.canonical_text(scope=scope, action=action, code=code)
            for code in audit_reasons.CATALOG[(scope, action)]
        ]
        assert len(texts) == len(set(texts))


def test_canonical_texts_table_has_no_orphans() -> None:
    expected = {
        (scope, action, code)
        for (scope, action), codes in APPROVED_CATALOG.items()
        for code in codes
    }
    assert set(audit_reasons.CANONICAL_TEXTS) == expected


def test_reason_codes_use_a_stable_shape() -> None:
    for code in audit_reasons.REASON_CODES:
        assert re.fullmatch(r"[A-Z][A-Z_]{2,31}", code), code


def test_removed_categories_cannot_come_back() -> None:
    forbidden = ("INJURY", "DISCIPLINE", "MEDICAL", "HEALTH", "SANCTION", "PREGNAN")
    for code in audit_reasons.REASON_CODES:
        assert not any(word in code for word in forbidden), code


def test_defaults_are_allowed_in_their_action() -> None:
    for (scope, action), code in audit_reasons.DEFAULT_CODES.items():
        assert code in audit_reasons.CATALOG[(scope, action)]


def test_default_codes_cover_exactly_the_optional_actions() -> None:
    assert set(audit_reasons.DEFAULT_CODES) == {
        ("competitor", "CREATE"),
        ("competitor", "UPDATE"),
        ("competitor", "DEACTIVATE"),
        ("competitor", "ACTIVATE"),
        ("tournament_registration", "CREATE"),
        ("tournament_registration", "UPDATE"),
        ("tournament_registration", "CONFIRM"),
    }


def test_note_allowed_codes_are_the_approved_three() -> None:
    assert audit_reasons.NOTE_ALLOWED_CODES == frozenset(
        {"ADMINISTRATIVE", "RULE_VIOLATION", "DATA_ERROR"}
    )
    assert audit_reasons.NOTE_ALLOWED_CODES <= audit_reasons.REASON_CODES


def test_catalog_entries_are_immutable() -> None:
    with pytest.raises(TypeError):
        audit_reasons.CATALOG[("competitor", "CREATE")] = ("OTRO",)  # type: ignore[index]


# ---------------------------------------------------------------------------
# B. Reglas por accion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "action", "code"),
    [
        ("competitor", "UPDATE", "CATEGORY_CHANGE"),
        ("competitor", "DEACTIVATE", "WITHDRAWAL_REQUEST"),
        ("tournament_registration", "UPDATE", "READY"),
        ("tournament_registration", "CONFIRM", "WITHDRAWAL_REQUEST"),
        ("tournament_registration", "WITHDRAW", "DATA_VERIFIED"),
    ],
)
def test_code_of_another_action_is_rejected(scope: str, action: str, code: str) -> None:
    with pytest.raises(audit_reasons.ReasonCodeNotAllowedError):
        audit_reasons.validate_reason_code(scope=scope, action=action, code=code)


def test_registration_update_only_accepts_its_three_codes() -> None:
    assert set(audit_reasons.allowed_codes(scope="tournament_registration", action="UPDATE")) == {
        "DATA_CORRECTION",
        "CATEGORY_CHANGE",
        "REPRESENTATION_CHANGE",
    }


def test_allowed_codes_returns_the_approved_order() -> None:
    assert audit_reasons.allowed_codes(scope="tournament_registration", action="DISQUALIFY") == (
        "SCHEDULING_NO_SHOW",
        "RULE_VIOLATION",
        "ELIGIBILITY",
        "ADMINISTRATIVE",
    )


# ---------------------------------------------------------------------------
# C. Codigos y acciones desconocidos
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["", "   ", "NO_EXISTE", "injury", "planned_entry", "INJURY"])
def test_unknown_code_is_rejected(code: str) -> None:
    with pytest.raises(audit_reasons.UnknownReasonCodeError):
        audit_reasons.validate_reason_code(
            scope="tournament_registration", action="CREATE", code=code
        )


@pytest.mark.parametrize("code", [None, 3, b"CREATE", ["CREATE"]])
def test_non_string_code_is_rejected(code: object) -> None:
    with pytest.raises(audit_reasons.UnknownReasonCodeError):
        audit_reasons.validate_reason_code(
            scope="tournament_registration", action="CREATE", code=code
        )


@pytest.mark.parametrize(
    ("scope", "action"), [("otra_cosa", "CREATE"), ("competitor", "BORRAR"), ("", "")]
)
def test_unknown_action_or_scope_is_rejected(scope: str, action: str) -> None:
    with pytest.raises(audit_reasons.UnknownAuditActionError):
        audit_reasons.allowed_codes(scope=scope, action=action)


# ---------------------------------------------------------------------------
# D. Notas: solo en codigos autorizados
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(audit_reasons.NOTE_ALLOWED_CODES))
def test_note_allowed_codes_accept_a_note(code: str) -> None:
    assert audit_reasons.validate_reason_note(code=code, note="gestion ordinaria") == (
        "gestion ordinaria"
    )


@pytest.mark.parametrize(
    "code", sorted(audit_reasons.REASON_CODES - audit_reasons.NOTE_ALLOWED_CODES)
)
def test_note_is_rejected_for_the_rest_of_the_codes(code: str) -> None:
    with pytest.raises(audit_reasons.ReasonNoteNotAllowedError):
        audit_reasons.validate_reason_note(code=code, note="texto libre")


def test_note_is_allowed_when_absent_on_any_code() -> None:
    for code in audit_reasons.REASON_CODES:
        assert audit_reasons.validate_reason_note(code=code, note=None) is None


def test_note_is_absent_by_default_on_an_optional_action() -> None:
    reason = audit_reasons.build_audit_reason(scope="tournament_registration", action="UPDATE")
    assert reason.note is None
    assert reason.code == "DATA_CORRECTION"
    assert reason.text == "edicion de borrador de inscripcion"


# ---------------------------------------------------------------------------
# E. Limites de la nota
# ---------------------------------------------------------------------------


def test_note_limit_is_200() -> None:
    assert audit_reasons.MAX_REASON_NOTE_LENGTH == 200


def test_note_at_the_limit_is_accepted() -> None:
    note = "a" * audit_reasons.MAX_REASON_NOTE_LENGTH
    assert audit_reasons.validate_reason_note(code="ADMINISTRATIVE", note=note) == note


def test_note_over_the_limit_is_rejected() -> None:
    note = "a" * (audit_reasons.MAX_REASON_NOTE_LENGTH + 1)
    with pytest.raises(audit_reasons.InvalidReasonNoteError):
        audit_reasons.validate_reason_note(code="ADMINISTRATIVE", note=note)


def test_note_limit_applies_after_normalization() -> None:
    note = "a" * audit_reasons.MAX_REASON_NOTE_LENGTH + "e\u0301"
    with pytest.raises(audit_reasons.InvalidReasonNoteError):
        audit_reasons.validate_reason_note(code="ADMINISTRATIVE", note=note)


@pytest.mark.parametrize("note", ["", "   ", "\t", "\n", "\u00a0"])
def test_empty_note_is_rejected(note: str) -> None:
    with pytest.raises(audit_reasons.InvalidReasonNoteError):
        audit_reasons.validate_reason_note(code="ADMINISTRATIVE", note=note)


@pytest.mark.parametrize("note", [3, b"x", ["x"], {"a": 1}])
def test_non_string_note_is_rejected(note: object) -> None:
    with pytest.raises(audit_reasons.InvalidReasonNoteError):
        audit_reasons.validate_reason_note(code="ADMINISTRATIVE", note=note)


def test_note_is_stripped() -> None:
    assert (
        audit_reasons.validate_reason_note(code="ADMINISTRATIVE", note="  gestion  ") == "gestion"
    )


# ---------------------------------------------------------------------------
# F. Unicode
# ---------------------------------------------------------------------------


def test_note_is_normalized_to_nfc() -> None:
    decomposed = "comite\u0301 de competicio\u0301n"
    result = audit_reasons.validate_reason_note(code="ADMINISTRATIVE", note=decomposed)
    assert result == unicodedata.normalize("NFC", decomposed)
    assert result == "comit\u00e9 de competici\u00f3n"
    assert unicodedata.is_normalized("NFC", result or "")
    assert len(result or "") == len("comite de competicion")


def test_combining_marks_do_not_inflate_the_length() -> None:
    result = audit_reasons.validate_reason_note(code="ADMINISTRATIVE", note="a\u0301")
    assert result == "\u00e1"
    assert len(result or "") == 1


def test_length_is_measured_after_normalization() -> None:
    note = "e\u0301" * audit_reasons.MAX_REASON_NOTE_LENGTH
    result = audit_reasons.validate_reason_note(code="ADMINISTRATIVE", note=note)
    assert len(result or "") == audit_reasons.MAX_REASON_NOTE_LENGTH


# ---------------------------------------------------------------------------
# G. Caracteres de control
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("note", ["a\x00b", "a\x1fb", "a\u2028b", "a\u000bb", "a\rb"])
def test_control_characters_are_rejected(note: str) -> None:
    with pytest.raises(audit_reasons.InvalidReasonNoteError):
        audit_reasons.validate_reason_note(code="ADMINISTRATIVE", note=note)


# ---------------------------------------------------------------------------
# H. Datos personales evidentes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("note", "expected_kind"),
    [
        ("escribir a ana@example.com", "email"),
        ("contacto: ana.lopez@correo.es", "email"),
        ("movil 612 345 678", "telefono"),
        ("tel +34 612345678", "telefono"),
        ("llamar al 0034612345678", "telefono"),
        ("ver https://ejemplo.test/inscripcion", "url"),
        ("mirar en www.bracketapp.nl", "url"),
        ("documento 12345678Z", "documento"),
        ("nie X1234567L", "documento"),
        ("iban ES9121000418450200051332", "iban"),
    ],
)
def test_evident_personal_data_is_rejected(note: str, expected_kind: str) -> None:
    with pytest.raises(audit_reasons.InvalidReasonNoteError) as error:
        audit_reasons.validate_reason_note(code="ADMINISTRATIVE", note=note)
    message = str(error.value)
    assert expected_kind in message
    assert note not in message


@pytest.mark.parametrize(
    "note",
    [
        "cambio de categoria solicitado por la academia",
        "revision del acta de la comision 2026-10-04",
        "duplicado detectado en el tramo 2 de la liguilla",
        "categoria -66kg revisada",
        "reinscripcion tras la jornada 3",
        "pendiente de validar con el tatami 1",
        "el club confirma los datos de la ficha",
    ],
)
def test_legitimate_notes_are_not_rejected(note: str) -> None:
    assert audit_reasons.validate_reason_note(code="ADMINISTRATIVE", note=note) == note


def test_find_personal_data_reports_kinds_not_values() -> None:
    kinds = audit_reasons.find_personal_data("avisar a ana@example.com o al 612345678")
    assert "email" in kinds
    assert "telefono" in kinds
    assert all("ana" not in kind for kind in kinds)


def test_find_personal_data_returns_empty_for_clean_text() -> None:
    assert not audit_reasons.find_personal_data("gestion ordinaria del comite")


def test_find_personal_data_is_deterministic_in_order() -> None:
    first = audit_reasons.find_personal_data("ana@example.com y 612345678")
    second = audit_reasons.find_personal_data("ana@example.com y 612345678")
    assert first == second
    assert first == ("email", "telefono")


@pytest.mark.parametrize(("scope", "action"), CATALOG_PAIRS)
def test_canonical_texts_pass_their_own_filter(scope: str, action: str) -> None:
    """El texto canonico nunca puede ser rechazado por las heuristicas del modulo."""

    for code in audit_reasons.CATALOG[(scope, action)]:
        text = audit_reasons.canonical_text(scope=scope, action=action, code=code)
        assert not audit_reasons.find_personal_data(text)
        assert text.isprintable()
        assert len(text) <= audit_reasons.MAX_REASON_NOTE_LENGTH


# ---------------------------------------------------------------------------
# I. Codigo obligatorio
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["WITHDRAW", "REINSTATE", "DISQUALIFY"])
def test_registration_lifecycle_actions_require_a_code(action: str) -> None:
    with pytest.raises(audit_reasons.MissingReasonCodeError):
        audit_reasons.build_audit_reason(scope="tournament_registration", action=action)


@pytest.mark.parametrize(
    ("scope", "action"),
    [
        ("competitor", "CREATE"),
        ("competitor", "UPDATE"),
        ("competitor", "DEACTIVATE"),
        ("competitor", "ACTIVATE"),
        ("tournament_registration", "CREATE"),
        ("tournament_registration", "UPDATE"),
        ("tournament_registration", "CONFIRM"),
    ],
)
def test_optional_actions_fall_back_to_their_default_code(scope: str, action: str) -> None:
    reason = audit_reasons.build_audit_reason(scope=scope, action=action)
    assert reason.code == audit_reasons.DEFAULT_CODES[(scope, action)]


def test_empty_code_is_not_a_valid_coverage_of_a_required_reason() -> None:
    with pytest.raises(audit_reasons.AuditReasonError):
        audit_reasons.build_audit_reason(
            scope="tournament_registration", action="WITHDRAW", code="  "
        )


def test_no_invalid_input_ever_returns_a_reason() -> None:
    invalid_calls = [
        {"scope": "otra_cosa", "action": "CREATE"},
        {"scope": "tournament_registration", "action": "BORRAR"},
        {"scope": "tournament_registration", "action": "CREATE", "code": "NOPE"},
        {"scope": "tournament_registration", "action": "CONFIRM", "code": "WITHDRAWAL_REQUEST"},
        {
            "scope": "tournament_registration",
            "action": "CONFIRM",
            "code": "READY",
            "note": "con nota",
        },
        {
            "scope": "tournament_registration",
            "action": "DISQUALIFY",
            "code": "ADMINISTRATIVE",
            "note": "avisar a ana@example.com",
        },
        {"scope": "tournament_registration", "action": "WITHDRAW"},
        {"scope": "competitor", "action": "DEACTIVATE", "code": "INJURY"},
    ]
    for call in invalid_calls:
        with pytest.raises(audit_reasons.AuditReasonError):
            audit_reasons.build_audit_reason(**call)


# ---------------------------------------------------------------------------
# J. Compatibilidad con los motivos historicos
# ---------------------------------------------------------------------------


def test_competitor_defaults_are_reproducible_by_the_catalog() -> None:
    expected = {
        "CREATE": ("PLANNED_ENTRY", "alta de competidor"),
        "UPDATE": ("DATA_CORRECTION", "actualizacion de datos basicos"),
        "DEACTIVATE": ("ADMINISTRATIVE", "baja logica de competidor"),
        "ACTIVATE": ("ADMINISTRATIVE", "reactivacion de competidor"),
    }
    assert competitors._DEFAULT_REASONS == {action: text for action, (_, text) in expected.items()}
    for action, (code, text) in expected.items():
        reason = audit_reasons.build_audit_reason(scope="competitor", action=action)
        assert (reason.code, reason.text) == (code, text)


def test_registration_defaults_are_reproducible_by_the_catalog() -> None:
    expected = {
        "CREATE": ("PLANNED_ENTRY", "alta de inscripcion"),
        "UPDATE": ("DATA_CORRECTION", "edicion de borrador de inscripcion"),
        "CONFIRM": ("READY", "confirmacion de inscripcion"),
    }
    assert registrations._DEFAULT_REASONS == {
        action: text for action, (_, text) in expected.items()
    }
    for action, (code, text) in expected.items():
        reason = audit_reasons.build_audit_reason(scope="tournament_registration", action=action)
        assert (reason.code, reason.text) == (code, text)


def test_scope_names_match_the_audit_entities() -> None:
    assert audit_reasons.SCOPE_COMPETITOR == competitors._COMPETITOR_ENTITY
    assert audit_reasons.SCOPE_REGISTRATION == registrations._REGISTRATION_ENTITY


def test_optional_and_required_actions_match_the_current_contracts() -> None:
    """Hoy `reason` es obligatorio exactamente en las tres acciones de ciclo de vida."""

    for name in ("withdraw_registration", "reinstate_registration", "disqualify_registration"):
        parameter = inspect.signature(getattr(registrations, name)).parameters["reason"]
        assert parameter.default is inspect.Parameter.empty

    for name in (
        "create_registration",
        "update_registration_draft",
        "confirm_registration",
        "create_competitor",
        "update_competitor_display_name",
        "deactivate_competitor",
        "activate_competitor",
    ):
        target = registrations if hasattr(registrations, name) else competitors
        parameter = inspect.signature(getattr(target, name)).parameters["reason"]
        assert parameter.default is None


# ---------------------------------------------------------------------------
# K. Sin conexion con la auditoria y sin cambios de contrato
# ---------------------------------------------------------------------------


def test_module_does_not_write_audit_or_touch_persistence() -> None:
    """El modulo es puro: ni escritor de auditoria, ni cliente de datos, ni transporte."""

    source = Path(audit_reasons.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "sql_insert_domain_change_log",
        "domain_writes",
        "sqlalchemy",
        "asyncpg",
        "INSERT INTO",
        "fastapi",
        "quote_ident",
    ):
        assert forbidden not in source, forbidden

    imported = {
        name
        for name, value in vars(audit_reasons).items()
        if not name.startswith("__") and type(value).__name__ == "module"
    }
    assert imported <= {"re", "unicodedata"}


def test_existing_audit_writer_keeps_its_signature() -> None:
    from bracket.sql.domain_writes import sql_insert_domain_change_log

    assert list(inspect.signature(sql_insert_domain_change_log).parameters) == [
        "entity",
        "entity_id",
        "action",
        "changed_fields",
        "actor_user_id",
        "actor_label",
        "reason",
        "tenant_club_id",
    ]


def test_domain_change_log_table_is_untouched() -> None:
    from bracket import schema

    assert schema.domain_change_log.name == "domain_change_log"
    assert [column.name for column in schema.domain_change_log.columns] == [
        "id",
        "entity",
        "entity_id",
        "action",
        "changed_fields",
        "actor_user_id",
        "actor_label",
        "reason",
        "created",
        "tenant_club_id",
    ]
    assert schema.domain_change_log.columns["reason"].type.__class__.__name__ == "Text"


@pytest.mark.parametrize(
    "name",
    [
        "create_registration",
        "update_registration_draft",
        "confirm_registration",
        "withdraw_registration",
        "reinstate_registration",
        "disqualify_registration",
    ],
)
def test_registration_operations_keep_their_public_signature(name: str) -> None:
    parameters = list(inspect.signature(getattr(registrations, name)).parameters)
    assert parameters[0] == "context"
    assert "reason" in parameters
    assert parameters[-1] == "reason"


@pytest.mark.parametrize(
    "name",
    [
        "create_competitor",
        "update_competitor_display_name",
        "deactivate_competitor",
        "activate_competitor",
    ],
)
def test_competitor_operations_keep_their_public_signature(name: str) -> None:
    parameters = list(inspect.signature(getattr(competitors, name)).parameters)
    assert parameters[0] == "context"
    assert "reason" in parameters
    assert parameters[-1] == "reason"


# ---------------------------------------------------------------------------
# L. Construccion completa y mensajes de error
# ---------------------------------------------------------------------------


def test_public_api_surface() -> None:
    assert set(audit_reasons.__all__) == {
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
    }


def test_build_audit_reason_with_note() -> None:
    reason = audit_reasons.build_audit_reason(
        scope="tournament_registration",
        action="WITHDRAW",
        code="ADMINISTRATIVE",
        note="  retirada acordada por el comite  ",
    )
    assert reason == audit_reasons.AuditReason(
        code="ADMINISTRATIVE",
        text="retirada administrativa",
        note="retirada acordada por el comite",
    )


def test_build_audit_reason_explicit_code_without_note() -> None:
    reason = audit_reasons.build_audit_reason(
        scope="competitor", action="DEACTIVATE", code="DUPLICATE"
    )
    assert (reason.code, reason.text, reason.note) == ("DUPLICATE", "baja por duplicado", None)


def test_build_audit_reason_rejects_note_on_non_notable_code() -> None:
    with pytest.raises(audit_reasons.ReasonNoteNotAllowedError):
        audit_reasons.build_audit_reason(
            scope="tournament_registration",
            action="DISQUALIFY",
            code="SCHEDULING_NO_SHOW",
            note="texto libre",
        )


def test_audit_reason_is_immutable() -> None:
    reason = audit_reasons.build_audit_reason(scope="tournament_registration", action="CONFIRM")
    with pytest.raises(AttributeError):
        reason.code = "OTRO"  # type: ignore[misc]


def test_note_is_allowed_for_a_notable_code_in_a_required_action() -> None:
    reason = audit_reasons.build_audit_reason(
        scope="tournament_registration",
        action="DISQUALIFY",
        code="RULE_VIOLATION",
        note="acta de la comision de competicion",
    )
    assert reason.note == "acta de la comision de competicion"
    assert reason.text == "descalificacion por incumplimiento de reglas"


def test_error_messages_never_echo_the_note() -> None:
    secret = "contacto ana@example.com"
    with pytest.raises(audit_reasons.InvalidReasonNoteError) as error:
        audit_reasons.build_audit_reason(
            scope="tournament_registration",
            action="DISQUALIFY",
            code="ADMINISTRATIVE",
            note=secret,
        )
    assert secret not in str(error.value)
    assert "ana" not in str(error.value)


def test_error_messages_do_not_expose_the_catalog_internals() -> None:
    with pytest.raises(audit_reasons.ReasonCodeNotAllowedError) as error:
        audit_reasons.validate_reason_code(
            scope="tournament_registration", action="CONFIRM", code="WITHDRAWAL_REQUEST"
        )
    message = str(error.value)
    assert "WITHDRAWAL_REQUEST" in message
    assert "CONFIRM" in message
