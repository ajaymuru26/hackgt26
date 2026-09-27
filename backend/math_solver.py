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
    if _OPERATOR.search(_d_over(text)):
        return _parse_raw(text).doit()
    return parse_expr(text, transformations=TRANSFORMS, local_dict={"BLANK": BLANK})


# d/dx, d/dt, d^2/dx^2 written as an operator. d/2 and 3/4 are not: the d must be followed by a letter.
_OP = r"(?<![A-Za-z])d\s*(?:\^\s*(\d+)\s*)?/\s*d\s*([A-Za-z])"
_OPERATOR = re.compile(_OP)
_NEXT_OPERATOR = re.compile(r"[+\-]\s*(?=" + _OP.replace("(\\d+)", "\\d+").replace("([A-Za-z])", "[A-Za-z]") + ")")


def _closing(text: str, start: int) -> int:
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("unbalanced parentheses")


def _leibniz(text: str) -> str:
    """ "d/dx (x^2)" or "d/dx x^2" or "d(x^2)/dx"  ->  "Derivative((x^2),x)" so SymPy differentiates it."""
    text = _d_over(text.replace("[", "(").replace("]", ")").replace("{", "(").replace("}", ")"))
    text = re.sub(r"\(\s*(d\s*(?:\^\s*\d+\s*)?/\s*d\s*[A-Za-z](?:\s*\^\s*\d+)?)\s*\)", r"\1", text)
    while True:
        match = _OPERATOR.search(text)
        if not match:
            return text
        order, var, end = match.group(1), match.group(2), match.end()
        if order:
            power = re.match(r"\s*\^\s*\d+", text[end:])
            if power:
                end += power.end()
        rest = text[end:]
        lead = re.match(r"\s*(?:of\s+)?", rest).end()
        if rest[lead:lead + 1] == "(":
            close = _closing(rest, lead)
            operand, after = rest[lead + 1:close], rest[close + 1:]
        else:
            more = _NEXT_OPERATOR.search(rest, lead)
            cut = more.start() if more else len(rest)
            operand, after = rest[lead:cut], rest[cut:]
        if not operand.strip():
            raise ValueError("nothing to differentiate")
        count = f",{order}" if order else ""
        text = f"{text[:match.start()]}Derivative(({operand}),{var}{count}){after}"


def _d_over(text: str) -> str:
    """ "d(x^3)/dx"  ->  "d/dx(x^3)" """
    start = 0
    while True:
        match = re.compile(r"(?<![A-Za-z])d\s*\(").search(text, start)
        if not match:
            return text
        try:
            close = _closing(text, match.end() - 1)
        except ValueError:
            return text
        tail = re.match(r"\s*/\s*d\s*([A-Za-z])", text[close + 1:])
        if not tail:
            start = match.end()
            continue
        inner = text[match.end():close]
        text = f"{text[:match.start()]}d/d{tail.group(1)}({inner}){text[close + 1 + tail.end():]}"


def _parse_raw(text: str):
    """Like _parse, but a derivative stays unevaluated so the steps can show it."""
    return parse_expr(_leibniz(text), transformations=TRANSFORMS, local_dict={"BLANK": BLANK})


def _dy_dx(expr: str) -> str:
    """ "y=x^2, dy/dx" or "d(x^2)/dx"  ->  "d/dx(x^2)". dy/dx on its own is left alone."""
    expr = _d_over(expr)
    wants = r"d\s*(?P<dep>[A-Za-z])\s*/\s*d\s*(?P<var>[A-Za-z])"
    defined = r"(?P<name>[A-Za-z])\s*=\s*(?P<body>[^,;=]+?)"
    for pattern in (rf"^\s*{defined}\s*[,;:]?\s*(?:find\s+)?{wants}\s*=?\s*$",
                    rf"^\s*(?:find\s+)?{wants}\s*[,;:]?\s*(?:of\s+|for\s+|if\s+|when\s+)?{defined}\s*=?\s*$"):
        match = re.match(pattern, expr, flags=re.I)
        if match and match.group("dep") == match.group("name") and match.group("dep") != match.group("var"):
            return f"d/d{match.group('var')}({match.group('body').strip()})"
    return expr


