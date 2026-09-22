# Atelier PDF

**Replace text in a PDF without moving anything else — and prove it.**

A PDF does not store text. It stores glyph codes placed at absolute coordinates,
drawn with fonts that are usually embedded as amputated subsets. That is why
"just edit the text" tools produce shifted lines, blank boxes and quietly broken
documents.

Atelier PDF rewrites the glyph codes where they are, in the original font, with
the original metrics — then renders both versions and compares them pixel by
pixel to show you exactly what changed and, more importantly, what did not.

<sub>Python · FastAPI · pikepdf · PyMuPDF · fontTools — no build step, no
JavaScript dependencies.</sub>

---

## What it does

**Find and replace, on batches of documents.** Drop several PDFs, write your
rules once, they apply to all of them. Download the results as one archive.

- **Literal search** with case sensitivity, whole-word, and tolerant matching of
  whitespace and punctuation variants — so `l'article` still finds `l’article`,
  and a query with a space still matches a name broken across two lines.
- **Regular expressions** with capture groups: `INV-(\d{4})-(\d+)` → `NO-$2/$1`.
- **Date detection** as a specialised finder: it recognises `15/03/2024`,
  `2024-03-15`, `March 15, 2024`, `1er mars 2020`, `31 décembre 2025`… works out
  the document's own day/month convention from its unambiguous dates, and
  **rewrites in the source format**. Shift every date by a year, or map date A to
  date B regardless of how each occurrence is written.

**Every replacement is inspected before it is applied.** Per match you see the
strategy, the width effect, the font involved, and whether it is refused and why.
You can override the target text, exclude a match, or force how its width is
handled.

**Nothing is produced silently.** If a font cannot render a character, the
replacement is refused with an explanation rather than emitting a blank box.

---

## How it stays faithful

### Four rewriting strategies

The engine always picks the highest one that applies, and tells you which.

| Strategy | When | Fidelity |
|---|---|---|
| `inplace` | same character count, unchanged advance width | each code is swapped **in its own operator**, not one kerning value is touched |
| `words` | the source separates words with kerning, not space glyphs (TeX) | each word rewritten in place; no space glyph is ever needed |
| `recomposed` | different length or width | rewritten as one block, width made up for (below) |
| `blocked` | glyph missing and unrepairable, font not editable | refused, with the reason |

Why `inplace` matters: in almost every text font the ten digits share one
advance width. Replacing a date digit for digit therefore has a width delta of
**0.000 pt** — the rendering is mathematically unchanged outside the glyphs
themselves.

### Three ways to handle width

Replacing `Jean Dupont` with `Christophe-Alexandre de La Tour` is 114 points
wider. There is no single right answer, so the engine measures the page and
decides — and lets you override per match.

| Mode | Effect |
|---|---|
| `exact` | kerning compensation; the rest of the line does not move by a point |
| `fit` | horizontal scaling (`Tz`) to occupy exactly the original width |
| `push` | natural width; the line grows and pushes what follows |

`auto` chooses from the real geometry: the distance to the **next glyph** on the
line, and the distance to the right edge of the text area. Negligible delta →
`exact`. Imperceptible stretch (±3 %) → `fit`. A wide gap after the text, so a
table cell or a column → preserve the zone. Running text → `push`, which is what
a word processor would do.

### Font repair

An embedded subset only contains the glyphs the document happened to use. Ask for
an `8` in a font that never needed one and you get a blank box.

Atelier PDF finds the original font on the system, extracts the missing glyphs,
scales them to the subset's units per em, and **grafts them into the embedded
font** — updating the glyph program, the width array, the CID-to-GID map and the
`ToUnicode` table. If the font is not on the server you can upload it. The result
is still the same font, just less amputated.

### Proof, not promise

After surgery both versions are rendered at 110 dpi, the regions that are
*supposed* to change are masked, and whatever differs outside is measured.

The mask is **computed** — the original box extended by the width delta, plus the
rest of the line when a replacement deliberately pushes it — never found by
searching for a string. Three verdicts rather than a boolean, because rendering
is not perfectly deterministic:

- `exact` — not one pixel changed outside the replaced regions;
- `negligible` — a handful of isolated antialiasing pixels, rasterisation noise;
- `drift` — content actually moved; the report says so and the Verify tab shows
  where, in red.

Pixel comparison has one structural blind spot: a **blank box appears exactly
where a change is expected**, so it can never be seen by comparing images. A
second, independent check reads the glyph id actually used for every character —
id 0 is `.notdef` by definition, and a positive id with a zero-width box means
the glyph does not exist. A single one forces the verdict to fail.

