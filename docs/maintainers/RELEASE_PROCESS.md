# Release Process (LaunchPad maintainers)

> **Audience:** LaunchPad maintainers shipping new versions of the plugin and template.
>
> **Not for downstream projects** scaffolded from LaunchPad. The v2.x kernel renderer never ships `docs/maintainers/` to downstream projects (the directory exists only in the LaunchPad source tree). Downstream projects choose their own release process (or none). v2.1 (BL-247) decommissioned the legacy `init-project.sh` flow that historically stripped this directory at init time; pin to v2.0.x for that flow.

Merging a PR that bumps LaunchPad's version releases it. The `release` workflow checks the release commit, creates its annotated tag, publishes the GitHub release from the hand-authored notes and fast-forwards `stable` in one run. Whoever can merge to `main` can release, so the `main` ruleset is the release control. Dispatching the workflow requires write access; a label alone never releases anything.

The Frameworked marketplace installs LaunchPad from `stable` on both hosts. There is no soak window after merge: delay a release by merging its version-bump PR later. `stable` never moves back; fix a shipped release with a new patch.

## How the lane works

The lane has five parts, each with a specific role in this repository:

- **Release workflow** (`.github/workflows/release.yml`): runs on every push to `main`, with `workflow_dispatch` for retries. When `plugin.json` carries a new version, it runs the release checks on that commit, creates an annotated tag on that commit, publishes the GitHub release from the notes file and fast-forwards `stable`. It is idempotent, queues runs instead of cancelling them and opens a "Release vX.Y.Z failed" issue on failure.
- **Required PR check** (`.github/workflows/version-guard.yml`): runs the same release checks when a PR changes the version. A PR that changes shipped files without a version bump fails unless it has the `no-release` label.
- **Main ruleset** (GitHub ruleset `main`): squash-only merges, linear history and required checks including `version-guard`. Whoever can merge to `main` can release.
- **Protected stable branch** (`stable`, protected by the `protect-stable` ruleset): the workflow only fast-forwards it and never moves it back. The marketplace installs from this branch.
- **Notes file** (`docs/releases/vX.Y.Z.md`): hand-written and required before merge; its content becomes the GitHub release body.

The shared logic lives in `plugins/launchpad/scripts/plugin-release-lane.py`, with subcommands `checks`, `plan`, `publish` and `pr-guard`.

## Codex checks before merge

Bump `plugins/launchpad/.claude-plugin/plugin.json` and `plugins/launchpad/.codex-plugin/plugin.json` together, then complete these manual checks before merging the release PR. They require a logged-in Codex command line, `jq`, and the release branch or commit in `RELEASE_REF`. This direct install from the LaunchPad repository is the maintainer test route. Users install through the Frameworked marketplace.

```bash
# Install check: install the release candidate and verify the required installed files.
RELEASE_REF=my-release-branch  # the release branch or commit to test
codex plugin marketplace add https://github.com/FrameworkedApp/launchpad.git --ref "$RELEASE_REF" --json
PLUGIN_PATH=$(codex plugin add launchpad@launchpad --json | jq -r '.installedPath')
codex plugin list
test -f "$PLUGIN_PATH/codex/skills/lp/SKILL.md"
test -f "$PLUGIN_PATH/scripts/plugin-codex-router.py"
test -d "$PLUGIN_PATH/commands"

# Help check: run live help and compare its helper inventory with the installed files.
codex exec '$launchpad:lp help'
diff <(find "$PLUGIN_PATH/commands" -maxdepth 1 -type f -name '*.md' -exec basename {} .md \; | sort) <(python3 "$PLUGIN_PATH/scripts/plugin-codex-router.py" inventory --kind command --json | jq -r '.items[].id' | sort)

# Record the host-generated direct entries for this Codex version.
codex debug prompt-input 'list' | grep -o 'launchpad:source-command-[a-z0-9-]*' | sort -u

codex plugin remove launchpad@launchpad --json
codex plugin marketplace remove launchpad --json
```

The install check passes when the plugin advertises `launchpad:lp` and all three installed-path checks exit zero. The help check passes when the transcript contains the helper's raw inventory JSON, the final message is a readable list, and `diff` exits zero. These checks remain manual; a green workflow does not replace them.

Whenever these checks are rerun, update the "As of" and "Last checked" dates and the Codex version in `README.md` and `docs/guides/HOW_IT_WORKS.md`.

## Why the notes file is required

A tag without hand-authored notes leaves two bad options: accept GitHub's auto-generated diff as the project's first impression of the release, or backfill notes after publication. Writing the notes before merge avoids both. The MemPalace open-source pattern study (`docs/reports/launchpad_reports/2026-04-24-mempalace-open-source-pattern-study.md`, §10 item 8) documents the convention. The `version-guard` check and the `release` workflow enforce it using the same release checks.

## Sequence for a new version `vX.Y.Z`

