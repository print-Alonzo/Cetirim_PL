# CSC617M Machine Project — Custom Language Interpreter

**Course:** CSC617M — Theory of Programming Languages
**Current Milestone:** Interpreter (complete)

---

## Overview

This repository contains the interpreter project for CSC617M. The group designed a new programming language from scratch and implemented a full interpreter for it in Python. The pipeline is:

**Scanner → Parser → Semantic Analyzer → IR Generator → VM**

All phases are complete: source runs end to end via `python interpreter.py <file>`, which scans, parses, type-checks, lowers to quadruple intermediate code, and executes it.

The language supports typed variable/constant declarations, control flow (if-else, for, while, repeat-until), functions (scalars passed by value, arrays and structs by reference), structs, pattern matching, exception handling, and interpolated strings.

> **This README is the manual for *using the interpreter*.** For how to *write
> programs in the language* — syntax, types, operators and precedence, control
> flow, functions and the parameter-passing scheme, structs, `match`,
> exceptions — see **[`LANGUAGE.md`](LANGUAGE.md)**.

---

## Repository Structure

```
.
├── scanner.py                                  # Lexical analyzer
├── parser.py                                   # CLI driver: scans, runs the grammar engine, formats reports
├── grammar.py                                  # The grammar, as data — edit THIS to change the language
├── grammar_engine.py                           # Generic combinator engine that walks grammar.py — never edit for language changes
├── ast_nodes.py                                # Node / ParseError / merge_type, shared across parser.py, grammar.py, semantics.py
├── semantics.py                                # Semantic analyzer: name resolution, type checking, unique IR names
├── ir.py                                       # Intermediate code generator: AST + SymbolTable -> quadruples
├── interpreter.py                              # VM: executes quadruples; CLI entry point for running a program
├── run_tests.py                                # Golden-output test runner (--update to regenerate goldens)
├── build.py                                    # Packages the runtime modules into cetirim.pyz (stdlib zipapp)
├── cetirim.pyz                                 # Built standalone binary — rebuild with `python build.py`
├── README.md                                   # This file: how to USE the interpreter
├── LANGUAGE.md                                 # Programmer's manual: how to WRITE programs in the language
├── LIMITATIONS.md                              # Design decisions, known edge cases, scope limits
├── docs/                                       # Course handouts (MP Specs.pdf)
├── tests/                                      # 12 feature fixtures (+ golden output) and 17 negative fixtures
├── prog1_calculator.src                        # Sample program 1
├── prog1_calculator_tokens.txt                 # Scanner output for prog1
├── prog1_calculator.in                         # stdin fixture for prog1's input() calls
├── prog1_calculator_ir.txt                     # Committed quadruple dump for prog1
├── prog1_calculator_expected.txt               # Golden program output for prog1
├── prog2_loops_arrays.src                      # Sample program 2
├── prog2_loops_arrays_tokens.txt               # Scanner output for prog2
├── prog2_loops_arrays.in                       # stdin fixture for prog2's input() calls
├── prog2_loops_arrays_ir.txt                   # Committed quadruple dump for prog2
├── prog2_loops_arrays_expected.txt             # Golden program output for prog2
├── prog3_functions.src                         # Sample program 3
├── prog3_functions_tokens.txt                  # Scanner output for prog3
├── prog3_functions_ir.txt                      # Committed quadruple dump for prog3
├── prog3_functions_expected.txt                # Golden program output for prog3
├── prog4_structs_match_exceptions.src          # Sample program 4
├── prog4_structs_match_exceptions_tokens.txt   # Scanner output for prog4
├── prog4_structs_match_exceptions_ir.txt       # Committed quadruple dump for prog4
├── prog4_structs_match_exceptions_expected.txt # Golden program output for prog4
├── prog5_advanced.src                          # Sample program 5
├── prog5_advanced_tokens.txt                   # Scanner output for prog5
├── prog5_advanced_ir.txt                       # Committed quadruple dump for prog5
└── prog5_advanced_expected.txt                 # Golden program output for prog5
```

---

## Requirements

- Python 3.8 or higher
- No external dependencies — standard library only

---

## Running the Scanner

```bash
# Print token stream to stdout
python scanner.py <source_file>

# Write token stream to a file
python scanner.py <source_file> -o <output_file>

# Suppress source code echo
python scanner.py <source_file> --no-src
```

### Example

```bash
python scanner.py prog1_calculator.src
python scanner.py prog3_functions.src -o out.txt
```

---

## Running the Parser

```bash
# Parse and print report to stdout
python parser.py <source_file>

# Write parse report to a file
python parser.py <source_file> -o <output_file>

# Include the AST as JSON in the report
python parser.py <source_file> --ast
```

### Example

```bash
python parser.py prog1_calculator.src
python parser.py prog4_structs_match_exceptions.src --ast
```

---

