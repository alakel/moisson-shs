# -*- coding: utf-8 -*-
"""
Vérification finale du lot C : ORCID sans nom (critère de sortie d'OPT-14).

Constat live du 2026-09-29 (client stdio réel, clé OpenAlex du poste) :
``search_by_author(name="", orcid=…)`` n'interrogeait que HAL, alors que
l'argument ``orcid`` promet « HAL et OpenAlex par identifiant » et qu'OpenAlex
porte cet ORCID (237 travaux) : 0 notice. OpenAlex est désormais interrogé
par son filtre ``author.orcid`` quand un ORCID est fourni sans nom ;
Isidore et Semantic Scholar, qui ne savent chercher que sur un nom, restent
au rapport sans être interrogés.

Aucun accès réseau (doublures de ``test_opt14_author``).
"""

from __future__ import annotations

import anyio
import pytest

import mcp_server
import utils.lookups as lookups

from test_opt14_author import (  # noqa: E402
    Router, _sdk_call, install, is_openalex_works, item, oa_work, use_router,
)

SOCIOLOGIST_ORCID = "0000-0002-3892-9611"


@pytest.fixture(autouse=True)
def _schema_isolated(monkeypatch):
    import mappers.zotero_schema_loader as zsl
    monkeypatch.setattr(zsl, "load_zotero_schema", lambda *a, **k: None,
                        raising=False)


@pytest.fixture
def keys(monkeypatch):
    table = {"OPENALEX_API_KEY": "KEY"}
    monkeypatch.setattr(lookups, "get_api_key",
                        lambda name, required=False: table.get(name))


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    monkeypatch.setattr(mcp_server, "_MOISSON_SEM", None)
    return tmp_path


def _paged_works(url, params):
    """Deux pages de travaux OpenAlex (curseur), 4 travaux en tout."""
    if params.get("cursor") == "*":
        return {"results": [oa_work(1), oa_work(2)],
                "meta": {"count": 4, "next_cursor": "page2"}}
    assert params.get("cursor") == "page2"
    return {"results": [oa_work(3), oa_work(4)],
            "meta": {"count": 4, "next_cursor": None}}


def _name_searches(router):
    """Requêtes de recherche sur un nom (profils OpenAlex, S2, facette HAL)."""
    return [u for u, _ in router.calls
            if u.endswith("/authors") or u.endswith("/author/search")]


def test_library_orcid_without_name_queries_openalex_by_orcid(monkeypatch,
                                                               keys):
    monkeypatch.setattr(lookups, "AUTHOR_OPENALEX_PAGE_SIZE", 2)
    hal, iso = install(monkeypatch, hal_items=[item("Dépôt HAL")])
    router = Router([(is_openalex_works, _paged_works)])
    out = lookups.search_by_author("", orcid=SOCIOLOGIST_ORCID,
                                   max_per_source=10, http_client=router)
    by = {r.display_name: r for r in out.report.sources}
    oa = by["OpenAlex"]
    assert oa.method == lookups.METHOD_ORCID
    assert oa.identity_verified and oa.item_count == 4
    assert oa.pages == 2
    # Aucune recherche sur un nom ; seul le filtre ORCID est envoyé.
    assert _name_searches(router) == []
    assert {p.get("filter") for _, p in router.calls} == {
        f"author.orcid:{SOCIOLOGIST_ORCID}"}
    for key in ("Isidore", "Semantic Scholar"):
        assert by[key].status == "skipped"
        assert by[key].reason == lookups.REASON_NO_IDENTITY
    assert iso.calls == []


def test_mcp_orcid_without_name_returns_openalex_works(monkeypatch, keys,
                                                        out_dir):
    monkeypatch.setattr(lookups, "AUTHOR_OPENALEX_PAGE_SIZE", 2)
    install(monkeypatch)
    router = Router([(is_openalex_works, _paged_works)])
    use_router(monkeypatch, router)
    res = anyio.run(_sdk_call, "search_by_author",
                    {"name": "", "orcid": SOCIOLOGIST_ORCID,
                     "max_per_source": 10})
    assert res.isError is False
    st = res.structuredContent["by_source_status"]
    assert st["OpenAlex"]["method"] == "orcid"
    assert st["OpenAlex"]["items"] == 4 and st["OpenAlex"]["pages"] == 2
    assert st["Isidore"]["status"] == "skipped"
    assert st["Semantic Scholar"]["status"] == "skipped"
    assert res.structuredContent["total_unique"] == 4
    assert _name_searches(router) == []
    # Sources sans identifiant propre : complétude jamais promise.
    assert res.structuredContent["production_complete"] is False
