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
    from scanner import TT, Scanner
    from cetirim_ide import SAMPLE, THEME, CetirimIDE
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


# --- Treeview readers ------------------------------------------------------
# The debugger panels and the Problems list are Treeviews (aligned columns
# beat "name = value" strings in a Listbox), so the checks below read rows
# through these instead of Listbox.get().

def tree_texts(tree, parent=""):
    """Every row's column-#0 text, depth-first."""
    rows = []
    for item in tree.get_children(parent):
        rows.append(tree.item(item, "text"))
        rows.extend(tree_texts(tree, item))
    return rows


def tree_pairs(tree, parent=""):
    """Leaf rows of a name/value tree, rendered "name = value". Group rows
    (Locals/Globals) carry no values and are skipped."""
    rows = []
    for item in tree.get_children(parent):
        values = tree.item(item, "values")
        if values:
            rows.append(f"{tree.item(item, 'text')} = {values[0]}")
        rows.extend(tree_pairs(tree, item))
    return rows


def problem_rows():
    return [" ".join((ide.problems.item(i, "text"),) + tuple(ide.problems.item(i, "values")))
            for i in ide.problems.get_children()]


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
        stack = tree_texts(ide.callstack_list)
        check("call stack shows main at pause", stack == ["main"], repr(stack))
        var_rows = tree_pairs(ide.vars_list)
        check("variables panel groups rows under Locals",
              "Locals" in tree_texts(ide.vars_list), repr(tree_texts(ide.vars_list)[:4]))
        check("variables panel shows x = 10", "x = 10" in var_rows, repr(var_rows))
        check("variables panel shows y = 20", "y = 20" in var_rows, repr(var_rows))
        check("variables panel hides _t temps", not any("_t" in r for r in var_rows), repr(var_rows))
        check("variables panel hides IR-qualified names", not any("main." in r for r in var_rows), repr(var_rows))
        ide.watch_entry.delete(0, "end")
        ide.watch_entry.insert(0, "x")
        ide.add_watch()
        watch_rows = tree_pairs(ide.watch_list)
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
              any("not in scope" in r for r in tree_pairs(ide.watch_list)),
              repr(tree_pairs(ide.watch_list)))

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
    problems = problem_rows()
    check("check_code flags the missing semicolon", ok6 is False and len(problems) >= 1, repr(problems))
    check("problem row carries its source line for navigation",
          any(v for v in ide._problem_lines.values()), repr(ide._problem_lines))
    check("problem row splits position out of the message",
          any("Line" in r for r in problems), repr(problems))
    check("problems tab shows the diagnostic count as a badge",
          "Problems" in ide.bottom_tabs._entry(ide.problems_frame)["label"].cget("text")
          and ide.bottom_tabs._entry(ide.problems_frame)["label"].cget("text").strip() != "Problems",
          repr(ide.bottom_tabs._entry(ide.problems_frame)["label"].cget("text")))
    check("status bar summarises the diagnostics", "✖" in ide.status.cget("text"),
          repr(ide.status.cget("text")))
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
          "(top level)" in tree_texts(ide.callstack_list),
          repr(tree_texts(ide.callstack_list)))
    ide.executor = None

    # ---- Test 13: find/replace bar -----------------------------------------
    load("void main() {\n    print(1);\n    print(2);\n}\n")
    ide.show_find_bar()
    ide.find_entry.delete(0, "end")
    ide.find_entry.insert(0, "print")
    ide._find_refresh(force=True)
    n_matches = len(ide.editor.tag_ranges("find_match")) // 2  # ranges come as start/end pairs
    check("find bar highlights every match", n_matches == 2, f"got {n_matches} matches")
    ide.find_next()
    check("find-next selects a current match", bool(ide.editor.tag_ranges("find_current")))
    ide.replace_entry.delete(0, "end")
    ide.replace_entry.insert(0, "write")
    ide.replace_all()
    check("replace-all rewrites every match",
          ide.source().count("write") == 2 and "print" not in ide.source(), repr(ide.source()))
    ide.hide_find_bar()
    check("hiding the find bar clears its highlights",
          not ide.editor.tag_ranges("find_match") and not ide.editor.tag_ranges("find_current"))

    # ---- Test 14: Symbols tab shows the semantic analyzer's symbol table ---
    load(prog3.read_text(encoding="utf-8"), prog3)
    ide.check_code()
    problems_rows = problem_rows()
    check("problems shows the ✓ all-clear row on a clean program",
          bool(problems_rows) and problems_rows[0].startswith("✓"), repr(problems_rows))
    check("status bar reports a clean check", "No problems" in ide.status.cget("text"),
          repr(ide.status.cget("text")))
    roots = {ide.symbols_tree.item(i, "text"): i for i in ide.symbols_tree.get_children()}
    check("symbol table lists a Functions root", "Functions" in roots, repr(list(roots)))
    if "Functions" in roots:
        func_items = ide.symbols_tree.get_children(roots["Functions"])
        func_names = [ide.symbols_tree.item(i, "text") for i in func_items]
        check("symbol table lists main()", any(t.startswith("main(") for t in func_names), repr(func_names))
        main_items = [i for i in func_items if ide.symbols_tree.item(i, "text").startswith("main(")]
        if main_items:
            details = [str(ide.symbols_tree.item(i, "values")) for i in ide.symbols_tree.get_children(main_items[0])]
            check("main's locals expose their unique ir names",
                  any("ir: main." in d for d in details), repr(details[:4]))

    # ---- Test 15: paused status segment survives the debounced refresh -----
    ide.breakpoints.clear()
    ide.breakpoints.add(87)
    ide.debug_program()
    ok = yield (paused_at(87), 30, "pause for the status-segment test")
    if ok:
        ide._refresh_all()  # used to clobber "Paused at line N" when it lived in the single status label
        check("paused status survives a debounced refresh",
              ide.status_run.cget("text") == "Paused at line 87", repr(ide.status_run.cget("text")))
        ide.debug_stop()
        yield (finished, 30, "stop after the status-segment test")
    ide.breakpoints.clear()

    # ---- Test 16: panel tab strip behaves like the Notebook it replaced ----
    ide.bottom_tabs.select(ide.symbols_frame)
    check("tab strip reports the selected frame",
          ide.bottom_tabs.select() == str(ide.symbols_frame), repr(ide.bottom_tabs.select()))
    accented = [e["text"] for e in ide.bottom_tabs._tabs
                if e["underline"].cget("background") == THEME["accent"]]
    check("exactly the selected tab carries the accent underline",
          accented == ["Symbols"], repr(accented))
    ide.bottom_tabs.select(0)
    check("tab strip selects by index too", ide.bottom_tabs.select() != str(ide.symbols_frame))

    # ---- Test 17: the outline, caret line and font-size controls ------------
    def outline_rows(parent=""):
        return [ide.outline.item(i, "text") for i in ide.outline.get_children(parent)]

    def outline_child_rows(needle):
        """Rows nested under the first top-level row containing `needle`."""
        for i in ide.outline.get_children(""):
            if needle in ide.outline.item(i, "text"):
                return outline_rows(i)
        return []

    load("struct Point {\n    int x;\n};\n\nvoid main() {\n    print(1);\n}\n")
    check("outline lists the struct and the function",
          any("Point" in r for r in outline_rows()) and any("main" in r for r in outline_rows()),
          repr(outline_rows()))
    check("struct fields nest under the struct",
          [r.split()[-1] for r in outline_child_rows("Point")] == ["x"],
          repr(outline_child_rows("Point")))

    # An inline `typedef struct C {...} R;` used to be split by the outline's
    # regex at the first ';', listing the field `r` as a typedef. It has to
    # produce a struct row (with its fields) plus the alias, and nothing else.
    load("const int LIMIT = 5;\n"
         "typedef struct Color { int r; int g; int b; } RGB;\n"
         "int scale(int base, int factor) {\n"
         "    return base * factor;\n"
         "}\n"
         "void main() {\n"
         "    var int count;\n"
         "    let total = 0;\n"
         "    print(count, total, scale(2, 3));\n"
         "}\n")
    tops = [r.strip() for r in outline_rows()]
    check("inline typedef-struct yields a struct row and an alias row, not a field",
          tops == ["▣  LIMIT", "◆  Color", "◇  RGB", "ƒ  scale", "ƒ  main"], repr(tops))
    check("struct fields of an inline typedef nest under it",
          [r.split()[-1] for r in outline_child_rows("Color")] == ["r", "g", "b"],
          repr(outline_child_rows("Color")))
    check("function locals nest under the function",
          [r.split()[-1] for r in outline_child_rows("main")] == ["count", "total"],
          repr(outline_child_rows("main")))
    check("parameters are listed as locals of their function",
          [r.split()[-1] for r in outline_child_rows("scale")] == ["base", "factor"],
          repr(outline_child_rows("scale")))
    check("outline navigates to a nested row's own line",
          ide.outline.item(
              [i for i in ide.outline.get_children(
                  [c for c in ide.outline.get_children("") if "main" in ide.outline.item(c, "text")][0])
               if "count" in ide.outline.item(i, "text")][0], "values")[0] in (7, "7"),
          "expected the 'var int count;' line")

    # A buffer that recovers to nothing must not blank the tree.
    before_rows = outline_rows()
    load("const int LIMIT = 5;\n@@@\n")
    check("a mid-edit unparseable buffer keeps the last good outline",
          outline_rows() == before_rows, repr(outline_rows()))

    load("struct Point {\n    int x;\n};\n\nvoid main() {\n    print(1);\n}\n")
    ide.editor.mark_set("insert", "6.0")
    ide._sync_caret()
    caret = ide.editor.tag_ranges("cursor_line")
    check("caret-line highlight follows the cursor",
          bool(caret) and str(caret[0]).startswith("6."), repr([str(c) for c in caret]))
    before = int(ide.font_editor.cget("size"))
    ide.change_font_size(2)
    grown = int(ide.font_editor.cget("size"))
    ide.change_font_size(-2)
    check("font size control resizes the editor font",
          grown == before + 2 and int(ide.font_editor.cget("size")) == before,
          f"{before} -> {grown} -> {ide.font_editor.cget('size')}")
    ide.change_font_size(-99)
    check("font size stays inside its bounds", int(ide.font_editor.cget("size")) == 9,
          repr(ide.font_editor.cget("size")))
    ide.font_editor.configure(size=before)

    # ---- Test 18: debugger transport lives in the toolbar, idle by default --
    check("transport buttons are disabled with no run in flight",
          all(str(b.cget("state")) == "disabled"
              for b in (ide.step_btn, ide.step_over_btn, ide.continue_btn, ide.stop_btn)))
    check("transport buttons sit in the header, not inside the bottom panel",
          not str(ide.step_btn).startswith(str(ide.bottom_tabs)),
          f"{ide.step_btn} vs {ide.bottom_tabs}")

    # ---- Test 19: syntax highlighting is scanner-driven ---------------------
    # Every check here is a case the two regexes this replaced got wrong: they
    # could not see that a word sat inside a comment or a string, and they
    # measured a literal by its escape-resolved lexeme rather than its width
    # on screen.
    SYNTAX_TAGS = {"keyword", "type", "string", "number", "comment", "function", "error"}

    def tags_at(needle, offset=0):
        """Syntax tags on the character `offset` past the start of `needle`."""
        start = ide.editor.search(needle, "1.0", "end")
        if not start:
            return set()
        return SYNTAX_TAGS.intersection(ide.editor.tag_names(f"{start}+{offset}c"))

    load('// a comment that has (parens) in it\n'
         'const string URL = "http://x.com";\n'
         'const string ESC = "a\\tb";\n'
         '/* a block\n'
         '   comment */\n'
         'int twice(int n) {\n'
         '    return n * 2;\n'
         '}\n'
         'void main() {\n'
         '    var int i;\n'
         '    for (i = 0; i < 3; i = i + 1) {\n'
         '        print(twice(i));\n'
         '    }\n'
         '}\n')

    check("a word before '(' inside a comment stays a comment",
          tags_at("has (") == {"comment"}, repr(tags_at("has (")))
    check("a keyword before '(' is not painted as a call",
          tags_at("for (") == {"keyword"}, repr(tags_at("for (")))
    check("print is a keyword, not a call",
          tags_at("print(") == {"keyword"}, repr(tags_at("print(")))
    check("a real call is painted as a function",
          tags_at("twice(i)") == {"function"}, repr(tags_at("twice(i)")))
    check("'//' inside a string literal does not start a comment",
          tags_at('"http://x.com"', 8) == {"string"}, repr(tags_at('"http://x.com"', 8)))
    # 6 source characters: " a \ t b " - the old length arithmetic used the
    # 5-character resolved lexeme and left the closing quote unpainted.
    check("a literal with an escape is painted through its closing quote",
          tags_at('"a\\tb"', 5) == {"string"}, repr(tags_at('"a\\tb"', 5)))
    check("a multi-line block comment is painted to its end",
          tags_at("comment */", 9) == {"comment"}, repr(tags_at("comment */", 9)))

    # An unterminated block comment: the regex needed the closing '*/' and so
    # left it plain until the moment it was typed.
    load("var int x;\n/* still typing this\n")
    check("an unterminated block comment is grey while being typed",
          tags_at("still typing") == {"comment"}, repr(tags_at("still typing")))
    # The editor's buffer has no trailing newline, so the comment runs to the
    # last character - which the scanner's loop used to stop one short of,
    # leaking it back out as a stray token the highlighter would then colour.
    leaked = [t.lexeme for t in Scanner(ide.source()).scan_all()[0] if t.ttype != TT.EOF]
    check("an unterminated block comment consumes its last character",
          leaked == ["var", "int", "x", ";"], repr(leaked))

    # An unterminated string marks the whole run, not just its opening quote.
    load('void main() {\n    print("still typing\n}\n')
    check("an unterminated string literal is marked across its whole run",
          "error" in ide.editor.tag_names(ide.editor.search("still", "1.0", "end")),
          repr(ide.editor.tag_names(ide.editor.search("still", "1.0", "end"))))

    # ---- Test 20: token classes differ in weight/slant, not only colour -----
    check("keywords are bold", str(ide.editor.tag_cget("keyword", "font")) == str(ide.font_editor_bold),
          repr(ide.editor.tag_cget("keyword", "font")))
    check("comments are italic", str(ide.editor.tag_cget("comment", "font")) == str(ide.font_editor_italic),
          repr(ide.editor.tag_cget("comment", "font")))
    base = int(ide.font_editor.cget("size"))
    ide.change_font_size(2)
    check("the bold and italic faces resize with the editor font",
          int(ide.font_editor_bold.cget("size")) == base + 2
          and int(ide.font_editor_italic.cget("size")) == base + 2,
          f"bold={ide.font_editor_bold.cget('size')} italic={ide.font_editor_italic.cget('size')}")
    ide.change_font_size(-2)

    # ---- Test 21: the two trace modes - Step In vs Step Over ---------------
    # samples/prog3_functions.src line 88 is `int sx, sy = swap(x, y);` and
    # swap's body is line 17, so the whole difference between the modes is
    # which of those the next pause lands on. Both runs break on 87 and step
    # once to reach 88, rather than breaking on 88 directly: a call statement's
    # own line is revisited after RETURN (the quads that store the result carry
    # it too), so a breakpoint there would fire a second time on the way back
    # and mask what the step itself did.
    load(prog3.read_text(encoding="utf-8"), prog3)
    ide.breakpoints.clear()
    ide.breakpoints.add(87)
    ide.debug_program()
    ok = yield (paused_at(87), 30, "pause at 87 before the step-in test")
    if ok:
        ide.debug_step()
        ok = yield (paused_at(88), 30, "step to the call on line 88")
        if ok:
            ide.debug_step()
            ok = yield (paused_at(17), 30, "step in reaches swap's body")
            check("Step In descends into the called function", ok)
            check("call stack shows swap above main after stepping in",
                  tree_texts(ide.callstack_list) == ["swap", "main"],
                  repr(tree_texts(ide.callstack_list)))
        ide.breakpoints.clear()
        ide.debug_continue()
        yield (finished, 30, "finish after the step-in test")

    load(prog3.read_text(encoding="utf-8"), prog3)
    ide.breakpoints.clear()
    ide.breakpoints.add(87)
    ide.debug_program()
    ok = yield (paused_at(87), 30, "pause at 87 before the step-over test")
    if ok:
        ide.debug_step()
        ok = yield (paused_at(88), 30, "step to the call before stepping over it")
        if ok:
            ide.debug_step_over()
            ok = yield (paused_at(89), 30, "step over lands on line 89")
            check("Step Over runs the call through to the next line", ok)
            check("Step Over never left main's frame",
                  tree_texts(ide.callstack_list) == ["main"],
                  repr(tree_texts(ide.callstack_list)))
            var_rows = tree_pairs(ide.vars_list)
            check("Step Over still applied the call's results (sx = 20, sy = 10)",
                  "sx = 20" in var_rows and "sy = 10" in var_rows, repr(var_rows))
        ide.breakpoints.clear()
        ide.debug_continue()
        yield (finished, 30, "finish after the step-over test")
        check("a stepped-over debug run still produces the full program output",
              expected3 in console_text(), repr(console_text()[:200]))

    # A breakpoint inside a function being stepped over still wins: the
    # stepping state gates only the step's own pause, never a breakpoint's.
    load(prog3.read_text(encoding="utf-8"), prog3)
    ide.breakpoints.clear()
    ide.breakpoints.update({91, 25})  # the array_sum(...) call, and a line in its loop
    ide.debug_program()
    ok = yield (paused_at(91), 30, "pause at the array_sum call")
    if ok:
        ide.debug_step_over()
        ok = yield (paused_at(25), 30, "breakpoint inside the stepped-over call")
        check("a breakpoint inside a stepped-over function still pauses", ok)
        ide.breakpoints.clear()
        ide.debug_continue()
        yield (finished, 30, "finish after the breakpoint-in-step-over test")
    check("Step Over goes idle again once the run ends",
          str(ide.step_over_btn.cget("state")) == "disabled")


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
