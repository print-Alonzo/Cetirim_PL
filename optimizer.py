"""Intermediate-representation optimizer: rewrites the quadruple list
`ir.py` produced into an equivalent but shorter/cheaper one, and records
every single change it made so an IDE can show the before/after side by
side.

Phase position: strictly optional, and strictly between `ir.generate()` and
`interpreter.IRExecutor`. Nothing upstream knows this module exists, and the
default pipeline never calls it - `python interpreter.py <file>` runs the
unoptimized quads exactly as before, byte-for-byte (which is what keeps the
committed `progN_ir.txt` goldens valid). Optimization is opt-in via
`interpreter.py -O`, or inspectable on its own via this module's CLI.

Three techniques are implemented, from `docs/Code_Optimization_Techniques.md`:

  1. **Constant propagation** (with constant folding as its companion) -
     substitutes a variable's known literal value for the variable itself,
     then evaluates any operation whose operands all became literals.
  2. **Algebraic simplification** - rewrites identity operations (`x * 1`,
     `x + 0`, unary `+`, ...) into a plain copy.
  3. **Dead code elimination** - removes unreachable quads, jumps to the
     immediately following label, stores nothing ever reads, and labels
     nothing ever jumps to.

The three run in a loop until nothing changes (or `_MAX_ROUNDS` rounds
elapse), because each one exposes work for the others: folding a condition
to a constant makes a whole branch unreachable, and removing that branch
makes the stores feeding it dead.

Viewability: `optimize()` returns an `OptResult` carrying the original
quads, the optimized quads, and a `records` list with one entry per
transformation (technique, kind, the original quad index, the source line,
the quad text before and after, and a human-readable explanation).
`build_view()` renders that as the payload the IDE's Optimizer tab (and the
`--json` flag) consumes; `format_report()`
renders it as an annotated text listing for the terminal. See this module's
CLI at the bottom, and README.md's "IR Optimization" section for the JSON
schema.

Safety model - every rewrite below is justified by one of these facts about
the IR, so none of them can change what a program prints:

  - `semantics.py` gives every variable a program-wide-unique `ir_name`, so
    "the only write to this name anywhere in the quad list" is a meaningful
    question to ask, and "this name is read nowhere" is decidable with a
    single linear scan (no scope walking, no aliasing between two same-named
    locals).
  - Declarations always precede uses, and every backward jump is a loop
    back-edge whose body re-executes its own declarations, so a name whose
    single definition is a literal store is that literal at every read.
  - Every point control can enter other than "the previous quad" is a
    `LABEL` (jump targets and `TRY_PUSH` handlers alike), so clearing the
    known-constants table at each `LABEL` is enough to make the per-block
    analysis sound.
  - Arithmetic operands are always `int` or `float` at the quad level -
    `semantics.py` rejects non-numeric arithmetic, and `ir.py` inserts an
    explicit `CAST -> int` for every `char` operand - which is what makes
    the algebraic identities below exact rather than approximate.
"""

import argparse
import json
import operator
import os
import sys
from dataclasses import dataclass, field

from ir import Const, IRGenError, Quad, _div, _fmt, _mod, format_quads, generate
from parser import parse_source
from semantics import analyze

TECHNIQUES = ("constant-propagation", "algebraic-simplification", "dead-code-elimination")

# Cap on the fixpoint loop below. Each round can only shrink the program or
# rewrite a quad into a strictly simpler form, so this is a safety net
# against a hypothetical oscillating rule, not a tuning knob - real programs
# reach a fixpoint in two or three rounds.
#
# Hitting the cap is not a correctness problem: whatever the loop stopped
# short of removing is still a quad the unoptimized program would have run.
# It does mean the listing is not as short as it could be, though - dead
# store elimination peels one link off a dead dependency chain per round, so
# a chain longer than the cap keeps some of its links. `optimize()` therefore
# reports whether it converged, and `format_report()` says so, rather than
# letting a partially optimized listing look final. See LIMITATIONS.md.
_MAX_ROUNDS = 10

VIEW_VERSION = 1


class OptimizerError(Exception):
    """An opcode this module has no operand classification for. Should be
    unreachable - `_SLOTS` below covers every opcode `ir.py` can emit - and
    exists so an un-classified opcode fails loudly here instead of being
    silently mis-analyzed (e.g. treated as having no reads, and its inputs
    then deleted as dead)."""


# -- operand classification -------------------------------------------------
#
# The whole module rests on knowing, for each opcode, which of its three
# operand slots the VM reads as a *value*, which it writes, and which are
# neither (a label name, a type tag, a struct/function name). Getting one of
# these wrong is the only way this optimizer can silently corrupt a program,
# so each row below is stated against `interpreter.py`'s `_exec` handler for
# that opcode rather than inferred from the opcode's name.

