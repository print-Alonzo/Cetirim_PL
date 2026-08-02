"""Semantic analysis: name resolution, static type checking, and unique
IR-name assignment. Sits between the parser and the IR generator.

    analyze(program) -> (SymbolTable, [SemanticError])

Phase handoff: receives the AST (`ast_nodes.Node` tree) built by
`grammar.py`/`parser.py`. Produces a `SymbolTable` whose *side tables* (see
"The side-table pattern" below) `ir.py` reads from directly - `ir.py` never
re-resolves a name, re-infers a type, or re-checks a coercion; every fact it
needs was already computed here, once, and looked up by `id(node)`.

Two passes over the AST:
  Pass 1 (globals) - collects struct/typedef definitions (resolving alias
    chains with cycle detection), function signatures, and global `const`
    declarations. Runs first, over *all* declarations, precisely so that
    Pass 2 can freely resolve a forward reference - a function may call
    another function declared later in the file, or use a struct/typedef
    declared later - without needing multiple passes over function bodies.
  Pass 2 (bodies)  - walks each function with a scope stack, resolving
    every name, checking every operator/call/assignment/control-flow
    construct, and recording the result on side tables keyed by id(node) so
    the IR generator (ir.py) never has to re-resolve anything.

The side-table pattern: rather than mutating the AST (adding a `.type`
attribute to nodes, say), every fact this pass learns about a node is
stored in a dict on `SymbolTable` keyed by `id(node)` - the node's Python
object identity. This keeps `ast_nodes.Node` itself completely generic (see
its module docstring) and keeps "the AST" and "what we learned about the
AST" as two separate, independently-inspectable things. The hazard: an
`id(node)` key is only meaningful while that exact Node object is still
alive and hasn't been replaced by an equal-looking new one - these tables
are never meant to outlive the single `Program` tree they were built for.

  node_symbol    - the resolved Symbol (or list of Symbols, for a LetDecl/
                   MultiAssign that binds several names at once)
  node_type      - the inferred type of an expression node
  node_coerce    - "this expression's value needs an implicit CAST to this
                   type before use" - the consumer is ir.py's
                   `_gen_expr_coerced`
  node_call      - the resolved FunctionSig for a CallExpr
  node_call_args - a CallExpr's arguments, positionally reordered (named
                   arguments get sorted back into parameter order here)

Design notes (see also LIMITATIONS.md for the sharper edges of these):
  - `val`/`const` freeze the *binding*, not an array/struct's contents:
    only reassigning the name is rejected, element/field mutation is not.
  - Implicit numeric coercion: char -> int -> float. Everything else must
    match exactly. A required coercion is recorded in `node_coerce` so the
    IR generator can wrap that node's quad with CAST.
  - Every local symbol gets a globally-unique ir_name ("main.i", "main.i$2"
    on shadowing) so the VM can never confuse two same-named locals or a
    local with a same-named global. This is the single most important
    cross-phase fact in the codebase: it's what lets interpreter.py's
    `resolve`/`set_var` use one flat per-frame dict instead of a real
    nested-scope lookup - see `_fresh_ir_name` below.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ast_nodes import Node


# ---------------------------------------------------------------------------
# Type descriptors - small tuples so they're hashable/comparable by value.
#   ("prim", "int" | "float" | "char" | "string" | "bool" | "void")
#   ("struct", struct_name)
#   ("array", element_type, size_or_None)
#   ("tuple", (type, type, ...))
#   ("range", T_INT)      internal-only: the value of a bare `a..b`
#   ("error",)            internal-only: suppresses cascading diagnostics
# ---------------------------------------------------------------------------

T_INT = ("prim", "int")
T_FLOAT = ("prim", "float")
T_CHAR = ("prim", "char")
T_STRING = ("prim", "string")
T_BOOL = ("prim", "bool")
T_VOID = ("prim", "void")
T_ERROR = ("error",)

PRIMITIVE_TYPES = {"int", "float", "char", "string", "bool", "void"}
# Numeric coercion ladder: char(0) -> int(1) -> float(2). A value may only
# ever be implicitly widened to a *higher* rank (see can_coerce) - there is
# no int->char or float->int implicit narrowing, and string/bool never
# appear here at all.
_NUMERIC_RANK = {T_CHAR: 0, T_INT: 1, T_FLOAT: 2}


def t_struct(name):
    """Build a `("struct", name)` type descriptor."""
    return ("struct", name)


def t_array(elem, size=None):
    """Build an `("array", element_type, size)` type descriptor. `size` is
    `None` when the array's length isn't known at compile time (e.g. no
    literal size and no initializer list to count)."""
    return ("array", elem, size)


def t_tuple(elems):
    """Build a `("tuple", (type, ...))` type descriptor for a multi-return
    function's return type."""
    return ("tuple", tuple(elems))


def is_error(t):
    """True for the internal `T_ERROR` sentinel - a type that couldn't be
    determined because an earlier error already occurred. Every type-lattice
    function below treats `T_ERROR` as compatible with anything, so a single
    root-cause error doesn't cascade into a wall of follow-on diagnostics
    for every later use of the same malformed expression."""
    return t == T_ERROR


def type_name(t):
    """Render a type descriptor as the string used in diagnostic messages
    (e.g. `"int[]"`, `"struct Point"`, `"(int, bool)"`)."""
    if t[0] == "prim":
        return t[1]
    if t[0] == "struct":
        return f"struct {t[1]}"
    if t[0] == "array":
        return f"{type_name(t[1])}[]"
    if t[0] == "tuple":
        return "(" + ", ".join(type_name(x) for x in t[1]) + ")"
    if t[0] == "range":
        return "range"
    return "?"


def types_equal(a, b):
    """Structural equality between two type descriptors. An array's *size*
    is deliberately ignored (`int[5]` and `int[10]` are the same type for
    compatibility purposes - only the element type matters), which is why
    this can't just be Python `==` on the tuples."""
    if is_error(a) or is_error(b):
        return True
    if a[0] != b[0]:
        return False
    if a[0] == "array":
        return types_equal(a[1], b[1])  # size is ignored for compatibility
    if a[0] == "tuple":
        return len(a[1]) == len(b[1]) and all(types_equal(x, y) for x, y in zip(a[1], b[1]))
    return a == b


def is_numeric(t):
    """True for char/int/float - the three types the coercion ladder and
    arithmetic operators apply to."""
    return t in _NUMERIC_RANK


def can_coerce(src, dst):
    """True if `src` may be implicitly converted to `dst` (assignment, arg
    passing, return, initialization)."""
    if is_error(src) or is_error(dst):
        return True
    if types_equal(src, dst):
        return True
    if is_numeric(src) and is_numeric(dst):
        # Only upward along the ladder: char->int, char->float, int->float.
        # int->char and float->int are NOT allowed here even though they're
        # "numeric to numeric" - see _NUMERIC_RANK.
        return _NUMERIC_RANK[src] <= _NUMERIC_RANK[dst]
    return False


def arithmetic_result(a, b):
    """Result type of + - * / % between two numeric types, or None if invalid."""
    if not (is_numeric(a) and is_numeric(b)):
        return None
    if a == T_FLOAT or b == T_FLOAT:
        return T_FLOAT
    return T_INT  # int op int, or char (promoted) op char/int


def unify(types):
    """Common type across a list of types (for MatchExpr arms). None on
    genuine mismatch; T_ERROR if every candidate was already an error."""
    types = [t for t in types if not is_error(t)]
    if not types:
        return T_ERROR
    result = types[0]
    for t in types[1:]:
        if types_equal(result, t):
            continue
        if is_numeric(result) and is_numeric(t):
            # Two different numeric arms unify to whichever is "wider" -
            # float wins over int/char, otherwise int (char is never the
            # final unified type since char op non-char always promotes).
            result = T_FLOAT if T_FLOAT in (result, t) else T_INT
            continue
        return None
    return result


