# generic PEG parser combinators, walks the grammar table built in grammar.py

from ast_nodes import Node, ParseError
from scanner import TT


def _stamp_position(result, token):
    # only stamp once, a node can bubble up through several nested rules unchanged
    if isinstance(result, Node) and result.line is None:
        result.line = token.line
        result.col = token.col


# keywords that can safely start a new statement/declaration, used by synchronize()
SYNC_KEYWORDS = {
    "const", "val", "var", "let", "if", "for", "while", "repeat",
    "return", "break", "continue", "match", "guard", "try", "throw",
    "struct", "typedef",
}


class Fail(Exception):
    pass  # backtrackable, nothing recorded yet


class HardFail(Exception):
    pass  # committed, an error is already in ps.errors


class ParseState:
    def __init__(self, tokens, grammar):
        self.tokens = tokens
        self.grammar = grammar
        self.pos = 0
        self.errors = []
        self.fn_name_tokens = {}  # name -> token, used to point "must be main" errors at the right spot
        self._memo = {}  # packrat cache: (rule_name, pos) -> (end_pos, result)

    def current(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else self.tokens[-1]

    def previous(self):
        return self.tokens[self.pos - 1]

    def at_end(self):
        return self.current().ttype == TT.EOF

    def check(self, ttype=None, lexeme=None):
        tok = self.current()
        if ttype is not None and tok.ttype != ttype:
            return False
        if lexeme is not None and tok.lexeme != lexeme:
            return False
        return True

    def error(self, message):
        self.errors.append(ParseError(message, self.current()))

    def synchronize(self):
        # skip ahead to a safe restart point: right after a ';', at a '}', or at a new statement keyword
        while not self.at_end():
            if self.pos > 0 and self.previous().ttype == TT.SEMICOLON:
                return
            if self.check(TT.RBRACE):
                return
            if self.check(TT.KEYWORD) and self.current().lexeme in SYNC_KEYWORDS:
                return
            self.pos += 1


class Term:
    def __init__(self, ttype, lexeme=None, msg=None):
        self.ttype = ttype
        self.lexeme = lexeme
        self.msg = msg

    def run(self, ps, committed):
        if ps.check(self.ttype, self.lexeme):
            tok = ps.current()
            ps.pos += 1
            return tok
        if committed:
            ps.error(self.msg or f"Expected {self.lexeme or self.ttype}")
            return ps.current()
        raise Fail()


def Kw(lexeme, msg=None):
    return Term(TT.KEYWORD, lexeme, msg)


class Cut:
    # marker only, Seq.run checks isinstance(part, Cut) to flip committed to True
    def run(self, ps, committed):
        return None


class Emit:
    def __init__(self, msg):
        self.msg = msg

    def run(self, ps, committed):
        ps.error(self.msg)
        return None


class Abort:
    def __init__(self, msg=None, consume=False):
        self.msg = msg
        self.consume = consume

    def run(self, ps, committed):
        if self.consume:
            ps.pos += 1
        if self.msg is not None:
            ps.error(self.msg)
        raise HardFail()


class Rule:
    def __init__(self, fn):
        self.fn = fn

    def run(self, ps, committed):
        start_pos = ps.pos  # grab before fn runs since fn advances ps.pos
        result = self.fn(ps, committed)
        _stamp_position(result, ps.tokens[start_pos] if start_pos < len(ps.tokens) else ps.tokens[-1])
        return result


class Bind:
    # Seq.run checks isinstance(part, Bind) to know which results go into captures
    def __init__(self, name, part):
        self.name = name
        self.part = part

    def run(self, ps, committed):
        return self.part.run(ps, committed)


class Seq:
    def __init__(self, *parts, action=None):
        self.parts = parts
        self.action = action

    def run(self, ps, committed):
        local_committed = committed
        captures = {}
        for part in self.parts:
            if isinstance(part, Cut):
                local_committed = True
                continue
            if isinstance(part, Bind):
                captures[part.name] = part.run(ps, local_committed)
            else:
                part.run(ps, local_committed)
        if self.action is not None:
            return self.action(ps, captures)
        return captures


class Alt:
    def __init__(self, *parts):
        self.parts = parts

    def run(self, ps, committed):
        # each branch is tried tentatively, first one that doesn't Fail wins
        save = ps.pos
        for part in self.parts:
            ps.pos = save
            try:
                return part.run(ps, False)
            except Fail:
                continue
        ps.pos = save
        raise Fail()


class Star:
    def __init__(self, part):
        self.part = part

    def run(self, ps, committed):
        results = []
        while True:
            save = ps.pos
            try:
                results.append(self.part.run(ps, False))
            except Fail:
                ps.pos = save
                return results


class Plus:
    def __init__(self, part):
        self.part = part

    def run(self, ps, committed):
        first = self.part.run(ps, committed)
        rest = Star(self.part).run(ps, committed)
        return [first, *rest]


class Opt:
    def __init__(self, part, default=None):
        self.part = part
        self.default = default

    def run(self, ps, committed):
        save = ps.pos
        try:
            return self.part.run(ps, False)
        except Fail:
            ps.pos = save
            return self.default


class And:
    # positive lookahead, succeeds without consuming if part matches
    def __init__(self, part):
        self.part = part

    def run(self, ps, committed):
        save = ps.pos
        try:
            self.part.run(ps, False)
        except Fail:
            ps.pos = save
            raise
        ps.pos = save
        return True


class Not:
    # negative lookahead, succeeds without consuming if part does NOT match
    def __init__(self, part):
        self.part = part

    def run(self, ps, committed):
        save = ps.pos
        try:
            self.part.run(ps, False)
        except Fail:
            ps.pos = save
            return True
        ps.pos = save
        raise Fail()


class Ref:
    def __init__(self, name, fail_msg=None):
        self.name = name
        self.fail_msg = fail_msg

    def run(self, ps, committed):
        rule = ps.grammar[self.name]
        start_pos = ps.pos
        key = (self.name, ps.pos)
        cached = ps._memo.get(key)
        if cached is not None:
            end_pos, result = cached
            ps.pos = end_pos
            return result
        start_errors = len(ps.errors)
        try:
            result = rule.run(ps, False)
        except Fail:
            if committed:
                ps.error(self.fail_msg or f"Expected {self.name}")
                raise HardFail()
            raise
        _stamp_position(result, ps.tokens[start_pos] if start_pos < len(ps.tokens) else ps.tokens[-1])
        # don't cache a run that recorded an error, it needs to re-run so the error is added every time
        if len(ps.errors) == start_errors:
            ps._memo[key] = (ps.pos, result)
        return result


def chainl(operand, op_rule, build):
    class ChainL:
        def run(self, ps, committed):
            left = operand.run(ps, committed)
            while True:
                save = ps.pos
                try:
                    op_tok = op_rule.run(ps, False)
                except Fail:
                    ps.pos = save
                    return left
                right = operand.run(ps, True)  # operator matched, so a missing rhs is now a real error
                left = build(op_tok, left, right)

    return ChainL()


def comma_list(part):
    class CommaList:
        def run(self, ps, committed):
            items = [part.run(ps, committed)]
            while True:
                save = ps.pos
                try:
                    Term(TT.COMMA).run(ps, False)
                except Fail:
                    ps.pos = save
                    return items
                items.append(part.run(ps, True))  # comma matched, so the next item is required

    return CommaList()


def many_rec(part, is_stop):
    class ManyRec:
        def run(self, ps, committed):
            items = []
            while not is_stop(ps) and not ps.at_end():
                before = ps.pos
                try:
                    items.append(part.run(ps, True))
                except HardFail:
                    ps.synchronize()
                    if not ps.at_end() and not ps.check(TT.RBRACE):
                        ps.pos += 1
                if ps.pos == before and not is_stop(ps) and not ps.at_end():
                    ps.pos += 1  # force progress if synchronize() didn't move the cursor
            return items

    return ManyRec()


class Engine:
    def parse(self, grammar, tokens, start="program"):
        ps = ParseState(tokens, grammar)
        try:
            ast = grammar[start].run(ps, True)
        except HardFail:
            ast = None
        return ast, ps.errors, ps.fn_name_tokens
