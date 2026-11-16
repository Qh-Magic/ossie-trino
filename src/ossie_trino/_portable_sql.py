from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from ._expressions import Between, Binary, Call, Case, Column, Expr, FilteredAggregate, InList, IsNull, Like, Literal as ExprLiteral, MeasureRef, Star, Unary
from ._model import BoundModel, Metric
from ._profile import SUPPORTED_AGGREGATES, SUPPORTED_SCALAR_FUNCTIONS
from .api import CompilationError, CompiledQuery, OutputColumn
from .planning import ComponentRef, MultiStageQueryPlan, MultiStageStage, PlannedExpressionFilter, PlannedFilter, QueryFilter, QueryPlan

def render_query(model: BoundModel, plan: QueryPlan | MultiStageQueryPlan) -> CompiledQuery:
    if isinstance(plan, MultiStageQueryPlan):
        return _render_multistage_query(model, plan)
    root = plan.joins.root
    aliases = {root: "t0"}
    for edge in plan.joins.edges:
        aliases.setdefault(edge.one, f"t{len(aliases)}")
    projections, columns = [], []
    for i, field in enumerate(plan.dimensions):
        alias = f"d{i}"
        projections.append(f"{_render_expr(field.expression, aliases)} AS `{alias}`")
        columns.append(OutputColumn(f"{field.dataset}.{field.name}", alias))
    for i, metric in enumerate(plan.metrics):
        alias = f"m{i}"
        projections.append(f"{_render_expr(metric.expression, aliases)} AS `{alias}`")
        columns.append(OutputColumn(metric.name, alias))
    lines = ["SELECT " + ", ".join(projections), f"FROM {_render_table(model, root)} AS {aliases[root]}"]
    for edge in plan.joins.edges:
        conditions = " AND ".join(f"{aliases[edge.many]}.`{left}` = {aliases[edge.one]}.`{right}`" for left, right in edge.keys)
        lines.append(f"LEFT JOIN {_render_table(model, edge.one)} AS {aliases[edge.one]} ON {conditions}")
    if plan.where:
        lines.append("WHERE " + " AND ".join(_render_filter(item, aliases) for item in plan.where))
    if plan.dimensions:
        lines.append("GROUP BY " + ", ".join(_render_expr(field.expression, aliases) for field in plan.dimensions))
    if plan.having:
        lines.append("HAVING " + " AND ".join(_render_filter(item, aliases) for item in plan.having))
    if plan.limit is not None:
        lines.append(f"LIMIT {plan.limit}")
    dependencies = tuple(dict.fromkeys([root, *(edge.one for edge in plan.joins.edges)]))
    return CompiledQuery("\n".join(lines), tuple(columns), dependencies)

