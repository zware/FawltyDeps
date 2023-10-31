"""A spec-compliant gitignore parser for Python."""

from __future__ import annotations

import os
import re
from os.path import abspath, dirname
from pathlib import Path
from typing import Callable, NamedTuple, Optional, Tuple, Union

PathOrStr = Union[str, Path]


def parse_gitignore(
    full_path: PathOrStr, base_dir: Optional[PathOrStr] = None
) -> Callable[[PathOrStr], bool]:
    """Parse the given .gitignore file and return the resulting rules checker.

    The return value is a predicate for paths to ignore, i.e. a callable that
    returns True/False based on whether the given path should be ignored or not.

    The 'base_dir', if given, specifies the directory relative to which the
    parsed ignore rules will be interpreted. If not given, the parent directory
    of the 'full_path' is used instead.
    """
    if base_dir is None:
        base_dir = dirname(full_path)
    rules = []
    with open(full_path) as ignore_file:
        counter = 0
        for line in ignore_file:
            counter += 1
            line = line.rstrip("\n")
            rule = rule_from_pattern(
                line, base_path=Path(base_dir).resolve(), source=(full_path, counter)
            )
            if rule:
                rules.append(rule)
    if not any(r.negation for r in rules):
        return lambda file_path: any(r.match(file_path) for r in rules)

    # We have negation rules. We can't use a simple "any" to evaluate them.
    # Later rules override earlier rules.
    def handle_negation(file_path: PathOrStr) -> bool:
        for rule in reversed(rules):
            if rule.match(file_path):
                return not rule.negation
        return False

    return handle_negation


def rule_from_pattern(  # pylint: disable=too-many-branches
    pattern: str,
    base_path: Optional[Path] = None,
    source: Optional[Tuple[PathOrStr, int]] = None,
) -> Optional[Rule]:
    """
    Take a .gitignore match pattern, such as "*.py[cod]" or "**/*.bak",
    and return a Rule suitable for matching against files and directories.
    Patterns which do not match files, such as comments and blank lines, will
    return None. Because git allows for nested .gitignore files, a base_path
    value is required for correct behavior. The base path should be absolute.
    """
    if base_path and base_path != Path(base_path).resolve():
        raise ValueError("base_path must be absolute")
    # Store the exact pattern for our repr and string functions
    orig_pattern = pattern
    # Early returns follow
    # Discard comments and separators
    if pattern.strip() == "" or pattern[0] == "#":
        return None
    # Strip leading bang before examining double asterisks
    if pattern[0] == "!":
        negation = True
        pattern = pattern[1:]
    else:
        negation = False
    # Multi-asterisks not surrounded by slashes (or at the start/end) should
    # be treated like single-asterisks.
    pattern = re.sub(r"([^/])\*{2,}", r"\1*", pattern)
    pattern = re.sub(r"\*{2,}([^/])", r"*\1", pattern)

    # Special-casing '/', which doesn't match any files or directories
    if pattern.rstrip() == "/":
        return None

    directory_only = pattern[-1] == "/"
    # A slash is a sign that we're tied to the base_path of our rule
    # set.
    anchored = "/" in pattern[:-1]
    if pattern[0] == "/":
        pattern = pattern[1:]
    if pattern[0] == "*" and len(pattern) >= 2 and pattern[1] == "*":
        pattern = pattern[2:]
        anchored = False
    if pattern[0] == "/":
        pattern = pattern[1:]
    if pattern[-1] == "/":
        pattern = pattern[:-1]
    # patterns with leading hashes or exclamation marks are escaped with a
    # backslash in front, unescape it
    if pattern[0] == "\\" and pattern[1] in ("#", "!"):
        pattern = pattern[1:]
    # trailing spaces are ignored unless they are escaped with a backslash
    i = len(pattern) - 1
    striptrailingspaces = True
    while i > 1 and pattern[i] == " ":
        if pattern[i - 1] == "\\":
            pattern = pattern[: i - 1] + pattern[i:]
            i = i - 1
            striptrailingspaces = False
        else:
            if striptrailingspaces:
                pattern = pattern[:i]
        i = i - 1
    regex = fnmatch_pathname_to_regex(
        pattern, directory_only, negation, anchored=bool(anchored)
    )
    return Rule(
        pattern=orig_pattern,
        regex=regex,
        negation=negation,
        directory_only=directory_only,
        anchored=anchored,
        base_path=_normalize_path(base_path) if base_path else None,
        source=source,
    )


