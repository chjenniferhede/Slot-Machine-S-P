"""Rules about the whole package, checked by reading its source.

No single layer owns these. Each one is easy to break by accident and easy to
miss in review, so a test does the looking instead. In Phase II another team
edits this code to hide a weakness in it; a scan that fails makes that edit
louder, because they now have to change the tests as well.

The scans read the code as Python (with ``ast``), not as text. A docstring
that says "never pickle" is not mistaken for a use of pickle, and a call
written over several lines is still seen. Every failure names the file and
line.

The tests at the bottom give each scan code it must catch and code it must
let through, so a scan that has quietly stopped finding anything fails too.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

SRC: Final[Path] = Path(__file__).resolve().parent.parent / "src" / "slotmachine"

#: A scan takes a file's path inside the package ("cli/menus.py") and its
#: source, and returns one "path:line: problem" string for each problem.
Scan = Callable[[str, str], list[str]]


def _all_python_source() -> list[tuple[Path, str]]:
    """Every .py file in the package, with its text."""
    # Explicit UTF-8: the default follows the machine's locale, which on a
    # Chinese Windows install is cp936 and cannot read the box-drawing
    # characters in engine.py.
    return [(path, path.read_text(encoding="utf-8")) for path in sorted(SRC.rglob("*.py"))]


def _assert_clean(scan: Scan) -> None:
    problems: list[str] = []
    for path, text in _all_python_source():
        problems.extend(scan(path.relative_to(SRC).as_posix(), text))
    assert not problems, "\n" + "\n".join(problems)


def _report(rel: str, found: Iterable[tuple[int, str]]) -> list[str]:
    """Format (line, problem) pairs, in line order and without repeats."""
    return [f"src/slotmachine/{rel}:{line}: {problem}" for line, problem in sorted(set(found))]


# ---------------------------------------------------------------------------
# Reading the code
# ---------------------------------------------------------------------------


def _dotted(node: ast.expr) -> str:
    """The dotted name an expression spells, like "os.urandom"; "" if it is not one."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def _match(name: str, prefixes: Iterable[str]) -> str | None:
    """The prefix that ``name`` is, or is inside of ("os.urandom" is inside "os")."""
    for prefix in prefixes:
        if name == prefix or name.startswith(prefix + "."):
            return prefix
    return None


def _imports(rel: str, tree: ast.Module) -> Iterator[tuple[int, str]]:
    """Each module a file imports, as (line, absolute dotted name).

    ``from os import urandom`` gives both "os" and "os.urandom", and relative
    imports are resolved, so ``from .. import rng`` in game/ gives
    "slotmachine.rng".
    """
    package = ["slotmachine", *rel.split("/")[:-1]]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - node.level + 1]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            yield node.lineno, module
            for alias in node.names:
                yield node.lineno, f"{module}.{alias.name}"


