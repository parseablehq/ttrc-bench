SELECT count(*) AS n, min(ts_ns) AS first_ns, max(ts_ns) AS last_ns
FROM logs
WHERE ts_ns >= $window_start AND ts_ns < $window_end
  AND ($service IS NULL OR service = $service)
  AND (NOT $errors_only OR (severity_number >= 17 OR lower(severity_text) IN ('error', 'fatal')))
  AND ($contains IS NULL OR contains(body, $contains))
ORDER BY n
