from __future__ import annotations

from dataclasses import dataclass
from ._expressions import Between, Binary, Call, Case, Column, Expr, ExpressionError, FilteredAggregate, InList, IsNull, Like, Literal, MeasureRef, Star, Unary, parse
from ._model import BoundModel, Field, Metric, Relationship
from ._profile import SUPPORTED_AGGREGATES, SUPPORTED_SCALAR_FUNCTIONS
from .api import CompilationError, Query, Scalar

@dataclass(frozen=True)
class PlannedFilter:
    expression_owner: Field | Metric
    op: str
    value: Scalar

@dataclass(frozen=True)
class PlannedExpressionFilter:
    expression: Expr
    dataset: str | None

QueryFilter = PlannedFilter | PlannedExpressionFilter

@dataclass(frozen=True)
class ComponentRef:
    key: str

@dataclass(frozen=True)
class SafeJoinTree:
    root: str
    edges: tuple[Relationship, ...]

@dataclass(frozen=True)
class QueryPlan:
    joins: SafeJoinTree
    metrics: tuple[Metric, ...]
    dimensions: tuple[Field, ...]
    where: tuple[QueryFilter, ...]
    having: tuple[PlannedFilter, ...]
    limit: int | None

@dataclass(frozen=True)
class MultiStageStage:
    key: str
    root: str
    expression: Expr
    joins: SafeJoinTree

@dataclass(frozen=True)
class MultiStageQueryPlan:
    metrics: tuple[Metric, ...]
    metric_expressions: tuple[Expr, ...]
    lowered_expressions: tuple[tuple[str, Expr], ...]
    metric_stage_keys: tuple[str | None, ...]
    direct_stage_keys: tuple[tuple[str, str], ...]
    dimensions: tuple[Field, ...]
    stages: tuple[MultiStageStage, ...]
    where: tuple[QueryFilter, ...]
    having: tuple[PlannedFilter, ...]
    limit: int | None

def plan_query(model: BoundModel, query: Query) -> QueryPlan | MultiStageQueryPlan:
    if not isinstance(query, Query):
        raise CompilationError("INVALID_REQUEST", "$", "query must be a Query")
    if not query.metrics and not query.dimensions:
        raise CompilationError("EMPTY_QUERY", "$", "at least one metric or dimension is required")
    dimensions = tuple(_field(model, name, f"$.dimensions[{i}]") for i, name in enumerate(query.dimensions))
    where_pairs = tuple((_field(model, item.field, f"$.where[{i}].field"), item) for i, item in enumerate(query.where))
    _validate_filters(query)
    expression_filter = _bind_query_filter(model, query.filter) if query.filter is not None else None
    where = (
        *(PlannedFilter(field, item.op, item.value) for field, item in where_pairs),
        *((expression_filter,) if expression_filter is not None else ()),
    )
    if query.limit is not None and (type(query.limit) is not int or not 0 <= query.limit <= 2_147_483_647):
        raise CompilationError("INVALID_LIMIT", "$.limit", "limit must be a non-negative integer")
    if not query.metrics:
        if query.having:
            raise CompilationError("INVALID_REQUEST", "$.having", "dimension-only queries cannot have metric filters")
        return _plan_dimension_only(model, dimensions, where, query.limit)
    metrics = tuple(_metric(model, name, f"$.metrics[{i}]") for i, name in enumerate(query.metrics))
    multistage = any(len(metric.roots) > 1 for metric in metrics)
    roots = {metric.root for metric in metrics}
    having_pairs = tuple((_metric(model, item.metric, f"$.having[{i}].metric"), item) for i, item in enumerate(query.having))
    multistage = multistage or any(len(metric.roots) > 1 for metric, _ in having_pairs)
    roots.update(metric.root for metric, _ in having_pairs)
    multistage = multistage or len(roots) > 1
    if multistage:
        stage_inputs: list[tuple[str, str, Expr]] = []
        seen_stages: set[str] = set()
        direct_stage_keys: dict[str, str] = {}
        lowered_expressions: dict[str, Expr] = {}
        all_metrics = tuple(dict.fromkeys((*metrics, *(metric for metric, _ in having_pairs))))
        for metric in all_metrics:
            if len(metric.roots) == 1:
                assert metric.root is not None
                key = _direct_stage_key(metric)
                direct_stage_keys[metric.qualified_name] = key
                if key not in seen_stages:
                    stage_inputs.append((key, metric.root, metric.expression))
                    seen_stages.add(key)
                continue
            lowered, components = _lower_multistage_components(metric)
            lowered_expressions[metric.qualified_name] = lowered
            for key, root, expression in components:
                if key not in seen_stages:
                    stage_inputs.append((key, root, expression))
                    seen_stages.add(key)
        stages: list[MultiStageStage] = []
        for key, root, expression in stage_inputs:
            edges: list[Relationship] = []
            seen_edges: set[str] = set()
            required_datasets = {
                *(field.dataset for field in dimensions),
                *(_filter_dataset(item) for item in where if _filter_dataset(item) is not None),
            }
            for dataset in sorted(required_datasets):
                for edge in _unique_path(model, root, dataset):
                    if edge.name not in seen_edges:
                        edges.append(edge)
                        seen_edges.add(edge.name)
            stages.append(MultiStageStage(key, root, expression, SafeJoinTree(root, tuple(edges))))
        return MultiStageQueryPlan(
            metrics,
            tuple(lowered_expressions.get(metric.qualified_name, metric.expression) for metric in metrics),
            tuple(lowered_expressions.items()),
            tuple(direct_stage_keys.get(metric.qualified_name) for metric in metrics),
            tuple(direct_stage_keys.items()),
            dimensions,
            tuple(stages),
            where,
            tuple(PlannedFilter(metric, item.op, item.value) for metric, item in having_pairs),
            query.limit,
        )
    root = next(iter(roots))
    assert root is not None
    required = {
        root,
        *(field.dataset for field in dimensions),
        *(_filter_dataset(item) for item in where if _filter_dataset(item) is not None),
    }
    edges: list[Relationship] = []
    seen: set[str] = set()
    for dataset in sorted(required):
        for edge in _unique_path(model, root, dataset):
            if edge.name not in seen:
                edges.append(edge)
                seen.add(edge.name)
    return QueryPlan(SafeJoinTree(root, tuple(edges)), metrics, dimensions,
                     where,
                     tuple(PlannedFilter(metric, item.op, item.value) for metric, item in having_pairs), query.limit)

