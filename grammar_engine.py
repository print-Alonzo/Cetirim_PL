"""Generic PEG-style combinator engine that walks a grammar table (see grammar.py).

Phase handoff: this module (together with `grammar.py`) implements the
Parser phase. It receives the token list produced by `scanner.py`
(`Scanner.scan_all()`), and — driven by the `GRAMMAR` table `grammar.py`
builds out of the primitives defined here — produces the AST (a tree of
`ast_nodes.Node`), a list of `ParseError`s, and a `name -> Token` map for
top-level function names. `parser.py`'s `parse_source()` is the actual
entry point that wires scanner -> `Engine().parse(GRAMMAR, tokens)` ->
`validate_program_structure()`; from there the AST feeds `semantics.py`.

This module has zero knowledge of the custom language's grammar. Changing
the language means editing grammar.py; this file should never need to
change.

Every combinator implements `.run(ps, committed) -> value`, either returning a
parsed value (possibly advancing ps.pos) or raising one of two exceptions:

  Fail      - backtrackable: nothing was recorded, the caller may try another
              alternative and must restore ps.pos itself.
  HardFail  - committed: an error has already been appended to ps.errors.
              Never caught by Alt/Opt/Star/And/Not - only many_rec() (or
              the top-level grammar rule) catches it. This is the same shape
              as a classic recursive-descent parser's "tentative check(),
              committed expect()" split, just expressed as two exception
              types instead of two methods.

"committed" tracks whether the enclosing Seq has passed a Cut() marker yet.
Before a Cut, a Term/Kw mismatch just backtracks (nothing is written to
ps.errors, and the caller - typically an Alt - is free to try a different
alternative from the same position). After a Cut, a mismatch is recorded as
a soft error and parsing proceeds as if the token were present, so
structurally-required delimiters (`;`, `)`, `}`) don't abort the whole rule
over one missing token.

Worked example: `Seq(Kw("if"), Cut(), Term(TT.LPAREN, msg="Expected '('"),
...)` - if the current token isn't the keyword `if`, this whole Seq just
fails to match (some other Alt branch gets a turn). But once `if` *has*
matched, the Cut() flips `committed` to True for everything after it: a
missing `(` is now unambiguously a real syntax error (an "if" was clearly
intended), so it's recorded via ps.error() and parsing carries on as if the
`(` were there, rather than bailing out of parsing the whole if-statement.
"""

from ast_nodes import Node, ParseError
from scanner import TT


def _stamp_position(result, token):
    """If `result` is a Node with no position yet, stamp it from `token`.

    Called from Ref.run/Rule.run so every grammar-produced Node picks up the
    position of the first token it consumed, without grammar.py having to do
    it manually for each action.

    The `result.line is None` guard matters because a single Node routinely
    bubbles up through *several* nested Ref/Rule layers unchanged - e.g. an
    `Identifier` built deep inside `primary` passes back up through
    `postfix`, `unary`, `factor`, `term`, ... each of which is itself a
    `Ref`. Only the innermost, first-successful call actually built the
    node and knows its true starting token; every outer layer must leave an
    already-stamped node alone rather than overwrite it with its own (later)
    idea of where "the rule" started.
    """
    if isinstance(result, Node) and result.line is None:
        result.line = token.line
        result.col = token.col


