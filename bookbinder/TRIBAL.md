# Bookbinder — tribal knowledge

One sentence: **old EPUBs and comics in, clean ones out.** Books: chapters rebuilt from the book's own headings, junk pages cut, metadata filled, your cover, formatting that reads like a Books store purchase on iPhone and iPad. Comics: uploader and ad pages cut, the database record found from the cover, barcode and indicia, a clean CBZ named and foldered for Panels with a full ComicInfo.xml inside.

URL: https://mitetython.github.io/Plane/bookbinder/ · one file, `index.html`, no build step, no CDN. The manifest is written at runtime as a data URL and the home-screen icon is inlined, so the folder holds nothing else the app needs. The RAR reader (node-unrar-js 2.0.2, MIT: the official unrar 6.1.7 source compiled to wasm) is inlined too: an esbuild IIFE that sets `window.UNRAR`, plus the wasm as base64 in `UNRAR_WASM_B64`, decoded on the first CBR. That's ~320 KB of the file.

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

## Comics (build 2)

- **In:** the same Add button. Files are sorted by their bytes, not their names: `Rar!` → CBR, a zip with `META-INF/container.xml` → EPUB, a zip of images → CBZ. CB7 and PDF are refused with a reason. The picker has no `accept` filter, because iOS greys out .cbz/.cbr it doesn't know.
- **Per comic:** `unpack → pages → classify → identify → match → cuts → meta → cover → build → zip → verify → store`.
  - unpack: CBZ through `zipOpenBlob` (reads only the directory and the entries asked for, so a 300 MB CBZ never sits in memory whole); CBR through the wasm unrar, all pages at once. `__MACOSX/`, dotfiles, Thumbs.db skipped; a ComicInfo.xml in the source is parsed as evidence, never trusted as the answer. Pages in natural order (`Intl.Collator` numeric).
  - pages: every page read once: real type from its magic bytes (jpeg, png, gif, webp, avif, heic, jxl, bmp, tiff), size from its header, a 132×198 thumbnail for the page grid (`pth:<id>`), a blank score (luminance spread on a 24×36 sample), and a numbered 300×450 cell on a 4×3 contact sheet.
  - classify: Claude reads the contact sheets (30 per call) and returns runs of page kinds: cover, story, chapter, credits, letters, text, extra, variant · cut: ad, preview, scanner, blank. Also the cover page, where the barcode is, which pages carry the indicia, right-to-left, format. "When it could be story, call it story." Rules without a key: blanks, plus scanner tags only when the file name breaks the archive's own naming pattern at the first two or last three pages.
  - identify: Claude reads the cover at 1568 px, the barcode corner enlarged up to 2×, and the indicia pages whole and with their bottom 40% enlarged. UPC check digit verified in code; a 5-digit add-on is issue/cover/printing and fills a missing issue number.
  - match: Claude with `web_search` + `web_fetch` restricted to comics.org, metron.cloud, comicvine.gamespot.com, leagueofcomicgeeks.com (3 searches, 2 fetches, 6k tokens a page). Server-side, so CORS and API keys don't apply. Answers as JSON in text (search results carry citations, which structured outputs refuse); unparseable text gets reshaped by a schema call. A record that disagrees with the comic on issue number, or on year by more than one, is set aside.
  - cuts: policy per kind, page grid overrides any page; the cover moves to the front if it was sorted elsewhere; over 50% cut refuses to build until set by hand.
  - meta: database > the cover read > the file's ComicInfo > the file name. Edits in the sheet store only what changed, so the rest keeps following the database.
  - build: flat CBZ, pages `001.ext…` stored (no recompression), originals untouched (Panels reads every format they come in), ComicInfo.xml last. Your cover, if set, replaces the cover page.
  - ComicInfo.xml: v2.0 schema, elements in XSD order (Title, Series, Number, Count, Volume, Summary, Notes, Year/Month/Day = cover date, creators, Publisher, Imprint, Web = the database page, PageCount, LanguageISO, Format, Manga, Characters, StoryArc, Pages with FrontCover/InnerCover/Roundup/Letters/Editorial/Other and DoublePage). UPC goes in Notes (GTIN is v2.1-only).
  - verify: page count, root-level pages in sort order, ComicInfo parses, PageCount and Page entries match, Series present, first page is an image.
- **Out:** file `Series 001 (Year).cbz` (TPBs `Series Vol. 01 (Year).cbz`), folder `Series (VolYear)`. Save sheet: share in batches (20 files / 400 MB), or ZIP by series (folders inside, 2 GB per ZIP: the writer has no zip64). Unzip in Files, move the folders into Panels.
- Two comics in flight at most; one over 150 MB runs alone.

## Decisions

