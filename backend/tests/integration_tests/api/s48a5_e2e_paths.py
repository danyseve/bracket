"""Rutas portables del bloque E2E S4.8a-5 (a-5).

Ni los tests ni su evidencia dependen de rutas absolutas del host de laboratorio: el repositorio se
localiza a partir de la ubicacion de este fichero y la evidencia §25 se escribe en un directorio
temporal del sistema, sobreescribible con la variable de entorno ``S48A5_EVIDENCE_DIR``.

Antes estas rutas estaban fijadas al host del laboratorio, lo que rompia cualquier ejecucion fuera
de el (CI incluida).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Final

#: Variable de entorno opcional que fija el directorio de evidencia §25.
EVIDENCE_DIR_ENV: Final = "S48A5_EVIDENCE_DIR"

#: Raiz del repositorio: este fichero vive en ``backend/tests/integration_tests/api/``.
REPO_ROOT: Final = Path(__file__).resolve().parents[4]

#: Raiz del backend del repositorio (``<repo>/backend``).
BACKEND_ROOT: Final = REPO_ROOT / "backend"


def evidence_dir() -> Path:
    """Directorio de la evidencia §25: el que declare el entorno, o uno temporal del sistema."""
    override = os.environ.get(EVIDENCE_DIR_ENV)
    return Path(override) if override else Path(tempfile.gettempdir()) / "s48a5-evidence"


def evidence_path(filename: str) -> Path:
    """Ruta del fichero de evidencia §25 indicado (no lo crea)."""
    return evidence_dir() / filename
