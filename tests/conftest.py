"""Shared pytest fixtures.

The autouse ``_reset_principal_framing`` fixture keeps the module-level
principal framing (ADR-0010) pinned to the person default between tests. The
framing is a process-wide singleton set once at server boot; a test that
installs an organization framing (or a ``build_server`` call with org settings)
would otherwise leak that framing into every subsequent test in the same
process and silently break the person-default byte-identity assumptions the rest
of the suite (and the golden) rely on.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from afair.agents.framing import reset_current

# litellm's import runs ``load_dotenv()`` unless LITELLM_MODE is non-"DEV",
# and python-dotenv walks parent directories — from a checkout it finds the
# developer's real repo ``.env`` and copies every key into ``os.environ``
# for the rest of the process. Tests mock ``call_tool``, so litellm never
# loads at collection time; the first REAL import happens mid-suite on the
# server's boot-warmup daemon thread, silently repointing later
# ``Settings()`` instances (even with ``_env_file=None``) at the
# developer's env — e.g. an ambient AFAIR_VAULT_KEY suddenly makes every
# later test try a SQLCipher open. An ordering-dependent failure CI can't
# see because it has no ambient ``.env``. Must be set before the first
# litellm import anywhere in the process, hence at conftest import time.
# ``setdefault`` keeps an explicit operator override working.
os.environ.setdefault("LITELLM_MODE", "PRODUCTION")

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _reset_principal_framing() -> Iterator[None]:
    """Reset the active principal framing to the person default around each test."""
    reset_current()
    try:
        yield
    finally:
        reset_current()
