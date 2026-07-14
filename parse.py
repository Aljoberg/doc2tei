from __future__ import annotations

import argparse
from contextlib import nullcontext, redirect_stdout
import sys

from doc2tei.parser import parse_document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert a configured document to TEI XML")
    parser.add_argument("input", help="input PDF or DOCX")
    parser.add_argument("-o", "--out", help="output XML (stdout when omitted)")
    parser.add_argument(
        "-c",
        "--config",
        default="config.py",
        help="configuration file (default: ./config.py)",
    )
    parser.add_argument("--debug-file", help="capture config debug logging")
    parser.add_argument("--diagnostics", help="write extraction/rule diagnostics as JSON")
    parser.add_argument("--data-output", help="write data exported by config hooks as JSON")
    parser.add_argument(
        "--xml-declaration", action="store_true", help="include an XML declaration"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
        result.write_xml(args.out, xml_declaration=args.xml_declaration)
    else:
        sys.stdout.buffer.write(
            result.to_bytes(xml_declaration=args.xml_declaration) + b"\n"
        )
    if args.diagnostics:
        result.write_diagnostics(args.diagnostics)
    if args.data_output:
        result.write_data(args.data_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
