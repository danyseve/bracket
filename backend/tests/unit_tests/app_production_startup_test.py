"""Gate de arranque en modo produccion (S4.8f §2 y §3).

`bracket/app.py` impone, para cada router de la tabla `routers`, que
`router.prefix == config.api_prefix`. Con `API_PREFIX` sin definir (CI, LAB) `config.api_prefix`
vale `""` y el invariante se cumple de forma trivial: un router declarado `APIRouter()` sin prefijo
pasa toda la suite y **solo** falla al arrancar en produccion, donde `API_PREFIX=/api` (S4.8e: el
contenedor entra en bucle de reinicio con `AssertionError` en `app.py:245`).

Los dos tests de este modulo cierran ese hueco:

* el invariante se comprueba sobre los routers reales de la tabla `routers`, no sobre el texto del
  fichero;
* el arranque se reproduce en un subproceso con el entorno de produccion
  (`ENVIRONMENT=PRODUCTION`, `API_PREFIX=/api`, `SERVE_FRONTEND=true`) y se valida la tabla de
  rutas: prefijo `/api` sin duplicarse, la ruta contractual de la clasificacion previa y una ruta de
  competidores bajo el prefijo.

El `JWT_SECRET` de este modulo es un valor de gate, no un secreto real: `Config.jwt_secret` no tiene
valor por defecto y el arranque en PRODUCTION lo exige.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from bracket.app import routers
from bracket.config import config

BACKEND_DIR = Path(__file__).resolve().parents[2]

PRODUCTION_BOOT_CHECK = """
import bracket.app as app_module
from bracket.config import config

routes = {route.path for route in app_module.app.routes}
assert config.api_prefix == "/api", f"API_PREFIX no aplicado: {config.api_prefix!r}"
for tag, router in app_module.routers.items():
    assert router.prefix == config.api_prefix, f"prefix no declarado en el router `{tag}`"

preview = "/api/tournaments/{tournament_id}/registrations/classification-preview"
assert preview in routes, sorted(path for path in routes if "registrations" in path)
competitors = "/api/clubs/{club_id}/competitors"
assert competitors in routes, sorted(path for path in routes if "competitors" in path)

duplicated = sorted(path for path in routes if path.startswith("/api/api"))
assert not duplicated, duplicated
print("production-startup-gate OK")
"""


def _production_boot() -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "ENVIRONMENT": "PRODUCTION",
        "API_PREFIX": "/api",
        "SERVE_FRONTEND": "true",
        "JWT_SECRET": "production-startup-gate-only",
    }
    return subprocess.run(
        [sys.executable, "-c", PRODUCTION_BOOT_CHECK],
        cwd=BACKEND_DIR,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_every_router_of_the_registry_declares_the_api_prefix() -> None:
    """La tabla `routers` solo admite routers con el prefijo de la aplicacion."""
    assert routers, "la tabla `routers` de app.py no puede estar vacia"

    for tag, router in routers.items():
        assert router.prefix == config.api_prefix, (
            f"el router `{tag}` no respeta el invariante de app.py: "
            f"prefix={router.prefix!r} != api_prefix={config.api_prefix!r}; "
            "en produccion (API_PREFIX=/api) el arranque aborta"
        )


def test_the_app_boots_in_production_mode_with_every_route_under_the_api_prefix() -> None:
    """El arranque real con el entorno de produccion monta las rutas bajo `/api`, sin duplicarlo."""
    result = _production_boot()

    assert result.returncode == 0, (
        f"el arranque en modo produccion fallo (exit {result.returncode}):\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "production-startup-gate OK" in result.stdout
