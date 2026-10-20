import sqlite3

from migration import migrate


def test_migration() -> None:
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT)")
    con.execute("INSERT INTO users VALUES(1, 'Ada')")
    migrate(con)
    con.execute("UPDATE users SET active=0 WHERE id=1")
    migrate(con)
    assert con.execute("SELECT * FROM users").fetchall() == [(1, "Ada", 0)]
    con.execute("INSERT INTO users(id,name) VALUES(2, 'Bob')")
    assert con.execute("SELECT active FROM users WHERE id=2").fetchone() == (1,)
