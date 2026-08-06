# IR optimizer: rewrites ir.py's quads into a shorter equivalent, opt-in via interpreter.py -O
# three techniques run to a fixpoint: constant propagation/folding, algebraic simplification, dead code elimination

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

# safety net against a hypothetical oscillating rule, real programs converge in 2-3 rounds - see LIMITATIONS.md
_MAX_ROUNDS = 10

VIEW_VERSION = 1


class OptimizerError(Exception):
    pass  # an opcode with no entry in _SLOTS below, should be unreachable


# operand classification: for each opcode, which slot the VM reads as a value, writes, or
# neither - checked against interpreter.py's _exec, not guessed from the opcode's name

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
    # field-name slot is classified META (not READ) so it can't be mistaken for a same-named global and substituted away
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
    # slots safe to replace with a Const: pure reads only. READ_DEF is excluded, substituting a literal for the array/struct being mutated would store into a throwaway value
    return [name for name, role in zip(_SLOT_NAMES, _roles(q)) if role == READ]


def _read_names(q):
    # every name this quad reads, including the container a store mutates
    for name, role in zip(_SLOT_NAMES, _roles(q)):
        if role in (READ, READ_DEF):
            operand = getattr(q, name)
            if isinstance(operand, str):
                yield operand


def _def_names(q):
    # every name this quad may write; READ_DEF counts too, or an array only ever stored into would look like a write-once constant
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
    # label names this quad jumps to (or registers as an exception handler)
    for name, role in zip(_SLOT_NAMES, _roles(q)):
        if role == LABEL_REF:
            operand = getattr(q, name)
            if operand is not None:
                yield operand


# purity

# opcodes whose only effect is writing their result, so deleting one whose result is never read is safe.
# I/O, calls, throws, stores, and loads that can raise (ARR_LOAD's bounds check, ARR_LEN, FIELD_LOAD) are excluded
_PURE_OPS = {
    "ASSIGN", "CAST", "ADD", "SUB", "MUL", "UMINUS", "UPLUS", "NOT",
    "EQ", "NE", "LT", "GT", "LE", "GE", "CONCAT", "TOSTR",
    "TUPLE_NEW", "STRUCT_NEW",
}

# opcodes that fold into a literal. TOSTR is pure but left out, duplicating IRExecutor._format's float/bool/array spelling here isn't worth it
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
# markers that end a straight-line run for the constants table: a LABEL can be entered
# from anywhere, FUNC_BEGIN/FUNC_END bracket a body a CALL jumps into fresh
_ENV_RESET_OPS = {"LABEL", "FUNC_BEGIN", "FUNC_END"}

_NO_FOLD = object()


def _is_plain_int(value):
    # bool is technically an int subclass in python, but true/1 aren't the same value here
    return isinstance(value, int) and not isinstance(value, bool)


def _is_int_const(operand, wanted):
    return isinstance(operand, Const) and _is_plain_int(operand.value) and operand.value == wanted


def _is_int_operand(operand, var_types):
    # true if operand is known int (literal or int-typed variable) - a temp never qualifies, the safe answer since these identities are unsound on float
    if isinstance(operand, Const):
        return _is_plain_int(operand.value)
    if isinstance(operand, str):
        return var_types.get(operand) == "int"
    return False


def _cast_const(value, type_tag):
    # mirrors IRExecutor._cast so folding a CAST matches running it
    if type_tag == "float":
        return float(ord(value)) if isinstance(value, str) else float(value)
    if type_tag == "int":
        return ord(value) if isinstance(value, str) else int(value)
    return value


def _const_operands(*operands):
    # excludes Const(None), ir.py's placeholder for a non-primitive declarator - not a real value, nothing to compute from it
    return all(isinstance(o, Const) and o.value is not None for o in operands)


def _fold_value(q):
    # evaluate q at compile time if possible. any exception just means don't fold, leave it for the VM to raise the same error at the same line
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
            return _NO_FOLD  # keep the divide by zero error intact
        try:
            return _FOLD_BINARY[op](q.arg1.value, q.arg2.value)
        except Exception:
            return _NO_FOLD
    return _NO_FOLD


def _is_removable(q):
    # true if deleting q (given nothing reads its result) is a genuine no-op. DIV/MOD and ARR_NEW only qualify once their operand is a literal that provably can't raise -
    # otherwise the deleted quad was the one that would've reported the runtime error
    if q.op in _PURE_OPS:
        return True
    if q.op in ("DIV", "MOD"):
        return isinstance(q.arg2, Const) and _is_plain_int(q.arg2.value) and q.arg2.value != 0
    if q.op == "ARR_NEW":
        return isinstance(q.arg1, Const) and _is_plain_int(q.arg1.value) and q.arg1.value >= 0
    return False


# bookkeeping


def _clone(q):
    return Quad(q.op, q.arg1, q.arg2, q.result, q.line)


