SELECT service, count(*) AS n
FROM spans
WHERE start_ns >= $window_start AND start_ns < $window_end
  AND ($kind IS NULL OR kind = $kind)
  AND (NOT $errors_only OR status_code = 'ERROR')
GROUP BY service
ORDER BY service
