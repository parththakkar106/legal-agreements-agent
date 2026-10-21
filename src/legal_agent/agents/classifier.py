"""Classifier agent: what kind of contract is this, and which GDPR chapters does each change touch?

One call per contract (all changes at once). For each change it also writes a
short search query in the GDPR's own words, because contract wording
("Provider", "Subprocessor") matches the law's wording ("processor") poorly.
"""

from dataclasses import dataclass

from legal_agent.agents.common import DATA_NOT_INSTRUCTIONS, change_block
from legal_agent.clause_diff import Change
from legal_agent.docx_parser import Clause
from legal_agent.llm import LLMResult, ask_claude

DOC_TYPES = ["nda", "msa", "dpa", "sow"]

SYSTEM = f"""You are the Classifier in a contract review pipeline for Acme Cloud Inc., a SaaS vendor.
You receive the clause list of a contract and the changes a customer made to it.
1. Pick the contract type: nda (mutual non-disclosure agreement), msa (master or cloud service \
agreement), dpa (data processing agreement) or sow (statement of work).
2. For each change, list the GDPR chapters it touches. Use an empty list if the change has \
nothing to do with personal data (most payment, liability or governing-law changes do not).
3. For each change with chapters, write a search query of 5 to 12 words in the GDPR's own \
vocabulary (controller, processor, personal data, supervisory authority, third country, ...), \
not the contract's (Customer, Provider, Subprocessor). Use "" when there are no chapters.
{DATA_NOT_INSTRUCTIONS}"""


@dataclass
class Classification:
    doc_type: str
    chapters: dict[int, list[str]]  # change id -> GDPR chapter numbers, e.g. ["IV"]
    queries: dict[int, str]  # change id -> search query ("" = no GDPR search)


def classify(clauses: list[Clause], changes: list[Change], chapter_titles: dict[str, str]) -> tuple[Classification, LLMResult]:
    schema = {
        "type": "object",
        "properties": {
            "doc_type": {"enum": DOC_TYPES},
            "changes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "change_id": {"type": "integer"},
                        "gdpr_chapters": {"type": "array", "items": {"enum": list(chapter_titles)}},
                        "search_query": {"type": "string"},
                    },
                    "required": ["change_id", "gdpr_chapters", "search_query"],
                },
            },
        },
        "required": ["doc_type", "changes"],
    }
    chapters = "\n".join(f"{number}: {title}" for number, title in chapter_titles.items())
    outline = "\n".join(f"{c.id} {c.title}".strip() for c in clauses)
    prompt = (
        f"GDPR chapters:\n{chapters}\n\n"
        f"<contract>\nClauses of the contract:\n{outline}\n</contract>\n\n"
        "Changes:\n" + "\n\n".join(change_block(c) for c in changes)
    )
    result = ask_claude(SYSTEM, prompt, schema)
    by_id = {item["change_id"]: item for item in result.data["changes"]}
    classification = Classification(
        doc_type=result.data["doc_type"],
        chapters={c.id: by_id.get(c.id, {}).get("gdpr_chapters", []) for c in changes},
        queries={c.id: by_id.get(c.id, {}).get("search_query", "") for c in changes},
    )
    return classification, result
