"""AST introspection of ``@task``-decorated functions.

Used to extract the multi-output field set from ``MultiResult(...)``
calls in a function body, so the submission pipeline can pre-allocate
``expected_output_ids`` for each task.

Limits:

- Only top-level ``MultiResult(...)`` literals in the function body
  are examined. Constructions in helpers, lambdas, or nested
  comprehensions are invisible.
- Aliased imports (``from pymonik import MultiResult as MR``) are
  resolved by walking the function's module-level imports.
- ``MultiResult(**dynamic)`` (kwargs expansion) raises a hard error.
- Inconsistent field sets across branches raise a hard error.

When the AST walk can't see the source (lambdas, REPL definitions,
generated code), :func:`extract_multi_fields` returns ``None`` to
signal "single-output task." Users who need a multi-output task with
an opaque body can declare the schema via ``@task(outputs=("a", "b"))``
on the decorator (see :mod:`pymonik.task`).
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from typing import Callable

from pymonik.errors import PymonikError


def _multiresult_aliases(func: Callable[..., object]) -> set[str]:
    """Names that resolve to :class:`pymonik.MultiResult` in func's module.

    Always includes the bare name ``"MultiResult"`` (the user might have
    a ``from pymonik import MultiResult`` even without an alias). Adds
    any ``from pymonik import MultiResult as <name>`` aliases found
    among the module's top-level imports.
    """
    aliases: set[str] = {"MultiResult"}
    try:
        module = inspect.getmodule(func)
        if module is None:
            return aliases
        src = inspect.getsource(module)
    except (OSError, TypeError):
        return aliases

    try:
        tree = ast.parse(src)
    except SyntaxError:
        return aliases

    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module in {"pymonik", "pymonik.multiresult"}:
            for alias in node.names:
                if alias.name == "MultiResult":
                    aliases.add(alias.asname or alias.name)
    return aliases


def _function_def(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def extract_multi_fields(
    func: Callable[..., object],
) -> tuple[str, ...] | None:
    """Return the sorted field set if ``func`` returns ``MultiResult``s.

    ``None`` if no ``MultiResult(...)`` literal is found (single-output
    task). Raises :class:`PymonikError` on inconsistent shapes or
    dynamic ``**kwargs`` expansion.
    """
    try:
        src = inspect.getsource(func)
    except (OSError, TypeError):
        return None

    src = textwrap.dedent(src)
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None

    func_def = _function_def(tree, getattr(func, "__name__", ""))
    if func_def is None:
        return None

    aliases = _multiresult_aliases(func)

    seen: list[tuple[int, frozenset[str]]] = []
    for node in ast.walk(func_def):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in aliases
        ):
            continue
        # Reject any **kwargs spread (kw.arg is None for **expr).
        if any(kw.arg is None for kw in node.keywords):
            raise PymonikError(
                f"@task {func.__name__!r}: MultiResult with **kwargs expansion "
                f"is not supported (line {node.lineno}). Construct "
                f"MultiResult with literal keyword arguments so the field "
                f"set can be extracted at decoration time, or pass "
                f"outputs=(...) to the @task decorator."
            )
        # Reject any positional args (MultiResult takes only kwargs).
        if node.args:
            raise PymonikError(
                f"@task {func.__name__!r}: MultiResult takes only keyword "
                f"arguments (line {node.lineno})."
            )
        fields = frozenset(kw.arg for kw in node.keywords if kw.arg)
        seen.append((node.lineno, fields))

    if not seen:
        return None

    distinct = {f for _, f in seen}
    if len(distinct) > 1:
        lines = "\n".join(
            f"  line {lineno}: MultiResult({', '.join(sorted(fields))})"
            for lineno, fields in seen
        )
        raise PymonikError(
            f"@task {func.__name__!r}: inconsistent MultiResult shapes:\n{lines}\n"
            f"Every return path must use the same field names."
        )

    fields = seen[0][1]
    # Reject field names that would shadow MultiResultHandle attributes.
    # The runtime check in MultiResult.__init__ catches dynamic
    # constructions; this catches the static cases at decoration.
    from pymonik.multiresult import MultiResult

    bad = fields & MultiResult._RESERVED_FIELD_NAMES
    if bad:
        raise PymonikError(
            f"@task {func.__name__!r}: MultiResult field names "
            f"{sorted(bad)} collide with MultiResultHandle attributes. "
            f"Reserved names: {sorted(MultiResult._RESERVED_FIELD_NAMES)}."
        )
    bad_underscore = {n for n in fields if n.startswith("_")}
    if bad_underscore:
        raise PymonikError(
            f"@task {func.__name__!r}: MultiResult field names "
            f"{sorted(bad_underscore)} are invalid: underscore-prefixed "
            f"names are reserved."
        )
    return tuple(sorted(fields))
