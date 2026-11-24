SELECT service, name,
       count(*) FILTER (WHERE start_ns >= $window_start AND start_ns < $window_end) AS n,
       count(*) FILTER (WHERE start_ns >= $baseline_start AND start_ns < $baseline_end) AS n_base
FROM spans
WHERE kind = 'SERVER'
  AND ((start_ns >= $window_start AND start_ns < $window_end) OR (start_ns >= $baseline_start AND start_ns < $baseline_end))
GROUP BY service, name
HAVING n >= $min_spans AND n_base >= $min_spans
ORDER BY service, name
