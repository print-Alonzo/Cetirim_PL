# IR generator: lowers a type-checked AST into a flat list of quadruples the VM executes directly
# a quad is (op, arg1, arg2, result); LOAD ops read (container, key), STORE ops write (key, value)

import argparse
import math
import os
import sys
from dataclasses import dataclass
from typing import Any

from ast_nodes import Node
from parser import parse_source
from semantics import T_CHAR, analyze

DEFAULTS = {"int": 0, "float": 0.0, "bool": False, "string": "", "char": "\0"}


def _div(a, b):
    # C-style truncated division (toward zero). lives here so optimizer.py's folder runs the exact same code the VM does
    if b == 0:
        raise ZeroDivisionError("Division by zero")
    if isinstance(a, float) or isinstance(b, float):
        return a / b
    q = a // b
    if q < 0 and q * b != a:
        q += 1  # // floors, correct toward zero instead
    return q


def _mod(a, b):
    # C-style truncated modulo (result takes the sign of the dividend), matching _div.
    # shared with optimizer.py's folder for the same reason
    if b == 0:
        raise ZeroDivisionError("Modulo by zero")
    if isinstance(a, float) or isinstance(b, float):
        return math.fmod(a, b)
    r = a % b
    if r != 0 and (r < 0) != (a < 0):
        r -= b
    return r

BINARY_OPCODE = {
    "+": "ADD", "-": "SUB", "*": "MUL", "/": "DIV", "%": "MOD",
    "==": "EQ", "!=": "NE", "<": "LT", ">": "GT", "<=": "LE", ">=": "GE",
}
UNARY_OPCODE = {"-": "UMINUS", "+": "UPLUS", "!": "NOT"}

_ARITHMETIC_BINOPS = {"+", "-", "*", "/", "%"}
_COMPARISON_BINOPS = {"==", "!=", "<", ">", "<=", ">="}


class IRGenError(Exception):
    pass  # a codegen-time error for constructs semantics.py doesn't already reject


class Const:
    # wraps a literal so a quad operand can be told apart from a variable/temp/label name
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def __repr__(self):
        return repr(self.value)


class TypeDesc:
    # full nested shape for an array/struct field type, unlike _type_tag's flattened string - lets interpreter.py's _make_default build a correctly-nested default value
    __slots__ = ("tag", "elem", "size", "name")

    def __init__(self, tag, elem=None, size=None, name=None):
        self.tag = tag  # a DEFAULTS key ("int"/"float"/"char"/"string"/"bool") | "array" | "struct"
        self.elem = elem  # TypeDesc, only set when tag == "array"
        self.size = size  # int or None, only set when tag == "array"
        self.name = name  # struct name, only set when tag == "struct"

    def __str__(self):
        if self.tag == "array":
            return f"{self.elem}[{'' if self.size is None else self.size}]"
        if self.tag == "struct":
            return f"struct {self.name}"
        return self.tag


@dataclass
class Quad:
    # line is the source line of the STATEMENT that produced this quad, not necessarily
    # the exact sub-expression, see gen_stmt's self._current_line
    op: str
    arg1: Any = None
    arg2: Any = None
    result: Any = None
    line: int = None

    def __str__(self):
        return f"({self.op}, {_fmt(self.arg1)}, {_fmt(self.arg2)}, {_fmt(self.result)})"


_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\t": "\\t", "\r": "\\r", "\0": "\\0"}


def _quote(s):
    # renders a string/char Const as an escaped double-quoted literal, so a real newline or NUL char can't corrupt the *_ir.txt listing
    out = ['"']
    for ch in s:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ch.isprintable():
            out.append(ch)
        else:
            out.append("\\x%02x" % ord(ch))
    out.append('"')
    return "".join(out)


def _fmt(operand):
    # a Const unwraps and quotes/escapes strings, a tuple joins with commas, a plain name stays bare - only a Const is ever quoted, that's what tells a literal apart from a name
    if operand is None:
        return "-"
    if isinstance(operand, Const):
        v = operand.value
        if isinstance(v, bool):
            return "true" if v else "false"
        return _quote(v) if isinstance(v, str) else str(v)
    if isinstance(operand, tuple):
        return "(" + ", ".join("_" if x is None else str(x) for x in operand) + ")"
    return str(operand)


