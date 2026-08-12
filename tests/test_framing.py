"""Principal framing — the no-regression guard for ADR-0010.

Three layers, in order of severity:

  1. BYTE-IDENTITY (the cardinal sin guard): every principal-framed builder,
     rendered at the person default, must byte-match a frozen golden captured
     from the pre-ADR-0010 strings (``tests/goldens/framing_person.json``). afair
     is in daily real-world use; a single changed byte on the person path is a
     silent behavioral regression. If a builder changes intentionally, regenerate
     the golden and review the diff — same discipline as ``mcp_surface.json``.

  2. GREP COMPLETENESS (the missed-site guard): a hand-curated registry cannot
     catch a person-framing site the curation itself missed. So we GREP every
     ``afair/agents/*.py`` + ``afair/mcp/*.py`` module for the banned
     person-only phrases sitting in module-level prompt constants / builders, and
     assert every hit is a registered framing builder. A new hardcoded
     "personal memory vault" anywhere fails loudly.

  3. ORG REFRAME (the it-actually-does-something guard): rendered under an
     organization principal, every builder must contain the organization name /
     "organization" and must NOT contain the person-only banned phrases (where
     voice applies). The tuner judge criteria are EXEMPT — frozen alongside
     JUDGE_PROMPT_VERSION for judge reproducibility (documented in ADR-0010).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from afair.agents import (
    conflict_resolver,
    consolidator,
    entity_articles,
    entity_canonicalizer,
    entity_dedup,
    living_syntheses,
    prompts,
    schema_evolver,
)
from afair.agents.framing import (
    PrincipalFraming,
    framing_from_settings,
    person_framing,
    reset_current,
)
from afair.mcp import descriptions, resources
from afair.settings import Settings

GOLDEN_PATH = Path(__file__).resolve().parent / "goldens" / "framing_person.json"

# The complete registry of principal-framed builders. Keyed by the golden name;
# each value is a zero-arg callable that renders at the CURRENT module framing.
# Layer 2 (grep) cross-checks that this registry is exhaustive.
_BUILDERS = {
    "extractor_tool_description": prompts.extractor_tool_description,
    "extractor_system_prompt": prompts.extractor_system_prompt,
    "consolidator_system_prompt": consolidator.system_prompt,
    "conflict_resolver_tool_description": conflict_resolver.tool_description,
    "conflict_resolver_system_prompt": conflict_resolver.system_prompt,
    "entity_canonicalizer_match_system_prompt": entity_canonicalizer.match_system_prompt,
    "living_syntheses_system_prompt": living_syntheses.system_prompt,
    "schema_evolver_tool_description": schema_evolver.tool_description,
    "schema_evolver_system_prompt": schema_evolver.system_prompt,
    "entity_articles_system_prompt": entity_articles.system_prompt,
    "entity_dedup_system_prompt": entity_dedup.system_prompt,
    "server_instructions": descriptions.server_instructions,
    "remember_description": descriptions.remember_description,
    "recall_description": descriptions.recall_description,
    "observe_description": descriptions.observe_description,
    "session_start_description": resources.session_start_description,
}


def _org_settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        principal_kind="organization",
        principal_name="Acme Corp",
    )


def _org_framing() -> PrincipalFraming:
    return framing_from_settings(_org_settings())


# ── Layer 1: byte-identity snapshots at the person default ───────────────────


def _golden() -> dict[str, str]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def test_golden_covers_exactly_the_registry() -> None:
    """The frozen golden and the builder registry name the same set — no builder
    is snapshot-untested, no golden entry is orphaned."""
    assert set(_golden()) == set(_BUILDERS), (
        "framing_person.json and _BUILDERS disagree; regenerate the golden after "
        "adding/removing a framing builder"
    )


@pytest.mark.parametrize("name", sorted(_BUILDERS))
def test_person_default_render_is_byte_identical(name: str) -> None:
    """The cardinal-sin guard: person-default render must byte-match the golden.

    If this fails you changed a person-path string. If intentional, regenerate
    ``tests/goldens/framing_person.json`` and review the diff. If NOT, you have a
    person-default regression — fix the builder, not the golden.
    """
    reset_current()  # ensure the person default is active
    assert _BUILDERS[name]() == _golden()[name]


def test_person_framing_default_is_active_singleton() -> None:
    """``reset_current`` restores the byte-identical person default."""
    reset_current()
    assert person_framing().kind == "person"
    assert person_framing().name == ""


# ── Layer 2: grep-based completeness (a missed site fails loudly) ─────────────

# Person-only phrases that must NOT appear hardcoded in a prompt-carrying string
# literal unless that literal is provably part of a registered framing builder's
# person render. Lowercased match. Includes the broad "personal vault" (no
# possessive) and the bare "the user's" so an unregistered owner-phrase in any
# prompt string is caught, not just the exact vault idioms.
_BANNED_PERSON_PHRASES = (
    "personal memory vault",
    "the user's substrate",
    "personal memory",  # catches "PERSONAL MEMORY vault", "the user's personal memory"
    "personal vault's",
    "personal vault",
    "personal-vault",
    "the user's",
)

# Modules that host REGISTERED framing builders (or, for framing.py, the person
# phrase VALUES themselves). Being listed here does NOT whitelist the module:
# every banned-phrase-bearing literal in these modules must still be a verbatim
# (case-insensitive) substring of some registered builder's person render — so a
# NEW hardcoded person phrase inside an already-registered module fails too.
_REGISTERED_BUILDER_MODULES = {
    "afair/agents/framing.py",
    "afair/agents/prompts.py",
    "afair/agents/consolidator.py",
    "afair/agents/conflict_resolver.py",
    "afair/agents/entity_canonicalizer.py",
    "afair/agents/living_syntheses.py",
    "afair/agents/schema_evolver.py",
    "afair/agents/entity_articles.py",
    "afair/agents/entity_dedup.py",
    "afair/mcp/descriptions.py",
    "afair/mcp/resources.py",
}

# The tuner judge criteria are the ONLY intentional person-flavored prompt text
# left unparametrized (frozen with JUDGE_PROMPT_VERSION for judge
# reproducibility — ADR-0010); llm_judge and temporal are neutral. Explicitly
# exempt, and asserted CLEAN of banned phrases below so the exemption stays a
# conscious decision rather than a hole.
_EXEMPT_MODULES = {
    "afair/agents/tuner.py",
    "afair/agents/llm_judge.py",
    "afair/agents/temporal.py",
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _prompt_string_literals(source: str) -> list[tuple[int, str]]:
    """Every string literal in ``source`` that could feed an LLM prompt or an
    AI-facing surface — where a hardcoded person phrase would be a real leak.

    Excluded via the AST: every bare-string ``Expr`` statement. That covers
    module/class/function docstrings AND attribute docstrings (the string
    statement after a field assignment) — a bare string statement has no runtime
    effect, so it can never reach a prompt; it is engineering prose the spec
    explicitly leaves alone. Included: plain string constants and each CONSTANT
    SEGMENT of an f-string individually (segment granularity lets the
    registered-module substring check below stay exact across placeholders).
    """
    import ast

    tree = ast.parse(source)

    # Every bare-string expression statement is documentation, not prompt text.
    doc_ids: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            doc_ids.add(id(node.value))

    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if id(node) in doc_ids:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # NB: an f-string's constant segments are Constant nodes inside a
            # JoinedStr and are collected here individually by the walk.
            out.append((node.lineno, node.value))
    return out


def _person_renders_lower() -> list[str]:
    """Every registered builder's person render, lowercased, from the golden."""
    return [v.lower() for v in _golden().values()]


