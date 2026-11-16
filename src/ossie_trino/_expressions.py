from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Iterable, Iterator


class ExpressionError(ValueError):
    pass


@dataclass(frozen=True)
class Column:
    dataset: str | None
    name: str


@dataclass(frozen=True)
class Literal:
    value: str | int | Decimal | bool | None


@dataclass(frozen=True)
class Star:
    pass


@dataclass(frozen=True)
class Binary:
    op: str
    left: "Expr"
    right: "Expr"


@dataclass(frozen=True)
class Unary:
    op: str
    operand: "Expr"


@dataclass(frozen=True)
class IsNull:
    operand: "Expr"
    negated: bool


@dataclass(frozen=True)
class Case:
    branches: tuple[tuple["Expr", "Expr"], ...]
    otherwise: "Expr | None"


@dataclass(frozen=True)
class Between:
    operand: "Expr"
    lower: "Expr"
    upper: "Expr"
    negated: bool


@dataclass(frozen=True)
class InList:
    operand: "Expr"
    values: tuple["Expr", ...]
    negated: bool


@dataclass(frozen=True)
class Like:
    operand: "Expr"
    pattern: "Expr"
    negated: bool


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple["Expr", ...]
    distinct: bool = False


@dataclass(frozen=True)
class FilteredAggregate:
    aggregate: Call
    predicate: "Expr"


@dataclass(frozen=True)
class MeasureRef:
    dataset: str
    metric: str
    expression: "Expr"


Expr = Column | Literal | Star | Binary | Unary | IsNull | Case | Between | InList | Like | Call | FilteredAggregate | MeasureRef


_TOKEN = re.compile(
    r"\s*(?:"
    r"(?P<number>\d+(?:\.\d+)?)|"
    r"(?P<string>'(?:''|[^'])*')|"
    r"(?P<identifier>[A-Za-z_][A-Za-z0-9_]*)|"
    r"(?P<operator><=|>=|<>|!=|[+\-*/%=<>])|"
    r"(?P<dot>\.)|(?P<comma>,)|(?P<lparen>\()|(?P<rparen>\))"
    r")"
)


def parse(text: str) -> Expr:
    tokens = tuple(_tokens(text))
    parser = _Parser(tokens)
    expression = parser.expression()
    if not parser.at_end:
        raise ExpressionError(f"unexpected token {parser.peek()[1]!r}")
    return expression


def _tokens(text: str) -> Iterator[tuple[str, str]]:
    position = 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if not match:
            raise ExpressionError(f"unsupported expression near {text[position:position + 24]!r}")
        kind = match.lastgroup
        assert kind is not None
        yield kind, match.group(kind)
        position = match.end()


