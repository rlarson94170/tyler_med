---
name: tyler_med
description: Convert a folder of MEDICAL/clinical academic PDFs into a token-efficient markdown wiki + evidence table for literature review. Use when the user has a folder of clinical journal PDFs (JVS, JAMA, NEJM, Ann Surg, Cochrane, etc.) to process into .md files with study-design, sample-size, data-source, and DOI metadata. Trigger phrases include "build my medical wiki", "process my clinical papers", "convert these journal PDFs", "/tyler_med".
allowed-tools: Bash, Read, Write, Edit
user-invocable: true
---

# PDF-to-Wiki Skill — Medical Edition

## Purpose

Converts a directory of clinical/medical academic PDFs into a structured, two-tier
markdown wiki optimised for token efficiency AND for literature-review workflows.
It is a medical-tuned fork of the `tyler` skill: it drops the economics machinery
(JEL codes, JSTOR filters) and adds clinical metadata.

A typical 12-page clinical paper costs ~12,000 tokens to read in full. This skill produces:

- **Tier 1 — Index** (`index.md`): ~450 tokens/paper, **grouped by study design**
  in evidence-hierarchy order (guidelines → systematic reviews → RCTs → cohort →
  … ). Each entry has title, authors, year, journal, DOI link, sample size (N),
  data source, and abstract.
- **Tier 1b — Evidence table** (`index.csv` + `index.json`): the same metadata as
  a spreadsheet-ready table — a ready skeleton for a PRISMA-style evidence table.
- **Tier 2 — Full papers** (`papers/*.md`): cleaned markdown with rich YAML
  frontmatter. Read on demand for detail.
- **References** (`references/*.md`): trimmed reference lists kept separately, so
  the main files stay lean but citations remain available for snowball searching.

## What makes it medical-tuned

- **Reliable titles**: reads the PDF's embedded metadata + DOI (via PyMuPDF), so
  it recovers correct titles even when a filename is truncated/abbreviated. A
  curated `YEAR JOURNAL - Title` filename still takes precedence when present.
- **DOI extraction** → frontmatter + clickable `https://doi.org/...` links.
- **Mojibake / ligature repair**: fixes clinical-PDF garbage such as `n ¼ 4,894`
  → `n = 4,894`, dropped `fi/fl` ligatures, and smart-quote/accent corruption.
- **Study-design classification** (`study_type`): RCT, systematic review,
  meta-analysis, scoping/narrative review, cohort, case-control, cross-sectional,
  case series, guideline/consensus, quality-improvement, protocol, editorial.
- **Sample size** (`n = …`), **data source** (NSQIP, VQI, Medicare/CMS, Cerner,
  NIS, SEER, Cochrane, single-center), and **registration IDs** (ClinicalTrials.gov
  `NCT…`, PROSPERO `CRD…`).
- **Duplicate detection**: flags papers sharing a DOI or the same year + title
  (your folders often contain two copies of one study).

Each `papers/*.md` file has YAML frontmatter:
```yaml
---
title: "Predictors of surgical site infection following infrainguinal bypass surgery"
authors: "Shima Rahgozar; Mohammed Hamouda; ..."
year: "2026"
journal: "JVS"
doi: "10.1016/j.jvs.2025.xx.xxx"
study_type: "Cohort study"
sample_size: "27089"
data_source: ["VQI"]
registration: ["NCT01234567"]   # if found
keywords: "Surgical site infection; Infrainguinal bypass; ..."
tags: [surgical-site-infection, design/cohort-study, source/vqi]
source_pdf: "2026 JVS - Predictors of SSI ...pdf"
references_file: "references/2026_JVS_-_Predictors_of_SSI.md"
---
```

The output folder is a ready-to-use **Obsidian vault**: frontmatter becomes note
properties, `design/…` and `source/…` become nested tags, and Dataview can query
by study_type, year, data_source, or DOI.

## Step-by-step instructions

### Step 0: Ask the user for inputs
- **PDF_DIR**: folder containing the PDFs.
- **WIKI_DIR**: output location. Default: `wiki/` in the current directory.
- **Recursive?** scan subdirectories. Default: no.
- **References?** default keeps them in `references/`. Offer `--keep-references`
  (inline) or `--drop-references` (discard) if the user prefers.

Confirm paths before proceeding.

### Step 1: Check/install libraries
```bash
python3 -c "import pymupdf4llm, fitz" 2>/dev/null || pip install pymupdf4llm --break-system-packages --quiet
```
`fitz` (PyMuPDF) ships with pymupdf4llm and is required for embedded-metadata/DOI
reading. Report success/failure before continuing.

### Step 2: Run the conversion script
The converter `convert.py` is bundled in **this skill's own directory**. Run it by
its real path — do NOT hardcode `$HOME/.claude/skills/tyler_med/`, because the folder
name and location differ across environments (the CLI/code environment keeps it at
`~/.claude/skills/tyler_med/`, but packaged/deployed environments such as Cowork mount
a read-only, kebab-cased copy like `.../skills/tyler-med/` at a different path). Use
the **skill base directory** the runtime gives you when this skill loads:
```bash
python3 "SKILL_DIR/convert.py" "PDF_DIR" "WIKI_DIR" [OPTIONS]
```
Substitute `SKILL_DIR` with this skill's actual base directory (the folder containing
this `SKILL.md`). If you're unsure of it, locate the script first, e.g.
`find "$HOME/.claude" /var/folders /sessions -name convert.py -path '*tyler*med*' 2>/dev/null | head -1`.