READ = "read"            # VM calls resolve() on it -> substitutable, counts as a use
READ_DEF = "read_def"    # container the VM resolves *and* mutates in place
WRITE = "write"          # VM calls set_var() with it
WRITE_TUPLE = "write_t"  # tuple of names set_var() is called with (None = `_` discard)
LABEL_DEF = "label_def"  # this quad *is* the label
LABEL_REF = "label_ref"  # names a label to jump to
META = "meta"            # not a value at all: a type tag, struct/function name, arg count

_SLOTS = {
    # control flow / misc
    "LABEL": (None, None, LABEL_DEF),
    "JMP": (None, None, LABEL_REF),
    "JZ": (READ, None, LABEL_REF),
    "JNZ": (READ, None, LABEL_REF),
    "NOP": (None, None, None),
    "HALT": (None, None, None),
    # data movement
    "ASSIGN": (READ, None, WRITE),
    "CAST": (READ, META, WRITE),  # arg2 is a bare type-tag string ("int"/"float")
    # arithmetic / relational
    "ADD": (READ, READ, WRITE),
    "SUB": (READ, READ, WRITE),
    "MUL": (READ, READ, WRITE),
    "DIV": (READ, READ, WRITE),
    "MOD": (READ, READ, WRITE),
    "EQ": (READ, READ, WRITE),
    "NE": (READ, READ, WRITE),
    "LT": (READ, READ, WRITE),
    "GT": (READ, READ, WRITE),
    "LE": (READ, READ, WRITE),
    "GE": (READ, READ, WRITE),
    # logical / unary
    "UMINUS": (READ, None, WRITE),
    "UPLUS": (READ, None, WRITE),
    "NOT": (READ, None, WRITE),
    # functions
    "FUNC_BEGIN": (None, None, META),  # result is the function's name
    "FUNC_END": (None, None, META),
    "PARAM": (READ, None, None),
    "CALL": (META, META, WRITE),  # arg1 = function name, arg2 = arg count
    "RETURN": (READ, None, None),
    # tuples
    "TUPLE_NEW": (None, None, WRITE),
    "TUPLE_APPEND": (READ, READ, WRITE),
    "UNPACK": (READ, None, WRITE_TUPLE),
    # arrays
    "ARR_NEW": (READ, META, WRITE),  # arg2 is a TypeDesc, not a value
    "ARR_LOAD": (READ, READ, WRITE),
    "ARR_STORE": (READ, READ, READ_DEF),  # result names the array being mutated
    "ARR_LEN": (READ, None, WRITE),
    # structs
    "STRUCT_NEW": (META, None, WRITE),  # arg1 is a bare struct-name string
    # FIELD_LOAD/FIELD_STORE's field-name slot is always a Const in practice
    # (see ir.py's `_gen_expr_dispatch`/`_gen_assign`), but it is classified
    # META rather than READ so that a bare field-name string could never be
    # mistaken for a same-named global and substituted away.
    "FIELD_LOAD": (READ, META, WRITE),
    "FIELD_STORE": (META, READ, READ_DEF),
    # strings
    "CONCAT": (READ, READ, WRITE),
    "TOSTR": (READ, None, WRITE),
    # exceptions
    "TRY_PUSH": (LABEL_REF, None, None),
    "TRY_POP": (None, None, None),
    "THROW": (READ, None, None),
    "RETHROW": (None, None, None),
    "CATCH_STORE": (None, None, WRITE),
    # I/O
    "PRINT": (READ, None, None),
    "PRINT_SEP": (None, None, None),
    "PRINTLN": (None, None, None),
    "INPUT": (None, None, WRITE_TUPLE),
}

_SLOT_NAMES = ("arg1", "arg2", "result")


def _roles(q):
    try:
        return _SLOTS[q.op]
    except KeyError:
        raise OptimizerError(f"No operand classification for opcode '{q.op}'") from None


def _subst_slots(q):
    """Slot names safe to replace with a `Const`: pure value reads only.
    `READ_DEF` is deliberately excluded - substituting a literal for the
    array/struct a `ARR_STORE`/`FIELD_STORE` mutates would store into a
    throwaway value instead of the real container."""
    return [name for name, role in zip(_SLOT_NAMES, _roles(q)) if role == READ]


def _read_names(q):
    """Every variable/temp name this quad reads, including the container a
    store mutates - this is the "is anything still using this name?" query
    dead-store elimination depends on being exhaustive."""
    for name, role in zip(_SLOT_NAMES, _roles(q)):
        if role in (READ, READ_DEF):
            operand = getattr(q, name)
            if isinstance(operand, str):
                yield operand


def _def_names(q):
    """Every name this quad may write. `READ_DEF` counts as a write too:
    it doesn't rebind the name, but treating an in-place mutation as a
    definition is what stops an array that only ever gets stored into from
    looking like a write-once constant."""
    for name, role in zip(_SLOT_NAMES, _roles(q)):
        operand = getattr(q, name)
        if role in (WRITE, READ_DEF):
            if isinstance(operand, str):
                yield operand
        elif role == WRITE_TUPLE and isinstance(operand, tuple):
            for target in operand:
                if target is not None:
                    yield target


