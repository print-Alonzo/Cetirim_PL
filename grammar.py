"""The grammar table for the custom language, as data.

This is the ONLY file to edit when the language's syntax changes: add a
production to GRAMMAR, wire it into whatever rule should reference it, and
write an action that builds the Node the interpreter expects.
grammar_engine.py (the thing that walks this table) never needs to change.

Phase handoff: together with grammar_engine.py this file implements the
Parser phase. It receives tokens from scanner.py (via parser.py's
parse_source()) and produces the AST (ast_nodes.Node trees) that
semantics.py resolves and type-checks next.

Each GRAMMAR entry corresponds to one production of the language's grammar.
Where a rule's shape doesn't compose cleanly out of the declarative
combinators (Seq/Alt/Star/...), it's written as a plain Python function
wrapped in `Rule(fn)` instead - `grammar_engine.py`'s module docstring
explains why that escape hatch exists.

Precedence chain for expressions (each rule below calls the next one down,
so binding gets tighter as you go): assignment -> or_expr -> and_expr ->
equality -> comparison -> range_expr -> term -> factor -> unary -> postfix
-> primary. `GRAMMAR["expression"]` is simply an alias for `assignment` -
"parse an expression" always means "parse starting from the loosest-binding
level".
"""

from ast_nodes import Node, merge_type
from scanner import TT, Scanner
from grammar_engine import (
    Term, Kw, Seq, Alt, Star, Plus, Opt, Ref, And, Not, Bind, Cut, Emit,
    Abort, Rule, Fail, HardFail, ParseState, chainl, comma_list, many_rec,
)


TYPE_KEYWORDS = {"int", "float", "char", "string", "bool", "void"}
DECL_KEYWORDS = {"const", "val", "var"}

GRAMMAR = {}


# ---------------------------------------------------------------------------
# Types & declarators
#   type          := tuple_type | 'struct' IDENTIFIER | TYPE_KEYWORD | IDENTIFIER
#   return_type   := type, plus tuple types and 'void' are allowed here only
#   declarator_name := IDENTIFIER ('[' expression? ']')*
# ---------------------------------------------------------------------------

def _make_type_rule(allow_tuple, allow_void):
    """Factory producing the two type-parsing rules that differ only in
    which extra forms they permit: plain `type` (used for variable/param/
    field/struct-field types) forbids tuple types and `void`, while
    `return_type` (used only for a function's declared return type) allows
    both - a function may return a tuple or nothing, but a variable can't be
    declared `void` or hold a bare tuple type.
    """

    def fn(ps, committed):
        if allow_tuple and ps.check(TT.LPAREN):
            # Tuple return type: '(' type (',' type)+ ')' - at least two
            # elements, since a single-parenthesized type is just Grouping,
            # not a tuple.
            ps.pos += 1
            elements = [Ref("type").run(ps, True)]
            while True:
                save = ps.pos
                try:
                    Term(TT.COMMA).run(ps, False)
                except Fail:
                    ps.pos = save
                    break
                elements.append(Ref("type").run(ps, True))
            Term(TT.RPAREN, msg="Expected ')' after tuple return type").run(ps, True)
            if len(elements) < 2:
                ps.error("Tuple return type must contain at least two types")
            return Node("TupleType", {"elements": elements})

        if ps.check(TT.KEYWORD, "struct"):
            # 'struct' IDENTIFIER - a reference to a previously-declared
            # struct type (as opposed to `typedef`'s inline struct *definition*).
            ps.pos += 1
            name_tok = Term(TT.IDENTIFIER, msg="Expected struct type name").run(ps, True)
            return Node("StructType", {"name": name_tok.lexeme})

        if ps.check(TT.KEYWORD) and ps.current().lexeme in TYPE_KEYWORDS:
            if ps.current().lexeme == "void" and not allow_void:
                ps.error("'void' is not a valid type here")
                raise HardFail()
            name = ps.current().lexeme
            ps.pos += 1
            return Node("Type", {"name": name})

        if ps.check(TT.IDENTIFIER):
            # A bare identifier used as a type is a typedef alias reference;
            # whether it actually resolves to one is semantics.py's job, not
            # the parser's - the grammar only knows "this shape is a type".
            name = ps.current().lexeme
            ps.pos += 1
            return Node("Type", {"name": name})

        raise Fail()  # none of the type shapes matched at all

    return Rule(fn)


GRAMMAR["type"] = _make_type_rule(allow_tuple=False, allow_void=False)
GRAMMAR["return_type"] = _make_type_rule(allow_tuple=True, allow_void=True)


def _declarator_name_fn(ps, committed):
    """Parse `IDENTIFIER ('[' expression? ']')*` - a name plus zero or more
    trailing array-size brackets (e.g. `scores[5]`, `grid[rows][cols]`,
    `buf[]` with no size).

    Returns a plain dict, not a Node - it's an intermediate result (the
    array sizes still need to be *nested onto* a base type) that only ever
    feeds `ast_nodes.merge_type(base_type, this_dict)`, never appears in the
    AST on its own, and so has no reason to carry the Node machinery.
    """
    name_tok = Term(TT.IDENTIFIER, msg="Expected identifier").run(ps, True)
    arrays = []
    while True:
        save = ps.pos
        try:
            Term(TT.LBRACKET).run(ps, False)
        except Fail:
            ps.pos = save
            break
        if ps.check(TT.RBRACKET):
            size = None  # `[]` - size omitted (must come from an initializer instead)
        else:
            size = Ref("expression", fail_msg="Expected expression").run(ps, True)
        Term(TT.RBRACKET, msg="Expected ']' after array size").run(ps, True)
        arrays.append(size)
    return {"name": name_tok.lexeme, "arrays": arrays}


GRAMMAR["declarator_name"] = Rule(_declarator_name_fn)


# ---------------------------------------------------------------------------
# Struct fields  (shared by struct_decl and typedef's inline struct form)
#   struct_field_line  := type declarator_name (',' declarator_name)* ';'
#   struct_fields_body := struct_field_line*
# ---------------------------------------------------------------------------

def _build_struct_field_line(ps, c):
    """One field *line* can declare several fields at once sharing a type
    (`int x, y;`) - fan that out into one `Field` Node per declarator name,
    each getting the (possibly array-wrapped) type via `merge_type`."""
    return [
        Node("Field", {"name": n["name"], "type": merge_type(c["field_type"], n)})
        for n in c["names"]
    ]


GRAMMAR["struct_field_line"] = Seq(
    Bind("field_type", Ref("type", fail_msg="Expected type")),
    Cut(),
    Bind("names", comma_list(Ref("declarator_name"))),
    Term(TT.SEMICOLON, msg="Expected ';' after struct field"),
    action=_build_struct_field_line,
)

# No error recovery here: a malformed field line raises HardFail straight
# through this Star with nothing catching it, all the way out to the
# nearest enclosing recovery point (a program-level or block-level
# many_rec) - unlike declarations/statements, individual struct field lines
# have no synchronization point of their own to resume from mid-struct-body.
GRAMMAR["struct_fields_body"] = Star(Ref("struct_field_line"))


def _flatten_fields(field_groups):
    """`struct_fields_body` yields one list-of-Fields per line (see
    `_build_struct_field_line`); flatten those into a single field list for
    the enclosing StructDecl/StructDef/typedef node."""
    return [f for group in field_groups for f in group]


