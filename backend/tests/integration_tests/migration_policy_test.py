"""S0 de F3B — politica de migraciones automaticas.

TDD: estas pruebas se escribieron **antes** de la implementacion
(``bracket/utils/migration_policy.py``) y fallaron en rojo con
``ModuleNotFoundError``; la implementacion posterior las lleva a verde.

Invariante que se protege: en ``ENVIRONMENT=PRODUCTION`` el esquema **nunca**
cambia de forma automatica. El arranque aborta con un error explicito si la
configuracion lo permitiria, sin tocar la base de datos y sin filtrar
credenciales ni la ``PG_DSN`` en el mensaje.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI

import bracket.app as app_module
import bracket.config as config_module
import bracket.utils.db_init as db_init_module
from bracket.config import Config, Environment
from bracket.database import database
from bracket.schema import metadata as schema_metadata
from bracket.utils.migration_policy import (
    AutomaticMigrationNotAllowed,
    assert_migration_configuration_is_safe,
    automatic_schema_changes_allowed,
)

LEAKY_DSN = "postgresql://leak_user:leak_password@db.internal:5432/leak_db"


def build_config(**overrides: Any) -> Config:
    """Config con valores deterministas, sin depender de ficheros de entorno."""
    return Config(jwt_secret="test-secret", **overrides)


def test_auto_run_migrations_defaults_to_false() -> None:
    assert build_config().auto_run_migrations is False


def test_non_production_environments_allow_automatic_schema_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for allowed_environment in (Environment.DEVELOPMENT, Environment.DEMO, Environment.CI):
        monkeypatch.setattr(config_module, "environment", allowed_environment)
        monkeypatch.setattr(config_module, "config", build_config(auto_run_migrations=True))

        assert automatic_schema_changes_allowed() is True
        assert_migration_configuration_is_safe()


def test_production_with_auto_migrations_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "environment", Environment.PRODUCTION)
    monkeypatch.setattr(config_module, "config", build_config(auto_run_migrations=True))

    assert automatic_schema_changes_allowed() is False

    with pytest.raises(AutomaticMigrationNotAllowed) as error:
        assert_migration_configuration_is_safe()

    message = str(error.value)
    assert "PRODUCTION" in message
    assert "AUTO_RUN_MIGRATIONS" in message
    assert "alembic upgrade head" in message


def test_production_without_auto_migrations_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "environment", Environment.PRODUCTION)
    monkeypatch.setattr(config_module, "config", build_config(auto_run_migrations=False))

    assert_migration_configuration_is_safe()


def test_error_message_never_leaks_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "environment", Environment.PRODUCTION)
    monkeypatch.setattr(
        config_module,
        "config",
        build_config(auto_run_migrations=True, pg_dsn=LEAKY_DSN),
    )

    with pytest.raises(AutomaticMigrationNotAllowed) as error:
        assert_migration_configuration_is_safe()

    message = str(error.value)
    for leaked in ("leak_user", "leak_password", "leak_db", "db.internal", "postgresql://"):
        assert leaked not in message


@pytest.mark.asyncio(loop_scope="session")
async def test_app_startup_aborts_before_touching_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched: list[str] = []

    async def fake_connect() -> None:
        touched.append("connect")

    monkeypatch.setattr(app_module, "environment", Environment.PRODUCTION)
    monkeypatch.setattr(config_module, "environment", Environment.PRODUCTION)
    monkeypatch.setattr(config_module, "config", build_config(auto_run_migrations=True))
    monkeypatch.setattr(database, "connect", fake_connect)

    with pytest.raises(AutomaticMigrationNotAllowed):
        async with app_module.lifespan(FastAPI()):
            pass

    assert not touched


@pytest.mark.asyncio(loop_scope="session")
async def test_production_startup_never_runs_migrations_automatically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps: list[str] = []

    async def fake_connect() -> None:
        steps.append("connect")

    async def fake_disconnect() -> None:
        steps.append("disconnect")

    async def fake_init_db_when_empty() -> None:
        steps.append("init_db_when_empty")

    monkeypatch.setattr(app_module, "environment", Environment.PRODUCTION)
    monkeypatch.setattr(config_module, "environment", Environment.PRODUCTION)
    monkeypatch.setattr(config_module, "config", build_config(auto_run_migrations=False))
    monkeypatch.setattr(database, "connect", fake_connect)
    monkeypatch.setattr(database, "disconnect", fake_disconnect)
    monkeypatch.setattr(app_module, "init_db_when_empty", fake_init_db_when_empty)
    monkeypatch.setattr(app_module, "alembic_run_migrations", lambda: steps.append("migrate"))
    monkeypatch.setattr(app_module, "start_cronjobs", lambda: steps.append("cronjobs"))

    async with app_module.lifespan(FastAPI()):
        pass

    assert steps == ["connect", "init_db_when_empty", "cronjobs", "disconnect"]
    assert "migrate" not in steps


@pytest.mark.asyncio(loop_scope="session")
async def test_init_db_when_empty_never_creates_or_stamps_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_fetch_val(*_args: Any, **_kwargs: Any) -> int:
        calls.append("fetch_val")
        return 0

    def fake_create_all(*_args: Any, **_kwargs: Any) -> None:
        calls.append("create_all")

    def fake_stamp_head(*_args: Any, **_kwargs: Any) -> None:
        calls.append("stamp_head")

    monkeypatch.setattr(config_module, "environment", Environment.PRODUCTION)
    monkeypatch.setattr(config_module, "config", build_config(auto_run_migrations=True))
    monkeypatch.setattr(database, "fetch_val", fake_fetch_val)
    monkeypatch.setattr(schema_metadata, "create_all", fake_create_all)
    monkeypatch.setattr(db_init_module, "alembic_stamp_head", fake_stamp_head)

    result = await db_init_module.init_db_when_empty()

    assert result is None
    assert not calls