def format_quads(quads):
    # zero-pads the index column to the width of the largest index so the table stays aligned
    width = max(1, len(str(len(quads) - 1)))
    return "\n".join(f"{i:0{width}d}: {q}" for i, q in enumerate(quads))


class IRGenerator:
    # one instance per generate() call, all the counters/tables below are per-program state

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
        # stamps the quad with the current statement's line, returns its index
        self.quads.append(Quad(op, arg1, arg2, result, self._current_line))
        return len(self.quads) - 1

    def new_temp(self):
        self._temp_count += 1
        return f"_t{self._temp_count}"

    def new_label(self):
        # labels are resolved to quad indices once, by IRExecutor.__init__, so quads
        # here only ever reference a label by name
        self._label_count += 1
        return f"L{self._label_count}"

    @staticmethod
    def _type_tag(type_):
        # collapse a semantics.py type descriptor to the short tag the VM cares about
        # at runtime, enough to pick a default value or a coercion target
        return type_[1] if type_[0] == "prim" else type_[0]

    @staticmethod
    def _type_desc(type_):
        # like _type_tag but recursive, keeps a nested array/struct's real shape (element
        # type, size, struct name) for ARR_NEW's arg2 and self.structs
        if type_[0] == "prim":
            return TypeDesc(type_[1])
        if type_[0] == "struct":
            return TypeDesc("struct", name=type_[1])
        if type_[0] == "array":
            return TypeDesc("array", elem=IRGenerator._type_desc(type_[1]), size=type_[2])
        return TypeDesc("void")

    @staticmethod
    def _default_const(type_):
        # arrays/structs never go through here, they're built by
        # _gen_array_declare/_gen_struct_declare instead
        if type_[0] == "prim":
            return Const(DEFAULTS.get(type_[1]))
        return Const(None)

    # top level

    def generate(self, program):
        # emits every global const, then every function body, then the real entry sequence.
        # the JMP over the function bodies exists since quads are one flat list with no separate "code section"
        self.structs = {
            name: {"fields": [(fname, self._type_desc(info.fields[fname])) for fname in info.field_order]}
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

        self._current_line = None
        self.emit("LABEL", result=start_label)
        self.emit("CALL", "main", 0, None)
        self.emit("HALT")

        return self.quads, self.functions, self.var_types, self.structs

    def gen_global_var_decl(self, decl):
        # one ASSIGN per declarator. globals are never arrays/structs here since
        # parser.py only allows const at global scope, and const initializers are literals
        for declarator in decl.fields["declarators"]:
            self._current_line = declarator.line
            sym = self.st.node_symbol[id(declarator)]
            self.var_types[sym.ir_name] = self._type_tag(sym.type)
            value = self._gen_expr_coerced(declarator.fields["initializer"])
            self.emit("ASSIGN", value, None, sym.ir_name)

    def gen_function(self, fn):
        # FUNC_BEGIN/FUNC_END are just readable boundary markers, skipped at runtime, and the
        # trailing RETURN covers falling off the end with no explicit return. entry is recorded as the index right after FUNC_BEGIN, that's where CALL jumps to
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

    # declarations

    def gen_block(self, block):
        # declarations then statements, mirrors semantics.py's _analyze_block
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
            self._gen_declare(sym, declarator.fields["initializer"], declarator.fields["type"])

    def _gen_declare(self, sym, init, type_node=None):
        # arrays/structs need their own multi-quad construction, a scalar is just one
        # ASSIGN of the (possibly coerced) initializer or a type-appropriate default
        type_ = sym.type
        if type_[0] == "array":
            self._gen_array_declare(sym, type_, init, type_node)
        elif type_[0] == "struct":
            self._gen_struct_declare(sym, type_, init)
        else:
            value = self._gen_expr_coerced(init) if init is not None else self._default_const(type_)
            self.emit("ASSIGN", value, None, sym.ir_name)

    def _gen_array_declare(self, sym, type_, init, type_node=None):
        # a {v1, ...} list sizes ARR_NEW to its own length then ARR_STOREs each element, an existing
        # array is just ASSIGNed (aliasing it), or with no init ARR_NEW zero-fills to the declared size
        elem_type = type_[1]
        elem_tag = self._type_desc(elem_type)
        if init is not None and init.kind == "InitializerList":
            values = init.fields["values"]
            self.emit("ARR_NEW", Const(len(values)), elem_tag, sym.ir_name)
            for i, v in enumerate(values):
                self.emit("ARR_STORE", Const(i), self._gen_expr_coerced(v), sym.ir_name)
        elif init is not None:
            self.emit("ASSIGN", self._gen_expr(init), None, sym.ir_name)
        else:
            size = type_[2]
            if size is not None:
                size_operand = Const(size)
            elif type_node is not None and type_node.kind == "ArrayType" and type_node.fields.get("size") is not None:
                size_operand = self._gen_expr(type_node.fields["size"])
            else:
                raise IRGenError(f"Array '{sym.name}' needs an explicit size or initializer")
            self.emit("ARR_NEW", size_operand, elem_tag, sym.ir_name)

    def _gen_struct_declare(self, sym, type_, init):
        # STRUCT_NEW runs first (default-inits every field), then either a {v1, v2, ...} list
        # overwrites fields positionally via FIELD_STORE, or an existing struct is ASSIGNed wholesale
        struct_name = type_[1]
        self.emit("STRUCT_NEW", struct_name, None, sym.ir_name)
        if init is not None and init.kind == "InitializerList":
            info = self.st.structs[struct_name]
            for fname, v in zip(info.field_order, init.fields["values"]):
                self.emit("FIELD_STORE", Const(fname), self._gen_expr_coerced(v), sym.ir_name)
        elif init is not None:
            self.emit("ASSIGN", self._gen_expr(init), None, sym.ir_name)

    def gen_let_decl(self, decl):
        # single-name array form reuses _gen_array_declare, destructuring form delegates
        # to _gen_destructure (shared with MultiAssign)
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
        # one value feeding several names (a multi-return call) becomes one UNPACK quad, otherwise
        # it's one ASSIGN per pair. a None entry in symbols is a `_` discard, evaluated but never stored
        for sym in symbols:
            if sym is not None:
                self.var_types[sym.ir_name] = self._type_tag(sym.type)
        if len(values) == 1 and len(symbols) > 1:
            value = self._gen_expr(values[0])
            names = tuple(s.ir_name if s is not None else None for s in symbols)
            self.emit("UNPACK", value, None, names)
        else:
            for sym, v in zip(symbols, values):
                value = self._gen_expr_coerced(v)
                if sym is not None:
                    self.emit("ASSIGN", value, None, sym.ir_name)

    # statements

    def gen_stmt(self, node):
        # every quad emitted here, including nested sub-expressions, gets line-tagged with THIS statement's line, not each sub-expression's own (see LIMITATIONS.md)
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
        # cond, JZ to Lelse, then-block, JMP to Lend, LABEL Lelse, else-block, LABEL Lend
        # a chained `else if` just recurses gen_stmt on the nested IfStmt in the Lelse slot
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
        # LABEL Ltop, cond, JZ to Lend, body, JMP back to Ltop, LABEL Lend
        # break/continue target (Ltop, Lend) via self._loop_stack - continue and the re-test point are the same label here, unlike for
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
        # init, LABEL Lcond, cond, JZ to Lend, body, LABEL Lcont, update, JMP back to Lcond, LABEL Lend
        # continue jumps to Lcont (so update still runs), break jumps straight to Lend
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
        # two lowerings sharing gen_for's loop shape: a RangeExpr counts the loop variable
        # itself up from start to end, an array counts a hidden index against ARR_LEN (computed once up front, not re-read every iteration) and ARR_LOADs into the loop variable
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
        # post-test loop, body always runs once first: LABEL Ltop, body, LABEL Lcont, cond, JZ back to Ltop, LABEL Lend
        # inverted sense vs while: JZ loops back, since "until" means "repeat while not yet true"
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
        # break/continue compile to one JMP to whatever the innermost loop pushed onto self._loop_stack; the guard below is just defense in depth
        if not self._loop_stack:
            raise IRGenError(f"'{node.fields['keyword']}' used outside of a loop")
        cont_label, end_label = self._loop_stack[-1]
        target = end_label if node.fields["keyword"] == "break" else cont_label
        self.emit("JMP", result=target)

    def gen_return(self, node):
        # bare return, one value, or several (built into a tuple via TUPLE_NEW/TUPLE_APPEND, then RETURNed whole - interpreter.py's _do_return hands that to whatever UNPACK is waiting, or discards it if unused)
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
        # print: one PRINT per arg with PRINT_SEP between, then PRINTLN. input: one INPUT quad with the whole tuple of target names, interpreter.py reads one token per name
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
        # the inverse of if: falls through when cond is true, only enters else_body when false
        cond = self._gen_expr_coerced(node.fields["condition"])
        else_label, end_label = self.new_label(), self.new_label()
        self.emit("JZ", cond, None, else_label)
        self.emit("JMP", result=end_label)
        self.emit("LABEL", result=else_label)
        self.gen_block(node.fields["else_body"])
        self.emit("LABEL", result=end_label)

    def gen_try(self, node):
        # the VM has no runtime notion of "finally", so this emits the finally body's quads
        # TWICE: once after a normal catch, once before a RETHROW, so a throw inside the catch still runs it. catch clauses carry no exception type, so only the first one is ever used
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
        # int a, b = f(); reuses the same unpack-or-zip logic let's destructuring form
        # uses, via _gen_destructure
        symbols = self.st.node_symbol[id(node)]
        self._gen_destructure(symbols, node.fields["values"])

    # match (statement and expression)

    def gen_match(self, node, is_expr):
        # both forms compile to an if-else chain: subject evaluated once, each case tests its
        # pattern (falls through on match, jumps to next_label otherwise) then jumps to end_label - at most one arm ever runs. expression form's result_temp collects whichever arm's value ran
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
        # falls through if pattern matches subject, jumps to fail_label if not - mirrors semantics.py's
        # _analyze_pattern: wildcard is a no-op, RangeExpr is GE-then-LE, a predicate pattern (flagged by node_is_predicate) evaluates standalone, anything else is a plain EQ against subject
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
        if self.st.node_is_predicate.get(id(pattern)):
            self.emit("JZ", self._gen_expr(pattern), None, fail_label)
            return
        eq_t = self.new_temp()
        self.emit("EQ", subject, self._gen_expr(pattern), eq_t)
        self.emit("JZ", eq_t, None, fail_label)

    # expressions

    def _gen_expr_coerced(self, node):
        # wraps node's value in a CAST if semantics.py recorded a node_coerce entry for it
        value = self._gen_expr(node)
        target = self.st.node_coerce.get(id(node))
        if target is None:
            return value
        t = self.new_temp()
        self.emit("CAST", value, self._type_tag(target), t)
        return t

    def _gen_expr(self, node):
        # save/set/restore self._current_line around the dispatch, so a sub-expression's quad gets stamped with ITS OWN line, not the enclosing statement's
        prev_line = self._current_line
        if node.line is not None:
            self._current_line = node.line
        try:
            return self._gen_expr_dispatch(node)
        finally:
            self._current_line = prev_line

    def _gen_expr_dispatch(self, node):
        # the actual dispatch by kind, split out so _gen_expr's line-tracking wrapper
        # doesn't have to re-indent this whole body
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
            op = node.fields["op"]
            operand_node = node.fields["operand"]
            if op in ("-", "+"):
                operand = self._gen_char_promoted(operand_node)
            else:
                operand = self._gen_expr(operand_node)
            t = self.new_temp()
            self.emit(UNARY_OPCODE[op], operand, None, t)
            return t
        if kind == "BinaryExpr":
            return self._gen_binary(node)
        if kind == "AssignExpr":
            return self._gen_assign(node)
        if kind == "CallExpr":
            return self._gen_call(node)
        if kind == "IndexExpr":
            # ARR_LOAD reads (array, index) -> result, per the LOAD/STORE convention up top
            obj = self._gen_expr(node.fields["object"])
            idx = self._gen_expr(node.fields["index"])
            t = self.new_temp()
            self.emit("ARR_LOAD", obj, idx, t)
            return t
        if kind == "MemberExpr":
            # FIELD_LOAD reads (struct, field_name) -> result.
            obj = self._gen_expr(node.fields["object"])
            t = self.new_temp()
            self.emit("FIELD_LOAD", obj, Const(node.fields["field"]), t)
            return t
        if kind == "MatchExpr":
            return self.gen_match(node, is_expr=True)
        if kind == "RangeExpr":
            # only meaningful as a for-in iterable or match pattern, both special-case it before ever calling _gen_expr
            raise IRGenError("RangeExpr cannot be evaluated as a plain expression")

        raise IRGenError(f"Expression not supported: {kind}")

    def _gen_char_promoted(self, ast_node):
        # if semantics.py inferred this as char, wrap it in CAST -> "int" first - a char and a length-1 string are the same python str at runtime, so this is the one place that actually knows which operands are char
        value = self._gen_expr(ast_node)
        if self.st.node_type.get(id(ast_node)) == T_CHAR:
            t = self.new_temp()
            self.emit("CAST", value, "int", t)
            return t
        return value

    def _gen_binary(self, node):
        # && and || divert to _gen_short_circuit since they lower to jumps. chars promote to int
        # unconditionally for arithmetic, but for comparisons only when the other side is int/float
        op = node.fields["op"]
        if op == "&&":
            return self._gen_short_circuit(node, is_and=True)
        if op == "||":
            return self._gen_short_circuit(node, is_and=False)
        left_node, right_node = node.fields["left"], node.fields["right"]
        if op in _ARITHMETIC_BINOPS:
            left = self._gen_char_promoted(left_node)
            right = self._gen_char_promoted(right_node)
        elif op in _COMPARISON_BINOPS:
            left_is_char = self.st.node_type.get(id(left_node)) == T_CHAR
            right_is_char = self.st.node_type.get(id(right_node)) == T_CHAR
            left = self._gen_char_promoted(left_node) if left_is_char and not right_is_char else self._gen_expr(left_node)
            right = self._gen_char_promoted(right_node) if right_is_char and not left_is_char else self._gen_expr(right_node)
        else:
            left = self._gen_expr(left_node)
            right = self._gen_expr(right_node)
        t = self.new_temp()
        self.emit(BINARY_OPCODE[op], left, right, t)
        return t

    def _gen_short_circuit(self, node, is_and):
        # lowers &&/|| to a conditional jump so the right operand is only evaluated when it
        # can still change the answer - there's no AND/OR opcode, so skipping needs a real jump
        result = self.new_temp()
        self.emit("ASSIGN", self._gen_expr(node.fields["left"]), None, result)
        skip_label = self.new_label()
        # JZ for &&: left already false, skip right. JNZ for ||: left already true, skip right
        self.emit("JZ" if is_and else "JNZ", result, None, skip_label)
        self.emit("ASSIGN", self._gen_expr(node.fields["right"]), None, result)
        self.emit("LABEL", result=skip_label)
        return result   # this temp holds the whole expression's value

    def _gen_assign(self, node):
        # target shape decides the quad: identifier -> ASSIGN, IndexExpr -> ARR_STORE, MemberExpr -> FIELD_STORE. the value is returned either way, since an assignment can itself be used as a value
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
        # one PARAM per argument (already positionally ordered by semantics.py), each coerced,
        # then one CALL naming the function/arg count/result temp - interpreter.py's _call pops exactly argcount values off the list those PARAMs built
        sig = self.st.node_call[id(node)]
        for a in self.st.node_call_args[id(node)]:
            self.emit("PARAM", self._gen_expr_coerced(a))
        t = self.new_temp()
        self.emit("CALL", sig.name, len(self.st.node_call_args[id(node)]), t)
        return t

    def _gen_interp_string(self, node):
        # each part (literal text or embedded expression) converts to a string via TOSTR,
        # then folds left-to-right with CONCAT - the language's only string-concat mechanism
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
        if any(e.severity == "ERROR" for e in sem_errors):
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
