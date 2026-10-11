from __future__ import annotations

from collections import defaultdict
from enum import auto
from functools import cache
from typing import TYPE_CHECKING

from pydantic import BaseModel

from bracket.utils.http import HTTPMethod
from bracket.utils.starlette import get_route_path
from bracket.utils.types import EnumAutoStr

if TYPE_CHECKING:
    from starlette.requests import Request


class PrometheusMetricType(EnumAutoStr):
    counter = auto()
    gauge = auto()
    summary = auto()
    histogram = auto()


# S3.8b: buckets de latencia. Son los buckets por defecto de `prometheus_client`
# (DEFAULT_BUCKETS), no umbrales inventados para este proyecto.
LATENCY_BUCKETS_SECONDS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

# S3.8b: codigos de error que SI se cuentan como serie propia. Es un conjunto cerrado: cualquier
# `detail` que no este aqui no se registra, para que ningun texto libre (que podria llevar rutas,
# ids o datos de entrada) acabe como label de una metrica.
ERROR_CODES: frozenset[str] = frozenset(
    {
        "F3_WRITE_DISABLED",
        "BRACKET_NOT_READY",
        "IDEMPOTENCY_REQUEST_INVALID",
        "IDEMPOTENCY_KEY_REUSED",
        "OPERATION_IN_PROGRESS",
        "LOCK_TIMEOUT",
        "STATEMENT_TIMEOUT",
    }
)


def status_class_of(status_code: int) -> str:
    """Clase de estado de 3 caracteres: ``2xx``, ``4xx``, ``5xx``..."""
    return f"{status_code // 100}xx"


def error_code_of(detail: object) -> str | None:
    """Extrae el codigo de error de un ``detail`` de HTTPException, si es uno de los conocidos.

    Los errores de F3 viajan de dos formas: como cadena (``"F3_WRITE_DISABLED"``) o dentro de un
    objeto (la revision del cuadro responde ``{"code": "BRACKET_NOT_READY", ...}``). Cualquier otra
    cosa devuelve ``None``: no se registra nada que no sea un codigo del conjunto cerrado.
    """
    if isinstance(detail, str):
        candidate: object = detail
    elif isinstance(detail, dict):
        candidate = detail.get("code")
    else:
        candidate = None

    return candidate if isinstance(candidate, str) and candidate in ERROR_CODES else None


class RequestDefinition(BaseModel):
    url: str
    method: HTTPMethod

    @staticmethod
    def from_request(request: Request) -> RequestDefinition:
        return RequestDefinition(
            url=get_route_path(request),
            method=HTTPMethod(request.method),
        )

    def to_labels(self) -> dict[str, str]:
        return {"url": self.url, "method": str(self.method.value)}

    def to_value_lookup(self, value: float) -> tuple[dict[str, str], float]:
        return self.to_labels(), value

    def __hash__(self) -> int:
        return str.__hash__(f"{self.method}-{self.url}")


class RequestStatusDefinition(RequestDefinition):
    """Definicion de la serie de recuento: ruta + metodo + clase de estado."""

    status_class: str

    @staticmethod
    def from_request_and_status(request: Request, status_code: int) -> RequestStatusDefinition:
        return RequestStatusDefinition(
            url=get_route_path(request),
            method=HTTPMethod(request.method),
            status_class=status_class_of(status_code),
        )

    def to_labels(self) -> dict[str, str]:
        return {
            "url": self.url,
            "method": str(self.method.value),
            "status_class": self.status_class,
        }

    def __hash__(self) -> int:
        return str.__hash__(f"{self.method}-{self.url}-{self.status_class}")


class RouteErrorDefinition(BaseModel):
    """Definicion de la serie de errores: ruta (plantilla) + codigo de error cerrado."""

    route: str
    code: str

    @staticmethod
    def from_request_and_code(request: Request, code: str) -> RouteErrorDefinition:
        return RouteErrorDefinition(route=get_route_path(request), code=code)

    def to_labels(self) -> dict[str, str]:
        return {"route": self.route, "code": self.code}

    def to_value_lookup(self, value: float) -> tuple[dict[str, str], float]:
        return self.to_labels(), value

    def __hash__(self) -> int:
        return str.__hash__(f"{self.route}-{self.code}")


