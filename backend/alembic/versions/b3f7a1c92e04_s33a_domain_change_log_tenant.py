"""S3.3a — atribucion explicita de tenant en la auditoria F3

Revision ID: b3f7a1c92e04
Revises: f3a0c1d2e3f4
Create Date: 2026-10-04 15:10:00.000000

Anade ``domain_change_log.tenant_club_id``: columna aditiva, NULLABLE, con FK a ``clubs(id)``
``ON DELETE RESTRICT`` (misma politica que el resto de claves de tenant de F3A) e indice
compuesto ``(tenant_club_id, created)``.

La nulabilidad es deliberada. NULL significa "anterior a la migracion / sin tenant derivable",
nunca "sin tenant": las filas historicas no tienen tenant reconstruible y las entidades de
plataforma (``competitors`` / ``sports_clubs`` con tenant NULL) no pertenecen a ningun tenant.
Por eso tampoco se pone ``NOT NULL``. La coherencia "tenant del evento == tenant de su entidad"
no es expresable con un CHECK simple porque cruza tablas: la garantiza la capa de escritura
(``sql_insert_domain_change_log``) y la verifica la suite.

Backfill de mejor esfuerzo, determinista e idempotente: cada sentencia toca solo filas con
``tenant_club_id IS NULL`` y toma el tenant de la propia entidad, asi que reejecutarla no
cambia nada y una entidad eliminada deja la fila en NULL. Se ejecuta dentro de la misma
transaccion que el DDL (Alembic abre una transaccion por migracion en PostgreSQL): no hay
commits parciales silenciosos. No se procesa por lotes porque la tabla de auditoria solo
recibe escrituras de F3B y su tamano es acotado; si creciera, el mecanismo seria
``context.autocommit_block()`` con lotes explicitos y su propia justificacion.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str | None = "b3f7a1c92e04"
down_revision: str | None = "f3a0c1d2e3f4"
branch_labels: str | None = None
depends_on: str | None = None


TENANT_COLUMN = "tenant_club_id"
TENANT_FOREIGN_KEY = "fk_domain_change_log_tenant_club_id"
TENANT_INDEX = "ix_domain_change_log_tenant_created"

# Backfill por entidad. Determinista (no depende del orden de las filas) e idempotente
# (la condicion IS NULL lo hace reejecutable). Una entidad inexistente o sin tenant deja la
# fila historica en NULL, que es el unico valor honesto: no se inventa atribucion.
BACKFILL_STATEMENTS: tuple[str, ...] = (
    """
    UPDATE domain_change_log
    SET tenant_club_id = competitors.managed_by_club_id
    FROM competitors
    WHERE domain_change_log.entity = 'competitor'
      AND domain_change_log.entity_id = competitors.id
      AND domain_change_log.tenant_club_id IS NULL
      AND competitors.managed_by_club_id IS NOT NULL
    """,
    """
    UPDATE domain_change_log
    SET tenant_club_id = sports_clubs.tenant_club_id
    FROM sports_clubs
    WHERE domain_change_log.entity = 'sports_club'
      AND domain_change_log.entity_id = sports_clubs.id
      AND domain_change_log.tenant_club_id IS NULL
      AND sports_clubs.tenant_club_id IS NOT NULL
    """,
    """
    UPDATE domain_change_log
    SET tenant_club_id = tournaments.club_id
    FROM tournament_registrations
    JOIN tournaments ON tournaments.id = tournament_registrations.tournament_id
    WHERE domain_change_log.entity = 'tournament_registration'
      AND domain_change_log.entity_id = tournament_registrations.id
      AND domain_change_log.tenant_club_id IS NULL
    """,
)


def upgrade() -> None:
    op.add_column("domain_change_log", sa.Column(TENANT_COLUMN, sa.BigInteger(), nullable=True))
    # El backfill va antes del FK y del indice: parte de datos ya validos por las propias FK de
    # la entidad, y asi la creacion de la restriccion y del indice se hace sobre datos estables.
    for statement in BACKFILL_STATEMENTS:
        op.execute(statement)
    op.create_foreign_key(
        TENANT_FOREIGN_KEY,
        "domain_change_log",
        "clubs",
        [TENANT_COLUMN],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(TENANT_INDEX, "domain_change_log", [TENANT_COLUMN, "created"], unique=False)


def downgrade() -> None:
    op.drop_index(TENANT_INDEX, table_name="domain_change_log")
    op.drop_constraint(TENANT_FOREIGN_KEY, "domain_change_log", type_="foreignkey")
    op.drop_column("domain_change_log", TENANT_COLUMN)
