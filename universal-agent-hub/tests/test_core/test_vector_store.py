"""تست جست‌وجوی هیبریدی برداری."""

from __future__ import annotations

import pytest

from src.core.vector_store import VectorIndex, cosine, hash_ngrams, normalize_text, reciprocal_rank_fusion


def test_hash_ngrams_is_deterministic() -> None:
    assert hash_ngrams("hello world") == hash_ngrams("hello world")


def test_hash_ngrams_empty() -> None:
    assert hash_ngrams("") == {}
    assert hash_ngrams("   ") == {}


def test_normalize_handles_persian_variants() -> None:
    assert normalize_text("كِتاب") == normalize_text("کتاب")
    assert normalize_text("ABC") == "abc"


def test_cosine_identical_is_one() -> None:
    vec = hash_ngrams("database backup procedure")
    assert cosine(vec, vec) == pytest.approx(1.0, abs=1e-9)


def test_cosine_empty_is_zero() -> None:
    assert cosine({}, hash_ngrams("x")) == 0.0
    assert cosine(hash_ngrams("x"), {}) == 0.0


def test_rrf_prefers_consensus() -> None:
    fused = reciprocal_rank_fusion([["a", "b", "c"], ["b", "a", "d"]])
    order = [item for item, _ in fused]
    assert order[0] in {"a", "b"}
    assert order[-1] == "d"


def test_rrf_empty() -> None:
    assert reciprocal_rank_fusion([]) == []


def test_rrf_rejects_bad_weights() -> None:
    with pytest.raises(ValueError, match="weights length"):
        reciprocal_rank_fusion([["a"]], weights=[1.0, 2.0])


def test_rrf_rejects_bad_k() -> None:
    with pytest.raises(ValueError, match="k must be positive"):
        reciprocal_rank_fusion([["a"]], k=0)


def test_rrf_zero_weight_ignored() -> None:
    fused = reciprocal_rank_fusion([["a"], ["b"]], weights=[1.0, 0.0])
    assert [item for item, _ in fused] == ["a"]


class TestVectorIndex:
    """ایندکس برداری."""

    def build(self) -> VectorIndex:
        index = VectorIndex()
        index.add("m1", "user prefers concise Persian answers")
        index.add("m2", "database credentials live in .env")
        index.add("m3", "the nightly backup procedure runs at 03:00")
        return index

    def test_add_and_len(self) -> None:
        index = self.build()
        assert len(index) == 3
        assert "m1" in index

    def test_add_many(self) -> None:
        index = VectorIndex()
        assert index.add_many([("a", "one"), ("b", "two")]) == 2

    def test_add_replaces(self) -> None:
        index = self.build()
        index.add("m1", "completely different content")
        assert len(index) == 3

    def test_search_ranks_relevant_first(self) -> None:
        index = self.build()
        results = index.search("nightly backup schedule", limit=3)
        assert results[0][0] == "m3"

    def test_rank_returns_ids(self) -> None:
        index = self.build()
        assert index.rank("database credentials")[0] == "m2"

    def test_rank_limit(self) -> None:
        index = self.build()
        assert len(index.rank("backup", limit=1)) == 1

    def test_empty_query(self) -> None:
        assert self.build().rank("") == []
        assert self.build().scores("") == []

    def test_empty_index(self) -> None:
        index = VectorIndex()
        assert index.rank("anything") == []
        assert index.scores("anything") == []

    def test_remove(self) -> None:
        index = self.build()
        assert index.remove("m1") is True
        assert index.remove("m1") is False
        assert len(index) == 2

    def test_clear(self) -> None:
        index = self.build()
        index.clear()
        assert len(index) == 0

    def test_hybrid_merges_both_channels(self) -> None:
        index = self.build()
        # کلیدواژه m1 را اول می‌داند، بردار m3 را؛ RRF باید هر دو را بیاورد.
        fused = index.hybrid("nightly backup", ["m1", "m3"], limit=3)
        assert "m3" in fused
        assert "m1" in fused

    def test_hybrid_with_empty_keyword_channel(self) -> None:
        index = self.build()
        fused = index.hybrid("database credentials", [], limit=3)
        assert fused[0] == "m2"

    def test_stats(self) -> None:
        stats = self.build().stats()
        assert stats["entries"] == 3
        assert stats["backend"].startswith("hashed-char-ngram")
        assert stats["avg_nonzero"] > 0

    def test_dim_is_clamped(self) -> None:
        assert VectorIndex(dim=1).dim >= 64

    def test_gram_bounds_are_ordered(self) -> None:
        index = VectorIndex(mingram=5, maxgram=2)
        assert index.maxgram >= index.mingram
