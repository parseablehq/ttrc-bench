SELECT DISTINCT metric, coalesce(unit, '') AS unit, type, coalesce(monotonic, false) AS monotonic
FROM points
WHERE ts_ns >= $window_start AND ts_ns < $window_end
ORDER BY metric, unit, type, monotonic
