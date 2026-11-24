SELECT count(DISTINCT trace_id) AS n
FROM spans
WHERE start_ns >= $window_start AND start_ns < $window_end
  AND ($service IS NULL OR service = $service)
ORDER BY n
