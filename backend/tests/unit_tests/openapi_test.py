import json
from pathlib import Path

from bracket.app import app
from openapi import openapi  # noqa: F401


def test_openapi_up_to_date() -> None:
    schema = app.openapi()
    if Path("openapi/openapi.json").read_text() != json.dumps(schema, indent=2, sort_keys=True):
        raise Exception("OpenAPI schema is out of date")


def test_openapi_preserve_assigned_documents_its_semantics() -> None:
    """S4.6 §7: el campo conserva sus dos comportamientos y la ambiguedad queda cerrada.

    ``preserve_assigned`` decide si una asignacion explicita se conserva o se puede replanificar.
    El ``default`` no cambia (True) y la descripcion debe decir las dos cosas en el propio esquema,
    que es lo unico que ve un consumidor de la API.
    """
    schema = app.openapi()
    body = schema["components"]["schemas"]["ScheduleGenerateBody"]
    field = body["properties"]["preserve_assigned"]
    description = field.get("description") or ""
    assert field["default"] is True
    assert "true" in description and "false" in description, description
    assert "conserva" in description, description
    assert "replanific" in description, description
