-- Failure store schema (harmonic-forge#1288).
--
-- A derived, rebuildable projection of the protocol-failures.jsonl logs; the
-- logs stay the system of record. `failure_store.py build` creates a fresh
-- database from this file on every build, so a published schema change is a
-- rebuild, never a migration. The data lives in
-- ~/Harmonic_Projects/operator-memory/, never in this repo.

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- One row per parseable log line. `line` is the 0-based line index in its
-- log, the same addressing an overlay's `log_line` uses.
CREATE TABLE failures (
  log                TEXT NOT NULL,     -- 'memory' or 'testplan'
  line               INTEGER NOT NULL,
  ts                 TEXT,
  session            TEXT,
  turn               TEXT,
  lane               TEXT,
  repo               TEXT,
  issue              TEXT,
  caught_by          TEXT,
  rule_existed       TEXT,
  rule_ref           TEXT,
  tool               TEXT,
  class              TEXT,
  probe              TEXT,
  command            TEXT,
  class_list_version INTEGER,
  PRIMARY KEY (log, line)
);

CREATE INDEX failures_key ON failures (tool, class, ts);

-- Relabels. Rows are inserted in application order (file name order, then
-- file order), so the highest rowid for a (log, line) is the last match, and
-- the last match wins. A record's own columns are never rewritten.
CREATE TABLE overlays (
  source TEXT NOT NULL,                 -- overlay file name
  log    TEXT NOT NULL,
  line   INTEGER NOT NULL,
  tool   TEXT NOT NULL,
  class  TEXT NOT NULL,
  probe  TEXT
);

-- The effective key of every record: an overlay key wins over the record's
-- own key; an overlay without a probe keeps the record's probe.
CREATE VIEW failure_keys AS
WITH last_overlay AS (
  SELECT log, line, tool, class, probe
  FROM (SELECT log, line, tool, class, probe,
               row_number() OVER (PARTITION BY log, line ORDER BY rowid DESC) AS rn
        FROM overlays)
  WHERE rn = 1
)
SELECT f.log, f.line, f.ts, f.session, f.turn, f.lane, f.repo, f.issue,
       f.caught_by, f.rule_existed, f.rule_ref, f.command, f.class_list_version,
       coalesce(o.tool, f.tool)   AS tool,
       coalesce(o.class, f.class) AS class,
       coalesce(o.probe, f.probe) AS probe,
       f.tool  AS own_tool,
       f.class AS own_class,
       o.log IS NOT NULL AS overlaid
FROM failures f
LEFT JOIN last_overlay o ON o.log = f.log AND o.line = f.line;