# ---------------------------------------------------------------------------
# struct_decl
#   struct_decl := 'struct' IDENTIFIER '{' struct_fields_body '}' ';'
# ---------------------------------------------------------------------------

def _build_struct_decl(ps, c):
    """Action for `GRAMMAR["struct_decl"]`: assemble the final `StructDecl`
    node from its captured name and (flattened) field list."""
    return Node("StructDecl", {"name": c["name"].lexeme, "fields": _flatten_fields(c["fields"])})


GRAMMAR["struct_decl"] = Seq(
    Kw("struct"), Cut(),
    Bind("name", Term(TT.IDENTIFIER, msg="Expected struct name")),
    Term(TT.LBRACE, msg="Expected '{' before struct fields"),
    Bind("fields", Ref("struct_fields_body")),
    Term(TT.RBRACE, msg="Expected '}' after struct body"),
    Term(TT.SEMICOLON, msg="Expected ';' after struct declaration"),
    action=_build_struct_decl,
)


# ---------------------------------------------------------------------------
# typedef_decl
#   typedef_decl := 'typedef' (inline_struct_typedef_body | type) IDENTIFIER ';'
#   inline_struct_typedef_body := 'struct' IDENTIFIER '{' struct_fields_body '}'
#     (only when directly followed by IDENTIFIER '{', via the And() lookahead
#      below - see the alias-name comment there)
# ---------------------------------------------------------------------------

def _build_inline_struct_typedef(ps, c):
    """Action for `GRAMMAR["inline_struct_typedef_body"]`: build the
    `StructDef` node (distinct from `StructDecl` - this one represents an
    inline definition inside a `typedef`, which `semantics.py`'s
    `_collect_structs_and_typedefs` treats specially, see there)."""
    return Node("StructDef", {"name": c["name"].lexeme, "fields": _flatten_fields(c["fields"])})


GRAMMAR["inline_struct_typedef_body"] = Seq(
    # Lookahead distinguishes `typedef struct Foo { ... } Bar;` (an inline
    # struct *definition* named Foo, aliased to Bar) from `typedef struct
    # Foo Bar;` (just a StructType *reference* to an already-declared Foo,
    # handled by the plain `type` alternative below instead). Both start
    # with `struct IDENTIFIER`, so the only way to tell them apart ahead of
    # time is to check whether a `{` follows.
    And(Seq(Kw("struct"), Term(TT.IDENTIFIER), Term(TT.LBRACE))),
    Kw("struct"), Cut(),
    Bind("name", Term(TT.IDENTIFIER, msg="Expected struct name in typedef")),
    Term(TT.LBRACE, msg="Expected '{' before inline struct fields"),
    Bind("fields", Ref("struct_fields_body")),
    Term(TT.RBRACE, msg="Expected '}' after inline struct body"),
    action=_build_inline_struct_typedef,
)


def _build_typedef_decl(ps, c):
    """Action for `GRAMMAR["typedef_decl"]`: build the final `TypedefDecl`
    node - `c["aliased"]` is either a plain `Type`/`StructType` node or a
    `StructDef` (inline struct form), both handled uniformly by
    semantics.py's typedef resolution."""
    return Node("TypedefDecl", {"name": c["name"].lexeme, "aliased_type": c["aliased"]})


GRAMMAR["typedef_decl"] = Seq(
    Kw("typedef"), Cut(),
    Bind("aliased", Alt(
        Ref("inline_struct_typedef_body"),
        Ref("type", fail_msg="Expected type"),
    )),
    Bind("name", Term(TT.IDENTIFIER, msg="Expected typedef alias name")),
    Term(TT.SEMICOLON, msg="Expected ';' after typedef"),
    action=_build_typedef_decl,
)


# ---------------------------------------------------------------------------
# var_decl / const_decl
#   var_decl   := ('const' | 'val' | 'var') type declarator_list ';'
#   const declarator_list is exactly one declarator, non-const is a comma_list
# ---------------------------------------------------------------------------

GRAMMAR["literal_value"] = Rule(lambda ps, committed: _literal_value(ps))


def _literal_value(ps):
    """A bare literal token (int/float/string/char/bool) - used only for
    `const` initializers, which are restricted to literals rather than
    arbitrary expressions (see `_const_declarator_fn` below)."""
    tok = ps.current()
    if tok.ttype in {TT.INTEGER_LIT, TT.FLOAT_LIT, TT.STRING_LIT, TT.CHAR_LIT, TT.BOOL_LIT}:
        ps.pos += 1
        return Node("Literal", {"token_type": tok.ttype, "lexeme": tok.lexeme, "value": tok.attr})
    raise Fail()


def _initializer_fn(ps, committed):
    """An initializer is either a brace-delimited `{ expr, expr, ... }`
    initializer list (for arrays/structs) or a plain expression."""
    if ps.check(TT.LBRACE):
        ps.pos += 1
        values = []
        if not ps.check(TT.RBRACE):
            values = comma_list(Ref("expression", fail_msg="Expected expression")).run(ps, True)
        Term(TT.RBRACE, msg="Expected '}' after initializer list").run(ps, True)
        return Node("InitializerList", {"values": values})
    return Ref("expression", fail_msg="Expected expression").run(ps, committed)


GRAMMAR["initializer"] = Rule(_initializer_fn)


def _const_declarator_fn(ps, committed):
    """A `const` declarator: `declarator_name '=' literal_value`. Unlike
    `val`/`var`, the right-hand side must be a bare literal, not an
    arbitrary expression - this is a deliberate language design choice
    (see CLAUDE.md), enforced here by calling `literal_value` instead of
    `initializer`/`expression`."""
    name_info = Ref("declarator_name").run(ps, True)
    Term(TT.ASSIGN_OP, msg="Expected '=' after const declarator name").run(ps, True)
    value = Ref("literal_value", fail_msg="Expected a literal value (int, float, char, string, or bool) in const declaration").run(ps, True)
    return Node("Declarator", {"name": name_info["name"], "type": None, "initializer": value, "_arrays": name_info["arrays"]})


def _nonconst_declarator_fn(ps, committed):
    """A `val`/`var` declarator: `declarator_name ('=' initializer)?` - the
    initializer is optional (an uninitialized `var` gets a type-appropriate
    default value at IR-generation time) and, when present, may be any
    expression or initializer list, not just a literal."""
    name_info = Ref("declarator_name").run(ps, True)
    save = ps.pos
    initializer = None
    try:
        Term(TT.ASSIGN_OP).run(ps, False)
    except Fail:
        ps.pos = save
    else:
        initializer = Ref("initializer", fail_msg="Expected expression").run(ps, True)
    return Node("Declarator", {"name": name_info["name"], "type": None, "initializer": initializer, "_arrays": name_info["arrays"]})