def _calls(tree: ast.Module) -> Iterator[tuple[ast.Call, str]]:
    """Each call, with the dotted name of what is called ("" if not a plain name)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node, _dotted(node.func)


# ---------------------------------------------------------------------------
# The scans
# ---------------------------------------------------------------------------


def find_insecure_rng(rel: str, text: str) -> list[str]:
    tree = ast.parse(text)
    found: list[tuple[int, str]] = []
    for line, name in _imports(rel, tree):
        if _match(name, ["random"]):
            found.append((line, "imports random, which is predictable; use slotmachine.rng"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _dotted(node).startswith("random."):
            found.append((node.lineno, f"uses {_dotted(node)}, which is predictable"))
    return _report(rel, found)


#: The only modules that draw entropy themselves. Everything else asks rng.py.
ENTROPY_MODULES: Final = frozenset({"rng.py", "auth.py", "payment.py"})
ENTROPY_SOURCES: Final = ("secrets", "os.urandom", "os.getrandom")


def find_entropy_outside_its_modules(rel: str, text: str) -> list[str]:
    if rel in ENTROPY_MODULES:
        return []
    tree = ast.parse(text)
    allowed = ", ".join(sorted(ENTROPY_MODULES))
    found: list[tuple[int, str]] = []
    for line, name in _imports(rel, tree):
        source = _match(name, ENTROPY_SOURCES)
        if source:
            found.append((line, f"draws entropy from {source}; only {allowed} may"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            source = _match(_dotted(node), ENTROPY_SOURCES)
            if source:
                found.append((node.lineno, f"draws entropy from {source}; only {allowed} may"))
    return _report(rel, found)


#: Formats whose loader runs code taken from the data.
PICKLE_LIKE: Final = ("pickle", "_pickle", "cPickle", "marshal", "shelve", "dill", "jsonpickle")
CODE_RUNNERS: Final = frozenset({"eval", "exec", "builtins.eval", "builtins.exec"})
UNSAFE_YAML: Final = frozenset(
    {"yaml.unsafe_load", "yaml.unsafe_load_all", "yaml.full_load", "yaml.full_load_all"}
)
YAML_LOADS: Final = frozenset({"yaml.load", "yaml.load_all"})
SAFE_YAML_LOADERS: Final = frozenset({"SafeLoader", "CSafeLoader"})


def find_unsafe_deserialisation(rel: str, text: str) -> list[str]:
    tree = ast.parse(text)
    found: list[tuple[int, str]] = []
    for line, name in _imports(rel, tree):
        module = _match(name, PICKLE_LIKE)
        if module:
            found.append((line, f"imports {module}, which runs code from the data it loads"))
        # Imported on its own, yaml.load can be called with no Loader in sight.
        if name in YAML_LOADS | UNSAFE_YAML:
            found.append((line, f"imports {name}; call yaml.safe_load instead"))
    for call, name in _calls(tree):
        if name in CODE_RUNNERS:
            found.append((call.lineno, f"calls {name}(), which runs a string as code"))
        elif name in UNSAFE_YAML:
            found.append((call.lineno, f"calls {name}(), which can build any Python object"))
        elif name in YAML_LOADS and not _has_safe_loader(call):
            found.append((call.lineno, f"calls {name}() without SafeLoader"))
    return _report(rel, found)


def _has_safe_loader(call: ast.Call) -> bool:
    loaders = [kw.value for kw in call.keywords if kw.arg == "Loader"] + call.args[1:]
    return any(_dotted(loader).split(".")[-1] in SAFE_YAML_LOADERS for loader in loaders)


SQL_METHODS: Final = frozenset({"execute", "executemany", "executescript"})


def find_sql_string_building(rel: str, text: str) -> list[str]:
    """SQL text put together from anything but fixed strings.

    Followed through variables, so ``sql = f"..."; conn.execute(sql)`` is
    caught. Joining fixed pieces is allowed, as audit.read() does with its
    "column = ?" filters: each piece is traced back, through variables and
    lists filled with .append(), to a string written in the code. A function
    parameter is never fixed text, since the caller chooses it.
    """
    tree = ast.parse(text)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    found: list[tuple[int, str]] = []
    for call, _ in _calls(tree):
        if not (
            isinstance(call.func, ast.Attribute) and call.func.attr in SQL_METHODS and call.args
        ):
            continue
        scope = _Scope.around(call, tree, parents)
        sql = call.args[0]
        candidates = [sql, *scope.values_of(sql.id)] if isinstance(sql, ast.Name) else [sql]
        if any(_builds_text(candidate, scope) for candidate in candidates):
            found.append(
                (call.lineno, f"builds the SQL for .{call.func.attr}() from values; bind them with ?")
            )
    return _report(rel, found)


@dataclass
class _Scope:
    """Where a variable is looked up: its function, and the module's top level."""

    nodes: tuple[ast.AST, ...]
    params: frozenset[str]
    parents: dict[ast.AST, ast.AST]

    @classmethod
    def around(cls, node: ast.AST, tree: ast.Module, parents: dict[ast.AST, ast.AST]) -> _Scope:
        while node in parents and not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node = parents[node]
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cls(tuple(ast.walk(tree)), frozenset(), parents)
        a = node.args
        params = [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]
        return cls(
            (*ast.walk(node), *tree.body),
            frozenset(p.arg for p in params if p is not None),
            parents,
        )

    def values_of(self, name: str) -> list[ast.expr]:
        """Every value given to ``name``. ``x += y`` comes back as ``x + y``."""
        values: list[ast.expr] = []
        for node in self.nodes:
            if isinstance(node, ast.Assign) and any(_is_name(t, name) for t in node.targets):
                values.append(node.value)
            elif isinstance(node, ast.AnnAssign) and node.value and _is_name(node.target, name):
                values.append(node.value)
            elif isinstance(node, ast.AugAssign) and _is_name(node.target, name):
                values.append(ast.BinOp(left=ast.Name(id=name), op=node.op, right=node.value))
        return values

    def uses_of(self, name: str) -> list[ast.Name]:
        return [node for node in self.nodes if isinstance(node, ast.Name) and node.id == name]


def _is_name(node: ast.expr, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _builds_text(expr: ast.expr, scope: _Scope) -> bool:
    """Whether an expression puts a string together from anything but fixed text."""
    for node in ast.walk(expr):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            return True
        if _is_method_call(node, {"format", "format_map"}):
            return True
        adds = isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)
        joins = _is_method_call(node, {"join"})
        if (isinstance(node, ast.JoinedStr) or adds or joins) and not _is_fixed_text(node, scope):
            return True
    return False