## Changing the Grammar

The parser is table-driven: the grammar lives as declarative data in
`grammar.py`, and a generic combinator engine (`grammar_engine.py`) walks
that table to parse source files. Changing the language means editing
`grammar.py` only — `grammar_engine.py` has no knowledge of this language's
syntax and should never need to change.

`grammar.py` builds a `GRAMMAR` dict of named rules out of a small set of
primitives from `grammar_engine.py`: `Term`/`Kw` (match a token), `Seq`
(match parts in order), `Alt` (ordered choice), `Star`/`Plus`/`Opt`
(repetition), `Ref` (reference another named rule, for recursion), `Cut`
(marks "we're committed to this alternative — later failures are real
errors, not backtracking"), `And`/`Not` (lookahead), `Bind` (label a `Seq`
capture so its `action` can read it by name), `Emit`/`Abort` (record an
error unconditionally, optionally aborting the current derivation), plus
helpers `chainl` (left-associative binary operators), `comma_list`, and
`many_rec` (repetition with the same error-recovery behavior as the rest of
the parser). For the handful of productions whose shape doesn't compose
cleanly out of these (e.g. `type`, most statements), `Rule(fn)` adapts a
plain Python function into a combinator — it's the most-used primitive in
`grammar.py` in practice. Each rule's `action` (or `Rule` function) builds
the same `Node` AST the interpreter consumes.

Example — adding a new statement kind is one rule plus one line wiring it
into the dispatcher:

```python
GRAMMAR["my_stmt"] = Seq(
    Kw("mykeyword"), Cut(),
    Bind("value", Ref("expression", fail_msg="Expected expression")),
    Term(TT.SEMICOLON, msg="Expected ';' after mykeyword"),
    action=lambda ps, c: Node("MyStmt", {"value": c["value"]}),
)

GRAMMAR["statement"] = Alt(
    Ref("my_stmt"),   # add here
    Ref("block"),
    Ref("if_stmt"),
    ...
)
```

No other file needs to change. See `grammar.py`'s module docstring for more
detail on the primitives.

---

## Running the Interpreter

```bash
# Scan -> parse -> type-check -> compile -> execute a source file
python interpreter.py <source_file>

# Print the generated quadruples before running, and trace each one as it executes
python interpreter.py <source_file> --ir --trace

# Print symbol-table metadata (functions, structs, variable types) before running
python interpreter.py <source_file> --symbols

# Cap total executed quads / call-stack depth, so an infinite loop or runaway
# recursion fails with a diagnosable [RUNTIME ERROR] instead of hanging.
# Defaults are 10,000,000 and 10,000; pass 0 to disable either cap.
python interpreter.py <source_file> --max-steps <N> --max-depth <N>
```

### Example

```bash
python interpreter.py prog1_calculator.src < prog1_calculator.in
python interpreter.py prog3_functions.src
```

If scanning, parsing, or semantic analysis reports any errors, the
interpreter prints them and exits with status `2` without running anything.
A failure while running (division by zero, an out-of-bounds array index, an
uncaught `throw`, ...) is reported as `[RUNTIME ERROR] Line N: ...` and exits
with status `3` — never a raw Python traceback.

## Running the Semantic Analyzer / IR Generator standalone

```bash
# Print the compiled quadruples for a source file (implies a clean
# scan/parse/semantic-check first; errors from any of those phases are
# printed and abort with exit status 2)
python ir.py <source_file>

# Write the quadruples to a file, and also print function/struct/variable metadata
python ir.py <source_file> -o <output_file> --symbols
```

`semantics.py` has no standalone CLI — it's a library used by `ir.py` and
`interpreter.py` (`analyze(program) -> (SymbolTable, [SemanticError])`).

## Running the Test Suite

```bash
# Diff each sample program's scanner token stream, compiled IR, and executed
# output against the committed goldens; run the feature fixtures under tests/
# and diff their output; and check that the negative fixtures fail at the
# right phase with the right exit status
python run_tests.py

# Regenerate the golden files after an intentional behavior change
python run_tests.py --update
```

The suite is **34 checks**: 5 sample programs (token stream + IR + output each),
12 feature fixtures under `tests/` (one language feature apiece — dynamic array
sizes, default parameters, `_` discard, parameter passing, nested structs, …),
and 17 negative fixtures that must fail at a specific phase (exit `2` for
lexical/syntax/semantic/IR errors, `3` for runtime errors).

Note that `--update` rewrites the `*_tokens.txt` goldens on every run even when
nothing changed, because those files embed a live `Scan time` measurement. The
comparison path strips that line, so it never causes a spurious failure — but it
does mean `--update` leaves the token files looking modified.

---

## Building a Standalone Binary

The interpreter has no external dependencies, so it can be packaged as a
single self-contained `cetirim.pyz` archive via Python's stdlib `zipapp`
module — runnable anywhere a Python 3.8+ interpreter exists, no `pip
install` needed:

