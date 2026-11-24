SELECT count(*) AS n
FROM spans
WHERE trace_id = $trace_id AND start_ns >= $window_start AND start_ns < $window_end
ORDER BY n
