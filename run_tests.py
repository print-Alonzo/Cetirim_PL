"""Golden-output test runner for the scanner -> parser -> semantics -> IR ->
interpreter pipeline.

    python run_tests.py            run every check, report pass/fail
    python run_tests.py --update   regenerate the golden files from current output

Positive fixtures (prog1..prog5, plus the check1..check5 checklist-coverage
set): each program's scanner token stream, compiled quadruple listing, and
executed output are diffed against the committed goldens (<name>_tokens.txt,
<name>_ir.txt, <name>_expected.txt). Programs that call input() read from
<name>.in. Diffing all three per program is what pins the rubric's three
grading columns at once - the token stream covers the scanner, the quad
listing covers the parser and semantic analyser, and the output covers the
interpreter. The token stream's "Scan
time" line is a live per-run timing measurement, not scanner output, so
it's stripped from both sides before comparing (but --update still writes
whatever timing that regeneration run happened to see, same as the
existing manual `scanner.py -o` workflow).

Negative fixtures (NEGATIVE_FIXTURES): each is expected to fail at one
specific phase; the runner only checks that the right "[... ERROR]" tag
appears and that the process exits with the declared code (2 for
lex/syntax/semantic/IR errors, 3 for runtime errors) - it does not pin exact
wording, so error-message copy can still be improved without breaking the
suite.

Feature fixtures (FEATURE_FIXTURES, tests/<name>.src + tests/<name>_expected.txt):
programs that exercise a specific language feature outside the five graded
sample programs. Each is expected to run to completion (exit 0); its
stdout+stderr is diffed against the committed golden.

Optimizer checks (see optimizer.py) are differential rather than golden-based:
every positive and feature fixture is additionally run through
`interpreter.py -O` and must produce byte-identical output and the same exit
code, and every runtime-error negative fixture must still fail the same way
with -O. That is the real correctness contract for an optimizer - it may
change how many quads run, never what they print - and it needs no goldens of
its own. The one optimizer fixture with a golden is OPTIMIZER_DEMO, whose
`optimizer.py` report is diffed so a change in what gets optimized shows up
as a reviewable diff rather than passing silently.
"""

import argparse
import difflib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent

PROGRAMS = [
    "prog1_calculator",
    "prog2_loops_arrays",
    "prog3_functions",
    "prog4_structs_match_exceptions",
    "prog5_advanced",
    # The checklist-coverage set: one program per group of rows in the
    # course rubric (docs/CSC617M_Machine_Problem_Checklist_Rubric.pdf), so
    # every graded construct has a program that demonstrates it running.
    # See docs/CHECKLIST_COVERAGE.md for the row-by-row map.
    "check1_declarations",
    "check2_expressions",
    "check3_control_flow",
    "check4_functions",
    "check5_io_heap",
]

NEGATIVE_FIXTURES = [
    ("tests/lex_error.src", "[LEXICAL ERROR]", 2),
    ("tests/syntax_error.src", "[SYNTAX ERROR]", 2),
    ("tests/stray_brace.src", "[SYNTAX ERROR]", 2),
    ("tests/match_expr_semicolon.src", "[SYNTAX ERROR]", 2),
    ("tests/semantic_error.src", "[SEMANTIC ERROR]", 2),
    ("tests/multi_catch_error.src", "[SEMANTIC ERROR]", 2),
    ("tests/throw_type_error.src", "[SEMANTIC ERROR]", 2),
    ("tests/input_array_error.src", "[SEMANTIC ERROR]", 2),
    ("tests/missing_return_error.src", "[SEMANTIC ERROR]", 2),
    ("tests/ir_array_error.src", "[IR ERROR]", 2),
    ("tests/div_by_zero.src", "[RUNTIME ERROR]", 3),
    ("tests/mod_by_zero.src", "[RUNTIME ERROR]", 3),
    ("tests/array_oob.src", "[RUNTIME ERROR]", 3),
    ("tests/uncaught_exception.src", "[RUNTIME ERROR]", 3),
    ("tests/infinite_recursion.src", "[RUNTIME ERROR]", 3),
    ("tests/recursive_struct_error.src", "[SEMANTIC ERROR]", 2),
    ("tests/dynamic_inner_dim_error.src", "[SEMANTIC ERROR]", 2),
    ("tests/unequal_dims_oob.src", "[RUNTIME ERROR]", 3),
    # The five semantic errors the course rubric names explicitly, one
    # fixture each, plus a file combining them to show the analyser
    # reports every error in a run rather than stopping at the first.
    ("tests/sem_undeclared_var.src", "[SEMANTIC ERROR]", 2),
    ("tests/sem_type_mismatch.src", "[SEMANTIC ERROR]", 2),
    ("tests/sem_redeclared_var.src", "[SEMANTIC ERROR]", 2),
    ("tests/sem_const_reassign.src", "[SEMANTIC ERROR]", 2),
    ("tests/sem_arg_cardinality.src", "[SEMANTIC ERROR]", 2),
    ("tests/sem_multi_errors.src", "[SEMANTIC ERROR]", 2),
]

