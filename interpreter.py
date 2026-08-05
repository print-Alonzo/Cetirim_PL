"""Stack-machine VM that executes the quadruples produced by ir.py.

Phase handoff: this is the last phase in the pipeline. It receives
`(quads, functions, var_types, structs)` from `ir.py`'s `generate()` and
just runs them - there is no further lowering after this. `main()` below
wires the whole pipeline together for the `python interpreter.py <file>`
CLI: scan -> parse -> analyze -> generate -> execute.

Frame model: every active call gets its own dict keyed by ir_name (the
globally-unique names semantics.py assigned - "main.i", "main.i$2" on
shadowing, temps as "_t7"), plus a single shared `self.globals` dict for
top-level `const`s. Because every name is unique across the whole program by
construction, a plain "is this name currently a key in globals?" check
(`resolve`/`set_var` below) is enough to route reads/writes to the right
dict - no name can ever mean two different things depending on which frame
is active, which is what let the old flat-frame VM confuse a local with a
same-named global.

Exceptions: `TRY_PUSH` records `(handler_pc, frame, call_stack_depth)`.
`THROW`/`RETHROW` unwind `call_stack` back to that depth and resume in the
recorded frame. There's no separate runtime notion of "finally" - ir.py's
gen_try already compiled two copies of the finally body (one inline after a
normal catch, one before RETHROW), so the VM only ever needs to know
"where's the nearest still-registered handler".

Arrays are plain Python lists and structs are dict-like StructValues, both
stored directly as the value in a frame slot - passing one to a function or
assigning it just copies the Python reference, which is exactly call-by-
reference for aggregates and call-by-value for scalars, matching the
project's design assumptions, for free.
"""

import argparse
import operator
import sys
import threading

from ir import Const, IRGenError, _div, _mod, format_quads, generate
from parser import parse_source
from semantics import analyze

DEFAULTS = {"int": 0, "float": 0.0, "bool": False, "string": "", "char": "\0"}


class RuntimeVMError(Exception):
    """A malformed-at-runtime condition (division by zero, an
    out-of-bounds array index, a bad `input()` parse, ...) that gets
    reported as a clean `[RUNTIME ERROR]` line instead of a raw Python
    traceback. Carries the source `line` it happened on, when known."""

    def __init__(self, message, line=None):
        super().__init__(message)
        self.message = message
        self.line = line

    def __str__(self):
        return self.message


class UncaughtException(Exception):
    """A `throw`n value that reached `_unwind` with no `TRY_PUSH`-registered
    handler left to catch it - i.e. it propagated all the way out of
    `main`. Reported the same way as `RuntimeVMError`, just with a
    different message shape."""

    def __init__(self, value, line=None):
        super().__init__(str(value))
        self.value = value
        self.line = line


class DebugStopped(Exception):
    """Raised internally to unwind `run()` when the debugger's Stop control
    is used. Caught in `run()` and treated as a plain (non-error) return -
    the program simply didn't finish, rather than having crashed."""


class StructValue(dict):
    """A struct instance: a plain dict of field values, plus the struct's
    type name (kept off to the side so `Name{field: value}` formatting
    doesn't need runtime type inference)."""

    __slots__ = ("type_name",)

    def __init__(self, type_name, fields):
        super().__init__(fields)
        self.type_name = type_name


# DIV/MOD's C-style truncated semantics live in ir.py so `optimizer.py`'s
# constant folder can fold a DIV/MOD quad through the exact same code this
# VM executes it with - see ir._div's docstring.
BINARY_OPS = {
    "ADD": operator.add, "SUB": operator.sub, "MUL": operator.mul, "DIV": _div, "MOD": _mod,
    "EQ": operator.eq, "NE": operator.ne, "LT": operator.lt, "GT": operator.gt,
    "LE": operator.le, "GE": operator.ge,
}
UNARY_OPS = {"UMINUS": operator.neg, "UPLUS": lambda v: v, "NOT": operator.not_}

