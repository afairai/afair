"""Suite-wide isolation for process-global substrate state.

The production server installs one vault key for the lifetime of its process.
Tests, however, intentionally exercise both encrypted and plaintext vaults in
the same pytest process.  Reset the module default around every test so a test
that builds an encrypted server cannot make unrelated later fixtures attempt a
SQLCipher open on macOS, where the Linux-only wheel is absent.

Per-test explicit keys still work unchanged between setup and teardown.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

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