---

## Reference results

Run over the PDFs available on the development machine — technical manuals,
TeX output, invoices, a game manual — with both text and date replacements:

| Document | Pages | Replacement | Applied | Verdict |
|---|---|---|---|---|
| valgrind_manual.pdf | 339 | `Memcheck` → `MemTest` | 347 | exact, 0 px |
| hwloc-a4.pdf | 273 | `hwloc` → `topolib` | 4000 | negligible, 78 px |
| SystemTap_Guide.pdf | 96 | `SystemTap` → `TraceKit` | 492 | exact, 0 px |
| nettle.pdf (TeX) | 121 | `Nettle` → `Ortie` | 171 | exact, 0 px |
| bzip2 manual (TeX) | 38 | `bzip2` → `compressor-x` | 139 | exact, 0 px |
| WoW Manual.pdf | 114 | `Azeroth` → `Terra` | 27 | exact, 0 px |

Also covered: all four page rotations, invisible OCR layers, shared form
XObjects, offset CropBoxes, encrypted documents, digitally signed documents.
No blank boxes anywhere.

---

## Running it

Requires Python 3.9+ and the system fonts you want available for repair.

```bash
git clone https://github.com/Lomchat/atelierPDF.git
cd atelierPDF
python3 -m venv venv
./venv/bin/pip install -r backend/requirements.txt
cp .env.example .env
# Set ADMIN_USERNAME and ADMIN_PASSWORD in .env before starting.
chmod 600 .env
cd backend && ../venv/bin/uvicorn main:app --port 10560
```

Open `http://localhost:10560`. The app serves both the API and the static
frontend from a single port — there is no build step and no bundler.

Uploaded documents, renders and session state live in `data/`, which is
git-ignored and purged after three hours.

### Deployment

`deploy/` contains a systemd unit and an Apache virtual host used in production.
Adjust the paths and the host name.

```bash
sudo cp deploy/pdf.service /etc/systemd/system/
sudo systemctl enable --now pdf
```

---

## Public API and administration

Interactive Swagger documentation: **https://pdf.chalco.website/api/docs**,
also linked from the home page. No API key is required.

`POST /api/replace` takes one or more `files` and a JSON array of `equivalences`
in a multipart form. All equivalences apply to each original document; rules
are not chained. Text, regex and date rules use the same engine as the platform.

```bash
curl --fail-with-body https://pdf.chalco.website/api/replace \
  -F 'files=@invoice.pdf;type=application/pdf' \
  -F 'equivalences=[{"query":"2024","replace":"2025"},{"query":"Jean Dupont","replace":"Marc Dubois"}]' \
  -o invoice-modified.pdf
```

Repeat `-F 'files=@another.pdf;type=application/pdf'` for multiple files and save
the response as `.zip`. The archive includes all resulting PDFs and `report.json`.
A single input returns a PDF. Files with no matches are returned unchanged.
`X-Replacements-Applied` reports the replacement count; `X-Operation-Id` links
the response to the admin log. Outputs use an `_1` suffix and duplicate names
are disambiguated.

Optional form fields: `locale=fr|en`, `auto_repair=true`, `verify=true`, and
`date_rule` as JSON. For example, use `equivalences=[{"kind":"dates"}]` with
`date_rule={"type":"shift","years":1}` or
`date_rule={"type":"map","pairs":{"2024-03-15":"2025-03-15"}}`.

Limits: 24 files, 40 MiB per file, 160 MiB total, 800 pages per document,
24 equivalences, 4000 matches per document and 240 seconds of processing.
Invalid files, refused replacements or failed visual verification return a JSON
error for the whole request (400/413/422/504), with no partial download.
The API uses a temporary process and directory, both released before returning
the result; it creates no persistent PDF session.

**Administration:** https://pdf.chalco.website/admin uses a normal login form
over HTTPS and a secure HttpOnly session cookie, valid for eight hours. The
logout button revokes the session on the server. Sessions are shared by both
workers and survive restarts; changing the credentials invalidates them. Configure `ADMIN_USERNAME` and `ADMIN_PASSWORD` in the root `.env`
(ignored by Git, mode 600); restart the service after changing them. An example
without production credentials is provided in `.env.example`.

The paginated, searchable log records platform and API operations: timestamp,
client IP, origin, path, HTTP status, duration, filenames, sizes, submitted rules,
replacement results and errors. It stores metadata only, never PDF binaries,
page images or whole-document extracted text. Source labels are informational
and inferred from the client, not an authentication mechanism. Existing
`data/usage.log` entries are imported once as historical records.

