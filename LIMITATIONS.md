# Known Limitations, Compromises, and Design Tradeoffs

This document is a candid audit of where the interpreter (`semantics.py` →
`ir.py` → `interpreter.py`) is not fully hardened, where it makes a
deliberate simplifying choice instead of a "correct" one, and where it's
simply out of scope for this project. It's organized in three tiers:

1. **Intentional design decisions** — made on purpose, defensible, and
   documented in `CLAUDE.md`. Listed here for completeness, with the
   reasoning restated.
2. **Known gaps** — real edge cases discovered while building the pipeline
   that aren't hardened against. None of these are hit by the 5 required
   sample programs, which is exactly why they went unnoticed until reviewed
   deliberately.
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
| `int / int` truncates toward zero (C-style) | Matches `prog1`'s existing behavior and is the more common convention for a C-influenced language |
| `..` ranges are inclusive | `for (i in 0..4)` visits all 5 elements of a 5-element array |
| Arrays/structs are reference values; scalars are call-by-value | Falls out of storing them as native Python `list`/dict objects — not enforced by semantics.py, just a consequence of how the VM represents values |
| First matching `catch` wins | Catch clauses carry no exception type, so there's no way to discriminate between them — see [§2](#multiple-catch-clauses-are-mostly-decorative) for what this actually means |
| `+` is numeric only, no string concatenation operator | Interpolated strings (`` `{a} {b}` ``) are the concatenation mechanism |
| `let` and `multi_assign` bindings are always mutable | Neither construct has a `val`/`const`-style marker in the grammar |
| Numeric coercion is `char -> int -> float` only | Everything else (`int -> char`, `float -> int`, anything involving `string`/`bool`) must match exactly |

---

## 2. Known gaps

### Thrown values aren't required to be strings

The catch variable is typed `string` (`semantics.py`, `_analyze_try`), but
`ThrowStmt`'s value is only type-*inferred*, never checked against
`T_STRING`:

```python
elif kind == "ThrowStmt":
    self._infer_expr(node.fields["value"])   # no can_coerce(..., T_STRING) check
```

`throw 42;` passes semantic analysis. At runtime it works "fine" in the
sense that Python doesn't crash — `CATCH_STORE` just stores whatever value
`current_exception` holds — but the catch variable's *declared* type (string)
no longer matches what's actually in it, and downstream string operations on
it (e.g. interpolating it into a message) would just stringify the int,
silently papering over the mismatch rather than flagging it.

### `input()` into a non-scalar variable silently misbehaves

`_analyze_builtin`'s check for `input()` targets only verifies the argument
is a plain, mutable identifier — it never restricts the target's *type*:

```python
for a in node.fields["args"]:
    if a.kind != "Identifier":
        self.error(a, "input() arguments must be plain variable names")
        continue
    self._infer_expr(a)
    sym = self.st.node_symbol.get(id(a))
    if isinstance(sym, Symbol) and not sym.mutable:
        self.error(a, f"Cannot input() into immutable variable '{sym.name}'")
```

So `input(myArray)` or `input(myStruct)` passes semantic analysis. At
runtime, `IRExecutor._coerce()` looks up the type tag (`"array"` / `"struct"`),
doesn't recognize it, and falls through to its default branch — the raw text
token gets stored as a plain string, silently overwriting the array/struct
with a string value instead of raising an error.

### Multiple `catch` clauses are mostly decorative

Because catch clauses carry no exception type, `ir.py`'s `gen_try` only ever
lowers `catch_clauses[0]` — every subsequent clause is still walked and
type-checked by `semantics.py`, but its compiled code is never emitted, so
it's permanently dead. A program with `catch (e) {...} catch (special) {...}`
will *never* run the second block, regardless of what's thrown or what a
reader might reasonably expect from the syntax. This is consistent with
`prog3`/`prog4`'s actual observed behavior, but is worth knowing explicitly
before writing a new test program that depends on selective catch dispatch.

### Named arguments require covering every parameter

`_infer_call` requires a named call to supply *all* parameters by name, with
no positional/named mixing and no default values:

```python
if len(named) != len(args):
    self.error(node, "Cannot mix positional and named arguments in a call")
...
missing = [p for p in sig.param_names if p not in provided]
if missing:
    self.error(node, f"Missing argument(s) for parameter(s): {', '.join(missing)}")
```

