import sys
import os
import time
import argparse
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


#  Token types
class TT:
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
    ttype:  str
    lexeme: str
    line:   int
    col:    int
    attr:   Optional[object] = None   # coerced value for literals

    def __str__(self) -> str:
        attr_part = f"  [attr={self.attr!r}]" if self.attr is not None else ""
        return (
            f"({self.ttype:<16} | lexeme={self.lexeme!r:<30} "
            f"| line={self.line:>4}, col={self.col:>4}){attr_part}"
        )


#  Lexical error
@dataclass
class LexError:
    message: str
    line:    int
    col:     int
    context: str = ""

    def __str__(self) -> str:
        ctx = f"  (near: {self.context!r})" if self.context else ""
        return f"[LEXICAL ERROR] Line {self.line}, Col {self.col}: {self.message}{ctx}"


#  Scanner
class Scanner:
    _SPECIAL_CHARS = set(r'!@#$%^&()-_+=[]{}|;:,.<>?/* ')

    def __init__(self, source: str):
        self.source  = source
        self.pos     = 0
        self.line    = 1
        self.col     = 1
        self.tokens: List[Token]   = []
        self.errors: List[LexError] = []

    def _peek(self, offset: int = 0) -> str:
        idx = self.pos + offset
        return self.source[idx] if idx < len(self.source) else "\0"

    def _advance(self) -> str:
        ch = self.source[self.pos]
        self.pos += 1
        if ch == "\n":
            self.line += 1
            self.col   = 1
        else:
            self.col  += 1
        return ch

    def _match(self, expected: str) -> bool:
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
        # Called when we see '.' and next char is a digit
        sl, sc = self.line, self.col
        self._advance() 
        lexeme = ["."]
        while self._peek().isdigit():
            lexeme.append(self._advance())
        lex = "".join(lexeme)
        return Token(TT.FLOAT_LIT, lex, sl, sc, float(lex))

    def _scan_char_lit(self) -> Token:
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
        self._advance()  # consume backslash
        nxt = self._peek()
        escape_map = {
            "n": "\n", "t": "\t", "r": "\r",
            "\\": "\\", "'": "'", '"': '"', "0": "\0"
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
        sl, sc = self.line, self.col
        self._advance()  # consume opening `
        chars = []
        while self.pos < len(self.source):
            ch = self._peek()
            if ch == "`":
                self._advance()
                lex = "`" + "".join(chars) + "`"
                return Token(TT.INTERP_STRING, lex, sl, sc, "".join(chars))
            if ch == "\\":
                chars.append(self._scan_escape_seq())
            else:
                chars.append(ch)
                self._advance()
        self._add_error("Unterminated interpolated string (missing closing `)", sl, sc)
        return Token(TT.ERROR, "`" + "".join(chars), sl, sc)

    def _scan_identifier(self) -> Token:
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