FEATURE_FIXTURES = [
    "struct_array_init",
    "dynamic_array",
    "discard",
    "default_params",
    "interp_edge_cases",
    "negative_mod",
    "string_comparison",
    "nested_array",
    "unequal_dims",
    "struct_array",
    "nested_struct",
    "struct_return",
    "param_passing",
    "optimizer_demo",
]

# The fixture whose optimizer report is itself a golden - written to exercise
# all three techniques at once, so the report doubles as the worked example
# the README points at.
OPTIMIZER_DEMO = "optimizer_demo"


def run(script, src, stdin_text="", extra_args=()):
    args = [sys.executable, script, src, *extra_args]
    try:
        return subprocess.run(
            args, cwd=ROOT, input=stdin_text, capture_output=True, text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        # A non-terminating parser/interpreter bug (e.g. the many_rec
        # infinite loop this suite once hit) would otherwise hang the whole
        # run indefinitely instead of failing one check.
        return subprocess.CompletedProcess(
            args=args, returncode=124,
            stdout="", stderr=f"[FAIL] {script} {src}: timed out after 60s\n",
        )


def _print_diff(expected, actual):
    diff = difflib.unified_diff(
        expected.splitlines(keepends=True),
        actual.splitlines(keepends=True),
        fromfile="expected", tofile="actual",
    )
    sys.stdout.writelines(diff)


def _strip_scan_time(text):
    """Scanner reports include a live 'Scan time : N.NNN ms' measurement -
    real scan output, but inherently different on every run, so it's
    stripped before comparing token streams (--update still writes it
    verbatim; only the comparison ignores it). Also normalizes the trailing
    newline count: `scanner.py`'s CLI writes the report as-is to a `-o`
    file but `print()`s it to stdout (which appends its own trailing
    newline on top of the report's own), so a stdout capture and a
    file-dumped golden differ by one blank line at the end even when the
    actual token stream is identical - not a regression worth flagging."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("Scan time")
    ).rstrip("\n")


def check_positive(name, update):
    src = f"{name}.src"
    in_file = ROOT / f"{name}.in"
    expected_file = ROOT / f"{name}_expected.txt"
    ir_file = ROOT / f"{name}_ir.txt"
    tokens_file = ROOT / f"{name}_tokens.txt"
    stdin_text = in_file.read_text() if in_file.exists() else ""

    scan_result = run("scanner.py", src)
    ir_result = run("ir.py", src)
    run_result = run("interpreter.py", src, stdin_text)
    actual_tokens = scan_result.stdout
    actual_ir = ir_result.stdout
    actual_out = run_result.stdout + run_result.stderr

    if update:
        tokens_file.write_text(actual_tokens)
        ir_file.write_text(actual_ir)
        expected_file.write_text(actual_out)
        print(f"[UPDATED] {name}")
        return True

    ok = True
    expected_tokens = tokens_file.read_text() if tokens_file.exists() else ""
    if _strip_scan_time(actual_tokens) != _strip_scan_time(expected_tokens):
        print(f"[FAIL] {name}: scanner token stream mismatch")
        _print_diff(_strip_scan_time(expected_tokens), _strip_scan_time(actual_tokens))
        ok = False

    expected_ir = ir_file.read_text() if ir_file.exists() else ""
    if actual_ir != expected_ir:
        print(f"[FAIL] {name}: IR mismatch")
        _print_diff(expected_ir, actual_ir)
        ok = False

    expected_out = expected_file.read_text() if expected_file.exists() else ""
    if actual_out != expected_out:
        print(f"[FAIL] {name}: output mismatch")
        _print_diff(expected_out, actual_out)
        ok = False

    if run_result.returncode != 0:
        print(f"[FAIL] {name}: exited {run_result.returncode}, expected 0")
        ok = False

    if ok:
        print(f"[PASS] {name}")
    return ok


def check_negative(path, tag, exit_code=2):
    result = run("interpreter.py", str(ROOT / path))
    combined = result.stdout + result.stderr
    ok = tag in combined and result.returncode == exit_code
    print(f"[{'PASS' if ok else 'FAIL'}] {path}: expected {tag!r} and exit {exit_code}, got exit {result.returncode}")
    if not ok:
        print(combined)
    return ok


def check_feature(name, update):
    src = f"tests/{name}.src"
    expected_file = ROOT / f"tests/{name}_expected.txt"
    result = run("interpreter.py", src)
    actual_out = result.stdout + result.stderr

    if update:
        expected_file.write_text(actual_out)
        print(f"[UPDATED] tests/{name}")
        return True

    ok = True
    expected_out = expected_file.read_text() if expected_file.exists() else ""
    if actual_out != expected_out:
        print(f"[FAIL] tests/{name}: output mismatch")
        _print_diff(expected_out, actual_out)
        ok = False
    if result.returncode != 0:
        print(f"[FAIL] tests/{name}: exited {result.returncode}, expected 0")
        ok = False
    if ok:
        print(f"[PASS] tests/{name}")
    return ok


def check_optimized(label, src, expected_file, stdin_text=""):
    """Differential optimizer check: running `src` with `-O` must produce
    exactly the output its unoptimized golden records, and still exit 0.
    Reuses the existing golden rather than introducing a second one -
    sharing the file is the whole point, since an optimizer that needs its
    own expected output has changed the program's behavior."""
    result = run("interpreter.py", src, stdin_text, ("-O",))
    actual = result.stdout + result.stderr
    expected = expected_file.read_text() if expected_file.exists() else ""

    ok = True
    if actual != expected:
        print(f"[FAIL] {label} (-O): optimized output differs from the golden")
        _print_diff(expected, actual)
        ok = False
    if result.returncode != 0:
        print(f"[FAIL] {label} (-O): exited {result.returncode}, expected 0")
        ok = False
    if ok:
        print(f"[PASS] {label} (-O)")
    return ok


def check_negative_optimized(path, tag, exit_code):
    """A runtime error must survive optimization. This is what pins the
    deliberate refusals in optimizer.py's `_fold_value`/`_is_removable`: a
    division by a literal zero is neither folded nor deleted, so
    `tests/div_by_zero.src` still reports `[RUNTIME ERROR]` and exits 3
    instead of being quietly optimized into nothing."""
    result = run("interpreter.py", str(ROOT / path), extra_args=("-O",))
    combined = result.stdout + result.stderr
    ok = tag in combined and result.returncode == exit_code
    print(f"[{'PASS' if ok else 'FAIL'}] {path} (-O): expected {tag!r} and exit {exit_code}, got exit {result.returncode}")
    if not ok:
        print(combined)
    return ok


def check_optimizer_report(update):
    """Diff the optimizer's own annotated report against a golden, then
    assert the two properties a golden diff alone wouldn't catch if the
    report format changed: that the program actually got smaller, and that
    all three techniques found something to do."""
    src = f"tests/{OPTIMIZER_DEMO}.src"
    golden = ROOT / f"tests/{OPTIMIZER_DEMO}_opt.txt"
    result = run("optimizer.py", src)
    actual = result.stdout + result.stderr

    if update:
        golden.write_text(actual)
        print(f"[UPDATED] tests/{OPTIMIZER_DEMO}_opt")
        return True

    ok = True
    expected = golden.read_text() if golden.exists() else ""
    if actual != expected:
        print(f"[FAIL] tests/{OPTIMIZER_DEMO}: optimizer report mismatch")
        _print_diff(expected, actual)
        ok = False
    if result.returncode != 0:
        print(f"[FAIL] tests/{OPTIMIZER_DEMO}: optimizer.py exited {result.returncode}, expected 0")
        ok = False

    view = run("optimizer.py", src, extra_args=("--json", "-"))
    try:
        payload = json.loads(view.stdout)
    except ValueError as e:
        print(f"[FAIL] tests/{OPTIMIZER_DEMO}: --json payload is not valid JSON ({e})")
        return False

    stats = payload["stats"]
    if stats["optimized_count"] >= stats["original_count"]:
        print(f"[FAIL] tests/{OPTIMIZER_DEMO}: optimization removed nothing "
              f"({stats['original_count']} -> {stats['optimized_count']} quads)")
        ok = False
    for technique in payload["techniques"]:
        if stats["by_technique"].get(technique, 0) <= 0:
            print(f"[FAIL] tests/{OPTIMIZER_DEMO}: technique {technique!r} made no transformations")
            ok = False

    if ok:
        print(f"[PASS] tests/{OPTIMIZER_DEMO} (optimizer report)")
    return ok


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--update", action="store_true", help="Regenerate golden files from current output")
    args = cli.parse_args()

    results = [check_positive(name, args.update) for name in PROGRAMS]
    results += [check_feature(name, args.update) for name in FEATURE_FIXTURES]
    results.append(check_optimizer_report(args.update))
    if args.update:
        print("Golden files updated.")
        return

    results += [check_negative(path, tag, exit_code) for path, tag, exit_code in NEGATIVE_FIXTURES]

    for name in PROGRAMS:
        in_file = ROOT / f"{name}.in"
        results.append(check_optimized(
            name, f"{name}.src", ROOT / f"{name}_expected.txt",
            in_file.read_text() if in_file.exists() else "",
        ))
    for name in FEATURE_FIXTURES:
        results.append(check_optimized(
            f"tests/{name}", f"tests/{name}.src", ROOT / f"tests/{name}_expected.txt",
        ))
    results += [
        check_negative_optimized(path, tag, exit_code)
        for path, tag, exit_code in NEGATIVE_FIXTURES if exit_code == 3
    ]

    passed, total = sum(results), len(results)
    print(f"\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
