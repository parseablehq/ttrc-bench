WITH d AS (
  SELECT duration_ns,
         row_number() OVER (ORDER BY duration_ns, span_id) AS r,
         count(*) OVER () AS n
  FROM spans
  WHERE start_ns >= $window_start AND start_ns < $window_end
    AND service = $service
    AND name = $name
    AND kind = $kind
)
SELECT coalesce(max(n), 0) AS n, round(max(duration_ns) FILTER (WHERE r = greatest(ceil($q * n), 1)) / 1e6, 3) AS p_ms
FROM d
ORDER BY n
