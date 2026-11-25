"""Release contract probes against disposable local Git remotes."""

import json
import os
import shutil
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "plugin-release-lane.py"
MANIFESTS = (
    "plugins/launchpad/.claude-plugin/plugin.json",
    "plugins/launchpad/.codex-plugin/plugin.json",
)
LINTERS = [
    ["plugins/launchpad/scripts/plugin-v2-handshake-lint.py", "--check-version-coherence", "--phase=post-tag"],
    ["plugins/launchpad/scripts/plugin-v2-handshake-lint.py", "--check-leakage"],
    ["plugins/launchpad/scripts/plugin-v2-handshake-lint.py", "--check-freshness"],
    ["plugins/launchpad/scripts/plugin-freshness-check.py", "--gating"],
]


class LaneRepo:
    def __init__(self, root):
        self.root = root
        self.origin = root / "origin.git"
        self.repo = root / "clone"
        self.bin = root / "bin"
        self.bin.mkdir()
        self.calls_file = root / "calls.jsonl"
        self.state_file = root / "releases.json"
        self.state_file.write_text("[]")
        real_git = shutil.which("git")
        git_stub = self.bin / "git"
        git_stub.write_text(f"#!{sys.executable}\n" +
            "import json, os, sys\n"
            "args = sys.argv[1:]\n"
            "if args and (args[0] == 'push' or (args[0] == 'tag' and '--list' not in args)):\n"
            "    with open(os.environ['LANE_CALLS'], 'a') as log:\n"
            "        log.write(json.dumps(['git-write', *args]) + '\\n')\n"
            "if args[:2] == ['ls-remote', 'origin'] and 'LS_REMOTE_OUTPUT' in os.environ:\n"
            "    print(os.environ['LS_REMOTE_OUTPUT'])\n"
            "    sys.exit(0)\n"
            "if args[:3] == ['ls-remote', '--tags', 'origin']:\n"
            "    if os.environ.get('FAIL_LS_TAGS'):\n"
            "        sys.exit(1)\n"
            "    if 'LS_TAGS_OUTPUT' in os.environ:\n"
            "        print(os.environ['LS_TAGS_OUTPUT'])\n"
            "        sys.exit(0)\n"
            f"os.execv({real_git!r}, [{real_git!r}, *args])\n")
        git_stub.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "GH_TOKEN": "fixture-only",
            "LANE_CALLS": str(self.calls_file),
            "LANE_STATE": str(self.state_file),
        }
        for key in ("GITHUB_OUTPUT", "PR_LABELS", "LP_BASE_REF", "RELEASE_SHA", "GITHUB_SHA"):
            self.env.pop(key, None)
        self.exec_git(root, "init", "--bare", "--initial-branch=main", str(self.origin))
        self.exec_git(root, "clone", str(self.origin), str(self.repo))
        for key, value in (("user.name", "Fixture"), ("user.email", "fixture@example.invalid"),
                           ("commit.gpgsign", "false"), ("tag.gpgsign", "false")):
            self.git("config", key, value)
        self.version("2.5.0")
        self.write("docs/releases/v2.5.0.md", "# Release v2.5.0\n\nFixture notes.\n")
        stub = '''import json, os, sys
from pathlib import Path
with open(os.environ["LANE_CALLS"], "a") as log:
    log.write(json.dumps(["lint", str(Path(__file__).relative_to(Path.cwd())), *sys.argv[1:]]) + "\\n")
sys.exit(1 if os.environ.get("FAIL_LINT") in sys.argv[1:] else 0)
'''
        for path in {args[0] for args in LINTERS}:
            self.write(path, stub)
        gh = self.bin / "gh"
        gh.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["LANE_CALLS"], "a") as log:
    log.write(json.dumps(["gh", *args]) + "\\n")
state = Path(os.environ["LANE_STATE"])
if args[0] == "api":
    if os.environ.get("FAIL_GH"):
        sys.exit(1)
    if "RELEASE_API_OUTPUT" in os.environ:
        print(os.environ["RELEASE_API_OUTPUT"])
    else:
        entries = [{"tag_name": entry["tagName"], "draft": entry["isDraft"]} for entry in json.loads(state.read_text())]
        pages = [entries[i:i+100] for i in range(0, len(entries), 100)] or [[]]
        print(json.dumps(pages))