There's no language feature for default parameter values, so this isn't
wrong per se, but it means named arguments are really just "positional
arguments written in a different order," not a partial-application
mechanism.

### Destructuring can't discard a value

`let`/`multi_assign` always bind every position to a name — there's no
wildcard (`_`) form for `let a, _ = pair();` the way `match` has one for
patterns. If you only care about one of several return values, you still
have to name (and declare) all of them.

### Match "predicate pattern" detection is a shape heuristic, not a real distinction

A match arm's pattern is treated as an independent boolean predicate
(evaluated on its own, *not* compared to the subject) whenever its AST's
**top node** is a relational/logical `BinaryExpr` or a `!`-`UnaryExpr`:

```python
def _is_predicate_pattern(self, pattern):
    if pattern.kind == "BinaryExpr" and pattern.fields.get("op") in _PREDICATE_BINOPS:
        return True
    return pattern.kind == "UnaryExpr" and pattern.fields.get("op") == "!"
```

This correctly handles `code > 9 =>` (`prog4`), but it also means there is
**no way to write a match pattern that equality-compares the subject against
a boolean-valued sub-expression** — any pattern whose outermost operator is
`==`, `!=`, `<`, `>`, `<=`, `>=`, `&&`, `||`, or `!` is always treated as
"evaluate me directly," never "compare me to the subject." This is an
AST-shape heuristic standing in for a real type-based distinction, and it's
the kind of thing a more adversarial test case could expose.

### `MOD` doesn't get the same C-style treatment as `DIV`

Integer division truncates toward zero (assumption in §1), but modulo uses
plain Python `%`, which is **floor-based** (result takes the sign of the
divisor), not C's truncated modulo (result takes the sign of the dividend):

```python
BINARY_OPS = {
    ..., "DIV": _div, "MOD": operator.mod, ...
}
```

For non-negative operands (everything the 5 sample programs use) these
agree. For negative operands they diverge — e.g. `-7 % 3` is `2` here
(Python/floor semantics) but would be `-1` under C's truncated semantics.
This inconsistency (DIV is C-style, MOD is not) was a deliberate scope call
during Phase D, not something anyone asked to be fixed.

### `ARR_NEW`'s VM handler has no negative-size guard of its own — but it's currently unreachable

```python
elif op == "ARR_NEW":
    size = self.resolve(q.arg1)
    self.set_var(q.result, [DEFAULTS.get(q.arg2)] * size)
```

Python's list repetition silently treats a negative multiplier as zero, so
*if* a negative size ever reached this quad, it would produce a silent
empty array rather than an error. In practice this isn't reachable through
`ir.py` as written: a literal size like `arr[-1]` parses as `UnaryExpr(-,
Literal(1))`, not a bare `INTEGER_LIT`, so `_const_int_or_none` never
returns a negative value; every `ARR_NEW` emitted from a literal-size
declaration gets a non-negative `Const`, and a non-literal size with no
initializer is rejected at compile time (see below) before it ever reaches
`ARR_NEW`. Worth knowing about only because the VM opcode itself doesn't
enforce the invariant — it's relying on `ir.py` never generating a bad
call, which is a coupling worth being aware of if either file changes
independently later.

### No "all paths return a value" checking

A non-`void` function that doesn't actually return on every code path (e.g.
an `if` with no `else`, where only the `if`-branch returns) is not flagged
as a semantic error. `ir.py` always emits an implicit fallthrough `RETURN`
(no value) at the end of every function body, so a caller silently receives
Python's `None`:

```c
int maybe(bool flag) {
    if (flag) { return 1; }
    // no else, no trailing return
}
```

Calling `maybe(false)` and printing the result prints the literal text
`None` — confirmed by running it — rather than a semantic error at compile
time or a clear runtime diagnosis. It only surfaces indirectly, e.g. as a
`TypeError` the next time that `None` is used in arithmetic (converted to a
generic `[RUNTIME ERROR]`, not a "missing return" message).

### No dead-code / unreachable-pattern detection

- Code after a `return`/`break`/`continue`/`throw` within the same block is
  silently compiled and simply never reached — not flagged.
