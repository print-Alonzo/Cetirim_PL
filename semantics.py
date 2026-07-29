"""Semantic analysis: name resolution, static type checking, and unique
IR-name assignment. Sits between the parser and the IR generator.

    analyze(program) -> (SymbolTable, [SemanticError])

Two passes over the AST:
  Pass 1 (globals) - collects struct/typedef definitions (resolving alias
    chains with cycle detection), function signatures, and global `const`
    declarations.
  Pass 2 (bodies)  - walks each function with a scope stack, resolving every
    name, checking every operator/call/assignment/control-flow construct,
    and recording the result on side tables keyed by id(node) so the IR
    generator (ir.py) never has to re-resolve anything.

Design notes (see the project plan for the full rationale):
  - `val`/`const` freeze the *binding*, not an array/struct's contents:
    only reassigning the name is rejected, element/field mutation is not.
  - Implicit numeric coercion: char -> int -> float. Everything else must
    match exactly. A required coercion is recorded in `node_coerce` so the
    IR generator can wrap that node's quad with CAST.
  - Every local symbol gets a globally-unique ir_name ("main.i", "main.i$2"
    on shadowing) so the VM can never confuse two same-named locals or a
    local with a same-named global.
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
_NUMERIC_RANK = {T_CHAR: 0, T_INT: 1, T_FLOAT: 2}


def t_struct(name):
    return ("struct", name)


def t_array(elem, size=None):
    return ("array", elem, size)


def t_tuple(elems):
    return ("tuple", tuple(elems))


def is_error(t):
    return t == T_ERROR


def type_name(t):
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
    return t in _NUMERIC_RANK


def can_coerce(src, dst):
    """True if `src` may be implicitly converted to `dst` (assignment, arg
    passing, return, initialization)."""
    if is_error(src) or is_error(dst):
        return True
    if types_equal(src, dst):
        return True
    if is_numeric(src) and is_numeric(dst):
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
            result = T_FLOAT if T_FLOAT in (result, t) else T_INT
            continue
        return None
    return result


# ---------------------------------------------------------------------------
# Symbol table
# ---------------------------------------------------------------------------

@dataclass
class Symbol:
    name: str
    type: tuple
    mutable: bool
    ir_name: str
    storage: str  # "global" | "local"


@dataclass
class StructInfo:
    name: str
    fields: Dict[str, tuple] = field(default_factory=dict)
    field_order: List[str] = field(default_factory=list)


@dataclass
class FunctionSig:
    name: str
    param_names: List[str]
    param_types: List[tuple]
    return_type: tuple  # T_VOID, a single type, or a ("tuple", ...) type
    node: Node


@dataclass
class SemanticError:
    message: str
    line: Optional[int]
    col: Optional[int]

    def __str__(self):
        pos = f"Line {self.line}, Col {self.col}: " if self.line is not None else ""
        return f"[SEMANTIC ERROR] {pos}{self.message}"


class SymbolTable:
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


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------

class Analyzer:
    def __init__(self):
        self.st = SymbolTable()
        self.errors: List[SemanticError] = []
        self._raw_structs: Dict[str, Tuple[list, Node]] = {}
        self._typedef_nodes: Dict[str, Node] = {}
        self._typedef_resolving: set = set()
        self._name_counters: Dict[str, int] = {}
        self._scopes: List[Dict[str, Symbol]] = []
        self._loop_depth = 0
        self._current_function: Optional[FunctionSig] = None

    def error(self, node, message):
        line = node.line if isinstance(node, Node) else None
        col = node.col if isinstance(node, Node) else None
        self.errors.append(SemanticError(message, line, col))

    def run(self, program: Node):
        decls = program.fields["declarations"]
        self._collect_structs_and_typedefs(decls)
        self._collect_functions_and_const_globals(decls)
        for d in decls:
            if d.kind == "FunctionDecl":
                self._analyze_function(d)
        return self.st, self.errors

    # -- Pass 1: types -----------------------------------------------------

    def _collect_structs_and_typedefs(self, decls):
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

    def _resolve_struct(self, name):
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

    def _resolve_type(self, node):
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
            base = self._resolve_type(node.fields["base"])
            size = self._const_int_or_none(node.fields.get("size"))
            return t_array(base, size)
        if node.kind == "TupleType":
            return t_tuple([self._resolve_type(e) for e in node.fields["elements"]])
        self.error(node, f"Unrecognized type node '{node.kind}'")
        return T_ERROR

    @staticmethod
    def _const_int_or_none(node):
        if node is not None and node.kind == "Literal" and node.fields.get("token_type") == "INTEGER_LIT":
            return node.fields["value"]
        return None

    def _collect_functions_and_const_globals(self, decls):
        for d in decls:
            if d.kind == "FunctionDecl":
                name = d.fields["name"]
                if name in self.st.functions:
                    self.error(d, f"Function '{name}' is already defined")
                    continue
                param_names, param_types, seen = [], [], set()
                for p in d.fields["params"]:
                    pname = p.fields["name"]
                    if pname in seen:
                        self.error(p, f"Duplicate parameter '{pname}' in function '{name}'")
                    seen.add(pname)
                    param_names.append(pname)
                    param_types.append(self._resolve_type(p.fields["type"]))
                ret_node = d.fields["return_type"]
                if ret_node.kind == "Type" and ret_node.fields["name"] == "void":
                    return_type = T_VOID
                else:
                    return_type = self._resolve_type(ret_node)
                self.st.functions[name] = FunctionSig(name, param_names, param_types, return_type, d)
            elif d.kind == "VarDecl":
                for decl in d.fields["declarators"]:
                    gname = decl.fields["name"]
                    if gname in self.st.globals:
                        self.error(decl, f"Global '{gname}' is already defined")
                        continue
                    gtype = self._resolve_type(decl.fields["type"])
                    sym = Symbol(gname, gtype, mutable=(d.fields["mutability"] != "const"),
                                 ir_name=gname, storage="global")
                    self.st.globals[gname] = sym
                    self.st.node_symbol[id(decl)] = sym
                    init = decl.fields["initializer"]
                    if init is not None:
                        self._check_initializer(init, gtype)

    # -- Pass 2: bodies ------------------------------------------------------

    def _fresh_ir_name(self, name):
        count = self._name_counters.get(name, 0) + 1
        self._name_counters[name] = count
        base = f"{self._current_function.name}.{name}"
        return base if count == 1 else f"{base}${count}"

    def _make_local_symbol(self, name, type_, mutable, owner_node):
        if name in self._scopes[-1]:
            self.error(owner_node, f"'{name}' is already declared in this scope")
        sym = Symbol(name, type_, mutable, self._fresh_ir_name(name), storage="local")
        self._scopes[-1][name] = sym
        return sym

    def _declare_local(self, name, type_, mutable, owner_node):
        sym = self._make_local_symbol(name, type_, mutable, owner_node)
        self.st.node_symbol[id(owner_node)] = sym
        return sym

    def _lookup(self, name):
        for scope in reversed(self._scopes):
            if name in scope:
                return scope[name]
        return self.st.globals.get(name)

    def _analyze_function(self, fn_node):
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
        self._current_function = None
        self._scopes = []

    def _analyze_block(self, block_node):
        self._scopes.append({})
        for decl in block_node.fields["declarations"]:
            self._analyze_local_decl(decl)
        for stmt in block_node.fields["statements"]:
            self._analyze_stmt(stmt)
        self._scopes.pop()

    def _analyze_block_or_stmt(self, node):
        if node.kind == "Block":
            self._analyze_block(node)
        else:
            self._analyze_stmt(node)  # chained else-if

    # -- local declarations --

    def _analyze_local_decl(self, decl):
        if decl.kind == "VarDecl":
            self._analyze_var_decl(decl)
        elif decl.kind == "LetDecl":
            self._analyze_let_decl(decl)
        else:
            self.error(decl, f"Unsupported declaration kind '{decl.kind}'")

    def _analyze_var_decl(self, decl):
        mutable = decl.fields["mutability"] == "var"
        for d in decl.fields["declarators"]:
            dtype = self._resolve_type(d.fields["type"])
            self._declare_local(d.fields["name"], dtype, mutable, d)
            init = d.fields["initializer"]
            if init is not None:
                self._check_initializer(init, dtype)

    def _check_initializer(self, init_node, declared_type):
        if init_node.kind == "InitializerList":
            self._check_initializer_list(init_node, declared_type)
            return
        init_type = self._infer_expr(init_node)
        if not can_coerce(init_type, declared_type):
            self.error(init_node, f"Cannot initialize '{type_name(declared_type)}' with value of type '{type_name(init_type)}'")
        elif not types_equal(init_type, declared_type):
            self.st.node_coerce[id(init_node)] = declared_type

    def _check_initializer_list(self, node, declared_type):
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
                for v in values[len(info.field_order):]:
                    self._infer_expr(v)
            return
        self.error(node, f"Initializer list used for unsupported type '{type_name(declared_type)}'")
        for v in values:
            self._infer_expr(v)

    def _analyze_let_decl(self, decl):
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
            elem_types = list(value_types[0][1])
        elif len(values) == len(names):
            elem_types = value_types
        elif len(values) == 1:
            self.error(decl, f"'{values[0].kind}' does not produce {len(names)} value(s) for this let declaration")
            elem_types = [T_ERROR] * len(names)
        else:
            self.error(decl, f"Expected {len(names)} value(s), got {len(values)}")
            elem_types = (value_types + [T_ERROR] * len(names))[:len(names)]

        symbols = [self._make_local_symbol(n, t, True, decl) for n, t in zip(names, elem_types)]
        self.st.node_symbol[id(decl)] = symbols

    # -- statements --

    def _analyze_stmt(self, node):
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
            self._infer_expr(node.fields["value"])
        elif kind == "MultiAssign":
            self._analyze_multi_assign(node)
        else:
            self.error(node, f"Unsupported statement kind '{kind}'")

    def _check_condition(self, node):
        t = self._infer_expr(node)
        if not (is_error(t) or types_equal(t, T_BOOL)):
            self.error(node, f"Condition must be of type 'bool', got '{type_name(t)}'")

    def _analyze_for(self, node):
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
        if node.fields["name"] == "print":
            for a in node.fields["args"]:
                self._infer_expr(a)
            return
        for a in node.fields["args"]:
            if a.kind != "Identifier":
                self.error(a, "input() arguments must be plain variable names")
                continue
            self._infer_expr(a)
            sym = self.st.node_symbol.get(id(a))
            if isinstance(sym, Symbol) and not sym.mutable:
                self.error(a, f"Cannot input() into immutable variable '{sym.name}'")

    def _is_predicate_pattern(self, pattern):
        if pattern.kind == "BinaryExpr" and pattern.fields.get("op") in {"<", ">", "<=", ">=", "==", "!=", "&&", "||"}:
            return True
        if pattern.kind == "UnaryExpr" and pattern.fields.get("op") == "!":
            return True
        return False

    def _analyze_pattern(self, pattern, subj_type):
        if pattern.kind == "WildcardPattern":
            return
        if pattern.kind == "RangeExpr":
            self._infer_expr(pattern.fields["start"])
            self._infer_expr(pattern.fields["end"])
            if not (is_error(subj_type) or is_numeric(subj_type)):
                self.error(pattern, f"Range pattern requires a numeric subject, got '{type_name(subj_type)}'")
            return
        if self._is_predicate_pattern(pattern):
            t = self._infer_expr(pattern)
            if not (is_error(t) or types_equal(t, T_BOOL)):
                self.error(pattern, f"Match predicate pattern must be of type 'bool', got '{type_name(t)}'")
            return
        pt = self._infer_expr(pattern)
        if not (is_error(pt) or is_error(subj_type)):
            if is_numeric(pt) and is_numeric(subj_type):
                pass
            elif not types_equal(pt, subj_type):
                self.error(pattern, f"Match pattern type '{type_name(pt)}' does not match subject type '{type_name(subj_type)}'")

    def _analyze_match(self, node, is_expr):
        subj_type = self._infer_expr(node.fields["subject"])
        value_key = "value" if is_expr else "body"
        arm_types = []
        for case in node.fields["cases"]:
            self._analyze_pattern(case.fields["pattern"], subj_type)
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
        self._analyze_block(node.fields["body"])
        for clause in node.fields["catch_clauses"]:
            self._scopes.append({})
            sym = self._make_local_symbol(clause.fields["name"], T_STRING, True, clause)
            self.st.node_symbol[id(clause)] = sym
            self._analyze_block(clause.fields["body"])
            self._scopes.pop()
        if node.fields["finally_body"] is not None:
            self._analyze_block(node.fields["finally_body"])

    def _analyze_multi_assign(self, node):
        lvalues = node.fields["lvalues"]  # list of {"type": Node, "name": str}
        values = node.fields["values"]
        types = [self._resolve_type(lv["type"]) for lv in lvalues]
        symbols = [self._make_local_symbol(lv["name"], t, True, node) for lv, t in zip(lvalues, types)]
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
            missing = [p for p in sig.param_names if p not in provided]
            if missing:
                self.error(node, f"Missing argument(s) for parameter(s): {', '.join(missing)}")
            ordered = []
            for pname, ptype in zip(sig.param_names, sig.param_types):
                v = provided.get(pname)
                if v is None:
                    continue
                vt = self._infer_expr(v)
                if not can_coerce(vt, ptype):
                    self.error(v, f"Argument '{pname}' expects '{type_name(ptype)}', got '{type_name(vt)}'")
                elif not types_equal(vt, ptype):
                    self.st.node_coerce[id(v)] = ptype
                ordered.append(v)
            self.st.node_call_args[id(node)] = ordered
        else:
            if len(args) != len(sig.param_names):
                self.error(node, f"Function '{name}' expects {len(sig.param_names)} argument(s), got {len(args)}")
            for a, ptype in zip(args, sig.param_types):
                vt = self._infer_expr(a)
                if not can_coerce(vt, ptype):
                    self.error(a, f"Argument expects '{type_name(ptype)}', got '{type_name(vt)}'")
                elif not types_equal(vt, ptype):
                    self.st.node_coerce[id(a)] = ptype
            for a in args[len(sig.param_names):]:
                self._infer_expr(a)
            self.st.node_call_args[id(node)] = list(args[:len(sig.param_names)])
        return sig.return_type


def analyze(program: Node):
    return Analyzer().run(program)
