# CSC617M Machine Project — Custom Language Interpreter

**Course:** CSC617M — Theory of Programming Languages
**Current Milestone:** Interpreter (complete)

---

## Overview

This repository contains the interpreter project for CSC617M. The group designed a new programming language from scratch and implemented a full interpreter for it in Python. The pipeline is:

**Scanner → Parser → Semantic Analyzer → IR Generator → VM**

All phases are complete: source runs end to end via `python interpreter.py <file>`, which scans, parses, type-checks, lowers to quadruple intermediate code, and executes it.

An optional **IR optimizer** (`optimizer.py`) sits between the IR generator and the VM: `-O` runs the optimized code, and `python optimizer.py <file>` shows exactly what it changed and why — see [IR Optimization](#ir-optimization).

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
├── optimizer.py                                # Optional IR optimizer + viewable before/after report (JSON for the IDE)
├── interpreter.py                              # VM: executes quadruples; CLI entry point for running a program
├── cetirim_ide.py                              # Tkinter IDE: editor, integrated debugger, optimizer view
├── run_tests.py                                # Golden-output test runner (--update to regenerate goldens)
├── build.py                                    # Packages the runtime modules into cetirim.pyz (stdlib zipapp)
├── cetirim.pyz                                 # Built standalone binary — rebuild with `python build.py`
├── README.md                                   # This file: how to USE the interpreter
├── LANGUAGE.md                                 # Programmer's manual: how to WRITE programs in the language
├── LIMITATIONS.md                              # Design decisions, known edge cases, scope limits
├── docs/                                       # Course handouts + CHECKLIST_COVERAGE.md (rubric row-by-row map)
├── samples/                                    # The five graded sample programs, each with its committed goldens:
│   ├── prog1_calculator.src                    #   the program itself
│   ├── prog1_calculator.in                     #   stdin fixture for its input() calls (prog1, prog2 only)
│   ├── prog1_calculator_tokens.txt             #   golden scanner token stream
│   ├── prog1_calculator_ir.txt                 #   golden quadruple dump
│   ├── prog1_calculator_expected.txt           #   golden program output
│   └── ...                                     #   prog2_loops_arrays … prog5_advanced, same pattern
├── checks/                                     # Rubric checklist-coverage programs, same .src/.in/goldens pattern:
│   └── ...                                     #   check1_declarations … check5_io_heap
└── tests/                                      # 14 feature fixtures (+ golden output), 25 negative fixtures,
                                                #   and the scanner-recovery fixture (test_errors.src + its golden)
```

---

## Requirements

- Python 3.8 or higher
- No external dependencies — standard library only

## Cetirim IDE

Launch the graphical development environment with:

```bash
python cetirim_ide.py
```

The IDE needs a Python whose `tkinter` is linked against **Tk 8.6 or
newer**. On macOS, avoid `/usr/bin/python3` — Apple's bundled Tk 8.5 opens
a blank, unpainted window on current macOS versions. Use a Homebrew Python
with `python-tk` installed (`brew install python-tk`), or a
[python.org](https://www.python.org) build, both of which ship a modern Tk.
Check with: `python3 -c "import tkinter; print(tkinter.TkVersion)"`.

The IDE is connected to this project's scanner, parser, semantic analyzer, and interpreter. The interface is a dark, deliberately minimal one: three surface levels, hairline dividers, and a single accent color, with the chrome kept out of the way of the code. A slim toolbar carries the analysis actions on the left (**Check**, **Optimize**) and execution on the right (**Step**, **Continue**, **Stop**, then **Debug** and **Run**); file actions live in the File menu and on `⌘N`/`⌘O`/`⌘S`. Below it sit a collapsible **Outline** sidebar, the editor, and — across a draggable sash — a bottom panel whose tabs (**Output · Problems · Debug · Trace · Symbols · Optimizer**) are marked by an accent underline. The editor has a breakpoint gutter with right-aligned line numbers (breakpoints are red dots), a subtle caret-line highlight, and a tab showing a `●` unsaved marker; the status bar reports the diagnostic count (click it to open Problems), the run/debug state, and the cursor position.

Fonts are chosen per platform (SF Mono/Menlo on macOS, Consolas/Cascadia Code on Windows) and the editor font size is adjustable with `⌘+`/`⌘−` or the View menu. On macOS the accelerators use `⌘` — `⌘S/⌘O/⌘N`, find/replace `⌘F`, rename `⌘R`, sidebar/panel toggles `⌘B`/`⌘J` (Ctrl on other platforms). Run/Debug/Check stay `F5`/`F6`/`F7`, and the debugger's transport is on `F8` (Step), `F9` (Continue) and `⇧F5` (Stop).

It provides syntax highlighting and lexical-error marking, Ctrl+Space templates / keyword autocomplete, a navigable code outline (built from a real parse, so it nests struct fields under their struct and each function's parameters and locals under the function, alongside the global constants and typedefs, each row named and symbol-marked by kind), inline find/replace with match highlighting (Enter/Shift+Enter cycle matches, Esc closes), rename refactoring, and problem checking — the **Problems** tab lists lexical, syntax, *and* semantic diagnostics in a table of severity, position and message, color-coded ✖ error / ⚠ warning, double-click to jump to the offending line, with a live count on the tab itself. The Output tab behaves as an interactive terminal: when a program calls `input(...)`, type the requested value there and press Enter or **Send** — submitting an empty line simply re-prompts, the same as pressing Enter on an empty line at a real terminal.

### Debugger

Click a line number in the gutter to toggle a breakpoint on that line. Press **Debug** or `F6` to run the program in debug mode. Execution pauses when it reaches a breakpoint, highlighting the current line in the editor. While paused, the **Debug** tab shows the active **Call Stack** (which function is executing, and who called it) and the **Variables** currently in scope, grouped into locals and globals. The transport controls sit in the toolbar, greyed out until a run is in flight:

- **Step** (`F8`) — run just the next source line, then pause again.
- **Continue** (`F9`) — resume running until the next breakpoint (or the program ends).
- **Stop** (`⇧F5`) — abort the run. Works during a plain **Run** as well as a debug session, and takes effect even when the program is blocked at an `input(...)` prompt or spinning in a loop with no breakpoints. Starting a new Run/Debug also stops any still-running previous one automatically.

Breakpoints can be toggled even while a debug session is running.

The **Debug** tab also has a **Watch** panel: type a variable name and click **Add** (or press Enter) to track it — its value refreshes every time execution pauses, showing `<not in scope>` if that variable isn't currently live. Double-click a watch entry to remove it.

The **Trace** tab logs every source line executed during a debug session, in order, along with which function it ran in — a full execution history, not just the lines you stopped on. It's populated even while just using Continue, not only while stepping.

### Symbol table

The **Symbols** tab renders the semantic analyzer's symbol table for the current buffer: every function (its full signature, then each parameter and local with its declared type, `var`/`val` mutability, and the globally-unique `ir_name` the VM addresses it by — `semantics.py`'s flat-namespace scheme, made visible), every struct with its fields, typedefs with what they alias, and `const` globals. It refreshes when the tab is opened, when **Refresh** is pressed, or after any **Check** (`F7`); it needs a program that passes semantic analysis (otherwise it points at the Problems tab instead of showing a stale table). Double-click a function to jump to its declaration. The Debug tab's **Call Stack** / **Variables** / **Watch** panels are the runtime complement — the same names carrying live values while execution is paused.

### Optimizer view

Press **Optimize** (toolbar or Tools menu) to run the [IR optimizer](#ir-optimization) on the current program and open the **Optimizer** tab: the original quad listing color-coded by each quad's fate (red = removed, amber = rewritten), the optimized listing beside it, the transformation log (double-click an entry to jump to the source line it came from), and a stats summary — how many quads survived, and how much work each of the three techniques found. The view is display-only: **Run** and **Debug** always execute the unoptimized IR, so breakpoints keep lining up with source lines (the test suite's differential `-O` checks prove the optimized IR produces byte-identical output anyway).

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
python scanner.py samples/prog1_calculator.src
python scanner.py samples/prog3_functions.src -o out.txt
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
python parser.py samples/prog1_calculator.src
python parser.py samples/prog4_structs_match_exceptions.src --ast
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

# Optimize the intermediate code before running it (see "IR Optimization" below).
# The program's output is unchanged; it just executes fewer quads.
python interpreter.py <source_file> -O

# Cap total executed quads / call-stack depth, so an infinite loop or runaway
# recursion fails with a diagnosable [RUNTIME ERROR] instead of hanging.
# Defaults are 10,000,000 and 10,000; pass 0 to disable either cap.
python interpreter.py <source_file> --max-steps <N> --max-depth <N>
```

### Example

```bash
python interpreter.py samples/prog1_calculator.src < samples/prog1_calculator.in
python interpreter.py samples/prog3_functions.src
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

---

## IR Optimization

`optimizer.py` is an **optional** phase between `ir.py` and `interpreter.py`.
It rewrites the quadruple list into an equivalent but shorter one, and records
every change it makes so the transformation can be inspected rather than taken
on faith. The default pipeline never calls it — `python interpreter.py <file>`
runs exactly the quads `ir.py` emitted, which is what keeps the committed
`progN_ir.txt` goldens valid.

```bash
# Annotated text report: optimized listing + every transformation + statistics
python optimizer.py <source_file>

# Write that report to a file
python optimizer.py <source_file> -o <output_file>

# Write the IDE view payload as JSON ('-' sends it to stdout)
python optimizer.py <source_file> --json <output_file>

# Run a program with the optimized IR (output is identical, fewer quads execute)
python interpreter.py <source_file> -O

# Show the optimized quads before running them
python interpreter.py <source_file> -O --ir
```

### The three techniques

**1. Constant propagation** (with constant folding). A variable whose only
definition in the whole program is a literal store — every global `const`, and
every `val` initialized to a literal — is replaced by that literal at each read.
A straight-line pass does the same for reassigned locals within a basic block.
Any operation whose operands all became literals is then evaluated at compile
time.

```
(ASSIGN, 3.14159, -, PI)          PI is write-once, so every read of it becomes 3.14159
(CAST, "A", int, _t6)      ->     (ASSIGN, 65, -, _t6)         folded ('A' promotes to its ordinal)
(ADD, _t6, 1, _t7)         ->     (ASSIGN, 66, -, _t7)         folded 65 + 1
```

A `JZ`/`JNZ` whose condition folds to a literal becomes an unconditional `JMP`
or disappears — which is how `const bool VERBOSE = false;` guarding an `if`
hands the whole arm to dead code elimination.

**2. Algebraic simplification.** Identity operations become a plain copy:

```
(UPLUS, main.a, -, _t4)           ->  (ASSIGN, main.a, -, _t4)      unary + is the identity
(MUL, identities.n, 1, _t2)       ->  (ASSIGN, identities.n, -, _t2)  x * 1 -> x
(ADD, identities.n, 0, _t3)       ->  (ASSIGN, identities.n, -, _t3)  x + 0 -> x
(MOD, identities.n, 1, _t5)       ->  (ASSIGN, 0, -, _t5)             x % 1 -> 0
```

`* 1` and `/ 1` are exact for both `int` and `float`. `+ 0`, `- 0` and `% 1`
are only applied when the operand is provably `int`, because `-0.0 + 0` is
`0.0` — a different value that prints differently.

**3. Dead code elimination.** Four kinds of removal: quads between an
unconditional transfer of control and the next label (this is what removes the
duplicate trailing `RETURN` `gen_function` always emits); a `JMP L` that only
labels separate from `LABEL L`; a side-effect-free quad whose result nothing
reads; and a `LABEL` nothing jumps to.

```
011: (RETURN, -, -, -)            removed — unreachable, the line above already returned
042: (JMP, -, -, L3)              removed — L3 is the very next quad
000: (ASSIGN, 3, -, MAX_TRIES)    removed — every read was propagated away, so nothing reads it
```

### What is deliberately *not* optimized

A division or modulo by a literal zero is neither folded nor deleted, so
`[RUNTIME ERROR] Line N: Division by zero` still fires at the same line — the
optimizer must not make a broken program look correct. `TOSTR` is never folded
(reproducing the VM's float/bool formatting here would duplicate rules only the
VM should own), and `ARR_LOAD`/`ARR_LEN`/`FIELD_LOAD` are never deleted, since
their bounds and key checks can raise.

### JSON payload for the IDE

`--json` emits a self-contained payload a viewer can render as a
side-by-side diff without doing any analysis of its own. The IDE's
Optimizer tab shows exactly this view — it calls `build_view()` directly
rather than shelling out for the JSON:

```json
{
  "version": 1,
  "source_file": "prog1_calculator.src",
  "techniques": ["constant-propagation", "algebraic-simplification", "dead-code-elimination"],
  "original": [
    { "i": 0, "op": "ASSIGN", "arg1": "3", "arg2": null, "result": "MAX_TRIES",
      "line": 6, "text": "(ASSIGN, 3, -, MAX_TRIES)", "status": "removed" }
  ],
  "optimized": [
    { "i": 0, "op": "JMP", "arg1": null, "arg2": null, "result": "L1",
      "line": 10, "text": "(JMP, -, -, L1)", "orig_index": 5 }
  ],
  "transformations": [
    { "technique": "constant-propagation", "kind": "fold", "orig_index": 45, "line": 34,
      "before": "(CAST, \"A\", int, _t6)", "after": "(ASSIGN, 65, -, _t6)",
      "detail": "folded (int) \"A\" -> 65" }
  ],
  "stats": {
    "original_count": 132, "optimized_count": 118, "removed": 14, "rewritten": 9,
    "by_technique": { "constant-propagation": 12, "algebraic-simplification": 1,
                      "dead-code-elimination": 14 }
  }
}
```

| Field | Meaning for the IDE |
|---|---|
| `original[]` / `optimized[]` | The two full listings. Operand fields are the same display strings the `*_ir.txt` files show (literals quoted and escaped, names bare, `null` for an unused slot); `text` is the whole quad pre-rendered. |
| `status` | `kept`, `rewritten`, or `removed` — enough to color each row of the original listing. |
| `orig_index` | Which original row an optimized row came from, for drawing the correspondence. |
| `line` | Source line, present on every quad and every transformation — use it to highlight the statement a quad came from. |
| `transformations[]` | The log, in the order the changes were applied. `after` is `null` when the quad was removed. `detail` is a ready-to-display sentence. |
| `kind` | One of `propagate`, `fold`, `simplify`, `remove-unreachable`, `remove-jump`, `remove-dead`, `remove-label`. |

`version` is the payload's schema version; bump it if any field above changes
meaning, so the IDE can tell old payloads from new ones.

### Worked example

`tests/optimizer_demo.src` is written to exercise all three techniques at once;
`tests/optimizer_demo_opt.txt` is its committed report (86 quads → 54, in 3
rounds: 22 propagations/folds, 5 simplifications, 32 removals).

```bash
python optimizer.py tests/optimizer_demo.src
```

---

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

The suite is **59 checks**: 5 sample programs (token stream + IR + output each),
13 feature fixtures under `tests/` (one language feature apiece — dynamic array
sizes, default parameters, `_` discard, parameter passing, nested structs, …),
17 negative fixtures that must fail at a specific phase (exit `2` for
lexical/syntax/semantic/IR errors, `3` for runtime errors), the optimizer report
golden, and 23 differential optimizer checks.

The differential checks are how the optimizer is held to its contract: every
sample program and feature fixture is run a second time with `-O` and must
produce byte-identical output against the *same* golden, and every runtime-error
negative fixture must still fail identically with `-O`. An optimizer that needed
its own expected-output file would by definition have changed the program's
behavior, so sharing the goldens is the point.

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

All five live in `samples/`. `prog1` and `prog2` call `input()`, so running
them non-interactively needs a stdin fixture: `python interpreter.py
samples/prog1_calculator.src < samples/prog1_calculator.in`. `prog3`–`prog5`
don't read input and can be run directly.

### Checklist-coverage set

A second set of programs is organized against the course grading checklist
(`docs/CSC617M_Machine_Problem_Checklist_Rubric.pdf`), one program per group of
rows, so no graded construct has to be spotted inside a larger program:

| File | Checklist rows |
|---|---|
| `check1_declarations.src` | Headers/Comments, Variable Declaration, Arrays, Structures/Records, Constant Declaration, Assignment |
| `check2_expressions.src` | Math Expr (simple, complex), Boolean Expr (simple, complex, complex logical) — laid out as the rubric's own `(a)`–`(e)` expression hierarchy |
| `check3_control_flow.src` | If Stmt, Loops (`while`, all three `for` forms, `repeat-until`), Nested Statements |
| `check4_functions.src` | Functions: Declare, Call, Recursion (direct, tree, mutual) |
| `check5_io_heap.src` | Input Stmt, Output Stmt, Heap Simulation (reference vs. value semantics) |

All five live in `checks/`. `check5` reads input, so run it as `python
interpreter.py checks/check5_io_heap.src < checks/check5_io_heap.in`. All
five are registered as positive fixtures in
`run_tests.py`, so their token stream, IR, and output are all diffed against
committed goldens.

**[`docs/CHECKLIST_COVERAGE.md`](docs/CHECKLIST_COVERAGE.md)** maps every
checklist row to the program that demonstrates it, including the five named
semantic errors and an honest note on the two rows the language does not
implement.

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
