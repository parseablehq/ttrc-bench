WITH RECURSIVE hit AS (
  SELECT DISTINCT trace_id FROM spans
  WHERE start_ns >= $window_start AND start_ns < $window_end
    AND status_code = 'ERROR' AND contains(status_message, $signature)
),
t AS (
  SELECT trace_id, span_id, parent_span_id, service, status_code, start_ns,
         status_code = 'ERROR' AND coalesce(contains(status_message, $signature), false) AS marked
  FROM spans
  WHERE trace_id IN (SELECT trace_id FROM hit)
),
leaf AS (
  SELECT trace_id, span_id FROM (
    SELECT a.trace_id, a.span_id, row_number() OVER (PARTITION BY a.trace_id ORDER BY a.start_ns, a.span_id) AS r
    FROM t a
    LEFT JOIN (SELECT DISTINCT trace_id, parent_span_id FROM t WHERE status_code = 'ERROR') c
      ON c.trace_id = a.trace_id AND c.parent_span_id = a.span_id
    WHERE a.marked AND c.parent_span_id IS NULL
  )
  WHERE r = 1
),
up(trace_id, span_id, parent_span_id, service, step) AS (
  SELECT t.trace_id, t.span_id, t.parent_span_id, t.service, 0 FROM t JOIN leaf USING (trace_id, span_id)
  UNION ALL
  SELECT p.trace_id, p.span_id, p.parent_span_id, p.service, up.step + 1
  FROM t p JOIN up ON p.trace_id = up.trace_id AND p.span_id = up.parent_span_id
  WHERE p.status_code = 'ERROR'
),
seq AS (
  SELECT trace_id, service, step, lag(service) OVER (PARTITION BY trace_id ORDER BY step) AS prev FROM up
),
chains AS (
  SELECT trace_id, string_agg(service, '>' ORDER BY step) AS chain FROM seq
  WHERE prev IS NULL OR prev <> service
  GROUP BY trace_id
),
top AS (
  SELECT chain, count(*) AS traces FROM chains GROUP BY chain ORDER BY traces DESC, chain LIMIT 1
)
SELECT CAST(generate_subscripts(string_split(chain, '>'), 1) AS INTEGER) AS pos,
       unnest(string_split(chain, '>')) AS service,
       traces
FROM top
ORDER BY pos
