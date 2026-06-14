"""DDL — locked at v1, additive-only per Invariant I3.

Every statement is idempotent so init runs cleanly on either a fresh or
fully-populated database.

DO NOT EDIT or REMOVE existing statements. If new fields or new tables
are needed, APPEND new statements at the end. Old data must remain
readable, queryable, and re-interpretable by every later version.
"""

from __future__ import annotations

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
    # logs. Append-only like everything else; readers compose the
    # timeline with ORDER BY recorded_at.
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
    """
    CREATE TRIGGER IF NOT EXISTS pipeline_events_no_update
    BEFORE UPDATE ON pipeline_events
    BEGIN
        SELECT RAISE(ABORT, 'pipeline_events is append-only (Invariant I2)');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS pipeline_events_no_delete
    BEFORE DELETE ON pipeline_events
    BEGIN
        SELECT RAISE(ABORT, 'pipeline_events is append-only (Invariant I2)');
    END
    """,
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
