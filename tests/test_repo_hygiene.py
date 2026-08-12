"""Repo hygiene guards for the public open-core repository.

The afair core is public (AGPLv3). Local AI-harness hooks have twice appended
business-internal memory dumps (a ``<claude-mem-context>`` block) to tracked
files, which must never reach the public repository. This test makes that
failure mode a red CI run instead of a manual catch.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Markers that identify locally-injected, business-internal content. Each is
# split so this test file does not trip its own guard.
FORBIDDEN_MARKERS = [
    "<claude-mem" + "-context>",
    "claude-mem" + "-context>",
]


def _tracked_text_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    files = []
    for line in out.splitlines():
        p = REPO_ROOT / line
        if p.suffix in {".md", ".txt", ".py", ".toml", ".yml", ".yaml", ".json"} and p.is_file():
            files.append(p)
    return files


def test_no_injected_memory_context_in_tracked_files() -> None:
    """No tracked file may contain a locally-injected memory-context block."""
    offenders: list[str] = []
    for path in _tracked_text_files():
        if path == Path(__file__).resolve():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for marker in FORBIDDEN_MARKERS:
            if marker in text:
                offenders.append(str(path.relative_to(REPO_ROOT)))
                break
    assert not offenders, (
        "business-internal memory-context blocks found in tracked files "
        f"(revert them before committing): {offenders}"
    )
