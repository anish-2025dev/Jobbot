-- Actual bytes per row (heap + indexes) for every table, partitions rolled up.
-- Run: psql "$DATABASE_URL" -f scripts/size_report.sql
WITH t AS (
  SELECT coalesce(p.relname, c.relname) AS tbl,
         sum(pg_total_relation_size(c.oid)) AS bytes,
         sum(c.reltuples)::bigint AS rows
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
  LEFT JOIN pg_inherits i ON i.inhrelid = c.oid
  LEFT JOIN pg_class p ON p.oid = i.inhparent
  WHERE c.relkind = 'r'
  GROUP BY 1
)
SELECT tbl, rows, pg_size_pretty(bytes) AS total,
       CASE WHEN rows > 0 THEN bytes / rows END AS bytes_per_row
FROM t ORDER BY bytes DESC;

SELECT pg_size_pretty(pg_database_size(current_database())) AS database_total;