The SQLite database `data/operations.sqlite3` persists across service restarts
and supports the two server workers. Both the admin page and its data endpoint
require authentication and send `Cache-Control: no-store`. The platform's
interactive PDFs and renders retain their existing three-hour expiration.

Run the integration tests with a separate test dependency install:

```bash
.venv/bin/pip install -r backend/requirements-dev.txt
.venv/bin/pytest backend/tests -q
```

## Session API

The unit of work is a **batch**: N documents sharing the same rules.

```
POST   /api/lots                                     multipart files[] → batch + analyses
POST   /api/lots/{id}/documents                      add more
DELETE /api/lots/{id}/documents/{doc}

POST   /api/lots/{id}/scan                           rules → matches per document
POST   /api/lots/{id}/simulate                       full result, writes nothing
POST   /api/lots/{id}/apply                          applies and verifies

GET    /api/lots/{id}/documents/{doc}/page/{n}.png   ?v=before|after&dpi=
GET    /api/lots/{id}/documents/{doc}/diff/{n}.png   verification image
GET    /api/lots/{id}/documents/{doc}/download
GET    /api/lots/{id}/download                       ZIP of modified documents, "_1" suffix

POST   /api/lots/{id}/fonts                          upload a source font for repair
```

A rule looks like this:

```json
{
  "finders": [
    {"id": "f1", "kind": "text",  "query": "Jean Dupont", "replace": "Marc Dubois",
     "case_sensitive": false, "whole_word": false, "loose": true},
    {"id": "f2", "kind": "regex", "query": "INV-(\\d{4})-(\\d+)", "replace": "NO-$2/$1"},
    {"id": "f3", "kind": "dates"}
  ],
  "date_rule": {"type": "shift", "years": 1},
  "per_doc": {"d0": {"overrides": {}, "fits": {}, "exclude": []}}
}
```

Removal is a **denylist**, never an allowlist: `exclude` holds stable identities
(page + content unit + offset + text). An allowlist computed by the client goes
stale the moment a rule changes, and would silently drop work.

---

## Known limits

Stated plainly, because the tool refuses rather than guesses:

- **Scanned pages with no text layer** — nothing is editable without OCR.
- **Invisible OCR layers** — editable, but the visible image does not change.
  Flagged.
- **Type1 / CFF font programs** — glyph injection is only implemented for
  TrueType (`FontFile2`). Replacements needing an absent glyph in a CFF font are
  refused, and uploading the source font does not help. Two ways out, not yet
  built: injecting CFF charstrings, or swapping the embedded program for the full
  source font (the PDF `/Widths` govern advance, so layout would be preserved).
- **Digital signatures** — any modification invalidates them, by design.
- **Encryption** — the document is rewritten in the clear; print and copy
  restrictions are lost.

---

## A word on what this is for

Replacing text in a PDF is how you correct an archive, prepare a template, or
anonymise a test corpus. It is also how you forge a document. The hosted instance
timestamps usage. You are responsible for what you produce.

---

## Licence

MIT. See [LICENSE](LICENSE).


## Engine fixes — 2026-09-08

Positioned glyphs now join only when adjacent on the same baseline, with traceable
character offsets preserved. Separate lines and columns remain distinct. This
supports producers that emit a separate text matrix for every letter.

Multiline replacement values are written on existing source baselines, rather
than encoding newlines as glyphs. If a replacement has more lines than available
source groups, trailing lines share the final baseline, separated by a comma.
This preserves all supplied address text without inserting a new line into a
neighbouring field. General paragraph reflow is not supported.

Simple TrueType repair initializes the format-2 post table when converting from
format 3 and updates symbolic (3,0) cmap entries at the font's existing code offset.
The injected glyph must be reachable by the renderer, not merely by ToUnicode.

Automatic fitting accounts for following text in other content runs. Isolated
right-aligned labels near the page edge expand to the left at their natural size;
the verification mask includes that expected shift. Other overflowing automatic
fits condense within their region instead of accepting off-page text. Verification
also compares visible glyph traces against page bounds, independently of pixel
masks, and rejects newly clipped glyphs (including explicitly forced overflow).

Validation: 21 tests, including the existing API suite, synthetic positioned-glyph,
multiline, margin and forced-overflow regressions, and optional private fixtures.
Private invoice fixtures are kept outside Git under data/repair-20260908; enable
with PDF_REGRESSION_FIXTURES. Never add these PDFs to the public repository.
