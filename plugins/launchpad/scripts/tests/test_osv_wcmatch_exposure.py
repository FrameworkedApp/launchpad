"""The wcmatch suppression in osv-scanner.toml must stay true.

CONTEXT. GHSA-53pm-wgxf-c6qf is a polynomial-time ReDoS in wcmatch: matching
a crafted file name against a pattern with several single `*` wildcards in one
path segment (for example `*_*_*_*_*.csv`) takes O(n^k) time. The fix (wcmatch
11.2) cannot be installed because semgrep pins `wcmatch~=8.3`, so the advisory
is suppressed in `plugins/launchpad/scripts/osv-scanner.toml`.

The suppression is justified only by the patterns semgrep feeds to wcmatch.
semgrep calls `wcmatch.glob.globmatch` solely to apply rule `paths:`
include/exclude patterns. LaunchPad's only sources of such patterns are the
rule files under `plugins/launchpad/.semgrep/`; there is no `.semgrepignore`
and no semgrep invocation passes `--include` or `--exclude`. File names are
attacker-controlled in a pull request, so safety depends entirely on the
patterns never taking the multi-star shape.

WHAT THIS FILE ENFORCES, while the suppression is present:
1. No rule `paths:` pattern has more than one single `*` in a path segment.
2. No `.semgrepignore` is tracked, and every semgrep invocation in
   lefthook.yml and .github/workflows/ passes neither `--include` nor
   `--exclude` and loads only rule files from `plugins/launchpad/.semgrep/`
   (the files item 1 checks). Invocations are read from the parsed YAML,
   with shell line continuations joined and each command split into shell
   tokens, so continuation lines, folded scalars, quoted options and inline
   comments are all handled.
3. With semgrep's own pattern preprocessing and matching flags, hostile paths
   (255-byte names, near-miss suffixes, deep directory chains) match every
   expanded pattern quickly. Each pattern is probed in a subprocess with a
   hard deadline, so a runaway match fails promptly and names the pattern.
   semgrep and wcmatch are required here: if either moves, the test fails and
   asks for the probe to be updated rather than skipping silently.

SCOPE. This guards against drift in checked-in configuration. It does not
try to defeat a pull request that deliberately edits this file, the rules
and the workflow together; osv-scanner.toml records that residual limit.

v2-handshake-lint runs this file as its own step before the semgrep steps.

Run: `cd plugins/launchpad/scripts && python -m pytest -q
tests/test_osv_wcmatch_exposure.py`. When the suppression is removed (a
semgrep release allows wcmatch >= 11.2), every test here skips.
"""

from __future__ import annotations

import inspect
import json
import re
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parents[1]
_REPO_ROOT = _SCRIPTS.parents[2]
_OSV_CONFIG = _SCRIPTS / "osv-scanner.toml"
_RULES_DIR = _REPO_ROOT / "plugins" / "launchpad" / ".semgrep"
_ADVISORY = "GHSA-53pm-wgxf-c6qf"

# Per hostile path, across one expanded pattern. Measured at well under 1 ms;
# the advisory's own shape needs about 1.3 s for a 120-byte name and over a
# minute at 255 bytes, so this bound separates the two by orders of magnitude.
_PER_PATH_BUDGET_S = 0.5
# Hard deadline for one pattern's subprocess: interpreter start-up plus every
# hostile path at full budget, with headroom for a slow CI runner.
_PATTERN_DEADLINE_S = 15.0
_NAME_MAX = 255

_LINE_CONTINUATION = re.compile(r"\\\r?\n")
_SHELL_SEPARATORS = frozenset({"|", "||", "&", "&&", ";", ";;"})
_PATTERN_FLAGS = ("--include", "--exclude")
_CONFIG_FLAGS = ("--config", "-c", "-f")
# The current layout: two calls in lefthook.yml, two in v2-handshake-lint.
# Fewer found means the parser drifted, not that the calls went away.
_MIN_EXPECTED_INVOCATIONS = 4

# Child process: time one pattern against every hostile path with the same
# flags semgrep uses (asserted against semgrep's source below).
_PROBE = r"""
import json, sys, time
from wcmatch import glob
pattern = sys.argv[1]
paths = json.load(sys.stdin)
flags = glob.GLOBSTAR | glob.DOTGLOB
timings = {}
for label, path in paths.items():
    started = time.perf_counter()
    glob.globmatch(path, pattern, flags=flags)
    timings[label] = time.perf_counter() - started
print(json.dumps(timings))
"""

pytestmark = pytest.mark.skipif(
    _ADVISORY
    not in {
        entry.get("id")
        for entry in tomllib.loads(_OSV_CONFIG.read_text(encoding="utf-8")).get(
            "IgnoredVulns", []
        )
    },
    reason=f"{_ADVISORY} is no longer suppressed; nothing to guard.",
)


def _rule_files() -> list[Path]:
    return sorted(_RULES_DIR.glob("*.y*ml"))


