# Synthetic Lakeflow direct-copy example

1. Upload `DirectCopy.dtsx` to Crosslake, assess it, and select its saved run.
2. Open Lakeflow studio and load the selected run. Choose `pipeline` for `Package\CopyCustomers`.
3. Set catalog/schema to an existing permitted target. For a file example, upload `customers.csv` into a Unity Catalog Volume you control, then configure:
   - Source type: `files`
   - Source: `/Volumes/<catalog>/<schema>/<volume>/customers` (the folder containing the CSV)
   - Target table: `customers_snapshot`
   - Mode: `batch` (materialized-view snapshot)
   - Format: `csv`
   - Schema DDL: `id BIGINT, name STRING, amount DECIMAL(12,2)`
4. Save, preview, and download the bundle. This mapping does not require the external SSIS converter because it generates a new declarative pipeline from an explicit direct-copy mapping.
5. Follow the bundle README for CLI validation, review-gate changes, deployment and a manual test run. You must supply your workspace identity, permissions and Volume path. No workspace assets are created by this sample until you deploy/start them yourself.
6. Query the resulting table and compare with `expected_customers.json`: four rows, one duplicate, one NULL amount, non-NULL amount sum 51.25. Declare validation columns `id` integer, `name` string, `amount` decimal; choose unordered matching to preserve duplicate multiplicity.

Switching to streaming generates an Auto Loader append pipeline with different refresh semantics; use a separate target and test incremental behavior explicitly. Source file parsing and schema evolution must be reviewed. This DTSX is a synthetic metadata fixture for the accelerator, not a certified complete SSDT project.

For Jobs from converted SQL, use `samples/ssis/ReadOnlySalesSmokeTest`: convert it first, then keep its three `auto_sql` mappings. Supported success dependencies generate three guarded notebook tasks. Jobs and pipelines can appear in the same saved migration run and bundle.
