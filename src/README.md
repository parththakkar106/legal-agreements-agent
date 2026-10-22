# Code map

| File | What it does |
| --- | --- |
| `main.py` | Command line entry point (`diff`, `search`, `review`, `serve`) |
| `legal_agent/docx_parser.py` | Reads a .docx and splits it into clauses (by typed clause numbers and Word headings) |
| `legal_agent/clause_diff.py` | Matches clauses between two versions and lists what was added, removed or modified, with a word-level diff |
| `legal_agent/gdpr.py` | Splits the GDPR into one chunk per article paragraph, each with a citation like "GDPR Art. 28(3)" |
| `legal_agent/search.py` | Hybrid search over those chunks: BM25 keywords + WordLlama embeddings, merged with Reciprocal Rank Fusion |
| `legal_agent/policy.py` | Loads Acme's playbook (`data/policy.yaml`) and runs its red-line checks |
| `legal_agent/llm.py` | The one wrapper around `claude -p`: JSON-schema answers, token and cost counts, clear error if unavailable |
| `legal_agent/agents/classifier.py` | AI: contract type, GDPR chapters per change, and a search query in GDPR wording |
| `legal_agent/agents/reviewer.py` | AI: judges one change against the playbook and GDPR excerpts; can ask for referenced clauses (up to 2 rounds) |
| `legal_agent/agents/grouper.py` | AI: groups related changes under one theme |
| `legal_agent/guardrails.py` | Code checks that can only make a decision stricter: red lines, injection, missing rule, unknown citation, low confidence |
| `legal_agent/orchestrator.py` | Runs the steps in order, reviews changes in parallel, totals tokens, cost and time |
| `legal_agent/store.py` | SQLite: agreements, every version exchanged (with SHA-256), review rounds, a person's answer to each change, and the audit trail |
| `legal_agent/negotiation.py` | The human side of a round: what each change needs (by AI colour), building our next version from the answers, the cover note, clause history |
| `legal_agent/docx_writer.py` | Writes clauses back to a .docx that `docx_parser` reads |
| `legal_agent/signing.py` | Signs the agreed version: PDF plus certificate page, behind a small `Signer` interface so a real e-signature provider can replace it |
| `legal_agent/web/app.py` | FastAPI web app: negotiation rounds (instant diff, background AI review, answers, next version), version and diff pages, downloads, signing |
| `legal_agent/web/sources.py` | The text behind each cited source (GDPR article, playbook rule, contract clause) for the side panel |
| `legal_agent/web/templates/` | The HTML pages (Jinja, no frontend framework) |
