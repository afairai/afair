"""MMR diversity rerank — pure-function tests for ``substrate/search.py``.

The pass exists because a vault fed by daily jobs grows families of
near-identical records, and relevance ranking alone serves the whole family.
These tests pin the three properties the recall path depends on: it is a pure
reordering (nothing added, nothing dropped), it is deterministic, and
``lambda_=1.0`` is exactly the identity so the knob has a provable no-op end.
"""

from __future__ import annotations

import pytest

from afair.substrate.events import Event
from afair.substrate.search import (
    MMR_MAX_CANDIDATES,
    event_token_profile,
    lexical_similarity,
    mmr_rerank,
)


def _event(eid: str, text: str) -> Event:
    return Event(
        id=eid,
        content_hash=f"sha256:{eid}",
        created_at="2026-08-03T10:00:00+00:00",
        origin="agent",
        kind="remember",
        payload={"content_type": "text", "text": text},
        parent_hashes=None,
        schema_version=1,
    )


# The shape that motivated the whole pass: three handoffs saying the same
# thing, two records saying something else entirely.
_DUPLICATE_FAMILY = [
    _event(
        "dup1",
        "Session handoff: Zentrale deployed, dashboard verified, telegram router checked",
    ),
    _event(
        "dup2",
        "Session handoff: Zentrale deployed and dashboard verified, telegram router checked",
    ),
    _event(
        "dup3",
        "Session handoff: Zentrale deployed, the dashboard verified, telegram router checked",
    ),
    _event("other1", "Amazon advertising loss of seventy euro per day on the headband listing"),
    _event("other2", "Mounjaro dosage note and the pharmacy appointment next tuesday"),
]


class TestPurity:
    """MMR reorders. It never invents a candidate and never loses one."""

    def test_returns_a_permutation_of_the_input(self) -> None:
        out = mmr_rerank(list(_DUPLICATE_FAMILY), limit=len(_DUPLICATE_FAMILY))
        assert {e.id for e in out} == {e.id for e in _DUPLICATE_FAMILY}
        assert len(out) == len(_DUPLICATE_FAMILY)

    def test_respects_the_limit(self) -> None:
        out = mmr_rerank(list(_DUPLICATE_FAMILY), limit=2)
        assert len(out) == 2

    def test_zero_limit_returns_empty(self) -> None:
        assert mmr_rerank(list(_DUPLICATE_FAMILY), limit=0) == []

    def test_single_and_empty_inputs_pass_through(self) -> None:
        assert mmr_rerank([], limit=5) == []
        one = [_event("solo", "a single memory")]
        assert mmr_rerank(one, limit=5) == one

    def test_lambda_one_is_the_identity(self) -> None:
        """The provable no-op end of the knob — pure relevance, original order."""
        out = mmr_rerank(list(_DUPLICATE_FAMILY), limit=5, lambda_=1.0)
        assert [e.id for e in out] == [e.id for e in _DUPLICATE_FAMILY]

    def test_first_pick_is_always_the_most_relevant(self) -> None:
        """Nothing outranks position 0: with an empty selected set the
        similarity term is zero for every candidate, so relevance decides."""
        out = mmr_rerank(list(_DUPLICATE_FAMILY), limit=5)
        assert out[0].id == "dup1"


class TestDiversity:
    def test_near_duplicates_are_demoted(self) -> None:
        """The whole point: one member of the family survives the top slots."""
        out = mmr_rerank(list(_DUPLICATE_FAMILY), limit=3)
        top_ids = [e.id for e in out]
        duplicates_in_top = [i for i in top_ids if i.startswith("dup")]
        assert len(duplicates_in_top) == 1, f"expected one of the family, got {top_ids}"
        assert "other1" in top_ids
        assert "other2" in top_ids

    def test_distinct_input_keeps_its_ranking(self) -> None:
        """No near-duplicates → the similarity penalty is ~0 → order survives.

        This is what makes the pass safe to apply unconditionally on its path:
        it only moves things when there is redundancy to punish.
        """
        distinct = [
            _event("a", "amazon advertising spend on the headband listing"),
            _event("b", "mounjaro dosage and pharmacy appointment"),
            _event("c", "notion database migration for the invoice archive"),
            _event("d", "ollama model storage moved to the internal ssd"),
        ]
        out = mmr_rerank(distinct, limit=4)
        assert [e.id for e in out] == ["a", "b", "c", "d"]

    def test_lower_lambda_penalizes_redundancy_harder(self) -> None:
        aggressive = mmr_rerank(list(_DUPLICATE_FAMILY), limit=5, lambda_=0.2)
        assert [e.id for e in aggressive[:3]].count("dup1") == 1
        # Under a hard diversity weight the remaining family members sink to
        # the very back, behind both unrelated records.
        assert {e.id for e in aggressive[3:]} == {"dup2", "dup3"}


