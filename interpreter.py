# stack-machine VM for ir.py's quadruples, the last phase in the pipeline
# each call gets its own frame dict keyed by ir_name, plus one shared globals dict

import argparse
import operator
import sys
import threading

from ir import Const, IRGenError, _div, _mod, format_quads, generate
from parser import parse_source
from semantics import analyze

DEFAULTS = {"int": 0, "float": 0.0, "bool": False, "string": "", "char": "\0"}


class RuntimeVMError(Exception):
    def __init__(self, message, line=None):
        super().__init__(message)
        self.message = message
        self.line = line

    def __str__(self):
        return self.message


class UncaughtException(Exception):
    def __init__(self, value, line=None):
        super().__init__(str(value))
        self.value = value
        self.line = line


class DebugStopped(Exception):
    pass  # raised internally to unwind run() when the debugger's Stop control is used


class StructValue(dict):
    __slots__ = ("type_name",)

    def __init__(self, type_name, fields):
        super().__init__(fields)
        self.type_name = type_name


# DIV/MOD's truncated semantics live in ir.py so optimizer.py's constant folder
# can fold them through the same code this VM executes them with, see ir._div
BINARY_OPS = {
    "ADD": operator.add, "SUB": operator.sub, "MUL": operator.mul, "DIV": _div, "MOD": _mod,
    "EQ": operator.eq, "NE": operator.ne, "LT": operator.lt, "GT": operator.gt,
    "LE": operator.le, "GE": operator.ge,
}
UNARY_OPS = {"UMINUS": operator.neg, "UPLUS": lambda v: v, "NOT": operator.not_}

# native python exceptions a malformed program can trigger, caught once in run()
# and reported as a clean [RUNTIME ERROR] instead of a raw traceback
_NATIVE_RUNTIME_ERRORS = (ZeroDivisionError, IndexError, KeyError, ValueError, TypeError)


