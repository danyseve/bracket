"""S3.3c-3 — almacen de claves de idempotencia por tenant y actor

Revision ID: 9c3e7a1d5b28
Revises: d5c1a7e83f21
Create Date: 2026-10-04 21:40:00.000000

Aditiva: crea unicamente la tabla ``domain_idempotency_keys`` con sus restricciones e indices.
No se anade ni se modifica ninguna columna de otra tabla y no se reescribe ninguna fila del
historico de competicion (torneos, inscripciones, equipos, cuadros): la migracion declara DDL y
nada mas. ``downgrade()`` retira exactamente lo que ``upgrade()`` creo.

Que guarda: la clave que envio el cliente, la huella HMAC de la peticion con la version de su
clave, el metodo y la ruta (identificador de la operacion), el estado, la referencia al recurso y
los metadatos de respuesta **permitidos**. No guarda el cuerpo de la peticion, ni cabeceras, ni
credenciales, ni la respuesta completa.

Politica de borrado: ``ON DELETE CASCADE`` hacia ``clubs`` y ``users``. La fila es un registro
operativo de reconocimiento de peticiones, no un historico inmutable de competicion: se va con su
tenant o con su actor. La auditoria de negocio (``domain_change_log``) mantiene su politica propia.

La tabla nace vacia y **no** hay purga automatica en esta fase: ``expires_at`` es obligatorio
(retencion provisional de 24 h) y una fila caducada no se reutiliza en silencio ni se borra para
liberar la clave. Purga, ventana y rotacion de la clave HMAC son decisiones de S3.3c-4.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str | None = "9c3e7a1d5b28"
down_revision: str | None = "d5c1a7e83f21"
branch_labels: str | None = None
depends_on: str | None = None


TABLE = "domain_idempotency_keys"

TENANT_FOREIGN_KEY = "fk_domain_idempotency_keys_tenant_club_id"
ACTOR_FOREIGN_KEY = "fk_domain_idempotency_keys_actor_user_id"
UNIQUE_CONSTRAINT = "uq_domain_idempotency_keys_tenant_actor_key"
EXPIRY_INDEX = "ix_domain_idempotency_keys_expires_at"

_STATES = ("IN_PROGRESS", "COMPLETED")
_METHODS = ("POST", "PUT", "PATCH", "DELETE")
_RESPONSE_KEYS = (
    "audit_event_id",
    "competitor_id",
    "registration_status",
    "resource_id",
    "resource_type",
)
_KEY_MIN_LENGTH = 8
_KEY_MAX_LENGTH = 255
_RESPONSE_BODY_MAX_BYTES = 2048

_STATE_CHECK = "state IN (" + ", ".join(f"'{value}'" for value in _STATES) + ")"
_PENDING_SHAPE_CHECK = (
    "state <> 'IN_PROGRESS' OR (completed_at IS NULL AND response_status IS NULL"
    " AND response_body IS NULL AND resource_type IS NULL AND resource_id IS NULL)"
)
_COMPLETED_SHAPE_CHECK = (
    "state <> 'COMPLETED' OR (completed_at IS NOT NULL AND response_status IS NOT NULL)"
)
_RESOURCE_COHERENCE_CHECK = "(resource_type IS NULL) = (resource_id IS NULL)"
_METHOD_CHECK = "request_method IN (" + ", ".join(f"'{value}'" for value in _METHODS) + ")"
_PATH_CHECK = "char_length(request_path) BETWEEN 1 AND 255 AND request_path LIKE '/%'"
_KEY_SHAPE_CHECK = (
    f"char_length(idempotency_key) BETWEEN {_KEY_MIN_LENGTH} AND {_KEY_MAX_LENGTH}"
    " AND idempotency_key ~ '^[A-Za-z0-9._:-]+$'"
)
_FINGERPRINT_CHECK = "request_fingerprint ~ '^[0-9a-f]{64}$'"
_KEY_VERSION_CHECK = "fingerprint_key_version ~ '^v[0-9]{1,3}$'"
_RESPONSE_STATUS_CHECK = "response_status IS NULL OR response_status BETWEEN 100 AND 599"
_RESPONSE_BODY_CHECK = (
    "response_body IS NULL OR (jsonb_typeof(response_body) = 'object'"
    " AND response_body - ARRAY["
    + ", ".join(f"'{key}'" for key in _RESPONSE_KEYS)
    + "]::text[] = '{}'::jsonb"
    f" AND octet_length(response_body::text) <= {_RESPONSE_BODY_MAX_BYTES}"
    ' AND NOT jsonb_path_exists(response_body, \'$.* ? (@.type() == "object"'
    ' || @.type() == "array")\'))'
)
_EXPIRY_CHECK = "expires_at > created"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_club_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=_KEY_MAX_LENGTH), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("fingerprint_key_version", sa.String(length=16), nullable=False),
        sa.Column("request_method", sa.String(length=10), nullable=False),
        sa.Column("request_path", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=True),
        sa.Column("resource_id", sa.BigInteger(), nullable=True),
        sa.Column("response_status", sa.SmallInteger(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_STATE_CHECK, name="ck_domain_idempotency_keys_state"),
        sa.CheckConstraint(_PENDING_SHAPE_CHECK, name="ck_domain_idempotency_keys_pending_shape"),
        sa.CheckConstraint(
            _COMPLETED_SHAPE_CHECK, name="ck_domain_idempotency_keys_completed_shape"
        ),
        sa.CheckConstraint(
            _RESOURCE_COHERENCE_CHECK, name="ck_domain_idempotency_keys_resource_coherence"
        ),
        sa.CheckConstraint(_METHOD_CHECK, name="ck_domain_idempotency_keys_request_method"),
        sa.CheckConstraint(_PATH_CHECK, name="ck_domain_idempotency_keys_request_path"),
        sa.CheckConstraint(_KEY_SHAPE_CHECK, name="ck_domain_idempotency_keys_key_shape"),
        sa.CheckConstraint(_FINGERPRINT_CHECK, name="ck_domain_idempotency_keys_fingerprint"),
        sa.CheckConstraint(_KEY_VERSION_CHECK, name="ck_domain_idempotency_keys_key_version"),
        sa.CheckConstraint(
            _RESPONSE_STATUS_CHECK, name="ck_domain_idempotency_keys_response_status"
        ),
        sa.CheckConstraint(_RESPONSE_BODY_CHECK, name="ck_domain_idempotency_keys_response_body"),
        sa.CheckConstraint(_EXPIRY_CHECK, name="ck_domain_idempotency_keys_expiry"),
        sa.ForeignKeyConstraint(
            ["tenant_club_id"], ["clubs.id"], name=TENANT_FOREIGN_KEY, ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], name=ACTOR_FOREIGN_KEY, ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_club_id", "actor_user_id", "idempotency_key", name=UNIQUE_CONSTRAINT
        ),
    )
    op.create_index(op.f("ix_domain_idempotency_keys_id"), TABLE, ["id"], unique=False)
    op.create_index(
        op.f("ix_domain_idempotency_keys_tenant_club_id"), TABLE, ["tenant_club_id"], unique=False
    )
    op.create_index(EXPIRY_INDEX, TABLE, ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index(EXPIRY_INDEX, table_name=TABLE)
    op.drop_index(op.f("ix_domain_idempotency_keys_tenant_club_id"), table_name=TABLE)
    op.drop_index(op.f("ix_domain_idempotency_keys_id"), table_name=TABLE)
    op.drop_table(TABLE)
