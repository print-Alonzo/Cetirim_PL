import argparse
import operator
import sys

from ir import Const, IRGenError, format_quads, generate
from parser import parse_source


class InterpreterError(Exception):
    pass


def _numeric(value):
    return ord(value) if isinstance(value, str) and len(value) == 1 else value


def _div(a, b):
    return a / b if isinstance(a, float) or isinstance(b, float) else int(a / b)


BINARY_OPS = {
    "ADD": operator.add, "SUB": operator.sub, "MUL": operator.mul, "DIV": _div, "MOD": operator.mod,
    "EQ": operator.eq, "NE": operator.ne, "LT": operator.lt, "GT": operator.gt,
    "LE": operator.le, "GE": operator.ge,
    "AND": lambda a, b: bool(a) and bool(b), "OR": lambda a, b: bool(a) or bool(b),
}
ARITH_OPCODES = {"ADD", "SUB", "MUL", "DIV", "MOD"}
UNARY_OPS = {"UMINUS": operator.neg, "UPLUS": lambda v: v, "NOT": operator.not_}


class IRExecutor:
    def __init__(self, quads, functions, var_types):
        self.quads = quads
        self.functions = functions
        self.var_types = var_types
        self.labels = {q.result: i for i, q in enumerate(quads) if q.op == "LABEL"}

    def run(self):
        self.globals = {}
        self.pending_args = []
        call_stack = []
        frame = self.globals
        pc = 0

        while pc < len(self.quads):
            q = self.quads[pc]
            op = q.op

            if op in ("LABEL", "FUNC_BEGIN", "FUNC_END"):
                pc += 1
            elif op == "JMP":
                pc = self.labels[q.result]
            elif op == "JZ":
                pc = self.labels[q.result] if not self.resolve(q.arg1, frame) else pc + 1
            elif op == "ASSIGN":
                self.set_var(q.result, self.resolve(q.arg1, frame), frame)
                pc += 1
            elif op in BINARY_OPS:
                left, right = self.resolve(q.arg1, frame), self.resolve(q.arg2, frame)
                if op in ARITH_OPCODES:
                    left, right = _numeric(left), _numeric(right)
                self.set_var(q.result, BINARY_OPS[op](left, right), frame)
                pc += 1
            elif op in UNARY_OPS:
                value = self.resolve(q.arg1, frame)
                if op != "NOT":
                    value = _numeric(value)
                self.set_var(q.result, UNARY_OPS[op](value), frame)
                pc += 1
            elif op == "PARAM":
                self.pending_args.append(self.resolve(q.arg1, frame))
                pc += 1
            elif op == "CALL":
                name, argcount = q.arg1, q.arg2
                args = self.pending_args[len(self.pending_args) - argcount:] if argcount else []
                if argcount:
                    del self.pending_args[len(self.pending_args) - argcount:]
                fn = self.functions[name]
                call_stack.append((pc + 1, q.result, frame))
                frame = dict(zip(fn["params"], args))
                pc = fn["entry"]
            elif op == "RETURN":
                value = self.resolve(q.arg1, frame) if q.arg1 is not None else None
                if not call_stack:
                    return
                pc, result_temp, frame = call_stack.pop()
                if result_temp is not None:
                    self.set_var(result_temp, value, frame)
            elif op == "PRINT":
                sys.stdout.write(self._format(self.resolve(q.arg1, frame)))
                pc += 1
            elif op == "PRINT_SEP":
                sys.stdout.write(" ")
                pc += 1
            elif op == "PRINTLN":
                sys.stdout.write("\n")
                pc += 1
            elif op == "INPUT":
                tokens = sys.stdin.readline().split()
                for name, text in zip(q.result, tokens):
                    self.set_var(name, self._coerce(text, self.var_types.get(name)), frame)
                pc += 1
            elif op == "HALT":
                return
            else:
                raise NotImplementedError(f"Unsupported opcode: {op}")

    def resolve(self, operand, frame):
        if operand is None:
            return None
        if isinstance(operand, Const):
            return operand.value
        if frame is not self.globals and operand in frame:
            return frame[operand]
        return self.globals[operand]

    def set_var(self, name, value, frame):
        # a function's frame only ever sees its own locals plus globals
        if frame is not self.globals and name in self.globals:
            self.globals[name] = value
        else:
            frame[name] = value

    @staticmethod
    def _format(value):
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    @staticmethod
    def _coerce(text, type_name):
        if type_name == "int":
            return int(text)
        if type_name == "float":
            return float(text)
        if type_name == "bool":
            return text.strip().lower() in ("true", "1")
        if type_name == "char":
            return text[0] if text else "\0"
        return text


def main():
    cli = argparse.ArgumentParser(description="CSC617M Custom Language Interpreter")
    cli.add_argument("source_file", help="Path to source file to run")
    cli.add_argument("--ir", action="store_true", help="Print the generated intermediate code (quadruples) before running")
    args = cli.parse_args()

    with open(args.source_file, "r", encoding="utf-8") as f:
        source = f.read()

    ast, syntax_errors, lex_errors = parse_source(source)
    if lex_errors or syntax_errors:
        for err in lex_errors:
            print(err, file=sys.stderr)
        for err in syntax_errors:
            print(err, file=sys.stderr)
        sys.exit(2)

    try:
        quads, functions, var_types = generate(ast)
    except IRGenError as e:
        print(f"[Analysis error] {e}", file=sys.stderr)
        sys.exit(2)

    if args.ir:
        print("Intermediate Code (Quadruples):")
        print(format_quads(quads))
        print()

    IRExecutor(quads, functions, var_types).run()


if __name__ == "__main__":
    main()