def is_derivative(expression: str) -> bool:
    """True when the problem is a d/dx derivative, not a fraction."""
    try:
        return bool(_OPERATOR.search(_dy_dx(_clean(str(expression or "")))))
    except Exception:
        return False


def format_expr(value) -> str:
    """3*x**2 + 2  ->  "3x^2+2"   (so the robot can write it)"""
    if value.is_number:
        return format_number(value)
    text = str(value).replace("**", "^").replace(" ", "")
    text = re.sub(r"exp\(([A-Za-z0-9]+)\)", r"e^\1", text)
    return re.sub(r"(?<=[\d)])\*(?=[A-Za-z(])", "", text)


def _tidy(value, var):
    try:
        sp.Poly(value, var)
        return sp.expand(value)
    except sp.PolynomialError:
        return sp.simplify(value)


def _operator_name(var, order: int = 1) -> str:
    return f"d/d{var}" if order == 1 else f"d^{order}/d{var}^{order}"


def _solve_derivative(expr: str) -> dict:
    left, _, right = expr.partition("=")
    if not _OPERATOR.search(left) and _OPERATOR.search(right):
        left, right = right, left
    raw = _parse_raw(left)
    variables = sorted(raw.atoms(sp.Derivative), key=str)
    var = variables[0].variables[0] if variables else None
    value = raw.doit()
    value = _tidy(value, var) if var is not None else sp.simplify(value)
    if right.strip():
        same = sp.simplify(value - _parse(right)) == 0
        return {"kind": "check", "answer": "✓" if same else "✗"}
    return {"kind": "derivative", "answer": format_expr(value), "var": var.name if var is not None else ""}


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


_INTEGRAL = re.compile(r"∫|\bintegral\b|\bint\s*[_(^]", re.I)


def solve(expression: str) -> dict:
    """Returns {"kind": "evaluate"|"solve"|"derivative"|..., "answer": str}. Raises ValueError if unreadable."""
    if _INTEGRAL.search(str(expression or "")):
        # the _ in int_0^1 would otherwise be read as a blank to fill in
        raise ValueError("integrals are not solved here")
    expr = _dy_dx(_clean(expression))
    if _OPERATOR.search(expr):
        return _solve_derivative(expr.rstrip("=").strip() if expr.count("=") == 1 and expr.endswith("=") else expr)
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


def relevant_formula(expression: str) -> str:
    """A formula the robot should write, or "" when the steps are enough on their own.

    Plain arithmetic does not get one. A quadratic does, with a, b, and c filled in.
    """
    text = _dy_dx(_clean(expression or ""))
    if not text:
        return ""
    if _OPERATOR.search(text):
        return _derivative_formula(text)
    if "=" in text and text.split("=", 1)[1].strip():
        left, right = text.split("=", 1)
        value = sp.expand(_parse(left) - _parse(right))
    else:
        value = sp.expand(_parse(text.rstrip("=")))
    symbols = [s for s in sorted(value.free_symbols, key=lambda s: s.name) if s != BLANK]
    if len(symbols) != 1:
        return ""
    var = symbols[0]
    try:
        poly = sp.Poly(value, var)
    except sp.PolynomialError:
        return ""
    if poly.degree() != 2:
        return ""
    a, b, c = (sp.simplify(coeff) for coeff in poly.all_coeffs())
    return (f"formula: {var.name} = (-b +- sqrt(b^2 - 4ac)) / (2a)\n"
            f"a = {format_number(a)}, b = {format_number(b)}, c = {format_number(c)}")


def show_work(expression: str, limit: int | None = 5) -> str:
    """Plain lines of working for the robot to write under the problem.

    limit is how many arithmetic lines to keep. None keeps every step.
    """
    result = solve(expression)
    expr = _dy_dx(_clean(expression)).rstrip("=").strip()
    if _OPERATOR.search(expr):
        return _derivative_steps(expr, limit)
    if result["kind"] == "evaluate":
        return _arithmetic_steps(expr, result["answer"], limit)
    if result["kind"] == "solve":
        return _algebra_steps(expr, result["answer"], limit)
    if result["kind"] == "blank":
        return f"the blank is {result['answer']}"
    return result["answer"]


