"""Suite-wide isolation for process-global substrate state.

The production server installs one vault key for the lifetime of its process.
Tests, however, intentionally exercise both encrypted and plaintext vaults in
the same pytest process.  Reset the module default around every test so a test
that builds an encrypted server cannot make unrelated later fixtures attempt a
SQLCipher open on macOS, where the Linux-only wheel is absent.

Per-test explicit keys still work unchanged between setup and teardown.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

# litellm's import runs ``load_dotenv()`` unless LITELLM_MODE is non-"DEV",
# and python-dotenv walks parent directories — from a checkout it finds the
# developer's real repo ``.env`` and copies every key into ``os.environ``
# for the rest of the process. Tests mock ``call_tool``, so litellm never
# loads at collection time; the first REAL import happens mid-suite (e.g.
# inside the extraction-retry tests), silently repointing later
# ``Settings()`` instances (even with ``_env_file=None``) at the
# developer's env — e.g. an ambient CANONICALIZER_MODEL makes the Sonnet
# escalation unmappable and fails ``test_sonnet_escalation_on_low_confidence``
# only when another module imported litellm first. An ordering-dependent
# failure CI can't see because it has no ambient ``.env``. Must be set
# before the first litellm import anywhere in the process, hence at
# conftest import time. ``setdefault`` keeps an operator override working.
os.environ.setdefault("LITELLM_MODE", "PRODUCTION")

from afair.substrate.db import set_vault_key

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _isolate_process_vault_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Never leak the real/local or module-level key into a test vault.

    An explicit empty environment value intentionally shadows a developer's
    repository ``.env``. Individual encryption/boot tests remain free to set a
    real test key with their own ``monkeypatch`` fixture.
    """
    monkeypatch.setenv("AFAIR_VAULT_KEY", "")
    set_vault_key(None)
    try:
        yield
    finally:
        set_vault_key(None)