# ---------------------------------------------------------------------------
# Symbol table
# ---------------------------------------------------------------------------

@dataclass
class Symbol:
    """One resolved name: its declared type, whether it can be reassigned
    (`val`/`const` -> False), its globally-unique IR name (see
    `_fresh_ir_name`), and whether it lives in `interpreter.py`'s
    `self.globals` or the current call's `self.frame`."""

    name: str
    type: tuple
    mutable: bool
    ir_name: str
    storage: str  # "global" | "local"


@dataclass
class StructInfo:
    """A resolved struct's field types, in declaration order (`field_order`
    is what lets `ir.py`'s STRUCT_NEW default-initialize fields in a
    deterministic order, and what a positional `{v1, v2, ...}` initializer
    list is matched against)."""

    name: str
    fields: Dict[str, tuple] = field(default_factory=dict)
    field_order: List[str] = field(default_factory=list)


@dataclass
class FunctionSig:
    """A resolved function signature - everything a call site needs to
    type-check against, plus the original `FunctionDecl` Node (`node`) so
    `ir.py` can find its body again when it's time to lower it.
    `param_defaults` is parallel to `param_names`/`param_types`: `None` for
    a required parameter, or the (already type-checked, already-`Literal`)
    default-value Node for one that may be omitted - the same Node is
    reused at every call site that omits it, since its coerced type never
    varies by call site (see `_infer_call`)."""

    name: str
    param_names: List[str]
    param_types: List[tuple]
    return_type: tuple  # T_VOID, a single type, or a ("tuple", ...) type
    node: Node
    param_defaults: List[Optional[Node]] = field(default_factory=list)


@dataclass
class SemanticError:
    """One recorded semantic diagnostic: a message plus the AST position it
    applies to (`line`/`col` are `None` only for whole-program errors with
    no single node to blame, e.g. "no main function"). `severity` is either
    `"ERROR"` (aborts compilation) or `"WARNING"` (reported but the pipeline
    still proceeds) - see `Analyzer.error`/`Analyzer.warn`."""

    message: str
    line: Optional[int]
    col: Optional[int]
    severity: str = "ERROR"

    def __str__(self):
        pos = f"Line {self.line}, Col {self.col}: " if self.line is not None else ""
        tag = "SEMANTIC ERROR" if self.severity == "ERROR" else "SEMANTIC WARNING"
        return f"[{tag}] {pos}{self.message}"


class SymbolTable:
    """Everything Pass 1/Pass 2 produce: the resolved global tables
    (structs, typedefs, functions, global symbols) plus the side tables
    (see the module docstring's "side-table pattern" section) that let
    `ir.py` read every per-node fact this analysis computed, keyed by
    `id(node)`, without re-deriving any of it."""

    def __init__(self):
        self.structs: Dict[str, StructInfo] = {}
        self.typedefs: Dict[str, tuple] = {}
        self.functions: Dict[str, FunctionSig] = {}
        self.globals: Dict[str, Symbol] = {}
        # side tables, keyed by id(node) - populated during Pass 2 so ir.py
        # never has to re-resolve names, types, or coercions.
        self.node_symbol: Dict[int, Any] = {}   # Symbol, or List[Symbol] for LetDecl/MultiAssign
        self.node_type: Dict[int, tuple] = {}
        self.node_coerce: Dict[int, tuple] = {}  # expr node -> target type to CAST to
        self.node_call: Dict[int, FunctionSig] = {}
        self.node_call_args: Dict[int, List[Node]] = {}  # CallExpr -> positionally-ordered args
        self.node_is_predicate: Dict[int, bool] = {}  # match pattern node -> predicate vs. equality-compared


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------

