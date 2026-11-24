WITH e AS (
  SELECT coalesce(status_message, '') AS signature, 1 AS span, 0 AS log, start_ns AS t
  FROM spans
  WHERE start_ns >= $window_start AND start_ns < $window_end
    AND service = $service
    AND ($region IS NULL OR region = $region)
    AND status_code = 'ERROR'
  UNION ALL
  SELECT coalesce(body, ''), 0, 1, ts_ns
  FROM logs
  WHERE ts_ns >= $window_start AND ts_ns < $window_end
    AND service = $service
    AND ($region IS NULL OR region = $region)
    AND (severity_number >= 13 OR lower(severity_text) IN ('warn', 'warning', 'error', 'fatal'))
)
SELECT signature, sum(span) AS spans, sum(log) AS logs, count(*) AS n, min(t) AS first_ns, max(t) AS last_ns
FROM e
GROUP BY signature
ORDER BY n DESC, signature