def _render_multistage_query(model: BoundModel, plan: MultiStageQueryPlan) -> CompiledQuery:
    stage_names = {
        stage.key: f"s{index}"
        for index, stage in enumerate(plan.stages)
    }
    stage_by_key = {stage.key: stage for stage in plan.stages}
    ctes = []
    for stage in plan.stages:
        stage_name = stage_names[stage.key]
        aliases = {stage.joins.root: "t0"}
        for edge in stage.joins.edges:
            aliases.setdefault(edge.one, f"t{len(aliases)}")
        projections = [
            *(
                f"{_render_expr(field.expression, aliases)} AS `k{index}`"
                for index, field in enumerate(plan.dimensions)
            ),
            f"{_render_expr(stage.expression, aliases)} AS `value`",
            "1 AS `present`",
        ]
        lines = [
            f"{stage_name} AS (SELECT {', '.join(projections)}",
            f"FROM {_render_table(model, stage.joins.root)} AS t0",
        ]
        for edge in stage.joins.edges:
            conditions = " AND ".join(
                f"{aliases[edge.many]}.`{left}` = {aliases[edge.one]}.`{right}`"
                for left, right in edge.keys
            )
            lines.append(f"LEFT JOIN {_render_table(model, edge.one)} AS {aliases[edge.one]} ON {conditions}")
        if plan.where:
            lines.append("WHERE " + " AND ".join(_render_filter(item, aliases) for item in plan.where))
        if plan.dimensions:
            lines.append("GROUP BY " + ", ".join(_render_expr(field.expression, aliases) for field in plan.dimensions))
        lines[-1] += ")"
        ctes.append("\n".join(lines))
    projections = []
    columns = []
    ordered_stage_names = [stage_names[stage.key] for stage in plan.stages]
    for index, field in enumerate(plan.dimensions):
        alias = f"d{index}"
        keys = ", ".join(f"{stage_name}.`k{index}`" for stage_name in ordered_stage_names)
        projections.append(f"COALESCE({keys}) AS `{alias}`")
        columns.append(OutputColumn(f"{field.dataset}.{field.name}", alias))
    for index, (metric, metric_expression, direct_stage_key) in enumerate(
        zip(plan.metrics, plan.metric_expressions, plan.metric_stage_keys)
    ):
        alias = f"m{index}"
        if direct_stage_key is not None:
            expression = _render_stage_value(direct_stage_key, stage_names, stage_by_key)
        else:
            expression = _render_multistage_expr(metric_expression, stage_names, stage_by_key)
        projections.append(f"{expression} AS `{alias}`")
        columns.append(OutputColumn(metric.name, alias))
    lines = ["WITH " + ",\n".join(ctes), "SELECT " + ", ".join(projections)]
    if not plan.dimensions:
        lines.append("FROM " + " CROSS JOIN ".join(ordered_stage_names))
    else:
        from_clause = f"FROM {ordered_stage_names[0]}"
        joined = [ordered_stage_names[0]]
        for stage_name in ordered_stage_names[1:]:
            conditions = []
            for index in range(len(plan.dimensions)):
                prior_keys = [f"{name}.`k{index}`" for name in joined]
                prior = prior_keys[0] if len(prior_keys) == 1 else f"COALESCE({', '.join(prior_keys)})"
                current = f"{stage_name}.`k{index}`"
                conditions.append(f"({prior} = {current} OR ({prior} IS NULL AND {current} IS NULL))")
            from_clause += f"\nFULL OUTER JOIN {stage_name} ON " + " AND ".join(conditions)
            joined.append(stage_name)
        lines.append(from_clause)
    if plan.having:
        lines.append(
            "WHERE "
            + " AND ".join(
                _render_multistage_filter(
                    item,
                    stage_names,
                    stage_by_key,
                    dict(plan.direct_stage_keys),
                    dict(plan.lowered_expressions),
                )
                for item in plan.having
            )
        )
    if plan.limit is not None:
        lines.append(f"LIMIT {plan.limit}")
    dependencies = tuple(
        dict.fromkeys(
            dependency
            for stage in plan.stages
            for dependency in (stage.joins.root, *(edge.one for edge in stage.joins.edges))
        )
    )
    return CompiledQuery("\n".join(lines), tuple(columns), dependencies)

def _render_multistage_filter(
    item: PlannedFilter,
    stages: dict[str, str],
    stage_by_key: dict[str, MultiStageStage],
    direct_stage_keys: dict[str, str],
    lowered_expressions: dict[str, Expr] | None = None,
) -> str:
    assert isinstance(item.expression_owner, Metric)
    direct_key = direct_stage_keys.get(item.expression_owner.qualified_name)
    if direct_key is not None:
        left = _render_stage_value(direct_key, stages, stage_by_key)
    else:
        expression = (lowered_expressions or {}).get(
            item.expression_owner.qualified_name,
            item.expression_owner.expression,
        )
        left = _render_multistage_expr(expression, stages, stage_by_key)
    if item.op == "is_null":
        return f"{left} IS NULL"
    if item.op == "is_not_null":
        return f"{left} IS NOT NULL"
    operator = {"eq": "=", "ne": "<>", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}[item.op]
    return f"{left} {operator} {_literal(item.value)}"

