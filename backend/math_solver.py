"""
Solve the equation the AI read off the board, using SymPy so the math is exact.

Cases:
  "12+7"        -> evaluate  -> "19"         (written after the = sign)
  "2*x+3=7"     -> solve     -> "x=2"        (written under the equation)
  "x^2-2x-3"    -> solve     -> "x=-1,3"     (a polynomial with no equals sign means = 0)
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
    # A ? in a sentence is a question mark. A ? in "5+?" is a blank.
    if len(re.findall(r"[A-Za-z]{2,}", expr)) < 2:
        expr = expr.replace("?", "_")
    expr = re.sub(r"_+", " BLANK ", expr)
    # "3x4" or "_x4" written with an x means multiply, not the variable x
    for _ in range(2):
        expr = re.sub(r"(\d|BLANK)\s*[xX]\s*(\d|BLANK)", r"\1*\2", expr)
    return (expr.replace("×", "*").replace("÷", "/").replace("−", "-")
                .replace("·", "*").strip())


def format_complex(value) -> str:
    """-1 + 1*I  ->  "-1+i"   (so the robot can write it)"""
    re_part, im_part = (sp.nsimplify(v) for v in sp.expand_complex(value).as_real_imag())
    im_abs = format_number(abs(im_part))
    im_text = "i" if im_abs == "1" else f"{im_abs}i"
    if re_part == 0:
        return ("-" if im_part < 0 else "") + im_text
    return f"{format_number(re_part)}{'-' if im_part < 0 else '+'}{im_text}"


def format_number(value) -> str:
    value = sp.nsimplify(value) if value.is_number else value
    if value.is_Integer:
        return str(int(value))
    if value.is_Rational:
        return f"{value.p}/{value.q}"
    num = float(sp.N(value))
    text = f"{num:.4g}"
    return text


def _solve_for(eq, var) -> dict:
    """Roots of one equation. Real roots when they exist, otherwise complex ones."""
    all_solutions = sp.solve(eq, var)
    solutions = [s for s in all_solutions if s.is_real is not False]
    if solutions:
        values = ",".join(format_number(s) for s in solutions)
        return {"kind": "solve", "answer": f"{var.name}={values}"}
    if all_solutions:  # e.g. x^2+2x+2=0 has no real answers, only complex ones
        values = ", ".join(format_complex(s) for s in all_solutions)
        return {"kind": "solve", "answer": f"{var.name}={values}", "complex": True}
    raise ValueError("no solution")


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
        return _solve_for(eq, symbols[0])

    expr = expr.rstrip("=").strip()
    value = _parse(expr)
    if value.free_symbols and any(c in MISREAD_CHARS for c in expr):
        # "s+4" is almost always "5+4". Only keep a letter when it is still a variable after that.
        fixed = _parse(expr.translate(MISREADS))
        if not fixed.free_symbols:
            value = fixed
        elif len(fixed.free_symbols) < len(value.free_symbols):
            value = fixed
    if value.free_symbols:
        # "x^2-2x-3" on its own is the quadratic equation set equal to zero.
        symbols = sorted(value.free_symbols, key=lambda s: s.name)
        if len(symbols) == 1 and BLANK not in value.free_symbols:
            try:
                poly = sp.Poly(sp.expand(value), symbols[0])
            except sp.PolynomialError:
                poly = None
            if poly is not None and poly.degree() >= 1:
                return _solve_for(sp.Eq(value, 0), symbols[0])
        raise ValueError("expression has unknowns but no equation to solve")
    return {"kind": "evaluate", "answer": format_number(sp.simplify(value))}


def show_work(expression: str) -> str:
    """A few plain lines of working for the robot to write under the problem."""
    result = solve(expression)
    expr = _clean(expression).rstrip("=").strip()
    if result["kind"] == "evaluate":
        return _arithmetic_steps(expr, result["answer"])
    if result["kind"] == "solve":
        return _algebra_steps(expr, result["answer"])
    if result["kind"] == "blank":
        return f"the blank is {result['answer']}"
    return result["answer"]


def _long_multiply(a: int, b: int) -> list[str]:
    """21 x 23 is written out: each digit, each partial product, then the sum."""
    neg = (a < 0) ^ (b < 0)
    a, b = abs(a), abs(b)
    lines = [f"{a} x {b}"]
    partials = []
    for i, ch in enumerate(reversed(str(b))):
        digit = int(ch)
        place = 10 ** i
        if a >= 10 and digit:
            value = 1
            for top in reversed(str(a)):
                part = int(top) * value
                if part:
                    lines.append(f"{digit} x {part} = {digit * part}")
                value *= 10
        prod = a * digit
        lines.append(f"{a} x {digit} = {prod}")
        if place == 1:
            partials.append(prod)
        else:
            shifted = prod * place
            lines.append(f"{prod} x {place} = {shifted}")
            partials.append(shifted)
    total = partials[0]
    for part in partials[1:]:
        lines.append(f"{total} + {part} = {total + part}")
        total += part
    if neg:
        lines.append(f"= -{total}")
    return lines


def _arithmetic_steps(expr: str, answer: str) -> str:
    compact = expr.replace(" ", "")
    product = re.fullmatch(r"(\d+)\*(\d+)", compact)
    if product:
        return "\n".join(_long_multiply(int(product.group(1)), int(product.group(2))))
    parts = re.findall(r"\d+\s*/\s*\d+|\d+(?:\.\d+)?|[+\-*/^]", expr.replace("**", "^"))
    if len(parts) < 3 or not parts[0][:1].isdigit():
        return f"{expr} = {answer}"
    acc = _parse(parts[0])
    lines = []
    i = 1
    while i + 1 < len(parts):
        op, nxt = parts[i], parts[i + 1]
        if op not in "+-*/^" or not re.match(r"\d", nxt):
            return f"{expr} = {answer}"
        acc = sp.simplify(_parse(f"({format_number(acc)}){op}({nxt})"))
        lines.append(f"{lines and lines[-1].split('=')[-1].strip() or format_number(_parse(parts[0]))} {op} {nxt.replace(' ', '')} = {format_number(acc)}")
        i += 2
        if len(lines) == 5:
            break
    if not lines:
        return f"{expr} = {answer}"
    if format_number(acc) != answer:
        lines.append(f"= {answer}")
    return "\n".join(lines)


def _algebra_steps(expr: str, answer: str) -> str:
    if "=" in expr and expr.split("=", 1)[1].strip():
        lhs_text, rhs_text = expr.split("=", 1)
        diff = sp.expand(_parse(lhs_text) - _parse(rhs_text))
        first = f"{lhs_text.strip()} = {rhs_text.strip()}"
    else:
        diff = sp.expand(_parse(expr))
        first = f"{expr} = 0"
    symbols = sorted(diff.free_symbols, key=lambda s: s.name)
    if len(symbols) != 1:
        return f"{first}\n{answer}"
    var = symbols[0]
    try:
        poly = sp.Poly(diff, var)
    except sp.PolynomialError:
        return f"{first}\n{answer}"
    lines = [first]
    if poly.degree() == 1:
        coeff = poly.coeff_monomial(var)
        const = poly.coeff_monomial(1)
        if const != 0:
            lines.append(f"{_coeff_term(coeff, var.name)} = {format_number(sp.simplify(-const))}")
    elif poly.degree() == 2:
        factored = sp.factor(diff)
        if factored != diff:
            lines.append(f"{factored} = 0".replace("**", "^").replace("*", ""))
    lines.append(answer)
    return "\n".join(lines[:5])


def _coeff_term(coeff, name: str) -> str:
    text = format_number(coeff)
    if text == "1":
        return name
    if text == "-1":
        return f"-{name}"
    return f"{text}*{name}"


def classify(expression: str, result: dict) -> tuple[str, str]:
    """Name the type of problem from how SymPy solved it (more reliable than asking the AI)."""
    expr = _clean(expression)
    kind = result["kind"]
    if kind == "solve":
        if "=" in expr and expr.split("=", 1)[1].strip():
            lhs_text, rhs_text = expr.split("=", 1)
            eq = _parse(lhs_text) - _parse(rhs_text)
        else:
            eq = _parse(expr.rstrip("="))
        var = sorted(eq.free_symbols, key=lambda s: s.name)[0]
        try:
            degree = sp.Poly(eq, var).degree()
            names = {1: "linear equation", 2: "quadratic equation", 3: "cubic equation"}
            shape = names.get(degree, f"degree-{degree} polynomial equation")
        except sp.PolynomialError:
            shape = "equation"
        return "Algebra", f"{shape}, solving for {var.name}"
    if kind == "blank":
        return "Fill in the blank", "arithmetic with a missing number"
    ops = []
    body = expr.split("=")[0]
    for sym, name in (("+", "addition"), ("-", "subtraction"), ("*", "multiplication"),
                      ("/", "division"), ("^", "exponents")):
        if sym in body.replace("**", "^"):
            ops.append(name)
    if kind == "check":
        return "Arithmetic", "checking a finished equation"
    if "/" in result["answer"]:
        return "Arithmetic", "fractions"
    if len(ops) == 1:
        return "Arithmetic", ops[0]
    return "Arithmetic", "mixed operations" if ops else "a number"


def _values(text: str):
    """ "x=2, x=-3" or "2 or 3" or "-1/2"  ->  list of exact numbers """
    vals = []
    for part in re.split(r",|;|\bor\b|\band\b", str(text)):
        part = part.split("=")[-1].strip()
        if not part:
            continue
        try:
            vals.append(sp.nsimplify(_parse(_clean(part))))
        except Exception:
            pass
    return vals


def check_work(expression: str = None, equation: str = None, student_answer: str = None) -> dict:
    """Is the student's answer right? Returns {"correct": True/False/None, "correct_answer": str}.
    None means there's no answer written yet."""
    if equation:
        correct = solve(equation)["answer"]
        if not student_answer:
            return {"correct": None, "correct_answer": correct}
        lhs_text, rhs_text = _clean(equation).split("=", 1)
        eq = sp.Eq(_parse(lhs_text), _parse(rhs_text))
        var = sorted(eq.free_symbols, key=lambda s: s.name)[0]
        sols = sp.solve(eq, var)
        got = _values(student_answer)
        ok = bool(got) and len(set(got)) == len(sols) and all(any(sp.simplify(g - t) == 0 for t in sols) for g in got)
        return {"correct": ok, "correct_answer": correct}

    expr = _clean(expression)
    if "=" not in expr or not expr.split("=", 1)[1].strip():
        return {"correct": None, "correct_answer": solve(expression)["answer"]}
    lhs_text, rhs_text = expr.split("=", 1)
    lhs, rhs = _parse(lhs_text), _parse(rhs_text)
    if lhs.free_symbols or rhs.free_symbols:  # it's an equation with no answer written
        return {"correct": None, "correct_answer": solve(expression)["answer"]}
    return {"correct": sp.simplify(lhs - rhs) == 0, "correct_answer": format_number(sp.simplify(lhs))}