class Rule(NamedTuple):
    """A single ignore rule, parsed from a gitignore pattern string."""

    # Basic values
    pattern: str
    regex: str
    # Behavior flags
    negation: bool
    directory_only: bool
    anchored: bool
    base_path: Optional[Path]  # meaningful for gitignore-style behavior
    source: Optional[Tuple[PathOrStr, int]]  # (file, line) tuple for reporting

    def __str__(self) -> str:
        return self.pattern

    def __repr__(self) -> str:
        return "".join(["Rule('", self.pattern, "')"])

    def match(self, abs_path: PathOrStr) -> bool:
        """Return True iff the given 'abs_path' should be ignored."""
        matched = False
        if self.base_path:
            rel_path = str(_normalize_path(abs_path).relative_to(self.base_path))
        else:
            rel_path = str(_normalize_path(abs_path))
        # Path() strips the trailing slash, so we need to preserve it
        # in case of directory-only negation
        if self.negation and isinstance(abs_path, str) and abs_path[-1] == "/":
            rel_path += "/"
        if rel_path.startswith("./"):
            rel_path = rel_path[2:]
        if re.search(self.regex, rel_path):
            matched = True
        return matched


# Frustratingly, python's fnmatch doesn't provide the FNM_PATHNAME
# option that .gitignore's behavior depends on.
def fnmatch_pathname_to_regex(  # pylint: disable=too-many-branches,too-many-statements
    pattern: str, directory_only: bool, negation: bool, anchored: bool = False
) -> str:
    """Convert the given fnmatch-style pattern to the equivalent regex.

    Implements fnmatch style-behavior, as though with FNM_PATHNAME flagged;
    the path separator will not match shell-style '*' and '.' wildcards.
    """
    i, n = 0, len(pattern)

    seps = [re.escape(os.sep)]
    if os.altsep is not None:
        seps.append(re.escape(os.altsep))
    seps_group = "[" + "|".join(seps) + "]"
    nonsep = rf"[^{'|'.join(seps)}]"

    res = []
    while i < n:
        c = pattern[i]
        i += 1
        if c == "*":
            try:
                if pattern[i] == "*":
                    i += 1
                    if i < n and pattern[i] == "/":
                        i += 1
                        res.append("".join(["(.*", seps_group, ")?"]))
                    else:
                        res.append(".*")
                else:
                    res.append("".join([nonsep, "*"]))
            except IndexError:
                res.append("".join([nonsep, "*"]))
        elif c == "?":
            res.append(nonsep)
        elif c == "/":
            res.append(seps_group)
        elif c == "[":
            j = i
            if j < n and pattern[j] == "!":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                res.append("\\[")
            else:
                stuff = pattern[i:j].replace("\\", "\\\\").replace("/", "")
                i = j + 1
                if stuff[0] == "!":
                    stuff = "".join(["^", stuff[1:]])
                elif stuff[0] == "^":
                    stuff = "".join("\\" + stuff)
                res.append(f"[{stuff}]")
        else:
            res.append(re.escape(c))
    if anchored:
        res.insert(0, "^")
    else:
        res.insert(0, f"(^|{seps_group})")
    if not directory_only:
        res.append("$")
    elif directory_only and negation:
        res.append("/$")
    else:
        res.append("($|\\/)")
    return "".join(res)


def _normalize_path(path: PathOrStr) -> Path:
    """Normalize a path without resolving symlinks.

    This is equivalent to `Path.resolve()` except that it does not resolve symlinks.
    Note that this simplifies paths by removing double slashes, `..`, `.` etc. like
    `Path.resolve()` does.
    """
    return Path(abspath(path))