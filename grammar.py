# the grammar table for the custom language, as data - edit this file when the syntax changes
# precedence chain: assignment -> or_expr -> and_expr -> equality -> comparison -> range_expr -> term -> factor -> unary -> postfix -> primary

from ast_nodes import Node, merge_type
from scanner import TT, Scanner
from grammar_engine import (
    Term, Kw, Seq, Alt, Star, Plus, Opt, Ref, And, Not, Bind, Cut, Emit,
    Abort, Rule, Fail, HardFail, ParseState, chainl, comma_list, many_rec,
)


TYPE_KEYWORDS = {"int", "float", "char", "string", "bool", "void"}
DECL_KEYWORDS = {"const", "val", "var"}

GRAMMAR = {}


# type := tuple_type | 'struct' IDENTIFIER | TYPE_KEYWORD | IDENTIFIER; declarator_name := IDENTIFIER ('[' expression? ']')*

def _make_type_rule(allow_tuple, allow_void):
    # produces the two type-parsing rules, differing only in what extra forms they allow:
    # plain type (vars/params/fields) forbids tuple types and void, return_type allows both

    def fn(ps, committed):
        if allow_tuple and ps.check(TT.LPAREN):
            # tuple return type: '(' type (',' type)+ ')', at least two elements since a single-parenthesized type is just Grouping, not a tuple
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
            # 'struct' IDENTIFIER, a reference to an already-declared struct (as opposed to typedef's inline struct definition)
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
            # a bare identifier here is a typedef alias reference, whether it actually resolves to one is semantics.py's job, the grammar just knows "this shape is a type"
            name = ps.current().lexeme
            ps.pos += 1
            return Node("Type", {"name": name})

        raise Fail()  # none of the type shapes matched at all

    return Rule(fn)


GRAMMAR["type"] = _make_type_rule(allow_tuple=False, allow_void=False)
GRAMMAR["return_type"] = _make_type_rule(allow_tuple=True, allow_void=True)


def _declarator_name_fn(ps, committed):
    # IDENTIFIER ('[' expression? ']')*  e.g. scores[5], grid[rows][cols], buf[]. returns a plain
    # dict, not a Node - an intermediate result that only feeds ast_nodes.merge_type, never appears in the AST on its own
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


# struct fields, shared by struct_decl and typedef's inline struct form

def _build_struct_field_line(ps, c):
    # one line can declare several fields sharing a type (int x, y;), one Field node per name -
    # position is stamped by hand since this returns a list, so _stamp_position never reaches it
    return [
        Node("Field", {"name": n["name"], "type": merge_type(c["field_type"], n)},
             line=c["field_type"].line, col=c["field_type"].col)
        for n in c["names"]
    ]


GRAMMAR["struct_field_line"] = Seq(
    Bind("field_type", Ref("type", fail_msg="Expected type")),
    Cut(),
    Bind("names", comma_list(Ref("declarator_name"))),
    Term(TT.SEMICOLON, msg="Expected ';' after struct field"),
    action=_build_struct_field_line,
)

# no error recovery here, a malformed field line raises HardFail straight out to the nearest enclosing many_rec
GRAMMAR["struct_fields_body"] = Star(Ref("struct_field_line"))


def _flatten_fields(field_groups):
    # struct_fields_body yields one list-of-Fields per line, flatten into one list
    return [f for group in field_groups for f in group]


# struct_decl := 'struct' IDENTIFIER '{' struct_fields_body '}' ';'

def _build_struct_decl(ps, c):
    return Node("StructDecl", {"name": c["name"].lexeme, "fields": _flatten_fields(c["fields"])})


GRAMMAR["struct_decl"] = Seq(
    # lookahead-gated: `struct IDENTIFIER {` is a declaration, but `struct IDENTIFIER` alone is
    # also a valid return type - without this check, top_level_decl's Alt would never get to try top_level_function_decl
    And(Seq(Kw("struct"), Term(TT.IDENTIFIER), Term(TT.LBRACE))),
    Kw("struct"), Cut(),
    Bind("name", Term(TT.IDENTIFIER, msg="Expected struct name")),
    Term(TT.LBRACE, msg="Expected '{' before struct fields"),
    Bind("fields", Ref("struct_fields_body")),
    Term(TT.RBRACE, msg="Expected '}' after struct body"),
    Term(TT.SEMICOLON, msg="Expected ';' after struct declaration"),
    action=_build_struct_decl,
)