class _Parser:
    def __init__(self, tokens: tuple[tuple[str, str], ...]):
        self.tokens = tokens
        self.position = 0

    @property
    def at_end(self) -> bool:
        return self.position == len(self.tokens)

    def peek(self) -> tuple[str, str]:
        if self.at_end:
            return "end", ""
        return self.tokens[self.position]

    def take(self, kind: str | None = None) -> tuple[str, str]:
        token = self.peek()
        if kind is not None and token[0] != kind:
            raise ExpressionError(f"expected {kind}, found {token[1]!r}")
        self.position += 1
        return token

    def expression(self, minimum_precedence: int = 0) -> Expr:
        left = self.unary()
        precedence = {"OR": 1, "AND": 2, "=": 3, "!=": 3, "<>": 3, "<": 3, "<=": 3, ">": 3, ">=": 3, "+": 4, "-": 4, "*": 5, "/": 5, "%": 5}
        while True:
            if self._keyword("FILTER"):
                if not isinstance(left, Call):
                    raise ExpressionError("FILTER may only modify an aggregate call")
                self.take()
                self.take("lparen")
                if not self._keyword("WHERE"):
                    raise ExpressionError("FILTER requires WHERE")
                self.take()
                predicate = self.expression()
                self.take("rparen")
                left = FilteredAggregate(left, predicate)
                continue
            if self._keyword("IS"):
                if 3 < minimum_precedence:
                    break
                self.take()
                negated = False
                if self._keyword("NOT"):
                    self.take()
                    negated = True
                if not self._keyword("NULL"):
                    raise ExpressionError("IS only supports NULL in the portable profile")
                self.take()
                left = IsNull(left, negated)
                continue
            negated_predicate = self._keyword("NOT") and self._next_keyword_in({"IN", "BETWEEN", "LIKE"})
            predicate = self.tokens[self.position + 1][1].upper() if negated_predicate else self.peek()[1].upper()
            if predicate in {"IN", "BETWEEN", "LIKE"} and (self.peek()[0] == "identifier"):
                if 3 < minimum_precedence:
                    break
                if negated_predicate:
                    self.take()
                self.take()
                if predicate == "IN":
                    self.take("lparen")
                    values: list[Expr] = []
                    if self.peek()[0] == "rparen":
                        raise ExpressionError("IN requires at least one value")
                    while True:
                        values.append(self.expression())
                        if self.peek()[0] != "comma":
                            break
                        self.take()
                    self.take("rparen")
                    left = InList(left, tuple(values), negated_predicate)
                elif predicate == "BETWEEN":
                    lower = self.expression(4)
                    if not self._keyword("AND"):
                        raise ExpressionError("BETWEEN requires AND")
                    self.take()
                    upper = self.expression(4)
                    left = Between(left, lower, upper, negated_predicate)
                else:
                    left = Like(left, self.expression(4), negated_predicate)
                continue
            kind, value = self.peek()
            op = value.upper() if kind == "identifier" and value.upper() in {"AND", "OR"} else value if kind == "operator" else None
            if op is None:
                break
            level = precedence[op]
            if level < minimum_precedence:
                break
            self.take()
            right = self.expression(level + 1)
            left = Binary(op, left, right)
        return left

    def unary(self) -> Expr:
        kind, value = self.peek()
        if kind == "operator" and value in {"+", "-"}:
            self.take()
            return Unary(value, self.unary())
        if self._keyword("NOT"):
            self.take()
            return Unary("NOT", self.expression(3))
        return self.primary()

    def _keyword(self, value: str) -> bool:
        kind, token = self.peek()
        return kind == "identifier" and token.upper() == value

    def _next_keyword_in(self, values: set[str]) -> bool:
        if self.position + 1 >= len(self.tokens):
            return False
        kind, token = self.tokens[self.position + 1]
        return kind == "identifier" and token.upper() in values

    def primary(self) -> Expr:
        kind, value = self.peek()
        if kind == "number":
            self.take()
            if "." in value:
                try:
                    return Literal(Decimal(value))
                except InvalidOperation as error:
                    raise ExpressionError("invalid decimal") from error
            return Literal(int(value))
        if kind == "string":
            self.take()
            return Literal(value[1:-1].replace("''", "'"))
        if kind == "operator" and value == "*":
            self.take()
            return Star()
        if kind == "lparen":
            self.take()
            value_expr = self.expression()
            self.take("rparen")
            return value_expr
        if kind != "identifier":
            raise ExpressionError(f"unexpected token {value!r}")
        first = self.take()[1]
        keyword = first.upper()
        if keyword == "CASE":
            branches: list[tuple[Expr, Expr]] = []
            while self._keyword("WHEN"):
                self.take()
                condition = self.expression()
                if not self._keyword("THEN"):
                    raise ExpressionError("CASE WHEN requires THEN")
                self.take()
                branches.append((condition, self.expression()))
            if not branches:
                raise ExpressionError("CASE requires at least one WHEN branch")
            otherwise = None
            if self._keyword("ELSE"):
                self.take()
                otherwise = self.expression()
            if not self._keyword("END"):
                raise ExpressionError("CASE requires END")
            self.take()
            return Case(tuple(branches), otherwise)
        if keyword in {"TRUE", "FALSE", "NULL"}:
            return Literal({"TRUE": True, "FALSE": False, "NULL": None}[keyword])
        if self.peek()[0] == "lparen":
            self.take()
            args: list[Expr] = []
            distinct = False
            if self._keyword("DISTINCT"):
                self.take()
                distinct = True
            if self.peek()[0] != "rparen":
                while True:
                    args.append(self.expression())
                    if self.peek()[0] != "comma":
                        break
                    self.take()
            self.take("rparen")
            if distinct and not args:
                raise ExpressionError("DISTINCT requires an argument")
            return Call(keyword, tuple(args), distinct)
        if self.peek()[0] == "dot":
            self.take()
            second = self.take("identifier")[1]
            return Column(first, second)
        return Column(None, first)


def contains_call(expression: Expr, names: frozenset[str]) -> bool:
    return any(isinstance(node, Call) and node.name in names for node in walk(expression))


def walk(expression: Expr) -> Iterable[Expr]:
    yield expression
    if isinstance(expression, Binary):
        yield from walk(expression.left)
        yield from walk(expression.right)
    elif isinstance(expression, Unary):
        yield from walk(expression.operand)
    elif isinstance(expression, IsNull):
        yield from walk(expression.operand)
    elif isinstance(expression, Case):
        for condition, value in expression.branches:
            yield from walk(condition)
            yield from walk(value)
        if expression.otherwise is not None:
            yield from walk(expression.otherwise)
    elif isinstance(expression, Between):
        yield from walk(expression.operand)
        yield from walk(expression.lower)
        yield from walk(expression.upper)
    elif isinstance(expression, InList):
        yield from walk(expression.operand)
        for value in expression.values:
            yield from walk(value)
    elif isinstance(expression, Like):
        yield from walk(expression.operand)
        yield from walk(expression.pattern)
    elif isinstance(expression, Call):
        for argument in expression.args:
            yield from walk(argument)
    elif isinstance(expression, FilteredAggregate):
        yield from walk(expression.aggregate)
        yield from walk(expression.predicate)
    elif isinstance(expression, MeasureRef):
        yield from walk(expression.expression)
