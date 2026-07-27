"""The grammar table for the custom language, as data.

This is the ONLY file to edit when the language's syntax changes: add a
production to GRAMMAR, wire it into whatever rule should reference it, and
write an action that builds the Node the interpreter expects. grammar_engine.py
(the thing that walks this table) never needs to change.

Each GRAMMAR entry mirrors one (or a small cluster of) parsing method(s) from
the original hand-written recursive-descent parser that this module replaced
(e.g. `if_stmt` corresponds to the old `parse_if_stmt`) - this is noted in
comments for traceability.
"""

from ast_nodes import Node, merge_type
from scanner import TT
from grammar_engine import (
    Term, Kw, Seq, Alt, Star, Plus, Opt, Ref, And, Not, Bind, Cut, Emit,
    Abort, Rule, Fail, HardFail, chainl, comma_list, many_rec,
)


TYPE_KEYWORDS = {"int", "float", "char", "string", "bool", "void"}
DECL_KEYWORDS = {"const", "val", "var"}

GRAMMAR = {}


# ---------------------------------------------------------------------------
# Types & declarators  (parse_type, parse_declarator_name)
# ---------------------------------------------------------------------------

def _make_type_rule(allow_tuple, allow_void):
    def fn(ps, committed):
        if allow_tuple and ps.check(TT.LPAREN):
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
            name = ps.current().lexeme
            ps.pos += 1
            return Node("Type", {"name": name})

        raise Fail()

    return Rule(fn)


GRAMMAR["type"] = _make_type_rule(allow_tuple=False, allow_void=False)
GRAMMAR["return_type"] = _make_type_rule(allow_tuple=True, allow_void=True)


def _declarator_name_fn(ps, committed):
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
            size = None
        else:
            size = Ref("expression", fail_msg="Expected expression").run(ps, True)
        Term(TT.RBRACKET, msg="Expected ']' after array size").run(ps, True)
        arrays.append(size)
    return {"name": name_tok.lexeme, "arrays": arrays}


GRAMMAR["declarator_name"] = Rule(_declarator_name_fn)


# ---------------------------------------------------------------------------
# Struct fields  (shared by struct_decl and typedef's inline struct form)
# ---------------------------------------------------------------------------

def _build_struct_field_line(ps, c):
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

# No error recovery here: legacy has no try/except around this loop, so a
# hard failure inside a field line propagates straight out of the whole
# struct body (all the way to the nearest program/block-level recovery).
GRAMMAR["struct_fields_body"] = Star(Ref("struct_field_line"))


def _flatten_fields(field_groups):
    return [f for group in field_groups for f in group]


# ---------------------------------------------------------------------------
# struct_decl  (parse_struct_decl)
# ---------------------------------------------------------------------------

def _build_struct_decl(ps, c):
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
# typedef_decl  (parse_typedef_decl, _peek_inline_struct)
# ---------------------------------------------------------------------------

def _build_inline_struct_typedef(ps, c):
    return Node("StructDef", {"name": c["name"].lexeme, "fields": _flatten_fields(c["fields"])})


GRAMMAR["inline_struct_typedef_body"] = Seq(
    And(Seq(Kw("struct"), Term(TT.IDENTIFIER), Term(TT.LBRACE))),
    Kw("struct"), Cut(),
    Bind("name", Term(TT.IDENTIFIER, msg="Expected struct name in typedef")),
    Term(TT.LBRACE, msg="Expected '{' before inline struct fields"),
    Bind("fields", Ref("struct_fields_body")),
    Term(TT.RBRACE, msg="Expected '}' after inline struct body"),
    action=_build_inline_struct_typedef,
)


def _build_typedef_decl(ps, c):
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
# var_decl / const_decl  (parse_var_decl, parse_literal, parse_initializer)
# Note: legacy's `require_semicolon` parameter is always called with True at
# both of its call sites, so it is not modeled as a parameter here.
# ---------------------------------------------------------------------------

GRAMMAR["literal_value"] = Rule(lambda ps, committed: _literal_value(ps))


def _literal_value(ps):
    tok = ps.current()
    if tok.ttype in {TT.INTEGER_LIT, TT.FLOAT_LIT, TT.STRING_LIT, TT.CHAR_LIT, TT.BOOL_LIT}:
        ps.pos += 1
        return Node("Literal", {"token_type": tok.ttype, "lexeme": tok.lexeme, "value": tok.attr})
    raise Fail()


