"""Generate ``grison/model/data/cwe.json`` from the official MITRE CWE catalog.

Downloads the "latest" comprehensive CWE catalog zip from
``https://cwe.mitre.org/data/xml/cwec_latest.xml.zip``, unzips it in memory, and
extracts every ``Weakness``, ``Category``, and ``View`` entry (all three carry
IDs that show up in real finding data — e.g. CWE-16 "Configuration" is a
deprecated Category, not a Weakness, but findings reference it anyway).

The output ``cwe.json`` is a plain ``{"<id>": "<name>", ...}`` map, id keys as
strings sorted numerically, and is committed to the repo so that grison builds
and runs fully offline — no network access is needed at import or run time.
The catalog version is intentionally NOT embedded in the JSON (kept as a clean
id -> name map); re-run this script and check its stdout for the version of
whatever ``cwec_latest.xml.zip`` currently resolves to.

Usage:
    uv run python scripts/gen_cwe_index.py
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import httpx
from defusedxml import ElementTree as ET

CATALOG_URL = "https://cwe.mitre.org/data/xml/cwec_latest.xml.zip"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "grison" / "model" / "data" / "cwe.json"

# Local names (namespace stripped) of the catalog sections that carry
# ID/Name entries we want to index. Weaknesses alone are not enough: category
# and view IDs (e.g. CWE-16 "Configuration") show up in real finding data.
ENTRY_PARENT_TO_CHILD = {
    "Weaknesses": "Weakness",
    "Categories": "Category",
    "Views": "View",
}


def local_name(tag: str) -> str:
    """Strip the ``{namespace}`` prefix from an ElementTree tag."""
    return tag.rsplit("}", 1)[-1]


def download_catalog_xml_bytes() -> bytes:
    response = httpx.get(CATALOG_URL, follow_redirects=True, timeout=60.0)
    response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        xml_names = [n for n in zf.namelist() if n.endswith(".xml")]
        if len(xml_names) != 1:
            raise RuntimeError(f"expected exactly one XML file in the zip, found {xml_names!r}")
        return zf.read(xml_names[0])


def extract_entries(root: ET.Element) -> dict[str, str]:
    entries: dict[str, str] = {}
    for child in root:
        parent_name = local_name(child.tag)
        entry_tag = ENTRY_PARENT_TO_CHILD.get(parent_name)
        if entry_tag is None:
            continue
        for entry in child:
            if local_name(entry.tag) != entry_tag:
                continue
            cwe_id = entry.get("ID")
            name = entry.get("Name")
            if cwe_id is None or name is None:
                continue
            entries[cwe_id] = name
    return entries


def main() -> None:
    xml_bytes = download_catalog_xml_bytes()
    root = ET.fromstring(xml_bytes)

    version = root.get("Version", "<unknown>")
    entries = extract_entries(root)

    sorted_entries = {k: entries[k] for k in sorted(entries, key=int)}

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(sorted_entries, indent=2) + "\n", encoding="utf-8")

    print(f"CWE catalog version: {version}")
    print(f"Entries written: {len(sorted_entries)}")
    print(f"Output: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