- 2026-11-12 — Started as a Mac batch script, pivoted on request to a phone web app: one file, books in and out through Files and the share sheet, no computer.
- Claude through structured outputs (`output_config.format`, json_schema), because Sonnet 5.5 refuses forced tool use with a 400. Haiku 5.5 is the default ($0.10 in / $0.50 out per MTok under 100k prompt); Sonnet 5.5 selectable. Effort: plan medium, metadata low; a 400 that names effort drops it and retries once. Browser calls carry `anthropic-dangerous-direct-browser-access: true`.
- Claude never writes the book's text. Its labels are checked word-for-word against the printed heading; a label with a word the heading doesn't have is replaced by the joined heading lines.
- Covers come from his own images, one per book or a batch matched by filename (title tokens ≥ 80% covered, ties refused). The cover page is just the picture in an SVG wrapper. No generated covers.
- Cut by default: the old cover page (rebuilt), half title, old contents page, index, also-by, praise, excerpts of other books, ads, newsletters, scanner credits, blanks. Everything else stays. Over 25% of the words cut → flag; over 50% → refuse to build until the cuts are set by hand in the sheet.
- Interior CSS leaves font, size, line spacing, justification and theme to Books. Book-style first-line indents, flush after headings, breaks, images and lists; scene breaks are a short rule in `currentColor` so night mode keeps them; hyphenation on (language is set).
- No CDN: zip read/write on `DecompressionStream`/`CompressionStream('deflate-raw')`, gzip framing as the fallback, CRC32 in JS.
- Diagnostics: global log (500 lines) + per-book log on the record; last 3 good raw exchanges per external stage (`base:<id>:<stage>`); a failure writes `dump:<id>:<time>` with stages, raw request/response, the matching baseline and the log tail. The row shows `[stage] expected …, got … · dump hh:mm:ss`; the sheet copies the dump.
- 2026-11-12 (build 2) — Comics target **Panels 3**, not Books: output is CBZ. Panels 3 does not read metadata inside files (their forum, March 2026: "We don't support metadata right now. We plan to add support for it in the next major version."); it goes by folders (a folder is a collection) and file names (sort order; Continue Reading walks the folder). So names and folders carry the metadata today and ComicInfo.xml carries it for the next Panels and for Komga, Kavita, Chunky.
- Comic databases (ComicVine, Metron, GCD) can't be called from a phone browser (no CORS, keys or Basic auth). Claude's server-side web search + fetch reaches their public pages instead: no proxy, no extra accounts, about a cent per search.
- A fixed-layout EPUB was the first plan for comics; dropped when Panels became the target.

## Gotchas

- iOS share needs the File built before the tap. Sheets preload the output; ZIP is two taps (pack, then share or download).
- The Claude key lives in localStorage on `mitetython.github.io`; every app on that origin can read it.
- DRM is refused by name (Adobe `rights.xml`, Apple `sinf.xml`, encrypted content), never attempted.
- A cleaned book gets a new identifier (UUID from the source hash). Delete the old copy in Books before importing, or there are two. Rebuilds of the same source keep the same identifier.
- `PARSER` must be bumped when extraction changes, or saved chapter maps get reused against different block numbers (rebuild checks block count, not content).
- Manifest or `apple-mobile-web-app-*` changes → delete the home-screen icon and re-add.
- Comics: the database match needs web search and web fetch allowed for your Claude org (Console settings). Off → the match step warns and the comic is tagged from the cover read alone.
- `COMIC_PARSER` must be bumped when page reading changes: a rebuild reuses the saved page map when the page count matches.
- Safari may not know `application/vnd.comicbook+zip`: sharing retries the same bytes as `application/zip` (the .cbz name stays).
- The unrar wasm is one shared decoder: CBRs unpack one at a time behind a lock, and the decoder is re-pointed at the archive right before the synchronous read.
- ZIPs over 2 GB split into parts: the zip writer has no zip64.

## Session log

**2026-11-12** — build 1. Whole app in one file: zip, parse, scrub, Claude/rules chapter map, metadata, covers by hand, EPUB 3 build + verify, books list, book sheet (cover, details, keep/cut per section, re-run, rules-only, log, dump), save sheet, settings floor (key, model, test, lookups, look, build, reload, storage, log, dump). Syntax-checked only; the first real run is on the phone. Where we left off: run a handful of ugly EPUBs, read the logs.

**2026-11-12** — build 2. Comics: CBZ/CBR in (RAR via inlined wasm), contact-sheet page sorting by Claude, cover/barcode/indicia read, database match through Claude web search on GCD/Metron/ComicVine/LoCG, CBZ out with ComicInfo.xml, Panels-style names and series folders, page grid with keep/cut per page, comic details editor, ZIP by series. One Add button for books and comics; byte sniffing decides which. Syntax-checked only. Where we left off: run a stack of uploader CBRs and CBZs, read the logs, check the folders land as collections in Panels.
