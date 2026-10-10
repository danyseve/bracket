"""Cobertura de claves ajenas del borrado de torneos (contrato de aplicacion).

Protege contra la reaparicion del defecto DELETE TOURNAMENT 500: al anadir una tabla con FK hacia
`tournaments` (o dentro del subgrafo de datos del torneo) el mecanismo de borrado debe contemplar
la constraint (enum `ForeignKey`) o bloquearla con un 409 seguro, nunca con un 500.

La lista de referencia se deriva del esquema real (`metadata` del proyecto + `pg_constraint`), no
de una constante escrita a mano, para que el test siga siendo valido cuando el esquema cambie.
"""

import pytest

from bracket.database import database
from bracket.schema import metadata
from bracket.utils.errors import ForeignKey

FOREIGN_KEYS_QUERY = """
    SELECT conrelid::regclass::text AS child_table,
           conname AS constraint_name,
           confrelid::regclass::text AS parent_table
    FROM pg_constraint
    WHERE contype = 'f'
      AND connamespace = 'public'::regnamespace
"""

#: Constraints que el inventario real del esquema debe contener: si no aparecen, la consulta (o el
#: esquema) no es la esperada y el test no puede dar un verde silencioso.
EXPECTED_IN_SCHEMA = frozenset(
    {
        "courts_tournament_id_fkey",
        "stage_item_inputs_tournament_id_fkey",
        "stage_items_ranking_id_fkey",
        "stages_tournament_id_fkey",
        "teams_tournament_id_fkey",
    }
)


async def foreign_keys() -> list[tuple[str, str, str]]:
    rows = await database.fetch_all(query=FOREIGN_KEYS_QUERY)
    return [
        (str(row["child_table"]), str(row["constraint_name"]), str(row["parent_table"]))
        for row in rows
    ]


def tournament_owned_tables(
    foreign_keys: list[tuple[str, str, str]], project_tables: set[str]
) -> set[str]:
    """
    Tablas cuyos datos pertenecen a un torneo: cierre desde `tournaments` siguiendo las FK en la
    direccion tabla que referencia -> tabla referenciada. Se ignoran las tablas ajenas al esquema
    del proyecto (residuos de otras ramas en la misma base de datos).
    """
    owned = {"tournaments"}
    changed = True
    while changed:
        changed = False
        for child, _, parent in foreign_keys:
            if child in project_tables and parent in owned and child not in owned:
                owned.add(child)
                changed = True
    return owned


@pytest.mark.asyncio(loop_scope="session")
async def test_every_foreign_key_in_the_delete_subgraph_is_in_the_app_contract() -> None:
    """
    Toda FK real del subgrafo del torneo debe estar en el contrato de aplicacion (`ForeignKey`):
    es la lista con la que el borrado reconoce dependencias esperadas, y su ausencia fue una de
    las causas del 500 original.

    Una tabla nueva con FK hacia `tournaments` entra automaticamente en `owned` y, si su constraint
    no esta en el enum, el test falla: obliga a decidir entre borrarla o bloquearla con 409.
    """
    project_tables = set(metadata.tables)
    keys = await foreign_keys()
    owned = tournament_owned_tables(keys, project_tables)

    relevant = sorted(
        name
        for child, name, parent in keys
        if child in owned and (parent in owned or parent == "tournaments")
    )

    assert EXPECTED_IN_SCHEMA <= set(relevant), (
        f"El esquema real no contiene las constraints esperadas; relevantes={relevant}"
    )

    unknown = [name for name in relevant if name not in ForeignKey.values()]
    assert unknown == [], (
        f"FK del subgrafo de un torneo fuera del contrato ForeignKey: {unknown}. "
        "Anade la constraint al enum y cubrela en el borrado o en el 409 seguro."
    )
