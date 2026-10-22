# Data

Everything the app needs is committed here, so nothing is downloaded at run time.

| Folder | What it is | Source and license |
| --- | --- | --- |
| `gdpr/gdpr.json` | Full GDPR text (99 articles in 11 chapters, plus recitals) as structured JSON | [GDPRtEXT](https://github.com/coolharsh55/GDPRtEXT) by Harshvardhan Pandit, CC BY 4.0 |
| `sources/commonpaper/` | Standard contract terms in Markdown: Mutual NDA, Cloud Service Agreement (our MSA), DPA | [Common Paper](https://commonpaper.com/standards/), CC BY 4.0 |
| `contracts/base/` | Acme's own versions of the four contracts as .docx (Common Paper terms plus our Key Terms cover page; the SOW is written by us) | Built by `scripts/build_contracts.py` |
| `contracts/redlines/` | Customer-edited versions of those contracts, used in the portfolio and tests | Built by `scripts/build_contracts.py` |
| `eval/cases.yaml` | 45 labelled changes in 8 contracts built from `contracts/base/`, 5 with hidden prompt injections: the expected decision, playbook rules, GDPR chapters and articles for each | Written by us from `policy.yaml` |
| `fonts/` | Handwriting fonts for signatures (Dancing Script, Caveat, Homemade Apple), used by the web page and the signed PDF | Google Fonts: SIL Open Font License 1.1 and Apache 2.0, licences alongside |
| `portfolio/` | The contracts the web app starts with: every version, AI review, answer and signed PDF, played through the app with live AI reviews | Built by `scripts/build_portfolio.py` |

To rebuild the .docx files after changing the script: `python scripts/build_contracts.py`.
To rebuild the portfolio (needs `claude -p`, a few minutes): `python scripts/build_portfolio.py`, then delete `data/app/` so the next `serve` loads it.
