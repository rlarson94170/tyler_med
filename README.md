# tyler_med — Medical Literature Wiki Skill

A [Claude Code](https://claude.com/claude-code) skill that converts a folder of
**medical / clinical journal PDFs** (JVS, JAMA, NEJM, Ann Surg, EJVES, Cochrane, …)
into a token-efficient, two-tier Markdown wiki for literature review — with
study-design classification, DOI extraction, an evidence-table export, and
duplicate detection.

It is a medical-tuned fork of the general-purpose **`tyler`** skill from the
[`econtools`](https://github.com/johanfourieza/econtools) project (see
[Acknowledgements](#acknowledgements)).

## What it produces

```
WIKI_DIR/
├── index.md            # papers grouped by study design (evidence hierarchy),
│                       #   with DOI links, sample size (N), and data source
├── index.csv           # evidence-table skeleton (one row per paper)
├── index.json          # same, machine-readable
├── papers/*.md         # cleaned full text + rich YAML frontmatter
├── references/*.md     # trimmed reference lists (kept, not discarded)
└── .wiki_state.json    # incremental cache
```

Read `index.md` (or the CSV) to navigate 100+ papers cheaply; open an individual
`papers/*.md` only when you need the full text. The folder is also a ready-to-use
**Obsidian vault** (frontmatter → note properties, `[[wikilinks]]`, nested tags).

## Requirements

- Python 3.9+
- [`pymupdf4llm`](https://pypi.org/project/pymupdf4llm/) (bundles PyMuPDF / `fitz`,
  used for embedded-metadata and DOI extraction)

```bash
pip install pymupdf4llm
```

## Usage

**As a Claude Code skill:** copy this folder to `~/.claude/skills/tyler_med/` and
invoke `/tyler_med`.

**Directly:**

```bash
python3 convert.py "PDF_DIR" "WIKI_DIR" [OPTIONS]
```

| Flag | Effect |
|------|--------|
| `-r`, `--recursive` | Scan `PDF_DIR` subdirectories |
| `--prefer-pdf-title` | Trust the PDF's embedded metadata title over the filename (good for older, badly-named files) |
| `--keep-references` | Keep references inline in each paper file |
| `--drop-references` | Discard references entirely (default: save to `references/`) |
| `--force` | Re-convert everything, ignoring the incremental cache |

## Medical-tuning highlights

- **Reliable titles** from embedded PDF metadata + DOI, not just the filename.
- **Mojibake / ligature repair** (e.g. `n ¼ 4,894` → `n = 4,894`, dropped `fi`/`fl`).
- **Study-design classification** (RCT, systematic review, meta-analysis, cohort,
  case-control, guideline/consensus, scoping/narrative review, QI, protocol,
  editorial). A named registry in the title (NSQIP, VQI, Medicare, …) implies a
  cohort; an explicit `EDITORIAL` tag in a title is honoured.
- **Structured metadata:** DOI, sample size (`n=`), data source, trial/PROSPERO
  registration IDs, and clean controlled tags.
- **Duplicate detection** (shared DOI or year+title) with `⚠️DUP-n` flags.

> **Note:** study design, sample size, and data source are heuristic (best-effort
> from the abstract). Verify anything load-bearing against the full text.

## Using it as an Obsidian vault

Open `WIKI_DIR` in Obsidian (or drop it into an existing vault). Each `papers/*.md`
carries YAML frontmatter that becomes note **properties**, and the index links every
paper with `[[wikilinks]]`.

**Tags are deliberately minimal and controlled** — only three nested namespaces, so
the tag pane and graph stay clean and queryable:

- `design/…` — study design (e.g. `design/cohort-study`, `design/randomized-controlled-trial`)
- `source/…` — data source (e.g. `source/nsqip`, `source/vqi`, `source/medicare/cms`)
- `year/…` — publication year (e.g. `year/2023`)

Per-keyword hashtags are **off by default** (they flood the graph); the human-readable
`keywords:` property is kept regardless. Pass `--keyword-tags` if you want them.

**Dataview** turns the frontmatter into a live evidence table. Examples:

````md
```dataview
TABLE study_type AS Design, year AS Year, sample_size AS N, journal
FROM "papers"
WHERE contains(data_source, "VQI")
SORT year DESC
```
````

````md
```dataview
TABLE WITHOUT ID link(file.link, title) AS Paper, doi
FROM #design/randomized-controlled-trial
SORT year DESC
```
````

For a **by-topic** view (what to cite for a given point), ask Claude to build
`index_by_theme.md` (see Step 4 in `SKILL.md`) — a curated thematic grouping with a
design badge and one-line contribution note per paper.

## Acknowledgements

This project is a derivative work of the **`tyler`** skill in the
[`econtools`](https://github.com/johanfourieza/econtools) project by
[@johanfourieza](https://github.com/johanfourieza), used and continued under the
MIT License. The original `tyler` was built for economics / social-science papers;
`tyler_med` re-tools it for clinical literature (study-design classification,
DOI/registry metadata, evidence-table export, mojibake repair, duplicate
detection). With thanks to the original author.

## License

Released under the **MIT License** — see [`LICENSE`](LICENSE). Copyright is held by
`johanfourieza` for the upstream `tyler` skill and by Robert A. Larson, MD for
the medical adaptation.
