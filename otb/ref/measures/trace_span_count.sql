SELECT count(*) AS n
FROM spans
WHERE trace_id = $trace_id
ORDER BY n
