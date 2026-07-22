from __future__ import annotations

import argparse
from contextlib import nullcontext, redirect_stdout
import sys

from doc2tei.parser import parse_document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert a configured document to TEI XML"
    )
    parser.add_argument("input", help="input PDF or DOCX")
    parser.add_argument("-o", "--out", help="output XML (stdout when omitted)")
    parser.add_argument(
        "-c",
        "--config",
        required=True,
        help="configuration file (choose one from examples/)",
    )
    parser.add_argument("--debug-file", help="capture config debug logging")
    parser.add_argument(
        "--diagnostics", help="write extraction/rule diagnostics as JSON"
    )
    parser.add_argument(
        "--data-output", help="write data exported by config hooks as JSON"
    )
    parser.add_argument(
        "--list-person-output",
        help="write a TEI listPerson from exported speaker data",
    )
    parser.add_argument(
        "--include-wikidata",
        action="store_true",
        help="enrich --list-person-output with best-effort Wikidata metadata",
    )
    parser.add_argument(
        "--xml-declaration", action="store_true", help="include an XML declaration"
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="indent XML output without changing mixed-content text",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    argument_parser = build_parser()
    args = argument_parser.parse_args(argv)
    if args.include_wikidata and not args.list_person_output:
        argument_parser.error("--include-wikidata requires --list-person-output")
    debug_context = (
        open(args.debug_file, "w", encoding="utf-8")
        if args.debug_file
        else nullcontext(None)
    )
    with debug_context as debug_stream:
        redirect_context = (
            redirect_stdout(debug_stream) if debug_stream is not None else nullcontext()
        )
        with redirect_context:
            result = parse_document(args.input, config=args.config)

    if args.out:
        result.write_xml(
            args.out,
            xml_declaration=args.xml_declaration,
            pretty=args.pretty,
        )
    else:
        sys.stdout.buffer.write(
            result.to_bytes(
                xml_declaration=args.xml_declaration,
                pretty=args.pretty,
            )
            + b"\n"
        )
    if args.diagnostics:
        result.write_diagnostics(args.diagnostics)
    if args.data_output:
        result.write_data(args.data_output)
    if args.list_person_output:
        result.write_list_person(
            args.list_person_output,
            xml_declaration=args.xml_declaration,
            pretty=args.pretty,
            include_wikidata=args.include_wikidata,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
