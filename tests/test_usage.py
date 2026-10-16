"""Manual usage readings persist, replace, and validate via the CLI."""
import sqlite3

from quota import usage


def test_reading_cli(tmp_path, capsys):
    path = str(tmp_path / 'quota.db')
    assert usage.main(['add', '2026-10-16', '40', '--db', path]) == 0
    with sqlite3.connect(path) as db:
        first = db.execute('SELECT usage_pct, recorded_at_ms FROM usage_readings').fetchone()
        assert first[0] == 40 and first[1] > 0
    assert usage.main(['--db', path, 'add', '2026-10-16', '42']) == 0
    assert usage.main(['list', '--db', path]) == 0
    assert '2026-10-16\t42\t' in capsys.readouterr().out
    for pct in ('nan', 'inf', '-1', '101'):
        assert usage.main(['add', '2026-10-17', pct, '--db', path]) == 2
    assert usage.main(['add', 'invalid', '40', '--db', path]) == 2
    assert usage.main(['fit', '--db', path]) == 0
    assert 'need 7 more readings' in capsys.readouterr().out
    assert usage.main(['rm', '2026-10-16', '--db', path]) == 0
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM usage_readings').fetchone()[0] == 0