# typedef_decl := 'typedef' (inline_struct_typedef_body | type) IDENTIFIER ';'

def _build_inline_struct_typedef(ps, c):
    # StructDef, distinct from StructDecl since this is an inline definition inside a typedef, which semantics.py's _collect_structs_and_typedefs treats specially
    return Node("StructDef", {"name": c["name"].lexeme, "fields": _flatten_fields(c["fields"])})


GRAMMAR["inline_struct_typedef_body"] = Seq(
    # distinguishes `typedef struct Foo {...} Bar;` (inline definition) from `typedef struct Foo
    # Bar;` (just a reference, handled by the plain `type` alternative) by checking for a `{`
    And(Seq(Kw("struct"), Term(TT.IDENTIFIER), Term(TT.LBRACE))),
    Kw("struct"), Cut(),
    Bind("name", Term(TT.IDENTIFIER, msg="Expected struct name in typedef")),
    Term(TT.LBRACE, msg="Expected '{' before inline struct fields"),
    Bind("fields", Ref("struct_fields_body")),
    Term(TT.RBRACE, msg="Expected '}' after inline struct body"),
    action=_build_inline_struct_typedef,
)


def _build_typedef_decl(ps, c):
    # aliased is either a Type/StructType node or a StructDef (inline struct form),
    # both handled uniformly by semantics.py's typedef resolution
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


# var_decl := ('const' | 'val' | 'var') type declarator_list ';'  (const's list is exactly one declarator)

GRAMMAR["literal_value"] = Rule(lambda ps, committed: _literal_value(ps))


def _literal_value(ps):
    # a bare literal token, used only for const initializers (restricted to literals,
    # not arbitrary expressions, see _const_declarator_fn)
    tok = ps.current()
    if tok.ttype in {TT.INTEGER_LIT, TT.FLOAT_LIT, TT.STRING_LIT, TT.CHAR_LIT, TT.BOOL_LIT}:
        ps.pos += 1
        return Node("Literal", {"token_type": tok.ttype, "lexeme": tok.lexeme, "value": tok.attr})
    raise Fail()


def _initializer_fn(ps, committed):
    # a brace-delimited { expr, expr, ... } list (arrays/structs), or a plain expression
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
    # declarator_name '=' literal_value - unlike val/var, the rhs must be a bare literal,
    # not an arbitrary expression (a deliberate language design choice)
    name_info = Ref("declarator_name").run(ps, True)
    Term(TT.ASSIGN_OP, msg="Expected '=' after const declarator name").run(ps, True)
    value = Ref("literal_value", fail_msg="Expected a literal value (int, float, char, string, or bool) in const declaration").run(ps, True)
    return Node("Declarator", {"name": name_info["name"], "type": None, "initializer": value, "_arrays": name_info["arrays"]})


def _nonconst_declarator_fn(ps, committed):
    # declarator_name ('=' initializer)? - initializer optional (an uninitialized var gets a type-appropriate default at IR-gen time), any expression or initializer list here
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
    # the declarator list isn't static in GRAMMAR["var_decl"]'s Seq below since WHICH declarator
    # rule applies depends on which keyword (const vs val/var) was just matched, so this action parses the rest of the declaration itself
    mutability = c["kw"].lexeme
    base_type = c["type"]
    is_const = mutability == "const"
    declarator_rule = Rule(_const_declarator_fn) if is_const else Rule(_nonconst_declarator_fn)

    if is_const:
        # const only ever declares one name per statement, `const int a = 1, b = 2;` isn't supported
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


# let_stmt := 'let' IDENTIFIER '[' expression? ']' '=' initializer ';' | 'let' IDENTIFIER (',' IDENTIFIER)* '=' expression (',' expression)* ';'

def _let_stmt_fn(ps, committed):
    # two shapes, both starting `let IDENTIFIER`, disambiguated by hand rather than Alt since the
    # destructuring form's name list keeps growing token by token - by the time '[' fails to match after the first name we already know which shape we're in
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

    # destructuring form; a `_` in place of a name discards that position (records None in
    # names, so semantics.py skips declaring a symbol for it) instead of binding it
    names = [first_name.lexeme]
    while True:
        save = ps.pos
        try:
            Term(TT.COMMA).run(ps, False)
        except Fail:
            ps.pos = save
            break
        try:
            Term(TT.UNDERSCORE).run(ps, False)
        except Fail:
            names.append(Term(TT.IDENTIFIER, msg="Expected identifier or '_' after ','").run(ps, True).lexeme)
        else:
            names.append(None)

    Term(TT.ASSIGN_OP, msg="Expected '=' in let declaration").run(ps, True)
    values = comma_list(Ref("expression", fail_msg="Expected expression")).run(ps, True)
    Term(TT.SEMICOLON, msg="Expected ';' after let declaration").run(ps, True)
    return Node("LetDecl", {"names": names, "values": values})


