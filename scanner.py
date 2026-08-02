"""Lexical analyzer: the first phase of the pipeline.

Phase handoff: receives raw source text (a plain Python `str`) and produces
a flat `List[Token]` (always ending in one `EOF` token) plus a
`List[LexError]`, via `Scanner(source).scan_all()`. `parser.py`'s
`parse_source()` is the only caller in the rest of the pipeline - it feeds
this module's token list straight into `grammar_engine.py`'s `Engine`.

Worked micro-example: the source line `int x = 1;` scans to six tokens:
`KEYWORD("int") IDENTIFIER("x") ASSIGN_OP("=") INTEGER_LIT("1", attr=1)
SEMICOLON(";") EOF("")` - note `INTEGER_LIT`'s `attr` already holds the
decoded Python `int` `1`, not just the source text `"1"`.

See `Scanner`'s class docstring for the position-tracking model and the
error-recovery philosophy every `_scan_*` method follows.
"""

import sys
import os
import time
import argparse
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


#  Token types
class TT:
    """Namespace of token type string constants - a plain class used as a
    grouped set of names (no `enum.Enum`) so a token's `ttype` field can just
    be compared/printed as a bare string, matching every other module's
    string-tag style (see e.g. `ast_nodes.Node.kind`)."""

    # Literals
    INTEGER_LIT   = "INTEGER_LIT"
    FLOAT_LIT     = "FLOAT_LIT"
    CHAR_LIT      = "CHAR_LIT"
    STRING_LIT    = "STRING_LIT"
    INTERP_STRING = "INTERP_STRING"
    BOOL_LIT      = "BOOL_LIT"

    # Identifiers & keywords
    IDENTIFIER    = "IDENTIFIER"
    KEYWORD       = "KEYWORD"

    # Operators (grouped for output clarity)
    ARITH_OP      = "ARITH_OP"       # + - * / %
    REL_OP        = "REL_OP"         # == != < > <= >=
    LOGIC_OP      = "LOGIC_OP"       # && || !
    ASSIGN_OP     = "ASSIGN_OP"      # =
    RANGE_OP      = "RANGE_OP"       # ..
    MATCH_ARROW   = "MATCH_ARROW"    # =>

    # Punctuation / Delimiters
    SEMICOLON     = "SEMICOLON"      # ;
    COMMA         = "COMMA"          # ,
    COLON         = "COLON"          # :
    LPAREN        = "LPAREN"         # (
    RPAREN        = "RPAREN"         # )
    LBRACE        = "LBRACE"         # {
    RBRACE        = "RBRACE"         # }
    LBRACKET      = "LBRACKET"       # [
    RBRACKET      = "RBRACKET"       # ]
    DOT           = "DOT"            # .
    UNDERSCORE    = "UNDERSCORE"     # _

    # Special
    EOF           = "EOF"
    ERROR         = "ERROR"


KEYWORDS = {
    "int", "float", "char", "string", "bool", "void",
    "const", "val", "var", "struct", "let",
    "typedef", "if", "else", "for", "while",
    "repeat", "until", "return", "break", "continue",
    "true", "false", "print", "input", "in",
    "match", "guard", "try", "catch", "finally",
    "throw", "_",
}

# Keywords that are bool literals
BOOL_KEYWORDS = {"true", "false"}


#  Token dataclass
@dataclass
class Token:
    """One scanned token: its type, exact source text (`lexeme`), position,
    and - for literals only - the already-decoded Python value (`attr`:
    a real `int`/`float`/`bool`/unescaped string, not source text the
    parser would have to re-parse). Every later phase (grammar.py's
    `_literal_value`, `_primary_fn`, ...) reads `attr` directly rather than
    parsing `lexeme` a second time."""

    ttype:  str
    lexeme: str
    line:   int
    col:    int
    attr:   Optional[object] = None   # coerced value for literals
    parts:  Optional[list] = None     # INTERP_STRING only: see _scan_interp_string

    def __str__(self) -> str:
        attr_part = f"  [attr={self.attr!r}]" if self.attr is not None else ""
        return (
            f"({self.ttype:<16} | lexeme={self.lexeme!r:<30} "
            f"| line={self.line:>4}, col={self.col:>4}){attr_part}"
        )