def _plan_dimension_only(
    model: BoundModel,
    dimensions: tuple[Field, ...],
    where: tuple[QueryFilter, ...],
    limit: int | None,
) -> QueryPlan:
    dimension_datasets = tuple(dict.fromkeys(field.dataset for field in dimensions))
    candidates: list[tuple[str, tuple[Relationship, ...]]] = []
    ambiguous: list[CompilationError] = []
    for anchor in dimension_datasets:
        edges: list[Relationship] = []
        seen: set[str] = set()
        for target in dimension_datasets:
            try:
                path = _unique_path(model, anchor, target)
            except CompilationError as error:
                if error.code == "AMBIGUOUS_JOIN":
                    ambiguous.append(error)
                break
            for edge in path:
                if edge.name not in seen:
                    edges.append(edge)
                    seen.add(edge.name)
        else:
            candidates.append((anchor, tuple(edges)))
    if len(candidates) > 1:
        raise CompilationError(
            "AMBIGUOUS_ANCHOR",
            "$.dimensions",
            "multiple dimension datasets can reach every requested dimension",
            tuple(anchor for anchor, _ in candidates),
        )
    if not candidates:
        if ambiguous:
            raise ambiguous[0]
        raise CompilationError(
            "NO_ANCHOR",
            "$.dimensions",
            "no dimension dataset can safely reach every requested dimension",
            dimension_datasets,
        )
    anchor, initial_edges = candidates[0]
    edges = list(initial_edges)
    seen = {edge.name for edge in edges}
    for item in where:
        dataset = _filter_dataset(item)
        if dataset is None:
            continue
        for edge in _unique_path(model, anchor, dataset):
            if edge.name not in seen:
                edges.append(edge)
                seen.add(edge.name)
    return QueryPlan(
        SafeJoinTree(anchor, tuple(edges)),
        (),
        dimensions,
        where,
        (),
        limit,
    )

