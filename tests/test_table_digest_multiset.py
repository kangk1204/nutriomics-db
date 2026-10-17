import sqlite3

from scripts.verify_explorer_performance_parity import table_digest


def test_full_table_digest_preserves_multiplicity_null_types_and_order_independence():
    with sqlite3.connect(":memory:") as left, sqlite3.connect(":memory:") as right:
        for db in (left, right):
            db.execute("CREATE TABLE x(id TEXT, value, blob BLOB)")
        rows = [("z", None, b"abc"), ("a", 0, b""), ("z", None, b"abc")]
        left.executemany("INSERT INTO x VALUES(?,?,?)", rows)
        right.executemany("INSERT INTO x VALUES(?,?,?)", reversed(rows))
        original = table_digest(left, "x")
        assert original == table_digest(right, "x")
        right.execute("DELETE FROM x WHERE rowid=1")
        assert original != table_digest(right, "x")
        right.execute("INSERT INTO x VALUES('z',0,?)", (b"abc",))
        assert original != table_digest(right, "x")

