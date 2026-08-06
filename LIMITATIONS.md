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

| Submitting an empty line in the IDE terminal raised a spurious `Unexpected end of input` | The IDE's `input_provider` now returns `value + "\n"` (`readline()`'s contract — a line always ends in a newline, and `""` means EOF), so an empty submit splits to zero tokens and re-prompts, exactly like an empty line at the CLI — `tests/ide_smoke.py`'s empty-submit checks |
| The IDE call-stack panel's `"(top level)"` fallback was dead code | `reversed(...)` returns an always-truthy iterator, so the `or` never fired; it's now materialized to a list first, making the fallback reachable — `tests/ide_smoke.py`'s fallback check |
| IDE Stop couldn't interrupt an `input()` wait or a plain-Run loop, and re-running left the old run alive underneath the new one | Three fixes behind one mechanism: `IRExecutor.run()` checks `_stop_requested` per quad even with no debugger attached, `_next_input_token` checks it per read (both no-ops for the CLI, where the flag is never set), and the IDE resolves a pending input request on Stop, enables Stop for plain Run, and retires any live previous run before starting a new one (a per-run serial makes every stale worker's UI callbacks inert) — `tests/ide_smoke.py`'s three stop/lifecycle checks |
| The status bar's `Paused at line N` was clobbered by the editor's debounced refresh (documented as "unreliable" in `ide_smoke.py`'s docstring) | The status bar is now segmented; the run/debug state lives in its own `status_run` segment that `_refresh_all` never writes — `tests/ide_smoke.py`'s status-segment check |
| Opening a file marked the buffer dirty immediately | The programmatic insert queued a `<<Modified>>` event that fired *after* `open_file` reset `dirty`; invisible before, exposed by the new editor-tab `●` unsaved marker. `new_file`/`open_file` (and the startup sample load) now clear `edit_modified` so the queued event no-ops — the dot only appears after real edits |
| The IDE's syntax highlighting mis-coloured comments, keywords and strings | It layered two regexes (one for comments, one for `name (` call names) over the scanner's tokens, and let Tk tag *priority* settle the overlaps — so a word before a `(` inside a comment lost its grey (`checks/check1_declarations.src`'s header block), `print`/`for` were painted as calls, a `//` or `/* */` inside a string literal turned the string grey, and an unterminated block comment stayed uncoloured until its `*/` was typed. Separately, the token spans were measured with `len(tok.lexeme)`, which is the *escape-resolved* text for a quoted literal, so `"a\tb"` was tagged one character short (and a resolved `\n` in the lexeme corrupted the end index outright). `highlight()` is now entirely scanner-driven: the scanner records every comment's extent as it skips it and stamps a true source end on the quoted forms, and a call name is recognised by the next *token* being `(` rather than by a regex that cannot see comments or strings. The tags no longer overlap at all, so creation order stopped mattering — `tests/ide_smoke.py`'s highlighting checks |
| An unterminated block comment leaked its last character back out as a token | `_skip_whitespace_and_comments`'s block-comment loop was bounded by `len(self.source) - 1`, stopping one character short of the end, so `/* comment` at EOF (exactly what the IDE's buffer looks like mid-typing, since it has no trailing newline) emitted a stray `IDENTIFIER` for the final character. `_peek(1)` already returns a `"\0"` sentinel past the end, so the bound is now `len(self.source)` and the `*/` test is still safe on the last character — `tests/ide_smoke.py`'s last-character check |

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
  - **The fixpoint loop is capped at ten rounds** (`optimizer._MAX_ROUNDS`), and
    a long enough chain of dead stores reaches that cap. Dead store elimination
    computes its live-name set once per round, so it can only remove the
    outermost link of a chain per pass: deleting the store to `a14` is what
    makes `a13` dead, which is not noticed until the next round. Programs
    written by hand settle in two or three rounds — `tests/optimizer_deep_chain.src`
    is a deliberately over-long chain that does not. A capped run is reported
    rather than hidden (a `Note` line in the report, `stats.converged` false in
    the `--json` payload, pinned by `run_tests.check_optimizer_cap_note`), and
    what it leaves behind are quads the unoptimized program would have run
    anyway — so the output is less reduced, never wrong. Making one round
    iterate to an inner fixpoint would fix it, at the cost of changing every
    reported round count.
  - **`CAST` is in `_PURE_OPS`, so a `CAST` nothing reads is deletable even
    though `_cast_const` can raise.** `ord()` on a multi-character string
    raises, and `int()` on a non-numeric string raises. This is currently
    unreachable, not merely unlikely: `ir.py` emits `CAST` only for the three
    semantics-approved numeric promotions (`_gen_expr_coerced`,
    `_gen_char_promoted`), so its operand is always a number or a genuine
    length-1 `char`. It is nonetheless the one member of the deletable set that
    isn't guarded the way `DIV`/`MOD`/`ARR_NEW` are in `_is_removable`, and it
    would need a guard first if the language ever gained a general cast syntax.
  - **Redundant-jump removal takes one jump per round.**
    `_remove_redundant_jumps` scans the pre-compaction list and stops at the
    first quad that is not a `LABEL`/`NOP`, including a `JMP` the same sweep
    has already marked dead — so a stack of `JMP L; JMP L; LABEL L` loses one
    jump per round instead of all of them at once. Only the second and later
    jumps in such a stack are unreachable anyway, so `_remove_unreachable`
    normally gets them first; the effect is cosmetic. (The `NOP` case is dead
    code in both senses: nothing in `ir.py` emits a `NOP`.)
- **Python's native numeric semantics** — integers are arbitrary-precision
  (no overflow behavior to define or test) and floats are native Python
  doubles; the language doesn't define its own overflow/precision rules the
  way a specification for a "real" systems language would need to.
