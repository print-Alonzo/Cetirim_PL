# Known Limitations, Compromises, and Design Tradeoffs

This document is a candid audit of where the interpreter (`semantics.py` →
`ir.py` → `interpreter.py`) is not fully hardened, where it makes a
deliberate simplifying choice instead of a "correct" one, and where it's
simply out of scope for this project. It's organized in three tiers:

1. **Intentional design decisions** — made on purpose and defensible. Listed here for completeness, with the
   reasoning restated.
2. **Known gaps** — real edge cases discovered while building the pipeline
   that weren't hardened against. A dedicated review pass closed every gap
   that was here as of the last audit (each is now either a §1 design
   decision or a fixed-and-tested behavior — see §2's table for exactly
   what changed and which `tests/*.src` fixture exercises it); this section
   is deliberately kept, and used as the place for whatever's found next
   time.
3. **Scope limitations** — things a "real" language/toolchain would have
   that this project never set out to build.

None of this is urgent to fix for the checkpoint demo — the 5 sample
programs all run correctly end-to-end (`python run_tests.py` is green). This
is a map of where a more adversarial test program, or a TA poking at edge
cases, could find a gap.

---

## 1. Intentional design decisions

These are deliberate calls, not bugs. Each is a one-line flip if a grader's
expectation differs.

