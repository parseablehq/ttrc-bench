SELECT 'traces' AS signal, max(list_max(list_transform(map_values(attrs), v -> length(v)))) AS n
FROM spans WHERE start_ns >= $window_start AND start_ns < $window_end
UNION ALL
SELECT 'logs', max(list_max(list_transform(map_values(attrs), v -> length(v))))
FROM logs WHERE ts_ns >= $window_start AND ts_ns < $window_end
UNION ALL
SELECT 'metrics', max(list_max(list_transform(map_values(attrs), v -> length(v))))
FROM points WHERE ts_ns >= $window_start AND ts_ns < $window_end
ORDER BY signal
