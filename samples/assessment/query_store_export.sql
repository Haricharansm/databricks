-- Read-only SQL Server Query Store export template. Run in the SOURCE database yourself.
-- Crosslake does not connect to SQL Server or execute this script.
-- Requires Query Store to be enabled and permission to read its catalog views.
-- Review bounds/permissions for your SQL Server version before running.
DECLARE @WindowStart datetimeoffset = DATEADD(day, -7, SYSDATETIMEOFFSET());
DECLARE @WindowEnd datetimeoffset = SYSDATETIMEOFFSET();
-- TOP 5000 intentionally bounds the import. This can be an incomplete workload sample.
-- Only complete runtime-stat intervals and regular executions are included.
-- Query Store duration and CPU averages are in microseconds: weight by executions,
-- then divide by 1000 to export TOTAL milliseconds, not average milliseconds.
SELECT TOP (5000)
    CONVERT(varchar(40), i.start_time, 127) AS interval_start,
    CONVERT(varchar(40), i.end_time, 127) AS interval_end,
    qt.query_sql_text AS query_text,
    SUM(CONVERT(bigint, rs.count_executions)) AS execution_count,
    CAST(SUM(rs.avg_duration * rs.count_executions) / 1000.0 AS decimal(28,3)) AS total_duration_ms,
    CAST(SUM(rs.avg_cpu_time * rs.count_executions) / 1000.0 AS decimal(28,3)) AS total_cpu_ms,
    CAST(ROUND(SUM(rs.avg_logical_io_reads * rs.count_executions), 0) AS bigint) AS logical_reads
FROM sys.query_store_query_text AS qt
JOIN sys.query_store_query AS q ON q.query_text_id = qt.query_text_id
JOIN sys.query_store_plan AS p ON p.query_id = q.query_id
JOIN sys.query_store_runtime_stats AS rs ON rs.plan_id = p.plan_id
JOIN sys.query_store_runtime_stats_interval AS i ON i.runtime_stats_interval_id = rs.runtime_stats_interval_id
WHERE i.start_time >= @WindowStart AND i.end_time <= @WindowEnd AND rs.execution_type = 0
GROUP BY i.start_time, i.end_time, q.query_id, qt.query_sql_text
ORDER BY SUM(CONVERT(bigint, rs.count_executions)) DESC;
-- Export the grid as a properly quoted CSV with headers. Application/user are optional
-- and are NOT available as reliable execution dimensions from these Query Store views.
-- Do not invent these values; leave them absent unless another trusted source supplies them.
