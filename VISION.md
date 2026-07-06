# afair: Operational Constitution

> *A user-owned, vendor-neutral, self-organizing cognitive memory layer for AI agents: built behind a stable MCP surface, free to mutate beneath it.*

---

## 1. Vision

> **Every individual owns a digital extension of their mind: portable across every AI, every surface, every vendor.**

Today the vendors own your context; you rent space in their vaults. This project inverts that: the user owns the substrate, the AI tools are clients.

A cognitive sovereignty layer for the next decade. What password managers did for credentials, this does for context across AI silos.

---

## 2. Mission

> **Build the first user-owned, vendor-neutral, self-organizing cognitive memory system: exposed via MCP, evolving freely beneath that interface, fully self-hostable, with managed single-tenant hosting as the commercial layer.**

The product model is not SaaS. It is software anyone can self-host, with an optional managed deployment where every paying user gets a dedicated isolated machine: one user, one machine, one vault on disk they control. Never multi-tenant. Never shared infrastructure for user data.

Four non-negotiable principles:

1. **User owns the substrate.** Self-hosting is first-class, not a fallback.
2. **Single-tenant always.** Every instance belongs to exactly one user. Managed hosting is "managed self-hosting," not SaaS.
3. **Cross-vendor by default.** Claude, GPT, Gemini, Mistral, local models: equal citizens. If it only works with one provider, it has failed.
4. **Schema is emergent.** Minimal bootstrap scaffold; everything else grows from interaction.

---

## 3. Why Now

Several converging conditions make this the right moment.