def _render_multistage_expr(expression: Expr, stages: dict[str, str], stage_by_key: dict[str, MultiStageStage]) -> str:
    if isinstance(expression, ComponentRef):
        return _render_stage_value(expression.key, stages, stage_by_key)
    if isinstance(expression, MeasureRef):
        return _render_stage_value(
            f"measure:{expression.dataset}.{expression.metric}",
            stages,
            stage_by_key,
        )
    if isinstance(expression, ExprLiteral):
        return _literal(expression.value)
    if isinstance(expression, Binary):
        return _render_binary(
            expression.op,
            _render_multistage_expr(expression.left, stages, stage_by_key),
            _render_multistage_expr(expression.right, stages, stage_by_key),
        )
    if isinstance(expression, Unary):
        separator = " " if expression.op == "NOT" else ""
        return f"({expression.op}{separator}{_render_multistage_expr(expression.operand, stages, stage_by_key)})"
    if isinstance(expression, IsNull):
        suffix = "IS NOT NULL" if expression.negated else "IS NULL"
        return f"({_render_multistage_expr(expression.operand, stages, stage_by_key)} {suffix})"
    if isinstance(expression, Case):
        branches = " ".join(
            f"WHEN {_render_multistage_expr(condition, stages, stage_by_key)} THEN {_render_multistage_expr(value, stages, stage_by_key)}"
            for condition, value in expression.branches
        )
        otherwise = f" ELSE {_render_multistage_expr(expression.otherwise, stages, stage_by_key)}" if expression.otherwise is not None else ""
        return f"CASE {branches}{otherwise} END"
    if isinstance(expression, Between):
        operator = "NOT BETWEEN" if expression.negated else "BETWEEN"
        return f"({_render_multistage_expr(expression.operand, stages, stage_by_key)} {operator} {_render_multistage_expr(expression.lower, stages, stage_by_key)} AND {_render_multistage_expr(expression.upper, stages, stage_by_key)})"
    if isinstance(expression, InList):
        operator = "NOT IN" if expression.negated else "IN"
        values = ", ".join(_render_multistage_expr(value, stages, stage_by_key) for value in expression.values)
        return f"({_render_multistage_expr(expression.operand, stages, stage_by_key)} {operator} ({values}))"
    if isinstance(expression, Like):
        operator = "NOT LIKE" if expression.negated else "LIKE"
        return f"({_render_multistage_expr(expression.operand, stages, stage_by_key)} {operator} {_render_multistage_expr(expression.pattern, stages, stage_by_key)})"
    if isinstance(expression, Call) and expression.name in SUPPORTED_SCALAR_FUNCTIONS:
        return _render_scalar_call(
            expression.name,
            tuple(_render_multistage_expr(argument, stages, stage_by_key) for argument in expression.args),
        )
    raise CompilationError("MULTISTAGE_UNSUPPORTED_EXPRESSION", "$.metrics", "unsupported node in cross-grain expression")

def _render_stage_value(key: str, stages: dict[str, str], stage_by_key: dict[str, MultiStageStage]) -> str:
    stage_name = stages[key]
    empty_value = _render_empty_input(stage_by_key[key].expression)
    return f"CASE WHEN {stage_name}.`present` IS NULL THEN {empty_value} ELSE {stage_name}.`value` END"

