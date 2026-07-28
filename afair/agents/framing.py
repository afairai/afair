"""Principal framing — the one place that decides whether the vault's
AI-facing cognition speaks about "the user" (a person) or "the organization"
(ADR-0010).

Every LLM prompt and MCP-surface string that used to hardcode a person
assumption ("a personal memory vault", "the user's substrate", second-person
voice) now renders through a single frozen :class:`PrincipalFraming` value.
The person default (:func:`person_framing`) renders EXACTLY the strings that
shipped before ADR-0010 — enforced by byte-identity snapshot tests — so a
person vault upgrading sees zero behavioral change. An organization principal
swaps the descriptor and possessive ("the organization's vault") and relaxes
the second-person voice rule.

Delivery
--------
The active framing is a module-level singleton set ONCE at server boot
(:func:`set_current`, called from ``build_server``) and read by every prompt
builder via :func:`current`. This is deliberately simpler than threading a
framing argument through 8 surface builders and 9 cold-path workers: the
principal is process-wide and immutable per instance (I8 — one instance, one
principal), so a boot-time singleton matches the invariant exactly. Tests reset
it to the person default via an autouse conftest fixture.

Byte-identity contract
----------------------
``person_framing()`` MUST render today's strings verbatim. If you change a
field's person value, a snapshot test in ``tests/test_framing.py`` goes red —
that is the guard, not a code comment. Change person values only with an
explicit, reviewed regeneration of the snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..settings import Settings


@dataclass(frozen=True)
class PrincipalFraming:
    """The vocabulary a vault's AI-facing prompts use to name their subject.

    Frozen + hashable so builders can be ``lru_cache``d on it. One value per
    running instance (person default, or an organization variant). Every field
    is a ready-to-interpolate fragment; the builders below compose them into
    the full prompt/surface strings.
    """

    kind: str
    """``"person"`` or ``"organization"`` — the principal kind."""

    name: str
    """Organization display name, or ``""`` for a person principal."""

    vault_descriptor: str
    """Lowercase noun phrase for the vault, e.g. ``"personal memory vault"`` or
    ``"organization's memory vault"``. Used mid-sentence ("a {descriptor}")."""

    vault_descriptor_caps: str
    """Upper-case-emphasis variant for prompts that shout the phrase, e.g.
    ``"PERSONAL MEMORY"`` (as in "a PERSONAL MEMORY vault")."""

    owner_possessive: str
    """Possessive noun phrase naming the owner, e.g. ``"the user's"`` or
    ``"the organization's"``. Composes "the user's substrate" → "{poss}
    substrate"."""

    substrate_phrase: str
    """The owner's substrate/persistent-memory phrase, e.g.
    ``"the user's substrate"`` / ``"the organization's substrate"``."""

    memory_phrase: str
    """The owner's memory phrase for "events from {phrase} relate", e.g.
    ``"the user's personal memory"`` / ``"the organization's memory"``. Used by
    the conflict-resolver tool description."""

    kind_owner_vault_phrase: str
    """The owner's vault phrase for "a new entity kind for {phrase}", e.g.
    ``"the user's personal memory vault"`` / ``"the organization's memory
    vault"``. Used by the schema-evolver tool description."""

    voice_rule: str
    """Voice instruction for the narrative writers (consolidator, articles).
    Person: second-person "you". Organization: neutral, name the members."""

    data_owner_phrase: str
    """How the schema-evolver refers to whose data is clustering, e.g.
    ``"THIS user's data"`` / ``"THIS organization's data"``."""

    this_vault_phrase: str
    """Short possessive for "for THIS vault"-style phrasing; person and org
    both use ``"THIS vault"`` today (kept as a field so an org variant could
    diverge without touching builders)."""

    vault_possessive: str
    """Possessive naming the vault itself, for "a {possessive} living syntheses"
    constructions, e.g. ``"personal vault's"`` / ``"organization's vault's"``.
    Used by the living-syntheses, entity-articles, and entity-dedup workers."""

    persistent_memory_phrase: str
    """The owner's persistent-memory phrase, e.g. ``"the user's persistent
    memory"`` / ``"the organization's persistent memory"`` (consolidator)."""

    articles_voice_rule: str
    """Voice instruction for the per-entity article writer. Person uses
    second-person "where the user is involved"; org names members neutrally."""


