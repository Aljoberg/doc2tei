# doc2tei

Converts documents (PDF & Word files) to a TEI xml document

All docs are in [CONFIG.md](CONFIG.md), so make sure to read that.

For a quick runover though, this program works by giving it an input (a PDF file) and it outputs an XML file based on a _configuration_ that is provided.

It works by parsing text accessible through the PDF / Word and assembling them into _chunks_,
small pieces of text with properties such as their font and position.
These chunks then get fed into a rule-system based configuration, and each invididual rule decides what to do with that chunk.

For example, a rule for a session title might be that it's bold, centered, and all caps.
This rule then opens a `<head type="session">` tag.

There's a lot more customization and helpers available, so if you're interested, read CONFIG.md.
You can check out the example configs in the [examples](examples/) directory.

## Running

Install the project dependencies:

```powershell
python -m pip install -r requirements.txt
```

Run `parse.py` with an input, an output, and an explicit config selected from
the `examples/` directory. The root `config.py` is intentionally not runnable.

```bash
python parse.py path_to_pdf.pdf --config examples/zbor-republik-in-pokrajin/config.py -o out.xml
```

Useful optional outputs are `--diagnostics diagnostics.json` (rule hit counts,
unmatched samples, pages and fonts) and `--data-output data.json` (data exported
by config hooks, such as a speaker mapping).
`--list-person-output listPerson.xml` writes a minimal TEI speaker list directly
from that mapping in the same parse invocation. It also recovers roles and
organization affiliations from speaker labels. Add `--include-wikidata` to
enrich people with best-effort Wikidata names, identifiers, dates, places,
occupations, nationalities, sex, and political-party affiliations. Network or
matching failures fall back to the local person record instead of aborting the
document. When no speakers are detected, it writes an `UnknownSpeaker`
placeholder instead of an invalid empty list or a pipeline-breaking exception.
`--pretty` adds readable structural indentation to the main XML and
`listPerson` output while preserving text inside mixed-content elements.

The same operation is available as a library API and has no mandatory output
side effects:

```python
from doc2tei import parse_document

result = parse_document(
    "input.pdf",
    config="examples/zbor-republik-in-pokrajin/config.py",
)
result.write_xml("out.xml")
result.write_diagnostics("diagnostics.json")
result.write_data("speakers.json")
result.write_list_person("listPerson.xml")
# Optional, best-effort network enrichment:
result.write_list_person("listPerson-enriched.xml", include_wikidata=True)
```

For Wikidata-enriched person records, generate the list during parsing:

```bash
python parse.py input.pdf --config examples/general-config/config.py \
  -o out.xml --list-person-output listPerson.xml --include-wikidata
```

The old `make_list_person.py data.json -o listPerson.xml` command remains as a
compatibility wrapper around the same implementation. Omit `--include-wikidata`
for deterministic, offline output.

## Web interface

Start the local Streamlit control panel from the repository root:

```powershell
python -m streamlit run streamlit_app.py
```

The UI accepts local files/directories, browser uploads, SIstory menu paths, or
a combination of them. It exposes the corpus, list, recovery, Wikidata, and
parallelism options from `batch_parse.py`, then runs that same CLI in an
isolated background process. The page updates from the real batch manifest,
shows a bounded live log, permits stopping the process tree, and offers the
manifest and generated XML files for download. After a run, **Download output
ZIP** packages every generated XML file while preserving the corpus directory
layout; source downloads and the separate diagnostics/audit tree are excluded.
The archive is generated only when clicked. Streamlit buffers the finished ZIP
in memory while serving the download, so downloading a very large corpus can
temporarily require approximately the compressed archive size in additional
RAM.

The default destinations are `out/web-corpus` and its sibling
`out/web-corpus-metadata`. Uploaded source files and UI run logs are retained
under the audit tree, never inside the corpus.

This interface is intended for trusted local use: it can read user-supplied
filesystem paths and start parser processes. Streamlit binds locally by
default; do not expose it to untrusted networks without adding authentication
and an appropriate filesystem/process sandbox.

## Batch processing

`batch_parse.py` recursively converts every PDF and DOCX below one or more
inputs. It uses the general config by default and preserves each supplied
source folder as a top-level folder beneath a neutral output container:

```powershell
python .\batch_parse.py "D:\documents" `
  --output-dir "D:\tei-output" `
  --pretty `
  --xml-declaration
```

Output folder and component names follow the ParlaMint conventions. Catalogue
indices are moved out of the leading position (`1. sklic (1947-1950)` becomes
`sklic-01`), and document components use names such as
`ParlaMint-SI_1947-12-15-sklic-01-01.xml`. Components live directly in their
subcorpus folder. The output container is not implicitly another corpus.
Human-review artifacts do not live in it either: by default, `tei-output` is
paired with a sibling `tei-output-metadata` tree containing
`<subcorpus>/<component>/data.json`, `diagnostics.json`, `status.json`, and any
`debug.log`. The batch manifest and default SIstory source cache are kept there
too. Use `--metadata-dir D:\review-files` to choose another audit location.
The original source filename is still the document's main TEI title. Reliably
inferred folder/term and date metadata are added as a subordinate title,
`<meeting>`, and header dates. Automatically generated structural `xml:id`
values use the final corpus component stem rather than the old source
filename.

`--corpus-prefix` controls the corpus-family part of filenames and generated
IDs (default `ParlaMint`), while `--corpus-code` controls the ISO
country/region part (default `SI`). For example,
`--corpus-prefix Debates --corpus-code GB` produces names beginning
`Debates-GB`. When no defensible transcript date can be extracted, the
component uses `undated` instead of inventing one.

Choose the `listPerson` aggregation level with:

```powershell
# One list beside all document bundles downloaded into the same source folder
python .\batch_parse.py "D:\documents" -o "D:\tei-output" `
  --list-person-scope folder

# One list covering every document in the complete batch
python .\batch_parse.py "D:\documents" -o "D:\tei-output" `
  --list-person-scope corpus
