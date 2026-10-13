"""S4.8a-3 §3/§4 — afiliacion del competidor y logo de la academia

Revision ID: f4b8d2a91c05
Revises: c1a4f7e92b08
Create Date: 2026-10-13 09:00:00.000000

Dos cambios aditivos minimos:

* ``domain_change_log``: se **recrean** los dos CHECK del motivo cerrado con la lista ampliada para
  admitir la pareja ``(competitor, UPDATE, AFFILIATION_CHANGE)`` (§3) y la pareja
  ``(sports_club, UPDATE, CLUB_LOGO_CHANGE)`` (§4);
* ``sports_clubs``: columna aditiva ``logo_path`` ``varchar`` NULLABLE (§4). En la base solo vive la
  ruta relativa del logo; el fichero, en disco. Nulable a proposito: una academia sin logo es
  valida.

La lista de codigos y de parejas se repite literalmente y **no** se importa de la aplicacion: una
migracion describe un salto concreto y no debe cambiar porque cambie el codigo vivo. Una prueba
(catalogo contra esquema) compara ambas copias.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str | None = "f4b8d2a91c05"
down_revision: str | None = "c1a4f7e92b08"
branch_labels: str | None = None
depends_on: str | None = None


REASON_CODE_CATALOG_CHECK = "ck_domain_change_log_reason_code_catalog"
REASON_CODE_ACTION_CHECK = "ck_domain_change_log_reason_code_action"

REASON_CODES: tuple[str, ...] = (
    "ADMINISTRATIVE",
    "AFFILIATION_CHANGE",
    "CATEGORY_CHANGE",
    "CLUB_LOGO_CHANGE",
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

REASON_CODE_ACTION_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("competitor", "CREATE", "PLANNED_ENTRY"),
    ("competitor", "UPDATE", "DATA_CORRECTION"),
    ("competitor", "UPDATE", "AFFILIATION_CHANGE"),
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
    ("sports_club", "UPDATE", "CLUB_LOGO_CHANGE"),
)

# Copias **congeladas** del estado previo (§3/§4 no existian): el downgrade tiene que restaurarlas.
PREVIOUS_REASON_CODES: tuple[str, ...] = tuple(
    code for code in REASON_CODES if code not in ("AFFILIATION_CHANGE", "CLUB_LOGO_CHANGE")
)
PREVIOUS_REASON_CODE_ACTION_PAIRS: tuple[tuple[str, str, str], ...] = tuple(
    pair
    for pair in REASON_CODE_ACTION_PAIRS
    if pair[2] not in ("AFFILIATION_CHANGE", "CLUB_LOGO_CHANGE")
)


def _catalog_check(codes: tuple[str, ...]) -> str:
    return (
        "reason_code IS NULL OR reason_code IN (" + ", ".join(f"'{code}'" for code in codes) + ")"
    )


def _action_check(pairs: tuple[tuple[str, str, str], ...]) -> str:
    return (
        "reason_code IS NULL OR (entity, action, reason_code) IN ("
        + ", ".join(f"('{entity}', '{action}', '{code}')" for entity, action, code in pairs)
        + ")"
    )


def upgrade() -> None:
    op.drop_constraint(REASON_CODE_ACTION_CHECK, "domain_change_log", type_="check")
    op.drop_constraint(REASON_CODE_CATALOG_CHECK, "domain_change_log", type_="check")
    op.create_check_constraint(
        REASON_CODE_CATALOG_CHECK, "domain_change_log", _catalog_check(REASON_CODES)
    )
    op.create_check_constraint(
        REASON_CODE_ACTION_CHECK, "domain_change_log", _action_check(REASON_CODE_ACTION_PAIRS)
    )
    op.add_column("sports_clubs", sa.Column("logo_path", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sports_clubs", "logo_path")
    op.drop_constraint(REASON_CODE_ACTION_CHECK, "domain_change_log", type_="check")
    op.drop_constraint(REASON_CODE_CATALOG_CHECK, "domain_change_log", type_="check")
    op.create_check_constraint(
        REASON_CODE_CATALOG_CHECK, "domain_change_log", _catalog_check(PREVIOUS_REASON_CODES)
    )
    op.create_check_constraint(
        REASON_CODE_ACTION_CHECK,
        "domain_change_log",
        _action_check(PREVIOUS_REASON_CODE_ACTION_PAIRS),
    )
