# Databricks notebook source
# Reference companion for ReadOnlySalesSmokeTest.dtsx.
# Only inline synthetic rows are queried. No source connection or tables are used.
dbutils.widgets.dropdown("migration_reviewed", "false", ["false", "true"])
if dbutils.widgets.get("migration_reviewed") != "true":
    raise RuntimeError("Review this synthetic sample, then set migration_reviewed=true and rerun.")

from decimal import Decimal

# COMMAND ----------
preview_source_rows_sql = "SELECT SaleId, Region, Amount, IsActive\nFROM (VALUES\n    (1, 'East', CAST(10.00 AS DECIMAL(12,2)), 1),\n    (2, 'East', CAST(20.50 AS DECIMAL(12,2)), 1),\n    (3, 'West', CAST(7.25 AS DECIMAL(12,2)), 1),\n    (4, 'West', CAST(99.00 AS DECIMAL(12,2)), 0),\n    (5, 'East', CAST(NULL AS DECIMAL(12,2)), 1)\n) AS sample(SaleId, Region, Amount, IsActive)\nORDER BY SaleId;"
preview_source_rows = spark.sql(preview_source_rows_sql)
display(preview_source_rows)

# COMMAND ----------
aggregate_active_sales_sql = "SELECT Region, COUNT(*) AS ActiveRowCount, COUNT(Amount) AS NonNullAmountCount, SUM(Amount) AS TotalAmount\nFROM (VALUES\n    (1, 'East', CAST(10.00 AS DECIMAL(12,2)), 1),\n    (2, 'East', CAST(20.50 AS DECIMAL(12,2)), 1),\n    (3, 'West', CAST(7.25 AS DECIMAL(12,2)), 1),\n    (4, 'West', CAST(99.00 AS DECIMAL(12,2)), 0),\n    (5, 'East', CAST(NULL AS DECIMAL(12,2)), 1)\n) AS sample(SaleId, Region, Amount, IsActive)\nWHERE IsActive = 1\nGROUP BY Region\nORDER BY Region;"
aggregate_active_sales = spark.sql(aggregate_active_sales_sql)
display(aggregate_active_sales)

# COMMAND ----------
verify_active_totals_sql = "SELECT COUNT(*) AS ActiveRowCount, COUNT(Amount) AS NonNullAmountCount, SUM(Amount) AS TotalAmount\nFROM (VALUES\n    (1, 'East', CAST(10.00 AS DECIMAL(12,2)), 1),\n    (2, 'East', CAST(20.50 AS DECIMAL(12,2)), 1),\n    (3, 'West', CAST(7.25 AS DECIMAL(12,2)), 1),\n    (4, 'West', CAST(99.00 AS DECIMAL(12,2)), 0),\n    (5, 'East', CAST(NULL AS DECIMAL(12,2)), 1)\n) AS sample(SaleId, Region, Amount, IsActive)\nWHERE IsActive = 1;"
verify_active_totals = spark.sql(verify_active_totals_sql)
display(verify_active_totals)

# COMMAND ----------
assert preview_source_rows.count() == 5, "Expected exactly five source rows."
actual_regions = [row.asDict() for row in aggregate_active_sales.collect()]
expected_regions = [
    {"Region": "East", "ActiveRowCount": 3, "NonNullAmountCount": 2, "TotalAmount": Decimal("30.50")},
    {"Region": "West", "ActiveRowCount": 1, "NonNullAmountCount": 1, "TotalAmount": Decimal("7.25")},
]
assert actual_regions == expected_regions, f"Region totals differ: {actual_regions!r}"
actual_totals = verify_active_totals.first().asDict()
assert actual_totals == {"ActiveRowCount": 4, "NonNullAmountCount": 3, "TotalAmount": Decimal("37.75")}, f"Totals differ: {actual_totals!r}"
print("PASS: five inline rows; filtering, NULL handling, and region/overall totals matched.")