elif args[:2] == ["release", "create"]:
    if os.environ.get("CREATE_TIMES"):
        with open(os.environ["CREATE_TIMES"], "a") as times:
            times.write(json.dumps(__import__("time").monotonic()) + "\\n")
    if os.environ.get("CREATE_FAILURES"):
        calls = [json.loads(line) for line in Path(os.environ["LANE_CALLS"]).read_text().splitlines()]
        attempts = sum(call[:3] == ["gh", "release", "create"] for call in calls)
        if attempts <= int(os.environ["CREATE_FAILURES"]):
            sys.exit(1)
    entries = json.loads(state.read_text())
    entries.append({"tagName": args[2], "isDraft": False})
    state.write_text(json.dumps(entries))
else:
    sys.exit(2)
''')
        gh.chmod(0o755)
        self.initial = self.commit()
        self.git("push", "origin", "main")
        self.env["GITHUB_SHA"] = self.initial
        self.env["RELEASE_SHA"] = self.initial

    def exec_git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, env=self.env,
                              text=True, capture_output=True, check=True, timeout=15).stdout.strip()

    def git(self, *args):
        return self.exec_git(self.repo, *args)

    def write(self, path, content):
        dest = self.repo / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)

    def version(self, version, codex=None):
        for path, value in zip(MANIFESTS, (version, codex or version)):
            self.write(path, json.dumps({"version": value}) + "\n")

    def commit(self):
        self.git("add", ".")
        self.git("commit", "--allow-empty", "-m", "fixture")
        return self.git("rev-parse", "HEAD")

    def advance(self):
        self.write("docs/next.md", "next\n")
        sha = self.commit()
        self.git("push", "origin", "main")
        self.env.update(GITHUB_SHA=sha, RELEASE_SHA=sha)
        return sha

    def tag(self, sha=None, annotated=False, tag="v2.5.0"):
        args = ["tag"]
        if annotated:
            args += ["-a", "-m", "fixture"]
        self.git(*args, tag, sha or self.initial)
        self.git("push", "origin", f"refs/tags/{tag}")

    def stable(self, sha):
        self.git("push", "origin", f"{sha}:refs/heads/stable")

    def release(self, draft=False):
        self.state_file.write_text(json.dumps([{"tagName": "v2.5.0", "isDraft": draft}]))

    def refs(self):
        return self.exec_git(self.root, "--git-dir", str(self.origin), "show-ref") if self.exec_git(
            self.root, "--git-dir", str(self.origin), "for-each-ref", "--format=%(refname)") else ""

    def calls(self, kind=None):
        calls = [json.loads(line) for line in self.calls_file.read_text().splitlines()] if self.calls_file.exists() else []
        return [call for call in calls if kind is None or call[0] == kind]

    def creates(self):
        return [call for call in self.calls("gh") if call[1:3] == ["release", "create"]]

    def run(self, command, code=0, **env):
        result = subprocess.run([sys.executable, str(SCRIPT), command], cwd=self.repo,
                                env={**self.env, **env}, text=True, capture_output=True,
                                check=False, timeout=30)
        assert result.returncode == code, result.stdout + result.stderr
        if code == 1:
            assert "::error::" in result.stdout + result.stderr
        return result.stdout + result.stderr

    def unchanged(self, command, code=0, **env):
        before = self.refs(), self.state_file.read_text(), len(self.creates()), len(self.calls("git-write"))
        output = self.run(command, code, **env)
        assert (self.refs(), self.state_file.read_text(), len(self.creates()), len(self.calls("git-write"))) == before
        return output


@pytest.fixture
def lane(tmp_path):
    return LaneRepo(tmp_path)


def test_p1_normal_release(lane):
    output = lane.unchanged("plan")
    assert "done=false" in output and f"sha={lane.initial}" in output
    assert "version=2.5.0" in output and "tag=v2.5.0" in output
    lane.run("checks")
    lane.run("publish")
    assert lane.git("ls-remote", "origin", "refs/tags/v2.5.0^{}").split()[0] == lane.initial
    assert lane.git("cat-file", "-t", "v2.5.0") == "tag"
    assert f"{lane.initial}\trefs/heads/stable" in lane.git("ls-remote", "origin", "refs/heads/stable")
    assert lane.creates() == [["gh", "release", "create", "v2.5.0", "--verify-tag", "--title", "LaunchPad v2.5.0", "--notes-file", "docs/releases/v2.5.0.md"]]


@pytest.mark.parametrize("ahead", [False, True], ids=["P2", "P2b"])
def test_completed_release(lane, ahead):
    lane.tag(annotated=True)
    lane.release()
    lane.stable(lane.advance() if ahead else lane.initial)
    assert "done=true" in lane.unchanged("plan")
    assert lane.calls("lint") == []


def test_p3_earlier_lightweight_hand_tag(lane):
    lane.tag()
    lane.advance()
    assert f"sha={lane.initial}" in lane.unchanged("plan")
    lane.git("checkout", "--detach", lane.initial)
    lane.run("publish", RELEASE_SHA=lane.initial)
    assert lane.git("ls-remote", "origin", "refs/tags/v2.5.0").split()[0] == lane.initial
    assert lane.git("ls-remote", "origin", "refs/heads/stable").split()[0] == lane.initial
    assert len(lane.creates()) == 1


@pytest.mark.parametrize("published", [False, True], ids=["P4", "P5"])
def test_resume_stable_behind(lane, published):
    lane.stable(lane.initial)
    sha = lane.advance()
    lane.tag(sha, annotated=True)
    tag_ref = lane.git("rev-parse", "v2.5.0")
    if published:
        lane.release()
    lane.run("publish")
    assert lane.git("rev-parse", "v2.5.0") == tag_ref
    assert lane.git("ls-remote", "origin", "refs/heads/stable").split()[0] == sha
    assert len(lane.creates()) == (0 if published else 1)


@pytest.mark.parametrize("case", ["P6", "P7", "P8", "P9"])
def test_invalid_release_commit(lane, case):
    if case in ("P6", "P8"):
        lane.git("checkout", "-b", "side")
        lane.write("side.txt", "side\n")
        sha = lane.commit()
        if case == "P6":
            lane.tag(sha)
    elif case == "P7":
        lane.version("2.4.0")
        sha = lane.commit()
        lane.git("push", "origin", "main")
        lane.tag(sha)
        lane.version("2.5.0")
        sha = lane.commit()
        lane.git("push", "origin", "main")
    else:
        lane.version("2.5.0-rc1")
        sha = lane.commit()
        lane.git("push", "origin", "main")
    lane.env.update(GITHUB_SHA=sha, RELEASE_SHA=sha)
    lane.unchanged("plan", 1)
    lane.git("checkout", "--detach", sha)
    lane.unchanged("publish", 1)
    assert lane.calls("lint") == []


@pytest.mark.parametrize("case", ["P10", "P11", "P12", "P13"])
def test_publish_preflight_refusals(lane, case):
    if case == "P10":
        lane.git("checkout", "-b", "side")
        lane.write("side.txt", "side\n")
        side = lane.commit()
        lane.stable(side)
        lane.git("checkout", "main")
        lane.advance()
    elif case == "P11":
        lane.release(draft=True)
    elif case == "P12":
        lane.tag()
        lane.advance()
    else:
        lane.advance()
        lane.env["RELEASE_SHA"] = lane.initial
    lane.unchanged("publish", 1)
    assert not lane.creates()
    if case != "P12":
        assert "refs/tags/v2.5.0" not in lane.refs()


def test_p14_stable_absent(lane):
    lane.tag()
    lane.release()
    lane.run("publish")
    assert lane.git("ls-remote", "origin", "refs/heads/stable").split()[0] == lane.initial
    assert not lane.creates()


def test_p15_publish_twice(lane):
    lane.run("publish")
    lane.unchanged("publish")
    assert len(lane.creates()) == 1


@pytest.mark.parametrize("failure", ["gh", "origin"], ids=["P16-gh", "P16-origin"])
def test_state_read_failure(lane, failure):
    if failure == "gh":
        lane.env["FAIL_GH"] = "1"
    else:
        lane.git("remote", "set-url", "origin", str(lane.root / "missing.git"))
    lane.unchanged("plan", 1)
    lane.unchanged("publish", 1)
    assert not lane.creates()


@pytest.mark.parametrize("case", ["C1", "C2", "C3", "C4", "C5"])
def test_check_refusals(lane, case):
    if case == "C1":
        lane.version("2.5.0-rc1")
    elif case == "C2":
        lane.version("2.5.0", "2.4.0")
    elif case == "C3":
        (lane.repo / "docs/releases/v2.5.0.md").unlink()
    elif case == "C4":
        lane.write("docs/releases/v2.5.0.md", "## Not a top heading\n")
    else:
        lane.tag(tag="v2.6.0")
    output = lane.unchanged("checks", 1)
    if case == "C5":
        assert "v2.6.0" in output and "skip" in output.lower() and "nothing is lost" in output
    assert lane.calls("lint") == []


def test_c1_to_c4_reported_together(lane):
    lane.version("2.5.0-rc1", "2.4.0")
    output = lane.unchanged("checks", 1)
    assert "semver" in output and "manifest" in output.lower() and "notes" in output.lower()
    lane.write("docs/releases/v2.5.0-rc1.md", "no heading\n")
    output = lane.unchanged("checks", 1)
    assert "semver" in output and "manifest" in output.lower() and "heading" in output.lower()
    assert lane.calls("lint") == []


def test_c5b_out_of_order_release(lane):
    lane.tag(tag="v2.6.0")
    lane.stable(lane.initial)
    assert "done=false" in lane.unchanged("plan")
    output = lane.unchanged("checks", 1)
    assert "v2.6.0" in output and "skip" in output.lower()
    assert lane.calls("lint") == []


def test_existing_tag_skips_higher_version_check(lane):
    lane.tag()
    lane.tag(tag="v2.6.0")
    lane.unchanged("checks")
    assert lane.calls("lint") == [["lint", *args] for args in LINTERS]


@pytest.mark.parametrize("fail", [False, True], ids=["C6b", "C6"])
def test_linter_order_and_failure(lane, fail):
    lane.unchanged("checks", 1 if fail else 0, FAIL_LINT="--check-leakage" if fail else "")
    expected = LINTERS[:2] if fail else LINTERS
    assert lane.calls("lint") == [["lint", *args] for args in expected]


def guard_base(lane):
    lane.env["LP_BASE_REF"] = lane.initial


@pytest.mark.parametrize("labels", ["[]", '["no-release"]'], ids=["G1", "G1b"])
def test_bump_needs_notes(lane, labels):
    guard_base(lane)
    lane.version("2.5.1")
    lane.commit()
    assert "notes" in lane.unchanged("pr-guard", 1, PR_LABELS=labels).lower()
    assert lane.calls("lint") == []


@pytest.mark.parametrize("labels", [None, "not json", "[]", '["no-release"]'], ids=["G6-unset", "G6-invalid", "G2", "G3"])
def test_shipped_change_label(lane, labels):
    lane.write("plugins/launchpad/commands/change.md", "change\n")
    lane.commit()
    env = {} if labels is None else {"PR_LABELS": labels}
    output = lane.unchanged("pr-guard", 0 if labels == '["no-release"]' else 1, **env)
    if labels != '["no-release"]':
        assert "plugins/launchpad/commands/change.md" in output
        assert "bump" in output.lower() and "no-release" in output
    assert lane.calls("lint") == []


@pytest.mark.parametrize("path", ["plugins/launchpad/scripts/tests/test_new.py", "plugins/launchpad/scripts/plugin_stack_adapters/tests/test_new.py", "docs/new.md", ".github/workflows/new.yml"])
def test_g4_non_shipped_changes(lane, path):
    lane.write(path, "fixture\n")
    lane.commit()
    lane.unchanged("pr-guard")
    assert lane.calls("lint") == []


def test_g5_mismatch_even_with_label(lane):
    lane.version("2.5.0", "2.4.0")
    lane.commit()
    assert "manifest" in lane.unchanged("pr-guard", 1, PR_LABELS='["no-release"]').lower()
    assert lane.calls("lint") == []


def test_g7_move_out_of_shipped_tree(lane):
    lane.write("plugins/launchpad/commands/old.md", "unchanged content\n")
    base = lane.commit()
    lane.env["LP_BASE_REF"] = base
    lane.git("mv", "plugins/launchpad/commands/old.md", "docs/moved.md")
    lane.commit()
    assert "plugins/launchpad/commands/old.md" in lane.unchanged("pr-guard", 1)
    assert lane.calls("lint") == []


def test_g8_revert_failed_bump(lane):
    lane.tag()
    lane.version("2.5.1")
    base = lane.commit()
    lane.env["LP_BASE_REF"] = base
    lane.version("2.5.0")
    lane.commit()
    lane.unchanged("pr-guard")
    assert lane.calls("lint") == [["lint", *args] for args in LINTERS]



@pytest.mark.parametrize("path,labels,code", [
    (None, "[]", 0),
    ("plugins/launchpad/commands/change.md", "[]", 1),
    ("plugins/launchpad/commands/change.md", '["no-release"]', 0),
    ("plugins/launchpad/scripts/tests/test_new.py", "[]", 0),
])
def test_revert_existing_version_applies_deferral(lane, path, labels, code):
    lane.tag()
    lane.version("2.5.1")
    lane.env["LP_BASE_REF"] = lane.commit()
    lane.version("2.5.0")
    if path:
        lane.write(path, "fixture\n")
    lane.commit()
    output = lane.unchanged("pr-guard", code, PR_LABELS=labels)
    if code:
        assert path in output and "no-release" in output
    assert lane.calls("lint") == [["lint", *args] for args in LINTERS]


def test_plan_github_output(lane):
    output = lane.root / "output"
    lane.unchanged("plan", GITHUB_OUTPUT=str(output))
    assert "done=false" in output.read_text()
    assert f"sha={lane.initial}" in output.read_text()


def test_usage_error(lane):
    lane.unchanged("unknown", 2)
    assert lane.calls("gh") == [] and lane.calls("lint") == []


@pytest.mark.parametrize("name", ["café.md", "line\nbreak.md"])
def test_shipped_path_quoting(lane, name):
    lane.write("plugins/launchpad/commands/" + name, "fixture\n")
    lane.commit()
    lane.unchanged("pr-guard", 1)
    assert lane.calls("lint") == []


@pytest.mark.parametrize("name", ["release", "version-guard"])
def test_w1_workflow_permissions(name):
    import yaml

    root = SCRIPT.parents[3]
    workflow = yaml.safe_load((root / ".github/workflows" / f"{name}.yml").read_text())
    assert workflow["permissions"] == {}


@pytest.mark.parametrize("name", ["release", "version-guard"])
def test_w2_workflow_write_permissions(name):
    import yaml

    root = SCRIPT.parents[3]
    workflow = yaml.safe_load((root / ".github/workflows" / f"{name}.yml").read_text())
    for job_name, job in workflow["jobs"].items():
        for resource, access in job.get("permissions", {}).items():
            if access == "write":
                assert name == "release"
                assert (job_name, resource) in {("publish", "contents"), ("report", "issues")}


@pytest.mark.parametrize("name", ["release", "version-guard"])
def test_w3_no_expressions_in_run(name):
    import yaml

    root = SCRIPT.parents[3]
    workflow = yaml.safe_load((root / ".github/workflows" / f"{name}.yml").read_text())
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            assert "${{" not in step.get("run", "")


@pytest.mark.parametrize("manifest,existing,title", [
    ('{"version":"2.5.0"}', "", "Release v2.5.0 failed"),
    ('{"version":"2.5.0"}', "42", "Release v2.5.0 failed"),
    ('{"version":"2.5.0-rc1"}', "", "Release failed (version unreadable)"),
    ('{"version":"02.4.1"}', "", "Release failed (version unreadable)"),
    ('{"version":"2.04.1"}', "", "Release failed (version unreadable)"),
    ('{"version":"2.4.01"}', "", "Release failed (version unreadable)"),
    ('broken JSON', "", "Release failed (version unreadable)"),
    (None, "", "Release failed (version unreadable)"),
])
@pytest.mark.parametrize("release_sha", ["b" * 40, ""])
def test_report_failure_offline(tmp_path, manifest, existing, title, release_sha):
    import yaml

    root = SCRIPT.parents[3]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text())
    script = workflow["jobs"]["report"]["steps"][-1]["run"]
    if manifest is not None:
        path = tmp_path / MANIFESTS[0]
        path.parent.mkdir(parents=True)
        path.write_text(manifest)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
entry = {"args": args}
if args[:2] == ["issue", "list"]:
    print(os.environ["FAKE_ISSUE"])
elif args[:2] in (["issue", "create"], ["issue", "comment"]):
    entry["body"] = Path(args[args.index("--body-file") + 1]).read_text()
else:
    sys.exit(2)
with open(os.environ["REPORT_CALLS"], "a") as log:
    log.write(json.dumps(entry) + "\\n")
''')
    gh.chmod(0o755)
    calls_file = tmp_path / "calls"
    sha = "a" * 40
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "GH_TOKEN": "fixture-only",
        "REPO": "fixture/repository",
        "EVENT_SHA": sha,
        "RELEASE_SHA": release_sha,
        "RUN_URL": "https://example.invalid/run/1",
        "RUNNER_TEMP": str(tmp_path),
        "VERIFY_RESULT": "failure",
        "PUBLISH_RESULT": "skipped",
        "FAKE_ISSUE": existing,
        "REPORT_CALLS": str(calls_file),
    }
    subprocess.run(["bash", "-e", "-c", script], cwd=tmp_path, env=env,
                   check=True, capture_output=True, text=True, timeout=15)
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    assert len(calls) == 2
    lookup, write = calls
    assert lookup["args"][:2] == ["issue", "list"]
    assert lookup["args"][lookup["args"].index("--search") + 1] == title + " in:title"
    if existing:
        assert write["args"][:3] == ["issue", "comment", existing]
    else:
        assert write["args"][:2] == ["issue", "create"]
        assert write["args"][write["args"].index("--title") + 1] == title
    assert workflow["jobs"]["report"]["steps"][-1]["env"]["RELEASE_SHA"] == "${{ needs.verify.outputs.sha }}"
    assert write["body"] == (f"Run: https://example.invalid/run/1\nCommit: {sha}\n"
                              f"Release commit: {release_sha or 'unresolved'}\n"
                              "verify: failure\npublish: skipped\n")


