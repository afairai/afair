#!/usr/bin/env python3
"""Read-only dry-run of the proposed entity-review queue against a live vault.

Reports what the broadened entity-audit WOULD surface — without writing
anything (only SELECTs; the substrate's I2 triggers block writes anyway). The
point is to size the queue and check signal-vs-noise against real data before
building the worker:

- entity counts by kind (the 7 emergent kinds),
- mentions by match_method (exact / alias / embedding / llm / new) — the
  `llm` ones are the fuzzy auto-clusters where false connections come from,
- auto-merges (entity_dedup),
- the deterministic type-mismatch candidates (domain-as-person, year-as-person),
- samples of each fuzzy decision so you can eyeball whether they're real
  errors or fine.

Run ON the Fly machine (sqlcipher3 is Linux-only). Sources AFAIR_VAULT_KEY +
vault dir from /proc/1/environ (the running app process), so it opens exactly
the DB the server uses regardless of the ssh session's own env.

    cat scripts/audit_dryrun.py | fly ssh console -a <app> -C "/app/.venv/bin/python -"
"""

from __future__ import annotations

import os


def _load_app_env() -> None:
    """Pull the secrets the app boots with from PID 1's environment.

    Fly injects secrets into the app process, not necessarily into an ssh
    shell. Reading /proc/1/environ gets the exact AFAIR_VAULT_KEY (and vault
    dir / environment) the server uses, so open_db decrypts the same file.
    """
    try:
        with open("/proc/1/environ", "rb") as fh:
            raw = fh.read()
    except OSError:
        return
    for chunk in raw.split(b"\x00"):
        if b"=" not in chunk:
            continue
        k, _, v = chunk.partition(b"=")
        key = k.decode("utf-8", "replace")
        if key in os.environ:
            continue
        if key.startswith(("AFAIR_", "VAULT_", "ENVIRONMENT")):
            os.environ[key] = v.decode("utf-8", "replace")


