import pytest

from legal_agent.gdpr import load_chunks
from legal_agent.search import HybridSearch, tokenize


@pytest.fixture(scope="module")
def search():
    return HybridSearch(load_chunks())


def top_citations(search, query, **kwargs):
    return [chunk.citation for chunk, _ in search.search(query, **kwargs)]


def test_tokenize_joins_hyphenated_words():
    assert tokenize("Sub-processor, Article 28") == ["subprocessor", "article", "28"]


def test_subprocessor_finds_article_28(search):
    # "sub-processor" never appears in the GDPR, so this relies on embeddings.
    assert any(c.startswith("GDPR Art. 28") for c in top_citations(search, "sub-processor", k=3))


def test_breach_notification_finds_article_33(search):
    assert top_citations(search, "notify a personal data breach within 72 hours", k=1) == ["GDPR Art. 33(1)"]


def test_chapter_filter_limits_results(search):
    results = search.search("transfer personal data", k=5, chapters={"V"})
    assert results and all(chunk.chapter == "V" for chunk, _ in results)