# Python exceptions a malformed-at-runtime program can trigger (bad array
# index, div by zero, missing dict key, bad literal coercion, ...). Caught
# once at the top of run() and reported as a clean [RUNTIME ERROR], never as
# a raw traceback.
_NATIVE_RUNTIME_ERRORS = (ZeroDivisionError, IndexError, KeyError, ValueError, TypeError)


class IRExecutor:
    """Executes one `(quads, functions, var_types, structs)` program to
    completion (or until a runtime error/uncaught exception aborts it)."""

    def __init__(self, quads, functions, var_types, structs, trace=False, max_steps=10_000_000,
                 max_depth=10_000, input_provider=None, breakpoints=None, on_pause=None, on_line=None):
        self.quads = quads
        self.functions = functions
        self.var_types = var_types
        self.structs = structs
        self.trace = trace
        # 0 means unlimited for both - see main()'s --max-steps/--max-depth.
        self.max_steps = max_steps
        self.max_depth = max_depth
        # IDEs can supply input without changing the command-line behavior.
        # The callback receives every target in one `input(...)` statement
        # and returns one whitespace-separated input line.
        self.input_provider = input_provider
        self.breakpoints = breakpoints if breakpoints is not None else set()
        self.on_pause = on_pause
        self.on_line = on_line
        self._stepping = False
        self._stop_requested = False
        self._resume_event = threading.Event()
        # Every LABEL's quad index is resolved exactly once, up front, so
        # JMP/JZ/JNZ during execution are an O(1) dict lookup rather than a
        # linear scan for the matching label each time a jump happens.
        self.labels = {q.result: i for i, q in enumerate(quads) if q.op == "LABEL"}

    def run(self):
        """Initialize all VM state and execute quads from `pc = 0` until
        `pc` runs off the end (via `HALT` or falling out of `main`).
        `_NATIVE_RUNTIME_ERRORS` are caught exactly once here, at the
        outermost loop, and re-raised as a `RuntimeVMError` carrying the
        currently-executing quad's source line - this is the only place a
        raw Python exception from a malformed program gets translated into
        the clean `[RUNTIME ERROR]` reporting `main()` prints.

        `steps` is a plain local (not `self.steps`) so counting it costs no
        attribute lookup per quad - the VM's own `while` loop is otherwise
        a flat dispatch with no other per-iteration bookkeeping. It's the
        only cap against an infinite loop; `_call`'s recursion-depth check
        is the corresponding cap against unbounded recursion (see
        LIMITATIONS.md - previously there was no cap on either)."""
        self.globals = {}
        self.call_stack = []  # (return_pc, result_temp, caller_frame)
        self.call_names = []  # function name for each entry in call_stack, for a debugger's call-stack view
        self.exception_handlers = []  # (handler_pc, frame, call_stack_depth)
        self.pending_args = []
        self.current_exception = None
        self._input_buffer = []
        self.frame = self.globals
        self.pc = 0
        self._prev_line = None

        try:
            steps = 0
            while self.pc < len(self.quads):
                if self.max_steps and steps >= self.max_steps:
                    self._runtime_error(f"Exceeded maximum step count ({self.max_steps}) - possible infinite loop")
                steps += 1
                q = self.quads[self.pc]
                if self.on_pause is not None or self.on_line is not None:
                    self._check_pause(q.line)
                if self.trace:
                    print(f"{self.pc:04d}: {q}", file=sys.stderr)
                self._exec(q)
        except DebugStopped:
            return
        except _NATIVE_RUNTIME_ERRORS as e:
            line = self.quads[self.pc].line if self.pc < len(self.quads) else None
            if isinstance(e, ZeroDivisionError):
                # _div/_mod are the only deliberate raisers among
                # _NATIVE_RUNTIME_ERRORS, always with an already-clean,
                # hand-authored message ("Division by zero"/"Modulo by
                # zero") - never leaked Python-internal text, so pass it
                # through unchanged.
                raise RuntimeVMError(str(e), line) from None
            # Anything else here is an unexpected internal failure this VM
            # doesn't have a hand-checked diagnostic for - report a generic,
            # non-Python-specific message naming the failing quad instead of
            # leaking raw Python exception text (e.g. "object of type
            # 'NoneType' has no len()") through the [RUNTIME ERROR] channel.
            # Should be unreachable for any well-formed program; kept as
            # defense in depth.
            raise RuntimeVMError(f"Unexpected internal error while executing quad {self.pc}", line) from None

    # -- dispatch ------------------------------------------------------------

    def _exec(self, q):
        """Execute exactly one quad and advance `self.pc` (every branch is
        responsible for its own `pc` update - most fall through to `pc += 1`,
        but jumps/calls/returns retarget `pc` directly instead). Grouped by
        opcode family below, matching ir.py's module docstring's grouping."""
        op = q.op

        # -- control flow / misc --
        if op in ("LABEL", "FUNC_BEGIN", "FUNC_END", "NOP"):
            self.pc += 1
        elif op == "JMP":
            self.pc = self.labels[q.result]
        elif op == "JZ":
            self.pc = self.labels[q.result] if not self.resolve(q.arg1) else self.pc + 1
        elif op == "JNZ":
            self.pc = self.labels[q.result] if self.resolve(q.arg1) else self.pc + 1

        # -- data movement --
        elif op == "ASSIGN":
            self.set_var(q.result, self.resolve(q.arg1))
            self.pc += 1
        elif op == "CAST":
            self.set_var(q.result, self._cast(self.resolve(q.arg1), q.arg2))
            self.pc += 1

        # -- arithmetic / relational / logical --
        elif op in BINARY_OPS:
            left, right = self.resolve(q.arg1), self.resolve(q.arg2)
            self.set_var(q.result, BINARY_OPS[op](left, right))
            self.pc += 1
        elif op in UNARY_OPS:
            value = self.resolve(q.arg1)
            self.set_var(q.result, UNARY_OPS[op](value))
            self.pc += 1

        # -- functions --
        elif op == "PARAM":
            self.pending_args.append(self.resolve(q.arg1))
            self.pc += 1
        elif op == "CALL":
            self._call(q)
        elif op == "RETURN":
            self._do_return(q)

        # -- tuples (multi-return / destructuring) --
        elif op == "TUPLE_NEW":
            self.set_var(q.result, [])
            self.pc += 1
        elif op == "TUPLE_APPEND":
            values = self.resolve(q.arg1)
            values.append(self.resolve(q.arg2))
            self.set_var(q.result, values)
            self.pc += 1
        elif op == "UNPACK":
            self._unpack(q)
            self.pc += 1

        # -- arrays --
        elif op == "ARR_NEW":
            size = self.resolve(q.arg1)
            if size < 0:
                self._runtime_error(f"Array size cannot be negative (got {size})")
            self.set_var(q.result, [self._make_default(q.arg2) for _ in range(size)])
            self.pc += 1
        elif op == "ARR_LOAD":
            # LOAD: (array, index) -> result.
            arr, idx = self.resolve(q.arg1), self.resolve(q.arg2)
            self._check_index(arr, idx)
            self.set_var(q.result, arr[idx])
            self.pc += 1
        elif op == "ARR_STORE":
            # STORE: (index, value) -> array=result - operand order is
            # swapped relative to ARR_LOAD, per ir.py's LOAD/STORE convention.
            idx, value = self.resolve(q.arg1), self.resolve(q.arg2)
            arr = self.resolve(q.result)
            self._check_index(arr, idx)
            arr[idx] = value
            self.pc += 1
        elif op == "ARR_LEN":
            self.set_var(q.result, len(self.resolve(q.arg1)))
            self.pc += 1

        # -- structs --
        elif op == "STRUCT_NEW":
            fields = self.structs.get(q.arg1, {"fields": []})["fields"]
            self.set_var(q.result, StructValue(q.arg1, {fn: self._make_default(ft) for fn, ft in fields}))
            self.pc += 1
        elif op == "FIELD_LOAD":
            # LOAD: (struct, field_name) -> result.
            obj = self.resolve(q.arg1)
            self.set_var(q.result, obj[self.resolve(q.arg2)])
            self.pc += 1
        elif op == "FIELD_STORE":
            # STORE: (field_name, value) -> struct=result.
            field, value = self.resolve(q.arg1), self.resolve(q.arg2)
            self.resolve(q.result)[field] = value
            self.pc += 1

        # -- strings --
        elif op == "CONCAT":
            self.set_var(q.result, self.resolve(q.arg1) + self.resolve(q.arg2))
            self.pc += 1
        elif op == "TOSTR":
            self.set_var(q.result, self._format(self.resolve(q.arg1)))
            self.pc += 1

        # -- exceptions --
        elif op == "TRY_PUSH":
            self.exception_handlers.append((self.labels[q.arg1], self.frame, len(self.call_stack)))
            self.pc += 1
        elif op == "TRY_POP":
            self.exception_handlers.pop()
            self.pc += 1
        elif op == "THROW":
            self.current_exception = self.resolve(q.arg1)
            self._unwind()
        elif op == "RETHROW":
            self._unwind()
        elif op == "CATCH_STORE":
            self.set_var(q.result, self.current_exception)
            self.pc += 1

        # -- I/O --
        elif op == "PRINT":
            sys.stdout.write(self._format(self.resolve(q.arg1)))
            self.pc += 1
        elif op == "PRINT_SEP":
            sys.stdout.write(" ")
            self.pc += 1
        elif op == "PRINTLN":
            sys.stdout.write("\n")
            self.pc += 1
        elif op == "INPUT":
            self._do_input(q)
            self.pc += 1

        # -- misc --
        elif op == "HALT":
            self.pc = len(self.quads)
        else:
            raise NotImplementedError(f"Unsupported opcode: {op}")

    # -- debugger ----------------------------------------------------------

    def _check_pause(self, line):
        """Called once per quad (only when a debugger is attached, i.e.
        `on_pause` and/or `on_line` was given) before that quad executes.
        `on_line` fires on every *statement boundary* - the first quad of a
        new source line - unconditionally, giving a debugger a full
        execution trace even for lines that never pause. Pausing itself
        (`on_pause`) only ever happens at that same statement-boundary
        granularity, since one source line can lower to several quads and a
        debugger should show one stop per line, not per quad. Also doubles
        as the Stop control's check point, so a program stuck in a
        breakpoint-free loop still notices a stop request promptly (once per
        line, not once per quad, but that's frequent enough in practice)."""
        if self._stop_requested:
            raise DebugStopped()
        if line is None or line == self._prev_line:
            return
        self._prev_line = line
        if self.on_line is not None:
            self.on_line(line)
        if self.on_pause is not None and (self._stepping or line in self.breakpoints):
            self._stepping = False
            self.on_pause(line)
            self._resume_event.wait()
            self._resume_event.clear()
            if self._stop_requested:
                raise DebugStopped()

    def dbg_step(self):
        """Resume a paused run for exactly one more source line, then pause
        again. Safe to call from another thread while this executor's
        thread is blocked in `_check_pause`."""
        self._stepping = True
        self._resume_event.set()

    def dbg_continue(self):
        """Resume a paused run until the next breakpoint (or the program
        ends)."""
        self._stepping = False
        self._resume_event.set()

    def dbg_stop(self):
        """Abort a debug run, whether it's currently paused or still
        running freely between breakpoints."""
        self._stop_requested = True
        self._resume_event.set()

    # -- storage ---------------------------------------------------------

    def _runtime_error(self, message):
        """Raise a `RuntimeVMError` tagged with the currently-executing
        quad's source line - the single helper every hand-checked runtime
        error (array bounds, unpack arity, bad input) goes through."""
        line = self.quads[self.pc].line if self.pc < len(self.quads) else None
        raise RuntimeVMError(message, line)

    def resolve(self, operand):
        """Read one quad operand's value: a `Const` unwraps to its literal
        value; otherwise the name is looked up in the current frame first,
        falling back to `self.globals`. This "check frame, then globals"
        order - never the reverse - is what a local of the same name as a
        global relies on to correctly shadow it."""
        if operand is None:
            return None
        if isinstance(operand, Const):
            return operand.value
        if operand in self.frame:
            return self.frame[operand]
        return self.globals[operand]

    def set_var(self, name, value):
        """Write to one variable slot. The central trick of the whole VM:
        because semantics.py already guaranteed every ir_name is unique
        *program-wide* (see its `_fresh_ir_name`), "is this name currently a
        key in `self.globals`?" is a completely unambiguous routing test -
        there is no scope-nesting to walk, and no possibility that a local
        write here could ever land in the globals dict instead (which is
        exactly the bug an earlier flat-frame version of this VM had, per
        the module docstring)."""
        if name in self.globals:
            self.globals[name] = value
        else:
            self.frame[name] = value

    # -- functions / control flow -----------------------------------------

    def _call(self, q):
        """`CALL name argcount result_temp`: pop exactly `argcount` values
        off the tail of `pending_args` (built up by the preceding `PARAM`
        quads), push `(return_pc, result_temp, caller_frame)` onto
        `call_stack` - this list *is* the call stack, a plain Python list
        rather than a real recursive Python function call, which is why a
        deeply-recursive source program doesn't blow Python's own stack
        (see LIMITATIONS.md for the flip side: nothing caps it either) -
        then build a brand-new frame dict from the callee's parameter names
        zipped with the popped argument values, and jump to its entry
        point."""
        name, argcount, result_temp = q.arg1, q.arg2, q.result
        if self.max_depth and len(self.call_stack) >= self.max_depth:
            self._runtime_error(f"Exceeded maximum call depth ({self.max_depth}) - possible infinite recursion")
        args = self.pending_args[len(self.pending_args) - argcount:] if argcount else []
        if argcount:
            del self.pending_args[len(self.pending_args) - argcount:]
        fn = self.functions[name]
        self.call_stack.append((self.pc + 1, result_temp, self.frame))
        self.call_names.append(name)
        self.frame = dict(zip(fn["params"], args))
        self.pc = fn["entry"]

    def _do_return(self, q):
        """`RETURN [value]`: with an empty `call_stack`, this is a return
        from `main` itself - halt the whole program. Otherwise pop the
        matching `(return_pc, result_temp, caller_frame)` entry `_call`
        pushed, restore the caller's frame and resume at its saved
        `return_pc`, and - if the call site actually wanted the result
        (`result_temp is not None`) - write the returned value into that
        slot in the now-restored caller frame."""
        value = self.resolve(q.arg1) if q.arg1 is not None else None
        if not self.call_stack:
            self.pc = len(self.quads)  # return from main -> halt
            return
        self.pc, result_temp, self.frame = self.call_stack.pop()
        self.call_names.pop()
        if result_temp is not None:
            self.set_var(result_temp, value)

    def _unpack(self, q):
        """`UNPACK tuple_value -> (name1, name2, ...)`: fan a Python list
        (built by TUPLE_NEW/TUPLE_APPEND, or handed back from a multi-return
        call) out into several named slots at once - a length mismatch is a
        runtime error, not a silent truncate/pad, since semantics.py can't
        always prove arity statically for every call shape. A `None` in
        `names` is a `_` discard slot (see ir.py's `_gen_destructure`) -
        still counted for the arity check, just not written anywhere."""
        values = self.resolve(q.arg1)
        names = q.result
        if len(values) != len(names):
            self._runtime_error(f"Expected {len(names)} value(s) from unpacking, got {len(values)}")
        for name, v in zip(names, values):
            if name is not None:
                self.set_var(name, v)

    def _unwind(self):
        """Shared by `THROW` and `RETHROW`: pop the nearest still-registered
        exception handler and jump straight to it, **truncating
        `call_stack` back to the depth it had when that handler's
        `TRY_PUSH` ran**. That truncation is what makes a `throw` several
        function calls deep correctly unwind through all of them in one
        step, rather than needing each intervening function to notice and
        re-propagate the exception itself. With no handler left at all, the
        exception has propagated out of `main` entirely - that's reported
        as an `UncaughtException`, not silently swallowed."""
        if not self.exception_handlers:
            line = self.quads[self.pc].line if self.pc < len(self.quads) else None
            raise UncaughtException(self.current_exception, line)
        handler_pc, handler_frame, depth = self.exception_handlers.pop()
        del self.call_stack[depth:]
        self.pc = handler_pc
        self.frame = handler_frame

    def _check_index(self, arr, idx):
        """Bounds-check before every array read/write - Python would
        happily raise its own `IndexError` for a negative or too-large
        index, but this gives a clearer, VM-native error message (still
        ultimately reported the same way, since `_runtime_error` raises the
        same `RuntimeVMError` any native exception gets translated into)."""
        if not (0 <= idx < len(arr)):
            self._runtime_error(f"Array index {idx} out of bounds (length {len(arr)})")

    def _make_default(self, desc):
        """Recursively build a fresh default value from a `TypeDesc` (see
        ir.py): a scalar's `DEFAULTS` entry, a zero-filled array where every
        element is its own freshly-built value - never one shared list/
        struct aliased across every slot, which is what a naive `[x] * n`
        would do (see the module docstring on arrays/structs being
        reference values) - or a struct with every field
        default-initialized the same way. Used by `ARR_NEW`/`STRUCT_NEW` in
        place of the old flat `DEFAULTS.get(tag)` lookup, which only knew
        the five primitive types and left any nested array/struct `None`."""
        if desc.tag == "array":
            return [self._make_default(desc.elem) for _ in range(desc.size or 0)]
        if desc.tag == "struct":
            fields = self.structs.get(desc.name, {"fields": []})["fields"]
            return StructValue(desc.name, {fn: self._make_default(ft) for fn, ft in fields})
        return DEFAULTS.get(desc.tag)

    # -- values ------------------------------------------------------------

    @staticmethod
    def _cast(value, type_tag):
        # Only ever invoked for a semantics.py-approved numeric promotion
        # (char->int, char->float, int->float), never an arbitrary cast.
        if type_tag == "float":
            return float(ord(value)) if isinstance(value, str) else float(value)
        if type_tag == "int":
            return ord(value) if isinstance(value, str) else int(value)
        return value

    def _format(self, value):
        """Render a runtime value as the text `print`/interpolated strings
        show: the language's own `true`/`false` spelling for bools (Python's
        `str(True)` would print `"True"`), `Name{field: value, ...}` for
        structs, `{v, v, ...}` for arrays (recursively formatted, so nested
        arrays/structs print correctly), and plain `str()` for everything
        else (ints, floats, strings, and chars - which are just 1-character
        Python strs, so they need no special case)."""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, StructValue):
            inner = ", ".join(f"{k}: {self._format(v)}" for k, v in value.items())
            return f"{value.type_name}{{{inner}}}"
        if isinstance(value, list):
            return "{" + ", ".join(self._format(v) for v in value) + "}"
        return str(value)

    def _next_input_token(self, names=None):
        """Pull the next whitespace-separated token from stdin, reading a
        fresh line whenever the buffer runs dry. This is what lets a
        program like `repeat { input(n); } until (n > 0);` read across
        multiple physical lines correctly - `input()` doesn't assume "one
        call = one line", it just keeps consuming from one continuous
        stream of whitespace-separated tokens."""
        while not self._input_buffer:
            raw = self.input_provider(names or []) if self.input_provider else sys.stdin.readline()
            if raw is None:
                raw = ""
            if raw == "":
                self._runtime_error("Unexpected end of input")
            self._input_buffer = raw.split()
        return self._input_buffer.pop(0)

    def _do_input(self, q):
        """`INPUT -> (name1, name2, ...)`: read one token per target name,
        coercing each to that name's declared type (from `var_types`,
        recorded by ir.py) before storing it."""
        for name in q.result:
            text = self._next_input_token(q.result)
            self.set_var(name, self._coerce(text, self.var_types.get(name, "string")))

    def _coerce(self, text, type_tag):
        """Parse one whitespace-separated input token according to the
        target variable's type tag. A `bool` accepts `"true"`/`"1"` (case-
        insensitive) as true, anything else as false, rather than raising on
        unrecognized text. A malformed `int`/`float` token is a clean
        runtime error, not a raw Python traceback, hence the explicit
        `try`/`except ValueError` here rather than letting it propagate to
        the generic `_NATIVE_RUNTIME_ERRORS` catch in `run()` (which would
        still work, just with a less specific message)."""
        try:
            if type_tag == "int":
                return int(text)
            if type_tag == "float":
                return float(text)
            if type_tag == "bool":
                return text.strip().lower() in ("true", "1")
            if type_tag == "char":
                return text[0] if text else "\0"
            return text
        except ValueError:
            self._runtime_error(f"Invalid input: expected '{type_tag}', got '{text}'")


