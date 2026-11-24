SELECT count(*) AS n, min(start_ns) AS first_ns, max(start_ns) AS last_ns
FROM spans
WHERE start_ns >= $window_start AND start_ns < $window_end
  AND ($service IS NULL OR service = $service)
  AND ($kind IS NULL OR kind = $kind)
  AND ($name IS NULL OR name = $name)
  AND (NOT $errors_only OR status_code = 'ERROR')
  AND ($message_contains IS NULL OR contains(status_message, $message_contains))
ORDER BY n