def _is_fixed_text(node: ast.AST, scope: _Scope, seen: frozenset[str] = frozenset()) -> bool:
    """A string made only of text written in the code."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.JoinedStr):
        return all(isinstance(part, ast.Constant) for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _is_fixed_text(node.left, scope, seen) and _is_fixed_text(node.right, scope, seen)
    if isinstance(node, ast.Name):
        if node.id in seen:
            return True  # q = q + "...": the other values of q decide
        values = scope.values_of(node.id)
        return (
            node.id not in scope.params
            and bool(values)
            and all(_is_fixed_text(value, scope, seen | {node.id}) for value in values)
        )
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "join":
        pieces = node.args[0] if len(node.args) == 1 and not node.keywords else None
        return (
            _is_fixed_text(node.func.value, scope, seen)
            and isinstance(pieces, ast.Name)
            and _is_fixed_list(pieces.id, scope)
        )
    return False


def _is_fixed_list(name: str, scope: _Scope) -> bool:
    """A list that only ever holds fixed text: made from a literal, added to with .append()."""
    values = scope.values_of(name)
    if name in scope.params or not values:
        return False
    for value in values:
        if not isinstance(value, (ast.List, ast.Tuple)):
            return False
        if not all(_is_fixed_text(item, scope) for item in value.elts):
            return False
    return all(_keeps_list_fixed(use, scope) for use in scope.uses_of(name))


def _keeps_list_fixed(use: ast.Name, scope: _Scope) -> bool:
    """Whether one use of a list leaves it holding only fixed text.

    Appending fixed text, joining it, taking its length and testing whether
    it is empty all do. Anything else, such as passing it to a function that
    could add to it, does not.
    """
    parent = scope.parents.get(use)
    if isinstance(use.ctx, ast.Store) or isinstance(parent, (ast.If, ast.While, ast.BoolOp)):
        return True
    if isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.Not):
        return True
    if isinstance(parent, ast.Attribute) and parent.attr == "append":
        call = scope.parents.get(parent)
        return (
            isinstance(call, ast.Call)
            and call.func is parent
            and all(_is_fixed_text(arg, scope) for arg in call.args)
        )
    if isinstance(parent, ast.Call) and len(parent.args) == 1 and parent.args[0] is use:
        return _dotted(parent.func) == "len" or _is_method_call(parent, {"join"})
    return False


def _is_method_call(node: ast.AST, names: set[str]) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in names
    )


#: The modules that handle money. Money is whole cents in an int, always.
MONEY_MODULES: Final = frozenset({"money.py", "accounts.py", "payment.py"})


def find_float_in_money_paths(rel: str, text: str) -> list[str]:
    if rel not in MONEY_MODULES:
        return []
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Name) and node.id == "float":
            found.append((node.lineno, "uses float; money is whole cents in an int"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, (float, complex)):
            found.append((node.lineno, f"float literal {node.value!r}; money is whole cents"))
        elif isinstance(node, (ast.BinOp, ast.AugAssign)) and isinstance(node.op, ast.Div):
            found.append((node.lineno, "uses /, which gives a float; use //"))
    return _report(rel, found)


#: Parts of a name that mean its value is a credential.
CREDENTIAL_WORDS: Final = ("password", "passwd", "secret", "token", "key")

#: Marks a line whose fixed string only looks like a credential.
CREDENTIAL_EXEMPT: Final = "# noqa: credential"


def find_hardcoded_credentials(rel: str, text: str) -> list[str]:
    """A credential-like name given, defaulted to or compared with a fixed string.

    Comparisons are included because a backdoor login is more likely to be
    ``if password == "letmein"`` than an assignment.
    """
    tree = ast.parse(text)
    lines = text.splitlines()
    pairs: list[tuple[ast.expr | str, ast.expr, int, int]] = []  # name, value, line span
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                pairs.append((target, node.value, *_span(node)))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            pairs.append((node.target, node.value, *_span(node)))
        elif isinstance(node, ast.keyword) and node.arg:
            pairs.append((node.arg, node.value, *_span(node)))
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if key is not None:
                    pairs.append((key, value, key.lineno, _span(value)[1]))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = node.args
            positional = args.posonlyargs + args.args
            with_defaults = positional[len(positional) - len(args.defaults) :]
            for arg, default in zip(with_defaults, args.defaults):
                pairs.append((arg.arg, default, *_span(default)))
            for arg, kw_default in zip(args.kwonlyargs, args.kw_defaults):
                if kw_default is not None:
                    pairs.append((arg.arg, kw_default, *_span(kw_default)))
        elif isinstance(node, ast.Compare) and len(node.comparators) == 1:
            if isinstance(node.ops[0], (ast.Eq, ast.NotEq)):
                left, right = node.left, node.comparators[0]
                pairs.append((left, right, *_span(node)))
                pairs.append((right, left, *_span(node)))
        elif isinstance(node, ast.Call) and _dotted(node.func).endswith("compare_digest"):
            for a in node.args:
                for b in node.args:
                    if a is not b:
                        pairs.append((a, b, *_span(node)))

    found: list[tuple[int, str]] = []
    for name_node, value, first, last in pairs:
        name = _credential_name(name_node)
        if name is None or not _is_secret_like(value):
            continue
        if any(CREDENTIAL_EXEMPT in line for line in lines[first - 1 : last]):
            continue
        found.append(
            (first, f"{name} is a fixed string; if it is not a secret, end the line {CREDENTIAL_EXEMPT}")
        )
    return _report(rel, found)


def _span(node: ast.expr | ast.stmt | ast.keyword) -> tuple[int, int]:
    return node.lineno, node.end_lineno or node.lineno


def _credential_name(node: ast.expr | str) -> str | None:
    if isinstance(node, str):
        name = node
    elif isinstance(node, ast.Name):
        name = node.id
    elif isinstance(node, ast.Attribute):
        name = node.attr
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        name = node.value  # a dict key
    else:
        return None
    return name if any(word in name.lower() for word in CREDENTIAL_WORDS) else None


def _is_secret_like(value: ast.expr) -> bool:
    return isinstance(value, ast.Constant) and isinstance(value.value, str) and len(value.value) > 3


#: Where printing is the job: the interfaces, the tools, and the entry point.
PRINT_ALLOWED_DIRS: Final = ("cli/", "tools/")
PRINT_ALLOWED_FILES: Final = frozenset({"__main__.py"})


def find_debug_leftovers(rel: str, text: str) -> list[str]:
    tree = ast.parse(text)
    found: list[tuple[int, str]] = []
    for line, name in _imports(rel, tree):
        debugger = _match(name, ["pdb", "ipdb", "pudb"])
        if debugger:
            found.append((line, f"imports {debugger}"))
    may_print = rel.startswith(PRINT_ALLOWED_DIRS) or rel in PRINT_ALLOWED_FILES
    for call, name in _calls(tree):
        if name == "breakpoint":
            found.append((call.lineno, "calls breakpoint()"))
        elif name == "print" and not may_print:
            found.append((call.lineno, "prints outside cli/ and tools/; return the value instead"))
    return _report(rel, found)


ENGINE: Final = "game/engine.py"

#: What the engine promises not to touch: randomness, the database, the clock.
ENGINE_MUST_NOT_IMPORT: Final = (
    "slotmachine.rng",
    "random",
    "secrets",
    "os",
    "slotmachine.storage",
    "slotmachine.audit",
    "slotmachine.accounts",
    "slotmachine.payment",
    "slotmachine.auth",
    "sqlite3",
    "datetime",
    "time",
)


def find_impure_engine_imports(rel: str, text: str) -> list[str]:
    if rel != ENGINE:
        return []
    found: list[tuple[int, str]] = []
    for line, name in _imports(rel, ast.parse(text)):
        banned = _match(name, ENGINE_MUST_NOT_IMPORT)
        if banned:
            found.append((line, f"imports {banned}; the engine must stay pure"))
    return _report(rel, found)


DYNAMIC_IMPORTS: Final = frozenset(
    {"__import__", "import_module", "importlib.import_module", "importlib.__import__"}
)


def find_dynamic_imports(rel: str, text: str) -> list[str]:
    """Importing by name at run time, which hides the import from every scan above."""
    found = [
        (call.lineno, f"calls {name}(); import modules with an import statement")
        for call, name in _calls(ast.parse(text))
        if name in DYNAMIC_IMPORTS
    ]
    return _report(rel, found)


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def test_scans_see_the_whole_package() -> None:
    """A scan of no files passes every rule, so check the files are there."""
    seen = {path.relative_to(SRC).as_posix() for path, _ in _all_python_source()}
    assert {"rng.py", "money.py", "storage.py", "game/engine.py", "cli/menus.py"} <= seen


def test_no_insecure_rng() -> None:
    """``random`` is predictable; every draw goes through rng.py.

    ProvidedResources/SecuredRNG.md names Python's ``random()`` as the unsafe
    function.
    """
    _assert_clean(find_insecure_rng)


def test_entropy_drawn_in_one_place() -> None:
    """``secrets`` and ``os.urandom`` only in rng.py, auth.py and payment.py."""
    _assert_clean(find_entropy_outside_its_modules)


def test_no_unsafe_deserialisation() -> None:
    """No pickle, marshal, shelve, eval, exec, or yaml.load without SafeLoader."""
    _assert_clean(find_unsafe_deserialisation)


def test_no_sql_string_building() -> None:
    """SQL text is fixed; values are bound with ? so none of them becomes SQL."""
    _assert_clean(find_sql_string_building)


def test_no_float_in_money_paths() -> None:
    """No float, float literal or / in the money modules. Integer cents never round."""
    _assert_clean(find_float_in_money_paths)


def test_no_hardcoded_credentials() -> None:
    _assert_clean(find_hardcoded_credentials)


def test_no_debug_leftovers() -> None:
    """No breakpoint(), no pdb, and no print() outside the interfaces and tools."""
    _assert_clean(find_debug_leftovers)


def test_engine_is_pure() -> None:
    """The engine's docstring promises no randomness, no database and no clock."""
    _assert_clean(find_impure_engine_imports)