# Keywords that legitimately begin a new statement or declaration. Used by
# ParseState.synchronize() to find a safe place to resume parsing after a
# HardFail - stopping in front of one of these means the next parse attempt
# starts at a real statement boundary instead of mid-expression.
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
    """Mutable state threaded through every combinator's `.run()` call: the
    token list, the current cursor position, accumulated errors, and a
    packrat memoization cache. One `ParseState` exists per parse (a fresh
    one is also created for each embedded `{expr}` inside an interpolated
    string - see grammar.py's `_parse_embedded_expr`)."""

    def __init__(self, tokens, grammar):
        self.tokens = tokens
        self.grammar = grammar
        self.pos = 0
        self.errors = []
        # Populated by grammar.py's top-level function rule, so
        # parser.py's validate_program_structure() can point a "must be
        # main()" error at the actual function-name token instead of EOF.
        self.fn_name_tokens = {}
        # Packrat memoization cache: (rule_name, pos) -> (end_pos, result).
        # See Ref.run for why an entry that recorded an error is never
        # cached.
        self._memo = {}

    def current(self):
        """The token at `pos`, or the last token (always EOF) once `pos`
        has run past the end - this keeps every other method's "check the
        current token" logic free of bounds-checking."""
        return self.tokens[self.pos] if self.pos < len(self.tokens) else self.tokens[-1]

    def previous(self):
        """The token immediately behind `pos` - used by `synchronize()` to
        test "did we just consume a statement-terminating `;`?"."""
        return self.tokens[self.pos - 1]

    def at_end(self):
        """True once `current()` is the EOF token."""
        return self.current().ttype == TT.EOF

    def check(self, ttype=None, lexeme=None):
        """Peek-only predicate: does the current token match this type
        and/or exact lexeme? Never advances `pos` - this is the primitive
        every `Term`/`Kw` match (and every hand-written lookahead in
        grammar.py) is built out of."""
        tok = self.current()
        if ttype is not None and tok.ttype != ttype:
            return False
        if lexeme is not None and tok.lexeme != lexeme:
            return False
        return True

    def error(self, message):
        """Record a ParseError at the current token. Just appends - it does
        not raise. Raising (via HardFail) is always a separate, deliberate
        step taken by the caller once it has decided the failure is no
        longer backtrackable."""
        self.errors.append(ParseError(message, self.current()))

    def synchronize(self):
        """Error recovery: skip tokens until we're standing at what looks
        like a safe re-entry point, so the next parse attempt (from
        `many_rec`) starts at a real boundary instead of mid-expression.

        Three stop conditions, each a different flavor of "boundary":
          - we just consumed a `;`               (end of the failed statement)
          - we're looking at a `}`                (end of the enclosing block)
          - we're looking at a keyword that only ever starts a new
            statement/declaration (`SYNC_KEYWORDS`)
        Anything else is skipped one token at a time.
        """
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
        """Consume and return the current token if it matches; otherwise
        either raise Fail() (not committed - some enclosing Alt may still
        want to try something else) or, if committed, record a soft error
        and return the current token anyway as a placeholder so the caller
        still gets a Token-shaped value to work with, without actually
        consuming it."""
        if ps.check(self.ttype, self.lexeme):
            tok = ps.current()
            ps.pos += 1
            return tok
        if committed:
            ps.error(self.msg or f"Expected {self.lexeme or self.ttype}")
            return ps.current()
        raise Fail()  # backtrackable: let an enclosing Alt try another branch


def Kw(lexeme, msg=None):
    """Sugar for `Term(TT.KEYWORD, lexeme, msg)` - matching a specific
    reserved word is the overwhelmingly common case."""
    return Term(TT.KEYWORD, lexeme, msg)


