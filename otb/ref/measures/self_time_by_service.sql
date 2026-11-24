WITH RECURSIVE hit AS (
  SELECT DISTINCT trace_id FROM spans
  WHERE ((start_ns >= $window_start AND start_ns < $window_end) OR (start_ns >= $baseline_start AND start_ns < $baseline_end))
    AND service = $entry_service AND kind = 'SERVER' AND name = $entry_op
),
t AS (
  SELECT trace_id, span_id, parent_span_id, service, duration_ns, start_ns,
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
  SELECT DISTINCT t.trace_id, t.span_id, t.start_ns >= $window_start AND t.start_ns < $window_end AS inw FROM t
  LEFT JOIN nested n ON n.trace_id = t.trace_id AND n.span_id = t.span_id
  WHERE t.is_op AND n.span_id IS NULL
    AND ((t.start_ns >= $window_start AND t.start_ns < $window_end) OR (t.start_ns >= $baseline_start AND t.start_ns < $baseline_end))
),
sub(trace_id, root_id, inw, span_id) AS (
  SELECT trace_id, span_id, inw, span_id FROM entry
  UNION ALL
  SELECT t.trace_id, sub.root_id, sub.inw, t.span_id
  FROM t JOIN sub ON t.trace_id = sub.trace_id AND t.parent_span_id = sub.span_id
),
kids AS (
  SELECT trace_id, parent_span_id AS span_id, sum(duration_ns) AS child_ns FROM t GROUP BY trace_id, parent_span_id
),
own AS (
  SELECT trace_id, sub.root_id, sub.inw, t.service, greatest(t.duration_ns - coalesce(kids.child_ns, 0), 0) AS self_ns
  FROM sub
  JOIN t USING (trace_id, span_id)
  LEFT JOIN kids USING (trace_id, span_id)
),
per_req AS (
  SELECT trace_id, root_id, inw, service, sum(self_ns) AS self_ns FROM own GROUP BY trace_id, root_id, inw, service
)
SELECT service,
       round(quantile_disc(self_ns, 0.5) FILTER (WHERE inw) / 1e6, 3) AS median_ms,
       round(quantile_disc(self_ns, 0.5) FILTER (WHERE NOT inw) / 1e6, 3) AS base_median_ms,
       round((quantile_disc(self_ns, 0.5) FILTER (WHERE inw) - quantile_disc(self_ns, 0.5) FILTER (WHERE NOT inw)) / 1e6, 3)
         AS delta_ms
FROM per_req
GROUP BY service
ORDER BY delta_ms DESC NULLS LAST, service