def test_no_dynamic_imports() -> None:
    _assert_clean(find_dynamic_imports)


#: Each module whose every public function is a privileged action, and the
#: one role allowed to run it.
PRIVILEGED_MODULES: Final = (
    ("cli/technician.py", "technician"),
    ("cli/commissioner.py", "commissioner"),
)


def find_unguarded_functions(module: ModuleType, rel: str, role: str) -> list[str]:
    """Public functions of ``module`` without ``@require_role(role)`` alone.

    Functions imported from elsewhere, and private ``_helpers``, are not the
    module's actions and are left out.
    """
    functions = [
        (name, obj)
        for name, obj in vars(module).items()
        if inspect.isfunction(obj) and obj.__module__ == module.__name__ and not name.startswith("_")
    ]
    if not functions:
        return [f"src/slotmachine/{rel}: no public functions, so there is nothing to check"]

    problems: list[str] = []
    for name, fn in functions:
        where = f"src/slotmachine/{rel}:{inspect.unwrap(fn).__code__.co_firstlineno}: {name}()"
        roles = getattr(fn, "__required_roles__", None)
        if roles is None:
            problems.append(f"{where} has no @require_role")
            continue
        names = sorted(str(getattr(r, "value", r)) for r in roles)
        if names != [role]:
            problems.append(f"{where} allows {names}; it should allow only {role!r}")
    return problems


