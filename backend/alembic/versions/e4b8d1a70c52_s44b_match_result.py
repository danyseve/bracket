"""S4.4b — hecho durable del resultado de combate: ganador, metodo y fin

Revision ID: e4b8d1a70c52
Revises: c1e7a5b39d84
Create Date: 2026-10-12 23:30:00.000000

Anade a ``matches`` el hecho de que un combate se ha jugado y como lo gano su ganador:

* ``winner_stage_item_input_id``: el pase que gano, declarado por la mesa/marcador;
* ``result_method``: por que metodo (catalogo cerrado, el mismo que ya usa el marcador);
* ``finished_at``: cuando quedo resuelto.

Por que: hasta ahora el ganador solo se deducia comparando ``stage_item_input1_score`` con
``stage_item_input2_score``, asi que un combate ganado por sumision, decision, descalificacion,
walkover o parada del arbitro —o por puntos empatados— no avanzaba a nadie; y "terminado" no se
podia publicar sin inventarselo a partir de ``score=0`` (que en datos reales no significa nada: hay
combates sin jugar con puntuaciones distintas de cero).

Las tres columnas son **nullable y sin relleno**: un combate sin jugar las deja en NULL y sigue
funcionando como antes (deduccion por puntuaciones). No se reescribe ninguna fila existente.
``downgrade()`` retira columnas y restricciones y devuelve la tabla exactamente a su forma previa.

La restriccion de coherencia exige que las tres columnas vayan juntas: un ganador sin metodo, o un
fin sin ganador, serian un resultado a medias.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str | None = "e4b8d1a70c52"
down_revision: str | None = "c1e7a5b39d84"
branch_labels: str | None = None
depends_on: str | None = None

TABLE = "matches"

#: Catalogo cerrado, identico al vocabulario del marcador (scoreboard-adapter/state.js).
METHODS: tuple[str, ...] = (
    "points",
    "submission",
    "decision",
    "disqualification",
    "walkover",
    "referee_stoppage",
    "other",
)

METHOD_CHECK = "ck_matches_result_method"
CONSISTENCY_CHECK = "ck_matches_result_consistent"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("winner_stage_item_input_id", sa.BigInteger(), nullable=True))
    op.add_column(TABLE, sa.Column("result_method", sa.String(length=32), nullable=True))
    op.add_column(TABLE, sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        None,
        TABLE,
        "stage_item_inputs",
        ["winner_stage_item_input_id"],
        ["id"],
        ondelete="SET NULL",
    )
    methods = ", ".join(f"'{method}'" for method in METHODS)
    op.create_check_constraint(
        METHOD_CHECK, TABLE, f"result_method IS NULL OR result_method IN ({methods})"
    )
    # Metodo y fin van juntos. El ganador NO entra en la coherencia: si el pase ganador se borra
    # (ON DELETE SET NULL), el metodo y el fin siguen siendo hechos historicos del combate.
    op.create_check_constraint(
        CONSISTENCY_CHECK,
        TABLE,
        "(result_method IS NULL) = (finished_at IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(CONSISTENCY_CHECK, TABLE, type_="check")
    op.drop_constraint(METHOD_CHECK, TABLE, type_="check")
    op.drop_constraint(op.f("matches_winner_stage_item_input_id_fkey"), TABLE, type_="foreignkey")
    op.drop_column(TABLE, "finished_at")
    op.drop_column(TABLE, "result_method")
    op.drop_column(TABLE, "winner_stage_item_input_id")