class LatencyHistogram(BaseModel):
    """Acumulador de un histograma de Prometheus para una serie (ruta + metodo)."""

    bucket_counts: dict[float, int] = defaultdict(int)
    total_sum: float = 0.0
    count: int = 0

    def observe(self, value: float) -> None:
        self.count += 1
        self.total_sum += value
        for bucket in LATENCY_BUCKETS_SECONDS:
            if value <= bucket:
                self.bucket_counts[bucket] += 1

    def to_prometheus_lines(self, name: str, labels: str) -> str:
        lines = []
        for bucket in LATENCY_BUCKETS_SECONDS:
            bucket_labels = f'{labels},le="{bucket}"' if labels else f'le="{bucket}"'
            lines.append(f"{name}_bucket{{{bucket_labels}}} {self.bucket_counts[bucket]}")
        infinite_labels = f'{labels},le="+Inf"' if labels else 'le="+Inf"'
        lines.append(f"{name}_bucket{{{infinite_labels}}} {self.count}")
        sum_labels = f"{{{labels}}}" if labels else ""
        lines.append(f"{name}_sum{sum_labels} {self.total_sum:.06f}")
        lines.append(f"{name}_count{sum_labels} {self.count}")
        return "\n".join(lines) + "\n"


class MetricDefinition(BaseModel):
    name: str
    description: str
    type_: PrometheusMetricType

    def format_for_prometheus(self, value: float) -> str:
        return (
            f"# HELP {self.name} {self.description}\n"
            f"# TYPE {self.name} {self.type_.value}\n"
            f"{self.name} {value:.06f}\n"
        )

    def format_for_prometheus_per_label(self, values: list[tuple[dict[str, str], float]]) -> str:
        result = f"# HELP {self.name} {self.description}\n# TYPE {self.name} {self.type_.value}\n"
        for labels, value in values:
            key_value = ",".join(
                [f'{label}="{label_value}"' for label, label_value in labels.items()]
            )
            result += f"{self.name}{{{key_value}}} {value}\n"

        return result

    def format_histogram_for_prometheus(
        self, values: list[tuple[dict[str, str], LatencyHistogram]]
    ) -> str:
        result = f"# HELP {self.name} {self.description}\n# TYPE {self.name} {self.type_.value}\n"
        for labels, histogram in values:
            key_value = ",".join(
                [f'{label}="{label_value}"' for label, label_value in labels.items()]
            )
            result += histogram.to_prometheus_lines(self.name, key_value)

        return result


RESPONSE_TIME_METRIC = MetricDefinition(
    name="bracket_response_time",
    description="Ultima latencia observada por endpoint (valor puntual, no percentiles)",
    type_=PrometheusMetricType.gauge,
)
REQUEST_COUNT_METRIC = MetricDefinition(
    name="bracket_request_count",
    description="Peticiones por endpoint y clase de estado",
    type_=PrometheusMetricType.counter,
)
LATENCY_HISTOGRAM_METRIC = MetricDefinition(
    name="bracket_response_time_seconds",
    description="Histograma de latencia por endpoint (base para p95/p99)",
    type_=PrometheusMetricType.histogram,
)
ERROR_COUNT_METRIC = MetricDefinition(
    name="bracket_error_total",
    description="Errores de negocio por ruta y codigo (conjunto cerrado de codigos)",
    type_=PrometheusMetricType.counter,
)
VERSION_METRIC = MetricDefinition(
    name="bracket_version",
    description="Marcador de version (constante 1; sin consumidores hoy)",
    type_=PrometheusMetricType.counter,
)


class RequestMetrics(BaseModel):
    response_time: dict[RequestDefinition, float] = defaultdict(float)
    request_count: dict[RequestStatusDefinition, int] = defaultdict(int)
    latency: dict[RequestDefinition, LatencyHistogram] = defaultdict(LatencyHistogram)
    error_count: dict[RouteErrorDefinition, int] = defaultdict(int)

    def record_request(self, request: Request, status_code: int, duration: float) -> None:
        definition = RequestDefinition.from_request(request)
        status_definition = RequestStatusDefinition.from_request_and_status(request, status_code)
        self.response_time[definition] = duration
        self.request_count[status_definition] += 1
        self.latency[definition].observe(duration)

    def record_error(self, request: Request, code: str) -> None:
        if code not in ERROR_CODES:
            return
        self.error_count[RouteErrorDefinition.from_request_and_code(request, code)] += 1

    def to_prometheus(self) -> str:
        metrics = [
            RESPONSE_TIME_METRIC.format_for_prometheus_per_label(
                [m.to_value_lookup(v) for m, v in self.response_time.items()]
            ),
            REQUEST_COUNT_METRIC.format_for_prometheus_per_label(
                [m.to_value_lookup(v) for m, v in self.request_count.items()]
            ),
            LATENCY_HISTOGRAM_METRIC.format_histogram_for_prometheus(
                [(m.to_labels(), v) for m, v in self.latency.items()]
            ),
            ERROR_COUNT_METRIC.format_for_prometheus_per_label(
                [m.to_value_lookup(v) for m, v in self.error_count.items()]
            ),
            VERSION_METRIC.format_for_prometheus(1.0),
        ]
        return "\n".join(metrics)


@cache
def get_request_metrics() -> RequestMetrics:
    return RequestMetrics()