def main():
    """CLI entry point: `python interpreter.py <source_file> [--ir] [--symbols] [--trace]`.
    Runs the complete pipeline - scan, parse, analyze, generate, execute -
    exiting `2` if scanning/parsing/semantic analysis found any error
    (nothing runs in that case) or `3` if execution itself fails
    (`RuntimeVMError`/`UncaughtException`), matching the exit-code contract
    described in `README.md`."""
    cli = argparse.ArgumentParser(description="CSC617M Custom Language Interpreter")
    cli.add_argument("source_file", help="Path to source file to run")
    cli.add_argument("--ir", action="store_true", help="Print the generated intermediate code (quadruples) before running")
    cli.add_argument("--symbols", action="store_true", help="Print function/struct/variable metadata before running")
    cli.add_argument("--trace", action="store_true", help="Print each quad to stderr as it executes")
    cli.add_argument("-O", "--optimize", action="store_true", help="Optimize the intermediate code before running it (see optimizer.py)")
    cli.add_argument("--max-steps", type=int, default=10_000_000, help="Abort with a runtime error after this many executed quads (0 = unlimited)")
    cli.add_argument("--max-depth", type=int, default=10_000, help="Abort with a runtime error past this call-stack depth (0 = unlimited)")
    args = cli.parse_args()

    try:
        with open(args.source_file, "r", encoding="utf-8") as f:
            source = f.read()
    except FileNotFoundError:
        print(f"Error: file not found: {args.source_file}", file=sys.stderr)
        sys.exit(1)

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
        quads, functions, var_types, structs = generate(ast, symtab)
    except IRGenError as e:
        print(f"[IR ERROR] {e}", file=sys.stderr)
        sys.exit(2)

    if args.optimize:
        # Imported lazily so the default (unoptimized) path doesn't pay for
        # loading a module it never uses.
        import optimizer
        try:
            result = optimizer.optimize(quads, functions, var_types)
        except optimizer.OptimizerError as e:
            print(f"[OPTIMIZER ERROR] {e}", file=sys.stderr)
            sys.exit(2)
        quads, functions = result.quads, result.functions

    if args.ir:
        label = "Optimized Intermediate Code" if args.optimize else "Intermediate Code"
        print(f"{label} (Quadruples):")
        print(format_quads(quads))
        print()

    if args.symbols:
        print("Functions:", {name: info["params"] for name, info in functions.items()})
        print("Structs:", {name: info["fields"] for name, info in structs.items()})
        print("Variables:", var_types)
        print()

    try:
        IRExecutor(quads, functions, var_types, structs, trace=args.trace,
                   max_steps=args.max_steps, max_depth=args.max_depth).run()
    except RuntimeVMError as e:
        loc = f"Line {e.line}: " if e.line is not None else ""
        print(f"[RUNTIME ERROR] {loc}{e}", file=sys.stderr)
        sys.exit(3)
    except UncaughtException as e:
        loc = f"Line {e.line}: " if e.line is not None else ""
        print(f"[RUNTIME ERROR] {loc}Uncaught exception: {e.value}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
