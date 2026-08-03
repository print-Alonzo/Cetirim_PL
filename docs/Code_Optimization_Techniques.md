| CODE OPTIMIZATION |
| :---: |

**CONSTANT PROPAGATION**

* The idea behind this stem is that, instead of performing a lookup every time the value of a static variable is accessed, we substitute its value to reduce lookup count.   
  * A static variable is a variable whose value never changes. It stays the same throughout the program.

**ALGEBRAIC SIMPLIFICATION**

* uses mathematical identities to simplify and eliminate unnecessary computations within basic blocks.

**COPY PROPAGATION**

* deals with assignments of the form u \= v, which are simply known as copy statements.   
* The core idea behind this technique is to use v in place of u wherever possible after the copy statement u \= v has been executed  

**COMMON SUBEXPRESSION ELIMINATION**

* The compiler avoids recalculating an expression that has already been evaluated.   
* An occurrence of an expression is identified as a "common subexpression" if it was previously computed and the values of its constituent variables have not changed since that last computation 

**DEAD CODE ELIMINATION**

* removes statements or instructions computing values that are never used.   
* In this context, a variable is considered "dead" at a specific point in a program if its value will not be accessed subsequently 

**LOOP INVARIANT REMOVAL**

* An optimization that decreases the amount of code executed within a loop by identifying expressions that yield the exact same result on every iteration.

**STRENGTH REDUCTION**

* An optimization transformation that replaces a computationally expensive operation with a cheaper, equivalent one.  
  * Common examples include:  
    * Exponentiation to multiplication: Replacing x2 with x∗x, which avoids a highly expensive call to an exponentiation routine  
    * Multiplication to addition: Replacing 2∗x with x+x  
    * Division to multiplication: Replacing x/2 with x∗0.5, or approximating floating-point division by a constant with a cheaper multiplication by a constant  
    * Multiplication/division to bit shifts: Replacing fixed-point multiplication or division by a power of two with highly efficient machine shift operations  