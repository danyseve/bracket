"""S4.8a-1 — esquema de clasificacion: catalogo de cinturones, reglas de categoria y datos basicos

Revision ID: c1a4f7e92b08
Revises: b2f5a7c31d90
Create Date: 2026-10-13 00:00:00.000000

Que anade (todo **aditivo**; no reescribe ninguna fila existente):

* el catalogo cerrado ``belts`` (diez filas: cinco cinturones juveniles y cinco adultos), con su
  clave natural por escala ``(scope, code)`` y su orden ``(scope, rank)``;
* cuatro tipos enumerados cerrados: ``belt_scope``, ``competitor_sex`` (``MALE``/``FEMALE``: el
  sexo de una persona nunca es ``MIXED``), ``sex_constraint`` (``MALE``/``FEMALE``/``MIXED``: la
  restriccion de una **categoria** si puede ser mixta) y ``modality`` (``GI``/``NO_GI``);
* reglas de clasificacion **nulables** en ``stage_items``: restriccion de sexo, rango de cinturon
  (``belt_min_id``/``belt_max_id``) y rango de edad y de peso;
* datos basicos del competidor: ``date_of_birth``, ``sex`` y ``belt_id`` (los tres nulables: la
  edad es derivada y no se guarda);
* peso declarado y modalidad de la inscripcion: ``declared_weight_kg`` (``NUMERIC(5,2)``, kg,
  ``0 < peso <= 300``) y ``modality``;
* logo opcional del club: ``clubs.logo_path`` (la ruta relativa, como en torneos y equipos).

Por que: un competidor deja de ser solo un nombre. Para clasificar una inscripcion hacen falta
datos permanentes del competidor (nacimiento, sexo, cinturon) y datos propios de cada torneo
(peso, modalidad, categoria), y las categorias necesitan reglas **estructuradas**, no un nombre
como "-71 kg" que nadie puede comparar. Todo lo anadido es nulable porque los cuadros y
competidores existentes no tienen ese dato: ``NULL`` significa "sin restriccion conocida" y nunca
se inventa un valor deportivo.

Dentro de la base solo se impone lo que PostgreSQL puede garantizar por si mismo (rangos de edad y
de peso, acotacion del peso declarado, unicidad del catalogo y ``RESTRICT`` al borrar un cinturon en
uso). La coherencia de un rango de cinturon (misma escala y minimo no superior al maximo) es
cruzada entre filas del catalogo y la comprueba la capa de dominio.

``downgrade()`` retira exactamente lo anadido y deja la base como estaba: ni una fila de negocio se
pierde, porque esta revision no toca ninguna fila de negocio.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str | None = "c1a4f7e92b08"
down_revision: str | None = "b2f5a7c31d90"
branch_labels: str | None = None
depends_on: str | None = None

BELT_SCOPE = "belt_scope"
COMPETITOR_SEX = "competitor_sex"
SEX_CONSTRAINT = "sex_constraint"
MODALITY = "modality"

#: Etiquetas de cada tipo enumerado, en el orden declarado (el mismo que ``bracket/schema.py``).
ENUM_LABELS: dict[str, tuple[str, ...]] = {
    BELT_SCOPE: ("JUVENILE", "ADULT"),
    COMPETITOR_SEX: ("MALE", "FEMALE"),
    SEX_CONSTRAINT: ("MALE", "FEMALE", "MIXED"),
    MODALITY: ("GI", "NO_GI"),
}

#: Catalogo sembrado: ``scope``, ``code``, ``label``, ``rank``. El mismo color puede existir en
#: las dos escalas (juvenil y adulta): son escalas distintas, no una lista unica.
BELT_SEED: tuple[tuple[str, str, str, int], ...] = (
    ("JUVENILE", "WHITE", "White", 1),
    ("JUVENILE", "GREY", "Grey", 2),
    ("JUVENILE", "YELLOW", "Yellow", 3),
    ("JUVENILE", "ORANGE", "Orange", 4),
    ("JUVENILE", "GREEN", "Green", 5),
    ("ADULT", "WHITE", "White", 1),
    ("ADULT", "BLUE", "Blue", 2),
    ("ADULT", "PURPLE", "Purple", 3),
    ("ADULT", "BROWN", "Brown", 4),
    ("ADULT", "BLACK", "Black", 5),
)

#: Columnas de reglas anadidas a ``stage_items``, en orden de retirada inversa.
STAGE_ITEM_RULE_COLUMNS: tuple[str, ...] = (
    "sex_constraint",
    "belt_min_id",
    "belt_max_id",
    "age_min",
    "age_max",
    "weight_min_kg",
    "weight_max_kg",
    "modality",
)


def _enum(name: str) -> postgresql.ENUM:
    """Tipo enumerado del laboratorio, ya creado por ``upgrade()`` (no lo vuelve a crear)."""
    return postgresql.ENUM(*ENUM_LABELS[name], name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for name in ENUM_LABELS:
        postgresql.ENUM(*ENUM_LABELS[name], name=name).create(bind, checkfirst=True)

    op.create_table(
        "belts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("scope", _enum(BELT_SCOPE), nullable=False),
        sa.Column("active", sa.Boolean(), server_default="t", nullable=False),
        sa.Column(
            "created", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope", "code", name="uq_belts_scope_code"),
        sa.UniqueConstraint("scope", "rank", name="uq_belts_scope_rank"),
    )
    op.create_index("ix_belts_active", "belts", ["active"], unique=False)
    op.create_index("ix_belts_id", "belts", ["id"], unique=False)

    op.execute(
        sa.text(
            "INSERT INTO belts (code, label, rank, scope) VALUES "
            + ", ".join(
                f"('{code}', '{label}', {rank}, '{scope}')"
                for scope, code, label, rank in BELT_SEED
            )
        )
    )

    op.add_column("clubs", sa.Column("logo_path", sa.String(), nullable=True))

    op.add_column("stage_items", sa.Column("sex_constraint", _enum(SEX_CONSTRAINT), nullable=True))
    op.add_column("stage_items", sa.Column("belt_min_id", sa.BigInteger(), nullable=True))
    op.add_column("stage_items", sa.Column("belt_max_id", sa.BigInteger(), nullable=True))
    op.add_column("stage_items", sa.Column("age_min", sa.Integer(), nullable=True))
    op.add_column("stage_items", sa.Column("age_max", sa.Integer(), nullable=True))
    op.add_column("stage_items", sa.Column("weight_min_kg", sa.Numeric(5, 2), nullable=True))
    op.add_column("stage_items", sa.Column("weight_max_kg", sa.Numeric(5, 2), nullable=True))
    op.add_column("stage_items", sa.Column("modality", _enum(MODALITY), nullable=True))
    op.create_foreign_key(
        "stage_items_belt_min_id_fkey",
        "stage_items",
        "belts",
        ["belt_min_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "stage_items_belt_max_id_fkey",
        "stage_items",
        "belts",
        ["belt_max_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_stage_items_age_range",
        "stage_items",
        "(age_min IS NULL OR age_min >= 0)"
        " AND (age_max IS NULL OR age_max >= 0)"
        " AND (age_min IS NULL OR age_max IS NULL OR age_min <= age_max)",
    )
    op.create_check_constraint(
        "ck_stage_items_weight_range",
        "stage_items",
        "(weight_min_kg IS NULL OR (weight_min_kg > 0 AND weight_min_kg <= 300))"
        " AND (weight_max_kg IS NULL OR (weight_max_kg > 0 AND weight_max_kg <= 300))"
        " AND (weight_min_kg IS NULL OR weight_max_kg IS NULL OR weight_min_kg <= weight_max_kg)",
    )

    op.add_column("competitors", sa.Column("date_of_birth", sa.Date(), nullable=True))
    op.add_column("competitors", sa.Column("sex", _enum(COMPETITOR_SEX), nullable=True))
    op.add_column("competitors", sa.Column("belt_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "competitors_belt_id_fkey",
        "competitors",
        "belts",
        ["belt_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.add_column(
        "tournament_registrations",
        sa.Column("declared_weight_kg", sa.Numeric(5, 2), nullable=True),
    )
    op.add_column("tournament_registrations", sa.Column("modality", _enum(MODALITY), nullable=True))
    op.create_check_constraint(
        "ck_tournament_registrations_declared_weight",
        "tournament_registrations",
        "declared_weight_kg IS NULL OR (declared_weight_kg > 0 AND declared_weight_kg <= 300)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_tournament_registrations_declared_weight",
        "tournament_registrations",
        type_="check",
    )
    op.drop_column("tournament_registrations", "declared_weight_kg")
    op.drop_column("tournament_registrations", "modality")

    op.drop_constraint("competitors_belt_id_fkey", "competitors", type_="foreignkey")
    op.drop_column("competitors", "date_of_birth")
    op.drop_column("competitors", "sex")
    op.drop_column("competitors", "belt_id")

    op.drop_constraint("ck_stage_items_age_range", "stage_items", type_="check")
    op.drop_constraint("ck_stage_items_weight_range", "stage_items", type_="check")
    op.drop_constraint("stage_items_belt_min_id_fkey", "stage_items", type_="foreignkey")
    op.drop_constraint("stage_items_belt_max_id_fkey", "stage_items", type_="foreignkey")
    for column in reversed(STAGE_ITEM_RULE_COLUMNS):
        op.drop_column("stage_items", column)

    op.drop_column("clubs", "logo_path")

    op.drop_index("ix_belts_active", table_name="belts")
    op.drop_index("ix_belts_id", table_name="belts")
    op.drop_table("belts")

    bind = op.get_bind()
    for name in reversed(list(ENUM_LABELS)):
        postgresql.ENUM(*ENUM_LABELS[name], name=name).drop(bind, checkfirst=True)