def _rule_path_patterns() -> list[tuple[str, str, str, str]]:
    """Return (rule file, rule id, include|exclude, pattern) for every rule."""
    found: list[tuple[str, str, str, str]] = []
    for rule_file in _rule_files():
        document = yaml.safe_load(rule_file.read_text(encoding="utf-8")) or {}
        for rule in document.get("rules", []):
            paths = rule.get("paths") or {}
            for kind in ("include", "exclude"):
                for pattern in paths.get(kind, []) or []:
                    found.append((rule_file.name, rule["id"], kind, pattern))
    return found


def _single_star_count(segment: str) -> int:
    return segment.replace("**", "").count("*")


def _yaml_strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _yaml_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _yaml_strings(value)


def _shell_tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        return list(lexer)
    except ValueError:  # unbalanced quotes: fall back to a plain split
        return command.split()


def _semgrep_invocations(yaml_text: str) -> list[list[str]]:
    """Return the argument list of every semgrep call in a YAML document.

    YAML parsing drops YAML comments and folds `>` scalars; joining shell
    `\\`-newline continuations turns each multi-line command into one logical
    line, which is then split into shell tokens (quotes removed, `#` comments
    dropped). A call's arguments run until the next shell separator.
    """
    invocations: list[list[str]] = []
    for text in _yaml_strings(yaml.safe_load(yaml_text)):
        for line in _LINE_CONTINUATION.sub(" ", text).splitlines():
            tokens = _shell_tokens(line)
            for index, token in enumerate(tokens):
                if token != "semgrep" and not token.endswith("/semgrep"):
                    continue
                args: list[str] = []
                for arg in tokens[index + 1 :]:
                    if arg in _SHELL_SEPARATORS:
                        break
                    args.append(arg)
                invocations.append(args)
    return invocations


def _configs(args: list[str]) -> list[str]:
    configs: list[str] = []
    for index, arg in enumerate(args):
        if arg in _CONFIG_FLAGS:
            configs.append(args[index + 1] if index + 1 < len(args) else "")
        elif arg.startswith("--config="):
            configs.append(arg.split("=", 1)[1])
    return configs


def _invocation_problems(yaml_text: str) -> list[str]:
    covered = {path.resolve() for path in _rule_files()}
    problems: list[str] = []
    for args in _semgrep_invocations(yaml_text):
        command = shlex.join(["semgrep", *args])
        flags = [
            arg
            for arg in args
            if arg in _PATTERN_FLAGS
            or arg.startswith(tuple(f"{flag}=" for flag in _PATTERN_FLAGS))
        ]
        if flags:
            problems.append(f"passes {flags}: {command}")
        configs = _configs(args)
        if not configs:
            problems.append(f"no --config, so semgrep picks its own rules: {command}")
        for config in configs:
            if (_REPO_ROOT / config).resolve() not in covered:
                problems.append(
                    f"--config {config!r} is not a rule file this guard checks: "
                    f"{command}"
                )
    return problems


def _invocation_files() -> list[Path]:
    return [_REPO_ROOT / "lefthook.yml"] + sorted(
        (_REPO_ROOT / ".github" / "workflows").glob("*.y*ml")
    )


def test_rule_files_exist() -> None:
    assert _RULES_DIR.is_dir(), f"missing semgrep rules dir: {_RULES_DIR}"
    assert _rule_path_patterns(), "no rule paths: patterns found; parser drift?"


def test_no_rule_pattern_has_multiple_stars_in_a_segment() -> None:
    offenders = [
        f"{rule_file}:{rule_id} {kind} {pattern!r}"
        for rule_file, rule_id, kind, pattern in _rule_path_patterns()
        if any(_single_star_count(seg) > 1 for seg in pattern.split("/"))
    ]
    assert not offenders, (
        f"These semgrep path patterns have several `*` in one segment, the shape "
        f"{_ADVISORY} (wcmatch ReDoS) needs. Rewrite them, or remove the "
        f"suppression from osv-scanner.toml once semgrep allows wcmatch >= 11.2:\n"
        + "\n".join(offenders)
    )


_GOOD_CONFIG = "--config=plugins/launchpad/.semgrep/general.yml"


@pytest.mark.parametrize(
    ("layout", "yaml_text"),
    [
        (
            "flag on a continuation line (the workflow's layout)",
            (
                "steps:\n"
                "  - run: |\n"
                "      set +e\n"
                f"      timeout 120 semgrep {_GOOD_CONFIG} \\\n"
                "        --exclude=vendor plugins/launchpad/scripts/ --error --metrics=off\n"
            ),
        ),
        (
            "flag inside a folded scalar",
            (
                "steps:\n"
                "  - run: >\n"
                f"      semgrep {_GOOD_CONFIG}\n"
                "      --include '*_*_*.py' plugins/launchpad/scripts/\n"
            ),
        ),
        (
            "flag on one physical line (lefthook's layout)",
            (
                "pre-commit:\n"
                "  commands:\n"
                "    semgrep-general:\n"
                f"      run: xargs -0 -r semgrep --exclude x {_GOOD_CONFIG} --\n"
            ),
        ),
        (
            "quoted option name",
            f'run: semgrep {_GOOD_CONFIG} "--exclude" vendor plugins/\n',
        ),
        (
            "quoted --flag=value argument",
            f"run: semgrep {_GOOD_CONFIG} '--include=*_*_*.py' plugins/\n",
        ),
        (
            "config outside the guarded rules dir",
            "run: semgrep --config=p/python plugins/launchpad/scripts/\n",
        ),
        (
            "short -c config flag outside the guarded rules dir",
            "run: semgrep -c other/rules.yml plugins/launchpad/scripts/\n",
        ),
        (
            "no config at all",
            "run: semgrep plugins/launchpad/scripts/ --error\n",
        ),
    ],
)
def test_invocation_scan_catches(layout: str, yaml_text: str) -> None:
    assert _invocation_problems(yaml_text), layout


