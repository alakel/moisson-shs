"""
PAT-13 (retour d'expérience du 2026-09-30, B8) : le retour des outils
producteurs ne colle plus le résumé, les liens et le digest quand un client
met les blocs bout à bout (« …/moisson_shs/outputcorpus_… »,
« ….jsoncorpus://….csl.json… »).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import mcp_server

STEM = "corpus_test_20260930_000000_abcdef12"


def _files(tmp_path: Path):
    out = tmp_path / "moisson_shs" / "output"
    out.mkdir(parents=True)
    items = [{"itemType": "journalArticle", "title": f"T{i}", "creators": [],
              "date": "2020", "extra": "Sources: HAL", "tags": []}
             for i in range(3)]
    primary = out / f"{STEM}.json"
    primary.write_text(json.dumps(items), encoding="utf-8")
    csl = out / f"{STEM}.csl.json"
    csl.write_text("[]", encoding="utf-8")
    stats = out / f"{STEM}_stats.txt"
    stats.write_text("stats", encoding="utf-8")
    return items, primary, [csl, stats]


def _flatten(blocks) -> str:
    """Ce qu'affiche un client qui met les blocs bout à bout."""
    parts = []
    for b in blocks:
        if b.type == "text":
            parts.append(b.text)
        elif b.type == "resource_link":
            parts.append(str(b.uri))
    return "".join(parts)


@pytest.fixture(autouse=True)
def _stdio(monkeypatch):
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")


@pytest.mark.parametrize("inline", ["none", "full"])
def test_flattened_blocks_are_separated(tmp_path, inline):
    items, primary, extra = _files(tmp_path)
    summary = mcp_server._compact_files_line(primary, None)
    blocks, _st = mcp_server._build_corpus_tool_result(
        summary_text=summary, primary_items=items, primary_path=primary,
        primary_mime="application/json", extra_files=extra,
        structured={}, inline=inline)
    flat = _flatten(blocks)
    for glued in ("outputcorpus", "jsoncorpus://", "txtcorpus://",
                  "txtNotices"):
        assert glued not in flat, glued
    assert blocks[0].text.endswith("\n")
    links = [i for i, b in enumerate(blocks) if b.type == "resource_link"]
    assert links
    for i in links:
        assert blocks[i + 1].type == "text" and blocks[i + 1].text == "\n"


def test_files_line_puts_folder_on_its_own_line(tmp_path):
    _items, primary, _extra = _files(tmp_path)
    line = mcp_server._compact_files_line(primary, tmp_path / "x.manifest.json",
                                          "plan, statuts, rangs et scores")
    first, second = line.split("\n")
    assert first.startswith(f"Fichiers {STEM} : .json ")
    assert first.endswith(".manifest.json (plan, statuts, rangs et scores)")
    assert second == f"Dossier : {primary.parent}/"
