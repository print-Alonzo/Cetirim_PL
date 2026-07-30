"""Thin CLI driver for the Parser phase.

Phase handoff: receives source text, runs `scanner.py`'s `Scanner` to get a
token list, then `grammar_engine.py`'s `Engine` (driven by `grammar.py`'s
`GRAMMAR` table) to get an AST, then applies one post-parse check
(`validate_program_structure`) that no single grammar rule can express on
its own. The resulting AST feeds `semantics.py` next (via `ir.py`/
`interpreter.py`, which both call `parse_source()` themselves rather than
duplicating this orchestration).

`parse_source(source)` is the actual entry point every other module calls;
the rest of this file (`format_parse_report`, `main`) only exists to support
running the parser standalone from the command line.
"""

import argparse
import json
import os
import sys
import time

from scanner import Scanner
from ast_nodes import Node, ParseError
from grammar import GRAMMAR
from grammar_engine import Engine


def validate_program_structure(declarations, fn_name_tokens, errors, eof_tok):
    """Enforce whole-program properties the grammar itself can't check
    locally, because no single production ever sees the *last* declaration
    in the file while it's being parsed - only after every top-level
    declaration is collected can we ask "which one came last, and was it
    correct?":

      - at least one function must be declared at all
      - the last function declared must be named `main`
      - `main` must return `void` and take no parameters

    A `main`-name error is reported at that function's own name token (via
    `fn_name_tokens`, populated by grammar.py's `_top_level_function_decl_fn`)
    rather than at `eof_tok`, so the error points somewhere a reader can
    actually look; `eof_tok` is only used for the "no functions at all" case,
    where there is no function token to point at.
    """
    func_decls = [d for d in declarations if d.kind == "FunctionDecl"]
    if not func_decls:
        errors.append(ParseError("Program must define at least one function", eof_tok))
        return

    last_fn = func_decls[-1]
    fn_name = last_fn.fields.get("name")
    fn_tok = fn_name_tokens.get(fn_name, eof_tok)
    if fn_name != "main":
        errors.append(ParseError(
            f"Expected 'main' as the last function declaration, found '{fn_name}'",
            fn_tok,
        ))
        return

    ret = last_fn.fields.get("return_type")
    if not (isinstance(ret, Node) and ret.kind == "Type" and ret.fields.get("name") == "void"):
        errors.append(ParseError("'main' function must have return type 'void'", fn_tok))

    params = last_fn.fields.get("params", [])
    if params:
        errors.append(ParseError("'main' function must have no parameters", fn_tok))


def parse_source(source):
    """Run the Scanner and Parser phases over `source` and return
    `(ast, syntax_errors, lex_errors)`.

    Lexical and syntax errors are returned as two separate lists (rather
    than merged into one) so a caller can distinguish "the characters
    themselves were malformed" from "the token stream didn't fit the
    grammar" - `interpreter.py`/`ir.py` print both, but only lexical errors
    ever prevent the scanner from producing *any* usable tokens, while
    syntax errors can coexist with a partially-usable AST.
    """
    scanner = Scanner(source)
    tokens, lex_errors = scanner.scan_all()
    ast, errors, fn_name_tokens = Engine().parse(GRAMMAR, tokens)
    validate_program_structure(ast.fields["declarations"], fn_name_tokens, errors, tokens[-1])
    return ast, errors, lex_errors


def format_parse_report(filename, ast, syntax_errors, lex_errors, elapsed_ms, show_ast):
    """Render the human-readable report the CLI prints/writes: an error
    summary, optionally every individual error, and optionally the full AST
    as JSON (via `Node.to_dict()`)."""
    lines = [
        f"[Parser] {filename}",
        f"  Lexical errors : {len(lex_errors)}",
        f"  Syntax errors  : {len(syntax_errors)}",
        f"  Parse time     : {elapsed_ms:.3f} ms",
    ]

    if lex_errors:
        lines.append("\nLexical errors:")
        lines.extend(f"  {err}" for err in lex_errors)

    if syntax_errors:
        lines.append("\nSyntax errors:")
        lines.extend(f"  {err}" for err in syntax_errors)

    if show_ast:
        lines.append("\nAST:")
        lines.append(json.dumps(ast.to_dict(), indent=2))
    elif not lex_errors and not syntax_errors:
        lines.append("  Result         : syntax accepted")

    return "\n".join(lines) + "\n"


def main():
    """CLI entry point: `python parser.py <source_file> [-o out] [--ast]`.
    Exits with status `2` if any lexical or syntax error was found -
    mirrored by every other phase's CLI (`scanner.py`, `ir.py`,
    `interpreter.py`) so a shell script can treat "exit 2" as "this file
    doesn't compile" uniformly regardless of which phase caught the
    problem."""
    cli = argparse.ArgumentParser(description="CSC617M Custom Language Parser")
    cli.add_argument("source_file", help="Path to source file to parse")
    cli.add_argument("-o", "--output", help="Write parse report to this file")
    cli.add_argument("--ast", action="store_true", help="Include the parsed AST as JSON")
    args = cli.parse_args()

    try:
        with open(args.source_file, "r", encoding="utf-8") as f:
            source = f.read()
    except FileNotFoundError:
        print(f"Error: file not found: {args.source_file}", file=sys.stderr)
        sys.exit(1)

    start = time.perf_counter()
    ast, syntax_errors, lex_errors = parse_source(source)
    elapsed_ms = (time.perf_counter() - start) * 1000

    report = format_parse_report(
        os.path.basename(args.source_file),
        ast,
        syntax_errors,
        lex_errors,
        elapsed_ms,
        args.ast,
    )

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"[Parser] {os.path.basename(args.source_file)}")
        print(f"  Lexical errors : {len(lex_errors)}")
        print(f"  Syntax errors  : {len(syntax_errors)}")
        print(f"  Output         : {args.output}")
    else:
        print(report)

    if lex_errors or syntax_errors:
        sys.exit(2)


if __name__ == "__main__":
    main()
