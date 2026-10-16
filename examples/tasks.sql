-- Generic tasks database for quota.plugins.sqlite_tasks.
-- Not read unless attribution_plugins or QUOTA_ATTRIBUTION_PLUGIN enables it.
-- Opened read-only by the collector. Keep the file somewhere you choose and
-- point tasks_db or QUOTA_TASKS_DB at it.

CREATE TABLE projects (
  id TEXT PRIMARY KEY,
  name TEXT,
  bot_id TEXT
);

CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  project_id TEXT
);
