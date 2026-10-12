import glob
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from starlette import status
from starlette.exceptions import HTTPException
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, Response
from starlette.staticfiles import StaticFiles

from bracket.config import Environment, config, environment, init_sentry
from bracket.cronjobs.scheduling import start_cronjobs
from bracket.database import database
from bracket.models.metrics import error_code_of, get_request_metrics
from bracket.routes import (
    auth,
    clubs,
    courts,
    domain_competitors,
    domain_court_queue,
    domain_registrations,
    domain_schedule,
    domain_stage_item_court,
    internals,
    matches,
    players,
    public_schedule,
    rankings,
    rounds,
    stage_item_inputs,
    stage_items,
    stages,
    teams,
    tournaments,
    users,
)
from bracket.utils.alembic import alembic_run_migrations
from bracket.utils.asyncio import AsyncioTasksManager
from bracket.utils.db_init import init_db_when_empty
from bracket.utils.logging import logger
from bracket.utils.migration_policy import assert_migration_configuration_is_safe
from bracket.utils.starlette import get_route_path

init_sentry()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # S0 (F3B): validacion de configuracion antes de tocar la base de datos.
    # En PRODUCTION con `AUTO_RUN_MIGRATIONS=true` el arranque aborta aqui.
    assert_migration_configuration_is_safe()

    await database.connect()
    await init_db_when_empty()

    if config.auto_run_migrations and environment is not Environment.CI:
        alembic_run_migrations()

    if environment is Environment.PRODUCTION:
        start_cronjobs()

    if environment is Environment.PRODUCTION and not config.is_cors_enabled():
        logger.warning("It's advised to set the `CORS_ORIGINS` environment variable in production")

    yield

    if environment is not Environment.CI:
        await database.disconnect()

    await AsyncioTasksManager.gather()


routers = {
    "Auth": auth.router,
    "Clubs": clubs.router,
    "Competitors": domain_competitors.router,
    "Courts": courts.router,
    "Court Queue": domain_court_queue.router,
    "Internals": internals.router,
    "Matches": matches.router,
    "Players": players.router,
    "Rankings": rankings.router,
    "Rounds": rounds.router,
    "Stage Items": stage_items.router,
    "Stage Item Inputs": stage_item_inputs.router,
    "Stages": stages.router,
    "Teams": teams.router,
    "Stage Item Court": domain_stage_item_court.router,
    "Tournament Registrations": domain_registrations.router,
    "Tournaments": tournaments.router,
    "Domain Schedule": domain_schedule.router,
    "Public Schedule": public_schedule.router,
    "Users": users.router,
}

table_of_contents = "\n\n".join(
    [f"- [{tag}](#tag/{tag.replace(' ', '-')})" for tag in routers.keys()]
)


description = f"""
### Description
This API allows you to do everything the frontend of [Bracket](https://github.com/evroon/bracket)
allows you to do (the frontend uses this API as well).

Fore more information, see the [documentation](https://docs.bracketapp.nl).

### Table of Contents
*(links only work for [ReDoc](https://api.bracketapp.nl/redoc), not for Swagger UI)*

{table_of_contents}

### Links
GitHub: <https://github.com/evroon/bracket>

Docs: <https://docs.bracketapp.nl>

Demo: <https://www.bracketapp.nl/demo>

API docs (Redoc): <https://api.bracketapp.nl/redoc>

API docs (Swagger UI): <https://api.bracketapp.nl/docs>
"""

