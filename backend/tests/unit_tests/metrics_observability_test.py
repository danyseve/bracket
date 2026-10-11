"""Observabilidad de peticiones (S3.8b, F3B, LAB ONLY).

Sin base de datos: fija la parte del contrato de telemetria que se puede comprobar sobre el propio
codigo —la clase de estado, el histograma de latencia, el conjunto cerrado de codigos de error, la
ausencia de identificadores en las etiquetas y la ausencia de secretos en los logs— para que un
cambio accidental en cualquiera de esas piezas falle aqui y no en produccion.
"""

from __future__ import annotations

import logging
from typing import Final

import pytest
from fastapi import HTTPException as FastApiHTTPException
from starlette import status
from starlette.exceptions import HTTPException
from starlette.requests import Request

from bracket.app import app, validation_exception_handler
from bracket.models.metrics import (
    ERROR_CODES,
    LATENCY_BUCKETS_SECONDS,
    RequestMetrics,
    RouteErrorDefinition,
    error_code_of,
    get_request_metrics,
    status_class_of,
)
from bracket.utils.starlette import get_route_path

#: Secreto sintetico del laboratorio: no sale de aqui y no es un valor de despliegue.
SYNTHETIC_TOKEN: Final = "synthetic-laboratory-token-not-for-deployment"
SYNTHETIC_IDEMPOTENCY_KEY: Final = "synthetic-laboratory-key-not-for-deployment"
LOGGER_NAME: Final = "bracket"


def build_request(
    path: str, method: str = "GET", headers: list[tuple[bytes, bytes]] | None = None
) -> Request:
    """Peticion minima, sin red: sirve para las piezas que dependen de `scope` y `state`."""
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers or [],
        "app": app,
        "state": {},
    }
    return Request(scope)


def test_status_class_is_bounded() -> None:
    """La etiqueta agrupa por clase de estado: cinco valores posibles, no un valor por codigo."""
    assert status_class_of(200) == "2xx"
    assert status_class_of(204) == "2xx"
    assert status_class_of(302) == "3xx"
    assert status_class_of(401) == "4xx"
    assert status_class_of(409) == "4xx"
    assert status_class_of(503) == "5xx"


def test_recording_keeps_status_class_and_latency() -> None:
    """Cada peticion deja recuento por clase de estado, ultima latencia e histograma."""
    metrics = RequestMetrics()
    request = build_request("/ping")

    metrics.record_request(request, status.HTTP_503_SERVICE_UNAVAILABLE, 0.03)

    status_series = {
        definition.status_class: value for definition, value in metrics.request_count.items()
    }
    assert status_series == {"5xx": 1}
    assert list(metrics.response_time.values()) == [0.03]

    histogram = next(iter(metrics.latency.values()))
    assert histogram.count == 1
    assert histogram.total_sum == pytest.approx(0.03)
    assert histogram.bucket_counts[0.025] == 0
    assert histogram.bucket_counts[0.05] == 1


def test_prometheus_exposition_has_valid_histogram_shape() -> None:
    """El histograma se publica con buckets acumulativos, `_sum` y `_count`."""
    metrics = RequestMetrics()
    metrics.record_request(build_request("/ping"), status.HTTP_200_OK, 0.002)
    metrics.record_request(build_request("/ping"), status.HTTP_200_OK, 0.2)
    text = metrics.to_prometheus()

    assert "# TYPE bracket_response_time_seconds histogram" in text
    bucket_label = f'le="{LATENCY_BUCKETS_SECONDS[0]}"'
    assert (
        f'bracket_response_time_seconds_bucket{{url="/ping",method="GET",{bucket_label}}} 1' in text
    )
    assert 'bracket_response_time_seconds_bucket{url="/ping",method="GET",le="+Inf"} 2' in text
    assert 'bracket_response_time_seconds_count{url="/ping",method="GET"} 2' in text
    assert 'bracket_response_time_seconds_sum{url="/ping",method="GET"}' in text


def test_unidentified_route_does_not_carry_the_requested_path() -> None:
    """Un path arbitrario no se convierte en etiqueta: se agrupa en una sola serie."""
    arbitrary = "/api/lo-que-sea-123456/998877"

    assert get_route_path(build_request(arbitrary)) == "unhandled"

    metrics = RequestMetrics()
    metrics.record_request(build_request(arbitrary), status.HTTP_404_NOT_FOUND, 0.001)
    metrics.record_request(build_request("/otro/654321"), status.HTTP_404_NOT_FOUND, 0.001)

    urls = {definition.url for definition in metrics.request_count}
    assert urls == {"unhandled"}


