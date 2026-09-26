"""
Solve the equation the AI read off the board, using SymPy so the math is exact.

Two cases:
  "12+7"        -> evaluate  -> "19"         (written after the = sign)
  "2*x+3=7"     -> solve     -> "x=2"        (written under the equation)
  "5+_=9"       -> blank     -> "4"          (written in the blank)
"""
import re

import sympy as sp
from sympy.parsing.sympy_parser import (
    parse_expr, standard_transformations,
    implicit_multiplication_application, convert_xor,
)

TRANSFORMS = standard_transformations + (implicit_multiplication_application, convert_xor)


BLANK = sp.Symbol("BLANK")

# Letters that handwritten digits commonly get misread as
MISREADS = str.maketrans({"s": "5", "S": "5", "z": "2", "Z": "2", "l": "1", "I": "1", "|": "1",
                          "o": "0", "O": "0", "g": "9", "q": "9", "b": "6", "G": "6", "B": "8", "T": "7"})
MISREAD_CHARS = set("sSzZlI|oOgqbGBT")


def _parse(text: str):
    return parse_expr(text, transformations=TRANSFORMS, local_dict={"BLANK": BLANK})


def _clean(expr: str) -> str:
    expr = expr.replace("?", "_")
    expr = re.sub(r"_+", " BLANK ", expr)
    # "3x4" or "_x4" written with an x means multiply, not the variable x
    for _ in range(2):
        expr = re.sub(r"(\d|BLANK)\s*[xX]\s*(\d|BLANK)", r"\1*\2", expr)
    return (expr.replace("×", "*").replace("÷", "/").replace("−", "-")
                .replace("·", "*").strip())


def format_number(value) -> str:
    value = sp.nsimplify(value) if value.is_number else value
    if value.is_Integer:
        return str(int(value))
    if value.is_Rational:
        return f"{value.p}/{value.q}"
    num = float(sp.N(value))
    text = f"{num:.4g}"
    return text


def solve(expression: str) -> dict:
    """Returns {"kind": "evaluate"|"solve", "answer": str}. Raises ValueError if unreadable."""
    expr = _clean(expression)
    if "=" in expr and expr.split("=", 1)[1].strip():
        lhs_text, rhs_text = expr.split("=", 1)
        lhs, rhs = _parse(lhs_text), _parse(rhs_text)
        eq = sp.Eq(lhs, rhs)
        if BLANK in eq.free_symbols:
            solutions = [s for s in sp.solve(eq, BLANK) if s.is_real is not False]
            if not solutions:
                raise ValueError("nothing fits in the blank")
            return {"kind": "blank", "answer": format_number(solutions[0])}
        symbols = sorted(eq.free_symbols, key=lambda s: s.name)
        if not symbols:
            return {"kind": "check", "answer": "✓" if sp.simplify(lhs - rhs) == 0 else "✗"}
        var = symbols[0]
        solutions = [s for s in sp.solve(eq, var) if s.is_real is not False]
        if not solutions:
            raise ValueError("no real solution")
        values = ",".join(format_number(s) for s in solutions)
        return {"kind": "solve", "answer": f"{var.name}={values}"}

    expr = expr.rstrip("=").strip()
    value = _parse(expr)
    if value.free_symbols:
        # "s+4=" has nothing to solve for, so the letters are probably misread digits
        fixed = _parse(expr.translate(MISREADS)) if any(c in MISREAD_CHARS for c in expr) else value
        if fixed.free_symbols:
            raise ValueError("expression has unknowns but no equation to solve")
        value = fixed
    return {"kind": "evaluate", "answer": format_number(sp.simplify(value))}