# Setup

You are a Grok Bot that just imported this template. Follow the steps in order. Stay inside this repository. Do not copy hostnames, user names, or agent ids from another machine into these files.

## 1. Prerequisites

You need `python3` with the standard library only. `sqlite3`, `json`, and `zoneinfo` are enough. Do not install packages. Do not add an LLM client.

Check:

```sh
python3 --version
```

## 2. Clone

If this tree is already the import, `cd` to its root (the directory that contains `quota/` and `SETUP.md`).

If you are cloning a remote you were given:

```sh
git clone <url> grokbot-quota
cd grokbot-quota
```

Do not add a remote from this template. Do not push.

## 3. Run the tests

From the repository root:

```sh
python3 -m unittest discover -s tests -v
```

Every test should pass before you point the collector at real data. The tests use temporary directories and the fixtures under `examples/`. They do not read your home directory.

## 4. See which sources exist

```sh
python3 -m quota.detect
```

Read the lines and report which sources exist. Each line is a count and a byte size. The command only lists and stats files. It does not open databases and it does not create directories.

You want to know about:

* `agent-data root` (`PRESENT` or `ABSENT`)
* `search-index.db`
* `agents/audit.jsonl`
* `agents/store.db`
* `agent-transcripts`
* `claude reports`
* `codex sessions`
* `grok sessions`
* `launches.jsonl`

The data root is the first directory that exists among `$QUOTA_DATA_ROOT`, `~/agent-data`, and `/home/*/agent-data` (found from your home directory, not from a hardcoded name). If the root is `ABSENT`, export `QUOTA_DATA_ROOT` to the directory that actually holds `agents/` and `search-index.db`, then run `python3 -m quota.detect` again.

Other paths, when the defaults are wrong:

| Variable | Meaning |
|---|---|
| `QUOTA_DATA_ROOT` | Directory that contains `agents/`, `agent-transcripts/`, `search-index.db` |
| `QUOTA_DB` | Quota sqlite file |
| `QUOTA_OUT` | Dashboard HTML path |
| `QUOTA_REPORTS_GLOB` | Claude `claude-*.jsonl` globs, several joined with `:` |
| `QUOTA_CODEX_SESSIONS` | Codex rollout root (`YYYY/MM/DD/rollout-*.jsonl`) |
| `QUOTA_GROK_SESSIONS` | Grok session root (`*/*/usage.json`) |
| `GROKBOT_QUOTA_STATE` | Directory for `launches.jsonl` and, if needed, the quota db |

If you do not set `QUOTA_DB`, the database is `<data-root>/quota/quota.db` when a data root exists, otherwise under the state directory. The state directory is `$GROKBOT_QUOTA_STATE`, or `~/.local/state/grokbot-quota` when that path can be created, or `~/.local/share/grokbot-quota` when it cannot.

Default Claude reports are `<data-root>/reports/*/claude-*.jsonl` and one directory deeper. Point `QUOTA_REPORTS_GLOB` at your real report tree if it lives somewhere else.

## 5. First collect

From the repository root, with the variables from step 4 exported:

```sh
python3 -m quota.collect --backfill --report
```

`--backfill` repeats passes until the byte backlog is gone (30 seconds and 20 MiB per pass). The summary is printed on stdout. `panel.html` is written next to the database.

A first run on a large history can take many passes. Let it finish. Do not raise the budget to push the work through a model.

## 6. Build the dashboard

```sh
python3 -m quota.dashboard
```

That writes the HTML from `QUOTA_OUT` or, if unset, `dashboard.html` beside the database. The file is self-contained. It does not change the database.

To build a copy with stand-in labels, add `--anonymize`:

```sh
python3 -m quota.dashboard --anonymize --out dashboard.html
```

The database is opened read-only. Names, ids, paths, email addresses, and tailnet hosts are rewritten in memory before the page is rendered. Bot names become `bot-alpha`, `bot-bravo`, and so on. Paths land under `/home/user/...`. If a real identifier would remain, the command exits 1 and does not write the file.

Check a file again with:

```sh
python3 -m quota.leakguard --db "$QUOTA_DB" dashboard.html
```

## 7. Serve the dashboard

`cd` to the directory that contains `dashboard.html` and serve only that directory:

```sh
python3 -m http.server 8765
```

Open `http://127.0.0.1:8765/dashboard.html` on the same machine.

