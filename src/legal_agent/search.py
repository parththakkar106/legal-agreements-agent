"""Hybrid search over GDPR chunks: keywords (BM25) + meaning (embeddings).

- BM25 finds exact terms such as "sub-processor" or "Article 28".
- Embeddings find text with the same meaning in other words, e.g.
  "customer information" ~ "personal data".
The two rankings are merged with Reciprocal Rank Fusion (RRF): a chunk scores
1 / (60 + rank) in each list and the scores are added. RRF needs no tuning and
does not care that BM25 and cosine scores are on different scales.

The embedding model is WordLlama (256 dimensions, ~16 MB). Its weights ship
inside the pip package, so nothing is downloaded at run time, and it embeds
all ~400 chunks in well under a second on a CPU. No vector database is needed:
the vectors are one NumPy array held in memory.
"""

import re
from pathlib import Path

import wordllama
from rank_bm25 import BM25Okapi
from wordllama import WordLlama

from legal_agent.gdpr import Chunk

RRF_K = 60  # the constant from the original RRF paper; dampens the weight of top ranks


def load_embedder() -> WordLlama:
    # Point the cache at the package folder, where the bundled weights and
    # tokenizer live, and forbid downloads so a missing file fails loudly.
    return WordLlama.load(cache_dir=Path(wordllama.__file__).parent, disable_download=True)


def tokenize(text: str) -> list[str]:
    """Lowercase words; "sub-processor" and "subprocessor" both become "subprocessor"."""
    return re.findall(r"\w+", text.lower().replace("-", ""))


class HybridSearch:
    def __init__(self, chunks: list[Chunk], embedder: WordLlama | None = None):
        self.chunks = chunks
        texts = [f"{c.article_title}. {c.text}" for c in chunks]  # the title adds useful words
        self.bm25 = BM25Okapi([tokenize(t) for t in texts])
        self.embedder = embedder or load_embedder()
        self.vectors = self.embedder.embed(texts, norm=True)  # shape (n_chunks, 256)

    def search(self, query: str, k: int = 5, chapters: set[str] | None = None,
               mode: str = "hybrid") -> list[tuple[Chunk, float]]:
        """Top-k chunks for the query, optionally only from the given GDPR chapters (e.g. {"IV", "V"}).

        `mode` "keyword" or "meaning" uses one ranking alone; the eval compares them with "hybrid".
        """
        allowed = [i for i, c in enumerate(self.chunks) if not chapters or c.chapter in chapters]
        keyword_scores = self.bm25.get_scores(tokenize(query))
        meaning_scores = self.vectors @ self.embedder.embed([query], norm=True)[0]  # cosine similarity

        # A chunk that shares no word with the query gets no keyword vote, otherwise
        # BM25's all-zero ties would hand out ranks at random (e.g. for "sub-processor",
        # a word the GDPR never uses).
        keyword_ranked = sorted((i for i in allowed if keyword_scores[i] > 0), key=lambda i: -keyword_scores[i])
        meaning_ranked = sorted(allowed, key=lambda i: -meaning_scores[i])

        fused = {i: 0.0 for i in allowed}
        rankings = {"hybrid": (keyword_ranked, meaning_ranked), "keyword": (keyword_ranked,),
                    "meaning": (meaning_ranked,)}[mode]
        for ranked in rankings:
            for rank, i in enumerate(ranked, start=1):
                fused[i] += 1 / (RRF_K + rank)

        best = sorted(fused, key=fused.get, reverse=True)[:k]
        return [(self.chunks[i], fused[i]) for i in best]
