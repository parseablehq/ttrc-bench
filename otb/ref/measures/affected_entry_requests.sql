WITH RECURSIVE marked AS (
  SELECT trace_id, span_id, start_ns AS t FROM spans
  WHERE start_ns >= $window_start AND start_ns < $window_end
    AND status_code = 'ERROR'
    AND contains(status_message, $signature)
  UNION ALL
  SELECT trace_id, span_id, ts_ns FROM logs
  WHERE ts_ns >= $window_start AND ts_ns < $window_end
    AND trace_id IS NOT NULL AND trace_id <> '' AND span_id IS NOT NULL AND span_id <> ''
    AND contains(body, $signature)
),
start AS (
  SELECT trace_id, span_id FROM (
    SELECT trace_id, span_id, row_number() OVER (PARTITION BY trace_id ORDER BY t, span_id) AS r FROM marked
  ) WHERE r = 1
),
t AS (
  SELECT trace_id, span_id, parent_span_id, service, name, kind FROM spans
  WHERE trace_id IN (SELECT trace_id FROM start)
),
up(trace_id, span_id, parent_span_id, step) AS (
  SELECT t.trace_id, t.span_id, t.parent_span_id, 0 FROM t JOIN start USING (trace_id, span_id)
  UNION ALL
  SELECT p.trace_id, p.span_id, p.parent_span_id, up.step + 1
  FROM t p JOIN up ON p.trace_id = up.trace_id AND p.span_id = up.parent_span_id
),
entry AS (
  SELECT up.trace_id, t.service, t.name,
         row_number() OVER (PARTITION BY up.trace_id ORDER BY up.step DESC) AS r
  FROM up JOIN t USING (trace_id, span_id)
  WHERE t.kind = 'SERVER' AND t.name NOT IN ('GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS', 'CONNECT', 'TRACE')
)
SELECT service, name AS operation, count(*) AS n, (SELECT count(DISTINCT trace_id) FROM marked) AS total
FROM entry
WHERE r = 1
GROUP BY service, name
ORDER BY n DESC, service, operation
