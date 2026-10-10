"""S3.4c — ventana de inscripcion del torneo (plazo y cierre manual)

Revision ID: a4e8d1c60f37
Revises: 9c3e7a1d5b28
Create Date: 2026-10-10 18:00:00.000000

Aditiva: anade unicamente dos columnas nulables a ``tournaments``. No se anade ninguna tabla,
ni restriccion, ni indice, ni clave foranea, y no se reescribe ninguna fila del historico de
competicion: la migracion declara DDL y nada mas. ``downgrade()`` retira exactamente lo que
``upgrade()`` creo.

Que guarda cada columna:

* ``registration_deadline`` (``timestamptz``, nulable): hora limite de inscripcion del torneo.
  Nula significa "sin plazo", no "cerrado ya": el estado de la ventana **se deriva** de las dos
  columnas y nunca se almacena, para que no pueda quedar incoherente con sus causas.
* ``registrations_closed_at`` (``timestamptz``, nulable): cierre manual explicito. Nula
  significa "sin cierre manual".

Quien cerro y cuando queda en ``domain_change_log`` (``entity='tournament'``), que ya es la
fuente de auditoria: no se duplica el actor en ``tournaments`` ni se inventa un enum de estado
que pudiera contradecir a las columnas. La ventana del torneo es independiente de
``tournaments.status`` (``OPEN``/``ARCHIVED``): son cosas distintas.

La tabla nace sin datos nuevos y las dos columnas quedan nulas en las filas existentes, asi que
el despliegue de la migracion no cambia la semantica de ningun torneo anterior: todos siguen
con la ventana abierta hasta que se les fije un plazo o se cierren a mano. No hay relleno
(backfill) ni dato derivado que calcular.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str | None = "a4e8d1c60f37"
down_revision: str | None = "9c3e7a1d5b28"
branch_labels: str | None = None
depends_on: str | None = None


TABLE = "tournaments"
DEADLINE_COLUMN = "registration_deadline"
CLOSED_AT_COLUMN = "registrations_closed_at"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column(DEADLINE_COLUMN, sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        TABLE,
        sa.Column(CLOSED_AT_COLUMN, sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column(TABLE, CLOSED_AT_COLUMN)
    op.drop_column(TABLE, DEADLINE_COLUMN)
