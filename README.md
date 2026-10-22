# Acme Agreement Desk: a contract manager with an AI review agent

A local, DocuSign-like tool for **Acme Cloud Inc.**, a SaaS vendor that sends customers its own
contracts (NDA, Cloud Service Agreement, DPA, SOW). Customers send back edited versions. The app
keeps every version, shows what changed clause by clause, has an AI agent review each change
against Acme's playbook and the GDPR, lets a person answer each change, builds our next version,
and finally signs the agreed version as a PDF with a certificate page.

**The AI recommends; a person decides and signs.** Code guardrails can only make an AI decision
stricter, never looser, and the AI never sends, accepts or signs anything.

## Run it

Needs Python 3.10+ and, for anything that calls the model, the `claude` CLI (Claude Code) with
`CLAUDE_KEY` set (passed to the CLI as `CLAUDE_CODE_OAUTH_TOKEN`). Without it, AI commands stop
with a clear error; nothing falls back to cached answers.

```bash
pip install -r requirements.txt

python src/main.py diff data/contracts/base/msa.docx data/contracts/redlines/msa_globex_v2.docx   # clause diff, no AI
python src/main.py search "sub-processor" --chapter IV                                            # GDPR search, no AI
python src/main.py review data/contracts/base/msa.docx data/contracts/redlines/msa_globex_v2.docx # full AI review
python src/main.py review data/contracts/base/sow.docx data/contracts/redlines/sow_globex_v2.docx \
    --related msa=data/contracts/base/msa.docx                                                    # SOW that points at the MSA
python src/main.py eval --baseline                                                                # eval vs one-call baseline (~3 min)
python -m pytest -q                                                                               # 58 tests, offline
```

`run.yaml` runs these commands as the grader does.

### The web app

```bash
python src/main.py serve              # http://127.0.0.1:8000
python src/main.py serve --port 8001  # if 8000 is taken
```

On first start it loads Acme's contract portfolio (9 agreements, from `data/portfolio/`) into
`data/app/`. Delete `data/app/` to start again from the portfolio.

| Section | What is there |
| --- | --- |
| Not reviewed | DPA from Globex: the redline is in; click **Run AI review** to watch the agent work live |
| Needs review | Cloud Service Agreement with Hooli (v4), SOW with Globex: AI findings, answer each change |
| Ready to sign | Mutual NDA with Vandelay: sign it and download the PDF |
| Waiting for counterparty | DPA with Initech (our v3 is with them), NDA with Soylent (v1 just sent) |
| Signed | Cloud Service Agreement with Globex (v1 to v4), NDA with Initech, DPA with Umbrella |

The portfolio was played once through the app with live AI reviews (`scripts/build_portfolio.py`);
those reviews are part of each agreement's history. Every review you start runs live.

## How a negotiation works

1. **Version in.** We send v1. They send back v2 (a .docx). The clause diff shows at once (plain code).
2. **AI review.** In the background, the agent reviews each change; the page updates as it goes.
   Each change gets a colour: **escalate** (red), **push back** (yellow) or **accept** (green), with
   what it means, the impact on Acme, a suggested reply, and its sources (playbook rule, GDPR
   paragraph, or a referenced clause), each viewable in a side panel.
3. **A person answers** each change: accept, reject (keep our wording) or counter (new wording).
   Red needs an outcome and an internal comment, yellow needs an outcome, green counts as accepted.
4. **Close the round.** If everything is accepted, the agreement is ready to sign. Otherwise the
   app builds our v3 from the answers plus a cover note, and it goes back unsigned. Internal
   comments and AI findings never go into the document sent out.
5. **Sign.** Only the agreed version, only by a person: name, optional designation and a
   handwriting style (saved once in a profile and reused), plus a consent tick per document.
   Signing is refused if the file's SHA-256 changed. A final red-line check runs on the whole
   text. The PDF carries the signature and a certificate page listing every version and its hash.

Everything lands in the agreement's audit trail.

## Architecture

```
 .docx v(n), v(n+1)
        |
  docx_parser --> clause_diff  ......................  plain code: which clauses changed
        |
  Classifier (AI, 1 call) ............................  contract type, GDPR chapters per change,
        |                                                a search query in GDPR wording
  hybrid search over the GDPR (BM25 + embeddings, RRF)   top 4 paragraphs per change
        |
  Reviewer (AI, 1 call per change, in parallel) ......  decision + rules + citations + reply;
        |   ^__ "need more context": asks for a           can ask for clauses the change refers to
        |       referenced clause, up to 2 rounds         (e.g. the MSA's Section 8 for an SOW)
  guardrails (code) ..................................  can only make a decision stricter
        |
  Grouper (AI, 1 call) ...............................  related changes under one theme
        |
  web app: a person answers -> next version or ready to sign -> sign -> PDF + certificate
```