| Decision | Reasoning |
|---|---|
| `val`/`const` freeze the **binding**, not the contents | `prog2` bubble-sorts a `val int scores[5]` — only rebinding the name is rejected, element/field mutation is allowed |
| `int / int` and `int % int` both use C-style **truncated** semantics | Matches `prog1`'s existing DIV behavior; `%`'s remainder now takes the sign of the *dividend* (not Python's floor-based `%`, which takes the sign of the divisor), the same truncation convention DIV already used — was an inconsistency (see the old "known gaps" entry below), closed by giving `MOD` its own explicit-zero-check truncating handler |
| `..` ranges are inclusive | `for (i in 0..4)` visits all 5 elements of a 5-element array |
| Arrays/structs are reference values; scalars are call-by-value | Falls out of storing them as native Python `list`/dict objects — not enforced by semantics.py, just a consequence of how the VM represents values |
| Only **one** `catch` clause is allowed per `try` | Catch clauses carry no exception type, so there was never a way to discriminate between multiple ones — `ir.py` used to just silently compile every clause after the first as dead code (see old §2 below); `semantics.py`'s `_analyze_try` now rejects a second clause as a hard `[SEMANTIC ERROR]` instead. The `catch_list → catch_clause catch_list` grammar production is unchanged (still demonstrable at the parse level, and still exercised by `tests/multi_catch_error.src`), only semantics enforces the limit. `prog3`/`prog4` were edited to drop their (dead) second clauses |
| `+` is numeric only, no string concatenation operator | Interpolated strings (`` `{a} {b}` ``) are the concatenation mechanism |
| `let` and `multi_assign` bindings are always mutable | Neither construct has a `val`/`const`-style marker in the grammar |
| Numeric coercion is `char -> int -> float` only | Everything else (`int -> char`, `float -> int`, anything involving `string`/`bool`) must match exactly |
| Semantic diagnostics have two severities: `[SEMANTIC ERROR]` (aborts) and `[SEMANTIC WARNING]` (reported, pipeline still runs) | Lets dead code (a statement after an unconditional `return`/`break`/`continue`/`throw` in the same block) and a duplicate `match` literal pattern be flagged without rejecting an otherwise-valid program — a full reachability/exhaustiveness analysis was judged out of scope, so this only catches the two shapes named above, not every dead-code shape a "real" compiler would |
| Default parameter values must be **trailing** and a **bare literal** | Mirrors the existing rule that `const` initializers must be literals; sidesteps any question of evaluation order or scope for the default expression, and keeps "which parameters can be omitted" unambiguous from the signature alone |
| The VM caps total executed quads (`--max-steps`, default 10,000,000) and call-stack depth (`--max-depth`, default 10,000); `0` disables either | Was previously unbounded — an infinite loop or infinite recursion would hang or grow memory without limit rather than failing with a diagnosable `[RUNTIME ERROR]`. Both caps are generous defaults meant to catch runaway bugs, not to constrain legitimate deep recursion or long-running loops |

---

## 2. Known gaps

A review pass closed every gap that was previously documented here (each
either became a §1 design decision with its own rationale, or was fixed
outright and is now covered by a `tests/*.src` fixture — see the table
below). One new gap was found in the process of closing the others and is
recorded here in the same spirit: honestly, before it's found by someone
else.

| Former entry | Resolution |
|---|---|
| Thrown values aren't required to be strings | `throw` now type-checks its value against `T_STRING` (`semantics.py`'s `ThrowStmt` handling) — `tests/throw_type_error.src` |
| `input()` into a non-scalar variable silently misbehaves | `input()` targets must now be a scalar type — `tests/input_array_error.src` |
| Multiple `catch` clauses are mostly decorative | Now a hard semantic error instead of silent dead code — see §1's "Only one `catch` clause" row, `tests/multi_catch_error.src` |
| Named arguments require covering every parameter | Default parameter values (trailing, literal-only) let a call omit them — see §1, `tests/default_params.src` |
| Destructuring can't discard a value | `_` is now a valid discard slot in `let`/`multi_assign` — `tests/discard.src` |
| Match "predicate pattern" detection is a shape heuristic | Replaced with a real type-based decision (`node_is_predicate`, set once by `semantics.py`, read by `ir.py`) — `prog4` still passes byte-identical, and the case the heuristic couldn't express (equality-comparing a subject against a bool-valued sub-expression) now works |
| `MOD` doesn't get the same C-style treatment as `DIV` | Both are now truncated (C-style) with explicit zero checks — see §1 |
| `ARR_NEW` has no negative-size guard, but was unreachable | Guard added, and dynamic array sizes (below) made it reachable — `tests/dynamic_array.src` exercises the array; a negative dynamic size hits the guard directly |
| No "all paths return a value" checking | Now a hard semantic error — `tests/missing_return_error.src` |
| No dead-code / unreachable-pattern detection | Now a `[SEMANTIC WARNING]` for a statement after an unconditional terminator, and for a duplicate `match` literal pattern — see §1's warning-tier row |
| Interpolated-string edge cases (`\{`, nested-quote brace confusion) | Splitting moved into the scanner, the only place that still knows which braces came from an escape and which characters are inside a nested string — `tests/interp_edge_cases.src` |
| Comparing two one-character strings can silently become a char comparison | The runtime `_numeric()` ordinal-promotion hack is gone; `ir.py` now promotes `char` operands explicitly (via `node_type`, at compile time), so a `string`-vs-`string` comparison is never touched — `tests/string_comparison.src` |
| Runtime error line numbers are per-statement, not per-sub-expression | `ir.py`'s `_gen_expr` now stamps each quad with its own sub-expression's line — `tests/div_by_zero.src`'s division is deliberately on its own line inside a multi-line expression to pin this |
| Dynamically-sized arrays without an initializer aren't supported | `int arr[n];` now computes `ARR_NEW`'s size at runtime when `n` isn't a compile-time literal — `tests/dynamic_array.src` |
| No protection against runaway recursion or infinite loops | The VM now caps total executed quads (`--max-steps`) and call-stack depth (`--max-depth`) — see §1, `tests/infinite_recursion.src` |
| Struct/array literal-init and equality untested by the 5 required programs | `tests/struct_array_init.src` now exercises both |
| A `match` expression used as a bare assignment/call-argument value hangs the parser | **Misattributed to `match` — it never was.** `r = match (x) { 1 => "one", _ => "other" };` (`samples/prog4_structs_match_exceptions.src:101`) and `print(match (x) { ... })` always parsed fine; a bare stray top-level `}` with no `match` anywhere reproduces the exact same hang. Root cause: `grammar_engine.py`'s `many_rec` could make zero progress when error recovery stranded the cursor on a `}` that the enclosing loop's `is_stop` didn't recognize as a stop (true for the top-level `program` loop, which never stops on `}`) — recovery would land on the same token forever, appending an unbounded stream of errors. Fixed by giving `many_rec` a forward-progress invariant: if an iteration consumes zero tokens and the loop isn't about to stop anyway, force a one-token step. The original repro used `;` to separate match-*expression* arms, which is actually invalid (expression arms are comma-separated; `;` is the match-*statement* form) — that's now a single precise `[SYNTAX ERROR]` instead of a cascade that happened to strand the cursor on `}`. See `tests/stray_brace.src` (pins the engine fix, `match`-independent) and `tests/match_expr_semicolon.src` (pins the new diagnostic) |

Two small open quirks in the IDE (`cetirim_ide.py`), found while verifying
it against the current pipeline and recorded here in that same spirit:

- **Submitting an empty line in the IDE terminal raises a spurious
  `Unexpected end of input` runtime error.** The IDE's `input_provider`
  returns the entry's text verbatim, so an empty submit hands
  `_next_input_token` the empty string — which is the CLI's *EOF* signal.
  At a real terminal an empty line arrives as `"\n"`, splits to zero
  tokens, and simply re-prompts. Fix sketch: return `value + "\n"` from
  the provider so the two paths agree.
