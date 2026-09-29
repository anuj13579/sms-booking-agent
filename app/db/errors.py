"""Recognise which database rule rejected a write.

The booking service relies on Postgres constraints for correctness (D-006) and reacts to *which*
one fired: an exclusion violation means "this technician is taken, try the next one"; the
idempotency index means "this exact booking already exists, return it".

It also has to recognise deadlocks (D-026). Unlike a unique index, an exclusion constraint has
no special protocol for concurrent inserts: each inserter adds its row, then waits for any
in-progress conflicting row to commit or abort. Two transactions whose uncommitted rows conflict
with each other wait on each other, and Postgres aborts one of them after ``deadlock_timeout``.
"""

from sqlalchemy.exc import DBAPIError

EXCLUSION_VIOLATION = "23P01"
UNIQUE_VIOLATION = "23505"
DEADLOCK_DETECTED = "40P01"


def _driver_error(exc: DBAPIError) -> BaseException | None:
    # SQLAlchemy wraps the asyncpg exception in a DBAPI-style adapter; the original asyncpg
    # error (which carries sqlstate and constraint_name) is its __cause__.
    orig = exc.orig
    return getattr(orig, "__cause__", None) or orig


def sqlstate(exc: DBAPIError) -> str | None:
    err = _driver_error(exc)
    return getattr(err, "sqlstate", None) or getattr(exc.orig, "sqlstate", None)


def constraint_name(exc: DBAPIError) -> str | None:
    err = _driver_error(exc)
    return getattr(err, "constraint_name", None)


def is_violation(exc: DBAPIError, name: str) -> bool:
    return constraint_name(exc) == name


def is_deadlock(exc: DBAPIError) -> bool:
    return sqlstate(exc) == DEADLOCK_DETECTED
