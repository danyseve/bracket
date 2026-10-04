"""S3.3c-3 — reglas puras del almacen de idempotencia: clave, huella y metadatos.

TDD: este modulo se escribio **antes** de ``bracket/logic/idempotency.py`` y fallo en rojo
(no existia el modulo). Define el contrato observable de:

* la clave de idempotencia que envia el cliente (forma y limites);
* el metodo y la ruta de la peticion a la que se ata la clave;
* la huella HMAC-SHA256 del servidor (version de clave, rotacion y fallo cerrado sin secreto);
* la lista permitida de metadatos de respuesta (nada de JSON arbitrario);
* la clasificacion de una reserva existente (replay, en curso, conflicto, caducada).

No hay aqui base de datos: lo que necesita PostgreSQL vive en
``tests/integration_tests/idempotency_storage_test.py`` y ``..._concurrency_test.py``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from heliclockter import datetime_utc

from bracket.config import config
from bracket.logic.idempotency import (
    IDEMPOTENCY_ALLOWED_RESPONSE_KEYS,
    IDEMPOTENCY_RETENTION_HOURS,
    IDEMPOTENCY_STATE_COMPLETED,
    IDEMPOTENCY_STATE_IN_PROGRESS,
    IdempotencyConfigurationError,
    IdempotencyDecision,
    IdempotencyError,
    classify_reservation,
    compute_request_fingerprint,
    default_expires_at,
    fingerprints_match,
    validate_idempotency_key,
    validate_request_method,
    validate_request_path,
    validate_response_metadata,
)

# Secreto **sintetico** de laboratorio: nunca se usa en despliegue ni se lee de produccion.
SYNTHETIC_SECRET = "synthetic-laboratory-secret-not-for-deployment"
SYNTHETIC_KEY = "lab-idempotency-key-0001"


@pytest.fixture
def synthetic_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """La clave HMAC de laboratorio se inyecta por configuracion, nunca por argumento."""
    monkeypatch.setattr(config, "idempotency_hmac_key", SYNTHETIC_SECRET)
    monkeypatch.setattr(config, "idempotency_hmac_key_version", "v1")


class _FakeReservation:
    """Reserva leida de la base, reducida a lo que mira el clasificador."""

    def __init__(
        self,
        state: str,
        fingerprint: str,
        key_version: str = "v1",
        expires_at: datetime_utc | None = None,
    ) -> None:
        self.state = state
        self.request_fingerprint = fingerprint
        self.fingerprint_key_version = key_version
        self.expires_at = expires_at or datetime_utc.now() + timedelta(hours=1)


def _now() -> datetime_utc:
    return datetime_utc.now()


def _fingerprint(**overrides: Any) -> str:
    arguments: dict[str, Any] = {
        "method": "POST",
        "path": "/clubs/1/competitors",
        "payload": b'{"display_name":"Ana"}',
    }
    arguments.update(overrides)
    return compute_request_fingerprint(**arguments).fingerprint


# --- Clave del cliente -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "lab-idempotency-key-0001",
        "abcDEF012._:-",
        "0" * 255,
    ],
)
def test_well_formed_keys_are_accepted(key: str) -> None:
    assert validate_idempotency_key(key) == key


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("", "vacia"),
        ("corta", "minimo"),
        ("0" * 256, "maximo"),
        ("clave con espacios", "caracteres"),
        ("clave/ambigua", "caracteres"),
        ("clave\nnueva", "caracteres"),
        ("clave\x00nula", "caracteres"),
        ("clave<comillas>", "caracteres"),
        ("cláve-acentuada-01", "caracteres"),
    ],
)
def test_malformed_keys_are_rejected_with_a_useful_reason(key: str, expected: str) -> None:
    with pytest.raises(IdempotencyError) as error:
        validate_idempotency_key(key)

    assert expected in str(error.value).lower()
    assert key[:8] not in str(error.value) or key == "", "el mensaje no repite la clave"


def test_key_that_is_not_a_string_is_rejected() -> None:
    with pytest.raises(IdempotencyError):
        validate_idempotency_key(1234)  # type: ignore[arg-type]


# --- Metodo y ruta -----------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_write_methods_are_accepted(method: str) -> None:
    assert validate_request_method(method) == method


@pytest.mark.parametrize("method", ["post", "GET", "HEAD", "OPTIONS", "", "POST "])
def test_non_write_or_malformed_methods_are_rejected(method: str) -> None:
    with pytest.raises(IdempotencyError):
        validate_request_method(method)


@pytest.mark.parametrize("path", ["/clubs/1/competitors", "/x", "/" + "a" * 254])
def test_absolute_and_bounded_paths_are_accepted(path: str) -> None:
    assert validate_request_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "",
        "clubs/1/competitors",
        "/" + "a" * 255,
        "/clubs/1?token=abc",
        "/clubs/1#frag",
        "/clubs/1\n",
        "https://host/clubs/1",
    ],
)
def test_paths_with_query_fragment_absolute_url_or_overlong_are_rejected(path: str) -> None:
    with pytest.raises(IdempotencyError):
        validate_request_path(path)


# --- Huella de la peticion ---------------------------------------------------------------------


def test_fingerprint_is_hmac_sha256_hex_with_its_key_version(synthetic_secret: None) -> None:
    fingerprint = compute_request_fingerprint(
        method="POST", path="/clubs/1/competitors", payload=b'{"display_name":"Ana"}'
    )

    assert len(fingerprint.fingerprint) == 64
    assert set(fingerprint.fingerprint) <= set("0123456789abcdef")
    assert fingerprint.key_version == "v1"


def test_fingerprint_is_stable_for_the_same_request(synthetic_secret: None) -> None:
    assert _fingerprint() == _fingerprint()


@pytest.mark.parametrize(
    "overrides",
    [
        {"method": "PATCH"},
        {"path": "/clubs/1/competitors/2"},
        {"payload": b'{"display_name":"Anb"}'},
    ],
)
def test_fingerprint_changes_when_any_part_of_the_request_changes(
    synthetic_secret: None, overrides: dict[str, Any]
) -> None:
    assert _fingerprint(**overrides) != _fingerprint()


def test_fingerprint_does_not_contain_the_secret(synthetic_secret: None) -> None:
    assert SYNTHETIC_SECRET not in _fingerprint()


def test_rotating_the_key_version_changes_the_fingerprint(
    synthetic_secret: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _fingerprint()

    monkeypatch.setattr(config, "idempotency_hmac_key_version", "v2")

    assert _fingerprint() != before
    assert compute_request_fingerprint(
        method="POST", path="/clubs/1/competitors", payload=b'{"display_name":"Ana"}'
    ).key_version == "v2"


def test_fingerprint_without_a_configured_secret_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "idempotency_hmac_key", None)

    with pytest.raises(IdempotencyConfigurationError) as error:
        _fingerprint()

    message = str(error.value)
    assert "IDEMPOTENCY_HMAC_KEY" in message, "el error nombra la variable, no un valor"
    assert SYNTHETIC_SECRET not in message


def test_an_empty_secret_is_not_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "idempotency_hmac_key", "")

    with pytest.raises(IdempotencyConfigurationError):
        _fingerprint()


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("a" * 64, "a" * 64, True),
        ("a" * 64, "b" * 64, False),
        ("a" * 63, "a" * 64, False),
        ("", "", True),
    ],
)
def test_fingerprint_comparison_is_total_and_constant_time(
    left: str, right: str, expected: bool
) -> None:
    assert fingerprints_match(left, right) is expected


# --- Metadatos de respuesta permitidos ----------------------------------------------------------


def test_the_allowed_metadata_list_is_small_and_explicit() -> None:
    assert set(IDEMPOTENCY_ALLOWED_RESPONSE_KEYS) == {
        "audit_event_id",
        "competitor_id",
        "registration_status",
        "resource_id",
        "resource_type",
    }


@pytest.mark.parametrize(
    "metadata",
    [
        {"resource_type": "tournament_registration", "resource_id": 7},
        {"registration_status": "CONFIRMED"},
        {"competitor_id": 12, "audit_event_id": 99},
        {},
    ],
)
def test_allowed_metadata_is_accepted(metadata: dict[str, Any]) -> None:
    assert validate_response_metadata(metadata) == metadata


@pytest.mark.parametrize(
    "metadata",
    [
        {"token": "x"},
        {"authorization": "Bearer x"},
        {"access_token": "x"},
        {"headers": {"authorization": "x"}},
        {"cookie": "session=1"},
        {"password": "x"},
        {"email": "ana@example.org"},
        {"name": "Ana"},
        {"display_name": "Ana"},
        {"body": {"a": 1}},
        {"payload": "texto"},
        {"response": {"a": 1}},
        {"resource_type": "competitor", "extra": 1},
        {"swagger": {}},
    ],
)
def test_forbidden_or_unknown_metadata_keys_are_rejected(metadata: dict[str, Any]) -> None:
    with pytest.raises(IdempotencyError) as error:
        validate_response_metadata(metadata)

    assert "no permitid" in str(error.value).lower()


@pytest.mark.parametrize(
    "metadata",
    [
        {"competitor_id": 1.5},
        {"competitor_id": -1},
        {"competitor_id": "12"},
        {"resource_type": "x" * 33},
        {"registration_status": "N" * 33},
        {"audit_event_id": True},
    ],
)
def test_metadata_with_wrong_value_shapes_is_rejected(metadata: dict[str, Any]) -> None:
    with pytest.raises(IdempotencyError):
        validate_response_metadata(metadata)


def test_nested_metadata_is_rejected() -> None:
    with pytest.raises(IdempotencyError):
        validate_response_metadata({"resource_id": {"nested": 1}})


def test_oversized_metadata_is_rejected() -> None:
    with pytest.raises(IdempotencyError) as error:
        validate_response_metadata({"registration_status": "X" * 2048})

    assert "tamano" in str(error.value).lower() or "2" in str(error.value)


def test_metadata_that_is_not_an_object_is_rejected() -> None:
    with pytest.raises(IdempotencyError):
        validate_response_metadata(["resource_id"])  # type: ignore[arg-type]


# --- Retencion y clasificacion de una reserva existente ----------------------------------------


def test_expiry_is_provisional_and_documented_as_such() -> None:
    created = datetime_utc(2026, 10, 4, 12, 0, 0)

    assert default_expires_at(created) == created + timedelta(hours=IDEMPOTENCY_RETENTION_HOURS)
    assert IDEMPOTENCY_RETENTION_HOURS == 24, "provisional: se revisa en S3.3c-4"


def test_an_absent_reservation_is_reserved(synthetic_secret: None) -> None:
    assert (
        classify_reservation(
            None,
            fingerprint=_fingerprint(),
            fingerprint_key_version="v1",
            now=_now(),
        )
        is IdempotencyDecision.RESERVED
    )


def test_a_completed_reservation_with_the_same_fingerprint_replays(synthetic_secret: None) -> None:
    assert (
        classify_reservation(
            _FakeReservation(IDEMPOTENCY_STATE_COMPLETED, _fingerprint()),
            fingerprint=_fingerprint(),
            fingerprint_key_version="v1",
            now=_now(),
        )
        is IdempotencyDecision.REPLAY
    )


def test_a_pending_reservation_with_the_same_fingerprint_is_in_progress(
    synthetic_secret: None,
) -> None:
    assert (
        classify_reservation(
            _FakeReservation(IDEMPOTENCY_STATE_IN_PROGRESS, _fingerprint()),
            fingerprint=_fingerprint(),
            fingerprint_key_version="v1",
            now=_now(),
        )
        is IdempotencyDecision.IN_PROGRESS
    )


def test_the_same_key_with_a_different_fingerprint_is_a_conflict(synthetic_secret: None) -> None:
    other = _fingerprint(method="PATCH")

    for state in (IDEMPOTENCY_STATE_IN_PROGRESS, IDEMPOTENCY_STATE_COMPLETED):
        assert (
            classify_reservation(
                _FakeReservation(state, other),
                fingerprint=_fingerprint(),
                fingerprint_key_version="v1",
                now=_now(),
            )
            is IdempotencyDecision.FINGERPRINT_CONFLICT
        )


def test_a_reservation_from_another_key_version_is_not_comparable(synthetic_secret: None) -> None:
    assert (
        classify_reservation(
            _FakeReservation(IDEMPOTENCY_STATE_COMPLETED, _fingerprint(), key_version="v0"),
            fingerprint=_fingerprint(),
            fingerprint_key_version="v1",
            now=_now(),
        )
        is IdempotencyDecision.FINGERPRINT_CONFLICT
    )


def test_an_expired_reservation_is_never_silently_reused(synthetic_secret: None) -> None:
    expired = _FakeReservation(
        IDEMPOTENCY_STATE_COMPLETED, _fingerprint(), expires_at=_now() - timedelta(seconds=1)
    )

    assert (
        classify_reservation(
            expired, fingerprint=_fingerprint(), fingerprint_key_version="v1", now=_now()
        )
        is IdempotencyDecision.EXPIRED
    )

    boundary = _FakeReservation(
        IDEMPOTENCY_STATE_IN_PROGRESS, _fingerprint(), expires_at=datetime_utc(2026, 10, 4, 12, 0, 0)
    )
    assert (
        classify_reservation(
            boundary,
            fingerprint=_fingerprint(),
            fingerprint_key_version="v1",
            now=datetime_utc(2026, 10, 4, 12, 0, 0),
        )
        is IdempotencyDecision.EXPIRED
    ), "el limite exacto ya no protege"


def test_an_unknown_state_in_the_database_is_an_error(synthetic_secret: None) -> None:
    with pytest.raises(IdempotencyError):
        classify_reservation(
            _FakeReservation("COMMITTED", _fingerprint()),
            fingerprint=_fingerprint(),
            fingerprint_key_version="v1",
            now=_now(),
        )


# --- Coherencia entre las reglas puras y el CHECK de la tabla -----------------------------------


def _schema_check(name: str) -> str:
    from sqlalchemy import CheckConstraint

    from bracket.schema import domain_idempotency_keys

    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in domain_idempotency_keys.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert name in checks, f"falta el CHECK {name} en el metadata vivo"
    return checks[name]


def test_the_state_check_only_admits_the_two_known_states() -> None:
    body = _schema_check("ck_domain_idempotency_keys_state")

    assert f"'{IDEMPOTENCY_STATE_IN_PROGRESS}'" in body
    assert f"'{IDEMPOTENCY_STATE_COMPLETED}'" in body
    assert "COMMITTED" not in body


def test_the_response_body_check_quotes_every_allowed_key_and_nothing_else() -> None:
    body = _schema_check("ck_domain_idempotency_keys_response_body")

    for key in IDEMPOTENCY_ALLOWED_RESPONSE_KEYS:
        assert f"'{key}'" in body, f"el CHECK no nombra {key}"

    assert "payload" not in body
    assert "token" not in body