def test_error_codes_are_a_closed_set_and_stay_separate() -> None:
    """Cada codigo conocido tiene su serie; `F3_WRITE_DISABLED` y `LOCK_TIMEOUT` no se mezclan."""
    metrics = RequestMetrics()
    request = build_request("/clubs/1/competitors", method="POST")

    for code in sorted(ERROR_CODES):
        metrics.record_error(request, code)

    series = {definition.code: value for definition, value in metrics.error_count.items()}
    assert set(series) == set(ERROR_CODES)
    assert series["F3_WRITE_DISABLED"] == 1
    assert series["LOCK_TIMEOUT"] == 1


def test_error_code_extraction_covers_both_shapes() -> None:
    """El codigo se reconoce tanto en cadena como dentro del cuerpo del rechazo por revision."""
    assert error_code_of("F3_WRITE_DISABLED") == "F3_WRITE_DISABLED"
    assert (
        error_code_of({"code": "BRACKET_NOT_READY", "blocking_issues": []}) == "BRACKET_NOT_READY"
    )
    assert error_code_of({"code": "NO_EXISTE"}) is None
    assert error_code_of("mensaje libre del servidor") is None
    assert error_code_of(None) is None
    assert error_code_of({"detail": "F3_WRITE_DISABLED"}) is None


def test_free_text_detail_is_never_recorded_as_a_label() -> None:
    """Un `detail` que no es un codigo conocido no se registra: ni metrica, ni log."""
    metrics = RequestMetrics()
    request = build_request("/clubs/1/competitors", method="POST")

    metrics.record_error(request, f"usuario no autorizado en {SYNTHETIC_TOKEN}")

    assert not metrics.error_count


@pytest.mark.asyncio(loop_scope="session")
async def test_handler_records_the_code_and_keeps_the_response_contract() -> None:
    """Apagada la escritura, la respuesta sigue siendo `503 F3_WRITE_DISABLED` y ahora se cuenta."""
    request = build_request("/clubs/1/competitors", method="POST")
    route = get_route_path(request)
    before = get_request_metrics().error_count[
        RouteErrorDefinition(route=route, code="F3_WRITE_DISABLED")
    ]

    response = await validation_exception_handler(
        request,
        HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="F3_WRITE_DISABLED"),
    )

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    after = get_request_metrics().error_count[
        RouteErrorDefinition(route=route, code="F3_WRITE_DISABLED")
    ]
    assert after == before + 1


@pytest.mark.asyncio(loop_scope="session")
async def test_handler_records_the_code_of_a_structured_rejection() -> None:
    """El rechazo por revision trae el codigo dentro del cuerpo: se cuenta por el codigo."""
    request = build_request("/tournaments/1/generate-bracket", method="POST")
    route = get_route_path(request)
    detail = {
        "code": "BRACKET_NOT_READY",
        "blocking_issues": [{"code": "REGISTRATIONS_PENDING", "count": 3}],
        "registration_window": "OPEN",
    }

    responses = await validation_exception_handler(
        request, FastApiHTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
    )

    assert responses.status_code == status.HTTP_409_CONFLICT
    assert (
        get_request_metrics().error_count[
            RouteErrorDefinition(route=route, code="BRACKET_NOT_READY")
        ]
        >= 1
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_logs_carry_route_status_and_code_without_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """La linea de log trae ruta, metodo, estado, codigo y duracion; nunca el token ni la clave."""
    request = build_request(
        "/tournaments/1/generate-bracket",
        method="POST",
        headers=[
            (b"authorization", f"Bearer {SYNTHETIC_TOKEN}".encode()),
            (b"idempotency-key", SYNTHETIC_IDEMPOTENCY_KEY.encode()),
        ],
    )

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        await validation_exception_handler(
            request,
            HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="LOCK_TIMEOUT"),
        )

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "code=LOCK_TIMEOUT" in log_text
    assert "route=" in log_text
    assert "duration_ms=" in log_text
    assert SYNTHETIC_TOKEN not in log_text
    assert SYNTHETIC_IDEMPOTENCY_KEY not in log_text
    assert "authorization" not in log_text.lower()


@pytest.mark.asyncio(loop_scope="session")
async def test_logs_do_not_carry_free_text_details(caplog: pytest.LogCaptureFixture) -> None:
    """Un `detail` libre no genera linea de log: no hay canal para volcar datos de entrada."""
    request = build_request("/clubs/1/competitors", method="POST")

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        await validation_exception_handler(
            request, HTTPException(status_code=status.HTTP_409_CONFLICT, detail=SYNTHETIC_TOKEN)
        )

    assert not [record for record in caplog.records if "request_error" in record.getMessage()]
