SELECT name AS signature, count(*) AS n, min(start_ns) AS first_ns, max(start_ns) AS last_ns
FROM spans
WHERE start_ns >= $window_start AND start_ns < $window_end
  AND service = $service
  AND ($region IS NULL OR region = $region)
  AND duration_ns >= CAST($min_duration_ms AS BIGINT) * 1000000
GROUP BY name
ORDER BY n DESC, signature