def test_no_unregistered_person_framing_site() -> None:
    """Scan every agents/ + mcp/ module's prompt string literals (docstrings
    excluded) for the banned person phrases. A hit is legitimate ONLY when the
    literal is a verbatim (case-insensitive) substring of some registered
    builder's person render — i.e. it IS part of a parametrized builder's person
    branch. Everything else fails loudly, including:

      - a hardcoded person phrase in an UNREGISTERED module, and
      - a NEW hardcoded person phrase inside an already-REGISTERED module (a
        literal that is not part of any registered person render), which a
        module-presence check would have silently waved through.

    This is the plan-review amendment: exhaustiveness is proven from the source
    (AST over prompt-carrying literals) against the frozen person golden, not
    from a hand-curated list."""
    root = _repo_root()
    renders = _person_renders_lower()
    offenders: list[str] = []
    for module in sorted(
        (*(root / "afair" / "agents").glob("*.py"), *(root / "afair" / "mcp").glob("*.py"))
    ):
        rel = module.relative_to(root).as_posix()
        source = module.read_text(encoding="utf-8")
        for lineno, literal in _prompt_string_literals(source):
            low = literal.lower()
            if not any(phrase in low for phrase in _BANNED_PERSON_PHRASES):
                continue
            hits = [p for p in _BANNED_PERSON_PHRASES if p in low]
            if rel in _REGISTERED_BUILDER_MODULES and any(low in r for r in renders):
                # The literal is verbatim part of a registered builder's person
                # render (a framing field value, a person-branch constant, or an
                # org-reframe source phrase). The byte-identity + org tests
                # prove that builder is actually parametrized.
                continue
            snippet = literal.strip().replace("\n", " ")[:80]
            offenders.append(f"{rel}:{lineno}: {snippet!r} (phrases {hits})")
    assert not offenders, (
        "hardcoded person-framing phrase(s) not covered by a registered framing "
        "builder (ADR-0010 completeness guard). Route the string through a "
        "framing builder:\n" + "\n".join(offenders)
    )


