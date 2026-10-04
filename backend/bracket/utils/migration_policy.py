"""Politica de cambios de esquema automaticos (S0 de F3B).

Regla unica: **en PRODUCTION el esquema nunca cambia sin intervencion del
operador**. El arranque valida la configuracion y aborta si un
``AUTO_RUN_MIGRATIONS=true`` heredado del entorno pudiera aplicar DDL por
accidente (fail-closed: el proceso no arranca en vez de migrar solo).

La migracion sigue siendo posible, pero explicita y controlada:
``alembic upgrade head`` con la ``PG_DSN`` correcta, dentro de una ventana de
cambio (ver ``docs/23`` y ``docs/24``).

Los mensajes de error son deliberadamente cerrados: nombran el entorno y la
variable, nunca la ``PG_DSN`` ni credenciales.
"""

from __future__ import annotations

from bracket import config as config_module
from bracket.config import Environment

MANUAL_MIGRATION_HINT = "alembic upgrade head"


class AutomaticMigrationNotAllowed(RuntimeError):
    """La configuracion permitiria DDL automatico en un entorno que lo prohibe."""


def automatic_schema_changes_allowed() -> bool:
    """Si el entorno actual admite cambios de esquema sin intervencion humana.

    Se lee el entorno de forma dinamica (``bracket.config.environment``) para que
    el modulo no quede fijado a la configuracion del momento de importar.
    """
    return config_module.environment is not Environment.PRODUCTION


def assert_migration_configuration_is_safe() -> None:
    """Aborta el arranque si la configuracion permitiria migrar en PRODUCTION.

    Se ejecuta **antes** de tocar la base de datos: si falla, el proceso no
    arranca y no se ha aplicado ningun DDL.
    """
    if automatic_schema_changes_allowed():
        return
    if not config_module.config.auto_run_migrations:
        return

    raise AutomaticMigrationNotAllowed(
        "AUTO_RUN_MIGRATIONS=true es una configuracion prohibida en "
        f"{config_module.environment.value.upper()}: el esquema solo se actualiza con una "
        f"migracion explicita y controlada ({MANUAL_MIGRATION_HINT}). "
        "Arranque abortado sin tocar la base de datos."
    )