- **MCP has become the universal protocol.** [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex CLI](https://developers.openai.com/codex/mcp), [Cursor](https://docs.cursor.com/context/mcp), Copilot, Windsurf, Cline: all speak MCP. The cross-vendor surface exists for the first time; a single server reaches everything.
- **Per-user dedicated infrastructure is economically feasible.** [Fly.io](https://fly.io) puts single-tenant deployment at $3–8/month per user instance for typical workloads. Combined with Fly's automatic volume snapshots (and optional continuous SQLite replication via LiteFS Cloud or Litestream when sub-second RPO matters), the architecture that used to be reserved for premium tools (Fastmail, 1Password) is now viable for individual products.
- **Memory is a recognized infrastructure category, and structurally locked in.** [Mem0 raised $24M](https://mem0.ai/series-a) (Oct 2025), Letta $10M, Supermemory $2.6M, Zep growing. All US-centric, vendor-leaning, multi-tenant SaaS, all built around imposed schemas.
- **The newest bet locks memory in even harder.** A wave of well-funded labs is moving memory out of a store and into the model itself, training your context directly into the weights (Engram raised $98M in June 2026 to do exactly this). It buys speed, but the memory becomes unreadable, non-portable, and impossible to correct, bound to a single model. That is the structural opposite of user ownership, and it makes a vendor-neutral, inspectable, correctable substrate more valuable, not less.
- **EU regulatory tailwind.** GDPR's right-to-be-forgotten and the AI Act (fully applicable August 2026, 10-year audit-trail requirement for high-risk systems) create a structural advantage for EU-native, user-owned architectures. Per-user dedicated instances make compliance physically obvious rather than legally complex.
- **The field knows memory is unsolved.** [Mem0's State of AI Agent Memory 2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026) names what is still open: cross-session identity resolution, memory staleness in high-relevance memories, the noise floor problem.

---

## 4. Constitutional Invariants

**Inviolable. Every architectural decision must preserve them. If a future evolution threatens an invariant, the evolution is wrong, not the invariant.**

### I1. MCP Surface Stability
External tool signatures are versioned and additive. New tools may be added. Existing signatures and semantics never break. Deprecation requires two major versions of support plus migration tooling. A consumer that integrated at v1.0 must still function unchanged at v9.0.

### I2. Substrate Immutability
The raw event log is append-only and content-addressed. Nothing is ever overwritten or deleted (except via explicit user-invoked right-to-erasure paths, which are themselves logged). The substrate is git-like: history is the truth.

I2 protects the user's **memory** — the events, interpretations, entities, edges, and temporal/belief metadata that constitute what the vault remembers. It does **not** cover purely operational tables: a regenerable suggestion queue (`proposed_corrections`), an ephemeral job record (`export_jobs`), or the pipeline's operational flight recorder (`pipeline_events`, `observability_snapshots`). Those carry no user memory, are never recalled, and are deliberately non-substrate: no append-only triggers, and the Pruner ages the telemetry out past a retention window. Pruning the flight recorder is no more an I2 erasure than rotating a log file is. See ADR-0005.

### I3. Backward-Compatible Evolution
Any data written by an earlier version must remain readable, queryable, and re-interpretable by every later version, forever. Schema migrations are not migrations, they are new materialized views over unchanged substrate.

### I4. User Ownership By Default
The substrate lives on disk under the user's control. Self-hosting is first-class. Hosted offering may exist but must never become structurally required. Export and full local operation are baseline guarantees, not premium features.

### I5. Vendor Neutrality
No code path may privilege one AI provider. The architecture must function with Claude, GPT, Gemini, Mistral, and local models, with feature parity where the model class allows.

### I6. Emergent Over Imposed
No fixed ontology of memory types ships with the system. A minimal bootstrap scaffold is acceptable; the system must be able to revise, merge, split, and discard categories based on usage. Forever.

### I7. Recursive Self-Modification with Rollback
The system may revise its own extraction rules, retrieval strategies, and agent compositions at runtime. Every modification is recorded in the substrate. Every modification is reversible. Invariants I1–I6 are exempt, they are the irreducible kernel.

### I8. Single-Tenant by Design
Every deployed instance, self-hosted or managed, belongs to exactly one user. No shared database, no shared application server, no row-level user separation. The hosted offering provisions a dedicated machine per paying user. Multi-tenancy is forbidden architecturally, not just practically. The orchestration layer that manages billing and provisioning may be shared; user data and application state never are.

> **Why these eight, and where their real risks are:** the reasoning behind the invariants, each as the negation of a specific failure mode, how they reinforce one another, and the accepted long-term bets they carry, is recorded in [ADR-0001](docs/adr/ADR-0001-constitutional-invariants.md). This text stays authoritative; the ADR explains it.

---

## 5. Core Thesis

### 5.1 What is actually unsolved

Despite mature frameworks ([Mem0](https://github.com/mem0ai/mem0), [Letta](https://github.com/letta-ai/letta), [Zep/Graphiti](https://github.com/getzep/graphiti), [Cognee](https://github.com/topoteretes/cognee), [Supermemory](https://github.com/supermemoryai/supermemory)), the field's own state-of-the-art summaries name these as genuinely open:

- **Memory staleness in high-relevance memories.** A user changes jobs; the system "knows" the old employer with high confidence and surfaces it. Decay handles low-relevance memories; this is unsolved for high-relevance ones.
- **Cross-session identity resolution.** Anonymous sessions, multi-device, mixed auth flows break the stable-user-id assumption.
- **The noise floor.** Agents accumulate so much "important" information that memory search becomes slower than processing full context.
- **Schema lock-in.** Every framework imposes a categorization (episodic/semantic/procedural, or flat key-value). User cognition does not fit that schema.
- **Vendor lock-in.** Every framework is structurally tied to its host ecosystem.

Sources: [Mem0 ECAI 2025 (arXiv:2504.19413)](https://arxiv.org/abs/2504.19413), [State of AI Agent Memory 2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026), [Best AI Agent Memory Systems 2026](https://vectorize.io/articles/best-ai-agent-memory-systems).

### 5.2 Memory phenotypes: cognitive diversity as design space

Human cognitive variation, historically pathologized, is a catalog of evolutionary strategies, each optimized for different contexts. A memory system built on a single (neurotypical) model leaves most of the design space unexplored.

This is not metaphor. Each variant maps to a computational pattern that solves specific memory tasks better than the neurotypical baseline.

**ADHD.** Constrained working memory forces depth-first search through conceptual space. The inattention/hyperfocus "paradox" is a coherent strategy, not a defect. ([Unifying the ADHD Paradox, 2025](https://sciety.org/articles/activity/10.31234/osf.io/frsp4_v5)). Maps to: an exploration-favoring agent that switches contexts hard, dives deep, and does not hold parallel state. Clinically also a **mode-switching failure** between default-mode and central-executive networks ([Bossong et al., 2013](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC3729458/)): instructive as a cautionary failure mode for the salience switcher.

**Autism / weak central coherence.** Detail-focused, local processing prioritized over global gestalt. Verbatim recall over gist. Pattern recognition over context integration. ([Happé & Frith, 2006](https://link.springer.com/article/10.1007/s10803-005-0039-0)). Maps to: an extractor that preserves raw form and retrieves via pattern matching rather than embedding similarity. Wins on code, legal text, anywhere exact form matters.

**Asperger / high-functioning systematizing.** Strong systems thinking, narrow deep specialization, exceptional pattern recognition within domains of interest. ([Baron-Cohen, empathizing–systemizing theory](https://www.cambridge.org/core/journals/the-british-journal-of-psychiatry/article/empathizingsystemizing-theory-an-update/)). Maps to: domain-specialized agents with deep retrieval, narrow lateral spread. The "expert mode" of a personal vault.

**Synesthesia.** Cross-modal binding. One type of input triggers another, often correlated with strong memory through multi-channel encoding. Maps to: an indexing agent that cross-references modalities (text + time + author + project + emotional valence), enabling retrieval through any dimension.

**Aphantasia.** Absence of voluntary mental imagery. Memory encoded propositionally rather than visually. ([Zeman et al., 2015](https://www.sciencedirect.com/science/article/pii/S0010945215000532)). Maps to: a fallback when image-based or spatial encoding is unavailable: pure propositional substrate. A reminder that not all useful memory needs to be embedding-based.

**Hyperthymesia.** Exhaustive recall of personal events with temporal precision. ([McGaugh & LePort](https://www.sciencedirect.com/science/article/pii/S1364661312001428)). Maps to: an agent dedicated to temporal indexing: every event timestamped, queryable by date, never compressed. Expensive; the only honest answer for therapy-, companion-, or audit-grade use cases.

**Savant syndrome.** Narrow extreme expertise (calendar calculation, prime factorization, musical recall), often co-occurring with autism. ([Treffert, 2009](https://royalsocietypublishing.org/doi/10.1098/rstb.2008.0326)). Maps to: highly specialized, narrow-domain retrieval agents, willing to be useless outside their domain because excellent within it.

**Hippocampal replay during sleep.** Not a disorder: a biological mechanism worth direct emulation. Weakly-learned memories are prioritized for offline replay; consolidation strengthens some, prunes others. ([Schapiro et al., 2018](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC6156217/), [Golden et al., 2022](https://www.nature.com/articles/s41467-022-34938-7)). [Auto Dream in Claude Code](https://dev.to/max_quimby/ai-agent-memory-in-2026-auto-dream-context-files-and-what-actually-works-39m8) is the first production implementation. The cold-path consolidator in this system is its descendant.

These are not neurotypical-with-bugs. They are alternative architectures, each adaptive in some niche. A memory system that can instantiate the right phenotype for the right task, and switch between them, beats one fixed architecture pretending to be universal.

### 5.3 Society of Mind

Minsky's 1986 thesis: the mind is not a single thing but a society of specialized sub-agents, each individually unintelligent, collectively producing cognition. ([Society of Mind retrospective, 2026](https://medium.com/@Micheal-Lanham/society-of-mind-the-50-year-old-blueprint-for-ai-agents-b0e62eb4ec06)).

The contemporary instantiation is the multi-agent swarm. LinkedIn shipped this as internal infrastructure in April 2026 ([Cognitive Memory Agent](https://www.infoq.com/news/2026/04/linkedin-cognitive-memory-agent/)). ICLR 2026 hosts a [MemAgents workshop](https://openreview.net/pdf?id=U51WxL382H). No one has shipped it as a cross-vendor user-owned product.

### 5.4 Triple Network coordination

Neuroscience converged on the Triple Network Model, three large-scale brain networks that coordinate cognition.

- **Default Mode Network (DMN)**: medial prefrontal cortex, posterior cingulate, hippocampus. Self-referential, consolidation, mind-wandering, future planning. Active during rest. ([Andrews-Hanna review](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC4427863/)).
- **Central Executive Network (CEN)**: dorsolateral prefrontal cortex. Task-focused, working memory, decision-making. Active during effortful cognition.
- **Salience Network (SN)**: insular cortex, anterior cingulate. Detects salient internal/external events and switches between DMN and CEN. ([Menon, 2011](https://www.sciencedirect.com/science/article/pii/S1364661311001148)).

DMN and CEN are mutually inhibitory. The SN does the switching. The thalamus underneath is the routing layer.

This is the architectural blueprint:

- A **router** (thalamic, dumb relay): the MCP surface.
- A **salience switcher** (mode selector): a meta-agent.
- Two complementary sub-swarms: DMN-like for reflection, CEN-like for task work.

ADHD-style failures (DMN does not deactivate during CEN tasks) are the cautionary case: if the salience switcher is miscalibrated, the system thrashes.

### 5.5 Emergent ontology

Locke's "tabula rasa" is biologically wrong, even infants come with [core knowledge priors](https://www.ncbi.nlm.nih.gov/books/NBK395094/) (object permanence, agency, number, statistical learning). Categories themselves emerge from interaction (Piaget, Saffran).

A minimal bootstrap scaffold (initial extraction templates) is acceptable. The system must then **subtractively learn**: pruning, merging, revising its own ontology toward a categorization that fits *this user's* actual life, not a generic schema.

LLMs make this faster than biology. The model already encodes most human categorical structure; the system's job is to carve out the subset relevant to one user. Days, not years.

---

## 6. Architecture

### 6.1 The layered stack

```
                External (Claude Code, Codex, Cursor, Copilot, …)
                External Sources (Gmail, Calendar, Drive, Slack, …)
                                    │
                                    │  (queries AND incoming events
                                    │   enter through the same router)
                                    ▼
                          ┌────────────────────┐
                          │   MCP Router       │   ← Thalamus
                          │   (stable forever) │     dumb relay, versioned
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

**The rule:** nothing writes to the substrate without passing Router → Salience → Swarm. External ingestion is not a privileged path. Like the thalamus in the brain, every sensory input is routed and filtered before it touches the cortex. The only exception is the system's own audit log of meta-actions (recorded directly to substrate as system events), analogous to the few brain pathways that bypass the thalamus (e.g., olfactory): small, well-defined exceptions, never the default.

**Theoretical grounding.** The CEN/DMN/Salience labels are Menon's Triple Network Model, they communicate *what* each module does. Two deeper frameworks carry *why* the architecture is shaped this way:

1. **Predictive Processing / Active Inference** (Friston). The substrate is a generative model of the user's world; every agent (extractor, bind, salience, recall, consolidator) is an inference process minimizing prediction error. Surprise: divergence between predicted and observed substrate state, is the load-bearing salience signal, not an engineered priority score.
2. **Complementary Learning Systems** (McClelland, McNaughton & O'Reilly 1995; updated in Kumaran, Hassabis & McClelland 2016). Memory splits between a fast, sparse, episodic store (hippocampus-like: append-only substrate) and a slow, distributed, semantic store (neocortex-like, interpretation layer and consolidator abstractions). One system cannot simultaneously learn fast (catastrophic interference) and generalize well. Same constraint here; same solution.

Triple Network is the visible architecture. CLS + PP is the theory that says it has to be this shape.

### 6.2 MCP surface (Invariant I1)

The external contract. Whatever changes underneath, these signatures hold.

The v1 verb set, the forever-surface:

- `remember(content, context?, type_hint?, parent_hashes?, invalidates?)`: explicit note. `type_hint` is advisory; the extractor ignores it if a better classification emerges. `invalidates` supersedes prior facts in one call (each target gets an append-only invalidation event; I2 preserved).
- `recall(query?, scope?, depth?, limit?, by_id?, by_content_hash?, full_payload?, stats?)`: the single retrieval verb. Five call modes coexist behind one signature:
  - `recall(query=...)`: semantic + FTS hybrid (default).
  - `recall(by_id=...)` / `recall(by_content_hash=...)`: single-event fetch.
  - `recall(stats=True)`: vault overview.
  - `recall(full_payload=True)`: un-truncated payload for matched hits.
  - `depth` controls reasoning effort (cheap lookup → full swarm consultation).
- `observe(event)`: structured logging of agent actions. Used by Claude Code, Codex, etc. to log their own work into the user's substrate.

Three verbs. Future capability is added via new tools or new optional kwargs, never via signature changes to these three. New tools appear with version suffixes. Old signatures keep working. When a tool becomes obsolete, it stays callable but emits a deprecation notice; removal requires two major versions of overlap.

### 6.3 Substrate layer (Invariant I2)

Append-only event log on local disk. Content-addressed (each event hashed). Git-friendly. Inspectable with `cat` and `sqlite3`.

```
~/vault/
├── substrate/
│   ├── events/        ← raw observation log, sharded by date
│   ├── ingested/      ← raw imports from external services
│   └── decisions/     ← system meta-actions
├── interpretations/   ← materialized views, regenerable
│   ├── v1/
│   ├── v2/
│   └── …              ← multiple versions coexist
├── ontology/          ← emergent category state
│   └── versions/
└── meta/
    ├── rules/         ← current extraction/retrieval policies (versioned)
    └── manifest.toml  ← current schema version, agent compositions
```

Each event carries: timestamp, source (agent or external system), content hash, payload, optional links to parent events. Nothing is ever updated. Corrections are new events that reference the corrected event.

### 6.4 Interpretation layer (Invariant I3)

The mutable, regenerable layer. Embeddings, extracted facts, graph edges, emergent categories. All produced *from* the substrate, never replacing it.

When extraction rules improve (new agent, better prompt, larger model), the system re-runs them over the substrate and produces a new interpretation version. Old versions remain for as long as anything references them. The user (or system) chooses which interpretation is "live."

This is what makes recursive self-improvement safe: the substrate is invariant, interpretations are versioned and reversible.

### 6.5 Agent swarm: hot, warm, cold

**Hot path (sync, target <300ms).** Retriever + cache only. No reasoning. Indexed lookups. ~95% of `recall` calls land here.

**Warm path (async, seconds).** A new observation arrives: from a user `remember` call, or from a Salience-routed `sense` event. Observer logs to substrate. Extractor processes async. By the time the user calls back, the new info is integrated. Caller never waits.

**Cold path (background, minutes to hours).** The sleep swarm. Runs during idle time. Consolidator re-clusters. Schema-Evolver revises ontology: it mines observed kind usage, drafts revision proposals, and the operator confirms or rejects each one before it takes effect. Conflict-Resolver handles contradictions. Pruner ages out unused interpretations. Queued low-priority ingestion events get integrated here: the system dreams over the day's accumulated input. Heavy thinking happens here; premium models earn their keep here.

Each agent runs on a different model class (cheap for Observer, premium for Schema-Evolver). Each agent's model is independently configurable: all default to a shared model, and per-agent overrides run premium models where they earn their keep. The judge panel is multi-vendor by default. Each agent A/B-tested independently. Each agent has its own memory phenotype (Extractor, autism-like verbatim; Salience-Detector, ADHD-like surprise-driven; Consolidator, hippocampal-replay-like).

User queries and external sensing events share the same event schema with different `origin` tags. The Salience Agent routes them through the appropriate path based on content, priority, and current mode. No separate ingestion pipeline.

### 6.6 External service bridge: continuous sensing

External services (Gmail, Calendar, Drive, Slack, GitHub) connect via MCP servers or native APIs. **They are sensing channels, not write paths.** Like sensory organs in biology, they generate event streams that enter through the same Router → Salience pipeline as user queries.

Three sensing modes:

- **Continuous sensing (background).** Pollers or webhooks watch external services. New items become incoming events that enter through the MCP Router as `sense(source, payload)` calls. The Salience Agent decides:
  - **Drop** (noise: newsletters, automated notifications): logged minimally for audit, not integrated.
  - **Queue cold**: integrate during the next idle phase.
  - **Process warm**: extract async via Extractor.
  - **Process hot**: high priority (e.g., email from a high-salience entity), integrate immediately and possibly surface.
- **Active perception (on-demand).** A user query needs fresh external data. The Salience Agent in CEN mode triggers a targeted fetch, processes it through the same pipeline, integrates the result before responding.
- **Cross-modal binding.** Once an external event has been classified (warm/hot), a Bind agent links it to existing substrate entries. Binding produces interpretation-layer edges, never substrate mutations.

Why not direct ingestion:

1. **Salience-filtered intake.** Two hundred newsletters a day do not deserve substrate entries. The salience filter mirrors how thalamus + reticular formation gate sensory input in the brain: without that gating, the cortex would drown.
2. **Mode-aware processing.** If the user is in active CEN work, expensive integration waits. If idle (DMN-active), incoming events get full consolidation treatment. Memory consolidation works this way biologically: heavy integration happens during rest, not focused activity.
3. **Adversarial robustness.** A compromised external connector cannot write directly to substrate. Every external event passes through Salience inspection. The architectural equivalent of "your brain cannot be directly programmed via your eyes."
4. **Uniform event model.** Queries and incoming events share one data structure. One pipeline, fewer special cases.

The swarm never hits external APIs during synchronous user-facing tool calls. Active perception is async; the user's tool call returns immediately with a "fetching" status if needed.

### 6.7 Deployment topology

The same binary runs in three contexts.

**Local (developer / power-user).** Runs on the user's own laptop. MCP server bound to `localhost`. SQLite file in `~/vault/`. No network surface. Fastest, most private. Requires the user to keep their machine running.

**Self-hosted (technical user).** Runs on the user's own server (home server, VPS, NAS). MCP bound to internal address or behind Tailscale / Cloudflare Tunnel. SQLite on the server's disk. User controls everything; user does the ops.

**Managed (paying user).** Each user gets a dedicated machine, provisioned automatically. Same binary, same SQLite, operated by us. Backup defaults to Fly automatic volume snapshots (~24h RPO, configurable retention). RPO upgrade paths: hourly snapshot cron, LiteFS Cloud namespace, or Litestream → user-owned S3-compat, are documented and picked per user's tier or self-host posture. The machine belongs to one user; if they cancel, the machine is destroyed (after retention window). Per-user dedicated URL provisioned via DNS API.

The three topologies use the same binary, same data format, same MCP surface. Migration between them is `tar`-and-`scp`. A managed user can export and self-host any time. A self-hosted user can hand over their SQLite and we run their existing vault. Portability is architecture, not feature.

The thin shared layer for managed hosting holds only orchestration metadata, which user has which machine, billing status, update windows. It never holds user substrate data. If the orchestration layer is offline, all managed user instances continue working.

---

## 7. Backward Compatibility Doctrine

Three rules, derived from Invariants I1–I3.

**Rule 1: MCP is forever.** A tool signature that ships in v1 must still work in v∞. New tools may be added. New optional parameters may be added. Semantics of existing tools may not change; existing tools may not be removed.

**Rule 2: Substrate is forever readable.** Any event format that has ever shipped must remain parseable. New extractors handle old formats gracefully (skip fields they don't understand, never error on legacy data). Old data is first-class data.

**Rule 3: Read old, write new.** When the substrate format evolves, new code reads both old and new formats, writes only the new. Old data stays untouched. Migrations are forbidden. New materialized views over old substrate are encouraged.

The architecture above satisfies all three. The substrate layer never mutates; only new views are added. The interpretation layer is versioned and parallel. The MCP surface is the stable contract.

---

## 8. Recursive Self-Improvement

What the system may modify about itself at runtime, without manual intervention.

**Modifiable at runtime.** Extraction prompts; retrieval strategies (weights, fusion functions); agent compositions (which agents run for which task); emergent categories (split, merge, rename, discard); cache and index structures; schedule of cold-path operations.

**Modifiable only with manual approval.** Changes to invariants I1–I7 (theoretically possible, practically forbidden); changes to substrate event schema (must be versioned and additive); addition or removal of agent roles; changes to the salience switcher's mode-decision logic.

**Never modifiable.** The append-only nature of the substrate; the principle that the user owns the data; the MCP surface contract for tools that have shipped.

Every self-modification is itself an event in the substrate. The full history of "why does the system behave this way at time T" is reconstructible. Rollback chooses an earlier rule version; the substrate is never altered by rollback, only the active rule pointer.

---

## 9. Competitive Landscape

For each competitor: what they have, what they don't, and the **structural reason** they cannot or will not close the gap.

### 9.1 Direct memory frameworks

**[Mem0](https://mem0.ai)**: Production memory infra, three-line integration, broad framework support, model-agnostic.
- **Doesn't have:** cross-vendor user-owned positioning, emergent ontology, swarm architecture, EU-native compliance.
- **Structural limit:** US-based, VC-backed, hosted-first. Revenue model rewards integration into their cloud, which contradicts user-owned primacy. They will optimize toward platform, not sovereignty.

**[Letta (ex-MemGPT)](https://www.letta.com)**: UC Berkeley spinout, OS-inspired tiered memory (core/recall/archival), self-editing agent memory.
- **Doesn't have:** cross-vendor MCP-first positioning, emergent ontology, multi-agent swarm, EU compliance focus.
- **Structural limit:** research-led, complex to deploy, oriented toward developers building stateful agents, not users owning context. The tiered architecture is imposed, not emergent. ([Letta docs](https://docs.letta.com)).

**[Zep / Graphiti](https://www.getzep.com)**: Temporal knowledge graph with bi-temporal model.
- **Has:** best-in-class temporal reasoning, conflict-as-data via t_valid/t_invalid edges, knowledge graph richness. [Zep paper (arXiv:2501.13956)](https://arxiv.org/abs/2501.13956).
- **Doesn't have:** multi-agent cognition, emergent ontology, user-owned local-first deployment, MCP-as-primary surface.
- **Structural limit:** enterprise SaaS positioning. Excellent technology; the business model demands managed deployment. Self-hosting allowed; not first-class.

**[Cognee](https://www.cognee.ai)**: Open-source memory + knowledge graph layer.
- **Has:** full local deployment, graph + vector hybrid, multi-source ingestion, GDPR-compatible.
- **Doesn't have:** emergent ontology, multi-agent swarm, MCP-first cross-vendor surface, opinionated memory phenotypes.
- **Structural limit:** library framing, not product. Lacks the consumer-facing thesis. Closest in spirit; furthest from execution polish.

**[Supermemory](https://supermemory.ai)**: MIT-licensed ([22.7K stars](https://github.com/supermemoryai/supermemory)), #1 on LongMemEval, LoCoMo, and ConvoMem. Aggressive multi-modal extraction (PDFs, OCR, transcription, AST-aware chunking). Adapters for Vercel AI SDK, LangChain, LangGraph, OpenAI Agents SDK, Mastra. Sub-300ms hybrid, ~50ms one-call profile retrieval. Explicit auto-forgetting / temporal-update handling.
- **Doesn't have:** user-owned positioning (they sell API/SDK to developers, not "your digital brain"), single-tenant-per-user, emergent ontology, swarm architecture, EU-native default.
- **Structural limit:** VC-backed multi-tenant economics. They architecturally cannot pivot to single-tenant-per-user without abandoning the growth thesis. Positioning is developer-tool, not user-product. **The closest direct competitor and the one to study: especially their benchmark and multi-modal execution. Differentiation is positioning + architecture, not memory-engine quality.**

**[GBrain](https://github.com/garrytan/gbrain)**: MIT, ~22.7K stars, built by Garry Tan (YC President). Single-developer "agent brain", viral on author profile + YC's company-brain RFS tailwind. Synthesis **with gap analysis** (`think`, tells you what the brain doesn't know yet, staleness, contradictions), **self-wiring typed knowledge graph** (zero-LLM edge extraction, benchmarked), 24/7 "dream cycle" overnight enrich/consolidate, multi-source ingestion (meetings/email/tweets/voice), 30+ MCP tools (OAuth 2.1/PKCE/DCR, scope tiers), broad client support incl. Perplexity + ChatGPT. Both single-tenant local (PGLite) **and** multi-tenant company-brain with fuzz-tested per-login isolation.
- **Has that we don't (yet):** gap-analysis, benchmarked zero-LLM graph, multi-source ingestion daemon, multi-tenant company-brain. In raw memory-engine terms it currently leads.
- **Doesn't have:** hosted product (DIY daemon: "your hardware, your DB, your keys"), EU/jurisdictional posture, durability/forever-API contract, emergent ontology (its typed `works_at`/`invested_in`/`founded` edges are an **imposed VC/CRM schema**, VISION §9.D applies directly), build-nothing end-user onramp (assumes you run OpenClaw/Hermes or wire a daemon yourself).
- **Structural limit:** it is an open-source tool you operate, not a product someone operates for you; opinionated to its author's stack; no jurisdiction, no managed isolation, no durability guarantee. afair's separation is **product-form + EU jurisdiction + emergent ontology + durability, not memory-engine quality, where GBrain leads today.** **Track as a first-class competitor.**

**[LangMem](https://github.com/langchain-ai/langmem)** / **[LlamaIndex Memory](https://www.llamaindex.ai)**: Framework-bound memory primitives. Outside their host frameworks, no value. Structural limit: tied to LangGraph / LlamaIndex.

### 9.2 Memory inside another product

**Anthropic / Claude (memory + [Auto Dream](https://dev.to/max_quimby/ai-agent-memory-in-2026-auto-dream-context-files-and-what-actually-works-39m8))**: Best-in-class consolidation, deep integration with their own agents. Claude-only by design. **Structural limit:** Anthropic's incentive is Claude lock-in. Cross-vendor memory would erode their primary moat. This is the structural opening.

**OpenAI / ChatGPT memory**: Same dynamic. Memory is lock-in, not portable asset.

**Cursor project context** (`.cursorrules` + RAG): Project-scoped, IDE-bound. Same structural limit.

**[LinkedIn CMA](https://www.infoq.com/news/2026/04/linkedin-cognitive-memory-agent/)**: April 2026 internal infrastructure. Multi-agent shared memory substrate. Not a product. Reference architecture for what is possible when a team with resources builds this properly.

### 9.3 Adjacent

**1Password / Bitwarden.** The cross-platform user-owned model for credentials. The strategic blueprint: vendor-neutral, user-owned, works everywhere: as a successful category.

**Obsidian / Logseq.** User-owned local-first knowledge bases. No agent layer. Strong philosophical alignment. Adjacent customer base.

**Notion / Roam.** Hosted, schema-imposing. Opposite positioning.

### 9.4 Business model references: per-user single-tenant hosting

Not memory products, but the business model is the reference: self-hostable + managed single-tenant hosting.

- **[Fastmail](https://www.fastmail.com)**: Per-account isolated email infrastructure since 1999. Profitable, durable, trusted. Proof that single-tenant scales economically when you charge enough per user.
- **[Plausible Analytics](https://plausible.io)**: Open-source self-hostable + managed hosting. EU-based. Direct philosophical analog.
- **[PostHog](https://posthog.com)**: Open-source self-hostable + cloud. More complex product, similar dual-track.
- **[Ghost](https://ghost.org)**: Open-source publishing + managed hosting where each blog is its own instance. Non-profit foundation governance, interesting structural reference.
- **[Tailscale](https://tailscale.com)**: Mesh architecture, no central data infrastructure. Coordination server only handles metadata; user devices hold their own state. Closest structural analog to our orchestration model.
- **[Supabase](https://supabase.com)**: Open-source Firebase alternative, self-hostable + managed. Enterprise plans offer dedicated infrastructure per customer.

The common pattern: **the architecture is the promise**. They do not claim privacy or ownership, they structurally provide it. Result: high customer trust, low churn, defensible against larger competitors who cannot match the architecture without rebuilding their stack.

### 9.5 Theoretical depth as one axis of differentiation

Mem0, Letta, Graphiti, Cognee, LangMem, Supermemory all operate at the storage-extraction layer: better chunking, better graph topology, better retrieval ranking. None publicly grounds the architecture in a coherent theory of cognition. Not a knock, that is how the market matured around benchmark performance. But it leaves an axis open: ask "why is your system shaped this way?" and most competitors' honest answer today is "because it benchmarks well."

Ours can be: append-only-with-consolidation is CLS; tiered latency is hippocampus-neocortex; salience is prediction-error. That is the foundation for the recursive self-improvement story (I7), you cannot recursively improve what you cannot theoretically justify.

Theory is a complement to benchmarks, not a substitute. Mem0 wins LongMemEval today. "We measured AND we can explain why this shape" becomes a stronger combined position than either alone. Theoretical depth is a brand pillar that quietly raises the bar competitors have to meet.

### 9.6 Why none of them wins this

Five facts close the field.

**A. Lock-in incentive trap.** Every major AI lab profits from owning user context. Anthropic, OpenAI, Google, Cursor cannot ship cross-vendor memory without cannibalizing their primary moat. They will ship better *internal* memory; they will not ship portable memory.

**B. US-first regulatory blindness.** Mem0, Letta, Zep, Supermemory all built for US developers first. EU AI Act 2026 enforcement and GDPR's mature jurisprudence create requirements they will need years to retrofit. EU-native architecture is a multi-year head start, not a feature toggle.

**C. Monolith vs. swarm.** All current frameworks ship monolithic memory pipelines. The swarm pattern (multi-agent, role-specialized, mode-switching) is research and internal infrastructure only. LinkedIn proves it works at scale; nobody has productized it.

**D. Imposed vs. emergent schema.** Every framework ships with a taxonomy (episodic/semantic/procedural, or tier-based, or flat KV). Each forces user cognition into a pre-built shape. The emergent-ontology approach is unbuilt; it is genuinely hard, which is why everyone has avoided it.

**E. Multi-tenant vs. single-tenant.** Every venture-funded memory startup is multi-tenant by necessity (margins demand it). Single-tenant per-user infrastructure is structurally incompatible with their unit economics. They cannot pivot without abandoning the growth thesis.

A new entrant holding all five positions simultaneously, cross-vendor, EU-native, swarm-based, emergent, single-tenant, exists in an empty quadrant.

---

## 10. Why This Wins

Six points, each correspondingly hard to replicate.

1. **Cross-vendor by structural design.** MCP-first. Works with every agent that speaks MCP. Cannot be matched by labs whose business model is lock-in.
2. **Single-tenant by design.** Every user instance is physically isolated: own machine, own SQLite, own state. Cannot be matched by venture-funded competitors whose margins depend on multi-tenancy.
3. **EU-native compliance.** Designed for GDPR + AI Act from day one. Data residency, audit trails, right-to-erasure built into the substrate model and deployment topology (destroy a machine = full deletion). US-first players retrofit; this starts there.
4. **Emergent ontology.** No two users' vaults look alike after a few months. The system becomes a cognitive fingerprint. Generic "memory layers" cannot compete on personalization because they are statically schemaed.
5. **Society-of-Mind swarm.** Each memory operation is handled by the right specialist at the right cost. Heterogeneous models per agent (each worker's model independently configurable; the judge panel multi-vendor by default). Independent A/B testing. Orchestration is the IP; models are commoditized.
6. **User-owned as political position.** Not just architecture: a stance: *your cognition belongs to you, not to your AI provider*. That position becomes a brand, a community, and a long-term moat. The same logic that made 1Password and Fastmail durable.

---

## 11. Anti-Patterns

What this is explicitly not.

- **Not multi-tenant. Ever.** Even when hosted, every user gets a dedicated isolated instance. If a feature requires shared user infrastructure to be viable, that feature is wrong.
- **Not a marketplace, plugin store, or skill ecosystem at launch.** Dead weight. Maybe later, not before the product proves itself.
- **Not a UI app.** First interface is via MCP from existing AI tools. Building a custom UI competes with consumers, not complements them. A minimal management dashboard for the hosted offering is acceptable.
- **Not multi-user-on-one-instance before single-user works.** If it does not help one person, it will not help five. Sharing across instances is a separate problem for later.
- **Not hosted before self-hosted is stable.** Hosting is a convenience layer over a working local product. Reverse order is fatal.
- **Not vector-only retrieval.** Vector similarity is necessary but radically insufficient. Hybrid (semantic + keyword + entity + graph) from the start.
- **Not a fixed schema as default.** Bootstrap scaffold yes; permanent ontology no. A fixed schema fails Invariant I6.
- **Not a memory framework "for developers."** Developers are the first users because they have the tool stack. The product is for users.
- **Not VC-funded if it forces multi-tenancy.** The unit economics of single-tenant + self-hostable + managed-hosting support a sustainable business, probably not a billion-dollar exit. If raising capital requires abandoning Invariant I8, do not raise.

---

## 12. Licensing Posture

The license is decided: **the open-source core is released under AGPLv3** (see `LICENSE`). Anyone can self-host, fork, and modify; a network-service fork must publish its modifications under the same license. The hosted offering at afair.ai is one deployment of this code, not a separate proprietary fork. The same posture that lets a user take their memory anywhere lets anyone take the code anywhere, and the copyleft keeps forks open, which fits the trust-driven, EU/regulated audience.

The operational rule that produced a clean, publishable codebase stays binding:

> **Build everything as if the repo goes public tomorrow.**

Concretely:

- Code quality, naming, comments, architecture: production-grade always.
- No secrets in code; no hardcoded credentials; no internal jokes or slurs against competitors in code or commit messages.
- No proprietary algorithmic tricks that depend on staying secret. A secret moat is a fragile moat.
- No lock-in mechanisms: no anti-export, no phone-home, no kill-switches. Self-hosting is first-class (Invariant I4).
- Telemetry minimal, off-by-default, anonymized.
- Tests and docs at every layer. A stranger should be able to read the repo and understand what is happening.
- Commit messages and PR descriptions reviewable by a stranger.
- Dependency licenses tracked. Transitive deps stay AGPL-compatible (Apache2/MIT/BSD) so the open release has no license conflict.

Invariants are independent of the license. I4 (user owns the substrate) is satisfied by documented export + binary self-hosting. I1 (MCP surface stability) is independent of license: the protocol surface is forever; the implementation behind it is not.

The hosted control plane (afair-web: provisioning, billing, the customer dashboard) is a separate, private codebase, not part of the AGPLv3 release. The split is deliberate: the product that delivers I4 is open; the business that operates the managed offering is not.

Why AGPLv3 over the alternatives that were on the table:

| Option | Why not chosen |
|---|---|
| **Apache2 core** (Supabase / Mem0) | Maximises adoption but lets a cloud provider run a closed fork. The copyleft is the point here. |
| **BSL / source-available** (Sentry / Cockroach) | Restricts forks but is not OSI-open; weaker trust signal for the EU/regulated audience. |
| **Closed source** (1Password / Fastmail) | Contradicts the user-ownership ethos and I4's self-hosting-first stance. |

---

## 13. Source Material

### Papers
- McClelland, J. L., McNaughton, B. L., & O'Reilly, R. C. (1995). [Why there are complementary learning systems in the hippocampus and neocortex](https://stanford.edu/~jlmcc/papers/McClellandMcNaughtonOReilly95.pdf). *Psychological Review*, 102(3), 419–457.
- Kumaran, D., Hassabis, D., & McClelland, J. L. (2016). [What learning systems do intelligent agents need? Complementary Learning Systems Theory updated](https://www.cell.com/trends/cognitive-sciences/fulltext/S1364-6613(16)30043-2). *Trends in Cognitive Sciences*, 20(7), 512–534.
- [Mem0: ECAI 2025 (arXiv:2504.19413)](https://arxiv.org/abs/2504.19413)
- [Zep: A Temporal Knowledge Graph Architecture for Agent Memory (arXiv:2501.13956)](https://arxiv.org/abs/2501.13956)
- [H-MEM: Hierarchical Memory for High-Efficiency Long-Term Reasoning (arXiv:2507.22925)](https://arxiv.org/abs/2507.22925)
- [HiMem: Hierarchical Long-Term Memory for LLM Long-Horizon Agents (arXiv:2601.06377)](https://arxiv.org/abs/2601.06377)
- [Rethinking Memory Mechanisms of Foundation Agents (arXiv:2602.06052)](https://arxiv.org/abs/2602.06052)
- [Generative Agents: Interactive Simulacra of Human Behavior (Park et al.)](https://arxiv.org/abs/2304.03442)
- [MemGPT: Towards LLMs as Operating Systems (arXiv:2310.08560)](https://arxiv.org/abs/2310.08560)
- [Unifying the ADHD Paradox: A Computational Model of Cognitive Specialization](https://sciety.org/articles/activity/10.31234/osf.io/frsp4_v5)
- [The Weak Coherence Account: Detail-focused Cognitive Style in ASD (Happé & Frith)](https://link.springer.com/article/10.1007/s10803-005-0039-0)
- [Hippocampal Replay Prioritizes Weakly Learned Information (Schapiro et al.)](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC6156217/)
- [Sleep-Like Replay Reduces Catastrophic Forgetting in ANNs (Nature Comms)](https://www.nature.com/articles/s41467-022-34938-7)
- [Lives Without Imagery: Aphantasia (Zeman et al.)](https://www.sciencedirect.com/science/article/pii/S0010945215000532)
- [The Savant Syndrome (Treffert)](https://royalsocietypublishing.org/doi/10.1098/rstb.2008.0326)
- [Triple Network Model / Salience Network (Menon, 2011)](https://www.sciencedirect.com/science/article/pii/S1364661311001148)
- Karl Friston: [The free-energy principle: a unified brain theory? (2010)](https://www.nature.com/articles/nrn2787)

### Frameworks & repos
- [Mem0](https://github.com/mem0ai/mem0)
- [Letta](https://github.com/letta-ai/letta)
- [Zep](https://github.com/getzep/zep) / [Graphiti](https://github.com/getzep/graphiti)
- [Cognee](https://github.com/topoteretes/cognee)
- [Supermemory](https://github.com/supermemoryai/supermemory)
- [LangMem](https://github.com/langchain-ai/langmem)
- [MemAgents Workshop ICLR 2026](https://openreview.net/pdf?id=U51WxL382H)
- [Agent Memory Paper List](https://github.com/Shichun-Liu/Agent-Memory-Paper-List)

### Industry references
- [State of AI Agent Memory 2026 (Mem0)](https://mem0.ai/blog/state-of-ai-agent-memory-2026)
- [Best AI Agent Memory Systems 2026 (Vectorize)](https://vectorize.io/articles/best-ai-agent-memory-systems)
- [LinkedIn Cognitive Memory Agent (InfoQ)](https://www.infoq.com/news/2026/04/linkedin-cognitive-memory-agent/)
- [Society of Mind: The 50-Year-Old Blueprint](https://medium.com/@Micheal-Lanham/society-of-mind-the-50-year-old-blueprint-for-ai-agents-b0e62eb4ec06)
- [AI Agent Memory: Auto Dream and Context Files](https://dev.to/max_quimby/ai-agent-memory-in-2026-auto-dream-context-files-and-what-actually-works-39m8)

### MCP documentation
- [Anthropic MCP overview](https://docs.anthropic.com/en/docs/agents-and-tools/mcp)
- [Codex MCP](https://developers.openai.com/codex/mcp)
- [Cursor MCP](https://docs.cursor.com/context/mcp)
- [MCP specification](https://modelcontextprotocol.io)

### Foundational reads
- Marvin Minsky, *Society of Mind* (1986).
- Daniel Schacter, *Searching for Memory* (1996).
- Endel Tulving, *Elements of Episodic Memory* (1983).
- Vannevar Bush, *As We May Think* (1945): [Atlantic original](https://www.theatlantic.com/magazine/archive/1945/07/as-we-may-think/303881/).

---

*End of Constitution. Question everything except §4.*