```

Exact speaker IDs are deduplicated while document `who` references remain
unchanged. Changing only the scope reuses existing `data.json` files rather
than reparsing PDFs. The manifest records the exact paths. Add
`--include-wikidata` for best-effort enrichment or `--no-list-person` to
suppress lists.

Add `--emit-corpus-xml` to build a corpus for every recursive folder level.
Each corpus is a standalone view of its complete subtree:

- Every top-level source folder is an independent root corpus. The output
  directory merely contains those folders and their parent-owned corpus files;
  no aggregate `<PREFIX>-<CODE>.xml` is generated for the output directory
  itself.
- Documents remain directly inside their subcorpus directory.
- A subcorpus root and its `listPerson`/`listOrg` are written one level outside,
  in the parent corpus directory. For example, `sklic-01/` holds the components
  while its parent holds `<PREFIX>-<CODE>-sklic-01.xml`,
  `<PREFIX>-<CODE>-sklic-01-listPerson.xml`, and
  `<PREFIX>-<CODE>-sklic-01-listOrg.xml`.
- Every corpus XIncludes all descendant document components directly. It never
  XIncludes another corpus XML.
- Every person and organisation list is a flat, deduplicated aggregate for that
  subtree. Lists never recursively XInclude child lists, avoiding duplicate
  `xml:id` values.
- Corpus `<extent>` counts cover the same complete subtree.

Add `--include-root-corpus` to also forge
`OUTPUT_DIR/<PREFIX>-<CODE>.xml` (plus its aggregate lists when enabled) over
every document in all top-level corpora. The root directly XIncludes
documents, never the independent child corpus XML files. This flag requires
`--emit-corpus-xml` and is disabled by default.

Corpus generation replaces the flat list scope for that run.
`--no-list-person` suppresses both person and organisation lists, and
`--corpus-lang` controls corpus header `xml:lang` (default `sl`).

By default, the runner uses up to four document processes. When multiple
documents run together, it forces each document's page extractor to one worker,
preventing nested process pools from multiplying CPU and memory use. `-j 1`
instead lets the general config use its own page-level parallelism. Explicit
`-j N` and `--page-workers N` values are available for tuning.

Completed bundles are fingerprinted from the source, config, parser code, and
relevant options. Re-running the command skips unchanged work, while changed
inputs or code are automatically rebuilt; use `--overwrite` to force
everything. Worker processes
are recycled after each document so PDF library memory is returned to the OS.
For very small documents, `--reuse-workers` trades that isolation for less
startup overhead.

A document-level exception does not stop the batch. The runner emits a minimal
reviewable TEI document, diagnostics, and an `UnknownSpeaker` listPerson, marks
the bundle `recovered`, and moves on. Only an output failure that prevents even
that fallback is marked `failed` and makes the command return a nonzero status.
Non-fatal review warnings remain visible in `data.json`, the TEI header, and the
batch manifest without changing an otherwise successful document from `ok`.

### Downloading directly from SIstory

The bundled `sistory-dl` submodule can feed a SIstory menu directly into the
same batch pipeline:

```powershell
python .\batch_parse.py `
  --sistory-menu 1/7/397/407 `
  --output-dir "D:\tei-output" `
  --pretty `
  --xml-declaration
```

A complete URL such as
`https://sistory.si/slv/menu/1/7/397/407` also works, and
`--sistory-menu` can be repeated. Sources are retained under
`METADATA_DIR/_sistory-downloads` in a stable per-menu cache. On later runs,
`sistory-dl` skips existing PDFs and doc2tei skips unchanged conversions, so the
whole download-and-parse command is resumable. Change the source cache with
`--sistory-download-dir`.

The private hash-named cache folders are not copied into the corpus layout.
Instead, each actual menu-title folder downloaded by `sistory-dl` becomes a
top-level folder inside `OUTPUT_DIR`. Repeating `--sistory-menu` produces
multiple sibling folders and independent root corpus XML/list files; it does
not create an aggregate corpus for `OUTPUT_DIR` unless
`--include-root-corpus` is supplied.

Download statistics and failures are stored in
`METADATA_DIR/batch-manifest.json`. A partial menu download does not prevent
successfully downloaded or previously cached documents from being parsed, but
the final command status remains nonzero so an incomplete acquisition is not
silently reported as complete. When local inputs and SIstory menus are supplied
together, both the corpus and audit trees are separated under `local/` and
`sistory/`.

Initialize the downloader after cloning with:

```bash
git submodule update --init sistory-dl
```

Again, I've written everything in CONFIG.md, so if you're interested in running this, you should start with that file.

### Good luck!