class Cut:
    """Marker: everything after this point in the enclosing Seq is
    committed.

    `Cut` does no work of its own - `.run()` is only ever called (and
    returns None, unused) so a `Cut()` instance can sit in a `Seq`'s `parts`
    tuple like any other combinator. Its entire purpose is to be detected
    by `isinstance(part, Cut)` inside `Seq.run`, which is what actually
    flips `local_committed` to True.
    """

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

    For the handful of productions that must unconditionally give up on the
    current derivation rather than soft-completing it with a placeholder
    token: e.g. a top-level declaration whose name isn't followed by `(` -
    there is no sensible placeholder parameter list to invent and keep
    parsing with, so parsing that declaration stops here rather than
    limping on with fabricated structure.
    """

    def __init__(self, msg=None, consume=False):
        self.msg = msg
        self.consume = consume

    def run(self, ps, committed):
        if self.consume:
            ps.pos += 1
        if self.msg is not None:
            ps.error(self.msg)
        raise HardFail()  # committed: an error is already recorded, never backtracked past


class Rule:
    """Adapts a plain function fn(ps, committed) -> value into a combinator.

    An escape hatch for the handful of productions whose imperative shape
    (e.g. "if the next token is X, there are zero of these; otherwise parse
    one-or-more") doesn't compose cleanly out of the declarative primitives.
    """

    def __init__(self, fn):
        self.fn = fn

    def run(self, ps, committed):
        # Captured *before* fn runs (fn is free to advance ps.pos, possibly
        # a lot, before returning) - this is the token the resulting node
        # should be stamped with, i.e. where the rule actually started.
        start_pos = ps.pos
        result = self.fn(ps, committed)
        _stamp_position(result, ps.tokens[start_pos] if start_pos < len(ps.tokens) else ps.tokens[-1])
        return result


class Bind:
    """Labels a capture inside a Seq so the action function receives it by
    name.

    `Bind` itself barely does anything - `.run()` just forwards straight to
    the wrapped part. Its only real job is to carry `.name`, which
    `Seq.run` special-cases via `isinstance(part, Bind)` to decide which
    parts' return values get collected into the `captures` dict passed to
    `action`. An un-`Bind`-ed part in a `Seq` still runs (and can still
    fail/commit) - its return value is simply discarded.
    """

    def __init__(self, name, part):
        self.name = name
        self.part = part

    def run(self, ps, committed):
        return self.part.run(ps, committed)


class Seq:
    """Match `parts` in order, threading `committed` through them, and
    build a result via `action(ps, captures)`.

    `parts` is a mix of plain combinators (matched for their side effect of
    consuming tokens / raising on mismatch; their return value is
    discarded), `Bind(name, part)` (return value captured under `name`),
    and `Cut()` (not a combinator to match at all - a marker that flips
    `committed` for everything after it). If no `action` is given, the raw
    `captures` dict is returned as-is (used by a few grammar.py rules that
    only need the labeled sub-results, not a constructed Node).
    """

    def __init__(self, *parts, action=None):
        self.parts = parts
        self.action = action

    def run(self, ps, committed):
        local_committed = committed
        captures = {}
        for part in self.parts:
            if isinstance(part, Cut):
                # From here on, a mismatch in this Seq is a real error, not
                # a reason to let an enclosing Alt try a different branch.
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
    records spurious errors just from probing which branch applies. Once a
    branch commits internally via its own Cut(), a HardFail raised from
    inside it is final and is *not* caught here - it propagates straight
    out of the Alt. This matches how committing to a choice ought to work:
    once enough of `if (...)` has been consumed to know "this is an
    if-statement", a further error inside it should be reported as a broken
    if-statement, not silently discarded in favor of trying to parse the
    same tokens as some other kind of statement.
    """

    def __init__(self, *parts):
        self.parts = parts

    def run(self, ps, committed):
        save = ps.pos
        for part in self.parts:
            ps.pos = save  # rewind before every attempt, including the first
            try:
                return part.run(ps, False)
            except Fail:
                continue  # this branch didn't apply; try the next one
        ps.pos = save
        raise Fail()  # none of the branches matched - backtrackable, not an error yet


class Star:
    """Zero-or-more repetition: keep matching `part` until it fails.

    Each attempt is tentative (committed=False), so a failed final attempt
    never leaves a recorded error - it just means "no more of these". Relies
    on `part` not being able to match successfully while consuming zero
    tokens; nothing here guards against that case, so a Star wrapping such a
    part would loop forever.
    """

    def __init__(self, part):
        self.part = part

    def run(self, ps, committed):
        results = []
        while True:
            save = ps.pos
            try:
                results.append(self.part.run(ps, False))
            except Fail:
                ps.pos = save  # rewind the failed attempt; keep everything matched so far
                return results


class Plus:
    """One-or-more repetition: `part` (required, using the caller's own
    `committed` state) followed by `Star(part)` for the rest."""

    def __init__(self, part):
        self.part = part

    def run(self, ps, committed):
        first = self.part.run(ps, committed)
        rest = Star(self.part).run(ps, committed)
        return [first, *rest]


class Opt:
    """Optional match: `part` if it matches, else `default` (None unless
    given), with `pos` silently rewound on failure. Always tries `part`
    tentatively regardless of the caller's `committed` state - an absent
    optional thing is never an error."""

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
            raise  # part genuinely doesn't match here - propagate the Fail
        ps.pos = save  # part matched, but lookahead never consumes - rewind anyway
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
            ps.pos = save  # part failed to match - that's exactly what Not wants
            return True
        ps.pos = save  # part matched (still rewound - lookahead never consumes)
        raise Fail()  # ...which means Not itself fails


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
        start_pos = ps.pos
        # Packrat memoization: the same (rule name, position) pair can be
        # attempted more than once during backtracking (e.g. two Alt
        # branches that both eventually try the same sub-rule from the same
        # starting point). Caching turns that from "re-parse from scratch"
        # into "look up what happened last time".
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
                # Backtracking is no longer an option here, but the target
                # rule failed outright (matched nothing) rather than
                # partially - escalate to a real, reported error.
                ps.error(self.fail_msg or f"Expected {self.name}")
                raise HardFail()
            raise
        _stamp_position(result, ps.tokens[start_pos] if start_pos < len(ps.tokens) else ps.tokens[-1])
        # Only cache a clean success. A run that recorded an error (via the
        # committed-Term "soft error, keep going" path) must be re-executed
        # every time it's reached, or a later cache hit would silently skip
        # re-appending that error to ps.errors while still advancing pos as
        # if the error had happened - i.e. the diagnostic would vanish from
        # the final error list even though the same malformed input is
        # parsed again.
        if len(ps.errors) == start_errors:
            ps._memo[key] = (ps.pos, result)
        return result


def chainl(operand, op_rule, build):
    """Left-associative binary-operator fold: operand (op operand)*.

    Once an operator has matched, the right-hand operand is required (a
    trailing operator with nothing after it is a real error - there's no
    other way to interpret an operator token with nothing following it once
    it has already been consumed).
    """

    class ChainL:
        def run(self, ps, committed):
            left = operand.run(ps, committed)
            while True:
                save = ps.pos
                try:
                    # Not committed: failing to find another operator just
                    # means "the chain ends here", not an error.
                    op_tok = op_rule.run(ps, False)
                except Fail:
                    ps.pos = save
                    return left
                # The operator matched, so a missing right-hand operand is
                # now a hard error, not a reason to backtrack.
                right = operand.run(ps, True)
                left = build(op_tok, left, right)

    return ChainL()


def comma_list(part):
    """One-or-more `part`, separated by commas.

    The first item uses whatever committed state the caller passes in; every
    subsequent item (i.e. once a comma has been matched) is required, since
    a trailing comma with nothing after it can only mean a missing item, not
    "the list is over" (an ended list simply has no comma at all).
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
    """Zero-or-more repetition with error recovery: on a HardFail (already
    recorded in ps.errors), synchronize to the next safe boundary and keep
    going, instead of letting one malformed statement/declaration abort
    everything after it.
    """

    class ManyRec:
        def run(self, ps, committed):
            items = []
            while not is_stop(ps) and not ps.at_end():
                try:
                    items.append(part.run(ps, True))
                except HardFail:  # error already recorded by whatever raised it
                    ps.synchronize()
                    # synchronize() can return having consumed zero tokens
                    # (e.g. it was already sitting on a SYNC_KEYWORD or `}`).
                    # Without this extra step-over, the loop would retry
                    # `part` at that exact same position and could HardFail
                    # again immediately, forever. Only step past when it's
                    # safe to: never past EOF, and never past the `}` that
                    # is supposed to end the enclosing block/loop.
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
        """Entry point: parse `tokens` starting from `grammar[start]`
        (default `"program"`), returning `(ast, errors, fn_name_tokens)`.

        `ast` is `None` only if the start rule itself raises HardFail
        outright. For the `"program"` rule specifically this is normally
        unreachable in practice - its body is a `many_rec` loop, which
        already swallows every HardFail internally via `synchronize()` - but
        the same try/except HardFail -> None fallback pattern is used again,
        deliberately, in grammar.py's `_parse_embedded_expr` when parsing a
        single interpolated `{expr}` outside of this method.
        """
        ps = ParseState(tokens, grammar)
        try:
            ast = grammar[start].run(ps, True)
        except HardFail:
            ast = None
        return ast, ps.errors, ps.fn_name_tokens
