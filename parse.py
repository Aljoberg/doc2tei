import argparse
from typing import cast
import xml.etree.ElementTree as ET
from contextlib import redirect_stdout
from engine import Chunk, PDFChunk, WordChunk

import engine
from engine import append, commit_children, pop, root, stack
from config_pdf import CONFIG, COSMETIC_ANNOTATIONS, get_frames
from type_decs import (
    PDFRule,
    PDFRunTest,
    WordRule,
    Rule,
    RuleGroup,
    WordRunTest,
)


def get_center_point(c: Chunk):
    # yes
    if isinstance(c, WordChunk):
        print(f"{c.x=}, {c.y=}, {c.w=}, {c.h=}")
        print(f"center of container: {c.x + c.w / 2}")
        return (
            c.x + c.w / 2
        )  # https://excalidraw.com/#json=Vz5yoLFIApDsFnrAIl9Y5,9JIGo2YGGwsgCHfrQ0AWJA
    else:
        # well shit, we can't exactly get the chunk's center since we don't know where it'll get off
        return c.x  # TODO


def do_rule_chores(
    rule: Rule,
    chunk: Chunk,
):
    print(chunk, rule)
    if isinstance(chunk, WordChunk):
        rule = cast(WordRule, rule)
        if "action" in rule:
            rule["action"](
                cast(WordChunk, chunk),
            )
        if "append_func" in rule:
            rule["append_func"](
                cast(WordChunk, chunk),
            )
        else:
            append(chunk)
    elif isinstance(chunk, PDFChunk):
        rule = cast(PDFRule, rule)
        if "action" in rule:
            print("acting the urle", rule, chunk)
            rule["action"](cast(PDFChunk, chunk))
            print("did le act")
        if "append_func" in rule:
            rule["append_func"](cast(PDFChunk, chunk))
        else:
            append(chunk)

    if "after_append" in rule:
        rule["after_append"]()


def match_rules(
    group: RuleGroup,
    chunk: Chunk,
) -> bool:
    run_immediate = group.get("run_immediate")
    if callable(run_immediate):
        run_immediate()

    # individual run rules
    # if a container generates multiple elements (such as a heading)
    # maybe should come before paragraph matching
    run_rules = {k: v for k, v in group.items() if not callable(v) and "test_run" in v}
    if run_rules:
        handled = False
        # for run in paragraph.runs:
        if not chunk.text or chunk.text == "\n":
            # stop it, get some help
            return False
        run_else: tuple[str, Rule] | None = None
        for key, rule in run_rules.items():
            if (
                "alignment" in rule
                and isinstance(chunk, WordChunk)
                and chunk.paragraph.alignment != rule["alignment"]
            ):
                continue
            test_run = rule["test_run"]
            if test_run == "_else":
                run_else = (key, rule)
                continue
            if isinstance(chunk, WordChunk):
                test_run = cast(WordRunTest, test_run)
                if test_run(chunk):
                    print(f"{key}: {chunk.text}")
                    do_rule_chores(rule, chunk)
                    handled = True
                    break
            elif isinstance(chunk, PDFChunk):
                test_run = cast(PDFRunTest, test_run)
                if test_run(chunk):
                    print(f"{key}: {chunk.text}")
                    do_rule_chores(rule, chunk)
                    handled = True
                    break

        else:
            if run_else is not None:
                key, rule = run_else
                print(f"{key} (fallback): {chunk.text}")
                do_rule_chores(rule, chunk)
                handled = True
        if handled:
            return True

    return False


def parse_text(chunk: Chunk):
    engine.is_first_run = False

    print("-- parsing text --")

    x = chunk.x
    y = chunk.y

    center = get_center_point(chunk)

    if 1100 < y < 1600:
        # header, likely
        # this is fragile because i don't have a way of figuring what a header is
        # in the doc i'm making it's got 3 elements so i can just seek by 3
        # but it's not gonna be 3 in every doc
        # also the y values are kinda bad for detection, i guess
        # and GUESS WHAT!!! every other page has the page number FLIPPEDDD!!! how jolly and fun
        print(f"HEADER: {chunk.text}")
        # if len(chunk.runs) == 1 and chunk.runs[0].text.strip().isdigit():
        #     # page num
        #     print(f"PAGE NUM: {chunk.text}")
        return

    alignments = CONFIG["alignments"]
    matched = False
    if isinstance(chunk, WordChunk):
        w, h = chunk.w, chunk.h
        if (
            4550 < center < 6560 and "center" in alignments
        ):  # TODO unmagic the magic numbers
            # centered
            matched = match_rules(alignments["center"], chunk)
        else:
            if (
                2500 < center < 3660 and "left" in alignments
            ):  # left here in case we need it
                print("left")
                matched = match_rules(alignments["left"], chunk)
            elif 7820 < center < 8460 and "right" in alignments:
                print("right")
                matched = match_rules(alignments["right"], chunk)
            elif "_else" in alignments:
                print("not left nor right")
                matched = match_rules(alignments["_else"], chunk)
    else:
        # deal with it
        matched = match_rules(alignments["any"], chunk)

    if not matched:
        # default, i suppose
        # TODO add to config
        engine.is_first_run = True
        # chunk.text = chunk.text.replace("\n", "") # TODO remove / make customizable
        append(chunk)

    print(
        f"{chunk.text=}, {x=}, {y=}, {chunk=}, {chunk=}, {len(chunk.paragraph.runs) if isinstance(chunk, WordChunk) else ''}"
    )
    print("\n")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("input", type=str)
    p.add_argument("-o", "--out", type=str, default=None)

    args = p.parse_args()

    chunks = get_frames(args.input)

    engine.COSMETIC_ANNOTATIONS = COSMETIC_ANNOTATIONS

    with open("meow.txt", "w", encoding="utf-8") as f, redirect_stdout(f):
        for chunk in chunks:
            parse_text(chunk)

    # close every still-open element back down to the root <div>
    while len(stack) > 1:
        pop()

    commit_children(stack[0])

    xml = ET.tostring(root, encoding="utf-8")

    if not args.out:
        print(xml)
    else:
        with open(args.out, "wb") as f:
            f.write(xml)
