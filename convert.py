#!/usr/bin/env python3
#
# tyler_med — PDF-to-Wiki converter for medical / clinical literature.
#
# Derived from the "tyler" skill in the econtools project by johanfourieza
# (https://github.com/johanfourieza/econtools), used under the MIT License.
# Medical adaptation and modifications (c) 2026 Robert A. Larson, MD.
# Distributed under the MIT License; see the LICENSE file for full terms.
#
"""
PDF-to-Wiki converter tuned for MEDICAL / clinical academic literature.

Forked from the general `tyler` skill and adapted for clinical journals
(JVS, Ann Surg, JAMA, NEJM, EJVES, Cochrane, ...). Compared with the original
econ-oriented script it:

  * Reads the PDF's EMBEDDED metadata (PyMuPDF) and DOI as a reliable
    title/author source, ahead of text-scraping.
  * Repairs the ligature / mojibake garbage common in Elsevier/clinical PDFs
    (e.g. "n ¼ 4,894" -> "n = 4,894", "signi<fi>cant", smart quotes).
  * Classifies STUDY DESIGN (RCT, systematic review, meta-analysis, cohort,
    guideline, ...) and extracts SAMPLE SIZE, DATA SOURCE (NSQIP/VQI/Medicare/
    Cochrane), and trial/PROSPERO REGISTRATION IDs.
  * Emits an evidence-table export (index.csv + index.json) alongside index.md,
    groups index.md by study design, and flags likely DUPLICATE papers.
  * Preserves structured-abstract labels (Background/Methods/Results/Conclusions).
  * Saves trimmed references to a separate references/ file instead of discarding.
  * Drops JEL codes and JSTOR boilerplate entirely; uses clinical boilerplate
    filters and MeSH-style keyword tags.

Output layout:
  WIKI_DIR/
  ├── index.md            # grouped by study design, with DOI/N/source inline
  ├── index.csv           # evidence-table skeleton (one row per paper)
  ├── index.json          # same, machine-readable
  ├── papers/*.md         # cleaned full text + YAML frontmatter
  ├── references/*.md     # trimmed reference lists (unless --drop-references)
  └── .wiki_state.json    # incremental cache
"""

import sys
import os
import re
import csv
import json
import html
import time
import hashlib
import difflib
import argparse
import unicodedata


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------

def sanitise_filename(name):
    """Convert a PDF filename to a safe markdown filename."""
    name = os.path.splitext(name)[0]
    name = re.sub(r'[^\w\s-]', '', name)
    name = re.sub(r'[\s]+', '_', name)
    name = name.strip('_')
    return name + '.md'


