"""Intermediate code generator: lowers a type-checked AST (see semantics.py)
into a flat list of quadruples the VM in interpreter.py executes directly.

Every quad is a genuine 4-tuple `(op, arg1, arg2, result)`. Operands are
either a `Const` (a literal value), a plain string naming a variable/temp/
label, or (for a few opcodes) a tuple of names.

Opcode groups:
  Data        ASSIGN, CAST
  Arithmetic  ADD SUB MUL DIV MOD, UMINUS, UPLUS
  Relational  EQ NE LT GT LE GE
  Logical     NOT   (&& and || lower to jumps for short-circuit evaluation)
  Control     LABEL, JMP, JZ, JNZ
  Functions   FUNC_BEGIN, FUNC_END, PARAM, CALL, RETURN
  Tuples      TUPLE_NEW, TUPLE_APPEND, UNPACK
  Arrays      ARR_NEW, ARR_LOAD, ARR_STORE, ARR_LEN
  Structs     STRUCT_NEW, FIELD_LOAD, FIELD_STORE
  Strings     CONCAT, TOSTR
  Exceptions  TRY_PUSH, TRY_POP, THROW, RETHROW, CATCH_STORE
  I/O         PRINT, PRINT_SEP, PRINTLN, INPUT
  Misc        HALT, NOP

Convention for the two-operand memory ops: LOAD ops read (container, key) ->
result; STORE ops write (key, value) -> result=container. So ARR_LOAD is
(array, index, temp) but ARR_STORE is (index, value, array); same shape for
FIELD_LOAD/FIELD_STORE.

`generate(program, symtab)` consumes the AST plus the SymbolTable built by
semantics.analyze() - every name reference, type coercion, and call-argument
ordering was already resolved there, so this module never re-resolves a name
or re-checks a type; it only walks the AST and emits quads.
"""

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Any

from ast_nodes import Node
from parser import parse_source
from semantics import analyze

DEFAULTS = {"int": 0, "float": 0.0, "bool": False, "string": "", "char": "\0"}

BINARY_OPCODE = {
    "+": "ADD", "-": "SUB", "*": "MUL", "/": "DIV", "%": "MOD",
    "==": "EQ", "!=": "NE", "<": "LT", ">": "GT", "<=": "LE", ">=": "GE",
}
UNARY_OPCODE = {"-": "UMINUS", "+": "UPLUS", "!": "NOT"}

_PREDICATE_BINOPS = {"<", ">", "<=", ">=", "==", "!=", "&&", "||"}


class IRGenError(Exception):
    """A code-generation-time error for constructs semantics.py doesn't
    already reject (e.g. an array with neither an explicit size nor an
    initializer)."""


class Const:
    """Wraps a literal value so a quad operand can be told apart from a
    variable/temporary/label name (both would otherwise just be a str)."""

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
    line: int = None  # source line of the statement that produced this quad

    def __str__(self):
        return f"({self.op}, {_fmt(self.arg1)}, {_fmt(self.arg2)}, {_fmt(self.result)})"


def _fmt(operand):
    if operand is None:
        return "-"
    if isinstance(operand, Const):
        v = operand.value
        return ("true" if v else "false") if isinstance(v, bool) else str(v)
    if isinstance(operand, tuple):
        return "(" + ", ".join(str(x) for x in operand) + ")"
    return str(operand)


def format_quads(quads):
    width = max(1, len(str(len(quads) - 1)))
    return "\n".join(f"{i:0{width}d}: {q}" for i, q in enumerate(quads))


