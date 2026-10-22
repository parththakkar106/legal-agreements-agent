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
| `legal_agent/evaluation.py` | The eval: builds the labelled contracts in `data/eval/cases.yaml`, runs the pipeline (and the baseline), scores decisions, escalations, injections, citations, chapters, contract type, cost and time, and compares keyword, embedding and hybrid retrieval |
| `legal_agent/baseline.py` | The baseline for the eval: one raw `claude -p` call over both whole versions and the whole playbook, no diff, search or guardrails |
| `legal_agent/desk.py` | The negotiation workflow, used by both the web pages and the API: new agreements and versions, upload checks, starting the AI review, answers, closing a round, signing |
| `legal_agent/config.py` | All settings from environment variables (data folder, API keys, webhook URLs and secret, upload limit) |
| `legal_agent/webhooks.py` | Outbound events: every audit-trail entry POSTed to the configured URLs, HMAC-signed with a timestamp, retried; `verify()` for receivers |
| `legal_agent/store.py` | SQLite: agreements, every version exchanged (with SHA-256), review rounds, a person's answer to each change, and the audit trail |
| `legal_agent/negotiation.py` | The human side of a round: what each change needs (by AI colour), building our next version from the answers, the cover note, clause history |
| `legal_agent/docx_writer.py` | Writes clauses back to a .docx that `docx_parser` reads |
| `legal_agent/signing.py` | Signs the agreed version: PDF plus certificate page, behind a small `Signer` interface so a real e-signature provider can replace it |
| `legal_agent/web/app.py` | FastAPI web app: the pages people use (contract list, negotiation rounds, versions, diff, signing), `/healthz`, and the API router |
| `legal_agent/web/api.py` | The JSON API under `/api/v1`: API-key auth, agreements, versions, reviews, answers, closing rounds, downloads; no signing |
| `legal_agent/web/portfolio.py` | Acme's contract portfolio: plays each negotiation through the app once (`scripts/build_portfolio.py`), saves it to `data/portfolio/`, loads it into an empty database on `serve` |
| `legal_agent/web/sources.py` | The text behind each cited source (GDPR article, playbook rule, contract clause) for the side panel |
| `legal_agent/web/templates/` | The HTML pages (Jinja, no frontend framework) |
