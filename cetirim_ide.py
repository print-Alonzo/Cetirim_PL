"""A standalone IDE for the CSC617M custom language. Run: python cetirim_ide.py"""
from __future__ import annotations

import contextlib
import io
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from parser import parse_source
from scanner import KEYWORDS, Scanner, TT

SAMPLE = '''// Welcome to the Cetirim Language IDE
// Ctrl+Space opens templates and keyword autocomplete.
// F5 runs the program in the terminal below.

void main() {
    var string name;
    print("What is your name?");
    input(name);
    print(`Welcome, {name}!`);
}
'''

NEW_FILE = '''void main() {
    // Write your code here.
}
'''
TEMPLATES = {
    "function": "int ${name}(int ${value}) {\n    ${cursor}\n    return 0;\n}",
    "if / else": "if (${condition}) {\n    ${cursor}\n} else {\n    \n}",
    "for loop": "for (var int ${i} = 0; ${i} < ${limit}; ${i} = ${i} + 1) {\n    ${cursor}\n}",
    "while loop": "while (${condition}) {\n    ${cursor}\n}",
    "match": "match (${value}) {\n    ${pattern} => ${cursor};\n    _ => ;\n}",
    "try / catch": "try {\n    ${cursor}\n} catch (${error}) {\n    \n} finally {\n    \n}",
    "variable": "var int ${name} = ${value};${cursor}",
}