def _label_refs(q):
    """Label names this quad jumps to (or registers as an exception
    handler) - a `LABEL` quad may only be deleted when nothing here names
    it, or `IRExecutor.__init__`'s label table would come up short."""
    for name, role in zip(_SLOT_NAMES, _roles(q)):
        if role == LABEL_REF:
            operand = getattr(q, name)
            if operand is not None:
                yield operand


# -- purity -----------------------------------------------------------------

# Opcodes whose only effect is writing their result slot, so deleting one
# whose result is never read cannot change the program's behavior. I/O,
# calls, throws, stores, and the loads that can raise (ARR_LOAD's bounds
# check, ARR_LEN, FIELD_LOAD) are all deliberately absent.
_PURE_OPS = {
    "ASSIGN", "CAST", "ADD", "SUB", "MUL", "UMINUS", "UPLUS", "NOT",
    "EQ", "NE", "LT", "GT", "LE", "GE", "CONCAT", "TOSTR",
    "TUPLE_NEW", "STRUCT_NEW",
}

# Opcodes that arithmetic-fold into a literal. TOSTR is pure but absent on
# purpose: reproducing `IRExecutor._format`'s float/bool/array spelling here
# would duplicate formatting rules that only the VM should own.
_FOLD_BINARY = {
    "ADD": operator.add, "SUB": operator.sub, "MUL": operator.mul,
    "DIV": _div, "MOD": _mod,
    "EQ": operator.eq, "NE": operator.ne, "LT": operator.lt,
    "GT": operator.gt, "LE": operator.le, "GE": operator.ge,
    "CONCAT": operator.add,
}
_FOLD_UNARY = {"UMINUS": operator.neg, "UPLUS": lambda v: v, "NOT": operator.not_}

_OP_SYMBOL = {
    "ADD": "+", "SUB": "-", "MUL": "*", "DIV": "/", "MOD": "%",
    "EQ": "==", "NE": "!=", "LT": "<", "GT": ">", "LE": "<=", "GE": ">=",
    "CONCAT": "++",
}

_UNCONDITIONAL_ENDERS = {"JMP", "RETURN", "THROW", "RETHROW", "HALT"}
_LEADER_OPS = {"LABEL", "FUNC_BEGIN"}
# Markers that end a straight-line run of quads for the constants table:
# a LABEL can be entered from anywhere, and FUNC_BEGIN/FUNC_END bracket a
# body a CALL jumps into with a brand-new frame.
_ENV_RESET_OPS = {"LABEL", "FUNC_BEGIN", "FUNC_END"}

_NO_FOLD = object()


