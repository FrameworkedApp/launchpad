"""Catalog stack aliases: the one table from scaffolders.yml keys to stack ids.

`/lp-pick-stack` layers and `/lp-scaffold-stack` receipts carry scaffolders.yml
keys such as `next` or `hono`, and `/lp-define` persists those keys verbatim in
`.launchpad/config.yml` `stacks:`. Every reader that keys on
`STACK_ID_ACTIVE_ENUM` (the scaffold decision's flat `stacks` array, the
bootstrap enrichers, the agent stack filter and the generated `agents.yml`)
maps them through `canonical_stack_ids`, so no reader keeps its own copy.

Standard library only, so read-only callers such as the agent stack filter
can import it without the decision writer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType

# Singleton fallback for uncomposed catalog keys. Every value is a
# STACK_ID_ACTIVE_ENUM member and no key is one (both guarded by tests).
CATALOG_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "supabase": "generic",
        "expo": "generic",
        "eleventy": "generic",
        "hugo": "generic",
        # Composed with `next` it becomes nextjs_hono_cloudflare instead.
        "hono": "generic",
        "next": "nextjs_standalone",
        # Not python_django: that widening is deferred to BL-263.
        "django": "python_generic",
        "fastapi": "python_generic",
    }
)

# Composition reductions, applied before the singleton fallback so
# `["next", "fastapi"]` becomes `["nextjs_fastapi"]` rather than
# `["nextjs_standalone", "python_generic"]`.
COMPOSITION_REDUCTIONS: Mapping[frozenset[str], str] = MappingProxyType(
    {
        frozenset({"next", "fastapi"}): "nextjs_fastapi",
        frozenset({"next", "hono"}): "nextjs_hono_cloudflare",
    }
)


def canonical_stack_ids(stack_ids: Iterable[str]) -> list[str]:
    """Map persisted or catalog stack ids to canonical stack ids.

    Composed ids come first, then the remaining ids in first-appearance
    order; duplicates collapse. Ids that are neither aliases nor consumed by
    a composition pass through unchanged, including empty strings, so a
    caller's `STACK_ID_ACTIVE_ENUM` check still reports them.
    """
    raw_ids = list(dict.fromkeys(stack_ids))
    raw_set = frozenset(raw_ids)
    out: list[str] = []
    consumed: set[str] = set()
    for combo, target_id in COMPOSITION_REDUCTIONS.items():
        if combo <= raw_set:
            out.append(target_id)
            consumed |= combo
    for stack_id in raw_ids:
        if stack_id in consumed:
            continue
        mapped = CATALOG_ALIASES.get(stack_id, stack_id)
        if mapped not in out:
            out.append(mapped)
    return out


__all__ = ["CATALOG_ALIASES", "COMPOSITION_REDUCTIONS", "canonical_stack_ids"]