def _build_var_decl(ps, c):
    """Build the full `VarDecl`. Unusual shape: the declarator list isn't a
    static part of the `GRAMMAR["var_decl"]` Seq below because *which*
    declarator rule applies (`_const_declarator_fn` vs
    `_nonconst_declarator_fn`) depends on which keyword (`const` vs
    `val`/`var`) was just matched by that Seq - so this action function
    parses the rest of the declaration itself, after the Seq has already
    committed to a keyword and a base type.

    Each declarator's array-bracket suffixes (collected as `_arrays` by
    `_declarator_name_fn`) get folded into its final type here via
    `merge_type`, then that scratch key is discarded - `_arrays` is a
    parsing-only detail, not part of the `Declarator` node's public shape.
    """
    mutability = c["kw"].lexeme
    base_type = c["type"]
    is_const = mutability == "const"
    declarator_rule = Rule(_const_declarator_fn) if is_const else Rule(_nonconst_declarator_fn)

    if is_const:
        # const only ever declares one name per statement (no comma_list) -
        # `const int a = 1, b = 2;` is not supported.
        declarators = [declarator_rule.run(ps, True)]
    else:
        declarators = comma_list(declarator_rule).run(ps, True)

    for d in declarators:
        arrays = d.fields.pop("_arrays")
        d.fields["type"] = merge_type(base_type, {"arrays": arrays})

    Term(TT.SEMICOLON, msg="Expected ';' after declaration").run(ps, True)
    return Node("VarDecl", {"mutability": mutability, "declarators": declarators})


GRAMMAR["var_decl"] = Seq(
    Bind("kw", Alt(Kw("const"), Kw("val"), Kw("var"))),
    Cut(),
    Bind("type", Ref("type", fail_msg="Expected type")),
    action=_build_var_decl,
)


# ---------------------------------------------------------------------------
# let_stmt
#   let_stmt := 'let' IDENTIFIER '[' expression? ']' '=' initializer ';'
#             | 'let' IDENTIFIER (',' IDENTIFIER)* '=' expression (',' expression)* ';'
#   (the first form is a single-name array `let`; the second is one-or-more
#    names destructuring one-or-more values - see ir.py's _gen_destructure
#    for how a single multi-return call vs. several separate values differ)
# ---------------------------------------------------------------------------

def _let_stmt_fn(ps, committed):
    """`let` has two mutually-exclusive shapes that both start the same way
    (`let IDENTIFIER`), disambiguated by hand here rather than via `Alt`
    because the second form's *name list* keeps growing token-by-token
    before we can tell which shape we're in - by the time a `[` fails to
    match after the first name, we already know this must be the
    comma-separated-names form and can fall through to parsing it directly.
    """
    if not ps.check(TT.KEYWORD, "let"):
        raise Fail()
    ps.pos += 1
    first_name = Term(TT.IDENTIFIER, msg="Expected identifier in let declaration").run(ps, True)

    save = ps.pos
    try:
        Term(TT.LBRACKET).run(ps, False)
    except Fail:
        ps.pos = save
    else:
        # `let name[size] = initializer;` - single-name array let.
        size = None if ps.check(TT.RBRACKET) else Ref("expression", fail_msg="Expected expression").run(ps, True)
        Term(TT.RBRACKET, msg="Expected ']' after array size").run(ps, True)
        Term(TT.ASSIGN_OP, msg="Expected '=' in let array declaration").run(ps, True)
        initializer = Ref("initializer", fail_msg="Expected expression").run(ps, True)
        Term(TT.SEMICOLON, msg="Expected ';' after let declaration").run(ps, True)
        return Node("LetDecl", {"names": [first_name.lexeme], "array_sizes": [size], "values": [initializer]})

    # `let name (, name)* = expr (, expr)*;` - destructuring form.
    names = [first_name.lexeme]
    while True:
        save = ps.pos
        try:
            Term(TT.COMMA).run(ps, False)
        except Fail:
            ps.pos = save
            break
        names.append(Term(TT.IDENTIFIER, msg="Expected identifier after ','").run(ps, True).lexeme)

    Term(TT.ASSIGN_OP, msg="Expected '=' in let declaration").run(ps, True)
    values = comma_list(Ref("expression", fail_msg="Expected expression")).run(ps, True)
    Term(TT.SEMICOLON, msg="Expected ';' after let declaration").run(ps, True)
    return Node("LetDecl", {"names": names, "values": values})


GRAMMAR["let_stmt"] = Rule(_let_stmt_fn)


# ---------------------------------------------------------------------------
# block-level declarations
#   block_decl := let_stmt | var_decl   (var_decl here is always val/var,
#     never const/global - see global_var_decl further down for that split)
# ---------------------------------------------------------------------------

def _at_block_decl(ps):
    """True if the current token could begin a block-level declaration
    (`val`, `var`, or `let`) - used as the stopping condition for the
    block's declaration phase (see `GRAMMAR["block"]` below)."""
    return (ps.check(TT.KEYWORD) and ps.current().lexeme in {"val", "var"}) or ps.check(TT.KEYWORD, "let")


GRAMMAR["block_decl"] = Alt(
    Ref("let_stmt"),
    Ref("var_decl"),
)


# ---------------------------------------------------------------------------
# block
#   block := '{' block_decl* block_phase2_item* '}'
#   block_phase2_item := (declaration -> recorded as an error) | statement
#
# Two-phase body: every declaration must come before every statement. Phase 1
# only ever consumes declarations (stopping as soon as a non-declaration
# token is seen); phase 2 then accepts statements freely but *also* still
# recognizes a declaration-shaped token so it can flag it as
# out-of-order (via Emit) instead of just producing a confusing "expected
# statement" error.
# ---------------------------------------------------------------------------

_BLOCK_DECL_LOOKAHEAD = And(Alt(Kw("val"), Kw("var"), Kw("let")))

GRAMMAR["block_phase2_item"] = Alt(
    Seq(
        # A declaration-shaped token *after* phase 1 has already ended is
        # grammatically parseable (it's still a valid val/var/let) but
        # semantically wrong here - Emit() records that as an error
        # unconditionally, and parsing then continues by actually parsing
        # the declaration anyway (via Cut() + block_decl), so its statements
        # still show up correctly in the AST despite the ordering violation.
        _BLOCK_DECL_LOOKAHEAD,
        Emit("Variable declarations must appear before statements in a block"),
        Cut(),
        Bind("decl", Ref("block_decl", fail_msg="Expected declaration")),
        action=lambda ps, c: ("decl", c["decl"]),
    ),
    Seq(
        Bind("stmt", Ref("statement", fail_msg="Expected statement")),
        action=lambda ps, c: ("stmt", c["stmt"]),
    ),
)


def _build_block(ps, c):
    """Action for `GRAMMAR["block"]`: assemble the final `Block` node from
    the two parsed phases - `phase1`'s declarations go straight into
    `declarations`; `phase2`'s tagged `("stmt"|"decl", Node)` items are
    filtered down to just the `"stmt"` ones for `statements` (see the
    comment below on what happens to a misplaced `"decl"` item)."""
    # A misplaced declaration (see block_phase2_item above) still produced a
    # ("decl", Node) tuple, but it's dropped here rather than added to either
    # list - it was already flagged as an error, and there's no field on
    # Block that would make sense to put a statement-position declaration
    # into.
    statements = [item[1] for item in c["phase2"] if item[0] == "stmt"]
    return Node("Block", {"declarations": c["phase1"], "statements": statements})


GRAMMAR["block"] = Seq(
    Term(TT.LBRACE, msg="Expected '{' before block"),
    Cut(),
    Bind("phase1", many_rec(
        Ref("block_decl", fail_msg="Expected declaration"),
        is_stop=lambda ps: ps.check(TT.RBRACE) or not _at_block_decl(ps),
    )),
    Bind("phase2", many_rec(
        Ref("block_phase2_item", fail_msg="Expected statement"),
        is_stop=lambda ps: ps.check(TT.RBRACE),
    )),
    Term(TT.RBRACE, msg="Expected '}' after block"),
    action=_build_block,
)