def main() -> int:
    _load_app_env()

    from afair.settings import Settings
    from afair.substrate import open_db
    from afair.substrate.db import set_vault_key

    s = Settings()
    if s.vault_key is not None:
        set_vault_key(s.vault_key.get_secret_value().encode("utf-8"))
    db = open_db(s.vault_dir, embedding_dim=s.embedding_dim)

    def q(sql: str, *args: object) -> list:
        return db.execute(sql, args).fetchall()

    def has_table(name: str) -> bool:
        return bool(q("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", name))

    print(f"# afair audit dry-run — vault {s.vault_dir}")
    print(f"# environment={s.environment}\n")

    # ── events (orientation) ─────────────────────────────────────────────────
    if has_table("events"):
        rows = q("SELECT kind, COUNT(*) c FROM events GROUP BY kind ORDER BY c DESC")
        total = sum(r["c"] for r in rows)
        print(f"events: {total} total")
        for r in rows:
            print(f"  {r['kind']:<24} {r['c']}")
        print()

    if not has_table("entities"):
        print("no entities table — entity graph not built on this vault.")
        return 0

    # ── entities by kind (only those not merged away = the live graph) ───────
    live = q(
        """
        SELECT e.kind, COUNT(*) c
        FROM entities e
        LEFT JOIN entity_merges m ON m.from_entity_id = e.id
        WHERE m.id IS NULL
        GROUP BY e.kind ORDER BY c DESC
        """
    )
    total_live = sum(r["c"] for r in live)
    total_all = q("SELECT COUNT(*) c FROM entities")[0]["c"]
    print(f"entities: {total_all} rows, {total_live} live (not merged away)")
    for r in live:
        print(f"  {r['kind']:<24} {r['c']}")
    print()

    # ── mentions by match_method — the automatic clustering decisions ────────
    if has_table("entity_mentions"):
        mm = q(
            "SELECT match_method, COUNT(*) c FROM entity_mentions "
            "GROUP BY match_method ORDER BY c DESC"
        )
        total_m = sum(r["c"] for r in mm)
        print(f"entity_mentions: {total_m} — by match_method (how each was clustered)")
        for r in mm:
            print(f"  {r['match_method']:<12} {r['c']}")
        fuzzy = sum(r["c"] for r in mm if r["match_method"] in ("llm", "embedding"))
        safe = total_m - fuzzy
        print(f"  -> fuzzy (llm+embedding) = {fuzzy}   deterministic (exact/alias/new) = {safe}")
        print()

        # Sample the fuzzy matches: surface_form -> canonical entity (+kind, conf).
        print("FUZZY MATCH SAMPLES (llm/embedding) — would go to the ACTIVE queue:")
        sample = q(
            """
            SELECT em.surface_form, em.match_method, em.confidence,
                   e.canonical_name, e.kind
            FROM entity_mentions em
            JOIN entities e ON e.id = em.entity_id
            WHERE em.match_method IN ('llm', 'embedding')
            ORDER BY em.confidence ASC
            LIMIT 30
            """
        )
        if not sample:
            print("  (none)")
        for r in sample:
            print(
                f"  [{r['match_method']}/{r['confidence']:.2f}] "
                f'"{r["surface_form"]}" -> {r["canonical_name"]} ({r["kind"]})'
            )
        print()

    # ── auto-merges (entity_dedup + canonicalizer demotions) ─────────────────
    if has_table("entity_merges"):
        n_merge = q("SELECT COUNT(*) c FROM entity_merges")[0]["c"]
        by_who = q(
            "SELECT merged_by, COUNT(*) c FROM entity_merges GROUP BY merged_by ORDER BY c DESC"
        )
        print(f"entity_merges: {n_merge} total")
        for r in by_who:
            print(f"  by {r['merged_by']:<22} {r['c']}")
        merges = q(
            """
            SELECT a.canonical_name fn, a.kind fk,
                   b.canonical_name tn, b.kind tk,
                   mg.confidence, mg.merged_by, mg.reason
            FROM entity_merges mg
            JOIN entities a ON a.id = mg.from_entity_id
            JOIN entities b ON b.id = mg.into_entity_id
            ORDER BY mg.merged_at DESC
            LIMIT 30
            """
        )
        print("MERGE SAMPLES (most recent) — would go to the ACTIVE queue:")
        for r in merges:
            conf = r["confidence"]
            conf_s = f"{conf:.2f}" if conf is not None else "n/a"
            print(
                f'  [{conf_s}/{r["merged_by"]}] "{r["fn"]}" ({r["fk"]}) -> "{r["tn"]}" ({r["tk"]})'
            )
        print()

    # ── deterministic type-mismatch candidates (the v0 detectors) ────────────
    import re

    domain_re = re.compile(r"\b[\w-]+\.(team|me|com|ai|io|app|dev|net|org|co|xyz|de|ch|eu)\b", re.I)
    year_re = re.compile(r"\b(?:19|20)\d{2}\b")
    persons = q(
        """
        SELECT e.canonical_name
        FROM entities e
        LEFT JOIN entity_merges m ON m.from_entity_id = e.id
        WHERE m.id IS NULL AND e.kind = 'person'
        """
    )
    dom = [r["canonical_name"] for r in persons if domain_re.search(r["canonical_name"])]
    yr = [r["canonical_name"] for r in persons if year_re.search(r["canonical_name"])]
    print("DETERMINISTIC TYPE-MISMATCH candidates (person filed wrong):")
    print(f"  domain-as-person ({len(dom)}): {dom[:15]}")
    print(f"  year-as-person   ({len(yr)}): {yr[:15]}")
    print()

    # ── surface-form merge candidates across ALL kinds (subset names) ────────
    all_live = q(
        """
        SELECT e.id, e.canonical_name, e.kind
        FROM entities e
        LEFT JOIN entity_merges m ON m.from_entity_id = e.id
        WHERE m.id IS NULL
        """
    )
    tok = re.compile(r"\w+", re.UNICODE)
    by_kind: dict[str, list[tuple[str, frozenset]]] = {}
    for r in all_live:
        toks = frozenset(t.lower() for t in tok.findall(r["canonical_name"]))
        by_kind.setdefault(r["kind"], []).append((r["canonical_name"], toks))
    subset_pairs = []
    for kind, items in by_kind.items():
        for i in range(len(items)):
            for j in range(len(items)):
                if i == j:
                    continue
                na, ta = items[i]
                nb, tb = items[j]
                if ta and tb and ta < tb:
                    subset_pairs.append((kind, na, nb))
    print(f"SURFACE-FORM merge candidates (same kind, name subset): {len(subset_pairs)}")
    for kind, na, nb in subset_pairs[:20]:
        print(f'  [{kind}] "{na}"  ⊂  "{nb}"')
    print()

    # ── projected queue size ─────────────────────────────────────────────────
    active = 0
    if has_table("entity_mentions"):
        active += q(
            "SELECT COUNT(*) c FROM entity_mentions WHERE match_method IN ('llm','embedding')"
        )[0]["c"]
    if has_table("entity_merges"):
        active += q("SELECT COUNT(*) c FROM entity_merges")[0]["c"]
    active += len(dom) + len(yr) + len(subset_pairs)
    print("=" * 60)
    print(f"PROJECTED ACTIVE queue (fuzzy + merges + type-mismatch): ~{active}")
    print(f"Live entities: {total_live}. Decide if that volume is reviewable.")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
