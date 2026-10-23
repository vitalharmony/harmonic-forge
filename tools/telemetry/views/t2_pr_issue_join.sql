-- F637 T2b (harmonic-forge#1167): the PR <-> issue join as a derived,
-- three-state view over harmonic-forge#1166's observation events. Nothing is
-- stamped: a later observation changes the answer on the next run.
-- Reads only the `events` table pr_join_report.py registers.
CREATE OR REPLACE VIEW pr_issue_join AS
WITH ev AS (
  SELECT *, CAST(replace(ts, 'Z', '') AS TIMESTAMP) AS t_at FROM events),
prs AS (
  SELECT repo, account, org, pr, min(t_at) AS merged_at
  FROM ev WHERE event_type = 'pr.merged' GROUP BY repo, account, org, pr),
links AS (
  SELECT repo, pr, provenance, a_link_kind, a_target_issue AS issue
  FROM ev
  WHERE event_type = 'pr.link.observed' AND a_target_issue IS NOT NULL
    AND lower(a_target_repo) = lower(repo)),
closed AS (
  SELECT repo, issue, min(t_at) AS closed_at
  FROM ev WHERE event_type = 'issue.closed' GROUP BY repo, issue),
joins AS (
  SELECT l.repo, l.pr, l.issue FROM links l
  WHERE l.provenance IN ('git-subject', 'pr-link-marker')
  UNION
  SELECT l.repo, l.pr, l.issue
  FROM links l JOIN prs p ON p.repo = l.repo AND p.pr = l.pr
  JOIN closed c ON c.repo = l.repo AND c.issue = l.issue
  WHERE l.provenance = 'timeline' AND l.a_link_kind = 'cross-referenced'
    AND c.closed_at >= p.merged_at AND c.closed_at <= p.merged_at + INTERVAL 24 HOUR),
joined AS (SELECT repo, pr, min(issue) AS issue FROM joins GROUP BY repo, pr),
connected AS (
  SELECT DISTINCT repo, pr FROM ev
  WHERE event_type = 'pr.link.observed' AND a_link_kind = 'connected'),
pr_status AS (
  SELECT repo, a_pr AS pr, t_at, a_status FROM ev
  WHERE event_type = 'fetch.status' AND a_pr IS NOT NULL),
newest AS (
  -- The PR's newest run; a failure anywhere in that run marks it failed.
  SELECT s.repo, s.pr, bool_or(s.a_status = 'failed') AS failed
  FROM pr_status s
  JOIN (SELECT repo, pr, max(t_at) AS t_at FROM pr_status GROUP BY repo, pr) m
    ON m.repo = s.repo AND m.pr = s.pr AND m.t_at = s.t_at
  GROUP BY s.repo, s.pr)
SELECT p.repo, p.account, p.org, p.pr, p.merged_at, j.issue,
       CASE WHEN j.issue IS NOT NULL THEN 'joined'
            WHEN c.pr IS NOT NULL OR n.pr IS NULL OR n.failed THEN 'unresolved'
            ELSE 'unjoined' END AS state
FROM prs p
LEFT JOIN joined j ON j.repo = p.repo AND j.pr = p.pr
LEFT JOIN connected c ON c.repo = p.repo AND c.pr = p.pr
LEFT JOIN newest n ON n.repo = p.repo AND n.pr = p.pr
ORDER BY p.repo, p.pr;

CREATE OR REPLACE VIEW pr_join_coverage AS
WITH months AS (
  SELECT repo, strftime(merged_at, '%Y-%m') AS month,
         count(*) FILTER (WHERE state = 'joined') AS joined,
         count(*) FILTER (WHERE state = 'unjoined') AS unjoined,
         count(*) FILTER (WHERE state = 'unresolved') AS unresolved
  FROM pr_issue_join GROUP BY 1, 2),
ev AS (SELECT *, CAST(replace(ts, 'Z', '') AS TIMESTAMP) AS t_at FROM events),
capped AS (
  SELECT repo, max(strftime(CAST(replace(a_oldest_merged_at, 'Z', '') AS TIMESTAMP), '%Y-%m')) AS through
  FROM ev WHERE event_type = 'fetch.status' AND a_status = 'capped' AND a_oldest_merged_at IS NOT NULL
  GROUP BY repo),
repo_status AS (
  SELECT repo, t_at, a_status FROM ev
  WHERE event_type = 'fetch.status' AND a_pr IS NULL AND a_status IN ('ok', 'failed')),
repo_failed AS (
  -- A repo-level failure with no later repo-level ok caveats every month.
  SELECT f.repo FROM repo_status f
  WHERE f.a_status = 'failed'
    AND NOT EXISTS (SELECT 1 FROM repo_status o
                    WHERE o.repo = f.repo AND o.a_status = 'ok' AND o.t_at > f.t_at)
  GROUP BY f.repo)
SELECT m.repo, m.month,
       CASE WHEN m.joined + m.unjoined = 0 THEN NULL
            ELSE round(100.0 * m.joined / (m.joined + m.unjoined), 1) END AS pct_joined,
       m.joined, m.unjoined, m.unresolved,
       concat_ws('; ',
         CASE WHEN c.through IS NOT NULL AND m.month <= c.through THEN 'capped' END,
         CASE WHEN f.repo IS NOT NULL THEN 'fetch failed' END) AS caveat
FROM months m
LEFT JOIN capped c ON c.repo = m.repo
LEFT JOIN repo_failed f ON f.repo = m.repo
ORDER BY m.repo, m.month;

CREATE OR REPLACE VIEW pr_join_medians AS
WITH ev AS (SELECT *, CAST(replace(ts, 'Z', '') AS TIMESTAMP) AS t_at FROM events),
opened AS (
  SELECT repo, issue, min(t_at) AS opened_at
  FROM ev WHERE event_type = 'issue.opened' GROUP BY repo, issue),
pairs AS (
  -- One pair per (repo, issue): the earliest joined merge.
  SELECT j.repo, j.issue, min(j.merged_at) AS merged_at
  FROM pr_issue_join j WHERE j.state = 'joined' GROUP BY j.repo, j.issue),
hours AS (
  SELECT p.repo, (epoch(p.merged_at) - epoch(o.opened_at)) / 3600.0 AS h
  FROM pairs p JOIN opened o ON o.repo = p.repo AND o.issue = p.issue),
grouped AS (
  SELECT 'all' AS scope, median(h) AS median_hours, count(*) AS pairs FROM hours
  UNION ALL
  SELECT repo, median(h), count(*) FROM hours GROUP BY repo)
SELECT scope, CASE WHEN pairs >= 20 THEN round(median_hours, 2) END AS median_hours, pairs
FROM grouped ORDER BY scope = 'all' DESC, scope;
