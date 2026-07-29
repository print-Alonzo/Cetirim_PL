"""Stack-machine VM that executes the quadruples produced by ir.py.

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
`THROW`/`RETHROW` unwind `call_stack` back to that depth and resume at
`handler_pc` in the recorded `frame`. There's no separate runtime notion of
"finally" - ir.py's gen_try already compiled two copies of the finally body
(one inline after a normal catch, one before RETHROW), so the VM only ever
needs to know "where's the nearest still-registered handler".

Arrays are plain Python lists and structs are dict-like StructValues, both
stored directly as the value in a frame slot - passing one to a function or
assigning it just copies the Python reference, which is exactly call-by-
reference for aggregates and call-by-value for scalars, matching the
project's design assumptions, for free.
"""

import argparse
import operator
import sys

from ir import Const, IRGenError, format_quads, generate
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


class StructValue(dict):
    """A struct instance: a plain dict of field values, plus the struct's
    type name (kept off to the side so `Name{field: value}` formatting
    doesn't need runtime type inference)."""

    __slots__ = ("type_name",)

    def __init__(self, type_name, fields):
        super().__init__(fields)
        self.type_name = type_name


def _numeric(value):
    """Promote a char (a 1-character Python str) to its ordinal so it can
    take part in arithmetic/relational ops. Known limitation: a genuine
    1-character *string* value is indistinguishable from a char at this
    point and gets promoted the same way - harmless for every program in
    this project (string values are never arithmetically/relationally
    compared), but worth knowing about."""
    return ord(value) if isinstance(value, str) and len(value) == 1 else value


def _div(a, b):
    # C-style truncating division: int/int -> int, truncated toward zero.
    return a / b if isinstance(a, float) or isinstance(b, float) else int(a / b)


BINARY_OPS = {
    "ADD": operator.add, "SUB": operator.sub, "MUL": operator.mul, "DIV": _div, "MOD": operator.mod,
    "EQ": operator.eq, "NE": operator.ne, "LT": operator.lt, "GT": operator.gt,
    "LE": operator.le, "GE": operator.ge,
}
ARITH_OPCODES = {"ADD", "SUB", "MUL", "DIV", "MOD"}
NUMERIC_PROMOTE_OPCODES = ARITH_OPCODES | {"LT", "GT", "LE", "GE", "EQ", "NE"}
UNARY_OPS = {"UMINUS": operator.neg, "UPLUS": lambda v: v, "NOT": operator.not_}

# Python exceptions a malformed-at-runtime program can trigger (bad array
# index, div by zero, missing dict key, bad literal coercion, ...). Caught
# once at the top of run() and reported as a clean [RUNTIME ERROR], never as
# a raw traceback.
_NATIVE_RUNTIME_ERRORS = (ZeroDivisionError, IndexError, KeyError, ValueError, TypeError)


