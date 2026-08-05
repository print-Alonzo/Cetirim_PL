"""A standalone IDE for the CSC617M custom language. Run: python cetirim_ide.py"""
from __future__ import annotations

import contextlib
import re
import threading
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ast_nodes import Node
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

# Every diagnostic in the pipeline prints as "[TAG] Line N, Col C: message"
# (the position is omitted for a whole-program semantic error), so one regex
# splits any of them into the Problems table's three columns.
DIAGNOSTIC = re.compile(r"^\[(?P<tag>[A-Z ]+)\]\s*(?:Line (?P<line>\d+), Col (?P<col>\d+):\s*)?(?P<message>.*)$",
                        re.S)

# One place for every color in the IDE. All widget construction and every
# ttk style reads from this dict - no hex literal may appear anywhere else
# in the file. Three surface levels and a single accent, by design: the
# fewer distinct planes there are, the quieter the window reads.
THEME = {
    # surfaces
    "bg":               "#181b21",  # editor
    "bg_dark":          "#14161b",  # chrome: header, sidebar, panel, status bar, dialogs
    "bg_raise":         "#1f232b",  # inputs, buttons at rest, find bar
    "bg_hover":         "#242933",
    "bg_active":        "#2b3140",  # pressed buttons / selected rows / scrollbar thumbs
    "border":           "#262a33",  # every 1px divider in the window
    # text
    "fg":               "#c9cfda",
    "fg_code":          "#d5dbe6",
    "fg_bright":        "#eef1f6",
    "fg_dim":           "#8b93a5",
    "fg_faint":         "#5d6577",
    # accent
    "accent":           "#6c9ef8",
    "accent_hover":     "#83aefc",
    "accent_press":     "#5486e0",
    "select_bg":        "#2b3b5a",
    "cursor":           "#d5dbe6",
    "cursor_line":      "#1d212a",  # the line the caret is on
    "current_line":     "#2a3040",  # the line the debugger is paused on
    # gutter / breakpoints
    "gutter_fg":        "#4d5565",
    "gutter_fg_active": "#8b93a5",
    "breakpoint":       "#e06c75",
    # syntax
    "syn_keyword":      "#c678dd",
    "syn_type":         "#61afef",
    "syn_string":       "#98c379",
    "syn_number":       "#d19a66",
    "syn_comment":      "#5c6370",
    "syn_function":     "#e5c07b",
    "syn_error":        "#e06c75",
    # panels
    "ok":               "#98c379",
    "warn":             "#e5c07b",
    "error":            "#e06c75",
    "trace_fg":         "#7aa5f0",
    "watch_fg":         "#e5c07b",
    "ir_before_fg":     "#9aa4b5",
    "ir_removed":       "#e06c75",
    "ir_rewritten":     "#e5c07b",
    "ir_after_fg":      "#98c379",
    # find bar
    "find_match":       "#4a3f1e",
    "find_current":     "#8a6a24",
}


def hairline(parent, side="top", **pack_options):
    """A 1px divider in the border color. ttk's own borders don't render
    reliably flat under clam, so every divider in this UI is one of these."""
    horizontal = side in ("top", "bottom")
    bar = tk.Frame(parent, background=THEME["border"],
                   height=1 if horizontal else 0, width=0 if horizontal else 1)
    bar.pack(side=side, fill="x" if horizontal else "y", **pack_options)
    return bar


def collect_locals(node, out):
    """Walk a `FunctionDecl` and append `(line, name)` for every name local
    to it, in source order.

    The walk is generic - it recurses through every Node/list field rather
    than enumerating the statement kinds that can hold a block - so a
    declaration nested inside an `if`/`for`/`while`/`try` body is found
    without this needing to know the statement vocabulary, and a new
    block-bearing statement in grammar.py needs no change here.

    Six binding forms, each keyed off the node kind that introduces it:
    `Param` (a parameter is as local to the function as anything it
    declares), `VarDecl` (`var`/`val`, one entry per declarator, each with
    its own line), `LetDecl` (`None` names are `_` discards, which bind
    nothing), `MultiAssign` (typed multi-assign declares ordinary locals
    too, and discards the same way), and the loop/catch variables of
    `ForInStmt`/`CatchClause`.
    """
    if isinstance(node, list):
        for item in node:
            collect_locals(item, out)
        return
    if not isinstance(node, Node):
        return

    if node.kind == "Param":
        out.append((node.line, node.fields["name"]))
    elif node.kind == "VarDecl":
        for d in node.fields["declarators"]:
            out.append((d.line or node.line, d.fields["name"]))
    elif node.kind == "LetDecl":
        for name in node.fields["names"]:
            if name is not None:
                out.append((node.line, name))
    elif node.kind == "MultiAssign":
        for lvalue in node.fields["lvalues"]:
            if lvalue["name"] is not None:
                out.append((node.line, lvalue["name"]))
    elif node.kind in ("ForInStmt", "CatchClause"):
        out.append((node.line, node.fields["name"]))

    for value in node.fields.values():
        collect_locals(value, out)


class PanelTabs(ttk.Frame):
    """A flat tab strip over a content area, in place of `ttk.Notebook`.

    clam has no per-tab element that can carry an underline, and its Tab
    map repaints a bevel on selection no matter how the style is pinned -
    drawing the strip by hand is what buys the accent-underlined tabs and
    keeps the strip on the same surface as the panel below it.

    Mirrors the slice of the Notebook API the IDE actually uses: `add`,
    `select` (by frame, path name or index), and a `<<TabChanged>>` virtual
    event. `badge()` adds the live problem count to a tab's label.
    """

    def __init__(self, master):
        super().__init__(master, style="Panel.TFrame")
        self._strip = ttk.Frame(self, style="Panel.TFrame")
        self._strip.pack(fill="x")
        hairline(self)
        self._holder = ttk.Frame(self, style="Panel.TFrame")
        self._holder.pack(fill="both", expand=True)
        self._tabs = []
        self._current = None

    def new_frame(self):
        return ttk.Frame(self._holder, style="Panel.TFrame")

    def add(self, frame, text):
        tab = ttk.Frame(self._strip, style="Panel.TFrame")
        tab.pack(side="left")
        label = ttk.Label(tab, text=text, style="Tab.TLabel")
        label.pack(padx=12, pady=(8, 6))
        underline = tk.Frame(tab, background=THEME["bg_dark"], height=2)
        underline.pack(fill="x")
        entry = {"frame": frame, "tab": tab, "label": label, "underline": underline, "text": text}
        self._tabs.append(entry)
        for widget in (tab, label):
            widget.bind("<Button-1>", lambda _e, f=frame: self.select(f))
            widget.bind("<Enter>", lambda _e, x=entry: self._hover(x, True))
            widget.bind("<Leave>", lambda _e, x=entry: self._hover(x, False))
        if self._current is None:
            self.select(frame)

    def badge(self, frame, text=""):
        entry = self._entry(frame)
        entry["label"].config(text=f"{entry['text']}  {text}" if text else entry["text"])

    def select(self, target=None):
        if target is None:
            return str(self._current["frame"]) if self._current else ""
        entry = self._entry(target)
        if entry is self._current:
            return None
        if self._current is not None:
            self._current["frame"].pack_forget()
        entry["frame"].pack(fill="both", expand=True)
        self._current = entry
        for other in self._tabs:
            selected = other is entry
            other["label"].config(style="TabOn.TLabel" if selected else "Tab.TLabel")
            other["underline"].config(background=THEME["accent"] if selected else THEME["bg_dark"])
        self.event_generate("<<TabChanged>>")
        return None

    def _entry(self, target):
        if isinstance(target, int):
            return self._tabs[target]
        name = str(target)
        for entry in self._tabs:
            if str(entry["frame"]) == name:
                return entry
        raise KeyError(name)

    def _hover(self, entry, entering):
        if entry is not self._current:
            entry["label"].config(style="TabHover.TLabel" if entering else "Tab.TLabel")


