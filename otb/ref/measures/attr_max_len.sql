SELECT max(length(attrs[$key])) AS n
FROM (
  SELECT attrs FROM spans WHERE $signal = 'traces' AND start_ns >= $window_start AND start_ns < $window_end
  UNION ALL
  SELECT attrs FROM logs WHERE $signal = 'logs' AND ts_ns >= $window_start AND ts_ns < $window_end
)
ORDER BY n
