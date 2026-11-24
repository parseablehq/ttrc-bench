SELECT text, sum(spans) AS spans, sum(logs) AS logs
FROM (
  SELECT status_message AS text, 1 AS spans, 0 AS logs
  FROM spans
  WHERE start_ns >= $window_start AND start_ns < $window_end
    AND status_code = 'ERROR' AND coalesce(status_message, '') <> ''
  UNION ALL
  SELECT body, 0, 1
  FROM logs
  WHERE ts_ns >= $window_start AND ts_ns < $window_end
    AND (severity_number >= 13 OR lower(severity_text) IN ('warn', 'warning', 'error', 'fatal'))
    AND coalesce(body, '') <> ''
)
GROUP BY text
ORDER BY text
