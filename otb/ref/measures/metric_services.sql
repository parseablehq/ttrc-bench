SELECT service, count(*) AS n
FROM points
WHERE ts_ns >= $window_start AND ts_ns < $window_end
  AND metric = $metric
  AND service IS NOT NULL
  AND len(list_filter($attrs::VARCHAR[],
          kv -> attrs[split_part(kv, '=', 1)] IS DISTINCT FROM substr(kv, strpos(kv, '=') + 1))) = 0
GROUP BY service
HAVING count(*) >= $min_points
ORDER BY service
