"""Every stack id LaunchPad persists must reach the stack-keyed consumers.

`/lp-scaffold-stack` records each layer's scaffolders.yml key (for example
`hono`) in the receipt, and `/lp-define` renders those keys verbatim into
`.launchpad/config.yml` `stacks:`. Readers that key on STACK_ID_ACTIVE_ENUM
map them through `lp_pick_stack.stack_aliases.canonical_stack_ids`, the
single owner of the alias table. These tests fail if the ids LaunchPad can
persist and the ids those readers accept drift apart, and they pin that
unknown ids are still reported.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from lp_bootstrap._enricher_common import read_enricher_stacks  # noqa: E402
from lp_define_runner import render_docs  # noqa: E402
from lp_pick_stack import VALID_COMBINATIONS  # noqa: E402
from lp_pick_stack.decision_writer import derive_stacks  # noqa: E402
from lp_pick_stack.stack_aliases import (  # noqa: E402
    CATALOG_ALIASES,
    COMPOSITION_REDUCTIONS,
    canonical_stack_ids,
)
from plugin_default_generators._renderer_base import STACK_ID_ACTIVE_ENUM  # noqa: E402
from plugin_stack_adapters import generic  # noqa: E402

_FILTER_PATH = _SCRIPTS / "plugin-agent-scope-filter.py"
_CONFIG_LOADER_PATH = _SCRIPTS / "plugin-config-loader.py"
_DETECTOR_PATH = _SCRIPTS / "plugin-stack-detector.py"
_SCAFFOLDERS_YML = _SCRIPTS.parent / "scaffolders.yml"
_CATALOG_YML = _SCRIPTS / "lp_pick_stack" / "data" / "category-patterns.yml"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def filter_mod() -> ModuleType:
    mod = _load("plugin_agent_scope_filter_alias_test", _FILTER_PATH)
    mod._load_agent_index.cache_clear()
    return mod


@pytest.fixture(scope="module")
def config_loader() -> ModuleType:
    return _load("plugin_config_loader_alias_test", _CONFIG_LOADER_PATH)


def _scaffolder_keys() -> set[str]:
    data = yaml.safe_load(_SCAFFOLDERS_YML.read_text(encoding="utf-8"))
    return set(data["stacks"])


def _catalog_layer_stacks() -> set[str]:
    data = yaml.safe_load(_CATALOG_YML.read_text(encoding="utf-8"))
    return {
        layer["stack"]
        for pattern in data["categories"]
        for layer in pattern.get("canonical_stack") or []
    }


def _detector_stack_ids() -> set[str]:
    """Stack id literals in the detector source.

    The detector exposes no id constant, so this reads its three emission
    forms; the floor in test_persistable_id_sources_are_not_empty fails if a
    refactor changes them.
    """
    source = _DETECTOR_PATH.read_text(encoding="utf-8")
    ids = set(re.findall(r'\bstack = "([a-z_]+)"', source))
    ids |= set(re.findall(r'"stack": "([a-z_]+)"', source))
    ids |= set(re.findall(r'stacks = \["([a-z_]+)"\]', source))
    return ids


def _persistable_stack_ids() -> list[str]:
    """Every id a LaunchPad writer can place in config.yml `stacks:`."""
    ids = _scaffolder_keys() | _catalog_layer_stacks() | _detector_stack_ids()
    ids |= {stack for stack, _role in VALID_COMBINATIONS}
    return sorted(ids)


_PERSISTABLE_STACK_IDS = _persistable_stack_ids()


def test_persistable_id_sources_are_not_empty():
    """Guard the parsers above so a format change cannot vacuously pass."""
    assert {"hono", "next", "generic"} <= _scaffolder_keys()
    assert {"hono", "next", "django"} <= _catalog_layer_stacks()
    assert _detector_stack_ids() >= {
        "astro",
        "generic",
        "go_cli",
        "nextjs_standalone",
        "python_django",
        "python_generic",
        "rails",
        "ts_monorepo",
    }


def test_alias_table_covers_exactly_the_non_canonical_scaffolder_keys():
    """A new scaffolders.yml key needs an alias unless it is a stack id."""
    assert set(CATALOG_ALIASES) == _scaffolder_keys() - STACK_ID_ACTIVE_ENUM
    assert not set(CATALOG_ALIASES) & STACK_ID_ACTIVE_ENUM
    assert set(CATALOG_ALIASES.values()) <= STACK_ID_ACTIVE_ENUM
    assert set(COMPOSITION_REDUCTIONS.values()) <= STACK_ID_ACTIVE_ENUM
    for combo in COMPOSITION_REDUCTIONS:
        assert combo <= set(CATALOG_ALIASES)


@pytest.mark.parametrize("stack_id", _PERSISTABLE_STACK_IDS)
def test_every_persistable_id_maps_into_active_enum(stack_id):
    """Each persistable id canonicalizes to a STACK_ID_ACTIVE_ENUM member."""
    assert canonical_stack_ids([stack_id])[0] in STACK_ID_ACTIVE_ENUM


@pytest.mark.parametrize(
    "layer_stacks",
    [["next", "hono"], ["next", "fastapi"], ["django"], ["astro", "astro"], []],
)
def test_derive_stacks_delegates_to_canonical_stack_ids(layer_stacks):
    """The decision writer and config readers share one mapping."""
    layers = [{"stack": s} for s in layer_stacks] + [{"stack": ""}, {"role": "x"}]
    assert derive_stacks(layers) == canonical_stack_ids(layer_stacks)


@pytest.mark.parametrize("stack_id", _PERSISTABLE_STACK_IDS)
def test_every_persistable_id_is_accepted_by_filter(filter_mod, stack_id):
    """Each persistable id is a valid filter input on its own."""
    assert filter_mod.filter_agents_by_stacks(
        ["lp-security-auditor"], [stack_id]
    ) == ["lp-security-auditor"]


@pytest.mark.parametrize(
    ("persisted", "expected"),
    [
        (["hono", "generic"], ["generic"]),
        (["next", "hono"], ["nextjs_hono_cloudflare"]),
        (["django"], ["python_generic"]),
    ],
)
def test_bootstrap_enrichers_read_canonical_ids(tmp_path, persisted, expected):
    """The enricher stack source maps persisted aliases the same way."""
    (tmp_path / ".launchpad").mkdir()
    (tmp_path / ".launchpad" / "config.yml").write_text(
        f"stacks: [{', '.join(persisted)}]\n", encoding="utf-8"
    )
    assert (
        read_enricher_stacks(tmp_path, module_spec_name="alias_test_config_loader")
        == expected
    )


def test_scaffolded_hono_generic_config_filters_without_error(
    filter_mod, config_loader, tmp_path
):
    """A config written by the scaffolding path filters instead of raising."""
    project = tmp_path / "example-api"
    (project / ".launchpad").mkdir(parents=True)
    (project / ".launchpad" / "config.yml").write_text(
        "stacks: [hono, generic]\n", encoding="utf-8"
    )
    stacks = config_loader.read_stacks(project)
    assert stacks == ["hono", "generic"]

    roster = ["lp-security-auditor", "lp-testing-reviewer", "lp-foad-go-reviewer"]
    survivors = filter_mod.filter_agents_by_stacks(
        roster, stacks, raise_on_no_match=True
    )
    assert survivors == ["lp-security-auditor", "lp-testing-reviewer"]


@pytest.mark.parametrize(
    ("stacks", "selector", "admitted"),
    [
        (["next", "hono"], "stack:typescript", True),
        (["next"], "stack:typescript", True),
        (["hono", "generic"], "stack:typescript", False),
        (["django"], "stack:python", True),
        (["fastapi"], "stack:python", True),
        (["hono"], "stack:hono", True),
        (["hono"], "stack:go", False),
    ],
)
def test_aliases_match_selectors_by_canonical_and_raw_id(
    filter_mod, stacks, selector, admitted
):
    """Selectors match the canonical id, its family, or the persisted id."""
    survivors = filter_mod.filter_agents_by_stacks(
        ["lp-security-auditor", "example-scoped-agent"],
        stacks,
        prevalidated_project_scopes={"example-scoped-agent": selector},
    )
    assert ("example-scoped-agent" in survivors) is admitted


@pytest.mark.parametrize(
    ("stacks", "reported"),
    [
        (["cobol"], ["cobol"]),
        (["hono", "cobol"], ["cobol"]),
        ([""], [""]),
        ([None], [None]),
    ],
)
def test_unknown_stack_id_is_still_reported(filter_mod, stacks, reported):
    """Accepting aliases must not turn into silently dropping unknown ids."""
    with pytest.raises(ValueError, match="unknown stack id") as exc:
        filter_mod.filter_agents_by_stacks(["lp-security-auditor"], stacks)
    assert f"unknown stack id(s) {reported!r}" in str(exc.value)


@pytest.mark.parametrize(
    ("stacks", "python_reviewer", "ts_reviewers"),
    [
        (["django"], True, False),
        (["fastapi"], True, False),
        (["python_generic"], True, False),
        (["hono", "generic"], False, False),
        (["ts_monorepo"], False, True),
    ],
)
def test_agents_yml_keys_on_canonical_stacks(
    tmp_path, stacks, python_reviewer, ts_reviewers
):
    """agents.yml reviewer conditionals see canonical ids; config.yml keeps
    the persisted ids verbatim."""
    rendered = render_docs(
        generic.run(),
        {"stacks": stacks},
        "Example",
        tmp_path,
        only={"agents.yml", "config.yml"},
    )
    agents = rendered[".launchpad/agents.yml"]
    assert ("lp-kieran-foad-python-reviewer" in agents) is python_reviewer
    assert ("lp-kieran-foad-ts-reviewer" in agents) is ts_reviewers
    assert ("lp-frontend-races-reviewer" in agents) is ts_reviewers
    config = yaml.safe_load(rendered[".launchpad/config.yml"])
    assert config["stacks"] == stacks
