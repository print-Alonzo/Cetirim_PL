"""Golden-output test runner for the scanner -> parser -> semantics -> IR ->
interpreter pipeline.

    python run_tests.py            run every check, report pass/fail
    python run_tests.py --update   regenerate the golden files from current output

Positive fixtures (samples/prog1..prog5, plus the checks/check1..check5
checklist-coverage set): each program's scanner token stream, compiled
quadruple listing, and
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
suite. A fixture may demand the tag more than once (an optional fourth
tuple element, min_count): sem_multi_errors.src requires five and
syn_multi_errors.src requires two, which is what actually pins error
*recovery* in the analyser and the parser - a phase that stopped at the
first diagnostic would still print the tag once and exit 2.

The scanner recovery check (check_scanner_recovery) is the third phase's
half of that same row, and needs a different mechanism: a lexical error
does not stop the scanner, so there is no exit code or tag count that
distinguishes recovering from halting. What distinguishes them is the token
stream itself, so tests/test_errors.src - four different lexical errors,
with valid tokens between and after them - has its whole scanner report
diffed against tests/test_errors_tokens.txt. A scanner that gave up at the
first bad
character would produce a visibly shorter stream. Note that scanner.py's
CLI exits 2 when it recorded any error, so this check expects 2, not 0.

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
as a reviewable diff rather than passing silently. OPTIMIZER_DEEP_CHAIN is the
other side of that coin (check_optimizer_cap_note): a program the optimizer
cannot fully reduce inside its round cap, checked for admitting so in both the
report and the --json payload.
"""

import argparse
import difflib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent

PROGRAMS = [
    "samples/prog1_calculator",
    "samples/prog2_loops_arrays",
    "samples/prog3_functions",
    "samples/prog4_structs_match_exceptions",
    "samples/prog5_advanced",
    # The checklist-coverage set: one program per group of rows in the
    # course rubric (docs/CSC617M_Machine_Problem_Checklist_Rubric.pdf), so
    # every graded construct has a program that demonstrates it running.
    # See docs/CHECKLIST_COVERAGE.md for the row-by-row map.
    "checks/check1_declarations",
    "checks/check2_expressions",
    "checks/check3_control_flow",
    "checks/check4_functions",
    "checks/check5_io_heap",
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
    # reports every error in a run rather than stopping at the first -
    # its min_count of 5 is what makes that recovery claim an assertion.
    ("tests/sem_undeclared_var.src", "[SEMANTIC ERROR]", 2),
    ("tests/sem_type_mismatch.src", "[SEMANTIC ERROR]", 2),
    ("tests/sem_redeclared_var.src", "[SEMANTIC ERROR]", 2),
    ("tests/sem_const_reassign.src", "[SEMANTIC ERROR]", 2),
    ("tests/sem_arg_cardinality.src", "[SEMANTIC ERROR]", 2),
    ("tests/sem_multi_errors.src", "[SEMANTIC ERROR]", 2, 5),
    # The parser-side counterpart: two separate missing semicolons, in two
    # different functions, both of which must be reported. min_count is 2
    # rather than the exact diagnostic count so the assertion pins recovery
    # without pinning how a future cascade happens to spell it.
    ("tests/syn_multi_errors.src", "[SYNTAX ERROR]", 2, 2),
]

# The scanner's recovery fixture. Unlike the fixtures above it is checked by
# diffing its whole token stream (see check_scanner_recovery), since a
# scanner that halted at the first error would still emit the tag and exit 2.
SCANNER_RECOVERY = "tests/test_errors"

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
    "optimizer_deep_chain",
]

# The fixture whose optimizer report is itself a golden - written to exercise
# all three techniques at once, so the report doubles as the worked example
# the README points at.
OPTIMIZER_DEMO = "optimizer_demo"

# The fixture whose dead dependency chain is longer than the optimizer's
# fixpoint round cap, so its run stops with work still left to find. Checked
# for the *reporting* of that, not for a particular quad count.
OPTIMIZER_DEEP_CHAIN = "optimizer_deep_chain"


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