class IRGenerator:
    def __init__(self, symtab):
        self.st = symtab
        self.quads = []
        self.functions = {}  # name -> {"params": [ir_name, ...], "entry": quad index}
        self.var_types = {}  # ir_name -> type tag ("int"/"float"/.../"array"/"struct"/"tuple")
        self.structs = {}  # struct name -> {"fields": [(field_name, type_tag), ...]}
        self._temp_count = 0
        self._label_count = 0
        self._loop_stack = []  # list of (continue_label, break_label)
        self._current_line = None

    def emit(self, op, arg1=None, arg2=None, result=None):
        self.quads.append(Quad(op, arg1, arg2, result, self._current_line))
        return len(self.quads) - 1

    def new_temp(self):
        self._temp_count += 1
        return f"_t{self._temp_count}"

    def new_label(self):
        self._label_count += 1
        return f"L{self._label_count}"

    @staticmethod
    def _type_tag(type_):
        return type_[1] if type_[0] == "prim" else type_[0]

    @staticmethod
    def _default_const(type_):
        if type_[0] == "prim":
            return Const(DEFAULTS.get(type_[1]))
        return Const(None)

    # -- top level -----------------------------------------------------

    def generate(self, program):
        self.structs = {
            name: {"fields": [(fname, self._type_tag(info.fields[fname])) for fname in info.field_order]}
            for name, info in self.st.structs.items()
        }

        declarations = program.fields["declarations"]
        const_decls = [d for d in declarations if d.kind == "VarDecl"]
        func_decls = [d for d in declarations if d.kind == "FunctionDecl"]

        for decl in const_decls:
            self.gen_global_var_decl(decl)

        start_label = self.new_label()
        self.emit("JMP", result=start_label)

        for fn in func_decls:
            self.gen_function(fn)

        self.emit("LABEL", result=start_label)
        self.emit("CALL", "main", 0, None)
        self.emit("HALT")

        return self.quads, self.functions, self.var_types, self.structs

    def gen_global_var_decl(self, decl):
        for declarator in decl.fields["declarators"]:
            self._current_line = declarator.line
            sym = self.st.node_symbol[id(declarator)]
            self.var_types[sym.ir_name] = self._type_tag(sym.type)
            value = self._gen_expr_coerced(declarator.fields["initializer"])
            self.emit("ASSIGN", value, None, sym.ir_name)

    def gen_function(self, fn):
        name = fn.fields["name"]
        param_ir_names = []
        for p in fn.fields["params"]:
            sym = self.st.node_symbol[id(p)]
            self.var_types[sym.ir_name] = self._type_tag(sym.type)
            param_ir_names.append(sym.ir_name)

        self.emit("FUNC_BEGIN", result=name)
        self.functions[name] = {"params": param_ir_names, "entry": len(self.quads)}
        self.gen_block(fn.fields["body"])
        self.emit("RETURN")  # covers implicit fall-off-the-end returns
        self.emit("FUNC_END", result=name)

    # -- declarations ----------------------------------------------------

    def gen_block(self, block):
        for decl in block.fields["declarations"]:
            self.gen_local_decl(decl)
        for stmt in block.fields["statements"]:
            self.gen_stmt(stmt)

    def gen_local_decl(self, decl):
        if decl.kind == "VarDecl":
            self.gen_var_decl(decl)
        elif decl.kind == "LetDecl":
            self.gen_let_decl(decl)
        else:
            raise IRGenError(f"Declaration not supported: {decl.kind}")

    def gen_var_decl(self, decl):
        for declarator in decl.fields["declarators"]:
            self._current_line = declarator.line
            sym = self.st.node_symbol[id(declarator)]
            self.var_types[sym.ir_name] = self._type_tag(sym.type)
            self._gen_declare(sym, declarator.fields["initializer"])

    def _gen_declare(self, sym, init):
        type_ = sym.type
        if type_[0] == "array":
            self._gen_array_declare(sym, type_, init)
        elif type_[0] == "struct":
            self._gen_struct_declare(sym, type_, init)
        else:
            value = self._gen_expr_coerced(init) if init is not None else self._default_const(type_)
            self.emit("ASSIGN", value, None, sym.ir_name)

    def _gen_array_declare(self, sym, type_, init):
        elem_type = type_[1]
        elem_tag = self._type_tag(elem_type)
        if init is not None and init.kind == "InitializerList":
            values = init.fields["values"]
            self.emit("ARR_NEW", Const(len(values)), elem_tag, sym.ir_name)
            for i, v in enumerate(values):
                self.emit("ARR_STORE", Const(i), self._gen_expr_coerced(v), sym.ir_name)
        elif init is not None:
            self.emit("ASSIGN", self._gen_expr(init), None, sym.ir_name)
        else:
            size = type_[2]
            if size is None:
                raise IRGenError(f"Array '{sym.name}' needs an explicit size or initializer")
            self.emit("ARR_NEW", Const(size), elem_tag, sym.ir_name)

    def _gen_struct_declare(self, sym, type_, init):
        struct_name = type_[1]
        self.emit("STRUCT_NEW", struct_name, None, sym.ir_name)
        if init is not None and init.kind == "InitializerList":
            info = self.st.structs[struct_name]
            for fname, v in zip(info.field_order, init.fields["values"]):
                self.emit("FIELD_STORE", Const(fname), self._gen_expr_coerced(v), sym.ir_name)
        elif init is not None:
            self.emit("ASSIGN", self._gen_expr(init), None, sym.ir_name)

    def gen_let_decl(self, decl):
        self._current_line = decl.line
        array_sizes = decl.fields.get("array_sizes")
        values = decl.fields["values"]
        if array_sizes is not None:
            sym = self.st.node_symbol[id(decl)]
            self.var_types[sym.ir_name] = self._type_tag(sym.type)
            self._gen_array_declare(sym, sym.type, values[0])
            return
        symbols = self.st.node_symbol[id(decl)]
        self._gen_destructure(symbols, values)

    def _gen_destructure(self, symbols, values):
        """Shared by LetDecl (multi-name form) and MultiAssign: unpack a
        single multi-return call, or zip several values positionally."""
        for sym in symbols:
            self.var_types[sym.ir_name] = self._type_tag(sym.type)
        if len(values) == 1 and len(symbols) > 1:
            value = self._gen_expr(values[0])
            self.emit("UNPACK", value, None, tuple(s.ir_name for s in symbols))
        else:
            for sym, v in zip(symbols, values):
                self.emit("ASSIGN", self._gen_expr_coerced(v), None, sym.ir_name)

    # -- statements --------------------------------------------------------

    def gen_stmt(self, node):
        kind = node.kind
        self._current_line = node.line
        if kind == "ExprStmt":
            self._gen_expr(node.fields["expression"])
        elif kind == "Block":
            self.gen_block(node)
        elif kind == "IfStmt":
            self.gen_if(node)
        elif kind == "ForStmt":
            self.gen_for(node)
        elif kind == "ForInStmt":
            self.gen_for_in(node)
        elif kind == "WhileStmt":
            self.gen_while(node)
        elif kind == "RepeatUntilStmt":
            self.gen_repeat_until(node)
        elif kind == "ReturnStmt":
            self.gen_return(node)
        elif kind == "LoopControlStmt":
            self.gen_loop_control(node)
        elif kind == "BuiltinStmt":
            self.gen_builtin(node)
        elif kind == "MatchStmt":
            self.gen_match(node, is_expr=False)
        elif kind == "GuardStmt":
            self.gen_guard(node)
        elif kind == "TryStmt":
            self.gen_try(node)
        elif kind == "ThrowStmt":
            self.emit("THROW", self._gen_expr_coerced(node.fields["value"]))
        elif kind == "MultiAssign":
            self.gen_multi_assign(node)
        else:
            raise IRGenError(f"Statement not supported: {kind}")

    def gen_if(self, node):
        cond = self._gen_expr_coerced(node.fields["condition"])
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

    def gen_while(self, node):
        top, end = self.new_label(), self.new_label()
        self.emit("LABEL", result=top)
        cond = self._gen_expr_coerced(node.fields["condition"])
        self.emit("JZ", cond, None, end)
        self._loop_stack.append((top, end))
        self.gen_block(node.fields["body"])
        self._loop_stack.pop()
        self.emit("JMP", result=top)
        self.emit("LABEL", result=end)

    def gen_for(self, node):
        if node.fields["init"] is not None:
            self._gen_expr(node.fields["init"])
        cond_label, cont_label, end_label = self.new_label(), self.new_label(), self.new_label()
        self.emit("LABEL", result=cond_label)
        if node.fields["condition"] is not None:
            cond = self._gen_expr_coerced(node.fields["condition"])
            self.emit("JZ", cond, None, end_label)
        self._loop_stack.append((cont_label, end_label))
        self.gen_block(node.fields["body"])
        self._loop_stack.pop()
        self.emit("LABEL", result=cont_label)
        if node.fields["update"] is not None:
            self._gen_expr(node.fields["update"])
        self.emit("JMP", result=cond_label)
        self.emit("LABEL", result=end_label)

    def gen_for_in(self, node):
        sym = self.st.node_symbol[id(node)]
        self.var_types[sym.ir_name] = self._type_tag(sym.type)
        iterable = node.fields["iterable"]
        cond_label, cont_label, end_label = self.new_label(), self.new_label(), self.new_label()

        if iterable.kind == "RangeExpr":
            start = self._gen_expr(iterable.fields["start"])
            end_temp = self.new_temp()
            self.emit("ASSIGN", self._gen_expr(iterable.fields["end"]), None, end_temp)
            self.emit("ASSIGN", start, None, sym.ir_name)
            self.emit("LABEL", result=cond_label)
            cmp_t = self.new_temp()
            self.emit("LE", sym.ir_name, end_temp, cmp_t)
            self.emit("JZ", cmp_t, None, end_label)
            self._loop_stack.append((cont_label, end_label))
            self.gen_block(node.fields["body"])
            self._loop_stack.pop()
            self.emit("LABEL", result=cont_label)
            incr_t = self.new_temp()
            self.emit("ADD", sym.ir_name, Const(1), incr_t)
            self.emit("ASSIGN", incr_t, None, sym.ir_name)
        else:
            arr = self._gen_expr(iterable)
            idx_t, len_t = self.new_temp(), self.new_temp()
            self.emit("ASSIGN", Const(0), None, idx_t)
            self.emit("ARR_LEN", arr, None, len_t)
            self.emit("LABEL", result=cond_label)
            cmp_t = self.new_temp()
            self.emit("LT", idx_t, len_t, cmp_t)
            self.emit("JZ", cmp_t, None, end_label)
            self.emit("ARR_LOAD", arr, idx_t, sym.ir_name)
            self._loop_stack.append((cont_label, end_label))
            self.gen_block(node.fields["body"])
            self._loop_stack.pop()
            self.emit("LABEL", result=cont_label)
            incr_t = self.new_temp()
            self.emit("ADD", idx_t, Const(1), incr_t)
            self.emit("ASSIGN", incr_t, None, idx_t)

        self.emit("JMP", result=cond_label)
        self.emit("LABEL", result=end_label)

    def gen_repeat_until(self, node):
        top, cont_label, end = self.new_label(), self.new_label(), self.new_label()
        self.emit("LABEL", result=top)
        self._loop_stack.append((cont_label, end))
        self.gen_block(node.fields["body"])
        self._loop_stack.pop()
        self.emit("LABEL", result=cont_label)
        cond = self._gen_expr_coerced(node.fields["condition"])
        self.emit("JZ", cond, None, top)
        self.emit("LABEL", result=end)

    def gen_loop_control(self, node):
        if not self._loop_stack:
            raise IRGenError(f"'{node.fields['keyword']}' used outside of a loop")
        cont_label, end_label = self._loop_stack[-1]
        target = end_label if node.fields["keyword"] == "break" else cont_label
        self.emit("JMP", result=target)

    def gen_return(self, node):
        values = node.fields["values"]
        if not values:
            self.emit("RETURN")
        elif len(values) == 1:
            self.emit("RETURN", self._gen_expr_coerced(values[0]))
        else:
            tup = self.new_temp()
            self.emit("TUPLE_NEW", None, None, tup)
            for v in values:
                self.emit("TUPLE_APPEND", tup, self._gen_expr_coerced(v), tup)
            self.emit("RETURN", tup)

    def gen_builtin(self, node):
        args = node.fields["args"]
        if node.fields["name"] == "print":
            for i, arg in enumerate(args):
                if i > 0:
                    self.emit("PRINT_SEP")
                self.emit("PRINT", self._gen_expr(arg))
            self.emit("PRINTLN")
        else:  # input
            names = tuple(self.st.node_symbol[id(a)].ir_name for a in args)
            self.emit("INPUT", result=names)

    def gen_guard(self, node):
        cond = self._gen_expr_coerced(node.fields["condition"])
        else_label, end_label = self.new_label(), self.new_label()
        self.emit("JZ", cond, None, else_label)
        self.emit("JMP", result=end_label)
        self.emit("LABEL", result=else_label)
        self.gen_block(node.fields["else_body"])
        self.emit("LABEL", result=end_label)

    def gen_try(self, node):
        # Catch clauses carry no exception type, so only the first one is
        # ever reachable (see the project plan's "first matching catch
        # wins" assumption) - later clauses were still type-checked by
        # semantics.py, but are dead code and are not lowered here.
        catch = node.fields["catch_clauses"][0]
        finally_body = node.fields["finally_body"]
        catch_label, fin_normal, fin_rethrow = self.new_label(), self.new_label(), self.new_label()

        self.emit("TRY_PUSH", catch_label)
        self.gen_block(node.fields["body"])
        self.emit("TRY_POP")
        self.emit("JMP", result=fin_normal)

        self.emit("LABEL", result=catch_label)
        catch_sym = self.st.node_symbol[id(catch)]
        self.var_types[catch_sym.ir_name] = "string"
        self.emit("CATCH_STORE", result=catch_sym.ir_name)
        self.emit("TRY_PUSH", fin_rethrow)  # a throw during the catch body still runs `finally`
        self.gen_block(catch.fields["body"])
        self.emit("TRY_POP")
        self.emit("JMP", result=fin_normal)

        self.emit("LABEL", result=fin_rethrow)
        if finally_body is not None:
            self.gen_block(finally_body)
        self.emit("RETHROW")

        self.emit("LABEL", result=fin_normal)
        if finally_body is not None:
            self.gen_block(finally_body)

    def gen_multi_assign(self, node):
        symbols = self.st.node_symbol[id(node)]
        self._gen_destructure(symbols, node.fields["values"])

    # -- match (statement and expression) -----------------------------------

    def gen_match(self, node, is_expr):
        subject = self._gen_expr(node.fields["subject"])
        end_label = self.new_label()
        result_temp = self.new_temp() if is_expr else None
        value_key = "value" if is_expr else "body"

        for case in node.fields["cases"]:
            next_label = self.new_label()
            self._gen_match_test(case.fields["pattern"], subject, next_label)
            value_node = case.fields[value_key]
            if is_expr:
                self.emit("ASSIGN", self._gen_expr_coerced(value_node), None, result_temp)
            else:
                self.gen_stmt(value_node)
            self.emit("JMP", result=end_label)
            self.emit("LABEL", result=next_label)

        self.emit("LABEL", result=end_label)
        return result_temp

    def _gen_match_test(self, pattern, subject, fail_label):
        """Emits code that falls through if `pattern` matches `subject`, or
        jumps to `fail_label` if it doesn't."""
        if pattern.kind == "WildcardPattern":
            return
        if pattern.kind == "RangeExpr":
            ge_t = self.new_temp()
            self.emit("GE", subject, self._gen_expr(pattern.fields["start"]), ge_t)
            self.emit("JZ", ge_t, None, fail_label)
            le_t = self.new_temp()
            self.emit("LE", subject, self._gen_expr(pattern.fields["end"]), le_t)
            self.emit("JZ", le_t, None, fail_label)
            return
        if self._is_predicate_pattern(pattern):
            self.emit("JZ", self._gen_expr(pattern), None, fail_label)
            return
        eq_t = self.new_temp()
        self.emit("EQ", subject, self._gen_expr(pattern), eq_t)
        self.emit("JZ", eq_t, None, fail_label)

    @staticmethod
    def _is_predicate_pattern(pattern):
        if pattern.kind == "BinaryExpr" and pattern.fields.get("op") in _PREDICATE_BINOPS:
            return True
        return pattern.kind == "UnaryExpr" and pattern.fields.get("op") == "!"

    # -- expressions ---------------------------------------------------------

    def _gen_expr_coerced(self, node):
        value = self._gen_expr(node)
        target = self.st.node_coerce.get(id(node))
        if target is None:
            return value
        t = self.new_temp()
        self.emit("CAST", value, self._type_tag(target), t)
        return t

    def _gen_expr(self, node):
        kind = node.kind

        if kind == "Literal":
            return Const(node.fields["value"])
        if kind == "Identifier":
            return self.st.node_symbol[id(node)].ir_name
        if kind == "Grouping":
            return self._gen_expr(node.fields["expression"])
        if kind == "InterpString":
            return self._gen_interp_string(node)
        if kind == "UnaryExpr":
            operand = self._gen_expr(node.fields["operand"])
            t = self.new_temp()
            self.emit(UNARY_OPCODE[node.fields["op"]], operand, None, t)
            return t
        if kind == "BinaryExpr":
            return self._gen_binary(node)
        if kind == "AssignExpr":
            return self._gen_assign(node)
        if kind == "CallExpr":
            return self._gen_call(node)
        if kind == "IndexExpr":
            obj = self._gen_expr(node.fields["object"])
            idx = self._gen_expr(node.fields["index"])
            t = self.new_temp()
            self.emit("ARR_LOAD", obj, idx, t)
            return t
        if kind == "MemberExpr":
            obj = self._gen_expr(node.fields["object"])
            t = self.new_temp()
            self.emit("FIELD_LOAD", obj, Const(node.fields["field"]), t)
            return t
        if kind == "MatchExpr":
            return self.gen_match(node, is_expr=True)
        if kind == "RangeExpr":
            # only meaningful as a for-in iterable or a match pattern, both
            # of which special-case it before ever calling _gen_expr on it.
            raise IRGenError("RangeExpr cannot be evaluated as a plain expression")

        raise IRGenError(f"Expression not supported: {kind}")

    def _gen_binary(self, node):
        op = node.fields["op"]
        if op == "&&":
            return self._gen_short_circuit(node, is_and=True)
        if op == "||":
            return self._gen_short_circuit(node, is_and=False)
        left = self._gen_expr(node.fields["left"])
        right = self._gen_expr(node.fields["right"])
        t = self.new_temp()
        self.emit(BINARY_OPCODE[op], left, right, t)
        return t

    def _gen_short_circuit(self, node, is_and):
        result = self.new_temp()
        self.emit("ASSIGN", self._gen_expr(node.fields["left"]), None, result)
        skip_label = self.new_label()
        self.emit("JZ" if is_and else "JNZ", result, None, skip_label)
        self.emit("ASSIGN", self._gen_expr(node.fields["right"]), None, result)
        self.emit("LABEL", result=skip_label)
        return result

    def _gen_assign(self, node):
        target = node.fields["target"]
        value = self._gen_expr_coerced(node.fields["value"])
        if target.kind == "Identifier":
            sym = self.st.node_symbol[id(target)]
            self.emit("ASSIGN", value, None, sym.ir_name)
            return sym.ir_name
        if target.kind == "IndexExpr":
            obj = self._gen_expr(target.fields["object"])
            idx = self._gen_expr(target.fields["index"])
            self.emit("ARR_STORE", idx, value, obj)
            return value
        if target.kind == "MemberExpr":
            obj = self._gen_expr(target.fields["object"])
            self.emit("FIELD_STORE", Const(target.fields["field"]), value, obj)
            return value
        raise IRGenError(f"Unsupported assignment target: {target.kind}")

    def _gen_call(self, node):
        sig = self.st.node_call[id(node)]
        for a in self.st.node_call_args[id(node)]:
            self.emit("PARAM", self._gen_expr_coerced(a))
        t = self.new_temp()
        self.emit("CALL", sig.name, len(self.st.node_call_args[id(node)]), t)
        return t

    def _gen_interp_string(self, node):
        parts = node.fields["parts"]
        if not parts:
            return Const("")
        result = None
        for p in parts:
            str_t = self.new_temp()
            self.emit("TOSTR", self._gen_expr(p), None, str_t)
            if result is None:
                result = str_t
            else:
                concat_t = self.new_temp()
                self.emit("CONCAT", result, str_t, concat_t)
                result = concat_t
        return result


