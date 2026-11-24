WITH RECURSIVE t AS (
  SELECT span_id, parent_span_id, service, name, start_ns, duration_ns, status_code
  FROM spans
  WHERE trace_id = $trace_id
),
roots AS (
  SELECT a.* FROM t a LEFT JOIN t p ON p.span_id = a.parent_span_id WHERE p.span_id IS NULL
),
depth(span_id, d) AS (
  SELECT span_id, 0 FROM roots
  UNION ALL
  SELECT c.span_id, depth.d + 1 FROM t c JOIN depth ON c.parent_span_id = depth.span_id
),
root AS (
  SELECT service, duration_ns FROM roots ORDER BY parent_span_id IS NOT NULL, start_ns, span_id LIMIT 1
),
deep AS (
  SELECT t.service, t.name
  FROM t JOIN depth USING (span_id)
  WHERE t.status_code = 'ERROR'
  ORDER BY depth.d DESC, t.start_ns, t.span_id
  LIMIT 1
)
SELECT (SELECT count(*) FROM t) AS spans,
       (SELECT service FROM root) AS root_service,
       (SELECT round(duration_ns / 1e6, 3) FROM root) AS root_duration_ms,
       (SELECT service FROM deep) AS deepest_error_service,
       (SELECT name FROM deep) AS deepest_error_operation
ORDER BY spans
