"""Source fingerprints of ansible-core functions that do not depend on the Python version.

Standard library only, so the same function can fingerprint other ansible-core trees with any
Python. What is hashed is ast.unparse() of each definition: source
text regenerated from the syntax tree, so comments and formatting do not count, and the same on
Python 3.12 to 3.15. ast.dump() output is not used, as it changes between Python releases (3.13
and 3.15 changed it).
"""

from __future__ import annotations

import ast
import hashlib
import os
from collections.abc import Iterable

# A target is (file relative to the ansible package, qualified name). The name is a function,
# method or class ("Class.method"), "refs:<name>": the functions of the file that use <name>,
# or, with "." as the file, "files:<name>": the files of the whole package that mention <name>.
Target = tuple[str, str]
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _definitions(tree: ast.Module, qualname: str) -> list[ast.AST]:
    """Return every definition with this qualified name (overloads included)."""
    scopes: list[ast.AST] = [tree]
    for part in qualname.split("."):
        scopes = [
            node
            for scope in scopes
            for node in getattr(scope, "body", ())
            if isinstance(node, _DEFINITIONS) and node.name == part
        ]
    return scopes


def _references(tree: ast.Module, name: str) -> list[str]:
    """Return the qualified names of the functions that use the module-level name."""
    found: list[str] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _DEFINITIONS):
                visit(child, f"{prefix}{child.name}.")
            elif isinstance(child, ast.Name) and child.id == name:
                scope = prefix.rstrip(".") or "<module>"
                found.append(f"{scope}:{type(child.ctx).__name__}")
            else:
                visit(child, prefix)

    visit(tree, "")
    return sorted(found)


def _files_mentioning(root: str, name: str) -> list[str]:
    """Return the Python files of the package at root whose text contains name."""
    found = []
    for directory, _subdirs, files in os.walk(root):
        for file in files:
            if file.endswith(".py"):
                path = os.path.join(directory, file)
                with open(path, encoding="utf-8", errors="replace") as handle:
                    if name in handle.read():
                        found.append(os.path.relpath(path, root))
    return sorted(found)


def fingerprint(targets: Iterable[Target], root: str) -> str:
    """Return the SHA-256 fingerprint of the targets in the ansible package at root."""
    digest = hashlib.sha256()
    parsed: dict[str, ast.Module] = {}
    for relative, qualname in targets:
        if qualname.startswith("files:"):
            text = repr(_files_mentioning(root, qualname[len("files:") :]))
            digest.update(f"{relative}:{qualname}\n{text}\n".encode())
            continue
        if relative not in parsed:
            with open(os.path.join(root, relative), encoding="utf-8") as handle:
                parsed[relative] = ast.parse(handle.read(), filename=relative)
        tree = parsed[relative]
        if qualname.startswith("refs:"):
            text = repr(_references(tree, qualname[len("refs:") :]))
        else:
            nodes = _definitions(tree, qualname)
            if not nodes:
                raise LookupError(f"{relative}: {qualname} not found")
            text = "\n".join(ast.unparse(node) for node in nodes)
        digest.update(f"{relative}:{qualname}\n{text}\n".encode())
    return digest.hexdigest()
