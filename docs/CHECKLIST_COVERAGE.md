# Checklist / Rubric Coverage Map

Maps every row of `docs/CSC617M_Machine_Problem_Checklist_Rubric.pdf` to the
program that demonstrates it. Two sets of programs are referenced:

- **`prog1`–`prog5`** — the five original sample programs.
- **`check1`–`check5`** — a coverage set written specifically against this
  rubric, one program per group of rows, so no row depends on being spotted
  inside a larger program.

All ten are registered in `run_tests.py` as positive fixtures. Each one has a
committed token stream, quadruple listing, and expected output, which is what
pins the rubric's three grading columns at once:

| Rubric column | Pinned by | Command |
|---|---|---|
| Parser | `<name>_tokens.txt` + a clean parse | `python parser.py <file>` |
| Semantics | `<name>_ir.txt` (IR is only generated once analysis passes) | `python ir.py <file>` |
| Interpreter | `<name>_expected.txt` | `python interpreter.py <file>` |

`python run_tests.py` checks all three for all ten programs, then re-runs each
under `-O` and runs the negative fixtures.

---

## 1. Completeness of Work

| Construct | Primary demonstration | Also in |
|---|---|---|
| Headers/Comments | `check1` — file header, section comments, and the `/* … */` block form | all programs carry a header comment |
| Variable Declaration | `check1` — `var`, `val`, `let`, every scalar type, zero-initialization | all |
| — Arrays | `check1` — literal size, initializer list, zero-filled, 2-D, runtime-computed size | `check3`, `check4`, `prog2` |
| — Structures/Records | `check1` — fields, nested struct field, array field, array of structs, `typedef` | `check5`, `prog4` |
| — Pointers | **Not implemented.** See the note below. | — |
| Constant Declaration | `check1` — a `const` per scalar type | `check2`, `prog1` |
| Assignment | `check1` — scalar, element, field, nested field, multi-assign, destructuring, `_` discard | all |
| Math Expr — Simple | `check2` §(a) | `check1`, `check3` |
| Math Expr — Complex | `check2` §(b) | `check4` |
| If Stmt | `check3` — two-way, no-else, and the nested chain that stands in for `else if` | `prog1`, `prog4` |
| Loops — While | `check3` | `prog2`, `prog5` |
| Loops — For | `check3` — C-style, omitted init/update, `for-in-range`, `for-in-collection` | `prog2`, `prog5` |
| Loops — Repeat-Until | `check3` — including the runs-at-least-once case | `prog2`, `prog5` |
| Boolean Expr — Simple | `check2` §(c) — every relational operator plus unary `!` | `check3` |
| Boolean Expr — Complex/Simple Logical | `check2` §(d) | `prog4` |
| Boolean Expr — Multiple/Complex Logical | `check2` §(e) | `prog5` |
| Input Stmt | `check5` — `int`, `float`, `string`, `char`, `bool` targets | `prog1`, `prog2` |
| Output Stmt | `check5` — multi-argument, escapes, interpolation, aggregates | all |
| Functions — Declare | `check4` — `void`, scalar, `float`, struct return, tuple return, array parameter, default parameter | `prog3` |
| Functions — Call | `check4` — positional, named, out-of-order named, defaulted, nested, destructured | `prog3` |
| Functions — Recursion | `check4` — direct (`factorial`), tree (`fib`), two-argument (`gcd`), over an array (`sum_from`), and mutual (`is_even_r`/`is_odd_r`) | `prog3` |
| Nested Statements | `check3` — loop-in-loop, and three deep (`for` → `if` → `while`) | `prog2`, `prog5` |

### Pointers

The language has **no pointer type**, by design — it is listed as an
unimplemented bonus item in `LANGUAGE.md` §13. There is no address-of operator,
no dereference, and no `new`/`delete`; a bare `&` is not even a recognized
token (only `&&` is). Nothing in this repository should be presented as
satisfying this row.

What the language does have instead is the split allocation model described
under Heap Simulation below, which is where reference behavior lives.

---

## 2. Error Messaging — accuracy and informativeness

Every diagnostic carries a phase tag, a line, usually a column, and the
offending name or types. The negative fixtures in `tests/` each pin one, and
the process exit code distinguishes the phase (`2` for lexical/syntax/semantic/IR,
`3` for runtime).

The five semantic errors the rubric names explicitly now have one fixture each:

| Rubric semantic error | Fixture | Message |
|---|---|---|
| 1. Undeclared variable | `tests/sem_undeclared_var.src` | `Undeclared identifier 'running_total'` |
| 2. Type mismatch | `tests/sem_type_mismatch.src` | `Cannot assign 'string' to 'int'` |
| 3. Multiply-defined variables | `tests/sem_redeclared_var.src` | `'total' is already declared in this scope` |
| 4. Constant reassignment | `tests/sem_const_reassign.src` | `Cannot assign to immutable variable 'MAX_TRIES'` |
| 5. Cardinality/ordinality of parameters | `tests/sem_arg_cardinality.src` | `Function 'add3' expects 3 argument(s), got 2` |

