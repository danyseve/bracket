"""S3.8c — reglas puras de retencion y purga de claves de idempotencia (LAB ONLY).

Comprueban el criterio de elegibilidad sin base de datos: que solo se pueda borrar lo que ya no
puede participar legitimamente en una repeticion, que el margen amplie la ventana protegida, que el
dry-run informe sin borrar, y que la salida no lleve material de clave.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from heliclockter import datetime_utc

from bracket.logic.idempotency import (
    IDEMPOTENCY_RETENTION_HOURS,
    IDEMPOTENCY_STATE_COMPLETED,
    IDEMPOTENCY_STATE_IN_PROGRESS,
)
from bracket.logic.idempotency_purge import (
    IDEMPOTENCY_PURGE_BATCH_SIZE_DEFAULT,
    IDEMPOTENCY_PURGE_GRACE_HOURS_DEFAULT,
    IDEMPOTENCY_PURGE_MAX_BATCHES_DEFAULT,
    PURGEABLE_STATES,
    PurgePolicy,
    PurgeReport,
)

NOW = datetime_utc(2026, 10, 11, 12, 0, 0)
FIRST_KEY = "purge-unit-key-0000000001"
LONG_KEY = "purge-unit-key-0000000002"
FIRST_FINGERPRINT = "a" * 64
SECOND_FINGERPRINT = "b" * 64


def test_defaults_keep_the_protected_window_beyond_the_retention() -> None:
    policy = PurgePolicy()

    assert PURGEABLE_STATES == (IDEMPOTENCY_STATE_COMPLETED,)
    assert policy.batch_size == IDEMPOTENCY_PURGE_BATCH_SIZE_DEFAULT
    assert policy.max_batches == IDEMPOTENCY_PURGE_MAX_BATCHES_DEFAULT
    assert policy.grace_hours == IDEMPOTENCY_PURGE_GRACE_HOURS_DEFAULT
    assert IDEMPOTENCY_PURGE_GRACE_HOURS_DEFAULT == IDEMPOTENCY_RETENTION_HOURS, (
        "el margen por defecto no puede recortar la ventana contractual de retencion"
    )
    assert policy.dry_run is False
    assert policy.grace() == timedelta(hours=IDEMPOTENCY_RETENTION_HOURS)
    assert policy.cutoff(NOW) == NOW - timedelta(hours=IDEMPOTENCY_RETENTION_HOURS)
    assert policy.row_ceiling() == (
        IDEMPOTENCY_PURGE_BATCH_SIZE_DEFAULT * IDEMPOTENCY_PURGE_MAX_BATCHES_DEFAULT
    )


def test_only_completed_reservations_are_purgeable() -> None:
    assert IDEMPOTENCY_STATE_IN_PROGRESS not in PURGEABLE_STATES, (
        "una reserva en curso puede estar sosteniendo una operacion viva"
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"batch_size": 0}, id="lote-de-cero-filas"),
        pytest.param({"batch_size": -1}, id="lote-negativo"),
        pytest.param({"max_batches": 0}, id="sin-lotes-no-hay-barrido"),
        pytest.param({"grace_hours": -1}, id="margen-negativo"),
    ],
)
def test_policy_rejects_values_outside_the_declared_range(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        PurgePolicy(**kwargs)


@pytest.mark.parametrize(
    ("state", "age_hours", "grace_hours", "expected"),
    [
        (IDEMPOTENCY_STATE_COMPLETED, 25, 24, True),
        (IDEMPOTENCY_STATE_COMPLETED, 24, 24, False),
        (IDEMPOTENCY_STATE_COMPLETED, 1, 24, False),
        (IDEMPOTENCY_STATE_COMPLETED, 25, 0, True),
        (IDEMPOTENCY_STATE_COMPLETED, -1, 24, False),
        (IDEMPOTENCY_STATE_IN_PROGRESS, 25, 24, False),
        (IDEMPOTENCY_STATE_IN_PROGRESS, 25, 0, False),
    ],
)
def test_eligibility_follows_the_declared_criterion(
    state: str, age_hours: int, grace_hours: int, expected: bool
) -> None:
    policy = PurgePolicy(grace_hours=grace_hours)
    expires_at = NOW - timedelta(hours=age_hours)

    assert policy.is_purgeable(state=state, expires_at=expires_at, now=NOW) is expected


def test_dry_run_report_reports_candidates_without_deletions() -> None:
    report = PurgeReport(
        dry_run=True,
        candidates=7,
        deleted=0,
        batches=0,
        duration_seconds=0.01,
        stopped_at_batch_limit=False,
    )

    lines = report.render_lines()

    assert any(line.startswith("filas elegibles=7") for line in lines)
    assert any(line.startswith("filas borradas=0") for line in lines)
    assert any("modo=dry-run" in line for line in lines)


def test_report_flags_a_batch_limit_that_left_rows_behind() -> None:
    stopped = PurgeReport(
        dry_run=False,
        candidates=4,
        deleted=4,
        batches=2,
        duration_seconds=0.5,
        stopped_at_batch_limit=True,
    )
    finished = PurgeReport(
        dry_run=False,
        candidates=3,
        deleted=3,
        batches=2,
        duration_seconds=0.5,
        stopped_at_batch_limit=False,
    )

    assert any("repetir la ejecucion" in line for line in stopped.render_lines())
    assert not any("repetir la ejecucion" in line for line in finished.render_lines())


def test_report_lines_carry_only_aggregates() -> None:
    report = PurgeReport(
        dry_run=False,
        candidates=2,
        deleted=2,
        batches=1,
        duration_seconds=1.25,
        stopped_at_batch_limit=False,
    )

    rendered = "\n".join(report.render_lines())

    for forbidden in (
        FIRST_KEY,
        LONG_KEY,
        FIRST_FINGERPRINT,
        SECOND_FINGERPRINT,
        "fingerprint",
        "idempotency_key",
    ):
        assert forbidden not in rendered
    assert "duracion_s=1.250" in rendered
