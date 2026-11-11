# Read-only SSIS sales smoke test

`ReadOnlySalesSmokeTest.dtsx` is a small SSIS **control-flow conversion test** with three Execute SQL tasks and two success-precedence constraints. It uses five inline synthetic rows and has no REST/custom/script components, secrets, source-table dependencies, table writes, or file destinations. It does not exercise data-flow transformations or SharePoint authentication.

## Download and test in Crosslake

1. Download the repository ZIP and extract it. Find this folder under `samples/ssis/ReadOnlySalesSmokeTest/`.
2. In your app folder, run `python setup_ssis.py` if necessary, then `python server.py`.
3. Confirm **SSIS ready**. Choose **New run from these sources** or start a new batch; remove previously selected files.
4. Upload **only `ReadOnlySalesSmokeTest.dtsx`**. Select **Assess readiness**, then **Convert sources**.
5. Expect three Execute SQL tasks, two success-precedence constraints, a generated Python notebook, a workflow template, and a migration plan. No `%DF%` placeholder or unsupported REST component should appear.
6. Download the generated artifact bundle. The current **Deploy to Databricks** section accepts SQL files only, so import the generated `.py` notebook manually using your Databricks workspace's Import action.
7. Review the notebook and set its `migration_reviewed` widget to `true` before rerunning. Workflow notebook paths still require configuration; no job is deployed automatically.

Source connection findings are expected because the package includes an example OLE DB connection. The converted queries use inline values and need no SQL Server connection or existing tables. This package is a migration fixture; original SSIS execution/import was not validated in SSDT or SQL Server. To run the original package in SSIS, configure `SampleSqlServer` to a SQL Server you can access with the installed `MSOLEDBSQL` provider. It currently uses Windows integrated authentication to localhost/tempdb and contains no password.

## Tasks and expected results

1. **Preview_Source_Rows**: returns five rows, including one inactive row and one active row with a NULL amount.
2. **Aggregate_Active_Sales**: filters `IsActive = 1`, groups by region, and returns:

| Region | ActiveRowCount | NonNullAmountCount | TotalAmount |
| --- | ---: | ---: | ---: |
| East | 3 | 2 | 30.50 |
| West | 1 | 1 | 7.25 |

3. **Verify_Active_Totals**: returns one row: `ActiveRowCount=4`, `NonNullAmountCount=3`, `TotalAmount=37.75`.

The original SSIS tasks use ResultSetType_None, so they do not persist returned rows. Run `source_queries.sql` in a SQL Server query editor to collect source-side results for comparison. Three `expected_*.csv` files contain expected rowsets; `\N` denotes NULL in the source-row fixture. Counts include the active row whose amount is NULL; SUM and COUNT(Amount) exclude its NULL amount.

The stock BladeBridge notebook assigns each SQL query to a Python string and calls `spark.sql`. To display the generated task results explicitly, add a cell:

```python
display(spark.sql(Package_Preview_Source_Rows))
display(spark.sql(Package_Aggregate_Active_Sales))
display(spark.sql(Package_Verify_Active_Totals))
```

Those names were checked against BladeBridge 0.3.0 output. If a different engine version changes them, use the corresponding string-variable names from your generated notebook.

## Reference notebook

`reference_databricks_notebook.py` is a separate, clean companion that executes the same three inline queries, displays results, and asserts the expected totals. Import it into a Python notebook on Databricks, review it, set `migration_reviewed=true`, and run it to test the fixture independently of converter-generated boilerplate. It imports only Python's Decimal class and uses Databricks-provided `spark`, `display`, and `dbutils`.

The reference notebook is not a substitute for checking your generated artifact. Stock converter output can contain unused environment-dependent imports such as `from curses import meta`; if such an import fails, remove the unused import after review or run the reference companion to isolate that boilerplate issue from query behavior.

## What was verified

- DTSX XML parsing, three-task inventory, connection reference, and two success-precedence constraints.
- Real conversion with the official BladeBridge 0.3.0 engine through the accelerator.
- Generated Python syntax, absence of unsupported placeholders, and presence/order of all three task queries.
- Expected counts/totals calculated independently with Python Decimal arithmetic.
- Reference notebook Python syntax and assertions against synthetic result objects.

No SQL Server, SSDT, or live Databricks execution was performed here. A successful syntax/conversion check does not establish live execution equivalence. Use a disposable Databricks notebook for the first run. The sample reads inline data only and does not alter tables or files.
