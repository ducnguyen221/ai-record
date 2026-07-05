#!/usr/bin/env python3
"""Research/refresh helper for the AI Record local-summarizer model catalog.

Reads the curated catalog (``ai_record/summarizer_models.json``), shows which
models are installed locally (``ollama list``), and best-effort queries the Ollama
registry for available tags per family so you can spot models newer than the
catalog. Everything degrades gracefully: with NO network and NO ollama it simply
prints the catalog plus a note.

Stdlib only (json, subprocess, urllib, argparse). Usage:

    python scripts/research-models.py            # human-readable table
    python scripts/research-models.py --json     # machine-readable output
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

# Load the catalog straight from the JSON so this script has no import-time deps
# on the ai_record package (works even if run from a bare checkout).
_CATALOG_PATH = Path(__file__).resolve().parents[1] / "ai_record" / "summarizer_models.json"
_REGISTRY = "https://registry.ollama.ai/v2/library/{family}/tags/list"
_HTTP_TIMEOUT = 6.0


def load_catalog() -> dict:
    try:
        return json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot read catalog {_CATALOG_PATH}: {exc}", file=sys.stderr)
        return {"default": "qwen2.5:7b", "models": []}


def installed_models() -> list[str]:
    """Locally-pulled tags via ``ollama list`` (guarded; [] on any failure)."""
    if shutil.which("ollama") is None:
        return []
    try:
        proc = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env={**os.environ, "NO_COLOR": "1"},
            shell=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if proc.returncode != 0:
        return []
    tags: list[str] = []
    for line in (proc.stdout or "").splitlines():
        if not line.strip():
            continue
        first = line.split()[0]
        if first.upper() == "NAME":
            continue
        tags.append(first)
    return tags


def registry_tags(family: str) -> list[str]:
    """Best-effort list of available tags for a family (empty on any failure/offline)."""
    url = _REGISTRY.format(family=family)
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    tags = data.get("tags") if isinstance(data, dict) else None
    return [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []


def _seen_family_tags(family: str, reg_tags: list[str]) -> list[str]:
    """Return ``family:tag`` combos seen in the registry (namespaced for readability)."""
    return [f"{family}:{t}" for t in reg_tags]


def research(catalog: dict) -> dict:
    models = catalog.get("models", [])
    installed = set(installed_models())
    catalog_tags = {m.get("tag") for m in models}

    # Query the registry once per unique family.
    families = sorted({m.get("family") for m in models if m.get("family")})
    fam_tags: dict[str, list[str]] = {fam: registry_tags(fam) for fam in families}

    rows = []
    for m in models:
        tag = m.get("tag")
        family = m.get("family")
        reg = fam_tags.get(family, [])
        rows.append(
            {
                "tag": tag,
                "family": family,
                "params": m.get("params"),
                "vram_gb": m.get("vram_gb"),
                "recommended": bool(m.get("recommended")),
                "installed": tag in installed,
                "registry_tags_seen": len(reg),
            }
        )

    # "New/available" = registry tags for our families that are NOT in the catalog.
    new_available: dict[str, list[str]] = {}
    for fam, reg in fam_tags.items():
        seen = _seen_family_tags(fam, reg)
        novel = [t for t in seen if t not in catalog_tags]
        if novel:
            new_available[fam] = novel

    # Suggested pulls for recommended models not yet installed.
    suggested = [
        f"ollama pull {m.get('tag')}"
        for m in models
        if m.get("recommended") and m.get("tag") not in installed
    ]

    return {
        "default": catalog.get("default"),
        "updated": catalog.get("updated"),
        "installed": sorted(installed),
        "rows": rows,
        "new_available": new_available,
        "suggested_pulls": suggested,
        "ollama_present": shutil.which("ollama") is not None,
    }


_NOTE = (
    "For a broader survey of newly-released models, ask the AI Record agent to "
    "refresh summarizer_models.json via web research."
)


def print_human(result: dict) -> None:
    print(f"Catalog default : {result['default']}  (updated {result['updated'] or 'n/a'})")
    if not result["ollama_present"]:
        print("Ollama          : NOT installed (install with scripts/setup-ollama.ps1)")
    else:
        inst = ", ".join(result["installed"]) or "(none pulled yet)"
        print(f"Installed models: {inst}")
    print()

    # Table.
    header = f"{'MODEL':<16} {'PARAMS':<7} {'VRAM':<6} {'INSTALLED':<10} {'REG TAGS':<9} REC"
    print(header)
    print("-" * len(header))
    for r in result["rows"]:
        print(
            f"{str(r['tag']):<16} {str(r['params']):<7} "
            f"{str(r['vram_gb']) + 'GB':<6} "
            f"{('yes' if r['installed'] else 'no'):<10} "
            f"{str(r['registry_tags_seen']):<9} "
            f"{'*' if r['recommended'] else ''}"
        )

    if result["new_available"]:
        print("\nTags available in the Ollama registry but NOT in the catalog:")
        for fam, tags in result["new_available"].items():
            preview = ", ".join(tags[:12])
            more = "" if len(tags) <= 12 else f"  (+{len(tags) - 12} more)"
            print(f"  {fam}: {preview}{more}")
    else:
        print("\n(No extra registry tags found - offline, or catalog already current.)")

    if result["suggested_pulls"]:
        print("\nSuggested pulls (recommended models not yet installed):")
        for cmd in result["suggested_pulls"]:
            print(f"  {cmd}")

    print(f"\nNote: {_NOTE}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Research AI Record summarizer models.")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    args = parser.parse_args(argv)

    catalog = load_catalog()
    result = research(catalog)

    if args.json:
        result["note"] = _NOTE
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print_human(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
