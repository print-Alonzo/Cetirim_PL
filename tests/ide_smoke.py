"""Programmatic smoke test for the IDE (`cetirim_ide.py`).

Manual/auxiliary tool - NOT run by run_tests.py, so the main suite stays
Tk-free. Run it after touching the IDE or the debugger hooks:

    python3 tests/ide_smoke.py            # repo root inferred
    python3 tests/ide_smoke.py <repo>     # or given explicitly

Needs a Python whose tkinter is linked against Tk 8.6+ (Apple's bundled
Tk 8.5 draws blank windows; see README.md's "Cetirim IDE" section). It
briefly creates a real - but hidden - IDE window.

It runs the real Tk mainloop (the IDE's worker threads marshal UI updates
via `after()`, which requires the main thread to be inside mainloop) and
drives the test steps as a generator advanced by scheduled callbacks.
Exit code 0 = every check passed, 1 = failures, 42 = no display available.
"""
import functools
import sys
import time
import types
from pathlib import Path

REAL_OUT = sys.stdout  # the IDE redirects sys.stdout process-wide during runs
log = functools.partial(print, file=REAL_OUT, flush=True)
REPO = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import tkinter as tk

try:
    from cetirim_ide import SAMPLE, CetirimIDE
    ide = CetirimIDE()
except tk.TclError as e:
    log(f"SKIP-GUI: cannot create a Tk window here ({e})")
    sys.exit(42)

ide.withdraw()  # keep the test window off the user's screen

FAILURES = []
EXIT_CODE = {"value": 1}


