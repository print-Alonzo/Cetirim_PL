"""Generic PEG-style combinator engine that walks a grammar table (see grammar.py).

This module has zero knowledge of the custom language's grammar. Changing the
language means editing grammar.py; this file should never need to change.

Every combinator implements `.run(ps, committed) -> value`, either returning a
parsed value (possibly advancing ps.pos) or raising one of two exceptions:

  Fail      - backtrackable: nothing was recorded, the caller may try another
              alternative and must restore ps.pos itself.
  HardFail  - committed: an error has already been appended to ps.errors.
              Never caught by Alt/Opt/Star/Plus/And/Not - only many_rec() (or
              the top-level grammar rule) catches it, mirroring the legacy
              hand-written parser's `raise RuntimeError()` + `synchronize()`.

"committed" tracks whether the enclosing Seq has passed a Cut() marker yet.
Before a Cut, a Term/Kw mismatch just backtracks (like the legacy parser's
tentative `check()`/`match()`). After a Cut, a mismatch is recorded as a soft
error and parsing proceeds as if the token were present (like the legacy
parser's `expect()`), so structurally-required delimiters (`;`, `)`, `}`)
don't abort the whole rule over one missing token.
"""

from ast_nodes import ParseError
from scanner import TT


SYNC_KEYWORDS = {
    "const", "val", "var", "let", "if", "for", "while", "repeat",
    "return", "break", "continue", "match", "guard", "try", "throw",
    "struct", "typedef",
}


class Fail(Exception):
    """Backtrackable failure: no error recorded, caller may try alternatives."""


class HardFail(Exception):
    """Committed failure: an error has already been recorded in ps.errors."""


class ParseState:
    def __init__(self, tokens, grammar):
        self.tokens = tokens
        self.grammar = grammar
        self.pos = 0
        self.errors = []
        self.fn_name_tokens = {}
        self._memo = {}

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
        while not self.at_end():
            if self.pos > 0 and self.previous().ttype == TT.SEMICOLON:
                return
            if self.check(TT.RBRACE):
                return
            if self.check(TT.KEYWORD) and self.current().lexeme in SYNC_KEYWORDS:
                return
            self.pos += 1


class Term:
    """Match a single token by type (and optionally exact lexeme)."""

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
    """Marker: everything after this point in the enclosing Seq is committed."""

    def run(self, ps, committed):
        return None


class Emit:
    """Always succeeds; unconditionally records the given error message.

    Useful for constructs that are grammatically parseable but semantically
    disallowed at this position (e.g. a declaration appearing after the first
    statement in a block).
    """

    def __init__(self, msg):
        self.msg = msg

    def run(self, ps, committed):
        ps.error(self.msg)
        return None


class Abort:
    """Always raises HardFail. Optionally records a message first and/or
    consumes one token before raising.

    Mirrors the legacy parser's explicit `self.error(msg); raise
    RuntimeError()` sites, which unconditionally abort the current
    derivation rather than soft-completing it with a placeholder token (e.g.
    a top-level declaration whose name isn't followed by '(' has no sensible
    placeholder parameter list to keep parsing with).
    """

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
    """Adapts a plain function fn(ps, committed) -> value into a combinator.

    An escape hatch for the handful of productions whose imperative shape
    (e.g. "if the next token is X, there are zero of these; otherwise parse
    one-or-more") doesn't compose cleanly out of the declarative primitives.
    """

    def __init__(self, fn):
        self.fn = fn

    def run(self, ps, committed):
        return self.fn(ps, committed)


class Bind:
    """Labels a capture inside a Seq so the action function receives it by name."""

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
    """Ordered choice: try each part in turn, first success wins.

    Each branch is tried tentatively (committed=False) so dispatch never
    records spurious errors. Once a branch commits internally via its own
    Cut(), a HardFail raised from it is final and is not caught here - it
    propagates, matching the legacy parser (once `if` is consumed there is
    no falling back to try parsing a different kind of statement).
    """

    def __init__(self, *parts):
        self.parts = parts

    def run(self, ps, committed):
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
    """Positive lookahead: succeeds without consuming if part would match."""

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
    """Negative lookahead: succeeds without consuming if part would NOT match."""

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
    """Lazy reference into the grammar table by rule name (enables recursive
    and forward-referencing rules).

    If fail_msg is given and this Ref is reached in a committed position, a
    total failure of the target rule (nothing in it matched at all) escalates
    to a HardFail carrying that message instead of silently propagating a
    bare Fail up past a point where backtracking no longer makes sense.
    """

    def __init__(self, name, fail_msg=None):
        self.name = name
        self.fail_msg = fail_msg

    def run(self, ps, committed):
        rule = ps.grammar[self.name]
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
        if len(ps.errors) == start_errors:
            ps._memo[key] = (ps.pos, result)
        return result


def chainl(operand, op_rule, build):
    """Left-associative binary-operator fold: operand (op operand)*.

    Once an operator has matched, the right-hand operand is required (a
    trailing operator with nothing after it is a hard error), mirroring how
    the legacy parser's parse_term/parse_factor etc. behave.
    """

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
                right = operand.run(ps, True)
                left = build(op_tok, left, right)

    return ChainL()


def comma_list(part):
    """One-or-more `part`, separated by commas.

    The first item uses whatever committed state the caller passes in; every
    subsequent item (i.e. once a comma has been matched) is required -
    mirrors the pervasive legacy pattern `while True: items.append(x); if
    not self.match(TT.COMMA): break`.
    """

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
                items.append(part.run(ps, True))

    return CommaList()


def many_rec(part, is_stop):
    """Zero-or-more repetition with error recovery.

    On a hard failure the error is already recorded, so synchronize() and
    keep going - mirrors the legacy parser's top-level and block-level loops
    (try/except RuntimeError: self.synchronize(); maybe skip one token).
    """

    class ManyRec:
        def run(self, ps, committed):
            items = []
            while not is_stop(ps) and not ps.at_end():
                try:
                    items.append(part.run(ps, True))
                except HardFail:
                    ps.synchronize()
                    if not ps.at_end() and not ps.check(TT.RBRACE):
                        ps.pos += 1
            return items

    return ManyRec()


class Engine:
    """Drives a grammar table (see grammar.py) over a token list.

    Contains no language-specific knowledge; the grammar table is the only
    thing that needs to change when the language changes.
    """

    def parse(self, grammar, tokens, start="program"):
        ps = ParseState(tokens, grammar)
        try:
            ast = grammar[start].run(ps, True)
        except HardFail:
            ast = None
        return ast, ps.errors, ps.fn_name_tokens