def _is_plain_int(value):
    """True for a genuine `int` literal. `bool` is excluded even though
    Python makes it an `int` subclass: `true` and `1` are different values
    to this language (`_format` prints them differently), so an identity
    keyed on "is this operand the number 1" must not fire on `true`."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_int_const(operand, wanted):
    """True when `operand` is exactly the integer literal `wanted`. Floats
    are rejected on purpose - `x * 1.0` widens an `int` result to `float`,
    so it is not the identity `x * 1` is."""
    return isinstance(operand, Const) and _is_plain_int(operand.value) and operand.value == wanted


def _is_int_operand(operand, var_types):
    """True when `operand` is known to hold an `int`. Only two things
    qualify: an integer literal, or a declared variable whose recorded type
    tag is `int`. A temporary has no `var_types` entry, so it never
    qualifies - which is the conservative answer, since the identities that
    consult this are the ones that are unsound on `float` (`-0.0 + 0`
    evaluates to `0.0`, losing the sign)."""
    if isinstance(operand, Const):
        return _is_plain_int(operand.value)
    if isinstance(operand, str):
        return var_types.get(operand) == "int"
    return False


def _cast_const(value, type_tag):
    """Compile-time twin of `IRExecutor._cast` - kept deliberately
    identical (including the `ord()` promotion of a `char`, which is how a
    folded `'A' + 1` reaches 66) so a folded CAST and an executed one can
    never disagree."""
    if type_tag == "float":
        return float(ord(value)) if isinstance(value, str) else float(value)
    if type_tag == "int":
        return ord(value) if isinstance(value, str) else int(value)
    return value


def _const_operands(*operands):
    """True when every operand is a `Const` holding a real value. A
    `Const(None)` (the placeholder `ir.py` gives a non-primitive declarator)
    is excluded: it is not a language value and nothing sensible can be
    computed from it."""
    return all(isinstance(o, Const) and o.value is not None for o in operands)


def _fold_value(q):
    """Evaluate `q` at compile time if all its value operands are literals,
    returning the result or `_NO_FOLD`.

    Any exception from the evaluation means "don't fold": either the
    operands are a shape this optimizer shouldn't reason about, or the
    operation genuinely fails - and in the second case leaving the quad
    alone is exactly right, because the VM then raises the same error at
    the same source line it always would have. That is also why a `DIV`/
    `MOD` by a literal zero is refused below rather than folded: the
    `[RUNTIME ERROR] Division by zero` has to still happen."""
    op = q.op
    if op == "CAST":
        if not _const_operands(q.arg1):
            return _NO_FOLD
        try:
            return _cast_const(q.arg1.value, q.arg2)
        except Exception:
            return _NO_FOLD
    if op in _FOLD_UNARY:
        if not _const_operands(q.arg1):
            return _NO_FOLD
        try:
            return _FOLD_UNARY[op](q.arg1.value)
        except Exception:
            return _NO_FOLD
    if op in _FOLD_BINARY:
        if not _const_operands(q.arg1, q.arg2):
            return _NO_FOLD
        if op in ("DIV", "MOD") and q.arg2.value == 0:
            return _NO_FOLD  # leave the runtime error intact
        try:
            return _FOLD_BINARY[op](q.arg1.value, q.arg2.value)
        except Exception:
            return _NO_FOLD
    return _NO_FOLD


def _is_removable(q):
    """True when deleting `q` (given nothing reads its result) is
    observably a no-op. The two guarded cases are the ones that can raise:
    a `DIV`/`MOD` is only removable once its divisor is provably a non-zero
    literal, and an `ARR_NEW` only once its size is provably a non-negative
    literal - otherwise the deleted quad was the one that would have
    reported the error."""
    if q.op in _PURE_OPS:
        return True
    if q.op in ("DIV", "MOD"):
        return isinstance(q.arg2, Const) and _is_plain_int(q.arg2.value) and q.arg2.value != 0
    if q.op == "ARR_NEW":
        return isinstance(q.arg1, Const) and _is_plain_int(q.arg1.value) and q.arg1.value >= 0
    return False


# -- bookkeeping ------------------------------------------------------------


def _clone(q):
    return Quad(q.op, q.arg1, q.arg2, q.result, q.line)


def _record(technique, kind, orig_index, before, after, detail):
    """One entry in the transformation log. `orig_index` points back into
    the *original* quad list (not the current working one), which is what
    lets the IDE line a transformation up with the listing the user is
    looking at even after several rounds have shifted every index."""
    return {
        "technique": technique,
        "kind": kind,
        "orig_index": orig_index,
        "line": before.line,
        "before": str(before),
        "after": str(after) if after is not None else None,
        "detail": detail,
    }


def _global_names(quads):
    """Names living in `IRExecutor.globals`: exactly those written by the
    global-initializer prologue `ir.generate()` emits ahead of the first
    function body. Computed rather than guessed from name shape, because
    it decides which known constants a `CALL` has to forget."""
    names = set()
    for q in quads:
        if q.op == "FUNC_BEGIN":
            break
        names.update(_def_names(q))
    return names


def _write_once_consts(quads, functions):
    """Names whose *only* definition in the entire program is a single
    literal store - the "static variable" case the optimization brief
    describes, and the one that catches global `const`s and
    literal-initialized `val` locals.

    Three things disqualify a name: being a function parameter (a `CALL`
    binds those invisibly, with no quad to see), having any other
    definition anywhere, and - as a belt-and-braces dominance check - being
    read at a lower quad index than the one that defines it. Nothing the
    generator emits should trip that last one, so it costs no real
    optimization; it is there so that any future lowering with a read
    ahead of its definition degrades into "not optimized" instead of
    "optimized wrongly"."""
    params = set()
    for info in functions.values():
        params.update(info["params"])

    defs = {}
    for i, q in enumerate(quads):
        for name in _def_names(q):
            defs.setdefault(name, []).append((i, q))

    first_read = {}
    for i, q in enumerate(quads):
        for name in _read_names(q):
            first_read.setdefault(name, i)

    out = {}
    for name, sites in defs.items():
        if name in params or len(sites) != 1:
            continue
        index, q = sites[0]
        if q.op != "ASSIGN" or not _const_operands(q.arg1):
            continue
        if first_read.get(name, index) < index:
            continue
        out[name] = q.arg1
    return out


# -- pass 1: constant propagation + folding ---------------------------------


def _pass_const_prop(quads, origins, functions, records):
    """Substitute known literals for variable reads, then evaluate whatever
    became fully literal.

    Two sources of "known literal" are combined. The program-wide one is
    `_write_once_consts` (a name defined exactly once, by a literal store).
    The local one is `env`, a straight-line table of the last literal
    assigned to each name, rebuilt from scratch at every `LABEL`/function
    boundary and invalidated for the globals at every `CALL`.

    A `JZ`/`JNZ` whose condition folded to a literal becomes an
    unconditional `JMP` or disappears entirely; that is what turns a
    `if (DEBUG)` guarded by a `const bool DEBUG = false;` into unreachable
    code for pass 3 to delete."""
    write_once = _write_once_consts(quads, functions)
    global_names = _global_names(quads)
    env = {}
    out, out_origins = [], []

    for i, q in enumerate(quads):
        if q.op in _ENV_RESET_OPS:
            env.clear()

        # -- substitution --
        replacement, subs = None, []
        for slot in _subst_slots(q):
            operand = getattr(q, slot)
            if not isinstance(operand, str):
                continue
            known = env.get(operand)
            if known is None:
                known = write_once.get(operand)
            if known is None:
                continue
            if replacement is None:
                replacement = _clone(q)
            setattr(replacement, slot, known)
            subs.append(f"{operand} is {_fmt(known)}")
        if replacement is not None:
            records.append(_record(
                TECHNIQUES[0], "propagate", origins[i], q, replacement, "; ".join(subs)))
            q = replacement

        # -- folding --
        folded = _fold_value(q)
        if folded is not _NO_FOLD:
            replacement = Quad("ASSIGN", Const(folded), None, q.result, q.line)
            records.append(_record(
                TECHNIQUES[0], "fold", origins[i], q, replacement, _fold_detail(q, folded)))
            q = replacement

        # -- branch folding --
        dropped = False
        if q.op in ("JZ", "JNZ") and _const_operands(q.arg1):
            truthy = bool(q.arg1.value)
            taken = (not truthy) if q.op == "JZ" else truthy
            if taken:
                replacement = Quad("JMP", None, None, q.result, q.line)
                records.append(_record(
                    TECHNIQUES[0], "fold", origins[i], q, replacement,
                    f"condition is always {_fmt(q.arg1)}; branch is always taken"))
                q = replacement
            else:
                records.append(_record(
                    TECHNIQUES[0], "fold", origins[i], q, None,
                    f"condition is always {_fmt(q.arg1)}; branch is never taken"))
                dropped = True

        if dropped:
            continue  # a never-taken conditional jump changes nothing it could have known

        # -- update the known-constants table --
        for name in _def_names(q):
            env.pop(name, None)
        if q.op == "ASSIGN" and isinstance(q.result, str) and _const_operands(q.arg1):
            env[q.result] = q.arg1
        if q.op == "CALL":
            # A callee runs with its own frame, so the caller's locals and
            # temps survive untouched - but it shares `globals`, so every
            # global constant has to be re-proved after the call returns.
            for name in global_names:
                env.pop(name, None)
        if q.op in _UNCONDITIONAL_ENDERS:
            env.clear()

        out.append(q)
        out_origins.append(origins[i])

    return out, out_origins


def _fold_detail(q, value):
    if q.op in _FOLD_BINARY:
        symbol = _OP_SYMBOL.get(q.op, q.op)
        return f"folded {_fmt(q.arg1)} {symbol} {_fmt(q.arg2)} -> {_fmt(Const(value))}"
    if q.op == "CAST":
        return f"folded ({q.arg2}) {_fmt(q.arg1)} -> {_fmt(Const(value))}"
    return f"folded {q.op} {_fmt(q.arg1)} -> {_fmt(Const(value))}"


# -- pass 2: algebraic simplification ---------------------------------------


def _simplify(q, var_types):
    """Rewrite one identity operation into a plain copy, or return `None`.

    Every rule here is an exact equality for the operand types that can
    actually reach it, not an approximation:

      - unary `+` is `IRExecutor`'s `lambda v: v`, so it is the identity
        for anything at all;
      - `x * 1` and `x / 1` are exact for both `int` and `float` (including
        the signed zero `-0.0`, which they preserve);
      - `x + 0`, `0 + x`, `x - 0` and `x % 1` are only offered for operands
        known to be `int`, because `-0.0 + 0` is `0.0` - a different value
        that prints differently.

    Deliberately absent: `x * 0 -> 0` (the result's `int`-vs-`float`
    spelling depends on `x`'s type, which is unknown for a temporary) and
    `x - x -> 0` (same reason, plus it needs the two operands proven to be
    the same evaluation, not merely the same name)."""
    op = q.op
    if op == "UPLUS":
        return Quad("ASSIGN", q.arg1, None, q.result, q.line), "unary + is the identity"
    if op == "MUL":
        if _is_int_const(q.arg2, 1):
            return Quad("ASSIGN", q.arg1, None, q.result, q.line), "x * 1 -> x"
        if _is_int_const(q.arg1, 1):
            return Quad("ASSIGN", q.arg2, None, q.result, q.line), "1 * x -> x"
    elif op == "DIV":
        if _is_int_const(q.arg2, 1):
            return Quad("ASSIGN", q.arg1, None, q.result, q.line), "x / 1 -> x"
    elif op == "ADD":
        if _is_int_const(q.arg2, 0) and _is_int_operand(q.arg1, var_types):
            return Quad("ASSIGN", q.arg1, None, q.result, q.line), "x + 0 -> x"
        if _is_int_const(q.arg1, 0) and _is_int_operand(q.arg2, var_types):
            return Quad("ASSIGN", q.arg2, None, q.result, q.line), "0 + x -> x"
    elif op == "SUB":
        if _is_int_const(q.arg2, 0) and _is_int_operand(q.arg1, var_types):
            return Quad("ASSIGN", q.arg1, None, q.result, q.line), "x - 0 -> x"
    elif op == "MOD":
        if _is_int_const(q.arg2, 1) and _is_int_operand(q.arg1, var_types):
            return Quad("ASSIGN", Const(0), None, q.result, q.line), "x % 1 -> 0"
    return None


def _pass_algebraic(quads, origins, var_types, records):
    """Peephole every quad through `_simplify`. Nothing is deleted here -
    each rewrite becomes an `ASSIGN`, which pass 1 can then propagate and
    pass 3 can then delete if its result turns out to be unused."""
    out = []
    for i, q in enumerate(quads):
        result = _simplify(q, var_types)
        if result is None:
            out.append(q)
            continue
        replacement, detail = result
        records.append(_record(TECHNIQUES[1], "simplify", origins[i], q, replacement, detail))
        out.append(replacement)
    return out, list(origins)


# -- pass 3: dead code elimination ------------------------------------------


def _compact(quads, origins, keep, records, kind, detail):
    """Drop every quad whose `keep` flag is false, logging one
    dead-code-elimination record apiece."""
    out, out_origins = [], []
    for i, q in enumerate(quads):
        if keep[i]:
            out.append(q)
            out_origins.append(origins[i])
        else:
            records.append(_record(TECHNIQUES[2], kind, origins[i], q, None, detail))
    return out, out_origins


def _remove_unreachable(quads, origins, records):
    """Delete quads between an unconditional transfer of control and the
    next place control can re-enter (a `LABEL` or a `FUNC_BEGIN`).

    `FUNC_END` is exempt: it is a structural marker for the listing, not
    executable code, and its neighbouring `FUNC_BEGIN` re-establishes
    reachability anyway. This is the rule that removes the duplicate
    trailing `RETURN` `ir.py`'s `gen_function` emits after a body that
    already ends in one."""
    keep = [True] * len(quads)
    dead = False
    for i, q in enumerate(quads):
        if q.op in _LEADER_OPS:
            dead = False
        elif dead:
            if q.op != "FUNC_END":
                keep[i] = False
            continue
        if q.op in _UNCONDITIONAL_ENDERS:
            dead = True
    return _compact(quads, origins, keep, records, "remove-unreachable",
                    "unreachable: control never falls through to here")


def _remove_redundant_jumps(quads, origins, records):
    """Delete a `JMP L` when only labels and no-ops separate it from
    `LABEL L` - falling through reaches exactly the same quad. This is the
    `JMP -> Lend` immediately ahead of `LABEL Lend` that `ir.py`'s `gen_if`
    emits for the then-branch of an `if` with no `else`."""
    keep = [True] * len(quads)
    for i, q in enumerate(quads):
        if q.op != "JMP":
            continue
        for j in range(i + 1, len(quads)):
            nxt = quads[j]
            if nxt.op == "LABEL" and nxt.result == q.result:
                keep[i] = False
                break
            if nxt.op not in ("LABEL", "NOP"):
                break
    return _compact(quads, origins, keep, records, "remove-jump",
                    "jumps to the label immediately following it")


def _remove_dead_stores(quads, origins, records):
    """Delete a side-effect-free quad whose result no other quad reads.

    The "no other quad reads it" test is a single scan of the whole
    program, which is only decidable because every name is unique
    program-wide. One scan per round is enough: a store that becomes dead
    only because *this* round deleted its last reader is caught by the next
    round of the fixpoint loop."""
    live = set()
    for q in quads:
        live.update(_read_names(q))
    keep = [True] * len(quads)
    for i, q in enumerate(quads):
        if not _is_removable(q):
            continue
        targets = list(_def_names(q))
        if targets and not any(t in live for t in targets):
            keep[i] = False
    return _compact(quads, origins, keep, records, "remove-dead",
                    "result is never read")


def _remove_unused_labels(quads, origins, records):
    """Delete a `LABEL` no jump and no `TRY_PUSH` names. Purely cosmetic
    for execution (the VM skips a `LABEL` in one step), but it is what
    keeps the optimized listing readable instead of leaving a drift of
    orphaned labels behind every deleted branch."""
    referenced = set()
    for q in quads:
        referenced.update(_label_refs(q))
    keep = [q.op != "LABEL" or q.result in referenced for q in quads]
    return _compact(quads, origins, keep, records, "remove-label",
                    "no jump targets this label")


def _pass_dce(quads, origins, records):
    quads, origins = _remove_unreachable(quads, origins, records)
    quads, origins = _remove_redundant_jumps(quads, origins, records)
    quads, origins = _remove_dead_stores(quads, origins, records)
    quads, origins = _remove_unused_labels(quads, origins, records)
    return quads, origins


# -- driver -----------------------------------------------------------------


@dataclass
class OptResult:
    """Everything a caller (the VM, the report writer, the IDE payload
    builder) needs: the program to run, the program it came from, and the
    full account of what changed in between."""

    quads: list
    functions: dict
    original_quads: list
    origins: list = field(default_factory=list)  # optimized index -> original index
    records: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    rounds: int = 0
    # False when the round cap stopped the loop while it was still finding
    # work. The quads are correct either way; see `_MAX_ROUNDS`.
    converged: bool = True


def _remap_entries(quads, functions):
    """Rebuild each function's entry point from the optimized listing.
    Entries are quad *indices*, so any deletion ahead of a `FUNC_BEGIN`
    invalidates them - they are recovered the same way `ir.py`'s
    `gen_function` established them, as "one past this function's
    `FUNC_BEGIN`"."""
    begins = {q.result: i for i, q in enumerate(quads) if q.op == "FUNC_BEGIN"}
    out = {}
    for name, info in functions.items():
        if name not in begins:
            raise OptimizerError(f"Function '{name}' lost its FUNC_BEGIN marker")
        out[name] = {"params": list(info["params"]), "entry": begins[name] + 1}
    return out


def optimize(quads, functions, var_types, max_rounds=_MAX_ROUNDS):
    """Run the three techniques to a fixpoint and return an `OptResult`.

    The input `quads` list is never mutated - every rewrite lands on a
    fresh `Quad` - so a caller can keep showing the original listing
    alongside the optimized one, which is exactly what the IDE payload
    does."""
    original = list(quads)
    work = [_clone(q) for q in quads]
    origins = list(range(len(quads)))
    records = []

    rounds = 0
    converged = False
    for _ in range(max_rounds):
        before = len(records)
        work, origins = _pass_const_prop(work, origins, functions, records)
        work, origins = _pass_algebraic(work, origins, var_types, records)
        work, origins = _pass_dce(work, origins, records)
        rounds += 1
        if len(records) == before:
            # A round that found nothing means no pass can find anything
            # more: the passes only ever react to each other's rewrites.
            converged = True
            break

    return OptResult(
        quads=work,
        functions=_remap_entries(work, functions),
        original_quads=original,
        origins=origins,
        records=records,
        stats=_build_stats(original, work, origins, records, converged),
        rounds=rounds,
        converged=converged,
    )


def _build_stats(original, work, origins, records, converged=True):
    surviving = {orig: work[i] for i, orig in enumerate(origins)}
    rewritten = sum(
        1 for orig, q in surviving.items() if str(q) != str(original[orig])
    )
    by_technique = {name: 0 for name in TECHNIQUES}
    for rec in records:
        by_technique[rec["technique"]] += 1
    return {
        "original_count": len(original),
        "optimized_count": len(work),
        "removed": len(original) - len(surviving),
        "rewritten": rewritten,
        "by_technique": by_technique,
        "converged": converged,
    }


# -- viewable output --------------------------------------------------------


def _operand_json(operand):
    """An operand as the IDE should display it - the same text the quad
    listings already show, via `ir._fmt` (so a literal is quoted/escaped
    and a name is bare) - or JSON `null` for an unused slot."""
    return None if operand is None else _fmt(operand)


def _quad_json(index, q, **extra):
    entry = {
        "i": index,
        "op": q.op,
        "arg1": _operand_json(q.arg1),
        "arg2": _operand_json(q.arg2),
        "result": _operand_json(q.result),
        "line": q.line,
        "text": str(q),
    }
    entry.update(extra)
    return entry


def build_view(result, source_file):
    """Build the JSON-serializable payload the IDE's Optimizer tab renders.

    Both listings are included in full, each quad tagged so the IDE can
    paint a side-by-side diff without computing one itself: an original
    quad carries `status` (`kept`/`rewritten`/`removed`), an optimized quad
    carries `orig_index` pointing back at the row it came from. Every quad
    also carries its source `line`, so hovering a quad can highlight the
    statement that produced it."""
    surviving = {orig: i for i, orig in enumerate(result.origins)}
    original = []
    for i, q in enumerate(result.original_quads):
        if i not in surviving:
            status = "removed"
        elif str(result.quads[surviving[i]]) != str(q):
            status = "rewritten"
        else:
            status = "kept"
        original.append(_quad_json(i, q, status=status))

    optimized = [
        _quad_json(i, q, orig_index=result.origins[i])
        for i, q in enumerate(result.quads)
    ]

    return {
        "version": VIEW_VERSION,
        "source_file": os.path.basename(source_file),
        "techniques": list(TECHNIQUES),
        "original": original,
        "optimized": optimized,
        "transformations": list(result.records),
        "stats": result.stats,
    }


def format_report(result, source_file):
    """Render the optimizer's work as an annotated text report: a summary
    header, the optimized quad listing (through `ir.format_quads`, so it
    reads identically to `ir.py`'s own output), the transformation log
    grouped by technique, and the statistics table.

    A run that stopped on the round cap gets one extra `Note` line, so a
    listing that is correct but not fully reduced is never mistaken for the
    optimizer's last word."""
    stats = result.stats
    lines = [
        f"[IR OPTIMIZATION] {os.path.basename(source_file)}",
        f"  Techniques : {', '.join(TECHNIQUES)}",
        f"  Quads      : {stats['original_count']} -> {stats['optimized_count']}"
        f" ({stats['removed']} removed, {stats['rewritten']} rewritten)",
        f"  Rounds     : {result.rounds}",
    ]
    if not result.converged:
        lines.append(
            f"  Note       : stopped at the {result.rounds}-round cap with work"
            " still being found; the quads below are correct but some dead"
            " ones may remain"
        )
    lines += [
        "",
        "Optimized Intermediate Code (Quadruples):",
        format_quads(result.quads),
        "",
        f"Transformations ({len(result.records)}):",
    ]

    if not result.records:
        lines.append("  (none - nothing in this program was optimizable)")
    for technique in TECHNIQUES:
        group = [r for r in result.records if r["technique"] == technique]
        lines.append("")
        lines.append(f"  {technique} ({len(group)}):")
        if not group:
            lines.append("    (no opportunities found)")
            continue
        for rec in group:
            location = f"#{rec['orig_index']:04d}"
            line_no = f"Line {rec['line']}" if rec["line"] is not None else "Line ?"
            lines.append(f"    {location} {line_no}  {rec['before']}")
            lines.append(f"        {rec['kind']:<20} -> {rec['after'] or '(removed)'}")
            lines.append(f"        {rec['detail']}")

    lines.append("")
    lines.append("Statistics:")
    lines.append(f"  original quads  : {stats['original_count']}")
    lines.append(f"  optimized quads : {stats['optimized_count']}")
    lines.append(f"  removed         : {stats['removed']}")
    lines.append(f"  rewritten       : {stats['rewritten']}")
    for technique, count in stats["by_technique"].items():
        lines.append(f"  {technique:<26}: {count}")
    return "\n".join(lines) + "\n"


# -- CLI --------------------------------------------------------------------


def compile_source(source):
    """Run the front end (scan -> parse -> analyze -> generate) and return
    `(quads, functions, var_types, structs)`. Shared by this module's CLI
    and nothing else - `interpreter.py -O` already has these in hand by the
    time it calls `optimize()`."""
    ast, syntax_errors, lex_errors = parse_source(source)
    if lex_errors or syntax_errors:
        for err in lex_errors:
            print(err, file=sys.stderr)
        for err in syntax_errors:
            print(err, file=sys.stderr)
        sys.exit(2)

    symtab, sem_errors = analyze(ast)
    if sem_errors:
        for err in sem_errors:
            print(err, file=sys.stderr)
        if any(err.severity == "ERROR" for err in sem_errors):
            sys.exit(2)

    try:
        return generate(ast, symtab)
    except IRGenError as e:
        print(f"[IR ERROR] {e}", file=sys.stderr)
        sys.exit(2)


def main():
    """CLI entry point: `python optimizer.py <source_file> [-o out] [--json out]`.

    Prints the annotated text report by default. `--json` writes the web
    IDE payload instead (or as well, if `-o` is also given); `--json -`
    sends it to stdout, which is what makes
    `python optimizer.py f.src --json - | python -m json.tool` work."""
    cli = argparse.ArgumentParser(description="CSC617M Custom Language IR Optimizer")
    cli.add_argument("source_file", help="Path to source file to compile and optimize")
    cli.add_argument("-o", "--output", help="Write the annotated text report to this file")
    cli.add_argument("--json", help="Write the IDE view payload as JSON to this file ('-' for stdout)")
    args = cli.parse_args()

    try:
        with open(args.source_file, "r", encoding="utf-8") as f:
            source = f.read()
    except FileNotFoundError:
        print(f"Error: file not found: {args.source_file}", file=sys.stderr)
        sys.exit(1)

    quads, functions, var_types, _structs = compile_source(source)

    try:
        result = optimize(quads, functions, var_types)
    except OptimizerError as e:
        print(f"[OPTIMIZER ERROR] {e}", file=sys.stderr)
        sys.exit(2)

    if args.json:
        payload = json.dumps(build_view(result, args.source_file), indent=2)
        if args.json == "-":
            print(payload)
        else:
            with open(args.json, "w", encoding="utf-8") as f:
                f.write(payload + "\n")

    report = format_report(result, args.source_file)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"[IR OPTIMIZATION] {os.path.basename(args.source_file)}")
        print(f"  Quads   : {result.stats['original_count']} -> {result.stats['optimized_count']}")
        print(f"  Output  : {args.output}")
    elif not args.json:
        print(report, end="")


if __name__ == "__main__":
    main()
