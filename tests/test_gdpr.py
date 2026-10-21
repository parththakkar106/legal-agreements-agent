from legal_agent.gdpr import chapter_titles, load_chunks


def test_every_article_is_chunked_with_unique_citations():
    chunks = load_chunks()
    citations = [c.citation for c in chunks]
    articles = {cite.split("Art. ")[1].split("(")[0] for cite in citations}

    assert len(articles) == 99
    assert len(citations) == len(set(citations))


def test_paragraph_chunk_keeps_its_lettered_points():
    chunks = {c.citation: c for c in load_chunks()}
    art_28_3 = chunks["GDPR Art. 28(3)"]

    assert art_28_3.chapter == "IV"
    assert art_28_3.article_title == "Processor"
    assert "(a) processes the personal data only on documented instructions" in art_28_3.text


def test_definitions_are_split_per_term():
    chunks = {c.citation: c for c in load_chunks()}
    assert chunks["GDPR Art. 4(7)"].text.startswith("‘controller’ means")


def test_chapter_titles():
    titles = chapter_titles()
    assert len(titles) == 11
    assert titles["V"].startswith("Transfers of personal data to third countries")