#  Lexical error
@dataclass
class LexError:
    """One recorded lexical problem: a message, the position it occurred
    at, and an optional context snippet (the offending text) for display.
    Recording this never stops scanning - see `Scanner`'s docstring for the
    recovery philosophy every `_add_error` call relies on."""

    message: str
    line:    int
    col:     int
    context: str = ""

    def __str__(self) -> str:
        ctx = f"  (near: {self.context!r})" if self.context else ""
        return f"[LEXICAL ERROR] Line {self.line}, Col {self.col}: {self.message}{ctx}"


#  Scanner
class Scanner:
    """Character-by-character lexer (no regex): reads `source` once,
    left-to-right, and produces a flat token list plus a list of lexical
    errors via `scan_all()`.

    Position tracking: `pos` (a plain index into `source`), `line`, and
    `col` are only ever advanced together, and only by `_advance()` - every
    other method that consumes characters goes through it (directly or via
    `_match`), which is what keeps `line`/`col` always in sync with `pos`
    without each caller having to manage them by hand.

    Error-recovery philosophy: the scanner **never stops** on a bad
    character or a malformed literal. Every `_scan_*` method that hits a
    problem calls `_add_error()` (recording a `LexError`, but not raising)
    and then still returns *some* token - usually an `ERROR` token, but
    sometimes a best-effort real one (e.g. an unterminated string still
    returns everything it managed to read). This means one bad token in
    line 3 never prevents every valid token in the rest of the file from
    being reported.
    """

    _SPECIAL_CHARS = set(r'!@#$%^&()-_+=[]{}|;:,.<>?/* ')

    def __init__(self, source: str):
        self.source  = source
        self.pos     = 0
        self.line    = 1
        self.col     = 1
        self.tokens: List[Token]   = []
        self.errors: List[LexError] = []

    def _peek(self, offset: int = 0) -> str:
        """Look at the character `offset` positions ahead of `pos` without
        consuming it. Returns `"\\0"` past the end of `source` - a sentinel
        so every caller can keep comparing against a plain character
        (`self._peek() == "."`, etc.) instead of separately checking bounds
        every time."""
        idx = self.pos + offset
        return self.source[idx] if idx < len(self.source) else "\0"

    def _advance(self) -> str:
        """Consume and return the current character, updating `line`/`col`
        - the *only* place `pos`/`line`/`col` move together (see the class
        docstring), so a newline correctly resets `col` to 1 and bumps
        `line` no matter which `_scan_*` method happens to be consuming it."""
        ch = self.source[self.pos]
        self.pos += 1
        if ch == "\n":
            self.line += 1
            self.col   = 1
        else:
            self.col  += 1
        return ch

    def _match(self, expected: str) -> bool:
        """Consume and return True if the current character is exactly
        `expected`; otherwise leave `pos` untouched and return False - the
        primitive most `_scan_*` methods use to conditionally consume one
        character."""
        if self.pos < len(self.source) and self.source[self.pos] == expected:
            self._advance()
            return True
        return False

    def _peek2(self) -> str:
        # Next two characters as string.
        return self.source[self.pos:self.pos+2]

    def _add_token(self, ttype: str, lexeme: str, start_line: int, start_col: int,
                   attr=None):
        self.tokens.append(Token(ttype, lexeme, start_line, start_col, attr))

    def _add_error(self, msg: str, line: int, col: int, context: str = ""):
        self.errors.append(LexError(msg, line, col, context))

    def _skip_whitespace_and_comments(self):
        """Advance past any run of whitespace, `//` line comments, and
        `/* ... */` block comments before the next token. An unterminated
        block comment is reported (via `_add_error`) but scanning still
        continues from wherever it ran out of input, rather than treating
        the rest of the file as "inside a comment forever"."""
        while self.pos < len(self.source):
            ch = self._peek()

            # Whitespace
            if ch in " \t\r\n":
                self._advance()
                continue

            # Single-line comment
            if ch == "/" and self._peek(1) == "/":
                while self.pos < len(self.source) and self._peek() != "\n":
                    self._advance()
                continue

            # Multi-line comment
            if ch == "/" and self._peek(1) == "*":
                sl, sc = self.line, self.col
                self._advance(); self._advance()   # consume /*
                closed = False
                while self.pos < len(self.source) - 1:
                    if self._peek() == "*" and self._peek(1) == "/":
                        self._advance(); self._advance()
                        closed = True
                        break
                    self._advance()
                if not closed:
                    self._add_error("Unterminated block comment", sl, sc)
                continue

            break

    def _scan_number(self) -> None:
        """Scan a digit-led token: a plain integer, a float (digits, a
        `.` not followed by another `.` - so `0..5` isn't mistaken for a
        float - then more digits), or the digit-letter collision case.

        The collision case (`32abc`): once a digit run is immediately
        followed by a letter/underscore, this keeps consuming the whole
        alphanumeric run, then finds the first non-digit character in it
        and **splits the run into two valid tokens right there** -
        `INTEGER_LIT("32")` + `IDENTIFIER("abc")` - rather than emitting one
        big `ERROR` token. This is what lets scanning (and later phases)
        keep making sense of the rest of the line instead of giving up on
        it entirely.
        """
        sl, sc = self.line, self.col
        lexeme = []

        # Integer part
        while self._peek().isdigit():
            lexeme.append(self._advance())

        # Check for float
        if self._peek() == "." and self._peek(1) != ".":
            lexeme.append(self._advance())
            while self._peek().isdigit():
                lexeme.append(self._advance())
            lex = "".join(lexeme)
            self.tokens.append(Token(TT.FLOAT_LIT, lex, sl, sc, float(lex)))
            return

        lex = "".join(lexeme)

        # Check if a letter immediately follows a digit sequence
        if self._peek().isalpha() or self._peek() == "_":
            bad = list(lex)
            while self._peek().isalnum() or self._peek() == "_":
                bad.append(self._advance())
            bad_lex = "".join(bad)
            for i, c in enumerate(bad_lex):
                if not c.isdigit():
                    self._add_error(
                        f"Invalid token: digit sequence mixed with identifier chars"
                        f"'{bad_lex}'. Treating leading digits as integer and remainder as identifier.",
                        sl, sc + i, bad_lex
                    )
                    digits = bad_lex[:i]
                    rest   = bad_lex[i:]
                    self.tokens.extend([
                        Token(TT.INTEGER_LIT, digits, sl, sc, int(digits)),
                        Token(TT.IDENTIFIER, rest, sl, sc + i),
                    ])
                    return

        self.tokens.append(Token(TT.INTEGER_LIT, lex, sl, sc, int(lex)))

    def _scan_dot_float(self) -> Optional[Token]:
        """Scan a float that starts with `.` (no leading digit, e.g.
        `.5`) - called from `scan_all` only after it's already confirmed
        the character after `.` is a digit, so this never needs to fail."""
        # Called when we see '.' and next char is a digit
        sl, sc = self.line, self.col
        self._advance()
        lexeme = ["."]
        while self._peek().isdigit():
            lexeme.append(self._advance())
        lex = "".join(lexeme)
        return Token(TT.FLOAT_LIT, lex, sl, sc, float(lex))

    def _scan_char_lit(self) -> Token:
        """Scan `'x'`, handling an escape sequence, an empty `''` (an
        error - there's no valid character it could mean), and a missing
        closing `'` (reported but still returns whatever character was
        read, rather than discarding it)."""
        sl, sc = self.line, self.col
        self._advance()  # consume opening '
        if self.pos >= len(self.source):
            self._add_error("Unterminated character literal (EOF)", sl, sc)
            return Token(TT.ERROR, "'", sl, sc)

        ch = self._peek()
        if ch == "\\":
            val = self._scan_escape_seq()
        elif ch == "'":
            self._add_error("Empty character literal", sl, sc)
            self._advance()
            return Token(TT.ERROR, "''", sl, sc)
        else:
            val = ch
            self._advance()

        if self._peek() == "'":
            self._advance()   # consume closing '
        else:
            self._add_error(
                "Unterminated character literal (missing closing ')",
                sl, sc, context=f"'{val}"
            )
        return Token(TT.CHAR_LIT, f"'{val}'", sl, sc, val)

    def _scan_escape_seq(self) -> str:
        """Consume a `\\x` escape sequence and return its decoded
        character (or, for an unrecognized escape, the literal two-character
        text `\\x` unchanged, plus a recorded error).

        This is the only place escape sequences are resolved for plain
        (`"..."`) and char (`'...'`) literals, and for the literal-text
        portions of an interpolated (`` `...` ``) string outside any
        `{...}` - see `_scan_interp_string`, which does *not* call this
        inside a `{...}` region (that's source code, not string data, so it
        gets no escape processing at all - it's re-scanned as real source
        by grammar.py). `{`/`}` are in the escape map specifically so `\\{`
        can produce a literal brace in an interpolated string's literal
        text without starting an expression region.
        """
        self._advance()  # consume backslash
        nxt = self._peek()
        escape_map = {
            "n": "\n", "t": "\t", "r": "\r",
            "\\": "\\", "'": "'", '"': '"', "0": "\0",
            "{": "{", "}": "}",
        }
        if nxt in escape_map:
            self._advance()
            return escape_map[nxt]
        else:
            sl, sc = self.line, self.col
            self._add_error(f"Unknown escape sequence: \\{nxt}", sl, sc)
            self._advance()
            return f"\\{nxt}"

    def _scan_string_lit(self) -> Token:
        """Scan `"..."`, resolving escapes as it goes. A newline before the
        closing `"` ends the literal early (strings can't span lines) and
        is reported as unterminated, same as running out of source
        entirely - either way, whatever characters were read so far are
        preserved in the error's context."""
        sl, sc = self.line, self.col
        self._advance()  # consume opening "
        chars = []
        while self.pos < len(self.source):
            ch = self._peek()
            if ch == '"':
                self._advance()
                lex = '"' + "".join(chars) + '"'
                return Token(TT.STRING_LIT, lex, sl, sc, "".join(chars))
            if ch == "\n":
                break
            if ch == "\\":
                chars.append(self._scan_escape_seq())
            else:
                chars.append(ch)
                self._advance()
        self._add_error(
            "Unterminated string literal (missing closing \")",
            sl, sc, context='"' + "".join(chars)
        )
        return Token(TT.ERROR, '"' + "".join(chars), sl, sc)

    def _scan_interp_string(self) -> Token:
        """Scan `` `...` ``, identical in shape to `_scan_string_lit` but
        delimited by backticks, *and* splits the body into alternating
        literal/expression parts as it goes - this is the only place with
        enough information to do that correctly, since it's the only place
        that still knows which braces came from a `\\{` escape and which
        characters are inside a nested `"..."`/`'...'` literal (where a
        brace or backtick means nothing special).

        Two parallel outputs:
          - `token.attr`/`lexeme` - the whole body as one escape-resolved
            string, computed exactly the way it always was (every
            character outside a `{...}` region goes through
            `_scan_escape_seq`; a `{...}` region contributes its literal
            source text, braces included, unchanged) - so the `*_tokens.txt`
            dumps and `format_output` don't move for any interpolation that
            doesn't actually exercise the two limitations this fixes.
          - `token.parts` - alternating `("lit", resolved_text)` /
            `("expr", raw_source_text)` tuples for grammar.py's
            `_build_interp_string_node` to consume directly, replacing the
            old post-hoc splitter. `expr` text is raw (unescaped) - it's
            source code, not string data, so grammar.py re-scans it as-is;
            resolving escapes in it first (like the old code effectively
            did, since it ran `_scan_escape_seq` over the whole body with
            no brace-awareness at all) could corrupt a nested string
            literal's own escapes before the re-scan ever saw them.

        Brace nesting inside a `{...}` region (e.g. an array literal) is
        tracked via `depth`; a `"..."`/`'...'` literal inside one is
        skipped over character-by-character (its own escapes included, so
        an escaped quote doesn't end it early) without interpretation, so
        neither its braces nor a stray backtick inside it can confuse the
        split - fixing `` `{f("}")}` ``.
        """
        sl, sc = self.line, self.col
        self._advance()  # consume opening `
        chars = []      # whole-body text, for attr/lexeme
        parts = []       # alternating ("lit", ...) / ("expr", ...)
        lit_buf = []
        expr_buf = []
        depth = 0        # 0 = outside any {...}, >0 = nested brace depth inside one

        while self.pos < len(self.source):
            ch = self._peek()

            if depth == 0 and ch == "`":
                self._advance()
                if lit_buf:
                    parts.append(("lit", "".join(lit_buf)))
                text = "".join(chars)
                tok = Token(TT.INTERP_STRING, "`" + text + "`", sl, sc, text)
                tok.parts = parts
                return tok

            if depth == 0 and ch == "{":
                if lit_buf:
                    parts.append(("lit", "".join(lit_buf)))
                    lit_buf = []
                depth = 1
                self._advance()
                continue

            if depth == 0 and ch == "\\":
                resolved = self._scan_escape_seq()
                chars.append(resolved)
                lit_buf.append(resolved)
                continue

            if depth == 0:
                chars.append(ch)
                lit_buf.append(ch)
                self._advance()
                continue

            # depth > 0: inside a `{...}` expression region - raw capture.
            if ch in ('"', "'"):
                quote = ch
                expr_buf.append(ch)
                self._advance()
                while self.pos < len(self.source) and self._peek() not in (quote, "\n"):
                    c2 = self._advance()
                    expr_buf.append(c2)
                    if c2 == "\\" and self.pos < len(self.source):
                        expr_buf.append(self._advance())
                if self.pos < len(self.source) and self._peek() == quote:
                    expr_buf.append(self._advance())
                continue

            if ch == "{":
                depth += 1
                expr_buf.append(ch)
                self._advance()
                continue

            if ch == "}":
                depth -= 1
                self._advance()
                if depth == 0:
                    parts.append(("expr", "".join(expr_buf)))
                    chars.append("{" + "".join(expr_buf) + "}")
                    expr_buf = []
                else:
                    expr_buf.append(ch)
                continue

            expr_buf.append(ch)
            self._advance()

        self._add_error("Unterminated interpolated string (missing closing `)", sl, sc)
        return Token(TT.ERROR, "`" + "".join(chars), sl, sc)

    def _scan_identifier(self) -> Token:
        """Scan a run of alphanumeric/underscore characters and classify
        it: a bool literal (`true`/`false`), the wildcard `_` (its own
        token type, `UNDERSCORE`, not a plain keyword - grammar.py's
        `_pattern_fn` matches on that type directly), any other reserved
        word (`KEYWORD`), or a plain `IDENTIFIER`."""
        sl, sc = self.line, self.col
        lexeme = []
        while self._peek().isalnum() or self._peek() == "_":
            lexeme.append(self._advance())
        lex = "".join(lexeme)

        if lex in BOOL_KEYWORDS:
            return Token(TT.BOOL_LIT, lex, sl, sc, lex == "true")
        if lex in KEYWORDS:
            if lex == "_":
                return Token(TT.UNDERSCORE, lex, sl, sc)
            return Token(TT.KEYWORD, lex, sl, sc)
        return Token(TT.IDENTIFIER, lex, sl, sc)

    # main scan loop
    def scan_all(self) -> Tuple[List[Token], List[LexError]]:
        """Tokenize the entire source: skip whitespace/comments, then
        dispatch on the current character, repeating until EOF. Returns
        `(tokens, errors)` - `tokens` always ends with one `EOF` token, and
        is complete (covers the whole file) even when `errors` is
        non-empty, per the class's recovery philosophy.

        Dispatch order matters and is deliberate:
          - quoted forms (`"`, `'`, `` ` ``) are checked *before*
            identifiers/keywords, since none of their delimiter characters
            could otherwise start an identifier anyway, but checking them
            first keeps the dispatch chain reading top-to-bottom by
            "most specific first".
          - `.` is handled as its own case *before* the two-char operator
            table, because it has three completely different meanings
            depending on context (range `..`, a leading-dot float `.5`, or
            plain member-access `.`) that the generic two-char lookup
            below has no way to disambiguate.
          - the two-character operators (`==`, `!=`, `&&`, ...) are checked
            *before* falling back to the single-character table, so `=`
            alone doesn't get tokenized before `==` gets a chance to match.
        """
        while True:
            self._skip_whitespace_and_comments()
            if self.pos >= len(self.source):
                self.tokens.append(Token(TT.EOF, "", self.line, self.col))
                break

            sl, sc = self.line, self.col
            ch = self._peek()

            # string literal
            if ch == '"':
                self.tokens.append(self._scan_string_lit())
                continue

            # char literal
            if ch == "'":
                self.tokens.append(self._scan_char_lit())
                continue

            # interpolated string
            if ch == "`":
                self.tokens.append(self._scan_interp_string())
                continue

            # identifier / keyword
            if ch.isalpha() or ch == "_":
                self.tokens.append(self._scan_identifier())
                continue

            # numeric: digit-led
            if ch.isdigit():
                self._scan_number()
                continue

            # dot: range (..), float (.digit+), or member (.)
            if ch == ".":
                if self._peek(1) == ".":
                    # Range operator  ..
                    self._advance(); self._advance()
                    self._add_token(TT.RANGE_OP, "..", sl, sc)
                elif self._peek(1).isdigit():
                    self.tokens.append(self._scan_dot_float())
                else:
                    self._advance()
                    self._add_token(TT.DOT, ".", sl, sc)
                continue

            # two-char operators
            two = self._peek2()

            if two == "==":
                self._advance(); self._advance()
                self._add_token(TT.REL_OP, "==", sl, sc); continue
            if two == "!=":
                self._advance(); self._advance()
                self._add_token(TT.REL_OP, "!=", sl, sc); continue
            if two == "<=":
                self._advance(); self._advance()
                self._add_token(TT.REL_OP, "<=", sl, sc); continue
            if two == ">=":
                self._advance(); self._advance()
                self._add_token(TT.REL_OP, ">=", sl, sc); continue
            if two == "&&":
                self._advance(); self._advance()
                self._add_token(TT.LOGIC_OP, "&&", sl, sc); continue
            if two == "||":
                self._advance(); self._advance()
                self._add_token(TT.LOGIC_OP, "||", sl, sc); continue
            if two == "=>":
                self._advance(); self._advance()
                self._add_token(TT.MATCH_ARROW, "=>", sl, sc); continue

            # single-char operators / punctuation
            self._advance()  # consume character

            single_map = {
                "+": (TT.ARITH_OP,  "+"),
                "-": (TT.ARITH_OP,  "-"),
                "*": (TT.ARITH_OP,  "*"),
                "/": (TT.ARITH_OP,  "/"),
                "%": (TT.ARITH_OP,  "%"),
                "<": (TT.REL_OP,    "<"),
                ">": (TT.REL_OP,    ">"),
                "!": (TT.LOGIC_OP,  "!"),
                "=": (TT.ASSIGN_OP, "="),
                ";": (TT.SEMICOLON, ";"),
                ",": (TT.COMMA,     ","),
                ":": (TT.COLON,     ":"),
                "(": (TT.LPAREN,    "("),
                ")": (TT.RPAREN,    ")"),
                "{": (TT.LBRACE,    "{"),
                "}": (TT.RBRACE,    "}"),
                "[": (TT.LBRACKET,  "["),
                "]": (TT.RBRACKET,  "]"),
            }

            if ch in single_map:
                ttype, lex = single_map[ch]
                self._add_token(ttype, lex, sl, sc)
            else:
                self._add_error(f"Unknown symbol: '{ch}'", sl, sc, context=ch)
                self._add_token(TT.ERROR, ch, sl, sc)

        return self.tokens, self.errors


