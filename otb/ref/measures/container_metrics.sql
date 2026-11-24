WITH receivers(receiver, cpu, memory, container_attr) AS (
  VALUES
    ('docker_stats', 'container.cpu.utilization', 'container.memory.usage.total', 'container.name'),
    ('kubeletstats', 'container.cpu.usage', 'container.memory.working_set', 'k8s.container.name')
),
r AS (SELECT DISTINCT resource_id, attrs FROM resources)
SELECT k.receiver, k.cpu, k.memory, k.container_attr
FROM receivers k
WHERE EXISTS (
  SELECT 1
  FROM points p LEFT JOIN r USING (resource_id)
  WHERE p.ts_ns >= $window_start AND p.ts_ns < $window_end
    AND p.metric IN (k.cpu, k.memory)
    AND coalesce(p.service, r.attrs[k.container_attr]) = $service
)
ORDER BY k.receiver
