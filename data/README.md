# Data

Everything the app needs is committed here, so nothing is downloaded at run time.

| Folder | What it is | Source and license |
| --- | --- | --- |
| `gdpr/gdpr.json` | Full GDPR text (99 articles in 11 chapters, plus recitals) as structured JSON | [GDPRtEXT](https://github.com/coolharsh55/GDPRtEXT) by Harshvardhan Pandit, CC BY 4.0 |
| `sources/commonpaper/` | Standard contract terms in Markdown: Mutual NDA, Cloud Service Agreement (our MSA), DPA | [Common Paper](https://commonpaper.com/standards/), CC BY 4.0 |
| `contracts/base/` | Acme's own versions of the four contracts as .docx (Common Paper terms plus our Key Terms cover page; the SOW is written by us) | Built by `scripts/build_contracts.py` |
| `contracts/redlines/` | Customer-edited versions of those contracts, used for the demo and tests | Built by `scripts/build_contracts.py` |

To rebuild the .docx files after changing the script: `python scripts/build_contracts.py`.