def _lower_multistage_components(metric: Metric) -> tuple[Expr, tuple[tuple[str, str, Expr], ...]]:
    components: list[tuple[str, str, Expr]] = []

    def component(expression: Expr, root: str, label: str) -> ComponentRef:
        key = f"component:{metric.qualified_name}:{label}:{len(components)}"
        components.append((key, root, expression))
        return ComponentRef(key)

    def lower(node: Expr) -> Expr:
        if isinstance(node, MeasureRef):
            key = _measure_stage_key(node)
            components.append((key, node.dataset, node.expression))
            return ComponentRef(key)
        if isinstance(node, FilteredAggregate):
            roots = _expression_roots(node)
            if len(roots) != 1:
                raise CompilationError(
                    "MULTISTAGE_UNSUPPORTED_EXPRESSION",
                    "$.metrics",
                    "each aggregate component must read exactly one dataset",
                )
            return component(node, next(iter(roots)), "filter")
        if isinstance(node, Call):
            if node.name in SUPPORTED_AGGREGATES:
                roots = _expression_roots(node)
                if len(roots) != 1:
                    raise CompilationError(
                        "MULTISTAGE_UNSUPPORTED_EXPRESSION",
                        "$.metrics",
                        "each aggregate component must read exactly one dataset",
                    )
                return component(node, next(iter(roots)), node.name.lower())
            return Call(node.name, tuple(lower(argument) for argument in node.args), node.distinct)
        if isinstance(node, Binary):
            return Binary(node.op, lower(node.left), lower(node.right))
        if isinstance(node, Unary):
            return Unary(node.op, lower(node.operand))
        if isinstance(node, IsNull):
            return IsNull(lower(node.operand), node.negated)
        if isinstance(node, Case):
            return Case(tuple((lower(condition), lower(value)) for condition, value in node.branches), lower(node.otherwise) if node.otherwise is not None else None)
        if isinstance(node, Between):
            return Between(lower(node.operand), lower(node.lower), lower(node.upper), node.negated)
        if isinstance(node, InList):
            return InList(lower(node.operand), tuple(lower(value) for value in node.values), node.negated)
        if isinstance(node, Like):
            return Like(lower(node.operand), lower(node.pattern), node.negated)
        if isinstance(node, (Literal,)):
            return node
        raise CompilationError(
            "MULTISTAGE_UNSUPPORTED_EXPRESSION",
            "$.metrics",
            "cross-grain scalar expressions must be composed from aggregate components",
        )

    lowered = lower(metric.expression)
    if not components:
        raise CompilationError("MULTISTAGE_UNSUPPORTED_EXPRESSION", "$.metrics", "cross-grain metric has no aggregate components")
    return lowered, tuple(components)

def _expression_roots(expression: Expr) -> set[str]:
    roots = {node.dataset for node in _walk_component(expression) if isinstance(node, Column) and node.dataset is not None}
    return roots

def _walk_component(expression: Expr):
    if isinstance(expression, MeasureRef):
        yield expression
        return
    yield expression
    if isinstance(expression, Binary):
        yield from _walk_component(expression.left)
        yield from _walk_component(expression.right)
    elif isinstance(expression, (Unary, IsNull)):
        yield from _walk_component(expression.operand)
    elif isinstance(expression, Case):
        for condition, value in expression.branches:
            yield from _walk_component(condition)
            yield from _walk_component(value)
        if expression.otherwise is not None:
            yield from _walk_component(expression.otherwise)
    elif isinstance(expression, Between):
        yield from _walk_component(expression.operand)
        yield from _walk_component(expression.lower)
        yield from _walk_component(expression.upper)
    elif isinstance(expression, InList):
        yield from _walk_component(expression.operand)
        for value in expression.values:
            yield from _walk_component(value)
    elif isinstance(expression, Like):
        yield from _walk_component(expression.operand)
        yield from _walk_component(expression.pattern)
    elif isinstance(expression, Call):
        for argument in expression.args:
            yield from _walk_component(argument)
    elif isinstance(expression, FilteredAggregate):
        yield from _walk_component(expression.aggregate)
        yield from _walk_component(expression.predicate)

def _measure_stage_key(reference: MeasureRef) -> str:
    return f"measure:{reference.dataset}.{reference.metric}"

def _direct_stage_key(metric: Metric) -> str:
    if metric.qualified_name != metric.name:
        return f"measure:{metric.qualified_name}"
    return f"metric:{metric.qualified_name}"

def _filter_dataset(item: QueryFilter) -> str | None:
    if isinstance(item, PlannedFilter):
        assert isinstance(item.expression_owner, Field)
        return item.expression_owner.dataset
    return item.dataset

