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

## API

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
