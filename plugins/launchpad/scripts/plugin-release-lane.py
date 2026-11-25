"""Resolve, check and complete LaunchPad releases from CI."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

MANIFESTS = (
    "plugins/launchpad/.claude-plugin/plugin.json",
    "plugins/launchpad/.codex-plugin/plugin.json",
)
LINTERS = (
    (
        "plugins/launchpad/scripts/plugin-v2-handshake-lint.py",
        "--check-version-coherence",
        "--phase=post-tag",
    ),
    ("plugins/launchpad/scripts/plugin-v2-handshake-lint.py", "--check-leakage"),
    ("plugins/launchpad/scripts/plugin-v2-handshake-lint.py", "--check-freshness"),
    ("plugins/launchpad/scripts/plugin-freshness-check.py", "--gating"),
)
SHIPPED_PREFIX = "plugins/launchpad/"
SHIPPED_EXCLUDE = (
    "plugins/launchpad/scripts/tests/",
    "plugins/launchpad/scripts/plugin_stack_adapters/tests/",
)
NO_RELEASE_LABEL = "no-release"
SEMVER = re.compile(r"\d+\.\d+\.\d+", re.ASCII)
SHA = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")


class Refusal(Exception):
    """A release precondition or external operation failed."""


def execute(
    argv: list[str], root: Path | None = None, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=root,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal(
            f"{argv[0]} could not complete. Repair the tool or connection and retry."
        ) from exc


def command(argv: list[str], root: Path | None = None) -> str:
    result = execute(argv, root)
    if result.returncode:
        # Do not echo subprocess diagnostics: authentication errors can contain
        # credentials, and public workflow logs must not expose them.
        raise Refusal(
            f"{argv[0]} {argv[1]} failed. Repair access or repository state and retry."
        )
    return result.stdout.strip()


def git(root: Path, *args: str) -> str:
    return command(["git", *args], root)


def version_from_json(text: str) -> str:
    value = json.loads(text)
    if not isinstance(value, dict) or not isinstance(value.get("version"), str):
        raise ValueError("manifest has no version string")
    return value["version"]


def working_versions(root: Path) -> tuple[list[str | None], list[str]]:
    versions: list[str | None] = []
    errors = []
    for path in MANIFESTS:
        try:
            versions.append(
                version_from_json((root / path).read_text(encoding="utf-8"))
            )
        except (OSError, ValueError):
            versions.append(None)
            errors.append(f"Manifest {path} is unreadable. Restore its version string.")
    return versions, errors


def current_version(root: Path) -> str:
    versions, errors = working_versions(root)
    version = versions[0]
    if errors or version is None or not SEMVER.fullmatch(version):
        raise Refusal("Version must be plain semver. Repair both plugin manifests.")
    return version


def error(message: str) -> None:
    print(f"::error::{message}", file=sys.stderr)


def checks(root: Path) -> int:
    versions, errors = working_versions(root)
    version = versions[0]
    plain = version is not None and SEMVER.fullmatch(version) is not None
    if not plain:
        errors.append("Version must be plain semver. Use MAJOR.MINOR.PATCH.")
    if versions[0] != versions[1]:
        errors.append(
            "Plugin manifest versions differ. Set both manifests to one version."
        )
    # Invalid versions still receive the other diagnostics, without allowing a
    # manifest value to escape the release notes directory.
    if version is not None and re.fullmatch(r"[\w.+-]+", version, re.ASCII):
        notes = root / "docs" / "releases" / f"v{version}.md"
        try:
            text = notes.read_text(encoding="utf-8")
            if not re.search(r"^# ", text, re.MULTILINE):
                errors.append(
                    "Release notes need a '# ' heading. Add the release heading."
                )
        except OSError:
            errors.append(
                f"Release notes docs/releases/v{version}.md are missing. Add them."
            )
    else:
        errors.append(
            "Release notes cannot be resolved. Repair the version and add notes."
        )
    tags = git(root, "tag", "--list").splitlines()
    if plain and f"v{version}" not in tags:
        assert version is not None
        numbers = tuple(map(int, version.split(".")))
        higher = [
            tag
            for tag in tags
            if tag.startswith("v")
            and SEMVER.fullmatch(tag[1:])
            and tuple(map(int, tag[1:].split("."))) > numbers
        ]
        if higher:
            highest = max(higher, key=lambda tag: tuple(map(int, tag[1:].split("."))))
            errors.append(
                f"v{version} is below {highest}. Bump above it; if that release already "
                "contains this commit, this version is skipped and nothing is lost."
            )
    if errors:
        for message in errors:
            error(message)
        return 1
    for args in LINTERS:
        result = execute([sys.executable, *args], root, timeout=600)
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        if result.returncode:
            raise Refusal(f"Release check {args[1]} failed. Fix that check and retry.")
    return 0


@dataclass(frozen=True)
class RemoteState:
    tag: str | None
    main: str
    stable: str | None
    release: str


def remote_state(root: Path, tag: str) -> RemoteState:
    tag_ref = f"refs/tags/{tag}"
    main_ref = "refs/heads/main"
    stable_ref = "refs/heads/stable"
    output = git(
        root, "ls-remote", "origin", tag_ref, f"{tag_ref}^{{}}", main_ref, stable_ref
    )
    refs = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 2 or not SHA.fullmatch(fields[0]):
            raise Refusal("Remote refs are unreadable. Repair origin and retry.")
        refs[fields[1]] = fields[0]
    if main_ref not in refs:
        raise Refusal("origin/main is missing. Restore main before releasing.")
    fetch = ["fetch", "--no-tags", "origin", f"{main_ref}:refs/remotes/origin/main"]
    if stable_ref in refs:
        fetch.append(f"{stable_ref}:refs/remotes/origin/stable")
    if tag_ref in refs:
        fetch.append(f"{tag_ref}:{tag_ref}")
    git(root, *fetch)
    releases_text = command(
        ["gh", "release", "list", "--limit", "100", "--json", "tagName,isDraft"], root
    )
    try:
        releases = json.loads(releases_text)
        if not isinstance(releases, list) or any(
            not isinstance(entry, dict)
            or not isinstance(entry.get("tagName"), str)
            or not isinstance(entry.get("isDraft"), bool)
            for entry in releases
        ):
            raise ValueError("invalid release listing")
    except ValueError as exc:
        raise Refusal(
            "Release list is unreadable. Repair GitHub access and retry."
        ) from exc
    release = "absent"
    for entry in releases:
        if entry["tagName"] == tag:
            release = "draft" if entry["isDraft"] else "published"
            break
    return RemoteState(
        refs.get(f"{tag_ref}^{{}}", refs.get(tag_ref)),
        refs[main_ref],
        refs.get(stable_ref),
        release,
    )


def ancestor(root: Path, older: str, newer: str) -> bool:
    result = execute(["git", "merge-base", "--is-ancestor", older, newer], root)
    if result.returncode not in (0, 1):
        raise Refusal("Git ancestry check failed. Fetch full history and retry.")
    return result.returncode == 0


def release_sha(root: Path, value: str | None) -> str:
    if value is None or not SHA.fullmatch(value):
        raise Refusal(
            "Release SHA is missing or invalid. Set the full commit SHA and retry."
        )
    return git(root, "rev-parse", "--verify", f"{value}^{{commit}}")


def validate_commit(root: Path, sha: str, version: str, state: RemoteState) -> None:
    if not ancestor(root, sha, state.main):
        raise Refusal(
            "Release commit is not on origin/main. Merge it to main before releasing."
        )
    if state.tag is not None:
        if not ancestor(root, state.tag, state.main):
            raise Refusal(
                "Release tag is off main. The maintainer must repair the tag."
            )
        for path in MANIFESTS:
            text = git(root, "show", f"{state.tag}:{path}")
            try:
                matches = version_from_json(text) == version
            except ValueError:
                matches = False
            if not matches:
                raise Refusal(
                    f"Release tag manifest {path} holds another version. "
                    "The maintainer must repair the tag; it will not be moved."
                )


def plan(root: Path) -> int:
    version = current_version(root)
    tag = f"v{version}"
    state = remote_state(root, tag)
    done = (
        state.tag is not None
        and state.release == "published"
        and state.stable is not None
        and ancestor(root, state.tag, state.stable)
    )
    sha = (
        state.tag
        if state.tag is not None
        else release_sha(root, os.environ.get("GITHUB_SHA"))
    )
    if not done:
        validate_commit(root, sha, version, state)
    output = f"version={version}\ntag={tag}\nsha={sha}\ndone={str(done).lower()}\n"
    destination = os.environ.get("GITHUB_OUTPUT")
    if destination:
        with Path(destination).open("a", encoding="utf-8") as handle:
            handle.write(output)
    else:
        print(output, end="")
    return 0


def publish(root: Path) -> int:
    version = current_version(root)
    tag = f"v{version}"
    sha = release_sha(root, os.environ.get("RELEASE_SHA"))
    if git(root, "rev-parse", "HEAD") != sha:
        raise Refusal(
            "HEAD is not RELEASE_SHA. Check out the release commit before publishing."
        )
    state = remote_state(root, tag)
    validate_commit(root, sha, version, state)
    if state.tag is not None and state.tag != sha:
        raise Refusal(
            "Release tag differs from RELEASE_SHA. Use the tagged commit; never move it."
        )
    if state.release == "draft":
        raise Refusal(
            "A draft release exists. The maintainer must publish or delete the draft."
        )
    move_stable = state.stable is None
    if state.stable is not None and state.stable != sha:
        if ancestor(root, state.stable, sha):
            move_stable = True
        elif not ancestor(root, sha, state.stable):
            raise Refusal(
                "stable has diverged. The maintainer must repair stable before retrying."
            )
    # All state rows have been validated before the first write.
    if state.tag is None:
        git(root, "tag", "-a", tag, "-m", f"LaunchPad {tag}", sha)
        git(root, "push", "origin", f"refs/tags/{tag}")
    if state.release == "absent":
        for attempt in range(3):
            try:
                command(
                    [
                        "gh",
                        "release",
                        "create",
                        tag,
                        "--verify-tag",
                        "--title",
                        f"LaunchPad {tag}",
                        "--notes-file",
                        f"docs/releases/{tag}.md",
                    ],
                    root,
                )
                break
            except Refusal:
                if attempt == 2:
                    raise
                time.sleep(10)
    if move_stable:
        git(root, "push", "origin", f"{sha}:refs/heads/stable")
    print(f"Release {tag} is published and stable contains {sha}.")
    return 0


def pr_guard(root: Path) -> int:
    versions, errors = working_versions(root)
    if errors or versions[0] != versions[1]:
        raise Refusal(
            "Plugin manifest versions differ or are unreadable. Repair both manifests."
        )
    base = os.environ.get("LP_BASE_REF", "origin/main")
    try:
        previous = version_from_json(git(root, "show", f"{base}:{MANIFESTS[0]}"))
    except ValueError as exc:
        raise Refusal(
            "Base manifest is unreadable. Restore its version and retry."
        ) from exc
    if versions[0] != previous:
        print("Version changed: running all release checks.")
        return checks(root)
    changed = git(
        root, "diff", "--no-renames", "--name-only", "-z", f"{base}...HEAD"
    ).split("\0")
    shipped = [
        path
        for path in changed
        if path.startswith(SHIPPED_PREFIX) and not path.startswith(SHIPPED_EXCLUDE)
    ]
    try:
        labels = json.loads(os.environ.get("PR_LABELS", "[]"))
    except ValueError:
        labels = []
    if not isinstance(labels, list) or not all(
        isinstance(label, str) for label in labels
    ):
        labels = []
    if shipped and NO_RELEASE_LABEL not in labels:
        raise Refusal(
            "Shipped files changed: "
            + ", ".join(shipped)
            + ". Bump the version, or add no-release when this change should ride with the next release."
        )
    print(
        "no-release label defers shipped changes."
        if shipped
        else "No shipped files changed."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("checks", "plan", "publish", "pr-guard"))
    args = parser.parse_args()
    try:
        root = Path(command(["git", "rev-parse", "--show-toplevel"]))
        return {
            "checks": checks,
            "plan": plan,
            "publish": publish,
            "pr-guard": pr_guard,
        }[args.command](root)
    except Refusal as exc:
        error(str(exc))
        return 1
    except (OSError, UnicodeError) as exc:
        error(
            f"Repository file operation failed ({type(exc).__name__}). Repair files and retry."
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