def _initializer_fn(ps, committed):
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
    name_info = Ref("declarator_name").run(ps, True)
    Term(TT.ASSIGN_OP, msg="Expected '=' after const declarator name").run(ps, True)
    value = Ref("literal_value", fail_msg="Expected a literal value (int, float, char, string, or bool) in const declaration").run(ps, True)
    return Node("Declarator", {"name": name_info["name"], "type": None, "initializer": value, "_arrays": name_info["arrays"]})


def _nonconst_declarator_fn(ps, committed):
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
    mutability = c["kw"].lexeme
    base_type = c["type"]
    is_const = mutability == "const"
    declarator_rule = Rule(_const_declarator_fn) if is_const else Rule(_nonconst_declarator_fn)

    if is_const:
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
# let_stmt  (parse_let_stmt)
# ---------------------------------------------------------------------------

def _let_stmt_fn(ps, committed):
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
        size = None if ps.check(TT.RBRACKET) else Ref("expression", fail_msg="Expected expression").run(ps, True)
        Term(TT.RBRACKET, msg="Expected ']' after array size").run(ps, True)
        Term(TT.ASSIGN_OP, msg="Expected '=' in let array declaration").run(ps, True)
        initializer = Ref("initializer", fail_msg="Expected expression").run(ps, True)
        Term(TT.SEMICOLON, msg="Expected ';' after let declaration").run(ps, True)
        return Node("LetDecl", {"names": [first_name.lexeme], "array_sizes": [size], "values": [initializer]})

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
# block-level declarations  (parse_block's decl phases, _at_block_decl)
# ---------------------------------------------------------------------------

def _at_block_decl(ps):
    return (ps.check(TT.KEYWORD) and ps.current().lexeme in {"val", "var"}) or ps.check(TT.KEYWORD, "let")


GRAMMAR["block_decl"] = Alt(
    Ref("let_stmt"),
    Ref("var_decl"),
)


# ---------------------------------------------------------------------------
# block  (parse_block)
# ---------------------------------------------------------------------------

_BLOCK_DECL_LOOKAHEAD = And(Alt(Kw("val"), Kw("var"), Kw("let")))