@pytest.mark.parametrize(("rel", "role"), PRIVILEGED_MODULES)
def test_every_privileged_function_is_decorated(rel: str, role: str) -> None:
    """Every public function carries ``@require_role`` for its module's role, and only it.

    This turns "did we remember the decorator?" from a review question into a
    failing test, and it is the question a Phase II backdoor hopes is answered
    by eye. A second role added to a technician action fails here too.

    Skipped until Layer 5 (#6) writes the module. Once the file exists it must
    import, so a broken import cannot pass as "not written yet".
    """
    if not (SRC / rel).exists():
        pytest.skip(f"{rel} is not written yet (Layer 5, #6)")
    module = importlib.import_module("slotmachine." + rel.removesuffix(".py").replace("/", "."))
    problems = find_unguarded_functions(module, rel, role)
    assert not problems, "\n" + "\n".join(problems)


# ---------------------------------------------------------------------------
# The scans themselves
# ---------------------------------------------------------------------------

CAUGHT: Final = [
    pytest.param(find_insecure_rng, "x.py", "import random\n", id="rng-import"),
    pytest.param(find_insecure_rng, "x.py", "import random as r\n", id="rng-import-as"),
    pytest.param(find_insecure_rng, "x.py", "from random import randint\n", id="rng-from"),
    pytest.param(find_insecure_rng, "x.py", "x = random.random()\n", id="rng-attribute"),
    pytest.param(
        find_entropy_outside_its_modules, "game/engine.py", "import secrets\n", id="entropy-secrets"
    ),
    pytest.param(
        find_entropy_outside_its_modules,
        "storage.py",
        "import os\nseed = os.urandom(32)\n",
        id="entropy-urandom",
    ),
    pytest.param(
        find_entropy_outside_its_modules, "x.py", "from os import urandom\n", id="entropy-from-os"
    ),
    pytest.param(find_unsafe_deserialisation, "x.py", "import pickle\n", id="pickle"),
    pytest.param(find_unsafe_deserialisation, "x.py", "from marshal import loads\n", id="marshal"),
    pytest.param(find_unsafe_deserialisation, "x.py", "import shelve\n", id="shelve"),
    pytest.param(find_unsafe_deserialisation, "x.py", "eval(text)\n", id="eval"),
    pytest.param(find_unsafe_deserialisation, "x.py", "exec(text)\n", id="exec"),
    pytest.param(find_unsafe_deserialisation, "x.py", "yaml.load(f)\n", id="yaml-no-loader"),
    pytest.param(
        find_unsafe_deserialisation, "x.py", "yaml.load(f, Loader=yaml.Loader)\n", id="yaml-loader"
    ),
    pytest.param(find_unsafe_deserialisation, "x.py", "yaml.unsafe_load(f)\n", id="yaml-unsafe"),
    pytest.param(
        find_unsafe_deserialisation, "x.py", "from yaml import load\n", id="yaml-import-load"
    ),
    pytest.param(
        find_sql_string_building,
        "x.py",
        "conn.execute(f\"SELECT * FROM users WHERE name = '{name}'\")\n",
        id="sql-fstring",
    ),
    pytest.param(
        find_sql_string_building,
        "x.py",
        "conn.execute(\"SELECT * FROM users WHERE name = '%s'\" % name)\n",
        id="sql-percent",
    ),
    pytest.param(
        find_sql_string_building,
        "x.py",
        "conn.execute(\"SELECT * FROM users WHERE name = '{}'\".format(name))\n",
        id="sql-format",
    ),
    pytest.param(
        find_sql_string_building,
        "x.py",
        "conn.execute(\"SELECT * FROM users WHERE name = '\" + name + \"'\")\n",
        id="sql-concat",
    ),
    pytest.param(
        find_sql_string_building,
        "x.py",
        "conn.execute(\n    f'SELECT * FROM users'\n    f' WHERE name = {name}'\n)\n",
        id="sql-multiline",
    ),
    pytest.param(
        find_sql_string_building,
        "x.py",
        "def f(conn, name):\n    sql = f'SELECT {name}'\n    conn.executemany(sql, [])\n",
        id="sql-through-variable",
    ),
    pytest.param(
        find_sql_string_building,
        "x.py",
        "def f(conn, name):\n    sql = 'SELECT '\n    sql += name\n    conn.execute(sql)\n",
        id="sql-plus-equals",
    ),
    pytest.param(
        find_sql_string_building,
        "x.py",
        "def read(conn, actor):\n"
        "    q = 'SELECT * FROM t'\n"
        "    q = q + \" WHERE actor = '\" + actor + \"'\"\n"
        "    conn.execute(q)\n",
        id="sql-parameter-added",
    ),
    pytest.param(
        find_sql_string_building,
        "x.py",
        "def f(conn, where):\n"
        "    if not where:\n"
        "        where = '1 = 1'\n"
        "    conn.execute('SELECT * FROM t WHERE ' + where)\n",
        id="sql-parameter-reassigned",
    ),
    pytest.param(
        find_sql_string_building,
        "x.py",
        "def read(conn, actor):\n"
        "    clauses = []\n"
        "    clauses.append(f\"actor = '{actor}'\")\n"
        "    conn.execute('SELECT * FROM t WHERE ' + ' AND '.join(clauses))\n",
        id="sql-list-given-a-value",
    ),
    pytest.param(
        find_sql_string_building,
        "x.py",
        "def read(conn, actor):\n"
        "    clauses = []\n"
        "    add_filter(clauses, actor)\n"
        "    conn.execute('SELECT * FROM t WHERE ' + ' AND '.join(clauses))\n",
        id="sql-list-passed-on",
    ),
    pytest.param(
        find_sql_string_building,
        "x.py",
        "def read(conn, clauses):\n"
        "    conn.execute('SELECT * FROM t WHERE ' + ' AND '.join(clauses))\n",
        id="sql-list-from-caller",
    ),
    pytest.param(find_float_in_money_paths, "money.py", "x: float = 0\n", id="float-name"),
    pytest.param(find_float_in_money_paths, "money.py", "c = float(s)\n", id="float-call"),
    pytest.param(find_float_in_money_paths, "accounts.py", "fee = c * 0.05\n", id="float-literal"),
    pytest.param(find_float_in_money_paths, "payment.py", "half = c / 2\n", id="float-divide"),
    pytest.param(find_float_in_money_paths, "money.py", "c /= 2\n", id="float-divide-assign"),
    pytest.param(
        find_hardcoded_credentials, "x.py", 'ADMIN_PASSWORD = "hunter22"\n', id="cred-assign"
    ),
    pytest.param(
        find_hardcoded_credentials, "x.py", 'api_key: str = "abcd1234"\n', id="cred-annotated"
    ),
    pytest.param(
        find_hardcoded_credentials, "x.py", 'self.token = "abcd1234"\n', id="cred-attribute"
    ),
    pytest.param(
        find_hardcoded_credentials, "x.py", 'connect(password="hunter22")\n', id="cred-keyword"
    ),
    pytest.param(
        find_hardcoded_credentials,
        "x.py",
        'def login(user, password="changeme"):\n    pass\n',
        id="cred-default",
    ),
    pytest.param(
        find_hardcoded_credentials,
        "x.py",
        'def login(user, *, secret="changeme"):\n    pass\n',
        id="cred-keyword-only-default",
    ),
    pytest.param(
        find_hardcoded_credentials, "x.py", 'CONFIG = {"secret": "s3cr3t!"}\n', id="cred-dict"
    ),
    pytest.param(
        find_hardcoded_credentials,
        "x.py",
        'if password == "letmein":\n    pass\n',
        id="cred-compare",
    ),
    pytest.param(
        find_hardcoded_credentials,
        "x.py",
        'if "letmein" == password:\n    pass\n',
        id="cred-compare-reversed",
    ),
    pytest.param(
        find_hardcoded_credentials,
        "x.py",
        'ok = hmac.compare_digest(token, "abcdef")\n',
        id="cred-compare-digest",
    ),
    pytest.param(find_debug_leftovers, "x.py", "breakpoint()\n", id="debug-breakpoint"),
    pytest.param(find_debug_leftovers, "x.py", "import pdb\n", id="debug-pdb"),
    pytest.param(find_debug_leftovers, "storage.py", 'print("here")\n', id="debug-print"),
    pytest.param(
        find_impure_engine_imports,
        "game/engine.py",
        "from slotmachine import storage\n",
        id="engine-storage",
    ),
    pytest.param(
        find_impure_engine_imports,
        "game/engine.py",
        "from slotmachine.rng import derive_stops\n",
        id="engine-rng",
    ),
    pytest.param(
        find_impure_engine_imports, "game/engine.py", "from .. import auth\n", id="engine-relative"
    ),
    pytest.param(
        find_impure_engine_imports, "game/engine.py", "import datetime\n", id="engine-datetime"
    ),
    pytest.param(
        find_impure_engine_imports,
        "game/engine.py",
        "from datetime import date\n",
        id="engine-from-datetime",
    ),
    pytest.param(find_dynamic_imports, "x.py", 'm = __import__("random")\n', id="dynamic-dunder"),
    pytest.param(
        find_dynamic_imports,
        "x.py",
        'm = importlib.import_module("pickle")\n',
        id="dynamic-importlib",
    ),
]

