SELECT url_path, count(*) AS n, CAST(sum(count(*)) OVER () AS BIGINT) AS total
FROM spans
WHERE start_ns >= $window_start AND start_ns < $window_end
  AND service = $service
  AND kind = 'SERVER'
  AND http_status >= 500
GROUP BY url_path
ORDER BY n DESC, url_path NULLS LAST
