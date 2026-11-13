# Assessment Studio sample

Upload `schema.sql`, `workload_queries.sql`, `procedure.sql`, and `caller.sql` into a saved Crosslake migration run, then assess it. Do not upload `query_store_export.sql` as a migration source: it is a separate source-side telemetry export template.

In **Assessment studio**, load that saved run. Highlights show source-derived metrics; Analysis shows SQL units and entity identities. The `dbo.ExternalOrders` reference is **reference-only** because its definition was not uploaded, not proof that the real table is missing. Scope can start from `caller.sql`; procedure-call and object references pull in required uploaded files as static planning groups. Not reached never means unused.

Import `workload.csv` in the Workload tab with evidence `Synthetic demonstration only; no database execution`. It contains four invented interval records, not customer data. Expected imported totals: 85 executions, 3,200 ms duration, 1,600 ms CPU, 1,560 logical reads. It matches three uploaded query texts, including a leading-wildcard optimization candidate. CPU/I/O shares describe these imported rows only. Duplicate SQL inside the procedure is kept as part of its compound unit and is not silently attributed to that file.

Download reports for HTML (open locally, then print to PDF), JSON and CSV inventories. No SQL, cloud resource or migration job is executed by loading insights, selecting scope or importing workload.

For real telemetry, review `query_store_export.sql`, run it yourself in the source SQL Server database, and export its result as a quoted CSV with headers. Query Store must be enabled and permissions depend on SQL Server version (for example VIEW DATABASE STATE / VIEW DATABASE PERFORMANCE STATE). The template is not executed or verified against a live source in this repository. It limits output to 5,000 rows and complete intervals; record its actual observed window and incompleteness. Re-export/overlap can duplicate counts, so replace evidence with one non-overlapping prepared dataset.
