# golden-output test runner for the whole pipeline
# python run_tests.py [--update]

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
    # checklist coverage set, one program per rubric row group (see docs/CHECKLIST_COVERAGE.md)
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
    # the 5 semantic errors named in the rubric, plus one file combining all of them
    ("tests/sem_undeclared_var.src", "[SEMANTIC ERROR]", 2),
    ("tests/sem_type_mismatch.src", "[SEMANTIC ERROR]", 2),
    ("tests/sem_redeclared_var.src", "[SEMANTIC ERROR]", 2),
    ("tests/sem_const_reassign.src", "[SEMANTIC ERROR]", 2),
    ("tests/sem_arg_cardinality.src", "[SEMANTIC ERROR]", 2),
    ("tests/sem_multi_errors.src", "[SEMANTIC ERROR]", 2, 5),  # must report all 5, not stop at the first
    # two missing semicolons in two different functions, both must be reported
    ("tests/syn_multi_errors.src", "[SYNTAX ERROR]", 2, 2),
]

# checked by diffing the whole token stream, not by tag/exit code (see check_scanner_recovery)
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

OPTIMIZER_DEMO = "optimizer_demo"  # its optimizer report is the golden the README points to
OPTIMIZER_DEEP_CHAIN = "optimizer_deep_chain"  # dead chain longer than the optimizer's round cap


def run(script, src, stdin_text="", extra_args=()):
    args = [sys.executable, script, src, *extra_args]
    try:
        return subprocess.run(
            args, cwd=ROOT, input=stdin_text, capture_output=True, text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        # catches an infinite loop in the parser/interpreter so one bad test doesn't hang the whole run
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
    # scan time is real output but changes every run, so ignore it when comparing token streams
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
    # diffs the whole token stream since a lexical error never stops the scan, so the exit code alone can't prove recovery happened
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
    # reuses the unoptimized golden, -O must still produce byte-identical output
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
    # runtime errors must survive optimization, e.g. div by zero should still raise, not get folded away
    result = run("interpreter.py", str(ROOT / path), extra_args=("-O",))
    combined = result.stdout + result.stderr
    ok = tag in combined and result.returncode == exit_code
    print(f"[{'PASS' if ok else 'FAIL'}] {path} (-O): expected {tag!r} and exit {exit_code}, got exit {result.returncode}")
    if not ok:
        print(combined)
    return ok


def check_optimizer_report(update):
    # diffs the optimizer's report, then checks it actually shrank the program and used all 3 techniques
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
    # this fixture's dead chain is longer than the round cap on purpose, so the report and --json payload should both admit it didn't fully converge
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
              f"round cap, so it can't pin the cap note")
        ok = False
    if ok:
        print(f"[PASS] tests/{OPTIMIZER_DEEP_CHAIN} (round cap reported)")
    return ok


def main():
    cli = argparse.ArgumentParser(description="run the test suite")
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
