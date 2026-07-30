"""Intermediate code generator: lowers a type-checked AST (see semantics.py)
into a flat list of quadruples the VM in interpreter.py executes directly.

Phase handoff: receives the AST plus the `SymbolTable` produced by
`semantics.py`'s `analyze()` - every name reference, inferred type, and
required coercion was already resolved there (see that module's "side-table
pattern"), so this module never re-resolves a name or re-checks a type; it
only walks the AST once and emits quads. Produces `(quads, functions,
var_types, structs)`, which `interpreter.py`'s `IRExecutor` then runs
directly.

Worked micro-example: the statement `x = a + 1;` (with `a` an `int` local of
function `f`) lowers to two quads:

    (ADD, f.a, 1, _t1)
    (ASSIGN, _t1, -, f.x)

- one temp holds the addition's result, then an ASSIGN copies it into `x`'s
  slot. `f.a`/`f.x` are the unique ir_names semantics.py assigned (see its
  `_fresh_ir_name`); `1` is wrapped in a `Const` (see below) so the VM can
  tell it apart from a variable name; `_t1` is a temp this module invents.

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
FIELD_LOAD/FIELD_STORE. Watch this convention at each individual emit site
below - arg1/arg2 swap meaning between the LOAD and STORE forms of the same
opcode family, which is easy to get backwards when reading in isolation.

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
    """One quadruple: an opcode plus up to two source operands and a
    result/destination. `line` is the source line of the *statement* that
    produced this quad (not necessarily the exact sub-expression - see
    `gen_stmt`'s `self._current_line` assignment), used to point runtime
    errors at a source line."""

    op: str
    arg1: Any = None
    arg2: Any = None
    result: Any = None
    line: int = None  # source line of the statement that produced this quad

    def __str__(self):
        return f"({self.op}, {_fmt(self.arg1)}, {_fmt(self.arg2)}, {_fmt(self.result)})"


def _fmt(operand):
    """Render one quad operand for display: unwrap a `Const`'s value
    (rendering bools as the language's own `true`/`false` spelling rather
    than Python's `True`/`False`), join a tuple of names with commas (used
    by e.g. `INPUT`'s multi-target result), or just `str()` a plain name -
    `None` prints as `-` to keep the quad table readable."""
    if operand is None:
        return "-"
    if isinstance(operand, Const):
        v = operand.value
        return ("true" if v else "false") if isinstance(v, bool) else str(v)
    if isinstance(operand, tuple):
        return "(" + ", ".join(str(x) for x in operand) + ")"
    return str(operand)


def format_quads(quads):
    """Render the full quad list as a numbered listing (used by both
    `ir.py --output` and `interpreter.py --ir`), zero-padding the index
    column to the width of the largest index so the table stays aligned."""
    width = max(1, len(str(len(quads) - 1)))
    return "\n".join(f"{i:0{width}d}: {q}" for i, q in enumerate(quads))


class IRGenerator:
    """Walks a type-checked `Program` AST once and emits quads into
    `self.quads`. One instance is used for exactly one `generate()` call -
    all the counters/tables below are per-program state, not reusable
    across multiple programs."""

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
        """Append one quad (stamped with the statement line currently being
        generated) and return its index - the index is occasionally useful
        to callers that need to patch something later, though this
        generator's control-flow constructs are built entirely with named
        labels rather than backpatched offsets (see `new_label`)."""
        self.quads.append(Quad(op, arg1, arg2, result, self._current_line))
        return len(self.quads) - 1

    def new_temp(self):
        """Allocate a fresh, program-wide-unique temporary name (`_t1`,
        `_t2`, ...) - temps never need semantics.py's shadowing scheme since
        this counter itself never repeats a number."""
        self._temp_count += 1
        return f"_t{self._temp_count}"

    def new_label(self):
        """Allocate a fresh, program-wide-unique label name (`L1`, `L2`,
        ...) for a jump target. Labels are resolved to quad indices once,
        by `interpreter.py`'s `IRExecutor.__init__`, so `JMP`/`JZ`/`JNZ`
        here just reference labels by name and never need to know their
        eventual quad index."""
        self._label_count += 1
        return f"L{self._label_count}"

    @staticmethod
    def _type_tag(type_):
        """Reduce a semantics.py type descriptor to the short string tag
        the VM actually cares about at runtime (`"int"`, `"array"`,
        `"struct"`, ...) - the VM has no use for a full type descriptor
        (e.g. an array's element type or a struct's field layout by name),
        only enough to pick a default value or a coercion target."""
        return type_[1] if type_[0] == "prim" else type_[0]

    @staticmethod
    def _default_const(type_):
        """The zero-value a declared-but-uninitialized variable gets:
        `DEFAULTS[tag]` for a primitive type, or `Const(None)` for anything
        else (arrays/structs are never default-constructed this way - they
        go through `_gen_array_declare`/`_gen_struct_declare` instead, which
        build a real empty array/struct value)."""
        if type_[0] == "prim":
            return Const(DEFAULTS.get(type_[1]))
        return Const(None)

    # -- top level -----------------------------------------------------

    def generate(self, program):
        """Entry point: emit every global `const`, then every function body,
        then the program's actual entry sequence.

        The `JMP start_label` immediately after globals exists so that all
        function bodies can be emitted *before* the `CALL main` that
        actually begins execution - quads are a flat list with no separate
        "code section", so without this jump, execution would fall straight
        into the first function's body instead of skipping over every
        function definition to reach the real entry point.
        """
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
        """Emit one `ASSIGN` per global declarator - globals are never
        arrays/structs requiring `ARR_NEW`/`STRUCT_NEW` here because
        `parser.py`'s `validate_program_structure` (via the grammar's
        `global_var_decl`) only allows `const` at global scope, and `const`
        initializers are always literals (see grammar.py)."""
        for declarator in decl.fields["declarators"]:
            self._current_line = declarator.line
            sym = self.st.node_symbol[id(declarator)]
            self.var_types[sym.ir_name] = self._type_tag(sym.type)
            value = self._gen_expr_coerced(declarator.fields["initializer"])
            self.emit("ASSIGN", value, None, sym.ir_name)

    def gen_function(self, fn):
        """Emit one function's quads: a `FUNC_BEGIN` marker (skipped over
        at runtime, see `interpreter.py`'s dispatch - it exists purely as a
        readable boundary marker in the quad listing), the body, an
        unconditional trailing `RETURN` to cover falling off the end
        without an explicit `return` (see LIMITATIONS.md's note on this
        producing Python's `None` for non-void functions), and a matching
        `FUNC_END` marker. `self.functions[name]["entry"]` records the quad
        index *after* `FUNC_BEGIN` - where `CALL` should actually jump to.
        """
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
        """Emit a block's declarations, then its statements, in that
        order - mirrors semantics.py's `_analyze_block`, and relies on the
        same grammar guarantee that declarations always precede statements
        within one block."""
        for decl in block.fields["declarations"]:
            self.gen_local_decl(decl)
        for stmt in block.fields["statements"]:
            self.gen_stmt(stmt)

    def gen_local_decl(self, decl):
        """Dispatch a local declaration to its specific generator."""
        if decl.kind == "VarDecl":
            self.gen_var_decl(decl)
        elif decl.kind == "LetDecl":
            self.gen_let_decl(decl)
        else:
            raise IRGenError(f"Declaration not supported: {decl.kind}")

    def gen_var_decl(self, decl):
        """Emit each declarator in a `val`/`var` declaration via
        `_gen_declare`, which branches further by the declarator's actual
        type (scalar/array/struct)."""
        for declarator in decl.fields["declarators"]:
            self._current_line = declarator.line
            sym = self.st.node_symbol[id(declarator)]
            self.var_types[sym.ir_name] = self._type_tag(sym.type)
            self._gen_declare(sym, declarator.fields["initializer"])

    def _gen_declare(self, sym, init):
        """Branch a single declarator's initialization by its type: arrays
        and structs need their own multi-quad construction sequences
        (`ARR_NEW`/`STRUCT_NEW` plus per-element/per-field stores); a plain
        scalar is just one `ASSIGN` of either its (possibly coerced)
        initializer or a type-appropriate default value."""
        type_ = sym.type
        if type_[0] == "array":
            self._gen_array_declare(sym, type_, init)
        elif type_[0] == "struct":
            self._gen_struct_declare(sym, type_, init)
        else:
            value = self._gen_expr_coerced(init) if init is not None else self._default_const(type_)
            self.emit("ASSIGN", value, None, sym.ir_name)

    def _gen_array_declare(self, sym, type_, init):
        """Emit an array declaration. Two shapes:

          - `{v1, v2, ...}` initializer list: `ARR_NEW` sized to the list's
            own length (ignoring any declared size - a literal-size
            mismatch was already flagged by semantics.py), then one
            `ARR_STORE` per element. Note the ARR_STORE operand order here:
            `(index, value) -> array` (see the module docstring's LOAD/
            STORE convention).
          - no initializer list: an existing array-typed expression is
            just `ASSIGN`ed directly (aliasing the same underlying Python
            list - see interpreter.py's module docstring on arrays being
            reference values), or, with no initializer at all, `ARR_NEW`
            zero-fills to the declared literal size (raising `IRGenError`
            if that size isn't known at compile time - see
            LIMITATIONS.md's "dynamically-sized arrays without an
            initializer aren't supported").
        """
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
        """Emit a struct declaration: `STRUCT_NEW` always runs first
        (default-initializing every field per `interpreter.py`'s
        `STRUCT_NEW` handler), then either a `{v1, v2, ...}` initializer
        list overwrites fields positionally via `FIELD_STORE` (operand
        order `(field_name, value) -> struct`, per the LOAD/STORE
        convention), or an existing struct-typed expression is `ASSIGN`ed
        wholesale (again aliasing the same underlying value)."""
        struct_name = type_[1]
        self.emit("STRUCT_NEW", struct_name, None, sym.ir_name)
        if init is not None and init.kind == "InitializerList":
            info = self.st.structs[struct_name]
            for fname, v in zip(info.field_order, init.fields["values"]):
                self.emit("FIELD_STORE", Const(fname), self._gen_expr_coerced(v), sym.ir_name)
        elif init is not None:
            self.emit("ASSIGN", self._gen_expr(init), None, sym.ir_name)

    def gen_let_decl(self, decl):
        """`let` has the same two shapes ir.py's sibling analysis in
        semantics.py handles: a single-name array form (delegates straight
        to `_gen_array_declare`, reusing the exact same array-construction
        logic `val`/`var` arrays use) or a destructuring form (delegates to
        `_gen_destructure`, shared with `MultiAssign`)."""
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
        single multi-return call, or zip several values positionally.

        Two shapes, matching semantics.py's own destructuring logic:
          - one value feeding several names: emit that one call, then a
            single `UNPACK` quad whose `result` is the whole tuple of
            target ir_names at once (`(value, -, (name1, name2, ...))`) -
            `interpreter.py`'s `_unpack` handler fans it out to each name
            in one step.
          - otherwise (equal counts): one plain `ASSIGN` per name/value
            pair, each individually coerced if semantics.py flagged one.
        """
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
        """Dispatch one statement to its generator, first stamping
        `self._current_line` from the statement node - every quad emitted
        while generating this statement (including nested sub-expression
        quads) is line-tagged with *this* statement's line, not each
        sub-expression's own line (see LIMITATIONS.md's note on runtime
        error line numbers being per-statement, not per-sub-expression)."""
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
        """Emits, for `if (cond) { then } else { else }`:

            <cond>
            JZ   cond   -> Lelse
            <then>
            JMP         -> Lend
            LABEL Lelse
            <else>              (only if an else branch exists)
            LABEL Lend

        A chained `else if` is just `gen_stmt` recursing on the nested
        `IfStmt` in place of a block (see `_if_stmt_fn` in grammar.py),
        which naturally nests another whole `if`-shaped quad sequence
        inside the `Lelse` slot.
        """
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
        """Emits, for `while (cond) { body }`:

            LABEL Ltop
            <cond>
            JZ   cond -> Lend
            <body>
            JMP       -> Ltop
            LABEL Lend

        `break`/`continue` inside `body` target `(Ltop, Lend)` via
        `self._loop_stack` - for a `while` loop, `continue` and the re-test
        point are the same label (unlike `for`, which needs a separate
        label for its update clause - see `gen_for`).
        """
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
        """Emits, for `for (init; cond; update) { body }` (any of the three
        clauses may be absent):

            <init>
            LABEL Lcond
            <cond>
            JZ   cond -> Lend
            <body>
            LABEL Lcont
            <update>
            JMP       -> Lcond
            LABEL Lend

        `continue` jumps to `Lcont` (so the update clause still runs before
        re-testing the condition), while `break` jumps straight to `Lend` -
        this is why `for` needs a separate continue-label from its
        condition-label, unlike `while`.
        """
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
        """`for (x in iterable) { body }` has two entirely different
        lowerings depending on what `iterable` is - both share the same
        `(cond_label, cont_label, end_label)` loop shape as `gen_for`, but
        drive it with a different implicit counter:

          - `RangeExpr` (`a..b`, inclusive): emits a hidden upper-bound temp
            once, assigns the loop variable to the start value, and loops
            `while loop_var <= end_temp`, incrementing the loop variable
            itself directly by 1 each iteration:

                <end_temp> = <end>
                <loop_var> = <start>
                LABEL Lcond
                LE loop_var, end_temp -> cmp
                JZ  cmp -> Lend
                <body>
                LABEL Lcont
                ADD loop_var, 1 -> loop_var
                JMP -> Lcond
                LABEL Lend

          - any array expression: emits a hidden index counter starting at
            0 and a length temp (via `ARR_LEN`, computed once up front -
            NOT re-read every iteration, so mutating the array's length
            mid-loop, if that were possible, wouldn't be observed), loops
            `while idx < len`, and loads `arr[idx]` into the loop variable
            at the top of each iteration body:

                <idx> = 0
                ARR_LEN <arr> -> len
                LABEL Lcond
                LT idx, len -> cmp
                JZ  cmp -> Lend
                ARR_LOAD arr, idx -> loop_var
                <body>
                LABEL Lcont
                ADD idx, 1 -> idx
                JMP -> Lcond
                LABEL Lend
        """
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
        """Emits, for `repeat { body } until (cond);` - a post-test loop,
        so the body always runs at least once before the condition is ever
        checked:

            LABEL Ltop
            <body>
            LABEL Lcont
            <cond>
            JZ   cond -> Ltop     (loop again while cond is FALSE)
            LABEL Lend

        Note the inverted sense versus `while`: here `JZ` (jump when false)
        loops *back* to the top, since "until" means "repeat while the
        condition has not yet become true".
        """
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
        """`break`/`continue` both compile to a single unconditional `JMP`
        to whichever label the innermost enclosing loop pushed onto
        `self._loop_stack` (its end-label for `break`, its continue-label
        for `continue`) - semantics.py already guaranteed we're inside a
        loop, so `self._loop_stack` is never empty here in a program that
        passed semantic analysis; the `IRGenError` guard exists only for
        defense-in-depth."""
        if not self._loop_stack:
            raise IRGenError(f"'{node.fields['keyword']}' used outside of a loop")
        cont_label, end_label = self._loop_stack[-1]
        target = end_label if node.fields["keyword"] == "break" else cont_label
        self.emit("JMP", result=target)

    def gen_return(self, node):
        """Three shapes: bare `return;` (a value-less `RETURN`), one value
        (`RETURN <value>`), or several values (multi-return: build a fresh
        tuple via `TUPLE_NEW`, `TUPLE_APPEND` each value onto it in order,
        then `RETURN` the whole tuple - `interpreter.py`'s `_do_return`
        hands that tuple back to whatever `let`/`MultiAssign` on the
        caller's side is waiting to `UNPACK` it, or just discards it if the
        caller ignores the result)."""
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
        """`print(a, b, ...)` emits one `PRINT` per argument, with a
        `PRINT_SEP` (a single space) between them but not before the first
        or after the last, then one trailing `PRINTLN` for the newline -
        matching how the sample programs' expected output is formatted.
        `input(a, b, ...)` instead emits a single `INPUT` quad whose
        `result` is the whole tuple of target ir_names at once;
        `interpreter.py`'s `_do_input` reads one whitespace-separated token
        per name, in order."""
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
        """Emits, for `guard (cond) else { else_body };` - the inverse of
        `if`: control falls straight through when `cond` is true, and only
        enters `else_body` when it's false:

            <cond>
            JZ   cond -> Lelse
            JMP        -> Lend
            LABEL Lelse
            <else_body>
            LABEL Lend
        """
        cond = self._gen_expr_coerced(node.fields["condition"])
        else_label, end_label = self.new_label(), self.new_label()
        self.emit("JZ", cond, None, else_label)
        self.emit("JMP", result=end_label)
        self.emit("LABEL", result=else_label)
        self.gen_block(node.fields["else_body"])
        self.emit("LABEL", result=end_label)

    def gen_try(self, node):
        """The highest-value lowering in this file to understand: the VM
        has **no runtime notion of "finally" at all** - see
        `interpreter.py`'s module docstring. Instead, this function emits
        the finally body's quads **twice**: once inline after a normal
        catch completes, and once more right before a `RETHROW`, so that a
        `throw` from *inside* the catch block still runs `finally` before
        propagating further.

        Label layout emitted, for `try { body } catch (e) { catch_body } finally { fin }`:

            TRY_PUSH -> Lcatch
            <body>
            TRY_POP
            JMP -> Lfin_normal

            LABEL Lcatch
            CATCH_STORE -> e           # current_exception -> e
            TRY_PUSH -> Lfin_rethrow   # a throw during catch_body still runs `finally`
            <catch_body>
            TRY_POP
            JMP -> Lfin_normal

            LABEL Lfin_rethrow
            <fin>                      # copy #1: runs before propagating the exception further
            RETHROW

            LABEL Lfin_normal
            <fin>                      # copy #2: runs after either a clean body or a handled catch

        Catch clauses carry no exception type, so `ir.py` only lowers
        `catch_clauses[0]` - later clauses were still type-checked by
        semantics.py, but are permanently dead code (see LIMITATIONS.md).
        """
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
        """`int a, b = f();` reuses the exact same unpack-or-zip logic
        `let`'s destructuring form uses, via the shared `_gen_destructure`
        helper - the only difference between the two at this stage is
        which side table (`node_symbol` keyed by the `LetDecl` vs. the
        `MultiAssign` node) supplies the target symbols."""
        symbols = self.st.node_symbol[id(node)]
        self._gen_destructure(symbols, node.fields["values"])

    # -- match (statement and expression) -----------------------------------

    def gen_match(self, node, is_expr):
        """Both `match` forms compile to an if-else chain: evaluate the
        subject once, then for each case, test the pattern (via
        `_gen_match_test`, falling through on a match or jumping to that
        case's own `next_label` otherwise), run the arm's body/value, then
        jump straight to `end_label` - so at most one arm's code ever
        executes. For the expression form only, `result_temp` collects
        whichever arm's value ran; for the statement form there's no
        result to collect, only side effects.

        Emitted shape (expression form; statement form is identical minus
        the `ASSIGN ... -> result_temp` line):

            <subject>
            <pattern test #1, falls through or jumps to L1>
            ASSIGN <arm #1 value> -> result_temp
            JMP -> Lend
            LABEL L1
            <pattern test #2, falls through or jumps to L2>
            ...
            LABEL Lend
        """
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
        jumps to `fail_label` if it doesn't.

        Dispatches by pattern *shape*, mirroring semantics.py's
        `_analyze_pattern`: wildcard always falls through (no code at
        all); a `RangeExpr` becomes an inclusive `GE`-then-`LE` pair against
        the range's own start/end (either check failing jumps away); a
        "predicate" pattern (see `_is_predicate_pattern`) is evaluated
        directly as its own boolean, *ignoring* `subject` entirely; anything
        else falls back to a plain `EQ` against `subject`.
        """
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
        """Same shape heuristic as semantics.py's `_analyze_pattern`: a
        pattern is treated as a standalone predicate (evaluated on its own,
        never compared to the subject) whenever its outermost node is a
        relational/logical `BinaryExpr` or a `!` `UnaryExpr`. This must stay
        in exact lockstep with semantics.py's version of the same check -
        if they ever disagreed, a pattern could be type-checked as one kind
        and lowered as the other. See LIMITATIONS.md for what this
        AST-shape heuristic can't express."""
        if pattern.kind == "BinaryExpr" and pattern.fields.get("op") in _PREDICATE_BINOPS:
            return True
        return pattern.kind == "UnaryExpr" and pattern.fields.get("op") == "!"

    # -- expressions ---------------------------------------------------------

    def _gen_expr_coerced(self, node):
        """Generate `node`'s value, then - if semantics.py recorded a
        `node_coerce` entry for it - wrap the result in a `CAST` to the
        target type tag before returning it. This is the single consumer
        of `semantics.py`'s `node_coerce` side table: every implicit
        numeric widening (char->int, char->float, int->float) decided
        during semantic analysis becomes exactly one `CAST` quad here, at
        the point of use, rather than being re-derived."""
        value = self._gen_expr(node)
        target = self.st.node_coerce.get(id(node))
        if target is None:
            return value
        t = self.new_temp()
        self.emit("CAST", value, self._type_tag(target), t)
        return t

    def _gen_expr(self, node):
        """Generate one expression node's value (never applying a
        coercion - see `_gen_expr_coerced` for that), dispatching by
        `kind`. Returns whatever operand (a `Const`, a variable/temp name)
        holds the resulting value."""
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
            # ARR_LOAD reads (array, index) -> result, per the module
            # docstring's LOAD/STORE convention.
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
            # only meaningful as a for-in iterable or a match pattern, both
            # of which special-case it before ever calling _gen_expr on it.
            raise IRGenError("RangeExpr cannot be evaluated as a plain expression")

        raise IRGenError(f"Expression not supported: {kind}")

    def _gen_binary(self, node):
        """`&&`/`||` are diverted to `_gen_short_circuit` (they lower to
        jumps, not a plain opcode - see that method); every other binary
        operator generates both operands unconditionally, then one opcode
        from `BINARY_OPCODE` mapping the source operator to its quad
        mnemonic (e.g. `+` -> `ADD`)."""
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
        """Lower `&&`/`||` to a conditional jump so the right operand is
        only evaluated when it can still change the answer.

        There is deliberately no AND/OR opcode: a quad's operands are
        already-computed values, so any single opcode would force both
        sides to be evaluated. Skipping the right operand requires a
        real jump.

        Emits, for `&&`:
            ASSIGN left  -> r
            JZ     r     -> Lskip
            ASSIGN right -> r
            LABEL  Lskip
        (`||` is identical but jumps on JNZ.)
        """
        result = self.new_temp()
        self.emit("ASSIGN", self._gen_expr(node.fields["left"]), None, result)
        skip_label = self.new_label()
        # JZ  for && : left false -> answer already false, skip right
        # JNZ for || : left true  -> answer already true,  skip right
        self.emit("JZ" if is_and else "JNZ", result, None, skip_label)
        self.emit("ASSIGN", self._gen_expr(node.fields["right"]), None, result)
        self.emit("LABEL", result=skip_label)
        return result   # this temp holds the whole expression's value

    def _gen_assign(self, node):
        """Generate an `AssignExpr`. The target determines the shape:
        a plain identifier gets a direct `ASSIGN`; an `IndexExpr` target
        emits `ARR_STORE` with operand order `(index, value) -> array`
        (note this is the STORE order, the reverse of ARR_LOAD's `(array,
        index) -> result` - see the module docstring); a `MemberExpr`
        target emits `FIELD_STORE` with order `(field_name, value) ->
        struct`, same STORE convention. The assignment's own value (the
        expression's result, as an assignment expression can itself be
        used as a value) is returned in every case."""
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
        """Emit one `PARAM` quad per argument (in the positionally-ordered
        list semantics.py's `node_call_args` already resolved - see that
        module's `_infer_call` for how named arguments get reordered into
        this same positional list), each individually coerced, followed by
        one `CALL` naming the function, its argument count, and a fresh
        temp to receive the return value. `interpreter.py`'s `_call` pops
        exactly `argcount` values off the pending-args list built up by
        those `PARAM` quads."""
        sig = self.st.node_call[id(node)]
        for a in self.st.node_call_args[id(node)]:
            self.emit("PARAM", self._gen_expr_coerced(a))
        t = self.new_temp()
        self.emit("CALL", sig.name, len(self.st.node_call_args[id(node)]), t)
        return t

    def _gen_interp_string(self, node):
        """An interpolated string's parts (alternating literal-text
        `Literal`s and embedded expression Nodes - see grammar.py's
        `_build_interp_string_node`) are each converted to a string via
        `TOSTR`, then folded together left-to-right with `CONCAT` - this is
        the language's *only* string-concatenation mechanism (see
        CLAUDE.md: `+` is numeric-only)."""
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
    """Module entry point: run a fresh `IRGenerator` over `program` and
    return `(quads, functions, var_types, structs)`."""
    return IRGenerator(symtab).generate(program)


def main():
    """CLI entry point: `python ir.py <source_file> [-o out] [--symbols]`.
    Runs the full scan/parse/analyze pipeline first (any error there aborts
    with exit `2`, printed to stderr, before this module is ever reached),
    then generates and prints/writes the quad listing - and, with
    `--symbols`, the function/struct/variable metadata tables alongside
    it."""
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
