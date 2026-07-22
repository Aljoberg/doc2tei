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

Again, I've written everything in CONFIG.md, so if you're interested in running this, you should start with that file.

### Good luck!