def _derivative_formula(text: str) -> str:
    """The power rule when the thing being differentiated has a power in it, otherwise nothing."""
    try:
        raw = _parse_raw(text.split("=", 1)[0])
    except Exception:
        return ""
    for part in raw.atoms(sp.Derivative):
        var = part.variables[0]
        if any(p.base == var for p in part.expr.atoms(sp.Pow)):
            return f"formula: d/d{var.name} ({var.name}^n) = n{var.name}^(n-1)"
    return ""


def _derivative_steps(expr: str, limit: int | None = 5) -> str:
    """d/dx (x^3+2x): each term's derivative, then the whole thing."""
    left = next((side.strip() for side in expr.split("=") if _OPERATOR.search(side)), expr)
    raw = _parse_raw(left)
    parts = sorted(raw.atoms(sp.Derivative), key=str)
    var = parts[0].variables[0] if parts else None
    answer = format_expr(_tidy(raw.doit(), var) if var is not None else sp.simplify(raw.doit()))
    if not isinstance(raw, sp.Derivative):
        return f"{left} = {answer}"
    order = len(raw.variables)
    operand = raw.expr
    name = _operator_name(var.name, order)
    lines = []
    if order > 1:
        current = operand
        for _ in range(order):
            nxt = _tidy(sp.diff(current, var), var)
            lines.append(f"{_operator_name(var.name)} ({format_expr(current)}) = {format_expr(nxt)}")
            current = nxt
    else:
        terms = operand.as_ordered_terms() if isinstance(operand, sp.Add) else [operand]
        if 1 < len(terms) <= 4:
            for term in terms:
                lines.append(f"{name} ({format_expr(term)}) = {format_expr(_tidy(sp.diff(term, var), var))}")
    lines.append(f"{name} ({format_expr(operand)}) = {answer}")
    if limit is not None and len(lines) > limit:
        lines = lines[:max(limit - 1, 0)] + lines[-1:]
    return "\n".join(lines)


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


def _arithmetic_steps(expr: str, answer: str, limit: int | None = 5) -> str:
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
        if limit is not None and len(lines) == limit:
            break
    if not lines:
        return f"{expr} = {answer}"
    if format_number(acc) != answer:
        lines.append(f"= {answer}")
    return "\n".join(lines)


def _algebra_steps(expr: str, answer: str, limit: int | None = 5) -> str:
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
    if limit is not None and len(lines) > limit:
        lines = lines[:max(limit - 1, 0)] + lines[-1:]
    return "\n".join(lines)


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
    if kind == "derivative" or is_derivative(expr):
        var = result.get("var") or "x"
        return "Calculus", f"derivative with respect to {var}"
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


def grade_submission(expression: str = None, equation: str = None,
                     student_answer: str = None, work: str = None) -> dict | None:
    """Read the problem, the steps, and the answer they wrote.

    status is "correct", "wrong", or "missing". None means the line could not be checked.
    This never raises.
    """
    try:
        return _grade_submission(expression, equation, student_answer, work)
    except Exception:
        return None


def _blank(text) -> str:
    text = " ".join(str(text or "").split())
    return "" if text.lower() in ("null", "none") else text


def _chunks(text) -> list[str]:
    if isinstance(text, list):
        text = "\n".join(str(part) for part in text)
    return [line for line in (_blank(part) for part in re.split(r"[\n;]+", str(text or ""))) if line]


def _final_answer(line: str) -> str:
    """A finished result such as 9 or x=2, not another line of working."""
    text = _clean(line).replace(" ", "")
    if re.fullmatch(r"-?\d+(?:\.\d+)?(?:/\d+)?", text):
        return line.strip()
    match = re.fullmatch(r"([A-Za-z])=(.+)", text)
    if not match:
        return ""
    rhs = re.split(r"or", match.group(2))[0]
    if re.fullmatch(r"-?\d+(?:\.\d+)?(?:/\d+)?", rhs):
        return line.strip()
    return ""


