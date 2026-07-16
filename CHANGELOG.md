# Changelog

All notable changes to afair are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it reaches
1.0.

afair is pre-1.0. The MCP surface (`remember`, `recall`, `observe`) is already
frozen and additive per Invariant I1; everything behind it can still move.
Per-feature detail and dates live in
[`analysis/build-log.md`](analysis/build-log.md).

## [0.1.23](https://github.com/afairai/afair/compare/v0.1.22...v0.1.23) (2026-07-16)


### Bug Fixes

* **conflicts:** protect the resolution record + self-heal orphaned proposals ([95942a2](https://github.com/afairai/afair/commit/95942a2d37af4a69d9f30a20a3b22142ac942fdc))


### Documentation

* reconcile I2 wording with the interpretations append-only triggers ([384de23](https://github.com/afairai/afair/commit/384de23e0269b9f6a23cea4f58fcd9a9c8448a20))

## [0.1.22](https://github.com/afairai/afair/compare/v0.1.21...v0.1.22) (2026-07-16)


### Features

* **conflicts:** operator-resolvable synthesis conflicts (Phase 2, ADR-0008) ([4743c36](https://github.com/afairai/afair/commit/4743c36d59e18c84a773b049ff64f58ce53f226e))
* **mcp:** operator decide surface over the correction queue (Phase 1) ([de61b1c](https://github.com/afairai/afair/commit/de61b1c854b04f80024330349328e01d3b5d94cc))


### Bug Fixes

* **conflicts:** pair-scoped resolution identity + claim-first decide race ([72f2ca8](https://github.com/afairai/afair/commit/72f2ca87ed3bbed645c202687967bca13dac567f))


### Documentation

* **adr:** ADR-0008 operator conflict resolution ([f5c44b7](https://github.com/afairai/afair/commit/f5c44b7d64c46e7ba829e5eb384f3582d5d33df1))

## [0.1.21](https://github.com/afairai/afair/compare/v0.1.20...v0.1.21) (2026-07-14)


### Bug Fixes

* **synthesis:** reconcile crash-duplicate syntheses on the unchanged path ([f880554](https://github.com/afairai/afair/commit/f8805545995b298991efa096a4679b7f48dae8de))

## [0.1.20](https://github.com/afairai/afair/compare/v0.1.19...v0.1.20) (2026-07-14)


### Bug Fixes

* **import:** clamp a future-dated created_at to now ([e1912d0](https://github.com/afairai/afair/commit/e1912d0129201ea09681573bc5ba99327c856fb9))
* **synthesis:** require a citation on every key point, not just facts ([f621af8](https://github.com/afairai/afair/commit/f621af8f56f034c21ff71e505c916f731c76d592))

## [0.1.19](https://github.com/afairai/afair/compare/v0.1.18...v0.1.19) (2026-07-14)


### Features

* **dashboard:** add read-only Memory Mirror projection ([004b64f](https://github.com/afairai/afair/commit/004b64f49ac7b0cfc0cbc7e1c98e1ef071b9d52a))
* **import:** accept user-initiated vault imports ([bd1f660](https://github.com/afairai/afair/commit/bd1f660631a9ed188f4c7ef259216a3f5de97175))
* **recall:** prefer current living syntheses over legacy entity articles ([f79eb48](https://github.com/afairai/afair/commit/f79eb483de4bd21c9a88da71eea9c97a988ba740))
* **server:** wire living-synthesis worker and import/mirror routes ([8213788](https://github.com/afairai/afair/commit/8213788ac6ca0cd4559ed73e3c6173cd7826315a))
* **synthesis:** discover and maintain cited living syntheses (ADR-0007) ([076625d](https://github.com/afairai/afair/commit/076625d3b02c0920bba81af15e4d9742e69c9e9d))


### Documentation

* living syntheses, product boundaries, import and dashboard ([4c4fb0f](https://github.com/afairai/afair/commit/4c4fb0f0fdaa8eeb608f3c81cfe138e4e530a929))

## [0.1.18](https://github.com/afairai/afair/compare/v0.1.17...v0.1.18) (2026-07-08)


### Bug Fixes

* **mcp:** extend stringified-arg tolerance to invalidates + parent_hashes ([344f649](https://github.com/afairai/afair/commit/344f6492a45739e1a909572e670f86d1427059b0))


### Documentation

* **adr:** ADR-0006 event provenance ([9e088a6](https://github.com/afairai/afair/commit/9e088a6782b7ebe4ff8a32f7515862d5f4eeb7af))
* **clients:** document asserted_by remember field ([485edeb](https://github.com/afairai/afair/commit/485edeb52e07d7d005cb45fcedce4a0ab3a4e638))
* refresh CLAUDE.md state + registry for v0.1.17 ([68d7543](https://github.com/afairai/afair/commit/68d7543f3e438bb3d0e352b6e295717a3d6ac972))

## [0.1.17](https://github.com/afairai/afair/compare/v0.1.16...v0.1.17) (2026-07-06)


### Features

* **provenance:** server-authoritative client provenance sidecar (W1) ([f3fc124](https://github.com/afairai/afair/commit/f3fc1249d8c1c82492b58ebed13d2bd5b7c09366))
* **recall:** surface durability rationale at verbosity=full (W2) ([e4b0f1d](https://github.com/afairai/afair/commit/e4b0f1d91e38a562f4c57f6d471d8607e08e9305))
* **remember:** optional asserted_by field feeding entrenchment (W3) ([1f54462](https://github.com/afairai/afair/commit/1f54462d794283b1c0c7df9c27a3558e103e4a8f))

## [0.1.16](https://github.com/afairai/afair/compare/v0.1.15...v0.1.16) (2026-07-06)


### Features

* **compound:** harden compound-content intake and re-interpretation ([c0ef416](https://github.com/afairai/afair/commit/c0ef4164ddaf6c2ed1d94308e6473d0b6a51113c))
* **oauth:** host canonicalization + refresh-token rotation, middleware tests ([2381468](https://github.com/afairai/afair/commit/2381468f0aeeb3dc051cc5346b5959f28874533b))

## [0.1.15](https://github.com/afairai/afair/compare/v0.1.14...v0.1.15) (2026-07-06)


### Features

* **audit:** suppress structural-junk kind-review proposals ([2eef552](https://github.com/afairai/afair/commit/2eef5528b923cc5f478ecd4423b0ea29e11cd544))
* **edges:** gate observe/transient/vague-predicate edge derivation + retro-sweep ([8b3754d](https://github.com/afairai/afair/commit/8b3754d02167c3b4a23c8fbbbfac6b8942416845))
* **edges:** serve-gated reviews + auto-expiry of never-served low-confidence edges ([dd93ca3](https://github.com/afairai/afair/commit/dd93ca3161902a8bf9c20b3cb238318c76cfdc3a))


### Bug Fixes

* **audit:** require both sides structural to suppress a merge review ([259a621](https://github.com/afairai/afair/commit/259a62124282057467bea3a2a0aba90301d95211))
* **corrections:** honor to_kind on confirm, not only reject ([0071712](https://github.com/afairai/afair/commit/0071712fa3c3682588ee2f4021b8b646b9cd2e25))
* **edges:** drop involve/retain from the non-durable predicate denylist ([f9cd80c](https://github.com/afairai/afair/commit/f9cd80c62a294dc0fe8ffc29749e9352e63d5f89))
* **edges:** gate auto-expiry behind a serve-tracking epoch so deploy can't mass-retire the legacy graph ([378b07a](https://github.com/afairai/afair/commit/378b07a6e18c960e1bb1b7e81fee847a0430f523))

## [0.1.14](https://github.com/afairai/afair/compare/v0.1.13...v0.1.14) (2026-07-06)


### Bug Fixes

* **agents:** keep distinct mode transitions from deduping to one event ([a1e02d7](https://github.com/afairai/afair/commit/a1e02d72dda208ac0671f6f1a5ced0077f664e37))
* **agents:** point symmetric conflict flag at the counterpart event id ([b4e27f1](https://github.com/afairai/afair/commit/b4e27f1df54ed793f04c2da72bc40024d33903ba))
* **agents:** stop a failed interpretation shadowing an earlier success ([9477d12](https://github.com/afairai/afair/commit/9477d126bded70c814c7ec07aa296b80a1c4e89f))
* **agents:** stop KeyError misclassifying as an LLM auth error ([aa17451](https://github.com/afairai/afair/commit/aa174510a212ffd7b5ca5b0441d4ddd7433a2a29))
* **agents:** store source event hashes in entity_article parent_hashes ([60cdb52](https://github.com/afairai/afair/commit/60cdb5245da705c8f4556fea71ce96f5bbdeefbc))
* **mcp:** normalize naive timestamps in recall recency rerank ([3284906](https://github.com/afairai/afair/commit/3284906016c86068a1e8ef6ce9aa676385bd4035))
* **mcp:** restore clean tool inputSchema; keep stringified-param tolerance ([b0b0543](https://github.com/afairai/afair/commit/b0b05436261f13e019fab1166031ba6184ed40a7))
* **mcp:** truncate-preserve over-long remember context and type_hint ([74860fd](https://github.com/afairai/afair/commit/74860fd5f452ec06e238f62ba2e64228f4f559e0))
* **mcp:** validate remember invalidation targets before content write ([a950616](https://github.com/afairai/afair/commit/a950616d295f08682c78de6c5b9580a41dc3feaa))

## [0.1.13](https://github.com/afairai/afair/compare/v0.1.12...v0.1.13) (2026-07-06)


### Features

* **agents:** worker watermarks to stop cursorless re-scans (P2a) ([19da60d](https://github.com/afairai/afair/commit/19da60da4015a1175b0f6b360c2630fc6257d2dd))
* **substrate:** ADR-0005 — prune telemetry, not memory ([2d64a96](https://github.com/afairai/afair/commit/2d64a963d9a15a1cb617b7eb517e2aa350a11715))


### Bug Fixes

* **watermarks:** lag the frontier so concurrent pre-minted ids can't be skipped ([9a3c5c2](https://github.com/afairai/afair/commit/9a3c5c26aa00deee0300476a2ad2bd375a3950dc))


### Performance Improvements

* **mcp:** cap anyio thread limiter to bound page-cache memory (P2a) ([ea6e09b](https://github.com/afairai/afair/commit/ea6e09b5bfcc16a9f687668ccde4cd1f8ece4e63))
* **mcp:** UNION entity-match rewrite + session-start salient index (P2a) ([d9cd5d9](https://github.com/afairai/afair/commit/d9cd5d91e9ec11babf922d32920eabc2933ae311))

## [0.1.12](https://github.com/afairai/afair/compare/v0.1.11...v0.1.12) (2026-07-06)


### Features

* **corrections:** open-row partial unique, detector guards, edge_review prune ([60ce528](https://github.com/afairai/afair/commit/60ce528743b65fee765da05131f52d940a5c8442))
* **dedup:** auto-confirm floor + referent-aware kind guidance ([29dc2ba](https://github.com/afairai/afair/commit/29dc2ba0fd4f4ab32f30e88d78940aa41e10da01))
* **mcp:** batch decide + pending-queue pagination on recall ([40a4e38](https://github.com/afairai/afair/commit/40a4e38618665eae6a33316cebe9fec6770adc7e))
* **mcp:** null-free ToolResult serialization + extractor output caps ([82c64a4](https://github.com/afairai/afair/commit/82c64a45d77fec4343ca2bc4e9ed363eeb4cd6e8))
* **mcp:** recall verbosity levels, limit cap, cursor pagination ([83e3d9c](https://github.com/afairai/afair/commit/83e3d9c0435b3c5b3f8f59bc5246999e75a157aa))


### Bug Fixes

* **mcp:** cap compact conflicts + review nits ([40f76b7](https://github.com/afairai/afair/commit/40f76b70d6c9baf4833d14d36bbe10feaae29b03))
* **mcp:** terminate cursor pagination + clamp pending_limit lower bound ([21b3ba1](https://github.com/afairai/afair/commit/21b3ba1280f75a5f5d70b5ac88048ea4e680ead7))

## [0.1.11](https://github.com/afairai/afair/compare/v0.1.10...v0.1.11) (2026-07-06)


### Bug Fixes

* **canonicalizer:** stop failed extractions starving event selection ([98412e6](https://github.com/afairai/afair/commit/98412e6bd526b4b05537420160c559b2969fbd98))
* **edge-scorer:** page the review-candidate pool so no subject starves ([2c78d60](https://github.com/afairai/afair/commit/2c78d60fd127c9e8fe87089ecdd8bff1f1c9c89c))
* **extractor:** record extraction.failed in every failure branch ([66dc6a6](https://github.com/afairai/afair/commit/66dc6a6f40ec5f5004b081af5851dcf33615f451))
* **mcp:** exact-key allowlist + cap for observe preserved fields ([5893583](https://github.com/afairai/afair/commit/589358397c6b542eb9ebaf02927631fbfe68720b))
* **mcp:** observe intake truncates oversized/brace-heavy extras ([9f73ba1](https://github.com/afairai/afair/commit/9f73ba1a6bdc4db790e8783eec2958785db56e33))
* **observability:** guard misleading extraction.failed trace + stable open_threads order ([b68b665](https://github.com/afairai/afair/commit/b68b665946d8c55a90ad6759ce84bc64329465a4))
* **session-start:** read open_threads from consolidation events ([a6c081a](https://github.com/afairai/afair/commit/a6c081ad3613dad43bdc6dbfec4a7cf71cbee782))
* **substrate:** chunk IN-list batch helpers, subquery recall entity-match ([db04833](https://github.com/afairai/afair/commit/db048330fbd17d5ebf145b4ff0841121a5baaeed))


### Performance Improvements

* **substrate:** index LOWER(predicate), batch corroboration, SQL edge-review pool ([e42eb55](https://github.com/afairai/afair/commit/e42eb55d7fe65aa44c2e2b9a0f3960f9dac0da97))

## [0.1.10](https://github.com/afairai/afair/compare/v0.1.9...v0.1.10) (2026-07-02)


### Features

* **observability:** surface silent pipeline failures via /health ([3f0cb46](https://github.com/afairai/afair/commit/3f0cb46628b354fd71e939c757f080ec4ce2d989))

## [0.1.9](https://github.com/afairai/afair/compare/v0.1.8...v0.1.9) (2026-07-02)


### Features

* **observability:** drop benign uvicorn client-disconnect noise ([71cbc16](https://github.com/afairai/afair/commit/71cbc16aee673b92c1213b51a46f702f4a3dbf19))


### Bug Fixes

* **installer:** anchor snippet markers to line start ([51cd4da](https://github.com/afairai/afair/commit/51cd4dab62b60a812adcaf9a0d4dfb368506b839))
* **installer:** detect legacy em-dash heading, collapse duplicate blocks ([67bc02e](https://github.com/afairai/afair/commit/67bc02ea9a0527654eb2a6f2c3f00c72b78c72ea))
* **mcp:** accept stringified content/event, never drop the write ([a2934df](https://github.com/afairai/afair/commit/a2934dff1e06c40c7f35931f4289298edb9a4555))
* **observe:** coerce over-long action/subject/result, never drop ([3f57a2d](https://github.com/afairai/afair/commit/3f57a2d928161aa00eb501cd86b82d475a10a5e0))
* **observe:** preserve caller-supplied &lt;field&gt;_full on truncation ([6cf4b45](https://github.com/afairai/afair/commit/6cf4b4541908d12cccd501f79bbeb49c5f72ae6a))
* **scripts:** set vault key before opening in backfill_entities ([21e7a96](https://github.com/afairai/afair/commit/21e7a9625a3b56d5c8744afa11e69089298c44ac))


### Documentation

* **claude:** refresh §0 current state to reflect shipped work ([820363d](https://github.com/afairai/afair/commit/820363d30453a709895b4a05e6b299b74947c8d7))

## [0.1.8](https://github.com/afairai/afair/compare/v0.1.7...v0.1.8) (2026-07-02)


### Features

* **recall:** surface pending-review count on every recall ([5b87e5d](https://github.com/afairai/afair/commit/5b87e5dae47b13b4f3caa928d790cd088696263f))


### Bug Fixes

* **scripts:** set vault key before opening in the drain tool ([b91d0c7](https://github.com/afairai/afair/commit/b91d0c7e495dad31762c6c3b5d00309e8d5d26ab))


### Documentation

* **clients:** nudge operators about the review queue ([985672d](https://github.com/afairai/afair/commit/985672db5dda6f2a0f3cdbd85675ae859add1459))

## [0.1.7](https://github.com/afairai/afair/compare/v0.1.6...v0.1.7) (2026-07-02)


### Features

* **agents:** add cold-path edge-confidence rescorer + calibration ([9aade1d](https://github.com/afairai/afair/commit/9aade1deaf04699be098759753092e2a7f1d449d))
* **articles:** stop laundering weak beliefs into article prose ([d1f1ef7](https://github.com/afairai/afair/commit/d1f1ef70c54e2fbf56965975264c3d7d63459191))
* **canonicalizer:** compute real edge-confidence prior at write time ([750913e](https://github.com/afairai/afair/commit/750913ea2161cedb31a2d712cc57c79b6644a686))
* **confidence:** add append-only edge-confidence overlay storage ([fcc68da](https://github.com/afairai/afair/commit/fcc68da8098c6536d6e6d5fd26ab7f2796d20d56))
* **confidence:** add pure edge-confidence log-odds model ([844922c](https://github.com/afairai/afair/commit/844922cebd5d56ac7d5f061bfc681f57b9f5e10b))
* **dedup:** skip deliberate homonym splits ([8c32531](https://github.com/afairai/afair/commit/8c32531fc1a1fcea220306ea2f594e78e7bc5f87))
* **dedup:** unify kind via assignment at high confidence ([6d9aba1](https://github.com/afairai/afair/commit/6d9aba17c80922d906eddb86205eec9e9609291c))
* **extractor:** retry transient extraction failures (llm_timeout) ([acfc372](https://github.com/afairai/afair/commit/acfc372835f2356a054bf6774ad2abffb2c4513f))
* **recall:** serve edge confidence, discriminate quarantine, caveat ([936792c](https://github.com/afairai/afair/commit/936792c35bc27469767752e4e29664134819091d))
* **review:** queue low-confidence edges + wire the decide loop ([2cbcb0c](https://github.com/afairai/afair/commit/2cbcb0c10d092fafea747f5192394d3bccd133e4))
* **scripts:** read-only entity-graph checkup ([3d2e292](https://github.com/afairai/afair/commit/3d2e292010e2cf75c3aecb3916da3819f234d11f))
* **scripts:** supervised dedup backlog drain tool ([d92e1ac](https://github.com/afairai/afair/commit/d92e1ac9f3e2ea0e3484d1daf3567903bb338428))
* **tuner:** whitelist edge-confidence + auto-confirm-floor tunables ([fce784c](https://github.com/afairai/afair/commit/fce784cb4294c0f72efc2ce65a26e1642f13b442))


### Bug Fixes

* **canonicalizer:** defer events on LLM-budget exhaustion ([8f4668c](https://github.com/afairai/afair/commit/8f4668ccde3dde1098957d0a078ac5434943752b))


### Documentation

* **adr:** add ADR-0004 edge-confidence model + registry entry ([16e1e47](https://github.com/afairai/afair/commit/16e1e4720ab3c9438ad5a68be2073bb974e9adc3))
* **adr:** mark ADR-0003 accepted; entity-graph runbook ([f70af7a](https://github.com/afairai/afair/commit/f70af7abb72caf69b1510fbf8c6a8b9e00e4c062))

## [0.1.6](https://github.com/afairai/afair/compare/v0.1.5...v0.1.6) (2026-07-02)


### Bug Fixes

* **dedup:** defer to operator-decided merges; stop re-merge cycle ([accb38d](https://github.com/afairai/afair/commit/accb38d2f55333efeb74a26278fd943d836f8133))

## [0.1.5](https://github.com/afairai/afair/compare/v0.1.4...v0.1.5) (2026-07-02)


### Features

* **agents:** ADR-0003 phase 3 — free-text entity kinds with an observations ledger ([8e58ed1](https://github.com/afairai/afair/commit/8e58ed1756ff9350f3cbfa14fbf7d723847064fb))
* **agents:** ADR-0003 phase 4 — Schema-Evolver in propose-only mode ([1336683](https://github.com/afairai/afair/commit/13366839873fbca0c84cc1b58e7e9cb859fc4f12))
* **mcp:** ADR-0003 phase 5 — operator-confirmed ontology revisions close the emergent-ontology loop ([b718ab1](https://github.com/afairai/afair/commit/b718ab16878e208c80d2c702bcfb0d90bf002e5c))
* **substrate:** ADR-0003 phase 1 — kind registry (kinds become data, behavior preserved) ([3f7dd8b](https://github.com/afairai/afair/commit/3f7dd8bdbfa17f5c3c1b115c5cd56ce3e81b9cd6))
* **substrate:** ADR-0003 phase 2 — decouple entity kind from identity (mutable kinds, v2 ids, homonym guard) ([1434896](https://github.com/afairai/afair/commit/14348961e784a3bc8c0d82f25ec01fc6ccbe2f46))


### Bug Fixes

* **export:** include correction + ontology tables so export/import is faithful (I4) ([29234ed](https://github.com/afairai/afair/commit/29234ed267c5665779a40a8d905f0a62632ad161))


### Documentation

* **adr:** ADR-0003 emergent ontology (revisable entity kinds + Schema-Evolver) ([02c1ade](https://github.com/afairai/afair/commit/02c1ade82848552daa7af2b298e03888106c42b2))

## [0.1.4](https://github.com/afairai/afair/compare/v0.1.3...v0.1.4) (2026-07-01)


### Features

* **agents:** per-agent model configuration (heterogeneous models per agent) ([27b0300](https://github.com/afairai/afair/commit/27b030087cadbf765d1e989c472b2cfdda8f7c46))


### Bug Fixes

* **auth:** enforce write-scope on recall(decide) and fail closed on missing scope ([4392d0c](https://github.com/afairai/afair/commit/4392d0c96a9c1a9897bebd2b91b07aa27e6f5bf4))
* **installer:** preserve a client's existing URL + safe snippet refresh ([0b45725](https://github.com/afairai/afair/commit/0b4572505e10b3cdfffd1bbe039c34138b3a28f3))
* **substrate:** recall must honor operator merge-invalidations; observe cannot spoof reserved payload keys ([7aaf063](https://github.com/afairai/afair/commit/7aaf06329216377f784d77625993f1df33190f05))
* **tuner:** default promotion off; keep raw event text out of the judge panel ([1380114](https://github.com/afairai/afair/commit/1380114592fa09f486ad48827bbc0694b01db3cd))


### Documentation

* **vision:** mark Schema-Evolver, per-agent models, entity-kind enum as roadmap not current ([86c8a79](https://github.com/afairai/afair/commit/86c8a799f5c5393a195b77f04e5bb74beb3ee30f))

## [0.1.3](https://github.com/afairai/afair/compare/v0.1.2...v0.1.3) (2026-07-01)


### Features

* **installer:** add Copilot CLI, Gemini CLI, Windsurf, Antigravity ([a598ea7](https://github.com/afairai/afair/commit/a598ea7ba376340c2296be816320253676533d14))
* **installer:** add GitHub Copilot (VS Code) to one-command install ([15f321d](https://github.com/afairai/afair/commit/15f321dc0a606b653706a3e00507bd213b5d70e7))
* **installer:** interactive client picker + don't offer web clients on localhost ([58200a8](https://github.com/afairai/afair/commit/58200a87ac28c33b6a4082d271115a22e3a40f00))
* **llm:** support keyless/subscription providers (GitHub Copilot, any litellm) ([a2411de](https://github.com/afairai/afair/commit/a2411de848c21f6e984693988db78df1f691327d))
* **mcp:** write-first intake — never reject remember/observe on shape ([b9ba3fc](https://github.com/afairai/afair/commit/b9ba3fc81f834701d70406543412e29bbf733c21))


### Bug Fixes

* **release:** make fallback Release publish idempotent ([c4582ec](https://github.com/afairai/afair/commit/c4582ec08e95423f49bd9dfdfca3106483e8c8e0))


### Documentation

* add 'Run it fully local (no external provider)' guide ([871294a](https://github.com/afairai/afair/commit/871294a32a1ae99d123087af8f1d8ac9c448b68c))
* **clients:** per-client guides for Copilot CLI, Gemini CLI, Windsurf, Antigravity ([b41ed5d](https://github.com/afairai/afair/commit/b41ed5ded61f02cd99ae5ed633bdc9277f188659))
* explain how many API keys afair needs (one is enough) ([325aa2b](https://github.com/afairai/afair/commit/325aa2bd916682da80dac238799bf8f7b240a684))
* GitHub Copilot is chat-only (no embedding/vision/transcription) ([6feb3d7](https://github.com/afairai/afair/commit/6feb3d7a2c38085ca9b143a465d174ecb04425df))
* **readme:** sharpen positioning around structuring as interpretation ([faedd45](https://github.com/afairai/afair/commit/faedd456425366ca0ca6ca615a9410bc5821c130))

## [0.1.2](https://github.com/afairai/afair/compare/v0.1.1...v0.1.2) (2026-06-28)


### Features

* generic life clusters (work / family & friends / personal), drop health naming ([57391ed](https://github.com/afairai/afair/commit/57391ed6776977765533be3827ea6f660121f930))
* **temporal:** P1 of relevance-decay — infer per-event temporal metadata ([bf0e560](https://github.com/afairai/afair/commit/bf0e560169bcd29ec6ffeb7c4252cf0e9c35d88e))
* **temporal:** P2 of relevance-decay — temporal decay in recall ranking ([b672b01](https://github.com/afairai/afair/commit/b672b0160eb7371b3d17bfcd972a263102e51d3f))
* **temporal:** P3 of relevance-decay — recurrence re-surfacing + upcoming ([7c60fbd](https://github.com/afairai/afair/commit/7c60fbd346e8f4d4bf822c735699e9e298bf58a7))
* **temporal:** P4 of relevance-decay — topic warmth (transient + decaying) ([5690335](https://github.com/afairai/afair/commit/56903354b58749440e7fc345f6b8df848669f889))


### Bug Fixes

* **eval:** keep the regression gate honest after the fixture grew ([c52f3fa](https://github.com/afairai/afair/commit/c52f3faae7dfdd626083de55528af66aa85a02a0))
* **settings:** treat blank secret env vars as unset ([9719039](https://github.com/afairai/afair/commit/9719039d29f9050435471d6e54f6f3be47612b8a))
* **temporal:** floor actually-invalidated memories in recall, not just guesses ([04c2172](https://github.com/afairai/afair/commit/04c217206490836645e65a2b623f28c404df823b))


### Documentation

* **claude:** correct the going-public status — history NOT yet scrubbed ([4166592](https://github.com/afairai/afair/commit/416659205a8b1eaa1e2d0bfa020e461b0ef6669f))
* dev branch retired, history scrub complete ([92a2249](https://github.com/afairai/afair/commit/92a224999d1f09e98876b1b907b095e0ec53ade7))
* refresh current state, fix framing, strip em dashes ([a272f5f](https://github.com/afairai/afair/commit/a272f5faf1df38669604d65210a5c63194c8dced))

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
