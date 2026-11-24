WITH d AS (
  SELECT service, duration_ns,
         row_number() OVER (PARTITION BY service ORDER BY duration_ns, span_id) AS r,
         count(*) OVER (PARTITION BY service) AS n
  FROM spans
  WHERE start_ns >= $window_start AND start_ns < $window_end
    AND kind = 'SERVER'
    AND service IS NOT NULL
)
SELECT service, max(n) AS n, round(max(duration_ns) FILTER (WHERE r = ceil($q * n)) / 1e6, 3) AS p_ms
FROM d
GROUP BY service
HAVING max(n) >= $min_spans AND max(duration_ns) > 0
ORDER BY service
