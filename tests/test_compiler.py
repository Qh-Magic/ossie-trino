from __future__ import annotations

import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from datetime import datetime

from ossie_trino import (
    CompilationError,
    Compiler,
    FieldFilter,
    MetricFilter,
    Query,
)

from tests.fixtures import clone, field, metric, sales_model


class CompilerTests(unittest.TestCase):
    def test_exposes_source_model_identity_for_audit(self):
        document = json.dumps(sales_model())
        compiler = Compiler.from_text(document, format="json")

        self.assertEqual("sales", compiler.model_identity.name)
        self.assertEqual("0.2.0.dev0", compiler.model_identity.version)
        self.assertEqual(
            hashlib.sha256(document.encode("utf-8")).hexdigest(),
            compiler.model_identity.sha256,
        )

    def test_file_identity_hashes_exact_source_bytes(self):
        content = json.dumps(sales_model(), indent=2).replace("\n", "\r\n").encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            path.write_bytes(content)
            compiler = Compiler.from_file(path)

        self.assertEqual(hashlib.sha256(content).hexdigest(), compiler.model_identity.sha256)

    def compiler(self, model: dict) -> Compiler:
        return Compiler.from_text(json.dumps(model), format="json")

    def compile(self, model: dict, query: Query):
        return self.compiler(model).compile(query)

    def assert_error(self, code: str, model: dict, query: Query | None = None):
        with self.assertRaises(CompilationError) as caught:
            compiler = Compiler.from_text(json.dumps(model), format="json")
            if query is not None:
                compiler.compile(query)
        self.assertEqual(code, caught.exception.code)

    def test_compiles_metric_and_joined_dimension(self):
        result = self.compile(
            sales_model(),
            Query(metrics=("revenue",), dimensions=("customers.country",)),
        )
        self.assertEqual(
            "SELECT t1.\"country\" AS \"d0\", SUM(t0.\"amount\") AS \"m0\"\n"
            "FROM \"default_catalog\".\"sales\".\"orders\" AS t0\n"
            "LEFT JOIN \"default_catalog\".\"sales\".\"customers\" AS t1 "
            "ON t0.\"customer_id\" = t1.\"customer_id\"\n"
            "GROUP BY t1.\"country\"",
            result.sql,
        )
        self.assertEqual(("customers.country", "revenue"), tuple(c.semantic_name for c in result.columns))

    def test_compiles_where_having_and_limit_with_safe_literals(self):
        result = self.compile(
            sales_model(),
            Query(
                metrics=("revenue",),
                where=(FieldFilter("orders.status", "eq", "O'Reilly"),),
                having=(MetricFilter("revenue", "gt", 100),),
                limit=10,
            ),
        )
        self.assertIn("WHERE t0.\"status\" = 'O''Reilly'", result.sql)
        self.assertIn("HAVING SUM(t0.\"amount\") > 100", result.sql)
        self.assertTrue(result.sql.endswith("LIMIT 10"))

    def test_compiles_bound_layer3_filter_expression(self):
        result = self.compile(
            sales_model(),
            Query(
                metrics=("revenue",),
                filter="orders.status IN ('complete', 'pending') OR orders.status LIKE 'vip%'",
            ),
        )

        self.assertIn(
            "WHERE ((t0.\"status\" IN ('complete', 'pending')) OR (t0.\"status\" LIKE 'vip%'))",
            result.sql,
        )

    def test_rejects_cross_dataset_layer3_filter(self):
        self.assert_error(
            "CROSS_DATASET_FILTER",
            sales_model(),
            Query(
                metrics=("revenue",),
                filter="orders.status = 'complete' AND customers.country = 'TW'",
            ),
        )

    def test_rejects_filter_and_legacy_where_together(self):
        self.assert_error(
            "INVALID_FILTER",
            sales_model(),
            Query(
                metrics=("revenue",),
                filter="orders.status = 'complete'",
                where=(FieldFilter("orders.status", "eq", "complete"),),
            ),
        )

    def test_constant_layer3_filter_needs_no_join(self):
        result = self.compile(
            sales_model(),
            Query(metrics=("revenue",), filter="FALSE"),
        )

        self.assertIn("WHERE FALSE", result.sql)
        self.assertNotIn("JOIN", result.sql)

    def test_compiles_dimension_only_query_from_its_dataset(self):
        result = self.compile(
            sales_model(),
            Query(dimensions=("customers.country",)),
        )

        self.assertEqual(
            "SELECT t0.\"country\" AS \"d0\"\n"
            "FROM \"default_catalog\".\"sales\".\"customers\" AS t0\n"
            "GROUP BY t0.\"country\"",
            result.sql,
        )
        self.assertEqual(("customers.country",), tuple(c.semantic_name for c in result.columns))

    def test_dimension_only_query_uses_many_side_as_anchor(self):
        result = self.compile(
            sales_model(),
            Query(dimensions=("orders.status", "customers.country")),
        )

        self.assertIn("FROM \"default_catalog\".\"sales\".\"orders\" AS t0", result.sql)
        self.assertIn("LEFT JOIN \"default_catalog\".\"sales\".\"customers\" AS t1", result.sql)
        self.assertIn("GROUP BY t0.\"status\", t1.\"country\"", result.sql)

    def test_dimension_only_query_rejects_missing_anchor(self):
        model = sales_model()
        model["datasets"].append(
            {
                "name": "products",
                "source": "default_catalog.sales.products",
                "fields": [field("category", "category")],
            }
        )

        self.assert_error(
            "NO_ANCHOR",
            model,
            Query(dimensions=("customers.country", "products.category")),
        )

    def test_rejects_empty_query(self):
        self.assert_error("EMPTY_QUERY", sales_model(), Query())

    def test_rejects_unknown_reference(self):
        self.assert_error(
            "UNKNOWN_FIELD",
            sales_model(),
            Query(metrics=("revenue",), dimensions=("customers.missing",)),
        )

    def test_plans_metrics_from_different_home_datasets_in_stages(self):
        model = sales_model()
        model["datasets"][1]["metrics"] = [metric("customer_count", "COUNT(customer_id)")]
        result = self.compile(
            model,
            Query(
                metrics=("revenue", "customers.customer_count"),
                dimensions=("customers.country",),
            ),
        )

        self.assertIn("SUM(t0.\"amount\") AS \"value\", 1 AS \"present\"", result.sql)
        self.assertIn("COUNT(t0.\"customer_id\") AS \"value\", 1 AS \"present\"", result.sql)
        self.assertIn("FULL OUTER JOIN s1", result.sql)
        self.assertIn("THEN NULL ELSE s0.\"value\" END AS \"m0\"", result.sql)
        self.assertIn("THEN 0 ELSE s1.\"value\" END AS \"m1\"", result.sql)

    def test_rejects_reverse_fanout(self):
        model = sales_model()
        model["datasets"][1]["metrics"] = [metric("customer_count", "COUNT(customer_id)")]
        self.assert_error(
            "FANOUT_JOIN",
            model,
            Query(metrics=("customers.customer_count",), dimensions=("orders.status",)),
        )

    def test_rejects_ambiguous_paths(self):
        model = sales_model()
        model["datasets"].append(
            {
                "name": "regions",
                "source": "default_catalog.sales.regions",
                "primary_key": ["region_id"],
                "fields": [field("region_id", "region_id"), field("name", "name")],
            }
        )
        model["datasets"][0]["fields"].append(field("region_id", "region_id"))
        model["datasets"][1]["fields"].append(field("region_id", "region_id"))
        model["relationships"].extend(
            [
                {
                    "name": "orders_region",
                    "from": "orders",
                    "to": "regions",
                    "from_columns": ["region_id"],
                    "to_columns": ["region_id"],
                },
                {
                    "name": "customers_region",
                    "from": "customers",
                    "to": "regions",
                    "from_columns": ["region_id"],
                    "to_columns": ["region_id"],
                },
            ]
        )
        self.assert_error(
            "AMBIGUOUS_JOIN",
            model,
            Query(metrics=("revenue",), dimensions=("regions.name",)),
        )

    def test_rejects_relationship_cycle_at_model_boundary(self):
        model = sales_model()
        model["datasets"][0]["primary_key"] = ["customer_id"]
        model["datasets"][1]["fields"].append(field("order_customer_id", "order_customer_id"))
        model["relationships"].append(
            {
                "name": "customers_orders",
                "from": "customers",
                "to": "orders",
                "from_columns": ["order_customer_id"],
                "to_columns": ["customer_id"],
            }
        )
        self.assert_error("RELATIONSHIP_CYCLE", model)

    def test_rejects_unsupported_expression_instead_of_passing_sql_through(self):
        model = sales_model()
        model["metrics"][0] = metric("revenue", "SUM(orders.amount) OVER ()")
        self.assert_error("UNSUPPORTED_EXPRESSION", model)

    def test_expands_computed_fields_inside_metrics(self):
        model = sales_model()
        model["datasets"][0]["fields"].append(field("double_amount", "amount * 2"))
        model["metrics"][0] = metric("revenue", "SUM(orders.double_amount)")
        result = self.compile(model, Query(metrics=("revenue",)))
        self.assertIn("SUM((t0.\"amount\" * 2)) AS \"m0\"", result.sql)
        self.assertNotIn("\"double_amount\"", result.sql)

    def test_rejects_computed_field_cycles(self):
        model = sales_model()
        model["datasets"][0]["fields"].extend(
            [field("first", "second + 1"), field("second", "first + 1")]
        )
        self.assert_error("EXPRESSION_CYCLE", model)

    def test_rejects_cross_dataset_computed_fields(self):
        model = sales_model()
        model["datasets"][1]["fields"].append(field("order_status", "orders.status"))
        self.assert_error("CROSS_DATASET_FIELD", model)

    def test_rejects_star_for_aggregates_other_than_count(self):
        model = sales_model()
        model["metrics"][0] = metric("revenue", "SUM(*)")
        self.assert_error("INVALID_METRIC_EXPRESSION", model)

    def test_rejects_nested_or_free_star(self):
        for expression in ("SUM(orders.amount + *)", "SUM(orders.amount) + *"):
            with self.subTest(expression=expression):
                model = sales_model()
                model["metrics"][0] = metric("revenue", expression)
                self.assert_error("INVALID_METRIC_EXPRESSION", model)

        model = sales_model()
        model["datasets"][0]["fields"].append(field("wild", "*"))
        self.assert_error("INVALID_FIELD_EXPRESSION", model)

    def test_rejects_computed_relationship_keys(self):
        model = sales_model()
        customer_id = model["datasets"][0]["fields"][0]
        customer_id["expression"]["dialects"][0]["expression"] = "customer_id + 1"
        self.assert_error("COMPUTED_RELATIONSHIP_KEY", model)

    def test_ossie_03_expression_sql_shape_is_accepted(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"][0]["expression"] = {"sql": "SUM(orders.amount)"}
        result = self.compile(model, Query(metrics=("revenue",)))
        self.assertIn("SUM(t0.\"amount\") AS \"m0\"", result.sql)

    def test_ossie_03_field_expression_names_source_columns(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["fields"].append(field("gross", "raw_amount * 2"))
        model["metrics"][0]["expression"] = {"sql": "SUM(orders.gross)"}

        result = self.compile(model, Query(metrics=("revenue",)))

        self.assertIn("SUM((t0.\"raw_amount\" * 2))", result.sql)

    def test_ossie_03_does_not_expand_field_names_inside_field_expressions(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["fields"].extend(
            [field("first", "second + 1"), field("second", "first + 1")]
        )
        model["metrics"][0]["expression"] = {"sql": "SUM(orders.first)"}

        result = self.compile(model, Query(metrics=("revenue",)))

        self.assertIn("SUM((t0.\"second\" + 1))", result.sql)

    def test_ossie_03_rejects_unknown_core_properties(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["dialects"] = []
        self.assert_error("INVALID_MODEL", model)

    def test_ossie_03_official_schema_rejects_invalid_metadata(self):
        cases = []

        invalid_datatype = sales_model()
        invalid_datatype["version"] = "0.3.0.dev0"
        invalid_datatype["datasets"][0]["fields"][0]["datatype"] = "Money"
        cases.append(invalid_datatype)

        invalid_dimension = sales_model()
        invalid_dimension["version"] = "0.3.0.dev0"
        invalid_dimension["datasets"][0]["fields"][0]["dimension"] = {"is_time": "yes"}
        cases.append(invalid_dimension)

        invalid_dialect = sales_model()
        invalid_dialect["version"] = "0.3.0.dev0"
        invalid_dialect["datasets"][0]["fields"][0]["expression"] = {
            "dialects": [{"dialect": "OSSIE_SQL_2026"}]
        }
        cases.append(invalid_dialect)

        invalid_extension = sales_model()
        invalid_extension["version"] = "0.3.0.dev0"
        invalid_extension["nonportable_extensions"] = [{"vendor_name": "STARROCKS"}]
        cases.append(invalid_extension)

        for model in cases:
            with self.subTest(model=model):
                self.assert_error("INVALID_MODEL", model)

    def test_ossie_03_official_schema_accepts_portable_metadata(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["description"] = "Sales model"
        model["datasets"][0]["fields"][0].update(
            {"datatype": "Integer", "dimension": {"is_time": False}}
        )
        model["nonportable_extensions"] = [
            {"vendor_name": "STARROCKS", "data": {"distribution": ["customer_id"]}}
        ]

        result = self.compile(model, Query(metrics=("revenue",)))

        self.assertIn("SUM(t0.\"amount\")", result.sql)

    def test_ossie_03_requires_expression_objects(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"][0]["expression"] = "SUM(orders.amount)"
        self.assert_error("INVALID_MODEL", model)

    def test_rejects_field_metric_namespace_collision(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["metrics"] = [metric("amount", "SUM(amount)")]
        self.assert_error("DUPLICATE_NAME", model)

    def test_ossie_03_dataset_metric_accepts_source_column(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"] = []
        model["datasets"][0]["metrics"] = [
            {"name": "raw_revenue", "expression": {"sql": "SUM(source.raw_amount)"}}
        ]

        result = self.compile(model, Query(metrics=("orders.raw_revenue",)))

        self.assertIn("SUM(t0.\"raw_amount\") AS \"m0\"", result.sql)

    def test_ossie_03_source_field_shadows_source_column_qualifier(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"] = []
        model["datasets"][0]["fields"].append(field("source", "physical_source"))
        model["datasets"][0]["metrics"] = [
            {"name": "raw_revenue", "expression": {"sql": "SUM(source.raw_amount)"}}
        ]

        self.assert_error("INVALID_METRIC_EXPRESSION", model)

    def test_ossie_03_model_metric_requires_dataset_qualified_fields(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"][0]["expression"] = {"sql": "SUM(amount)"}

        self.assert_error("UNKNOWN_FIELD", model)

    def test_ossie_03_lowers_direct_scalar_functions_to_trino(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["fields"].extend(
            [
                {"name": "normalized_status", "expression": {"sql": "UPPER(TRIM(raw_status))"}},
                {"name": "order_month", "expression": {"sql": "DATE_TRUNC('month', ordered_at)"}},
            ]
        )
        model["metrics"][0]["expression"] = {"sql": "SUM(ABS(orders.amount))"}

        result = self.compile(
            model,
            Query(metrics=("revenue",), dimensions=("orders.normalized_status", "orders.order_month")),
        )

        self.assertIn("UPPER(TRIM(t0.\"raw_status\"))", result.sql)
        self.assertIn("DATE_TRUNC('month', t0.\"ordered_at\")", result.sql)
        self.assertIn("SUM(ABS(t0.\"amount\"))", result.sql)

    def test_ossie_03_compiles_required_string_and_math_functions(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["fields"].append(
            {
                "name": "status_label",
                "expression": {
                    "sql": "CONCAT(UPPER(status), '-', SPLIT_PART('a-b', '-', 2))"
                },
            }
        )
        model["metrics"] = [
            {
                "name": "math_profile",
                "expression": {
                    "sql": (
                        "GREATEST(MOD(COUNT(orders.customer_id), 5), "
                        "LEAST(POWER(2, 3), 10)) + SIGN(SUM(orders.amount)) + "
                        "EXP(0) + LN(1) + LOG(10, 100) + LOG10(100)"
                    )
                },
            }
        ]

        result = self.compiler(model).compile(
            Query(metrics=("math_profile",), dimensions=("orders.status_label",))
        )

        for sql in (result.sql,):
            self.assertIn("CONCAT(UPPER(", sql)
            self.assertIn("SPLIT_PART('a-b', '-', 2)", sql)
            self.assertIn("GREATEST(MOD(COUNT(", sql)
            self.assertIn("LEAST(POWER(2, 3), 10)", sql)
            self.assertIn("SIGN(SUM(", sql)
            self.assertIn("EXP(0)", sql)
            self.assertIn("LN(1)", sql)
            self.assertIn("LOG(10, 100)", sql)
            self.assertIn("LOG10(100)", sql)

    def test_ossie_03_preserves_sample_and_population_statistics(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"] = [
            {
                "name": name.lower(),
                "expression": {"sql": f"{name}(orders.amount)"},
            }
            for name in (
                "STDDEV",
                "STDDEV_POP",
                "STDDEV_SAMP",
                "VARIANCE",
                "VAR_POP",
                "VAR_SAMP",
            )
        ]
        query = Query(metrics=tuple(metric["name"] for metric in model["metrics"]))

        result = self.compiler(model).compile(query)

        for sql in (result.sql,):
            self.assertIn("STDDEV_SAMP(", sql)
            self.assertIn("STDDEV_POP(", sql)
            self.assertIn("VAR_SAMP(", sql)
            self.assertIn("VAR_POP(", sql)
            self.assertNotIn("STDDEV(t0", sql)
            self.assertNotIn("VARIANCE(t0", sql)

    def test_ossie_03_lowers_required_string_search_functions(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["fields"].extend(
            [
                {"name": "status_position", "expression": {"sql": "CHARINDEX('o', status)"}},
                {"name": "status_contains", "expression": {"sql": "CONTAINS(status, 'o')"}},
                {"name": "status_starts", "expression": {"sql": "STARTSWITH(status, 'c')"}},
                {"name": "status_ends", "expression": {"sql": "ENDSWITH(status, 'e')"}},
            ]
        )
        query = Query(
            metrics=("revenue",),
            dimensions=tuple(
                f"orders.status_{suffix}"
                for suffix in ("position", "contains", "starts", "ends")
            ),
        )

        result = self.compiler(model).compile(query)

        self.assertIn('STRPOS(t0."status", \'o\')', result.sql)
        self.assertNotIn("LOCATE(", result.sql)
        self.assertIn('(SUBSTRING(t0."status", -(LENGTH(\'e\'))) = \'e\')', result.sql)
        for portable_name in ("CHARINDEX(", "CONTAINS(", "STARTSWITH(", "ENDSWITH("):
            self.assertNotIn(portable_name, result.sql)

    def test_ossie_03_lowers_truncation_toward_zero(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"] = [
            {
                "name": "positive",
                "expression": {"sql": "TRUNC(SUM(orders.amount) / 7, 2)"},
            },
            {
                "name": "negative",
                "expression": {"sql": "TRUNCATE(-(SUM(orders.amount) / 7), 2)"},
            },
            {
                "name": "integer",
                "expression": {"sql": "TRUNC(SUM(orders.amount) / 7)"},
            },
        ]
        query = Query(metrics=("positive", "negative", "integer"))

        result = self.compiler(model).compile(query)

        for sql in (result.sql,):
            self.assertIn("SIGN(", sql)
            self.assertIn("FLOOR(ABS(", sql)
            self.assertIn("POWER(10, 2)", sql)
            self.assertIn("POWER(10, 0)", sql)
            self.assertNotIn("TRUNC(", sql)
            self.assertNotIn("TRUNCATE(", sql)

    def test_ossie_03_lowers_integer_division_to_true_division(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"] = [
            {
                "name": "count_sevenths",
                "expression": {"sql": "COUNT(orders.customer_id) / 7"},
            },
            {
                "name": "orders_per_customer",
                "expression": {
                    "sql": "COUNT(orders.customer_id) / COUNT(customers.customer_id)"
                },
            },
        ]

        direct = self.compiler(model).compile(Query(metrics=("count_sevenths",)))
        staged = self.compiler(model).compile(
            Query(metrics=("orders_per_customer",), dimensions=("customers.country",)),
        )

        self.assertIn('CAST(COUNT(t0."customer_id") AS DOUBLE) / 7', direct.sql)
        self.assertIn("CAST(CASE WHEN s0.", staged.sql)
        self.assertIn("AS DOUBLE) / CASE WHEN s1.", staged.sql)

    def test_ossie_03_compiles_required_conditional_functions(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"] = [
            {
                "name": "safe_average",
                "expression": {
                    "sql": "COALESCE(SUM(orders.amount) / NULLIF(COUNT(orders.customer_id), 0), 0)"
                },
            }
        ]

        result = self.compile(model, Query(metrics=("safe_average",)))

        self.assertIn(
            "COALESCE((CAST(SUM(t0.\"amount\") AS DOUBLE) / NULLIF(COUNT(t0.\"customer_id\"), 0)), 0)",
            result.sql,
        )

    def test_ossie_03_lowers_required_conditional_aliases_to_trino(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"] = [
            {
                "name": "normalized_revenue",
                "expression": {
                    "sql": (
                        "IFF(COUNT(orders.customer_id) > 0, "
                        "NVL2(SUM(orders.amount), ZEROIFNULL(SUM(orders.amount)), 0), "
                        "NVL(NULLIFZERO(COUNT(orders.customer_id)), 0))"
                    )
                },
            }
        ]

        result = self.compile(model, Query(metrics=("normalized_revenue",)))

        self.assertIn("IF((COUNT(t0.\"customer_id\") > 0)", result.sql)
        self.assertIn("IF(SUM(t0.\"amount\") IS NOT NULL", result.sql)
        self.assertIn("COALESCE(SUM(t0.\"amount\"), 0)", result.sql)
        self.assertIn("COALESCE(NULLIF(COUNT(t0.\"customer_id\"), 0), 0)", result.sql)
        for ossie_alias in ("IFF(", "NVL(", "NVL2(", "ZEROIFNULL(", "NULLIFZERO("):
            self.assertNotIn(ossie_alias, result.sql)

    def test_ossie_03_lowers_conditional_aliases_after_stage_stitching(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"] = [
            {
                "name": "safe_ratio",
                "expression": {
                    "sql": "NVL(SUM(orders.amount), 0) / NULLIFZERO(COUNT(customers.customer_id))"
                },
            }
        ]

        result = self.compile(
            model,
            Query(metrics=("safe_ratio",), dimensions=("customers.country",)),
        )

        self.assertIn("COALESCE(CASE WHEN s0.\"present\" IS NULL", result.sql)
        self.assertIn("NULLIF(CASE WHEN s1.\"present\" IS NULL", result.sql)
        self.assertNotIn("NVL(", result.sql)
        self.assertNotIn("NULLIFZERO(", result.sql)

    def test_compiles_the_same_semantic_plan_for_trino(self):
        model = sales_model()

        result = self.compiler(model).compile(
            Query(
                metrics=("revenue",),
                dimensions=("customers.country",),
                where=(FieldFilter("orders.status", "eq", "complete"),),
            ),
        )

        self.assertIn('SUM(t0."amount") AS "m0"', result.sql)
        self.assertIn(
            'LEFT JOIN "default_catalog"."sales"."customers" AS t1',
            result.sql,
        )
        self.assertIn('WHERE t0."status" = \'complete\'', result.sql)
        self.assertNotIn('`', result.sql)

    def test_trino_target_lowers_cross_dataset_conditional_metric(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"] = [
            {
                "name": "safe_ratio",
                "expression": {
                    "sql": "NVL(SUM(orders.amount), 0) / NULLIFZERO(COUNT(customers.customer_id))"
                },
            }
        ]

        result = self.compiler(model).compile(
            Query(metrics=("safe_ratio",), dimensions=("customers.country",)),
        )

        self.assertIn('COALESCE(CASE WHEN s0."present" IS NULL', result.sql)
        self.assertIn('FULL OUTER JOIN s1', result.sql)
        self.assertIn(
            '(s0."k0" = s1."k0" OR (s0."k0" IS NULL AND s1."k0" IS NULL))',
            result.sql,
        )
        self.assertNotIn('COALESCE(s0."k0")', result.sql)
        self.assertNotIn("IFNULL", result.sql)
        self.assertNotIn('`', result.sql)

    def test_trino_target_uses_timestamp_type(self):
        model = sales_model()
        model["datasets"][0]["fields"].append(field("ordered_at", "ordered_at"))

        result = self.compiler(model).compile(
            Query(
                metrics=("revenue",),
                where=(FieldFilter("orders.ordered_at", "ge", datetime(2026, 1, 1, 12, 30)),),
            ),
        )

        self.assertIn("CAST('2026-01-01 12:30:00' AS TIMESTAMP)", result.sql)
        self.assertNotIn("DATETIME", result.sql)

    def test_trino_target_does_not_rewrite_string_literals(self):
        model = sales_model()

        result = self.compiler(model).compile(
            Query(
                metrics=("revenue",),
                where=(FieldFilter("orders.status", "eq", "IFNULL DATETIME `literal`"),),
            ),
        )

        self.assertIn("'IFNULL DATETIME `literal`'", result.sql)

    def test_rejects_target_selector(self):
        with self.assertRaises(TypeError):
            self.compiler(sales_model()).compile(
                Query(metrics=("revenue",)),
                target="duckdb",  # type: ignore[arg-type]
            )

    def test_trino_target_lowers_string_edge_functions(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["fields"].extend(
            [
                {"name": "status_prefix", "expression": {"sql": "LEFT(status, 2)"}},
                {
                    "name": "status_suffix",
                    "expression": {"sql": "RIGHT(UPPER(status), 3)"},
                },
            ]
        )

        result = self.compiler(model).compile(
            Query(
                metrics=("revenue",),
                dimensions=("orders.status_prefix", "orders.status_suffix"),
            ),
        )

        self.assertIn('SUBSTRING(t0."status", 1, 2)', result.sql)
        self.assertIn('SUBSTRING(UPPER(t0."status"), -(3))', result.sql)
        self.assertNotIn("LEFT(", result.sql)
        self.assertNotIn("RIGHT(", result.sql)

    def test_ossie_03_validates_conditional_function_arity(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"] = [
            {
                "name": "invalid",
                "expression": {"sql": "COALESCE(SUM(orders.amount))"},
            }
        ]

        self.assert_error("INVALID_EXPRESSION_ARITY", model)

    def test_rejects_wrong_scalar_function_arity(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["fields"].append(
            {"name": "bad_status", "expression": {"sql": "UPPER(status, status)"}}
        )

        self.assert_error("INVALID_EXPRESSION_ARITY", model)

    def test_rejects_unmapped_ossie_function(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["fields"].append(
            {"name": "formatted", "expression": {"sql": "TO_CHAR(ordered_at, 'YYYY')"}}
        )

        self.assert_error("UNSUPPORTED_EXPRESSION", model)

    def test_ossie_03_compiles_conditional_aggregation(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"][0]["expression"] = {
            "sql": (
                "SUM(CASE WHEN orders.status = 'complete' AND orders.amount IS NOT NULL "
                "THEN -orders.amount % 10 ELSE 0 END)"
            )
        }

        result = self.compile(model, Query(metrics=("revenue",)))

        self.assertIn(
            "SUM(CASE WHEN ((t0.\"status\" = 'complete') AND (t0.\"amount\" IS NOT NULL)) "
            "THEN ((-t0.\"amount\") % 10) ELSE 0 END)",
            result.sql,
        )

    def test_ossie_03_compiles_case_field_with_null_predicate(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["fields"].append(
            {
                "name": "status_bucket",
                "expression": {
                    "sql": "CASE WHEN raw_status IS NULL OR raw_status = '' THEN 'unknown' ELSE raw_status END"
                },
            }
        )

        result = self.compile(
            model,
            Query(metrics=("revenue",), dimensions=("orders.status_bucket",)),
        )

        self.assertIn(
            "CASE WHEN ((t0.\"raw_status\" IS NULL) OR (t0.\"raw_status\" = '')) "
            "THEN 'unknown' ELSE t0.\"raw_status\" END",
            result.sql,
        )

    def test_ossie_03_predicate_precedence_is_preserved(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["fields"].append(
            {
                "name": "flag",
                "expression": {
                    "sql": "CASE WHEN NOT raw_status = 'closed' AND (amount + 1) IS NOT NULL THEN 1 ELSE 0 END"
                },
            }
        )

        result = self.compile(model, Query(metrics=("revenue",), dimensions=("orders.flag",)))

        self.assertIn(
            "CASE WHEN ((NOT (t0.\"raw_status\" = 'closed')) AND ((t0.\"amount\" + 1) IS NOT NULL)) "
            "THEN 1 ELSE 0 END",
            result.sql,
        )

    def test_ossie_03_compiles_list_range_and_pattern_predicates(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["fields"].append(
            {
                "name": "segment",
                "expression": {
                    "sql": (
                        "CASE WHEN raw_status IN ('new', 'open') AND amount BETWEEN 10 AND 100 "
                        "AND customer_name LIKE 'A%' THEN 'target' "
                        "WHEN raw_status NOT IN ('closed') OR amount NOT BETWEEN 0 AND 1000 "
                        "OR customer_name NOT LIKE '%test%' THEN 'other' ELSE 'excluded' END"
                    )
                },
            }
        )

        result = self.compile(model, Query(metrics=("revenue",), dimensions=("orders.segment",)))

        self.assertIn("t0.\"raw_status\" IN ('new', 'open')", result.sql)
        self.assertIn("t0.\"amount\" BETWEEN 10 AND 100", result.sql)
        self.assertIn("t0.\"customer_name\" LIKE 'A%'", result.sql)
        self.assertIn("t0.\"raw_status\" NOT IN ('closed')", result.sql)
        self.assertIn("t0.\"amount\" NOT BETWEEN 0 AND 1000", result.sql)
        self.assertIn("t0.\"customer_name\" NOT LIKE '%test%'", result.sql)

    def test_ossie_03_rejects_empty_in_list(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["fields"].append(
            {"name": "bad", "expression": {"sql": "raw_status IN ()"}}
        )

        self.assert_error("UNSUPPORTED_EXPRESSION", model)

    def test_ossie_03_lowers_distinct_filtered_aggregates(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"] = [
            {
                "name": "active_customers",
                "expression": {
                    "sql": "COUNT(DISTINCT orders.customer_id) FILTER (WHERE orders.status = 'complete')"
                },
            },
            {
                "name": "active_rows",
                "expression": {
                    "sql": "COUNT(*) FILTER (WHERE orders.status = 'complete')"
                },
            },
            {
                "name": "active_average",
                "expression": {
                    "sql": "AVG(orders.amount) FILTER (WHERE orders.status = 'complete')"
                },
            },
        ]

        result = self.compile(
            model,
            Query(metrics=("active_customers", "active_rows", "active_average")),
        )

        self.assertIn(
            "COUNT(DISTINCT CASE WHEN (t0.\"status\" = 'complete') THEN t0.\"customer_id\" END)",
            result.sql,
        )
        self.assertIn(
            "COUNT(CASE WHEN (t0.\"status\" = 'complete') THEN 1 END)",
            result.sql,
        )
        self.assertIn(
            "AVG(CASE WHEN (t0.\"status\" = 'complete') THEN t0.\"amount\" END)",
            result.sql,
        )
        self.assertNotIn("ELSE 0", result.sql)

    def test_ossie_03_supports_sum_distinct(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"][0]["expression"] = {"sql": "SUM(DISTINCT orders.amount)"}

        result = self.compile(model, Query(metrics=("revenue",)))

        self.assertIn("SUM(DISTINCT t0.\"amount\")", result.sql)

    def test_ossie_03_rejects_distinct_on_unsupported_aggregate(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"][0]["expression"] = {"sql": "AVG(DISTINCT orders.amount)"}

        self.assert_error("INVALID_METRIC_EXPRESSION", model)

    def test_ossie_03_evaluates_same_grain_dataset_metrics_with_agg(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["metrics"] = [
            {"name": "gross_revenue", "expression": {"sql": "SUM(amount)"}},
            {"name": "orders_count", "expression": {"sql": "COUNT(customer_id)"}},
        ]
        model["metrics"] = [
            {
                "name": "average_order_value",
                "expression": {
                    "sql": "AGG(orders.gross_revenue) / AGG(orders.orders_count)"
                },
            }
        ]

        result = self.compile(model, Query(metrics=("average_order_value",)))

        self.assertIn('(CAST(SUM(t0."amount") AS DOUBLE) / COUNT(t0."customer_id"))', result.sql)
        self.assertNotIn("AGG(", result.sql)

    def test_ossie_03_plans_global_cross_grain_agg_in_stages(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["metrics"] = [
            {"name": "orders_count", "expression": {"sql": "COUNT(customer_id)"}}
        ]
        model["datasets"][1]["metrics"] = [
            {"name": "customers_count", "expression": {"sql": "COUNT(customer_id)"}}
        ]
        model["metrics"] = [
            {
                "name": "ratio",
                "expression": {
                    "sql": "AGG(orders.orders_count) / AGG(customers.customers_count)"
                },
            }
        ]

        result = self.compile(model, Query(metrics=("ratio",)))

        self.assertIn(
            "s0 AS (SELECT COUNT(t0.\"customer_id\") AS \"value\", 1 AS \"present\"\nFROM "
            "\"default_catalog\".\"sales\".\"orders\" AS t0)",
            result.sql,
        )
        self.assertIn(
            "s1 AS (SELECT COUNT(t0.\"customer_id\") AS \"value\", 1 AS \"present\"\nFROM "
            "\"default_catalog\".\"sales\".\"customers\" AS t0)",
            result.sql,
        )
        self.assertIn("SELECT (CAST(CASE WHEN s0.\"present\" IS NULL THEN 0", result.sql)
        self.assertIn("AS DOUBLE) / CASE WHEN s1.", result.sql)
        self.assertIn("FROM s0 CROSS JOIN s1", result.sql)
        self.assertEqual(("orders", "customers"), result.dependencies)

    def test_ossie_03_splits_raw_cross_dataset_aggregates_into_components(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"] = [
            {
                "name": "revenue_per_customer",
                "expression": {
                    "sql": "SUM(orders.amount) / COUNT(customers.customer_id)"
                },
            }
        ]

        result = self.compile(
            model,
            Query(
                metrics=("revenue_per_customer",),
                dimensions=("customers.country",),
            ),
        )

        self.assertIn("SUM(t0.\"amount\") AS \"value\", 1 AS \"present\"", result.sql)
        self.assertIn("COUNT(t0.\"customer_id\") AS \"value\", 1 AS \"present\"", result.sql)
        self.assertIn("FULL OUTER JOIN s1", result.sql)
        self.assertIn(
            "CAST(CASE WHEN s0.\"present\" IS NULL THEN NULL ELSE s0.\"value\" END AS DOUBLE) / CASE WHEN s1.\"present\" IS NULL THEN 0",
            result.sql,
        )

    def test_ossie_03_rejects_aggregate_component_that_reads_two_datasets(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"] = [
            {
                "name": "invalid",
                "expression": {
                    "sql": "SUM(orders.amount + customers.customer_id)"
                },
            }
        ]

        self.assert_error(
            "MULTISTAGE_UNSUPPORTED_EXPRESSION",
            model,
            Query(metrics=("invalid",)),
        )

    def test_ossie_03_plans_keyed_cross_grain_dimension_stages(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["metrics"] = [
            {"name": "orders_count", "expression": {"sql": "COUNT(customer_id)"}}
        ]
        model["datasets"][1]["metrics"] = [
            {"name": "customers_count", "expression": {"sql": "COUNT(customer_id)"}}
        ]
        model["metrics"] = [
            {
                "name": "ratio",
                "expression": {"sql": "AGG(orders.orders_count) / AGG(customers.customers_count)"},
            }
        ]

        result = self.compile(model, Query(metrics=("ratio",), dimensions=("customers.country",)))

        self.assertIn("t1.\"country\" AS \"k0\", COUNT(t0.\"customer_id\") AS \"value\"", result.sql)
        self.assertIn("LEFT JOIN \"default_catalog\".\"sales\".\"customers\" AS t1", result.sql)
        self.assertIn("t0.\"country\" AS \"k0\", COUNT(t0.\"customer_id\") AS \"value\"", result.sql)
        self.assertIn("FULL OUTER JOIN s1 ON", result.sql)
        self.assertIn("COALESCE(s0.\"k0\", s1.\"k0\") AS \"d0\"", result.sql)
        self.assertIn("CASE WHEN s0.\"present\" IS NULL THEN 0", result.sql)
        self.assertIn("CASE WHEN s1.\"present\" IS NULL THEN 0", result.sql)

    def test_ossie_03_pushes_safe_where_into_every_cross_grain_stage(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["metrics"] = [
            {"name": "orders_count", "expression": {"sql": "COUNT(customer_id)"}}
        ]
        model["datasets"][1]["metrics"] = [
            {"name": "customers_count", "expression": {"sql": "COUNT(customer_id)"}}
        ]
        model["metrics"] = [
            {
                "name": "ratio",
                "expression": {"sql": "AGG(orders.orders_count) / AGG(customers.customers_count)"},
            }
        ]

        result = self.compile(
            model,
            Query(
                metrics=("ratio",),
                where=(FieldFilter("customers.country", "eq", "TW"),),
            ),
        )

        self.assertEqual(2, result.sql.count("WHERE"))
        self.assertIn("WHERE t1.\"country\" = 'TW'", result.sql)
        self.assertIn("WHERE t0.\"country\" = 'TW'", result.sql)

    def test_ossie_03_cross_grain_where_rejects_fanout_stage(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["metrics"] = [
            {"name": "orders_count", "expression": {"sql": "COUNT(customer_id)"}}
        ]
        model["datasets"][1]["metrics"] = [
            {"name": "customers_count", "expression": {"sql": "COUNT(customer_id)"}}
        ]
        model["metrics"] = [
            {
                "name": "ratio",
                "expression": {"sql": "AGG(orders.orders_count) / AGG(customers.customers_count)"},
            }
        ]

        self.assert_error(
            "FANOUT_JOIN",
            model,
            Query(metrics=("ratio",), where=(FieldFilter("orders.status", "eq", "complete"),)),
        )

    def test_ossie_03_applies_cross_grain_having_after_stage_join(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["metrics"] = [
            {"name": "orders_count", "expression": {"sql": "COUNT(customer_id)"}}
        ]
        model["datasets"][1]["metrics"] = [
            {"name": "customers_count", "expression": {"sql": "COUNT(customer_id)"}}
        ]
        model["metrics"] = [
            {
                "name": "ratio",
                "expression": {"sql": "AGG(orders.orders_count) / AGG(customers.customers_count)"},
            }
        ]

        result = self.compile(
            model,
            Query(
                metrics=("ratio",),
                dimensions=("customers.country",),
                having=(MetricFilter("ratio", "gt", 1),),
            ),
        )

        self.assertIn("WHERE (CAST(CASE WHEN s0.\"present\" IS NULL THEN 0", result.sql)
        self.assertIn("AS DOUBLE) / CASE WHEN s1.\"present\" IS NULL THEN 0 ELSE s1.\"value\" END) > 1", result.sql)

    def test_ossie_03_rejects_agg_of_a_field(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"] = [
            {"name": "invalid", "expression": {"sql": "AGG(orders.amount)"}}
        ]

        self.assert_error("UNKNOWN_METRIC", model)


if __name__ == "__main__":
    unittest.main()
