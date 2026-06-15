# Changelog

All notable changes to afair are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it reaches
1.0.

afair is pre-1.0. The MCP surface (`remember`, `recall`, `observe`) is already
frozen and additive per Invariant I1; everything behind it can still move.
Per-feature detail and dates live in
[`analysis/build-log.md`](analysis/build-log.md).

## [Unreleased]

## [0.1.0] - 2026-06-15

### Added

- **The substrate.** Append-only, content-addressed SQLite with FTS5 and
  sqlite-vec for semantic recall. Encrypted at rest (SQLCipher on the database,
  AES-256-GCM on filesystem blobs). Append-only is enforced by database triggers,
  not just convention (Invariant I2).
- **The MCP surface.** The three frozen verbs — `remember`, `recall`, `observe`
  — over Streamable HTTP. `recall` is the single retrieval verb (survey,
  by-id, and full-payload modes are arguments). Auth is OAuth 2.1 (dynamic
  client registration + PKCE) or a scoped bearer token.
- **Multi-modal memory.** Text, PDF (text extraction), audio (transcription),
  and images (vision), plus streaming blob upload and atomic compound events.
- **Cold-path agents.** A warm-path extractor, a salience scorer, a CEN/DMN
  mode switcher, a per-hit surprise score, and an emergent entity graph built by
  a canonicalizer over five append-only tables. No fixed ontology (Invariant I6).
- **Recursive self-improvement.** A tuner proposes bounded parameter changes; a
  multi-vendor LLM judge panel (Anthropic + OpenAI + Google) and per-worker guard
  suites vet them; an auto-rollback monitor reverts a change that degrades
  quality. The MCP surface is exempt from self-modification (Invariant I7).
- **Async vault export** as a background job with an emailed download link, and
  full export of the substrate at any time (Invariant I4).
- **Self-hosting.** `docs/self-hosting.md` and `fly.toml.example` for running
  your own instance; a complete 34-variable `.env.example` reference.
- **Open-source scaffolding.** `CONTRIBUTING.md`, `SECURITY.md`,
  `CODE_OF_CONDUCT.md`, `CITATION.cff`, and issue + pull-request templates.
- **Operational tooling.** `scripts/check_secrets.py`, a pre-deploy guard that
  fails fast and named when a deployment target is missing a boot-required
  secret.

### Changed

- **Licensing decided: AGPLv3** for the open-source core (`LICENSE`,
  `VISION.md` §12).
- **Open-core deploy split.** This repository runs CI only (lint, types, tests).
  The hosted fleet is deployed from the private control plane, pinned to a
  release ref, so the product repository never deploys operator infrastructure.

### Security

- Secrets are validated at boot — the server refuses to start in production
  without an authentication token and a vault encryption key.
- Per-vault encryption at rest; a stolen volume snapshot is useless without the
  key.
- Prompt-injection defenses across the LLM cold-path workers; narrowly scoped
  bearer tokens for the signup and export side-channels; rate limiting on
  authentication endpoints.

[Unreleased]: https://github.com/afairai/afair/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/afairai/afair/releases/tag/v0.1.0
