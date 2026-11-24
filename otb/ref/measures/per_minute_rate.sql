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
d AS (
  SELECT service, instance, series, start_ns, ts_ns,
         v - lag(v) OVER w AS dv,
         (ts_ns - lag(ts_ns) OVER w) / 1e9 AS dt
  FROM p
  WINDOW w AS (PARTITION BY service, instance, series, start_ns ORDER BY ts_ns)
)
SELECT CAST(ts_ns // 60000000000 * 60000000000 AS BIGINT) AS minute_ns,
       round(sum(dv / dt ORDER BY service, instance, series, start_ns, ts_ns), 4) AS rate_per_s
FROM d
WHERE ts_ns >= $window_start AND dt > 0 AND dv >= 0
GROUP BY minute_ns
ORDER BY rate_per_s DESC, minute_ns