# ---------------------------------------------------------------------------
# Expressions
#   assignment -> or_expr -> and_expr -> equality -> comparison -> range_expr
#     -> term -> factor -> unary -> postfix -> primary
#   (see the module docstring for the full precedence chain; the section
#   below builds each level from the next one down)
# ---------------------------------------------------------------------------

def _build_binop(op_tok, left, right):
    """Shared `build` callback passed to every `chainl(...)` in the
    precedence chain below - turns one matched operator token plus its two
    already-parsed operands into a `BinaryExpr` node."""
    return Node("BinaryExpr", {"op": op_tok.lexeme, "left": left, "right": right})


def _assignment_fn(ps, committed):
    """`assignment := or_expr ('=' assignment)?` - right-associative (the
    recursive call to `assignment` on the right, rather than looping), and
    the left-hand side is only *validated* as an assignable target later, by
    semantics.py - the grammar accepts any `or_expr` there so that e.g.
    `a[i] = x` and `a.field = x` parse the same way as a plain identifier
    target."""
    left = Ref("or_expr").run(ps, committed)
    save = ps.pos
    try:
        Term(TT.ASSIGN_OP).run(ps, False)
    except Fail:
        ps.pos = save
        return left
    value = Ref("assignment", fail_msg="Expected expression").run(ps, True)
    return Node("AssignExpr", {"target": left, "value": value})


GRAMMAR["assignment"] = Rule(_assignment_fn)
GRAMMAR["expression"] = Ref("assignment")

GRAMMAR["or_expr"] = chainl(Ref("and_expr"), Term(TT.LOGIC_OP, "||"), _build_binop)
GRAMMAR["and_expr"] = chainl(Ref("equality"), Term(TT.LOGIC_OP, "&&"), _build_binop)
GRAMMAR["equality"] = chainl(
    Ref("comparison"),
    Alt(Term(TT.REL_OP, "=="), Term(TT.REL_OP, "!=")),
    _build_binop,
)
GRAMMAR["comparison"] = chainl(
    Ref("range_expr"),
    Alt(Term(TT.REL_OP, "<"), Term(TT.REL_OP, ">"), Term(TT.REL_OP, "<="), Term(TT.REL_OP, ">=")),
    _build_binop,
)


def _is_int_literal(node):
    """True if `node` is a bare `INTEGER_LIT` literal - used by
    `_range_expr_fn` to enforce that `..` operands are integer literals,
    not arbitrary integer-typed expressions (a language design choice, see
    CLAUDE.md)."""
    return isinstance(node, Node) and node.kind == "Literal" and node.fields.get("token_type") == TT.INTEGER_LIT


def _range_expr_fn(ps, committed):
    """`range_expr := term ('..' term)*` - `a..b` is inclusive (see
    CLAUDE.md) and, as a language design choice, both operands must be
    integer *literals* (not arbitrary expressions, and not even a variable
    holding an int) - checked here, at parse time, rather than left to
    semantics.py, since it's a purely syntactic literal-shape restriction
    with nothing to type-infer.
    """
    left = Ref("term").run(ps, committed)
    while True:
        save = ps.pos
        try:
            Term(TT.RANGE_OP).run(ps, False)
        except Fail:
            ps.pos = save
            return left
        right = Ref("term").run(ps, True)
        if not _is_int_literal(left):
            ps.error("Range operands must be integer literals")
        if not _is_int_literal(right):
            ps.error("Range operands must be integer literals")
        left = Node("RangeExpr", {"start": left, "end": right})


GRAMMAR["range_expr"] = Rule(_range_expr_fn)

GRAMMAR["term"] = chainl(Ref("factor"), Alt(Term(TT.ARITH_OP, "+"), Term(TT.ARITH_OP, "-")), _build_binop)
GRAMMAR["factor"] = chainl(
    Ref("unary"),
    Alt(Term(TT.ARITH_OP, "*"), Term(TT.ARITH_OP, "/"), Term(TT.ARITH_OP, "%")),
    _build_binop,
)

_UNARY_OPS = [(TT.LOGIC_OP, "!"), (TT.ARITH_OP, "-"), (TT.ARITH_OP, "+")]


def _unary_fn(ps, committed):
    """`unary := ('!' | '-' | '+') unary | postfix` - right-recursive, so
    `!!x` and `--x` (unary double-negation, not a decrement operator - this
    language has no `--`) both parse as nested `UnaryExpr`s."""
    for ttype, lex in _UNARY_OPS:
        if ps.check(ttype, lex):
            ps.pos += 1
            operand = Ref("unary", fail_msg="Expected expression").run(ps, True)
            return Node("UnaryExpr", {"op": lex, "operand": operand})
    return Ref("postfix").run(ps, committed)


GRAMMAR["unary"] = Rule(_unary_fn)


def _call_args_inner_fn(ps, committed):
    """The part of a call's argument list *after* the opening `(` has
    already been consumed by the caller - factored out so both
    `argument_list` (a standalone call used by `print`/`input`) and
    `postfix`'s inline call-parsing (`expr(args)`) share it."""
    if ps.check(TT.RPAREN):
        ps.pos += 1
        return []
    args = comma_list(Ref("argument", fail_msg="Expected expression")).run(ps, True)
    Term(TT.RPAREN, msg="Expected ')' after call arguments").run(ps, True)
    return args


GRAMMAR["call_args_inner"] = Rule(_call_args_inner_fn)

GRAMMAR["argument_list"] = Seq(
    Term(TT.LPAREN, msg="Expected '(' before arguments"),
    Cut(),
    Bind("args", Ref("call_args_inner")),
    action=lambda ps, c: c["args"],
)


def _argument_fn(ps, committed):
    """`argument := IDENTIFIER ':' expression | expression` - a named
    argument (`name: value`) is distinguished from a plain expression by a
    two-token lookahead (identifier immediately followed by `:`), since a
    bare identifier is *also* a valid start of an ordinary expression and
    `Alt`-based backtracking would be needlessly expensive here."""
    if ps.check(TT.IDENTIFIER) and ps.pos + 1 < len(ps.tokens) and ps.tokens[ps.pos + 1].ttype == TT.COLON:
        name = ps.current().lexeme
        ps.pos += 2
        value = Ref("expression", fail_msg="Expected expression").run(ps, True)
        return Node("NamedArg", {"name": name, "value": value})
    return Ref("expression", fail_msg="Expected expression").run(ps, committed)


GRAMMAR["argument"] = Rule(_argument_fn)


