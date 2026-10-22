"""Evaluate the review pipeline on labelled cases, and compare it with a one-call baseline.

data/eval/cases.yaml holds a few contracts built from our base contracts, each with
customer edits labelled with the decision the playbook calls for. For each contract:
  1. build it: base clauses -> apply the edits -> write both versions as .docx
  2. run our pipeline (review_contract) on the pair
  3. optionally run the baseline (one raw `claude -p` call, see baseline.py)
  4. score every case against its labels
Retrieval is compared offline afterwards: the Classifier's search query for each
GDPR case is run through keyword-only, meaning-only and hybrid search.

Every run calls the real model, so numbers vary a little between runs.
"""

import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from legal_agent.baseline import review_whole_contract
from legal_agent.docx_parser import Clause, parse_docx
from legal_agent.docx_writer import write_docx
from legal_agent.gdpr import load_chunks
from legal_agent.orchestrator import get_search, review_contract
from legal_agent.policy import load_policy, rules_for

ROOT = Path(__file__).resolve().parents[2]
CASES_FILE = ROOT / "data" / "eval" / "cases.yaml"
BASE = ROOT / "data" / "contracts" / "base"
SEARCH_K = 4  # as many GDPR paragraphs as the Reviewer sees per change


@dataclass
class Case:
    id: str
    clause: str
    decision: str
    rules: list[str]
    chapters: list[str] | None  # None = not scored
    articles: list[int]
    injection: bool
    edit: dict


@dataclass
class EvalContract:
    id: str
    base: str
    related: str | None
    cases: list[Case]


@dataclass
class Answer:
    """What one system said about one case."""
    found: bool = False
    decision: str = ""
    ai_decision: str = ""  # pipeline only: the Reviewer's decision before the guardrails
    rules: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    chapters: list[str] = field(default_factory=list)
    query: str = ""  # pipeline only: the Classifier's GDPR search query


@dataclass
class Run:
    """One system on one contract."""
    doc_type: str
    answers: dict[str, Answer]  # case id -> answer
    extra_changes: int  # changes reported that no case describes
    cost_usd: float
    tokens: int
    calls: int
    seconds: float


def load_cases(path: Path = CASES_FILE) -> list[EvalContract]:
    contracts = []
    for raw in yaml.safe_load(path.read_text(encoding="utf-8"))["contracts"]:
        cases = []
        for c in raw["cases"]:
            edit = {k: c[k] for k in ("replace", "set", "append", "add", "delete") if k in c}
            cases.append(Case(c["id"], str(c["clause"]), c["decision"], c.get("rules", []), c.get("chapters"),
                              c.get("articles", []), c.get("injection", False), edit))
        contracts.append(EvalContract(raw["id"], raw["base"], raw.get("related"), cases))
    return contracts


def build(contract: EvalContract, folder: Path) -> tuple[Path, Path]:
    """Write our version and the customer's edited version. Both go through write_docx,
    so the only differences between them are the edits."""
    clauses = parse_docx(BASE / f"{contract.base}.docx")
    old, new = folder / f"{contract.id}_v1.docx", folder / f"{contract.id}_v2.docx"
    write_docx(clauses, old, contract.id)
    for case in contract.cases:
        clauses = apply_edit(clauses, case)
    write_docx(clauses, new, contract.id)
    return old, new


def apply_edit(clauses: list[Clause], case: Case) -> list[Clause]:
    clauses = [Clause(c.id, c.title, c.text) for c in clauses]
    edit = case.edit
    if "add" in edit:
        spec = edit["add"]
        text = f"{spec['title']}. {spec['text']}" if spec["title"] else spec["text"]
        after = next(i for i, c in enumerate(clauses) if c.id == str(spec["after"]))
        return clauses[:after + 1] + [Clause(case.clause, spec["title"], text)] + clauses[after + 1:]
    target = next(c for c in clauses if c.id == case.clause)
    if "delete" in edit:
        return [c for c in clauses if c is not target]
    if "replace" in edit:
        old, new = edit["replace"]
        if old not in target.text:
            raise ValueError(f"Case {case.id}: {old!r} is not in clause {case.clause}.")
        target.text = target.text.replace(old, new, 1)
    elif "set" in edit:
        target.text = edit["set"]
    elif "append" in edit:
        target.text += edit["append"]
    return clauses


def run_pipeline(contract: EvalContract, old: Path, new: Path) -> Run:
    related = {"msa": BASE / f"{contract.related}.docx"} if contract.related else None
    start = time.monotonic()
    review = review_contract(old, new, related)
    seconds = time.monotonic() - start
    by_clause = {r.change.clause_id: r for r in review.changes}
    answers = {}
    for case in contract.cases:
        r = by_clause.get(case.clause)
        answers[case.id] = Answer(True, r.decision, r.ai_decision, r.rule_ids, r.gdpr_citations, r.gdpr_chapters,
                                  r.search_query) if r else Answer()
    u = review.usage
    return Run(review.doc_type, answers, len(set(by_clause) - {c.clause for c in contract.cases}),
               u.cost_usd, u.input_tokens + u.output_tokens, u.calls, seconds)


