"""DDL — locked at v1, additive-only per Invariant I3.

Every statement is idempotent so init runs cleanly on either a fresh or
fully-populated database.

DO NOT EDIT or REMOVE existing statements. If new fields or new tables
are needed, APPEND new statements at the end. Old data must remain
readable, queryable, and re-interpretable by every later version.
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = 1
"""Substrate writer version. Bump only when fields are ADDED, never removed."""

SCHEMA_DDL: tuple[str, ...] = (
    # ── events: the immutable append-only log ───────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS events (
        id              TEXT PRIMARY KEY,
        content_hash    TEXT NOT NULL UNIQUE,
        created_at      TEXT NOT NULL,
        origin          TEXT NOT NULL,
        kind            TEXT NOT NULL,
        payload         TEXT NOT NULL,
        parent_hashes   TEXT,
        schema_version  INTEGER NOT NULL
    ) STRICT
    """,
    # Read-path indexes — writes never UPDATE, these help SELECTs.
    "CREATE INDEX IF NOT EXISTS events_created_at_idx ON events(created_at)",
    "CREATE INDEX IF NOT EXISTS events_kind_idx       ON events(kind)",
    "CREATE INDEX IF NOT EXISTS events_origin_idx     ON events(origin)",
    # Composite for the hot _recent_canonical_context query (Perf audit C1):
    # filters kind IN (...) then orders by created_at DESC. Without this
    # composite, SQLite picks one of the two single-column indexes and
    # sorts in-Python — ~15-25ms p95 cost per recall on a 50k-event vault.
    "CREATE INDEX IF NOT EXISTS events_kind_created_at_idx ON events(kind, created_at DESC)",
    # ── append-only enforcement at the DB level (Invariant I2) ──────────────
    """
    CREATE TRIGGER IF NOT EXISTS events_no_update
    BEFORE UPDATE ON events
    BEGIN
        SELECT RAISE(ABORT, 'substrate is append-only (Invariant I2)');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS events_no_delete
    BEFORE DELETE ON events
    BEGIN
        SELECT RAISE(ABORT, 'substrate is append-only (Invariant I2)');
    END
    """,
    # ── FTS5 virtual table for keyword search over derived searchable text ──
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
        content_hash UNINDEXED,
        searchable_text,
        tokenize = 'unicode61 remove_diacritics 2'
    )
    """,
    # ── interpretations: materialized views over substrate (Invariant I3) ──
    # Multiple versions may coexist. The substrate row is invariant; this
    # table is regenerable. Populated by the Extractor agent in task #4.
    #
    # Append-only, enforced by triggers with a SINGLE scoped exception. The
    # write path (write_interpretation) only ever INSERTs — a re-interpretation
    # is a NEW version/producer row, never an in-place UPDATE — so there are
    # ZERO legitimate UPDATEs (``interpretations_no_update`` is unconditional).
    # There is exactly ONE legitimate DELETE path in the whole codebase: the
    # Pruner's stale-failed-extraction GC (agents/pruner.py), tightly scoped to
    # ``produced_by LIKE 'extractor:%'`` rows with ``status='failed'`` that
    # already have a SUCCESS sibling — it drops regenerable failure diagnostics,
    # never a memory-of-record row. ``interpretations_no_delete`` therefore
    # blocks every DELETE EXCEPT those on ``extractor:%`` producers, which is the
    # exact (and only) shape the Pruner GC emits.
    #
    # ADR-0008: the operator's conflict-resolution decision lives here as a
    # ``conflict_resolution:v1:<pair_key>`` interpretation — a NON-regenerable
    # decision-of-record, unlike the extractor views this table was built for.
    # The ``no_delete`` trigger's ``NOT LIKE 'extractor:%'`` guard now PHYSICALLY
    # protects it: a stray DELETE of a ``conflict_resolution:v1:`` row aborts,
    # and no code path UPDATEs interpretations at all. The Fable adversarial
    # review flagged that this decision-of-record was un-triggered while framed
    # as a "regenerable I3 view"; the triggers below close that gap. Existing
    # fleet vaults gain the triggers idempotently on next open
    # (CREATE TRIGGER IF NOT EXISTS).
    """
    CREATE TABLE IF NOT EXISTS interpretations (
        id              TEXT PRIMARY KEY,
        event_id        TEXT NOT NULL REFERENCES events(id),
        event_hash      TEXT NOT NULL REFERENCES events(content_hash),
        version         INTEGER NOT NULL,
        produced_at     TEXT NOT NULL,
        produced_by     TEXT NOT NULL,
        extraction      TEXT NOT NULL,
        UNIQUE(event_hash, version, produced_by)
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS interpretations_event_idx ON interpretations(event_id)",
    # ── interpretations append-only enforcement (I2), one scoped exception ──
    # No UPDATE is ever legitimate (write_interpretation is insert-only).
    """
    CREATE TRIGGER IF NOT EXISTS interpretations_no_update
    BEFORE UPDATE ON interpretations
    BEGIN
        SELECT RAISE(ABORT, 'interpretations is append-only (I2)');
    END
    """,
    # DELETE is allowed ONLY for the Pruner's extractor stale-failed GC
    # (produced_by LIKE 'extractor:%'); everything else — including the
    # ADR-0008 conflict_resolution:v1: decision-of-record — is blocked.
    """
    CREATE TRIGGER IF NOT EXISTS interpretations_no_delete
    BEFORE DELETE ON interpretations
    WHEN OLD.produced_by NOT LIKE 'extractor:%'
    BEGIN
        SELECT RAISE(ABORT, 'interpretations is append-only (I2); only the extractor failure-GC may delete');
    END
    """,
    # ── OAuth state (Phase 1) — pluggable identity + JWT issuance ──────────
    # These tables are MUTABLE (codes are short-lived, tokens get revoked).
    # The events-table append-only triggers do NOT apply here. They live in
    # substrate.db alongside the immutable substrate for backup-locality.
    """
    CREATE TABLE IF NOT EXISTS oauth_clients (
        client_id           TEXT PRIMARY KEY,
        client_secret_hash  TEXT,
        redirect_uris       TEXT NOT NULL,
        client_name         TEXT,
        registered_at       TEXT NOT NULL,
        metadata            TEXT
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_codes (
        code                  TEXT PRIMARY KEY,
        client_id             TEXT NOT NULL,
        redirect_uri          TEXT NOT NULL,
        scope                 TEXT,
        code_challenge        TEXT NOT NULL,
        code_challenge_method TEXT NOT NULL,
        user_sub              TEXT NOT NULL,
        user_email            TEXT,
        expires_at            TEXT NOT NULL,
        created_at            TEXT NOT NULL
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS oauth_codes_client_idx ON oauth_codes(client_id)",
    "CREATE INDEX IF NOT EXISTS oauth_codes_expires_idx ON oauth_codes(expires_at)",
    """
    CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
        token_hash    TEXT PRIMARY KEY,
        client_id     TEXT NOT NULL,
        user_sub      TEXT NOT NULL,
        scope         TEXT,
        expires_at    TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        revoked_at    TEXT
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS oauth_refresh_user_idx ON oauth_refresh_tokens(user_sub)",
    # ── Login flow state ───────────────────────────────────────────────────
    # Holds the in-flight OAuth dance state between /oauth/authorize and
    # the identity backend's callback. Short-lived (~10 min). Mutable.
    """
    CREATE TABLE IF NOT EXISTS oauth_login_state (
        state                 TEXT PRIMARY KEY,
        client_id             TEXT NOT NULL,
        redirect_uri          TEXT NOT NULL,
        scope                 TEXT,
        code_challenge        TEXT NOT NULL,
        code_challenge_method TEXT NOT NULL,
        client_state          TEXT,
        expires_at            TEXT NOT NULL,
        created_at            TEXT NOT NULL
    ) STRICT
    """,
    # ── Phase 4 Track 1: Emergent Entity Graph (I6 — Emergent over Imposed) ─
    # All five tables below are STRICT append-only per I2 — no mutable
    # columns. Supersession (entity merges) and edge invalidation are
    # represented as additional rows in dedicated lookup tables, never as
    # in-place updates. Identity is content-derived so a rebuild from
    # substrate is deterministic. Added 2026-05-26; pre-existing rows in
    # events/interpretations are unaffected (I3 — old data stays readable).
    #
    # entity-id scheme: ``entity:<sha256(lowercase(canonical_name)|kind)>``
    # gives reproducible IDs across rebuilds from substrate.
    """
    CREATE TABLE IF NOT EXISTS entities (
        id              TEXT PRIMARY KEY,
        canonical_name  TEXT NOT NULL,
        kind            TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        created_by      TEXT NOT NULL,
        confidence      REAL NOT NULL,
        source_event_id TEXT NOT NULL REFERENCES events(id)
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS entities_kind_canonical_idx ON entities(kind, canonical_name)",
    "CREATE INDEX IF NOT EXISTS entities_canonical_idx ON entities(canonical_name)",
    """
    CREATE TRIGGER IF NOT EXISTS entities_no_update
    BEFORE UPDATE ON entities
    BEGIN
        SELECT RAISE(ABORT, 'entities is append-only (Invariant I2)');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS entities_no_delete
    BEFORE DELETE ON entities
    BEGIN
        SELECT RAISE(ABORT, 'entities is append-only (Invariant I2)');
    END
    """,
    # ── entity_mentions: one row per (event, surface form → entity) ─────────
    # The link from a raw event to a canonical entity. UNIQUE constraint
    # makes the canonicalizer idempotent — re-running on the same event
    # is a no-op rather than a duplicate-row write.
    """
    CREATE TABLE IF NOT EXISTS entity_mentions (
        id                  TEXT PRIMARY KEY,
        entity_id           TEXT NOT NULL REFERENCES entities(id),
        event_id            TEXT NOT NULL REFERENCES events(id),
        event_hash          TEXT NOT NULL,
        surface_form        TEXT NOT NULL,
        canonicalized_at    TEXT NOT NULL,
        canonicalized_by    TEXT NOT NULL,
        match_method        TEXT NOT NULL,
        confidence          REAL NOT NULL,
        UNIQUE(entity_id, event_id, surface_form)
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS entity_mentions_event_idx ON entity_mentions(event_id)",
    "CREATE INDEX IF NOT EXISTS entity_mentions_event_hash_idx ON entity_mentions(event_hash)",
    "CREATE INDEX IF NOT EXISTS entity_mentions_entity_idx ON entity_mentions(entity_id)",
    "CREATE INDEX IF NOT EXISTS entity_mentions_surface_idx ON entity_mentions(surface_form)",
    """
    CREATE TRIGGER IF NOT EXISTS entity_mentions_no_update
    BEFORE UPDATE ON entity_mentions
    BEGIN
        SELECT RAISE(ABORT, 'entity_mentions is append-only (Invariant I2)');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS entity_mentions_no_delete
    BEFORE DELETE ON entity_mentions
    BEGIN
        SELECT RAISE(ABORT, 'entity_mentions is append-only (Invariant I2)');
    END
    """,
    # ── entity_edges: subject-predicate-object triples between entities ─────
    # Bi-temporal: valid_from/valid_to track when the fact was true (NULL
    # valid_to = open-ended, still believed true). discovered_at is OUR
    # timeline (when we learned it). Edge-invalidation lives in its own
    # table so the substrate stays append-only.
    """
    CREATE TABLE IF NOT EXISTS entity_edges (
        id              TEXT PRIMARY KEY,
        subject_id      TEXT NOT NULL REFERENCES entities(id),
        predicate       TEXT NOT NULL,
        object_id       TEXT NOT NULL REFERENCES entities(id),
        valid_from      TEXT,
        valid_to        TEXT,
        discovered_at   TEXT NOT NULL,
        discovered_by   TEXT NOT NULL,
        source_event_id TEXT NOT NULL REFERENCES events(id),
        confidence      REAL NOT NULL,
        UNIQUE(subject_id, predicate, object_id, source_event_id)
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS entity_edges_subject_idx ON entity_edges(subject_id)",
    "CREATE INDEX IF NOT EXISTS entity_edges_object_idx ON entity_edges(object_id)",
    "CREATE INDEX IF NOT EXISTS entity_edges_source_event_idx ON entity_edges(source_event_id)",
    "CREATE INDEX IF NOT EXISTS entity_edges_predicate_idx ON entity_edges(predicate)",
    """
    CREATE TRIGGER IF NOT EXISTS entity_edges_no_update
    BEFORE UPDATE ON entity_edges
    BEGIN
        SELECT RAISE(ABORT, 'entity_edges is append-only (Invariant I2)');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS entity_edges_no_delete
    BEFORE DELETE ON entity_edges
    BEGIN
        SELECT RAISE(ABORT, 'entity_edges is append-only (Invariant I2)');
    END
    """,
    # ── entity_merges: when canonicalizer realizes two entities are one ─────
    # "Sajinth" was first canonicalized as kind=person from the elvah
    # context, later evidence shows it's the same Sajinth in the Athara
    # context — write a merge row pointing from_entity_id → into_entity_id.
    # The "current canonical" for any entity is the transitive closure
    # through this table; reads compose the view at query time.
    """
    CREATE TABLE IF NOT EXISTS entity_merges (
        id              TEXT PRIMARY KEY,
        from_entity_id  TEXT NOT NULL REFERENCES entities(id),
        into_entity_id  TEXT NOT NULL REFERENCES entities(id),
        merged_at       TEXT NOT NULL,
        merged_by       TEXT NOT NULL,
        reason          TEXT NOT NULL,
        confidence      REAL NOT NULL,
        CHECK (from_entity_id != into_entity_id)
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS entity_merges_from_idx ON entity_merges(from_entity_id)",
    "CREATE INDEX IF NOT EXISTS entity_merges_into_idx ON entity_merges(into_entity_id)",
    """
    CREATE TRIGGER IF NOT EXISTS entity_merges_no_update
    BEFORE UPDATE ON entity_merges
    BEGIN
        SELECT RAISE(ABORT, 'entity_merges is append-only (Invariant I2)');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS entity_merges_no_delete
    BEFORE DELETE ON entity_merges
    BEGIN
        SELECT RAISE(ABORT, 'entity_merges is append-only (Invariant I2)');
    END
    """,
    # ── edge_invalidations: when an entity_edge is later superseded ─────────
    # Cascade target from remember(..., invalidates=[hash]): when the
    # source event of an edge is invalidated, the edge gets its own
    # invalidation row referencing that event. Bi-temporal: valid_to on
    # the edge itself can also mark "the fact stopped being true on date
    # X" without a downstream invalidate event.
    """
    CREATE TABLE IF NOT EXISTS edge_invalidations (
        id                TEXT PRIMARY KEY,
        edge_id           TEXT NOT NULL REFERENCES entity_edges(id),
        invalidated_at    TEXT NOT NULL,
        invalidated_by    TEXT NOT NULL,
        reason            TEXT NOT NULL,
        source_event_id   TEXT REFERENCES events(id),
        UNIQUE(edge_id, source_event_id)
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS edge_invalidations_edge_idx ON edge_invalidations(edge_id)",
    """
    CREATE TRIGGER IF NOT EXISTS edge_invalidations_no_update
    BEFORE UPDATE ON edge_invalidations
    BEGIN
        SELECT RAISE(ABORT, 'edge_invalidations is append-only (Invariant I2)');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS edge_invalidations_no_delete
    BEFORE DELETE ON edge_invalidations
    BEGIN
        SELECT RAISE(ABORT, 'edge_invalidations is append-only (Invariant I2)');
    END
    """,
    # ── merge_invalidations: undo a merge without deleting it (I2) ───────────
    # An entity_merge is append-only, but a wrong one (a bad auto-merge the
    # operator rejected, or a kind reverted to where it came from) must be
    # reversible — otherwise resolve_canonical is stuck with it. Invalidating a
    # merge makes resolve_canonical skip that edge, so the from-entity stops
    # resolving through it. Append-only itself: the original merge row stays as
    # history (I7 — recorded + reversible, not erased).
    """
    CREATE TABLE IF NOT EXISTS merge_invalidations (
        id                TEXT PRIMARY KEY,
        merge_id          TEXT NOT NULL REFERENCES entity_merges(id),
        invalidated_at    TEXT NOT NULL,
        invalidated_by    TEXT NOT NULL,
        reason            TEXT NOT NULL,
        source_event_id   TEXT REFERENCES events(id),
        UNIQUE(merge_id)
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS merge_invalidations_merge_idx ON merge_invalidations(merge_id)",
    """
    CREATE TRIGGER IF NOT EXISTS merge_invalidations_no_update
    BEFORE UPDATE ON merge_invalidations
    BEGIN
        SELECT RAISE(ABORT, 'merge_invalidations is append-only (Invariant I2)');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS merge_invalidations_no_delete
    BEFORE DELETE ON merge_invalidations
    BEGIN
        SELECT RAISE(ABORT, 'merge_invalidations is append-only (Invariant I2)');
    END
    """,
    # ── entity_retractions: take a non-entity out of the live graph (I2) ─────
    # Some extractions are noise — a file path, a test fixture, a doc section
    # (scripts/smoke_mcp.py, smoke-test-..., VISION.md §15) that should never
    # have been an entity. Deleting the row is forbidden (I2), so retraction is
    # append-only: a row here marks the entity withdrawn, and every live-graph
    # read (recall overlay, audit, canonicalizer/dedup candidates, articles)
    # filters it out. The entity + its mentions stay as history; they just stop
    # being served as part of the user's current graph.
    """
    CREATE TABLE IF NOT EXISTS entity_retractions (
        id              TEXT PRIMARY KEY,
        entity_id       TEXT NOT NULL REFERENCES entities(id),
        retracted_at    TEXT NOT NULL,
        retracted_by    TEXT NOT NULL,
        reason          TEXT NOT NULL,
        source_event_id TEXT REFERENCES events(id),
        UNIQUE(entity_id)
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS entity_retractions_entity_idx ON entity_retractions(entity_id)",
    """
    CREATE TRIGGER IF NOT EXISTS entity_retractions_no_update
    BEFORE UPDATE ON entity_retractions
    BEGIN
        SELECT RAISE(ABORT, 'entity_retractions is append-only (Invariant I2)');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS entity_retractions_no_delete
    BEFORE DELETE ON entity_retractions
    BEGIN
        SELECT RAISE(ABORT, 'entity_retractions is append-only (Invariant I2)');
    END
    """,
    # ── edge_reviews: the operator's confirm/reject verdicts (ADR-0002) ──────
    # A derived edge is a defeasible belief, not silent truth. The operator
    # (directly, or an AI on their behalf with confirmation) reviews edges; each
    # verdict is an append-only row. The CURRENT trust state of an edge is its
    # latest review, else the auto-confirm policy (see substrate/belief.py).
    # A 'reject' verdict also writes an edge_invalidation (the existing
    # defeasible-retraction path); 'confirm' is the new signal — and the
    # ground-truth the self-improvement tuner lacks.
    """
    CREATE TABLE IF NOT EXISTS edge_reviews (
        id           TEXT PRIMARY KEY,
        edge_id      TEXT NOT NULL REFERENCES entity_edges(id),
        verdict      TEXT NOT NULL CHECK (verdict IN ('confirm', 'reject')),
        reason       TEXT,
        reviewed_by  TEXT NOT NULL,
        reviewed_at  TEXT NOT NULL
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS edge_reviews_edge_idx ON edge_reviews(edge_id, reviewed_at)",
    """
    CREATE TRIGGER IF NOT EXISTS edge_reviews_no_update
    BEFORE UPDATE ON edge_reviews
    BEGIN
        SELECT RAISE(ABORT, 'edge_reviews is append-only (Invariant I2)');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS edge_reviews_no_delete
    BEFORE DELETE ON edge_reviews
    BEGIN
        SELECT RAISE(ABORT, 'edge_reviews is append-only (Invariant I2)');
    END
    """,
    # ── edge_confidence_scores: append-only confidence overlay (ADR-0004) ────
    # entity_edges.confidence is the immutable AT-DISCOVERY snapshot; the
    # CURRENT belief strength of an edge is the latest row here, falling back
    # to the column when no score row exists (old vaults, mid-backfill). Same
    # supersession pattern as entity_kind_assignments (ADR-0003) and
    # edge_reviews (ADR-0002): the base row never changes, the current view is
    # the latest overlay row, reads compose at query time. The 176 flat-0.8
    # legacy edges are never mutated (I2/I3) — the cold-path scorer appends
    # their first real score, computed over signals recovered from substrate.
    # ``components`` stores the full per-term breakdown so "why this number?"
    # has an answer; ``computed_by`` versions the model so a re-derivation is a
    # bump with history kept (I7).
    """
    CREATE TABLE IF NOT EXISTS edge_confidence_scores (
        id           TEXT PRIMARY KEY,
        edge_id      TEXT NOT NULL REFERENCES entity_edges(id),
        confidence   REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
        components   TEXT NOT NULL,
        computed_by  TEXT NOT NULL,
        computed_at  TEXT NOT NULL
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS edge_confidence_scores_edge_idx "
    "ON edge_confidence_scores(edge_id, computed_at)",
    """
    CREATE TRIGGER IF NOT EXISTS edge_confidence_scores_no_update
    BEFORE UPDATE ON edge_confidence_scores
    BEGIN
        SELECT RAISE(ABORT, 'edge_confidence_scores is append-only (Invariant I2)');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS edge_confidence_scores_no_delete
    BEFORE DELETE ON edge_confidence_scores
    BEGIN
        SELECT RAISE(ABORT, 'edge_confidence_scores is append-only (Invariant I2)');
    END
    """,
    # ── edge_serves: the "this edge was actually served in a recall" signal ──
    # A DURABLE gate input, not telemetry. An edge only earns a review-queue
    # slot after recall has surfaced it to the operator at least once
    # (edge_scorer's serve-gated candidate query), and the auto-expiry sweep
    # keys on the ABSENCE of a row here — so this table must be append-only and
    # is deliberately NOT prunable (see the Pruner "MUST NEVER touch" list). One
    # row per edge, stamped the first time it was served; INSERT OR IGNORE on
    # the PK makes the recall-hot-path write idempotent and cheap.
    """
    CREATE TABLE IF NOT EXISTS edge_serves (
        edge_id         TEXT PRIMARY KEY REFERENCES entity_edges(id),
        first_served_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TRIGGER IF NOT EXISTS edge_serves_no_update
    BEFORE UPDATE ON edge_serves
    BEGIN
        SELECT RAISE(ABORT, 'edge_serves is append-only (Invariant I2)');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS edge_serves_no_delete
    BEFORE DELETE ON edge_serves
    BEGIN
        SELECT RAISE(ABORT, 'edge_serves is append-only (Invariant I2)');
    END
    """,
    # ── proposed_corrections: the entity-audit review queue (ADR-0002) ───────
    # MUTABLE derived state, not substrate — the audit worker regenerates it, a
    # decision updates its status, the pruner ages out decided edge_review rows.
    # The *applied* correction (retype / merge / edge review) is the append-only
    # part; this table is just the suggestion the operator confirms. Contract:
    # one OPEN proposal per (kind, entity) — enforced by the partial unique index
    # below (status='proposed' only). Decided history PERSISTS as the detectors'
    # anti-re-nag memory (entity_audit checks it explicitly via NOT EXISTS, so a
    # closed question is never re-opened). Decided edge_review rows ARE prunable
    # because their durable never-re-review guard is the append-only edge_reviews
    # table (edge_scorer's NOT EXISTS), not the queue row.
    """
    CREATE TABLE IF NOT EXISTS proposed_corrections (
        id            TEXT PRIMARY KEY,
        kind          TEXT NOT NULL CHECK (kind IN ('retype', 'merge', 'merge_review', 'edge_review')),
        entity_id     TEXT NOT NULL REFERENCES entities(id),
        detail        TEXT NOT NULL,
        evidence      TEXT NOT NULL,
        confidence    REAL NOT NULL,
        tier          TEXT NOT NULL CHECK (tier IN ('auto', 'review')),
        detected_by   TEXT NOT NULL,
        detected_at   TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'proposed'
                      CHECK (status IN ('proposed', 'confirmed', 'rejected', 'applied')),
        decided_at    TEXT,
        decided_by    TEXT
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS proposed_corrections_status_idx ON proposed_corrections(status)",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS proposed_corrections_open_unique
    ON proposed_corrections(kind, entity_id) WHERE status = 'proposed'
    """,
    # ── pipeline_events: end-to-end lifecycle tracing (Phase 0.5 obs) ───────
    # Every step in an event's journey gets a row:
    #   event.written            — write_event_with_status returned ok
    #   extraction.enqueued      — schedule_extraction submitted the job
    #   extraction.completed     — extractor wrote a success interpretation
    #   extraction.failed        — extractor wrote a failed interpretation
    #   embedding.stored         — vec row inserted
    #   binder.linked            — find_and_record_links recorded N links
    #   canonicalizer.processed  — entity_canonicalizer ran on this event
    #   consolidator.included    — consolidator pulled this into a day-roll
    #   conflict_resolver.judged — conflict_resolver verdict written
    #
    # Designed to answer "where did event X get stuck?" without grepping
    # logs. Readers compose the timeline with ORDER BY recorded_at.
    #
    # OPERATIONAL TELEMETRY, not user memory (ADR-0005). This is the pipeline's
    # flight recorder — instrumentation about how the plumbing ran; it carries
    # no user memory and is never recalled. Like proposed_corrections above and
    # export_jobs below, it is deliberately NON-substrate: no append-only
    # triggers. The Pruner ages rows out past TELEMETRY_RETENTION_DAYS (I2
    # protects the user's memory, not the flight recorder — see the ADR). The
    # DROP TRIGGER statements below retire the old I2 triggers on existing
    # vaults created before ADR-0005; a fresh vault never creates them.
    """
    CREATE TABLE IF NOT EXISTS pipeline_events (
        id              TEXT PRIMARY KEY,
        event_id        TEXT NOT NULL,
        event_hash      TEXT,
        stage           TEXT NOT NULL,
        status          TEXT NOT NULL,
        recorded_at     TEXT NOT NULL,
        producer        TEXT,
        detail          TEXT
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS pipeline_events_event_id_idx ON pipeline_events(event_id, recorded_at)",
    "CREATE INDEX IF NOT EXISTS pipeline_events_stage_idx ON pipeline_events(stage, recorded_at DESC)",
    # ADR-0005: retire the append-only triggers so the Pruner can age telemetry
    # out. Idempotent DROP (a no-op on a fresh post-ADR-0005 vault). Additive in
    # the migration-runner sense — every statement is safe to re-run at boot.
    "DROP TRIGGER IF EXISTS pipeline_events_no_update",
    "DROP TRIGGER IF EXISTS pipeline_events_no_delete",
    # ── observability_snapshots: expectation-checker counters (Phase 0.5) ──
    # One row per checker cycle. ``counters`` is a JSON object of
    # INTEGER-ONLY values (enforced by the writer in observability.py —
    # never content, names, or paths). /health reads only the latest row
    # (single indexed LIMIT 1) so per-probe aggregate scans stay off the
    # hot path.
    #
    # OPERATIONAL TELEMETRY, not user memory (ADR-0005), the same as
    # pipeline_events above: instrumentation counters, never recalled, ~35k
    # rows/year. NON-substrate, no append-only triggers; the Pruner ages rows
    # out past TELEMETRY_RETENTION_DAYS. The DROP TRIGGER statements below
    # retire the pre-ADR-0005 I2 triggers on existing vaults.
    """
    CREATE TABLE IF NOT EXISTS observability_snapshots (
        id           TEXT PRIMARY KEY,
        recorded_at  TEXT NOT NULL,
        producer     TEXT NOT NULL,
        counters     TEXT NOT NULL
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS observability_snapshots_recorded_at_idx "
    "ON observability_snapshots(recorded_at DESC)",
    # ADR-0005: retire the append-only triggers (idempotent DROP).
    "DROP TRIGGER IF EXISTS observability_snapshots_no_update",
    "DROP TRIGGER IF EXISTS observability_snapshots_no_delete",
    # ── tuner_state ────────────────────────────────────────────────────
    # Append-only log of every self-modification the tuner makes
    # (and observations / hypotheses it considers).
    #
    # Used as the source of truth for "what's the current value of
    # tunable X?" — the latest 'promote' row for (worker, tunable) wins,
    # falling back to the static default declared in
    # afair/agents/tunable_registry.py when no row exists.
    #
    # Kinds:
    #   'promote'      — variant has been adopted; new_value is live
    #   'rollback'     — auto-rollback restored an older value
    #   'hypothesis'   — tuner generated a candidate, may or may not be tested
    #   'observation'  — tuner notes a signal (e.g., judge verdict, divergence)
    #
    # Designed for two query patterns:
    #   (1) latest promote/rollback per (worker, tunable) — covered by
    #       the (worker, tunable, recorded_at DESC) index
    #   (2) timeline by recorded_at — covered by the recorded_at index
    """
    CREATE TABLE IF NOT EXISTS tuner_state (
        id              TEXT PRIMARY KEY,
        recorded_at     TEXT NOT NULL,
        kind            TEXT NOT NULL CHECK (kind IN (
                            'promote', 'rollback', 'hypothesis', 'observation'
                        )),
        worker          TEXT NOT NULL,
        tunable         TEXT NOT NULL,
        old_value_json  TEXT,
        new_value_json  TEXT,
        evidence_json   TEXT,
        rationale       TEXT
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS tuner_state_lookup_idx ON tuner_state(worker, tunable, recorded_at DESC)",
    "CREATE INDEX IF NOT EXISTS tuner_state_timeline_idx ON tuner_state(recorded_at DESC)",
    """
    CREATE TRIGGER IF NOT EXISTS tuner_state_no_update
    BEFORE UPDATE ON tuner_state
    BEGIN
        SELECT RAISE(ABORT, 'tuner_state is append-only (Invariant I2 + I7)');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS tuner_state_no_delete
    BEFORE DELETE ON tuner_state
    BEGIN
        SELECT RAISE(ABORT, 'tuner_state is append-only (Invariant I2 + I7)');
    END
    """,
    # ── api_tokens: per-user revocable bearer tokens for agents ────────────
    # MUTABLE table (revoke flips revoked_at, every successful auth bumps
    # last_used_at). The static AFAIR_AUTH_TOKEN env stays the master and
    # is NOT in this table — its compromise still grants full access; this
    # table lets the user mint additional tokens for bots / CI / each
    # individual agent and revoke them independently.
    #
    # token_hash is sha256(plaintext) hex. Plaintext lives ONLY in the
    # response of POST /internal/tokens; the DB stores the hash forever.
    # No way to recover a lost token — only re-mint.
    """
    CREATE TABLE IF NOT EXISTS api_tokens (
        id            TEXT PRIMARY KEY,
        label         TEXT NOT NULL,
        token_hash    TEXT NOT NULL UNIQUE,
        scope         TEXT NOT NULL DEFAULT 'full',
        created_at    TEXT NOT NULL,
        last_used_at  TEXT,
        revoked_at    TEXT
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS api_tokens_hash_idx ON api_tokens(token_hash)",
    "CREATE INDEX IF NOT EXISTS api_tokens_active_idx ON api_tokens(revoked_at) WHERE revoked_at IS NULL",
    # ── Perf-scaling indexes (appended 2026-06-10, additive per I3) ─────────
    # These close the launch-scale query cliffs the pre-announce audit found.
    # All are expression / partial / composite indexes over EXISTING columns —
    # no schema change, no data rewrite, idempotent.
    #
    # 1+2. Case-insensitive entity lookups. The recall entity-match and the
    #      article/dedup candidate enumeration filter on LOWER(canonical_name)
    #      / LOWER(surface_form); without an expression index on the lowered
    #      value SQLite full-scans. Match the query expression exactly so the
    #      planner uses these.
    "CREATE INDEX IF NOT EXISTS entities_canonical_lower_idx ON entities(LOWER(canonical_name))",
    "CREATE INDEX IF NOT EXISTS entity_mentions_surface_lower_idx "
    "ON entity_mentions(LOWER(surface_form))",
    # 3. Invalidation lookups run on every recall: WHERE kind='invalidate'
    #    AND json_extract(payload,'$.target_hash') IN (...). A partial
    #    expression index over the extracted hash turns a per-recall scan of
    #    all invalidate rows into an index probe.
    "CREATE INDEX IF NOT EXISTS events_invalidate_target_idx "
    "ON events(json_extract(payload, '$.target_hash')) WHERE kind = 'invalidate'",
    # 4. Entity-article entity_key lookups (the article + dedup workers, and
    #    the new _supersede_prior_articles sweep) filter article rows by
    #    json_extract(payload,'$.entity_key').
    "CREATE INDEX IF NOT EXISTS events_entity_article_key_idx "
    "ON events(json_extract(payload, '$.entity_key')) WHERE kind = 'entity_article'",
    # 5. resolve_canonical_batch ranks merges by (from_entity_id, merged_at
    #    DESC) per recall; this composite serves the latest-merge lookup
    #    without re-windowing the whole table.
    "CREATE INDEX IF NOT EXISTS entity_merges_from_merged_idx "
    "ON entity_merges(from_entity_id, merged_at DESC)",
    # 6. Corroboration counting (edge scorer + canonicalizer write-time) filters
    #    live edges by LOWER(e.predicate) = LOWER(?); the plain
    #    entity_edges_predicate_idx on predicate cannot serve the lowered
    #    expression, so every scored edge full-scanned entity_edges. A matching
    #    expression index turns that into an index probe (appended 2026-07,
    #    additive per I3).
    "CREATE INDEX IF NOT EXISTS entity_edges_predicate_lower_idx ON entity_edges(LOWER(predicate))",
    # 7. Session-start top-salient lookup by producer. _read_top_salient
    #    (mcp/resources.py) runs `WHERE produced_by = ? ORDER BY produced_at
    #    DESC` on EVERY connect — without this composite it full-scans
    #    interpretations + temp-sorts. (appended 2026-07, additive per I3.)
    "CREATE INDEX IF NOT EXISTS interpretations_producer_produced_idx "
    "ON interpretations(produced_by, produced_at DESC)",
    # ── export_jobs: async full-vault export (appended 2026-06-14) ──────────
    # MUTABLE operational table (status transitions pending→ready, downloaded
    # gets stamped, purge expires). NOT append-only substrate — it tracks an
    # ephemeral job, not a fact about the world. Lives in the vault DB for
    # backup-locality, like oauth_codes / api_tokens.
    #
    # A request generates a gzip'd JSONL snapshot of the whole vault into
    # <vault_dir>/exports/<id>.jsonl.gz, gated behind a capability token
    # (download_token_hash = sha256 of the plaintext token that travels in
    # the email link). Artifacts auto-purge after expires_at — a full
    # plaintext-equivalent vault dump must not linger on disk.
    """
    CREATE TABLE IF NOT EXISTS export_jobs (
        id                   TEXT PRIMARY KEY,
        status               TEXT NOT NULL CHECK (status IN (
                                 'pending', 'ready', 'failed', 'expired'
                             )),
        include_blobs        INTEGER NOT NULL DEFAULT 1,
        artifact_filename    TEXT,
        download_token_hash  TEXT,
        size_bytes           INTEGER,
        error                TEXT,
        requested_at         TEXT NOT NULL,
        ready_at             TEXT,
        expires_at           TEXT,
        downloaded_at        TEXT
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS export_jobs_status_idx ON export_jobs(status, requested_at DESC)",
    "CREATE INDEX IF NOT EXISTS export_jobs_token_idx ON export_jobs(download_token_hash)",
    "CREATE INDEX IF NOT EXISTS export_jobs_expires_idx ON export_jobs(expires_at)",
    # ── event_temporal: derived time/relevance metadata per event ───────────
    # Phase 1 of the relevance-decay design. The temporal worker
    # infers, per event, an optional temporal class + event time + relevance
    # horizon + recurrence + closure, so recall can LATER de-prioritize expired
    # one-offs and re-surface recurring items. Append-only and re-derivable
    # (I2/I3): nothing is ever deleted; "forgetting" is a recall score, not a
    # row mutation. UNIQUE(event_hash, computed_by) makes the row its own
    # idempotency marker (no cursor) while letting a bumped worker version
    # re-derive over the unchanged substrate (I7).
    """
    CREATE TABLE IF NOT EXISTS event_temporal (
        id                 TEXT PRIMARY KEY,
        event_id           TEXT NOT NULL REFERENCES events(id),
        event_hash         TEXT NOT NULL REFERENCES events(content_hash),
        temporal_class     TEXT NOT NULL,
        event_time         TEXT,
        relevance_horizon  TEXT,
        recurrence_rule    TEXT,
        closure_state      TEXT,
        confidence         REAL NOT NULL,
        computed_by        TEXT NOT NULL,
        created_at         TEXT NOT NULL,
        UNIQUE(event_hash, computed_by)
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS event_temporal_event_idx ON event_temporal(event_id)",
    "CREATE INDEX IF NOT EXISTS event_temporal_class_idx ON event_temporal(temporal_class)",
    "CREATE INDEX IF NOT EXISTS event_temporal_time_idx ON event_temporal(event_time)",
    """
    CREATE TRIGGER IF NOT EXISTS event_temporal_no_update
    BEFORE UPDATE ON event_temporal
    BEGIN
        SELECT RAISE(ABORT, 'event_temporal is append-only (Invariant I2)');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_temporal_no_delete
    BEFORE DELETE ON event_temporal
    BEGIN
        SELECT RAISE(ABORT, 'event_temporal is append-only (Invariant I2)');
    END
    """,
    # ── kind_registry: the emergent ontology's kind set (ADR-0003 Phase 1) ──
    # Entity kinds become data instead of a hardcoded enum (I6). The current
    # seven kinds are seeded at init with created_by='bootstrap:v1' (see
    # substrate/kinds.py) — the "minimal bootstrap scaffold" I6 permits, a
    # starting point rather than law. Append-only per I2: a kind is never
    # edited or removed; its lifecycle is expressed as kind_revisions rows.
    """
    CREATE TABLE IF NOT EXISTS kind_registry (
        id              TEXT PRIMARY KEY,
        slug            TEXT NOT NULL UNIQUE,
        label           TEXT NOT NULL,
        description     TEXT,
        created_at      TEXT NOT NULL,
        created_by      TEXT NOT NULL,
        source_event_id TEXT REFERENCES events(id)
    ) STRICT
    """,
    """
    CREATE TRIGGER IF NOT EXISTS kind_registry_no_update
    BEFORE UPDATE ON kind_registry
    BEGIN
        SELECT RAISE(ABORT, 'kind_registry is append-only (Invariant I2)');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS kind_registry_no_delete
    BEFORE DELETE ON kind_registry
    BEGIN
        SELECT RAISE(ABORT, 'kind_registry is append-only (Invariant I2)');
    END
    """,
    # ── kind_revisions: the ontology's append-only revision history ─────────
    # Latest-row-wins resolution (the tuner_state pattern): a slug's current
    # successor is the to_slug of its latest 'rename'/'merge' row; a later
    # 'restore' row terminates the chain at the slug itself (that is how a
    # rename or merge is reversed — a compensating row, never a mutation, I7).
    # A slug is live unless its latest revision row is deprecate/rename/merge.
    """
    CREATE TABLE IF NOT EXISTS kind_revisions (
        id              TEXT PRIMARY KEY,
        action          TEXT NOT NULL CHECK (action IN
                            ('add','rename','merge','split','deprecate','restore')),
        from_slug       TEXT,
        to_slug         TEXT,
        detail          TEXT,
        revised_at      TEXT NOT NULL,
        revised_by      TEXT NOT NULL,
        reason          TEXT NOT NULL,
        source_event_id TEXT REFERENCES events(id)
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS kind_revisions_from_idx "
    "ON kind_revisions(from_slug, revised_at DESC)",
    """
    CREATE TRIGGER IF NOT EXISTS kind_revisions_no_update
    BEFORE UPDATE ON kind_revisions
    BEGIN
        SELECT RAISE(ABORT, 'kind_revisions is append-only (Invariant I2)');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS kind_revisions_no_delete
    BEFORE DELETE ON kind_revisions
    BEGIN
        SELECT RAISE(ABORT, 'kind_revisions is append-only (Invariant I2)');
    END
    """,
    # ── kind_observations: raw extractor kind proposals, preserved ──────────
    # Free-text kinds never auto-register: the write path normalizes them
    # deterministically (variant map, else 'other') so intake never blocks on
    # ontology questions. The raw proposal is preserved here — the usage
    # signal a future Schema-Evolver mines (when 'research_paper' shows up 40
    # times squashed into 'concept', the promotion evidence sits in this
    # table). Written from Phase 3 onward; the DDL ships in Phase 1 so the
    # substrate shape is settled before any writer exists.
    """
    CREATE TABLE IF NOT EXISTS kind_observations (
        id               TEXT PRIMARY KEY,
        raw_kind         TEXT NOT NULL,
        normalized_slug  TEXT NOT NULL,
        entity_id        TEXT NOT NULL REFERENCES entities(id),
        event_id         TEXT NOT NULL REFERENCES events(id),
        observed_at      TEXT NOT NULL,
        observed_by      TEXT NOT NULL
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS kind_observations_raw_idx ON kind_observations(raw_kind)",
    "CREATE INDEX IF NOT EXISTS kind_observations_slug_idx "
    "ON kind_observations(normalized_slug, observed_at)",
    """
    CREATE TRIGGER IF NOT EXISTS kind_observations_no_update
    BEFORE UPDATE ON kind_observations
    BEGIN
        SELECT RAISE(ABORT, 'kind_observations is append-only (Invariant I2)');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS kind_observations_no_delete
    BEFORE DELETE ON kind_observations
    BEGIN
        SELECT RAISE(ABORT, 'kind_observations is append-only (Invariant I2)');
    END
    """,
    # ── kind_current_v1: SQL-inspectable view of the live ontology ──────────
    # sqlite3 users can see the current kind set without Python. Views are
    # versioned by name (I3): a changed definition ships as kind_current_v2,
    # never a redefinition of _v1.
    """
    CREATE VIEW IF NOT EXISTS kind_current_v1 AS
    SELECT k.slug,
           k.label,
           k.description,
           NOT EXISTS (
               SELECT 1 FROM kind_revisions r
               WHERE r.from_slug = k.slug
                 AND r.id = (SELECT r2.id FROM kind_revisions r2
                             WHERE r2.from_slug = k.slug
                             ORDER BY r2.revised_at DESC, r2.id DESC LIMIT 1)
                 AND r.action IN ('deprecate','rename','merge')
           ) AS is_live
    FROM kind_registry k
    """,
    # ── entity_kind_assignments: kind as a mutable-by-append attribute ──────
    # ADR-0003 Phase 2. An entity's kind stops being identity-bearing: the
    # CURRENT kind is its latest assignment row, falling back to the immutable
    # entities.kind every existing row already carries (see the
    # entity_current_kind_v1 view below). That fallback IS the backfill — zero
    # rows rewritten, zero rows copied; old vaults resolve identically until
    # the first assignment overlays a row. A retype is ONE row here (anchored
    # to an observe event via source_event_id, I7) instead of merge-chain
    # surgery; a revert is just another assignment row.
    """
    CREATE TABLE IF NOT EXISTS entity_kind_assignments (
        id              TEXT PRIMARY KEY,
        entity_id       TEXT NOT NULL REFERENCES entities(id),
        kind_slug       TEXT NOT NULL,
        assigned_at     TEXT NOT NULL,
        assigned_by     TEXT NOT NULL,
        confidence      REAL NOT NULL,
        reason          TEXT NOT NULL,
        source_event_id TEXT REFERENCES events(id)
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS entity_kind_assignments_entity_idx "
    "ON entity_kind_assignments(entity_id, assigned_at DESC)",
    """
    CREATE TRIGGER IF NOT EXISTS entity_kind_assignments_no_update
    BEFORE UPDATE ON entity_kind_assignments
    BEGIN
        SELECT RAISE(ABORT, 'entity_kind_assignments is append-only (Invariant I2)');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS entity_kind_assignments_no_delete
    BEFORE DELETE ON entity_kind_assignments
    BEGIN
        SELECT RAISE(ABORT, 'entity_kind_assignments is append-only (Invariant I2)');
    END
    """,
    # ── entity_identities: the v2 name-first identity ledger ────────────────
    # ADR-0003 Phase 2. NEW entities derive their id as
    # ``entity:v2:<sha256(lower(name)|disambiguator)>`` where the
    # disambiguator is an ordinal that starts at "0" and increments ONLY on a
    # deliberate homonym split (the LLM/operator rules a new "Apple" is a
    # different thing from every existing live "Apple"). Each v2 identity is
    # recorded here for introspection and for the ordinal computation — the
    # ordinal is the count of prior v2 rows for the name, a pure function of
    # prior graph state, so a rebuild that replays the same canonical
    # decisions in the same event order reproduces the same IDs. EXISTING v1
    # ids (kind-in-hash) are never recomputed; v1 rows may be backfilled
    # lazily but are never required.
    """
    CREATE TABLE IF NOT EXISTS entity_identities (
        entity_id      TEXT PRIMARY KEY REFERENCES entities(id),
        name_lower     TEXT NOT NULL,
        disambiguator  TEXT NOT NULL,
        id_scheme      TEXT NOT NULL,
        created_at     TEXT NOT NULL,
        UNIQUE(name_lower, disambiguator, id_scheme)
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS entity_identities_name_idx ON entity_identities(name_lower)",
    """
    CREATE TRIGGER IF NOT EXISTS entity_identities_no_update
    BEFORE UPDATE ON entity_identities
    BEGIN
        SELECT RAISE(ABORT, 'entity_identities is append-only (Invariant I2)');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS entity_identities_no_delete
    BEFORE DELETE ON entity_identities
    BEGIN
        SELECT RAISE(ABORT, 'entity_identities is append-only (Invariant I2)');
    END
    """,
    # ── entity_current_kind_v1: the backward-compatible kind read path ──────
    # COALESCE(latest assignment, entities.kind): an existing entity with no
    # assignment row resolves to its stored kind byte-identically (ZERO
    # backfill); the first assignment overlays it at read time. Hot paths use
    # the Python batch helper (entities.resolve_entity_kind_batch) which
    # additionally pipes the slug through the kind-registry revision chain so
    # a registry-level merge retypes every affected entity at read time with
    # a single revision row. Views are versioned by name (I3): a changed
    # definition ships as entity_current_kind_v2, never a redefinition.
    """
    CREATE VIEW IF NOT EXISTS entity_current_kind_v1 AS
    SELECT e.id AS entity_id,
           COALESCE(
               (SELECT ka.kind_slug FROM entity_kind_assignments ka
                WHERE ka.entity_id = e.id
                ORDER BY ka.assigned_at DESC, ka.id DESC LIMIT 1),
               e.kind
           ) AS kind_slug
    FROM entities e
    """,
    # ── proposed_ontology_revisions: the Schema-Evolver quarantine queue ─────
    # ADR-0003 Phase 4. MUTABLE derived state by the same documented exception
    # as proposed_corrections: a regenerable suggestion queue, not a belief —
    # the evolver re-derives it from usage signals, a decision updates its
    # status, and the *applied* revision (kind_registry / kind_revisions /
    # entity_kind_assignments rows, Phase 5) is the append-only part. No I2
    # trigger pair on purpose. proposed_corrections cannot host these rows:
    # its CHECK (kind IN ('retype','merge','merge_review')) is frozen and its
    # rows are entity-keyed, while these are ontology-keyed. Row ids carry an
    # 'ont_' prefix so the Phase-5 decide loop can dispatch on id shape.
    # One open proposal per (action, subject_slug) — re-running the evolver
    # won't duplicate or overwrite a decided one (INSERT OR IGNORE on the
    # UNIQUE), same discipline as proposed_corrections.
    """
    CREATE TABLE IF NOT EXISTS proposed_ontology_revisions (
        id            TEXT PRIMARY KEY,
        action        TEXT NOT NULL CHECK (action IN
                          ('add','rename','merge','split','deprecate')),
        subject_slug  TEXT NOT NULL,
        detail        TEXT NOT NULL,
        evidence      TEXT NOT NULL,
        confidence    REAL NOT NULL,
        detected_by   TEXT NOT NULL,
        detected_at   TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'proposed'
                      CHECK (status IN ('proposed','confirmed','rejected','applied')),
        decided_at    TEXT,
        decided_by    TEXT,
        UNIQUE(action, subject_slug)
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS proposed_ontology_revisions_status_idx "
    "ON proposed_ontology_revisions(status, detected_at DESC)",
    # ── proposed_conflict_resolutions: the operator conflict queue (ADR-0008) ─
    # MUTABLE derived state, not substrate — the conflict_resolver enqueues one
    # row per unresolved conflict pair, a decision flips its status, the pruner
    # ages out DECIDED rows past retention (open rows are never touched). Same
    # framing as proposed_corrections / proposed_ontology_revisions above and
    # export_jobs below: no append-only triggers on purpose (deciding mutates
    # ``status``), so it is deliberately NON-substrate (I2 protects the user's
    # MEMORY — events, interpretations, invalidations — not a regenerable
    # suggestion queue; see ADR-0005's memory-vs-telemetry line). The APPLIED
    # resolution is the append-only part: an invalidation event + a
    # conflict_resolution interpretation + an observe event (I2/I7), each of
    # which rides the export. This queue is REGENERABLE from the unresolved
    # conflict_flag rows, so it is EXCLUDED from the export (export_route.py).
    #
    # The subject of a conflict is an event PAIR, not an entity — that is why
    # this is its OWN queue and not a proposed_corrections row (which requires an
    # entity_id FK). ``pair_key`` is the deterministic ``min:max`` of the two
    # content hashes so the same unordered pair maps to one key; the partial
    # unique index on OPEN rows is the anti-re-nag guard. ``newer_hash`` records
    # which side is chronologically newer, so a directional decision maps onto
    # the frozen verdict enum without widening it (ADR-0008 / I1).
    """
    CREATE TABLE IF NOT EXISTS proposed_conflict_resolutions (
        id            TEXT PRIMARY KEY,
        pair_key      TEXT NOT NULL,
        event_a_id    TEXT NOT NULL,
        event_a_hash  TEXT NOT NULL,
        event_b_id    TEXT NOT NULL,
        event_b_hash  TEXT NOT NULL,
        newer_hash    TEXT NOT NULL,
        flag_verdict  TEXT NOT NULL,
        reason        TEXT NOT NULL,
        confidence    REAL NOT NULL,
        detected_by   TEXT NOT NULL,
        detected_at   TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'proposed'
                      CHECK (status IN ('proposed', 'applied', 'rejected')),
        resolution    TEXT
                      CHECK (resolution IS NULL OR resolution IN
                          ('superseded_older', 'superseded_newer', 'no_conflict')),
        decided_at    TEXT,
        decided_by    TEXT
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS proposed_conflict_resolutions_status_idx "
    "ON proposed_conflict_resolutions(status, detected_at DESC)",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS proposed_conflict_resolutions_open_unique
    ON proposed_conflict_resolutions(pair_key) WHERE status = 'proposed'
    """,
    # ── worker_watermarks: cold-path re-scan cursors (P2a, added 2026-07) ────
    # MUTABLE derived state, not substrate (Invariant I2 exception, same
    # framing as proposed_corrections above and export_jobs below). A cold-path
    # worker records how far it has processed (a ULID high-water cursor) so it
    # stops re-scanning already-handled history each cycle. Deleting a row =
    # that worker re-scans from zero once, NO data loss. No append-only
    # triggers on purpose. The cursor is the ULID ``through_id``; advances lag
    # the frontier by FRONTIER_LAG_SECONDS so a concurrent writer's pre-lock-
    # minted id (ids are NOT reliably monotonic with commit order) can't be
    # stranded below the cursor. ``through_created_at`` is stored for inspection
    # only. See substrate/watermarks.py for the full never-skip contract.
    # Additive per I3.
    """
    CREATE TABLE IF NOT EXISTS worker_watermarks (
        worker             TEXT NOT NULL PRIMARY KEY,
        through_created_at TEXT NOT NULL,
        through_id         TEXT NOT NULL,
        updated_at         TEXT NOT NULL
    ) STRICT
    """,
    # ── event_provenance: server-authoritative client provenance (ADR-0006) ──
    # A #29 multi-client vault needs "which client wrote this?" without folding
    # the answer into event identity. ``origin`` is part of the content_hash
    # (events.py content_hash(kind, origin, payload, parents)), so refining
    # ``origin`` per-client would silently split the dedup/hash contract — a hard
    # fork. Instead the authenticated client is stamped into this append-only
    # SIDECAR keyed by event_id, OUT of the hash: absence = a pre-provenance or
    # non-HTTP (direct/in-process) write, same overlay discipline as
    # edge_confidence_scores / edge_serves. The ``client`` slug is derived ONLY
    # from the credential (token label / OAuth client_name / master / local),
    # never from client-supplied headers or tool args (I4/I8 data-minimization);
    # no session_id / tool_call_id is recorded. One row per distinct
    # (event_id, client): a second client writing the same dedup'd event appends
    # a second (honest) row; the same client re-stamping is an INSERT OR IGNORE
    # no-op. Append-only per I2 (triggers below, from day one) — this is the
    # user's own provenance record, not telemetry, so it is NOT prunable and it
    # rides the export (I4). See docs/adr/ADR-0006-event-provenance.md.
    """
    CREATE TABLE IF NOT EXISTS event_provenance (
        id          TEXT PRIMARY KEY,
        event_id    TEXT NOT NULL REFERENCES events(id),
        client      TEXT NOT NULL,
        auth_kind   TEXT NOT NULL,
        verb        TEXT NOT NULL,
        stamped_at  TEXT NOT NULL,
        UNIQUE(event_id, client)
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS event_provenance_client_idx ON event_provenance(client)",
    "CREATE INDEX IF NOT EXISTS event_provenance_event_idx ON event_provenance(event_id)",
    """
    CREATE TRIGGER IF NOT EXISTS event_provenance_no_update
    BEFORE UPDATE ON event_provenance
    BEGIN
        SELECT RAISE(ABORT, 'event_provenance is append-only (ADR-0006 / Invariant I2)');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_provenance_no_delete
    BEFORE DELETE ON event_provenance
    BEGIN
        SELECT RAISE(ABORT, 'event_provenance is append-only (ADR-0006 / Invariant I2)');
    END
    """,
)


# Vector-table DDL — parameterized by embedding dimension and rendered at
# boot via str.format(dim=...). sqlite-vec must be loaded first.
# Stored separately from SCHEMA_DDL so it can be re-run with different
# dimensions if needed (e.g., during a model migration in a later phase).
VEC_DDL: tuple[str, ...] = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS events_vec USING vec0(
        content_hash TEXT PRIMARY KEY,
        embedding    FLOAT[{dim}]
    )
    """,
)


# The rebuilt proposed_corrections definition — the final shape: the
# 'edge_review' kind (ADR-0004) AND no inline UNIQUE (the one-open-per-(kind,
# entity) contract now lives in the partial index proposed_corrections_open_unique,
# P1-1). Fresh vaults get this shape via SCHEMA_DDL; existing vaults are migrated
# in place by :func:`migrate_proposed_corrections_kind_check` (CHECK widen) and
# :func:`migrate_proposed_corrections_open_unique` (drop the inline UNIQUE). Kept
# column-identical to the SCHEMA_DDL statement above so INSERT ... SELECT *
# round-trips positionally.
_PROPOSED_CORRECTIONS_REBUILD_DDL = """
    CREATE TABLE proposed_corrections (
        id            TEXT PRIMARY KEY,
        kind          TEXT NOT NULL CHECK (kind IN ('retype', 'merge', 'merge_review', 'edge_review')),
        entity_id     TEXT NOT NULL REFERENCES entities(id),
        detail        TEXT NOT NULL,
        evidence      TEXT NOT NULL,
        confidence    REAL NOT NULL,
        tier          TEXT NOT NULL CHECK (tier IN ('auto', 'review')),
        detected_by   TEXT NOT NULL,
        detected_at   TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'proposed'
                      CHECK (status IN ('proposed', 'confirmed', 'rejected', 'applied')),
        decided_at    TEXT,
        decided_by    TEXT
    ) STRICT
"""


def migrate_proposed_corrections_kind_check(conn: Any) -> bool:
    """Widen the ``proposed_corrections.kind`` CHECK to admit ``edge_review``.

    ``CREATE TABLE IF NOT EXISTS`` never alters an existing table's constraint,
    so a vault created before ADR-0004 keeps the frozen
    ``CHECK (kind IN ('retype','merge','merge_review'))`` and would REJECT an
    ``edge_review`` insert. ``proposed_corrections`` is explicitly NON-substrate
    (a regenerable suggestion queue, no I2 triggers — see its DDL comment), so a
    guarded, transactional table rebuild is legitimate and touches neither I2
    nor I3: the append-only *applied* corrections live elsewhere.

    Idempotent: the guard (does the stored DDL already mention ``edge_review``?)
    makes a re-run a no-op, and a fresh vault whose table already has the widened
    CHECK is never rebuilt. Returns True when a rebuild happened.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'proposed_corrections'"
    ).fetchone()
    if row is None or row["sql"] is None:
        return False  # table not created yet (shouldn't happen post-DDL)
    if "edge_review" in row["sql"]:
        return False  # already widened (fresh vault or prior migration)

    # Rebuild in ONE transaction: rename → recreate widened → copy → drop old →
    # re-index. FOREIGN KEYS are fine (nothing references this table; it
    # references entities, which are untouched). Column order is identical so
    # INSERT ... SELECT * maps positionally.
    with conn:
        conn.execute("ALTER TABLE proposed_corrections RENAME TO proposed_corrections_old")
        conn.execute(_PROPOSED_CORRECTIONS_REBUILD_DDL)
        conn.execute("INSERT INTO proposed_corrections SELECT * FROM proposed_corrections_old")
        conn.execute("DROP TABLE proposed_corrections_old")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS proposed_corrections_status_idx "
            "ON proposed_corrections(status)"
        )
        # The rebuild DDL no longer carries an inline UNIQUE — restore the
        # one-open-per-(kind, entity) contract as the partial index (P1-1), so a
        # pre-ADR-0004 vault migrating through here lands on the final shape and
        # the follow-up migrate_proposed_corrections_open_unique is a no-op.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS proposed_corrections_open_unique "
            "ON proposed_corrections(kind, entity_id) WHERE status = 'proposed'"
        )
    return True


def migrate_proposed_corrections_open_unique(conn: Any) -> bool:
    """Drop the table-level ``UNIQUE(kind, entity_id)`` in favor of a partial
    unique index on OPEN rows only. A decided proposal previously blocked
    every future proposal for the same (kind, subject) forever — for
    edge_review that froze ADR-0004's per-subject calibration growth.
    proposed_corrections is non-substrate (no I2 triggers, see the DDL
    comment); the guarded transactional rebuild is the same legitimate
    operation :func:`migrate_proposed_corrections_kind_check` already performs.
    Idempotent: guarded on the stored table SQL still containing the inline
    UNIQUE. Existing rows can never violate the partial index (the old
    constraint was strictly tighter). Returns True on rebuild.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'proposed_corrections'"
    ).fetchone()
    if row is None or row["sql"] is None:
        return False
    if "UNIQUE(kind, entity_id)" not in row["sql"]:
        # Already index-based (fresh vault / prior run / kind-check rebuilt it) —
        # just ensure the partial index exists.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS proposed_corrections_open_unique "
            "ON proposed_corrections(kind, entity_id) WHERE status = 'proposed'"
        )
        return False
    with conn:
        conn.execute("ALTER TABLE proposed_corrections RENAME TO proposed_corrections_old")
        conn.execute(_PROPOSED_CORRECTIONS_REBUILD_DDL)
        conn.execute("INSERT INTO proposed_corrections SELECT * FROM proposed_corrections_old")
        conn.execute("DROP TABLE proposed_corrections_old")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS proposed_corrections_status_idx "
            "ON proposed_corrections(status)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS proposed_corrections_open_unique "
            "ON proposed_corrections(kind, entity_id) WHERE status = 'proposed'"
        )
    return True