def _record(technique, kind, orig_index, before, after, detail):
    # orig_index points into the ORIGINAL quad list, not the current working one, so the IDE
    # can line this transformation up with what's on screen even after later rounds shift things
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
    # names written by the global-initializer prologue ir.generate() emits before the first function body, i.e. everything that ends up in IRExecutor.globals
    names = set()
    for q in quads:
        if q.op == "FUNC_BEGIN":
            break
        names.update(_def_names(q))
    return names


def _write_once_consts(quads, functions):
    # names whose only definition anywhere is a single literal store: global consts and literal-initialized locals. params are excluded, a CALL binds those invisibly.
    # the "read before its definition" check is just a belt-and-braces guard, nothing the generator currently emits should trip it
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


# pass 1: constant propagation + folding


def _pass_const_prop(quads, origins, functions, records):
    # substitute known literals for reads, then fold whatever became fully literal. "known"
    # combines _write_once_consts (program-wide) with env, a local table reset every LABEL/CALL
    write_once = _write_once_consts(quads, functions)
    global_names = _global_names(quads)
    env = {}
    out, out_origins = [], []

    for i, q in enumerate(quads):
        if q.op in _ENV_RESET_OPS:
            env.clear()

        # substitution
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

        # folding
        folded = _fold_value(q)
        if folded is not _NO_FOLD:
            replacement = Quad("ASSIGN", Const(folded), None, q.result, q.line)
            records.append(_record(
                TECHNIQUES[0], "fold", origins[i], q, replacement, _fold_detail(q, folded)))
            q = replacement

        # branch folding
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

        # update the known-constants table
        for name in _def_names(q):
            env.pop(name, None)
        if q.op == "ASSIGN" and isinstance(q.result, str) and _const_operands(q.arg1):
            env[q.result] = q.arg1
        if q.op == "CALL":
            # a callee runs with its own frame (locals/temps survive) but shares globals, so every global constant has to be re-proved after the call returns
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


# pass 2: algebraic simplification


def _simplify(q, var_types):
    # rewrite one identity op into a plain copy, or return None. x+0/0+x/x-0/x%1 only apply
    # when the operand is known int, since -0.0 + 0 is 0.0, a different value that prints differently
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
    # peephole every quad through _simplify. nothing's deleted here, a rewrite just becomes an ASSIGN that pass 1 can propagate and pass 3 can delete if it's unused
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


# pass 3: dead code elimination


def _compact(quads, origins, keep, records, kind, detail):
    # drop every quad whose keep flag is false, logging one dead-code record apiece
    out, out_origins = [], []
    for i, q in enumerate(quads):
        if keep[i]:
            out.append(q)
            out_origins.append(origins[i])
        else:
            records.append(_record(TECHNIQUES[2], kind, origins[i], q, None, detail))
    return out, out_origins


def _remove_unreachable(quads, origins, records):
    # deletes quads between an unconditional transfer of control and the next LABEL/FUNC_BEGIN -
    # removes the duplicate trailing RETURN ir.py's gen_function always emits
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
    # delete a JMP L when only labels and no-ops separate it from LABEL L, falling through reaches the same quad - this is the JMP -> Lend right before LABEL Lend
    # that ir.py's gen_if emits for an if with no else
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
    # deletes a side-effect-free quad nothing reads (a single scan, decidable since every name
    # is unique program-wide) - a store that only becomes dead this round gets caught next round
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
    # delete a LABEL nothing jumps to (or registers as a TRY_PUSH handler). purely cosmetic for
    # execution, but keeps the listing readable instead of leaving orphaned labels behind every deleted branch
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


# driver


@dataclass
class OptResult:
    quads: list
    functions: dict
    original_quads: list
    origins: list = field(default_factory=list)  # optimized index -> original index
    records: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    rounds: int = 0
    converged: bool = True  # False when the round cap stopped the loop while still finding work


def _remap_entries(quads, functions):
    # rebuilds each function's entry point (quad indices shift after deletions) the same way gen_function set it up: one past this function's FUNC_BEGIN
    begins = {q.result: i for i, q in enumerate(quads) if q.op == "FUNC_BEGIN"}
    out = {}
    for name, info in functions.items():
        if name not in begins:
            raise OptimizerError(f"Function '{name}' lost its FUNC_BEGIN marker")
        out[name] = {"params": list(info["params"]), "entry": begins[name] + 1}
    return out


def optimize(quads, functions, var_types, max_rounds=_MAX_ROUNDS):
    # runs the three techniques to a fixpoint. quads is never mutated, every rewrite lands on
    # a fresh Quad, so a caller can keep showing the original listing next to the optimized one
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
            # a round that found nothing means no pass can find anything more, they only
            # ever react to each other's rewrites
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


# viewable output


def _operand_json(operand):
    # an operand the same way the quad listings already show it (via ir._fmt), or None
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
    # JSON payload the IDE's Optimizer tab renders - an original quad carries status
    # (kept/rewritten/removed), an optimized quad carries orig_index pointing back at its row
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
    # summary header, the optimized listing, the transformation log, then stats - a run stopped by the round cap gets a Note line so it's never mistaken for the optimizer's last word
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


# CLI


def compile_source(source):
    # run scan -> parse -> analyze -> generate and return (quads, functions, var_types, structs)
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
