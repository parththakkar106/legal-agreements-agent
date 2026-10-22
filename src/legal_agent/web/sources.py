"""The text behind each source a review cites, so the page can show it in place.

A source is a GDPR article, a playbook rule, or a clause in one of the contracts.
The page opens it in a side panel with the exact paragraph highlighted.
"""

from functools import cache

from legal_agent.docx_parser import parse_docx
from legal_agent.gdpr import load_chunks
from legal_agent.policy import load_policy

DECISION_LABELS = {"accept": "We accept", "push_back": "We push back on", "escalate": "We escalate"}


@cache
def _gdpr_articles() -> dict[str, list]:
    """{"GDPR Art. 28": [chunk for 28(1), 28(2), ...]}"""
    articles = {}
    for chunk in load_chunks():
        articles.setdefault(chunk.citation.split("(")[0], []).append(chunk)
    return articles


def gdpr_source(citation: str) -> dict | None:
    article = citation.split("(")[0]
    chunks = _gdpr_articles().get(article)
    if not chunks:
        return None
    return {"kind": "GDPR", "title": f"{article.removeprefix('GDPR ')} {chunks[0].article_title}",
            "subtitle": f"Chapter {chunks[0].chapter}: {chunks[0].chapter_title}",
            "parts": [{"label": c.citation.removeprefix("GDPR Art. "), "text": c.text,
                       "hit": citation in (c.citation, article)} for c in chunks]}


def rule_source(rule_id: str, decision: str) -> dict | None:
    """The playbook rule, with the line matching the decision highlighted."""
    rule = next((r for r in load_policy() if r.id == rule_id), None)
    if rule is None:
        return None
    parts = [{"label": "Our standard", "text": rule.standard, "hit": False}]
    parts += [{"label": label, "text": getattr(rule, key), "hit": key == decision}
              for key, label in DECISION_LABELS.items() if getattr(rule, key)]
    return {"kind": "Acme playbook", "title": rule.topic, "subtitle": f"Rule \"{rule.id}\" in data/policy.yaml",
            "parts": parts}


def document(path: str, name: str) -> dict:
    """A whole contract, clause by clause, for the side panel and the document page."""
    return {"kind": "Contract", "title": name, "subtitle": "",
            "parts": [_clause_part(c) for c in parse_docx(path)]}


def _clause_part(clause) -> dict:
    """Numbered clauses get their number as a label; Key Terms ("Fees") get their name in bold."""
    if clause.id == clause.title:
        return {"id": clause.id, "label": "", "term": clause.title, "text": clause.text}
    return {"id": clause.id, "label": clause.id, "term": "", "text": clause.text}


def round_sources(old: dict, new: dict, related_file: str | None, result: dict) -> dict:
    """Everything one round needs to open its sources: {"docs": {...}, "sources": {...}}.

    `old` and `new` are the two versions compared. Contracts are sent once under "docs";
    a clause source just names the doc ("old", "new" or "msa") and the clause.
    """
    docs = {"old": document(old["file"], f"v{old['number']} · {old['name']}"),
            "new": document(new["file"], f"v{new['number']} · {new['name']}")}
    if related_file:
        docs["msa"] = document(related_file, "Cloud Service Agreement (signed, in force)")
    sources = {}
    for r in result["changes"]:
        change = r["change"]
        version = "old" if change["kind"] == "removed" else "new"
        sources[f"{version}:{change['clause_id']}"] = {"doc": version, "hit": change["clause_id"]}
        for ref in r["context_used"]:
            doc, _, clause = ref.rpartition(":")
            sources[f"{doc or 'new'}:{clause}"] = {"doc": doc or "new", "hit": clause}
        for citation in r["gdpr_citations"]:
            sources[citation] = gdpr_source(citation)
        for rule_id in r["rule_ids"]:
            sources[f"rule:{rule_id}:{r['decision']}"] = rule_source(rule_id, r["decision"])
    return {"docs": docs, "sources": {key: value for key, value in sources.items() if value}}