class IRExecutor:
    def __init__(self, quads, functions, var_types, structs, trace=False, max_steps=10_000_000,
                 max_depth=10_000, input_provider=None, breakpoints=None, on_pause=None, on_line=None):
        self.quads = quads
        self.functions = functions
        self.var_types = var_types
        self.structs = structs
        self.trace = trace
        self.max_steps = max_steps  # 0 = unlimited
        self.max_depth = max_depth  # 0 = unlimited
        self.input_provider = input_provider  # lets an IDE supply input instead of stdin
        self.breakpoints = breakpoints if breakpoints is not None else set()
        self.on_pause = on_pause
        self.on_line = on_line
        self._stepping = False
        # step-over state: call depth + line the step was issued at, both None means step-into
        self._step_depth = None
        self._step_line = None
        # depth + line the last pause resumed from, so a breakpoint there doesn't fire again
        # while control is still finishing that same statement
        self._resume_depth = None
        self._resume_line = None
        self._paused = False  # true only while parked in _check_pause, waiting on a dbg_* control
        self._stop_requested = False
        self._resume_event = threading.Event()
        # resolve every LABEL up front so JMP/JZ/JNZ are O(1) lookups instead of a linear scan
        self.labels = {q.result: i for i, q in enumerate(quads) if q.op == "LABEL"}

    def run(self):
        self.globals = {}
        self.call_stack = []  # (return_pc, result_temp, caller_frame)
        self.call_names = []  # function name per call_stack entry, for a debugger's call-stack view
        self.exception_handlers = []  # (handler_pc, frame, call_stack_depth)
        self.pending_args = []
        self.current_exception = None
        self._input_buffer = []
        self.frame = self.globals
        self.pc = 0
        self._prev_line = None
        self._prev_depth = 0  # call depth as of the last statement boundary, see _check_pause

        try:
            steps = 0
            while self.pc < len(self.quads):
                if self.max_steps and steps >= self.max_steps:
                    self._runtime_error(f"Exceeded maximum step count ({self.max_steps}) - possible infinite loop")
                steps += 1
                q = self.quads[self.pc]
                if self.on_pause is not None or self.on_line is not None or self._stop_requested:
                    self._check_pause(q.line)
                if self.trace:
                    print(f"{self.pc:04d}: {q}", file=sys.stderr)
                self._exec(q)
        except DebugStopped:
            return
        except _NATIVE_RUNTIME_ERRORS as e:
            line = self.quads[self.pc].line if self.pc < len(self.quads) else None
            if isinstance(e, ZeroDivisionError):
                # _div/_mod always raise with an already-clean hand-authored message
                raise RuntimeVMError(str(e), line) from None
            # anything else here is an unexpected internal failure, not a hand-checked diagnostic
            raise RuntimeVMError(f"Unexpected internal error while executing quad {self.pc}", line) from None

    # dispatch

    def _exec(self, q):
        op = q.op

        # control flow / misc
        if op in ("LABEL", "FUNC_BEGIN", "FUNC_END", "NOP"):
            self.pc += 1
        elif op == "JMP":
            self.pc = self.labels[q.result]
        elif op == "JZ":
            self.pc = self.labels[q.result] if not self.resolve(q.arg1) else self.pc + 1
        elif op == "JNZ":
            self.pc = self.labels[q.result] if self.resolve(q.arg1) else self.pc + 1

        # data movement
        elif op == "ASSIGN":
            self.set_var(q.result, self.resolve(q.arg1))
            self.pc += 1
        elif op == "CAST":
            self.set_var(q.result, self._cast(self.resolve(q.arg1), q.arg2))
            self.pc += 1

        # arithmetic / relational / logical
        elif op in BINARY_OPS:
            left, right = self.resolve(q.arg1), self.resolve(q.arg2)
            self.set_var(q.result, BINARY_OPS[op](left, right))
            self.pc += 1
        elif op in UNARY_OPS:
            value = self.resolve(q.arg1)
            self.set_var(q.result, UNARY_OPS[op](value))
            self.pc += 1

        # functions
        elif op == "PARAM":
            self.pending_args.append(self.resolve(q.arg1))
            self.pc += 1
        elif op == "CALL":
            self._call(q)
        elif op == "RETURN":
            self._do_return(q)

        # tuples (multi-return / destructuring)
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

        # arrays
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
            # STORE: (index, value) -> array=result, operand order flipped vs ARR_LOAD
            idx, value = self.resolve(q.arg1), self.resolve(q.arg2)
            arr = self.resolve(q.result)
            self._check_index(arr, idx)
            arr[idx] = value
            self.pc += 1
        elif op == "ARR_LEN":
            self.set_var(q.result, len(self.resolve(q.arg1)))
            self.pc += 1

        # structs
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

        # strings
        elif op == "CONCAT":
            self.set_var(q.result, self.resolve(q.arg1) + self.resolve(q.arg2))
            self.pc += 1
        elif op == "TOSTR":
            self.set_var(q.result, self._format(self.resolve(q.arg1)))
            self.pc += 1

        # exceptions
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

        # I/O
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

        # misc
        elif op == "HALT":
            self.pc = len(self.quads)
        else:
            raise NotImplementedError(f"Unsupported opcode: {op}")

    # debugger

    def _check_pause(self, line):
        # called before every quad once a debugger is attached. on_line/on_pause fire at each
        # statement boundary; while stepping, a boundary also counts as "new call depth" so step-over works inside recursion
        if self._stop_requested:
            raise DebugStopped()
        depth = len(self.call_stack)
        if line is None or (line == self._prev_line
                            and not (self._stepping and depth != self._prev_depth)):
            return
        self._prev_line = line
        self._prev_depth = depth
        if self.on_line is not None:
            self.on_line(line)
        # checked at every boundary so it reliably retires itself, not just when a breakpoint fires
        suppressed = self._resumed_statement(line)
        if self.on_pause is not None and (self._should_step_pause(line)
                                          or (line in self.breakpoints and not suppressed)):
            self._stepping = False
            self._step_depth = None
            self._step_line = None
            self._resume_event.clear()
            self._paused = True
            self.on_pause(line)
            self._resume_event.wait()
            self._paused = False
            self._resume_event.clear()
            self._resume_depth = len(self.call_stack)
            self._resume_line = line
            if self._stop_requested:
                raise DebugStopped()

    def _resumed_statement(self, line):
        # true if this boundary is still the tail of the statement the last pause resumed from, e.g. "int x = f();" re-enters its own line once f() returns
        # that would otherwise look like a fresh hit on a breakpoint just continued past
        if self._resume_depth is None:
            return False
        if len(self.call_stack) > self._resume_depth:
            return False
        if line == self._resume_line:
            return True
        self._resume_depth = None
        self._resume_line = None
        return False

    def _should_step_pause(self, line):
        # step-into always stops at the next boundary. step-over stops once back at a shallower depth (the call/recursion returned), or same depth but a different line
        # not same depth AND line, that's just the call site re-entering
        if not self._stepping:
            return False
        if self._step_depth is None:
            return True
        depth = len(self.call_stack)
        if depth < self._step_depth:
            return True
        return depth == self._step_depth and line != self._step_line

    @property
    def paused(self):
        return self._paused

    def dbg_step(self):
        # resume for exactly one more line, descending into a call if this line makes one
        if not self._paused:
            return
        self._stepping = True
        self._step_depth = None
        self._step_line = None
        self._resume_event.set()

    def dbg_step_over(self):
        # resume for one more line in the current frame, running any call it makes to completion
        if not self._paused:
            return
        self._stepping = True
        self._step_depth = len(self.call_stack)
        self._step_line = self._prev_line
        self._resume_event.set()

    def dbg_continue(self):
        if not self._paused:
            return
        self._stepping = False
        self._step_depth = None
        self._step_line = None
        self._resume_event.set()

    def dbg_stop(self):
        self._stop_requested = True
        self._resume_event.set()

    # storage

    def _runtime_error(self, message):
        line = self.quads[self.pc].line if self.pc < len(self.quads) else None
        raise RuntimeVMError(message, line)

    def resolve(self, operand):
        # frame is checked before globals, so a local correctly shadows a global of the same name
        if operand is None:
            return None
        if isinstance(operand, Const):
            return operand.value
        if operand in self.frame:
            return self.frame[operand]
        return self.globals[operand]

    def set_var(self, name, value):
        # every ir_name is unique program-wide (see semantics.py), so "is this a global name"
        # is enough to route the write correctly, no scope walking needed
        if name in self.globals:
            self.globals[name] = value
        else:
            self.frame[name] = value

    # functions / control flow

    def _call(self, q):
        # call_stack is a plain list, not real python recursion, so deep recursion in the
        # source program doesn't blow python's own call stack
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
        value = self.resolve(q.arg1) if q.arg1 is not None else None
        if not self.call_stack:
            self.pc = len(self.quads)  # return from main -> halt
            return
        self.pc, result_temp, self.frame = self.call_stack.pop()
        self.call_names.pop()
        if result_temp is not None:
            self.set_var(result_temp, value)

    def _unpack(self, q):
        values = self.resolve(q.arg1)
        names = q.result
        if len(values) != len(names):
            self._runtime_error(f"Expected {len(names)} value(s) from unpacking, got {len(values)}")
        for name, v in zip(names, values):
            if name is not None:  # None = a `_` discard slot, still counted for the arity check
                self.set_var(name, v)

    def _unwind(self):
        # truncating call_stack back to the handler's depth is what makes a throw several
        # calls deep unwind through all of them in one step, instead of each one re-raising
        if not self.exception_handlers:
            line = self.quads[self.pc].line if self.pc < len(self.quads) else None
            raise UncaughtException(self.current_exception, line)
        handler_pc, handler_frame, depth = self.exception_handlers.pop()
        del self.call_stack[depth:]
        self.pc = handler_pc
        self.frame = handler_frame

    def _check_index(self, arr, idx):
        if not (0 <= idx < len(arr)):
            self._runtime_error(f"Array index {idx} out of bounds (length {len(arr)})")

    def _make_default(self, desc):
        # build each element/field fresh so nested arrays/structs aren't aliased across slots
        if desc.tag == "array":
            return [self._make_default(desc.elem) for _ in range(desc.size or 0)]
        if desc.tag == "struct":
            fields = self.structs.get(desc.name, {"fields": []})["fields"]
            return StructValue(desc.name, {fn: self._make_default(ft) for fn, ft in fields})
        return DEFAULTS.get(desc.tag)

    # values

    @staticmethod
    def _cast(value, type_tag):
        # only used for semantics.py-approved numeric promotions (char->int, char->float, int->float)
        if type_tag == "float":
            return float(ord(value)) if isinstance(value, str) else float(value)
        if type_tag == "int":
            return ord(value) if isinstance(value, str) else int(value)
        return value

    def _format(self, value):
        if isinstance(value, bool):
            return "true" if value else "false"  # not python's "True"/"False"
        if isinstance(value, StructValue):
            inner = ", ".join(f"{k}: {self._format(v)}" for k, v in value.items())
            return f"{value.type_name}{{{inner}}}"
        if isinstance(value, list):
            return "{" + ", ".join(self._format(v) for v in value) + "}"
        return str(value)

    def _next_input_token(self, names=None):
        # keeps pulling from stdin/input_provider across lines until there's a token to return
        while not self._input_buffer:
            if self._stop_requested:
                raise DebugStopped()
            raw = self.input_provider(names or []) if self.input_provider else sys.stdin.readline()
            if raw is None:
                raw = ""
            if raw == "":
                self._runtime_error("Unexpected end of input")
            self._input_buffer = raw.split()
        return self._input_buffer.pop(0)

    def _do_input(self, q):
        for name in q.result:
            text = self._next_input_token(q.result)
            self.set_var(name, self._coerce(text, self.var_types.get(name, "string")))

    def _coerce(self, text, type_tag):
        try:
            if type_tag == "int":
                return int(text)
            if type_tag == "float":
                return float(text)
            if type_tag == "bool":
                return text.strip().lower() in ("true", "1")  # anything else reads as false
            if type_tag == "char":
                return text[0] if text else "\0"
            return text
        except ValueError:
            self._runtime_error(f"Invalid input: expected '{type_tag}', got '{text}'")


def main():
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
        import optimizer  # lazy import, so the default (unoptimized) path doesn't pay for it
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
