WITH s AS (
  SELECT service,
         count(*) FILTER (WHERE start_ns >= $window_start AND start_ns < $window_end) AS total,
         count(*) FILTER (WHERE start_ns >= $window_start AND start_ns < $window_end AND status_code = 'ERROR') AS errors,
         count(*) FILTER (WHERE start_ns >= $baseline_start AND start_ns < $baseline_end) AS base_total,
         count(*) FILTER (WHERE start_ns >= $baseline_start AND start_ns < $baseline_end AND status_code = 'ERROR')
           AS base_errors
  FROM spans
  WHERE ((start_ns >= $window_start AND start_ns < $window_end) OR (start_ns >= $baseline_start AND start_ns < $baseline_end))
    AND ($region IS NULL OR region = $region)
  GROUP BY service
)
SELECT service, errors, total, base_errors, base_total,
       CASE WHEN base_errors = 0 THEN NULL
            ELSE round((errors / total) / (base_errors / base_total), 3) END AS ratio
FROM s
WHERE errors >= $min_errors
  AND (base_errors = 0 OR errors * base_total > $min_ratio * base_errors * total)
ORDER BY service
