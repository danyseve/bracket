"""S4.8g-2 §14 — catalogo de auditoria del club deportivo (alta, renombre y estado)

Revision ID: b7d4e019ca62
Revises: f4b8d2a91c05
Create Date: 2026-10-14 13:20:00.000000

Cambio aditivo minimo, **solo constraint/catalogo**:

* ``domain_change_log``: se recrean los dos CHECK del motivo cerrado para admitir las tres parejas
  nuevas del club deportivo — ``(sports_club, CREATE, CLUB_CREATE)``,
  ``(sports_club, UPDATE, CLUB_RENAME)`` y
  ``(sports_club, UPDATE, CLUB_ACTIVE_CHANGE)`` — y los tres
  codigos nuevos en el catalogo (``CLUB_CREATE``, ``CLUB_RENAME``, ``CLUB_ACTIVE_CHANGE``). Se
  conservan todos los codigos y parejas que ya permitia ``f4b8d2a91c05``, incluida
  ``(sports_club, UPDATE, CLUB_LOGO_CHANGE)``.

No hay cambio de datos, ni de tablas, ni de columnas, ni de claves ajenas: el alta, el renombre y el
cambio de estado del club son operaciones nuevas que necesitan su vocabulario de auditoria, no una
forma distinta de guardar lo que ya existe.

Las listas se repiten literalmente y **no** se importan de la aplicacion: una migracion describe un
salto concreto. ``PREVIOUS_*`` son copias congeladas del estado de ``f4b8d2a91c05``, de modo que el
``downgrade`` restaura exactamente el CHECK anterior.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str | None = "b7d4e019ca62"
down_revision: str | None = "f4b8d2a91c05"
branch_labels: str | None = None
depends_on: str | None = None

REASON_CODE_CATALOG_CHECK = "ck_domain_change_log_reason_code_catalog"
REASON_CODE_ACTION_CHECK = "ck_domain_change_log_reason_code_action"

#: Catalogo con los tres codigos del club deportivo (S4.8g-2 §14).
REASON_CODES: tuple[str, ...] = (
    "ADMINISTRATIVE",
    "AFFILIATION_CHANGE",
    "CATEGORY_CHANGE",
    "CLUB_ACTIVE_CHANGE",
    "CLUB_CREATE",
    "CLUB_LOGO_CHANGE",
    "CLUB_RENAME",
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

#: Parejas (entidad, accion, codigo) admitidas: las de ``f4b8d2a91c05`` mas las tres del club.
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
    ("sports_club", "CREATE", "CLUB_CREATE"),
    ("sports_club", "UPDATE", "CLUB_ACTIVE_CHANGE"),
    ("sports_club", "UPDATE", "CLUB_LOGO_CHANGE"),
    ("sports_club", "UPDATE", "CLUB_RENAME"),
)

# Copias **congeladas** del estado previo: el downgrade tiene que restaurarlo tal cual.
PREVIOUS_REASON_CODES: tuple[str, ...] = tuple(
    code
    for code in REASON_CODES
    if code not in ("CLUB_ACTIVE_CHANGE", "CLUB_CREATE", "CLUB_RENAME")
)
PREVIOUS_REASON_CODE_ACTION_PAIRS: tuple[tuple[str, str, str], ...] = tuple(
    pair
    for pair in REASON_CODE_ACTION_PAIRS
    if pair[2] not in ("CLUB_CREATE", "CLUB_RENAME", "CLUB_ACTIVE_CHANGE")
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


def downgrade() -> None:
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
