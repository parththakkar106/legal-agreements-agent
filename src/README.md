# Code map

| File | What it does |
| --- | --- |
| `main.py` | Command line entry point (`diff`, `search`) |
| `legal_agent/docx_parser.py` | Reads a .docx and splits it into clauses (by typed clause numbers and Word headings) |
| `legal_agent/clause_diff.py` | Matches clauses between two versions and lists what was added, removed or modified, with a word-level diff |
| `legal_agent/gdpr.py` | Splits the GDPR into one chunk per article paragraph, each with a citation like "GDPR Art. 28(3)" |
| `legal_agent/search.py` | Hybrid search over those chunks: BM25 keywords + WordLlama embeddings, merged with Reciprocal Rank Fusion |