def _bind_query_filter(model: BoundModel, text: str) -> PlannedExpressionFilter:
    if not isinstance(text, str) or not text.strip():
        raise CompilationError("INVALID_FILTER", "$.filter", "filter must be a non-empty expression string")
    try:
        expression = parse(text)
    except ExpressionError as error:
        raise CompilationError("INVALID_FILTER", "$.filter", str(error)) from error
    datasets: set[str] = set()

    def bind(node: Expr) -> Expr:
        if isinstance(node, Column):
            if node.dataset is None:
                raise CompilationError(
                    "UNKNOWN_FIELD",
                    "$.filter",
                    f"filter field {node.name!r} must be dataset-qualified",
                )
            field = model.field_by_name.get(f"{node.dataset}.{node.name}")
            if field is None:
                raise CompilationError(
                    "UNKNOWN_FIELD",
                    "$.filter",
                    f"unknown filter field {node.dataset}.{node.name!r}",
                )
            datasets.add(field.dataset)
            return field.expression
        if isinstance(node, Literal):
            return node
        if isinstance(node, Binary):
            return Binary(node.op, bind(node.left), bind(node.right))
        if isinstance(node, Unary):
            return Unary(node.op, bind(node.operand))
        if isinstance(node, IsNull):
            return IsNull(bind(node.operand), node.negated)
        if isinstance(node, Case):
            return Case(
                tuple((bind(condition), bind(value)) for condition, value in node.branches),
                bind(node.otherwise) if node.otherwise is not None else None,
            )
        if isinstance(node, Between):
            return Between(bind(node.operand), bind(node.lower), bind(node.upper), node.negated)
        if isinstance(node, InList):
            return InList(bind(node.operand), tuple(bind(value) for value in node.values), node.negated)
        if isinstance(node, Like):
            return Like(bind(node.operand), bind(node.pattern), node.negated)
        if isinstance(node, Call):
            if node.name in SUPPORTED_AGGREGATES or node.name not in SUPPORTED_SCALAR_FUNCTIONS:
                raise CompilationError(
                    "INVALID_FILTER",
                    "$.filter",
                    f"unsupported filter function {node.name}",
                )
            if node.distinct:
                raise CompilationError("INVALID_FILTER", "$.filter", "DISTINCT is not valid in a scalar filter function")
            accepted = SUPPORTED_SCALAR_FUNCTIONS[node.name]
            if len(node.args) not in accepted:
                raise CompilationError(
                    "INVALID_FILTER",
                    "$.filter",
                    f"{node.name} has an invalid argument count",
                )
            return Call(node.name, tuple(bind(argument) for argument in node.args), node.distinct)
        if isinstance(node, (FilteredAggregate, MeasureRef, Star)):
            raise CompilationError("INVALID_FILTER", "$.filter", "filter cannot contain aggregates or *")
        raise AssertionError(node)

    bound = bind(expression)
    if len(datasets) > 1:
        raise CompilationError(
            "CROSS_DATASET_FILTER",
            "$.filter",
            "filter expression may reference fields from only one dataset",
            tuple(sorted(datasets)),
        )
    return PlannedExpressionFilter(bound, next(iter(datasets)) if datasets else None)

def _field(model: BoundModel, name: str, location: str) -> Field:
    field = model.field_by_name.get(name)
    if field is None:
        raise CompilationError("UNKNOWN_FIELD", location, f"unknown field {name!r}")
    return field

def _metric(model: BoundModel, name: str, location: str) -> Metric:
    metric = model.metric_by_name.get(name)
    if metric is None:
        raise CompilationError("UNKNOWN_METRIC", location, f"unknown or ambiguous metric {name!r}")
    return metric

def _unique_path(model: BoundModel, root: str, target: str) -> tuple[Relationship, ...]:
    if root == target:
        return ()
    found: list[tuple[Relationship, ...]] = []
    def visit(dataset: str, path: tuple[Relationship, ...]) -> None:
        if len(found) >= 2:
            return
        for edge in model.outgoing[dataset]:
            next_path = (*path, edge)
            if edge.one == target:
                found.append(next_path)
                if len(found) >= 2:
                    return
            else:
                visit(edge.one, next_path)
    visit(root, ())
    if len(found) > 1:
        raise CompilationError("AMBIGUOUS_JOIN", "$.dimensions", f"multiple safe paths from {root} to {target}", tuple(" -> ".join(e.name for e in path) for path in found))
    if found:
        return found[0]
    if _undirected_connected(model, root, target):
        raise CompilationError("FANOUT_JOIN", "$.dimensions", f"reaching {target} from {root} requires a one-to-many traversal")
    raise CompilationError("UNREACHABLE_DATASET", "$.dimensions", f"no relationship path from {root} to {target}")

def _undirected_connected(model: BoundModel, root: str, target: str) -> bool:
    neighbors = {name: set() for name in model.dataset_by_name}
    for edge in model.relationships:
        neighbors[edge.many].add(edge.one)
        neighbors[edge.one].add(edge.many)
    pending, seen = [root], {root}
    while pending:
        current = pending.pop()
        if current == target:
            return True
        for neighbor in neighbors[current] - seen:
            seen.add(neighbor)
            pending.append(neighbor)
    return False

def _validate_filters(query: Query) -> None:
    if query.filter is not None and query.where:
        raise CompilationError("INVALID_FILTER", "$", "use either filter or where, not both")
    allowed = {"eq", "ne", "lt", "le", "gt", "ge", "is_null", "is_not_null"}
    for collection, filters in (("where", query.where), ("having", query.having)):
        for i, item in enumerate(filters):
            if not isinstance(item.op, str) or item.op not in allowed:
                raise CompilationError("INVALID_FILTER", f"$.{collection}[{i}].op", f"unsupported operator {item.op!r}")
            if item.op in {"is_null", "is_not_null"} and item.value is not None:
                raise CompilationError("INVALID_FILTER", f"$.{collection}[{i}].value", "NULL predicates do not take a value")
            if item.op not in {"is_null", "is_not_null"} and item.value is None:
                raise CompilationError("INVALID_FILTER", f"$.{collection}[{i}].value", "use is_null or is_not_null for NULL")
