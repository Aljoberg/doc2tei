# General-config output examples

These bundles were generated with `examples/general-config/config.py`. Each source PDF
has its own directory containing:

- `document.xml` - pretty-printed TEI output;
- `listPerson.xml` - pretty-printed minimal speaker list;
- `data.json` - data exported by configuration hooks;
- `diagnostics.json` - rule counts, unmatched samples, fonts, pages, and
  recovery information.

The first directory level identifies the source set (`testdocs`,
`novi-primeri`, or `uploaded-example`). Source PDFs are not duplicated here.

To regenerate a bundle, run this from the repository root:

```powershell
python .\parse.py "path\to\source.pdf" `
  --config .\examples\general-config\config.py `
  --out "path\to\bundle\document.xml" `
  --diagnostics "path\to\bundle\diagnostics.json" `
  --data-output "path\to\bundle\data.json" `
  --list-person-output "path\to\bundle\listPerson.xml" `
  --xml-declaration `
  --pretty
```

`--pretty` only adds structural XML indentation. It preserves text in
mixed-content elements such as utterances, references, and inline styling.
Add `--include-wikidata` to enrich `listPerson.xml`; omit it for deterministic,
offline output matching the checked-in bundles.