def _algebraic(text: str) -> bool:
    try:
        body = _clean(text)
        if "=" in body:
            left, right = body.split("=", 1)
            return bool((_parse(left).free_symbols if left.strip() else set())
                        or (_parse(right).free_symbols if right.strip() else set()))
        return bool(_parse(body.rstrip("=")).free_symbols)
    except Exception:
        return bool(re.search(r"[A-Za-z]", text or ""))


def _safe_solve(text: str):
    text = _blank(text)
    if not text:
        return None
    try:
        return solve(text)
    except Exception:
        return None


def _answers_match(student: str, correct_answer: str) -> bool:
    got, expected = _values(student), _values(correct_answer)
    if got and expected:
        if len(got) != len(expected):
            return False
        used = [False] * len(expected)
        for item in got:
            hit = False
            for i, target in enumerate(expected):
                if not used[i] and sp.simplify(item - target) == 0:
                    used[i] = True
                    hit = True
                    break
            if not hit:
                return False
        return True
    left = re.sub(r"\s+", "", str(student).lower())
    right = re.sub(r"\s+", "", str(correct_answer).lower())
    return bool(left) and left == right


def _gives(line: str, answer: str) -> bool:
    try:
        return _answers_match(line, answer)
    except Exception:
        return re.sub(r"\s+", "", line.split("=")[-1]) == re.sub(r"\s+", "", answer.split("=")[-1])


def answer_last(work: str, answer: str) -> str:
    """Lines of working that end on the answer. A bare answer written before the steps
    (x = 3 on the first line) is moved to the end instead of being written twice."""
    lines = [line for line in str(work or "").splitlines() if line.strip()]
    answer = str(answer or "").strip()
    if not answer or answer.lower() in ("null", "none", "✓", "✗"):
        return "\n".join(lines)
    if lines and _gives(lines[-1], answer):
        return "\n".join(lines)
    lines = [line for line in lines if not (_final_answer(line) and _gives(line, answer))]
    return "\n".join(lines + [answer])


def _assignment(correct_answer: str) -> dict:
    subs = {}
    for piece in re.split(r"\bor\b|,", _clean(str(correct_answer or ""))):
        match = re.match(r"\s*([A-Za-z])\s*=\s*(.+)", piece)
        if not match:
            continue
        try:
            subs[sp.Symbol(match.group(1))] = _parse(match.group(2))
        except Exception:
            continue
    return subs


def _step_holds(step: str, subs: dict):
    """True when a written step is true, False when it is not, None when it is not an equation."""
    if "=" not in step:
        return None
    left, right = _clean(step).split("=", 1)
    if not right.strip():
        return None
    diff = sp.simplify(_parse(left) - _parse(right))
    if _OPERATOR.search(_d_over(step)):
        return bool(diff == 0)
    if subs:
        diff = sp.simplify(diff.subs(subs))
    if diff.free_symbols:
        return None
    return bool(sp.simplify(diff) == 0)


def _bad_step(steps, correct_answer: str) -> str:
    subs = _assignment(correct_answer)
    for step in steps:
        try:
            holds = _step_holds(step, subs)
        except Exception:
            continue
        if holds is False:
            return step
    return ""


def _pack_grade(status: str, correct_answer: str, note: str, detail: str, missing: str = "") -> dict:
    return {
        "status": status,
        "correct_answer": correct_answer,
        "missing": missing,
        "note": " ".join(note.split())[:90],
        "detail": " ".join(detail.split()),
    }


def _wrong_step(step: str, correct_answer: str, finished: bool) -> dict:
    if finished:
        return _pack_grade(
            "wrong", correct_answer,
            f"this step is wrong: {step}",
            f"The answer {correct_answer} is right, but this step is not: {step}.",
        )
    return _pack_grade(
        "wrong", correct_answer,
        f"that step is wrong. Answer: {correct_answer}",
        f"You wrote {step}, and that does not follow. The answer is {correct_answer}.",
    )


