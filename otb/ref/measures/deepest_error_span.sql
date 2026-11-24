WITH RECURSIVE hit AS (
  SELECT DISTINCT trace_id FROM spans
  WHERE start_ns >= $window_start AND start_ns < $window_end
    AND service = $entry_service AND kind = 'SERVER' AND name = $entry_op
),
t AS (
  SELECT trace_id, span_id, parent_span_id, service, name, kind, start_ns, status_code,
         service = $entry_service AND kind = 'SERVER' AND name = $entry_op AS is_op
  FROM spans
  WHERE trace_id IN (SELECT trace_id FROM hit)
),
anc(trace_id, span_id, ancestor_id) AS (
  SELECT trace_id, span_id, parent_span_id FROM t WHERE is_op AND parent_span_id IS NOT NULL
  UNION
  SELECT anc.trace_id, anc.span_id, p.parent_span_id
  FROM anc JOIN t p ON p.trace_id = anc.trace_id AND p.span_id = anc.ancestor_id
  WHERE p.parent_span_id IS NOT NULL
),
nested AS (
  SELECT DISTINCT anc.trace_id, anc.span_id FROM anc
  JOIN t a ON a.trace_id = anc.trace_id AND a.span_id = anc.ancestor_id
  WHERE a.is_op
),
entry AS (
  SELECT DISTINCT t.trace_id, t.span_id FROM t
  LEFT JOIN nested n ON n.trace_id = t.trace_id AND n.span_id = t.span_id
  WHERE t.is_op AND n.span_id IS NULL AND t.start_ns >= $window_start AND t.start_ns < $window_end
),
sub(trace_id, root_id, span_id, d) AS (
  SELECT trace_id, span_id, span_id, 0 FROM entry
  UNION ALL
  SELECT t.trace_id, sub.root_id, t.span_id, sub.d + 1
  FROM t JOIN sub ON t.trace_id = sub.trace_id AND t.parent_span_id = sub.span_id
),
errs AS (
  SELECT sub.trace_id, sub.root_id, t.service, t.name,
         row_number() OVER (
           PARTITION BY sub.trace_id, sub.root_id ORDER BY sub.d DESC, t.start_ns, t.span_id, t.service, t.name
         ) AS r
  FROM sub JOIN t USING (trace_id, span_id)
  WHERE t.status_code = 'ERROR'
),
deepest AS (SELECT service, name FROM errs WHERE r = 1)
SELECT service, name AS operation, count(*) AS n,
       (SELECT count(*) FROM deepest) AS failures,
       round(count(*) / (SELECT count(*) FROM deepest), 4) AS share
FROM deepest
GROUP BY service, name
ORDER BY n DESC, service, operation