class TestDeterminism:
    def test_repeated_runs_are_identical(self) -> None:
        first = [e.id for e in mmr_rerank(list(_DUPLICATE_FAMILY), limit=5)]
        second = [e.id for e in mmr_rerank(list(_DUPLICATE_FAMILY), limit=5)]
        assert first == second

    def test_ties_resolve_to_the_earlier_rank(self) -> None:
        """Two identical payloads at different ranks: the earlier one wins."""
        twins = [
            _event("first", "identical body text for the tie break check"),
            _event("second", "identical body text for the tie break check"),
        ]
        out = mmr_rerank(twins, limit=2)
        assert [e.id for e in out] == ["first", "second"]

    def test_payload_key_order_does_not_change_the_profile(self) -> None:
        a = Event(
            id="a",
            content_hash="sha256:a",
            created_at="2026-08-03T10:00:00+00:00",
            origin="agent",
            kind="remember",
            payload={"text": "alpha beta", "context": "gamma delta", "content_type": "text"},
            parent_hashes=None,
            schema_version=1,
        )
        b = Event(
            id="b",
            content_hash="sha256:b",
            created_at="2026-08-03T10:00:00+00:00",
            origin="agent",
            kind="remember",
            payload={"content_type": "text", "context": "gamma delta", "text": "alpha beta"},
            parent_hashes=None,
            schema_version=1,
        )
        assert event_token_profile(a) == event_token_profile(b)


class TestTokenProfile:
    def test_unreadable_payload_yields_an_empty_profile(self) -> None:
        empty = Event(
            id="x",
            content_hash="sha256:x",
            created_at="2026-08-03T10:00:00+00:00",
            origin="agent",
            kind="observe",
            payload={"counts": [1, 2, 3]},
            parent_hashes=None,
            schema_version=1,
        )
        assert event_token_profile(empty) == {}

    def test_an_empty_profile_is_similar_to_nothing(self) -> None:
        """A payload we cannot read is never treated as a duplicate."""
        profile = event_token_profile(_DUPLICATE_FAMILY[0])
        assert lexical_similarity({}, profile) == 0.0
        assert lexical_similarity(profile, {}) == 0.0

    def test_identical_text_scores_one(self) -> None:
        profile = event_token_profile(_event("a", "same words in the same order"))
        assert lexical_similarity(profile, profile) == pytest.approx(1.0)

    def test_similarity_is_symmetric_and_bounded(self) -> None:
        a = event_token_profile(_DUPLICATE_FAMILY[0])
        b = event_token_profile(_DUPLICATE_FAMILY[3])
        assert lexical_similarity(a, b) == lexical_similarity(b, a)
        assert 0.0 <= lexical_similarity(a, b) <= 1.0

    def test_the_calibration_gap_the_defaults_rely_on_still_holds(self) -> None:
        """MMR_DEFAULT_LAMBDA and REDUNDANCY_EXPONENT were tuned against this
        separation: near-duplicates above 0.9, unrelated records below 0.3. If
        tokenization ever changes and closes that gap, the constants stop being
        justified — this test is what says so out loud."""
        profiles = [event_token_profile(e) for e in _DUPLICATE_FAMILY]
        duplicate_pairs = [(0, 1), (0, 2), (1, 2)]
        unrelated_pairs = [(0, 3), (0, 4), (1, 3), (2, 4), (3, 4)]
        for i, j in duplicate_pairs:
            assert lexical_similarity(profiles[i], profiles[j]) > 0.9
        for i, j in unrelated_pairs:
            assert lexical_similarity(profiles[i], profiles[j]) < 0.3

    def test_compound_and_observe_payloads_are_read(self) -> None:
        """The collector is shape-agnostic — nested parts count as text."""
        compound = Event(
            id="c",
            content_hash="sha256:c",
            created_at="2026-08-03T10:00:00+00:00",
            origin="agent",
            kind="remember",
            payload={
                "content_type": "compound",
                "parts": [{"type": "text", "text": "telegram router reliability"}],
            },
            parent_hashes=None,
            schema_version=1,
        )
        assert "telegram" in event_token_profile(compound)


class TestCandidateCeiling:
    def test_tail_beyond_the_ceiling_passes_through_in_order(self) -> None:
        """MMR is O(n^2); past the ceiling the tail is appended, never dropped."""
        many = [_event(f"e{i}", f"distinct memory number {i} about topic {i}") for i in range(220)]
        out = mmr_rerank(many, limit=len(many))
        assert len(out) == len(many)
        assert {e.id for e in out} == {e.id for e in many}
        tail_ids = [e.id for e in out[MMR_MAX_CANDIDATES:]]
        assert tail_ids == [e.id for e in many[MMR_MAX_CANDIDATES:]]