def generate(program: Node, symtab):
    return IRGenerator(symtab).generate(program)


def main():
    cli = argparse.ArgumentParser(description="CSC617M Custom Language Intermediate Code Generator")
    cli.add_argument("source_file", help="Path to source file to compile")
    cli.add_argument("-o", "--output", help="Write the quadruple listing to this file")
    cli.add_argument("--symbols", action="store_true", help="Also print function/struct/variable metadata")
    args = cli.parse_args()

    try:
        with open(args.source_file, "r", encoding="utf-8") as f:
            source = f.read()
    except FileNotFoundError:
        print(f"Error: file not found: {args.source_file}", file=sys.stderr)
        sys.exit(1)

    ast, syntax_errors, lex_errors = parse_source(source)
    if lex_errors or syntax_errors:
        for e in lex_errors:
            print(e, file=sys.stderr)
        for e in syntax_errors:
            print(e, file=sys.stderr)
        sys.exit(2)

    symtab, sem_errors = analyze(ast)
    if sem_errors:
        for e in sem_errors:
            print(e, file=sys.stderr)
        sys.exit(2)

    try:
        quads, functions, var_types, structs = generate(ast, symtab)
    except IRGenError as e:
        print(f"[IR ERROR] {e}", file=sys.stderr)
        sys.exit(2)

    lines = [format_quads(quads)]
    if args.symbols:
        lines.append("")
        lines.append("Functions:")
        for name, info in functions.items():
            lines.append(f"  {name}({', '.join(info['params'])}) @ {info['entry']}")
        lines.append("Structs:")
        for name, info in structs.items():
            fields = ", ".join(f"{fn}:{ft}" for fn, ft in info["fields"])
            lines.append(f"  {name} {{ {fields} }}")
        lines.append("Variables (name: type):")
        for name, tname in var_types.items():
            lines.append(f"  {name}: {tname}")
    report = "\n".join(lines) + "\n"

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"[IR] {os.path.basename(args.source_file)}")
        print(f"  Quads   : {len(quads)}")
        print(f"  Output  : {args.output}")
    else:
        print(report)


if __name__ == "__main__":
    main()