GRAMMAR["block_phase2_item"] = Alt(
    Seq(
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
# Expressions  (parse_expression .. parse_primary, parse_match_expression)
# ---------------------------------------------------------------------------

def _build_binop(op_tok, left, right):
    return Node("BinaryExpr", {"op": op_tok.lexeme, "left": left, "right": right})


def _assignment_fn(ps, committed):
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
    for ttype, lex in _UNARY_OPS:
        if ps.check(ttype, lex):
            ps.pos += 1
            operand = Ref("unary", fail_msg="Expected expression").run(ps, True)
            return Node("UnaryExpr", {"op": lex, "operand": operand})
    return Ref("postfix").run(ps, committed)


GRAMMAR["unary"] = Rule(_unary_fn)


def _call_args_inner_fn(ps, committed):
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
    if ps.check(TT.IDENTIFIER) and ps.pos + 1 < len(ps.tokens) and ps.tokens[ps.pos + 1].ttype == TT.COLON:
        name = ps.current().lexeme
        ps.pos += 2
        value = Ref("expression", fail_msg="Expected expression").run(ps, True)
        return Node("NamedArg", {"name": name, "value": value})
    return Ref("expression", fail_msg="Expected expression").run(ps, committed)


GRAMMAR["argument"] = Rule(_argument_fn)


def _postfix_fn(ps, committed):
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
    TT.INTEGER_LIT, TT.FLOAT_LIT, TT.STRING_LIT, TT.CHAR_LIT, TT.INTERP_STRING, TT.BOOL_LIT,
}


def _primary_fn(ps, committed):
    tok = ps.current()

    if tok.ttype == TT.ERROR:
        ps.pos += 1  # consume so parsing advances past the bad token
        raise HardFail()  # lex error already recorded by the scanner

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

    raise Fail()


GRAMMAR["primary"] = Rule(_primary_fn)


def _make_paren_expr(label):
    def fn(ps, committed):
        Term(TT.LPAREN, msg=f"Expected '(' before {label}").run(ps, True)
        expr = Ref("expression", fail_msg="Expected expression").run(ps, True)
        Term(TT.RPAREN, msg=f"Expected ')' after {label}").run(ps, True)
        return expr

    return Rule(fn)


def _pattern_fn(ps, committed):
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
    subject, cases = _match_cases_fn("match statement", "body")(ps, committed)
    return Node("MatchStmt", {"subject": subject, "cases": cases})


def _match_expr_fn(ps, committed):
    subject, cases = _match_cases_fn("match expression", "value")(ps, committed)
    return Node("MatchExpr", {"subject": subject, "cases": cases})


GRAMMAR["match_expression"] = Rule(_match_expr_fn)
GRAMMAR["match_statement"] = Rule(_match_stmt_fn)


# ---------------------------------------------------------------------------
# Statements  (parse_statement and each parse_*_stmt)
# ---------------------------------------------------------------------------

def _if_stmt_fn(ps, committed):
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
    if not ps.check(TT.KEYWORD, "while"):
        raise Fail()
    ps.pos += 1
    condition = _make_paren_expr("while condition").run(ps, True)
    body = Ref("block", fail_msg="Expected '{' before block").run(ps, True)
    return Node("WhileStmt", {"condition": condition, "body": body})


GRAMMAR["while_stmt"] = Rule(_while_stmt_fn)


def _repeat_stmt_fn(ps, committed):
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
    if not (ps.check(TT.KEYWORD, "break") or ps.check(TT.KEYWORD, "continue")):
        raise Fail()
    keyword = ps.current().lexeme
    ps.pos += 1
    Term(TT.SEMICOLON, msg=f"Expected ';' after {keyword}").run(ps, True)
    return Node("LoopControlStmt", {"keyword": keyword})


GRAMMAR["loop_control_stmt"] = Rule(_loop_control_stmt_fn)


def _builtin_stmt_fn(ps, committed):
    if not (ps.check(TT.KEYWORD, "print") or ps.check(TT.KEYWORD, "input")):
        raise Fail()
    name = ps.current().lexeme
    ps.pos += 1
    args = Ref("argument_list", fail_msg="Expected '(' before arguments").run(ps, True)
    Term(TT.SEMICOLON, msg=f"Expected ';' after {name} statement").run(ps, True)
    return Node("BuiltinStmt", {"name": name, "args": args})


GRAMMAR["builtin_stmt"] = Rule(_builtin_stmt_fn)


def _guard_stmt_fn(ps, committed):
    if not ps.check(TT.KEYWORD, "guard"):
        raise Fail()
    ps.pos += 1
    condition = _make_paren_expr("guard condition").run(ps, True)
    Kw("else", msg="Expected 'else' after guard condition").run(ps, True)
    else_body = Ref("block", fail_msg="Expected '{' before block").run(ps, True)
    return Node("GuardStmt", {"condition": condition, "else_body": else_body})


GRAMMAR["guard_stmt"] = Rule(_guard_stmt_fn)


def _try_stmt_fn(ps, committed):
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
    if not ps.check(TT.KEYWORD, "throw"):
        raise Fail()
    ps.pos += 1
    value = Ref("expression", fail_msg="Expected expression").run(ps, True)
    Term(TT.SEMICOLON, msg="Expected ';' after throw").run(ps, True)
    return Node("ThrowStmt", {"value": value})


GRAMMAR["throw_stmt"] = Rule(_throw_stmt_fn)


def _multi_assign_stmt_fn(ps, committed):
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
# Top-level declarations  (parse_top_level, finish_function_decl)
# ---------------------------------------------------------------------------

def _global_var_decl_fn(ps, committed):
    if not (ps.check(TT.KEYWORD) and ps.current().lexeme in DECL_KEYWORDS):
        raise Fail()
    if ps.current().lexeme in {"val", "var"}:
        ps.error(f"'{ps.current().lexeme}' declarations are not allowed at global scope")
    return Ref("var_decl").run(ps, True)


GRAMMAR["global_var_decl"] = Rule(_global_var_decl_fn)


def _param_fn(ps, committed):
    param_type = Ref("type", fail_msg="Expected type").run(ps, True)
    param_name = Ref("declarator_name").run(ps, True)
    return Node("Param", {"name": param_name["name"], "type": merge_type(param_type, param_name)})


GRAMMAR["param"] = Rule(_param_fn)


def _param_list_fn(ps, committed):
    if ps.check(TT.RPAREN):
        return []
    return comma_list(Ref("param", fail_msg="Expected parameter type")).run(ps, True)


GRAMMAR["param_list"] = Rule(_param_list_fn)


def _top_level_function_decl_fn(ps, committed):
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
# Program  (parse())
# ---------------------------------------------------------------------------

def _build_program(ps, c):
    return Node("Program", {"declarations": c["declarations"]})


GRAMMAR["program"] = Seq(
    Bind("declarations", many_rec(
        Ref("top_level_decl", fail_msg="Expected type"),
        is_stop=lambda ps: False,
    )),
    action=_build_program,
)
