"""Golden-output test runner for the scanner -> parser -> semantics -> IR ->
interpreter pipeline.

    python run_tests.py            run every check, report pass/fail
    python run_tests.py --update   regenerate the golden files from current output

Positive fixtures (prog1..prog5): each program's compiled quadruple listing
and executed output are diffed against the committed goldens (progN_ir.txt,
progN_expected.txt). Programs that call input() read from progN.in.

Negative fixtures (tests/*.src): each is expected to fail at one specific
phase; the runner only checks that the right "[... ERROR]" tag appears and
that the process exits 2 - it does not pin exact wording, so error-message
copy can still be improved without breaking the suite.
"""

import argparse
import difflib
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
]

NEGATIVE_FIXTURES = [
    ("tests/lex_error.src", "[LEXICAL ERROR]"),
    ("tests/syntax_error.src", "[SYNTAX ERROR]"),
    ("tests/semantic_error.src", "[SEMANTIC ERROR]"),
]


def run(script, src, stdin_text=""):
    return subprocess.run(
        [sys.executable, script, src],
        cwd=ROOT, input=stdin_text, capture_output=True, text=True,
    )


def _print_diff(expected, actual):
    diff = difflib.unified_diff(
        expected.splitlines(keepends=True),
        actual.splitlines(keepends=True),
        fromfile="expected", tofile="actual",
    )
    sys.stdout.writelines(diff)


def check_positive(name, update):
    src = f"{name}.src"
    in_file = ROOT / f"{name}.in"
    expected_file = ROOT / f"{name}_expected.txt"
    ir_file = ROOT / f"{name}_ir.txt"
    stdin_text = in_file.read_text() if in_file.exists() else ""

    ir_result = run("ir.py", src)
    run_result = run("interpreter.py", src, stdin_text)
    actual_ir = ir_result.stdout
    actual_out = run_result.stdout + run_result.stderr

    if update:
        ir_file.write_text(actual_ir)
        expected_file.write_text(actual_out)
        print(f"[UPDATED] {name}")
        return True

    ok = True
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


def check_negative(path, tag):
    result = run("interpreter.py", str(ROOT / path))
    combined = result.stdout + result.stderr
    ok = tag in combined and result.returncode == 2
    print(f"[{'PASS' if ok else 'FAIL'}] {path}: expected {tag!r} and exit 2, got exit {result.returncode}")
    if not ok:
        print(combined)
    return ok


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--update", action="store_true", help="Regenerate golden files from current output")
    args = cli.parse_args()

    results = [check_positive(name, args.update) for name in PROGRAMS]
    if args.update:
        print("Golden files updated.")
        return

    results += [check_negative(path, tag) for path, tag in NEGATIVE_FIXTURES]

    passed, total = sum(results), len(results)
    print(f"\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
