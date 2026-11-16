# Crosslake golden validation / sandbox CI package

This package contains source/target read queries and the golden data you explicitly included. Use a private approved repository for customer data. No data is committed or deployed by exporting the package.

## Before running

1. Review `crosslake-suite.json`, dataset contents, fixture provenance and source/target queries. Prepare aligned, controlled input fixtures in both systems yourself; Crosslake records the snapshot reference but does not provision or verify input snapshots.
2. Commit the package into an approved artifact repository. Preserve baseline and candidate hashes. Changing golden files invalidates their hashes; update them through a reviewed export rather than hiding a regression by editing expected results in CI.
3. Offline: `python ci_runner.py crosslake-suite.json --mode fixtures`. This requires an explicitly exported recorded target fixture for every case. It tests supplied result comparisons only, not Databricks execution.
4. Live target: configure Databricks CLI unified auth plus `DATABRICKS_HOST` and `DATABRICKS_WAREHOUSE_ID`; optionally `DATABRICKS_CATALOG` and `DATABRICKS_SCHEMA`. Then run `python ci_runner.py crosslake-suite.json --mode databricks --allow-compute`. A CLI-authenticated identity or `DATABRICKS_TOKEN` is used in memory and never written to this package.

## GitHub Actions

The workflow is manual (`workflow_dispatch`); it does not execute customer queries on pull requests. Create a protected GitHub environment named `crosslake-dev`, with required reviewers and deployment-branch restrictions. Configure environment variables DATABRICKS_HOST, DATABRICKS_CLIENT_ID and DATABRICKS_WAREHOUSE_ID, plus optional catalog/schema. Configure Databricks workload identity federation for the repository/environment's GitHub OIDC subject. No personal token is embedded. Workflow actions and CLI versions are pinned at generation time; review/update those pins under your organization's dependency policy.

Choose fixture replay only when recorded target fixtures were included. Live validation queries use warehouse compute. The workflow uploads JSON and JUnit evidence for seven days; raw mismatch values are omitted by default. Schema identifiers, test titles and remote run IDs can still appear in evidence.

Optional sandbox deployment requires an included `migration/` bundle and a `job_resource` key from its resources. Candidate files and golden hashes are checked *before* workspace actions. The workflow validates/deploys dev, resolves that bundle's job ID, starts it, waits for success and compares outputs. Source SQL scripts with no reviewed job wrapper are not automatically assigned execution order. There is no production promotion stage. Review workspace notebook immutability and job settings; a passing query suite is not whole-migration equivalence.

Generated notebook gates remain false unless the export explicitly enabled reviewed sandbox notebooks. That option requires approval of all converted sources in the app. Review flags are not automated semantic certification. Never bypass gates just to obtain a green test.

## Optional SQL Server source / SSIS job execution

Run from a trusted app server or self-hosted runner with network access to SQL Server. Install `pyodbc` and Microsoft ODBC Driver 18. Set `CROSSLAKE_SQLSERVER_CONNECTION_STRING` in the environment/secret store (use encryption and an approved identity); never put it in the manifest. Set `CROSSLAKE_SQLSERVER_ALLOWED_JOBS` to a JSON array of exact SQL Agent job names that administrators allow to run. Empty list means no source job may be started.

`python ci_runner.py crosslake-suite.json --mode sqlserver --allow-compute --allow-source-queries`

To start existing, reviewed jobs as part of the test, add `--source-job NAME --allow-source-job` and/or `--target-job ID --allow-target-job`. A source SSIS package can be wrapped by its existing SQL Agent job. The job must already contain the required input/parameter/environment configuration. The runner does not upload packages, alter SQL Agent jobs, copy source data, or infer schedules.

The sequence is source job -> source queries/baseline checks -> target job -> target queries. Both target-to-golden and source-to-target comparisons must pass; source drift stops the target job from starting. This is controlled source-first execution, not continuous dual running of production workloads. Use fixture snapshots and approved job configurations to avoid unrelated updates during a test.

SQL Agent permissions, cancellation and job history retention must be validated with your source environment. Timeouts/cancellation request stops where the remote API permits; inspect actual remote state after network failure or app/CI restart. SQL Server timestamps without offsets are not assigned a guessed timezone; cast timestamp outputs explicitly when required by a test definition.

## Scope and evidence

Results are bounded to 10,000 rows / 2 MB per dataset; target results must fit one inline chunk. Partial/truncated results cannot pass. Queries must be single fully bound SELECT/WITH statements and case parameters must be empty. Only the declared queries and jobs are checked; output parameters, transaction rollback, Power BI measures, full-estate scaling and fixture-seeding automation need additional implementation.

JSON and JUnit reports are emitted to `suite-results/`. Exit 0 means this declared suite passed; exit 1 means comparisons/execution failed; exit 2 means configuration/loading failed. `--check-only` checks manifest/baseline/candidate hashes without execution. It is not migration approval. Neither a pass nor an artifact export automatically approves production deployment.