#  Output formatting
def _header(title: str, width: int = 72) -> str:
    """Render a `===`-bordered section title for the CLI report."""
    bar = "=" * width
    return f"\n{bar}\n  {title}\n{bar}\n"


def format_output(
    source:     str,
    tokens:     List[Token],
    errors:     List[LexError],
    filename:   str,
    elapsed_ms: float,
    show_src:   bool = True,
) -> str:
    """Render the full scanner CLI report: an optional source listing, the
    lexical error table, the full token stream table, and summary
    statistics (including a token-type breakdown) - see README.md's
    "Scanner Output Format" for the exact layout this produces."""
    lines = []

    if show_src:
        lines.append(_header(f"SOURCE:  {filename}"))
        for i, ln in enumerate(source.splitlines(), 1):
            lines.append(f"  {i:>4} | {ln}")

    # Error summary
    lines.append(_header(f"LEXICAL ERRORS  ({len(errors)} found)"))
    if errors:
        for e in errors:
            lines.append(f"  {e}")
    else:
        lines.append("  None.")

    # Token table
    non_eof = [t for t in tokens if t.ttype != TT.EOF]
    lines.append(_header(f"TOKEN STREAM  ({len(non_eof)} tokens)"))
    lines.append(
        f"  {'#':>5}  {'TYPE':<16}  {'LEXEME':<32}  {'LINE':>5}  {'COL':>5}  ATTR"
    )
    lines.append("  " + "-" * 80)
    for idx, tok in enumerate(tokens, 1):
        attr_s = repr(tok.attr) if tok.attr is not None else ""
        lines.append(
            f"  {idx:>5}  {tok.ttype:<16}  {tok.lexeme!r:<32}  "
            f"{tok.line:>5}  {tok.col:>5}  {attr_s}"
        )

    # Stats
    lines.append(_header("STATISTICS"))
    from collections import Counter
    ttype_counts = Counter(t.ttype for t in tokens if t.ttype != TT.EOF)
    lines.append(f"  Total tokens  : {len(non_eof)}")
    lines.append(f"  Total errors  : {len(errors)}")
    lines.append(f"  Scan time     : {elapsed_ms:.3f} ms")
    lines.append(f"  Source lines  : {source.count(chr(10)) + 1}")
    lines.append(f"  Source chars  : {len(source)}")
    lines.append("")
    lines.append("  Token type breakdown:")
    for ttype, count in sorted(ttype_counts.items(), key=lambda x: -x[1]):
        lines.append(f"    {ttype:<20}  {count}")

    return "\n".join(lines) + "\n"