`tests/semantic_error.src` pins the same immutability rule for `val`, the
function-local form, alongside the `const` case above.

Run any of them directly to show the message:

```bash
python interpreter.py tests/sem_arg_cardinality.src
```

---

## 3. Error Recovery

`tests/sem_multi_errors.src` contains five *different* mistakes — one of each
kind in the table above — and the analyser reports all five in a single run
rather than stopping at the first:

```bash
python interpreter.py tests/sem_multi_errors.src
```

Recovery is implemented at all three front-end phases:

- **Scanner** — emits an `ERROR` token and resumes, so every valid token after
  a bad one is still reported (`test_errors.src`, `tests/lex_error.src`).
- **Parser** — `many_rec` synchronizes and skips to the next declaration or
  statement, with a guaranteed one-token minimum advance so a malformed input
  cannot loop forever (`tests/stray_brace.src`).
- **Semantic analyser** — accumulates diagnostics instead of raising, which is
  what `sem_multi_errors.src` demonstrates.

---

## 4. Robustness

- No Python traceback can reach the user: runtime failures are reported as
  `[RUNTIME ERROR] Line N: …` and exit `3`. Pinned by the exit-3 fixtures
  (`div_by_zero`, `mod_by_zero`, `array_oob`, `uncaught_exception`,
  `infinite_recursion`).
- Runaway programs are bounded by `--max-steps` and `--max-depth` (defaults
  10,000,000 quads and 10,000 frames) rather than hanging.
- Every negative fixture is also re-run under `-O` in the suite, so
  optimization can never swallow an error that would otherwise fire.

---

## 5. Flexibility and Robustness of Expressions

`check2_expressions.src` is organized as the rubric's own hierarchy, with its
sections labelled `(a)`–`(e)` in the source and in the output:

| Tier | Section | What it shows |
|---|---|---|
| (a) simple_math | `---- (a) simple math ----` | `id + num`, `id * id`, `num / num`, `%`, unary `-`/`+`, and integer-division truncation |
| (b) complex_math | `---- (b) complex math ----` | the rubric's own `func1(23,4,5) + 34/234*z32` shape; a single expression combining a **function call, an array element and a struct field**; nested parenthesised sub-expressions; mixed int/float promotion; calls whose arguments are themselves complex expressions |
| (c) simple boolean | `---- (c) simple boolean ----` | every relational operator (`< > <= >= == !=`), unary `!`, and `complex_math rel_op complex_math` |
| (d) complex_boolean | `---- (d) complex boolean ----` | `simple_boolean && simple_boolean`, the `||` form, and a three-term chain |
| (e) Complex_Logical | `---- (e) complex logical ----` | nested complex_boolean mixing complex_math operands, several logical operators, and negated groups |

It closes with a short-circuit demonstration: `noisy()` prints when it runs, so
`false && noisy(true)` proves the right operand was never evaluated.

```bash
python interpreter.py check2_expressions.src
```

---

## 6. Efficiency

The optional IR optimizer (`optimizer.py`) implements three techniques —
constant propagation and folding, algebraic simplification, and dead code
elimination — run to a fixpoint. It never mutates its input, so both listings
can be shown side by side:

```bash
python optimizer.py check2_expressions.src      # annotated before/after report
python interpreter.py check2_expressions.src -O # run the optimized IR
```

On `check2_expressions.src` this removes 108 of 491 quadruples. The correctness
contract is differential rather than golden-based: `run_tests.py` re-runs every
program under `-O` and requires byte-identical output, since an optimizer that
changes what a program prints has broken it.

---

## 8. Heap Simulation

There is no pointer type to expose, but there is a two-tier storage model, and
`check5_io_heap.src` demonstrates it end to end:

| Value kind | Storage | Passing | Assignment |
|---|---|---|---|
| `int`, `float`, `char`, `string`, `bool` | in the call frame | by value | copies the value |
| arrays, structs | heap-allocated object; the variable holds a reference | by reference | copies the reference |

The program shows, with printed before/after state:

1. A callee's write to a scalar parameter is invisible outside; the same write
   to an array or struct parameter is visible — one object, two frames.
2. Assigning an aggregate aliases it: after `alias = pt;` a write through
   `alias` is observable through `pt`.
3. Every slot of an array of structs is its own allocation, and each call to a
   struct-returning function allocates a fresh record — writing one never
   disturbs another.
4. Reclamation is automatic. After `first = second;` the record `first`
   previously held is unreachable; there is no `free`/`delete` in the language.

Frames are a stack of dictionaries and aggregates are Python objects, so this
is genuine reference semantics rather than a simulated address space — there
are no addresses, and no pointer arithmetic is possible.

```bash
python interpreter.py check5_io_heap.src < check5_io_heap.in
```

---

## Running the whole thing

```bash
python run_tests.py
```

75 checks: 10 positive programs × (tokens, IR, output), 13 feature fixtures,
23 negative fixtures, the optimizer report, and the `-O` differential re-runs.
