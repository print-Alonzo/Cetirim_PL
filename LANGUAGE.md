# The Cetirim Language — Programmer's Manual

**Course:** CSC617M — Theory of Programming Languages

This is the *language* half of the User's Manual: how to **write programs** in
Cetirim. For how to **run** the interpreter — CLI flags, output formats, the
test suite, building the binary — see [`README.md`](README.md).

Every example below is a complete program that runs as written.

**Contents**

1. [Hello world](#1-hello-world)
2. [Lexical structure](#2-lexical-structure)
3. [Program structure](#3-program-structure)
4. [Declarations: `const`, `val`, `var`, `let`](#4-declarations)
5. [Types and coercion](#5-types-and-coercion)
6. [Operators and precedence](#6-operators-and-precedence)
7. [Control flow](#7-control-flow)
8. [Functions and parameter passing](#8-functions-and-parameter-passing)
9. [Arrays, structs, and typedef](#9-arrays-structs-and-typedef)
10. [`match`, `guard`, and exceptions](#10-match-guard-and-exceptions)
11. [Interpolated strings](#11-interpolated-strings)
12. [Input and output](#12-input-and-output)
13. [What the language does not have](#13-what-the-language-does-not-have)

---

## 1. Hello world

```c
void main() {
    print("Hello, world!");
}
```

Save it as `hello.src` and run it:

```bash
python interpreter.py hello.src
```

Every program needs a `main` function returning `void`, and `main` must be the
**last** function in the file.

---

## 2. Lexical structure

**Comments** are C-style. Line comments run to end of line; block comments do
not nest.

```c
// a line comment

/* a block comment,
   which may span lines */

void main() {
    print("comments are ignored");
}
```

**Literals**

| Kind | Examples |
|---|---|
| Integer | `0`, `42`, `20220001` |
| Float | `3.14159`, `2.0` |
| Character | `'A'`, `'-'`, `'\n'` |
| String | `"Result"`, `"Line1\nLine2"` |
| Interpolated string | `` `Hello, {name}!` `` |
| Boolean | `true`, `false` |

**Escape sequences**, valid in both character and string literals:

| Escape | Meaning |
|---|---|
| `\n` | newline |
| `\t` | tab |
| `\r` | carriage return |
| `\\` | backslash |
| `\'` | single quote |
| `\"` | double quote |
| `\0` | NUL |
| `\{` `\}` | literal brace (for interpolated strings) |

Any other escape is a lexical error.

**Identifiers** start with a letter or underscore and continue with letters,
digits, or underscores. A bare `_` is not an identifier — it is the *discard*
token (see [§4](#4-declarations)).

**Reserved keywords** — none of these may be used as an identifier:

```
int    float   char    string  bool    void    const   val     var
struct let     typedef if      else    for     while   repeat  until
return break   continue true   false   print   input   in      match
guard  try     catch   finally throw   _
```

---

## 3. Program structure

The group defines the ordering of declarations and code explicitly, and the
parser enforces it. Three rules:

**1. Only `const`, `struct`, and `typedef` may appear at global scope.** There
are no global variables — `val` and `var` belong inside a function.

```c
const int MAX_TRIES = 3;

struct Point {
    int x;
    int y;
};

typedef int Meters;

void main() {
    print("globals are const/struct/typedef only:", MAX_TRIES);
}
```

Writing `val int g = 5;` at global scope gives:

```
[SYNTAX ERROR] Line 2, Col 1: 'val' declarations are not allowed at global scope (found 'val')
```

**2. Inside a block, all declarations come before any statement.** Once the
first statement appears, the declaration section is closed.

```c
void main() {
    var int x;        // declaration section
    var int y;
    x = 1;            // statement section starts here
    y = x + 1;
    print(y);
}
```

Declaring after a statement gives:

```
[SYNTAX ERROR] Line 5, Col 5: Variable declarations must appear before statements in a block (found 'var')
```

**3. `main` must be the last function declared, and must return `void`.**
Helper functions go above it.

```c
int helper() {
    return 1;
}

void main() {
    print("helper says", helper());
}
```

Putting a function after `main` gives:

```
[SYNTAX ERROR] Line 5, Col 5: Expected 'main' as the last function declaration, found 'helper' (found 'helper')
```

---

## 4. Declarations

There are four binding forms.

| Form | Mutable? | Where | Initializer |
|---|---|---|---|
| `const` | no | global only | required, must be a **literal** |
| `val` | no | inside a function | required |
| `var` | yes | inside a function | optional (zero-initialized) |
| `let` | yes | inside a function | required, type inferred |

```c
const float PI = 3.14159;

void main() {
    val string label = "Result";   // immutable, type written out
    var int count;                 // mutable, starts at 0
    let total = 10;                // mutable, type inferred as int

    count = count + 1;
    print(label, count, total, PI);
}
```

A `var` with no initializer is zero-initialized: `0` for `int`, `0.0` for
`float`, `false` for `bool`, `""` for `string`, and NUL for `char`.

You may declare several names at once, and `var`/`val` may carry an
initializer per declarator:

```c
void main() {
    var int a, b, c;
    val int lo = 1, hi = 9;
    a = lo;
    b = hi;
    c = a + b;
    print(a, b, c);
}
```

### `val` and `const` freeze the *binding*, not the contents

This is the single most important thing to know about immutability here.
Rebinding the name is rejected, but mutating an element or field *through* the
name is allowed. This is why `prog2_loops_arrays.src` can bubble-sort a
`val int scores[5]`.

```c
void main() {
    val int scores[3] = {30, 10, 20};

    scores[0] = 99;        // allowed - mutating the contents
    print("mutated:", scores[0]);

    // scores = ...;       // rejected - rebinding the name
}
```

Attempting the rebind gives
`[SEMANTIC ERROR] Cannot assign to immutable variable 'scores'`.

### Discarding with `_`

`let` and multiple-assignment can throw away a position instead of binding it.
The value is still computed (so side effects still happen); it is just never
stored.

```c
(int, int) divmod(int a, int b) {
    return a / b, a % b;
}

void main() {
    let q, _ = divmod(17, 5);   // keep the quotient, discard the remainder
    print("quotient:", q);
}
```

---

## 5. Types and coercion

**Primitives:** `int`, `float`, `char`, `string`, `bool`. Plus `void` for
functions that return nothing.

**Composites:** arrays, structs, and tuples (the type of a multi-value return).

`int` is arbitrary-precision (it inherits Python's integers), and `float` is a
double.

### Coercion is deliberately minimal

The **only** implicit conversions are `char → int → float`. Everything else
must match exactly.

```c
void main() {
    var float f;
    var int i;

    i = 'A';        // char -> int, gives 65
    f = i;          // int -> float
    print(i, f);
}
```

There is **no cast syntax at all**, and notably no `float → int` conversion —
so a `float` can never be assigned to an `int`, and a `string` never converts
to a number. If you need truncation, use integer division.

---

## 6. Operators and precedence

From lowest precedence to highest:

| Level | Operators | Associativity |
|---|---|---|
| Assignment | `=` | right |
| Logical OR | `\|\|` | left |
| Logical AND | `&&` | left |
| Equality | `==` `!=` | left |
| Comparison | `<` `>` `<=` `>=` | left |
| Range | `..` | left |
| Additive | `+` `-` | left |
| Multiplicative | `*` `/` `%` | left |
| Unary | `!` `-` `+` | right |
| Postfix | `f(...)` `a[i]` `s.field` | left |

Parentheses group as usual. The grammar handles arbitrarily complex
expressions:

```c
void main() {
    var int x, y, z;
    var float result;
    var bool flag;

    x = 10;
    y = 20;
    z = 30;

    result = (x + y) * z / (x - 5) + 3.0;
    flag = (x > 5 && y < 100) || (z == 30 && !(x == y));

    print("result:", result);
    print("flag:", flag);
}
```

### Four rules that will bite you

**1. `/` and `%` on integers truncate toward zero (C-style).** `%` takes the
sign of the *dividend*, not the divisor (which is where it differs from
Python).

```c
void main() {
    print("7 / 2   =", 7 / 2);      // 3, not 3.5
    print("-7 % 3  =", -7 % 3);     // -1  (sign of the dividend)
    print("7 % -3  =", 7 % -3);     // 1
}
```

To get a real quotient, make an operand a float: `7 / 2.0`.

**2. `+` is numeric only — there is no string concatenation.** Use an
interpolated string ([§11](#11-interpolated-strings)) to join text.

**3. `&&` and `||` short-circuit.** The right operand is not evaluated when the
left already decides the result.

**4. `..` builds an inclusive range, and both operands must be integer
*literals*** — not variables, not expressions. `0..4` visits 0, 1, 2, 3, **and
4**.

---

## 7. Control flow

Every body requires braces `{ }`. There is no brace-less single-statement form.

### `if` / `else`

There is no `else if` keyword; nest an `if` inside the `else` block.

```c
void main() {
    var int score;
    score = 82;

    if (score >= 90) {
        print("Grade: A");
    } else {
        if (score >= 75) {
            print("Grade: B");
        } else {
            print("Grade: C");
        }
    }
}
```

### `for` — three forms

```c
void main() {
    val int scores[5] = {95, 82, 77, 90, 68};
    var int i, s;

    // 1. C-style. Init and update may each be omitted.
    for (i = 0; i < 5; i = i + 1) {
        print("C-style:", scores[i]);
    }

    // 2. Over an inclusive integer-literal range.
    for (i in 0..4) {
        print("range:", scores[i]);
    }

    // 3. Over a collection, binding each element.
    for (s in scores) {
        print("collection:", s);
    }
}
```

### `while` and `repeat`/`until`

`repeat` always executes its body at least once and loops *until* the condition
becomes true — the opposite sense of `while`.

```c
void main() {
    var int n;

    n = 0;
    while (n < 3) {
        print("while:", n);
        n = n + 1;
    }

    n = 0;
    repeat {
        print("repeat:", n);
        n = n + 1;
    } until (n >= 3);
}
```

### `break` and `continue`

Both apply to the innermost enclosing loop.

```c
void main() {
    var int i;
    for (i = 0; i <= 20; i = i + 2) {
        if (i % 6 == 0) {
            continue;
        }
        if (i > 18) {
            break;
        }
        print(i);
    }
}
```

Nesting is unrestricted — loops, conditionals, and functions may all be nested
to any depth.

---

## 8. Functions and parameter passing

A function is `<return type> <name>(<params>) { ... }`. Parameters are typed and
comma-separated. Recursion is supported.

```c
int factorial(int n) {
    if (n <= 1) {
        return 1;
    }
    return n * factorial(n - 1);
}

void main() {
    print("5! =", factorial(5));
}
```

A non-`void` function must return a value on **every** path — an `if` with no
`else` that falls off the end is a semantic error, not a silent `null`.

### Parameter passing scheme

This is the scheme the group defined:

| Argument type | Passing | Effect of assigning to the parameter |
|---|---|---|
| `int`, `float`, `char`, `string`, `bool` | **by value** | invisible to the caller |
| arrays | **by reference** | visible to the caller |
| structs | **by reference** | visible to the caller |

A scalar parameter is a private copy, so the callee cannot modify the caller's
variable through it — return a value instead. Arrays and structs are shared, so
writing to an element or field *is* visible to the caller.

```c
struct Box {
    int v;
};

void mutate_scalar(int n) {
    n = 999;
}

void mutate_array(int a[]) {
    a[0] = 999;
}

void mutate_struct(struct Box b) {
    b.v = 999;
}

void main() {
    var int s;
    var int arr[3];
    var struct Box bx;

    s = 1;
    arr[0] = 1;
    bx.v = 1;

    mutate_scalar(s);
    mutate_array(arr);
    mutate_struct(bx);

    print("scalar (by value, unchanged):", s);
    print("array  (by reference, changed):", arr[0]);
    print("struct (by reference, changed):", bx.v);
}
```

This prints `1`, `999`, `999`. The behavior is pinned by
`tests/param_passing.src`.

An array parameter is declared with empty brackets and its length passed
alongside, since arrays do not carry their size:

```c
int array_sum(int arr[], int len) {
    var int total, i;
    total = 0;
    for (i = 0; i < len; i = i + 1) {
        total = total + arr[i];
    }
    return total;
}

void main() {
    val int data[6] = {3, 7, 1, 9, 4, 6};
    print("sum:", array_sum(data, 6));
}
```

### Multiple return values

Declare the return type as a parenthesized tuple and `return` several
comma-separated values.

```c
(int, int) swap(int a, int b) {
    return b, a;
}

(int, int, int) three() {
    return 1, 2, 3;
}

void main() {
    let x, y = swap(10, 20);
    print("swapped:", x, y);

    int a, b, c = three();
    print("three:", a, b, c);
}
```

Destructuring comes in several spellings, all equivalent in effect:

```c
(int, int) divmod(int a, int b) {
    return a / b, a % b;
}

void main() {
    let q1, r1 = divmod(17, 5);        // let, inferred
    let q4, r4 = 3, 2;                 // let, from an expression list

    int q2, r2 = divmod(17, 5);        // one shared type
    int q3, int r3 = divmod(17, 5);    // a type per name

    print(q1, r1);
    print(q2, r2);
    print(q3, r3);
    print(q4, r4);
}
```

Note the ordering above: a `let` is a **declaration**, while the typed forms
(`int q2, r2 = ...`) are **statements**. By the rule in
[§3](#3-program-structure) every `let` must therefore appear before the first
typed destructuring in the same block — putting a `let` after one gives
`Variable declarations must appear before statements in a block`.

### Named arguments and default values

Arguments may be passed by parameter name, in any order. Trailing parameters
may declare a default, which must be a **bare literal**.

```c
string greet(string name, int count = 0) {
    return `Hello, {name}! You have {count} messages.`;
}

void main() {
    print(greet("Alonzo", 4));                  // positional
    print(greet(name: "World", count: 1));      // named
    print(greet(count: 2, name: "Reordered"));  // named, out of order
    print(greet("Default"));                    // count omitted -> 0
}
```

---

## 9. Arrays, structs, and typedef

### Arrays

Arrays are fixed-length and zero-indexed. An out-of-bounds index is a runtime
error, not silent corruption.

```c
void main() {
    val int primes[4] = {2, 3, 5, 7};   // with an initializer
    var int buf[8];                     // zero-filled
    var int grid[3][3];                 // two-dimensional

    buf[0] = primes[3];
    grid[1][2] = 42;

    print(primes[0], buf[0], grid[1][2]);
}
```

The **outermost** dimension may be a runtime value rather than a literal. The
size must come from something already available in the declaration section —
give it an initializer, since a plain assignment would be a statement and would
close the declaration section ([§3](#3-program-structure)):

```c
void main() {
    var int n = 4;       // initialized here, not by a later statement
    var int arr[n];      // size computed at runtime

    arr[0] = 1;
    print("dynamic size:", n, "first:", arr[0]);
}
```

Only that outermost dimension may be dynamic — in `int grid[3][n]` the `n` is
fine, but `int grid[n][3]` is rejected.

### Structs

A struct groups named fields. Declare the type at global scope, then declare
variables of it with `struct <Name>`. Fields are reached with `.`.

```c
struct Student {
    string name;
    int id;
    float gpa;
    bool enrolled;
};

void main() {
    var struct Student s;

    s.name = "Alonzo";
    s.id = 20220001;
    s.gpa = 3.75;
    s.enrolled = true;

    print(s.name, s.id, s.gpa, s.enrolled);
}
```

Several fields may share one line (`int x, y;`), a struct may contain an array,
and an array may contain structs. A struct may **not** contain itself by value,
directly or through a chain — that would be infinitely large.

A function may return a struct:

```c
struct Point {
    int x;
    int y;
};

struct Point make(int x, int y) {
    var struct Point p;
    p.x = x;
    p.y = y;
    return p;
}

void main() {
    var struct Point p;
    p = make(3, 4);
    print(p.x, p.y);
}
```

### `typedef`

Three forms: alias a primitive, alias a struct, or define a struct and alias it
in one statement.

```c
typedef int StudentID;                              // alias a primitive
typedef struct Color { int r; int g; int b; } RGB;  // define + alias
typedef struct Color Palette;                       // alias an existing struct

void show(StudentID id) {
    print("id:", id);
}

void main() {
    var RGB c;
    var Palette p;

    c.r = 255;
    p.g = 128;

    show(20220001);
    print(c.r, p.g);
}
```

An alias is interchangeable with the type it names — `StudentID` *is* `int`.

---

## 10. `match`, `guard`, and exceptions

### `match` as a statement

Arms are separated by **semicolons**. A pattern may be a literal, an inclusive
range, or a boolean expression tested against the subject. `_` is the wildcard
and must come last if present.

```c
void handle(int code) {
    match (code) {
        0 => print("OK");
        1 => print("Warning");
        3..9 => print("Unknown error code");
        code > 9 => print("Very high code");
        _ => print("Unrecognized");
    }
}

void main() {
    handle(0);
    handle(5);
    handle(42);
}
```

### `match` as an expression

Arms are separated by **commas**, and the whole `match` produces a value. This
comma-vs-semicolon difference between the two forms is the most common syntax
mistake in the language.

```c
void main() {
    var int status;
    var string severity;

    status = 1;
    severity = match (status) {
        0 => "low",
        1 => "medium",
        _ => "high"
    };

    print("severity:", severity);
}
```

### `guard`

An early-exit check. The condition needs parentheses, and the `else` block must
leave the enclosing block — by `return`, `break`, `continue`, or `throw`.

```c
string classify(int n) {
    guard (n != 0) else {
        return "zero";
    }
    if (n > 0) {
        return "positive";
    }
    return "negative";
}

void main() {
    print(classify(0));
    print(classify(7));
    print(classify(-3));
}
```

### `try` / `catch` / `finally` and `throw`

**Thrown values must be strings.** A `try` takes exactly one `catch` (catch
clauses carry no exception type, so a second could never be reached); `finally`
is optional and runs on both the normal and the exceptional path.

```c
void validate(int id) {
    guard (id > 0) else {
        throw "Invalid ID";
    }
    print("ID", id, "is valid.");
}

void main() {
    try {
        validate(42);
        validate(-5);       // throws
        print("not reached");
    } catch (e) {
        print("Caught:", e);
    } finally {
        print("Always runs.");
    }
}
```

An uncaught `throw` ends the program as a runtime error with exit status `3`.

---

## 11. Interpolated strings

Because `+` does not concatenate, interpolated strings are how text is built.
They use **backticks**, and `{ ... }` embeds an arbitrary expression — including
a call, arithmetic, an index, or a struct field.

```c
struct Student {
    string name;
    float gpa;
};

int double_it(int n) {
    return n * 2;
}

void main() {
    var struct Student s;
    val int nums[3] = {7, 8, 9};
    var int count;

    s.name = "Alonzo";
    s.gpa = 3.75;
    count = 4;

    print(`Student: {s.name}  GPA: {s.gpa}`);
    print(`Arithmetic: {count * 2 + 1}`);
    print(`Call: {double_it(count)}`);
    print(`Index: {nums[1]}`);
    print(`Escaped braces: \{literal\}`);
}
```

---

## 12. Input and output

`print` takes any number of comma-separated arguments and writes them
space-separated with a trailing newline.

`input` reads whitespace-separated values into one or more variables, parsing
each according to the variable's declared type. Targets must be scalars — you
cannot read directly into an array or struct. A value that does not parse is a
runtime error.

```c
void main() {
    var int a, b;
    var string name;

    print("Enter two integers:");
    input(a, b);

    print("Enter a name:");
    input(name);

    print(`{name}: {a} + {b} = {a + b}`);
}
```

Input is read from a token buffer that pulls fresh lines as needed, so values
may be spread across lines however the user types them. To supply input
non-interactively, redirect a file:

```bash
python interpreter.py myprogram.src < myprogram.in
```

---

## 13. What the language does not have

Deliberate scope limits, so you know what not to reach for:

- **No modules or imports.** A program is a single `.src` file.
- **No standard library** beyond `print` and `input`. Sorting, math helpers,
  and string utilities must be hand-written — `prog2_loops_arrays.src` includes
  a bubble sort.
- **No casts.** Only the implicit `char → int → float` promotions exist.
- **No string concatenation operator.** Use interpolated strings.
- **No pointers** (a bonus item, not implemented).
- **No `else if` keyword**, no ternary operator, no compound assignment
  (`+=`), and no `++`/`--`.

---

## Where to look next

- [`README.md`](README.md) — running the interpreter, CLI flags, output formats.
- `prog1_calculator.src` … `prog5_advanced.src` — the five sample programs,
  which between them exercise every construct in this manual.
- `tests/` — small focused programs, one feature each.
- [`LIMITATIONS.md`](LIMITATIONS.md) — design decisions and known edge cases.