@pytest.mark.parametrize(
    ("layout", "yaml_text"),
    [
        (
            "shell comment line",
            (
                "steps:\n"
                "  - run: |\n"
                "      # a semgrep --include/--exclude flag would reach wcmatch\n"
                f"      semgrep {_GOOD_CONFIG} plugins/launchpad/scripts/\n"
            ),
        ),
        (
            "inline shell comment",
            f"run: semgrep {_GOOD_CONFIG} plugins/ --error  # no --exclude here\n",
        ),
        (
            "semgrep in a pipeline with other commands",
            (
                "run: git diff --name-only | xargs -0 -r semgrep "
                f"{_GOOD_CONFIG} -- && echo --exclude\n"
            ),
        ),
    ],
)
def test_invocation_scan_allows(layout: str, yaml_text: str) -> None:
    assert not _invocation_problems(yaml_text), layout


def test_no_other_pattern_sources() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "--", ".semgrepignore", "**/.semgrepignore"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.split()
    assert not tracked, (
        f"A tracked .semgrepignore feeds patterns to wcmatch; re-assess the "
        f"{_ADVISORY} suppression before adding one: {tracked}"
    )

    found = 0
    problems: list[str] = []
    for path in _invocation_files():
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        found += len(_semgrep_invocations(text))
        problems += [
            f"{path.relative_to(_REPO_ROOT)}: {problem}"
            for problem in _invocation_problems(text)
        ]
    assert found >= _MIN_EXPECTED_INVOCATIONS, (
        f"found only {found} semgrep invocations in lefthook.yml and "
        f".github/workflows/; the parser has drifted from how they are written"
    )
    assert not problems, (
        f"semgrep invocations outside what the {_ADVISORY} suppression "
        f"assessed:\n" + "\n".join(problems)
    )


def test_hostile_paths_match_quickly() -> None:
    try:
        from semgrep.target_manager import TargetManager
        from wcmatch import glob as wcmatch_glob  # noqa: F401  (probe dependency)
    except ImportError as exc:
        pytest.fail(
            f"semgrep or wcmatch moved ({exc}); update this probe, it guards the "
            f"{_ADVISORY} suppression."
        )

    matcher_source = inspect.getsource(TargetManager._globmatch)
    assert "globmatch" in matcher_source and (
        "glob.GLOBSTAR | glob.DOTGLOB" in matcher_source
    ), (
        "semgrep changed how it calls wcmatch; update _PROBE's flags to match:\n"
        + matcher_source
    )

    patterns: list[str] = []
    for _, rule_id, kind, pattern in _rule_path_patterns():
        patterns += TargetManager.preprocess_path_patterns(
            rule_id=rule_id,
            patterns=[pattern],
            is_include=kind == "include",
            legacy_rule_filtering=False,
        )

    base = "/plugins/launchpad/scripts/"
    hostile = json.dumps(
        {
            "long name": base + "a" * _NAME_MAX,
            "all dots": base + "." * _NAME_MAX,
            "near-miss .j2": base + ".j2x" * (_NAME_MAX // 4),
            "near-miss .py": base + ".p" * (_NAME_MAX // 2) + "x",
            "underscores": base + "_" * _NAME_MAX,
            "deep dirs": base + "a/" * 2000 + "x.j3",
            "deep _vendo dirs": base + "_vendo/" * 500 + "x",
            "deep test dirs": base + "test/" * 700 + "x",
        }
    )

    problems: list[str] = []
    for pattern in sorted(set(patterns)):
        try:
            probe = subprocess.run(
                [sys.executable, "-c", _PROBE, pattern],
                input=hostile,
                capture_output=True,
                text=True,
                check=True,
                timeout=_PATTERN_DEADLINE_S,
            )
        except subprocess.TimeoutExpired:
            problems.append(f"{pattern!r}: no result within {_PATTERN_DEADLINE_S}s")
            continue
        for label, elapsed in json.loads(probe.stdout).items():
            if elapsed > _PER_PATH_BUDGET_S:
                problems.append(f"{pattern!r} vs {label}: {elapsed:.2f}s")
    assert not problems, (
        f"Hostile paths are slow to match LaunchPad's semgrep path patterns, so "
        f"the {_ADVISORY} suppression no longer holds:\n" + "\n".join(problems)
    )
