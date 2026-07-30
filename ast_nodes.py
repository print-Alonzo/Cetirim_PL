"""Shared AST vocabulary: the single `Node` type every other phase passes
around, plus the small helpers that operate on trees of it.

This file has no "phase" of its own — it is the common ground between the
parser and everything downstream of it:

    grammar.py / parser.py   build Node trees (the AST)
    semantics.py             walks Node trees to resolve names and types
    ir.py                    walks Node trees (now annotated by semantics.py)
                             to emit quadruples

`Token` and `TT` are imported from `scanner.py` only for `ParseError`, which
needs to report the token position where a parse failed.

Worked example — `int x[3];` parses its declarator as
`{"name": "x", "arrays": [Literal(3)]}` (see `grammar.py`'s
`_declarator_name_fn`), and `merge_type(Node("Type", {"name": "int"}), ...)`
folds that into:

    Node("ArrayType", {"base": Node("Type", {"name": "int"}), "size": Literal(3)})

which is the `Node` semantics.py and ir.py actually see for `x`'s type.
"""

from dataclasses import dataclass, field
from typing import Any, Dict

from scanner import TT, Token


@dataclass
class ParseError:
    """A single recorded syntax error: a message plus the token where the
    parser was standing when it gave up on the current alternative."""

    message: str
    token: Token

    def __str__(self):
        # EOF has no meaningful lexeme to show ("" is confusing in an error
        # message), so it gets a literal "EOF" label instead of `repr(self.token.lexeme)`.
        found = "EOF" if self.token.ttype == TT.EOF else repr(self.token.lexeme)
        return (
            f"[SYNTAX ERROR] Line {self.token.line}, Col {self.token.col}: "
            f"{self.message} (found {found})"
        )


@dataclass
class Node:
    """A generic AST node: a `kind` tag plus a `fields` dict of children.

    There is deliberately no subclass per node kind (no `IfStmt` class, no
    `BinaryExpr` class, ...) — everything is this one `Node` shape. That
    keeps `to_dict()`/JSON serialization, and every tree-walk in
    semantics.py/ir.py, generic: adding a new node kind in grammar.py never
    requires touching this file.

    `line`/`col` are real dataclass fields, but kept *outside* `fields` on
    purpose: `to_dict()` only serializes `fields`, so position info never
    leaks into the `--ast` JSON output. They're stamped automatically by
    grammar_engine.py (see `_stamp_position`), not set here.
    """

    kind: str
    fields: Dict[str, Any] = field(default_factory=dict)
    line: int = None
    col: int = None

    def to_dict(self):
        """Recursively convert this node to a plain, JSON-safe dict (used by
        parser.py's `--ast` output). Delegates each field's value to
        `to_jsonable`, since a field can itself be a Node, a list of Nodes,
        or a plain value."""
        return {"kind": self.kind, **{k: to_jsonable(v) for k, v in self.fields.items()}}


def to_jsonable(value):
    """Mutually recursive with `Node.to_dict`: unwrap whatever shape a
    field's value takes (a nested Node, a list of them, a dict of them, or
    a plain literal) into something `json.dumps` can serialize directly."""
    if isinstance(value, Node):
        return value.to_dict()
    if isinstance(value, list):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    return value


def merge_type(base, declarator):
    """Fold a declarator's trailing `[size]` suffixes into `base`, building
    one `ArrayType` node per bracket pair.

    `declarator["arrays"]` is a plain list of size expressions (or `None`
    for `[]`), one per bracket pair, in the order they were scanned
    left-to-right (see grammar.py's `_declarator_name_fn`). Each iteration
    wraps the *previous* result, so the first bracket scanned becomes the
    innermost `ArrayType` and the last becomes the outermost — e.g. for
    `int x[3];`, `arrays == [Literal(3)]` and this returns
    `ArrayType(base=Type(int), size=Literal(3))`.
    """
    typ = base

    for size in declarator["arrays"]:
        typ = Node("ArrayType", {"base": typ, "size": size})

    return typ
