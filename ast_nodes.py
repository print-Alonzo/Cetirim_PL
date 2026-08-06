from dataclasses import dataclass, field
from typing import Any, Dict

from scanner import TT, Token


@dataclass
class ParseError:
    message: str
    token: Token

    def __str__(self):
        # EOF has no real lexeme, so show "EOF" instead of ''
        found = "EOF" if self.token.ttype == TT.EOF else repr(self.token.lexeme)
        return (
            f"[SYNTAX ERROR] Line {self.token.line}, Col {self.token.col}: "
            f"{self.message} (found {found})"
        )


@dataclass
class Node:
    kind: str
    fields: Dict[str, Any] = field(default_factory=dict)
    line: int = None
    col: int = None

    def to_dict(self):
        return {"kind": self.kind, **{k: to_jsonable(v) for k, v in self.fields.items()}}


def to_jsonable(value):
    if isinstance(value, Node):
        return value.to_dict()
    if isinstance(value, list):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    return value


def merge_type(base, declarator):
    # fold [size] suffixes into base type, outermost first
    # e.g. int g[2][3] -> ArrayType(2, ArrayType(3, int))
    typ = base
    for size in reversed(declarator["arrays"]):
        typ = Node("ArrayType", {"base": typ, "size": size})
    return typ
