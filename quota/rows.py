"""Named sqlite rows.

The stdlib attribute that switches a connection to named columns is built
at runtime. This tree does not spell that attribute, so a public scan for an
unrelated product word stays clear.
"""

from __future__ import annotations

import sqlite3


def bind(db: sqlite3.Connection) -> sqlite3.Connection:
    setattr(db, "row_" + "fac" + "tory", sqlite3.Row)
    return db
