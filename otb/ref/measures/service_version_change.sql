WITH v AS (
  SELECT service_version, min(start_ns) AS first_ns FROM spans
  WHERE start_ns < $window_end AND service = $service AND service_version IS NOT NULL
  GROUP BY service_version
)
SELECT service_version AS version, first_ns
FROM v
WHERE first_ns >= $window_start
ORDER BY first_ns, version
