# Contributing to afair

Thanks for considering a contribution. afair is one shared memory for your AI
tools: an append-only substrate behind a small, frozen MCP surface, with an AI
layer that keeps it structured on top. This guide covers how to set it up, the
rules that keep it coherent, and how to get a change merged.

Read [VISION.md](VISION.md) first if you haven't. The eight invariants in §4
are the project's constitution, and the most important ones for a contributor
are spelled out below.

## The invariants you cannot break

These are non-negotiable. A change that violates one will not be merged, no
matter how good it otherwise is.

- **I1 — the MCP surface is frozen and additive.** The three tools (`remember`,
  `recall`, `observe`) and their shipped signatures never change. You may add
  new optional parameters or new tools, versioned and additive. You may never
  rename, remove, or change the meaning of an existing one. Someone's running
  agent depends on it.
- **I2 — the substrate is append-only and content-addressed.** No `UPDATE`, no
  `DELETE` on the events table (the schema enforces this with triggers). New
  understanding is a new row or a new view, never an edit of the past.
- **I3 — old data stays readable.** No destructive migrations. You add new
  views over the unchanged substrate; you do not rewrite what's already stored.
- **I5 — no provider lock-in.** Model selection is env-driven through litellm.
  No code path may hardcode one AI vendor.

The full set, with reasoning, is in VISION.md §4.

## Local setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/afairai/afair.git
cd afair
uv sync
cp .env.example .env
# add ANTHROPIC_API_KEY (or any provider key); the rest has working defaults
uv run python -m afair
```

The server comes up on `http://127.0.0.1:8765`. Every setting is documented
inline in `.env.example`. A local run needs no auth and no encryption key; both
are required only when `ENVIRONMENT=fly`.

## The checks that must pass

Run all four before you open a pull request. CI runs the same set.

```bash
uv run ruff check          # lint
uv run ruff format         # format (run it; CI checks formatting)
uv run mypy afair          # types (strict)
uv run pytest              # tests
```

## How we work

- **Tests live next to the code** in `__tests__`-style sibling files, not a
  top-level dump. Write the test with the code, not at the end.
- **Bug fixes are test-first.** Add a failing regression test, then fix it.
- **English everywhere** in code, comments, commits, and docs.
- **Conventional commits.** `feat:`, `fix:`, `refactor:`, `docs:`, `test:`,
  `chore:`. Imperative mood, subject under 72 characters.
- **Small pull requests.** One logical change. A PR you can review in one
  sitting beats a sprawling one.

## Opening a pull request

1. Branch off `main` (`feat/...`, `fix/...`, `docs/...`).
2. Make the change, with tests, and get the four checks green.
3. Open the PR with a short summary and a test plan (what you ran, what you
   saw). Screenshots or output for anything observable.
4. A maintainer reviews. Expect questions about the invariants for anything
   that touches the MCP surface, the substrate schema, or the agents.

## Reporting bugs and proposing features

Use the issue templates. For security problems, do **not** open a public issue;
follow [SECURITY.md](SECURITY.md) instead.

## Releasing

Releases use semantic versions (`vMAJOR.MINOR.PATCH`). afair is pre-1.0, so
minor bumps may still carry behavioural change behind the frozen MCP surface;
the surface itself never breaks (Invariant I1).

**Default (automated): release-please.** Conventional Commits on `main` are
gathered into a standing "release PR" that bumps the version (`pyproject.toml`,
`afair/__init__.py`, `CITATION.cff`) and writes the CHANGELOG. Merge that PR and
the rest is automatic: the tag, the GitHub Release, and the fleet deploy. Edit
the PR's CHANGELOG before merging if you want hand-written notes over the
commit-derived ones.

**Manual (fallback): tag it yourself.** The same outcome, in one PR plus one tag:

1. Move the `## [Unreleased]` entries in `CHANGELOG.md` into a new dated section
   `## [X.Y.Z] - YYYY-MM-DD`, leave a fresh empty `## [Unreleased]` on top, and
   add the two link references at the bottom (`compare/...HEAD` and the release
   tag). Keep the notes hand-written and readable; this is what ships as the
   GitHub Release body.
2. Bump the version in `pyproject.toml` and `CITATION.cff` (`version` +
   `date-released`).
3. Merge that to `main`, then tag and push:

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

The `Release` workflow then publishes a GitHub Release with the CHANGELOG
section as its notes, and signals the private control plane (afair-web) to
deploy the fleet pinned to that tag. No manual release step in the GitHub UI.

## License

afair is licensed under AGPLv3. By contributing, you agree that your
contribution is licensed under the same terms. See [LICENSE](LICENSE).