class IRExecutor:
    def __init__(self, quads, functions, var_types, structs, trace=False):
        self.quads = quads
        self.functions = functions
        self.var_types = var_types
        self.structs = structs
        self.trace = trace
        self.labels = {q.result: i for i, q in enumerate(quads) if q.op == "LABEL"}

    def run(self):
        self.globals = {}
        self.call_stack = []  # (return_pc, result_temp, caller_frame)
        self.exception_handlers = []  # (handler_pc, frame, call_stack_depth)
        self.pending_args = []
        self.current_exception = None
        self._input_buffer = []
        self.frame = self.globals
        self.pc = 0

        try:
            while self.pc < len(self.quads):
                q = self.quads[self.pc]
                if self.trace:
                    print(f"{self.pc:04d}: {q}", file=sys.stderr)
                self._exec(q)
        except _NATIVE_RUNTIME_ERRORS as e:
            line = self.quads[self.pc].line if self.pc < len(self.quads) else None
            raise RuntimeVMError(str(e), line) from None

    # -- dispatch ------------------------------------------------------------

    def _exec(self, q):
        op = q.op

        if op in ("LABEL", "FUNC_BEGIN", "FUNC_END", "NOP"):
            self.pc += 1
        elif op == "JMP":
            self.pc = self.labels[q.result]
        elif op == "JZ":
            self.pc = self.labels[q.result] if not self.resolve(q.arg1) else self.pc + 1
        elif op == "JNZ":
            self.pc = self.labels[q.result] if self.resolve(q.arg1) else self.pc + 1
        elif op == "ASSIGN":
            self.set_var(q.result, self.resolve(q.arg1))
            self.pc += 1
        elif op == "CAST":
            self.set_var(q.result, self._cast(self.resolve(q.arg1), q.arg2))
            self.pc += 1
        elif op in BINARY_OPS:
            left, right = self.resolve(q.arg1), self.resolve(q.arg2)
            if op in NUMERIC_PROMOTE_OPCODES:
                left, right = _numeric(left), _numeric(right)
            self.set_var(q.result, BINARY_OPS[op](left, right))
            self.pc += 1
        elif op in UNARY_OPS:
            value = self.resolve(q.arg1)
            if op != "NOT":
                value = _numeric(value)
            self.set_var(q.result, UNARY_OPS[op](value))
            self.pc += 1
        elif op == "PARAM":
            self.pending_args.append(self.resolve(q.arg1))
            self.pc += 1
        elif op == "CALL":
            self._call(q)
        elif op == "RETURN":
            self._do_return(q)
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
        elif op == "ARR_NEW":
            size = self.resolve(q.arg1)
            self.set_var(q.result, [DEFAULTS.get(q.arg2)] * size)
            self.pc += 1
        elif op == "ARR_LOAD":
            arr, idx = self.resolve(q.arg1), self.resolve(q.arg2)
            self._check_index(arr, idx)
            self.set_var(q.result, arr[idx])
            self.pc += 1
        elif op == "ARR_STORE":
            idx, value = self.resolve(q.arg1), self.resolve(q.arg2)
            arr = self.resolve(q.result)
            self._check_index(arr, idx)
            arr[idx] = value
            self.pc += 1
        elif op == "ARR_LEN":
            self.set_var(q.result, len(self.resolve(q.arg1)))
            self.pc += 1
        elif op == "STRUCT_NEW":
            fields = self.structs.get(q.arg1, {"fields": []})["fields"]
            self.set_var(q.result, StructValue(q.arg1, {fn: DEFAULTS.get(ft) for fn, ft in fields}))
            self.pc += 1
        elif op == "FIELD_LOAD":
            obj = self.resolve(q.arg1)
            self.set_var(q.result, obj[self.resolve(q.arg2)])
            self.pc += 1
        elif op == "FIELD_STORE":
            field, value = self.resolve(q.arg1), self.resolve(q.arg2)
            self.resolve(q.result)[field] = value
            self.pc += 1
        elif op == "CONCAT":
            self.set_var(q.result, self.resolve(q.arg1) + self.resolve(q.arg2))
            self.pc += 1
        elif op == "TOSTR":
            self.set_var(q.result, self._format(self.resolve(q.arg1)))
            self.pc += 1
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
        elif op == "HALT":
            self.pc = len(self.quads)
        else:
            raise NotImplementedError(f"Unsupported opcode: {op}")

    # -- storage ---------------------------------------------------------

    def _runtime_error(self, message):
        line = self.quads[self.pc].line if self.pc < len(self.quads) else None
        raise RuntimeVMError(message, line)

    def resolve(self, operand):
        if operand is None:
            return None
        if isinstance(operand, Const):
            return operand.value
        if operand in self.frame:
            return self.frame[operand]
        return self.globals[operand]

    def set_var(self, name, value):
        if name in self.globals:
            self.globals[name] = value
        else:
            self.frame[name] = value

    # -- functions / control flow -----------------------------------------

    def _call(self, q):
        name, argcount, result_temp = q.arg1, q.arg2, q.result
        args = self.pending_args[len(self.pending_args) - argcount:] if argcount else []
        if argcount:
            del self.pending_args[len(self.pending_args) - argcount:]
        fn = self.functions[name]
        self.call_stack.append((self.pc + 1, result_temp, self.frame))
        self.frame = dict(zip(fn["params"], args))
        self.pc = fn["entry"]

    def _do_return(self, q):
        value = self.resolve(q.arg1) if q.arg1 is not None else None
        if not self.call_stack:
            self.pc = len(self.quads)  # return from main -> halt
            return
        self.pc, result_temp, self.frame = self.call_stack.pop()
        if result_temp is not None:
            self.set_var(result_temp, value)

    def _unpack(self, q):
        values = self.resolve(q.arg1)
        names = q.result
        if len(values) != len(names):
            self._runtime_error(f"Expected {len(names)} value(s) from unpacking, got {len(values)}")
        for name, v in zip(names, values):
            self.set_var(name, v)

    def _unwind(self):
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
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, StructValue):
            inner = ", ".join(f"{k}: {self._format(v)}" for k, v in value.items())
            return f"{value.type_name}{{{inner}}}"
        if isinstance(value, list):
            return "{" + ", ".join(self._format(v) for v in value) + "}"
        return str(value)

    def _next_input_token(self):
        while not self._input_buffer:
            raw = sys.stdin.readline()
            if raw == "":
                self._runtime_error("Unexpected end of input")
            self._input_buffer = raw.split()
        return self._input_buffer.pop(0)

    def _do_input(self, q):
        for name in q.result:
            text = self._next_input_token()
            self.set_var(name, self._coerce(text, self.var_types.get(name, "string")))

    def _coerce(self, text, type_tag):
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
    cli = argparse.ArgumentParser(description="CSC617M Custom Language Interpreter")
    cli.add_argument("source_file", help="Path to source file to run")
    cli.add_argument("--ir", action="store_true", help="Print the generated intermediate code (quadruples) before running")
    cli.add_argument("--symbols", action="store_true", help="Print function/struct/variable metadata before running")
    cli.add_argument("--trace", action="store_true", help="Print each quad to stderr as it executes")
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
        sys.exit(2)

    try:
        quads, functions, var_types, structs = generate(ast, symtab)
    except IRGenError as e:
        print(f"[IR ERROR] {e}", file=sys.stderr)
        sys.exit(2)

    if args.ir:
        print("Intermediate Code (Quadruples):")
        print(format_quads(quads))
        print()

    if args.symbols:
        print("Functions:", {name: info["params"] for name, info in functions.items()})
        print("Structs:", {name: info["fields"] for name, info in structs.items()})
        print("Variables:", var_types)
        print()

    try:
        IRExecutor(quads, functions, var_types, structs, trace=args.trace).run()
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