```bash
# Build cetirim.pyz at the repo root
python build.py

# Run it exactly like interpreter.py
python cetirim.pyz <source_file> [--ir] [--trace] [--symbols]

# Or, on Unix, run it directly - the archive is executable
./cetirim.pyz <source_file>
```

Rebuild with `python build.py` any time `scanner.py`, `parser.py`,
`grammar.py`, `grammar_engine.py`, `ast_nodes.py`, `semantics.py`, `ir.py`,
or `interpreter.py` changes — the archive is a snapshot, not a symlink.

---

## Scanner Output Format

Each run produces three sections:

**1. Source listing** (suppressed with `--no-src`)
```
  1 | const int MAX_TRIES = 3;
  2 | const float PI = 3.14159;
```

**2. Lexical errors** — with line number, column, and context
```
[LEXICAL ERROR] Line 3, Col 18: Invalid token: digit sequence mixed with identifier chars
    (near: '32432bace12awf')
```

**3. Token stream table**
```
    #  TYPE              LEXEME                             LINE    COL  ATTR
  --------------------------------------------------------------------------------
    1  KEYWORD           'const'                               1      1
    2  KEYWORD           'int'                                 1      7
    3  IDENTIFIER        'MAX_TRIES'                           1     11
    4  ASSIGN_OP         '='                                   1     21
    5  INTEGER_LIT       '3'                                   1     23  3
    6  SEMICOLON         ';'                                   1     24
```

Literal tokens carry a coerced attribute value: integers as `int`, floats as `float`, strings/chars as their unescaped Python value, and booleans as `True`/`False`.

**4. Statistics** — token type breakdown, total errors, scan time, source size.

---

## Parser Output Format

The parse report includes:

**1. Summary** — parse error count and lex error count  
**2. Parse errors** — each with line number, column, and message  
**3. Optional AST** (with `--ast`) — full abstract syntax tree as JSON

```
Parse complete: 0 syntax error(s), 0 lex error(s)
```

With `--ast`:
```json
{
  "kind": "Program",
  "declarations": [
    {
      "kind": "FunctionDecl",
      "name": "main",
      "return_type": "void",
      ...
    }
  ]
}
```

---

## Token Types

| Category | Token Types |
|---|---|
| Literals | `INTEGER_LIT`, `FLOAT_LIT`, `CHAR_LIT`, `STRING_LIT`, `INTERP_STRING`, `BOOL_LIT` |
| Names | `IDENTIFIER`, `KEYWORD`, `UNDERSCORE` |
| Arithmetic | `ARITH_OP` (`+` `-` `*` `/` `%`) |
| Relational | `REL_OP` (`==` `!=` `<` `>` `<=` `>=`) |
| Logical | `LOGIC_OP` (`&&` `\|\|` `!`) |
| Assignment | `ASSIGN_OP` (`=`) |
| Special ops | `RANGE_OP` (`..`), `MATCH_ARROW` (`=>`) |
| Delimiters | `SEMICOLON`, `COMMA`, `COLON`, `LPAREN`, `RPAREN`, `LBRACE`, `RBRACE`, `LBRACKET`, `RBRACKET`, `DOT` |
| Special | `EOF`, `ERROR` |

### Reserved Keywords

```
int    float   char    string  bool    void    const   val     var
struct let     typedef if      else    for     while   repeat  until
return break   continue true   false   print   input   in      match
guard  try     catch   finally throw   _
```

---

## AST Node Reference

| Kind | Key Fields |
|---|---|
| `Program` | `declarations` |
| `FunctionDecl` | `name`, `return_type`, `params`, `body` |
| `StructDecl` | `name`, `fields` |
| `TypedefDecl` | `name`, `aliased_type` |
| `VarDecl` | `mutability` (`const`/`val`/`var`), `declarators` |
| `Declarator` | `name`, `type`, `initializer` |
| `LetDecl` | `names` (a `None` entry is a `_` discard slot), `values` |
| `Block` | `declarations`, `statements` |
| `IfStmt` | `condition`, `then`, `else` |
| `ForStmt` | `init`, `condition`, `update`, `body` |
| `ForInStmt` | `name`, `iterable`, `body` |
| `WhileStmt` | `condition`, `body` |
| `RepeatUntilStmt` | `body`, `condition` |
| `MatchStmt` / `MatchExpr` | `subject`, `cases` |
| `TryStmt` | `body`, `catch_clauses`, `finally_body` |
| `GuardStmt` | `condition`, `else_body` |
| `ThrowStmt` | `value` |
| `ReturnStmt` | `values` |
| `AssignExpr` | `target`, `value` |
| `BinaryExpr` | `op`, `left`, `right` |
| `UnaryExpr` | `op`, `operand` |
| `RangeExpr` | `start`, `end` |
| `CallExpr` | `callee`, `args` |
| `IndexExpr` | `object`, `index` |
| `MemberExpr` | `object`, `field`, `op` (`.`) |
| `Literal` | `token_type`, `lexeme`, `value` |
| `Identifier` | `name` |
| `Grouping` | `expression` |
| `WildcardPattern` | _(no fields)_ |
| `MatchCase` | `pattern`, `body` or `value` |
| `CatchClause` | `name`, `body` |
| `LoopControlStmt` | `keyword` (`break`/`continue`) |
| `BuiltinStmt` | `name` (`print`/`input`), `args` |
| `ExprStmt` | `expression` |
| `MultiAssign` | `lvalues`, `values` |
| `NamedArg` | `name`, `value` |
| `Param` | `name`, `type`, `default` (`None`, or a literal `Literal` node) |
| `Field` | `name`, `type` |
| `InitializerList` | `values` |
| `Type` | `name` |
| `StructType` | `name` |
| `StructDef` | `name`, `fields` |
| `TupleType` | `elements` |
| `ArrayType` | `base`, `size` |
| `InterpString` | `parts` (list of `Literal`/expression nodes) |