def _missing_answer(problem: str, steps, correct_answer: str) -> dict:
    if steps:
        last = steps[-1]
        detail = f"You got as far as {last}. The answer {correct_answer} is still missing."
        note = f"stopped at {last}. Still missing {correct_answer}"
    else:
        detail = f"The answer is missing. {problem} should be {correct_answer}."
        note = f"answer is missing. It should be {correct_answer}"
    return _pack_grade("missing", correct_answer, note, detail, detail)


def _from_verdict(problem: str, steps, judged: dict) -> dict:
    answer = str(judged.get("correct_answer") or "")
    bad = _bad_step(steps, answer) if answer and answer not in ("✓", "✗") else ""
    if judged.get("correct") and bad:
        return _wrong_step(bad, answer, True)
    if judged.get("correct"):
        return _pack_grade("correct", answer, "", f"{problem} is correct.")
    bug = explain_mistake(expression=problem, student_answer=None, correct_answer=answer)
    return _pack_grade("wrong", answer, bug["note"], bug["detail"])


def _precedence_lines(expr: str) -> list[str]:
    """Multiply and divide first, then add and subtract, one line per step."""
    tokens = re.findall(r"\d+(?:\.\d+)?|[+\-*/^]", expr.replace("**", "^").replace(" ", ""))
    if len(tokens) < 3 or not tokens[0][:1].isdigit():
        return []

    def reduce(ops):
        lines = []
        i = 1
        while i < len(tokens) - 1:
            if tokens[i] in ops and tokens[i - 1][:1].isdigit() and tokens[i + 1][:1].isdigit():
                left, op, right = tokens[i - 1], tokens[i], tokens[i + 1]
                value = format_number(sp.simplify(_parse(f"({left}){op}({right})")))
                lines.append(f"{left} {op} {right} = {value}")
                tokens[i - 1:i + 2] = [value]
                i = 1
            else:
                i += 2
        return lines

    steps = reduce("*/^") + reduce("+-")
    return steps


def _worked_lines(problem: str) -> list[str]:
    """Every correct step. A finished wrong line like 2+2=5 is worked from the left side."""
    text = _blank(problem)
    if not text:
        return []
    target = text
    if not _algebraic(text) and "=" in _clean(text):
        left = _clean(text).split("=", 1)[0].strip()
        if left:
            target = left
    if not _algebraic(target):
        compact = target.replace(" ", "")
        product = re.fullmatch(r"(\d+)\*(\d+)", compact)
        if product and (len(product.group(1)) > 1 or len(product.group(2)) > 1):
            return _long_multiply(int(product.group(1)), int(product.group(2)))
        steps = _precedence_lines(target)
        if steps:
            return steps
    try:
        worked = show_work(target, limit=None)
    except Exception:
        return []
    lines = []
    for line in worked.splitlines():
        line = " ".join(line.split())
        if line and line not in ("✓", "✗"):
            lines.append(line)
    return lines


def _attach_work(grade, problem: str):
    """The board and the voice get the steps, not a one-line 'too high' or 'too low'."""
    if not grade or grade.get("status") == "correct":
        return grade
    lines = _worked_lines(problem)
    if not lines:
        return grade
    grade["note"] = "\n".join(lines)
    grade["detail"] = ". ".join(lines) + "."
    return grade