def _postfix_fn(ps, committed):
    """`postfix := primary ( '(' args ')' | '[' expression ']' | '.' IDENTIFIER )*`
    - call, index, and member-access all chain onto the same base
    expression, left-to-right, so `f(x)[0].field` parses as nested
    CallExpr/IndexExpr/MemberExpr wrapping the previous result each time."""
    expr = Ref("primary", fail_msg="Expected expression").run(ps, committed)
    while True:
        if ps.check(TT.LPAREN):
            ps.pos += 1
            args = Ref("call_args_inner").run(ps, True)
            expr = Node("CallExpr", {"callee": expr, "args": args})
        elif ps.check(TT.LBRACKET):
            ps.pos += 1
            index = Ref("expression", fail_msg="Expected expression").run(ps, True)
            Term(TT.RBRACKET, msg="Expected ']' after index").run(ps, True)
            expr = Node("IndexExpr", {"object": expr, "index": index})
        elif ps.check(TT.DOT):
            ps.pos += 1
            field_tok = Term(TT.IDENTIFIER, msg="Expected field name after '.'").run(ps, True)
            expr = Node("MemberExpr", {"object": expr, "field": field_tok.lexeme, "op": "."})
        else:
            return expr


GRAMMAR["postfix"] = Rule(_postfix_fn)

_PRIMARY_LITERAL_TYPES = {
    TT.INTEGER_LIT, TT.FLOAT_LIT, TT.STRING_LIT, TT.CHAR_LIT, TT.BOOL_LIT,
}


# ---------------------------------------------------------------------------
# Interpolated strings  (`text {expr} more text`)
#
# The scanner hands us the fully escape-resolved string in tok.attr. We split
# it on unescaped { ... } runs, re-scan+re-parse each embedded expression
# with the same Scanner/GRAMMAR machinery used for the whole file, and lower
# the literal runs into ordinary Literal nodes. Known limitation: because the
# scanner already resolved escapes before we ever see the string, `\{` cannot
# be used to escape a literal brace (see README / LIMITATIONS.md).
# ---------------------------------------------------------------------------

def _split_interp_parts(text, ps, tok):
    """Split an interpolated string's already-escape-resolved text into
    alternating ("lit", text) / ("expr", text) chunks on unescaped `{ ... }`
    runs.

    The brace matching here is a **naive character-level depth counter**
    (`depth` just counts `{`/`}` occurrences) - it has no idea it might be
    inside a nested string literal, so an embedded expression containing its
    own `{`/`}` inside a string (e.g. `` `{f("}")}` ``) will confuse it. This
    is a known, accepted limitation (see LIMITATIONS.md), not a bug to fix
    here.
    """
    parts = []
    buf = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "{":
            if buf:
                parts.append(("lit", "".join(buf)))
                buf = []
            j = i + 1
            depth = 1
            expr_chars = []
            while j < n and depth > 0:
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                expr_chars.append(text[j])
                j += 1
            if depth != 0:
                ps.error(f"Unterminated interpolation '{{' in string {tok.lexeme}")
                return parts
            parts.append(("expr", "".join(expr_chars)))
            i = j + 1
        else:
            buf.append(ch)
            i += 1
    if buf:
        parts.append(("lit", "".join(buf)))
    return parts


def _parse_embedded_expr(text, ps, tok):
    """Parse one `{expr}` chunk's raw text as a standalone expression, by
    re-entering the Scanner and the GRAMMAR table recursively - the embedded
    expression is lexed and parsed exactly as if it were its own tiny source
    file, in a *fresh* `ParseState` (`sub_ps`) so its own error/position
    bookkeeping never touches the enclosing parse (`ps`). Any failure at any
    stage (lex error, parse error, or leftover unconsumed tokens) is
    reported as one error on the *outer* `ps` and replaced with a harmless
    empty-string literal, so one bad interpolation doesn't abort parsing the
    rest of the enclosing source file.
    """
    if not text.strip():
        ps.error(f"Empty interpolation '{{}}' in string {tok.lexeme}")
        return Node("Literal", {"token_type": TT.STRING_LIT, "lexeme": "", "value": ""})

    sub_tokens, lex_errors = Scanner(text).scan_all()
    if lex_errors:
        ps.error(f"Invalid interpolated expression {{{text}}} in string {tok.lexeme}: {lex_errors[0].message}")
        return Node("Literal", {"token_type": TT.STRING_LIT, "lexeme": "", "value": ""})

    sub_ps = ParseState(sub_tokens, GRAMMAR)
    try:
        expr_node = GRAMMAR["expression"].run(sub_ps, True)
    except HardFail:
        expr_node = None
    if expr_node is None or sub_ps.errors or not sub_ps.at_end():
        ps.error(f"Invalid interpolated expression {{{text}}} in string {tok.lexeme}")
        return Node("Literal", {"token_type": TT.STRING_LIT, "lexeme": "", "value": ""})
    return expr_node


def _build_interp_string_node(ps, tok):
    """Assemble the final `InterpString` node: walk the (lit, expr) chunks
    from `_split_interp_parts`, lowering each literal run into a plain
    `Literal(STRING_LIT)` node and each expression run through
    `_parse_embedded_expr`."""
    parts = []
    for kind, text in _split_interp_parts(tok.attr, ps, tok):
        if kind == "lit":
            parts.append(Node("Literal", {"token_type": TT.STRING_LIT, "lexeme": text, "value": text}))
        else:
            parts.append(_parse_embedded_expr(text, ps, tok))
    return Node("InterpString", {"parts": parts})


def _primary_fn(ps, committed):
    """`primary := literal | INTERP_STRING | IDENTIFIER | '(' expression ')' | match_expression`
    - the base case of the whole expression grammar; everything above this
    (`postfix`, `unary`, ... `assignment`) is built out of chaining and
    wrapping these atoms."""
    tok = ps.current()

    if tok.ttype == TT.ERROR:
        # The scanner already recorded a LexError for this token; consuming
        # it here (rather than leaving it for the caller to trip over again)
        # lets parsing keep advancing instead of getting stuck re-reporting
        # the same bad token.
        ps.pos += 1
        raise HardFail()

    if tok.ttype == TT.INTERP_STRING:
        ps.pos += 1
        return _build_interp_string_node(ps, tok)

    if tok.ttype in _PRIMARY_LITERAL_TYPES:
        ps.pos += 1
        return Node("Literal", {"token_type": tok.ttype, "lexeme": tok.lexeme, "value": tok.attr})

    if tok.ttype == TT.IDENTIFIER:
        ps.pos += 1
        return Node("Identifier", {"name": tok.lexeme})

    if ps.check(TT.LPAREN):
        ps.pos += 1
        expr = Ref("expression", fail_msg="Expected expression").run(ps, True)
        Term(TT.RPAREN, msg="Expected ')' after expression").run(ps, True)
        return Node("Grouping", {"expression": expr})

    if ps.check(TT.KEYWORD, "match"):
        return Ref("match_expression").run(ps, True)

    raise Fail()  # nothing here looks like the start of any expression


GRAMMAR["primary"] = Rule(_primary_fn)


def _make_paren_expr(label):
    """Factory for the common `'(' expression ')'` shape shared by `if`,
    `while`, `until`, and `guard` conditions - `label` only affects error
    message wording (e.g. "Expected '(' before if condition")."""
    def fn(ps, committed):
        Term(TT.LPAREN, msg=f"Expected '(' before {label}").run(ps, True)
        expr = Ref("expression", fail_msg="Expected expression").run(ps, True)
        Term(TT.RPAREN, msg=f"Expected ')' after {label}").run(ps, True)
        return expr

    return Rule(fn)


