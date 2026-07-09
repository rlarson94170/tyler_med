# Changelog

## v0.1.1

Robustness and metadata-quality release.

### Resumability / large or time-capped runs
- State is saved after **every** file; the index/evidence table is rebuilt from
  state each pass (`--index-only` to rebuild without converting).
- `--time-budget SECONDS` for time-capped, no-background sandboxes (Cowork): re-run
  the same command until it prints `ALL_DONE`. Already-converted files are skipped
  cheaply (size + mtime, no re-hashing) so passes converge; work is ordered
  smallest-first so one big file can't starve the queue; a guard warns if `--force`
  is combined with `--time-budget`.
- `--max-pages N` page-caps oversized documents (records the truncation in
  frontmatter) so a single huge PDF fits within one call.

### OCR
- `--ocr {auto,off,force}` (default `auto`): skips OCR for born-digital journal PDFs
  (~4–5× faster per file, identical extraction) and OCRs only true scans.

### Metadata quality
- Repair the U+FFFD (`�`) glyph collapse in high-confidence statistical contexts
  (e.g. `N �` → `N =`, `� 80%` → `≥ 80%`, `mean �SD` → `mean ± SD`), which also
  recovers many previously-blank sample sizes. Ambiguous cases are left untouched.
- Cleaner author extraction: prefer the embedded PDF author when it reads like
  names, reject affiliation/address lines, capture multi-line bylines, strip orphan
  superscript markers.
- **Controlled Obsidian tags** — only `design/…`, `source/…`, and `year/…`
  namespaces (per-keyword hashtags moved behind `--keyword-tags`, off by default);
  journal boilerplate ("Article history: Received …") no longer bleeds into keywords.
- Broader sample-size detection (more phrasings and noun forms).

### Docs
- `SKILL.md`: a repeatable `index_by_theme.md` (by-topic) routine; the conversion
  command now references the skill's own base directory, so it resolves in packaged
  or Cowork deploys where the folder is named `tyler-med`.
- `README.md`: an Obsidian + Dataview usage section.

## v0.1.0

Initial release: a two-tier Markdown wiki (study-design-grouped `index.md` + full
`papers/*.md`) plus a `index.csv`/`index.json` evidence table, with reliable titles
from embedded PDF metadata + DOI, study-design classification, sample size, data
source, trial/PROSPERO registration IDs, mojibake/ligature repair, and duplicate
detection. MIT-licensed; derived from the `tyler` skill in econtools.
