-- D1 enforcement gap (harmonic-forge#1159): rules missed twice or more while
-- in force, split into "no hook" and "hook not binding" (a hook exists now and
-- misses were recorded; the registry cannot say when it was wired, so read
-- last_miss). Reads only the tables detector_report.py registers.
CREATE OR REPLACE VIEW d1_enforcement_gap AS
WITH misses AS (
  SELECT regexp_extract(rule_ref, 'R-\d{4}') AS rule_id,
         count(*) AS misses,
         count(*) FILTER (WHERE caught_by = 'operator') AS operator_caught,
         max(ts) AS last_miss
  FROM misses_raw
  WHERE rule_existed IN ('rule', 'scoped_out')
    AND regexp_extract(rule_ref, 'R-\d{4}') <> ''
  GROUP BY 1)
SELECT m.rule_id, m.misses, m.operator_caught, m.last_miss,
       CASE WHEN coalesce(h.hook_count, 0) = 0 THEN 'no hook' ELSE 'hook not binding' END AS section,
       coalesce(h.scripts, '') AS hooks
FROM misses m LEFT JOIN rule_hooks h ON h.rule_id = m.rule_id
WHERE m.misses >= 2 AND NOT coalesce(h.protected, false)
ORDER BY section DESC, m.misses DESC, m.rule_id;
