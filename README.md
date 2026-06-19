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
You can check out two example configs and their outputs in the [examples](examples/) directory.

## Running

You can run the program by running `parse.py`, giving it a path to a PDF / Word file and an outfile.
The config will be read from `config.py` in the project root.

```python
python3 parse.py path_to_pdf.pdf -o out.xml
```

If you want to assemble a `<listPerson>` file, you can also run

```python
python3 make_list_person.py out/speaker_utterance.json -o listPerson.xml
```

This program takes a .json file with speaker ids and their long forms.
This file's generated through the config's `on_end` hook, so make sure to check that out.

Again, I've written everything in CONFIG.md, so if you're interested in running this, you should start with that file.

### Good luck!