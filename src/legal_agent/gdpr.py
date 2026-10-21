"""Split the GDPR into searchable chunks, one per article paragraph.

Each chunk carries an exact citation ("GDPR Art. 28(3)") and its chapter,
so search can be narrowed to chapters and the Reviewer can cite sources.
Recitals (the explanatory preamble) are left out: they are not binding rules.
"""

import json
from dataclasses import dataclass
from pathlib import Path

GDPR_FILE = Path(__file__).resolve().parents[2] / "data" / "gdpr" / "gdpr.json"


@dataclass
class Chunk:
    citation: str  # "GDPR Art. 28(3)"
    chapter: str  # "IV"
    chapter_title: str  # "Controller and processor"
    article_title: str  # "Processor"
    text: str  # the paragraph, with its lettered points


def load_chunks(path: Path = GDPR_FILE) -> list[Chunk]:
    gdpr = json.loads(path.read_text(encoding="utf-8"))
    chunks = []
    for chapter in gdpr["chapters"]:
        for article in _articles(chapter):
            chunks.extend(_article_chunks(chapter, article))
    return chunks


def chapter_titles(path: Path = GDPR_FILE) -> dict[str, str]:
    """{"I": "General provisions", ..., "XI": "Final provisions"}"""
    gdpr = json.loads(path.read_text(encoding="utf-8"))
    return {chapter["number"]: chapter["title"] for chapter in gdpr["chapters"]}


def _articles(node: dict) -> list[dict]:
    """Articles sit directly in a chapter or inside its sections."""
    found = []
    for item in node["contents"]:
        if item["type"] == "article":
            found.append(item)
        else:
            found.extend(_articles(item))
    return found


def _article_chunks(chapter: dict, article: dict) -> list[Chunk]:
    number = article["number"]

    def chunk(label: str, text: str) -> Chunk:
        return Chunk(f"GDPR Art. {number}{label}", chapter["number"], chapter["title"], article["title"], text)

    # Numbered paragraphs become chunks: "Art. 28(3)". Unnumbered text joins the
    # paragraph above it (a follow-on subparagraph) or, if it comes first, the one
    # below it (an intro such as Art. 4's "For the purposes of this Regulation:").
    chunks, intro = [], ""
    for paragraph in article["contents"]:
        points = [f"({p['number']}) {p['text']}" for p in paragraph["subpoints"]]
        text = "\n".join([paragraph["text"], *points])
        if paragraph["number"]:
            chunks.append(chunk(f"({paragraph['number']})", (intro + "\n" + text).strip()))
            intro = ""
        elif chunks:
            chunks[-1].text += "\n" + text
        else:
            intro = (intro + "\n" + text).strip()
    if intro:
        chunks.append(chunk("", intro))  # an article with no numbered paragraphs: "Art. 67"
    return chunks
