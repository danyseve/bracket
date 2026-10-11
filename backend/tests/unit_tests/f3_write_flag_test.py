"""Bandera de escritura de F3 (S3.6b, F3B, LAB ONLY).

TDD: este modulo se escribio **antes** de la implementacion y fija el contrato descrito en
``docs/26`` §47. Lo que la bandera garantiza:

* el campo nace **apagado** (``F3_WRITE_ENABLED=false``) y se lee de la variable documentada;
* apagada, la puerta falla cerrado con ``503 F3_WRITE_DISABLED``, un codigo estable que no dice
  nada de la operacion ni del llamante (no es 403: no es un permiso);
* encendida, la puerta no levanta nada y la autorizacion de cada operacion sigue siendo la suya.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette import status

from bracket.config import Config, config
from bracket.routes.f3_write_gate import F3_WRITE_DISABLED, require_f3_write_enabled

#: Secreto sintetico del laboratorio: no sale de aqui y no es un valor de despliegue.
SYNTHETIC_JWT_SECRET = "synthetic-laboratory-secret-not-for-deployment"


def test_f3_write_flag_is_disabled_by_default() -> None:
    """El campo nace apagado: desplegar la imagen de F3 no habilita escrituras por si solo."""
    field = Config.model_fields.get("f3_write_enabled")
    assert field is not None
    assert field.default is False


def test_f3_write_flag_reads_its_documented_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """La variable documentada es ``F3_WRITE_ENABLED``; el despliegue la inyecta por entorno."""
    monkeypatch.setenv("F3_WRITE_ENABLED", "true")

    assert Config(jwt_secret=SYNTHETIC_JWT_SECRET).f3_write_enabled is True


def test_f3_write_flag_is_not_the_bridge_flag() -> None:
    """No reutiliza la bandera del Bridge API: son campos distintos y sin acoplamiento."""
    assert Config.model_fields.get("f3_write_enabled") is not None
    assert Config.model_fields.get("bracket_result_write_enabled") is None


def test_gate_fails_closed_with_a_stable_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apagada, la puerta responde 503 con el codigo estable y nada mas (sin internals)."""
    monkeypatch.setattr(config, "f3_write_enabled", False)

    with pytest.raises(HTTPException) as captured:
        require_f3_write_enabled()

    assert captured.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert captured.value.detail == F3_WRITE_DISABLED
    assert captured.value.headers is None


def test_gate_does_not_stand_in_the_way_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Encendida, la puerta no decide nada: la ruta sigue con su autorizacion normal."""
    monkeypatch.setattr(config, "f3_write_enabled", True)

    require_f3_write_enabled()