- **One way to call the model:** `src/legal_agent/llm.py` runs `claude -p` with a JSON schema, no
  tools, no MCP servers, no settings, from an empty folder. Every answer is structured data.
- **Plain code decides the order** (`orchestrator.py`); the three agents only make judgments.
- **Knowledge:** Acme's playbook (`data/policy.yaml`, 14 rules with accept / push back / escalate
  positions and red-line patterns) and the GDPR (414 chunks, one per article paragraph). There is
  no fixed clause taxonomy: a change no rule covers is escalated.
- **Search:** BM25 finds exact words, WordLlama embeddings (shipped inside the pip package, so
  nothing is downloaded) find the same meaning in other words; Reciprocal Rank Fusion merges them.

The code map is in [`src/README.md`](src/README.md); the data and its licences in [`data/README.md`](data/README.md).

## Guardrails

Each Reviewer answer goes through code checks (`guardrails.py`). Any hit escalates the change:

| Check | Why |
| --- | --- |
| A red-line pattern in the words the customer added (e.g. `uncapped`, a sanctioned country) | Some things must always reach a person, whatever the model says |
| Text that tries to instruct the AI (pattern match, or the Reviewer's own flag) | Prompt injection hidden in clause text |
| No playbook rule cited | If we have no position, a person decides |
| A cited rule or GDPR paragraph that was not given to the Reviewer | Stops invented sources |
| Confidence below 0.6 | Unsure means a person looks |

Counterparty text is always wrapped in tags and the prompts say it is data, not instructions. The
Reviewer sees only the change, the playbook, the GDPR excerpts and the references it may ask for,
not the text of other clauses unless it asks.

## Evaluation

`python src/main.py eval --baseline` builds 8 contracts from our base contracts with 45 labelled
customer edits (5 hide prompt injections), runs the pipeline, runs a **baseline** (one raw
`claude -p` call with both whole versions and the whole playbook), and scores both. Full report:
[`docs/eval-results.md`](docs/eval-results.md).

| | Our pipeline | Baseline: one raw call |
| --- | --- | --- |
| Decision accuracy | **45/45 (100%)** | 42/45 (93%) |
| Escalation recall (changes a person must see) | **18/18 (100%)** | 16/18 (89%) |
| Prompt injections caught | 5/5 | 5/5 |
| Unsafe accepts | 0 | 0 |
| Right playbook rule cited | 100% | 100% |
| GDPR citations that exist | 100% | 100% |
| GDPR chapters exactly right | 80% | 100% |
| Cost / time per contract | $0.115 / 23 s | $0.025 / 8 s |

What it shows: the baseline is cheaper and nearly as accurate, but in both runs it only pushed back
on governing law moved to Russia and Belarus, which the playbook says a person must decide. The
pipeline caught every one of them. The eval also found a real bug: the Reviewer's list of available
references carried the first words of every clause, so an injection hidden in one clause reached
every review and wrongly escalated innocent changes. It now lists references only. Caveats: 45
cases labelled by us from the playbook, one live run each (numbers move a little between runs),
and two ambiguous cases were reworded after the first run (noted in `data/eval/cases.yaml`).

## Limitations

- **Our paper only.** Contracts start from Acme's templates; reviewing a counterparty's own paper
  (a whole-document review plus a check for missing protections) is designed but not built.
- **Not modelled:** the counterparty's countersignature, roles and signing authority (for example
  legal-only sign-off on MSAs), login, multiple profiles, notifications and webhooks.
- **Formatting:** our next version is regenerated in our layout, not as Word tracked changes in
  their file. Unusually formatted .docx files may split into clauses badly.
- **Signature:** a typed electronic signature with consent and a SHA-256 certificate, behind a
  small `Signer` interface so DocuSign or Dropbox Sign could replace it. It is not a qualified
  electronic signature.
- **GDPR only** as legal knowledge, and only the binding articles (no recitals).
- **Cost and time:** about $0.10 and 20 to 40 seconds per contract review; the eval costs about
  $1.10 with the baseline.
- **Local only:** SQLite and files in `data/app/`, one user, no authentication.

## Sources

Contract terms from [Common Paper](https://commonpaper.com/standards/) (CC BY 4.0); GDPR text from
[GDPRtEXT](https://github.com/coolharsh55/GDPRtEXT) (CC BY 4.0); handwriting fonts from Google Fonts
(SIL Open Font License, Apache 2.0). Company and people names in the examples are fictional.