@pytest.mark.parametrize("failures,attempts,code", [(1, 2, 0), (99, 3, 1)])
def test_release_creation_retry_and_exhaustion(lane, failures, attempts, code):
    lane.stable(lane.initial)
    sha = lane.advance()
    times_file = lane.root / "creation-times.jsonl"
    writes_before = len(lane.calls("git-write"))
    lane.run("publish", code, CREATE_FAILURES=str(failures), CREATE_TIMES=str(times_file))
    assert len(lane.creates()) == attempts
    times = [json.loads(line) for line in times_file.read_text().splitlines()]
    assert len(times) == attempts
    assert all(later - earlier >= 10 for earlier, later in zip(times, times[1:]))
    assert lane.git("ls-remote", "origin", "refs/tags/v2.5.0^{}").split()[0] == sha
    assert lane.git("cat-file", "-t", "v2.5.0") == "tag"
    stable = lane.git("ls-remote", "origin", "refs/heads/stable").split()[0]
    writes = lane.calls("git-write")[writes_before:]
    if code:
        assert stable == lane.initial
        assert json.loads(lane.state_file.read_text()) == []
        assert [call[1] for call in writes] == ["tag", "push"]
    else:
        assert stable == sha
        assert json.loads(lane.state_file.read_text()) == [{"tagName": "v2.5.0", "isDraft": False}]
        assert [call[1] for call in writes] == ["tag", "push", "push"]


