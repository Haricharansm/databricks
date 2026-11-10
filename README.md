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
```

Tests cover SQL masking/line references, procedure metadata, SSIS inventory/constraints, unsafe XML/ISPAC inputs, output guards/paths, partial conversion, CLI behavior, LSP edits, HTTP routing/origin checks, and ZIP exports. Real Morpheus/BladeBridge integration tests skip when their engines are not installed. Browser testing on Linux exercised mixed-source assessment, actual engine conversion, artifact selection, ZIP download, and mobile layout. Databricks execution validation has not been performed.

## Runtime and limits

This remains a local pilot app; the GitHub repository is a downloadable source distribution, not a hosted web service. The server binds to `127.0.0.1`. Team hosting requires a Python/Java-capable server behind authenticated TLS hosting; the included Python server does not provide user accounts or durable storage.

- Up to 50 expanded sources and 4 MB source text; ISPAC archives also cap total expanded contents at 4 MB / 200 entries.
- One engine conversion at a time per process. Each engine batch/package has a 180-second limit; Morpheus uses a 768 MB JVM heap.
- Maximum combined SSIS/mixed output: 200 artifacts / 16 MB. Input filenames are unique, flat, and limited to letters, digits, spaces, underscores, dots, parentheses, and hyphens.
- Sources stay in browser memory and are sent to the app server for processing. Temporary conversion files are removed after processing. Download results before refreshing.
- The app never executes uploaded SQL or generated notebook code; it invokes trusted engine programs to generate code.

## Upstream sources

- https://github.com/databrickslabs/lakebridge
- https://databrickslabs.github.io/lakebridge/docs/transpile/pluggable_transpilers/morpheus
- https://databrickslabs.github.io/lakebridge/docs/transpile/source_systems/ssis
- Morpheus artifact: `com.databricks.labs:databricks-morph-plugin:0.10.0`, SHA-256 `befab886308f7ccdc4775c9045f584209b6bd993a7eb2a65206be24edd5732d3`.
- BladeBridge package: `databricks-bb-plugin==0.3.0`.

Third-party engines download separately and remain subject to upstream licenses and notices. This is an independent accelerator, not an official Databricks product.
