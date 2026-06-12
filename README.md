# doc2tei

- converts docs to tei
- is awesome
- is a wip
- meow :3

### It only works on .docx files for now, because they actually have structure unlike some formats (khm pdf)
PDFs were actually the *original* way of parsing documents, but they were *really* inconsistent with me having to hardcode x & y values of text chunks

But then a colleague of mine got an idea of converting it to a word document, and it actually had STRUCTURE and text in CONTAINERS

I immediately rewrote the whole program to handle .docx files, however that required a parser from PDF to .docx (in our case, ABBYY FineReader)

And searchable documents are damn well searchable but not consistent or structured at all

So converting a PDF to TEI would require defining a lot of precise coordinates in the config
& the library for reading the PDF would probably misinterpret texts

This is because (i assume) the text in a searchable PDF is just characters at a specific x & y value

This means that to know whether the text is a word, if there's any breaks in between, and so on, means you have to measure the space between two characters

This is *very* fragile and specific, so misintepretations happen in the library for PDFs

So the spacings would (probably) have to be tweaked as well, for every document

*(Do you see why I immediately switched to .docx processing yet?)*

So yeah, good luck to whoever wants to use this as a PDF

Will refactor the code to support both

:3