class Analyzer:
    """Drives the two passes described in the module docstring over one
    `Program` node, accumulating results into `self.st` (a `SymbolTable`)
    and diagnostics into `self.errors`."""

    def __init__(self):
        self.st = SymbolTable()
        self.errors: List[SemanticError] = []
        # Pass-1 scratch state, discarded once globals are fully resolved:
        self._raw_structs: Dict[str, Tuple[list, Node]] = {}   # name -> (field Nodes, decl Node)
        self._typedef_nodes: Dict[str, Node] = {}              # typedef name -> its aliased-type Node
        self._typedef_resolving: set = set()                   # cycle-detection guard, see _resolve_typedef
        # Pass-2 scratch state, reset per-function in _analyze_function:
        self._name_counters: Dict[str, int] = {}
        self._scopes: List[Dict[str, Symbol]] = []
        self._loop_depth = 0
        self._current_function: Optional[FunctionSig] = None

    def error(self, node, message):
        """Record a diagnostic at `node`'s position (or with no position,
        for whole-program errors where `node` is `None`)."""
        line = node.line if isinstance(node, Node) else None
        col = node.col if isinstance(node, Node) else None
        self.errors.append(SemanticError(message, line, col))

    def warn(self, node, message):
        """Record a non-fatal diagnostic - same shape as `error`, but the
        pipeline still proceeds to IR generation/execution as long as no
        `error`-severity diagnostic was also recorded."""
        line = node.line if isinstance(node, Node) else None
        col = node.col if isinstance(node, Node) else None
        self.errors.append(SemanticError(message, line, col, severity="WARNING"))

    def run(self, program: Node):
        """Entry point: Pass 1 over every top-level declaration, then
        Pass 2 over every function body. Returns `(self.st, self.errors)`."""
        decls = program.fields["declarations"]
        self._collect_structs_and_typedefs(decls)
        self._collect_functions_and_const_globals(decls)
        for d in decls:
            if d.kind == "FunctionDecl":
                self._analyze_function(d)
        return self.st, self.errors

    # -- Pass 1: types -----------------------------------------------------

    def _collect_structs_and_typedefs(self, decls):
        """First half of Pass 1: gather every `StructDecl` and `TypedefDecl`
        (including the inline `typedef struct Foo { ... } Bar;` form, which
        both defines struct `Foo` *and* aliases it to `Bar`) into scratch
        dicts, then resolve them all. Structs and typedefs are collected
        into flat dicts *before* any resolution happens so that a struct
        field of type `Bar` (a typedef) or a typedef aliasing a not-yet-
        declared struct both work regardless of declaration order.
        """
        for d in decls:
            if d.kind == "StructDecl":
                name = d.fields["name"]
                if name in self._raw_structs:
                    self.error(d, f"Struct '{name}' is already defined")
                    continue
                self._raw_structs[name] = (d.fields["fields"], d)
            elif d.kind == "TypedefDecl":
                aliased = d.fields["aliased_type"]
                if aliased.kind == "StructDef":
                    # Inline struct form: register the struct itself under
                    # its own name, and separately record that this
                    # typedef name aliases a StructType reference to it.
                    sname = aliased.fields["name"]
                    if sname in self._raw_structs:
                        self.error(aliased, f"Struct '{sname}' is already defined")
                    else:
                        self._raw_structs[sname] = (aliased.fields["fields"], aliased)
                    self._typedef_nodes[d.fields["name"]] = Node(
                        "StructType", {"name": sname}, line=d.line, col=d.col
                    )
                else:
                    self._typedef_nodes[d.fields["name"]] = aliased

        for name in list(self._raw_structs):
            self._resolve_struct(name)
        for name in list(self._typedef_nodes):
            self._resolve_typedef(name)
        self._check_struct_recursion()

    def _check_struct_recursion(self):
        """Reject a struct that contains itself by value - directly,
        through a chain of other structs, or through an array field's
        element type (a fixed-size array field still eagerly constructs one
        instance of its element type per slot, so an array-of-self is
        exactly as circular as a plain self-typed field). Without this,
        `ir.py`'s STRUCT_NEW/ARR_NEW recursive default-value construction
        (`interpreter.py`'s `_make_default`) would recurse forever building
        the value - this catches it as a diagnosable error instead."""

        def field_struct_refs(type_):
            while type_[0] == "array":
                type_ = type_[1]
            if type_[0] == "struct":
                yield type_[1]

        def reaches_self(start, current, visited):
            info = self.st.structs.get(current)
            if info is None:
                return False
            for fname in info.field_order:
                for ref in field_struct_refs(info.fields[fname]):
                    if ref == start:
                        return True
                    if ref not in visited:
                        visited.add(ref)
                        if reaches_self(start, ref, visited):
                            return True
            return False

        for name in self.st.structs:
            if reaches_self(name, name, {name}):
                node = self._raw_structs[name][1]
                self.error(node, f"Struct '{name}' contains itself by value (directly or indirectly)")

    def _resolve_struct(self, name):
        """Resolve one struct's field types, registering an (initially
        empty) `StructInfo` in `self.st.structs` *before* resolving any
        field - this is what lets a struct contain a field whose type
        refers back to the struct itself (directly, or via a typedef cycle
        through another struct): the lookup in `_resolve_type`'s
        `StructType` branch finds the in-progress entry instead of
        recursing into `_resolve_struct` again."""
        if name in self.st.structs:
            return self.st.structs[name]
        field_nodes = self._raw_structs[name][0]
        info = StructInfo(name=name)
        self.st.structs[name] = info  # register early to break recursive references
        for f in field_nodes:
            fname = f.fields["name"]
            if fname in info.fields:
                self.error(f, f"Duplicate field '{fname}' in struct '{name}'")
                continue
            info.fields[fname] = self._resolve_type(f.fields["type"])
            info.field_order.append(fname)
        return info

    def _resolve_typedef(self, name):
        """Resolve one typedef's alias chain, detecting cycles via
        `_typedef_resolving`: if resolving `name` requires resolving `name`
        again (a direct or indirect self-reference, e.g. `typedef A B;
        typedef B A;`), that inner call finds `name` already in
        `_typedef_resolving` and reports a cycle error instead of recursing
        forever."""
        if name in self.st.typedefs:
            return self.st.typedefs[name]
        if name in self._typedef_resolving:
            self.error(self._typedef_nodes[name], f"Circular typedef definition involving '{name}'")
            self.st.typedefs[name] = T_ERROR
            return T_ERROR
        self._typedef_resolving.add(name)
        resolved = self._resolve_type(self._typedef_nodes[name])
        self._typedef_resolving.discard(name)
        self.st.typedefs[name] = resolved
        return resolved

    def _resolve_type(self, node, _array_depth=0):
        """Turn a parser-produced type Node (`Type`, `StructType`,
        `ArrayType`, or `TupleType`) into a type descriptor tuple,
        resolving typedef aliases and nested struct/array/tuple shapes
        recursively.

        `_array_depth` counts how many `ArrayType` levels deep the current
        node is from the entry call (0 at every external call site; bumped
        by 1 only when recursing into an `ArrayType`'s own `base`). Only
        the outermost dimension (depth 0) may have a non-literal size:
        `ir.py`'s `_gen_array_declare` recovers exactly one runtime-computed
        size expression from the declarator's `ArrayType` node (the
        outermost one) and has no way to compute a second, so a non-literal
        *inner* dimension (e.g. `int grid[3][n];`) is rejected here instead
        of silently becoming a zero-length dimension at runtime."""
        if node is None:
            return T_ERROR
        if node.kind == "Type":
            name = node.fields["name"]
            if name in PRIMITIVE_TYPES:
                return ("prim", name)
            if name in self._typedef_nodes:
                return self._resolve_typedef(name)
            self.error(node, f"Unknown type '{name}'")
            return T_ERROR
        if node.kind == "StructType":
            name = node.fields["name"]
            if name in self._raw_structs and name not in self.st.structs:
                self._resolve_struct(name)
            if name not in self.st.structs:
                self.error(node, f"Unknown struct '{name}'")
                return T_ERROR
            return t_struct(name)
        if node.kind == "ArrayType":
            base = self._resolve_type(node.fields["base"], _array_depth=_array_depth + 1)
            size_node = node.fields.get("size")
            size = self._const_int_or_none(size_node)
            if size is None and size_node is not None:
                # Not a compile-time literal (e.g. `int arr[n];`) - still
                # has to be resolved and type-checked here, since this is
                # the only place anything walks a type node at all; ir.py's
                # `_gen_array_declare` later regenerates this same
                # `size_node` into a runtime-computed ARR_NEW operand and
                # needs it to have already been through `_infer_expr` (for
                # `node_symbol`/`node_type`) just like any other expression.
                size_type = self._infer_expr(size_node)
                if not (is_error(size_type) or types_equal(size_type, T_INT)):
                    self.error(size_node, f"Array size must be of type 'int', got '{type_name(size_type)}'")
                elif _array_depth > 0:
                    self.error(size_node, "Only the outermost array dimension may use a non-literal size")
            return t_array(base, size)
        if node.kind == "TupleType":
            return t_tuple([self._resolve_type(e) for e in node.fields["elements"]])
        self.error(node, f"Unrecognized type node '{node.kind}'")
        return T_ERROR

    @staticmethod
    def _const_int_or_none(node):
        """An array size is only usable at compile time (by `ir.py`'s
        ARR_NEW) when it's a bare integer literal - anything else (a
        variable, an expression) resolves to `None` here, meaning "size
        unknown until an initializer list supplies one"."""
        if node is not None and node.kind == "Literal" and node.fields.get("token_type") == "INTEGER_LIT":
            return node.fields["value"]
        return None

    def _collect_functions_and_const_globals(self, decls):
        """Second half of Pass 1: record every function's signature (so
        Pass 2 can resolve calls to functions declared anywhere in the
        file, including later), and fully resolve + type-check every global
        `const` declaration (globals have no forward-reference problem
        among themselves the way functions do, since a `const`'s
        initializer must be a literal, never a reference to another
        global - see grammar.py's `_const_declarator_fn`).
        """
        for d in decls:
            if d.kind == "FunctionDecl":
                name = d.fields["name"]
                if name in self.st.functions:
                    self.error(d, f"Function '{name}' is already defined")
                    continue
                param_names, param_types, param_defaults, seen = [], [], [], set()
                seen_default = False
                for p in d.fields["params"]:
                    pname = p.fields["name"]
                    if pname in seen:
                        self.error(p, f"Duplicate parameter '{pname}' in function '{name}'")
                    seen.add(pname)
                    param_names.append(pname)
                    ptype = self._resolve_type(p.fields["type"])
                    param_types.append(ptype)
                    default = p.fields.get("default")
                    if default is not None:
                        seen_default = True
                        dtype = self._infer_expr(default)
                        if not can_coerce(dtype, ptype):
                            self.error(default, f"Cannot use '{type_name(dtype)}' as default for parameter '{pname}' of type '{type_name(ptype)}'")
                        elif not types_equal(dtype, ptype):
                            self.st.node_coerce[id(default)] = ptype
                    elif seen_default:
                        self.error(p, f"Parameter '{pname}' without a default cannot follow a parameter with one")
                    param_defaults.append(default)
                ret_node = d.fields["return_type"]
                if ret_node.kind == "Type" and ret_node.fields["name"] == "void":
                    return_type = T_VOID
                else:
                    return_type = self._resolve_type(ret_node)
                self.st.functions[name] = FunctionSig(name, param_names, param_types, return_type, d, param_defaults)
            elif d.kind == "VarDecl":
                for decl in d.fields["declarators"]:
                    gname = decl.fields["name"]
                    if gname in self.st.globals:
                        self.error(decl, f"Global '{gname}' is already defined")
                        continue
                    gtype = self._resolve_type(decl.fields["type"])
                    # Globals keep their bare source name as ir_name (no
                    # _fresh_ir_name call) - there's exactly one of each
                    # global for the whole program, so no shadowing scheme
                    # is needed the way there is for locals.
                    sym = Symbol(gname, gtype, mutable=(d.fields["mutability"] != "const"),
                                 ir_name=gname, storage="global")
                    self.st.globals[gname] = sym
                    self.st.node_symbol[id(decl)] = sym
                    init = decl.fields["initializer"]
                    if init is not None:
                        self._check_initializer(init, gtype)

    # -- Pass 2: bodies ------------------------------------------------------

    def _fresh_ir_name(self, name):
        """Generate this local's globally-unique IR name: `"<function>.<name>"`,
        or `"<function>.<name>$2"`, `"$3"`, ... if `name` has already been
        used (by an outer scope's local of the same name, i.e. shadowing)
        earlier in this same function.

        This is the mechanism behind the module docstring's central claim:
        because every local's ir_name is unique across the *entire
        program* (not just its own scope), interpreter.py never needs a
        real nested-scope lookup at runtime - `resolve`/`set_var` can just
        ask "is this exact name currently a key in globals?" and get the
        right answer every time, for every frame, with no ambiguity.
        """
        count = self._name_counters.get(name, 0) + 1
        self._name_counters[name] = count
        base = f"{self._current_function.name}.{name}"
        return base if count == 1 else f"{base}${count}"

    def _make_local_symbol(self, name, type_, mutable, owner_node):
        """Build a fresh local `Symbol` (flagging a same-scope redeclaration
        as an error) and register it in the innermost active scope, without
        yet recording it in `node_symbol` - some callers (e.g. `LetDecl`
        with multiple names) need to build several symbols before deciding
        how to key them in the side table, hence the split from
        `_declare_local` below."""
        if name in self._scopes[-1]:
            self.error(owner_node, f"'{name}' is already declared in this scope")
        sym = Symbol(name, type_, mutable, self._fresh_ir_name(name), storage="local")
        self._scopes[-1][name] = sym
        return sym

    def _declare_local(self, name, type_, mutable, owner_node):
        """`_make_local_symbol` plus recording the result in `node_symbol`
        keyed by the declaring node itself - used whenever exactly one
        symbol maps to exactly one declarator/param/etc. node."""
        sym = self._make_local_symbol(name, type_, mutable, owner_node)
        self.st.node_symbol[id(owner_node)] = sym
        return sym

    def _lookup(self, name):
        """Resolve `name` by walking the scope stack innermost-first (so an
        inner declaration correctly shadows an outer one), falling back to
        the globals table if no local scope has it."""
        for scope in reversed(self._scopes):
            if name in scope:
                return scope[name]
        return self.st.globals.get(name)

    def _analyze_function(self, fn_node):
        """Reset all per-function Pass-2 state (name counters, scope
        stack, loop depth), declare parameters as the outermost scope's
        locals, then walk the body. `sig is None` only happens when this
        function's name was already rejected as a duplicate in Pass 1 (see
        `_collect_functions_and_const_globals`) - there's nothing further
        to check against in that case."""
        sig = self.st.functions.get(fn_node.fields["name"])
        if sig is None:
            return  # duplicate-name error already reported in Pass 1
        self._current_function = sig
        self._name_counters = {}
        self._scopes = [{}]
        self._loop_depth = 0
        for p, ptype in zip(fn_node.fields["params"], sig.param_types):
            self._declare_local(p.fields["name"], ptype, True, p)
        self._analyze_block(fn_node.fields["body"])
        if sig.return_type != T_VOID and not self._always_returns(fn_node.fields["body"]):
            self.error(fn_node, f"Function '{sig.name}' does not return a value on every code path")
        self._current_function = None
        self._scopes = []

    def _always_returns(self, node):
        """True if control cannot fall off the end of `node` without having
        already executed a `return` (or an equivalent terminator - a
        `throw`, or every branch of an exhaustive construct returning).
        Used to catch a non-void function whose body might otherwise
        silently yield Python's `None` at runtime (`ir.py` always emits an
        unconditional fallthrough `RETURN` with no value at the end of
        every function body). Dispatches directly on `node.kind`, which
        works uniformly whether `node` is a `Block` or a bare statement
        (e.g. an `else if`'s chained `IfStmt` - see
        `_analyze_block_or_stmt`) since both shapes are handled here."""
        kind = node.kind
        if kind in ("ReturnStmt", "ThrowStmt"):
            return True
        if kind == "Block":
            return any(self._always_returns(s) for s in node.fields["statements"])
        if kind == "IfStmt":
            if node.fields["else"] is None:
                return False
            return self._always_returns(node.fields["then"]) and self._always_returns(node.fields["else"])
        if kind == "MatchStmt":
            cases = node.fields["cases"]
            if not any(c.fields["pattern"].kind == "WildcardPattern" for c in cases):
                return False
            return all(self._always_returns(c.fields["body"]) for c in cases)
        if kind == "TryStmt":
            finally_body = node.fields["finally_body"]
            if finally_body is not None and self._always_returns(finally_body):
                return True
            body_returns = self._always_returns(node.fields["body"])
            catches_return = all(self._always_returns(c.fields["body"]) for c in node.fields["catch_clauses"])
            return body_returns and catches_return
        return False  # GuardStmt, loops, and everything else: conservatively not guaranteed

    def _terminates(self, node):
        """Like `_always_returns`, but also true for `break`/`continue` -
        used to flag unreachable statements within a block, where any of
        the three (`return`, `break`, `continue`, or the terminators
        `_always_returns` already covers) makes everything after it in the
        same block dead code."""
        if node.kind == "LoopControlStmt":
            return True
        return self._always_returns(node)

    def _analyze_block(self, block_node):
        """Push a fresh scope, analyze the block's declarations then its
        statements (in that order - `grammar.py`'s two-phase block parsing
        already guarantees declarations precede statements), then pop the
        scope back off so names declared here don't leak into the
        enclosing block. Warns once on the first statement made
        unreachable by an earlier terminator (`return`/`break`/`continue`/
        `throw`) in the same block - everything after it is still analyzed
        normally, just flagged."""
        self._scopes.append({})
        for decl in block_node.fields["declarations"]:
            self._analyze_local_decl(decl)
        dead = False
        for stmt in block_node.fields["statements"]:
            if dead:
                self.warn(stmt, "Unreachable code")
                dead = False  # only warn once per block, at the first unreachable statement
            self._analyze_stmt(stmt)
            if self._terminates(stmt):
                dead = True
        self._scopes.pop()

    def _analyze_block_or_stmt(self, node):
        """`if`'s then/else branches are always a real `Block`, *except*
        an `else if`, which is a bare chained `IfStmt` (see grammar.py's
        `_if_stmt_fn`) with no block of its own to push/pop a scope for -
        this dispatches to whichever handling actually applies."""
        if node.kind == "Block":
            self._analyze_block(node)
        else:
            self._analyze_stmt(node)  # chained else-if

    # -- local declarations --

    def _analyze_local_decl(self, decl):
        """Dispatch a block's leading declaration to whichever kind it is
        (`VarDecl` for `val`/`var`, `LetDecl` for `let`)."""
        if decl.kind == "VarDecl":
            self._analyze_var_decl(decl)
        elif decl.kind == "LetDecl":
            self._analyze_let_decl(decl)
        else:
            self.error(decl, f"Unsupported declaration kind '{decl.kind}'")

    def _analyze_var_decl(self, decl):
        """`val`/`var` declaration: declare each comma-separated name as a
        local of the declared type (mutable only for `var`), then
        type-check its initializer, if any, against that type."""
        mutable = decl.fields["mutability"] == "var"
        for d in decl.fields["declarators"]:
            dtype = self._resolve_type(d.fields["type"])
            self._declare_local(d.fields["name"], dtype, mutable, d)
            init = d.fields["initializer"]
            if init is not None:
                self._check_initializer(init, dtype)

    def _check_initializer(self, init_node, declared_type):
        """Type-check one initializer expression (or initializer list)
        against the variable's declared type, recording a `node_coerce`
        entry when the value's type is compatible but not identical (e.g.
        an `int` initializing a `float`) so `ir.py` knows to wrap it in a
        `CAST`."""
        if init_node.kind == "InitializerList":
            self._check_initializer_list(init_node, declared_type)
            return
        init_type = self._infer_expr(init_node)
        if not can_coerce(init_type, declared_type):
            self.error(init_node, f"Cannot initialize '{type_name(declared_type)}' with value of type '{type_name(init_type)}'")
        elif not types_equal(init_type, declared_type):
            self.st.node_coerce[id(init_node)] = declared_type

    def _check_initializer_list(self, node, declared_type):
        """Type-check a brace `{ v1, v2, ... }` initializer list against
        either an array type (each value must coerce to the element type,
        and the count must match an explicit size if there is one) or a
        struct type (values are matched positionally against
        `field_order`, extras still type-checked - for their own errors -
        but otherwise ignored)."""
        values = node.fields["values"]
        if declared_type[0] == "array":
            elem_type, size = declared_type[1], declared_type[2]
            if size is not None and len(values) != size:
                self.error(node, f"Array initializer has {len(values)} element(s), expected {size}")
            for v in values:
                vt = self._infer_expr(v)
                if not can_coerce(vt, elem_type):
                    self.error(v, f"Cannot use value of type '{type_name(vt)}' in an array of '{type_name(elem_type)}'")
                elif not types_equal(vt, elem_type):
                    self.st.node_coerce[id(v)] = elem_type
            return
        if declared_type[0] == "struct":
            info = self.st.structs.get(declared_type[1])
            if info is not None:
                if len(values) != len(info.field_order):
                    self.error(node, f"Struct initializer has {len(values)} value(s), expected {len(info.field_order)}")
                for fname, v in zip(info.field_order, values):
                    ft = info.fields[fname]
                    vt = self._infer_expr(v)
                    if not can_coerce(vt, ft):
                        self.error(v, f"Cannot use value of type '{type_name(vt)}' for field '{fname}' of type '{type_name(ft)}'")
                    elif not types_equal(vt, ft):
                        self.st.node_coerce[id(v)] = ft
                # Extra values past the field count still get type-checked
                # (so a bad expression there is still reported) even though
                # they have no field to be matched against.
                for v in values[len(info.field_order):]:
                    self._infer_expr(v)
            return
        self.error(node, f"Initializer list used for unsupported type '{type_name(declared_type)}'")
        for v in values:
            self._infer_expr(v)

    def _analyze_let_decl(self, decl):
        """`let` has two shapes (see grammar.py's `_let_stmt_fn`):

          - single-name array form (`array_sizes` is not None): infer the
            element type either from an initializer list's own elements
            (unified to one common type) or from an existing array
            expression being copied, then declare one new array-typed
            local.
          - destructuring form: infer every value's type, then match them
            against the name list either by unpacking one single
            tuple-typed value (a multi-return call) or positionally
            zipping several separate values - mismatched counts are
            reported but still produce placeholder T_ERROR-typed symbols
            so the rest of the function can keep being analyzed.

        Every `let`-bound name is always mutable (see CLAUDE.md: neither
        `let` nor `multi_assign` has a `val`/`const`-style marker in the
        grammar).
        """
        names = decl.fields["names"]
        values = decl.fields["values"]
        array_sizes = decl.fields.get("array_sizes")

        if array_sizes is not None:
            name, init, size_node = names[0], values[0], array_sizes[0]
            size = self._const_int_or_none(size_node)
            if init.kind == "InitializerList":
                elem_types = [self._infer_expr(v) for v in init.fields["values"]]
                elem_type = unify(elem_types) if elem_types else T_INT
                if elem_type is None:
                    self.error(init, "Array initializer elements have incompatible types")
                    elem_type = T_ERROR
            else:
                src = self._infer_expr(init)
                if src[0] == "array":
                    elem_type = src[1]
                else:
                    self.error(init, f"Expected an array initializer for 'let {name}[...]'")
                    elem_type = T_ERROR
            arr_type = t_array(elem_type, size)
            self._declare_local(name, arr_type, True, decl)
            if init.kind == "InitializerList" and size is not None and len(init.fields["values"]) != size:
                self.error(init, f"Array initializer has {len(init.fields['values'])} element(s), expected {size}")
            return

        value_types = [self._infer_expr(v) for v in values]
        if len(values) == 1 and value_types[0][0] == "tuple" and len(value_types[0][1]) == len(names):
            # One call returning a matching-arity tuple: unpack it.
            elem_types = list(value_types[0][1])
        elif len(values) == len(names):
            # Several separate values, one per name, matched positionally.
            elem_types = value_types
        elif len(values) == 1:
            self.error(decl, f"'{values[0].kind}' does not produce {len(names)} value(s) for this let declaration")
            elem_types = [T_ERROR] * len(names)
        else:
            self.error(decl, f"Expected {len(names)} value(s), got {len(values)}")
            elem_types = (value_types + [T_ERROR] * len(names))[:len(names)]

        # A `None` name (from a `_` slot in grammar.py's `_let_stmt_fn`)
        # discards that position - no symbol is declared for it, and
        # `None` is kept in its place in the returned list so ir.py's
        # `gen_let_decl`/`_gen_destructure` can zip against `elem_types`
        # positionally and skip emitting a store for it.
        symbols = [self._make_local_symbol(n, t, True, decl) if n is not None else None
                   for n, t in zip(names, elem_types)]
        self.st.node_symbol[id(decl)] = symbols

    # -- statements --

    def _analyze_stmt(self, node):
        """Dispatch one statement node to its specific `_analyze_*`
        handler by `kind`."""
        kind = node.kind
        if kind == "ExprStmt":
            self._infer_expr(node.fields["expression"])
        elif kind == "Block":
            self._analyze_block(node)
        elif kind == "IfStmt":
            self._check_condition(node.fields["condition"])
            self._analyze_block_or_stmt(node.fields["then"])
            if node.fields["else"] is not None:
                self._analyze_block_or_stmt(node.fields["else"])
        elif kind == "ForStmt":
            self._analyze_for(node)
        elif kind == "ForInStmt":
            self._analyze_for_in(node)
        elif kind == "WhileStmt":
            self._check_condition(node.fields["condition"])
            self._loop_depth += 1
            self._analyze_block(node.fields["body"])
            self._loop_depth -= 1
        elif kind == "RepeatUntilStmt":
            self._loop_depth += 1
            self._analyze_block(node.fields["body"])
            self._loop_depth -= 1
            self._check_condition(node.fields["condition"])
        elif kind == "ReturnStmt":
            self._analyze_return(node)
        elif kind == "LoopControlStmt":
            if self._loop_depth == 0:
                self.error(node, f"'{node.fields['keyword']}' used outside of a loop")
        elif kind == "BuiltinStmt":
            self._analyze_builtin(node)
        elif kind == "MatchStmt":
            self._analyze_match(node, is_expr=False)
        elif kind == "GuardStmt":
            self._check_condition(node.fields["condition"])
            self._analyze_block(node.fields["else_body"])
        elif kind == "TryStmt":
            self._analyze_try(node)
        elif kind == "ThrowStmt":
            t = self._infer_expr(node.fields["value"])
            if not (is_error(t) or types_equal(t, T_STRING)):
                self.error(node.fields["value"], f"'throw' requires a 'string' value, got '{type_name(t)}'")
        elif kind == "MultiAssign":
            self._analyze_multi_assign(node)
        else:
            self.error(node, f"Unsupported statement kind '{kind}'")

    def _check_condition(self, node):
        """Every control-flow condition (if/while/until/guard) must be
        exactly `bool` - no truthy-value coercion of ints/pointers the way
        C allows."""
        t = self._infer_expr(node)
        if not (is_error(t) or types_equal(t, T_BOOL)):
            self.error(node, f"Condition must be of type 'bool', got '{type_name(t)}'")

    def _analyze_for(self, node):
        """C-style `for`: its own scope wraps the init/condition/update/body
        so an init-declared variable (if `init` is a declaration-shaped
        expression) doesn't leak past the loop - though note `init`/`update`
        are analyzed as plain expressions here, not declarations; a true
        `for (var int i = 0; ...)` init form isn't part of this grammar
        (see grammar.py's `_for_stmt_fn` - `init` is always `expression?`)."""
        self._scopes.append({})
        if node.fields["init"] is not None:
            self._infer_expr(node.fields["init"])
        if node.fields["condition"] is not None:
            self._check_condition(node.fields["condition"])
        if node.fields["update"] is not None:
            self._infer_expr(node.fields["update"])
        self._loop_depth += 1
        self._analyze_block(node.fields["body"])
        self._loop_depth -= 1
        self._scopes.pop()

    def _analyze_for_in(self, node):
        """`for (x in iterable)`: a `RangeExpr` iterable always yields
        `int` (the range's own bounds are checked as plain expressions,
        not via `_check_condition`, since they aren't a boolean test); any
        other iterable must be an array, and the loop variable takes its
        element type."""
        iterable = node.fields["iterable"]
        if iterable.kind == "RangeExpr":
            self._infer_expr(iterable.fields["start"])
            self._infer_expr(iterable.fields["end"])
            elem_type = T_INT
        else:
            it = self._infer_expr(iterable)
            if is_error(it):
                elem_type = T_ERROR
            elif it[0] == "array":
                elem_type = it[1]
            else:
                self.error(iterable, f"Cannot iterate over type '{type_name(it)}'")
                elem_type = T_ERROR
        self._scopes.append({})
        self._declare_local(node.fields["name"], elem_type, True, node)
        self._loop_depth += 1
        self._analyze_block(node.fields["body"])
        self._loop_depth -= 1
        self._scopes.pop()

    def _analyze_return(self, node):
        """Check a `return`'s value count and type(s) against the enclosing
        function's declared return type - three distinct shapes (`void`,
        a single type, or a tuple type), each with its own arity check
        before any type-checking, so a wrong-arity return still gets every
        value's own expression checked (for its own independent errors)
        rather than being skipped outright."""
        values = node.fields["values"]
        ret_type = self._current_function.return_type

        if ret_type == T_VOID:
            if values:
                self.error(node, "'void' function cannot return a value")
            for v in values:
                self._infer_expr(v)
            return

        if ret_type[0] == "tuple":
            expected = list(ret_type[1])
            if len(values) != len(expected):
                self.error(node, f"Expected {len(expected)} return value(s), got {len(values)}")
            for v, et in zip(values, expected):
                vt = self._infer_expr(v)
                if not can_coerce(vt, et):
                    self.error(v, f"Cannot return '{type_name(vt)}' as '{type_name(et)}'")
                elif not types_equal(vt, et):
                    self.st.node_coerce[id(v)] = et
            for v in values[len(expected):]:
                self._infer_expr(v)
            return

        if len(values) != 1:
            self.error(node, f"Expected 1 return value, got {len(values)}")
            for v in values:
                self._infer_expr(v)
            return
        vt = self._infer_expr(values[0])
        if not can_coerce(vt, ret_type):
            self.error(values[0], f"Cannot return '{type_name(vt)}' as '{type_name(ret_type)}'")
        elif not types_equal(vt, ret_type):
            self.st.node_coerce[id(values[0])] = ret_type

    def _analyze_builtin(self, node):
        """`print` accepts any expressions. `input` is stricter: every
        argument must be a plain, already-declared, mutable identifier
        (not an array/struct field, index expression, or literal) - `input`
        writes a value *into* a variable, so it needs an actual assignable
        slot to write into. It must also be a scalar (`int`/`float`/`char`/
        `string`/`bool`) - an array/struct/tuple target would silently be
        overwritten with a raw string by `IRExecutor._coerce`'s fallback
        branch, since it has no representation to parse into for those
        tags."""
        if node.fields["name"] == "print":
            for a in node.fields["args"]:
                self._infer_expr(a)
            return
        for a in node.fields["args"]:
            if a.kind != "Identifier":
                self.error(a, "input() arguments must be plain variable names")
                continue
            t = self._infer_expr(a)
            sym = self.st.node_symbol.get(id(a))
            if isinstance(sym, Symbol) and not sym.mutable:
                self.error(a, f"Cannot input() into immutable variable '{sym.name}'")
            if not (is_error(t) or (t[0] == "prim" and t != T_VOID)):
                self.error(a, f"input() target must be a scalar variable, got '{type_name(t)}'")

    def _analyze_pattern(self, pattern, subj_type):
        """Type-check one match arm's pattern against the subject's type,
        branching on the same three pattern shapes ir.py's
        `_gen_match_test` later dispatches on: wildcard (nothing to check),
        range (needs a numeric subject), predicate (evaluated standalone,
        ignoring the subject), or plain equality-comparable value.

        A pattern is a predicate iff *its own* type is `bool` and the
        subject's type is *not* - a real type-based decision, recorded in
        `node_is_predicate` for `ir.py`'s `_gen_match_test` to reuse
        without re-deriving it, rather than the previous AST-shape guess
        (any top-level relational/logical operator). That heuristic could
        never express a pattern that equality-compares the subject against
        a bool-valued sub-expression; this can, since the decision now
        looks at the subject's type too - e.g. `match (p) { true => ...
        }` with a `bool` subject correctly stays an equality compare."""
        if pattern.kind == "WildcardPattern":
            return
        if pattern.kind == "RangeExpr":
            self._infer_expr(pattern.fields["start"])
            self._infer_expr(pattern.fields["end"])
            if not (is_error(subj_type) or is_numeric(subj_type)):
                self.error(pattern, f"Range pattern requires a numeric subject, got '{type_name(subj_type)}'")
            return
        pt = self._infer_expr(pattern)
        is_predicate = types_equal(pt, T_BOOL) and not (is_error(subj_type) or types_equal(subj_type, T_BOOL))
        self.st.node_is_predicate[id(pattern)] = is_predicate
        if is_predicate:
            return
        if not (is_error(pt) or is_error(subj_type)):
            if is_numeric(pt) and is_numeric(subj_type):
                pass
            elif not types_equal(pt, subj_type):
                self.error(pattern, f"Match pattern type '{type_name(pt)}' does not match subject type '{type_name(subj_type)}'")

    def _analyze_match(self, node, is_expr):
        """Shared by `MatchStmt` and `MatchExpr` (`is_expr` picks which):
        infer the subject once, then check every arm's pattern against it.
        For the expression form only, also `unify()` every arm's value type
        into one common result type (recorded in `node_type`, consumed by
        `ir.py`'s `gen_match` to know the type of its result temp) -
        `unify` returning `None` (genuine mismatch) is reported once here,
        for the whole match, rather than per-arm."""
        subj_type = self._infer_expr(node.fields["subject"])
        value_key = "value" if is_expr else "body"
        arm_types = []
        seen_literals = set()
        for case in node.fields["cases"]:
            pattern = case.fields["pattern"]
            if pattern.kind == "Literal":
                key = (pattern.fields.get("token_type"), pattern.fields.get("value"))
                if key in seen_literals:
                    self.warn(pattern, f"Duplicate match pattern '{pattern.fields.get('lexeme')}' is unreachable")
                seen_literals.add(key)
            self._analyze_pattern(pattern, subj_type)
            value_node = case.fields[value_key]
            if is_expr:
                arm_types.append(self._infer_expr(value_node))
            else:
                self._analyze_stmt(value_node)
        if is_expr:
            result = unify(arm_types) if arm_types else T_ERROR
            if result is None:
                self.error(node, "match expression arms do not agree on a common type")
                result = T_ERROR
            self.st.node_type[id(node)] = result
            return result
        return None

    def _analyze_try(self, node):
        """`try`/`catch`/`finally`: the catch variable is always declared
        as a fresh `string`-typed local scoped to just that catch clause
        (matching the language rule that thrown/caught values are always
        strings). Catch clauses carry no exception type, so there's no way
        to selectively dispatch between more than one - the grammar still
        accepts `catch_list -> catch_clause catch_list` (a `TryStmt` can
        carry several `CatchClause` nodes), but only one is semantically
        meaningful, so anything past the first is rejected here rather than
        silently compiled as dead code."""
        self._analyze_block(node.fields["body"])
        clauses = node.fields["catch_clauses"]
        if len(clauses) > 1:
            self.error(clauses[1], "Only one 'catch' clause is supported")
        for clause in clauses:
            self._scopes.append({})
            sym = self._make_local_symbol(clause.fields["name"], T_STRING, True, clause)
            self.st.node_symbol[id(clause)] = sym
            self._analyze_block(clause.fields["body"])
            self._scopes.pop()
        if node.fields["finally_body"] is not None:
            self._analyze_block(node.fields["finally_body"])

    def _analyze_multi_assign(self, node):
        """`int a, b = f();` form: declares brand-new locals (unlike a plain
        `AssignExpr`, which targets already-declared names) with their own
        explicit types, then checks the right-hand side the same way
        `_analyze_let_decl`'s destructuring form does - one tuple-typed call
        unpacked, or several values zipped positionally.

        `src_nodes` holds `None` placeholders in the tuple-unpack case
        (`values = [x]` but `lvalues` has several entries) because there is
        no single per-target *source expression* to attach a `node_coerce`
        entry to - the source is one call's tuple result, not one AST node
        per target - so the coercion loop below simply skips recording a
        coercion whenever `v is None`. (Whether that unpacked tuple's
        *element* types actually get cast at all is `ir.py`'s `UNPACK`
        opcode's concern, not this analysis's.)
        """
        lvalues = node.fields["lvalues"]  # list of {"type": Node, "name": str|None}
        values = node.fields["values"]
        types = [self._resolve_type(lv["type"]) for lv in lvalues]
        # A `None` name (from a `_` slot in grammar.py's `_multi_assign_stmt_fn`)
        # discards that position - still type-checked below like any other
        # slot, just never declared as a symbol.
        symbols = [self._make_local_symbol(lv["name"], t, True, node) if lv["name"] is not None else None
                   for lv, t in zip(lvalues, types)]
        self.st.node_symbol[id(node)] = symbols

        value_types = [self._infer_expr(v) for v in values]
        if len(values) == 1 and value_types[0][0] == "tuple" and len(value_types[0][1]) == len(lvalues):
            src_types = list(value_types[0][1])
            src_nodes = [None] * len(lvalues)
        elif len(values) == len(lvalues):
            src_types = value_types
            src_nodes = values
        else:
            self.error(node, f"Expected {len(lvalues)} value(s), got {len(values)}")
            src_types = [T_ERROR] * len(lvalues)
            src_nodes = [None] * len(lvalues)

        for t, st_, v in zip(types, src_types, src_nodes):
            if not can_coerce(st_, t):
                self.error(node, f"Cannot assign '{type_name(st_)}' to '{type_name(t)}'")
            elif v is not None and not types_equal(st_, t):
                self.st.node_coerce[id(v)] = t

    # -- expressions --

    def _infer_expr(self, node):
        """Infer (and record in `node_type`) the type of one expression
        node, dispatching by `kind`. This is the single place every
        expression-type fact in the program is decided - every other
        method above calls into this rather than duplicating type logic."""
        kind = node.kind

        if kind == "Literal":
            t = {
                "INTEGER_LIT": T_INT, "FLOAT_LIT": T_FLOAT, "STRING_LIT": T_STRING,
                "CHAR_LIT": T_CHAR, "BOOL_LIT": T_BOOL,
            }.get(node.fields["token_type"], T_ERROR)
            self.st.node_type[id(node)] = t
            return t

        if kind == "Identifier":
            sym = self._lookup(node.fields["name"])
            if sym is None:
                self.error(node, f"Undeclared identifier '{node.fields['name']}'")
                self.st.node_type[id(node)] = T_ERROR
                return T_ERROR
            self.st.node_symbol[id(node)] = sym
            self.st.node_type[id(node)] = sym.type
            return sym.type

        if kind == "Grouping":
            t = self._infer_expr(node.fields["expression"])
            self.st.node_type[id(node)] = t
            return t

        if kind == "InterpString":
            for p in node.fields["parts"]:
                self._infer_expr(p)
            self.st.node_type[id(node)] = T_STRING
            return T_STRING

        if kind == "UnaryExpr":
            t = self._infer_expr(node.fields["operand"])
            op = node.fields["op"]
            if op == "!":
                if not (is_error(t) or types_equal(t, T_BOOL)):
                    self.error(node, f"Operator '!' requires a bool operand, got '{type_name(t)}'")
                result = T_BOOL
            elif is_error(t) or is_numeric(t):
                result = T_ERROR if is_error(t) else (T_INT if t == T_CHAR else t)
            else:
                self.error(node, f"Operator '{op}' requires a numeric operand, got '{type_name(t)}'")
                result = T_ERROR
            self.st.node_type[id(node)] = result
            return result

        if kind == "BinaryExpr":
            result = self._infer_binary(node)
            self.st.node_type[id(node)] = result
            return result

        if kind == "RangeExpr":
            self._infer_expr(node.fields["start"])
            self._infer_expr(node.fields["end"])
            t = ("range", T_INT)
            self.st.node_type[id(node)] = t
            return t

        if kind == "AssignExpr":
            t = self._infer_assign(node)
            self.st.node_type[id(node)] = t
            return t

        if kind == "CallExpr":
            t = self._infer_call(node)
            self.st.node_type[id(node)] = t
            return t

        if kind == "IndexExpr":
            obj_t = self._infer_expr(node.fields["object"])
            idx_t = self._infer_expr(node.fields["index"])
            if not (is_error(idx_t) or types_equal(idx_t, T_INT)):
                self.error(node.fields["index"], f"Array index must be of type 'int', got '{type_name(idx_t)}'")
            if is_error(obj_t):
                result = T_ERROR
            elif obj_t[0] == "array":
                result = obj_t[1]
            else:
                self.error(node, f"Cannot index into non-array type '{type_name(obj_t)}'")
                result = T_ERROR
            self.st.node_type[id(node)] = result
            return result

        if kind == "MemberExpr":
            obj_t = self._infer_expr(node.fields["object"])
            if is_error(obj_t):
                result = T_ERROR
            elif obj_t[0] == "struct":
                info = self.st.structs.get(obj_t[1])
                fname = node.fields["field"]
                if info is None or fname not in info.fields:
                    self.error(node, f"Struct '{obj_t[1]}' has no field '{fname}'")
                    result = T_ERROR
                else:
                    result = info.fields[fname]
            else:
                self.error(node, f"Cannot access field '.{node.fields['field']}' on non-struct type '{type_name(obj_t)}'")
                result = T_ERROR
            self.st.node_type[id(node)] = result
            return result

        if kind == "MatchExpr":
            return self._analyze_match(node, is_expr=True)

        self.error(node, f"Unsupported expression kind '{kind}'")
        self.st.node_type[id(node)] = T_ERROR
        return T_ERROR

    def _infer_binary(self, node):
        """Type-check one `BinaryExpr` by operator family: arithmetic
        (`+ - * / %`, both operands numeric; `+` additionally rejects
        `string` - this language has no string concatenation operator, see
        CLAUDE.md), equality (`== !=`, numeric cross-comparison allowed, or
        exact type match otherwise), relational (`< > <= >=`, numeric
        only), and logical (`&& ||`, both operands must be exactly
        `bool`)."""
        op = node.fields["op"]
        l = self._infer_expr(node.fields["left"])
        r = self._infer_expr(node.fields["right"])
        if is_error(l) or is_error(r):
            return T_ERROR

        if op in ("+", "-", "*", "/", "%"):
            if op == "+" and (l == T_STRING or r == T_STRING):
                self.error(node, "'+' does not support string concatenation; use an interpolated string instead")
                return T_ERROR
            result = arithmetic_result(l, r)
            if result is None:
                self.error(node, f"Operator '{op}' requires numeric operands, got '{type_name(l)}' and '{type_name(r)}'")
                return T_ERROR
            return result

        if op in ("==", "!="):
            if is_numeric(l) and is_numeric(r):
                return T_BOOL
            if types_equal(l, r):
                return T_BOOL
            self.error(node, f"Cannot compare '{type_name(l)}' and '{type_name(r)}'")
            return T_ERROR

        if op in ("<", ">", "<=", ">="):
            if is_numeric(l) and is_numeric(r):
                return T_BOOL
            self.error(node, f"Operator '{op}' requires numeric operands, got '{type_name(l)}' and '{type_name(r)}'")
            return T_ERROR

        if op in ("&&", "||"):
            if not types_equal(l, T_BOOL):
                self.error(node.fields["left"], f"Operator '{op}' requires bool operands, got '{type_name(l)}'")
            if not types_equal(r, T_BOOL):
                self.error(node.fields["right"], f"Operator '{op}' requires bool operands, got '{type_name(r)}'")
            return T_BOOL

        self.error(node, f"Unknown operator '{op}'")
        return T_ERROR

    def _infer_assign(self, node):
        """Type-check an `AssignExpr`. The target must be an `Identifier`
        (checked mutable - `val`/`const` binding reassignment is rejected
        here, though *element/field* mutation through such a binding is
        not, since that goes through the `IndexExpr`/`MemberExpr` branch
        instead, which has no mutability of its own to check - only the
        *container* variable's mutability would apply, and arrays/structs
        are mutable containers by construction) or an `IndexExpr`/
        `MemberExpr` (whose own type-checking already validates the
        container/field access). A compatible-but-different value type
        records a `node_coerce` entry the same way initializers do."""
        target = node.fields["target"]
        value = node.fields["value"]

        if target.kind == "Identifier":
            sym = self._lookup(target.fields["name"])
            if sym is None:
                self.error(target, f"Undeclared identifier '{target.fields['name']}'")
                self._infer_expr(value)
                return T_ERROR
            self.st.node_symbol[id(target)] = sym
            self.st.node_type[id(target)] = sym.type
            if not sym.mutable:
                self.error(node, f"Cannot assign to immutable variable '{sym.name}'")
            target_type = sym.type
        elif target.kind in ("IndexExpr", "MemberExpr"):
            target_type = self._infer_expr(target)
        else:
            self.error(target, f"Invalid assignment target '{target.kind}'")
            self._infer_expr(value)
            return T_ERROR

        value_type = self._infer_expr(value)
        if not can_coerce(value_type, target_type):
            self.error(node, f"Cannot assign '{type_name(value_type)}' to '{type_name(target_type)}'")
        elif not types_equal(value_type, target_type):
            self.st.node_coerce[id(value)] = target_type
        return target_type

    def _infer_call(self, node):
        """Resolve and type-check a `CallExpr`. Records the resolved
        `FunctionSig` in `node_call` and - the key output `ir.py` actually
        needs - the arguments in **positional call order** in
        `node_call_args`, regardless of whether the call used positional or
        named arguments; `ir.py`'s `_gen_call` just emits one `PARAM` quad
        per entry in that list, in order, so it never needs to know or care
        whether the source used `f(1, 2)` or `f(b: 2, a: 1)`.

        Named and positional arguments cannot be mixed in one call (checked
        below). A parameter with a default (see grammar.py's `_param_fn`)
        may be omitted from either call form; omitting one splices in the
        same shared default-value Node `_collect_functions_and_const_globals`
        already type-checked once at the parameter's declaration, so it
        never needs re-checking per call site.
        """
        callee = node.fields["callee"]
        if callee.kind != "Identifier":
            self.error(node, "Only direct function calls are supported")
            return T_ERROR
        name = callee.fields["name"]
        sig = self.st.functions.get(name)
        if sig is None:
            self.error(node, f"Call to undeclared function '{name}'")
            for a in node.fields["args"]:
                self._infer_expr(a.fields["value"] if a.kind == "NamedArg" else a)
            return T_ERROR
        self.st.node_call[id(node)] = sig

        args = node.fields["args"]
        named = [a for a in args if a.kind == "NamedArg"]
        if named:
            if len(named) != len(args):
                self.error(node, "Cannot mix positional and named arguments in a call")
            provided = {}
            for a in named:
                pname = a.fields["name"]
                if pname not in sig.param_names:
                    self.error(a, f"Function '{name}' has no parameter '{pname}'")
                    self._infer_expr(a.fields["value"])
                    continue
                if pname in provided:
                    self.error(a, f"Parameter '{pname}' specified more than once")
                provided[pname] = a.fields["value"]
            defaults_by_name = dict(zip(sig.param_names, sig.param_defaults))
            missing = [p for p in sig.param_names if p not in provided and defaults_by_name[p] is None]
            if missing:
                self.error(node, f"Missing argument(s) for parameter(s): {', '.join(missing)}")
            # Rebuild the argument list in the function's own declared
            # parameter order - this is what makes node_call_args
            # positionally-ordered regardless of the named call's writing
            # order. An omitted defaulted parameter splices in its shared
            # default Node instead of being skipped.
            ordered = []
            for pname, ptype, default in zip(sig.param_names, sig.param_types, sig.param_defaults):
                v = provided.get(pname)
                if v is None:
                    if default is not None:
                        ordered.append(default)
                    continue
                vt = self._infer_expr(v)
                if not can_coerce(vt, ptype):
                    self.error(v, f"Argument '{pname}' expects '{type_name(ptype)}', got '{type_name(vt)}'")
                elif not types_equal(vt, ptype):
                    self.st.node_coerce[id(v)] = ptype
                ordered.append(v)
            self.st.node_call_args[id(node)] = ordered
        else:
            required_count = sum(1 for d in sig.param_defaults if d is None)
            if not (required_count <= len(args) <= len(sig.param_names)):
                if required_count == len(sig.param_names):
                    self.error(node, f"Function '{name}' expects {len(sig.param_names)} argument(s), got {len(args)}")
                else:
                    self.error(node, f"Function '{name}' expects {required_count} to {len(sig.param_names)} argument(s), got {len(args)}")
            for a, ptype in zip(args, sig.param_types):
                vt = self._infer_expr(a)
                if not can_coerce(vt, ptype):
                    self.error(a, f"Argument expects '{type_name(ptype)}', got '{type_name(vt)}'")
                elif not types_equal(vt, ptype):
                    self.st.node_coerce[id(a)] = ptype
            for a in args[len(sig.param_names):]:
                self._infer_expr(a)
            # Trailing omitted parameters (only possible when every one of
            # them has a default - see the arity check above) splice in
            # their shared default Nodes.
            ordered = list(args[:len(sig.param_names)])
            ordered += [d for d in sig.param_defaults[len(args):] if d is not None]
            self.st.node_call_args[id(node)] = ordered
        return sig.return_type


def analyze(program: Node):
    """Module entry point: run a fresh `Analyzer` over `program` and return
    `(SymbolTable, [SemanticError])`."""
    return Analyzer().run(program)
