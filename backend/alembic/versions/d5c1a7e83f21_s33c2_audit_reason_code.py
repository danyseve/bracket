"""S3.3c-2 — motivo cerrado (codigo + nota) en la auditoria de dominio

Revision ID: d5c1a7e83f21
Revises: b3f7a1c92e04
Create Date: 2026-10-04 20:40:00.000000

Anade a ``domain_change_log`` el motivo estructurado de cada evento:

* ``reason_code`` — ``String(32)``, NULLABLE, con dos CHECK: pertenencia al catalogo cerrado y
  coherencia de la pareja ``(entity, action, reason_code)``;
* ``reason_note`` — ``Text``, NULLABLE, con un CHECK que la limita a los codigos que la admiten, la
  exige no vacia, sin espacios en los extremos y sin caracteres de control, y la acota a 200
  caracteres (``char_length``, no bytes).

Nulabilidad deliberada, igual que en S3.3a. NULL en ``reason_code`` significa "fila anterior a esta
migracion" —no se inventa atribucion— y ademas cubre las entidades de plataforma sin catalogo
propio (``sports_club``), que el escritor interno sigue auditando sin codigo. La nulabilidad no
debilita la garantia: los CHECK cierran el conjunto de valores alli donde el codigo existe, y la
lista literal de esta migracion se compara con el catalogo de la aplicacion en la suite.

Sin backfill, a diferencia de S3.3a. El tenant de una fila historica es un hecho reconstruible
(la entidad lo conoce); el motivo no: reinterpretar texto libre para deducir un codigo inventaria
el motivo de un hecho ya ocurrido. Las filas historicas conservan su ``reason`` original y quedan
con ``reason_code IS NULL``, que es el unico valor honesto.

La lista de codigos y de parejas se repite literalmente y **no** se importa de la aplicacion: una
migracion describe un salto concreto y no debe cambiar porque cambie el codigo vivo. Una prueba
(catalogo contra esquema) compara ambas copias.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str | None = "d5c1a7e83f21"
down_revision: str | None = "b3f7a1c92e04"
branch_labels: str | None = None
depends_on: str | None = None


REASON_CODE_COLUMN = "reason_code"
REASON_NOTE_COLUMN = "reason_note"
REASON_CODE_CATALOG_CHECK = "ck_domain_change_log_reason_code_catalog"
REASON_CODE_ACTION_CHECK = "ck_domain_change_log_reason_code_action"
REASON_NOTE_CHECK = "ck_domain_change_log_reason_note"
REASON_CODE_MAX_LENGTH = 32
REASON_NOTE_MAX_LENGTH = 200

REASON_CODES: tuple[str, ...] = (
    "ADMINISTRATIVE",
    "CATEGORY_CHANGE",
    "DATA_CORRECTION",
    "DATA_ERROR",
    "DATA_VERIFIED",
    "DUPLICATE",
    "ELIGIBILITY",
    "ELIGIBILITY_LOST",
    "ELIGIBILITY_REGAINED",
    "MISTAKEN_WITHDRAWAL",
    "PLANNED_ENTRY",
    "READY",
    "REPRESENTATION_CHANGE",
    "REQUESTED_BY_ATHLETE",
    "RULE_VIOLATION",
    "SCHEDULING_NO_SHOW",
    "WITHDRAWAL_REQUEST",
)

REASON_NOTE_ALLOWED_CODES: tuple[str, ...] = ("ADMINISTRATIVE", "DATA_ERROR", "RULE_VIOLATION")

REASON_CODE_ACTION_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("competitor", "CREATE", "PLANNED_ENTRY"),
    ("competitor", "UPDATE", "DATA_CORRECTION"),
    ("competitor", "DEACTIVATE", "REQUESTED_BY_ATHLETE"),
    ("competitor", "DEACTIVATE", "DUPLICATE"),
    ("competitor", "DEACTIVATE", "DATA_ERROR"),
    ("competitor", "DEACTIVATE", "ADMINISTRATIVE"),
    ("competitor", "ACTIVATE", "REQUESTED_BY_ATHLETE"),
    ("competitor", "ACTIVATE", "DUPLICATE"),
    ("competitor", "ACTIVATE", "DATA_ERROR"),
    ("competitor", "ACTIVATE", "ADMINISTRATIVE"),
    ("tournament_registration", "CREATE", "PLANNED_ENTRY"),
    ("tournament_registration", "UPDATE", "DATA_CORRECTION"),
    ("tournament_registration", "UPDATE", "CATEGORY_CHANGE"),
    ("tournament_registration", "UPDATE", "REPRESENTATION_CHANGE"),
    ("tournament_registration", "CONFIRM", "READY"),
    ("tournament_registration", "CONFIRM", "DATA_VERIFIED"),
    ("tournament_registration", "WITHDRAW", "WITHDRAWAL_REQUEST"),
    ("tournament_registration", "WITHDRAW", "DUPLICATE"),
    ("tournament_registration", "WITHDRAW", "ELIGIBILITY_LOST"),
    ("tournament_registration", "WITHDRAW", "ADMINISTRATIVE"),
    ("tournament_registration", "REINSTATE", "MISTAKEN_WITHDRAWAL"),
    ("tournament_registration", "REINSTATE", "ELIGIBILITY_REGAINED"),
    ("tournament_registration", "REINSTATE", "ADMINISTRATIVE"),
    ("tournament_registration", "DISQUALIFY", "SCHEDULING_NO_SHOW"),
    ("tournament_registration", "DISQUALIFY", "RULE_VIOLATION"),
    ("tournament_registration", "DISQUALIFY", "ELIGIBILITY"),
    ("tournament_registration", "DISQUALIFY", "ADMINISTRATIVE"),
)

_CATALOG_CHECK = (
    "reason_code IS NULL OR reason_code IN ("
    + ", ".join(f"'{code}'" for code in REASON_CODES)
    + ")"
)
_ACTION_CHECK = (
    "reason_code IS NULL OR (entity, action, reason_code) IN ("
    + ", ".join(
        f"('{entity}', '{action}', '{code}')" for entity, action, code in REASON_CODE_ACTION_PAIRS
    )
    + ")"
)
# La nota es texto humano corto y sin forma de dato: nada vacio, nada que empiece o acabe en
# espacio, nada de caracteres de control y solo en los codigos que la admiten.
_NOTE_CHECK = (
    "reason_note IS NULL OR (reason_code IN ("
    + ", ".join(f"'{code}'" for code in REASON_NOTE_ALLOWED_CODES)
    + f") AND char_length(reason_note) BETWEEN 1 AND {REASON_NOTE_MAX_LENGTH}"
    " AND reason_note = btrim(reason_note) AND reason_note !~ '[[:cntrl:]]')"
)


def upgrade() -> None:
    op.add_column(
        "domain_change_log",
        sa.Column(REASON_CODE_COLUMN, sa.String(REASON_CODE_MAX_LENGTH), nullable=True),
    )
    op.add_column("domain_change_log", sa.Column(REASON_NOTE_COLUMN, sa.Text(), nullable=True))
    op.create_check_constraint(REASON_CODE_CATALOG_CHECK, "domain_change_log", _CATALOG_CHECK)
    op.create_check_constraint(REASON_CODE_ACTION_CHECK, "domain_change_log", _ACTION_CHECK)
    op.create_check_constraint(REASON_NOTE_CHECK, "domain_change_log", _NOTE_CHECK)


def downgrade() -> None:
    op.drop_constraint(REASON_NOTE_CHECK, "domain_change_log", type_="check")
    op.drop_constraint(REASON_CODE_ACTION_CHECK, "domain_change_log", type_="check")
    op.drop_constraint(REASON_CODE_CATALOG_CHECK, "domain_change_log", type_="check")
    op.drop_column("domain_change_log", REASON_NOTE_COLUMN)
    op.drop_column("domain_change_log", REASON_CODE_COLUMN)