@pytest.mark.parametrize("command", ["plan", "publish"])
@pytest.mark.parametrize("response,reason", [
    ("not-a-sha\trefs/heads/main\n", "Remote refs are unreadable"),
    ("", "origin/main is missing"),
])
def test_malformed_remote_ref_success_refuses_without_writes(lane, command, response, reason):
    output = lane.unchanged(command, 1, LS_REMOTE_OUTPUT=response)
    assert reason in output
    assert lane.calls("gh") == []


@pytest.mark.parametrize("command", ["plan", "publish"])
@pytest.mark.parametrize("response", [
    "not JSON", "{}", "[]", "[null]", "[[null]]",
    '[[{"tag_name":null,"draft":false}]]',
    '[[{"tag_name":"v2.5.0","draft":"false"}]]',
    '[[], [{"tag_name":"v2.5.0"}]]',
])
def test_malformed_release_list_success_refuses_without_writes(lane, command, response):
    output = lane.unchanged(command, 1, RELEASE_API_OUTPUT=response)
    assert "Release list is unreadable" in output
    assert lane.calls("gh") == [["gh", "api", "repos/{owner}/{repo}/releases?per_page=100", "--paginate", "--slurp"]]


@pytest.mark.parametrize("version,code", [
    ("02.4.1", 1), ("2.04.1", 1), ("2.4.01", 1),
    ("0.0.0", 0), ("10.20.30", 0),
])
@pytest.mark.parametrize("command", ["checks", "plan", "publish", "pr-guard"])
def test_canonical_version_components(lane, version, code, command):
    lane.env["LP_BASE_REF"] = lane.initial
    lane.version(version)
    lane.write(f"docs/releases/v{version}.md", f"# Release v{version}\n")
    sha = lane.commit()
    lane.git("push", "origin", "main")
    lane.env.update(GITHUB_SHA=sha, RELEASE_SHA=sha)
    if command == "publish" and not code:
        lane.run(command)
    else:
        output = lane.unchanged(command, code)
        if code:
            assert "semver" in output


