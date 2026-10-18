"""Job bot. Two kinds of folder:

APP   the program and the lists every account starts from (this folder's parent).
DATA  everything that must be kept: the one database (jobbot.db, see db.py), the service's keys, and
      every account's home. It is APP itself unless the JOBBOT_DATA environment variable names another
      folder, which is how a server keeps its data on a disk that outlives the program.
home  one account's own files: generated resumes, reports, logs, uploads and mail passwords. Its rows are
      in the one database; the home also tells db.py which account is being served.

The first account (the owner's) has its home in DATA itself. Which home is in use comes from, in
order: `use_home()` (the API sets it per request), the JOBBOT_HOME environment variable (set for
each scheduled run), then DATA."""
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("JOBBOT_DATA") or APP).resolve()
_home = ContextVar("jobbot_home", default=None)


def home():
    """The data folder of the account being served right now."""
    return _home.get() or Path(os.environ.get("JOBBOT_HOME") or DATA)


@contextmanager
def use_home(path):
    """Serve one account for the length of a `with` block (this thread or task only)."""
    token = _home.set(Path(path))
    try:
        yield
    finally:
        _home.reset(token)


class HomePath:
    """A path inside the current account's home, worked out each time it is used, so one name such
    as `OUTPUT` always means the folder of whichever account is being served."""

    def __init__(self, *parts):
        self._parts = parts

    def _now(self):
        return home().joinpath(*self._parts)

    def __fspath__(self):
        return str(self._now())

    def __str__(self):
        return str(self._now())

    def __repr__(self):
        return f"HomePath({self._now()!s})"

    def __truediv__(self, other):
        return self._now() / other

    def __getattr__(self, name):
        return getattr(self._now(), name)


def starting(*parts):
    """A file an account starts from: its own copy if it has one, else the neutral one in APP/defaults."""
    own = home().joinpath(*parts)
    return own if own.exists() else APP.joinpath("defaults", *parts)
