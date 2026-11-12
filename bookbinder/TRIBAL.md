# Bookbinder — tribal knowledge

One sentence: **old EPUBs in, clean books out.** Chapters rebuilt from the book's own headings, junk pages cut, metadata filled, your cover, formatting that reads like a Books store purchase on iPhone and iPad.

URL: https://mitetython.github.io/Plane/bookbinder/ · one file, `index.html`, no build step, no CDN. The manifest is written at runtime as a data URL and the home-screen icon is inlined, so the folder holds nothing else the app needs.

## How it works

- **In:** EPUBs picked from Files, any number at once. Each is hashed (SHA-256, first 16 hex = book id), stored in IndexedDB as `src:<id>`, queued. Three books run at once (`CONF.workers`); the screen stays awake while they do.
- **Per book:** `read → parse → scrub → candidates → plan → sections → meta → cover → build → zip → verify → store`, every stage logged with in/out counts and ms.
  - parse: own zip reader, container → OPF (metadata, manifest, spine, cover), nav or NCX, CSS reduced to meaning (italic, bold, centred, right, small caps, size, indent, page break, hidden). Every spine file is flattened into one stream of blocks with inline markup rebuilt from scratch; link-target ids survive as `f<file>-<id>`.
  - scrub: mojibake, soft hyphens, page numbers (only when they fall mid-sentence), running heads (title/author lines repeated 3+ times), piracy watermarks (OceanofPDF and friends), spacer paragraphs, dinkuses → scene breaks, blockquote-for-the-whole-book, indents or centring applied to everything, OCR line breaks (merge mode only when >6% of paragraph pairs break mid-sentence), hyphen joins checked against the book's own vocabulary, curly quotes and em dashes when the book was typed straight, verse kept as verse.
  - candidates: every line where a section could begin (file starts, heading tags, TOC targets, page breaks, short styled lines, chapter words, the line after a heading).
  - plan: Claude picks section starts by candidate number, the kind of each section, its level, and the contents label built only from the heading's own words. Without a key, rules do it (file starts, heading levels per file, chapter words, filenames, copyright text).
  - sections: `CONF.policy` decides keep/cut per kind; the book sheet overrides any section.
  - meta: the OPF, the title and copyright pages, the opening, Google Books, Open Library; Claude reconciles (rules without a key). ISBNs checksum-verified.
  - cover: yours (`cov:<id>`) when set, else the file's own; re-encoded JPEG, long side ≤ 2560.
  - build: EPUB 3, one XHTML per section in `OEBPS/text/`, nav + NCX, landmarks (cover, contents, bodymatter = first chapter-ish section, which is where Books opens it), guide, `style.css`.
  - verify: re-open the zip, mimetype first and stored, every XHTML/OPF/NCX parses as XML, ids unique per file, every link and image resolves.
- **Out:** the book sheet's Share (Books imports, Files keeps), Save all → share in batches of 20, or ZIP all → share/download.

## Decisions

- 2026-11-12 — Started as a Mac batch script, pivoted on request to a phone web app: one file, books in and out through Files and the share sheet, no computer.
- Claude through structured outputs (`output_config.format`, json_schema), because Sonnet 5.5 refuses forced tool use with a 400. Haiku 5.5 is the default ($0.10 in / $0.50 out per MTok under 100k prompt); Sonnet 5.5 selectable. Effort: plan medium, metadata low; a 400 that names effort drops it and retries once. Browser calls carry `anthropic-dangerous-direct-browser-access: true`.
- Claude never writes the book's text. Its labels are checked word-for-word against the printed heading; a label with a word the heading doesn't have is replaced by the joined heading lines.
- Covers come from his own images, one per book or a batch matched by filename (title tokens ≥ 80% covered, ties refused). The cover page is just the picture in an SVG wrapper. No generated covers.
- Cut by default: the old cover page (rebuilt), half title, old contents page, index, also-by, praise, excerpts of other books, ads, newsletters, scanner credits, blanks. Everything else stays. Over 25% of the words cut → flag; over 50% → refuse to build until the cuts are set by hand in the sheet.
- Interior CSS leaves font, size, line spacing, justification and theme to Books. Book-style first-line indents, flush after headings, breaks, images and lists; scene breaks are a short rule in `currentColor` so night mode keeps them; hyphenation on (language is set).
- No CDN: zip read/write on `DecompressionStream`/`CompressionStream('deflate-raw')`, gzip framing as the fallback, CRC32 in JS.
- Diagnostics: global log (500 lines) + per-book log on the record; last 3 good raw exchanges per external stage (`base:<id>:<stage>`); a failure writes `dump:<id>:<time>` with stages, raw request/response, the matching baseline and the log tail. The row shows `[stage] expected …, got … · dump hh:mm:ss`; the sheet copies the dump.

## Gotchas

- iOS share needs the File built before the tap. Sheets preload the output; ZIP is two taps (pack, then share or download).
- The Claude key lives in localStorage on `mitetython.github.io`; every app on that origin can read it.
- DRM is refused by name (Adobe `rights.xml`, Apple `sinf.xml`, encrypted content), never attempted.
- A cleaned book gets a new identifier (UUID from the source hash). Delete the old copy in Books before importing, or there are two. Rebuilds of the same source keep the same identifier.
- `PARSER` must be bumped when extraction changes, or saved chapter maps get reused against different block numbers (rebuild checks block count, not content).
- Manifest or `apple-mobile-web-app-*` changes → delete the home-screen icon and re-add.

## Session log

**2026-11-12** — build 1. Whole app in one file: zip, parse, scrub, Claude/rules chapter map, metadata, covers by hand, EPUB 3 build + verify, books list, book sheet (cover, details, keep/cut per section, re-run, rules-only, log, dump), save sheet, settings floor (key, model, test, lookups, look, build, reload, storage, log, dump). Syntax-checked only; the first real run is on the phone. Where we left off: run a handful of ugly EPUBs, read the logs.
