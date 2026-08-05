"""A standalone IDE for the CSC617M custom language. Run: python cetirim_ide.py"""
from __future__ import annotations

import contextlib
import re
import threading
import tkinter as tk
import tkinter.font as tkfont
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

# One place for every color in the IDE (VS Code Dark+-inspired). All widget
# construction and every ttk style reads from this dict - no hex literal may
# appear anywhere else in the file.
THEME = {
    # surfaces
    "bg":               "#1e1e1e",  # editor background
    "bg_dark":          "#181818",  # bottom panel / console wells
    "bg_side":          "#252526",  # sidebar, activity rail, tab strip, dialogs
    "bg_raise":         "#2d2d2d",  # buttons at rest, tree headings
    "bg_hover":         "#2a2d2e",  # hover states
    "bg_active":        "#37373d",  # pressed buttons / selected rows / scrollbar thumbs
    "titlebar":         "#323233",  # toolbar strip
    "border":           "#3c3c3c",
    # text
    "fg":               "#cccccc",
    "fg_code":          "#d4d4d4",
    "fg_bright":        "#ffffff",
    "fg_dim":           "#858585",
    "fg_faint":         "#6e7681",
    # accents
    "accent":           "#0e639c",  # primary buttons (VS Code blue)
    "accent_hover":     "#1177bb",
    "accent_press":     "#094771",
    "select_bg":        "#264f78",
    "statusbar":        "#007acc",
    "cursor":           "#aeafad",
    "current_line":     "#3a3d41",  # debugger paused-line highlight
    # gutter / breakpoints
    "gutter_fg":        "#858585",
    "gutter_fg_active": "#c6c6c6",
    "breakpoint":       "#e51400",
    # syntax (Dark+)
    "syn_keyword":      "#c586c0",
    "syn_type":         "#569cd6",
    "syn_string":       "#ce9178",
    "syn_number":       "#b5cea8",
    "syn_comment":      "#6a9955",
    "syn_function":     "#dcdcaa",
    "syn_error":        "#f44747",
    # panels
    "ok":               "#89d185",
    "warn":             "#cca700",
    "error":            "#f14c4c",
    "trace_fg":         "#9cdcfe",
    "watch_fg":         "#cca700",
    "ir_before_fg":     "#9da9b8",
    "ir_removed":       "#f14c4c",
    "ir_rewritten":     "#cca700",
    "ir_after_fg":      "#89d185",
    # find bar
    "find_match":       "#613214",
    "find_current":     "#9e6a03",
}


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
        self.font_section = tkfont.Font(family=ui, size=10, weight="bold")
        self.font_icon = tkfont.Font(family=ui, size=15)

    def _apply_theme(self):
        T = THEME
        self.configure(background=T["bg"])
        style = ttk.Style(self)
        style.theme_use("clam")  # the only built-in theme whose colors fully obey configure()
        # Setting light/dark/border colors at the root style is what kills
        # clam's 3D bevels everywhere; focuscolor=bg removes the dotted ring.
        style.configure(".", background=T["bg"], foreground=T["fg"], font=self.font_ui,
                        bordercolor=T["border"], lightcolor=T["bg"], darkcolor=T["bg"],
                        focuscolor=T["bg"], troughcolor=T["bg_dark"],
                        selectbackground=T["select_bg"], selectforeground=T["fg_bright"])
        style.configure("TFrame", background=T["bg"])
        style.configure("Side.TFrame", background=T["bg_side"])
        style.configure("Toolbar.TFrame", background=T["titlebar"])
        style.configure("TabBar.TFrame", background=T["bg_side"])
        style.configure("Status.TFrame", background=T["statusbar"])
        style.configure("Panel.TFrame", background=T["bg_dark"])
        style.configure("Find.TFrame", background=T["bg_raise"])
        style.configure("TLabel", background=T["bg"], foreground=T["fg"])
        style.configure("Title.TLabel", background=T["titlebar"], foreground=T["fg_bright"], font=self.font_ui_bold)
        style.configure("Toolbar.TLabel", background=T["titlebar"], foreground=T["fg_dim"], font=self.font_ui_sm)
        style.configure("Section.TLabel", background=T["bg"], foreground=T["fg_dim"], font=self.font_section)
        style.configure("Side.TLabel", background=T["bg_side"], foreground=T["fg_dim"], font=self.font_section)
        style.configure("EditorTab.TLabel", background=T["bg"], foreground=T["fg_bright"], font=self.font_ui_sm, padding=(14, 6))
        style.configure("Status.TLabel", background=T["statusbar"], foreground=T["fg_bright"], font=self.font_ui_sm, padding=(10, 3))
        style.configure("Panel.TLabel", background=T["bg_dark"], foreground=T["fg"], font=self.font_ui_sm)
        style.configure("PanelSection.TLabel", background=T["bg_dark"], foreground=T["fg_dim"], font=self.font_section)
        style.configure("PanelHint.TLabel", background=T["bg_dark"], foreground=T["fg_faint"], font=self.font_ui_sm)
        style.configure("Prompt.TLabel", background=T["bg_dark"], foreground=T["ok"], font=self.font_mono)
        style.configure("Find.TLabel", background=T["bg_raise"], foreground=T["fg_dim"], font=self.font_ui_sm)
        style.configure("TButton", background=T["bg_raise"], foreground=T["fg"], borderwidth=0, padding=(11, 6))
        style.map("TButton", background=[("active", T["bg_hover"]), ("pressed", T["bg_active"])],
                  foreground=[("disabled", T["fg_faint"])])
        style.configure("Accent.TButton", background=T["accent"], foreground=T["fg_bright"])
        style.map("Accent.TButton", background=[("active", T["accent_hover"]), ("pressed", T["accent_press"])],
                  foreground=[("disabled", T["fg_faint"])])
        style.configure("Toolbar.TButton", background=T["titlebar"], foreground=T["fg"], borderwidth=0, padding=(10, 5))
        style.map("Toolbar.TButton", background=[("active", T["bg_hover"]), ("pressed", T["bg_active"])],
                  foreground=[("disabled", T["fg_faint"])])
        style.configure("Run.Toolbar.TButton", background=T["accent"], foreground=T["fg_bright"])
        style.map("Run.Toolbar.TButton", background=[("active", T["accent_hover"]), ("pressed", T["accent_press"])])
        style.configure("Activity.TButton", background=T["bg_side"], foreground=T["fg_dim"], borderwidth=0,
                        padding=(0, 9), font=self.font_icon)
        style.map("Activity.TButton", background=[("active", T["bg_side"]), ("pressed", T["bg_side"])],
                  foreground=[("active", T["fg_bright"]), ("pressed", T["fg_bright"])])
        style.configure("Find.TButton", background=T["bg_raise"], foreground=T["fg"], borderwidth=0, padding=(5, 2))
        style.map("Find.TButton", background=[("active", T["bg_hover"]), ("pressed", T["bg_active"])])
        style.configure("TEntry", fieldbackground=T["bg_dark"], foreground=T["fg"], insertcolor=T["fg_bright"],
                        bordercolor=T["border"], lightcolor=T["border"], darkcolor=T["border"], padding=6)
        style.configure("TSpinbox", fieldbackground=T["bg_dark"], foreground=T["fg"], arrowcolor=T["fg_dim"],
                        bordercolor=T["border"], lightcolor=T["border"], darkcolor=T["border"])
        style.configure("Treeview", background=T["bg_side"], fieldbackground=T["bg_side"], foreground=T["fg"],
                        borderwidth=0, rowheight=24, font=self.font_ui_sm)
        style.map("Treeview", background=[("selected", T["bg_active"])], foreground=[("selected", T["fg_bright"])])
        style.configure("Panel.Treeview", background=T["bg_dark"], fieldbackground=T["bg_dark"])
        style.configure("Treeview.Heading", background=T["bg_raise"], foreground=T["fg_dim"], borderwidth=0,
                        relief="flat", font=self.font_ui_sm)
        style.map("Treeview.Heading", background=[("active", T["bg_hover"])])
        style.configure("TNotebook", background=T["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=T["bg"], foreground=T["fg_dim"], padding=(12, 6), borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", T["bg"])], foreground=[("selected", T["fg_bright"])])
        # Flat panel tabs: same background as the panel, dim label that turns
        # white when selected (clam has no per-tab underline element).
        style.configure("Panel.TNotebook", background=T["bg_dark"], borderwidth=0, tabmargins=(8, 4, 8, 0))
        style.configure("Panel.TNotebook.Tab", background=T["bg_dark"], foreground=T["fg_dim"], padding=(10, 5),
                        borderwidth=0, font=self.font_ui_sm, bordercolor=T["bg_dark"],
                        lightcolor=T["bg_dark"], darkcolor=T["bg_dark"], focuscolor=T["bg_dark"])
        # clam's built-in Tab map brightens lightcolor/border on selection -
        # every color has to be pinned per state or a pale box appears.
        flat_tab = [("selected", T["bg_dark"]), ("active", T["bg_dark"]), ("!selected", T["bg_dark"])]
        style.map("Panel.TNotebook.Tab", background=flat_tab, lightcolor=flat_tab, darkcolor=flat_tab,
                  bordercolor=flat_tab, focuscolor=flat_tab,
                  foreground=[("selected", T["fg_bright"]), ("active", T["fg"])])
        style.configure("TSeparator", background=T["border"])
        style.configure("TPanedwindow", background=T["bg"])
        style.configure("Sash", sashthickness=6, gripcount=0)
        # Arrow-less flat scrollbars (thickness tracks arrowsize in clam).
        for orient, sticky in (("Vertical", "ns"), ("Horizontal", "ew")):
            style.layout(f"Flat.{orient}.TScrollbar",
                         [(f"{orient}.Scrollbar.trough", {"sticky": sticky, "children":
                             [(f"{orient}.Scrollbar.thumb", {"expand": 1, "sticky": "nswe"})]})])
            style.configure(f"Flat.{orient}.TScrollbar", troughcolor=T["bg_dark"], background=T["bg_active"],
                            bordercolor=T["bg_dark"], lightcolor=T["bg_active"], darkcolor=T["bg_active"],
                            gripcount=0, arrowsize=11, relief="flat")
            style.map(f"Flat.{orient}.TScrollbar", background=[("active", T["fg_faint"])])

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        self._make_menu()
        self._build_toolbar()
        self._build_statusbar()  # packed side="bottom" before the body so shrinking never squeezes it out
        self._build_layout()
        self._build_sidebar()
        self._build_editor()
        self._build_panel()
        self._bind_shortcuts()

    def _build_toolbar(self):
        toolbar = ttk.Frame(self, padding=(14, 8), style="Toolbar.TFrame")
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="CETIRIM", style="Title.TLabel").pack(side="left", padx=(0, 18))
        groups = [
            [("New", self.new_file, "Toolbar.TButton"),
             ("Open", self.open_file, "Toolbar.TButton"),
             ("Save", self.save_file, "Toolbar.TButton")],
            [("▶ Run", self.run_program, "Run.Toolbar.TButton"),
             ("Debug", self.debug_program, "Toolbar.TButton")],
            [("✓ Check", self.check_code, "Toolbar.TButton"),
             ("⚡ Optimize", self.show_optimization, "Toolbar.TButton")],
        ]
        for i, group in enumerate(groups):
            if i:
                ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8, pady=3)
            for text, callback, button_style in group:
                ttk.Button(toolbar, text=text, command=callback, style=button_style).pack(side="left", padx=(0, 4))
        self.font_size = tk.IntVar(value=13)
        ttk.Spinbox(toolbar, from_=9, to=24, width=4, textvariable=self.font_size,
                    command=self.change_font).pack(side="right")
        ttk.Label(toolbar, text="Font", style="Toolbar.TLabel").pack(side="right", padx=(0, 6))

    def _build_statusbar(self):
        bar = ttk.Frame(self, style="Status.TFrame")
        bar.pack(side="bottom", fill="x")
        self.status = ttk.Label(bar, style="Status.TLabel", anchor="w")
        self.status.pack(side="left")
        self.status_run = ttk.Label(bar, style="Status.TLabel", anchor="w")
        self.status_run.pack(side="left")
        self.status_lang = ttk.Label(bar, text="Cetirim", style="Status.TLabel", anchor="e")
        self.status_lang.pack(side="right")
        self.status_pos = ttk.Label(bar, text="Ln 1, Col 1", style="Status.TLabel", anchor="e")
        self.status_pos.pack(side="right")

    def _build_layout(self):
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.activity_bar = ttk.Frame(body, width=44, style="Side.TFrame")
        self.activity_bar.pack(side="left", fill="y")
        self.activity_bar.pack_propagate(False)
        for glyph, command in (("▤", self.toggle_sidebar),
                               ("▣", self.toggle_panel),
                               ("⚡", lambda: self.bottom_tabs.select(self.optimizer_frame))):
            ttk.Button(self.activity_bar, text=glyph, command=command, style="Activity.TButton",
                       takefocus=0).pack(fill="x", pady=(4, 0))
        self.vsplit = ttk.PanedWindow(body, orient="vertical")
        self.vsplit.pack(fill="both", expand=True)
        self.hsplit = ttk.PanedWindow(self.vsplit, orient="horizontal")
        self._sidebar = ttk.Frame(self.hsplit, width=230, style="Side.TFrame")
        self._editor_area = ttk.Frame(self.hsplit)
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
                self.vsplit.sashpos(0, height - 290)
            self.hsplit.sashpos(0, 230)
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
        ttk.Label(self._sidebar, text="OUTLINE", style="Side.TLabel").pack(anchor="w", padx=12, pady=(10, 6))
        self.outline = ttk.Treeview(self._sidebar, show="tree", selectmode="browse")
        self.outline.pack(fill="both", expand=True, padx=(4, 0))
        self.outline.bind("<<TreeviewSelect>>", self.go_to_outline)

    def _build_editor(self):
        T = THEME
        area = self._editor_area
        tabbar = ttk.Frame(area, style="TabBar.TFrame")
        self.editor_tab = ttk.Label(tabbar, style="EditorTab.TLabel")
        self.editor_tab.pack(side="left")
        self.line_numbers = tk.Text(area, width=6, height=1, padx=8, pady=8, takefocus=0, state="disabled",
                                    background=T["bg"], foreground=T["gutter_fg"], borderwidth=0,
                                    highlightthickness=0, font=self.font_editor, cursor="arrow")
        self.line_numbers.tag_configure("breakpoint", foreground=T["breakpoint"])
        self.line_numbers.tag_configure("active_ln", foreground=T["gutter_fg_active"])
        self.line_numbers.bind("<Button-1>", self.toggle_breakpoint)
        self.line_numbers.bind("<MouseWheel>", self._gutter_scroll)
        vscroll = ttk.Scrollbar(area, orient="vertical", style="Flat.Vertical.TScrollbar")
        self.editor = tk.Text(area, height=1, undo=True, wrap="none", borderwidth=0, highlightthickness=0,
                              padx=12, pady=8, background=T["bg"], foreground=T["fg_code"],
                              insertbackground=T["cursor"], selectbackground=T["select_bg"],
                              font=self.font_editor,
                              yscrollcommand=lambda a, b: self._scroll_both(a, b, vscroll))
        vscroll.config(command=self._scroll_editor)
        self.editor_hscroll = ttk.Scrollbar(area, orient="horizontal", style="Flat.Horizontal.TScrollbar",
                                            command=self.editor.xview)
        self.editor.config(xscrollcommand=self.editor_hscroll.set)
        tabbar.grid(row=0, column=0, columnspan=3, sticky="ew")
        self.line_numbers.grid(row=1, column=0, sticky="ns")
        self.editor.grid(row=1, column=1, sticky="nsew")
        vscroll.grid(row=1, column=2, sticky="ns")
        self.editor_hscroll.grid(row=2, column=0, columnspan=2, sticky="ew")
        area.grid_rowconfigure(1, weight=1)
        area.grid_columnconfigure(1, weight=1)
        self._configure_tags()
        self._build_find_bar()

    def _build_find_bar(self):
        T = THEME
        # A 1px THEME border via an outer tk.Frame; shown/hidden with place()
        # so the editor never reflows.
        self.find_bar = tk.Frame(self._editor_area, background=T["border"], padx=1, pady=1)
        inner = ttk.Frame(self.find_bar, padding=6, style="Find.TFrame")
        inner.pack(fill="both", expand=True)
        row1 = ttk.Frame(inner, style="Find.TFrame")
        row1.pack(fill="x")
        self.find_entry = ttk.Entry(row1, width=22, font=self.font_mono_sm)
        self.find_entry.pack(side="left")
        self.find_count = ttk.Label(row1, text="", width=10, style="Find.TLabel")
        self.find_count.pack(side="left", padx=(8, 4))
        ttk.Button(row1, text="↑", width=2, command=self.find_prev, style="Find.TButton").pack(side="left")
        ttk.Button(row1, text="↓", width=2, command=self.find_next, style="Find.TButton").pack(side="left", padx=(2, 0))
        ttk.Button(row1, text="✕", width=2, command=self.hide_find_bar, style="Find.TButton").pack(side="left", padx=(6, 0))
        row2 = ttk.Frame(inner, style="Find.TFrame")
        row2.pack(fill="x", pady=(4, 0))
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
        self.bottom_tabs = ttk.Notebook(self._panel, style="Panel.TNotebook")
        self.bottom_tabs.pack(fill="both", expand=True)
        frames = {}
        for key, label in (("output", "OUTPUT"), ("problems", "PROBLEMS"), ("debug", "DEBUG"),
                           ("trace", "TRACE"), ("symbols", "SYMBOLS"), ("optimizer", "OPTIMIZER")):
            frame = ttk.Frame(self.bottom_tabs, style="Panel.TFrame")
            self.bottom_tabs.add(frame, text=f"  {label}  ")
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
        self.bottom_tabs.bind("<<NotebookTabChanged>>", self._on_tab_changed)

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

    def _build_output_tab(self, frame):
        T = THEME
        # The input row packs first (side="bottom") so a short panel squeezes
        # the console, never the prompt.
        terminal_input = ttk.Frame(frame, padding=(8, 6), style="Panel.TFrame")
        terminal_input.pack(side="bottom", fill="x")
        console_wrap = ttk.Frame(frame, style="Panel.TFrame")
        console_wrap.pack(fill="both", expand=True)
        self.console = tk.Text(console_wrap, height=10, background=T["bg_dark"], foreground=T["fg"],
                               insertbackground=T["cursor"], font=self.font_mono, state="disabled",
                               borderwidth=0, highlightthickness=0, padx=12, pady=8)
        self._scrolled(self.console)
        self.console.pack(side="left", fill="both", expand=True)
        self.terminal_prompt = ttk.Label(terminal_input, text="❯", style="Prompt.TLabel")
        self.terminal_prompt.pack(side="left", padx=(0, 6))
        self.terminal_entry = ttk.Entry(terminal_input, font=self.font_mono, state="disabled")
        self.terminal_entry.pack(side="left", fill="x", expand=True)
        self.terminal_entry.bind("<Return>", self.submit_terminal_input)
        self.terminal_send = ttk.Button(terminal_input, text="Send", command=self.submit_terminal_input,
                                        state="disabled", style="Accent.TButton")
        self.terminal_send.pack(side="left", padx=(6, 0))

    def _build_problems_tab(self, frame):
        self.problems = self._scrolled_list(frame, height=9, font=self.font_mono_sm)
        self.problems.bind("<Double-1>", self.go_to_problem)

    def _build_debug_tab(self, frame):
        debug_toolbar = ttk.Frame(frame, padding=(8, 6), style="Panel.TFrame")
        debug_toolbar.pack(fill="x")
        self.step_btn = ttk.Button(debug_toolbar, text="⏵ Step", command=self.debug_step, state="disabled")
        self.step_btn.pack(side="left", padx=(0, 5))
        self.continue_btn = ttk.Button(debug_toolbar, text="⏭ Continue", command=self.debug_continue, state="disabled")
        self.continue_btn.pack(side="left", padx=(0, 5))
        self.stop_btn = ttk.Button(debug_toolbar, text="⏹ Stop", command=self.debug_stop, state="disabled")
        self.stop_btn.pack(side="left")
        ttk.Label(debug_toolbar, text="Click a line number to toggle a breakpoint.",
                  style="PanelHint.TLabel").pack(side="left", padx=(14, 0))
        panes = ttk.Frame(frame, padding=(8, 0, 8, 8), style="Panel.TFrame")
        panes.pack(fill="both", expand=True)
        call_col = ttk.Frame(panes, style="Panel.TFrame")
        call_col.pack(side="left", fill="both", padx=(0, 12))
        ttk.Label(call_col, text="CALL STACK", style="PanelSection.TLabel").pack(anchor="w", pady=(0, 4))
        self.callstack_list = self._scrolled_list(call_col, width=22, height=8)
        vars_col = ttk.Frame(panes, style="Panel.TFrame")
        vars_col.pack(side="left", fill="both", expand=True, padx=(0, 12))
        ttk.Label(vars_col, text="VARIABLES", style="PanelSection.TLabel").pack(anchor="w", pady=(0, 4))
        self.vars_list = self._scrolled_list(vars_col, height=8)
        watch_col = ttk.Frame(panes, style="Panel.TFrame")
        watch_col.pack(side="left", fill="both", expand=True)
        ttk.Label(watch_col, text="WATCH", style="PanelSection.TLabel").pack(anchor="w", pady=(0, 4))
        watch_entry_row = ttk.Frame(watch_col, style="Panel.TFrame")
        watch_entry_row.pack(fill="x", pady=(0, 4))
        self.watch_entry = ttk.Entry(watch_entry_row, font=self.font_mono_sm)
        self.watch_entry.pack(side="left", fill="x", expand=True)
        self.watch_entry.bind("<Return>", lambda e: self.add_watch())
        ttk.Button(watch_entry_row, text="Add", command=self.add_watch).pack(side="left", padx=(5, 0))
        ttk.Label(watch_col, text="Double-click a watch to remove it.",
                  style="PanelHint.TLabel").pack(anchor="w")
        self.watch_list = self._scrolled_list(watch_col, height=6, foreground=THEME["watch_fg"])
        self.watch_list.bind("<Double-1>", self.remove_watch)

    def _build_trace_tab(self, frame):
        inner = ttk.Frame(frame, padding=8, style="Panel.TFrame")
        inner.pack(fill="both", expand=True)
        self.trace_list = self._scrolled_list(inner, foreground=THEME["trace_fg"])

    def _build_symbols_tab(self, frame):
        symbols_toolbar = ttk.Frame(frame, padding=(8, 6), style="Panel.TFrame")
        symbols_toolbar.pack(fill="x")
        ttk.Button(symbols_toolbar, text="⟳ Refresh", command=self._refresh_symbols_from_source).pack(side="left")
        ttk.Label(symbols_toolbar,
                  text="The semantic analyzer's symbol table: functions, structs, typedefs and globals.",
                  style="PanelHint.TLabel").pack(side="left", padx=(12, 0))
        wrap = ttk.Frame(frame, style="Panel.TFrame")
        wrap.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.symbols_tree = ttk.Treeview(wrap, columns=("type", "detail"), style="Panel.Treeview", height=8)
        self.symbols_tree.heading("#0", text="Name", anchor="w")
        self.symbols_tree.heading("type", text="Type", anchor="w")
        self.symbols_tree.heading("detail", text="Details", anchor="w")
        self.symbols_tree.column("#0", width=280, stretch=True)
        self.symbols_tree.column("type", width=180, stretch=False)
        self.symbols_tree.column("detail", width=360, stretch=True)
        self.symbols_tree.tag_configure("dim", foreground=THEME["fg_faint"])
        self._scrolled(self.symbols_tree)
        self.symbols_tree.pack(side="left", fill="both", expand=True)
        self.symbols_tree.bind("<Double-1>", self.go_to_symbol)
        self.refresh_symbols()

    def _build_optimizer_tab(self, frame):
        T = THEME
        optimizer_toolbar = ttk.Frame(frame, padding=(8, 6), style="Panel.TFrame")
        optimizer_toolbar.pack(fill="x")
        ttk.Button(optimizer_toolbar, text="⚡ Optimize", command=self.show_optimization,
                   style="Accent.TButton").pack(side="left")
        self.optimizer_stats = ttk.Label(optimizer_toolbar,
                                         text="Press Optimize to compare the IR before and after optimization.",
                                         style="Panel.TLabel")
        self.optimizer_stats.pack(side="left", padx=(14, 0))
        optimizer_panes = ttk.Frame(frame, padding=(8, 0, 8, 8), style="Panel.TFrame")
        optimizer_panes.pack(fill="both", expand=True)
        before_col = ttk.Frame(optimizer_panes, style="Panel.TFrame")
        before_col.pack(side="left", fill="both", expand=True, padx=(0, 12))
        ttk.Label(before_col, text="ORIGINAL IR", style="PanelSection.TLabel").pack(anchor="w", pady=(0, 4))
        before_wrap = ttk.Frame(before_col, style="Panel.TFrame")
        before_wrap.pack(fill="both", expand=True)
        self.opt_before = tk.Text(before_wrap, height=8, state="disabled", wrap="none",
                                  background=T["bg_dark"], foreground=T["ir_before_fg"],
                                  font=self.font_mono_sm, borderwidth=0, highlightthickness=0)
        self._scrolled(self.opt_before, hbar=True)
        self.opt_before.pack(side="left", fill="both", expand=True)
        self.opt_before.tag_configure("removed", foreground=T["ir_removed"])
        self.opt_before.tag_configure("rewritten", foreground=T["ir_rewritten"])
        after_col = ttk.Frame(optimizer_panes, style="Panel.TFrame")
        after_col.pack(side="left", fill="both", expand=True, padx=(0, 12))
        ttk.Label(after_col, text="OPTIMIZED IR", style="PanelSection.TLabel").pack(anchor="w", pady=(0, 4))
        after_wrap = ttk.Frame(after_col, style="Panel.TFrame")
        after_wrap.pack(fill="both", expand=True)
        self.opt_after = tk.Text(after_wrap, height=8, state="disabled", wrap="none",
                                 background=T["bg_dark"], foreground=T["ir_after_fg"],
                                 font=self.font_mono_sm, borderwidth=0, highlightthickness=0)
        self._scrolled(self.opt_after, hbar=True)
        self.opt_after.pack(side="left", fill="both", expand=True)
        log_col = ttk.Frame(optimizer_panes, style="Panel.TFrame")
        log_col.pack(side="left", fill="both", expand=True)
        ttk.Label(log_col, text="TRANSFORMATIONS", style="PanelSection.TLabel").pack(anchor="w", pady=(0, 4))
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
        menu.add_cascade(label="View", menu=view_menu)
        tools = tk.Menu(menu, tearoff=False)
        for label, callback, key in (("Run", self.run_program, "F5"), ("Debug", self.debug_program, "F6"),
                                     ("Check code", self.check_code, "F7"),
                                     ("Optimize IR", self.show_optimization, acc("Shift-O")),
                                     ("Symbol table", self.show_symbols, "")):
            tools.add_command(label=label, command=callback, accelerator=key)
        menu.add_cascade(label="Tools", menu=tools)
        self.config(menu=menu)

    def _bind_shortcuts(self):
        self.editor.bind("<<Modified>>", self.on_modified)
        self.editor.bind("<KeyRelease>", self.on_key_release)
        self.editor.bind("<ButtonRelease-1>", lambda e: self._schedule_refresh())
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
            "<F5>": lambda e: self.run_program(),
            "<F6>": lambda e: self.debug_program(),
            "<F7>": lambda e: self.check_code(),
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
        self.editor.tag_configure("current_line", background=T["current_line"])
        self.editor.tag_configure("find_match", background=T["find_match"])
        self.editor.tag_configure("find_current", background=T["find_current"], foreground=T["fg_bright"])
        self.editor.tag_raise("find_match")
        self.editor.tag_raise("find_current")
        self.editor.tag_raise("sel")

    def change_font(self):
        self.font_editor.configure(size=self.font_size.get())
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
        self._schedule_refresh()

    def _schedule_refresh(self):
        if self.refresh_id:
            self.after_cancel(self.refresh_id)
        self.refresh_id = self.after(180, self._refresh_all)

    def _refresh_all(self):
        self.refresh_id = None
        self.highlight()
        self.refresh_line_numbers()
        self.refresh_outline()
        self._update_editor_tab()
        line, col = self.editor.index("insert").split(".")
        self.status.config(text=self.file_path.name if self.file_path else "Untitled.src")
        self.status_pos.config(text=f"Ln {line}, Col {int(col) + 1}")

    def _update_editor_tab(self):
        name = self.file_path.name if self.file_path else "Untitled.src"
        self.editor_tab.config(text=f"{name} ●" if self.dirty else name)

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
        self.line_numbers.config(state="normal", width=digits + 3)
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

    def refresh_outline(self):
        self.outline.delete(*self.outline.get_children())
        src = self.source()
        pattern = re.compile(r"^\s*(?:struct\s+(\w+)|typedef\s+.+?\s+(\w+)\s*;|(?:int|float|char|string|bool|void)\s+(\w+)\s*\()", re.M)
        for match in pattern.finditer(src):
            name = next(item for item in match.groups() if item)
            kind = "struct" if match.group(1) else "typedef" if match.group(2) else "function"
            line = src[:match.start()].count("\n") + 1
            self.outline.insert("", "end", text=f"{kind}  {name}", values=(line,))

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
        self.find_bar.place(in_=self.editor, relx=1.0, x=-16, y=8, anchor="ne")
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

    def show_suggestions(self, _event=None):
        self.show_templates()
        return "break"

    def show_templates(self):
        T = THEME
        win = tk.Toplevel(self)
        win.title("Templates & autocomplete")
        win.transient(self)
        win.geometry("420x390")
        win.resizable(False, False)
        win.configure(bg=T["bg_side"])
        win.grab_set()
        header = tk.Frame(win, bg=T["titlebar"], height=62)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="INSERT A CODE TEMPLATE", bg=T["titlebar"], fg=T["fg_bright"],
                 font=self.font_section).pack(anchor="w", padx=16, pady=(12, 0))
        tk.Label(header, text="Templates and language keywords", bg=T["titlebar"], fg=T["fg_dim"],
                 font=self.font_ui_sm).pack(anchor="w", padx=16)
        body = tk.Frame(win, bg=T["bg_side"])
        body.pack(fill="both", expand=True, padx=12, pady=12)
        box = tk.Listbox(body, font=self.font_mono_sm, height=13, bg=T["bg_dark"], fg=T["fg"],
                         selectbackground=T["select_bg"], selectforeground=T["fg_bright"],
                         highlightthickness=1, highlightbackground=T["border"], highlightcolor=T["accent"],
                         borderwidth=0, activestyle="none")
        for item in list(TEMPLATES) + sorted(KEYWORDS):
            box.insert("end", item)
        box.pack(fill="both", expand=True)
        box.selection_set(0)

        def insert(_event=None):
            self.insert_template(box.get("active"))
            win.destroy()

        actions = tk.Frame(win, bg=T["bg_side"])
        actions.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(actions, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(actions, text="Insert", command=insert, style="Accent.TButton").pack(side="right", padx=(0, 8))
        box.bind("<Double-1>", insert)
        box.bind("<Return>", insert)
        win.bind("<Escape>", lambda e: win.destroy())
        box.focus_set()

    def themed_prompt(self, title, message, initial=""):
        """A small themed replacement for Tk's platform-coloured askstring."""
        T = THEME
        result = {"value": None}
        win = tk.Toplevel(self)
        win.title(title)
        win.transient(self)
        win.configure(bg=T["bg_side"])
        win.resizable(False, False)
        win.grab_set()
        header = tk.Frame(win, bg=T["titlebar"], height=52)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text=title.upper(), bg=T["titlebar"], fg=T["fg_bright"],
                 font=self.font_section).pack(anchor="w", padx=16, pady=15)
        body = tk.Frame(win, bg=T["bg_side"])
        body.pack(fill="both", expand=True, padx=16, pady=16)
        tk.Label(body, text=message, bg=T["bg_side"], fg=T["fg"], font=self.font_ui_sm,
                 wraplength=360, justify="left").pack(anchor="w", pady=(0, 9))
        entry = tk.Entry(body, width=42, bg=T["bg_dark"], fg=T["fg"], insertbackground=T["cursor"],
                         relief="flat", highlightthickness=1, highlightbackground=T["border"],
                         highlightcolor=T["accent"], font=self.font_mono_sm)
        entry.insert(0, initial)
        entry.pack(fill="x", ipady=6)
        actions = tk.Frame(body, bg=T["bg_side"])
        actions.pack(fill="x", pady=(15, 0))

        def accept(_event=None):
            result["value"] = entry.get()
            win.destroy()

        ttk.Button(actions, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(actions, text="OK", command=accept, style="Accent.TButton").pack(side="right", padx=(0, 8))
        entry.bind("<Return>", accept)
        win.bind("<Escape>", lambda e: win.destroy())
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
        self.write_console(f"Refactoring complete: renamed {count} occurrence(s) of '{old}' to '{new}'.\n")
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
        T = THEME
        if text.startswith("✓"):
            color = T["ok"]
        elif "WARNING" in text:
            color, text = T["warn"], "⚠ " + text
        else:
            color, text = T["error"], "✖ " + text
        self.problems.insert("end", text)
        self.problems.itemconfig("end", foreground=color)

    def check_code(self):
        symtab, diagnostics = self._analyze_buffer()
        self._last_symtab = symtab
        self.problems.delete(0, "end")
        if not diagnostics:
            self._add_problem("✓ No lexical, syntax or semantic problems found.")
            self.write_console("Check complete: no problems found.\n")
        else:
            for text in diagnostics:
                self._add_problem(text)
            self.write_console(f"Check complete: {len(diagnostics)} problem(s) found.\n")
        if symtab is None:
            self.bottom_tabs.select(self.problems_frame)
        self.refresh_symbols()
        return symtab is not None

    def go_to_problem(self, _event=None):
        match = re.search(r"Line (\d+)", self.problems.get("active"))
        if match:
            self.editor.mark_set("insert", f"{match.group(1)}.0")
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
                        text="— run ✓ Check (F7) with a clean program to populate the symbol table; "
                             "problems are listed in the Problems tab —")
            return
        from semantics import type_name
        if symtab.functions:
            functions_root = tree.insert("", "end", text="Functions", open=True,
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
            structs_root = tree.insert("", "end", text="Structs", open=True,
                                       values=("", f"{len(symtab.structs)} declared"))
            for name, info in symtab.structs.items():
                item = tree.insert(structs_root, "end", text=f"struct {name}",
                                   values=("struct", f"{len(info.field_order)} field(s)"))
                for field_name in info.field_order:
                    tree.insert(item, "end", text=field_name,
                                values=(type_name(info.fields[field_name]), "field"))
        if symtab.typedefs:
            typedefs_root = tree.insert("", "end", text="Typedefs", open=True,
                                        values=("", f"{len(symtab.typedefs)} declared"))
            for name, aliased in symtab.typedefs.items():
                tree.insert(typedefs_root, "end", text=name, values=(type_name(aliased), "typedef"))
        if symtab.globals:
            globals_root = tree.insert("", "end", text="Globals", open=True,
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
        self.optimizer_stats.config(text=(f"{stats['original_count']} → {stats['optimized_count']} quads  |  "
                                          f"removed {stats['removed']}, rewritten {stats['rewritten']}  |  "
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
        self.callstack_list.delete(0, "end")
        self.vars_list.delete(0, "end")
        self.trace_list.delete(0, "end")
        self.write_console("$ Debugging program…\n" if debug else "$ Running program…\n")
        self.status_run.config(text="Debugging…" if debug else "Running…")
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

    def on_debug_pause(self, line):
        self.highlight_current_line(line)
        self.refresh_debug_panels()
        self.step_btn.config(state="normal")
        self.continue_btn.config(state="normal")
        self.status_run.config(text=f"Paused at line {line}")

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
        self.callstack_list.delete(0, "end")
        for name in (list(reversed(executor.call_names)) or ["(top level)"]):
            self.callstack_list.insert("end", name)
        self.vars_list.delete(0, "end")
        self.vars_list.insert("end", "-- Locals --")
        for name, value in executor.frame.items():
            if not name.startswith("_t"):
                self.vars_list.insert("end", f"{self._display_name(name)} = {value!r}")
        if executor.frame is not executor.globals:
            self.vars_list.insert("end", "-- Globals --")
            for name, value in executor.globals.items():
                if not name.startswith("_t"):
                    self.vars_list.insert("end", f"{self._display_name(name)} = {value!r}")
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
        self.status_run.config(text="Running…")
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
        self.write_console(message)
        self.terminal_entry.config(state="disabled")
        self.terminal_send.config(state="disabled")
        self.pending_input = None
        self.clear_current_line()
        self.step_btn.config(state="disabled")
        self.continue_btn.config(state="disabled")
        self.stop_btn.config(state="disabled")
        self.status_run.config(text="")
        self.executor = None
        self.refresh_watches()

    def write_console(self, message):
        self.console.config(state="normal")
        self.console.insert("end", message)
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