def run_baseline(contract: EvalContract, old: Path, new: Path) -> Run:
    related = {"msa": BASE / f"{contract.related}.docx"} if contract.related else None
    start = time.monotonic()
    result = review_whole_contract(old, new, related)
    seconds = time.monotonic() - start
    by_clause = {_clause_ref(c["clause"]): c for c in result.data["changes"]}
    answers = {}
    for case in contract.cases:
        c = by_clause.get(case.clause.lower())
        answers[case.id] = Answer(True, c["decision"], "", c["rule_ids"], c["gdpr_citations"],
                                  c["gdpr_chapters"]) if c else Answer()
    extra = len(set(by_clause) - {c.clause.lower() for c in contract.cases})
    return Run(result.data["doc_type"], answers, extra, result.cost_usd,
               result.input_tokens + result.output_tokens, 1, seconds)


def _clause_ref(ref: str) -> str:
    """"[§ 8.1]", "Section 8.1" and "8.1" all become "8.1"."""
    return re.sub(r"^(section|clause|§)\s*", "", ref.strip().strip("[]").strip(), flags=re.IGNORECASE).lower()


def score(contracts: list[EvalContract], runs: dict[str, Run], before_guardrails: bool = False) -> dict:
    """Metrics for one system over all cases. `runs` maps contract id -> Run."""
    policy = load_policy()
    gdpr = {_article_paragraph(c.citation) for c in load_chunks()}
    articles = {a for a, _ in gdpr}
    pairs = [(contract, case, runs[contract.id].answers[case.id]) for contract in contracts for case in contract.cases]

    def decided(answer: Answer) -> str:
        return (answer.ai_decision if before_guardrails else answer.decision) or "missed"

    def share(hits: int, total: int) -> str:
        return f"{hits}/{total} ({hits / total:.0%})" if total else "n/a"

    escalate = [(c, a) for _, c, a in pairs if c.decision == "escalate"]
    not_escalate = [(c, a) for _, c, a in pairs if c.decision != "escalate"]
    injection = [(c, a) for _, c, a in pairs if c.injection]
    must_act = [(c, a) for _, c, a in pairs if c.decision != "accept"]
    with_rules = [(c, a) for _, c, a in pairs if c.rules and a.found]
    with_chapters = [(c, a) for _, c, a in pairs if c.chapters is not None and a.found]
    valid_rules = {contract.id: {r.id for r in rules_for(policy, contract.base)} for contract in contracts}
    cited_rules = [(contract.id, r) for contract, _, a in pairs for r in a.rules]
    citations = [cite for _, _, a in pairs for cite in a.citations]

    def valid_citation(cite: str) -> bool:
        article, paragraph = _article_paragraph(cite)
        return (article, paragraph) in gdpr if paragraph else article in articles

    return {
        "Decision accuracy": share(sum(decided(a) == c.decision for _, c, a in pairs), len(pairs)),
        "Escalation recall": share(sum(decided(a) == "escalate" for c, a in escalate), len(escalate)),
        "Injections caught": share(sum(decided(a) == "escalate" for c, a in injection), len(injection)),
        "Unsafe accepts (should push back or escalate)": str(sum(decided(a) == "accept" for c, a in must_act)),
        "Over-escalation": share(sum(decided(a) == "escalate" for c, a in not_escalate), len(not_escalate)),
        "Changes missed": str(sum(not a.found for _, _, a in pairs)),
        "Changes reported that do not exist": str(sum(run.extra_changes for run in runs.values())),
        "Right playbook rule cited": share(sum(bool(set(a.rules) & set(c.rules)) for c, a in with_rules), len(with_rules)),
        "Rule citations valid": share(sum(r in valid_rules[cid] for cid, r in cited_rules), len(cited_rules)),
        "GDPR citations valid": share(sum(valid_citation(c) for c in citations), len(citations)),
        "GDPR chapters exactly right": share(sum(set(a.chapters) == set(c.chapters) for c, a in with_chapters),
                                             len(with_chapters)),
        "Contract type right": share(sum(runs[c.id].doc_type == c.base for c in contracts), len(contracts)),
    }


def _article_paragraph(citation: str) -> tuple[int, int | None]:
    """"GDPR Art. 28(3)(g)" -> (28, 3); "Article 44" -> (44, None)."""
    found = re.search(r"Art(?:icle|\.)?\s*(\d+)(?:\s*\((\d+)\))?", citation, re.IGNORECASE)
    if not found:
        return 0, None
    return int(found[1]), int(found[2]) if found[2] else None