def _grade_problem(problem: str, student: str, steps) -> dict | None:
    problem, student = _blank(problem), _blank(student)
    steps = [step for step in steps if _blank(step) and step not in (problem, student)]

    def finish(grade):
        return _attach_work(grade, problem)

    if not problem and not student:
        return None

    if problem and is_derivative(problem):
        problem, student, steps = _derivative_claim(problem, student, steps)

    if problem and not student:
        try:
            judged = check_work(expression=problem)
        except Exception:
            judged = None
        if judged and judged.get("correct") is not None and not _algebraic(problem):
            return finish(_from_verdict(problem, steps, judged))

    if not student:
        for i in range(len(steps) - 1, -1, -1):
            found = _final_answer(steps[i])
            if found:
                student = found
                del steps[i]
                break

    if not student and not is_derivative(problem):
        for step in reversed(steps):
            if "=" not in step:
                continue
            try:
                claimed = check_work(expression=step)
            except Exception:
                continue
            if claimed and claimed.get("correct") is not None:
                earlier = [item for item in steps if item != step]
                return finish(_from_verdict(step, earlier, claimed))

    solved = _safe_solve(problem) if problem else None
    if solved and solved.get("kind") == "check":
        try:
            judged = check_work(expression=problem, equation=problem if _algebraic(problem) else None,
                                student_answer=student or None)
        except Exception:
            judged = None
        if judged and judged.get("correct") is not None:
            return finish(_from_verdict(problem, steps, judged))
        if judged and not student:
            solved = {"kind": "missing", "answer": judged.get("correct_answer") or ""}
        else:
            solved = _safe_solve(problem.split("=", 1)[0]) if "=" in problem else None

    answer = str((solved or {}).get("answer") or "")
    if not answer:
        return None
    bad = _bad_step(steps, answer)
    if not student:
        if bad:
            return finish(_wrong_step(bad, answer, False))
        return finish(_missing_answer(problem, steps, answer))
    if _answers_match(student, answer) and bad:
        return finish(_wrong_step(bad, answer, True))
    if _answers_match(student, answer):
        return _pack_grade("correct", answer, "", f"{problem} = {student} is correct.")
    claim = problem if "=" in problem and problem.split("=", 1)[1].strip() and not _algebraic(problem) else ""
    bug = explain_mistake(
        expression=claim or (None if _algebraic(problem) else f"{problem.split('=', 1)[0].strip()}={student}"),
        equation=problem if _algebraic(problem) else None,
        student_answer=student,
        correct_answer=answer,
    )
    return finish(_pack_grade("wrong", answer, bug["note"], bug["detail"]))


def _derivative_claim(problem: str, student: str, steps):
    """d/dx(x^2)=2x is the problem d/dx(x^2) with the answer 2x. So is a last line like = 2x."""
    problem = _dy_dx(_clean(problem))
    left, _, right = problem.partition("=")
    if not _OPERATOR.search(left) and _OPERATOR.search(right):
        left, right = right, left
    problem = left.strip()
    if not student and right.strip():
        student = right.strip()
    if not student:
        whole = _parse(problem)
        for i in range(len(steps) - 1, -1, -1):
            sides = _dy_dx(_clean(steps[i])).split("=")
            tail = sides[-1].strip()
            if not tail or _OPERATOR.search(tail) or re.fullmatch(r"d\s*[A-Za-z]\s*/\s*d\s*[A-Za-z]", tail):
                continue
            try:
                # d/dx(2x)=2 is one term of d/dx(x^3+2x), not the final answer.
                if any(_OPERATOR.search(side) and sp.simplify(_parse(side) - whole) != 0 for side in sides[:-1]):
                    continue
            except Exception:
                continue
            student = tail
            steps = steps[:i] + steps[i + 1:]
            break
    return problem, student, steps


def with_derivative(problem: str, others=()) -> str:
    """The problem y=x^2 with dy/dx written in the work is the derivative d/dx(x^2)."""
    match = re.fullmatch(r"\s*([A-Za-z])\s*=\s*(.+?)\s*", _clean(problem or ""))
    if not match:
        return problem
    name, body = match.groups()
    for text in _chunks(list(others)):
        asked = re.search(rf"d\s*{name}\s*/\s*d\s*([A-Za-z])", str(text or ""))
        if asked and asked.group(1) != name:
            return f"d/d{asked.group(1)}({body})"
    return problem


def _grade_submission(expression, equation, student_answer, work) -> dict | None:
    student = _blank(student_answer)
    problem = _blank(equation)
    steps = [line for line in _chunks(work) if line != student]
    lines = _chunks(expression)

    if not problem and len(lines) == 1 and not student and not steps:
        return _grade_problem(lines[0], "", [])

    if not problem:
        pool = list(lines)
        if not student and len(pool) >= 2 and _final_answer(pool[-1]):
            student = pool[-1]
            pool = pool[:-1]
        if pool:
            problem = pool[0]
            steps = pool[1:] + [line for line in steps if line not in pool[1:] and line not in (student, problem)]
    else:
        steps = [line for line in lines + steps if line not in (problem, student)]

    if not problem and steps:
        written = list(steps)
        if len(written) == 1 and not student:
            return _grade_problem(written[0], "", [])
        if not student and _final_answer(written[-1]):
            student = written[-1]
            written = written[:-1]
        if written:
            problem = written[0]
            steps = written[1:]
        else:
            steps = []
    if problem and not is_derivative(problem):
        problem = with_derivative(problem, [student, *steps])
    if student and not _final_answer(student) and "=" in student and not _algebraic(student):
        return _grade_problem(student, "", [line for line in steps if line != student])
    return _grade_problem(problem, student, steps)