def _render_empty_input(expression: Expr) -> str:
    if isinstance(expression, ExprLiteral):
        return _literal(expression.value)
    if isinstance(expression, Call):
        if expression.name in SUPPORTED_AGGREGATES:
            return "0" if expression.name == "COUNT" else "NULL"
        if expression.name in SUPPORTED_SCALAR_FUNCTIONS:
            return _render_scalar_call(
                expression.name,
                tuple(_render_empty_input(argument) for argument in expression.args),
            )
    if isinstance(expression, FilteredAggregate):
        return "0" if expression.aggregate.name == "COUNT" else "NULL"
    if isinstance(expression, MeasureRef):
        return _render_empty_input(expression.expression)
    if isinstance(expression, Binary):
        return _render_binary(
            expression.op,
            _render_empty_input(expression.left),
            _render_empty_input(expression.right),
        )
    if isinstance(expression, Unary):
        separator = " " if expression.op == "NOT" else ""
        return f"({expression.op}{separator}{_render_empty_input(expression.operand)})"
    if isinstance(expression, IsNull):
        suffix = "IS NOT NULL" if expression.negated else "IS NULL"
        return f"({_render_empty_input(expression.operand)} {suffix})"
    if isinstance(expression, Case):
        branches = " ".join(
            f"WHEN {_render_empty_input(condition)} THEN {_render_empty_input(value)}"
            for condition, value in expression.branches
        )
        otherwise = f" ELSE {_render_empty_input(expression.otherwise)}" if expression.otherwise is not None else ""
        return f"CASE {branches}{otherwise} END"
    if isinstance(expression, Between):
        operator = "NOT BETWEEN" if expression.negated else "BETWEEN"
        return f"({_render_empty_input(expression.operand)} {operator} {_render_empty_input(expression.lower)} AND {_render_empty_input(expression.upper)})"
    if isinstance(expression, InList):
        operator = "NOT IN" if expression.negated else "IN"
        values = ", ".join(_render_empty_input(value) for value in expression.values)
        return f"({_render_empty_input(expression.operand)} {operator} ({values}))"
    if isinstance(expression, Like):
        operator = "NOT LIKE" if expression.negated else "LIKE"
        return f"({_render_empty_input(expression.operand)} {operator} {_render_empty_input(expression.pattern)})"
    raise CompilationError("MULTISTAGE_UNSUPPORTED_EXPRESSION", "$.metrics", "cannot derive empty-input value")

def _render_table(model: BoundModel, dataset_name: str) -> str:
    return ".".join(f"`{part}`" for part in model.dataset_by_name[dataset_name].source.parts)

def _render_expr(expression: Expr, aliases: dict[str, str]) -> str:
    if isinstance(expression, Column):
        if expression.dataset is None or expression.dataset not in aliases:
            raise CompilationError("INTERNAL_ERROR", "$.expression", "unplanned dataset in expression")
        return f"{aliases[expression.dataset]}.`{expression.name}`"
    if isinstance(expression, ExprLiteral):
        return _literal(expression.value)
    if isinstance(expression, Star):
        return "*"
    if isinstance(expression, Binary):
        return _render_binary(
            expression.op,
            _render_expr(expression.left, aliases),
            _render_expr(expression.right, aliases),
        )
    if isinstance(expression, Unary):
        separator = " " if expression.op == "NOT" else ""
        return f"({expression.op}{separator}{_render_expr(expression.operand, aliases)})"
    if isinstance(expression, IsNull):
        suffix = "IS NOT NULL" if expression.negated else "IS NULL"
        return f"({_render_expr(expression.operand, aliases)} {suffix})"
    if isinstance(expression, Case):
        branches = " ".join(
            f"WHEN {_render_expr(condition, aliases)} THEN {_render_expr(value, aliases)}"
            for condition, value in expression.branches
        )
        otherwise = f" ELSE {_render_expr(expression.otherwise, aliases)}" if expression.otherwise is not None else ""
        return f"CASE {branches}{otherwise} END"
    if isinstance(expression, Between):
        operator = "NOT BETWEEN" if expression.negated else "BETWEEN"
        return f"({_render_expr(expression.operand, aliases)} {operator} {_render_expr(expression.lower, aliases)} AND {_render_expr(expression.upper, aliases)})"
    if isinstance(expression, InList):
        operator = "NOT IN" if expression.negated else "IN"
        values = ", ".join(_render_expr(value, aliases) for value in expression.values)
        return f"({_render_expr(expression.operand, aliases)} {operator} ({values}))"
    if isinstance(expression, Like):
        operator = "NOT LIKE" if expression.negated else "LIKE"
        return f"({_render_expr(expression.operand, aliases)} {operator} {_render_expr(expression.pattern, aliases)})"
    if isinstance(expression, Call):
        if expression.name not in SUPPORTED_AGGREGATES and expression.name not in SUPPORTED_SCALAR_FUNCTIONS:
            raise CompilationError("UNSUPPORTED_EXPRESSION", "$.expression", f"unsupported function {expression.name}")
        distinct = "DISTINCT " if expression.distinct else ""
        arguments = tuple(_render_expr(arg, aliases) for arg in expression.args)
        if expression.name in SUPPORTED_SCALAR_FUNCTIONS:
            return _render_scalar_call(expression.name, arguments)
        return f"{_render_aggregate_name(expression.name)}({distinct}{', '.join(arguments)})"
    if isinstance(expression, FilteredAggregate):
        aggregate = expression.aggregate
        predicate = _render_expr(expression.predicate, aliases)
        argument = aggregate.args[0]
        if isinstance(argument, Star):
            conditional = f"CASE WHEN {predicate} THEN 1 END"
        else:
            conditional = f"CASE WHEN {predicate} THEN {_render_expr(argument, aliases)} END"
        distinct = "DISTINCT " if aggregate.distinct else ""
        return f"{_render_aggregate_name(aggregate.name)}({distinct}{conditional})"
    if isinstance(expression, MeasureRef):
        return _render_expr(expression.expression, aliases)
    raise AssertionError(expression)

