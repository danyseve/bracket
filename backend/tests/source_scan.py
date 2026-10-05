"""Utilidad de pruebas: leer el codigo fuente sin su documentacion.

Un docstring no es una sentencia. ``ON DELETE CASCADE`` explicado en la cabecera de una migracion
no borra ninguna fila, y ``DELETE FROM`` dentro de una sentencia SQL si. Las comprobaciones del tipo
"este codigo no hace X" tienen que distinguirlo, o acaban prohibiendo la palabra en la prosa y
obligando a reescribir la documentacion para contentar al test.

Se conservan los literales de **codigo** a proposito: ahi es donde vive la sentencia que se busca.
"""

from __future__ import annotations

import ast
import io
import tokenize


def code_without_documentation(source: str) -> str:
    """Devuelve el codigo sin comentarios ni docstrings (los literales de codigo se conservan)."""
    without_comments = "".join(
        "" if token.type == tokenize.COMMENT else token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
    )
    docstrings = {
        docstring
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        if (docstring := ast.get_docstring(node, clean=False)) is not None
    }
    for docstring in docstrings:
        without_comments = without_comments.replace(docstring, "")
    return without_comments
