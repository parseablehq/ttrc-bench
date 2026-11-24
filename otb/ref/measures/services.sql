WITH s AS (SELECT service, count(*) AS n FROM spans
           WHERE start_ns >= $window_start AND start_ns < $window_end GROUP BY service),
     l AS (SELECT service, count(*) AS n FROM logs
           WHERE ts_ns >= $window_start AND ts_ns < $window_end GROUP BY service),
     p AS (SELECT service, count(*) AS n FROM points
           WHERE ts_ns >= $window_start AND ts_ns < $window_end GROUP BY service),
     names AS (SELECT service FROM s UNION SELECT service FROM l UNION SELECT service FROM p)
SELECT names.service, coalesce(s.n, 0) AS spans, coalesce(l.n, 0) AS logs, coalesce(p.n, 0) AS points
FROM names LEFT JOIN s USING (service) LEFT JOIN l USING (service) LEFT JOIN p USING (service)
WHERE names.service IS NOT NULL
ORDER BY names.service