Every node also carries `line`/`col` (stamped automatically by the grammar
engine, kept outside `fields` so they never show up in the `--ast` JSON) —
used by semantic and runtime error messages.

---

## Error Detection

### Lexical errors (Scanner)

| Error | Example |
|---|---|
| Digit–letter mixed token | `32432bace` |
| Unterminated string literal | `"hello world` |
| Unterminated character literal | `'A` |
| Empty character literal | `''` |
| Unterminated block comment | `/* never closed` |
| Unknown symbol | `@`, `#`, `$` |
| Unknown escape sequence | `\q` |

The scanner recovers and continues after each error.

### Syntax errors (Parser)

The grammar engine has built-in error recovery — after a hard error it
synchronizes to the next statement boundary and keeps parsing (see
`many_rec` in "Changing the Grammar" above), so all errors in a file are
reported in one pass.

### Semantic errors (`semantics.py`)

Reported as `[SEMANTIC ERROR] Line N, Col C: ...`. Checks include:
undeclared/redeclared names, assignment to an immutable `val`/`const`
binding, call arity and argument/named-argument validity, return-count vs.
declared arity and `void` misuse, `break`/`continue` outside a loop,
non-`bool` conditions, indexing a non-array or with a non-`int`, member
access on a non-struct or unknown field, operand types for every operator,
and `match` pattern/arm-type compatibility. See `semantics.py`'s module
docstring for the full type-checking design.

### Runtime errors (`interpreter.py`)

Reported as `[RUNTIME ERROR] Line N: ...` and exit status `3` — never a raw
Python traceback. Covers division by zero, out-of-bounds array indices,
tuple-arity mismatches on destructuring, malformed `input()` values, and
uncaught `throw`n exceptions.

---

## Sample Programs

| File | Constructs Demonstrated |
|---|---|
| `prog1_calculator.src` | `const`, `val`, `var`, `int`/`float`/`string`, `input`, `print`, arithmetic, nested `if-else` |
| `prog2_loops_arrays.src` | C-style `for`, `for-in-range`, `for-in-collection`, `while`, `repeat-until`, arrays, `break`, `continue` |
| `prog3_functions.src` | Function declarations, call-by-value, array params, multiple return values, recursion, `let`, named parameters, interpolated strings |
| `prog4_structs_match_exceptions.src` | `struct`, `typedef`, `match` statement, `match` expression, `guard`, `try`/`catch`/`finally`, `throw`, `char`/`bool` literals, escape sequences |
| `prog5_advanced.src` | Multi-assignment, `let` destructuring, complex expressions, nested loops |

`prog1` and `prog2` call `input()`, so running them non-interactively needs a
stdin fixture: `python interpreter.py prog1_calculator.src <
prog1_calculator.in`. `prog3`–`prog5` don't read input and can be run
directly.

---

## Speed

Tested on a synthetic 5,000-line / ~190 KB file:

```
Total tokens : 37,711
Scan time    : ~207 ms
```

---

## Milestones

| Milestone | Deadline | Status |
|---|---|---|
| CFG, Lexical Rules, Intermediate Code Spec, Language Choice | May 28, 2026 | ✅ Done |
| Scanner | June 11, 2026 | ✅ Done |
| Parser | July 2, 2026 | ✅ Done |
| Interpreter Checkpoint Demo | July 30, 2026 | ✅ Done |
| Final Project Demo & Submission | August 6, 2026 | 🔲 Upcoming |

---

## Implementation Language

The interpreter is implemented in **Python 3**. Chosen for its dynamic typing, native dict/list/tuple data structures (ideal for symbol tables and token streams), strong string processing, and suitability for recursive descent parsing.