- **The call-stack panel's `"(top level)"` fallback is dead code.**
  `reversed(executor.call_names) or ["(top level)"]` never takes the `or`
  branch — `reversed()` returns an (always-truthy) iterator, not a list.
  Harmless in practice: the synthetic prologue quads (`CALL main` and
  friends) carry `line=None` and can never pause, so `call_names` is
  non-empty at every pause the panel can ever render.

---

## 3. Scope limitations

These are things a production language/toolchain would have, deliberately
left out because they're outside this project's scope:

- **Single-file programs only** — no `import`/module system.
- **No standard library** beyond `print`/`input` — no math, string, or
  array/collection library functions (sort, map, filter, etc. all have to be
  hand-written, as `prog2`'s bubble sort demonstrates).
- **No arbitrary type casting** — only the three numeric promotions
  (`char->int`, `char->float`, `int->float`) are ever compiled to a `CAST`
  quad; there's no explicit cast syntax in the language at all.
- **IR optimization is opt-in and deliberately shallow.** `ir.py` itself is
  still a direct, unoptimized 1:1 structural lowering; `optimizer.py` is a
  separate opt-in pass (`-O`) implementing three of the seven techniques in
  `docs/Code_Optimization_Techniques.md`. What it does *not* do, and why:
  - **Constant propagation is per-basic-block plus a write-once
    program-wide rule** — there is no real dataflow analysis, so a constant
    that reaches a join point along both predecessors (e.g. assigned the
    same literal in both arms of an `if`) is not propagated past the merge.
    The known-constants table is cleared at every `LABEL`.

    Worked example. `x` is provably `5` at the `return` — both arms assign
    that same literal, and no other path reaches the merge:

    ```
    int pick(int c) {
        var int x;
        if (c > 0) { x = 5; } else { x = 5; }
        return x;
    }
    ```

    Even under `-O`, that still compiles to a variable read:

    ```
    02: (ASSIGN, 0, -, pick.x)
    03: (GT, pick.c, 0, _t1)
    04: (JZ, _t1, -, L2)
    05: (ASSIGN, 5, -, pick.x)     then-arm
    06: (JMP, -, -, L3)
    07: (LABEL, -, -, L2)
    08: (ASSIGN, 5, -, pick.x)     else-arm, same literal
    09: (LABEL, -, -, L3)
    10: (RETURN, pick.x, -, -)     <- not folded to (RETURN, 5, -, -)
    ```

    A dataflow-based optimizer merges `L3`'s two predecessors, proves
    `x = 5` there, and emits `(RETURN, 5, -, -)`, deleting both stores and
    the entire branch. This one clears its table at `LABEL L3`, and the
    write-once rule can't help either, since `pick.x` has three definitions.

    The condition has to be genuinely unknowable for the limitation to be
    visible at all: written as `if (1 > 0)` instead, the condition folds,
    the else-arm becomes unreachable, exactly one store survives — and the
    write-once rule then *does* reduce the whole function body to
    `(RETURN, 5, -, -)`. That contrast is the clearest statement of where
    the boundary actually sits: this optimizer reasons well along a single
    path and not at all across a merge.
  - **Only `int`-typed operands get the additive identities.** `x + 0`,
    `x - 0` and `x % 1` are skipped unless the operand has an `int` entry in
    `var_types`, which a temporary never does — so `+n - 0` simplifies the
    unary `+` but leaves the `- 0` alone. The reason is `float`: `-0.0 + 0`
    is `0.0`, a value that prints differently. `x * 1` and `x / 1` need no
    such guard and are always applied.
  - **`TOSTR` is never constant-folded.** Folding it would mean
    reimplementing `IRExecutor._format`'s float/bool/array spelling in a
    second place, and any drift between the two would silently change
    printed output.
  - **No common subexpression elimination, loop-invariant code motion, copy
    propagation, or strength reduction.** The first two need dataflow across
    blocks; copy propagation needs alias reasoning the write-once rule
    sidesteps; strength reduction buys nothing on a Python-hosted VM where
    every opcode costs a dict lookup regardless.
  - **Operand classification is a hand-maintained table** (`optimizer._SLOTS`).
    A new opcode added to `ir.py` without a row there raises `OptimizerError`
    rather than being silently mis-analyzed, but it does have to be added by
    hand.
- **Python's native numeric semantics** — integers are arbitrary-precision
  (no overflow behavior to define or test) and floats are native Python
  doubles; the language doesn't define its own overflow/precision rules the
  way a specification for a "real" systems language would need to.
