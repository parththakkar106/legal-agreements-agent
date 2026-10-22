"""Command line entry point.

  python src/main.py diff OLD.docx NEW.docx        list the clause changes between two versions
  python src/main.py search "QUERY" [--chapter IV]  search the GDPR
  python src/main.py review OLD.docx NEW.docx      full AI review (needs `claude -p` and CLAUDE_KEY)
  python src/main.py eval [--baseline]              score the pipeline on labelled cases (needs `claude -p`)
  python src/main.py serve                         the web app at http://127.0.0.1:8000
"""

import argparse
import json
import sys
from dataclasses import asdict

from legal_agent.clause_diff import diff_clauses
from legal_agent.docx_parser import parse_docx


def cmd_diff(args):
    changes = diff_clauses(parse_docx(args.old), parse_docx(args.new))
    print(f"{len(changes)} change(s)\n")
    for change in changes:
        title = f" {change.title}" if change.title and change.title != change.clause_id else ""
        print(f"#{change.id} {change.kind.upper()}: {change.clause_id}{title}")
        if change.kind == "modified":
            for op, words in change.word_diff:
                if op != "equal":
                    print(f"   {'-' if op == 'delete' else '+'} {words}")
        else:
            print(f"   {'+' if change.kind == 'added' else '-'} {change.new_text or change.old_text}")
        print()


def cmd_search(args):
    # Imported here so `diff` does not pay for loading the embedding model.
    from legal_agent.gdpr import load_chunks
    from legal_agent.search import HybridSearch

    results = HybridSearch(load_chunks()).search(args.query, k=args.k, chapters=set(args.chapter) or None)
    for chunk, score in results:
        print(f"{chunk.citation} ({chunk.article_title}), chapter {chunk.chapter}, score {score:.4f}")
        print(f"   {chunk.text[:200]}...\n")


def cmd_review(args):
    from legal_agent.llm import LLMUnavailable
    from legal_agent.orchestrator import review_contract

    related = dict(item.split("=", 1) for item in args.related)
    try:
        review = review_contract(args.old, args.new, related)
    except LLMUnavailable as error:
        sys.exit(f"Error: {error}")

    print(f"Contract type: {review.doc_type}    Verdict: {review.verdict.upper()}\n")
    for r in review.changes:
        title = f" {r.change.title}" if r.change.title and r.change.title != r.change.clause_id else ""
        overridden = f" (AI said {r.ai_decision})" if r.ai_decision != r.decision else ""
        print(f"#{r.change.id} {r.change.kind} {r.change.clause_id}{title}: {r.decision.upper()}{overridden}")
        print(f"   Meaning: {r.meaning}")
        print(f"   Impact:  {r.impact}")
        sources = r.rule_ids + r.gdpr_citations + [f"clause {ref}" for ref in r.context_used]
        print(f"   Sources: {', '.join(sources) or 'none'}")
        if r.counter_proposal:
            print(f"   Reply:   {r.counter_proposal}")
        for note in r.guardrail_notes:
            print(f"   Guardrail: {note}")
        print()
    for group in review.groups:
        print(f"Group \"{group['theme']}\" (changes {', '.join(map(str, group['change_ids']))}): {group['summary']}")
    u = review.usage
    print(f"\n{u.calls} AI calls, {u.input_tokens:,} input + {u.output_tokens:,} output tokens, "
          f"${u.cost_usd:.3f}, {u.llm_seconds:.0f}s of model time")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(asdict(review), f, indent=2)


def cmd_eval(args):
    from pathlib import Path

    from legal_agent import evaluation
    from legal_agent.llm import LLMUnavailable

    try:
        report = evaluation.run(baseline=args.baseline)
    except LLMUnavailable as error:
        sys.exit(f"Error: {error}")
    print(report)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")


def cmd_serve(args):
    import uvicorn

    from legal_agent.web import portfolio

    if portfolio.load_if_empty():
        print("Loaded the contract portfolio from data/portfolio/ into data/app/.")
    uvicorn.run("legal_agent.web.app:app", host=args.host, port=args.port)


def main():
    parser = argparse.ArgumentParser(description="Legal agreements agent")
    commands = parser.add_subparsers(dest="command", required=True)

    diff = commands.add_parser("diff", help="list clause changes between two .docx versions")
    diff.add_argument("old")
    diff.add_argument("new")
    diff.set_defaults(func=cmd_diff)

    search = commands.add_parser("search", help="hybrid search over the GDPR")
    search.add_argument("query")
    search.add_argument("--chapter", action="append", default=[], help="limit to a GDPR chapter, e.g. IV (repeatable)")
    search.add_argument("-k", type=int, default=5, help="number of results")
    search.set_defaults(func=cmd_search)

    review = commands.add_parser("review", help="AI review of a redline (needs claude -p)")
    review.add_argument("old")
    review.add_argument("new")
    review.add_argument("--related", action="append", default=[], metavar="NAME=PATH",
                        help="another contract the reviewer may consult, e.g. msa=data/contracts/base/msa.docx")
    review.add_argument("--json", help="also write the full result to this JSON file")
    review.set_defaults(func=cmd_review)

    evaluate = commands.add_parser("eval", help="score the pipeline on data/eval/cases.yaml (needs claude -p)")
    evaluate.add_argument("--baseline", action="store_true", help="also run one raw claude -p call per contract")
    evaluate.add_argument("--out", help="also write the report to this Markdown file")
    evaluate.set_defaults(func=cmd_eval)

    serve = commands.add_parser("serve", help="run the web app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