def test_w_guard_uses_event_base_sha():
    import yaml

    workflow = yaml.safe_load((SCRIPT.parents[3] / ".github/workflows/version-guard.yml").read_text())
    step = workflow["jobs"]["version-guard"]["steps"][-1]
    assert step["env"]["LP_BASE_REF"] == "${{ github.event.pull_request.base.sha }}"


def test_w_release_commit_selected_before_dependency_install():
    import yaml

    workflow = yaml.safe_load((SCRIPT.parents[3] / ".github/workflows/release.yml").read_text())
    steps = workflow["jobs"]["verify"]["steps"]
    install = next(i for i, step in enumerate(steps) if step.get("name") == "Install vendored deps")
    select = next(i for i, step in enumerate(steps) if 'git checkout --detach "$RELEASE_SHA"' in step.get("run", ""))
    preserve = next(i for i, step in enumerate(steps) if step.get("run", "").startswith("cp plugins/launchpad/scripts/plugin-release-lane.py"))
    assert preserve < select < install
    assert steps[select]["env"]["RELEASE_SHA"] == "${{ steps.plan.outputs.sha }}"
    assert steps[select]["env"]["EVENT_SHA"] == "${{ github.sha }}"
    assert steps[select]["if"] == steps[install]["if"] == "steps.plan.outputs.done != 'true'"
    assert "_vendor/PSUTIL_VERSION" in steps[install]["run"]
    assert "_vendor/PYYAML_VERSION" in steps[install]["run"]
    assert steps[-1]["run"] == 'python "$RUNNER_TEMP/plugin-release-lane.py" checks'