app = FastAPI(
    title="Bracket API",
    docs_url="/docs",
    version="1.0.0",
    lifespan=lifespan,
    summary="API for Bracket, an open source tournament system.",
    description=description,
    license_info={
        "name": "AGPL-3.0",
        "url": "https://www.gnu.org/licenses/agpl-3.0.en.html",
    },
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_origins,
    allow_origin_regex=config.cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_process_time_header(request: Request, call_next: RequestResponseEndpoint) -> Response:
    # S3.8b: la clase de estado solo se conoce despues de la respuesta, asi que el recuento se
    # registra al final (antes se contaba la peticion y nunca se sabia como acababa). El
    # instante de entrada se guarda en `request.state` para medir la duracion tambien desde los
    # manejadores de error, que se ejecutan dentro de este middleware.
    start_time = time.time()
    request.state.metrics_start_time = start_time
    request_metrics = get_request_metrics()
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception:
        # Excepcion no controlada por los manejadores: se registra como 5xx antes de propagarla,
        # para que un fallo del backend sea visible en /api/metrics y no solo en los logs.
        request_metrics.record_request(request, status_code, time.time() - start_time)
        raise

    request_metrics.record_request(request, status_code, time.time() - start_time)
    return response


def _request_duration_ms(request: Request) -> float:
    start_time: float | None = getattr(request.state, "metrics_start_time", None)
    if start_time is None:
        return 0.0
    return (time.time() - start_time) * 1000


def log_and_count_route_error(request: Request, detail: object, status_code: int) -> None:
    """Registra metrica y log de un error de negocio, sin datos sensibles.

    S3.8b: solo se registran los `detail` que llevan un codigo del conjunto cerrado `ERROR_CODES`
    (codigos de F3 y del protocolo de idempotencia). Nunca se registran cuerpos, cabeceras, tokens,
    claves de idempotencia ni datos de entrada: si el `detail` no lleva un codigo conocido, no se
    registra nada.
    """
    code = error_code_of(detail)
    if code is None:
        return

    get_request_metrics().record_error(request, code)
    logger.warning(
        "request_error route=%s method=%s status=%s code=%s duration_ms=%.1f",
        get_route_path(request),
        request.method,
        status_code,
        code,
        _request_duration_ms(request),
    )


@app.exception_handler(HTTPException)
async def validation_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    # S3.4a: se preserva `Retry-After` cuando la excepcion lo trae (409 de operacion en curso y
    # 503 de espera agotada). Es el unico header que se conserva: el cuerpo y el resto del
    # contrato de error no cambian.
    retry_after = None if exc.headers is None else exc.headers.get("Retry-After")
    headers = None if retry_after is None else {"Retry-After": retry_after}
    log_and_count_route_error(request, exc.detail, exc.status_code)
    # Hotfix de produccion (141eb40, integrado en S3.10): si la excepcion trae un codigo estable
    # (`TournamentDeleteConflictError.code`), se añade como `code` al cuerpo. `detail` no cambia y
    # los codigos de F3 siguen viajando en `detail` (contrato S3.3c-4).
    payload: dict[str, str] = {"detail": exc.detail}
    code = getattr(exc, "code", None)
    if code is not None:
        payload["code"] = code
    return JSONResponse(payload, status_code=exc.status_code, headers=headers)


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "request_error route=%s method=%s status=%s code=INTERNAL_ERROR exception=%s",
        get_route_path(request),
        request.method,
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        type(exc).__name__,
    )
    return JSONResponse({"detail": "Internal server error"}, status_code=500)


app.mount(f"{config.api_prefix}/static", StaticFiles(directory="static"), name="static")

for tag, router in routers.items():
    assert router.prefix == config.api_prefix, f"Prefix not set on router with tag `{tag}`"
    app.include_router(router, tags=[tag])

if config.serve_frontend:
    msg = "API_PREFIX env var must be set (e.g. `/api`) when serving the frontend"
    assert config.api_prefix.startswith("/"), msg

    frontend_root = Path("frontend-dist")
    allowed_paths = list(glob.iglob("frontend-dist/**/*", recursive=True))

    @app.get("/{full_path:path}")
    async def frontend(full_path: str) -> FileResponse:
        path = frontend_root / Path(full_path)

        # Checking `str(path) in allowed_paths` should be enough here but we check for more cases
        # to be sure and avoid AI tools raising false positives.
        if (
            path.exists()
            and path.is_file()
            and str(path) in allowed_paths
            and frontend_root in path.parents
        ):
            return FileResponse(path)

        return FileResponse(frontend_root / Path("index.html"))