def check_scanner_recovery(update):
    """Diff the scanner's own report for a file full of lexical errors.

    This is the scanner's half of the checklist's "Error Recovery" row. The
    other two phases can assert recovery by counting diagnostics, but a
    lexical error never stops the scan, so counting proves nothing here: a
    scanner that emitted one error and stopped would still report an error
    and exit 2. What actually distinguishes recovery is that valid tokens
    keep coming *after* each bad one, so the whole report is diffed -
    test_errors.src's four errors sit between otherwise valid declarations,
    and the golden records the full 30-token stream that survives them.

    Uses `_strip_scan_time` for the same reason check_positive does, and
    expects exit 2, which is what scanner.py's CLI returns once it has
    recorded any lexical error."""
    src = f"{SCANNER_RECOVERY}.src"
    golden = ROOT / f"{SCANNER_RECOVERY}_tokens.txt"
    result = run("scanner.py", src)
    actual = result.stdout

    if update:
        golden.write_text(actual)
        print(f"[UPDATED] {SCANNER_RECOVERY}")
        return True

    ok = True
    expected = golden.read_text() if golden.exists() else ""
    if _strip_scan_time(actual) != _strip_scan_time(expected):
        print(f"[FAIL] {SCANNER_RECOVERY}: scanner token stream mismatch")
        _print_diff(_strip_scan_time(expected), _strip_scan_time(actual))
        ok = False
    if result.returncode != 2:
        print(f"[FAIL] {SCANNER_RECOVERY}: exited {result.returncode}, expected 2")
        ok = False

    if ok:
        print(f"[PASS] {SCANNER_RECOVERY} (scanner error recovery)")
    return ok


def check_negative(path, tag, exit_code=2, min_count=1):
    result = run("interpreter.py", str(ROOT / path))
    combined = result.stdout + result.stderr
    found = combined.count(tag)
    ok = found >= min_count and result.returncode == exit_code
    print(f"[{'PASS' if ok else 'FAIL'}] {path}: expected {tag!r} x{min_count} and exit {exit_code}, got x{found} and exit {result.returncode}")
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


def check_optimizer_cap_note():
    """A run that stops on the fixpoint round cap has to say so.

    Two assertions, in order of what they protect. First, the text report and
    the `--json` payload must agree about convergence - a `Note` line appears
    in the report exactly when `stats.converged` is false - so neither view
    can present a partially reduced listing as final. Second, this fixture
    must actually be hitting the cap, or the first assertion is checking
    nothing: its dead chain is deliberately longer than `_MAX_ROUNDS` can
    peel. If dead store elimination is ever taught to reach an inner fixpoint
    in one round, this fixture will converge and this check should be
    retired along with the note it pins - the failure is a report of that,
    not a regression."""
    src = f"tests/{OPTIMIZER_DEEP_CHAIN}.src"
    report = run("optimizer.py", src)
    view = run("optimizer.py", src, extra_args=("--json", "-"))

    try:
        payload = json.loads(view.stdout)
    except ValueError as e:
        print(f"[FAIL] tests/{OPTIMIZER_DEEP_CHAIN}: --json payload is not valid JSON ({e})")
        return False

    converged = payload["stats"]["converged"]
    noted = "Note       :" in report.stdout

    ok = True
    if noted == converged:
        state = "converged" if converged else "hit the round cap"
        print(f"[FAIL] tests/{OPTIMIZER_DEEP_CHAIN}: the run {state} but the report "
              f"{'has' if noted else 'is missing'} its cap note")
        ok = False
    if converged:
        print(f"[FAIL] tests/{OPTIMIZER_DEEP_CHAIN}: fixture no longer exceeds the "
              f"round cap, so it cannot pin the cap note (see this check's docstring)")
        ok = False
    if ok:
        print(f"[PASS] tests/{OPTIMIZER_DEEP_CHAIN} (round cap reported)")
    return ok


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--update", action="store_true", help="Regenerate golden files from current output")
    args = cli.parse_args()

    results = [check_positive(name, args.update) for name in PROGRAMS]
    results += [check_feature(name, args.update) for name in FEATURE_FIXTURES]
    results.append(check_scanner_recovery(args.update))
    results.append(check_optimizer_report(args.update))
    if args.update:
        print("Golden files updated.")
        return

    results.append(check_optimizer_cap_note())
    results += [check_negative(*fixture) for fixture in NEGATIVE_FIXTURES]

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
        for path, tag, exit_code, *_ in NEGATIVE_FIXTURES if exit_code == 3
    ]

    passed, total = sum(results), len(results)
    print(f"\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