### 1. Branch, bump both manifests and draft notes

Use any branch name; release detection reads the manifest, not the branch name. For example:

```bash
git switch -c chore/next-release
```

Set both plugin manifests to `X.Y.Z` and create `docs/releases/vX.Y.Z.md` with a top-level `# ` heading. Draft the notes early and refine them as the PR matures. Recommended sections, only when applicable:

- `## Security`: security fixes
- `## Added`: new features
- `## Improved`: enhancements
- `## Documentation`: documentation changes
- `## Internal`: maintenance that does not change external behavior
- `## Upgrading from vX.Y.(Z-1)`: migration guidance
- `## Full changelog`: a link to `CHANGELOG.md`

### 2. Implement and account for accumulated changes

Update the notes to match the actual release. Read what has accumulated since the previous tag, including shipped changes deferred by `no-release`:

```bash
git log vPREV..HEAD -- plugins/launchpad
```

### 2a. Re-validate and re-stamp the OPERATIONS §4 freshness docs (before merge)

The required `version-guard` check runs both freshness gates on version-bump PRs. The `release` workflow runs them again before writing a tag or release:

- `plugin-v2-handshake-lint.py --check-freshness`: catalog/pattern freshness, schema and `knowledge_anchor_sha256` integrity for `scaffolders.yml`, `category-patterns.yml` and `scaffolders/*-pattern.md`.
- `plugin-freshness-check.py --gating`: the full §4 target set, also including `pillar-framework.md`, `SCAFFOLD_HANDSHAKE.md` and `SCAFFOLD_OPERATIONS.md`.

Every `last_validated:` must be within the 30-day window at release time. Re-validation belongs on the release branch before merge. Preview stale files locally:

```bash
python plugins/launchpad/scripts/plugin-freshness-check.py --gating
```

For each flagged file, re-read it and confirm that it still describes current behavior before updating the date. This is a real re-validation, not a date bump:

- Catalog/pattern docs: update `last_validated:` to today. Re-stamping a `*-pattern.md` changes its content hash, so update its matching `knowledge_anchor_sha256` in `scaffolders.yml` in the same commit. The handshake lint prints the expected hash.
- Contract docs: re-stamp `SCAFFOLD_HANDSHAKE.md` and `SCAFFOLD_OPERATIONS.md` in their own dedicated commit, with subject `chore(v2): re-stamp HANDSHAKE/OPERATIONS last_validated, no contract change` and a `Restamp-Affirmation: <prior-SHA> -> <new-SHA>` trailer. Touch only the `last_validated:` frontmatter line, with no body diff.

Confirm both gates pass:

```bash
python plugins/launchpad/scripts/plugin-v2-handshake-lint.py --check-freshness
python plugins/launchpad/scripts/plugin-freshness-check.py --gating
```

Freshness remains advisory on ordinary PRs. It blocks version-bump PRs even when they carry `no-release`.

### 3. Open the PR and pass the release checks

`version-guard` runs on every PR to `main`, including label changes. Both manifests must agree. When the Claude manifest version differs from the PR base, the guard runs the release checks: plain semver, matching manifests, notes with a heading, a version higher than existing plain-semver tags when its tag is absent, then version coherence, leakage and both freshness checks. An existing version tag skips the higher-version check so recovery and reverting a failed bump remain possible. When the version changes to an existing tag, the guard also requires `no-release` for shipped-file changes other than the two manifests; reverting only the manifests or changing only excluded test-folder files passes without the label.

### 4. Review and complete the manual Codex checks

Address the review findings and pass the required CI checks, including `version-guard`. Codex and Greptile PR reviews remain advisory. Complete the Codex install and help checks above before merge.

### 5. Squash-merge to `main`

The squash commit is the normal release commit. Merge is the release decision. Wait for this release run before merging the next version bump.

### 6. Let the workflow tag, publish and promote

On the push to `main`, `release` resolves the version's live tag, GitHub release and `stable` state. It checks the release commit before any write, creates an annotated tag on that push's commit, publishes the notes file and fast-forwards `stable` to the tag's commit. It never substitutes a newer `main` head for the push's commit.

If a tag already exists on an earlier `main` commit with the same version in both manifests, that tagged commit supplies the notes and checks. The tag stays untouched and `stable` advances to it. A fully published version already contained by `stable` is a no-op. Runs queue instead of cancelling one another, and retries complete any missing steps.

### 7. Confirm the release

Confirm the `release` run is green, inspect the GitHub release and compare `stable` with the tag's commit:

```bash
gh run list --workflow release.yml --limit 1
gh release view vX.Y.Z
git ls-remote origin 'refs/tags/vX.Y.Z*'
git ls-remote origin stable
```

For an annotated tag, compare `stable` with the peeled `refs/tags/vX.Y.Z^{}` commit, not the tag object's SHA. Then confirm the user install route once on Codex:

```bash
codex plugin marketplace add FrameworkedApp/marketplace
codex plugin add launchpad@frameworked
codex exec '$launchpad:lp help'
codex plugin remove launchpad@frameworked
codex plugin marketplace remove frameworked
```

### 8. Follow through on release housekeeping

Check that subsumed Dependabot PRs close, confirm the expected vulnerability-alert changes when shipping security fixes, and keep `CHANGELOG.md` consistent with the release notes.

## The `no-release` rule

A PR that changes shipped files under `plugins/launchpad/` without changing the version needs the `no-release` label. The two test directories (`scripts/tests/` and `scripts/plugin_stack_adapters/tests/`) are excluded; documentation and workflow changes outside the plugin tree do not need the label. Pip Dependabot PRs receive it automatically alongside their existing labels.

The label defers shipped changes to the next release; it does not approve the change. Review and other required checks still apply. A version bump always runs release checks regardless of the label. Both manifests must agree in every case.

A fix merged after a failed release carries `no-release` when it keeps the version unchanged. Its merge starts another run that completes the pending release once its checks and state permit it.

A pending release ships the commit its retry selects (see Failure and retry). If that commit includes changes merged with `no-release` after the failed run, they ship too; update the release notes first when one of them belongs there.

## Failure and retry

A run for an untagged version that was rolled back on `main` refuses before any write; close its failure issue by hand.

A failed job opens or comments on `Release vX.Y.Z failed`. If the version is unreadable, the title is `Release failed (version unreadable)`. The body contains the run URL, commit and job conclusions. The run page is the source of truth, including cancellations and timeouts. Close the issue by hand after the failure is resolved; the lane does not close it automatically.

Choose a retry path:

1. Re-run the failed jobs: `gh run rerun <run-id> --failed`.
2. Dispatch from `main`: `gh workflow run release.yml --ref main`.
3. Merge the fix or another change to `main`; the next push retries the version.

Re-running failed jobs keeps the original run's commit and lane script. A new dispatch from `main` or the next push uses its own commit and lane script, so it can include a merged fix. An existing valid tag still selects the tagged commit for release content and checks.

Two states require maintainer intervention before retrying:

- `stable` has diverged from the release commit. Repair `stable`; the lane never forces it or moves it back.
- A draft release exists for the tag. Publish or delete that draft; the lane never edits or publishes a release it did not create.

A tag off `main` or holding another manifest version is also refused and never moved by the lane. The maintainer must repair that tag state. A tagged version that can no longer pass its checks, such as one with stale freshness dates, is abandoned by releasing the next patch.

Queue order is not guaranteed. If two bumps merge within minutes and the higher version releases first, the lower version is skipped: its code is already on `stable`, so nothing is lost. Close its failure issue by hand. Waiting for the first `release` run before merging the next bump avoids this case.

## Manual recovery path

Manual tagging is recovery only. The unchanged `v2-release.yml` workflow has its own older checks, including requiring the tag's commit to equal the current `main` head. A maintainer can hand-push an annotated tag for that commit:

```bash
git tag -a vX.Y.Z -m "LaunchPad vX.Y.Z" <release-commit>
git push origin refs/tags/vX.Y.Z
```

The tag push runs `verify-v2-ship`, then promotes `stable` if those checks pass. For a transient failure, rerun the original tag-push run's failed jobs so promotion remains eligible. A manual dispatch of `v2-release.yml` verifies only and never promotes. The next `release` run, dispatched from `main` or triggered by its next push, publishes the GitHub release and finishes any remaining promotion. Use the [OPERATIONS §7 recovery procedure](../architecture/SCAFFOLD_OPERATIONS.md#7-rollback-procedure-layer-2-f-11--p1-4--layer-3-simplicity-p1-e--scope-p1-2--product-p2-1-compressed) for the older verification path.

## Defense layers

| Layer              | What it enforces                                                                 | Where                                 |
| ------------------ | -------------------------------------------------------------------------------- | ------------------------------------- |
| PR check           | Matching manifests, release checks for bumps, release intent for shipped changes | `.github/workflows/version-guard.yml` |
| Release workflow   | Checks before writes; tag, GitHub release and forward-only `stable` in one run   | `.github/workflows/release.yml`       |
| Maintainer runbook | Editorial notes, re-validation and manual Codex checks before merge              | `docs/maintainers/RELEASE_PROCESS.md` |

## Historical workflow and scaffold boundary

Scaffolded projects choose their own release conventions. The retired `release-notes-check.yml` workflow was specific to LaunchPad and was not needed in those projects.

The v2.x kernel renderer never shipped `release-notes-check.yml` or `docs/maintainers/` to scaffolded projects. The legacy `init-project.sh` flow previously removed them at init time; v2.1 BL-247 retired that flow. These are historical details, not steps in the current release lane.