@cache
def person_framing() -> PrincipalFraming:
    """The default framing: renders EXACTLY the pre-ADR-0010 person strings.

    Cached so the frozen singleton is shared. Do NOT edit these values without
    regenerating the byte-identity snapshots in ``tests/test_framing.py``.
    """
    return PrincipalFraming(
        kind="person",
        name="",
        vault_descriptor="personal memory vault",
        vault_descriptor_caps="PERSONAL MEMORY",
        owner_possessive="the user's",
        substrate_phrase="the user's substrate",
        memory_phrase="the user's personal memory",
        kind_owner_vault_phrase="the user's personal memory vault",
        voice_rule=(
            'Write in second person ("you decided", "you and Sajinth shipped X"),\n'
            "present tense, plain language."
        ),
        data_owner_phrase="THIS user's data",
        this_vault_phrase="THIS vault",
        vault_possessive="personal vault's",
        persistent_memory_phrase="the user's persistent memory",
        articles_voice_rule=(
            "Plain language, present tense, second person where the user is involved\n"
            '("you decided", "you and Sajinth shipped X").'
        ),
    )


@cache
def _organization_framing(name: str) -> PrincipalFraming:
    """Framing for an organization principal named ``name`` (cached per name).

    Swaps the person descriptor and possessives for organization ones and
    relaxes the second-person voice rule to a neutral, member-naming voice.
    ``name`` is already sanitized by ``Settings._validate_principal``.
    """
    possessive = f"{name}'s"
    return PrincipalFraming(
        kind="organization",
        name=name,
        vault_descriptor=f"memory vault for the organization {name}",
        vault_descriptor_caps="ORGANIZATION MEMORY",
        owner_possessive=possessive,
        substrate_phrase=f"{possessive} substrate",
        memory_phrase=f"{possessive} memory",
        kind_owner_vault_phrase=f"{possessive} memory vault",
        voice_rule=(
            "Write in plain language, present tense, naming the organization's "
            "members and agents explicitly rather than addressing a single reader."
        ),
        data_owner_phrase=f"{name}'s data",
        this_vault_phrase="THIS vault",
        vault_possessive=f"{possessive} vault's",
        persistent_memory_phrase=f"{possessive} persistent memory",
        articles_voice_rule=(
            "Plain language, present tense, naming the organization's members "
            "explicitly rather than addressing a single reader."
        ),
    )


def framing_from_settings(settings: Settings) -> PrincipalFraming:
    """Build the framing for a settings object.

    Person (the default) returns the byte-identical :func:`person_framing`.
    Organization returns the org variant keyed on the sanitized name.
    """
    if settings.principal_kind == "organization":
        return _organization_framing(settings.principal_name)
    return person_framing()


# ── module-level active framing (set once at server boot) ────────────────────

_current: PrincipalFraming = person_framing()


def current() -> PrincipalFraming:
    """The active framing for this process. Person default until boot sets it.

    Every prompt/surface builder reads this. Outside a running server (bare
    tests, scripts) it is the person default, which is why the golden and every
    person-mode snapshot stay byte-identical without any explicit setup.
    """
    return _current


def set_current(framing: PrincipalFraming) -> None:
    """Install the active framing. Called once from ``build_server`` at boot.

    Idempotent and cheap; a test's autouse fixture resets it to the person
    default between tests so cross-test leakage can't happen.
    """
    global _current
    _current = framing


def reset_current() -> None:
    """Reset the active framing to the person default. For test fixtures."""
    global _current
    _current = person_framing()