def _pattern_fn(ps, committed):
    """`pattern := '_' | expression` - a match arm's pattern is either the
    wildcard or an arbitrary expression; how that expression is actually
    matched against the subject (range test, predicate, or equality) is
    decided later, per-pattern, in `_match_cases_fn` below and in ir.py's
    `_gen_match_test` - the grammar itself doesn't need to know which."""
    save = ps.pos
    try:
        Term(TT.UNDERSCORE).run(ps, False)
    except Fail:
        ps.pos = save
    else:
        return Node("WildcardPattern")
    return Ref("expression", fail_msg="Expected expression").run(ps, committed)


GRAMMAR["pattern"] = Rule(_pattern_fn)


def _match_cases_fn(label, value_key):
    """Shared body for match-as-statement and match-as-expression case lists.
    value_key is "body" for statements, "value" for expressions.

    One closure serves both `match_statement` and `match_expression` (see
    the two thin wrappers right below this) because the two forms are
    identical apart from what follows `=>` (a statement vs. an expression)
    and whether a trailing comma is allowed between arms (only the
    expression form allows/expects one, since expression-form arms read
    like a list of cases rather than a sequence of statements).
    """

    def fn(ps, committed):
        if not ps.check(TT.KEYWORD, "match"):
            raise Fail()
        ps.pos += 1
        subject = _make_paren_expr(f"{label} subject").run(ps, True)
        Term(TT.LBRACE, msg=f"Expected '{{' before {label} cases").run(ps, True)
        cases = []
        seen_wildcard = False
        while not ps.check(TT.RBRACE) and not ps.at_end():
            if seen_wildcard:
                # Grammar-level check (not left to semantics.py) because
                # it's purely about case *order*, independent of any type
                # information: `_` must be the last case, so anything after
                # it can never be reached.
                ps.error(f"Unreachable {label} case after wildcard '_'")
            pattern = Ref("pattern", fail_msg="Expected pattern").run(ps, True)
            Term(TT.MATCH_ARROW, msg=f"Expected '=>' in {label} case").run(ps, True)
            if value_key == "body":
                value = Ref("statement", fail_msg="Expected statement").run(ps, True)
            else:
                value = Ref("expression", fail_msg="Expected expression").run(ps, True)
                save = ps.pos
                try:
                    Term(TT.COMMA).run(ps, False)
                except Fail:
                    ps.pos = save
            if pattern.kind == "WildcardPattern":
                seen_wildcard = True
            cases.append(Node("MatchCase", {"pattern": pattern, value_key: value}))
        Term(TT.RBRACE, msg=f"Expected '}}' after {label}").run(ps, True)
        return subject, cases

    return fn


def _match_stmt_fn(ps, committed):
    """Thin wrapper: run the shared `_match_cases_fn` closure in its
    statement form and wrap the result in a `MatchStmt` node."""
    subject, cases = _match_cases_fn("match statement", "body")(ps, committed)
    return Node("MatchStmt", {"subject": subject, "cases": cases})


def _match_expr_fn(ps, committed):
    """Thin wrapper: run the shared `_match_cases_fn` closure in its
    expression form and wrap the result in a `MatchExpr` node."""
    subject, cases = _match_cases_fn("match expression", "value")(ps, committed)
    return Node("MatchExpr", {"subject": subject, "cases": cases})


GRAMMAR["match_expression"] = Rule(_match_expr_fn)
GRAMMAR["match_statement"] = Rule(_match_stmt_fn)


# ---------------------------------------------------------------------------
# Statements
#   statement := block | if_stmt | for_stmt | while_stmt | repeat_stmt
#              | return_stmt | loop_control_stmt | builtin_stmt
#              | match_statement | guard_stmt | try_stmt | throw_stmt
#              | multi_assign_stmt | expr_stmt
#   (tried in this order via Alt - see GRAMMAR["statement"] at the bottom of
#   this section; multi_assign_stmt and expr_stmt both start with a type-ish
#   token or an expression, so they're deliberately tried last)
# ---------------------------------------------------------------------------

def _if_stmt_fn(ps, committed):
    """`if_stmt := 'if' '(' expression ')' block ('else' (if_stmt | block))?`
    - `else if` is just `else` followed by a nested `if_stmt` (recursive
    call), which is why an `if/else if/else if/.../else` chain builds a
    right-leaning tree of nested `IfStmt`s rather than a flat list."""
    if not ps.check(TT.KEYWORD, "if"):
        raise Fail()
    ps.pos += 1
    condition = _make_paren_expr("if condition").run(ps, True)
    then_branch = Ref("block", fail_msg="Expected '{' before block").run(ps, True)
    else_branch = None
    save = ps.pos
    try:
        Kw("else").run(ps, False)
    except Fail:
        ps.pos = save
    else:
        if ps.check(TT.KEYWORD, "if"):
            else_branch = Ref("if_stmt").run(ps, True)
        else:
            else_branch = Ref("block", fail_msg="Expected '{' before block").run(ps, True)
    return Node("IfStmt", {"condition": condition, "then": then_branch, "else": else_branch})


GRAMMAR["if_stmt"] = Rule(_if_stmt_fn)


def _for_stmt_fn(ps, committed):
    """Two unrelated statements share the `for` keyword:
      for_in_stmt := 'for' '(' IDENTIFIER 'in' expression ')' block
      for_stmt    := 'for' '(' expression? ';' expression? ';' expression? ')' block
    Disambiguated by a 2-token lookahead (`IDENTIFIER` immediately followed
    by the `in` keyword) right after `(`, since both shapes could otherwise
    start with an identifier expression.
    """
    if not ps.check(TT.KEYWORD, "for"):
        raise Fail()
    ps.pos += 1
    Term(TT.LPAREN, msg="Expected '(' after for").run(ps, True)

    if (
        ps.check(TT.IDENTIFIER)
        and ps.pos + 1 < len(ps.tokens)
        and ps.tokens[ps.pos + 1].ttype == TT.KEYWORD
        and ps.tokens[ps.pos + 1].lexeme == "in"
    ):
        name_tok = Term(TT.IDENTIFIER).run(ps, True)
        Kw("in").run(ps, True)
        iterable = Ref("expression", fail_msg="Expected expression").run(ps, True)
        Term(TT.RPAREN, msg="Expected ')' after for-in clause").run(ps, True)
        body = Ref("block", fail_msg="Expected '{' before block").run(ps, True)
        return Node("ForInStmt", {"name": name_tok.lexeme, "iterable": iterable, "body": body})

    # C-style for: any of the three clauses may be omitted (`for (;;)` is
    # a valid infinite loop), but all three semicolon/paren delimiters are
    # always required.
    init = None if ps.check(TT.SEMICOLON) else Ref("expression", fail_msg="Expected expression").run(ps, True)
    Term(TT.SEMICOLON, msg="Expected ';' after for initializer").run(ps, True)
    condition = None if ps.check(TT.SEMICOLON) else Ref("expression", fail_msg="Expected expression").run(ps, True)
    Term(TT.SEMICOLON, msg="Expected ';' after for condition").run(ps, True)
    update = None if ps.check(TT.RPAREN) else Ref("expression", fail_msg="Expected expression").run(ps, True)
    Term(TT.RPAREN, msg="Expected ')' after for clauses").run(ps, True)
    body = Ref("block", fail_msg="Expected '{' before block").run(ps, True)
    return Node("ForStmt", {"init": init, "condition": condition, "update": update, "body": body})


GRAMMAR["for_stmt"] = Rule(_for_stmt_fn)


