from dataclasses import dataclass
from typing import Any

from ast_nodes import Node

DEFAULTS = {"int": 0, "float": 0.0, "bool": False, "string": "", "char": "\0"}

BINARY_OPCODE = {
    "+": "ADD", "-": "SUB", "*": "MUL", "/": "DIV", "%": "MOD",
    "==": "EQ", "!=": "NE", "<": "LT", ">": "GT", "<=": "LE", ">=": "GE",
    "&&": "AND", "||": "OR",
}
UNARY_OPCODE = {"-": "UMINUS", "+": "UPLUS", "!": "NOT"}


class IRGenError(Exception):
    """A semantic error caught while generating intermediate code."""


class Const:
    """Wraps a literal value so a quad operand can be told apart from a
    variable/temporary name (both would otherwise just be a str)."""

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def __repr__(self):
        return repr(self.value)


@dataclass
class Quad:
    op: str
    arg1: Any = None
    arg2: Any = None
    result: Any = None

    def __str__(self):
        return f"({self.op}, {_fmt(self.arg1)}, {_fmt(self.arg2)}, {_fmt(self.result)})"


def _fmt(operand):
    if operand is None:
        return "-"
    if isinstance(operand, Const):
        v = operand.value
        return ("true" if v else "false") if isinstance(v, bool) else str(v)
    if isinstance(operand, tuple):
        return "(" + ", ".join(operand) + ")"
    return str(operand)


def format_quads(quads):
    width = max(1, len(str(len(quads) - 1)))
    return "\n".join(f"{i:0{width}d}: {q}" for i, q in enumerate(quads))


def type_name_of(type_node):
    if isinstance(type_node, Node) and type_node.kind == "Type":
        return type_node.fields["name"]
    return None


class IRGenerator:
    def __init__(self):
        self.quads = []
        self.functions = {}  # name -> {"params": [name, ...], "entry": quad index}
        self.var_types = {}  # variable/param name -> declared type name
        self.mutable = {}  # variable name -> may it be reassigned
        self._temp_count = 0
        self._label_count = 0

    def emit(self, op, arg1=None, arg2=None, result=None):
        self.quads.append(Quad(op, arg1, arg2, result))
        return len(self.quads) - 1

    def new_temp(self):
        self._temp_count += 1
        return f"_t{self._temp_count}"

    def new_label(self):
        self._label_count += 1
        return f"L{self._label_count}"

    def generate(self, program):
        declarations = program.fields["declarations"]
        const_decls = [d for d in declarations if d.kind == "VarDecl"]
        func_decls = [d for d in declarations if d.kind == "FunctionDecl"]

        for decl in const_decls:
            self.gen_var_decl(decl)

        start_label = self.new_label()
        self.emit("JMP", result=start_label)

        for fn in func_decls:
            self.gen_function(fn)

        self.emit("LABEL", result=start_label)
        self.emit("CALL", "main", 0, None)
        self.emit("HALT")

        return self.quads, self.functions, self.var_types

    # declarations

    def gen_var_decl(self, decl):
        mutable = decl.fields["mutability"] == "var"
        for declarator in decl.fields["declarators"]:
            name = declarator.fields["name"]
            type_name = type_name_of(declarator.fields["type"])
            self.var_types[name] = type_name
            self.mutable[name] = mutable
            initializer = declarator.fields["initializer"]
            value = self.gen_expr(initializer) if initializer is not None else Const(DEFAULTS.get(type_name))
            self.emit("ASSIGN", value, None, name)

    def gen_function(self, fn):
        name = fn.fields["name"]
        params = [p.fields["name"] for p in fn.fields["params"]]
        for p in fn.fields["params"]:
            self.var_types[p.fields["name"]] = type_name_of(p.fields["type"])
            self.mutable[p.fields["name"]] = True

        self.emit("FUNC_BEGIN", result=name)
        self.functions[name] = {"params": params, "entry": len(self.quads)}
        self.gen_block(fn.fields["body"])
        self.emit("RETURN")  # covers implicit fall-off-the-end returns
        self.emit("FUNC_END", result=name)

    def gen_block(self, block):
        for decl in block.fields["declarations"]:
            if decl.kind != "VarDecl":
                raise NotImplementedError(f"Declaration not supported yet: {decl.kind}")
            self.gen_var_decl(decl)
        for stmt in block.fields["statements"]:
            self.gen_stmt(stmt)

    # statements

    def gen_stmt(self, node):
        kind = node.kind
        if kind == "ExprStmt":
            self.gen_expr(node.fields["expression"])
        elif kind == "IfStmt":
            self.gen_if(node)
        elif kind == "ReturnStmt":
            values = node.fields["values"]
            value = self.gen_expr(values[0]) if values else None
            self.emit("RETURN", value)
        elif kind == "BuiltinStmt":
            self.gen_builtin(node)
        elif kind == "Block":
            self.gen_block(node)
        else:
            raise NotImplementedError(f"Statement not supported yet: {kind}")

    def gen_if(self, node):
        cond = self.gen_expr(node.fields["condition"])
        else_label = self.new_label()
        end_label = self.new_label()

        self.emit("JZ", cond, None, else_label)
        self.gen_block(node.fields["then"])
        self.emit("JMP", result=end_label)

        self.emit("LABEL", result=else_label)
        else_branch = node.fields["else"]
        if else_branch is not None:
            if else_branch.kind == "Block":
                self.gen_block(else_branch)
            else:
                self.gen_stmt(else_branch)  # chained 'else if'
        self.emit("LABEL", result=end_label)

    def gen_builtin(self, node):
        args = node.fields["args"]
        if node.fields["name"] == "print":
            for i, arg in enumerate(args):
                if i > 0:
                    self.emit("PRINT_SEP")
                self.emit("PRINT", self.gen_expr(arg))
            self.emit("PRINTLN")
        else:  # input
            names = tuple(a.fields["name"] for a in args)
            self.emit("INPUT", result=names)

    # expressions

    def gen_expr(self, node):
        kind = node.kind

        if kind == "Literal":
            return Const(node.fields["value"])
        if kind == "Identifier":
            return node.fields["name"]
        if kind == "Grouping":
            return self.gen_expr(node.fields["expression"])

        if kind == "UnaryExpr":
            operand = self.gen_expr(node.fields["operand"])
            t = self.new_temp()
            self.emit(UNARY_OPCODE[node.fields["op"]], operand, None, t)
            return t

        if kind == "BinaryExpr":
            left = self.gen_expr(node.fields["left"])
            right = self.gen_expr(node.fields["right"])
            t = self.new_temp()
            self.emit(BINARY_OPCODE[node.fields["op"]], left, right, t)
            return t

        if kind == "AssignExpr":
            target = node.fields["target"]
            if target.kind != "Identifier":
                raise NotImplementedError("Only plain identifier assignment is supported")
            name = target.fields["name"]
            if not self.mutable.get(name, True):
                raise IRGenError(f"Cannot assign to immutable variable '{name}'")
            value = self.gen_expr(node.fields["value"])
            self.emit("ASSIGN", value, None, name)
            return name

        if kind == "CallExpr":
            callee = node.fields["callee"]
            if callee.kind != "Identifier":
                raise NotImplementedError("Only direct function calls are supported")
            args = [self.gen_expr(a) for a in node.fields["args"]]
            for a in args:
                self.emit("PARAM", a)
            t = self.new_temp()
            self.emit("CALL", callee.fields["name"], len(args), t)
            return t

        raise NotImplementedError(f"Expression not supported yet: {kind}")


def generate(program):
    return IRGenerator().generate(program)
