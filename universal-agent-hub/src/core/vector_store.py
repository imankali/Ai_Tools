"""جست‌وجوی هیبریدی: کلیدواژه + بردار، با Reciprocal Rank Fusion.

چرا بدون وابستگی تازه؟
----------------------
IronClaw می‌گوید «Hybrid Search — full-text + vector using Reciprocal Rank
Fusion» و Agent-Zero سراغ FAISS می‌رود. اما این پروژه روی Termux اندروید هم
باید کار کند و ``pip install`` سنگین، تعهدی است که نباید بی‌دلیل به کاربر
تحمیل شود. پس بردارها را خودمان می‌سازیم:

* **hashed character n-gram** → بردار تنک. بدون مدل، بدون دانلود، قطعی
  (deterministic)، و برای حافظه‌ی ایجنت که رکوردهایش کوتاه است کاملاً کافی.
* **cosine** روی همان بردارها.
* **RRF** برای ترکیب رتبه‌ی کلیدواژه (که ``memory.py`` از قبل دارد) با رتبه‌ی
  برداری. RRF عمداً انتخاب شد چون فقط به *رتبه* نگاه می‌کند، نه به امتیاز خام؛
  پس مقیاس دو روش لازم نیست هم‌خوان باشد.

اگر روزی یک embedder واقعی لازم شد، کافی است :meth:`VectorIndex.vectorize`
جایگزین شود — بقیه‌ی ماژول دست‌نخورده می‌ماند.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Sequence
from typing import Any

__all__ = ["VectorIndex", "cosine", "hash_ngrams", "reciprocal_rank_fusion"]

#: اندازه‌ی n-gram کاراکتری. 3–5 تعادل خوبی برای متن کوتاه چندزبانه است.
_DEFAULT_MINGRAM = 3
_DEFAULT_MAXGRAM = 5

#: ابعاد فضای هش. 4096 برای چند هزار رکورد کوتاه، برخورد مؤثری ندارد.
_DEFAULT_DIM = 4096

_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u0600-\u06FF]+", re.IGNORECASE)


#: نگاشت حروف عربی → معادل فارسی. NFKC این‌ها را *یکسان نمی‌کند* (کدپینت‌های
#: جداگانه‌اند)، پس برای یک پروژه‌ی دوزبانه باید صریح انجام شود.
_ARABIC_TO_PERSIAN = str.maketrans(
    {
        "\u0643": "\u06a9",  # ك → ک
        "\u0649": "\u06cc",  # ى → ی
        "\u064a": "\u06cc",  # ي → ی
        "\u0623": "\u0627",  # أ → ا
        "\u0625": "\u0627",  # إ → ا
        "\u0622": "\u0627",  # آ → ا
        "\u0629": "\u0647",  # ة → ه
    }
)


def normalize_text(text: str) -> str:
    """یکدست‌سازی متن برای ایندکس.

    سه کار می‌کند، چون هیچ‌کدام زیرِ دیگری نیست:

    1. ``NFKC`` + ``casefold`` — اعداد/حروف تمام‌عرض و بزرگی لاتین.
    2. حذف نویزهای ترکیبی (رده‌ی ``Mn``) — کسره/فتحه/تشدید. بدون این،
       «كِتاب» و «کتاب» دو رشته‌ی متفاوت می‌مانند.
    3. نگاشت حروف عربی → فارسی — ``NFKC`` عمداً این کار را *نمی‌کند*
       (U+0643 و U+06A9 کدپینت جداگانه‌اند)، پس صریح انجام می‌شود.
    """
    folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return unicodedata.normalize("NFC", stripped).translate(_ARABIC_TO_PERSIAN)


def hash_ngrams(
    text: str,
    *,
    dim: int = _DEFAULT_DIM,
    mingram: int = _DEFAULT_MINGRAM,
    maxgram: int = _DEFAULT_MAXGRAM,
) -> dict[int, float]:
    """متن → بردار تنک (dict از ایندکس → وزن).

    هم توکن‌های کلمه‌ای و هم n-gram کاراکتری وارد می‌شوند:
    * توکن‌ها دقت معنایی می‌دهند،
    * n-gram‌ها غلط املایی و تفاوت‌های صرفی را تحمل می‌کنند.

    Args:
        text: متن.
        dim: ابعاد فضای هش.
        mingram: کوچک‌ترین n.
        maxgram: بزرگ‌ترین n.

    Returns:
        بردار تنک نرمال‌نشده.
    """
    normalized = normalize_text(text)
    if not normalized.strip():
        return {}
    vector: dict[int, float] = {}

    def bump(index: int, weight: float) -> None:
        vector[index] = vector.get(index, 0.0) + weight

    for token in _TOKEN_RE.findall(normalized):
        # وزن توکن کامل بیشتر است تا کلمه‌های واقعی بر نویز n-gram غلبه کنند.
        bump(hash(("w", token)) % dim, 2.0)

    compact = re.sub(r"\s+", " ", normalized)
    low = max(1, min(mingram, maxgram))
    high = max(low, max(mingram, maxgram))
    for size in range(low, high + 1):
        if len(compact) < size:
            continue
        for start in range(len(compact) - size + 1):
            gram = compact[start : start + size]
            if gram.strip() == "":
                continue
            bump(hash(("g", size, gram)) % dim, 1.0)
    return vector


def cosine(left: dict[int, float], right: dict[int, float]) -> float:
    """شباهت کسینوسی دو بردار تنک؛ بین ۰ و ۱ (وزن‌ها منفی نیستند)."""
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    dot = 0.0
    for index, weight in left.items():
        other = right.get(index)
        if other:
            dot += weight * other
    if dot <= 0.0:
        return 0.0
    norm_left = math.sqrt(sum(w * w for w in left.values()))
    norm_right = math.sqrt(sum(w * w for w in right.values()))
    if norm_left == 0.0 or norm_right == 0.0:
        return 0.0
    return dot / (norm_left * norm_right)


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[Any]],
    *,
    k: int = 60,
    weights: Sequence[float] | None = None,
) -> list[tuple[Any, float]]:
    """ترکیب چند رتبه‌بندی با RRF.

    ``score(d) = Σ w_i / (k + rank_i(d))``

    Args:
        rankings: چند فهرست از شناسه‌ها، هر کدام به‌ترتیب بهترین اول.
        k: ثابت نرم‌سازی (۶۰ مقدار استاندارد مقاله‌ی اصلی RRF است).
        weights: وزن هر فهرست (پیش‌فرض همه یکسان).

    Returns:
        فهرست ``(شناسه, امتیاز)`` به‌ترتیب نزولی.
    """
    if not rankings:
        return []
    used_weights = list(weights) if weights is not None else [1.0] * len(rankings)
    if len(used_weights) != len(rankings):
        raise ValueError("weights length must match rankings length")
    if k <= 0:
        raise ValueError("k must be positive")
    scores: dict[Any, float] = {}
    for ranking, weight in zip(rankings, used_weights, strict=False):
        if weight == 0:
            continue
        for position, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + weight / (k + position + 1)
    return sorted(scores.items(), key=lambda kv: (-kv[1], str(kv[0])))


class VectorIndex:
    """ایندکس برداری سبک روی رکوردهای متنی.

    نمونه::

        index = VectorIndex()
        index.add("m1", "user prefers concise Persian answers")
        index.add("m2", "database credentials live in .env")
        index.search("how should I answer?", limit=3)
    """

    def __init__(self, *, dim: int = _DEFAULT_DIM, mingram: int = _DEFAULT_MINGRAM, maxgram: int = _DEFAULT_MAXGRAM):
        """Args:
        dim: ابعاد فضای هش.
        mingram: کوچک‌ترین n-gram.
        maxgram: بزرگ‌ترین n-gram.
        """
        self.dim = max(64, int(dim))
        self.mingram = max(1, int(mingram))
        self.maxgram = max(self.mingram, int(maxgram))
        self._vectors: dict[str, dict[int, float]] = {}
        self._texts: dict[str, str] = {}

    # ------------------------------------------------------------------ mutation
    def vectorize(self, text: str) -> dict[int, float]:
        """متن → بردار. نقطه‌ی جایگزینی برای یک embedder واقعی."""
        return hash_ngrams(text, dim=self.dim, mingram=self.mingram, maxgram=self.maxgram)

    def add(self, item_id: str, text: str) -> None:
        """یک رکورد را ایندکس می‌کند (اگر باشد، جایگزین می‌شود)."""
        key = str(item_id)
        self._vectors[key] = self.vectorize(text)
        self._texts[key] = text

    def add_many(self, items: Iterable[tuple[str, str]]) -> int:
        """افزودن گروهی.

        Returns:
            تعداد افزوده‌شده.
        """
        count = 0
        for item_id, text in items:
            self.add(item_id, text)
            count += 1
        return count

    def remove(self, item_id: str) -> bool:
        """حذف یک رکورد."""
        key = str(item_id)
        existed = self._vectors.pop(key, None) is not None
        self._texts.pop(key, None)
        return existed

    def clear(self) -> None:
        """خالی کردن ایندکس."""
        self._vectors.clear()
        self._texts.clear()

    def __len__(self) -> int:
        """تعداد رکوردهای ایندکس‌شده."""
        return len(self._vectors)

    def __contains__(self, item_id: object) -> bool:
        """آیا این شناسه ایندکس شده؟"""
        return str(item_id) in self._vectors

    # ------------------------------------------------------------------ querying
    def rank(self, query: str, *, limit: int | None = None) -> list[str]:
        """شناسه‌ها را فقط بر اساس شباهت برداری رتبه می‌کند."""
        target = self.vectorize(query)
        if not target or not self._vectors:
            return []
        scored = [(item_id, cosine(target, vector)) for item_id, vector in self._vectors.items() if vector]
        scored = [(item_id, value) for item_id, value in scored if value > 0.0]
        scored.sort(key=lambda kv: (-kv[1], kv[0]))
        ids = [item_id for item_id, _ in scored]
        return ids[:limit] if limit is not None and limit >= 0 else ids

    def scores(self, query: str, *, limit: int | None = None) -> list[tuple[str, float]]:
        """مثل :meth:`rank` ولی با امتیاز."""
        target = self.vectorize(query)
        if not target or not self._vectors:
            return []
        scored = [(item_id, cosine(target, vector)) for item_id, vector in self._vectors.items() if vector]
        scored = [(item_id, value) for item_id, value in scored if value > 0.0]
        scored.sort(key=lambda kv: (-kv[1], kv[0]))
        return scored[:limit] if limit is not None and limit >= 0 else scored

    def search(self, query: str, *, limit: int = 8) -> list[tuple[str, float]]:
        """همان :meth:`scores` با سقف پیش‌فرض."""
        return self.scores(query, limit=max(1, limit))

    def hybrid(
        self,
        query: str,
        keyword_ranking: Sequence[str],
        *,
        limit: int = 8,
        k: int = 60,
        keyword_weight: float = 1.0,
        vector_weight: float = 1.0,
    ) -> list[str]:
        """ترکیب رتبه‌ی کلیدواژه‌ی بیرونی با رتبه‌ی برداری از طریق RRF.

        Args:
            query: متن پرس‌وجو.
            keyword_ranking: شناسه‌ها به‌ترتیب امتیاز کلیدواژه (از ``memory.py``).
            limit: سقف نتیجه.
            k: ثابت RRF.
            keyword_weight: وزن کانال کلیدواژه.
            vector_weight: وزن کانال بردار.

        Returns:
            شناسه‌ها به‌ترتیب امتیاز ترکیبی.
        """
        vector_ranking = self.rank(query)
        fused = reciprocal_rank_fusion(
            [list(keyword_ranking), vector_ranking],
            k=k,
            weights=[keyword_weight, vector_weight],
        )
        return [item_id for item_id, _ in fused[: max(1, limit)]]

    def stats(self) -> dict[str, Any]:
        """خلاصه برای ``/api/diagnostics``."""
        sizes = [len(v) for v in self._vectors.values()]
        return {
            "entries": len(self._vectors),
            "dim": self.dim,
            "mingram": self.mingram,
            "maxgram": self.maxgram,
            "avg_nonzero": round(sum(sizes) / len(sizes), 2) if sizes else 0.0,
            "backend": "hashed-char-ngram+cosine+rrf",
        }