class CetirimIDE(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Cetirim IDE — Custom Language Environment")
        # Fit small screens and position near the top so the status bar never
        # ends up under the macOS Dock.
        width = min(1280, self.winfo_screenwidth() - 80)
        height = min(800, self.winfo_screenheight() - 130)
        self.geometry(f"{width}x{height}+40+35")
        self.minsize(900, 580)
        self.file_path = None
        self.dirty = False
        self.refresh_id = None
        self.pending_input = None
        self.breakpoints = set()
        self.executor = None
        self.run_serial = 0  # bumped per run; stale worker threads compare against it and go inert
        self.watches = []
        self._last_symtab = None
        self._symbols_src = None   # source snapshot the symbol table was built from
        self._symbol_lines = {}    # symbols-tree item id -> declaration line
        self._problem_lines = {}   # problems-tree item id -> diagnostic line
        self._checked_src = None   # source snapshot the problem counts describe
        self._diag_counts = (0, 0)
        self._outline_src = None   # source snapshot the outline tree was parsed from
        self._outline_path = None  # and the file it came from, so a new file always rebuilds
        self._outline_count = 0    # top-level rows in it, to spot a mid-edit shrink
        self._outline_seen = {}    # outline item id -> times used, for the #n suffix
        self._find_matches = []
        self._find_pos = -1
        self._find_term = None
        self.is_aqua = self.tk.call("tk", "windowingsystem") == "aqua"
        self._pick_fonts()
        self._apply_theme()
        self._build_ui()
        self.editor.insert("1.0", SAMPLE)
        self.editor.edit_modified(False)  # the queued <<Modified>> must not mark the fresh buffer dirty
        self._refresh_all()

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------

    def _pick_fonts(self):
        """Shared named Font objects, resolved per platform. Widgets hold the
        objects (not tuples), so one configure() call resizes all of them."""
        families = set(tkfont.families(self))

        def first(candidates, fallback_named):
            for name in candidates:
                if name in families:
                    return name
            return tkfont.nametofont(fallback_named).actual("family")

        mono = first(["SF Mono", "Menlo", "Cascadia Code", "Consolas", "DejaVu Sans Mono"], "TkFixedFont")
        ui = first(["SF Pro Text", "Helvetica Neue", "Segoe UI", "DejaVu Sans"], "TkDefaultFont")
        self.font_editor = tkfont.Font(family=mono, size=13)
        self.font_mono = tkfont.Font(family=mono, size=12)
        self.font_mono_sm = tkfont.Font(family=mono, size=11)
        self.font_ui = tkfont.Font(family=ui, size=12)
        self.font_ui_sm = tkfont.Font(family=ui, size=11)
        self.font_ui_bold = tkfont.Font(family=ui, size=12, weight="bold")
        self.font_section = tkfont.Font(family=ui, size=11)

    def _apply_theme(self):
        T = THEME
        self.configure(background=T["bg_dark"])
        style = ttk.Style(self)
        style.theme_use("clam")  # the only built-in theme whose colors fully obey configure()
        # Setting light/dark/border colors at the root style is what kills
        # clam's 3D bevels everywhere; focuscolor=bg removes the dotted ring.
        style.configure(".", background=T["bg"], foreground=T["fg"], font=self.font_ui,
                        bordercolor=T["border"], lightcolor=T["bg"], darkcolor=T["bg"],
                        focuscolor=T["bg"], troughcolor=T["bg_dark"],
                        selectbackground=T["select_bg"], selectforeground=T["fg_bright"])
        # Surfaces: editor level, chrome level, one raised level for inputs.
        style.configure("TFrame", background=T["bg"])
        style.configure("Panel.TFrame", background=T["bg_dark"])
        style.configure("Raise.TFrame", background=T["bg_raise"])
        # Labels
        style.configure("TLabel", background=T["bg"], foreground=T["fg"])
        style.configure("Panel.TLabel", background=T["bg_dark"], foreground=T["fg"], font=self.font_ui_sm)
        style.configure("Wordmark.TLabel", background=T["bg_dark"], foreground=T["fg_dim"], font=self.font_ui_bold)
        style.configure("Section.TLabel", background=T["bg_dark"], foreground=T["fg_dim"], font=self.font_section)
        style.configure("Hint.TLabel", background=T["bg_dark"], foreground=T["fg_faint"], font=self.font_ui_sm)
        style.configure("Status.TLabel", background=T["bg_dark"], foreground=T["fg_dim"],
                        font=self.font_ui_sm, padding=(10, 4))
        style.configure("Prompt.TLabel", background=T["bg_dark"], foreground=T["accent"], font=self.font_mono)
        style.configure("Raise.TLabel", background=T["bg_raise"], foreground=T["fg_dim"], font=self.font_ui_sm)
        style.configure("EditorTab.TLabel", background=T["bg"], foreground=T["fg"],
                        font=self.font_ui_sm, padding=(14, 8))
        # Panel tab strip (see PanelTabs) - three states, label color only.
        for name, color in (("Tab", T["fg_dim"]), ("TabHover", T["fg"]), ("TabOn", T["fg_bright"])):
            style.configure(f"{name}.TLabel", background=T["bg_dark"], foreground=color, font=self.font_ui_sm)
        # Buttons. Ghost is the default in chrome; only Run is filled.
        # width=0 is load-bearing: clam ships TButton with `-width -11`, a
        # minimum of eleven characters, which pads every label out into a
        # sea of dead space.
        flat = dict(borderwidth=0, relief="flat", width=0)
        style.configure("TButton", background=T["bg_raise"], foreground=T["fg"], padding=(12, 6), **flat)
        style.map("TButton", background=[("active", T["bg_hover"]), ("pressed", T["bg_active"])],
                  foreground=[("disabled", T["fg_faint"])])
        style.configure("Ghost.TButton", background=T["bg_dark"], foreground=T["fg_dim"],
                        padding=(10, 5), font=self.font_ui_sm, **flat)
        style.map("Ghost.TButton",
                  background=[("disabled", T["bg_dark"]), ("pressed", T["bg_active"]), ("active", T["bg_hover"])],
                  foreground=[("disabled", T["fg_faint"]), ("active", T["fg_bright"])],
                  lightcolor=[("pressed", T["bg_active"]), ("active", T["bg_hover"])],
                  darkcolor=[("pressed", T["bg_active"]), ("active", T["bg_hover"])])
        style.configure("Accent.TButton", background=T["accent"], foreground=T["bg_dark"],
                        padding=(13, 5), font=self.font_ui_sm, **flat)
        style.map("Accent.TButton",
                  background=[("disabled", T["bg_raise"]), ("pressed", T["accent_press"]), ("active", T["accent_hover"])],
                  foreground=[("disabled", T["fg_faint"])],
                  lightcolor=[("pressed", T["accent_press"]), ("active", T["accent_hover"])],
                  darkcolor=[("pressed", T["accent_press"]), ("active", T["accent_hover"])])
        style.configure("Find.TButton", background=T["bg_raise"], foreground=T["fg_dim"], padding=(7, 3),
                        font=self.font_ui_sm, **flat)
        style.map("Find.TButton", background=[("active", T["bg_hover"]), ("pressed", T["bg_active"])],
                  foreground=[("active", T["fg_bright"])])
        # Inputs
        style.configure("TEntry", fieldbackground=T["bg_raise"], foreground=T["fg"], insertcolor=T["fg_bright"],
                        bordercolor=T["border"], lightcolor=T["border"], darkcolor=T["border"], padding=6)
        style.map("TEntry", bordercolor=[("focus", T["accent"])], lightcolor=[("focus", T["accent"])],
                  darkcolor=[("focus", T["accent"])], fieldbackground=[("disabled", T["bg_dark"])],
                  foreground=[("disabled", T["fg_faint"])])
        # Trees. bordercolor has to be pinned to the surface too, or clam
        # draws a 1px box around every panel from the root style's border.
        style.configure("Treeview", background=T["bg_dark"], fieldbackground=T["bg_dark"], foreground=T["fg"],
                        borderwidth=0, bordercolor=T["bg_dark"], lightcolor=T["bg_dark"],
                        darkcolor=T["bg_dark"], rowheight=23, font=self.font_ui_sm)
        style.map("Treeview", background=[("selected", T["bg_active"])], foreground=[("selected", T["fg_bright"])])
        style.configure("Panel.Treeview", background=T["bg_dark"], fieldbackground=T["bg_dark"])
        style.configure("Treeview.Heading", background=T["bg_dark"], foreground=T["fg_faint"],
                        borderwidth=0, relief="flat", font=self.font_ui_sm, padding=(6, 5))
        style.map("Treeview.Heading", background=[("active", T["bg_hover"])])
        # A 5px sash in the border color reads as a divider you can grab.
        style.configure("TPanedwindow", background=T["border"])
        style.configure("Sash", sashthickness=5, gripcount=0)
        style.configure("TSeparator", background=T["border"])
        # Arrow-less flat scrollbars (thickness tracks arrowsize in clam), in
        # two trough colors so they disappear into whichever surface hosts
        # them. "Editor.Flat.X" inherits the arrow-less layout from "Flat.X"
        # through ttk's leading-component fallback - only the colors differ.
        for orient, sticky in (("Vertical", "ns"), ("Horizontal", "ew")):
            style.layout(f"Flat.{orient}.TScrollbar",
                         [(f"{orient}.Scrollbar.trough", {"sticky": sticky, "children":
                             [(f"{orient}.Scrollbar.thumb", {"expand": 1, "sticky": "nswe"})]})])
            for prefix, trough in (("Flat", T["bg_dark"]), ("Editor.Flat", T["bg"])):
                # The thumb rests barely above its trough so a panel whose
                # content already fits doesn't grow a bright bar down its side.
                style.configure(f"{prefix}.{orient}.TScrollbar", troughcolor=trough, background=T["border"],
                                bordercolor=trough, lightcolor=T["border"], darkcolor=T["border"],
                                gripcount=0, arrowsize=9, relief="flat")
                style.map(f"{prefix}.{orient}.TScrollbar", background=[("active", T["bg_active"])],
                          lightcolor=[("active", T["bg_active"])], darkcolor=[("active", T["bg_active"])])

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        self._make_menu()
        self._build_header()
        self._build_statusbar()  # packed side="bottom" before the body so shrinking never squeezes it out
        self._build_layout()
        self._build_sidebar()
        self._build_editor()
        self._build_panel()
        self._bind_shortcuts()

    def _build_header(self):
        """One slim bar: analysis actions on the left, execution on the
        right. File actions live in the File menu only - a toolbar button
        for Save earns nothing next to a Cmd-S every editor user knows."""
        header = ttk.Frame(self, style="Panel.TFrame", padding=(14, 6))
        header.pack(fill="x")
        left = ttk.Frame(header, style="Panel.TFrame")
        left.pack(side="left")
        right = ttk.Frame(header, style="Panel.TFrame")
        right.pack(side="right")
        ttk.Label(left, text="Cetirim", style="Wordmark.TLabel").pack(side="left", padx=(2, 14))
        self._header_divider(left)
        for text, callback in (("Check", self.check_code), ("Optimize", self.show_optimization)):
            ttk.Button(left, text=text, command=callback, style="Ghost.TButton").pack(side="left")
        # Transport stays visible but disabled while nothing is running, so
        # the debugger's controls are discoverable before a session starts.
        self.step_btn = ttk.Button(right, text="Step", command=self.debug_step,
                                   state="disabled", style="Ghost.TButton")
        self.step_btn.pack(side="left")
        self.continue_btn = ttk.Button(right, text="Continue", command=self.debug_continue,
                                       state="disabled", style="Ghost.TButton")
        self.continue_btn.pack(side="left")
        self.stop_btn = ttk.Button(right, text="Stop", command=self.debug_stop,
                                   state="disabled", style="Ghost.TButton")
        self.stop_btn.pack(side="left")
        self._header_divider(right)
        ttk.Button(right, text="Debug", command=self.debug_program, style="Ghost.TButton").pack(side="left")
        ttk.Button(right, text="▶  Run", command=self.run_program,
                   style="Accent.TButton").pack(side="left", padx=(6, 2))
        hairline(self)

    @staticmethod
    def _header_divider(parent):
        tk.Frame(parent, background=THEME["border"], width=1).pack(side="left", fill="y", padx=10, pady=4)

    def _build_statusbar(self):
        bar = ttk.Frame(self, style="Panel.TFrame")
        bar.pack(side="bottom", fill="x")
        hairline(bar)
        self.status = ttk.Label(bar, style="Status.TLabel", anchor="w", cursor="hand2")
        self.status.pack(side="left")
        self.status.bind("<Button-1>", lambda _e: self.bottom_tabs.select(self.problems_frame))
        self.status_run = ttk.Label(bar, style="Status.TLabel", anchor="w")
        self.status_run.pack(side="left")
        self.status_lang = ttk.Label(bar, text="Cetirim", style="Status.TLabel", anchor="e")
        self.status_lang.pack(side="right")
        self.status_pos = ttk.Label(bar, text="Ln 1, Col 1", style="Status.TLabel", anchor="e")
        self.status_pos.pack(side="right")

    def _build_layout(self):
        body = ttk.Frame(self, style="Panel.TFrame")
        body.pack(fill="both", expand=True)
        self.vsplit = ttk.PanedWindow(body, orient="vertical")
        self.vsplit.pack(fill="both", expand=True)
        self.hsplit = ttk.PanedWindow(self.vsplit, orient="horizontal")
        self._sidebar = ttk.Frame(self.hsplit, width=220, style="Panel.TFrame")
        self._editor_area = ttk.Frame(self.hsplit, style="TFrame")
        self.hsplit.add(self._sidebar, weight=0)
        self.hsplit.add(self._editor_area, weight=1)
        self._panel = ttk.Frame(self.vsplit, style="Panel.TFrame")
        self.vsplit.add(self.hsplit, weight=4)
        self.vsplit.add(self._panel, weight=1)
        # Initial sash placement must wait until the panes are actually
        # mapped - the smoke harness runs the whole IDE withdraw()n, where
        # sashpos() is unreliable, and <Map> never fires there.
        self.vsplit.bind("<Map>", self._init_sashes)

    def _init_sashes(self, _event=None):
        self.vsplit.unbind("<Map>")
        try:
            self.update_idletasks()
            height = self.vsplit.winfo_height()
            if height > 500:
                self.vsplit.sashpos(0, height - 280)
            self.hsplit.sashpos(0, 220)
        except tk.TclError:
            pass

    def toggle_sidebar(self):
        if str(self._sidebar) in self.hsplit.panes():
            self.hsplit.forget(self._sidebar)
        else:
            self.hsplit.insert(0, self._sidebar, weight=0)

    def toggle_panel(self):
        if str(self._panel) in self.vsplit.panes():
            self.vsplit.forget(self._panel)
        else:
            self.vsplit.add(self._panel, weight=1)

    def _build_sidebar(self):
        head = ttk.Frame(self._sidebar, style="Panel.TFrame", padding=(14, 10, 14, 8))
        head.pack(fill="x")
        ttk.Label(head, text="Outline", style="Section.TLabel").pack(side="left")
        wrap = ttk.Frame(self._sidebar, style="Panel.TFrame")
        wrap.pack(fill="both", expand=True)
        self.outline = ttk.Treeview(wrap, show="tree", selectmode="browse", style="Panel.Treeview")
        for kind, color in (("function", THEME["syn_function"]), ("struct", THEME["syn_type"]),
                            ("typedef", THEME["syn_keyword"]), ("const", THEME["syn_number"]),
                            ("field", THEME["fg_dim"]), ("variable", THEME["fg"])):
            self.outline.tag_configure(kind, foreground=color)
        self._scrolled(self.outline)
        self.outline.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.outline.bind("<<TreeviewSelect>>", self.go_to_outline)

    def _build_editor(self):
        T = THEME
        area = self._editor_area
        tabbar = ttk.Frame(area, style="Panel.TFrame")
        # The divider is packed first so it spans the whole strip; a
        # side="left" label packed before it would claim the full height and
        # push the line out from under itself.
        hairline(tabbar, side="bottom")
        self.editor_tab = ttk.Label(tabbar, style="EditorTab.TLabel")
        self.editor_tab.pack(side="left")
        self.line_numbers = tk.Text(area, width=6, height=1, padx=8, pady=10, takefocus=0, state="disabled",
                                    background=T["bg"], foreground=T["gutter_fg"], borderwidth=0,
                                    highlightthickness=0, font=self.font_editor, cursor="arrow",
                                    spacing1=1, spacing3=1)
        self.line_numbers.tag_configure("breakpoint", foreground=T["breakpoint"])
        self.line_numbers.tag_configure("active_ln", foreground=T["gutter_fg_active"])
        self.line_numbers.bind("<Button-1>", self.toggle_breakpoint)
        self.line_numbers.bind("<MouseWheel>", self._gutter_scroll)
        vscroll = ttk.Scrollbar(area, orient="vertical", style="Editor.Flat.Vertical.TScrollbar")
        self.editor = tk.Text(area, height=1, undo=True, wrap="none", borderwidth=0, highlightthickness=0,
                              padx=10, pady=10, background=T["bg"], foreground=T["fg_code"],
                              insertbackground=T["cursor"], insertwidth=2, selectbackground=T["select_bg"],
                              font=self.font_editor, spacing1=1, spacing3=1,
                              yscrollcommand=lambda a, b: self._scroll_both(a, b, vscroll))
        vscroll.config(command=self._scroll_editor)
        self.editor_hscroll = ttk.Scrollbar(area, orient="horizontal", style="Editor.Flat.Horizontal.TScrollbar",
                                            command=self.editor.xview)
        self.editor.config(xscrollcommand=self.editor_hscroll.set)
        tabbar.grid(row=0, column=0, columnspan=3, sticky="ew")
        self.line_numbers.grid(row=1, column=0, sticky="ns")
        self.editor.grid(row=1, column=1, sticky="nsew")
        vscroll.grid(row=1, column=2, sticky="ns")
        self.editor_hscroll.grid(row=2, column=0, columnspan=3, sticky="ew")
        area.grid_rowconfigure(1, weight=1)
        area.grid_columnconfigure(1, weight=1)
        self._configure_tags()
        self._build_find_bar()

    def _build_find_bar(self):
        # A 1px THEME border via an outer tk.Frame; shown/hidden with place()
        # so the editor never reflows.
        self.find_bar = tk.Frame(self._editor_area, background=THEME["border"], padx=1, pady=1)
        inner = ttk.Frame(self.find_bar, padding=8, style="Raise.TFrame")
        inner.pack(fill="both", expand=True)
        row1 = ttk.Frame(inner, style="Raise.TFrame")
        row1.pack(fill="x")
        self.find_entry = ttk.Entry(row1, width=22, font=self.font_mono_sm)
        self.find_entry.pack(side="left")
        self.find_count = ttk.Label(row1, text="", width=10, style="Raise.TLabel")
        self.find_count.pack(side="left", padx=(8, 4))
        ttk.Button(row1, text="↑", width=2, command=self.find_prev, style="Find.TButton").pack(side="left")
        ttk.Button(row1, text="↓", width=2, command=self.find_next, style="Find.TButton").pack(side="left", padx=(2, 0))
        ttk.Button(row1, text="✕", width=2, command=self.hide_find_bar, style="Find.TButton").pack(side="left", padx=(8, 0))
        row2 = ttk.Frame(inner, style="Raise.TFrame")
        row2.pack(fill="x", pady=(6, 0))
        self.replace_entry = ttk.Entry(row2, width=22, font=self.font_mono_sm)
        self.replace_entry.pack(side="left")
        ttk.Button(row2, text="Replace", command=self.replace_current, style="Find.TButton").pack(side="left", padx=(8, 0))
        ttk.Button(row2, text="All", command=self.replace_all, style="Find.TButton").pack(side="left", padx=(2, 0))
        for entry in (self.find_entry, self.replace_entry):
            entry.bind("<Escape>", self.hide_find_bar)
        self.find_entry.bind("<Return>", self.find_next)
        self.find_entry.bind("<Shift-Return>", self.find_prev)
        self.find_entry.bind("<KeyRelease>", self._find_refresh)
        self.replace_entry.bind("<Return>", lambda e: self.replace_current())

    def _build_panel(self):
        self.bottom_tabs = PanelTabs(self._panel)
        self.bottom_tabs.pack(fill="both", expand=True)
        frames = {}
        for key, label in (("output", "Output"), ("problems", "Problems"), ("debug", "Debug"),
                           ("trace", "Trace"), ("symbols", "Symbols"), ("optimizer", "Optimizer")):
            frame = self.bottom_tabs.new_frame()
            self.bottom_tabs.add(frame, label)
            frames[key] = frame
        self.problems_frame = frames["problems"]
        self.symbols_frame = frames["symbols"]
        self.optimizer_frame = frames["optimizer"]
        self._build_output_tab(frames["output"])
        self._build_problems_tab(frames["problems"])
        self._build_debug_tab(frames["debug"])
        self._build_trace_tab(frames["trace"])
        self._build_symbols_tab(frames["symbols"])
        self._build_optimizer_tab(frames["optimizer"])
        self.bottom_tabs.bind("<<TabChanged>>", self._on_tab_changed)

    # -- small builders shared by the panel tabs ------------------------

    def _panel_list(self, parent, **kwargs):
        T = THEME
        options = dict(background=T["bg_dark"], foreground=T["fg"], font=self.font_mono_sm,
                       borderwidth=0, highlightthickness=0, activestyle="none",
                       selectbackground=T["bg_active"], selectforeground=T["fg_bright"])
        options.update(kwargs)
        return tk.Listbox(parent, **options)

    def _scrolled(self, widget, hbar=False):
        """Attach flat themed scrollbars to `widget` inside its parent.
        Call before packing the widget itself (pack order matters)."""
        parent = widget.master
        if hbar:
            horizontal = ttk.Scrollbar(parent, orient="horizontal", style="Flat.Horizontal.TScrollbar",
                                       command=widget.xview)
            widget.config(xscrollcommand=horizontal.set)
            horizontal.pack(side="bottom", fill="x")
        vertical = ttk.Scrollbar(parent, orient="vertical", style="Flat.Vertical.TScrollbar",
                                 command=widget.yview)
        widget.config(yscrollcommand=vertical.set)
        vertical.pack(side="right", fill="y")

    def _scrolled_list(self, parent, hbar=False, **kwargs):
        wrap = ttk.Frame(parent, style="Panel.TFrame")
        wrap.pack(fill="both", expand=True)
        box = self._panel_list(wrap, **kwargs)
        self._scrolled(box, hbar=hbar)
        box.pack(side="left", fill="both", expand=True)
        return box

    def _scrolled_tree(self, parent, columns=(), widths=(), **kwargs):
        """A headless (no heading row) tree + flat scrollbar, the shape every
        debugger panel uses: column #0 is the name, the rest are values.
        Only the last column stretches - letting an intermediate one absorb
        the slack pushes the final column far off to the right."""
        wrap = ttk.Frame(parent, style="Panel.TFrame")
        wrap.pack(fill="both", expand=True)
        tree = ttk.Treeview(wrap, columns=columns, show="tree", style="Panel.Treeview", **kwargs)
        tree.column("#0", width=widths[0] if widths else 200, stretch=not columns)
        for index, (name, width) in enumerate(zip(columns, widths[1:])):
            tree.column(name, width=width, stretch=index == len(columns) - 1)
        tree.tag_configure("group", foreground=THEME["fg_dim"])
        tree.tag_configure("dim", foreground=THEME["fg_faint"])
        self._scrolled(tree)
        tree.pack(side="left", fill="both", expand=True)
        return tree

    @staticmethod
    def _column(parent, title, weight=True, divider=True):
        """One titled column of the Debug/Optimizer tabs, with the hairline
        that separates it from the previous one."""
        if divider:
            hairline(parent, side="left", pady=8)
        column = ttk.Frame(parent, style="Panel.TFrame", padding=(10, 8, 10, 10))
        column.pack(side="left", fill="both", expand=weight)
        ttk.Label(column, text=title, style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        return column

    # -- panel tabs -----------------------------------------------------

    def _build_output_tab(self, frame):
        T = THEME
        # The input row packs first (side="bottom") so a short panel squeezes
        # the console, never the prompt.
        terminal_input = ttk.Frame(frame, padding=(12, 8), style="Panel.TFrame")
        terminal_input.pack(side="bottom", fill="x")
        hairline(frame, side="bottom")  # lands directly above the input row
        console_wrap = ttk.Frame(frame, style="Panel.TFrame")
        console_wrap.pack(fill="both", expand=True)
        self.console = tk.Text(console_wrap, width=1, height=10, background=T["bg_dark"], foreground=T["fg"],
                               insertbackground=T["cursor"], font=self.font_mono, state="disabled",
                               borderwidth=0, highlightthickness=0, padx=14, pady=10, spacing1=1)
        self.console.tag_configure("meta", foreground=T["fg_faint"])
        self._scrolled(self.console)
        self.console.pack(side="left", fill="both", expand=True)
        self.terminal_prompt = ttk.Label(terminal_input, text="❯", style="Prompt.TLabel")
        self.terminal_prompt.pack(side="left", padx=(2, 8))
        self.terminal_entry = ttk.Entry(terminal_input, font=self.font_mono, state="disabled")
        self.terminal_entry.pack(side="left", fill="x", expand=True)
        self.terminal_entry.bind("<Return>", self.submit_terminal_input)
        self.terminal_send = ttk.Button(terminal_input, text="Send", command=self.submit_terminal_input,
                                        state="disabled", style="Accent.TButton")
        self.terminal_send.pack(side="left", padx=(8, 0))

    def _build_problems_tab(self, frame):
        wrap = ttk.Frame(frame, style="Panel.TFrame", padding=(2, 4, 2, 2))
        wrap.pack(fill="both", expand=True)
        self.problems = self._scrolled_tree(wrap, columns=("where", "message"), widths=(170, 130, 700),
                                            selectmode="browse")
        for tag, color in (("ok", THEME["ok"]), ("warn", THEME["warn"]), ("error", THEME["error"])):
            self.problems.tag_configure(tag, foreground=color)
        self.problems.bind("<Double-1>", self.go_to_problem)

    def _build_debug_tab(self, frame):
        hint = ttk.Frame(frame, padding=(14, 8, 14, 4), style="Panel.TFrame")
        hint.pack(fill="x")
        ttk.Label(hint, text="Click a line number to toggle a breakpoint, then Debug.  ·  "
                            "Step and Continue are in the toolbar.  ·  "
                            "Double-click a watch to remove it.",
                  style="Hint.TLabel").pack(side="left")
        panes = ttk.Frame(frame, style="Panel.TFrame")
        panes.pack(fill="both", expand=True)
        call_col = self._column(panes, "Call Stack", weight=False, divider=False)
        call_col.configure(width=190)
        call_col.pack_propagate(False)
        self.callstack_list = self._scrolled_tree(call_col, widths=(170,), selectmode="none")
        vars_col = self._column(panes, "Variables")
        self.vars_list = self._scrolled_tree(vars_col, columns=("value",), widths=(160, 260), selectmode="browse")
        watch_col = self._column(panes, "Watch")
        watch_entry_row = ttk.Frame(watch_col, style="Panel.TFrame")
        watch_entry_row.pack(fill="x", pady=(0, 6))
        self.watch_entry = ttk.Entry(watch_entry_row, font=self.font_mono_sm)
        self.watch_entry.pack(side="left", fill="x", expand=True)
        self.watch_entry.bind("<Return>", lambda e: self.add_watch())
        ttk.Button(watch_entry_row, text="Add", command=self.add_watch,
                   style="Ghost.TButton").pack(side="left", padx=(6, 0))
        self.watch_list = self._scrolled_tree(watch_col, columns=("value",), widths=(140, 200), selectmode="browse")
        self.watch_list.tag_configure("watched", foreground=THEME["watch_fg"])
        self.watch_list.bind("<Double-1>", self.remove_watch)

    def _build_trace_tab(self, frame):
        inner = ttk.Frame(frame, padding=(12, 10), style="Panel.TFrame")
        inner.pack(fill="both", expand=True)
        self.trace_list = self._scrolled_list(inner, foreground=THEME["trace_fg"])

    def _build_symbols_tab(self, frame):
        symbols_toolbar = ttk.Frame(frame, padding=(12, 8, 12, 4), style="Panel.TFrame")
        symbols_toolbar.pack(fill="x")
        ttk.Button(symbols_toolbar, text="Refresh", command=self._refresh_symbols_from_source,
                   style="Ghost.TButton").pack(side="left")
        ttk.Label(symbols_toolbar,
                  text="The semantic analyzer's symbol table: functions, structs, typedefs and globals.",
                  style="Hint.TLabel").pack(side="left", padx=(10, 0))
        wrap = ttk.Frame(frame, style="Panel.TFrame")
        wrap.pack(fill="both", expand=True, padx=2, pady=(0, 2))
        self.symbols_tree = ttk.Treeview(wrap, columns=("type", "detail"), style="Panel.Treeview", height=8)
        self.symbols_tree.heading("#0", text="Name", anchor="w")
        self.symbols_tree.heading("type", text="Type", anchor="w")
        self.symbols_tree.heading("detail", text="Details", anchor="w")
        # Only the trailing column stretches, so a wide panel doesn't strand
        # Type and Details far off to the right of the names.
        self.symbols_tree.column("#0", width=420, stretch=False)
        self.symbols_tree.column("type", width=150, stretch=False)
        self.symbols_tree.column("detail", width=360, stretch=True)
        self.symbols_tree.tag_configure("dim", foreground=THEME["fg_faint"])
        self.symbols_tree.tag_configure("group", foreground=THEME["fg_dim"])
        self._scrolled(self.symbols_tree)
        self.symbols_tree.pack(side="left", fill="both", expand=True)
        self.symbols_tree.bind("<Double-1>", self.go_to_symbol)
        self.refresh_symbols()

    def _build_optimizer_tab(self, frame):
        T = THEME
        optimizer_toolbar = ttk.Frame(frame, padding=(12, 8, 12, 4), style="Panel.TFrame")
        optimizer_toolbar.pack(fill="x")
        ttk.Button(optimizer_toolbar, text="Optimize", command=self.show_optimization,
                   style="Ghost.TButton").pack(side="left")
        self.optimizer_stats = ttk.Label(optimizer_toolbar,
                                         text="Press Optimize to compare the IR before and after optimization.",
                                         style="Hint.TLabel")
        self.optimizer_stats.pack(side="left", padx=(10, 0))
        optimizer_panes = ttk.Frame(frame, style="Panel.TFrame")
        optimizer_panes.pack(fill="both", expand=True)
        before_col = self._column(optimizer_panes, "Original IR", divider=False)
        # width=1: tk.Text asks for 80 columns by default, and two of those
        # eat the whole panel before the third column gets any space at all.
        self.opt_before = tk.Text(before_col, width=1, height=8, state="disabled", wrap="none",
                                  background=T["bg_dark"], foreground=T["ir_before_fg"],
                                  font=self.font_mono_sm, borderwidth=0, highlightthickness=0)
        self._scrolled(self.opt_before, hbar=True)
        self.opt_before.pack(side="left", fill="both", expand=True)
        self.opt_before.tag_configure("removed", foreground=T["ir_removed"])
        self.opt_before.tag_configure("rewritten", foreground=T["ir_rewritten"])
        after_col = self._column(optimizer_panes, "Optimized IR")
        self.opt_after = tk.Text(after_col, width=1, height=8, state="disabled", wrap="none",
                                 background=T["bg_dark"], foreground=T["ir_after_fg"],
                                 font=self.font_mono_sm, borderwidth=0, highlightthickness=0)
        self._scrolled(self.opt_after, hbar=True)
        self.opt_after.pack(side="left", fill="both", expand=True)
        log_col = self._column(optimizer_panes, "Transformations")
        self.opt_log = self._scrolled_list(log_col, height=8)
        self.opt_log.bind("<Double-1>", self.go_to_transformation)

    def _make_menu(self):
        def acc(key):
            return f"Command-{key}" if self.is_aqua else f"Ctrl+{key}"

        menu = tk.Menu(self)
        file_menu = tk.Menu(menu, tearoff=False)
        for label, callback, key in (("New", self.new_file, acc("N")), ("Open…", self.open_file, acc("O")),
                                     ("Save", self.save_file, acc("S"))):
            file_menu.add_command(label=label, command=callback, accelerator=key)
        menu.add_cascade(label="File", menu=file_menu)
        edit_menu = tk.Menu(menu, tearoff=False)
        for label, callback, key in (("Find / Replace…", self.show_find_bar, acc("F")),
                                     ("Rename symbol…", self.rename_symbol, acc("R")),
                                     ("Insert template…", self.show_templates, "Ctrl+Space")):
            edit_menu.add_command(label=label, command=callback, accelerator=key)
        menu.add_cascade(label="Edit", menu=edit_menu)
        view_menu = tk.Menu(menu, tearoff=False)
        for label, callback, key in (("Toggle sidebar", self.toggle_sidebar, acc("B")),
                                     ("Toggle panel", self.toggle_panel, acc("J"))):
            view_menu.add_command(label=label, command=callback, accelerator=key)
        view_menu.add_separator()
        # The font-size control lives here now that the toolbar spinbox is gone.
        for label, delta, key in (("Increase font size", 1, acc("+")), ("Decrease font size", -1, acc("-"))):
            view_menu.add_command(label=label, command=lambda d=delta: self.change_font_size(d), accelerator=key)
        menu.add_cascade(label="View", menu=view_menu)
        tools = tk.Menu(menu, tearoff=False)
        for label, callback, key in (("Run", self.run_program, "F5"), ("Debug", self.debug_program, "F6"),
                                     ("Check code", self.check_code, "F7"),
                                     ("Optimize IR", self.show_optimization, acc("Shift-O")),
                                     ("Symbol table", self.show_symbols, "")):
            tools.add_command(label=label, command=callback, accelerator=key)
        tools.add_separator()
        for label, callback, key in (("Step", self.debug_step, "F8"),
                                     ("Continue", self.debug_continue, "F9"),
                                     ("Stop", self.debug_stop, "Shift+F5")):
            tools.add_command(label=label, command=callback, accelerator=key)
        menu.add_cascade(label="Tools", menu=tools)
        self.config(menu=menu)

    def _bind_shortcuts(self):
        self.editor.bind("<<Modified>>", self.on_modified)
        self.editor.bind("<KeyRelease>", self.on_key_release)
        self.editor.bind("<ButtonRelease-1>", self.on_key_release)
        self.editor.bind("<Escape>", self.hide_find_bar)
        mod = "Command" if self.is_aqua else "Control"
        bindings = {
            f"<{mod}-n>": lambda e: self.new_file(),
            f"<{mod}-o>": lambda e: self.open_file(),
            f"<{mod}-s>": lambda e: self.save_file(),
            f"<{mod}-r>": lambda e: self.rename_symbol(),
            f"<{mod}-f>": self.show_find_bar,
            f"<{mod}-b>": lambda e: self.toggle_sidebar(),
            f"<{mod}-j>": lambda e: self.toggle_panel(),
            f"<{mod}-Shift-O>": lambda e: self.show_optimization(),
            # plus/equal both, so the size grows with or without Shift held.
            f"<{mod}-plus>": lambda e: self.change_font_size(1),
            f"<{mod}-equal>": lambda e: self.change_font_size(1),
            f"<{mod}-minus>": lambda e: self.change_font_size(-1),
            "<F5>": lambda e: self.run_program(),
            "<Shift-F5>": lambda e: self.debug_stop(),
            "<F6>": lambda e: self.debug_program(),
            "<F7>": lambda e: self.check_code(),
            "<F8>": lambda e: self.debug_step(),
            "<F9>": lambda e: self.debug_continue(),
            "<Control-space>": self.show_suggestions,
        }
        for sequence, callback in bindings.items():
            self.bind_all(sequence, self._shortcut(callback))
        if not self.is_aqua:
            # The Text class has emacs-style bindings for several Control
            # keys (Control-o = open line, Control-n = next line, ...) that
            # would run before a bind_all handler; a widget-level bind
            # returning "break" preempts them.
            for sequence in (f"<{mod}-n>", f"<{mod}-o>", f"<{mod}-f>", f"<{mod}-b>", f"<{mod}-j>"):
                self.editor.bind(sequence, self._shortcut(bindings[sequence]))

    @staticmethod
    def _shortcut(callback):
        def handler(event):
            callback(event)
            return "break"
        return handler

    def _configure_tags(self):
        T = THEME
        for name, color in (("keyword", T["syn_keyword"]), ("type", T["syn_type"]), ("string", T["syn_string"]),
                            ("number", T["syn_number"]), ("comment", T["syn_comment"]),
                            ("function", T["syn_function"]), ("error", T["syn_error"])):
            self.editor.tag_configure(name, foreground=color)
        self.editor.tag_configure("error", underline=True)
        self.editor.tag_configure("cursor_line", background=T["cursor_line"])
        self.editor.tag_configure("current_line", background=T["current_line"])
        self.editor.tag_configure("find_match", background=T["find_match"])
        self.editor.tag_configure("find_current", background=T["find_current"], foreground=T["fg_bright"])
        # The caret line sits under every other background tag; the paused
        # line, the find highlights and the selection all have to win over it.
        self.editor.tag_lower("cursor_line")
        for name in ("current_line", "find_match", "find_current", "sel"):
            self.editor.tag_raise(name)

    def change_font_size(self, delta):
        size = max(9, min(24, int(self.font_editor.cget("size")) + delta))
        self.font_editor.configure(size=size)
        self.refresh_line_numbers()

    def _scroll_both(self, first, last, scrollbar):
        scrollbar.set(first, last)
        self.line_numbers.yview_moveto(first)

    def _scroll_editor(self, *args):
        self.editor.yview(*args)
        self.line_numbers.yview(*args)

    def _gutter_scroll(self, event):
        # aqua reports small per-notch deltas (±1..±10); X11/Windows use ±120.
        if abs(event.delta) >= 120:
            step = -1 * (event.delta // 120)
        else:
            step = -1 if event.delta > 0 else 1
        self.editor.yview_scroll(step, "units")
        return "break"

    def source(self):
        return self.editor.get("1.0", "end-1c")

    def on_modified(self, _event=None):
        if self.editor.edit_modified():
            self.dirty = True
            self.editor.edit_modified(False)
            self._schedule_refresh()

    def on_key_release(self, _event=None):
        # The caret's own feedback is cheap, so it updates on every keystroke
        # instead of waiting out the highlight/outline debounce.
        self._sync_caret()
        self._schedule_refresh()

    def _schedule_refresh(self):
        if self.refresh_id:
            self.after_cancel(self.refresh_id)
        self.refresh_id = self.after(180, self._refresh_all)

    def _sync_caret(self):
        line, col = self.editor.index("insert").split(".")
        self.status_pos.config(text=f"Ln {line}, Col {int(col) + 1}")
        self.editor.tag_remove("cursor_line", "1.0", "end")
        self.editor.tag_add("cursor_line", f"{line}.0", f"{int(line) + 1}.0")

    def _refresh_all(self):
        self.refresh_id = None
        self.highlight()
        self.refresh_line_numbers()
        self.refresh_outline()
        self._update_editor_tab()
        self._sync_caret()
        self._refresh_diagnostics_status()

    def _refresh_diagnostics_status(self):
        """The status bar's left segment. Counts are only meaningful for the
        buffer they were produced from, so an edit demotes them to a hint
        rather than leaving a stale ✓ on screen."""
        if self._checked_src != self.source():
            self.status.config(text="Not checked", foreground=THEME["fg_faint"])
            return
        errors, warnings = self._diag_counts
        if not errors and not warnings:
            self.status.config(text="✓  No problems", foreground=THEME["ok"])
        else:
            self.status.config(text=f"✖ {errors}    ⚠ {warnings}",
                               foreground=THEME["error"] if errors else THEME["warn"])

    def _update_editor_tab(self):
        name = self.file_path.name if self.file_path else "Untitled.src"
        self.editor_tab.config(text=f"{name}  ●" if self.dirty else name,
                               foreground=THEME["fg_bright"] if self.dirty else THEME["fg"])

    def highlight(self):
        src = self.source()
        for tag in ("keyword", "type", "string", "number", "comment", "function", "error"):
            self.editor.tag_remove(tag, "1.0", "end")
        tokens, errors = Scanner(src).scan_all()
        for tok in tokens:
            if tok.ttype in (TT.EOF, TT.ERROR):
                continue
            start, end = f"{tok.line}.{tok.col-1}", f"{tok.line}.{tok.col-1+len(tok.lexeme)}"
            tag = "type" if tok.ttype == TT.KEYWORD and tok.lexeme in {"int","float","char","string","bool","void"} else "keyword" if tok.ttype == TT.KEYWORD else "string" if tok.ttype in (TT.STRING_LIT,TT.CHAR_LIT,TT.INTERP_STRING) else "number" if tok.ttype in (TT.INTEGER_LIT,TT.FLOAT_LIT,TT.BOOL_LIT) else None
            if tag:
                self.editor.tag_add(tag, start, end)
        for match in re.finditer(r"//[^\n]*|/\*[\s\S]*?\*/", src):
            self.editor.tag_add("comment", f"1.0+{match.start()}c", f"1.0+{match.end()}c")
        for match in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", src):
            self.editor.tag_add("function", f"1.0+{match.start(1)}c", f"1.0+{match.end(1)}c")
        for error in errors:
            self.editor.tag_add("error", f"{error.line}.{error.col-1}", f"{error.line}.{error.col}")

    def refresh_line_numbers(self):
        count = max(1, int(self.editor.index("end-1c").split(".")[0]))
        digits = max(2, len(str(count)))
        active = int(self.editor.index("insert").split(".")[0])
        self.line_numbers.config(state="normal", width=digits + 2)
        self.line_numbers.delete("1.0", "end")
        self.line_numbers.insert("1.0", "\n".join(
            f"{'●' if i in self.breakpoints else ' '} {i:>{digits}}" for i in range(1, count + 1)))
        for line in self.breakpoints:
            if line <= count:
                self.line_numbers.tag_add("breakpoint", f"{line}.0", f"{line}.1")
        if active <= count:
            self.line_numbers.tag_add("active_ln", f"{active}.2", f"{active}.end")
        self.line_numbers.config(state="disabled")
        self.line_numbers.yview_moveto(self.editor.yview()[0])

    def toggle_breakpoint(self, event):
        line = int(self.line_numbers.index(f"@{event.x},{event.y}").split(".")[0])
        self.breakpoints.symmetric_difference_update({line})
        self.refresh_line_numbers()

    # Diamonds are types (filled defines one, hollow only aliases one),
    # squares are storage, and ƒ is a function. Nothing here is a round dot:
    # a list of bullets reads as prose, not as a symbol tree.
    OUTLINE_GLYPHS = {"function": "ƒ", "struct": "◆", "typedef": "◇",
                      "const": "▣", "field": "▪", "variable": "▫"}

    def refresh_outline(self):
        """Rebuild the sidebar tree from a real parse of the buffer.

        This runs off `parse_source`, not a regex over the text: the shape it
        has to report - which `struct` keyword opens a declaration and which
        one merely types a field, where an inline `typedef struct C {...} R;`
        ends - is exactly what a parser decides and a regex can only guess
        at. The parser's `many_rec` recovery hands back a *partial* AST when
        the buffer is mid-edit, which is what makes this usable on every
        keystroke rather than only on a clean file.
        """
        src = self.source()
        if src == self._outline_src and self.file_path == self._outline_path:
            return                              # nothing changed - keep the tree (and its scroll)
        try:
            ast, syntax_errors, lex_errors = parse_source(src)
            declarations = ast.fields["declarations"]
        except Exception:
            return                              # unparseable mid-edit: leave the last good tree up
        # Half-written code makes recovery *drop* the declaration being typed,
        # so rebuilding on every keystroke would make whole functions blink out
        # of the tree and back. While the buffer has errors, a shrinking
        # outline is assumed to be that, and the last complete one stays up; it
        # still grows immediately, and any clean parse always rebuilds (so
        # deleting a function really does remove it). Loading a different file
        # is not an edit, though - its outline always replaces the old one,
        # however few declarations survive.
        same_file = self._outline_path == self.file_path
        if (same_file and (syntax_errors or lex_errors)
                and len(declarations) < self._outline_count):
            return
        self._outline_src = src
        self._outline_path = self.file_path
        self._outline_count = len(declarations)

        # Rebuilding drops both the scroll offset and every expand/collapse
        # the user set, so they are captured and replayed. Item ids are keyed
        # by name rather than by line so that typing *above* a struct doesn't
        # silently re-expand it.
        open_state = {iid: bool(self.outline.item(iid, "open")) for iid in self._outline_ids()}
        top = self.outline.yview()[0]
        self.outline.delete(*self.outline.get_children())
        self._outline_seen = {}

        for decl in declarations:
            if decl.kind == "VarDecl":          # only `const` reaches global scope
                for d in decl.fields["declarators"]:
                    self._outline_row(open_state, "", "const", d.fields["name"],
                                      d.line or decl.line)
            elif decl.kind == "StructDecl":
                self._outline_struct(open_state, decl.fields["name"], decl.fields["fields"], decl.line)
            elif decl.kind == "TypedefDecl":
                aliased = decl.fields["aliased_type"]
                if aliased is not None and aliased.kind == "StructDef":
                    # `typedef struct Color {...} RGB;` both defines a struct
                    # and aliases it, so it earns a row of each.
                    self._outline_struct(open_state, aliased.fields["name"],
                                         aliased.fields["fields"], aliased.line or decl.line)
                self._outline_row(open_state, "", "typedef", decl.fields["name"], decl.line)
            elif decl.kind == "FunctionDecl":
                parent = self._outline_row(open_state, "", "function",
                                           decl.fields["name"], decl.line)
                # The whole declaration, not just the body: parameters are
                # local to the function too, and sort ahead of its own
                # declarations by virtue of being on the signature's line.
                locals_found = []
                collect_locals(decl, locals_found)
                for line, name in sorted(locals_found, key=lambda item: item[0] or 0):
                    self._outline_row(open_state, parent, "variable", name, line)

        self.outline.yview_moveto(top)

    def _outline_ids(self, parent=""):
        for iid in self.outline.get_children(parent):
            yield iid
            yield from self._outline_ids(iid)

    def _outline_struct(self, open_state, name, fields, line):
        parent = self._outline_row(open_state, "", "struct", name, line)
        for f in fields:
            self._outline_row(open_state, parent, "field", f.fields["name"], f.line or line)

    def _outline_row(self, open_state, parent, kind, name, line):
        """Insert one row under `parent`, restoring its previous open state.

        Rows are labelled with the bare name - a declaration's *type* is the
        Symbols tab's job (it renders resolved types via `semantics.type_name`,
        which needs a `SymbolTable` this doesn't have), and spelling it here
        only competes with the name for a narrow sidebar's width.

        The item id is keyed by that name rather than by position, so it
        survives edits elsewhere in the file; a `#n` suffix disambiguates the
        genuine duplicates (a local shadowed in a nested block, two same-named
        declarations in a broken buffer)."""
        iid = f"{parent}/{kind}:{name}"
        self._outline_seen[iid] = self._outline_seen.get(iid, 0) + 1
        if self._outline_seen[iid] > 1:
            iid = f"{iid}#{self._outline_seen[iid]}"
        return self.outline.insert(parent, "end", iid=iid,
                                   text=f" {self.OUTLINE_GLYPHS[kind]}  {name}",
                                   values=(line or 1,), tags=(kind,),
                                   open=open_state.get(iid, True))

    def go_to_outline(self, _event=None):
        selected = self.outline.selection()
        if selected:
            self.editor.mark_set("insert", f"{self.outline.item(selected[0], 'values')[0]}.0")
            self.editor.see("insert")
            self.editor.focus_set()

    # ------------------------------------------------------------------
    # Find / replace
    # ------------------------------------------------------------------

    def show_find_bar(self, _event=None):
        try:
            selection = self.editor.get("sel.first", "sel.last")
        except tk.TclError:
            selection = ""
        self.find_bar.place(in_=self.editor, relx=1.0, x=-14, y=10, anchor="ne")
        if selection and "\n" not in selection:
            self.find_entry.delete(0, "end")
            self.find_entry.insert(0, selection)
        self.find_entry.focus_set()
        self.find_entry.select_range(0, "end")
        self._find_refresh(force=True)
        return "break"

    def hide_find_bar(self, _event=None):
        self.find_bar.place_forget()
        self.editor.tag_remove("find_match", "1.0", "end")
        self.editor.tag_remove("find_current", "1.0", "end")
        self._find_term = None
        self._find_matches = []
        self._find_pos = -1
        self.editor.focus_set()
        return "break"

    def _find_refresh(self, _event=None, force=False):
        term = self.find_entry.get()
        if not force and term == self._find_term:
            return
        self._find_term = term
        self.editor.tag_remove("find_match", "1.0", "end")
        self.editor.tag_remove("find_current", "1.0", "end")
        self._find_matches = []
        self._find_pos = -1
        if not term:
            self.find_count.config(text="")
            return
        index = "1.0"
        while True:
            index = self.editor.search(term, index, stopindex="end", nocase=True)
            if not index:
                break
            end = f"{index}+{len(term)}c"
            self.editor.tag_add("find_match", index, end)
            self._find_matches.append(self.editor.index(index))
            index = end
        self.find_count.config(text=f"{len(self._find_matches)} found" if self._find_matches else "No results")

    def _find_select(self, position):
        self._find_pos = position
        start = self._find_matches[position]
        end = f"{start}+{len(self._find_term)}c"
        self.editor.tag_remove("find_current", "1.0", "end")
        self.editor.tag_add("find_current", start, end)
        self.editor.mark_set("insert", start)
        self.editor.see(start)
        self.find_count.config(text=f"{position + 1} of {len(self._find_matches)}")

    def find_next(self, _event=None):
        self._find_refresh()
        if not self._find_matches:
            return "break"
        if self._find_pos == -1:
            insert = self.editor.index("insert")
            position = next((i for i, m in enumerate(self._find_matches)
                             if self.editor.compare(m, ">=", insert)), 0)
        else:
            position = (self._find_pos + 1) % len(self._find_matches)
        self._find_select(position)
        return "break"

    def find_prev(self, _event=None):
        self._find_refresh()
        if not self._find_matches:
            return "break"
        position = (self._find_pos - 1) % len(self._find_matches) if self._find_pos != -1 else len(self._find_matches) - 1
        self._find_select(position)
        return "break"

    def replace_current(self):
        ranges = self.editor.tag_ranges("find_current")
        if not ranges:
            self.find_next()
            return
        self.editor.delete(ranges[0], ranges[1])
        self.editor.insert(ranges[0], self.replace_entry.get())
        self._find_refresh(force=True)
        self.find_next()

    def replace_all(self):
        self._find_refresh(force=True)
        if not self._find_matches:
            return
        replacement = self.replace_entry.get()
        length = len(self._find_term)
        for start in reversed(self._find_matches):
            self.editor.delete(start, f"{start}+{length}c")
            self.editor.insert(start, replacement)
        count = len(self._find_matches)
        self._find_refresh(force=True)
        self.find_count.config(text=f"Replaced {count}")

    # ------------------------------------------------------------------
    # Templates / dialogs
    # ------------------------------------------------------------------

    def _dialog(self, title, size=None):
        """A modal, themed Toplevel: flat surface, a body the caller fills,
        centered over the main window. `size` is "WxH"; omit it to let Tk
        fit the window to its contents. The title shows in the window's own
        title bar only - repeating it as a heading inside a 400px dialog
        just says the same thing twice."""
        T = THEME
        win = tk.Toplevel(self)
        win.title(title)
        win.transient(self)
        win.configure(bg=T["bg_dark"])
        win.resizable(False, False)
        if size:
            win.geometry(size)
        win.grab_set()
        body = tk.Frame(win, bg=T["bg_dark"], padx=18, pady=16)
        body.pack(fill="both", expand=True)
        win.bind("<Escape>", lambda e: win.destroy())
        # Deferred: the caller hasn't filled the body yet, so the requested
        # size isn't known until the event loop next goes idle.
        win.after(0, self._center_dialog, win)
        return win, body

    def _center_dialog(self, win):
        try:
            win.update_idletasks()
            width, height = win.winfo_width(), win.winfo_height()
            if width <= 1:  # never mapped with an explicit geometry
                width, height = win.winfo_reqwidth(), win.winfo_reqheight()
            x = self.winfo_rootx() + (self.winfo_width() - width) // 2
            y = self.winfo_rooty() + (self.winfo_height() - height) // 3
            win.geometry(f"+{max(0, x)}+{max(0, y)}")
        except tk.TclError:
            pass  # dismissed before the idle callback ran

    def show_suggestions(self, _event=None):
        self.show_templates()
        return "break"

    def show_templates(self):
        T = THEME
        win, body = self._dialog("Insert a code template", "420x440")
        tk.Label(body, text="Templates and language keywords", bg=T["bg_dark"], fg=T["fg_dim"],
                 font=self.font_ui_sm, anchor="w").pack(fill="x", pady=(0, 10))
        frame = tk.Frame(body, bg=T["border"], padx=1, pady=1)
        frame.pack(fill="both", expand=True)
        box = tk.Listbox(frame, font=self.font_mono_sm, height=13, bg=T["bg_raise"], fg=T["fg"],
                         selectbackground=T["select_bg"], selectforeground=T["fg_bright"],
                         highlightthickness=0, borderwidth=0, activestyle="none")
        for item in list(TEMPLATES) + sorted(KEYWORDS):
            box.insert("end", item)
        scroll = ttk.Scrollbar(frame, orient="vertical", style="Flat.Vertical.TScrollbar", command=box.yview)
        box.config(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        box.pack(side="left", fill="both", expand=True)
        box.selection_set(0)

        def insert(_event=None):
            self.insert_template(box.get("active"))
            win.destroy()

        actions = tk.Frame(body, bg=T["bg_dark"])
        actions.pack(fill="x", pady=(14, 0))
        # Rightmost is the default action, per platform convention.
        ttk.Button(actions, text="Insert", command=insert, style="Accent.TButton").pack(side="right")
        ttk.Button(actions, text="Cancel", command=win.destroy).pack(side="right", padx=(0, 8))
        box.bind("<Double-1>", insert)
        box.bind("<Return>", insert)
        box.focus_set()

    def themed_prompt(self, title, message, initial=""):
        """A small themed replacement for Tk's platform-coloured askstring."""
        T = THEME
        result = {"value": None}
        win, body = self._dialog(title)
        tk.Label(body, text=message, bg=T["bg_dark"], fg=T["fg"], font=self.font_ui_sm,
                 wraplength=340, justify="left").pack(anchor="w", pady=(0, 8))
        border = tk.Frame(body, bg=T["border"], padx=1, pady=1)
        border.pack(fill="x")
        entry = tk.Entry(border, width=42, bg=T["bg_raise"], fg=T["fg"], insertbackground=T["cursor"],
                         relief="flat", highlightthickness=0, borderwidth=0, font=self.font_mono_sm)
        entry.insert(0, initial)
        entry.pack(fill="x", ipady=6, ipadx=6)
        actions = tk.Frame(body, bg=T["bg_dark"])
        actions.pack(fill="x", pady=(16, 0))

        def accept(_event=None):
            result["value"] = entry.get()
            win.destroy()

        ttk.Button(actions, text="OK", command=accept, style="Accent.TButton").pack(side="right")
        ttk.Button(actions, text="Cancel", command=win.destroy).pack(side="right", padx=(0, 8))
        entry.bind("<Return>", accept)
        entry.focus_set()
        entry.selection_range(0, "end")
        self.wait_window(win)
        return result["value"]

    def insert_template(self, name):
        content = TEMPLATES.get(name, name)
        values = {"cursor": ""}
        for placeholder in dict.fromkeys(re.findall(r"\$\{(\w+)\}", content)):
            if placeholder != "cursor":
                values[placeholder] = self.themed_prompt("Template value", f"Enter a value for {placeholder}:") or placeholder
        self.editor.insert("insert", re.sub(r"\$\{(\w+)\}", lambda m: values.get(m.group(1), ""), content))
        self._refresh_all()

    def rename_symbol(self):
        old = self.editor.get("insert wordstart", "insert wordend")
        if not old or old in KEYWORDS:
            messagebox.showinfo("Rename symbol", "Place the cursor on an identifier first.", parent=self)
            return
        new = self.themed_prompt("Rename symbol", f"Rename '{old}' to:", old)
        if new is None or new == old:
            return
        if not re.fullmatch(r"[A-Za-z_]\w*", new) or new in KEYWORDS:
            messagebox.showerror("Rename symbol", "Enter a valid non-keyword identifier.", parent=self)
            return
        source = self.source()
        count = len(re.findall(rf"\b{re.escape(old)}\b", source))
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", re.sub(rf"\b{re.escape(old)}\b", new, source))
        self.write_console(f"Refactoring complete: renamed {count} occurrence(s) of '{old}' to '{new}'.\n", meta=True)
        self._refresh_all()

    # ------------------------------------------------------------------
    # Check / problems / symbol table
    # ------------------------------------------------------------------

    def _analyze_buffer(self):
        """Front end over the current buffer: scan+parse, then (only when
        clean) semantic analysis. Returns (symtab, diagnostics) where
        symtab is None whenever the program is not runnable."""
        ast, syntax_errors, lex_errors = parse_source(self.source())
        diagnostics = [str(e) for e in lex_errors] + [str(e) for e in syntax_errors]
        if diagnostics:
            return None, diagnostics
        from semantics import analyze
        symtab, semantic_diags = analyze(ast)
        diagnostics = [str(e) for e in semantic_diags]
        if any(e.severity == "ERROR" for e in semantic_diags):
            return None, diagnostics
        return symtab, diagnostics

    def _add_problem(self, text):
        """One row of the Problems table: severity glyph and phase in the
        name column, position in the second, message in the third."""
        if text.startswith("✓"):
            self.problems.insert("", "end", text="✓", values=("", text.lstrip("✓ ")), tags=("ok",))
            return
        match = DIAGNOSTIC.match(text)
        if not match:  # never seen in practice - show the raw line rather than dropping it
            self.problems.insert("", "end", text="✖", values=("", text), tags=("error",))
            return
        tag, line, col, message = match.group("tag", "line", "col", "message")
        warning = tag.endswith("WARNING")
        phase = tag.rsplit(" ", 1)[0].title()
        where = f"Line {line}, Col {col}" if line else ""
        item = self.problems.insert("", "end", text=f"{'⚠' if warning else '✖'}  {phase}",
                                    values=(where, message.strip()),
                                    tags=("warn" if warning else "error",))
        self._problem_lines[item] = int(line) if line else None

    def check_code(self):
        symtab, diagnostics = self._analyze_buffer()
        self._last_symtab = symtab
        self.problems.delete(*self.problems.get_children())
        self._problem_lines = {}
        if not diagnostics:
            self._add_problem("✓ No lexical, syntax or semantic problems found.")
            self.write_console("Check complete: no problems found.\n", meta=True)
        else:
            for text in diagnostics:
                self._add_problem(text)
            self.write_console(f"Check complete: {len(diagnostics)} problem(s) found.\n", meta=True)
        warnings = sum(1 for text in diagnostics if "WARNING" in text)
        self._diag_counts = (len(diagnostics) - warnings, warnings)
        self._checked_src = self.source()
        self._refresh_diagnostics_status()
        self.bottom_tabs.badge(self.problems_frame, str(len(diagnostics)) if diagnostics else "")
        if symtab is None:
            self.bottom_tabs.select(self.problems_frame)
        self.refresh_symbols()
        return symtab is not None

    def go_to_problem(self, _event=None):
        selected = self.problems.selection()
        line = self._problem_lines.get(selected[0]) if selected else None
        if line:
            self.editor.mark_set("insert", f"{line}.0")
            self.editor.see("insert")
            self.editor.focus_set()

    def _on_tab_changed(self, _event=None):
        if self.bottom_tabs.select() != str(self.symbols_frame):
            return
        if self.source() != self._symbols_src:
            self._refresh_symbols_from_source()

    def _refresh_symbols_from_source(self):
        """Quiet variant of check_code for the Symbols tab: analyzes the
        buffer without touching the Problems list or the console."""
        self._last_symtab, _ = self._analyze_buffer()
        self.refresh_symbols()

    def show_symbols(self):
        # Selecting the tab triggers _on_tab_changed, which refreshes the
        # table if the buffer changed since it was last built.
        self.bottom_tabs.select(self.symbols_frame)

    @staticmethod
    def _function_locals(symtab, func_name, exclude):
        """Every local Symbol of `func_name`, in first-resolution order,
        pulled from the analyzer's node_symbol side table (keyed by node,
        so the same Symbol appears once per use - dedupe by ir_name)."""
        prefix = func_name + "."
        seen = {}
        for entry in symtab.node_symbol.values():
            symbols = entry if isinstance(entry, list) else [entry]
            for sym in symbols:
                if sym is None or getattr(sym, "storage", None) != "local":
                    continue
                if sym.ir_name.startswith(prefix) and sym.ir_name not in exclude and sym.ir_name not in seen:
                    seen[sym.ir_name] = sym
        return list(seen.values())

    def refresh_symbols(self):
        tree = self.symbols_tree
        tree.delete(*tree.get_children())
        self._symbol_lines = {}
        self._symbols_src = self.source()
        symtab = self._last_symtab
        if symtab is None:
            tree.insert("", "end", tags=("dim",),
                        text="— run Check (F7) with a clean program to populate the symbol table; "
                             "problems are listed in the Problems tab —")
            return
        from semantics import type_name
        if symtab.functions:
            functions_root = tree.insert("", "end", text="Functions", open=True, tags=("group",),
                                         values=("", f"{len(symtab.functions)} declared"))
            for name, sig in symtab.functions.items():
                params = ", ".join(
                    f"{pn}: {type_name(pt)}" + (" = …" if default is not None else "")
                    for pn, pt, default in zip(sig.param_names, sig.param_types, sig.param_defaults))
                item = tree.insert(functions_root, "end", text=f"{name}({params})",
                                   values=(type_name(sig.return_type), "function"))
                self._symbol_lines[item] = sig.node.line
                param_ir_names = {f"{name}.{pn}" for pn in sig.param_names}
                for pn, pt in zip(sig.param_names, sig.param_types):
                    tree.insert(item, "end", text=pn, values=(type_name(pt), "parameter"))
                for sym in self._function_locals(symtab, name, param_ir_names):
                    mutability = "var" if sym.mutable else "val"
                    tree.insert(item, "end", text=sym.name,
                                values=(type_name(sym.type), f"{mutability} · local · ir: {sym.ir_name}"))
        if symtab.structs:
            structs_root = tree.insert("", "end", text="Structs", open=True, tags=("group",),
                                       values=("", f"{len(symtab.structs)} declared"))
            for name, info in symtab.structs.items():
                item = tree.insert(structs_root, "end", text=f"struct {name}",
                                   values=("struct", f"{len(info.field_order)} field(s)"))
                for field_name in info.field_order:
                    tree.insert(item, "end", text=field_name,
                                values=(type_name(info.fields[field_name]), "field"))
        if symtab.typedefs:
            typedefs_root = tree.insert("", "end", text="Typedefs", open=True, tags=("group",),
                                        values=("", f"{len(symtab.typedefs)} declared"))
            for name, aliased in symtab.typedefs.items():
                tree.insert(typedefs_root, "end", text=name, values=(type_name(aliased), "typedef"))
        if symtab.globals:
            globals_root = tree.insert("", "end", text="Globals", open=True, tags=("group",),
                                       values=("", f"{len(symtab.globals)} declared"))
            for name, sym in symtab.globals.items():
                mutability = "var" if sym.mutable else "const"
                tree.insert(globals_root, "end", text=name,
                            values=(type_name(sym.type), f"{mutability} · global"))

    def go_to_symbol(self, _event=None):
        selected = self.symbols_tree.selection()
        if selected:
            line = self._symbol_lines.get(selected[0])
            if line:
                self.editor.mark_set("insert", f"{line}.0")
                self.editor.see("insert")
                self.editor.focus_set()

    # ------------------------------------------------------------------
    # Optimizer view
    # ------------------------------------------------------------------

    def show_optimization(self):
        """Run the IR optimizer on the current source and fill the Optimizer
        tab with `build_view()`'s payload: the original listing color-coded
        by each quad's fate, the optimized listing, and the transformation
        log. View-only - Run/Debug always execute the unoptimized IR, so
        breakpoints keep lining up with source lines."""
        if not self.check_code():
            return
        from ir import generate
        from optimizer import build_view, optimize
        from semantics import analyze
        ast, _, _ = parse_source(self.source())
        symtab, semantic_errors = analyze(ast)
        if any(error.severity == "ERROR" for error in semantic_errors):
            self.write_console("Semantic errors:\n" + "\n".join(str(error) for error in semantic_errors) + "\n")
            return
        quads, functions, types, structs = generate(ast, symtab)
        view = build_view(optimize(quads, functions, types), self.file_path.name if self.file_path else "untitled.src")
        for widget, rows, tagged in ((self.opt_before, view["original"], True), (self.opt_after, view["optimized"], False)):
            widget.config(state="normal")
            widget.delete("1.0", "end")
            for row in rows:
                tags = (row["status"],) if tagged and row["status"] != "kept" else ()
                widget.insert("end", f"{row['i']:>3}: {row['text']}\n", tags)
            widget.config(state="disabled")
        self.opt_log.delete(0, "end")
        for t in view["transformations"]:
            where = f"line {t['line']}: " if t["line"] is not None else ""
            self.opt_log.insert("end", f"[{t['technique']}] {where}{t['detail']}")
        stats = view["stats"]
        by = stats["by_technique"]
        self.optimizer_stats.config(text=(f"{stats['original_count']} → {stats['optimized_count']} quads  ·  "
                                          f"removed {stats['removed']}, rewritten {stats['rewritten']}  ·  "
                                          f"const-prop {by.get('constant-propagation', 0)}, "
                                          f"algebraic {by.get('algebraic-simplification', 0)}, "
                                          f"DCE {by.get('dead-code-elimination', 0)}"))
        self.bottom_tabs.select(self.optimizer_frame)

    def go_to_transformation(self, _event=None):
        match = re.search(r"line (\d+)", self.opt_log.get("active"))
        if match:
            self.editor.mark_set("insert", f"{match.group(1)}.0")
            self.editor.see("insert")
            self.editor.focus_set()

    # ------------------------------------------------------------------
    # Run / debug
    # ------------------------------------------------------------------

    def run_program(self, debug=False):
        if not self.check_code():
            return
        from ir import generate
        from interpreter import DebugStopped, IRExecutor
        from semantics import analyze
        ast, _, _ = parse_source(self.source())
        symtab, semantic_errors = analyze(ast)
        if any(error.severity == "ERROR" for error in semantic_errors):
            self.write_console("Semantic errors:\n" + "\n".join(str(error) for error in semantic_errors) + "\n")
            return
        # Retire any still-live previous run first: bump the serial (its
        # callbacks all check it and go inert), tell its executor to stop,
        # and unblock it if it's sitting in an input() wait.
        self.run_serial += 1
        serial = self.run_serial
        if self.executor is not None:
            self.executor.dbg_stop()
        self._abort_pending_input()
        self.console.config(state="normal")
        self.console.delete("1.0", "end")
        self.console.config(state="disabled")
        self.clear_current_line()
        self.callstack_list.delete(*self.callstack_list.get_children())
        self.vars_list.delete(*self.vars_list.get_children())
        self.trace_list.delete(0, "end")
        self.write_console("$ Debugging program…\n" if debug else "$ Running program…\n", meta=True)
        self.set_run_status("Debugging…" if debug else "Running…", THEME["accent"])
        self.stop_btn.config(state="normal")
        self.bottom_tabs.select(0)

        def live():
            return serial == self.run_serial

        def request_input(names):
            if not live():
                raise DebugStopped()
            request = {"names": names, "value": None, "event": threading.Event()}
            self.pending_input = request
            self.after(0, self.activate_terminal_input, request)
            request["event"].wait()
            if not live():
                raise DebugStopped()
            # readline()'s contract: a line always ends in "\n", so an empty
            # submit re-prompts (splits to no tokens) instead of reading as EOF.
            return request["value"] + "\n"

        def on_pause(line):
            if live():
                self.after(0, self.on_debug_pause, line)

        def on_line(line):
            if live():
                self.after(0, self.append_trace, line)

        class TerminalWriter:
            def __init__(self, ide):
                self.ide = ide

            def write(self, text):
                if text and live():
                    self.ide.after(0, self.ide.write_console, text)
                return len(text)

            def flush(self):
                pass

        def execute():
            try:
                with contextlib.redirect_stdout(TerminalWriter(self)):
                    quads, functions, types, structs = generate(ast, symtab)
                    executor = IRExecutor(quads, functions, types, structs, input_provider=request_input,
                                          breakpoints=self.breakpoints if debug else None,
                                          on_pause=on_pause if debug else None,
                                          on_line=on_line if debug else None)
                    self.executor = executor
                    executor.run()
                message = "\n$ Program stopped.\n" if executor._stop_requested else "\n$ Program finished.\n"
                if live():
                    self.after(0, self.finish_execution, message)
            except Exception as exc:
                if live():
                    self.after(0, self.finish_execution, f"\nRuntime error: {exc}\n")
        threading.Thread(target=execute, daemon=True).start()

    def debug_program(self):
        self.run_program(debug=True)

    def set_run_status(self, text, color=None):
        self.status_run.config(text=text, foreground=color or THEME["fg_dim"])

    def on_debug_pause(self, line):
        self.highlight_current_line(line)
        self.refresh_debug_panels()
        self.step_btn.config(state="normal")
        self.continue_btn.config(state="normal")
        self.set_run_status(f"Paused at line {line}", THEME["warn"])

    def append_trace(self, line):
        executor = self.executor
        if executor is None:
            return
        function = executor.call_names[-1] if executor.call_names else "(top level)"
        self.trace_list.insert("end", f"{function}  —  line {line}")
        self.trace_list.see("end")
        overflow = self.trace_list.size() - 500  # cap the log so a long-running loop can't grow it unbounded
        if overflow > 0:
            self.trace_list.delete(0, overflow - 1)

    def refresh_debug_panels(self):
        executor = self.executor
        stack = self.callstack_list
        stack.delete(*stack.get_children())
        for name in (list(reversed(executor.call_names)) or ["(top level)"]):
            stack.insert("", "end", text=name)
        variables = self.vars_list
        variables.delete(*variables.get_children())
        self._fill_scope(variables, "Locals", executor.frame)
        if executor.frame is not executor.globals:
            self._fill_scope(variables, "Globals", executor.globals)
        self.refresh_watches()

    def _fill_scope(self, tree, title, scope):
        group = tree.insert("", "end", text=title, open=True, tags=("group",))
        for name, value in scope.items():
            if not name.startswith("_t"):
                tree.insert(group, "end", text=self._display_name(name), values=(repr(value),))

    def add_watch(self):
        name = self.watch_entry.get().strip()
        if name and name not in self.watches:
            self.watches.append(name)
            self.watch_entry.delete(0, "end")
            self.refresh_watches()

    def remove_watch(self, _event=None):
        selected = self.watch_list.selection()
        if selected:
            del self.watches[self.watch_list.index(selected[0])]
            self.refresh_watches()

    def refresh_watches(self):
        self.watch_list.delete(*self.watch_list.get_children())
        for name in self.watches:
            value, found = self._lookup_watch(name)
            self.watch_list.insert("", "end", text=name,
                                   values=(repr(value) if found else "<not in scope>",),
                                   tags=("watched",) if found else ("dim",))

    def _lookup_watch(self, name):
        executor = self.executor
        if executor is None:
            return None, False
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

    def clear_current_line(self):
        self.editor.tag_remove("current_line", "1.0", "end")

    def debug_step(self):
        if not self.executor:
            return
        self.step_btn.config(state="disabled")
        self.continue_btn.config(state="disabled")
        self.executor.dbg_step()

    def debug_continue(self):
        if not self.executor:
            return
        self.clear_current_line()
        self.step_btn.config(state="disabled")
        self.continue_btn.config(state="disabled")
        self.set_run_status("Running…", THEME["accent"])
        self.executor.dbg_continue()

    def debug_stop(self):
        if self.executor:
            self.executor.dbg_stop()
            self._abort_pending_input()

    def _abort_pending_input(self):
        """Wake a worker blocked in `request_input` by resolving its pending
        request with an empty line (it then notices the stop request or the
        stale serial and unwinds via DebugStopped), and idle the terminal."""
        request, self.pending_input = self.pending_input, None
        if request is not None:
            request["value"] = ""
            request["event"].set()
        self.terminal_entry.config(state="disabled")
        self.terminal_send.config(state="disabled")

    def activate_terminal_input(self, request):
        # The terminal cursor is sufficient context; avoid exposing internal
        # IR-qualified variable names such as `main.a` to the user.
        self.terminal_entry.config(state="normal")
        self.terminal_send.config(state="normal")
        self.terminal_entry.delete(0, "end")
        self.terminal_entry.focus_set()

    def submit_terminal_input(self, _event=None):
        if not self.pending_input:
            return "break"
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
        self.write_console(message, meta=True)
        self.terminal_entry.config(state="disabled")
        self.terminal_send.config(state="disabled")
        self.pending_input = None
        self.clear_current_line()
        self.step_btn.config(state="disabled")
        self.continue_btn.config(state="disabled")
        self.stop_btn.config(state="disabled")
        self.set_run_status("")
        self.executor = None
        self.refresh_watches()

    def write_console(self, message, meta=False):
        self.console.config(state="normal")
        self.console.insert("end", message, ("meta",) if meta else ())
        self.console.see("end")
        self.console.config(state="disabled")

    # ------------------------------------------------------------------
    # File handling
    # ------------------------------------------------------------------

    def new_file(self):
        if self.dirty and not messagebox.askyesno("New file", "Discard unsaved changes?", parent=self):
            return
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", NEW_FILE)
        self.editor.mark_set("insert", "2.4")
        self.editor.edit_modified(False)
        self.file_path = None
        self.dirty = False
        self._refresh_all()

    def open_file(self):
        path = filedialog.askopenfilename(filetypes=[("Cetirim source", "*.src"), ("All files", "*.*")])
        if path:
            self.editor.delete("1.0", "end")
            self.editor.insert("1.0", Path(path).read_text(encoding="utf-8"))
            self.editor.edit_modified(False)
            self.file_path = Path(path)
            self.dirty = False
            self._refresh_all()

    def save_file(self):
        path = self.file_path or filedialog.asksaveasfilename(defaultextension=".src", filetypes=[("Cetirim source", "*.src")])
        if path:
            Path(path).write_text(self.source(), encoding="utf-8")
            self.file_path = Path(path)
            self.dirty = False
            self._refresh_all()


if __name__ == "__main__":
    CetirimIDE().mainloop()