If you want it reachable from your other machines, bind the server to a private network interface you already use. Tailscale is one option. Do not write a real hostname into this repository, the dashboard, or the routine. Stop the server when you are done looking. The weekday routine does not need to serve the file.

## 8. One weekday routine

Create **one** routine. Do not add a second one.

Schedule it on weekdays only, once each weekday. A cron line that does that is:

```
15 8 * * 1-5
```

The hour field must be a single hour, not `*` and not a list. The command is `bin/quota-daily` from this repository (the script `cd`s to the repo root):

```sh
/absolute/path/to/this/repo/bin/quota-daily
```

Put the same environment variables from step 4 on the routine. The script runs:

```sh
python3 -m quota.collect --quiet --report
python3 -m quota.dashboard
```

It prints nothing when both commands exit 0 and no summary line starts with `EXCESSIVE:`. If a command exits non-zero, it prints the captured output and exits 1. If a line starts with `EXCESSIVE:`, it prints those lines.

Do not wrap this in a model call. Do not mail the full summary every day.

## 9. Configure budgets and prices

`config/budgets.json` sets daily and weekly USD limits per tool and per bot. `unattributed` spend counts toward the tool, not toward a bot. A week is the last 7 Lisbon days ending today. Limits you want to change:

* `per_tool.daily_usd` and `per_tool.weekly_usd`
* `per_bot.daily_usd` and `per_bot.weekly_usd`
* `overrides.tools` and `overrides.bots` for one name

Use round numbers you choose. The shipped file is a starting point (20 a day, 75 a week).

`config/prices.json` is USD per 1M tokens for Codex estimates. Keep the `_source` URL and update the date in that string when you re-check it. The shipped rates are the public table from that URL. Unknown Codex models are priced as `default_model` and marked as such.

The next `bin/quota-daily` run picks up both files. You do not need to rerun the backfill for a price or budget edit. Codex estimates are recomputed each pass.

`config/quota.json` ships with `"attribution_plugins": []`. Leave it empty unless you want task to bot mapping. To turn on the example plugin, either:

```json
{
  "attribution_plugins": ["quota.plugins.sqlite_tasks:lookup"],
  "tasks_db": "/path/you/choose/tasks.db"
}
```

or export:

```sh
export QUOTA_ATTRIBUTION_PLUGIN=quota.plugins.sqlite_tasks:lookup
export QUOTA_TASKS_DB=/path/you/choose/tasks.db
```

The sqlite file is the schema in `examples/tasks.sql` (`projects.id`, `projects.name`, `projects.bot_id`, `tasks.id`, `tasks.project_id`). The collector opens it read-only. It does not create the file. With the plugin off, runs that have no launch tag stay `unattributed`.

## 10. Adopt the launch wrapper

When you start a CLI session for a bot, go through the wrapper so the run is tagged:

```sh
bin/grokbot-run bot-alpha JOB-1 -- claude ...
bin/grokbot-run bot-alpha JOB-1 -- codex ...
bin/grokbot-run bot-alpha JOB-1 -- grok ...
```

`bot-alpha` and `JOB-1` are placeholders. Use your own bot id and task id. The wrapper appends one JSON line to `$GROKBOT_QUOTA_STATE/launches.jsonl` (or `~/.local/state/grokbot-quota/launches.jsonl`, falling back to `~/.local/share/grokbot-quota/launches.jsonl`). It stores bot, task, tool, cwd, time, pid, and session id. It does not store arguments or prompts. Then it replaces itself with the tool.

For Claude, if you did not already pass `--session-id`, `--resume`, `-r`, `--continue`, or `-c`, the wrapper inserts a fresh `--session-id`. Codex and Grok are not given a session id.

The next collect reads the new lines and can retag matching runs as `launch_session` or `launch_cwd`.

## Keeping it cheap

* Do not run an LLM over transcripts, audit logs, or session files. Not in bulk, not "to summarize the week," not to attribute bots. Attribution is the plugin or the launch log.
* Collection is incremental. JSONL sources keep a byte cursor, a boundary hash, and an inode. Store and search-index sources keep an id watermark. A pass stops at 30 seconds or 20 MiB. A crash replays the last uncommitted batch.
* Source sqlite files are opened read-only (`mode=ro`). The quota database is the only file this tool writes, plus the HTML reports and `launches.jsonl`.
* One routine per day, weekdays only. Do not add a collector that runs every hour. The excess flag `frequent_automation` exists for that pattern, including for this tracker.