def test_org_renders_contain_no_person_owner_phrases() -> None:
    """Every builder's ORG render is free of the person owner-phrases. Closes
    the one gap the source-scan cannot see: a person phrase hardcoded INSIDE a
    builder body is a verbatim part of the person render (so the
    substring-allowance above accepts the literal), but it would also survive
    into the org render — which this test fails. Together with the source scan,
    a new hardcoded person phrase in a registered module is always caught.

    "personal vault" (broad, no possessive) is excluded here ONLY because the
    actor guidance legitimately says "Omit for a personal vault" in every mode
    (it tells an agent when NOT to pass actor); the possessive/vault idioms and
    "the user's" must never appear in an org render.
    """
    banned_in_org = (
        "personal memory vault",
        "the user's substrate",
        "personal memory",
        "personal vault's",
        "personal-vault",
        "the user's",
    )
    org = _org_framing()
    for name, builder in sorted(_BUILDERS.items()):
        rendered = builder(org).lower()
        hits = [p for p in banned_in_org if p in rendered]
        assert not hits, (
            f"{name} org render still carries person owner-phrase(s) {hits} — "
            "a hardcoded person phrase inside the builder body; route it through "
            "a framing field."
        )


def test_exempt_modules_are_declared_not_accidental() -> None:
    """The tuner/judge/temporal exemptions are intentional (ADR-0010). Their
    PROMPT literals (docstrings excluded, same scan as the main guard) must stay
    clean of banned person phrases — if one ever grows one, the frozen-judge
    exemption must be re-examined consciously rather than silently absorbed."""
    root = _repo_root()
    for rel in sorted(_EXEMPT_MODULES):
        source = (root / rel).read_text(encoding="utf-8")
        hits = [
            (lineno, phrase)
            for lineno, literal in _prompt_string_literals(source)
            for phrase in _BANNED_PERSON_PHRASES
            if phrase in literal.lower()
        ]
        assert not hits, (
            f"{rel} is an ADR-0010 framing-exempt module but its prompt literals "
            f"now contain person phrase(s) {hits}; re-examine the exemption "
            f"(frozen judge criteria vs. principal framing)."
        )


# ── Layer 3: organization reframe actually swaps the framing ─────────────────


@pytest.mark.parametrize("name", sorted(_BUILDERS))
def test_org_render_differs_from_person(name: str) -> None:
    """Every builder renders DIFFERENTLY under an organization principal — the
    org mode is not a no-op."""
    org = _org_framing()
    person = _BUILDERS[name]()  # module default = person
    assert _BUILDERS[name](org) != person, f"{name} org render is identical to person"


@pytest.mark.parametrize("name", sorted(_BUILDERS))
def test_org_render_names_the_organization(name: str) -> None:
    """Every org render references the organization (by name or the word
    'organization'), so the AI knows whose vault it's reasoning over."""
    org = _org_framing()
    rendered = _BUILDERS[name](org).lower()
    assert "acme corp" in rendered or "organization" in rendered, (
        f"{name} org render names neither the org nor 'organization': {rendered[:200]!r}"
    )


# Which builders carry a person-only banned phrase that must be GONE in org mode.
# (The MCP descriptions keep incidental example prose like "personal things" in a
# non-owner sense; the org reframe targets OWNER language, so we assert the
# owner-framing banned phrases are gone from the prompt-role builders where the
# framing applies.)
_OWNER_FRAMED_BUILDERS = {
    "extractor_system_prompt": "personal memory vault",
    "conflict_resolver_system_prompt": "personal memory",
    "conflict_resolver_tool_description": "personal memory",
    "entity_canonicalizer_match_system_prompt": "personal memory vault",
    "living_syntheses_system_prompt": "personal vault's",
    "schema_evolver_system_prompt": "personal memory",
    "schema_evolver_tool_description": "personal memory",
    "entity_articles_system_prompt": "personal vault's",
    "entity_dedup_system_prompt": "personal vault's",
}


@pytest.mark.parametrize("name,banned", sorted(_OWNER_FRAMED_BUILDERS.items()))
def test_org_render_drops_person_owner_phrase(name: str, banned: str) -> None:
    """The person owner phrase is GONE from the org render of each prompt-role
    builder where the framing applies."""
    org = _org_framing()
    assert banned not in _BUILDERS[name](org).lower(), (
        f"{name} org render still contains the person owner phrase {banned!r}"
    )


def test_second_person_voice_relaxed_in_org_consolidator() -> None:
    """The consolidator's second-person 'you' voice rule is relaxed for an org
    (the org names members instead of addressing a single reader)."""
    org = _org_framing()
    rendered = consolidator.system_prompt(org)
    assert "second person" not in rendered.lower()
    assert 'write in second person ("you decided"' not in rendered.lower()


# ── voice reset hygiene (belt-and-suspenders vs the autouse conftest reset) ──


def test_framing_reset_between_modes() -> None:
    """Setting org then resetting restores the person default byte-for-byte."""
    from afair.agents import framing as framing_mod

    framing_mod.set_current(_org_framing())
    assert framing_mod.current().kind == "organization"
    reset_current()
    assert framing_mod.current().kind == "person"
    # And a person-default render is byte-identical again.
    assert prompts.extractor_system_prompt() == _golden()["extractor_system_prompt"]
