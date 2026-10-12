"""S4.5 — claves permitidas del cuerpo de respuesta del almacen de idempotencia

Revision ID: b2f5a7c31d90
Revises: e4b8d1a70c52
Create Date: 2026-10-12 23:10:00.000000

Recrea **una** restriccion de comprobacion (``ck_domain_idempotency_keys_response_body``) para
admitir las claves del contrato de respuesta del planificador de torneo (S4.5): ``tournament_id``,
``court_count``, ``scheduled_match_count``, ``moved_match_count``, ``manual_match_count``,
``unscheduled_match_count`` y ``start_time``. No anade ni quita columnas, no reescribe filas y no
cambia el limite de tamano (2048 bytes).

Por que: el contrato de idempotencia exige que la repeticion de una operacion ya confirmada devuelva
**la respuesta original**, incluido cuantos combates movio *esa* ejecucion y con cuantos tatamis se
planifico. Ese efecto no se puede reconstruir del estado actual (una segunda generacion mueve cero
combates), asi que viaja con el resultado confirmado. La lista sigue siendo cerrada (una clave de
fuera se rechaza) y los valores se revalidan al reenviarlos. ``downgrade()`` restaura exactamente
la lista anterior.

La tabla esta vacia en laboratorio (0 reservas) y el planificador todavia no esta publicado, asi que
no hay filas que reescribir y la restriccion se puede sustituir sin ventana de compatibilidad.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str | None = "b2f5a7c31d90"
down_revision: str | None = "e4b8d1a70c52"
branch_labels: str | None = None
depends_on: str | None = None

TABLE = "domain_idempotency_keys"
CONSTRAINT = "ck_domain_idempotency_keys_response_body"
RESPONSE_BODY_MAX_BYTES = 2048

#: Claves admitidas antes de esta migracion (S4.3).
PREVIOUS_KEYS: tuple[str, ...] = (
    "affected_match_count",
    "audit_event_id",
    "competitor_id",
    "court_id",
    "court_name",
    "registration_status",
    "resource_id",
    "resource_type",
    "stage_item_id",
)

#: Claves admitidas despues: las anteriores mas el contrato del planificador de torneo.
CURRENT_KEYS: tuple[str, ...] = (
    "affected_match_count",
    "audit_event_id",
    "competitor_id",
    "court_count",
    "court_id",
    "court_name",
    "manual_match_count",
    "moved_match_count",
    "registration_status",
    "resource_id",
    "resource_type",
    "scheduled_match_count",
    "stage_item_id",
    "start_time",
    "tournament_id",
    "unscheduled_match_count",
)


def _response_body_check(keys: tuple[str, ...]) -> str:
    """Misma expresion que declara el modelo, con la lista de claves que se le pase."""
    return (
        "response_body IS NULL OR (jsonb_typeof(response_body) = 'object'"
        " AND response_body - ARRAY["
        + ", ".join(f"'{key}'" for key in keys)
        + "]::text[] = '{}'::jsonb"
        f" AND octet_length(response_body::text) <= {RESPONSE_BODY_MAX_BYTES}"
        ' AND NOT jsonb_path_exists(response_body, \'$.* ? (@.type() == "object"'
        ' || @.type() == "array")\'))'
    )


def _replace(keys: tuple[str, ...]) -> None:
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.create_check_constraint(CONSTRAINT, TABLE, _response_body_check(keys))


def upgrade() -> None:
    _replace(CURRENT_KEYS)


def downgrade() -> None:
    _replace(PREVIOUS_KEYS)
