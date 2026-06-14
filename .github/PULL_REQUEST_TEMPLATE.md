<!--
Thanks for contributing. Keep the PR to one logical change, and make sure the
four checks pass: ruff check, ruff format, mypy afair, pytest.
-->

## Summary

<!-- What does this change and why? One to three bullets. -->

## Test plan

<!-- What did you run, and what did you see? Commands + output / screenshots. -->

## Checklist

- [ ] Tests added or updated, and `uv run pytest` is green
- [ ] `uv run ruff check` and `uv run ruff format` are clean
- [ ] `uv run mypy afair` passes
- [ ] No shipped MCP signature changed (additive only, per Invariant I1)
- [ ] No edits to past substrate data (append-only, per I2/I3)
- [ ] Docs updated if behaviour or configuration changed