**Flags:**
- `--recursive` / `-r`: scan recursively
- `--prefer-pdf-title`: trust the PDF's embedded metadata title over the filename
  (best for older, badly-named files)
- `--keep-references`: keep references inline in each paper file
- `--drop-references`: discard references entirely (default: save to `references/`)
- `--force`: re-convert everything, ignoring the incremental cache
- `--ocr {auto,off,force}`: OCR policy (default `auto`). `auto` OCRs only PDFs that
  lack an adequate text layer, so born-digital journal PDFs skip the slow Tesseract
  pass (~4-5x faster per file, identical extraction); `off` never OCRs; `force`
  always OCRs. Use `off` for a known born-digital set to go fastest.
- `--time-budget SECONDS`: stop starting new conversions after SECONDS, then exit 0
  with a `PENDING n` line (for time-capped sandboxes — see below)
- `--index-only`: skip conversion, just rebuild `index.md` + evidence table from the
  cache
- `--max-pages N`: convert only the first N pages of any PDF longer than N (records
  `truncated: true` + `pages_converted` / `total_pages` in that paper's frontmatter).
  Use for oversized documents (e.g. a 200+ page review) whose full conversion would
  exceed a per-call time budget — title/DOI/abstract/study_type come from the front
  matter, so the entry is still correct; only the tail (appendices, full references)
  is dropped.

The script automatically: finds PDFs (skips unchanged), converts via pymupdf4llm,
reads embedded metadata + DOI, repairs mojibake, extracts clinical metadata,
classifies study design, cleans text, splits references, writes each paper with
frontmatter, detects duplicates, and builds `index.md` + `index.csv` + `index.json`.

**Important:** the index and evidence table are built entirely in Python — you do
NOT need to read individual paper files to build them.

**Resumable runs (time-capped / no-background environments like Cowork):** state is
saved to `.wiki_state.json` after *every* file, and the index is rebuilt from that
state each pass, so a run can be interrupted and resumed without losing work. When a
shell has a hard per-call wall-clock limit and cannot run background processes, pass
`--time-budget` (e.g. a value comfortably under the cap) and **re-run the exact same
command** until the output prints `ALL_DONE` instead of `PENDING n`. Already-converted
files are skipped cheaply (by size+mtime, no re-hashing), so every pass spends its
budget on real work and the run converges. Example supervisor loop:
```bash
until python3 "SKILL_DIR/convert.py" "PDF_DIR" "WIKI_DIR" \
      --time-budget 30 | tee /dev/stderr | grep -qa ALL_DONE; do :; done
```
(`SKILL_DIR` = this skill's base directory, as in Step 2.)

**Do NOT put `--force` in the resume loop** — `--force` re-converts every file each
pass and will never converge under a budget (the script warns if you combine them).
For a resumable *full rebuild*, use `--force` on the **first pass only**, then re-run
the loop **without** it. And if a single document is so large that even one file's
conversion exceeds the per-call cap, add `--max-pages N` so it can be checkpointed
within one call.

In the code environment (no wall-clock cap), omit `--time-budget` and it runs in one
pass as before.

### Step 3: Report to the user
Report: PDFs found / converted / skipped / failed; the study-design breakdown the
script prints; any duplicate groups flagged; token savings; and locations of
`index.md`, `index.csv`/`index.json`. Flag any files that produced <500 bytes
(likely scanned — need OCR).

Then explain future use:

> **Using your wiki:** read `WIKI_DIR/index.md` for all papers grouped by study
> design, or open `index.csv` as an evidence table. Ask me questions referencing
> the index — I'll read individual `papers/*.md` files only when I need full text.
> Grep across `papers/` for specific terms.

### Step 4 (optional): Enrich the index with Claude
Only if the user asks: add a one-line contribution note per paper, build a
thematic (topic) grouping to complement the by-design grouping, or draft a
narrative synthesis / evidence-table rows from the abstracts.

## Gotchas and known issues
- **Scanned PDFs & OCR**: with `--ocr auto` (default) the script samples each PDF's
  text layer and only runs Tesseract OCR on PDFs that lack one (true scans). Born-
  digital journal PDFs skip OCR entirely — much faster, with identical extraction.
  A genuine scan with no text layer still yields near-empty output if OCR can't
  recover it (flagged <500 bytes); `ocrmypdf` first is the fallback. Use
  `--ocr force` to OCR everything (old behavior) or `--ocr off` to never OCR.
- **Heuristic metadata**: study-design, N, and data-source are best-effort from
  the abstract; verify anything load-bearing against the full text. `study_type`
  defaults to "Observational/Other" when no cues match.
- **Sample size** picks the largest plausible `n=`/"total of X patients" figure —
  usually the study total, but check for multi-cohort papers.
- **DOI** is read from embedded metadata / first page / early text; a few papers
  (older scans, some Cochrane) may lack one.
- **Duplicate flags** are advisory (DOI or year+title match) — confirm before
  deleting anything.
- **Symbol repair** maps `¼`/`½` → `=` (their overwhelming meaning in Elsevier/JVS
  extraction); a genuine "¼" fraction would be rare collateral.
- **References** are saved to `references/` by default (not discarded); this costs
  a little disk but preserves citation chasing.
- **Re-running** is incremental; use `--force` to rebuild. Note: re-running
  re-derives metadata from the PDF, so manual edits to a paper's frontmatter are
  overwritten if that PDF changes.

## Credit & license

Derived from the `tyler` skill in the [econtools](https://github.com/johanfourieza/econtools)
project by [@johanfourieza](https://github.com/johanfourieza), used and continued
under the MIT License. See `LICENSE` and `README.md`.
