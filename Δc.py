#!/usr/bin/env python3

import argparse
import re
import subprocess
from collections import defaultdict
from collections import namedtuple
from difflib import SequenceMatcher
from functools import partial
from itertools import takewhile
from operator import attrgetter
from pathlib import Path

CONTEXT_LEN = 3
SOURCE_LINE_RE = re.compile(r"^\s*(?P<lineno>\d+)\|\s*(?P<count>[^|\s]*)\s*\|(?P<text>.*)$")

Line = namedtuple("Line", "line, lineno, count, is_covered, text")


class CompareBy(namedtuple("CompareBy", "keys, item")):
    def __eq__(self, other):
        if not isinstance(other, CompareBy) or self.keys != other.keys:
            return NotImplemented
        key = attrgetter(*self.keys)
        return key(self.item) == key(other.item)

    def __hash__(self):
        return hash(attrgetter(*self.keys)(self.item))


class DiffLine(namedtuple("DiffLine", "marker, line, tag")):
    def __str__(self):
        return f"{self.marker}{self.line}"


def parse(lines):
    files = defaultdict(list)
    filename = Path("/<unknown file>")
    for line in lines:
        line = line.rstrip()
        if line.startswith("/") and line.endswith(":"):
            filename = Path(line.removesuffix(":"))
        else:
            match = SOURCE_LINE_RE.fullmatch(line)
            if match is not None:
                files[filename].append(
                    Line(
                        line=match[0],
                        lineno=int(match["lineno"]),
                        count=match["count"],
                        is_covered=match["count"] != "0",
                        text=match["text"],
                    ),
                )

    return files


def context(lines):
    return takewhile(lambda line: line.marker == " ", lines)


def diff(filename, left, right):
    comparer = partial(CompareBy, ("text", "is_covered"))
    matcher = SequenceMatcher(
        None,
        list(map(comparer, left)),
        list(map(comparer, right)),
    )

    show_header = True
    for opcodes in matcher.get_grouped_opcodes(n=CONTEXT_LEN):
        lines = []
        for tag, l_from, l_to, r_from, r_to in opcodes:
            if tag == "equal":
                for line in right[r_from:r_to]:
                    lines.append(DiffLine(marker=" ", line=line.line, tag=tag))
            elif tag in {"replace", "insert"}:
                for line in right[r_from:r_to]:
                    marker = " " if line.is_covered else "+"
                    lines.append(DiffLine(marker=marker, line=line.line, tag=tag))

        start_context = sum(1 for _ in context(lines))
        start_offset = max(0, start_context - CONTEXT_LEN)
        end_context = sum(1 for _ in context(reversed(lines)))
        end_offset = max(0, end_context - CONTEXT_LEN)

        lines = lines[start_offset:len(lines) - end_offset]
        if not lines:
            continue

        if show_header:
            show_header = False
            yield f"--- a/{filename}"
            yield f"+++ b/{filename}"

        _, left_start, _, right_start, _ = opcodes[0]

        yield f"@@ -{left_start + 1},1 +{right_start + start_offset + 1},{len(lines)} @@"
        yield from lines


def check_output(*args, **kwargs):
    return subprocess.check_output(*args, **kwargs, text=True)


def git(repo):
    return ["git", "-C", repo]


def git_switch(repo, ref):
    subprocess.check_call([*git(repo), "checkout", ref])


def run_in(repo, ref, command, *args, **kwargs):
    try:
        git_switch(repo, ref)
        return check_output(command, *args, **kwargs)
    finally:
        git_switch(repo, "-")


def get_coverage(repo, ref, command):
    return run_in(repo, ref, command, cwd=repo).splitlines()


def main(args=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-C", "--git-repo",
        type=lambda arg: Path(arg).resolve(),
        default=Path.cwd(),
    )
    parser.add_argument("left")
    parser.add_argument("right")
    parser.add_argument("command", nargs="+")
    args = parser.parse_args(args)

    left = check_output([*git(args.git_repo), "merge-base", args.left, args.right]).strip()

    left = parse(get_coverage(args.git_repo, args.left, args.command))
    right = parse(get_coverage(args.git_repo, args.right, args.command))

    for (filename, left_lines), right_lines in zip(left.items(), right.values()):
        lines = diff(
            filename.relative_to(args.git_repo, walk_up=True),
            left_lines,
            right_lines,
        )
        for line in lines:
            print(line)


if __name__ == "__main__":
    main()
