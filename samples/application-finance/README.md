# Daily finance application demo

This synthetic conversion fixture shows **application-level packaging**, repeated
SQL, explicit input bindings, logging and scheduler handoff. It does not certify
native SSIS execution or full application equivalence.

## Prepare two saved migrations

1. Use a dedicated Databricks sandbox catalog with an existing `dbo` schema.
   `DailyLedgerLoad.dtsx` drops/recreates **dbo.CrosslakeDemoLedger** and inserts
   four sample ledger entries. Do not use a catalog holding a real table of that name.
2. Upload/assess/convert **DailyLedgerLoad.dtsx** as one saved migration run and
   **DailyLedgerSummary.dtsx** as another. Give the runs recognizable business labels.
3. Inspect generated code and approve each source in Runs & reviews.
4. For each run, load Lakeflow mappings, set the target sandbox catalog/schema and
   a serverless environment version supported by your workspace. Optionally enable
   shared audit logging. Save mappings. Keep schedules off here if the application
   master will own the schedule.
5. In DailyLedgerSummary, the **Summarize Business Date** task needs explicit
   nonsecret input values: `{"User::BusinessDate":"2026-01-31"}`. This maps the
   source's positional OLE DB input to a widget and a bound Spark SQL argument.
   Runtime values are not interpolated into SQL. Other task input JSON stays `{}`.

Both packages contain the same `SELECT COUNT(*) AS LedgerCnt FROM
 dbo.CrosslakeDemoLedger` query. The application duplicate report should identify
the repeated text and show both locations. Review-only shared-read candidates are
exported; callers are never rewritten automatically.

## Group and review the application

1. Open Application workspace and create **DailyFinanceClose**.
2. Select each saved run in Runs & reviews, return, and click **Add selected saved
   run**. Set business labels `DailyLedgerLoad` and `DailyLedgerSummary`.
3. Add the reviewed success dependency **DailyLedgerLoad → DailyLedgerSummary**.
4. Set a naming prefix such as `saxon` and environment label such as `sit`.
   These affect exported Job/pipeline display names, not physical SQL object names.
5. Use the architecture register for source-to-target decisions, for example:
   `[{"run_id":"<load-run-id>","source":"DailyLedgerLoad SSIS package",
   "target":"Lakeflow notebook Job","decision":"replace","notes":"Reviewed sandbox create/load/count behavior"}]`.
6. Optional scheduler import (replace the run ID):
   `[{"run_id":"<load-run-id>","source_scheduler":"manual",
   "source_reference":"Synthetic daily finance close demo",
   "quartz_cron_expression":"0 0 2 * * ?","timezone_id":"America/Chicago",
   "reviewed":false}]`.
   The importer always resets entries to unreviewed. After checking the source
   calendar, DST, dependencies and translation, explicitly set `reviewed:true` in
   the scheduler register. It exports a **paused** application master schedule.
7. Record a release review note, check the application review box and save.
   Confirm code/metadata inclusion, then download the application handoff.

Expected ready export: **review_required**, two child Jobs and one master Job,
with the reviewed success dependency. A blocked export contains reports and
planning material but no executable Job/pipeline resources.

## Validate and execute explicitly

Read the exported README and application-report.html. Review
resources/application.json, runtime configuration, parameters and all target
object names. Configure Databricks CLI authentication. Run
`databricks bundle validate -t dev` before deployment. Review gates remain false;
enable each reviewed notebook's `migration_reviewed` Job parameter and any pipeline
review configuration only after code review and before sandbox execution.
Deploy explicitly with `databricks bundle deploy -t dev`, then start the master Job
from Databricks. The application handoff exporter does not perform live deployment.

The existing per-run Lakeflow live panel still deploys/runs individual migration
Jobs and can link golden suites. For an application master deployed with CLI,
inspect the master/child runs in Databricks and compare outputs with saved golden
tests in Crosslake. Automated application-wide release promotion is not provided.

Expected outputs:

* `golden-ledger-count.json`: `LedgerCnt = 4`.
* `golden-summary.json`: BusinessDate `2026-01-31`, LedgerCnt `4`, TotalAmount `100.00`.
* Shared audit events: started/completed/failed, application ID, package/task, run
  ID, correlation ID and duration. No SQL, parameter values or credentials.

## Optional procedure assessment

`OptionalProcedure.sql` is a separate architecture/assessment asset. It is **not**
automatically called by these packages. Adding a SQL/procedure-only run to an
application blocks executable release until explicit orchestration is supplied.
Likewise, SSIS stored procedure calls, output-variable consumers, non-Input or
sensitive bindings, complex scopes and dynamic expressions require reviewed
migration. Show these boundaries rather than claiming automatic conversion.