_OP_WORD = {"+": "plus", "-": "minus", "*": "times", "/": "divided by", "^": "to the power of"}


def explain_mistake(expression: str = None, equation: str = None, student_answer: str = None,
                    correct_answer: str = "") -> dict:
    """Why a wrong answer is wrong. note is written on the board; detail is spoken."""
    try:
        found = _explain_mistake(expression, equation, student_answer, correct_answer)
    except Exception:
        found = None
    if found:
        return found
    fix = " ".join(str(correct_answer or "different").split())
    wrote = " ".join(str(student_answer or expression or "that").split())
    return {
        "note": f"not quite. Answer: {fix}"[:90],
        "detail": f"You wrote {wrote}. That does not check out. The right result is {fix}.",
    }


def _explain_mistake(expression, equation, student_answer, correct_answer) -> dict | None:
    if equation:
        return _explain_algebra(equation, student_answer or "", correct_answer)
    expr = _clean(expression or "")
    if "=" not in expr:
        return None
    lhs_text, rhs_text = expr.split("=", 1)
    if not rhs_text.strip() or _parse(lhs_text).free_symbols or _parse(rhs_text).free_symbols:
        if _parse(lhs_text).free_symbols or _parse(rhs_text).free_symbols:
            return _explain_algebra(expr, rhs_text, correct_answer)
        return None
    student = sp.simplify(_parse(rhs_text))
    correct = sp.simplify(_parse(lhs_text))
    if sp.simplify(student - correct) == 0:
        return None
    binary = _binary(lhs_text)
    if binary:
        slip = _binary_slip(*binary, student, correct)
        if slip:
            return slip
    order = _order_slip(lhs_text, student, correct)
    if order:
        return order
    return _gap_slip(lhs_text, student, correct, correct_answer)


def _binary(lhs_text):
    text = _clean(lhs_text).replace(" ", "")
    match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)([+\-*/^])([+-]?\d+(?:\.\d+)?)", text)
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


def _as_int(value):
    number = sp.simplify(value)
    if number.is_integer:
        return int(number)
    return None


def _pack(note, detail) -> dict:
    return {"note": " ".join(note.split())[:90], "detail": " ".join(detail.split())}


def _binary_slip(a, op, b, student, correct):
    left, right = _parse(a), _parse(b)
    for trial in "+-*/":
        if trial == op:
            continue
        try:
            got = sp.simplify(_parse(f"({a}){trial}({b})"))
        except Exception:
            continue
        if sp.simplify(got - student) == 0:
            return _pack(
                f"used { _OP_WORD[trial] }, not { _OP_WORD[op] }. {a}{op}{b}={format_number(correct)}",
                f"The operation is wrong. {a} {_OP_WORD[trial]} {b} is {format_number(student)}, "
                f"which is what you wrote, but the problem asks for {a} {_OP_WORD[op]} {b}. "
                f"That is {format_number(correct)}.",
            )
    if op == "-" and sp.simplify(_parse(f"({b})-({a})") - student) == 0:
        return _pack(
            f"subtraction is backwards. {a}-{b}={format_number(correct)}",
            f"The subtraction is backwards. {b} minus {a} is {format_number(student)}, "
            f"but the problem is {a} minus {b}, which is {format_number(correct)}.",
        )
    if sp.simplify(student + correct) == 0:
        return _pack(
            f"sign is flipped. Answer: {format_number(correct)}",
            f"The amount is right, but the sign is flipped. "
            f"You wrote {format_number(student)}. The answer is {format_number(correct)}.",
        )
    ai, bi = _as_int(left), _as_int(right)
    si, ci = _as_int(student), _as_int(correct)
    if op == "+" and ai is not None and bi is not None and si is not None and ci is not None:
        ones = (ai % 10) + (bi % 10)
        if ones >= 10 and si == ones and si != ci:
            return _pack(
                f"added only the ones. {a}+{b}={ci}",
                f"You added the ones digits and stopped. {ai % 10} plus {bi % 10} is {ones}, "
                f"but the rest of the number still has to be added. {a} plus {b} is {ci}.",
            )
        if ones >= 10 and si == ci - 10:
            return _pack(
                f"forgot to carry. {a}+{b}={ci}",
                f"You forgot to carry. {ai % 10} plus {bi % 10} is {ones}, "
                f"so write {ones % 10} and carry 1. {a} plus {b} is {ci}, not {si}.",
            )
    return None


