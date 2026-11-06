# Crosslake — SQL Server migration accelerator

A runnable web app for uploading SQL Server scripts, assessing migration risks, and converting SQL to Databricks SQL with the official Lakebridge Morpheus engine.

## Download and start

[Download the source ZIP](https://github.com/Haricharansm/databricks/archive/refs/heads/main.zip), extract it, and open a terminal in the extracted `databricks-main` folder. This repository contains a runnable local app; downloading it does not require access to the remote workspace used to build it.

Requirements: Python 3.10+ and Java 21+. No Python packages or Databricks workspace credentials are needed for the default engine.

```bash
python setup_engine.py
python server.py
```

Open http://127.0.0.1:8000 on the machine running the app. The setup downloads the pinned Morpheus 0.10.0 artifact from Maven Central and verifies its SHA-256 checksum. Restart the app after changing engine settings.

1. Upload UTF-8 `.sql` files, paste SQL, or load the included sample scripts.
2. Select **Assess readiness**. Expand each file to see findings, line numbers, and review actions.
3. Select **Convert SQL** to run the installed Morpheus engine.
4. Inspect source and output, select files using the dropdowns, and expand engine diagnostics.
5. Export the assessment JSON or download a ZIP containing generated SQL and the report.

Uploaded sources are held in browser memory and sent to this app's server when assessed or converted. Conversion creates temporary files, then removes them. There is no external SQL upload, database connection, analytics, browser persistence, or database execution. Download artifacts before refreshing the page.

## What the assessment means

The initial readiness report is a heuristic source-code scan. It masks quoted text, quoted identifiers, single-line comments, and nested block comments before evaluating rules. Findings cover stored procedures, dynamic SQL, cursors, transaction semantics, temporary tables, table variables, query hints, linked-server constructs, identity columns, MERGE, OUTPUT, exception handling, SQL Server functions, TOP, collations, and session settings.

Complexity is High when any high-severity category matches, Medium when any medium category matches, otherwise Low. Counts represent matching categories per file, not an effort estimate or probability of migration success. No matching rules does not establish readiness. This scanner is **not Lakebridge Analyzer**, does not parse a complete SQL grammar, and does not inspect a live SQL Server instance, data volumes, query history, or resolved dependencies.

Conversion uses **the actual Lakebridge Morpheus 0.10.0 artifact**, through its stdio Language Server Protocol (`document/transpileToDatabricks`). The adapter uses the protocol defined in Lakebridge's LSPEngine. It preserves diagnostics and generated FIXME comments. Engine diagnostics can include errors even when output is produced. Generated SQL is never represented as executed or validated on Databricks. Review and reconcile it before deployment.

## Optional: use an existing Lakebridge CLI installation

If you already have the Databricks CLI and Lakebridge installed:

```bash
databricks labs install lakebridge
databricks labs lakebridge install-transpile
```

Select Morpheus during Lakebridge setup. Set `LAKEBRIDGE_TRANSPILER_CONFIG` to the absolute Morpheus configuration-file path produced by that installation. When the CLI and configuration are present, the app uses `databricks labs lakebridge transpile --source-dialect mssql ... --skip-validation true`. Otherwise it uses the installed standalone Morpheus artifact. `MORPHEUS_JAR` can override the local JAR path. If neither route is configured, assessment remains available and conversion is disabled.

Lakebridge installation may require additional setup beyond this app. Reference the upstream installation documentation for your environment. Do not place tokens in source files or the hosting manifest.

## Verification

```bash
python -m unittest discover -s tests -v
node --check static/app.js
```

Tests cover source masking, line references, input validation, the missing-engine path, CLI arguments, temporary-file cleanup, LSP text edits, HTTP assessment, origin rejection, static routing, and ZIP safety. The real Morpheus test is skipped when the JAR or Java is unavailable.

## Hosting and current limits

This is a local pilot app. The server binds to `127.0.0.1` by default. To deploy for a team, run on a Java-capable server or container behind an authenticated TLS reverse proxy. This Python server has no user accounts or durable storage. The default Sites Worker runtime cannot run Java; a hosted frontend would need a separate Java-capable conversion service.

- Up to 50 files / 4 MB source text per request; filenames must be unique and contain no folder paths.
- One conversion per server process, with a 180-second limit and a 768 MB JVM heap in standalone mode.
- No Databricks connection, automatic deployment, live profiling, data reconciliation, dependency resolution, or job orchestration is included.
- Browser visual QA and Databricks execution validation were not performed in this environment.

## Upstream sources

- https://github.com/databrickslabs/lakebridge
- https://databrickslabs.github.io/lakebridge/docs/choosing_tools
- https://databrickslabs.github.io/lakebridge/docs/transpile/pluggable_transpilers/morpheus
- Artifact: `com.databricks.labs:databricks-morph-plugin:0.10.0`
- Verified SHA-256: `befab886308f7ccdc4775c9045f584209b6bd993a7eb2a65206be24edd5732d3`

The third-party artifact is downloaded separately and remains subject to its upstream license and notices. This app is an independent accelerator, not an official Databricks product.
