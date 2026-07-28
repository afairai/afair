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

from typing import TYPE_CHECKING

import pytest

from afair.agents.framing import reset_current

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