def _order_slip(lhs_text, student, correct):
    text = _clean(lhs_text).replace(" ", "")
    match = re.fullmatch(r"(\d+)([+\-])(\d+)([*/])(\d+)", text)
    if not match:
        return None
    a, op1, b, op2, c = match.groups()
    grouped = sp.simplify(_parse(f"(({a}){op1}({b})){op2}({c})"))
    if sp.simplify(grouped - student) != 0 or sp.simplify(grouped - correct) == 0:
        return None
    return _pack(
        f"do { _OP_WORD[op2] } first. Answer: {format_number(correct)}",
        f"You did {a} {_OP_WORD[op1]} {b} first, then {_OP_WORD[op2]} {c}, which gives {format_number(student)}. "
        f"Multiply or divide before you add or subtract. "
        f"{b} {_OP_WORD[op2]} {c} comes first, so the answer is {format_number(correct)}.",
    )


def _gap_slip(lhs_text, student, correct, correct_answer):
    diff = sp.simplify(student - correct)
    gap = _as_int(diff)
    answer = format_number(correct) if correct_answer in ("", None) else str(correct_answer)
    problem = _clean(lhs_text)
    if gap == 1:
        why = "You are 1 too high."
    elif gap == -1:
        why = "You are 1 too low."
    elif gap in (10, -10):
        why = "The tens place is off by 1. Check the carry."
    else:
        why = f"You are off by {format_number(abs(diff))}."
    steps = ""
    try:
        worked = [ln.strip() for ln in show_work(problem).splitlines() if ln.strip()]
        if len(worked) > 1:
            steps = " " + ". ".join(worked[:4]) + "."
    except Exception:
        steps = ""
    return _pack(
        f"{why} {problem}={answer}",
        f"{problem.replace('*', ' times ').replace('/', ' divided by ').replace('+', ' plus ').replace('-', ' minus ')} "
        f"is {answer}, not {format_number(student)}. {why}{steps}",
    )


def _explain_algebra(equation, student_answer, correct_answer):
    equation = _clean(equation)
    student = " ".join(str(student_answer or "").split()) or "nothing"
    answer = " ".join(str(correct_answer or "").split())
    worked = ""
    try:
        lines = [ln.strip() for ln in show_work(equation).splitlines() if ln.strip()]
        spoken = []
        for line in lines[:4]:
            spoken.append(line.replace("*", " times ").replace("^", " to the power "))
        if spoken:
            worked = " " + ". ".join(spoken) + "."
    except Exception:
        worked = ""
    copied = re.findall(r"-?\d+(?:\.\d+)?", student)
    in_problem = set(re.findall(r"\d+(?:\.\d+)?", equation.split("=")[0]))
    bare_number = not re.search(r"[A-Za-z]", student)
    if bare_number and copied and copied[-1].lstrip("-") in in_problem and answer and copied[-1] not in answer:
        detail = (f"You wrote {student}, but {copied[-1]} is already in the equation. "
                  f"It is not the solution.{worked} The answer is {answer}.")
        note = f"{copied[-1]} is in the problem, not the answer. {answer}"
    else:
        detail = f"You wrote {student}.{worked} The answer is {answer}."
        note = f"not {student}. {answer}"
    return _pack(note, detail)