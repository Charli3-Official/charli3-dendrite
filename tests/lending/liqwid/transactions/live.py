"""A dbsync backend for the live Liqwid builds.

:class:`~charli3_dendrite.backend.dbsync.DbsyncBackend` reads the five ``DBSYNC_*``
variables. :func:`dbsync_backend` fills them from ``DBSYNC_DSN`` when it is set (a
libpq URI or key-value conninfo, optionally quoted or written as a SQLAlchemy driver
URL), so the live builds take the same variables as the fixture capture.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from psycopg.conninfo import conninfo_to_dict

from tests.lending.liqwid.transactions.replay import dbsync_connect_kwargs

if TYPE_CHECKING:
    from charli3_dendrite.backend.dbsync import DbsyncBackend

# DbsyncBackend variable -> libpq conninfo key, with the default when it is absent.
_PARTS = {
    "DBSYNC_HOST": ("host", "localhost"),
    "DBSYNC_PORT": ("port", "5432"),
    "DBSYNC_DB_NAME": ("dbname", "cexplorer"),
    "DBSYNC_USER": ("user", "cexplorer"),
    "DBSYNC_PASS": ("password", ""),
}


def dsn_parts(conninfo: str) -> dict[str, str]:
    """The ``DBSYNC_*`` variables of a conninfo, absent parts at their defaults."""
    parts = conninfo_to_dict(conninfo)
    return {
        variable: str(parts.get(key) or default)
        for variable, (key, default) in _PARTS.items()
    }


def dbsync_backend() -> DbsyncBackend:
    """A dbsync backend on the database the environment names.

    With ``DBSYNC_DSN`` set, its parts replace the ``DBSYNC_*`` variables first;
    otherwise those variables are used as they are.
    """
    from charli3_dendrite.backend.dbsync import DbsyncBackend

    kwargs = dbsync_connect_kwargs()
    if "conninfo" in kwargs:
        os.environ.update(dsn_parts(kwargs["conninfo"]))
    return DbsyncBackend()
