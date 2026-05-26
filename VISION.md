# neverforget — Operational Constitution

> *A user-owned, vendor-neutral, self-organizing cognitive memory layer for AI agents — built behind a stable MCP surface, free to mutate beneath it.*

> **Note on naming:** `neverforget` is a working codename. It is used for repo names, file paths, internal references, and conversation while building. The final product name is deferred to Phase 6-7, once the product has shape and audience is clearer. See §15.

---

## 0. How To Read This Document

This file is the operating constitution for a solo-built project. It is written to be consumed by **Claude Code (or any other AI assistant) at the start of every working session**, so that no context has to be re-explained.

- **Read order**: §1 → §4 → §5 → §6 → rest as needed.
- **Treat §4 (Invariants) as inviolable.** Everything else is mutable.
- **The phase plan in §9 uses capability gates, not dates.** A phase is complete when its gate passes, not when a calendar says so.
- **Sources are linked inline.** When something feels speculative, click through. When a claim is empirical, the source carries it.

---

## 1. Vision

> **Every individual owns a digital extension of their mind — that travels with them, grows with them, and works for them across every AI system, every surface, and every vendor.**

The inversion: today, Anthropic, OpenAI, Google, and Cursor own *your* context. They are the vaults; you rent space. This project flips the relationship. The user owns the substrate. The AI tools are clients.

The end state is not "another memory framework for developers." The end state is a **cognitive sovereignty layer** for the next decade — the way password managers liberated credentials from individual browsers, this liberates context from individual AI silos.

---

## 2. Mission

> **Build the first user-owned, vendor-neutral, self-organizing cognitive memory system — exposed via MCP, evolving freely beneath that interface, open-source at the core, with managed single-tenant hosting as the commercial layer.**

The product model is not SaaS. It is **open-source software that anyone can self-host**, with an optional **managed deployment service** where each paying user gets their own dedicated isolated machine (one user = one Fly machine = one SQLite database). Never multi-tenant. Never shared infrastructure for user data.

Four non-negotiable principles:

1. **User owns the substrate.** Self-hosting is first-class, not a fallback. Managed hosting is convenience, never structural dependency.
2. **Single-tenant always.** Every instance belongs to exactly one user. The hosted offering is "managed self-hosting," not multi-tenant SaaS.
3. **Cross-vendor by default.** If it only works with one model provider, it has failed. Claude, GPT, Gemini, Mistral, local models — all equal citizens.
4. **Schema is emergent, never imposed.** Minimal bootstrap scaffold, everything else grows from interaction.

---

## 3. Why Now

Four converging conditions make this the right moment:

- **MCP has become the universal protocol.** [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex CLI](https://developers.openai.com/codex/mcp), [Cursor](https://docs.cursor.com/context/mcp), Copilot, Windsurf, Cline — all speak MCP. The cross-vendor surface exists for the first time. A single server reaches everything.
- **Per-user dedicated infrastructure is economically feasible.** Platforms like [Fly.io](https://fly.io) make single-tenant deployment cheap ($3–8/month per user instance for typical workloads). Combined with SQLite-based replication (LiteFS), the architecture that was historically only viable for premium tools (Fastmail, 1Password) is now viable for individual products.
- **Memory has become a recognized infrastructure category** but is **structurally locked-in**. [Mem0 raised $24M](https://mem0.ai/series-a) (Oct 2025), [Letta raised $10M](https://www.felicis.com/blog/letta), Supermemory $2.6M, Zep growing — but all of them are US-centric, vendor-leaning, multi-tenant SaaS, and built around imposed schemas.
- **EU regulatory tailwind**: GDPR's right-to-be-forgotten and the AI Act (fully applicable from August 2026, with 10-year audit-trail requirements for high-risk systems) create a structural advantage for EU-native, user-owned architectures. Per-user dedicated instances make compliance physically obvious rather than legally complex.
- **The recognition that memory is the unsolved problem of agents.** [Mem0's State of AI Agent Memory 2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026) explicitly names what's still open: cross-session identity resolution, memory staleness in high-relevance memories, the noise floor problem. The field knows it has not solved this.

---

## 4. Constitutional Invariants

**These are inviolable. Every architectural decision must preserve them. If any future evolution threatens an invariant, the evolution is wrong, not the invariant.**

### I1. MCP Surface Stability
The set of MCP tools exposed to external consumers (Claude Code, Codex, Cursor, etc.) is **versioned and additive**. New tools may be added. Existing tool signatures and semantics **never break**. Deprecation requires a minimum support window of two major versions plus migration tooling. A consumer that integrated at v1.0 must still function unchanged at v9.0.

### I2. Substrate Immutability
The raw event log is **append-only and content-addressed**. Nothing is ever overwritten or deleted (except via explicit user-invoked right-to-erasure paths, which are themselves logged). The substrate is git-like: history is the truth.

### I3. Backward-Compatible Evolution
Any data written by an earlier version of the system must remain **readable, queryable, and re-interpretable** by every later version, forever. Schema migrations are not migrations — they are **new materialized views over unchanged substrate**.

### I4. User Ownership By Default
The substrate lives on disk under the user's control. Self-hosting is a first-class deployment. Hosted offering may exist, but must never become structurally required. Export and full local operation are baseline guarantees, not premium features.

### I5. Vendor Neutrality
No code path may privilege one AI provider over another. The architecture must function with Claude, GPT, Gemini, Mistral, and local models, with feature parity where the model class allows.

### I6. Emergent Over Imposed
The system **does not ship with a fixed ontology** of memory types. A minimal bootstrap scaffold is acceptable as a starting point, but the system must be capable of revising, merging, splitting, and discarding categories based on usage. Forever.

### I7. Recursive Self-Modification with Rollback
The system can revise its own extraction rules, retrieval strategies, and agent compositions at runtime. Every modification is itself recorded in the substrate. Every modification is reversible. Invariants I1–I6 are exempt from self-modification — they constitute the irreducible kernel.

### I8. Single-Tenant by Design
Every deployed instance — self-hosted or managed — belongs to exactly one user. There is no shared database, no shared application server, no row-level user separation. The hosted offering provisions a dedicated Fly machine (or equivalent) per paying user. Multi-tenancy is forbidden architecturally, not just practically. The thin orchestration layer that manages billing and provisioning may be shared; user data and application state never are.

---

## 5. Core Thesis

### 5.1 The Memory Problem (What's Actually Unsolved)

Despite the existence of mature frameworks ([Mem0](https://github.com/mem0ai/mem0), [Letta](https://github.com/letta-ai/letta), [Zep/Graphiti](https://github.com/getzep/graphiti), [Cognee](https://github.com/topoteretes/cognee), [Supermemory](https://github.com/supermemoryai/supermemory)), the field's own state-of-the-art summaries identify these as genuinely open:

- **Memory staleness in high-relevance memories** — a user changes jobs; the system "knows" the old employer with high confidence and surfaces it. Decay handles low-relevance memories; this is unsolved for high-relevance ones.
- **Cross-session identity resolution** — anonymous sessions, multi-device, mixed auth flows break the stable-user-id assumption.
- **The noise floor problem** — agents accumulate so much "important" information that memory search becomes slower than processing full context.
- **Schema lock-in** — every framework imposes a categorization (episodic/semantic/procedural, or flat key-value). User cognition does not fit that schema.
- **Vendor lock-in** — every framework is structurally tied to its host ecosystem.

Sources: [Mem0 ECAI 2025 paper (arXiv:2504.19413)](https://arxiv.org/abs/2504.19413), [State of AI Agent Memory 2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026), [Best AI Agent Memory Systems 2026 comparison](https://vectorize.io/articles/best-ai-agent-memory-systems).

### 5.2 Memory Phenotypes — Cognitive Diversity As Design Space

The premise: human cognitive variation — historically pathologized — is in fact a catalog of evolutionary cognitive strategies, each optimized for different contexts. A memory system built on a single (neurotypical) model is leaving most of the design space unexplored.

This is not a metaphor. Each variant maps to a specific computational pattern that solves specific memory tasks better than the neurotypical baseline.

**ADHD** — Constrained working memory forces a **depth-first search** strategy through conceptual space. The "paradox" of inattention + hyperfocus is a coherent strategy, not a defect. ([Unifying the ADHD Paradox, 2025](https://sciety.org/articles/activity/10.31234/osf.io/frsp4_v5)). Computational mapping: an exploration-favoring agent that switches contexts hard, dives deep, and doesn't try to hold parallel state. Clinically, ADHD also presents as a **mode-switching failure** between the default mode and central executive networks ([Bossong et al., 2013](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC3729458/)) — instructive as a cautionary failure mode for the system's salience switcher.

**Autism / Weak Central Coherence** — Detail-focused, local processing prioritized over global gestalt. Verbatim recall over gist. Pattern recognition over context integration. ([Happé & Frith, 2006](https://link.springer.com/article/10.1007/s10803-005-0039-0)). Computational mapping: an extractor agent that preserves raw form and retrieves via pattern matching rather than embedding similarity. Wins on code, legal text, anywhere exact form matters.

**Asperger / High-Functioning Systematizing** — Strong systems thinking, narrow deep specialization, exceptional pattern recognition within domains of interest. ([Baron-Cohen's empathizing-systemizing theory](https://www.cambridge.org/core/journals/the-british-journal-of-psychiatry/article/empathizingsystemizing-theory-an-update/)). Computational mapping: domain-specialized agents with deep retrieval within their domain, narrow lateral spread. Good for the "expert mode" of a personal vault.

**Synesthesia** — Cross-modal binding, where one type of input triggers another. Often correlates with strong memory due to multi-channel encoding. Computational mapping: an indexing agent that deliberately cross-references modalities (text + time + author + project + emotional valence) for any given event, enabling retrieval through any of the dimensions.

**Aphantasia** — Absence of voluntary mental imagery. Memory encoded propositionally/verbally, not visually. ([Zeman et al., 2015](https://www.sciencedirect.com/science/article/pii/S0010945215000532)). Computational mapping: a fallback when image-based or spatial encoding is unavailable — pure propositional substrate. A reminder that not all useful memory needs to be embedding-based.

**Hyperthymesia** (Highly Superior Autobiographical Memory) — Exhaustive recall of personal events with temporal precision. ([McGaugh & LePort](https://www.sciencedirect.com/science/article/pii/S1364661312001428)). Computational mapping: an agent dedicated to temporal indexing — every event timestamped, queryable by date, never compressed. Expensive but the only honest answer for therapy-, companion-, or audit-grade use cases.

**Savant Syndrome** — Narrow extreme expertise (calendar calculation, prime factorization, musical recall) often co-occurring with autism. ([Treffert, 2009](https://royalsocietypublishing.org/doi/10.1098/rstb.2008.0326)). Computational mapping: highly specialized, narrow-domain retrieval agents, willing to be useless outside their domain because excellent within it.

**Hippocampal replay during sleep** — Not a disorder, but a biological mechanism worth direct emulation. Weakly-learned memories are prioritized for offline replay; consolidation strengthens some, prunes others. ([Schapiro et al., 2018](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC6156217/), [Golden et al., 2022](https://www.nature.com/articles/s41467-022-34938-7)). Anthropic's [Auto Dream in Claude Code](https://dev.to/max_quimby/ai-agent-memory-in-2026-auto-dream-context-files-and-what-actually-works-39m8) is the first production implementation. The cold-path consolidator in this system is its descendant.

**The framing**: these are not "neurotypical with bugs." They are alternative cognitive architectures, each adaptive in some niche. A memory system that can instantiate the right phenotype for the right task — and switch between them — beats one fixed architecture pretending to be universal.

### 5.3 Society of Mind

Minsky's 1986 thesis: the mind is not a single thing but a society of specialized sub-agents, each individually unintelligent, collectively producing cognition. ([Society of Mind retrospective, 2026](https://medium.com/@Micheal-Lanham/society-of-mind-the-50-year-old-blueprint-for-ai-agents-b0e62eb4ec06)).

The contemporary instantiation is the multi-agent swarm. LinkedIn shipped this as internal infrastructure in April 2026 ([Cognitive Memory Agent](https://www.infoq.com/news/2026/04/linkedin-cognitive-memory-agent/)). ICLR 2026 hosts a [MemAgents workshop](https://openreview.net/pdf?id=U51WxL382H). No one has shipped it as a cross-vendor user-owned product.

### 5.4 Triple Network Coordination

Neuroscience converged on the Triple Network Model: three large-scale brain networks that coordinate cognition.

- **Default Mode Network (DMN)** — medial prefrontal cortex, posterior cingulate, hippocampus. Self-referential, consolidation, mind-wandering, future planning. Active during rest. ([Andrews-Hanna review](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC4427863/)).
- **Central Executive Network (CEN)** — dorsolateral prefrontal cortex. Task-focused, working memory, decision-making. Active during effortful cognition.
- **Salience Network (SN)** — insular cortex, anterior cingulate. Detects salient internal/external events and **switches** between DMN and CEN. ([Menon, 2011](https://www.sciencedirect.com/science/article/pii/S1364661311001148)).

DMN and CEN are **mutually inhibitory** — when one is active, the other is suppressed. The SN does the switching. The thalamus underneath them is the routing layer.

This is the architectural blueprint. The agent swarm has three layers:
- A **router** (thalamic, dumb relay) = the MCP surface
- A **salience switcher** (mode selector) = a meta-agent
- Two complementary sub-swarms (DMN-like for reflection, CEN-like for task work) = the actual specialized agents

ADHD-style failures (DMN doesn't deactivate during CEN tasks) are the cautionary case: if the salience switcher is miscalibrated, the system thrashes.

### 5.5 Emergent Ontology

Locke's "tabula rasa" is biologically wrong — even infants come with [core knowledge priors](https://www.ncbi.nlm.nih.gov/books/NBK395094/) (object permanence, agency, number, statistical learning). But categories themselves emerge from interaction (Piaget, Saffran).

For this system: a minimal bootstrap scaffold (initial extraction templates) is acceptable. But the system must be able to **subtractively learn** — pruning, merging, and revising its own ontology — toward a categorization that fits *this user's* actual life, not a generic schema.

LLMs make this faster than biology. The model already encodes most human categorical structure; the system's job is to **carve out the subset relevant to one user**. Days, not years.

---

## 6. Architecture

### 6.1 The Layered Stack

```
                External (Claude Code, Codex, Cursor, Copilot, …)
                External Sources (Gmail, Calendar, Drive, Slack, …)
                                    │
                                    │  (both queries AND incoming events
                                    │   enter through the same router)
                                    ▼
                          ┌────────────────────┐
                          │   MCP Router       │   ← Thalamus
                          │   (stable forever) │     dumb relay, versioned
                          │                    │     routes queries IN
                          │                    │     routes events IN
                          └─────────┬──────────┘
                                    │
                          ┌─────────▼──────────┐
                          │ Salience Agent     │   ← Salience Network
                          │ (mode switcher +   │     filters incoming events
                          │  ingestion filter) │     hot / warm / cold
                          └─────┬──────────┬───┘
                                │          │
                       ┌────────▼──┐    ┌──▼─────────┐
                       │   CEN     │    │    DMN     │  ← mutually exclusive
                       │  Swarm    │    │   Swarm    │     under heavy load
                       │           │    │            │
                       │ Retriever │    │Consolidator│
                       │ Extractor │    │ Schema-    │
                       │ Bind      │    │  Evolver   │
                       │ Conflict  │    │ Pruner     │
                       │  -light   │    │ Conflict-  │
                       │           │    │  Resolver  │
                       └────────┬──┘    └──┬─────────┘
                                │          │
                       ┌────────▼──────────▼─────────┐
                       │   Interpretation Layer       │
                       │   (materialized views,       │
                       │    version-tagged,           │
                       │    regenerable)              │
                       └────────────┬─────────────────┘
                                    │
                       ┌────────────▼─────────────────┐
                       │   Substrate                  │   ← Invariant I2
                       │   (immutable, append-only,   │
                       │    content-addressed)        │
                       └──────────────────────────────┘
```

**Critical architectural rule:** Nothing writes to the Substrate without passing through the Router → Salience → Swarm pipeline. External ingestion is not a privileged path. Like the thalamus in the brain: every sensory input is routed and filtered before it affects the cortex. The only exception is the system's own audit log of its meta-actions (recorded directly to Substrate as system events), analogous to the few brain pathways that bypass the thalamus (e.g., olfactory) — small, well-defined exceptions, never the default.

### 6.1a Theoretical Grounding

The CEN/DMN/Salience labels are taken from Menon's Triple Network Model (2011) and used as architectural metaphor — they communicate intent ("task work vs. reflection" / "salience as mode switcher") in language familiar to anyone with a neuroscience background. The Triple Network Model is well-validated but not cutting-edge; it describes *what* each module does, not *why* the architecture is shaped this way. Two deeper frameworks carry that load:

1. **Predictive Processing / Active Inference** (Friston, Free Energy Principle). The substrate is treated as a generative model of the user's world; every agent (extractor, bind, salience, recall, consolidator) is an inference process minimizing prediction error. Surprise — the divergence between predicted and observed substrate state — is the *intended* load-bearing salience signal (Phase 4 implementation), not engineered priority scores. The `open_threads` field emitted by `consolidator:v0` is already a proto-instance of this: explicit unresolved prediction errors handed forward.

2. **Complementary Learning Systems** (McClelland, McNaughton & O'Reilly 1995; updated in Kumaran, Hassabis & McClelland 2016). Memory is split between a fast, sparse, episodic store (hippocampus-like — our hot path and append-only substrate) and a slow, distributed, semantic store (neocortex-like — our interpretation layer and the consolidator's abstraction events). The hot/warm/cold tiering is biologically motivated: the brain runs two systems because one cannot simultaneously learn fast (catastrophic interference) and generalize well. Same constraint applies here. `consolidator:v0` (event 01KSHNPH37BGHTJMMAYK3H3S2N, 2026-05-26) is CLS replay: samples episodic events, generates abstractions with parent_hashes to sources, emits themes as emergent semantic tags. This component was built before the theory was named; CLS is a retroactive justification, not a forward design constraint.

Together: Triple Network = the visible architecture. CLS + PP = the load-bearing theory that says it has to be this shape.

### 6.2 MCP Surface (Invariant I1)

The external contract. Whatever changes underneath, these signatures hold.

Initial tools (v1):
- `remember(content, context?, type_hint?)` — explicit note. Type hint is advisory only; system ignores it if better classification emerges.
- `recall(query, scope?, depth?)` — retrieval. Depth controls reasoning effort (cheap lookup → full swarm consultation).
- `list_context(about?)` — what the system currently knows, optionally scoped to a subject.
- `observe(event)` — structured logging of agent actions. Used by Claude Code, Codex, etc. to log their own work.

Versioning policy: additive only. New tools appear with version suffixes. Old signatures keep working. When a tool is genuinely obsolete, it stays callable but emits a deprecation notice; removal requires two major versions of overlap.

### 6.3 Substrate Layer (Invariant I2)

Append-only event log on local disk. Content-addressed (each event hashed). Git-friendly. Inspectable with `cat` and `sqlite3`.

File layout (initial sketch — may evolve as long as old entries remain readable):

```
~/vault/
├── substrate/
│   ├── events/        ← raw observation log, sharded by date
│   ├── ingested/      ← raw imports from external services
│   └── decisions/     ← system meta-actions also logged here
├── interpretations/   ← materialized views, regenerable
│   ├── v1/
│   ├── v2/
│   └── …              ← multiple versions coexist
├── ontology/          ← emergent category state
│   └── versions/
└── meta/
    ├── rules/         ← current extraction/retrieval policies (also versioned)
    └── manifest.toml  ← current schema version, agent compositions
```

Each event carries: timestamp, source (agent or external system), content hash, payload, optional links to parent events. Nothing is ever updated. Corrections are new events that reference the corrected event.

### 6.4 Interpretation Layer (Invariant I3)

The mutable, regenerable layer. Embeddings, extracted facts, graph edges, emergent categories. All produced *from* the substrate, never replacing it.

When extraction rules improve (new agent, better prompt, larger model), the system re-runs them over the substrate and produces a new version of the interpretation. Old versions remain for as long as anything references them. The user (or system) chooses which interpretation version is "live."

This is what makes recursive self-improvement safe: the substrate is invariant, interpretations are versioned and reversible.

### 6.5 Agent Swarm — Hot / Warm / Cold Paths

**Hot path (sync, target <300ms)**: Retriever + cache only. No reasoning. Indexed lookups. ~95% of `recall` calls go here.

**Warm path (async, seconds)**: New observation comes in (from user via `remember`, *or* from external sensing via Salience-routed `sense` events). Observer logs to substrate. Extractor processes async — by the time the user calls back, the new info is integrated. Caller never waits.

**Cold path (background, minutes to hours)**: The "sleep swarm." Runs during idle time. Consolidator re-clusters. Schema-Evolver revises ontology. Conflict-Resolver handles contradictions. Pruner ages out unused interpretations. Cold-path is also where queued low-priority ingestion events get integrated — the system "dreams over" the day's accumulated input. This is where the heavy thinking happens — and where premium models earn their keep.

Each agent can run on a different model class (cheap for Observer, premium for Schema-Evolver). Each agent can be A/B tested independently. Each agent can have its own memory phenotype (Extractor = autism-like verbatim; Salience-Detector = ADHD-like surprise-driven; Consolidator = hippocampal-replay-like).

User queries and external sensing events use the **same event schema** with different `origin` tags. The Salience Agent routes them through the appropriate path based on content, priority, and current mode. There is no separate ingestion pipeline.

### 6.6 External Service Bridge — Continuous Sensing

External services (Gmail, Calendar, Drive, Slack, GitHub) connect via MCP servers or native APIs. **They are sensing channels, not write paths.** Like sensory organs in biology, they generate event streams that enter the system through the same Router → Salience pipeline as user queries.

Three sensing modes:

- **Continuous sensing (background)**: Pollers or webhooks watch external services. New items become **incoming events** that enter through the MCP Router as `sense(source, payload)` calls. The Salience Agent decides what to do with each event:
  - **Drop** (noise: newsletters, automated notifications) — logged minimally for audit, not integrated.
  - **Queue cold** (low priority, integrate during next idle phase).
  - **Process warm** (normal priority, extract async via Extractor agent).
  - **Process hot** (high priority, e.g., email from a high-salience entity, integrate immediately and possibly surface to the user).
- **Active perception (on-demand)**: A user query requires fresh external data. The Salience Agent in CEN mode triggers a targeted fetch from the right service, processes it through the same pipeline, and integrates the result before responding.
- **Cross-modal binding**: Once an external event has been classified by the Extractor (warm/hot path), a Bind agent links it to existing substrate entries (e.g., email from Sajinth → existing Sajinth cluster). Binding produces interpretation-layer edges, never substrate mutations.

**Why this design (and not direct ingestion):**

1. **Salience-filtered intake.** 200 newsletters/day do not deserve substrate entries. The salience filter mirrors how the thalamus + reticular formation gate sensory input in the brain — without that gating, the cortex would drown.
2. **Mode-aware processing.** If the user is in active CEN-mode work, expensive integration of new external events waits. If the user is idle (DMN-active), incoming events get full consolidation treatment. This is exactly how memory consolidation works biologically — heavy integration happens during rest, not during focused activity.
3. **Adversarial robustness.** A compromised external MCP connector cannot write directly to substrate. Every external event passes through the Salience Agent's inspection. This is the architectural equivalent of why your brain can't be directly programmed via your eyes.
4. **Uniform event model.** Queries and incoming events use the same data structure (an `event` with origin tag, payload, and timestamp). One pipeline handles both. Simpler code, fewer special cases.

The MCP swarm never hits external APIs during synchronous user-facing tool calls. Active perception is async and the user's tool call returns immediately with a "fetching" status if needed.

### 6.7 Deployment Topology

The same binary runs in three contexts:

**Local (developer/power-user):** Runs on the user's own laptop. MCP server bound to `localhost`. SQLite file in `~/vault/`. No network surface. Fastest, most private, requires the user to keep their machine running.

**Self-hosted (technical user):** Runs on the user's own server (home server, VPS, NAS). MCP server bound to internal address or behind Tailscale/Cloudflare Tunnel. SQLite file on the server's disk. User controls everything; user does the ops work.

**Managed (paying user, eventually):** Each user gets a **dedicated Fly machine** provisioned automatically. Same binary, same SQLite, but operated by us. LiteFS for backup and optional read replication. The machine belongs to one user; if they cancel, the machine is destroyed and the data with it (after retention window). Per-machine cost passes through; management margin covers ops, updates, and premium features.

**Critical:** The three topologies use the **same binary, same data format, same MCP surface**. Migration between them is `tar`-and-`scp`. A managed user can export and self-host any time. A self-hosted user can hand us their SQLite and we run their existing vault. The architecture is portability-first.

The thin shared layer for managed hosting holds **only orchestration metadata**: which user has which Fly machine, billing status, update windows. It never holds user substrate data. If the orchestration layer is offline, all managed user instances continue working.

### 6.8 Tech Stack (Decided)

- **Language:** Python 3.12+. Reason: every memory/agent research ecosystem (Mem0, Letta, Cognee, Graphiti) is Python; FastMCP and LLM SDKs are Python-first; Claude Code is excellent in Python.
- **MCP Server:** [FastMCP](https://github.com/jlowin/fastmcp). Most mature Python MCP-server framework.
- **HTTP Layer:** FastAPI underneath FastMCP, for non-MCP endpoints (health checks, management API).
- **MCP Transport:** Streamable HTTP from day one. STDIO is not used. HTTP is required for Claude.ai connectivity, works equally well locally (`localhost:8765`) and on Fly (`0.0.0.0:443`).
- **Substrate Storage:** SQLite with FTS5 (full-text search) + [sqlite-vec](https://github.com/asg017/sqlite-vec) (vector search). Single file, transactional, embeddable, portable. Inspectable with `sqlite3` CLI.
- **Replication (managed):** [LiteFS](https://fly.io/docs/litefs/) for SQLite replication on Fly. Backup to S3-compatible storage (Backblaze B2 likely cheapest).
- **Parallel Export:** Markdown export of substrate runs continuously, optionally git-tracked. Source of truth is SQLite; Markdown is human-inspectable mirror for trust and emergency recovery.
- **Validation/Schemas:** Pydantic.
- **Package Manager:** `uv` (fast, modern).
- **Deployment:** Docker container, deployable to Fly machine or any container host. Same image used for local, self-hosted, and managed.
- **Embeddings:** initially OpenAI/Anthropic API-based; later option for local embeddings via [FastEmbed](https://github.com/qdrant/fastembed) for users who want full local operation.
- **LLM Calls:** Model-agnostic via litellm or direct provider SDKs. Each agent specifies its preferred model class; user can override globally.

---

## 7. Backward Compatibility Doctrine

Three rules, derived from Invariants I1–I3:

**Rule 1 — MCP is forever.** A tool signature that ships in v1 must still work in v∞. The system may add new tools. The system may add new optional parameters. The system may not change semantics of existing tools or remove them. Period.

**Rule 2 — Substrate is forever readable.** Any event format that has ever shipped must remain parseable. New extractors must handle old event formats gracefully (skipping fields they don't understand, never erroring on legacy data). Old data is first-class data.

**Rule 3 — Read old, write new.** When the substrate format evolves, new code reads both old and new formats, writes only the new format. Old data stays untouched. Migrations are forbidden. New materialized views over old substrate are encouraged.

The architecture above is consistent with all three. The Substrate Layer never changes its data; only new views are added. The Interpretation Layer is versioned and parallel. The MCP Surface is the stable contract.

---

## 8. Recursive Self-Improvement

What can the system modify about itself, at runtime, without manual intervention?

**Modifiable at runtime:**
- Extraction prompts
- Retrieval strategies (weights, fusion functions)
- Agent compositions (which agents run for which task)
- Emergent categories (split, merge, rename, discard)
- Cache and index structures
- Schedule of cold-path operations

**Modifiable only with manual approval:**
- Changes to invariants I1–I7 (theoretically possible but practically forbidden)
- Changes to substrate event schema (must be versioned and additive)
- Addition or removal of agent roles
- Changes to the salience switcher's mode-decision logic

**Never modifiable:**
- The append-only nature of substrate
- The principle that user owns the data
- The MCP surface contract for tools that have shipped

Every self-modification is itself an event in the substrate. The full history of "why does the system behave this way at time T" is reconstructible. Rollback is a matter of choosing an earlier rule version. The substrate is never altered by rollback — only the active rule pointer is.

---

## 9. Phase Plan (capability gates, not dates)

### Phase 0 — Substrate + MCP Surface (cross-vendor, zero automated ingestion)
**Capability gate:** Daily use from **Claude Code, Codex CLI, and Claude.ai chat** for two weeks without rebuilding. Trust the system enough to keep calling it because nothing reaches into your life uninvited.
**Ships:** Append-only substrate, MCP server (Streamable HTTP) with `remember` / `recall` / `list_context` / `observe`, a default **context-aware Extractor** on the warm path (richer extraction — entities, relations, time references, source attribution, best-guess-kind inferred from the call's context rather than a fixed enum). Deployed to the user's own Fly machine from day one (HTTPS required for Claude.ai connectivity; same binary exercises the eventual managed path).
**Explicitly out of scope:** Gmail, Calendar, Drive, Slack, GitHub, or any external connector. Zero automated sensing. The only way data enters the substrate in Phase 0 is via deliberate `remember` / `observe` calls made by the user or by an AI agent the user is actively talking to.
**Trust ladder (binding for all phases):** Phase 0 — explicit calls only. Phase 1+ — user-initiated manual import (paste, drag a file, "ingest this URL"). Phase 2+ — opt-in connectors, one source at a time, with the Salience filter inspectable before enable. Continuous sensing (Gmail, Calendar, etc.) is earned, not assumed.
**Also out of scope for Phase 0:** No swarm. No emergent ontology. No consolidation. The Salience-Router-Swarm pipeline from §6 is built as a single linear pass; later phases progressively fan it out.

### Phase 1 — First Real Value
**Capability gate:** Three documented "this saved me work" moments within 30 days of daily use.
**Ships:** Better extraction, basic conflict handling, expanded ingestion sources, retrieval quality improvements.

### Phase 2 — Salience-Driven Mode Switching
**Capability gate:** System distinguishes hot/warm/cold paths automatically; user experiences no perceptible thrashing.
**Ships:** Salience agent, three-path execution model, idle-detection.

### Phase 3 — Sleep Swarm (CLS replay)
**Capability gate:** Quality of retrievals visibly improves after idle periods compared to before.
**Ships:** Consolidator (CLS-replay), Conflict-Resolver, Pruner. Background operation over interpretation layer.
**Status:** `consolidator:v0` is shipped (daily-scheduled, scopes to a `target_day`, samples episodic events, emits abstraction events with `parent_hashes`, themes, and `open_threads`). Phase 3 expands this:
  (a) **Cross-domain consolidation** — v0 clusters thematically within a day but does not bridge domains; add a higher-level consolidator that samples across topics to generate structural abstractions.
  (b) **`open_threads` consumer agent** — v0 emits unresolved prediction errors but nothing picks them up. Wire a follow-up agent (DMN sleep swarm) that takes `open_threads` and queues them as low-priority recall or as user-facing prompts.
  (c) **Auto-linking on consolidations** — `linked_event_ids` is currently empty on consolidation events. Decide: do consolidations participate in the top-3 NN graph like other events, or do they form their own abstraction layer with different linking semantics?
  (d) **Surprise-triggered consolidation** — currently only daily. Add a trigger when cumulative surprise from recall queries exceeds threshold, indicating the user's working context has drifted enough that re-abstraction is warranted.

### Phase 4 — Emergent Ontology + Active Inference Salience
**Capability gate:** Categories I never explicitly defined appear in `list_context` and feel correct, AND the Salience agent switches modes based on substrate-side surprise rather than engineered heuristics.
**Ships:**
- Schema-Evolver, ontology versioning, emergent category bootstrapping from substrate patterns (the Emergent-Ontology track, binding I6).
- Active-Inference Salience: recall returns a surprise score per hit (specific metric TBD — KL-divergence between expected and observed result distributions is one candidate). The Salience agent uses cumulative surprise to decide when to switch from CEN-mode (exploitation of current task) to DMN-mode (re-indexing, consolidation, exploration). Surprise above threshold triggers an `observe()` event automatically — the system journals its own moments of being-wrong, the substrate for recursive self-improvement (I7).

### Phase 5 — Cross-Vendor Polish
**Capability gate:** Equally usable from Claude Code, Codex CLI, and Cursor with no functional gap.
**Ships:** Vendor-specific edge case handling, MCP transport robustness, documentation per client.

### Phase 6 — First Non-Self User
**Capability gate:** One trusted external user maintains daily use for 30 days without abandoning.
**Ships:** Installer, onboarding flow for technical users, first round of usability fixes from real friction. Still self-host only.

### Phase 7 — Public Release (license decision per §16)
**Capability gate:** Public repo under whichever license §16 selects, documented setup, first 10 self-hosting users running independently.
**Ships:** Source release under the §16-chosen license, installation docs, basic Discord/GitHub community, contribution guidelines. **If §16's Phase-6 review concludes closed source**, Phase 7 transforms into a binary-distribution / managed-only launch — the capability gate (10 external users) and the operational deliverables still apply, just without the OSS repo.

### Phase 8 — Managed Hosting (Per-User Dedicated Machines)
**Capability gate:** First paying user runs on managed Fly machine for 30+ days; provisioning is automated end-to-end.
**Ships:** Thin orchestration layer (account, billing, Fly machine provisioning, automatic backups to S3-equivalent, update mechanism). Each paying user gets one dedicated Fly machine. No multi-tenancy ever. Pricing model: monthly subscription covering Fly costs + management margin + premium features (sync, hosted backups, priority support).

### Phase 9 — Compliance Layer for Regulated Users
**Capability gate:** GDPR + EU AI Act audit-ready. SOC2 in progress. Single-tenant architecture makes most of this physically obvious rather than procedurally complex.
**Ships:** Audit trails as substrate views, right-to-erasure flows (trivial: destroy the user's machine), data residency controls (Fly regions), encryption at rest, documented compliance posture.

### Phase 10 (optional) — Sharing & Teams
**Capability gate:** Real demand from users for cross-vault sharing. Do not build speculatively.
**Ships:** Inter-instance sharing protocols, scoped cross-vault queries, team vaults if warranted. Architectural complexity is significant; build only if pulled by real users.

---

## 10. Competitive Landscape

For each competitor: what they have, what they don't, and — crucially — the **structural reason** they cannot or will not close the gap.

### 10.1 Direct Memory Frameworks

**[Mem0](https://mem0.ai)** — $24M raised, 41K+ GitHub stars, 14M+ downloads, AWS Agent SDK partnership.
- **Has:** Mature production memory infra, three-line integration, broad framework support, model-agnostic.
- **Doesn't have:** Cross-vendor user-owned positioning, emergent ontology, swarm architecture, EU-native compliance.
- **Structural limit:** US-based, VC-backed, hosted-first. Their revenue model rewards hosting and integration into their cloud, which contradicts user-owned primacy. They will optimize toward platform, not sovereignty.

**[Letta (ex-MemGPT)](https://www.letta.com)** — $10M seed at $70M post-money, UC Berkeley spinout, OS-inspired tiered memory.
- **Has:** Strong research foundation, tiered memory architecture (core/recall/archival), self-editing agent memory, model-agnostic API.
- **Doesn't have:** Cross-vendor MCP-first positioning, emergent ontology, multi-agent swarm, EU compliance focus.
- **Structural limit:** Research-led, complex to deploy, oriented toward developers building stateful agents — not users owning context. Tiered architecture is *imposed*, not emergent. ([Letta paper / docs](https://docs.letta.com)).

**[Zep / Graphiti](https://www.getzep.com)** — Temporal knowledge graph with bi-temporal model.
- **Has:** Best-in-class temporal reasoning, conflict-as-data via t_valid/t_invalid edges, knowledge graph richness. [Zep paper (arXiv:2501.13956)](https://arxiv.org/abs/2501.13956).
- **Doesn't have:** Multi-agent cognition, emergent ontology, user-owned local-first deployment, MCP-as-primary surface.
- **Structural limit:** Enterprise SaaS positioning. Excellent technology, but the business model demands managed deployment. Self-hosting is allowed; not first-class.

**[Cognee](https://www.cognee.ai)** — Open-source memory + knowledge graph layer.
- **Has:** Full local deployment, graph + vector hybrid, multi-source ingestion, GDPR-compatible.
- **Doesn't have:** Emergent ontology, multi-agent swarm, MCP-first cross-vendor surface, opinionated memory phenotypes.
- **Structural limit:** Library/framework framing, not product. Lacks the consumer-facing thesis. Closest in spirit; furthest from execution polish.

**[Supermemory](https://supermemory.ai)** — $2.6M seed, OSS-from-day-one memory infrastructure.
- **Has:** **MIT-licensed** ([github.com/supermemoryai/supermemory](https://github.com/supermemoryai/supermemory)), 22.7K stars, full monorepo (web app + SDKs + MCP server + plugins). **#1 on LongMemEval, LoCoMo, AND ConvoMem** — the three current memory benchmarks. Aggressive multi-modal extraction (PDFs, OCR for images, transcription for video, AST-aware chunking for code). Framework adapters for Vercel AI SDK, LangChain, LangGraph, OpenAI Agents SDK, Mastra. Claims sub-300ms hybrid + ~50ms one-call user profile retrieval. Explicit auto-forgetting / temporal-update handling.
- **Doesn't have:** User-owned positioning (they sell API/SDK to developers, not "your digital brain"), single-tenant-per-user (hosted multi-tenant SaaS economics), emergent ontology (their memory graph has imposed shape), swarm architecture (monolithic pipeline), EU-native default.
- **Structural limit:** $2.6M VC means hosted multi-tenant economics — they architecturally CANNOT pivot to single-tenant-per-user without abandoning the growth thesis. Positioning is developer-tool, not user-product; the moat is benchmark wins + integrations, not cognitive sovereignty. **They are the closest direct competitor and the one to study, especially their benchmark + multi-modal execution; we differentiate on positioning + architecture, not on memory-engine quality.**

**[LangMem](https://github.com/langchain-ai/langmem)** — LangChain's memory primitives.
- **Has:** Tight LangGraph integration, free, simple.
- **Doesn't have:** Anything sophisticated. Flat key-value with vector search, no graph, no entity extraction. ([Vectorize 2026 comparison](https://vectorize.io/articles/best-ai-agent-memory-systems)).
- **Structural limit:** Tied to LangGraph. Outside that, no value.

**[LlamaIndex Memory](https://www.llamaindex.ai)** — Similar to LangMem, tied to LlamaIndex.
- Same structural limit.

### 10.2 Memory Inside Another Product

**Anthropic / Claude (memory + Auto Dream)** — Native memory in Claude.ai, [Auto Dream consolidation in Claude Code](https://dev.to/max_quimby/ai-agent-memory-in-2026-auto-dream-context-files-and-what-actually-works-39m8).
- **Has:** Best-in-class consolidation (Auto Dream), deep integration with their own agents.
- **Doesn't have:** Cross-vendor anything. Claude-only by design.
- **Structural limit:** Anthropic's incentive is Claude lock-in. They cannot build cross-vendor memory because it would erode their primary moat. This is the structural opening.

**OpenAI / ChatGPT memory** — Same dynamic. Memory is a lock-in feature, not a portable asset.

**Cursor project context (.cursorrules + RAG)** — Project-scoped, IDE-bound.
- Same structural limit.

**[LinkedIn CMA](https://www.infoq.com/news/2026/04/linkedin-cognitive-memory-agent/)** — April 2026 internal infrastructure. Multi-agent shared memory substrate. Not a product. Reference architecture for what's possible when a team with resources builds this properly.

### 10.3 Adjacent — Not Memory But Overlap

**1Password / Bitwarden** — The cross-platform user-owned model for credentials. Direct strategic analog. The blueprint for "vendor-neutral, user-owned, works everywhere" as a successful category.

**Obsidian / Logseq** — User-owned local-first knowledge bases. No agent layer. Strong philosophical alignment. Adjacent customer base.

**Notion / Roam** — Hosted, schema-imposing. Opposite of our positioning.

**Personal Information Manager category broadly** — Decades of attempts. The differentiator now is AI-first interaction, which none of them are.

### 10.4 Business Model References — Per-User Single-Tenant Hosting

These are not memory products, but their business model is what we replicate: open-source self-hostable + managed single-tenant hosting.

- **[Fastmail](https://www.fastmail.com)** — Per-account isolated email infrastructure since 1999. Profitable, durable, trusted. Proof that single-tenant scales economically when you charge enough per user.
- **[Plausible Analytics](https://plausible.io)** — Open-source self-hostable + managed hosting. EU-based. Anti-Google-Analytics positioning. Direct philosophical and operational analog.
- **[Posthog](https://posthog.com)** — Open-source self-hostable + cloud. More complex product, similar dual-track model.
- **[Ghost](https://ghost.org)** — Open-source publishing platform + managed hosting where each blog is its own instance. Non-profit foundation governance — interesting structural reference.
- **[Tailscale](https://tailscale.com)** — Mesh architecture, no central data infrastructure. Coordination server only handles metadata; user devices hold their own state. Closest structural analog to our orchestration model.
- **[Supabase](https://supabase.com)** — Open-source Firebase alternative, self-hostable + managed. Enterprise plans offer dedicated infrastructure per customer.
- **[Soverin](https://soverin.net)** — Privacy-focused managed email, per-user isolation, EU-based. Niche but durable.

What unites them: **the architecture is the promise**. They don't claim privacy/ownership; they structurally provide it. Result: high customer trust, low churn, defensible against larger competitors who cannot match the architecture without rebuilding their entire stack.

### 10.4a Theoretical depth as one axis of differentiation

Mem0, Letta, Graphiti, Cognee, LangMem, and Supermemory all operate at the storage-extraction layer: better chunking, better graph topology, better retrieval ranking. None of them publicly grounds the architecture in a coherent theory of cognition. This isn't a knock — it's how the market matured around benchmark performance. But it leaves an axis open: when asked "why is your system shaped this way?" the honest answer from most competitors today is "because it benchmarks well."

Ours can be: append-only-with-consolidation is CLS; tiered latency is hippocampus-neocortex; salience is prediction-error. That's the foundation for the recursive self-improvement story (I7) — you can't recursively improve what you can't theoretically justify. `consolidator:v0` demonstrates this isn't post-hoc rationalization: the implementation came first, the theory recognized it.

**The honest framing**: theory is a *complement* to benchmarks, not a substitute. Mem0 wins LongMemEval today; we don't yet have public benchmark numbers. Once Phase 4+ benchmarks land alongside the existing CLS+PP grounding, "we measured AND we can explain why this shape" becomes a stronger combined position than either alone. Until then, theoretical depth is a brand pillar that quietly raises the bar competitors have to meet — not a winning argument on its own.

### 10.5 Structural Analysis: Why None Of Them Wins This

Four structural facts that close the field:

**A. Lock-in incentive trap.** Every major AI lab profits from owning user context. Anthropic, OpenAI, Google, and Cursor cannot ship cross-vendor memory without cannibalizing their primary moat. They will ship better *internal* memory; they will not ship portable memory.

**B. US-first regulatory blindness.** Mem0, Letta, Zep, Supermemory all built for US developers first. The EU AI Act's 2026 enforcement and GDPR's mature jurisprudence create requirements they will need years to retrofit. EU-native architecture is a multi-year head start, not a feature toggle.

**C. Monolith vs. swarm.** All current frameworks ship monolithic memory pipelines. The swarm pattern (multi-agent, role-specialized, mode-switching) is research and internal infrastructure only. LinkedIn proves it works at scale; nobody has productized it.

**D. Imposed vs. emergent schema.** Every framework ships with a taxonomy (episodic/semantic/procedural, or tier-based, or flat KV). Each forces user cognition into a pre-built shape. The emergent-ontology approach is unbuilt; constructing it is genuinely hard, which is why everyone has avoided it.

**E. Multi-tenant vs. single-tenant.** Every venture-funded memory startup is multi-tenant by necessity (margins demand it). Single-tenant per-user infrastructure is structurally incompatible with their unit economics. They cannot pivot to single-tenant without abandoning their growth thesis.

A new entrant that holds all five positions simultaneously — cross-vendor, EU-native, swarm-based, emergent, single-tenant — exists in an empty quadrant.

---

## 11. Why This Wins

Six points, each correspondingly hard to replicate:

1. **Cross-vendor by structural design.** MCP-first. Works with every agent that speaks MCP. Cannot be matched by labs whose business model is lock-in.

2. **Single-tenant by design.** Every user instance is physically isolated — own Fly machine, own SQLite, own state. Cannot be matched by venture-funded competitors whose margins depend on multi-tenancy.

3. **EU-native compliance.** Designed for GDPR + AI Act from day one. Data residency, audit trails, right-to-erasure built into the substrate model and the deployment topology (destroy a machine = full data deletion). US-first players retrofit; we start there.

4. **Emergent ontology.** No two users' vaults look alike after a few months. The system becomes a cognitive fingerprint. Generic "memory layers" cannot compete on personalization because they're statically schemaed.

5. **Society-of-Mind swarm.** Each memory operation is handled by the right specialist at the right cost. Heterogeneous models per agent. Independent A/B testing. The orchestration is the IP; models are commoditized.

6. **User-owned as political position.** This is not just architecture. It is a stance: *your cognition belongs to you, not to your AI provider*. That position becomes a brand, a community, and a long-term moat. Same logic that made 1Password and Fastmail durable.

---

## 12. Anti-Patterns — What This Is Explicitly Not

- **Not multi-tenant. Ever.** Even when hosted, every user gets a dedicated isolated instance. Multi-tenancy is forbidden architecturally. If a feature requires shared user infrastructure to be viable, that feature is wrong.
- **Not a marketplace, plugin store, or skill ecosystem at launch.** Dead weight. Maybe later, not before product proves itself.
- **Not a UI app.** First interface is via MCP from existing AI tools. Building a custom UI competes with the consumers, not complements them. A minimal management dashboard for the hosted offering is acceptable.
- **Not multi-user-on-one-instance before single-user works.** If it doesn't help one person, it won't help five. Sharing across instances is a separate problem for a later phase.
- **Not hosted before self-hosted is stable.** Hosting is a convenience layer over a working local product. Reverse order is fatal.
- **Not vector-only retrieval.** Vector similarity is necessary but radically insufficient. Hybrid (semantic + keyword + entity + graph) from the start.
- **Not a fixed schema as default.** Bootstrap scaffold yes; permanent ontology no. If shipped with a fixed schema, the project has failed Invariant I6.
- **Not a memory framework "for developers."** Developers are the first users because they have the tool stack. The product is for users, not as a dev tool.
- **Not VC-funded if it forces multi-tenancy.** The unit economics of single-tenant + open-source-core + managed-hosting can support a sustainable business, but probably not a billion-dollar exit. If raising capital requires abandoning Invariant I8, do not raise.

---

## 13. Open Questions

Honest list of what is not yet figured out:

- **Substrate granularity.** What is an atomic event? A keystroke is too fine; a session is too coarse. Likely answer: surprise-bounded segmentation à la [HiMem (arXiv:2601.06377)](https://arxiv.org/pdf/2601.06377), but unproven for this use case.
- **Multi-modal indexing.** Code, prose, audio, images, structured data in one substrate. Content addressing solves storage; cross-modal retrieval is open.
- **Invariant enforcement under self-modification.** The system can modify its rules. What stops it from modifying its way out of correctness? Likely answer: a thin kernel of invariants protected by external (out-of-system) checks. Mechanism unclear.
- **Identity resolution.** When you switch device, lose a session, or share an agent with someone briefly — who is the user? This is unsolved in the field broadly. In our single-tenant model, partially solved by "one instance = one user."
- **Pricing.** Managed hosting subscription must cover Fly costs + management margin + premium features. Indicative range: €15–30/month per user, but the real number depends on actual Fly costs once load is measured. Family/group plans? Unclear. Cross-cuts with §16 — different OSS models suggest different price floors.
- **Licensing / source availability.** See §16. Deferred to a Phase-6 review with real data.
- **Scope/Context handling.** Even in single-user single-tenant, the user has multiple life contexts (work / personal / family / hobby). Fingerprint-based emergent clusters from §5.5 thinking apply, but exact retrieval scoping behavior is undefined.
- **Naming.** See §15.

---

## 14. Source Material

### Papers
- McClelland, J. L., McNaughton, B. L., & O'Reilly, R. C. (1995). [Why there are complementary learning systems in the hippocampus and neocortex: insights from the successes and failures of connectionist models of learning and memory](https://stanford.edu/~jlmcc/papers/McClellandMcNaughtonOReilly95.pdf). *Psychological Review*, 102(3), 419–457. — Foundational CLS paper. Justifies our hot/warm/cold tiering biologically.
- Kumaran, D., Hassabis, D., & McClelland, J. L. (2016). [What learning systems do intelligent agents need? Complementary Learning Systems Theory updated](https://www.cell.com/trends/cognitive-sciences/fulltext/S1364-6613(16)30043-2). *Trends in Cognitive Sciences*, 20(7), 512–534. — DeepMind-era CLS update; Hassabis explicitly bridges CLS to deep-RL architectures. Directly relevant: `consolidator:v0` IS CLS replay.
- [Mem0: ECAI 2025 (arXiv:2504.19413)](https://arxiv.org/abs/2504.19413) — Production memory benchmarks.
- [Zep: A Temporal Knowledge Graph Architecture for Agent Memory (arXiv:2501.13956)](https://arxiv.org/abs/2501.13956) — Bi-temporal graph memory.
- [H-MEM: Hierarchical Memory for High-Efficiency Long-Term Reasoning (arXiv:2507.22925)](https://arxiv.org/abs/2507.22925)
- [HiMem: Hierarchical Long-Term Memory for LLM Long-Horizon Agents (arXiv:2601.06377)](https://arxiv.org/abs/2601.06377)
- [CraniMem: Cranial Inspired Gated and Bounded Memory (arXiv:2603.15642)](https://arxiv.org/abs/2603.15642)
- [Rethinking Memory Mechanisms of Foundation Agents: A Survey (arXiv:2602.06052)](https://arxiv.org/abs/2602.06052)
- [Generative Agents: Interactive Simulacra of Human Behavior (Park et al., Stanford)](https://arxiv.org/abs/2304.03442)
- [MemGPT: Towards LLMs as Operating Systems (arXiv:2310.08560)](https://arxiv.org/abs/2310.08560)
- [Unifying the ADHD Paradox: A Computational Model of Cognitive Specialization](https://sciety.org/articles/activity/10.31234/osf.io/frsp4_v5)
- [The Weak Coherence Account: Detail-focused Cognitive Style in ASD (Happé & Frith, 2006)](https://link.springer.com/article/10.1007/s10803-005-0039-0)
- [Hippocampal Replay Prioritizes Weakly Learned Information (Schapiro et al.)](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC6156217/)
- [Sleep-Like Replay Reduces Catastrophic Forgetting in ANNs (Nature Comms)](https://www.nature.com/articles/s41467-022-34938-7)
- [Lives Without Imagery: Aphantasia (Zeman et al., 2015)](https://www.sciencedirect.com/science/article/pii/S0010945215000532)
- [The Savant Syndrome (Treffert, 2009)](https://royalsocietypublishing.org/doi/10.1098/rstb.2008.0326)
- [Triple Network Model / Salience Network (Menon, 2011)](https://www.sciencedirect.com/science/article/pii/S1364661311001148)

### Frameworks & Repos
- [Mem0](https://github.com/mem0ai/mem0)
- [Letta](https://github.com/letta-ai/letta)
- [Zep](https://github.com/getzep/zep) / [Graphiti](https://github.com/getzep/graphiti)
- [Cognee](https://github.com/topoteretes/cognee)
- [Supermemory](https://github.com/supermemoryai/supermemory)
- [LangMem](https://github.com/langchain-ai/langmem)
- [MemAgents Workshop ICLR 2026](https://openreview.net/pdf?id=U51WxL382H)
- [Agent Memory Paper List (curated)](https://github.com/Shichun-Liu/Agent-Memory-Paper-List)
- [Awesome AI Agents 2026](https://github.com/ARUNAGIRINATHAN-K/awesome-ai-agents-2026)

### Industry References
- [State of AI Agent Memory 2026 (Mem0)](https://mem0.ai/blog/state-of-ai-agent-memory-2026)
- [Best AI Agent Memory Systems 2026 (Vectorize)](https://vectorize.io/articles/best-ai-agent-memory-systems)
- [LinkedIn Cognitive Memory Agent (InfoQ)](https://www.infoq.com/news/2026/04/linkedin-cognitive-memory-agent/)
- [Society of Mind: The 50-Year-Old Blueprint (Lanham)](https://medium.com/@Micheal-Lanham/society-of-mind-the-50-year-old-blueprint-for-ai-agents-b0e62eb4ec06)
- [AI Agent Memory: Auto Dream and Context Files](https://dev.to/max_quimby/ai-agent-memory-in-2026-auto-dream-context-files-and-what-actually-works-39m8)

### MCP Documentation
- [Anthropic MCP overview](https://docs.anthropic.com/en/docs/agents-and-tools/mcp)
- [Codex MCP](https://developers.openai.com/codex/mcp)
- [Cursor MCP](https://docs.cursor.com/context/mcp)
- [MCP specification](https://modelcontextprotocol.io)

### Foundational Reads
- Marvin Minsky, *Society of Mind* (1986).
- Daniel Schacter, *Searching for Memory* (1996).
- Endel Tulving, *Elements of Episodic Memory* (1983).
- Vannevar Bush, *As We May Think* (1945) — [Atlantic original](https://www.theatlantic.com/magazine/archive/1945/07/as-we-may-think/303881/).
- Karl Friston, free-energy principle / predictive coding — see [The free-energy principle: a unified brain theory? (2010)](https://www.nature.com/articles/nrn2787).

---

## 15. Naming

### Current Status

**Codename: `neverforget`** — used throughout development for repo, file paths, internal references. Says what the product does. Tippable. Will be retired when the final name is chosen.

### Why the final name is deferred

Naming attempts during pre-build phase exhausted the `.ai` namespace and most short English words. Lesson: the `.ai` TLD is saturated (every reasonable 2024-era memory/mind/brain word is taken by squatters or dead projects). Naming was consuming energy that should go into building.

Decision: **defer final naming to Phase 6-7**, when the product has shape, audience is clearer, and the right name will be more obvious. Until then, `neverforget` is sufficient.

### Naming criteria for the final name

When the time comes, the final name should:

- Be **one word, two syllables maximum**.
- Convey ownership, durability, or memory **without literally saying "memory" or "mind"** (those clusters are exhausted).
- Be **available beyond `.ai`** — `.com`, `.so`, `.computer`, `.app` are equally valid; do not over-index on `.ai`.
- Carry no negative cultural weight in major languages (German, English, French; secondarily Spanish, Italian).
- Be **non-descriptive on purpose** (Linear, Cursor, Notion, Vercel pattern) — let the product give the name meaning, not the other way around.

### Candidates explored (for reference)

The following were considered seriously and either had unavailable domains or didn't survive critical review. Kept here as audit trail in case a related variant becomes available later.

- **Mneme** *(Greek goddess of memory)* — strong positioning, domains taken.
- **Memex** *(Vannevar Bush, 1945)* — best historical positioning, trademark/usage concerns.
- **Tessera** *(mosaic tile)* — matches emergent-ontology thesis, domains taken.
- **Engram** *(memory trace, Karl Lashley)* — technical and evocative, domains taken.
- **Loom** *(weaving)* — matches swarm thesis, heavily used elsewhere.
- **Stele** *(inscribed stone pillar)* — matches substrate immutability, needs explanation.
- **Numen** *(Latin "inner spirit of a thing")* — strong semantics, domains taken.
- **Aevi** *(from Latin "aevum", age/eternity)* — modern-sounding, domains taken.
- **Ken** *(Old English "knowledge")* — short, distinct, domains taken.
- **Trace, Wake, Solum, Atrium, Nous** — all explored, all unavailable.

Pattern that did not work: anything in the form `[number]mind`, `[number]brain`, `[number]memory`, `[adjective]mind` is exhausted.

Pattern most likely to work in Phase 6-7: a real word with **adjacent (not descriptive) semantic meaning**, in `.com` rather than `.ai`. Reference cases: Cursor, Linear, Notion, Arc, Granola, Loop.

---

## 16. Licensing & Source Availability

### Current Status

**Decision deferred to a Phase-6 review.** Until then, **build everything as if the repo goes public tomorrow** (see "operational rule" below).

The Open Source Release in §9 Phase 7 is described as a *capability gate*, not a foregone conclusion. Whether the core ships under an OSS license (AGPL3 / Apache2 / MIT), as source-available (BSL / SSPL), or remains proprietary — that decision is made with real adoption + competitive + operational data, not committed in advance.

### Why the decision is deferred

Three reasons:

1. **The market isn't settled.** As of Phase 0/1, direct memory-framework competitors split: 5 of 9 fully open (Mem0, Letta, Cognee, Graphiti, LangMem), 1 open-core (Zep), 3 closed (Supermemory, Anthropic Memory, OpenAI Memory). The right answer depends on which model is winning at Phase-6 entry.
2. **We don't yet know what's load-bearing.** The orchestration layer (per-user Fly provisioning, billing, web UI) might be the only piece worth keeping closed; the substrate + MCP + agents may be totally fine open. We cannot draw that line cleanly without operational experience.
3. **Premature commitment closes doors.** Locking in "closed" now invites lock-in patterns into the architecture. Locking in "open" now exposes half-built work to public scrutiny. Both are bad in opposite ways.

### Operational rule until decision time

> **Build everything as if it goes public tomorrow.**

This is the only stance that keeps both doors open without expensive rewrites later. Concretely:

- Code quality, naming, comments, architecture — **production-grade always**.
- **No secrets in code, no hardcoded credentials, no internal jokes, no slurs against competitors** in code or commit messages.
- **No proprietary algorithmic tricks that depend on staying secret for their value.** If the algorithm is the moat, the moat is fragile.
- **No lock-in mechanisms** — no anti-export, no phone-home, no kill-switches, no DRM-style checks. Self-hosting is a first-class deployment (Invariant I4).
- **Telemetry minimal, off-by-default, anonymized** when on. Documented in the README.
- **Tests + docs at every layer.** A stranger should be able to read the repo and understand what's happening.
- **Commit messages and PR descriptions reviewable by a stranger.** Conventional commits, imperative voice, no "fix the thing" subjects.
- **Dependency licenses tracked.** All transitive deps Apache2/MIT/BSD-compatible so an open release isn't accidentally GPL-tainted (or vice versa, so a closed release isn't accidentally AGPL-poisoned by a transitive).
- **No code that we'd be embarrassed to ship in public.** Same standard whether the audience is one engineer or ten thousand.

### What gets decided at Phase 6

When we reach Phase 6 (first external user maintaining 30 days of daily use), we re-evaluate with:

- Real adoption numbers for OSS vs closed memory frameworks
- Direct competitor moves (consolidations, license flips, exits)
- Our own operational maturity (can we maintain an OSS community? do we have the bandwidth for issue triage?)
- Customer signal (do prospective paying users care about "really open" or do they just want "I can export my data"?)
- Legal climate (AI Act enforcement, dependency-license sweeps, regulator stances on open-source AI)

### Decision criteria

When the call comes, the choice matrix:

| Option | When it's the right answer |
|---|---|
| **AGPL3 core + closed orchestration** (PostHog / Plausible model) | Trust-driven sales motion; EU/regulated buyers want forks-stay-open; we want viral copyleft |
| **Apache2 core + closed orchestration** (Supabase / Mem0 model) | Maximum adoption; enterprise-friendly; willing to accept the risk of commercial forks |
| **BSL / source-available** (Sentry / Cockroach pattern) | Want to prevent specific cloud-provider forks while staying mostly transparent |
| **Closed source** (1Password / Fastmail model) | Service quality + brand is the moat, not the code; small team, no community capacity |

**Default expectation if no strong signal emerges**: Apache2 core + closed orchestration, mirroring Supabase / Mem0 — both solved the same problem we're solving (user-owned + managed hosting + adoption).

### What this section does NOT change

- Invariant I4 (user owns the substrate) remains binding regardless of license choice. Even closed-source can satisfy I4 via documented export + binary self-hosting; OSS just makes the proof easier.
- Invariant I1 (MCP surface stability) is independent of license — the protocol surface is forever, not the implementation behind it.
- Invariant I8 (single-tenant) is architectural, not legal — applies to both OSS and closed deployments.

---

*End of Constitution. Read this first. Question everything except §4.*