GRAMMAR["let_stmt"] = Rule(_let_stmt_fn)


# block_decl := let_stmt | var_decl (var_decl here is always val/var, see global_var_decl for const)

def _at_block_decl(ps):
    # true if the current token could start a block-level declaration (val/var/let)
    return (ps.check(TT.KEYWORD) and ps.current().lexeme in {"val", "var"}) or ps.check(TT.KEYWORD, "let")


GRAMMAR["block_decl"] = Alt(
    Ref("let_stmt"),
    Ref("var_decl"),
)


# block := '{' block_decl* block_phase2_item* '}', two-phase so every declaration comes before every statement

_BLOCK_DECL_LOOKAHEAD = And(Alt(Kw("val"), Kw("var"), Kw("let")))

GRAMMAR["block_phase2_item"] = Alt(
    Seq(
        # a declaration-shaped token after phase 1 has ended is still grammatically valid but
        # wrong here - Emit() flags it as an error, then Cut()+block_decl parses it anyway so its statements still show up in the AST
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
    # phase1's declarations go straight into declarations; phase2's tagged items are filtered
    # down to just "stmt" for statements (a misplaced "decl" was already flagged as an error above)
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


# expression precedence chain builds here, see the module header for the full order

def _build_binop(op_tok, left, right):
    # shared build callback for every chainl(...) below
    return Node("BinaryExpr", {"op": op_tok.lexeme, "left": left, "right": right})


def _assignment_fn(ps, committed):
    # or_expr ('=' assignment)? - right-associative. the left side is only validated as an
    # assignable target later by semantics.py, so a[i] = x parses the same as a plain identifier target
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
    return isinstance(node, Node) and node.kind == "Literal" and node.fields.get("token_type") == TT.INTEGER_LIT


def _range_expr_fn(ps, committed):
    # term ('..' term)* - a..b is inclusive, and both operands must be integer LITERALS (not
    # arbitrary expressions, not even a variable holding an int), checked here since it's a purely syntactic restriction with nothing to type-infer
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
    # ('!' | '-' | '+') unary | postfix - right-recursive, so !!x and --x (double negation, this language has no decrement operator) both parse as nested UnaryExprs
    for ttype, lex in _UNARY_OPS:
        if ps.check(ttype, lex):
            ps.pos += 1
            operand = Ref("unary", fail_msg="Expected expression").run(ps, True)
            return Node("UnaryExpr", {"op": lex, "operand": operand})
    return Ref("postfix").run(ps, committed)


GRAMMAR["unary"] = Rule(_unary_fn)


def _call_args_inner_fn(ps, committed):
    # the part of a call's arg list after the opening '(' is already consumed, shared by
    # argument_list (print/input) and postfix's inline call parsing (expr(args))
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
    # IDENTIFIER ':' expression | expression - a named arg is told apart from a plain
    # expression by a 2-token lookahead (identifier then ':')
    if ps.check(TT.IDENTIFIER) and ps.pos + 1 < len(ps.tokens) and ps.tokens[ps.pos + 1].ttype == TT.COLON:
        name = ps.current().lexeme
        ps.pos += 2
        value = Ref("expression", fail_msg="Expected expression").run(ps, True)
        return Node("NamedArg", {"name": name, "value": value})
    return Ref("expression", fail_msg="Expected expression").run(ps, committed)


GRAMMAR["argument"] = Rule(_argument_fn)


def _postfix_fn(ps, committed):
    # primary ( '(' args ')' | '[' expression ']' | '.' IDENTIFIER )* chained left-to-right, so f(x)[0].field parses as nested Call/Index/MemberExpr
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


# interpolated strings (`text {expr} more text`): scanner.py already split tok.parts into
# alternating ("lit", text)/("expr", text) chunks, this module just lowers each one

def _parse_embedded_expr(text, ps, tok):
    # parses one {expr} chunk as a standalone expression in a fresh ParseState, so its own
    # error/position bookkeeping never touches the enclosing parse - a bad interpolation just becomes an error plus an empty-string literal
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
    # walks the (lit, expr) chunks the scanner already split out in tok.parts
    parts = []
    for kind, text in tok.parts:
        if kind == "lit":
            parts.append(Node("Literal", {"token_type": TT.STRING_LIT, "lexeme": text, "value": text}))
        else:
            parts.append(_parse_embedded_expr(text, ps, tok))
    return Node("InterpString", {"parts": parts})


def _primary_fn(ps, committed):
    # literal | INTERP_STRING | IDENTIFIER | '(' expression ')' | match_expression, the base case of the whole expression grammar
    tok = ps.current()

    if tok.ttype == TT.ERROR:
        # the scanner already recorded a LexError for this token, consuming it here lets parsing advance instead of getting stuck re-reporting it
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
    # factory for the '(' expression ')' shape shared by if/while/until/guard, label only affects error wording ("Expected '(' before if condition")
    def fn(ps, committed):
        Term(TT.LPAREN, msg=f"Expected '(' before {label}").run(ps, True)
        expr = Ref("expression", fail_msg="Expected expression").run(ps, True)
        Term(TT.RPAREN, msg=f"Expected ')' after {label}").run(ps, True)
        return expr

    return Rule(fn)


def _pattern_fn(ps, committed):
    # '_' | expression - wildcard or an arbitrary expression, how it's actually matched is
    # decided later per-pattern in semantics.py's _analyze_pattern and ir.py's _gen_match_test
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
    # shared body for match-as-statement and match-as-expression case lists, value_key is
    # "body" for statements, "value" for expressions - only the expression form allows a trailing comma between arms

    def fn(ps, committed):
        if not ps.check(TT.KEYWORD, "match"):
            raise Fail()
        ps.pos += 1
        subject = _make_paren_expr(f"{label} subject").run(ps, True)
        Term(TT.LBRACE, msg=f"Expected '{{' before {label} cases").run(ps, True)
        cases = []
        seen_wildcard = False
        reported_semicolon = False
        while not ps.check(TT.RBRACE) and not ps.at_end():
            if seen_wildcard:
                # grammar-level check since it's purely about case order: `_` must be last
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
                # a ';' here is the statement-form separator used by mistake, consume it (once) so the arm loop stays in sync
                while ps.check(TT.SEMICOLON):
                    if not reported_semicolon:
                        ps.error("match expression cases are separated by "
                                 "',' - ';' is only valid in a match statement")
                        reported_semicolon = True
                    ps.pos += 1
            if pattern.kind == "WildcardPattern":
                seen_wildcard = True
            cases.append(Node("MatchCase", {"pattern": pattern, value_key: value}))
        Term(TT.RBRACE, msg=f"Expected '}}' after {label}").run(ps, True)
        return subject, cases

    return fn


def _match_stmt_fn(ps, committed):
    subject, cases = _match_cases_fn("match statement", "body")(ps, committed)
    return Node("MatchStmt", {"subject": subject, "cases": cases})


def _match_expr_fn(ps, committed):
    subject, cases = _match_cases_fn("match expression", "value")(ps, committed)
    return Node("MatchExpr", {"subject": subject, "cases": cases})


GRAMMAR["match_expression"] = Rule(_match_expr_fn)
GRAMMAR["match_statement"] = Rule(_match_stmt_fn)


# statement := block | if_stmt | for_stmt | while_stmt | repeat_stmt | return_stmt | loop_control_stmt
# | builtin_stmt | match_statement | guard_stmt | try_stmt | throw_stmt | multi_assign_stmt | expr_stmt

def _if_stmt_fn(ps, committed):
    # if_stmt := 'if' '(' expression ')' block ('else' (if_stmt | block))?
    # else-if is just else followed by a recursive if_stmt, building a right-leaning tree of nested IfStmts
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
    # for_in_stmt := 'for' '(' IDENTIFIER 'in' expression ')' block, otherwise C-style for_stmt.
    # disambiguated by a 2-token lookahead (IDENTIFIER then 'in') right after '('
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

    # C-style for: any of the three clauses may be omitted, but all three delimiters are required
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
    # while_stmt := 'while' '(' expression ')' block
    if not ps.check(TT.KEYWORD, "while"):
        raise Fail()
    ps.pos += 1
    condition = _make_paren_expr("while condition").run(ps, True)
    body = Ref("block", fail_msg="Expected '{' before block").run(ps, True)
    return Node("WhileStmt", {"condition": condition, "body": body})


GRAMMAR["while_stmt"] = Rule(_while_stmt_fn)


def _repeat_stmt_fn(ps, committed):
    # repeat_stmt := 'repeat' block 'until' '(' expression ')' ';', a post-test loop
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
    # return_stmt := 'return' (expression (',' expression)*)? ';', arity checked by semantics.py
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
    # loop_control_stmt := ('break' | 'continue') ';', whether inside a loop is checked elsewhere
    if not (ps.check(TT.KEYWORD, "break") or ps.check(TT.KEYWORD, "continue")):
        raise Fail()
    keyword = ps.current().lexeme
    ps.pos += 1
    Term(TT.SEMICOLON, msg=f"Expected ';' after {keyword}").run(ps, True)
    return Node("LoopControlStmt", {"keyword": keyword})


GRAMMAR["loop_control_stmt"] = Rule(_loop_control_stmt_fn)


def _builtin_stmt_fn(ps, committed):
    # builtin_stmt := ('print' | 'input') argument_list ';', keywords not identifiers so they can't be ordinary CallExprs
    if not (ps.check(TT.KEYWORD, "print") or ps.check(TT.KEYWORD, "input")):
        raise Fail()
    name = ps.current().lexeme
    ps.pos += 1
    args = Ref("argument_list", fail_msg="Expected '(' before arguments").run(ps, True)
    Term(TT.SEMICOLON, msg=f"Expected ';' after {name} statement").run(ps, True)
    return Node("BuiltinStmt", {"name": name, "args": args})


GRAMMAR["builtin_stmt"] = Rule(_builtin_stmt_fn)


def _guard_stmt_fn(ps, committed):
    # guard_stmt := 'guard' '(' expression ')' 'else' block, the inverse of if
    if not ps.check(TT.KEYWORD, "guard"):
        raise Fail()
    ps.pos += 1
    condition = _make_paren_expr("guard condition").run(ps, True)
    Kw("else", msg="Expected 'else' after guard condition").run(ps, True)
    else_body = Ref("block", fail_msg="Expected '{' before block").run(ps, True)
    return Node("GuardStmt", {"condition": condition, "else_body": else_body})


GRAMMAR["guard_stmt"] = Rule(_guard_stmt_fn)


def _try_stmt_fn(ps, committed):
    # try_stmt := 'try' block ('catch' '(' IDENTIFIER ')' block)+ ('finally' block)?
    # multiple catches are accepted and type-checked, but only the first is ever reachable at runtime, see ir.py's gen_try
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
        catch_kw = ps.tokens[save]
        Term(TT.LPAREN, msg="Expected '(' after catch").run(ps, True)
        catch_name = Term(TT.IDENTIFIER, msg="Expected catch variable").run(ps, True).lexeme
        Term(TT.RPAREN, msg="Expected ')' after catch variable").run(ps, True)
        catch_body = Ref("block", fail_msg="Expected '{' before block").run(ps, True)
        # built inline rather than as its own rule, so position it from the `catch` keyword by hand
        catch_clauses.append(Node("CatchClause", {"name": catch_name, "body": catch_body},
                                  line=catch_kw.line, col=catch_kw.col))

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
    # throw_stmt := 'throw' expression ';'
    if not ps.check(TT.KEYWORD, "throw"):
        raise Fail()
    ps.pos += 1
    value = Ref("expression", fail_msg="Expected expression").run(ps, True)
    Term(TT.SEMICOLON, msg="Expected ';' after throw").run(ps, True)
    return Node("ThrowStmt", {"value": value})


GRAMMAR["throw_stmt"] = Rule(_throw_stmt_fn)


def _multi_assign_stmt_fn(ps, committed):
    # type (IDENTIFIER|'_') (',' type? (IDENTIFIER|'_'))* '=' expression (',' expression)* ';'
    # declares NEW locals, unlike a plain assignment; tried before expr_stmt since a bare type keyword never starts an ordinary expression
    if not (ps.check(TT.KEYWORD) and ps.current().lexeme in TYPE_KEYWORDS):
        raise Fail()
    first_type = Ref("type", fail_msg="Expected type").run(ps, True)
    try:
        Term(TT.UNDERSCORE).run(ps, False)
    except Fail:
        first_name = Term(TT.IDENTIFIER, msg="Expected identifier after type").run(ps, True).lexeme
    else:
        first_name = None  # `_` discards this slot - see semantics.py's _analyze_multi_assign
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
        try:
            Term(TT.UNDERSCORE).run(ps, False)
        except Fail:
            next_name = Term(TT.IDENTIFIER, msg="Expected identifier or '_' in multi-assign").run(ps, True).lexeme
        else:
            next_name = None
        lvalues.append({"type": next_type, "name": next_name})

    Term(TT.ASSIGN_OP, msg="Expected '=' in multi-assign statement").run(ps, True)
    values = comma_list(Ref("expression", fail_msg="Expected expression")).run(ps, True)
    Term(TT.SEMICOLON, msg="Expected ';' after multi-assign").run(ps, True)
    return Node("MultiAssign", {"lvalues": lvalues, "values": values})


GRAMMAR["multi_assign_stmt"] = Rule(_multi_assign_stmt_fn)


def _expr_stmt_fn(ps, committed):
    # expr_stmt := expression ';'  the catch-all: any expression used alone as a statement
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


# top_level_decl := struct_decl | typedef_decl | global_var_decl | top_level_function_decl

def _global_var_decl_fn(ps, committed):
    # reuses var_decl's grammar for shape, but only const is valid at global scope - val/var here
    # still parse fine (so the rest of the file keeps going) but record an error instead of failing outright
    if not (ps.check(TT.KEYWORD) and ps.current().lexeme in DECL_KEYWORDS):
        raise Fail()
    if ps.current().lexeme in {"val", "var"}:
        ps.error(f"'{ps.current().lexeme}' declarations are not allowed at global scope")
    return Ref("var_decl").run(ps, True)


GRAMMAR["global_var_decl"] = Rule(_global_var_decl_fn)


def _param_fn(ps, committed):
    # param := type declarator_name ('=' literal_value)?, default value restricted to a bare
    # literal like const initializers are; whether defaults must be trailing is checked by semantics.py
    param_type = Ref("type", fail_msg="Expected type").run(ps, True)
    param_name = Ref("declarator_name").run(ps, True)
    default = None
    save = ps.pos
    try:
        Term(TT.ASSIGN_OP).run(ps, False)
    except Fail:
        ps.pos = save
    else:
        default = Ref("literal_value", fail_msg="Expected a literal value (int, float, char, string, or bool) as parameter default").run(ps, True)
    return Node("Param", {"name": param_name["name"], "type": merge_type(param_type, param_name), "default": default})


GRAMMAR["param"] = Rule(_param_fn)


def _param_list_fn(ps, committed):
    # param_list := (param (',' param)*)?  empty lists are valid, checked before comma_list
    if ps.check(TT.RPAREN):
        return []
    return comma_list(Ref("param", fail_msg="Expected parameter type")).run(ps, True)


GRAMMAR["param_list"] = Rule(_param_list_fn)


def _top_level_function_decl_fn(ps, committed):
    # top_level_function_decl := return_type IDENTIFIER '(' param_list ')' block. if the name
    # isn't followed by '(' at all, aborts outright rather than inventing an empty parameter list
    ret_type = Ref("return_type", fail_msg="Expected type").run(ps, committed)
    name_tok = Term(TT.IDENTIFIER, msg="Expected declaration name").run(ps, True)

    if not ps.check(TT.LPAREN):
        ps.error("Expected function parameter list after top-level declaration")
        raise HardFail()
    ps.pos += 1

    params = Ref("param_list").run(ps, True)
    Term(TT.RPAREN, msg="Expected ')' after parameters").run(ps, True)
    body = Ref("block", fail_msg="Expected '{' before block").run(ps, True)

    ps.fn_name_tokens[name_tok.lexeme] = name_tok  # so parser.py can point a "must be main" error at the right token
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


# program := top_level_decl*, parser.py's validate_program_structure() enforces whole-program rules afterward

def _build_program(ps, c):
    return Node("Program", {"declarations": c["declarations"]})


GRAMMAR["program"] = Seq(
    Bind("declarations", many_rec(
        Ref("top_level_decl", fail_msg="Expected type"),
        is_stop=lambda ps: False,
    )),
    action=_build_program,
)
