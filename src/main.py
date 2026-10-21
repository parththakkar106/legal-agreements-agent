"""Command line entry point.

  python src/main.py diff OLD.docx NEW.docx        list the clause changes between two versions
  python src/main.py search "QUERY" [--chapter IV]  search the GDPR
"""

import argparse

from legal_agent.clause_diff import diff_clauses
from legal_agent.docx_parser import parse_docx


def cmd_diff(args):
    changes = diff_clauses(parse_docx(args.old), parse_docx(args.new))
    print(f"{len(changes)} change(s)\n")
    for change in changes:
        title = "" if change.title == change.clause_id else f" {change.title}"
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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