def file_hash(path):
    """Return SHA-256 hex digest of a file (for incremental mode)."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Text repair: ligatures + mojibake (Tier 1)
# ---------------------------------------------------------------------------

# Windows-1252-decoded-as-UTF-8 and other high-frequency clinical-PDF glitches.
_MOJIBAKE = {
    'ﬀ': 'ff', 'ﬁ': 'fi', 'ﬂ': 'fl', 'ﬃ': 'ffi',
    'ﬄ': 'ffl', 'ﬅ': 'ft', 'ﬆ': 'st',
    'â€™': "'", 'â€˜': "'", 'â€œ': '"', 'â€\x9d': '"', 'â€"': '—',
    'â€“': '–', 'â€¦': '…', 'â€': '"',
    'Ã©': 'é', 'Ã¨': 'è', 'Ã¡': 'á', 'Ã ': 'à', 'Ã¤': 'ä', 'Ã¶': 'ö',
    'Ã¼': 'ü', 'Ã±': 'ñ', 'Ã§': 'ç', 'Ã‰': 'É', 'Ã–': 'Ö', 'Ãœ': 'Ü',
    'Â ': ' ', 'Â': '',
}

# Characters PyMuPDF frequently substitutes for '=' and similar operators
# in Elsevier/JVS PDFs (e.g. "n ¼ 4,894", "P ¼ .01", "age ½ 66").
_SYMBOL_FIX = {
    '¼': '=',   # ¼  -> =
    '¾': '>=',  # ¾  -> >= (approx; rare)
    '½': '=',   # ½  -> = (context-dependent, but overwhelmingly '=')
    ' ': ' ', ' ': ' ', ' ': ' ',  # thin/nbsp spaces
    '‐': '-', '‑': '-',  # unicode hyphens -> ascii
}


def repair_text(text):
    """Fix ligatures, mojibake, and operator substitutions; NFC-normalise."""
    if not text:
        return text
    for bad, good in _MOJIBAKE.items():
        if bad in text:
            text = text.replace(bad, good)
    for bad, good in _SYMBOL_FIX.items():
        if bad in text:
            text = text.replace(bad, good)
    # Normalise to composed form so accents render as single code points.
    text = unicodedata.normalize('NFC', text)
    return text


def yaml_dq(s):
    """Return a YAML double-quoted scalar with proper escaping."""
    s = '' if s is None else str(s)
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


# ---------------------------------------------------------------------------
# Metadata extraction heuristics
# ---------------------------------------------------------------------------

def clean_field(text):
    """Remove markdown/formatting artifacts from an extracted metadata field."""
    text = re.sub(r'\*\*?', '', text)
    text = re.sub(r'_([^_]+)_', r'\1', text)
    text = re.sub(r'\[(\d+)\]', '', text)
    text = re.sub(r'\[[a-zA-Z]{1,3}\]', '', text)
    text = re.sub(r'\[\s*,\s*\]', '', text)
    text = re.sub(r'==>.*?<==', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def is_junk_line(line):
    """Return True for clinical-PDF boilerplate (download stamps, footers)."""
    s = line.strip().lower()
    junk = [
        r'==>.*?<==',
        r'downloaded for ',
        r'downloaded from ',
        r'clinicalkey',
        r'sciencedirect',
        r'for personal use only',
        r'no other uses without permission',
        r'copyright ©',
        r'all rights reserved',
        r'elsevier inc',
        r'published by elsevier',
        r'the author\(s\)',
        r'creativecommons',
        r'https?://(?:dx\.)?doi\.org',
        r'journals\.sagepub\.com',
        r'wolters kluwer',
        r'see end of article',
        r'article reuse guidelines',
    ]
    for pat in junk:
        if re.search(pat, s):
            return True
    return False


def parse_filename_meta(filename):
    """Parse a 'YEAR JOURNAL - Title' style filename into metadata.

    Curated clinical collections name files like
    '2015 JVS - Cost analysis of vascular readmissions.pdf'. When present this
    is a reliable title/journal/year source. Splits on the FIRST ' - ' so a
    title containing its own dash/colon stays intact.
    """
    stem = os.path.splitext(os.path.basename(filename))[0]
    m = re.match(r'\s*((?:19|20)\d{2})\b[\s.\-]*(.*)$', stem)
    if not m:
        return {}
    out = {'year': m.group(1)}
    rest = m.group(2).strip()
    if ' - ' in rest:
        journal, title = rest.split(' - ', 1)
        journal = journal.strip(' -')
        title = title.strip(' -')
        if 0 < len(journal) <= 40:
            out['journal'] = journal
        if len(title) >= 6:
            out['title'] = title
    return out


def pdf_embedded_meta(pdf_path):
    """Return (title, author, doi, page1_text) from the PDF itself via PyMuPDF.

    Embedded document metadata + first-page text are often more reliable than
    scraping a rendered two-column layout — and they rescue papers whose
    filenames are truncated or abbreviated.
    """
    title = author = doi = ''
    page1 = ''
    try:
        import fitz  # PyMuPDF
        with fitz.open(pdf_path) as doc:
            md = doc.metadata or {}
            title = repair_text((md.get('title') or '').strip())
            author = repair_text((md.get('author') or '').strip())
            # DOI sometimes hides in the 'subject'/'keywords' metadata too.
            meta_blob = ' '.join(str(md.get(k, '')) for k in
                                 ('subject', 'keywords', 'doi'))
            if doc.page_count:
                page1 = repair_text(doc[0].get_text() or '')
            doi = find_doi(meta_blob + '\n' + page1)
    except Exception:
        pass
    # Reject junk embedded titles (filenames, 'Microsoft Word - ...', all caps noise)
    if title:
        low = title.lower()
        if (low.endswith('.pdf') or low.endswith('.doc') or low.endswith('.docx')
                or low.startswith('microsoft word')
                or low.startswith('untitled') or len(title) < 8 or len(title) > 300):
            title = ''
    return title, author, doi, page1


def _pdf_needs_ocr(pdf_path, sample_pages=12, min_chars_per_page=100):
    """Heuristic: a PDF needs OCR only if it lacks an adequate embedded text
    layer (i.e. it is a scan). Born-digital journal PDFs carry thousands of
    characters per page and should skip the (slow) Tesseract pass entirely —
    pymupdf4llm's layout engine otherwise OCRs figure/image pages even when the
    real text is already present, which is pure wasted time. Returns True only
    when the sampled text density is too low to trust."""
    try:
        import fitz
        with fitz.open(pdf_path) as d:
            n = min(sample_pages, d.page_count) or 1
            chars = sum(len(d[i].get_text()) for i in range(n))
        return (chars / n) < min_chars_per_page
    except Exception:
        return True  # if we cannot tell, allow OCR (safe default)


DOI_RE = re.compile(r'\b10\.\d{4,9}/[-._;()/:a-z0-9]+', re.IGNORECASE)


def find_doi(text):
    """Extract and normalise the first DOI found in text."""
    if not text:
        return ''
    m = DOI_RE.search(text)
    if not m:
        return ''
    doi = m.group(0).rstrip('.,);:]>')
    # Trim common trailing artefacts glued on by extraction.
    doi = re.sub(r'(this|abstract|introduction|received|©).*$', '', doi,
                 flags=re.IGNORECASE)
    return doi.strip().rstrip('.,);:]>')


CREDENTIALS = re.compile(
    r'\b(?:MD|PhD|DO|MBBS|MBChB|MSc|MS|MPH|MBA|BSc|BS|BA|RN|NP|PA-C|CRNP|CNL|'
    r'FACS|FRCS|FRCR|FACC|FAHA|FESC|FEBVS|DPhil|PharmD|DrPH|ScD|DPM|MHS|RPVI)\b')


def clean_authors(text):
    """Clean a medical author byline; collapse affiliation markers to ';'."""
    text = re.sub(r'\*\*?|__?', '', text)
    text = re.sub(r'\s*,?\s*(?:\[[^\]]{1,8}\])+', '; ', text)
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r';\s*;', '; ', text)
    return text.strip(' ;,*_-')


def extract_title(md_text):
    """Extract a paper title from markdown text (fallback path)."""
    for m in re.finditer(r'^#{1,3}\s+(.+)$', md_text, re.MULTILINE):
        title = m.group(1).strip()
        if 10 < len(title) < 300 and '==>' not in title:
            return clean_field(title)
    for m in re.finditer(r'\*\*(.{10,200}?)\*\*', md_text):
        candidate = m.group(1).strip()
        if '==>' not in candidate and not is_junk_line(candidate):
            return clean_field(candidate)
    for line in md_text.split('\n'):
        line = line.strip()
        if len(line) > 15 and not is_junk_line(line) and '==>' not in line:
            return clean_field(line[:200])
    return "Unknown Title"


def extract_authors(md_text, title):
    """Extract authors from a credential-bearing byline, else positional guess."""
    lines_all = md_text.split('\n')
    for i, raw in enumerate(lines_all[:60]):
        s = raw.strip().strip('#*_ ').strip()
        if not s or is_junk_line(s) or '==>' in s or '@' in s:
            continue
        if re.match(r'^(abstract|summary|introduction|background)\b', s, re.IGNORECASE):
            break
        if CREDENTIALS.search(s) and len(s) < 400:
            block = s
            if i + 1 < len(lines_all):
                nxt = lines_all[i + 1].strip().strip('#*_ ').strip()
                if nxt and CREDENTIALS.search(nxt) and len(nxt) < 400:
                    block += ' ' + nxt
            authors = clean_authors(block)
            if len(authors) > 4:
                return authors

    lines = md_text.split('\n')
    title_idx = None
    title_clean = re.sub(r'[#*_\s]+', ' ', title).strip().lower()
    for i, line in enumerate(lines):
        line_clean = re.sub(r'[#*_\s]+', ' ', line).strip().lower()
        if title_clean and title_clean in line_clean:
            title_idx = i
            break
    if title_idx is None:
        return "Unknown"

    candidates = []
    for line in lines[title_idx + 1: title_idx + 8]:
        line = line.strip().strip('*').strip('_').strip()
        if not line or is_junk_line(line) or '==>' in line:
            continue
        if re.match(r'^(abstract|introduction|#{1,3}\s)', line, re.IGNORECASE):
            break
        if re.match(r'^\d{4}', line):
            break
        if '@' in line and ',' not in line:
            continue
        if 5 < len(line) <= 200:
            candidates.append(clean_field(line))
        if len(candidates) >= 3:
            break
    return '; '.join(candidates) if candidates else "Unknown"


def extract_year(md_text, filename):
    """Extract publication year from filename or text."""
    m = re.search(r'(19|20)\d{2}', filename)
    if m:
        return m.group(0)
    years = re.findall(r'((?:19|20)\d{2})', md_text[:2000])
    valid = [y for y in years if 1900 <= int(y) <= 2030]
    return max(valid) if valid else "Unknown"


# ---- Structured abstract (Tier 3: preserve labels) ------------------------

_ABS_LABELS = (r'Background|Objectives?|Purpose|Aims?|Introduction|Importance|'
               r'Methods|Materials and Methods|Design|Setting|Participants|'
               r'Results|Findings|Main Outcomes?(?: and Measures)?|'
               r'Conclusions?|Interpretation')


_MONTHS = (r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
           r'Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|'
           r'Dec(?:ember)?)')


def _strip_article_history(text):
    """Remove Elsevier 'Article history' column bleed (Received/Accepted/dates)
    that gets interleaved into two-column abstracts during extraction."""
    text = re.sub(r'\b(?:Received(?: in revised form)?|Accepted|Available online)\b',
                  ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'\b\d{1,2}\s+' + _MONTHS + r'\s+\d{4}\b', ' ', text)
    return re.sub(r'\s{2,}', ' ', text).strip()


def extract_abstract(md_text):
    """Extract the abstract, preserving structured labels on their own lines.

    Layouts, in priority order: explicit 'Abstract'/'Summary' heading; inline
    'Abstract:' label; headerless structured abstract (Background/…); the Elsevier
    two-column 'article info | Article history:' layout where the abstract's
    labels bleed inline; and the BMJ Open layout where the abstract sits under
    'Introduction' after a 'Strengths and limitations of this study' box. The
    last two are additive fallbacks — they run only when the first three fail,
    so papers that already parse are unaffected.
    """
    head = re.sub(r'A\s+B\s+S\s+T\s+R\s+A\s+C\s+T', 'Abstract',
                  md_text[:9000], flags=re.IGNORECASE)
    patterns = [
        ('heading',
         r'(?:^|\n)\s*#{0,3}\s*\**\s*(?:Abstract|Summary)\s*\**\s*[:.\-—]?\s*\n+(.*?)'
         r'(?=\n\s*#{1,3}\s|\n\s*\**\s*(?:Introduction|Keywords|©)\b|\n\s*\**\s*\d+\.\s)'),
        ('inline',
         r'(?:^|\n)\s*\**\s*(?:Abstract|Summary)\s*\**\s*[-—:.]\s*(.*?)'
         r'(?=\n\s*\**\s*(?:Introduction|Keywords|©)\b|\n\s*\**\s*\d+\.\s)'),
        ('structured',
         r'(?:^|\n)((?:\**\s*)?(?:Background|Objectives?|Purpose|Aims?|Importance)\s*\**\s*[:.\-—].*?)'
         r'(?=\n\s*#{1,3}\s|\n\s*\**\s*Keywords|©|\Z)'),
        # Elsevier two-column layout: labels bleed inline after 'Article history:'
        ('elsevier',
         r'Article\s+history:.*?((?:Background|Objectives?|Purpose|Aims?|Importance)\s*[:.\-—].*?)'
         r'(?=\n\s*\**\s*(?:Keywords|©|Crown Copyright)|\n\s*#{1,3}\s|\Z)'),
        # BMJ Open: abstract under 'Introduction' after a 'Strengths and limitations' box
        ('bmj',
         r'Strengths and limitations of this study\s*\**\s*\n+\s*\**\s*Introduction\**\s*(.*?)'
         r'(?=\n\s*#{1,3}\s|\Z)'),
    ]
    for name, pattern in patterns:
        m = re.search(pattern, head, re.IGNORECASE | re.DOTALL)
        if m:
            abstract = re.sub(r'\s+', ' ', m.group(1)).strip()
            if name == 'elsevier':
                abstract = _strip_article_history(abstract)
            if len(abstract) > 60:
                return structure_abstract(abstract[:3200])
    return ""


def structure_abstract(abstract):
    """Put each structured-abstract label (Methods:, Results:, ...) on its own
    line so sections are individually addressable. No-op for unstructured ones."""
    labelled = re.sub(r'\s*(?<![A-Za-z])(' + _ABS_LABELS + r')\s*[:.\-—]\s+',
                      lambda m: '\n**' + m.group(1).strip() + ':** ',
                      abstract)
    return labelled.strip()


def abstract_oneline(abstract):
    """Collapse a (possibly structured) abstract to one line for the index blurb."""
    return re.sub(r'\s+', ' ', abstract.replace('**', '')).strip()


# ---- Keywords -------------------------------------------------------------

def extract_keywords(md_text):
    """Extract author keywords if present."""
    m = re.search(r'(?:^|\n)\s*\**Key\s?words?\**[\s:]+(.+?)'
                  r'(?:\n\n|\n\s*\**(?:Introduction|Abstract|1[\.\s]))',
                  md_text[:6000], re.IGNORECASE | re.DOTALL)
    if m:
        kw = re.sub(r'\s+', ' ', m.group(1).strip())
        kw = kw.strip(' *:;–-')  # drop leading bold/label artefacts
        return kw[:500]
    return ""


# ---- Medical structured metadata (Tier 2) ---------------------------------

# Study-design detection, checked in priority order; first hit wins.
_DESIGN_RULES = [
    ('Study protocol',
     r'protocol for a systematic review|study protocol|protocol registration|'
     r'crd4?2\d{2,}'),
    ('Guideline/Consensus',
     r'consensus statement|clinical practice guideline|practice guidelines?|'
     r'society (?:for|of).{0,40}(?:recommend|guideline)|delphi'),
    ('Systematic review/Meta-analysis',
     r'systematic review and meta-?analys|meta-?analysis and systematic review'),
    ('Meta-analysis', r'meta-?analys[ie]s'),
    ('Systematic review',
     r'systematic review|cochrane database of systematic reviews|cochrane review'),
    ('Scoping review', r'scoping review'),
    ('Randomized controlled trial',
     r'randomi[sz]ed(?: controlled| clinical)? trial|'
     r'\brct\b|double-?blind|randomly assigned|placebo-?controlled'),
    ('Case-control study', r'case-?control'),
    ('Cross-sectional study', r'cross-?sectional'),
    ('Cohort study',
     r'retrospective (?:cohort|review|analysis|study)|prospective cohort|'
     r'\bcohort\b|retrospectively (?:review|analy|identif)|'
     r'observational (?:cohort|study)'),
    ('Case series/report', r'case series|case report'),
    ('Quality improvement/Implementation',
     # Avoid matching "National Surgical Quality Improvement Program" (NSQIP).
     r'(?<!surgical )quality improvement(?! program)|'
     r'implementation (?:of|project|science)|'
     r'plan,? do,? check|care redesign|pdsa'),
    ('Editorial/Commentary',
     r'\beditorial\b|commentary|viewpoint|the jama forum|perspective\b'),
    ('Narrative/Comprehensive review',
     r'narrative review|comprehensive review|state of the art|'
     r'this review (?:sought|aim)|we review'),
]


# Named registry / administrative databases. Their presence — especially in the
# title — is a strong signal for a retrospective COHORT study (not QI/other).
_REGISTRY_DB = [
    ('NSQIP', r'\bnsqip\b|national surg(?:ical|ery) quality improvement'),
    ('VQI', r'\bvqi\b|vascular quality initiative|vascular implant surveillance'),
    ('Medicare/CMS', r'\bmedicare\b|centers for medicare'),
    ('Cerner Health Facts', r'cerner|health facts'),
    ('NIS', r'nationwide inpatient sample|national inpatient sample'),
    ('SEER', r'\bseer\b'),
    ('State inpatient DB', r'state inpatient database'),
]


def _has_registry_db(text):
    t = (text or '').lower()
    return any(re.search(pat, t) for _, pat in _REGISTRY_DB)


def classify_design(title, abstract, body='', journal=''):
    """Classify study design; returns a canonical label.

    Rules run on title + journal + (abstract when present, else a body slice).
    The journal is included so publications like the 'Cochrane Database of
    Systematic Reviews' are recognised even when the article title omits the word
    'review'. A named administrative/registry database (NSQIP, VQI, Medicare, ...)
    is a strong signal for a retrospective cohort study:
      * in the TITLE -> reclassify an unclassified or QI-labelled paper as cohort
        (per the rule that a data source in the title implies a cohort, not QI);
      * in a REAL structured abstract -> upgrade only the 'Observational/Other'
        default. It is deliberately NOT applied to the body fallback, so an
        editorial/commentary that merely discusses an NSQIP study is not upgraded.
    """
    # Curator convention: an explicit uppercase 'EDITORIAL' tag in the title
    # (appended to the titles of editorial/commentary articles) is authoritative.
    if 'EDITORIAL' in title:
        return 'Editorial/Commentary'
    text = abstract if abstract else body
    blob = (title + ' \n ' + journal + ' \n ' + text).lower()
    label = None
    for lab, pat in _DESIGN_RULES:
        if re.search(pat, blob):
            label = lab
            break
    if label is None:
        label = ('Narrative/Comprehensive review'
                 if re.search(r'\breview\b', title.lower())
                 else 'Observational/Other')
    if label in ('Observational/Other', 'Quality improvement/Implementation'):
        if _has_registry_db(title):
            label = 'Cohort study'
        elif label == 'Observational/Other' and abstract and _has_registry_db(abstract):
            label = 'Cohort study'
    return label


def extract_sample_size(abstract, body):
    """Best-effort cohort size. Prefers 'n = X' / 'N = X' / 'total of X patients'."""
    text = (abstract + ' \n ' + body[:4000])
    cands = []
    for m in re.finditer(r'\b[nN]\s*=\s*([\d][\d,]{0,8})', text):
        cands.append(int(m.group(1).replace(',', '')))
    for m in re.finditer(r'(?:total of|included|identified|enrolled|comprising|'
                         r'analy[sz]ed)\s+([\d][\d,]{2,8})\s+(?:patients|'
                         r'participants|admissions|procedures|cases|operations)',
                         text, re.IGNORECASE):
        cands.append(int(m.group(1).replace(',', '')))
    cands = [c for c in cands if 5 <= c <= 50_000_000]
    if not cands:
        return ''
    return str(max(cands))  # study-level total is usually the largest figure


_DATA_SOURCES = [
    ('NSQIP', r'\bnsqip\b|national surg(?:ical|ery) quality improvement'),
    ('VQI', r'\bvqi\b|vascular quality initiative|vascular implant surveillance'),
    ('Medicare/CMS', r'\bmedicare\b|\bcms\b|centers for medicare'),
    ('Cerner Health Facts', r'cerner|health facts'),
    ('NIS', r'nationwide inpatient sample|national inpatient sample|\bnis\b'),
    ('SEER', r'\bseer\b'),
    ('Cochrane', r'cochrane'),
    ('State inpatient DB', r'state inpatient database'),
    ('Single-center', r'single[- ]center|single[- ]centre|single institution|'
                      r'our institution|academic medical center|tertiary'),
]


def extract_data_sources(abstract, body):
    """Detect named data sources / registries used by the study."""
    text = (abstract + ' \n ' + body[:5000]).lower()
    found = []
    for name, pat in _DATA_SOURCES:
        if re.search(pat, text):
            found.append(name)
    # De-dup, keep first-listed priority order, cap.
    seen, out = set(), []
    for x in found:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out[:3]


def extract_registration(abstract, body):
    """Extract ClinicalTrials.gov (NCT) or PROSPERO (CRD) registration IDs."""
    text = abstract + ' \n ' + body[:6000]
    ids = []
    ids += re.findall(r'\bNCT0\d{7}\b', text)
    ids += ['PROSPERO:' + x for x in re.findall(r'\bCRD4?2\d{6,}\b', text)]
    seen, out = set(), []
    for x in ids:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out[:3]


# ---------------------------------------------------------------------------
# Obsidian tags (MeSH-style; no JEL)
# ---------------------------------------------------------------------------

def slugify_tag(text):
    text = text.strip().lower()
    text = re.sub(r'[^\w\s/-]', '', text)
    text = re.sub(r'[\s_]+', '-', text)
    text = re.sub(r'-{2,}', '-', text).strip('-/')
    if not text or text.isdigit():
        return ""
    return text


def build_tags(keywords, design, data_sources):
    """Build Obsidian tags from keywords + study design + data sources."""
    tags = []
    if keywords:
        for kw in re.split(r'[;,]', keywords):
            t = slugify_tag(kw)
            if t and len(t) >= 2:
                tags.append(t)
    if design:
        t = slugify_tag(design.split('/')[0])
        if t:
            tags.append('design/' + t)
    for ds in data_sources:
        t = slugify_tag(ds)
        if t:
            tags.append('source/' + t)
    seen, out = set(), []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:20]


# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------

def clean_markdown(md_text):
    """Clean pymupdf4llm output for token efficiency."""
    lines = md_text.split('\n')
    lines = [l for l in lines if not is_junk_line(l)]
    lines = [l for l in lines if '==>' not in l]

    if len(lines) > 50:
        freq = {}
        for line in lines:
            stripped = line.strip()
            if 3 < len(stripped) < 80:
                freq[stripped] = freq.get(stripped, 0) + 1
        repeated = {k for k, v in freq.items() if v >= 3}
        lines = [l for l in lines if l.strip() not in repeated]

    lines = [l for l in lines if not re.match(r'^\s*-?\s*\d{1,4}\s*-?\s*$', l)]

    cleaned, blank = [], 0
    for line in lines:
        if line.strip() == '':
            blank += 1
            if blank <= 2:
                cleaned.append(line)
        else:
            blank = 0
            cleaned.append(line)
    return '\n'.join(cleaned)


def split_references(md_text):
    """Split off the references section. Returns (body, references_or_empty)."""
    patterns = [
        r'\n\s*#{0,3}\s*\**\s*References\s*\**\s*\n',
        r'\n\s*#{0,3}\s*\**\s*Bibliography\s*\**\s*\n',
        r'\n\s*\**\s*REFERENCES\s*\**\s*\n',
    ]
    for pattern in patterns:
        m = re.search(pattern, md_text)
        if m:
            return md_text[:m.start()].rstrip(), md_text[m.start():].strip()
    return md_text, ""


# ---------------------------------------------------------------------------
# Core conversion
# ---------------------------------------------------------------------------

def convert_one_pdf(pdf_path, output_path, refs_dir=None, references_mode='separate',
                    prefer_pdf_title=False, ocr_mode='auto'):
    """Convert a single PDF to cleaned markdown + YAML frontmatter.

    references_mode: 'separate' (default, write to refs_dir), 'inline' (keep in
    body), or 'drop' (discard).
    prefer_pdf_title: if True, trust the PDF's embedded metadata title ahead of
    the filename (useful for older, badly-named files).
    ocr_mode: 'auto' (OCR only PDFs without an adequate text layer — the fast
    default), 'off' (never OCR), or 'force' (always OCR).
    Returns a metadata dict for the index, or raises on failure.
    """
    import pymupdf4llm

    if ocr_mode == 'force':
        use_ocr = True
    elif ocr_mode == 'off':
        use_ocr = False
    else:  # 'auto'
        use_ocr = _pdf_needs_ocr(pdf_path)
    try:
        raw = pymupdf4llm.to_markdown(pdf_path, use_ocr=use_ocr)
    except TypeError:
        # Older/classic pymupdf4llm engine has no `use_ocr` kwarg (and does not
        # OCR on its own) — fall back to the plain call.
        raw = pymupdf4llm.to_markdown(pdf_path)
    md_text = repair_text(raw)
    original_filename = os.path.basename(pdf_path)

    # --- Title / authors / DOI: filename -> embedded PDF metadata -> scrape ---
    fname_meta = parse_filename_meta(original_filename)
    emb_title, emb_author, emb_doi, page1 = pdf_embedded_meta(pdf_path)

    if prefer_pdf_title:
        title = emb_title or fname_meta.get('title') or extract_title(md_text)
    else:
        title = fname_meta.get('title') or emb_title or extract_title(md_text)
    # Prefer the embedded title when it is essentially the same as the filename
    # title but better formed — repairs dropped-ligature gaps that live in the
    # filename itself (e.g. "justi ed" -> "justified", "signi cant" -> "significant",
    # "Limb- Threatening" -> "Limb-Threatening").
    if (not prefer_pdf_title and emb_title and fname_meta.get('title')
            and emb_title != title
            and difflib.SequenceMatcher(None, emb_title.lower(),
                                        title.lower()).ratio() >= 0.90):
        title = emb_title
    journal = fname_meta.get('journal', '')
    year = fname_meta.get('year') or extract_year(md_text, original_filename)
    doi = emb_doi or find_doi(page1) or find_doi(md_text[:6000])

    authors = extract_authors(md_text, title)
    if authors in ('Unknown', '') and emb_author:
        authors = emb_author

    abstract = extract_abstract(md_text)
    keywords = extract_keywords(md_text)

    # Clean body + handle references first (design/N read the cleaned body).
    cleaned = clean_markdown(md_text)
    body, refs = split_references(cleaned)

    # --- Medical structured metadata ---
    design = classify_design(title, abstract, body[:1500], journal)
    sample_size = extract_sample_size(abstract, body)
    data_sources = extract_data_sources(abstract, body)
    registration = extract_registration(abstract, body)
    tags = build_tags(keywords, design, data_sources)

    # References handling
    refs_file = ''
    if references_mode == 'inline':
        body = cleaned
    elif references_mode == 'separate' and refs and refs_dir:
        os.makedirs(refs_dir, exist_ok=True)
        refs_file = os.path.basename(output_path)
        with open(os.path.join(refs_dir, refs_file), 'w', encoding='utf-8') as rf:
            rf.write(f'# References — {title}\n\n{refs}\n')
    # 'drop' -> body already excludes refs; nothing written.

    # --- Frontmatter ---
    fm = ['---',
          f'title: {yaml_dq(title)}',
          f'authors: {yaml_dq(authors)}',
          f'year: {yaml_dq(year)}']
    if journal:
        fm.append(f'journal: {yaml_dq(journal)}')
    if doi:
        fm.append(f'doi: {yaml_dq(doi)}')
    fm.append(f'study_type: {yaml_dq(design)}')
    if sample_size:
        fm.append(f'sample_size: {yaml_dq(sample_size)}')
    if data_sources:
        fm.append('data_source: [' + ', '.join(yaml_dq(d) for d in data_sources) + ']')
    if registration:
        fm.append('registration: [' + ', '.join(yaml_dq(r) for r in registration) + ']')
    if keywords:
        fm.append(f'keywords: {yaml_dq(keywords)}')
    if tags:
        fm.append(f'tags: [{", ".join(tags)}]')
    fm.append(f'source_pdf: {yaml_dq(original_filename)}')
    if refs_file:
        fm.append(f'references_file: {yaml_dq("references/" + refs_file)}')
    fm.append('---')
    fm.append('')

    if doi:
        fm.append(f'**DOI:** [{doi}](https://doi.org/{doi})')
        fm.append('')
    if abstract:
        fm.append('## Abstract')
        fm.append('')
        fm.append(abstract)
        fm.append('')
        fm.append('---')
        fm.append('')

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(fm) + '\n' + body)

    return {
        'title': title, 'authors': authors, 'year': year, 'journal': journal,
        'doi': doi, 'study_type': design, 'sample_size': sample_size,
        'data_source': data_sources, 'registration': registration,
        'abstract': abstract_oneline(abstract)[:800],
        'keywords': keywords, 'tags': tags,
        'source_pdf': original_filename,
        'md_filename': os.path.basename(output_path),
        'references_file': ('references/' + refs_file) if refs_file else '',
        'body_tokens_approx': len(body.split()),
    }


# ---------------------------------------------------------------------------
# Duplicate detection (Tier 1)
# ---------------------------------------------------------------------------

def _title_key(title):
    words = re.sub(r'[^\w\s]', '', title.lower()).split()
    return ' '.join(words[:8])


def find_duplicates(metadata_list):
    """Group papers that share a DOI, or the same year + first-8-word title.
    Returns a list of duplicate groups (each a list of md_filenames, len>=2)."""
    by_doi, by_title = {}, {}
    for m in metadata_list:
        if m.get('doi'):
            by_doi.setdefault(m['doi'].lower(), []).append(m['md_filename'])
        key = (m.get('year', ''), _title_key(m.get('title', '')))
        if key[1]:
            by_title.setdefault(key, []).append(m['md_filename'])
    groups, seen = [], set()
    for grp in list(by_doi.values()) + list(by_title.values()):
        uniq = sorted(set(grp))
        if len(uniq) >= 2:
            sig = tuple(uniq)
            if sig not in seen:
                seen.add(sig)
                groups.append(uniq)
    return groups


# ---------------------------------------------------------------------------
# Index generation
# ---------------------------------------------------------------------------

# Evidence-hierarchy display order for grouping index.md.
_DESIGN_ORDER = [
    'Guideline/Consensus',
    'Systematic review/Meta-analysis',
    'Meta-analysis',
    'Systematic review',
    'Scoping review',
    'Narrative/Comprehensive review',
    'Randomized controlled trial',
    'Cohort study',
    'Case-control study',
    'Cross-sectional study',
    'Case series/report',
    'Quality improvement/Implementation',
    'Study protocol',
    'Editorial/Commentary',
    'Observational/Other',
]


def _dup_lookup(dupe_groups):
    mp = {}
    for i, grp in enumerate(dupe_groups, 1):
        for fn in grp:
            mp[fn] = i
    return mp


def build_index(metadata_list, output_path, dupe_groups):
    """Build index.md, grouped by study design, evidence-hierarchy order."""
    n = len(metadata_list)
    dup_of = _dup_lookup(dupe_groups)
    lines = [
        '# Literature Wiki — Index (Medical)',
        '',
        f'This index covers **{n} papers**, grouped by study design. Each entry '
        'has structured metadata, the abstract, and a DOI link where found.',
        '',
        '**How to use this wiki:**',
        '- Read this index to see what each paper covers and its evidence level',
        '- `index.csv` / `index.json` hold the same data as an evidence table',
        '- Grep across `papers/` for specific terms; read a paper file for full detail',
        '- Open the folder as an Obsidian vault: papers link via [[wikilinks]] and '
        'carry `design/…` and `source/…` tags',
        '',
    ]

    if dupe_groups:
        lines.append(f'> ⚠️ **{len(dupe_groups)} possible duplicate group(s) '
                     'detected** — see the ⚠️DUP-n markers below. You likely only '
                     'need to read one paper from each group.')
        lines.append('')

    lines.append('---')
    lines.append('')

    by_design = {}
    for m in metadata_list:
        by_design.setdefault(m.get('study_type', 'Observational/Other'), []).append(m)

    ordered = [d for d in _DESIGN_ORDER if d in by_design]
    ordered += [d for d in by_design if d not in _DESIGN_ORDER]

    for design in ordered:
        group = sorted(by_design[design], key=lambda m: (m['year'], m['title']))
        lines.append(f'# {design}  ({len(group)})')
        lines.append('')
        for m in group:
            dmark = f' ⚠️DUP-{dup_of[m["md_filename"]]}' if m['md_filename'] in dup_of else ''
            lines.append(f'## {m["title"]}{dmark}')
            lines.append('')
            lines.append(f'**Authors:** {m["authors"]}  ')
            meta_line = f'**Year:** {m["year"]}'
            if m.get('journal'):
                meta_line += f' · **Journal:** {m["journal"]}'
            extras = []
            if m.get('sample_size'):
                extras.append(f'N={m["sample_size"]}')
            if m.get('data_source'):
                extras.append('Source: ' + ', '.join(m['data_source']))
            if extras:
                meta_line += ' · ' + ' · '.join(extras)
            lines.append(meta_line + '  ')
            if m.get('doi'):
                lines.append(f'**DOI:** [{m["doi"]}](https://doi.org/{m["doi"]})  ')
            if m.get('registration'):
                lines.append('**Registration:** ' + ', '.join(m['registration']) + '  ')
            if m.get('keywords'):
                lines.append(f'**Keywords:** {m["keywords"]}  ')
            if m.get('tags'):
                lines.append('**Tags:** ' + ' '.join(f'#{t}' for t in m['tags']) + '  ')
            note = m['md_filename'][:-3] if m['md_filename'].endswith('.md') else m['md_filename']
            lines.append(f'**Full text:** [[{note}]]  ')
            lines.append('')
            if m['abstract']:
                lines.append(f'> {m["abstract"]}')
                lines.append('')
            lines.append('---')
            lines.append('')

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return output_path


def build_evidence_table(metadata_list, csv_path, json_path, dupe_groups):
    """Write index.csv + index.json — a ready-made evidence-table skeleton."""
    dup_of = _dup_lookup(dupe_groups)
    cols = ['year', 'study_type', 'title', 'journal', 'sample_size',
            'data_source', 'doi', 'registration', 'authors', 'keywords',
            'md_filename', 'duplicate_group']
    rows = []
    for m in sorted(metadata_list, key=lambda x: (x.get('study_type', ''), x['year'], x['title'])):
        rows.append({
            'year': m.get('year', ''),
            'study_type': m.get('study_type', ''),
            'title': m.get('title', ''),
            'journal': m.get('journal', ''),
            'sample_size': m.get('sample_size', ''),
            'data_source': '; '.join(m.get('data_source', [])),
            'doi': m.get('doi', ''),
            'registration': '; '.join(m.get('registration', [])),
            'authors': m.get('authors', ''),
            'keywords': m.get('keywords', ''),
            'md_filename': m.get('md_filename', ''),
            'duplicate_group': dup_of.get(m['md_filename'], ''),
        })
    with open(csv_path, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------

def load_state(state_path):
    if os.path.exists(state_path):
        with open(state_path, 'r') as f:
            return json.load(f)
    return {}


def save_state(state_path, state):
    with open(state_path, 'w') as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _state_key(pdf_path):
    """Stable, normalized state key (absolute path) so different path forms for
    the same file don't create duplicate/stale cache entries across environments."""
    return os.path.abspath(pdf_path)


