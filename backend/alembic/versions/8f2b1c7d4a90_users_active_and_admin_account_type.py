"""users.active and ADMIN account type

Revision ID: 8f2b1c7d4a90
Revises: c1ab44651e79
Create Date: 2026-10-02

Anade el modelo de administracion de usuarios:

* ``users.active`` (BOOLEAN NOT NULL DEFAULT true). Los usuarios inactivos no
  pueden autenticarse (ni obtener token ni usar un JWT ya emitido).
* Valor ``ADMIN`` en el enum ``account_type``. No es un nivel de cuota como
  REGULAR/DEMO: es una autorizacion administrativa explicita.

Ambos cambios son retrocompatibles con el codigo anterior (columna nueva con
default y valor de enum sin usar todavia), por lo que la migracion puede
aplicarse antes de desplegar la imagen nueva.
"""

import sqlalchemy as sa

from alembic import op

revision: str | None = "8f2b1c7d4a90"
down_revision: str | None = "c1ab44651e79"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ``ALTER TYPE ... ADD VALUE`` no puede ejecutarse en una transaccion (y su
    # valor no puede usarse en la misma transaccion donde se anade):
    # ``autocommit_block`` es el patron recomendado por Alembic para enums.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE account_type ADD VALUE IF NOT EXISTS 'ADMIN'")

    op.add_column(
        "users",
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )


def downgrade() -> None:
    op.drop_column("users", "active")
    # PostgreSQL no permite eliminar un valor de un enum. 'ADMIN' permanece en el
    # tipo tras el downgrade; es inocuo porque sin la dependencia desplegada
    # ningun usuario con ese valor obtiene permisos.