#  CLI entry point
def main():
    """CLI entry point: `python scanner.py <source_file> [-o out] [--no-src]`.
    Prints the report to stdout by default, or writes it to `-o`/`--output`
    (still printing a short summary to stdout either way). With no
    `--output`, exits `2` if any lexical error was found - the same
    exit-2-on-error contract every other phase's CLI follows."""
    parser = argparse.ArgumentParser(
        description="CSC617M Custom Language Scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scanner.py prog1_calculator.src
  python scanner.py prog1_calculator.src -o tokens_out.txt
  python scanner.py prog1_calculator.src --no-src -o tokens_out.txt
        """
    )
    parser.add_argument("source_file", help="Path to source file to tokenize")
    parser.add_argument("-o", "--output", help="Write output to this file (file dump mode)")
    parser.add_argument("--no-src", action="store_true",
                        help="Suppress source code echo in output")
    args = parser.parse_args()

    # Read source
    try:
        with open(args.source_file, "r", encoding="utf-8") as f:
            source = f.read()
    except FileNotFoundError:
        print(f"Error: file not found: {args.source_file}", file=sys.stderr)
        sys.exit(1)

    # Scan
    t_start = time.perf_counter()
    scanner = Scanner(source)
    tokens, errors = scanner.scan_all()
    elapsed_ms = (time.perf_counter() - t_start) * 1000

    # Format
    report = format_output(
        source     = source,
        tokens     = tokens,
        errors     = errors,
        filename   = os.path.basename(args.source_file),
        elapsed_ms = elapsed_ms,
        show_src   = not args.no_src,
    )

    # Output mode
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
        # Always echo a short summary to stdout even in file-dump mode
        print(f"[Scanner] {os.path.basename(args.source_file)}")
        print(f"  Tokens  : {len([t for t in tokens if t.ttype != TT.EOF])}")
        print(f"  Errors  : {len(errors)}")
        print(f"  Time    : {elapsed_ms:.3f} ms")
        print(f"  Output  : {args.output}")
        if errors:
            print("\nErrors found:")
            for e in errors:
                print(f"  {e}")
    else:
        print(report)
        if errors:
            sys.exit(2)   # non-zero exit if errors present


if __name__ == "__main__":
    main()
