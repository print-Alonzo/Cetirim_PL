from dataclasses import dataclass, field
from typing import Any, Dict

from scanner import TT, Token


@dataclass
class ParseError:
    message: str
    token: Token

    def __str__(self):
        found = "EOF" if self.token.ttype == TT.EOF else repr(self.token.lexeme)
        return (
            f"[SYNTAX ERROR] Line {self.token.line}, Col {self.token.col}: "
            f"{self.message} (found {found})"
        )


@dataclass
class Node:
    kind: str
    fields: Dict[str, Any] = field(default_factory=dict)

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
    typ = base

    for size in declarator["arrays"]:
        typ = Node("ArrayType", {"base": typ, "size": size})

    return typ