ALLOWED: Final = [
    pytest.param(
        find_insecure_rng, "x.py", '"""Never use random here."""\n# import random\n', id="rng-prose"
    ),
    pytest.param(find_insecure_rng, "x.py", "t = rng.random_token()\n", id="rng-our-module"),
    pytest.param(find_entropy_outside_its_modules, "rng.py", "import secrets\n", id="entropy-rng"),
    pytest.param(
        find_entropy_outside_its_modules,
        "x.py",
        "from slotmachine import rng\nseed = rng.secure_seed()\n",
        id="entropy-through-rng",
    ),
    pytest.param(
        find_unsafe_deserialisation, "x.py", '"""Always JSON. Never pickle."""\n', id="deser-prose"
    ),
    pytest.param(
        find_unsafe_deserialisation, "x.py", 'conn.execute("SELECT 1")\n', id="deser-execute"
    ),
    pytest.param(
        find_unsafe_deserialisation,
        "x.py",
        "yaml.load(f, Loader=yaml.SafeLoader)\n",
        id="yaml-safe-loader",
    ),
    pytest.param(
        find_sql_string_building,
        "x.py",
        'conn.execute("SELECT * FROM users WHERE name = ?", (name,))\n',
        id="sql-bound",
    ),
    pytest.param(
        find_sql_string_building,
        "x.py",
        'conn.execute(\n    "SELECT * FROM users"\n    " WHERE name = ?",\n    (name,),\n)\n',
        id="sql-split-literal",
    ),
    pytest.param(
        find_sql_string_building,
        "x.py",
        'conn.execute("SELECT *" + " FROM users")\n',
        id="sql-fixed-concat",
    ),
    pytest.param(
        find_sql_string_building,
        "x.py",
        "def f(conn, path):\n    sql = path.read_text()\n    conn.executescript(sql)\n",
        id="sql-schema-file",
    ),
    pytest.param(
        find_sql_string_building,
        "x.py",
        "def read(conn, actor=None):\n"
        "    clauses: list[str] = []\n"
        "    params = []\n"
        "    if actor is not None:\n"
        "        clauses.append('actor = ?')\n"
        "        params.append(actor)\n"
        "    query = 'SELECT * FROM t'\n"
        "    if clauses:\n"
        "        query = query + ' WHERE ' + ' AND '.join(clauses)\n"
        "    query += ' LIMIT ?'\n"
        "    conn.execute(query, tuple(params))\n",
        id="sql-fixed-filters",
    ),
    pytest.param(
        find_sql_string_building,
        "x.py",
        "SELECT = 'SELECT * FROM t'\n"
        "def f(conn):\n"
        "    conn.execute(SELECT + ' WHERE id = ?', (1,))\n",
        id="sql-module-constant",
    ),
    pytest.param(find_float_in_money_paths, "money.py", "half = c // 2\n", id="float-floor-div"),
    pytest.param(
        find_float_in_money_paths, "money.py", '"""Never a float."""\n', id="float-prose"
    ),
    pytest.param(
        find_float_in_money_paths, "game/engine.py", "rtp = 0.95\n", id="float-not-money"
    ),
    pytest.param(
        find_hardcoded_credentials,
        "x.py",
        'PASSWORD_CHANGE = "auth.password_change"  # noqa: credential\n',
        id="cred-marked",
    ),
    pytest.param(
        find_hardcoded_credentials,
        "x.py",
        'token = "tok_" + rng.random_token()\n',
        id="cred-not-a-literal",
    ),
    pytest.param(
        find_hardcoded_credentials, "x.py", 'password = ask("Password: ")\n', id="cred-prompted"
    ),
    pytest.param(find_hardcoded_credentials, "x.py", 'key = "q"\n', id="cred-short"),
    pytest.param(
        find_hardcoded_credentials,
        "x.py",
        'if password == "":\n    pass\n',
        id="cred-empty-check",
    ),
    pytest.param(find_debug_leftovers, "cli/menus.py", 'print("hi")\n', id="print-cli"),
    pytest.param(find_debug_leftovers, "tools/rtpcalc.py", "print(x)\n", id="print-tools"),
    pytest.param(find_debug_leftovers, "__main__.py", "print(x)\n", id="print-main"),
    pytest.param(find_debug_leftovers, "storage.py", "console.print(x)\n", id="print-method"),
    pytest.param(
        find_impure_engine_imports,
        "game/engine.py",
        "from slotmachine.game.spec import GameSpec\n",
        id="engine-spec",
    ),
    pytest.param(
        find_impure_engine_imports, "storage.py", "import datetime\n", id="engine-other-file"
    ),
    pytest.param(
        find_dynamic_imports, "x.py", "from importlib import resources\n", id="dynamic-resources"
    ),
]


