import pytest

from collector import db
from collector.scheduler import FileLock


@pytest.fixture
def con(cfg):
    c = db.connect(cfg.db_path)
    c.execute("""INSERT INTO devices (name, type, in_inventory) VALUES
                 ('YP1', 'Parking', true), ('YW1', 'Wastebin', true), ('YL1', 'Lighting', true)""")
    yield c
    c.close()


def test_telemetry_deduplicates_on_device_key_ts(con):
    rows = [("YP1", "status", 1000, "1"), ("YP1", "status", 1000, "1"),
            ("YP1", "vbatt", 1000, "3.6")]
    assert db.insert_telemetry(con, rows, "latest") == 2
    assert db.insert_telemetry(con, rows, "stream") == 0
    got = con.execute(
        "SELECT key, epoch_ms(ts), value, value_num, source FROM telemetry ORDER BY key").fetchall()
    assert got == [("status", 1000, "1", 1.0, "latest"), ("vbatt", 1000, "3.6", 3.6, "latest")]


def test_values_with_quotes_and_json_survive(con):
    raw = '{"last_volt":235,"note":"it\'s \\"fine\\""}'
    db.insert_telemetry(con, [("YL1", "powerstats", 5, raw)], "latest")
    assert con.execute("SELECT value FROM telemetry").fetchone()[0] == raw


def test_state_changes_from_value_changes(con):
    rows = [("YP1", "status", t, v) for t, v in
            [(1000, "1"), (2000, "1"), (3000, "0"), (4000, "1")]]
    db.store_telemetry(con, rows, "latest", db.device_types(con))
    got = con.execute("SELECT epoch_ms(ts), old_state, new_state FROM state_changes "
                      "ORDER BY ts").fetchall()
    assert got == [(3000, "occupied", "free"), (4000, "free", "occupied")]


def test_state_changes_recomputed_when_older_rows_arrive(con):
    types = db.device_types(con)
    db.store_telemetry(con, [("YW1", "level", 5000, "85")], "latest", types)
    db.store_telemetry(con, [("YW1", "level", 3000, "20"), ("YW1", "level", 4000, "50")],
                       "backfill", types)
    got = con.execute("SELECT epoch_ms(ts), old_state, new_state FROM state_changes "
                      "ORDER BY ts").fetchall()
    assert got == [(4000, "low", "medium"), (5000, "medium", "full")]


def test_untracked_keys_make_no_state_changes(con):
    db.store_telemetry(con, [("YP1", "vbatt", 1, "3.6"), ("YP1", "vbatt", 2, "3.5")], "latest",
                       db.device_types(con))
    assert con.execute("SELECT count(*) FROM state_changes").fetchone()[0] == 0


def test_last_telemetry_only_moves_forward_and_ignores_platform_keys(con):
    types = db.device_types(con)
    db.store_telemetry(con, [("YP1", "status", 5000, "1")], "latest", types)
    db.store_telemetry(con, [("YP1", "status", 3000, "1"), ("YP1", "active", 9000, "false")],
                       "latest", types)
    assert con.execute("SELECT epoch_ms(last_telemetry) FROM devices WHERE name = 'YP1'"
                       ).fetchone()[0] == 5000


def test_watermarks(con):
    assert db.get_watermark(con, "stream") is None
    db.set_watermark(con, "stream", 123)
    db.set_watermark(con, "stream", 456)
    assert db.get_watermark(con, "stream") == 456


def test_connect_is_idempotent(cfg):
    db.connect(cfg.db_path).close()
    db.connect(cfg.db_path).close()
    with db.session(cfg.db_path, read_only=True) as c:
        assert c.execute("SELECT count(*) FROM telemetry").fetchone()[0] == 0


def test_file_lock_is_exclusive(tmp_path):
    a, b = FileLock(tmp_path / "x.lock"), FileLock(tmp_path / "x.lock")
    assert a.acquire()
    assert not b.acquire()
    a.release()
    assert b.acquire()
    b.release()
