"""Compatibility CLI for building an enriched TEI listPerson from JSON data."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

from doc2tei.helpers import build_list_person


def make_list_person(mapping: Mapping[str, str]) -> ET.Element:
    """Preserve the old importable entry point using the shared implementation."""

    return build_list_person(mapping, include_wikidata=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a Wikidata-enriched TEI listPerson"
    )
    parser.add_argument("input", help="JSON speaker mapping or parse data output")
    parser.add_argument("-o", "--out", help="output XML (stdout when omitted)")
    parser.add_argument(
        "--pretty", action="store_true", help="indent the generated XML"
    )
    parser.add_argument(
        "--xml-declaration",
        action="store_true",
        help="include an XML declaration",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("speakers"), dict):
        payload = payload["speakers"]
    mapping = (
        {
            str(reference): str(label)
            for reference, label in payload.items()
            if isinstance(reference, str) and isinstance(label, str)
        }
        if isinstance(payload, dict)
        else {}
    )
    root = make_list_person(mapping)
    if args.pretty:
        ET.indent(root, space="  ")
    content = ET.tostring(
        root,
        encoding="utf-8",
        xml_declaration=args.xml_declaration,
    )
    if args.out:
        Path(args.out).write_bytes(content + (b"\n" if args.pretty else b""))
    else:
        sys.stdout.buffer.write(content + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
