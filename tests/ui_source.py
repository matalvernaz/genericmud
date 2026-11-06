"""Find the source of a named definition in genericmud/ui/, wherever it is defined.

The source-pinning tests read code through these helpers instead of reading one file's text, so a
class or function can move to another module without its pins losing their scope or quietly
passing. Each module is parsed on its own; a pin only ever sees the one definition it names.
"""

from __future__ import annotations

import ast
from pathlib import Path

UI_PACKAGE = Path(__file__).resolve().parents[1] / "genericmud" / "ui"


def ui_modules() -> list[tuple[Path, str, ast.Module]]:
    """Every module of the native window: its path, its text and its parsed tree."""
    modules = []
    for path in sorted(UI_PACKAGE.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        modules.append((path, text, ast.parse(text)))
    return modules


def _defines(node: ast.stmt, name: str) -> bool:
    """Whether a top-level statement defines ``name``: a class, a function or an assignment."""
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name == name
    if isinstance(node, ast.Assign):
        return any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
    if isinstance(node, ast.AnnAssign):
        return isinstance(node.target, ast.Name) and node.target.id == name
    return False


def _definitions(name: str) -> list[tuple[str, ast.stmt]]:
    """Every top-level definition of ``name`` under genericmud/ui/, with its module's text."""
    found = []
    for _path, text, tree in ui_modules():
        for node in tree.body:
            if _defines(node, name):
                found.append((text, node))
    return found


def ui_defines(name: str) -> bool:
    """Whether any module of the native window defines ``name`` at its top level."""
    return bool(_definitions(name))


def _definition(name: str) -> tuple[str, ast.stmt]:
    """The one top-level definition of ``name``; a pin on a missing or doubled name fails."""
    found = _definitions(name)
    assert len(found) == 1, f"{name} is defined {len(found)} times under genericmud/ui/"
    return found[0]


def _class_and_its_bases(name: str) -> list[tuple[str, ast.ClassDef]]:
    """A class, followed by every base class of it that is itself defined under genericmud/ui/.

    A window class can be made of mixins that each hold some of its methods; together they are
    the class's code, so a pin on the class sees all of them.
    """
    text, node = _definition(name)
    assert isinstance(node, ast.ClassDef), f"{name} is not a class"
    classes = [(text, node)]
    for base in node.bases:
        if isinstance(base, ast.Name) and ui_defines(base.id):
            classes.extend(_class_and_its_bases(base.id))
    return classes


def ui_source(name: str) -> str:
    """The source of a top-level class, function or constant, or of a method named "Class.method".

    A method is looked for in the class and then in the mixins the class is made of.
    """
    if "." not in name:
        text, node = _definition(name)
        return ast.get_source_segment(text, node)
    class_name, method_name = name.split(".", 1)
    for text, node in _class_and_its_bases(class_name):
        for item in node.body:
            is_function = isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            if is_function and item.name == method_name:
                return ast.get_source_segment(text, item)
    raise AssertionError(f"{class_name} has no method {method_name} under genericmud/ui/")


def ui_class_source(name: str) -> str:
    """The source of a class together with the mixins it is made of, one after another."""
    parts = [ast.get_source_segment(text, node) for text, node in _class_and_its_bases(name)]
    return "\n\n".join(parts)