def _done_metadata(pdf_files, state):
    """Metadata for the current inputs already converted, read from state."""
    out = []
    for p in pdf_files:
        entry = state.get(_state_key(p))
        if entry and entry.get('metadata'):
            out.append(entry['metadata'])
    return out


def _rebuild_outputs(pdf_files, state, wiki_dir):
    """Idempotent (re)build of index.md + evidence table from state. Because it
    reads from state, it reflects everything converted so far and works after a
    partial or resumed run."""
    done = _done_metadata(pdf_files, state)
    dupes = find_duplicates(done)
    index_path = os.path.join(wiki_dir, 'index.md')
    build_index(done, index_path, dupes)
    build_evidence_table(done, os.path.join(wiki_dir, 'index.csv'),
                         os.path.join(wiki_dir, 'index.json'), dupes)
    return done, dupes, index_path


def main():
    p = argparse.ArgumentParser(
        description='Convert medical/clinical PDFs to a structured markdown wiki.')
    p.add_argument('pdf_dir', help='Directory containing PDF files')
    p.add_argument('wiki_dir', help='Output wiki directory')
    p.add_argument('--recursive', '-r', action='store_true',
                   help='Scan PDF_DIR recursively for PDFs')
    p.add_argument('--keep-references', action='store_true',
                   help='Keep references inline in each paper file')
    p.add_argument('--drop-references', action='store_true',
                   help='Discard references entirely (default: save to references/)')
    p.add_argument('--prefer-pdf-title', action='store_true',
                   help="Trust the PDF's embedded metadata title over the filename "
                        "(useful for older, badly-named files)")
    p.add_argument('--force', action='store_true',
                   help='Re-convert all files, ignoring incremental state')
    p.add_argument('--ocr', choices=['auto', 'off', 'force'], default='auto',
                   help="OCR policy: 'auto' (default) OCRs only PDFs that lack an "
                        "adequate text layer (born-digital journal PDFs skip it, "
                        "~4-5x faster); 'off' never OCRs; 'force' always OCRs.")
    p.add_argument('--time-budget', type=float, default=None, metavar='SECONDS',
                   help='Stop STARTING new conversions once this many seconds have '
                        'elapsed, then exit 0 with a PENDING count. State is saved '
                        'per file, so re-run the same command until ALL_DONE. Use '
                        'this in time-capped sandboxes that cannot run in the '
                        'background.')
    p.add_argument('--index-only', action='store_true',
                   help='Do not convert; just rebuild index.md + evidence table '
                        'from the existing .wiki_state.json cache, then exit.')
    args = p.parse_args()

    # Preflight: required libraries (fail fast with an actionable message).
    try:
        import pymupdf4llm  # noqa: F401
        import fitz  # noqa: F401  (PyMuPDF)
    except Exception as e:
        print(f"ERROR: a required library is not importable ({e}).")
        print("  Install with:  pip install pymupdf4llm")
        print("  (sandboxes may need:  pip install pymupdf4llm --break-system-packages)")
        sys.exit(1)

    pdf_dir, wiki_dir = args.pdf_dir, args.wiki_dir
    if not os.path.isdir(pdf_dir):
        print(f"ERROR: PDF directory not found: {pdf_dir}")
        sys.exit(1)

    references_mode = 'separate'
    if args.keep_references:
        references_mode = 'inline'
    elif args.drop_references:
        references_mode = 'drop'

    papers_dir = os.path.join(wiki_dir, 'papers')
    refs_dir = os.path.join(wiki_dir, 'references')
    os.makedirs(papers_dir, exist_ok=True)

    if args.recursive:
        pdf_files = [os.path.abspath(os.path.join(root, f))
                     for root, _, files in os.walk(pdf_dir)
                     for f in files if f.lower().endswith('.pdf')]
    else:
        pdf_files = [os.path.abspath(os.path.join(pdf_dir, f))
                     for f in os.listdir(pdf_dir) if f.lower().endswith('.pdf')]
    if not pdf_files:
        print(f"No PDF files found in: {pdf_dir}")
        sys.exit(0)
    pdf_files.sort()

    state_path = os.path.join(wiki_dir, '.wiki_state.json')
    state = load_state(state_path) if not args.force else {}

    # --index-only: rebuild outputs from the cache and stop.
    if args.index_only:
        done, _, index_path = _rebuild_outputs(pdf_files, state, wiki_dir)
        print(f"Rebuilt outputs from cache: {len(done)}/{len(pdf_files)} inputs present.")
        print(f"  Index: {index_path}  (+ index.csv / index.json)")
        return

    print(f"Found {len(pdf_files)} PDF files.\n")

    start = time.monotonic()
    succeeded, skipped, failed, failed_keys = [], [], [], set()
    budget_hit = False

    for i, pdf_path in enumerate(pdf_files, 1):
        pdf_filename = os.path.basename(pdf_path)
        key = _state_key(pdf_path)
        current_hash = file_hash(pdf_path)
        cached = state.get(key)
        if (cached and cached.get('hash') == current_hash and not args.force
                and cached.get('metadata')):
            skipped.append(pdf_filename)
            print(f"  [{i}/{len(pdf_files)}] SKIP (unchanged): {pdf_filename}")
            continue

        # Time budget: stop before STARTING a new conversion once spent. Cached
        # files above are cheap and keep advancing; only new work is gated.
        if (args.time_budget is not None
                and time.monotonic() - start >= args.time_budget):
            budget_hit = True
            print(f"  [{i}/{len(pdf_files)}] time budget reached "
                  f"({args.time_budget:g}s) — deferring remaining files")
            break

        md_filename = sanitise_filename(pdf_filename)
        output_path = os.path.join(papers_dir, md_filename)
        try:
            meta = convert_one_pdf(pdf_path, output_path, refs_dir, references_mode,
                                   prefer_pdf_title=args.prefer_pdf_title,
                                   ocr_mode=args.ocr)
            if os.path.getsize(output_path) < 500:
                print(f"  [{i}/{len(pdf_files)}] WARNING (possible scan, little text): {pdf_filename}")
            else:
                print(f"  [{i}/{len(pdf_files)}] OK [{meta['study_type']}]: {pdf_filename}")
            succeeded.append(pdf_filename)
            state[key] = {'hash': current_hash, 'metadata': meta,
                          'source_pdf': pdf_filename}
            save_state(state_path, state)          # persist after EVERY file
        except Exception as e:
            print(f"  [{i}/{len(pdf_files)}] FAILED: {pdf_filename} | Error: {e}")
            failed.append((pdf_filename, str(e)))
            failed_keys.add(key)

    save_state(state_path, state)

    # Build outputs from state — reflects everything converted so far.
    done, dupe_groups, index_path = _rebuild_outputs(pdf_files, state, wiki_dir)

    done_keys = {_state_key(p) for p in pdf_files
                 if state.get(_state_key(p), {}).get('metadata')}
    pending = [os.path.basename(p) for p in pdf_files
               if _state_key(p) not in done_keys and _state_key(p) not in failed_keys]

    print(f"\n{'='*50}")
    print("  Conversion pass complete")
    print(f"{'='*50}")
    print(f"  Converted this pass: {len(succeeded)}")
    print(f"  Skipped (cached):    {len(skipped)}")
    print(f"  Failed:              {len(failed)}")
    print(f"  In index (total):    {len(done)}/{len(pdf_files)}")
    print(f"  Wiki:        {wiki_dir}")
    print(f"  Index:       {index_path}  (+ index.csv / index.json)")
    print(f"  References:  {references_mode}")

    counts = {}
    for m in done:
        counts[m.get('study_type', '?')] = counts.get(m.get('study_type', '?'), 0) + 1
    if counts:
        print("\n  Study designs:")
        for d in _DESIGN_ORDER:
            if d in counts:
                print(f"    {counts[d]:>3}  {d}")
        for d, c in counts.items():
            if d not in _DESIGN_ORDER:
                print(f"    {c:>3}  {d}")

    if dupe_groups:
        print(f"\n  ⚠️  {len(dupe_groups)} possible duplicate group(s):")
        for j, grp in enumerate(dupe_groups, 1):
            print(f"    DUP-{j}: " + '  |  '.join(grp))

    if failed:
        print("\n  Failed files:")
        for name, err in failed:
            print(f"    - {name}: {err}")

    total = len(done)
    pdf_est, idx_est = total * 12000, total * 450
    print("\n  Token estimate:")
    print(f"    Reading all PDFs directly:  ~{pdf_est:,} tokens")
    print(f"    Reading index.md only:      ~{idx_est:,} tokens")
    print(f"    Savings:                    ~{pdf_est - idx_est:,} tokens "
          f"({((pdf_est - idx_est)/max(pdf_est,1))*100:.0f}%)")

    # Resumability signal for supervisors in time-capped sandboxes.
    if pending:
        why = "time budget" if budget_hit else "not yet processed"
        print(f"\n  PENDING {len(pending)} ({why}) — re-run the same command to continue.")
        sys.exit(0)
    print("\n  ALL_DONE")


if __name__ == '__main__':
    main()
