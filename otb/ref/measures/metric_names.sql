SELECT DISTINCT metric
FROM points
WHERE ts_ns >= $window_start AND ts_ns < $window_end
ORDER BY metric
