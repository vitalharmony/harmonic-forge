-- D2 persistent base failure (harmonic-forge#1160): each test failing in every
-- one of the newest N >= 2 consecutive complete te-runner base checks, ordered
-- by result.json mtime (te_run.previous_base's order), sha breaking a tie.
-- Reads only the tables detector_report.py registers.
CREATE OR REPLACE VIEW d2_persistent_base_failure AS
WITH checks AS (
  SELECT sha, failures, row_number() OVER (ORDER BY mtime DESC, sha) AS recency
  FROM base_checks WHERE complete),
per_test AS (SELECT DISTINCT unnest(failures) AS test, recency FROM checks),
-- The streak is the run from the newest check back to the first check the
-- test passed in: its k-th newest failure is in the k-th newest check.
ranked AS (
  SELECT test, recency, row_number() OVER (PARTITION BY test ORDER BY recency) AS k
  FROM per_test),
streaks AS (SELECT test, count(*) AS n FROM ranked WHERE recency = k GROUP BY test)
SELECT s.test, s.n AS consecutive_base_checks
FROM streaks s LEFT JOIN base_check_protected p ON p.test = s.test
-- A test with no protected row cannot be checked, so it is protected.
WHERE s.n >= 2 AND NOT coalesce(p.protected, true)
ORDER BY s.test;
