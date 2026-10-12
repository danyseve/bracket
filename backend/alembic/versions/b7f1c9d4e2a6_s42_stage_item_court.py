"""S4.2 — tatami explicito del cuadro (asignacion persistida)

Revision ID: b7f1c9d4e2a6
Revises: a4e8d1c60f37
Create Date: 2026-10-12 20:00:00.000000

Aditiva: anade **una** columna nulable a ``stage_items``. No se anade ninguna tabla, ni
restriccion, ni indice, ni se reescribe ninguna fila existente: la migracion declara DDL y nada
mas, y ``downgrade()`` retira exactamente lo que ``upgrade()`` creo.

Que guarda la columna:

* ``stage_items.court_id`` (``bigint``, nulable, clave foranea a ``courts.id``): el tatami donde
  se disputa el cuadro entero. Nula significa "sin tatami asignado" (el estado en el que estan
  todos los cuadros existentes), no "asignado al tatami 0".

Por que no se apana el modelo legado: ``matches.court_id`` sigue siendo la verdad de cada combate
y la operacion de dominio **propaga** ahi la decision del cuadro. La columna nueva no duplica esa
relacion, la resume: 1 cuadro -> 1 tatami.

Sin indice unico, a proposito: un tatami aloja varios cuadros, asi que la exclusividad no existe
en este dominio y un indice unico seria una regla inventada. Tampoco se hace backfill: los
cuadros anteriores se quedan sin tatami explicito y su verdad siguen siendo sus combates, que es
exactamente lo que hacian antes de esta migracion. La columna es compatible con la imagen
anterior: quien no la conoce la ignora y sigue leyendo ``matches.court_id``.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str | None = "b7f1c9d4e2a6"
down_revision: str | None = "a4e8d1c60f37"
branch_labels: str | None = None
depends_on: str | None = None


TABLE = "stage_items"
COURT_COLUMN = "court_id"
COURTS_TABLE = "courts"
FOREIGN_KEY_NAME = "stage_items_court_id_fkey"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column(COURT_COLUMN, sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        FOREIGN_KEY_NAME, TABLE, COURTS_TABLE, [COURT_COLUMN], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint(FOREIGN_KEY_NAME, TABLE, type_="foreignkey")
    op.drop_column(TABLE, COURT_COLUMN)