@pytest.mark.parametrize(("scan", "rel", "code"), CAUGHT)
def test_scan_catches(scan: Scan, rel: str, code: str) -> None:
    problems = scan(rel, code)
    assert problems, f"{scan.__name__} missed:\n{code}"
    assert all(p.startswith(f"src/slotmachine/{rel}:") for p in problems)


@pytest.mark.parametrize(("scan", "rel", "code"), ALLOWED)
def test_scan_allows(scan: Scan, rel: str, code: str) -> None:
    assert scan(rel, code) == []


def test_problem_names_file_and_line() -> None:
    assert find_insecure_rng("game/x.py", '"""Doc."""\n\nimport random\n') == [
        "src/slotmachine/game/x.py:3: imports random, which is predictable; use slotmachine.rng"
    ]


# A stand-in for Layer 2's decorator and roles, so the decorator check can be
# shown working before cli/technician.py exists.
_FAKE_AUTH = """
import enum
import functools
from os.path import join  # imported, so not this module's action

class Role(str, enum.Enum):
    PLAYER = "player"
    TECHNICIAN = "technician"
    COMMISSIONER = "commissioner"

def _require_role(*allowed):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            return fn(*args, **kwargs)
        wrapper.__required_roles__ = frozenset(allowed)
        return wrapper
    return decorate

def _helper():
    pass
"""


