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
2. No `.semgrepignore` is tracked, and no semgrep command in lefthook.yml or
   .github/workflows/ passes `--include` or `--exclude`.
3. With semgrep's own pattern preprocessing and matching flags, hostile paths
   (255-byte names, near-miss suffixes, deep directory chains) match every
   expanded pattern quickly. This is the reproducible form of the timing
   probe cited in the suppression reason.

Run: `cd plugins/launchpad/scripts && python -m pytest -q
tests/test_osv_wcmatch_exposure.py`. When the suppression is removed (a
semgrep release allows wcmatch >= 11.2), every test here skips.
"""

from __future__ import annotations

import re
import subprocess
import time
import tomllib
from pathlib import Path

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parents[1]
_REPO_ROOT = _SCRIPTS.parents[2]
_OSV_CONFIG = _SCRIPTS / "osv-scanner.toml"
_RULES_DIR = _REPO_ROOT / "plugins" / "launchpad" / ".semgrep"
_ADVISORY = "GHSA-53pm-wgxf-c6qf"

# Generous ceiling per hostile path across all expanded patterns. Measured at
# under 4 ms locally; the advisory's own shape needs about 1.3 s for a 120-byte
# name and over a minute at 255 bytes, so this bound separates the two by
# orders of magnitude in both directions.
_PER_PATH_BUDGET_S = 0.5
_NAME_MAX = 255

_CLI_PATTERN_FLAG = re.compile(r"--(?:include|exclude)(?:=|\s)")

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


def _rule_path_patterns() -> list[tuple[str, str, str, str]]:
    """Return (rule file, rule id, include|exclude, pattern) for every rule."""
    found: list[tuple[str, str, str, str]] = []
    for rule_file in sorted(_RULES_DIR.glob("*.y*ml")):
        document = yaml.safe_load(rule_file.read_text(encoding="utf-8")) or {}
        for rule in document.get("rules", []):
            paths = rule.get("paths") or {}
            for kind in ("include", "exclude"):
                for pattern in paths.get(kind, []) or []:
                    found.append((rule_file.name, rule["id"], kind, pattern))
    return found


def _single_star_count(segment: str) -> int:
    return segment.replace("**", "").count("*")


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

    invocation_files = [_REPO_ROOT / "lefthook.yml"] + sorted(
        (_REPO_ROOT / ".github" / "workflows").glob("*.y*ml")
    )
    offenders = [
        f"{path.relative_to(_REPO_ROOT)}: {line.strip()}"
        for path in invocation_files
        if path.is_file()
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
        and "semgrep" in line
        and _CLI_PATTERN_FLAG.search(line)
    ]
    assert not offenders, (
        f"semgrep --include/--exclude patterns reach wcmatch; re-assess the "
        f"{_ADVISORY} suppression:\n" + "\n".join(offenders)
    )


def test_hostile_paths_match_quickly() -> None:
    target_manager = pytest.importorskip("semgrep.target_manager")
    wcmatch_glob = pytest.importorskip("wcmatch.glob")

    patterns: list[str] = []
    for _, rule_id, kind, pattern in _rule_path_patterns():
        patterns += target_manager.TargetManager.preprocess_path_patterns(
            rule_id=rule_id,
            patterns=[pattern],
            is_include=kind == "include",
            legacy_rule_filtering=False,
        )
    flags = wcmatch_glob.GLOBSTAR | wcmatch_glob.DOTGLOB  # as semgrep calls it

    base = "/plugins/launchpad/scripts/"
    hostile = {
        "long name": base + "a" * _NAME_MAX,
        "all dots": base + "." * _NAME_MAX,
        "near-miss .j2": base + ".j2x" * (_NAME_MAX // 4),
        "near-miss .py": base + ".p" * (_NAME_MAX // 2) + "x",
        "underscores": base + "_" * _NAME_MAX,
        "deep dirs": base + "a/" * 2000 + "x.j3",
        "deep _vendo dirs": base + "_vendo/" * 500 + "x",
        "deep test dirs": base + "test/" * 700 + "x",
    }
    slow = []
    for label, path in hostile.items():
        started = time.perf_counter()
        for pattern in patterns:
            wcmatch_glob.globmatch(path, pattern, flags=flags)
        elapsed = time.perf_counter() - started
        if elapsed > _PER_PATH_BUDGET_S:
            slow.append(f"{label}: {elapsed:.2f}s")
    assert not slow, (
        f"Hostile paths are slow to match LaunchPad's semgrep path patterns, so "
        f"the {_ADVISORY} suppression no longer holds:\n" + "\n".join(slow)
    )
