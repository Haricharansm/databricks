# Crosslake — SQL Server and SSIS migration accelerator

A local web app for assessing legacy SQL, stored procedures, and SSIS packages, then generating Databricks SQL, Python/PySpark notebooks, migration plans, and draft workflow definitions with Lakebridge engines.

[Download the latest source ZIP](https://github.com/Haricharansm/databricks/archive/refs/heads/main.zip), extract it, and open a terminal in `databricks-main`. You do not need access to the remote workspace where this was built.

## Install and run

Requirements: Python 3.10+; Java 21+ for SQL/stored-procedure conversion; internet access during engine installation. Windows, macOS, and Linux are supported by the upstream engines, but this version was tested on Linux.

```bash
python setup_engine.py
python setup_ssis.py
python server.py
```

Open http://127.0.0.1:8000 **on your computer**. On Windows, use `py` in place of `python` if needed. SQL-only users can omit `setup_ssis.py`; SSIS-only users can omit `setup_engine.py` and Java.

- `setup_engine.py` downloads and SHA-256-verifies the pinned official Morpheus 0.10.0 JAR.
- `setup_ssis.py` installs official `databricks-bb-plugin==0.3.0` and its dependencies in `engine/bladebridge`, a project-local virtual environment.
- Neither standalone conversion route requires Databricks workspace credentials. Generated code is not run against your databases or workspace by this app.

## Use the app

1. Upload `.sql`, `.dtsx`, or `.ispac` files. Mixed batches are supported. Use the SQL and SSIS sample buttons to try it first.
2. Select **Assess readiness** and expand results. Review procedure parameters and unresolved textual references, or SSIS tasks, data-flow components, connections, parameters, and precedence constraints.
3. Select **Convert sources**. SQL/procedures use Morpheus; SSIS packages use experimental BladeBridge notebook conversion.
4. Select artifacts in the output dropdown. Inspect generated code, migration plans, diagnostics, and coverage. A partially converted batch explicitly lists blocked sources.
5. Select **Download bundle**. ZIP paths are preserved so notebook locations and workflow templates can be matched.

Assessment is available without installing either conversion engine. The UI displays engine readiness separately. In a mixed batch, an available engine can convert its sources while the coverage report records sources blocked by the other engine.

## Databricks settings and connection tests

The **Databricks settings** section configures workspace access and SQL warehouse defaults. It configures workspace access and runs connection probes. Converted SQL is executed only from the separate Deploy to Databricks section; notebooks and jobs are not deployed.

### Configure your workspace

1. Start the app and open **Databricks settings** in its sidebar.
2. Enter the base workspace URL from your Databricks browser session, such as `https://dbc-<workspace>.cloud.databricks.com`. Remove editor paths, `?o=...` query parameters, and fragments. An account-console URL is not a workspace URL.
3. Select one authentication method below. Enter credentials only in your local app or server environment, never in chat, source files, or GitHub.
4. Select **Save settings**, then **Test workspace connection**. This reads the current workspace identity through the workspace SCIM `Me` API and does not start a warehouse or run SQL.
5. Select **Load SQL warehouses**. Choose a visible warehouse or paste its ID, then save settings again. Listing warehouses does not start compute. If copying an HTTP path such as `/sql/1.0/warehouses/<id>`, enter only the final warehouse ID.
6. Optionally set a catalog and schema. Blank values use the workspace/warehouse defaults; these fields do not create objects or rewrite converted code.
7. To verify SQL access, explicitly check the compute-consent box and select **Run fixed SELECT 1 test**. This may start a stopped warehouse and use compute. The only submitted statement is `SELECT 1 AS connection_test`; uploaded SQL and validation-case query text are never submitted.

The warehouse test requests a 10-second inline wait with cancellation on timeout, a one-row/1 KB result bound, and verifies that the returned scalar equals 1. Pending tests receive a best-effort cancellation request when a statement ID is available. Canceled/pending tests are labeled not verified; cold warehouses may need warm-up before retrying. Check query history if cancellation or verification is incomplete. Workspace authentication, warehouse permission, and migrated-query correctness are separate checks.

Databricks Free Edition support depends on the authentication methods, SQL warehouse access, and APIs enabled for your account. If personal access tokens are disabled, use a supported CLI OAuth login. No live workspace account was available during development; API behavior was tested with mocked Databricks responses, not a real account.

### Authentication choices

| Method | Configuration and lifetime |
| --- | --- |
| Personal access token | Enter in the password field. It is sent to the local app server, held in memory, and never returned in settings responses or written to SQLite. The input clears after saving. Re-enter after restarting the server. A blank field preserves an existing token only for the same workspace and authentication mode. |
| Existing CLI OAuth login | Install the official Databricks CLI on the machine running this app and run `databricks auth login --host <workspace-url>` there. An optional profile can be selected in the UI. The app invokes `databricks auth token --host ... [--profile ...]` to obtain a token for a test; it does not start an interactive login or persist token output. CLI availability is not proof that a login is valid. |
| Server environment token | Set `DATABRICKS_HOST` and `DATABRICKS_TOKEN` in the app server process environment before starting it. The saved host must match the environment host before that token can be used. The token value is not displayed or copied into the database; the workspace URL is non-secret metadata and is saved normally. The app does not automatically load `.env` files. |

PAT creation and CLI OAuth availability depend on workspace policy and your permissions. Use the least-privileged account that can access the workspace and intended SQL warehouse. A valid credential can still receive a permission error when listing or using warehouses.

Only workspace URL, authentication-mode/profile names, warehouse ID, optional catalog/schema, settings revision, and sanitized test results are stored in the existing local database. Identity names in successful test results are non-secret metadata but can contain personal information. Tokens are not placed in browser storage, query strings, reports, source ZIPs, or GitHub updates. Changing the saved workspace or authentication mode clears the old session token. **Clear session credential** removes it and selects session-token mode; it does not revoke tokens or erase credentials managed by your environment or Databricks CLI.

Settings saves reset cached test outcomes. Displayed test outcomes are historical observations with timestamps, not a guarantee that a credential is still valid. After a restart, a historical successful test can remain visible while the current session has no token. Version checks reject stale edits and tests when settings change concurrently.

### Network and local hosting boundary

Requests use HTTPS with normal TLS verification and inherited proxy/CA settings. Supported workspace suffixes are `cloud.databricks.com`, `azuredatabricks.net`, and `gcp.databricks.com`; arbitrary hosts, IP addresses, custom ports, URL credentials, account-console URLs, and redirect targets are rejected. Government/custom workspace domains require a reviewed allowlist extension. Network, VPN, and private-DNS access still need to work from the app server.

The app's API accepts localhost/loopback Host headers for its listening port by default to reduce DNS-rebinding exposure. For an authenticated TLS reverse proxy, `ACCELERATOR_ALLOWED_HOSTS` can add comma-separated, exact Host-header values, such as `migration.example.com`; include a port when the browser Host header includes one. This setting is not an authentication mechanism. The included local app has no user accounts or per-user credential isolation: run it on a trusted computer. Shared deployment requires an authenticated gateway, TLS, appropriate access controls, and a reviewed credential model before enabling it for a team.

### Settings API

- `GET /api/databricks/settings`: non-secret settings, credential-presence indicators, and last test outcomes.
- `POST /api/databricks/settings`: save metadata and optional session `token`, using the current `revision`.
- `POST /api/databricks/test-workspace`: verify current identity using the saved `revision`.
- `POST /api/databricks/warehouses`: list visible warehouse metadata using the saved `revision`.
- `POST /api/databricks/test-warehouse`: fixed SQL probe; requires current `revision` and `allow_compute: true`.
- `POST /api/databricks/forget-credential`: clear the in-memory credential using the current `revision`.

All action responses are sanitized. Authentication failures, permission denials, unsupported APIs, network problems, and incomplete SQL probes are surfaced without returning raw upstream error bodies or CLI output. No token value is returned by any endpoint.

## Automatic fixes

Conversion and deployment repair common problems automatically and list every change, so you do not edit scripts by hand. Your uploaded sources are never modified.

- **Before conversion** (SQL Server script): adds the comma SQL Server lets you omit before a table-level `PRIMARY KEY`/`CONSTRAINT`/`FOREIGN KEY`; fixes a `PRIMARY KEY`/`UNIQUE` that names a column the table does not have when one existing column clearly matches (for example `Pk_RoleTypeID` → `RoleTypeID`), and otherwise removes that key (Databricks keys are informational).
- **After conversion** (Databricks SQL): removes bare `NULL` column constraints from `CREATE TABLE`, which Databricks rejects.
- **At deploy**: handles tables and views that already exist, so deployments can be re-run. **Skip** (default) adds `IF NOT EXISTS` and keeps the existing object and its data; **Replace** uses `CREATE OR REPLACE`, which rebuilds tables and deletes their data; **Stop with an error** keeps the original statement.
- **At deploy**: creates each schema the SQL writes to (for example `dbo`) with `CREATE SCHEMA IF NOT EXISTS` before the objects. Untick the option to skip this.

Changes appear under **Engine diagnostics & errors** ("Automatic fixes applied") and as notes in deploy Preview. Problems these rules do not cover still need a manual fix; paste the error to get help.

## Deploy converted SQL to Databricks

The **Deploy to Databricks** section runs a converted run's `.sql` files on the SQL warehouse saved in Databricks settings, using the saved default catalog and schema.

1. Configure Databricks settings: workspace URL, credential, SQL warehouse, catalog and schema. Save, then test the workspace connection.
2. Open a converted run in the Migration monitor. Its converted `.sql` files appear in the Deploy section, all selected.
3. Select **Preview statements** to see each statement that will run, in order. Nothing is sent to Databricks.
4. Tick the consent box and select **Deploy to Databricks**. Statements run one at a time through the SQL Statement Execution API. By default deployment stops at the first failed statement and shows the Databricks error message; choose **Continue with the remaining statements** to run everything and list all failures. **Cancel deployment** cancels the running statement and stops.

Before running, bare SQL Server style `NULL` column constraints (for example `col INT NULL`) are removed from `CREATE TABLE` column lists, because Databricks accepts only `NOT NULL` and columns are nullable by default. `NOT NULL`, `DEFAULT NULL`, strings and CHECK clauses are untouched, and Preview notes each adjusted statement. Files are split on top-level semicolons; semicolons inside strings, comments, `$$` bodies and `BEGIN … END` blocks do not split. Each statement may run for up to 15 minutes. Notebooks, workflow templates, plans and reports are never executed. Deployment progress is kept in memory until the server restarts, and start/finish summaries are added to the run timeline. Fully qualified names in the SQL override the default catalog and schema.

## Migration monitoring dashboard

The monitor saves runs, source code, generated artifacts, assessment results, engine diagnostics, per-source review notes, and manual validation evidence in `.data/migration.sqlite3` on the computer running the server. This changes the earlier browser-only behavior: run data now persists until you delete it. The database and engine runtime files are ignored by Git and are never included in source downloads.

- **Assess readiness** creates a saved run and queues its assessment. **Convert sources** starts background conversion for that assessed run. One worker processes sources sequentially, recording each source as queued, converting, converted, or blocked.
- The page polls saved progress and restores the selected run after refresh. Closing a tab does not stop server-side work. Run history can be searched, filtered, paginated, and reopened to restore reports and downloadable artifacts.
- Dashboard counts summarize all saved runs/source attempts, not deduplicated database objects. Run timestamps display in your browser's local timezone. Durations refer to the current/last phase (assessment or conversion).
- Open a source's **Review notes & manual validation** panel to record pending/in-review/approved status, notes, and a manual validation result. Passed or failed validation requires evidence. A source must be converted and its review approved before you can mark it passed. Blocked sources cannot be approved or marked passed.
- These validation results are **manual attestations**, not automatic Databricks checks. The migration monitor is not connected to Databricks job execution; workspace connection probes are configured separately in Databricks settings. Saving a passed result does not remove SSIS notebook review gates, enable workflows, or set engine-generated `validated_on_databricks` to true.
- Review changes have an event history and version checks to reject stale edits from another tab. This single local workspace has no authenticated reviewer identities or tamper-proof audit guarantees.
- Use **New run from these sources** to create a separate attempt. Completed/failed runs are not silently overwritten or automatically retried. A server restart marks unfinished runs interrupted instead of claiming completion or replaying conversion.
- **Delete saved run** removes a finished run's sources, outputs, and review records from the app database. Active runs cannot be deleted. This is not a secure-erasure guarantee for disk backups or previously exported files.
- Exported readiness reports and bundles include the selected run's monitoring metadata and review history.

Keep the database private: it can contain proprietary SQL, package contents, connection values carried through engine output, and notes. It is not encrypted by this app. To back it up, stop the server and copy `.data/migration.sqlite3`. `MIGRATION_DB_PATH` can select a different local database file. Run one server process per database; multi-process scheduling is not supported. Graceful server shutdown waits for queued work; force-stopped runs are recovered as interrupted on the next startup.

### Monitoring API

- `POST /api/runs` with the existing files payload creates a persistent assessment run (`202`).
- `GET /api/runs` lists runs; `q`, `status`, `limit` (up to 50), and `offset` filter/page results. Global dashboard statistics accompany the list.
- `GET /api/runs/<id>` restores sources, assessment, artifacts, and history. Add `?summary=1` to poll progress without transferring source/artifact bodies.
- `POST /api/runs/<id>/convert` queues conversion of an assessed run (`202`).
- `POST /api/runs/<id>/review` records a source's review/manual result with its current version.
- `POST /api/runs/<id>/delete` deletes a non-active run.

Existing synchronous `/api/assess`, `/api/convert`, and `/api/export` endpoints remain for compatibility; direct synchronous calls do not create monitored runs. The web interface uses the persistent run endpoints.

## Validation workbench: SQL and stored procedures

The first validation workbench compares **result exports supplied by you**. It does not connect to SQL Server/Databricks, execute queries, or independently verify export provenance. A passing comparison establishes a match only for the supplied snapshots and declared rules; it is not proof of complete migration correctness. SSIS pipeline validation, procedure return codes/output parameters, multiple result sets, side effects, transaction behavior, and performance validation are not automated in this version.

### Suggested workflow

1. Open a saved migration run in the monitor after its active phase finishes. The workbench offers SQL files and stored procedures from that run.
2. Create a test case: select its source, give it a name and scenario, and optionally record the SQL Server/Databricks queries or procedure calls and a JSON object of parameter values. Query text is metadata only and is never executed.
3. Declare result columns using exact exported names, explicit types, nullability, and matching rules. Save the case before comparing. The **Load demo case** button supplies clearly labeled synthetic fixtures so you can learn the interface without database access.
4. Run both queries manually against aligned, representative fixtures or consistent snapshots. Export their result sets as UTF-8 CSV or JSON. Preserve decimal precision, NULL versus empty-string distinctions, duplicate rows, and any meaningful ordering.
5. Import or paste the SQL Server expected results and Databricks actual results. Record both export/query references plus execution evidence: fixture identity, parameters, export times, workspace/job references, and known limitations. These are manually supplied references, not authenticated execution evidence.
6. Select **Compare uploaded results**. Inspect individual checks, mismatch samples, and the recorded definition/evidence. Download the JSON comparison report.
7. Add cases for normal inputs, no rows, nulls, duplicates, decimal boundaries, date/time boundaries, and unsupported-feature rewrites. Each procedure result set should have a separate case. Review side effects and procedural semantics separately.

### Matching rules

| Mode | Behavior |
| --- | --- |
| Unordered | Exact normalized rows compared as multisets. Row order is ignored; duplicate multiplicity is preserved. Nonzero numeric tolerance is rejected. |
| Ordered | Rows align by position. Use deterministic source/target ordering. Explicit absolute numeric tolerances can be set per column. |
| Keyed | Rows align by declared unique, non-NULL keys. Duplicate/null keys fail the comparison. Keys always compare exactly; non-key numeric columns can have explicit absolute tolerances. |

Column names and field sets must match exactly, including case. Header order can differ because columns map by name. Every row must have the declared fields. Supported declared types are `string`, `integer`, `decimal`, `boolean`, `date`, and `timestamp`. These check exported values against the declared types; they do **not** inspect actual database column metadata.

- Strings remain case-sensitive and whitespace-sensitive. Numeric values are not implicitly converted into strings.
- Numbers use decimal arithmetic, not binary floating-point comparison. Integers must be integral. Numeric text can use standard decimal/scientific notation; non-finite values and excessively large numbers/exponents are rejected.
- CSV uses the explicit NULL token `\N` by default. Empty text remains an empty string. You can deliberately change the token, including selecting an empty token when that matches an export convention; doing so cannot recover empty-string/NULL distinctions already lost in an export.
- JSON uses actual `null`; a string containing a backslash followed by N is not treated as NULL. Duplicate JSON object keys and NaN/Infinity constants are rejected.
- Boolean values are true/false (JSON booleans or true/false text). Dates require `YYYY-MM-DD`. Timestamps require an explicit UTC offset or `Z`, normalize to UTC, and support at most six fractional digits. Naive timestamps and higher precision are rejected rather than silently assigned a timezone or truncated.
- JSON accepts a nonempty array of row objects or an explicit envelope such as `{"columns":["id","amount"],"rows":[]}`. Use the envelope for empty results; plain `[]` cannot establish a schema. CSV always requires headers.

Reports include row-count, schema, declared-type, null-count, key-quality (when applicable), and value checks. Exact duplicate-row counts are reported separately. Up to 20 mismatch samples are shown with a total value-mismatch count. Structural/type failures can stop value comparison; inspect earlier failed checks even when the value mismatch count is zero. `error` means the input could not be compared, not that a query execution failed.

### Definitions, history, and data storage

Each case belongs to one source/run. Saving edits creates a new version; previous comparisons retain their original definition and result. The current summary marks old-version comparisons as superseded rather than treating them as a pass for new rules. Version checks reject stale saves/comparisons from another tab.

Each attempt stores both raw result snapshots, input formats/hashes, the definition snapshot, source/run artifact hashes, manually supplied evidence, timestamp, and result in the existing local database. Reports are immutable through the app API. **Restore recorded inputs** reuses inputs from a historical attempt, while the next comparison applies the currently saved definition. The latest 50 comparisons per case are listed in the UI.

A matched comparison does not change source review, manually recorded validation status, notebook execution gates, `validated_on_databricks`, or deployment readiness. Comparison reports download separately from migration artifact bundles. Their mismatch samples, optional query text, and evidence can contain sensitive information; raw datasets remain in the local database unless you restore/export them yourself. Deleting the owning migration run cascades to its cases and comparison records.

Limits: 100 columns, 10,000 rows and 2 MB per result set; 100 cases and 500 saved attempts per run. No sampling or truncation is used to make a dataset pass. Database disk usage grows with saved snapshots; delete unwanted runs and maintain appropriate local access/backup policies. Existing local databases gain the validation tables automatically on server startup; no additional packages are required.

### Validation API

- `GET /api/runs/<run>/validation`: case list and current-definition comparison summary.
- `POST /api/runs/<run>/validation/cases`: create a case.
- `GET /api/runs/<run>/validation/cases/<case>`: definition and latest 50 attempt summaries.
- `POST /api/runs/<run>/validation/cases/<case>`: save a new definition version using the current `version`.
- `POST /api/runs/<run>/validation/cases/<case>/compare`: compare `expected` and `actual` datasets (`format` and `content`), with current `version`, `expected_ref`, `actual_ref`, and `evidence`.
- `GET /api/runs/<run>/validation/attempts/<attempt>`: immutable report and whether its definition has been superseded. Add `?inputs=1` to restore the recorded raw inputs.

## Inputs and assessment

### SQL and stored procedures

Supply T-SQL in `.sql` files, including `CREATE`, `ALTER`, or `CREATE OR ALTER PROCEDURE`. Morpheus generates Databricks SQL scripting equivalents where supported. The app inventories procedure names and parameter types/modes, flags output parameters, and shows unresolved textual table/procedure references.

The heuristic scanner also flags dynamic SQL, cursors, transactions, temporary tables, table variables, query hints, linked-server constructs, identity columns, MERGE, OUTPUT, exception handling, dialect-specific functions, TOP, collations, and session settings. Comments and literals are masked before scanning. Textual inventory is not a complete grammar or a resolved dependency graph.

The app does not silently translate unsupported procedural behavior into PySpark. Dynamic SQL, complex procedural logic, result sets, transactions, caller bindings, and target-runtime procedure support need review. Inspect Morpheus diagnostics and FIXME comments, then test representative cases.

### SSIS packages and projects

Upload unencrypted `.dtsx` exports, or `.ispac` project archives. Browser file inputs decode UTF-8 and BOM-marked UTF-16 text. ISPAC processing expands DTSX entries in memory, rejects unsafe paths/duplicates/oversized contents, and inventories other project filenames. No archive contents are extracted to disk.

Assessment inventories control-flow tasks, data-flow component types, connection-manager names/types, variable names/scopes, parameter names, precedence constraints, and embedded Execute SQL task text. Unknown components, Script Tasks/Components, nested loops/containers, package calls, conditional/failure branches, unresolved/cyclic edges, parameter bindings, and connection mappings are flagged.

**Project-level parameter values and connection-manager files are not applied by the per-package converter.** They must be mapped separately. Encrypted packages, DTDs/entities, unsupported formats, and deeply nested or oversized inputs are rejected. Remove embedded credentials before uploading packages. The assessment inventory does not include connection strings, but generated engine output can preserve values from source packages: review before sharing or deploying.

This assessment is a heuristic SQL scan and XML inventory, **not Lakebridge Analyzer** or a live database assessment. Complexity is High for high-severity findings, Medium for medium findings or an SSIS package, otherwise Low. No matching rules does not establish readiness. Data volumes, query history, live dependencies, and result equivalence are not assessed.

## SSIS output and workflow review

BladeBridge's supported SSIS target option is `SPARKSQL`. Its output is a Databricks Python source notebook using Spark SQL/PySpark APIs, not a guaranteed all-DataFrame implementation. This app requests that supported target rather than an unsupported `PYSPARK` SSIS flag.

For each package, the bundle includes:

- `notebooks/<package-key>/...`: original engine notebook/artifact paths, with a review gate added to executable notebooks.
- `plans/<package-key>.json`: source inventory, engine findings, review actions, and explicit `deployable: false` state.
- `workflows/<package-key>.job.json`: one package-level job template when the engine produces exactly one notebook entrypoint.
- `migration-coverage.json`: each source's conversion status in a mixed or SSIS batch.
- `report.json`: the assessment plus conversion metadata selected for export.

If the engine produces multiple notebooks, the app does not invent their ordering: it generates the plan and flags that the entrypoint/workflow needs manual construction. Templates are per package, not an inferred cross-package dependency graph. Intra-package ordering remains in engine-generated notebook code and is recorded in the source plan for review.

### Test in Databricks

1. Import generated `.py` source notebooks into your Databricks workspace, preserving their relative folders.
2. Map connections, secrets, catalog/schema names, file locations, variables, and parameter bindings.
3. Inspect **all** notebook cells. Script Task/Component C#/VB.NET logic requires manual rewriting; generated placeholders are not equivalent implementations. Review data-flow semantics, scopes, branch conditions, and transaction handling.
4. Replace `/REVIEW_REQUIRED/...` notebook paths in job templates with actual workspace paths and select compute supported by your workspace. No schedule is generated or enabled.
5. Set the notebook/job parameter `migration_reviewed=true` **only after review**. It defaults to `false`, causing the first notebook cell to stop before the generated body runs.
6. Run against disposable sample data and compare with source behavior: row counts, values, nulls, date precision, types, side effects, and failure/retry behavior.

Databricks Free Edition may be suitable for supported sample workloads; check its current compute, procedure, connection, and workflow capabilities. The app does not automatically deploy jobs, connect to a workspace, or claim Databricks execution validation.

## Alternative existing Lakebridge CLI setup

If `databricks` is installed and `LAKEBRIDGE_TRANSPILER_CONFIG` points to an existing Morpheus config, SQL conversion uses `databricks labs lakebridge transpile --source-dialect mssql --skip-validation true`. Otherwise it uses the local Morpheus JAR. `MORPHEUS_JAR` can override that path. `BLADEBRIDGE_PYTHON` can select an existing Python interpreter with the official BladeBridge package installed; keep the virtualenv executable path rather than resolving its symlink to a base interpreter.

## Verification

```bash
python -m unittest discover -s tests -v
node --check static/app.js
node --check static/monitor.js
node --check static/validation.js
node --check static/databricks.js
```

Tests cover SQL masking/line references, procedure metadata, SSIS inventory/constraints, unsafe XML/ISPAC inputs, output guards/paths, partial conversion, CLI behavior, LSP edits, HTTP routing/origin checks, and ZIP exports. Real Morpheus/BladeBridge integration tests skip when their engines are not installed. Browser testing on Linux exercised mixed-source assessment, actual engine conversion, artifact selection, ZIP download, and mobile layout. Monitoring tests also cover restart recovery, persistent artifacts/reviews, partial failures, evidence requirements, optimistic edit conflicts, pagination, and deletion. Browser testing verified refresh during conversion, restoration of saved reviews/artifacts, and history filtering. Validation-workbench tests cover exact decimal handling, row multiplicity, NULL/empty distinctions, ordered/keyed comparisons, explicit tolerances, timezone rules, invalid inputs, immutable version history, evidence requirements, run scoping, and persistence. Browser testing covered case creation/versioning, matching/mismatching CSV/JSON results, refresh restoration, file import, historical report download, and mobile layout. Databricks-settings tests also cover session/environment/CLI authentication selection, credential non-persistence, host binding, safe URL handling, sanitized failures, fixed-query consent, incomplete-probe cancellation, stale settings, and local API Host checks. Browser tests exercised settings against mocked API responses. Live workspace/warehouse testing and Databricks execution validation have not been performed.

## Runtime and limits

This remains a local pilot app; the GitHub repository is a downloadable source distribution, not a hosted web service. The server binds to `127.0.0.1`. Team hosting requires a Python/Java-capable server behind authenticated TLS hosting; the included Python server does not provide user accounts; durable run data is stored in local SQLite.

- Up to 50 expanded sources and 4 MB source text; ISPAC archives also cap total expanded contents at 4 MB / 200 entries.
- One background worker per server process; up to 20 active/queued runs. One engine conversion at a time per process. Each engine batch/package has a 180-second limit; Morpheus uses a 768 MB JVM heap.
- Maximum combined SSIS/mixed output: 200 artifacts / 16 MB. Input filenames are unique, flat, and limited to letters, digits, spaces, underscores, dots, parentheses, and hyphens.
- Sources and generated results are saved in the local monitoring database. Temporary engine working files are removed after processing. Saved runs can be restored after refresh.
- The app never executes uploaded SQL or generated notebook code; it invokes trusted engine programs to generate code.

## Upstream sources

- https://github.com/databrickslabs/lakebridge
- https://databrickslabs.github.io/lakebridge/docs/transpile/pluggable_transpilers/morpheus
- https://databrickslabs.github.io/lakebridge/docs/transpile/source_systems/ssis
- Morpheus artifact: `com.databricks.labs:databricks-morph-plugin:0.10.0`, SHA-256 `befab886308f7ccdc4775c9045f584209b6bd993a7eb2a65206be24edd5732d3`.
- BladeBridge package: `databricks-bb-plugin==0.3.0`.

Third-party engines download separately and remain subject to upstream licenses and notices. This is an independent accelerator, not an official Databricks product.
