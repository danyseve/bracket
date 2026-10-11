"""Contrato interno de la superficie HTTP de competidores (S3.7a, F3B, LAB ONLY).

Sin base de datos: fija la parte del contrato que se puede comprobar sobre el propio codigo —la
proyeccion publica, los cuerpos, la tabla de rutas, la puerta de escritura y el mapa de errores—
para que un cambio accidental en cualquiera de esas piezas falle aqui y no en produccion.
"""

from __future__ import annotations

from typing import Final

import pytest
from pydantic import ValidationError
from starlette import status

from bracket.app import app
from bracket.logic.competitors import (
    CompetitorDomainError,
    CompetitorNotFoundError,
    InsufficientPrivilegesError,
    InvalidCompetitorDataError,
    TenantNotAuthorizedError,
)
from bracket.models.db.competitor_api import (
    CompetitorCreateBody,
    CompetitorLifecycleBody,
    CompetitorListResponse,
    CompetitorRenameBody,
    CompetitorResponse,
)
from bracket.models.db.domain import Competitor
from bracket.routes import domain_competitors
from bracket.routes.f3_write_gate import require_f3_write_enabled
from bracket.utils.id_types import ClubId
from heliclockter import datetime_utc

#: Las seis rutas de la fase, con su metodo. El ambito es el club, no el torneo.
EXPECTED_ROUTES: Final = {
    ("POST", "/clubs/{club_id}/competitors"),
    ("GET", "/clubs/{club_id}/competitors"),
    ("GET", "/clubs/{club_id}/competitors/{competitor_id}"),
    ("POST", "/clubs/{club_id}/competitors/{competitor_id}/rename"),
    ("POST", "/clubs/{club_id}/competitors/{competitor_id}/deactivate"),
    ("POST", "/clubs/{club_id}/competitors/{competitor_id}/activate"),
}
#: Las cuatro escrituras: las unicas que llevan la puerta de F3.
WRITE_ROUTES: Final = {route for route in EXPECTED_ROUTES if route[0] == "POST"}
READ_ROUTES: Final = EXPECTED_ROUTES - WRITE_ROUTES
#: Proyeccion publica: ni tenant, ni estado interno de mas.
PUBLIC_FIELDS: Final = {"id", "display_name", "active", "created", "updated_at"}


def a_competitor() -> Competitor:
    return Competitor(
        id=1,
        display_name="Competidor de prueba",
        active=True,
        managed_by_club_id=ClubId(7),
        created=datetime_utc.now(),
        updated_at=None,
    )


def test_the_public_projection_is_closed_and_hides_the_tenant() -> None:
    response = CompetitorResponse.from_competitor(a_competitor())
    body = response.model_dump()

    assert set(body) == PUBLIC_FIELDS
    assert "managed_by_club_id" not in body
    assert not {"tenant_club_id", "managed_by_club_id"} & set(CompetitorResponse.model_fields)
    listing = CompetitorListResponse(count=1, competitors=[response])
    assert set(listing.model_dump()) == {"count", "competitors"}


@pytest.mark.parametrize(
    "extra",
    [
        {"managed_by_club_id": 99},
        {"tenant_club_id": 99},
        {"actor_user_id": 99},
        {"active": False},
        {"id": 99},
        {"inventado": "x"},
    ],
)
def test_the_bodies_forbid_tenant_actor_and_state(extra: dict[str, object]) -> None:
    for model in (CompetitorCreateBody, CompetitorRenameBody, CompetitorLifecycleBody):
        with pytest.raises(ValidationError):
            model(**{"display_name": "X", **extra})


def test_the_motive_is_optional_and_free_text_is_bounded() -> None:
    body = CompetitorCreateBody(display_name="X")
    assert body.reason_code is None
    assert body.reason_note is None
    # Que un codigo admita o no nota (y con que forma) lo decide el catalogo de motivos, no el
    # contrato HTTP: aqui no se duplica esa regla.
    assert CompetitorLifecycleBody(reason_code="DUPLICATE", reason_note="nota breve").reason_note == (
        "nota breve"
    )


def test_the_router_exposes_exactly_the_documented_routes() -> None:
    routes = {
        (method, route.path)
        for route in domain_competitors.router.routes
        for method in route.methods  # type: ignore[attr-defined]
    }

    assert routes == EXPECTED_ROUTES
    assert not any("tournaments" in path for _, path in routes)


def test_only_the_writes_carry_the_write_gate() -> None:
    for route in domain_competitors.router.routes:
        methods = route.methods  # type: ignore[attr-defined]
        gated = [
            dependency.dependency
            for dependency in route.dependencies  # type: ignore[attr-defined]
        ]
        if "POST" in methods:
            assert gated == [require_f3_write_enabled], route.path
        else:
            assert gated == [], route.path


def test_the_surface_is_registered_in_the_application() -> None:
    mounted = {route.path for route in app.routes}

    for _, path in EXPECTED_ROUTES:
        assert path in mounted, path
    # La superficie legacy de clubes sigue publicada: la nueva no la sustituye.
    assert "/clubs" in mounted
    assert "/clubs/{club_id}" in mounted


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (CompetitorNotFoundError(), status.HTTP_404_NOT_FOUND),
        (InvalidCompetitorDataError(), status.HTTP_400_BAD_REQUEST),
        (TenantNotAuthorizedError(), status.HTTP_403_FORBIDDEN),
        (InsufficientPrivilegesError(), status.HTTP_403_FORBIDDEN),
    ],
)
def test_the_error_map_covers_the_domain_errors_without_leaking_internals(
    error: CompetitorDomainError, expected_status: int
) -> None:
    exception = domain_competitors._http_exception(error)  # noqa: SLF001

    assert exception.status_code == expected_status
    detail = str(exception.detail)
    for leak in ("SELECT", "competitors", "Traceback", error.__class__.__name__):
        assert leak not in detail


def test_an_unknown_domain_error_is_not_promised_as_known() -> None:
    exception = domain_competitors._http_exception(  # noqa: SLF001
        CompetitorDomainError("algo que el mapa no conoce")
    )

    assert exception.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
