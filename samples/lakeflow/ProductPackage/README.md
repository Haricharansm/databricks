# ProductPackage: Lakeflow Jobs and SSIS single-row outputs

This synthetic sample creates a product table, inserts four rows and assigns
`SELECT COUNT(*) AS ProductCnt` to the package Int32 variable `User::ProductCount`.
The variable is output-only; no downstream task reads or mutates it.

## Crosslake demo

1. Update Crosslake, restart `python server.py` (add `--port 8080` if needed), and
   hard-refresh the browser. Preserve your `.data` and `engine` folders.
2. Upload `ProductPackage.dtsx`, assess, then convert with the configured SSIS
   engine. An existing successfully converted run of the same package can be reused.
3. Select that saved run in **Runs & reviews**. Open **Lakeflow studio** and click
   **Load selected run**. Choose `auto_sql` for all three tasks.
4. Choose an existing **sandbox catalog** and `dbo` schema. The SQL explicitly
   references `dbo.Product`: selecting a different default schema does not rewrite
   that two-part name. Have an administrator create the sandbox `dbo` schema and
   grant the required permissions before running; never select a catalog holding
   a real `dbo.Product` table. The first task drops and recreates that table.
5. Save mappings and click **Preview Jobs and pipelines**. Expected:
   **1 Job(s) · 0 pipeline(s) · review_required** and no package blockers.
   This package contains Execute SQL tasks, so zero declarative pipelines is correct.
6. Show the `Create Product Table → Load Product Data → Count Loaded Products`
   dependencies in the downloaded `resources/lakeflow.json`. Preview shows the
   output mapping `User::ProductCount → ssis_User_ProductCount`, and its producer
   task key. The report preserves this mapping in `task_values`.
7. Download the bundle, review the generated SQL/notebooks and target names, then
   configure the Databricks CLI and run `databricks bundle validate -t dev`.
   After review, set the generated Job tasks' `migration_reviewed` base parameter
   to `true` in `resources/lakeflow.json`, deploy explicitly with
   `databricks bundle deploy -t dev`, and manually run the Job in the sandbox.
   Leave its schedule paused until validation succeeds. Job/serverless availability
   and Unity Catalog permissions depend on the workspace.
8. In the **Count Loaded Products** task's run details, inspect task value
   `ssis_User_ProductCount`; expected **4**. Notebook output also prints the value.
   Verify with `SELECT COUNT(*) FROM <sandbox_catalog>.dbo.Product`.

The generated count code executes one SELECT, takes at most two rows, requires
exactly one row, resolves a named result column (or a zero-based ordinal), and
checks the integer type and declared Int32/Int64 range before publishing values.
NULL, missing/ambiguous columns, multiple rows and overflow fail the task.

Generated resources retain review gates and paused schedules. Local automated
checks verify generation and simulated task-value execution, not live Databricks
execution. SSIS initial variable values are not used because these outputs are
written exactly once and have no other references. SQL parameter bindings,
variable readers, expressions, multiple writers, noninteger types and nested
variable scope require explicit migration and remain blocked.
