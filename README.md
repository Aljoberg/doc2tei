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
```

If you want to assemble a `<listPerson>` file, you can also run

```python
python3 make_list_person.py out/speaker_utterance.json -o listPerson.xml
```

This program takes a .json file with speaker ids and their long forms.
This file can be exported by a config's `on_end(result)` hook and the
`--data-output` option, so make sure to check that out.

Again, I've written everything in CONFIG.md, so if you're interested in running this, you should start with that file.

### Good luck!