def test_publish_rechecks_live_version_ceiling(lane):
    assert "done=false" in lane.unchanged("plan")
    lane.tag(tag="v2.6.0")
    lane.git("tag", "-d", "v2.6.0")
    assert "v2.6.0" not in lane.git("tag", "--list")
    output = lane.unchanged("publish", 1)
    assert "v2.6.0" in output and "skip" in output.lower()
    assert "refs/tags/v2.5.0" not in lane.refs()


@pytest.mark.parametrize("command", ["plan", "publish"])
def test_release_older_than_first_hundred_is_found(lane, command):
    lane.tag()
    lane.stable(lane.initial)
    releases = [{"tagName": f"v3.0.{i}", "isDraft": False} for i in range(101)]
    releases.append({"tagName": "v2.5.0", "isDraft": False})
    lane.state_file.write_text(json.dumps(releases))
    output = lane.unchanged(command)
    if command == "plan":
        assert "done=true" in output
    assert not lane.creates()


@pytest.mark.parametrize("env,reason", [
    ({"LS_TAGS_OUTPUT": "not-a-sha\trefs/tags/v2.6.0\n"}, "Remote tags are unreadable"),
    ({"LS_TAGS_OUTPUT": "a" * 40}, "Remote tags are unreadable"),
    ({"FAIL_LS_TAGS": "1"}, "git ls-remote failed"),
])
def test_publish_live_tag_read_refuses_without_writes(lane, env, reason):
    output = lane.unchanged("publish", 1, **env)
    assert reason in output
    assert lane.calls("gh") == [["gh", "api", "repos/{owner}/{repo}/releases?per_page=100", "--paginate", "--slurp"]]
    assert "refs/tags/v2.5.0" not in lane.refs()