def check(label, cond, detail=""):
    if cond:
        log(f"[PASS] {label}")
    else:
        log(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def console_text():
    return ide.console.get("1.0", "end")


def load(source, path=None):
    ide.editor.delete("1.0", "end")
    ide.editor.insert("1.0", source)
    ide.file_path = path
    ide.dirty = False
    ide._refresh_all()


def finished():
    text = console_text()
    return ("$ Program finished." in text or "$ Program stopped." in text
            or "Runtime error" in text)


def paused_at(line):
    """True once on_debug_pause ran for `line`: the current-line highlight is
    on that line and the Step button was re-enabled (the status-bar text is
    unreliable - the editor's debounced _refresh_all overwrites it)."""
    def cond():
        rng = ide.editor.tag_ranges("current_line")
        return (bool(rng) and str(rng[0]).startswith(f"{line}.")
                and str(ide.step_btn.cget("state")) == "normal")
    return cond


prog3 = REPO / "samples" / "prog3_functions.src"
expected3 = (REPO / "samples" / "prog3_functions_expected.txt").read_text(encoding="utf-8")


def driver():
    # ---- Test 1: welcome sample, interactive terminal input ----------------
    ide.run_program()
    ok = yield (lambda: ide.pending_input is not None, 30, "input request (welcome sample)")
    if ok:
        ide.terminal_entry.config(state="normal")
        ide.terminal_entry.delete(0, "end")
        ide.terminal_entry.insert(0, "Alonzo")
        ide.submit_terminal_input()
        ok = yield (finished, 30, "welcome sample finish")
        check("terminal input -> interpolated output", "Welcome, Alonzo!" in console_text(),
              repr(console_text()[-200:]))

    # ---- Test 2: samples/prog3_functions.src runs, matches golden ----------
    load(prog3.read_text(encoding="utf-8"), prog3)
    ide.run_program()
    yield (finished, 30, "prog3 finish")
    check("prog3 output matches committed golden", expected3 in console_text(),
          repr(console_text()[:300]))

    # ---- Test 3: tests/unequal_dims.src (2-D arrays, post-IDE-branch) ------
    ud = REPO / "tests" / "unequal_dims.src"
    load(ud.read_text(encoding="utf-8"), ud)
    ide.run_program()
    yield (finished, 30, "unequal_dims finish")
    expected_ud = (REPO / "tests" / "unequal_dims_expected.txt").read_text(encoding="utf-8")
    check("unequal_dims (post-merge feature) output matches golden", expected_ud in console_text(),
          repr(console_text()[:300]))

    # ---- Test 4: debugger - breakpoint, panels, watch, step, continue ------
    load(prog3.read_text(encoding="utf-8"), prog3)
    ide.breakpoints.clear()
    ide.breakpoints.add(87)  # the "Before swap" print - x=10, y=20 assigned
    ide.debug_program()
    ok = yield (paused_at(87), 30, "pause at breakpoint 87")
    if ok:
        stack = list(ide.callstack_list.get(0, "end"))
        check("call stack shows main at pause", stack == ["main"], repr(stack))
        var_rows = list(ide.vars_list.get(0, "end"))
        check("variables panel shows x = 10", "x = 10" in var_rows, repr(var_rows))
        check("variables panel shows y = 20", "y = 20" in var_rows, repr(var_rows))
        check("variables panel hides _t temps", not any("_t" in r for r in var_rows), repr(var_rows))
        check("variables panel hides IR-qualified names", not any("main." in r for r in var_rows), repr(var_rows))
        ide.watch_entry.delete(0, "end")
        ide.watch_entry.insert(0, "x")
        ide.add_watch()
        watch_rows = list(ide.watch_list.get(0, "end"))
        check("watch resolves x = 10", "x = 10" in watch_rows, repr(watch_rows))
        ide.debug_step()
        ok = yield (paused_at(88), 30, "step to line 88")
        check("step advanced exactly one source line", ok)
        trace_rows = list(ide.trace_list.get(0, "end"))
        check("trace logged the paused line with its function", "main  —  line 87" in trace_rows,
              repr(trace_rows[-5:]))
        ide.debug_continue()
        yield (finished, 30, "debug continue to finish")
        check("debug run produced full program output", expected3 in console_text(),
              repr(console_text()[:200]))
        check("watch shows <not in scope> after finish",
              any("not in scope" in r for r in ide.watch_list.get(0, "end")),
              repr(list(ide.watch_list.get(0, "end"))))

    # ---- Test 5: debugger Stop from a paused state -------------------------
    load(prog3.read_text(encoding="utf-8"), prog3)
    ide.breakpoints.clear()
    ide.breakpoints.add(87)
    ide.debug_program()
    ok = yield (paused_at(87), 30, "pause before stop test")
    if ok:
        ide.debug_stop()
        yield (finished, 30, "stop unwinds the run")
        check("stop aborts cleanly (no full output, no crash)", expected3 not in console_text(),
              repr(console_text()[:200]))
        check("stop re-disables the stop button", str(ide.stop_btn.cget("state")) == "disabled")

    # ---- Test 6: Problems tab on a syntax error, run bails safely ----------
    load("void main() {\n    print(1)\n}\n")
    ok6 = ide.check_code()
    problems = list(ide.problems.get(0, "end"))
    check("check_code flags the missing semicolon", ok6 is False and len(problems) >= 1, repr(problems))
    ide.run_program()  # must return early without starting a thread
    check("run_program bails on syntax errors without crashing", ide.executor is None)

    # ---- Test 7: Optimizer tab on tests/optimizer_demo.src -----------------
    demo = REPO / "tests" / "optimizer_demo.src"
    load(demo.read_text(encoding="utf-8"), demo)
    ide.show_optimization()
    stats_text = ide.optimizer_stats.cget("text")
    check("optimizer stats show 86 -> 54 quads", "86 → 54 quads" in stats_text, repr(stats_text))
    log_rows = list(ide.opt_log.get(0, "end"))
    for tech in ("constant-propagation", "algebraic-simplification", "dead-code-elimination"):
        check(f"transformation log has {tech} entries", any(tech in r for r in log_rows),
              repr(log_rows[:3]))
    check("original pane marks removed quads", bool(ide.opt_before.tag_ranges("removed")))
    check("original pane marks rewritten quads", bool(ide.opt_before.tag_ranges("rewritten")))
    n_after = int(ide.opt_after.index("end-1c").split(".")[0]) - 1  # trailing newline
    check("optimized pane lists 54 quads", n_after == 54, f"got {n_after} rows")
    check("Optimizer tab got selected", ide.bottom_tabs.select() == str(ide.optimizer_frame))
    ide.run_program()  # Run still executes the unoptimized IR normally afterwards
    yield (finished, 30, "optimizer_demo run after optimize view")
    expected_demo = (REPO / "tests" / "optimizer_demo_expected.txt").read_text(encoding="utf-8")
    check("optimizer_demo still runs and matches golden", expected_demo in console_text(),
          repr(console_text()[:200]))

    # ---- Test 8: empty terminal submit re-prompts instead of erroring ------
    load(SAMPLE)
    ide.run_program()
    ok = yield (lambda: ide.pending_input is not None, 30, "input request (empty-submit test)")
    if ok:
        ide.terminal_entry.config(state="normal")
        ide.terminal_entry.delete(0, "end")
        ide.submit_terminal_input()  # empty line - must re-prompt, not error
        ok = yield (lambda: ide.pending_input is not None, 30, "re-prompt after empty submit")
        check("empty submit re-prompts without a runtime error",
              ok and "Runtime error" not in console_text(), repr(console_text()[-200:]))
        ide.terminal_entry.config(state="normal")
        ide.terminal_entry.delete(0, "end")
        ide.terminal_entry.insert(0, "Alonzo")
        ide.submit_terminal_input()
        yield (finished, 30, "finish after re-prompt")
        check("run still completes normally after an empty submit",
              "Welcome, Alonzo!" in console_text(), repr(console_text()[-200:]))

    # ---- Test 9: Stop interrupts a run blocked at an input() prompt --------
    load(SAMPLE)
    ide.run_program()  # plain Run, not Debug
    ok = yield (lambda: ide.pending_input is not None, 30, "input request (stop test)")
    if ok:
        check("Stop button is enabled during a plain Run",
              str(ide.stop_btn.cget("state")) == "normal")
        ide.debug_stop()
        yield (finished, 30, "stop while waiting for input")
        check("stop while input-blocked ends with 'stopped', no error",
              "$ Program stopped." in console_text() and "Runtime error" not in console_text(),
              repr(console_text()[-200:]))

    # ---- Test 10: Stop halts a free-running loop in plain Run mode ---------
    load("void main() {\n"
         "    var int i;\n"
         "    var int s = 0;\n"
         "    for (i = 0; i < 100000000; i = i + 1) {\n"
         "        s = s + 1;\n"
         "    }\n"
         "    print(s);\n"
         "}\n")
    ide.run_program()  # plain Run - no debugger hooks attached
    yield (lambda: ide.executor is not None and "$ Running program" in console_text(),
           30, "busy loop started")
    ide.debug_stop()
    yield (finished, 45, "stop halts the busy loop")
    check("stopping a Run-mode loop works (no step-cap error, no output)",
          "$ Program stopped." in console_text() and "Runtime error" not in console_text()
          and "100000000" not in console_text(), repr(console_text()[-250:]))

    # ---- Test 11: starting a new run retires a paused old one cleanly ------
    load(prog3.read_text(encoding="utf-8"), prog3)
    ide.breakpoints.clear()
    ide.breakpoints.add(87)
    ide.debug_program()
    ok = yield (paused_at(87), 30, "pause before re-run test")
    if ok:
        ide.breakpoints.clear()
        ide.run_program()  # plain Run while the old debug run is still paused
        yield (finished, 30, "re-run over a paused run")
        check("new run completes with exactly one copy of the output",
              console_text().count(expected3) == 1, repr(console_text()[:200]))
        check("stale run did not clobber the new run's state",
              ide.executor is None and str(ide.step_btn.cget("state")) == "disabled")

    # ---- Test 12: "(top level)" call-stack fallback is reachable -----------
    ide.executor = types.SimpleNamespace(call_names=[], frame={}, globals={})
    ide.refresh_debug_panels()
    check("call-stack panel falls back to (top level)",
          "(top level)" in ide.callstack_list.get(0, "end"),
          repr(list(ide.callstack_list.get(0, "end"))))
    ide.executor = None


gen = driver()
state = {}


def finish_tests():
    log()
    if FAILURES:
        log(f"SMOKE RESULT: {len(FAILURES)} failure(s): {FAILURES}")
        EXIT_CODE["value"] = 1
    else:
        log("SMOKE RESULT: all IDE smoke checks passed")
        EXIT_CODE["value"] = 0
    ide.destroy()


def advance(send_val):
    try:
        cond, timeout, label = gen.send(send_val)
    except StopIteration:
        finish_tests()
        return
    except Exception as e:  # a test-body bug shouldn't hang the harness
        log(f"[FAIL] driver crashed: {e!r}")
        FAILURES.append("driver crash")
        finish_tests()
        return
    state.update(cond=cond, deadline=time.time() + timeout, label=label)
    ide.after(20, poll)


def poll():
    try:
        satisfied = state["cond"]()
    except Exception as e:
        log(f"[FAIL] wait condition for {state['label']} raised: {e!r}")
        FAILURES.append(state["label"])
        advance(False)
        return
    if satisfied:
        advance(True)
    elif time.time() > state["deadline"]:
        log(f"[FAIL] timed out waiting for {state['label']}")
        FAILURES.append(state["label"])
        advance(False)
    else:
        ide.after(20, poll)


ide.after(100, lambda: advance(None))
ide.mainloop()
sys.exit(EXIT_CODE["value"])
