WITH p AS (
  SELECT service, instance, attrs::VARCHAR AS series, start_ns, ts_ns,
         max(coalesce(CAST(value_int AS DOUBLE), value)) AS v
  FROM points
  WHERE ts_ns < $window_end
    AND metric = $metric
    AND ($service IS NULL OR service = $service)
    AND len(list_filter($attrs::VARCHAR[],
            kv -> attrs[split_part(kv, '=', 1)] IS DISTINCT FROM substr(kv, strpos(kv, '=') + 1))) = 0
  GROUP BY service, instance, series, start_ns, ts_ns
),
s AS (
  SELECT service, instance, series, start_ns,
         arg_max(v, ts_ns) FILTER (WHERE ts_ns >= $window_start) AS last_in,
         arg_min(v, ts_ns) FILTER (WHERE ts_ns >= $window_start) AS first_in,
         arg_max(v, ts_ns) FILTER (WHERE ts_ns < $window_start) AS last_before
  FROM p
  GROUP BY service, instance, series, start_ns
)
SELECT CAST(round(coalesce(sum(last_in - CASE
         WHEN last_before IS NOT NULL THEN last_before
         WHEN start_ns >= $window_start THEN 0
         ELSE first_in END ORDER BY service, instance, series, start_ns), 0)) AS BIGINT) AS n
FROM s
WHERE last_in IS NOT NULL
ORDER BY n
