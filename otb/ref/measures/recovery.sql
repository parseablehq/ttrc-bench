WITH e AS (
  SELECT start_ns AS t FROM spans
  WHERE start_ns >= $window_start AND start_ns < $window_end
    AND status_code = 'ERROR' AND contains(status_message, $signature)
  UNION ALL
  SELECT ts_ns FROM logs
  WHERE ts_ns >= $window_start AND ts_ns < $window_end
    AND contains(body, $signature)
),
l AS (SELECT max(t) AS last_ns FROM e)
SELECT last_ns,
       last_ns + CAST($quiet_s AS BIGINT) * 1000000000 <= $window_end AS recovered,
       CASE WHEN last_ns + CAST($quiet_s AS BIGINT) * 1000000000 <= $window_end
            THEN CAST((last_ns // 60000000000 + 1) * 60000000000 AS BIGINT) END AS recovered_at_ns
FROM l
ORDER BY last_ns