class CetirimIDE(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Cetirim IDE — Custom Language Environment")
        self.geometry("1250x780"); self.minsize(900, 580)
        self.file_path = None; self.dirty = False; self.refresh_id = None
        self.pending_input = None
        self.breakpoints = set()
        self.executor = None
        self.watches = []
        self._apply_theme()
        self._build_ui()
        self.editor.insert("1.0", SAMPLE); self._refresh_all()

    def _apply_theme(self):
        """A purpose-built palette rather than the platform's generic widgets."""
        self.configure(background="#0b1020")
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background="#0b1020", foreground="#dbeafe", font=("Segoe UI", 10))
        style.configure("TFrame", background="#0b1020")
        style.configure("Toolbar.TFrame", background="#121a2d")
        style.configure("TLabel", background="#0b1020", foreground="#b9c6db")
        style.configure("Title.TLabel", background="#121a2d", foreground="#f8fafc", font=("Segoe UI Semibold", 12))
        style.configure("Section.TLabel", background="#0b1020", foreground="#7dd3fc", font=("Segoe UI Semibold", 9))
        style.configure("TButton", background="#1f2a44", foreground="#e5efff", borderwidth=0, padding=(11, 6))
        style.map("TButton", background=[("active", "#334b75"), ("pressed", "#17233a")], foreground=[("disabled", "#64748b")])
        style.configure("Accent.TButton", background="#0e7490", foreground="white")
        style.map("Accent.TButton", background=[("active", "#0891b2"), ("pressed", "#155e75")])
        style.configure("TEntry", fieldbackground="#111b30", foreground="#e5efff", bordercolor="#334155", lightcolor="#334155", darkcolor="#334155", padding=6)
        style.configure("TSpinbox", fieldbackground="#111b30", foreground="#e5efff", arrowcolor="#7dd3fc")
        style.configure("Treeview", background="#111827", fieldbackground="#111827", foreground="#cbd5e1", borderwidth=0, rowheight=28)
        style.map("Treeview", background=[("selected", "#164e63")], foreground=[("selected", "#ecfeff")])
        style.configure("TNotebook", background="#0b1020", borderwidth=0)
        style.configure("TNotebook.Tab", background="#121a2d", foreground="#8fa1bb", padding=(15, 7), borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", "#17233a"), ("active", "#1f2a44")], foreground=[("selected", "#7dd3fc")])
        style.configure("TSeparator", background="#26344f")

    def _build_ui(self):
        self._make_menu()
        toolbar = ttk.Frame(self, padding=(14, 9), style="Toolbar.TFrame"); toolbar.pack(fill="x")
        ttk.Label(toolbar, text="CETIRIM", style="Title.TLabel").pack(side="left", padx=(0, 20))
        for text, callback in (("New", self.new_file), ("Open", self.open_file), ("Save", self.save_file),
                               ("Run ▶", self.run_program), ("Debug", self.debug_program), ("Check", self.check_code),
                               ("Rename…", self.rename_symbol), ("Templates", self.show_templates)):
            ttk.Button(toolbar, text=text, command=callback, style="Accent.TButton" if text == "Run ▶" else "TButton").pack(side="left", padx=(0, 5))
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=7)
        ttk.Label(toolbar, text="Font").pack(side="left")
        self.font_size = tk.IntVar(value=13)
        ttk.Spinbox(toolbar, from_=9, to=24, width=4, textvariable=self.font_size,
                    command=self.change_font).pack(side="left", padx=5)
        main = ttk.PanedWindow(self, orient="horizontal"); main.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        left, center = ttk.Frame(main, width=230), ttk.Frame(main)
        main.add(left, weight=1); main.add(center, weight=5)
        ttk.Label(left, text="CODE OUTLINE", style="Section.TLabel").pack(anchor="w", pady=(3, 7))
        self.outline = ttk.Treeview(left, show="tree", selectmode="browse"); self.outline.pack(fill="both", expand=True)
        self.outline.bind("<<TreeviewSelect>>", self.go_to_outline)
        self.line_numbers = tk.Text(center, width=4, height=1, padx=6, pady=8, takefocus=0, state="disabled", background="#1d2330", foreground="#8792a2", borderwidth=0, font=("Consolas", 13), cursor="arrow")
        self.line_numbers.pack(side="left", fill="y")
        self.line_numbers.tag_configure("breakpoint", background="#7f1d1d", foreground="#fca5a5")
        self.line_numbers.bind("<Button-1>", self.toggle_breakpoint)
        self.line_numbers.bind("<MouseWheel>", self._gutter_scroll)
        scroll = ttk.Scrollbar(center, orient="vertical"); scroll.pack(side="right", fill="y")
        self.editor = tk.Text(center, height=1, undo=True, wrap="none", borderwidth=0, padx=10, pady=8, background="#111827", foreground="#e5e7eb", insertbackground="white", selectbackground="#34517a", font=("Consolas", 13), yscrollcommand=lambda a, b: self._scroll_both(a, b, scroll))
        self.editor.pack(side="left", fill="both", expand=True); scroll.config(command=self._scroll_editor)
        self._configure_tags()
        bottom = ttk.Notebook(self); bottom.pack(fill="x", padx=8, pady=(0, 8))
        console_frame, problems_frame, debug_frame, trace_frame = ttk.Frame(bottom), ttk.Frame(bottom), ttk.Frame(bottom), ttk.Frame(bottom)
        bottom.add(console_frame, text=" Output "); bottom.add(problems_frame, text=" Problems "); bottom.add(debug_frame, text=" Debug "); bottom.add(trace_frame, text=" Trace ")
        self.console = tk.Text(console_frame, height=8, background="#070b16", foreground="#d1fae5", insertbackground="#5eead4", font=("Consolas", 11), state="disabled", borderwidth=0, padx=10, pady=8)
        self.console.pack(fill="both", expand=True)
        terminal_input = ttk.Frame(console_frame, padding=(7, 5))
        terminal_input.pack(fill="x")
        self.terminal_prompt = ttk.Label(terminal_input, text="❯", foreground="#5eead4", font=("Consolas", 12))
        self.terminal_prompt.pack(side="left", padx=(0, 6))
        self.terminal_entry = ttk.Entry(terminal_input, font=("Consolas", 11), state="disabled")
        self.terminal_entry.pack(side="left", fill="x", expand=True)
        self.terminal_entry.bind("<Return>", self.submit_terminal_input)
        self.terminal_send = ttk.Button(terminal_input, text="Send", command=self.submit_terminal_input, state="disabled", style="Accent.TButton")
        self.terminal_send.pack(side="left", padx=(6, 0))
        self.problems = tk.Listbox(problems_frame, height=9, background="#0b1020", foreground="#fca5a5", font=("Consolas", 11), borderwidth=0)
        self.problems.pack(fill="both", expand=True); self.problems.bind("<Double-1>", self.go_to_problem)
        debug_toolbar = ttk.Frame(debug_frame, padding=(7, 5)); debug_toolbar.pack(fill="x")
        self.step_btn = ttk.Button(debug_toolbar, text="Step ⏵", command=self.debug_step, state="disabled"); self.step_btn.pack(side="left", padx=(0, 5))
        self.continue_btn = ttk.Button(debug_toolbar, text="Continue ⏭", command=self.debug_continue, state="disabled"); self.continue_btn.pack(side="left", padx=(0, 5))
        self.stop_btn = ttk.Button(debug_toolbar, text="Stop ⏹", command=self.debug_stop, state="disabled"); self.stop_btn.pack(side="left")
        ttk.Label(debug_toolbar, text="Click a line number to toggle a breakpoint.").pack(side="left", padx=(14, 0))
        debug_panes = ttk.Frame(debug_frame, padding=(7, 0)); debug_panes.pack(fill="both", expand=True)
        call_col = ttk.Frame(debug_panes); call_col.pack(side="left", fill="both", padx=(0, 10))
        ttk.Label(call_col, text="CALL STACK", style="Section.TLabel").pack(anchor="w")
        self.callstack_list = tk.Listbox(call_col, width=22, height=8, background="#0b1020", foreground="#e5efff", font=("Consolas", 10), borderwidth=0)
        self.callstack_list.pack(fill="both", expand=True)
        vars_col = ttk.Frame(debug_panes); vars_col.pack(side="left", fill="both", expand=True, padx=(0, 10))
        ttk.Label(vars_col, text="VARIABLES", style="Section.TLabel").pack(anchor="w")
        self.vars_list = tk.Listbox(vars_col, height=8, background="#0b1020", foreground="#e5efff", font=("Consolas", 10), borderwidth=0)
        self.vars_list.pack(fill="both", expand=True)
        watch_col = ttk.Frame(debug_panes); watch_col.pack(side="left", fill="both", expand=True)
        ttk.Label(watch_col, text="WATCH", style="Section.TLabel").pack(anchor="w")
        watch_entry_row = ttk.Frame(watch_col); watch_entry_row.pack(fill="x", pady=(0, 4))
        self.watch_entry = ttk.Entry(watch_entry_row, font=("Consolas", 10))
        self.watch_entry.pack(side="left", fill="x", expand=True)
        self.watch_entry.bind("<Return>", lambda e: self.add_watch())
        ttk.Button(watch_entry_row, text="Add", command=self.add_watch).pack(side="left", padx=(5, 0))
        ttk.Label(watch_col, text="Double-click a watch to remove it.", font=("Segoe UI", 8)).pack(anchor="w")
        self.watch_list = tk.Listbox(watch_col, height=6, background="#0b1020", foreground="#fbbf24", font=("Consolas", 10), borderwidth=0)
        self.watch_list.pack(fill="both", expand=True)
        self.watch_list.bind("<Double-1>", self.remove_watch)
        self.trace_list = tk.Listbox(trace_frame, background="#0b1020", foreground="#94a3b8", font=("Consolas", 10), borderwidth=0)
        self.trace_list.pack(fill="both", expand=True, padx=7, pady=7)
        self.status = ttk.Label(self, anchor="w", padding=(12, 6), background="#121a2d", foreground="#7dd3fc"); self.status.pack(fill="x")
        self.editor.bind("<<Modified>>", self.on_modified); self.editor.bind("<KeyRelease>", self.on_key_release)
        self.editor.bind("<Control-space>", self.show_suggestions); self.editor.bind("<Control-s>", lambda e: self.save_file())
        self.editor.bind("<Control-o>", lambda e: self.open_file()); self.editor.bind("<Control-n>", lambda e: self.new_file())
        self.editor.bind("<F5>", lambda e: self.run_program()); self.editor.bind("<Control-r>", lambda e: self.rename_symbol())
        self.editor.bind("<F6>", lambda e: self.debug_program())

    def _make_menu(self):
        menu = tk.Menu(self); file_menu = tk.Menu(menu, tearoff=False)
        for label, callback, key in (("New", self.new_file, "Ctrl+N"), ("Open…", self.open_file, "Ctrl+O"), ("Save", self.save_file, "Ctrl+S")):
            file_menu.add_command(label=label, command=callback, accelerator=key)
        menu.add_cascade(label="File", menu=file_menu)
        tools = tk.Menu(menu, tearoff=False)
        for label, callback, key in (("Run", self.run_program, "F5"), ("Debug", self.debug_program, "F6"), ("Check syntax", self.check_code, ""), ("Rename symbol", self.rename_symbol, "Ctrl+R"), ("Insert template", self.show_templates, "Ctrl+Space")):
            tools.add_command(label=label, command=callback, accelerator=key)
        menu.add_cascade(label="Tools", menu=tools); self.config(menu=menu)

    def _configure_tags(self):
        for name, color in {"keyword":"#c084fc", "type":"#67e8f9", "string":"#86efac", "number":"#fbbf24", "comment":"#94a3b8", "function":"#60a5fa", "error":"#fb7185"}.items(): self.editor.tag_configure(name, foreground=color)
        self.editor.tag_configure("error", underline=True)
        self.editor.tag_configure("current_line", background="#1e3a5f")

    def change_font(self):
        size = self.font_size.get(); self.editor.config(font=("Consolas", size)); self.line_numbers.config(font=("Consolas", size))

    def _scroll_both(self, first, last, scrollbar): scrollbar.set(first, last); self.line_numbers.yview_moveto(first)
    def _scroll_editor(self, *args): self.editor.yview(*args); self.line_numbers.yview(*args)
    def _gutter_scroll(self, event):
        self.editor.yview_scroll(-1 * (event.delta // 120), "units")
        return "break"
    def source(self): return self.editor.get("1.0", "end-1c")

    def on_modified(self, _event=None):
        if self.editor.edit_modified(): self.dirty = True; self.editor.edit_modified(False); self._schedule_refresh()
    def on_key_release(self, _event=None): self._schedule_refresh()
    def _schedule_refresh(self):
        if self.refresh_id: self.after_cancel(self.refresh_id)
        self.refresh_id = self.after(180, self._refresh_all)
    def _refresh_all(self):
        self.refresh_id = None; self.highlight(); self.refresh_line_numbers(); self.refresh_outline()
        line, col = self.editor.index("insert").split("."); filename = self.file_path.name if self.file_path else "Untitled.src"
        self.status.config(text=f"{filename}  |  Ln {line}, Col {int(col)+1}  |  Ctrl+Space: suggestions")

    def highlight(self):
        src = self.source()
        for tag in ("keyword", "type", "string", "number", "comment", "function", "error"): self.editor.tag_remove(tag, "1.0", "end")
        tokens, errors = Scanner(src).scan_all()
        for tok in tokens:
            if tok.ttype in (TT.EOF, TT.ERROR): continue
            start, end = f"{tok.line}.{tok.col-1}", f"{tok.line}.{tok.col-1+len(tok.lexeme)}"
            tag = "type" if tok.ttype == TT.KEYWORD and tok.lexeme in {"int","float","char","string","bool","void"} else "keyword" if tok.ttype == TT.KEYWORD else "string" if tok.ttype in (TT.STRING_LIT,TT.CHAR_LIT,TT.INTERP_STRING) else "number" if tok.ttype in (TT.INTEGER_LIT,TT.FLOAT_LIT,TT.BOOL_LIT) else None
            if tag: self.editor.tag_add(tag, start, end)
        for match in re.finditer(r"//[^\n]*|/\*[\s\S]*?\*/", src): self.editor.tag_add("comment", f"1.0+{match.start()}c", f"1.0+{match.end()}c")
        for match in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", src): self.editor.tag_add("function", f"1.0+{match.start(1)}c", f"1.0+{match.end(1)}c")
        for error in errors: self.editor.tag_add("error", f"{error.line}.{error.col-1}", f"{error.line}.{error.col}")

    def refresh_line_numbers(self):
        count = max(1, int(self.editor.index("end-1c").split(".")[0])); self.line_numbers.config(state="normal"); self.line_numbers.delete("1.0", "end"); self.line_numbers.insert("1.0", "\n".join(str(i) for i in range(1, count+1)))
        for line in self.breakpoints:
            if line <= count: self.line_numbers.tag_add("breakpoint", f"{line}.0", f"{line}.end")
        self.line_numbers.config(state="disabled")
        self.line_numbers.yview_moveto(self.editor.yview()[0])
    def toggle_breakpoint(self, event):
        line = int(self.line_numbers.index(f"@{event.x},{event.y}").split(".")[0])
        self.breakpoints.symmetric_difference_update({line})
        self.refresh_line_numbers()
    def refresh_outline(self):
        self.outline.delete(*self.outline.get_children()); src = self.source()
        pattern = re.compile(r"^\s*(?:struct\s+(\w+)|typedef\s+.+?\s+(\w+)\s*;|(?:int|float|char|string|bool|void)\s+(\w+)\s*\()", re.M)
        for match in pattern.finditer(src):
            name = next(item for item in match.groups() if item); kind = "struct" if match.group(1) else "typedef" if match.group(2) else "function"; line = src[:match.start()].count("\n")+1
            self.outline.insert("", "end", text=f"{kind}  {name}", values=(line,))
    def go_to_outline(self, _event=None):
        selected = self.outline.selection()
        if selected: self.editor.mark_set("insert", f"{self.outline.item(selected[0], 'values')[0]}.0"); self.editor.see("insert"); self.editor.focus_set()

    def show_suggestions(self, _event=None): self.show_templates(); return "break"
    def show_templates(self):
        win = tk.Toplevel(self); win.title("Templates & autocomplete"); win.transient(self); win.geometry("420x390"); win.resizable(False, False); win.configure(bg="#0b1020")
        win.grab_set()
        header = tk.Frame(win, bg="#121a2d", height=62); header.pack(fill="x"); header.pack_propagate(False)
        tk.Label(header, text="INSERT A CODE TEMPLATE", bg="#121a2d", fg="#7dd3fc", font=("Segoe UI Semibold", 11)).pack(anchor="w", padx=16, pady=(11, 0))
        tk.Label(header, text="Templates and language keywords", bg="#121a2d", fg="#94a3b8", font=("Segoe UI", 9)).pack(anchor="w", padx=16)
        body = tk.Frame(win, bg="#0b1020"); body.pack(fill="both", expand=True, padx=12, pady=12)
        box = tk.Listbox(body, font=("Consolas", 11), height=13, bg="#111827", fg="#dbeafe", selectbackground="#155e75", selectforeground="#ecfeff", highlightthickness=1, highlightbackground="#334155", highlightcolor="#14b8a6", borderwidth=0, activestyle="none")
        for item in list(TEMPLATES)+sorted(KEYWORDS): box.insert("end", item)
        box.pack(fill="both", expand=True); box.selection_set(0)
        def insert(_event=None): self.insert_template(box.get("active")); win.destroy()
        actions = tk.Frame(win, bg="#0b1020"); actions.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(actions, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(actions, text="Insert", command=insert, style="Accent.TButton").pack(side="right", padx=(0, 8))
        box.bind("<Double-1>", insert); box.bind("<Return>", insert); win.bind("<Escape>", lambda e: win.destroy()); box.focus_set()

    def themed_prompt(self, title, message, initial=""):
        """A small themed replacement for Tk's platform-coloured askstring."""
        result = {"value": None}
        win = tk.Toplevel(self); win.title(title); win.transient(self); win.configure(bg="#0b1020"); win.resizable(False, False); win.grab_set()
        header = tk.Frame(win, bg="#121a2d", height=52); header.pack(fill="x"); header.pack_propagate(False)
        tk.Label(header, text=title.upper(), bg="#121a2d", fg="#7dd3fc", font=("Segoe UI Semibold", 11)).pack(anchor="w", padx=16, pady=15)
        body = tk.Frame(win, bg="#0b1020"); body.pack(fill="both", expand=True, padx=16, pady=16)
        tk.Label(body, text=message, bg="#0b1020", fg="#cbd5e1", font=("Segoe UI", 10), wraplength=360, justify="left").pack(anchor="w", pady=(0, 9))
        entry = tk.Entry(body, width=42, bg="#18243b", fg="#e5fff7", insertbackground="#5eead4", relief="flat", highlightthickness=1, highlightbackground="#334155", highlightcolor="#14b8a6", font=("Consolas", 11))
        entry.insert(0, initial); entry.pack(fill="x", ipady=6)
        actions = tk.Frame(body, bg="#0b1020"); actions.pack(fill="x", pady=(15, 0))
        def accept(_event=None): result["value"] = entry.get(); win.destroy()
        ttk.Button(actions, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(actions, text="OK", command=accept, style="Accent.TButton").pack(side="right", padx=(0, 8))
        entry.bind("<Return>", accept); win.bind("<Escape>", lambda e: win.destroy()); entry.focus_set(); entry.selection_range(0, "end")
        self.wait_window(win)
        return result["value"]
    def insert_template(self, name):
        content = TEMPLATES.get(name, name); values = {"cursor":""}
        for placeholder in dict.fromkeys(re.findall(r"\$\{(\w+)\}", content)):
            if placeholder != "cursor": values[placeholder] = self.themed_prompt("Template value", f"Enter a value for {placeholder}:") or placeholder
        self.editor.insert("insert", re.sub(r"\$\{(\w+)\}", lambda m: values.get(m.group(1), ""), content)); self._refresh_all()

    def rename_symbol(self):
        old = self.editor.get("insert wordstart", "insert wordend")
        if not old or old in KEYWORDS: messagebox.showinfo("Rename symbol", "Place the cursor on an identifier first.", parent=self); return
        new = self.themed_prompt("Rename symbol", f"Rename '{old}' to:", old)
        if new is None or new == old: return
        if not re.fullmatch(r"[A-Za-z_]\w*", new) or new in KEYWORDS: messagebox.showerror("Rename symbol", "Enter a valid non-keyword identifier.", parent=self); return
        source = self.source(); count = len(re.findall(rf"\b{re.escape(old)}\b", source)); self.editor.delete("1.0", "end"); self.editor.insert("1.0", re.sub(rf"\b{re.escape(old)}\b", new, source)); self.write_console(f"Refactoring complete: renamed {count} occurrence(s) of '{old}' to '{new}'.\n"); self._refresh_all()

    def check_code(self):
        _, syntax_errors, lex_errors = parse_source(self.source()); errors = list(lex_errors)+list(syntax_errors); self.problems.delete(0,"end")
        if not errors: self.problems.insert("end", "✓ No lexical or syntax errors found."); self.write_console("Check complete: no problems found.\n")
        else:
            for error in errors: self.problems.insert("end", str(error))
            self.write_console(f"Check complete: {len(errors)} problem(s) found.\n")
        return not errors
    def go_to_problem(self, _event=None):
        match = re.search(r"Line (\d+)", self.problems.get("active"))
        if match: self.editor.mark_set("insert", f"{match.group(1)}.0"); self.editor.see("insert"); self.editor.focus_set()
    def run_program(self, debug=False):
        if not self.check_code(): return
        from ir import generate
        from interpreter import IRExecutor
        from semantics import analyze
        ast, _, _ = parse_source(self.source())
        symtab, semantic_errors = analyze(ast)
        if any(error.severity == "ERROR" for error in semantic_errors):
            self.write_console("Semantic errors:\n" + "\n".join(str(error) for error in semantic_errors) + "\n")
            return
        self.console.config(state="normal"); self.console.delete("1.0", "end"); self.console.config(state="disabled")
        self.clear_current_line(); self.callstack_list.delete(0, "end"); self.vars_list.delete(0, "end"); self.trace_list.delete(0, "end")
        self.write_console("$ Debugging program…\n" if debug else "$ Running program…\n")
        if debug: self.stop_btn.config(state="normal")
        def request_input(names):
            request = {"names": names, "value": None, "event": threading.Event()}
            self.pending_input = request
            self.after(0, self.activate_terminal_input, request)
            request["event"].wait()
            return request["value"]
        def on_pause(line):
            self.after(0, self.on_debug_pause, line)
        def on_line(line):
            self.after(0, self.append_trace, line)
        class TerminalWriter:
            def __init__(self, ide): self.ide = ide
            def write(self, text):
                if text: self.ide.after(0, self.ide.write_console, text)
                return len(text)
            def flush(self): pass
        def execute():
            try:
                with contextlib.redirect_stdout(TerminalWriter(self)):
                    quads, functions, types, structs = generate(ast, symtab)
                    self.executor = IRExecutor(quads, functions, types, structs, input_provider=request_input,
                                                breakpoints=self.breakpoints if debug else None,
                                                on_pause=on_pause if debug else None,
                                                on_line=on_line if debug else None)
                    self.executor.run()
                self.after(0, self.finish_execution, "\n$ Program finished.\n")
            except Exception as exc:
                self.after(0, self.finish_execution, f"\nRuntime error: {exc}\n")
        threading.Thread(target=execute, daemon=True).start()

    def debug_program(self): self.run_program(debug=True)

    def on_debug_pause(self, line):
        self.highlight_current_line(line)
        self.refresh_debug_panels()
        self.step_btn.config(state="normal"); self.continue_btn.config(state="normal")
        self.status.config(text=f"Paused at line {line}")

    def append_trace(self, line):
        executor = self.executor
        if executor is None: return
        function = executor.call_names[-1] if executor.call_names else "(top level)"
        self.trace_list.insert("end", f"{function}  —  line {line}")
        self.trace_list.see("end")
        overflow = self.trace_list.size() - 500  # cap the log so a long-running loop can't grow it unbounded
        if overflow > 0: self.trace_list.delete(0, overflow - 1)

    def refresh_debug_panels(self):
        executor = self.executor
        self.callstack_list.delete(0, "end")
        for name in reversed(executor.call_names) or ["(top level)"]:
            self.callstack_list.insert("end", name)
        self.vars_list.delete(0, "end")
        self.vars_list.insert("end", "-- Locals --")
        for name, value in executor.frame.items():
            if not name.startswith("_t"): self.vars_list.insert("end", f"{self._display_name(name)} = {value!r}")
        if executor.frame is not executor.globals:
            self.vars_list.insert("end", "-- Globals --")
            for name, value in executor.globals.items():
                if not name.startswith("_t"): self.vars_list.insert("end", f"{self._display_name(name)} = {value!r}")
        self.refresh_watches()

    def add_watch(self):
        name = self.watch_entry.get().strip()
        if name and name not in self.watches:
            self.watches.append(name)
            self.watch_entry.delete(0, "end")
            self.refresh_watches()
    def remove_watch(self, _event=None):
        selected = self.watch_list.curselection()
        if selected:
            del self.watches[selected[0]]
            self.refresh_watches()
    def refresh_watches(self):
        self.watch_list.delete(0, "end")
        for name in self.watches:
            value, found = self._lookup_watch(name)
            self.watch_list.insert("end", f"{name} = {value!r}" if found else f"{name} = <not in scope>")
    def _lookup_watch(self, name):
        executor = self.executor
        if executor is None: return None, False
        for scope in (executor.frame, executor.globals):
            for ir_name, value in scope.items():
                if not ir_name.startswith("_t") and self._display_name(ir_name) == name:
                    return value, True
        return None, False

    @staticmethod
    def _display_name(ir_name):
        # Hides the function-qualified IR name (e.g. "main.i") in favor of
        # the surface-source name the user actually wrote.
        return ir_name.split(".", 1)[1] if "." in ir_name else ir_name

    def highlight_current_line(self, line):
        self.editor.tag_remove("current_line", "1.0", "end")
        self.editor.tag_add("current_line", f"{line}.0", f"{line}.end+1c")
        self.editor.see(f"{line}.0")
    def clear_current_line(self): self.editor.tag_remove("current_line", "1.0", "end")

    def debug_step(self):
        if not self.executor: return
        self.step_btn.config(state="disabled"); self.continue_btn.config(state="disabled")
        self.executor.dbg_step()
    def debug_continue(self):
        if not self.executor: return
        self.clear_current_line()
        self.step_btn.config(state="disabled"); self.continue_btn.config(state="disabled")
        self.executor.dbg_continue()
    def debug_stop(self):
        if self.executor: self.executor.dbg_stop()

    def activate_terminal_input(self, request):
        # The terminal cursor is sufficient context; avoid exposing internal
        # IR-qualified variable names such as `main.a` to the user.
        self.terminal_entry.config(state="normal")
        self.terminal_send.config(state="normal")
        self.terminal_entry.delete(0, "end")
        self.terminal_entry.focus_set()

    def submit_terminal_input(self, _event=None):
        if not self.pending_input: return "break"
        value = self.terminal_entry.get()
        self.write_console(value + "\n")
        self.terminal_entry.delete(0, "end")
        self.terminal_entry.config(state="disabled")
        self.terminal_send.config(state="disabled")
        request, self.pending_input = self.pending_input, None
        request["value"] = value
        request["event"].set()
        return "break"

    def finish_execution(self, message):
        self.write_console(message)
        self.terminal_entry.config(state="disabled")
        self.terminal_send.config(state="disabled")
        self.pending_input = None
        self.clear_current_line()
        self.step_btn.config(state="disabled"); self.continue_btn.config(state="disabled"); self.stop_btn.config(state="disabled")
        self.executor = None
        self.refresh_watches()
    def write_console(self, message): self.console.config(state="normal"); self.console.insert("end", message); self.console.see("end"); self.console.config(state="disabled")
    def new_file(self):
        if self.dirty and not messagebox.askyesno("New file", "Discard unsaved changes?", parent=self): return
        self.editor.delete("1.0", "end"); self.editor.insert("1.0", NEW_FILE)
        self.editor.mark_set("insert", "2.4")
        self.file_path=None; self.dirty=False; self._refresh_all()
    def open_file(self):
        path = filedialog.askopenfilename(filetypes=[("Cetirim source", "*.src"), ("All files", "*.*")])
        if path: self.editor.delete("1.0", "end"); self.editor.insert("1.0", Path(path).read_text(encoding="utf-8")); self.file_path=Path(path); self.dirty=False; self._refresh_all()
    def save_file(self):
        path = self.file_path or filedialog.asksaveasfilename(defaultextension=".src", filetypes=[("Cetirim source", "*.src")])
        if path: Path(path).write_text(self.source(), encoding="utf-8"); self.file_path=Path(path); self.dirty=False; self._refresh_all()

if __name__ == "__main__": CetirimIDE().mainloop()