def _while_stmt_fn(ps, committed):
    """`while_stmt := 'while' '(' expression ')' block`"""
    if not ps.check(TT.KEYWORD, "while"):
        raise Fail()
    ps.pos += 1
    condition = _make_paren_expr("while condition").run(ps, True)
    body = Ref("block", fail_msg="Expected '{' before block").run(ps, True)
    return Node("WhileStmt", {"condition": condition, "body": body})


GRAMMAR["while_stmt"] = Rule(_while_stmt_fn)


def _repeat_stmt_fn(ps, committed):
    """`repeat_stmt := 'repeat' block 'until' '(' expression ')' ';'`
    - a post-test loop: the body always runs at least once, then the
    condition is checked (note the trailing `;`, which `while`/`for` don't
    have, since `until (...)` reads like a statement in its own right)."""
    if not ps.check(TT.KEYWORD, "repeat"):
        raise Fail()
    ps.pos += 1
    body = Ref("block", fail_msg="Expected '{' before block").run(ps, True)
    Kw("until", msg="Expected 'until' after repeat block").run(ps, True)
    condition = _make_paren_expr("until condition").run(ps, True)
    Term(TT.SEMICOLON, msg="Expected ';' after repeat-until").run(ps, True)
    return Node("RepeatUntilStmt", {"body": body, "condition": condition})


GRAMMAR["repeat_stmt"] = Rule(_repeat_stmt_fn)


def _return_stmt_fn(ps, committed):
    """`return_stmt := 'return' (expression (',' expression)*)? ';'`
    - zero values (bare `return;`, for `void` functions), one value, or
    several (multi-return, lowered by ir.py into a tuple) are all the same
    production; which arity is actually legal for the enclosing function is
    checked by semantics.py, not here."""
    if not ps.check(TT.KEYWORD, "return"):
        raise Fail()
    ps.pos += 1
    values = []
    if not ps.check(TT.SEMICOLON):
        values = comma_list(Ref("expression", fail_msg="Expected expression")).run(ps, True)
    Term(TT.SEMICOLON, msg="Expected ';' after return").run(ps, True)
    return Node("ReturnStmt", {"values": values})


GRAMMAR["return_stmt"] = Rule(_return_stmt_fn)


def _loop_control_stmt_fn(ps, committed):
    """`loop_control_stmt := ('break' | 'continue') ';'` - whether we're
    actually inside a loop at this point is a semantic check
    (semantics.py's `_loop_depth`), not something the grammar tracks."""
    if not (ps.check(TT.KEYWORD, "break") or ps.check(TT.KEYWORD, "continue")):
        raise Fail()
    keyword = ps.current().lexeme
    ps.pos += 1
    Term(TT.SEMICOLON, msg=f"Expected ';' after {keyword}").run(ps, True)
    return Node("LoopControlStmt", {"keyword": keyword})


GRAMMAR["loop_control_stmt"] = Rule(_loop_control_stmt_fn)


def _builtin_stmt_fn(ps, committed):
    """`builtin_stmt := ('print' | 'input') argument_list ';'` - `print`
    and `input` are statements, not ordinary function calls (they're
    keywords, not identifiers, and can't be used as expressions), so they
    get their own grammar rule and AST node kind rather than going through
    `CallExpr`."""
    if not (ps.check(TT.KEYWORD, "print") or ps.check(TT.KEYWORD, "input")):
        raise Fail()
    name = ps.current().lexeme
    ps.pos += 1
    args = Ref("argument_list", fail_msg="Expected '(' before arguments").run(ps, True)
    Term(TT.SEMICOLON, msg=f"Expected ';' after {name} statement").run(ps, True)
    return Node("BuiltinStmt", {"name": name, "args": args})


GRAMMAR["builtin_stmt"] = Rule(_builtin_stmt_fn)


def _guard_stmt_fn(ps, committed):
    """`guard_stmt := 'guard' '(' expression ')' 'else' block` - the
    inverse of `if`: control only ever enters the block when the condition
    is *false* (an early-exit block, conventionally ending in
    return/break/continue/throw, though the grammar doesn't enforce that -
    see LIMITATIONS.md's note on no unreachable-code checking)."""
    if not ps.check(TT.KEYWORD, "guard"):
        raise Fail()
    ps.pos += 1
    condition = _make_paren_expr("guard condition").run(ps, True)
    Kw("else", msg="Expected 'else' after guard condition").run(ps, True)
    else_body = Ref("block", fail_msg="Expected '{' before block").run(ps, True)
    return Node("GuardStmt", {"condition": condition, "else_body": else_body})


GRAMMAR["guard_stmt"] = Rule(_guard_stmt_fn)


def _try_stmt_fn(ps, committed):
    """`try_stmt := 'try' block ('catch' '(' IDENTIFIER ')' block)+ ('finally' block)?`
    - at least one `catch` is required (checked at the end, after the loop,
    since the grammar can't tell in advance how many there'll be); `finally`
    is optional. Note: even though multiple `catch` clauses are accepted and
    each is fully parsed and type-checked, only the *first* is ever actually
    reachable at runtime - see ir.py's `gen_try` and LIMITATIONS.md.
    """
    if not ps.check(TT.KEYWORD, "try"):
        raise Fail()
    ps.pos += 1
    body = Ref("block", fail_msg="Expected '{' before block").run(ps, True)

    catch_clauses = []
    while True:
        save = ps.pos
        try:
            Kw("catch").run(ps, False)
        except Fail:
            ps.pos = save
            break
        Term(TT.LPAREN, msg="Expected '(' after catch").run(ps, True)
        catch_name = Term(TT.IDENTIFIER, msg="Expected catch variable").run(ps, True).lexeme
        Term(TT.RPAREN, msg="Expected ')' after catch variable").run(ps, True)
        catch_body = Ref("block", fail_msg="Expected '{' before block").run(ps, True)
        catch_clauses.append(Node("CatchClause", {"name": catch_name, "body": catch_body}))

    finally_body = None
    save = ps.pos
    try:
        Kw("finally").run(ps, False)
    except Fail:
        ps.pos = save
    else:
        finally_body = Ref("block", fail_msg="Expected '{' before block").run(ps, True)

    if not catch_clauses:
        ps.error("Expected at least one catch clause after try block")

    return Node("TryStmt", {"body": body, "catch_clauses": catch_clauses, "finally_body": finally_body})


GRAMMAR["try_stmt"] = Rule(_try_stmt_fn)


def _throw_stmt_fn(ps, committed):
    """`throw_stmt := 'throw' expression ';'` - the thrown value's type is
    only *inferred*, never checked against `string` here or in
    semantics.py, even though the `catch` variable is always typed `string`
    (see LIMITATIONS.md's note on this gap)."""
    if not ps.check(TT.KEYWORD, "throw"):
        raise Fail()
    ps.pos += 1
    value = Ref("expression", fail_msg="Expected expression").run(ps, True)
    Term(TT.SEMICOLON, msg="Expected ';' after throw").run(ps, True)
    return Node("ThrowStmt", {"value": value})


GRAMMAR["throw_stmt"] = Rule(_throw_stmt_fn)