def _render_scalar_call(name: str, arguments: tuple[str, ...]) -> str:
    if name in {"TRUNC", "TRUNCATE"}:
        value = arguments[0]
        digits = arguments[1] if len(arguments) == 2 else "0"
        factor = f"POWER(10, {digits})"
        return f"(SIGN({value}) * FLOOR(ABS({value}) * {factor}) / {factor})"
    if name == "CHARINDEX":
        return f"LOCATE({arguments[0]}, {arguments[1]})"
    if name == "CONTAINS":
        return f"(LOCATE({arguments[1]}, {arguments[0]}) > 0)"
    if name == "STARTSWITH":
        return f"(LOCATE({arguments[1]}, {arguments[0]}) = 1)"
    if name == "ENDSWITH":
        return f"(RIGHT({arguments[0]}, LENGTH({arguments[1]})) = {arguments[1]})"
    if name == "IFF":
        name = "IF"
    elif name == "NVL":
        name = "IFNULL"
    elif name == "NVL2":
        return f"IF({arguments[0]} IS NOT NULL, {arguments[1]}, {arguments[2]})"
    elif name == "ZEROIFNULL":
        return f"IFNULL({arguments[0]}, 0)"
    elif name == "NULLIFZERO":
        return f"NULLIF({arguments[0]}, 0)"
    return f"{name}({', '.join(arguments)})"

def _render_binary(operator: str, left: str, right: str) -> str:
    if operator == "/":
        return f"DIVIDE({left}, {right})"
    return f"({left} {operator} {right})"

def _render_aggregate_name(name: str) -> str:
    return {
        "STDDEV": "STDDEV_SAMP",
        "VARIANCE": "VAR_SAMP",
    }.get(name, name)

def _render_filter(item: QueryFilter, aliases: dict[str, str]) -> str:
    if isinstance(item, PlannedExpressionFilter):
        return _render_expr(item.expression, aliases)
    left = _render_expr(item.expression_owner.expression, aliases)
    if item.op == "is_null":
        return f"{left} IS NULL"
    if item.op == "is_not_null":
        return f"{left} IS NOT NULL"
    operator = {"eq": "=", "ne": "<>", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}[item.op]
    return f"{left} {operator} {_literal(item.value)}"

def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if value is True:
        return "TRUE"
    if value is False:
        return "FALSE"
    if type(value) is int:
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CompilationError("UNSAFE_LITERAL", "$.filter", "decimal must be finite")
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            raise CompilationError("UNSAFE_LITERAL", "$.filter", "timezone-aware datetime is not supported")
        return f"CAST('{value.isoformat(sep=' ')}' AS DATETIME)"
    if isinstance(value, date):
        return f"CAST('{value.isoformat()}' AS DATE)"
    if isinstance(value, str):
        if "\x00" in value or "\\" in value:
            raise CompilationError("UNSAFE_LITERAL", "$.filter", "NUL and backslash string values are not supported")
        return "'" + value.replace("'", "''") + "'"
    raise CompilationError("UNSAFE_LITERAL", "$.filter", f"unsupported literal type {type(value).__name__}")