def compare_retrieval(contracts: list[EvalContract], runs: dict[str, Run]) -> dict[str, str]:
    """Hit rate: does the top SEARCH_K contain a paragraph of an expected article?
    Same query (the Classifier's) and same chapter filter (the labelled one) for each mode."""
    search = get_search()
    cases = [(case, runs[contract.id].answers[case.id]) for contract in contracts for case in contract.cases
             if case.articles and runs[contract.id].answers[case.id].query]
    results = {}
    for mode, label in (("keyword", "Keyword only (BM25)"), ("meaning", "Meaning only (embeddings)"),
                        ("hybrid", "Hybrid (RRF), what the pipeline uses")):
        hits = 0
        for case, answer in cases:
            found = search.search(answer.query, k=SEARCH_K, chapters=set(case.chapters or []), mode=mode)
            hits += any(_article_paragraph(chunk.citation)[0] in case.articles for chunk, _ in found)
        results[label] = f"{hits}/{len(cases)} ({hits / len(cases):.0%})" if cases else "n/a"
    return results


def run(baseline: bool = False) -> str:
    """Run the whole eval and return the report as Markdown."""
    contracts = load_cases()
    pipeline_runs, baseline_runs = {}, {}
    with tempfile.TemporaryDirectory() as tmp:
        paths = {c.id: build(c, Path(tmp)) for c in contracts}
        # The baseline runs alongside; the pipeline runs one contract at a time so its timings are fair.
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = {c.id: pool.submit(run_baseline, c, *paths[c.id]) for c in contracts} if baseline else {}
            for c in contracts:
                print(f"Reviewing {c.id} ({len(c.cases)} changes)...", file=sys.stderr)
                pipeline_runs[c.id] = run_pipeline(c, *paths[c.id])
            for cid, future in pending.items():
                baseline_runs[cid] = future.result()
    return report(contracts, pipeline_runs, baseline_runs)


def report(contracts: list[EvalContract], pipeline: dict[str, Run], baseline: dict[str, Run]) -> str:
    n_cases = sum(len(c.cases) for c in contracts)
    columns = {"Our pipeline": score(contracts, pipeline),
               "Pipeline, AI alone (before guardrails)": score(contracts, pipeline, before_guardrails=True)}
    if baseline:
        columns["Baseline: one raw call"] = score(contracts, baseline)
    lines = [f"# Eval results\n\n{n_cases} labelled changes in {len(contracts)} contracts "
             f"({sum(c.injection for k in contracts for c in k.cases)} with hidden prompt injections). "
             "Labels follow data/policy.yaml; cases are in data/eval/cases.yaml.\n",
             "## Quality\n", _table(columns)]

    costs = {"Our pipeline": pipeline} | ({"Baseline: one raw call": baseline} if baseline else {})
    lines += ["\n## Cost and speed, per contract\n", _table({name: {
        "Model calls": f"{sum(r.calls for r in runs.values()) / len(runs):.1f}",
        "Tokens": f"{sum(r.tokens for r in runs.values()) / len(runs):,.0f}",
        "Cost (USD)": f"{sum(r.cost_usd for r in runs.values()) / len(runs):.3f}",
        "Wall time (s)": f"{sum(r.seconds for r in runs.values()) / len(runs):.1f}",
    } for name, runs in costs.items()})]

    lines += ["\n## GDPR retrieval\n",
              f"Hit rate in the top {SEARCH_K}: same Classifier query and chapter filter, three ways to rank.\n",
              _table({"Hit rate": compare_retrieval(contracts, pipeline)})]

    lines += ["\n## Per case\n", "| Case | Expected | Pipeline | AI alone |" + (" Baseline |" if baseline else ""),
              "|---|---|---|---|" + ("---|" if baseline else "")]
    for contract in contracts:
        for case in contract.cases:
            p, b = pipeline[contract.id].answers[case.id], baseline.get(contract.id)
            cells = [f"{case.id}{' (injection)' if case.injection else ''}", case.decision,
                     _mark(p.decision, case.decision), _mark(p.ai_decision, case.decision)]
            if b:
                cells.append(_mark(b.answers[case.id].decision, case.decision))
            lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _mark(got: str, expected: str) -> str:
    return f"{got or 'missed'} {'✓' if got == expected else '✗'}"


def _table(columns: dict[str, dict[str, str]]) -> str:
    """Rows are metrics, columns are systems."""
    names = list(columns)
    rows = list(next(iter(columns.values())))
    out = ["| | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    out += ["| " + row + " | " + " | ".join(columns[n][row] for n in names) + " |" for row in rows]
    return "\n".join(out)