def _fake_module(actions: str) -> ModuleType:
    module = ModuleType("slotmachine.cli.fake")
    exec(_FAKE_AUTH + actions, module.__dict__)
    return module


def test_decorator_check_passes_a_guarded_module() -> None:
    module = _fake_module(
        "@_require_role(Role.TECHNICIAN)\n"
        "def disable_game(session, conn, game_id):\n"
        "    pass\n"
    )
    assert find_unguarded_functions(module, "cli/fake.py", "technician") == []


@pytest.mark.parametrize(
    ("actions", "expected"),
    [
        pytest.param(
            "def disable_game(session, conn, game_id):\n    pass\n",
            "disable_game() has no @require_role",
            id="missing",
        ),
        pytest.param(
            "@_require_role(Role.TECHNICIAN, Role.PLAYER)\n"
            "def disable_game(session, conn, game_id):\n    pass\n",
            "disable_game() allows ['player', 'technician']",
            id="extra-role",
        ),
        pytest.param(
            "@_require_role(Role.COMMISSIONER)\n"
            "def disable_game(session, conn, game_id):\n    pass\n",
            "disable_game() allows ['commissioner']",
            id="wrong-role",
        ),
        pytest.param("", "no public functions", id="empty-module"),
    ],
)
def test_decorator_check_catches(actions: str, expected: str) -> None:
    problems = find_unguarded_functions(_fake_module(actions), "cli/fake.py", "technician")
    assert len(problems) == 1
    assert expected in problems[0]
    assert problems[0].startswith("src/slotmachine/cli/fake.py:")
