# Changelog

All notable changes to afair are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it reaches
1.0.

afair is pre-1.0. The MCP surface (`remember`, `recall`, `observe`) is already
frozen and additive per Invariant I1; everything behind it can still move.
Per-feature detail and dates live in
[`analysis/build-log.md`](analysis/build-log.md).

## [0.1.1](https://github.com/afairai/afair/compare/v0.1.0...v0.1.1) (2026-06-27)


### Features

* **agents:** entity-audit worker proposes corrections for confirmation ([7faa818](https://github.com/afairai/afair/commit/7faa8185abe7c904e315f8c288c456ad0a88f960))
* **agents:** review cross-kind auto-merges instead of fuzzy name-matches ([322df8d](https://github.com/afairai/afair/commit/322df8de2cee60f012e03b8b542974bcbdc6a62e))
* **mcp:** operator-confirmation surface for entity-audit proposals ([91f391f](https://github.com/afairai/afair/commit/91f391f9bebbd44ae1474e76a4d2d055f1012027))
* **prompts+readme:** state the whole-life breadth positively, drop negations ([320fe72](https://github.com/afairai/afair/commit/320fe72676777e3c2be59fa44a5e78ce9ff84c2c))
* **prompts:** frame afair as whole-life memory, not a work tool ([e81a918](https://github.com/afairai/afair/commit/e81a918f6ea24225e3e3b0685c746e82180a775e))
* **recall:** mark each surfaced edge with its trust state (ADR-0002) ([44d36d8](https://github.com/afairai/afair/commit/44d36d8f1217e359b367a61f323bb378478349f3))
* **scripts:** rebuild_vault --drop-superseded for current-truth replays ([d4f4627](https://github.com/afairai/afair/commit/d4f462720241216d88711dacb5936eeb331605e5))
* **scripts:** rebuild_vault — replay source events into a fresh derived layer ([9497faf](https://github.com/afairai/afair/commit/9497fafefb049d22ead3d115e666bb388f25d220))
* **substrate:** belief-revision foundation for the derived layer (ADR-0002) ([8e5d872](https://github.com/afairai/afair/commit/8e5d8726bd06d3b56fbbed8151a7f0bcb565b400))
* **substrate:** entity retraction — withdraw noise from the live graph ([9b186bf](https://github.com/afairai/afair/commit/9b186bf2e0b0f87d0c8503cb54e2c9d23a1b3035))
* **substrate:** garbage-collect orphaned object-store blobs ([f820023](https://github.com/afairai/afair/commit/f820023ccff612a08f377ab682de9d5a8500dc53))
* **substrate:** retype_entity — append-only entity re-typing (ADR-0002) ([9e688ad](https://github.com/afairai/afair/commit/9e688ade00408fbdc32407f544f569a981a682b6))


### Bug Fixes

* **agents:** ground entity-graph relations in verbatim evidence ([9e2f4f8](https://github.com/afairai/afair/commit/9e2f4f80dbcf5d39f911c8483d34bc17f34753ce))
* **ci:** derive release-please PR branch from action output ([907312f](https://github.com/afairai/afair/commit/907312f50483d8dfe69de6286d86bb9ec61a8335))
* **ci:** keep release-please in 0.x and pass PR output via env ([a4f59ee](https://github.com/afairai/afair/commit/a4f59ee003570badc702b1bd6e9840e3a0808bf6))
* **install:** make the one-command installer work for local self-host ([d9f4c70](https://github.com/afairai/afair/commit/d9f4c70149ee7a35615b088914e83cc30d337cae))
* **scripts:** phase rebuild_vault cold path to stop the article runaway ([510e1a0](https://github.com/afairai/afair/commit/510e1a0ed05f6827f8dd7c5cc8360b197490e152))
* **scripts:** set the vault key before opening the encrypted vault in rebuild_vault ([806c3f3](https://github.com/afairai/afair/commit/806c3f3ed1fcd9cf01b26f453a137ba903727f12))
* **substrate:** make spilled text-large content searchable ([1ee0e90](https://github.com/afairai/afair/commit/1ee0e9083a0ba69ddf3dd53a8450bea3ca97db1c))
* **substrate:** report plaintext size_bytes for stored blobs ([94d4125](https://github.com/afairai/afair/commit/94d412564aa83b4c167197bfff5eb34b8be3e7f7))


### Reverts

* **docs:** drop sovereign-inference section (exploratory query, not a request) ([0a85bf2](https://github.com/afairai/afair/commit/0a85bf281022d319ee1d9e26e7fa6badc85ffd66))


### Documentation

* **adr:** record the as-built entity-audit + decide surface in ADR-0002 ([dbb34ec](https://github.com/afairai/afair/commit/dbb34ece42ab32317acc8584ab8b1ca6c25d66b0))
* **adr:** record the rationale for the constitutional invariants ([42a4030](https://github.com/afairai/afair/commit/42a40309479ff9f4489141122c0f5d45f18fe799))
* align CONTRIBUTING intro with current positioning ([186c952](https://github.com/afairai/afair/commit/186c95272799f7cb0a9eb476e482d0973f6b6e0c))
* **claude:** correct the operator vault host reference ([ee02645](https://github.com/afairai/afair/commit/ee02645a0f76f00ccfd57218e41694d6d9860c11))
* make the connect + hosting story self-host-ready for going public ([3e071b2](https://github.com/afairai/afair/commit/3e071b2e198a8ab1c80f0a235ba881ba4ac93ba3))
* **readme:** surface the instruction snippet, add CI badge, fix snippet drift ([8652759](https://github.com/afairai/afair/commit/8652759e7073fd325e164cc6b462f664940dee12))
* **self-host:** add Docker/compose path, a verify step, and a README doc index ([b306745](https://github.com/afairai/afair/commit/b30674510ea52fdb426ff541182a48c9f7c2fb3f))
* **self-hosting:** document sovereign / local / decentralized inference ([e988a08](https://github.com/afairai/afair/commit/e988a08facb3afb075c51bea105f2bf134702d5d))
* **self-host:** make GitHub OAuth a first-class self-host path, not legacy ([217b5be](https://github.com/afairai/afair/commit/217b5bee7a9f5121e564e52fbadc6fa38b2c7183))
* sharpen README intro and VISION why-now to current positioning ([8086c79](https://github.com/afairai/afair/commit/8086c7913aeb8cb3ea0549bd0e486eb628833503))

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