- Two `match` arms with the same literal pattern (e.g. `0 => a; 0 => b;`)
  aren't flagged as a duplicate; the second is just permanently unreachable.
  (The grammar *does* catch the one case explicitly mentioned in the spec —
  a case appearing after the wildcard `_` — but that's a parser-level check,
  not a general reachability analysis.)

### Interpolated-string edge cases

- **`\{` can't escape a literal brace.** The scanner resolves escape
  sequences before the interpolation splitter (`grammar.py`) ever sees the
  string, so by the time splitting happens there's no way to tell "a literal
  backslash followed by a brace" apart from "an escaped brace."
- **The brace-matching for `{expr}` is a naive character-level depth
  counter**, not token-aware. An embedded expression that itself contains a
  string literal with a `}` or `{` inside it (e.g. `` `{f("}")}` ``) will
  confuse the splitter, since it doesn't know it's inside a nested string
  literal when counting braces.

### Comparing two one-character strings can silently become a char comparison

`_numeric()` promotes any length-1 Python string to its ordinal so that
`char`-vs-`int`/`float` comparisons work (this is required — `semantics.py`
explicitly permits char/numeric cross-comparisons). But at the VM level, a
`char` value and a genuine length-1 `string` value are the *same* Python
object shape (`isinstance(value, str) and len(value) == 1`), so:

```python
def _numeric(value):
    return ord(value) if isinstance(value, str) and len(value) == 1 else value
```

comparing two one-character *strings* with `==`, `<`, etc. gets silently
routed through the same ordinal-promotion path as a char comparison, instead
of a plain string comparison. None of the 5 sample programs ever compare
strings with relational/equality operators, so this has never surfaced.

### Runtime error line numbers are per-statement, not per-sub-expression

`ir.py` stamps every quad with `self._current_line`, which is updated once
per statement (`gen_stmt`) — not for every individual sub-expression inside
it. A division-by-zero buried inside a long compound expression will report
the line of the *enclosing statement*, which is usually good enough but
isn't pinpoint-accurate for a statement that spans (or is built from
sub-expressions on) multiple lines.

### Dynamically-sized arrays without an initializer aren't supported

The grammar allows an arbitrary expression as an array size (`int arr[n];`),
but `ir.py` only knows how to zero-fill an array at declaration time when the
size is a literal integer known at compile time:

```python
size = type_[2]
if size is None:
    raise IRGenError(f"Array '{sym.name}' needs an explicit size or initializer")
```

A variable-length array with no initializer list raises a compile-time
`IRGenError` rather than allocating at runtime based on the variable's value.
(An array *with* an initializer list works regardless, since its size comes
from the list's length instead.)

### No protection against runaway recursion or infinite loops

The VM's `run()` is a flat `while` loop — `CALL`/`RETURN` just push/pop
`self.call_stack` (a plain Python list) rather than making a real recursive
Python function call, so a deeply-recursive *source* program doesn't blow
Python's own call stack the way a naive tree-walking interpreter would.
That's a nice property, but it also means there's no cap at all: an
infinite-recursion or infinite-loop bug in a test program will hang or grow
`call_stack` until the process runs out of memory, rather than failing with
a clean "stack overflow" or "timeout" diagnostic.

### Struct/array literal-initialization and equality are implemented but untested by the 5 required programs

- `_check_initializer_list`'s struct branch (positional field initialization
  from `{val1, val2, ...}`, mapped in `field_order`) is implemented
  generically, but no sample program initializes a struct with a brace list
  — `prog4` only ever assigns struct fields individually (`s1.name = ...`).
- Comparing two structs or two arrays with `==`/`!=` type-checks fine
  (`types_equal` allows it) and works at runtime via Python's native
  dict/list structural equality — but this is an emergent property of the
  implementation, not a deliberately designed and tested language feature,
  and it isn't exercised anywhere in the sample programs.

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
- **No IR optimization pass** — `ir.py` is a direct, unoptimized 1:1
  structural lowering (e.g. short-circuit `&&`/`||` always emits the full
  jump sequence, even when one operand is a trivial constant). This is fine
  for a class project's program sizes but wouldn't scale to real workloads.
- **Python's native numeric semantics** — integers are arbitrary-precision
  (no overflow behavior to define or test) and floats are native Python
  doubles; the language doesn't define its own overflow/precision rules the
  way a specification for a "real" systems language would need to.
