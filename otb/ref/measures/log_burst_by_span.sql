WITH e AS (
  SELECT trace_id, span_id, ts_ns >= $window_start AND ts_ns < $window_end AS inw
  FROM logs
  WHERE ((ts_ns >= $window_start AND ts_ns < $window_end) OR (ts_ns >= $baseline_start AND ts_ns < $baseline_end))
    AND (severity_number >= 17 OR lower(severity_text) IN ('error', 'fatal'))
    AND span_id IS NOT NULL AND span_id <> ''
)
SELECT s.service, s.name AS operation, count(*) FILTER (WHERE e.inw) AS n, count(*) FILTER (WHERE NOT e.inw) AS n_base
FROM e JOIN spans s ON s.trace_id = e.trace_id AND s.span_id = e.span_id
GROUP BY s.service, s.name
HAVING n >= $min_records AND n > 2 * n_base
ORDER BY n DESC, s.service, operation