def _multi_assign_stmt_fn(ps, committed):
    """`multi_assign_stmt := type IDENTIFIER (',' type? IDENTIFIER)* '=' expression (',' expression)* ';'`
    - re-declares one or more *new* locals (each with its own type,
    inheriting the previous one's type when omitted, e.g. `int a, b = f();`)
    and assigns them in one statement; distinct from `let`, which never
    states an explicit type. Recognized by a leading type keyword, which is
    why this is tried before `expr_stmt` in the `statement` Alt below - a
    bare type keyword is never the start of a valid ordinary expression.
    """
    if not (ps.check(TT.KEYWORD) and ps.current().lexeme in TYPE_KEYWORDS):
        raise Fail()
    first_type = Ref("type", fail_msg="Expected type").run(ps, True)
    first_name = Term(TT.IDENTIFIER, msg="Expected identifier after type").run(ps, True).lexeme
    lvalues = [{"type": first_type, "name": first_name}]

    while True:
        save = ps.pos
        try:
            Term(TT.COMMA).run(ps, False)
        except Fail:
            ps.pos = save
            break
        if ps.check(TT.KEYWORD) and ps.current().lexeme in TYPE_KEYWORDS:
            next_type = Ref("type", fail_msg="Expected type").run(ps, True)
        else:
            next_type = first_type
        next_name = Term(TT.IDENTIFIER, msg="Expected identifier in multi-assign").run(ps, True).lexeme
        lvalues.append({"type": next_type, "name": next_name})

    Term(TT.ASSIGN_OP, msg="Expected '=' in multi-assign statement").run(ps, True)
    values = comma_list(Ref("expression", fail_msg="Expected expression")).run(ps, True)
    Term(TT.SEMICOLON, msg="Expected ';' after multi-assign").run(ps, True)
    return Node("MultiAssign", {"lvalues": lvalues, "values": values})


GRAMMAR["multi_assign_stmt"] = Rule(_multi_assign_stmt_fn)


def _expr_stmt_fn(ps, committed):
    """`expr_stmt := expression ';'` - the catch-all: any expression used
    on its own as a statement (almost always an assignment or a call, since
    a bare `1 + 2;` would type-check but do nothing observable)."""
    expr = Ref("expression", fail_msg="Expected expression").run(ps, committed)
    Term(TT.SEMICOLON, msg="Expected ';' after expression").run(ps, True)
    return Node("ExprStmt", {"expression": expr})


GRAMMAR["expr_stmt"] = Rule(_expr_stmt_fn)


GRAMMAR["statement"] = Alt(
    Ref("block"),
    Ref("if_stmt"),
    Ref("for_stmt"),
    Ref("while_stmt"),
    Ref("repeat_stmt"),
    Ref("return_stmt"),
    Ref("loop_control_stmt"),
    Ref("builtin_stmt"),
    Ref("match_statement"),
    Ref("guard_stmt"),
    Ref("try_stmt"),
    Ref("throw_stmt"),
    Ref("multi_assign_stmt"),
    Ref("expr_stmt"),
)


# ---------------------------------------------------------------------------
# Top-level declarations
#   top_level_decl := struct_decl | typedef_decl | global_var_decl
#                   | top_level_function_decl
#   global_var_decl := 'const' type declarator_list ';'   (val/var rejected here)
#   top_level_function_decl := return_type IDENTIFIER '(' param_list ')' block
# ---------------------------------------------------------------------------

def _global_var_decl_fn(ps, committed):
    """A global-scope declaration reuses `var_decl`'s grammar for shape, but
    only `const` is actually valid at global scope - `val`/`var` here still
    parse successfully (so the rest of the file keeps parsing normally) but
    record an error, rather than failing the whole top-level declaration
    outright."""
    if not (ps.check(TT.KEYWORD) and ps.current().lexeme in DECL_KEYWORDS):
        raise Fail()
    if ps.current().lexeme in {"val", "var"}:
        ps.error(f"'{ps.current().lexeme}' declarations are not allowed at global scope")
    return Ref("var_decl").run(ps, True)


GRAMMAR["global_var_decl"] = Rule(_global_var_decl_fn)


def _param_fn(ps, committed):
    """`param := type declarator_name` - a function parameter is a type
    plus a declarator name, which lets a parameter itself be array-typed
    (`int scores[5]`) via the same `merge_type` machinery declarations use."""
    param_type = Ref("type", fail_msg="Expected type").run(ps, True)
    param_name = Ref("declarator_name").run(ps, True)
    return Node("Param", {"name": param_name["name"], "type": merge_type(param_type, param_name)})


GRAMMAR["param"] = Rule(_param_fn)


def _param_list_fn(ps, committed):
    """`param_list := (param (',' param)*)?` - empty parameter lists
    (`()`) are valid, so this checks for the closing `)` before trying
    `comma_list`, which otherwise requires at least one item."""
    if ps.check(TT.RPAREN):
        return []
    return comma_list(Ref("param", fail_msg="Expected parameter type")).run(ps, True)


GRAMMAR["param_list"] = Rule(_param_list_fn)


def _top_level_function_decl_fn(ps, committed):
    """`top_level_function_decl := return_type IDENTIFIER '(' param_list ')' block`

    Records the function name's Token in `ps.fn_name_tokens` so
    `parser.py`'s `validate_program_structure()` can later point a "the last
    function must be named 'main'" error at the actual name token instead of
    a generic end-of-file position.

    If the name isn't followed by `(` at all, this aborts outright
    (`Abort`-style HardFail, not a soft-completed placeholder) rather than
    inventing an empty parameter list to keep parsing with - there's no
    parameter list to sensibly recover with when none was ever written.
    """
    ret_type = Ref("return_type", fail_msg="Expected type").run(ps, committed)
    name_tok = Term(TT.IDENTIFIER, msg="Expected declaration name").run(ps, True)

    if not ps.check(TT.LPAREN):
        ps.error("Expected function parameter list after top-level declaration")
        raise HardFail()
    ps.pos += 1

    params = Ref("param_list").run(ps, True)
    Term(TT.RPAREN, msg="Expected ')' after parameters").run(ps, True)
    body = Ref("block", fail_msg="Expected '{' before block").run(ps, True)

    ps.fn_name_tokens[name_tok.lexeme] = name_tok
    return Node("FunctionDecl", {
        "name": name_tok.lexeme,
        "return_type": ret_type,
        "params": params,
        "body": body,
    })


GRAMMAR["top_level_function_decl"] = Rule(_top_level_function_decl_fn)

GRAMMAR["top_level_decl"] = Alt(
    Ref("struct_decl"),
    Ref("typedef_decl"),
    Ref("global_var_decl"),
    Ref("top_level_function_decl"),
)


# ---------------------------------------------------------------------------
# Program
#   program := top_level_decl*
#   (parser.py's validate_program_structure() then enforces whole-program
#   properties this grammar can't see locally: at least one function, and
#   the last one must be main(): void with no parameters)
# ---------------------------------------------------------------------------

def _build_program(ps, c):
    """Action for `GRAMMAR["program"]`: wrap the whole top-level
    declaration list in the root `Program` node - the AST's actual root,
    handed to `semantics.analyze()`."""
    return Node("Program", {"declarations": c["declarations"]})


GRAMMAR["program"] = Seq(
    Bind("declarations", many_rec(
        Ref("top_level_decl", fail_msg="Expected type"),
        is_stop=lambda ps: False,
    )),
    action=_build_program,
)
