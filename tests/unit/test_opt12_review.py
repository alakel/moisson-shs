"""
OPT-12, revue : non-régression des constats corrigés.

Sans aucun accès réseau (doublures des tests OPT-08 et OPT-12) :

- HAL : chaque lot est ramené au reste à collecter, le dernier compris, dans
  les deux modes (plus de 1 000 documents téléchargés pour en garder 1, 200
  ou 100 quand ``max_results`` dépasse 1 000) ; un manque (doublon de
  réplica, mappage en échec) se comble par un lot de plus ; en collecte,
  les notices retenues restent les premiers ``docid`` (résultat inchangé) ;
- ``SourceResult.aligned_ranks`` : à longueur égale, une liste réordonnée
  ou dont une notice a été remplacée n'hérite pas des rangs par position ;
- Isidore : un doublon servi par l'API laisse un trou dans les rangs natifs
  (position dans la réponse brute), comme pour HAL, dans une même page et
  d'une page à l'autre, pour la source directe comme pour les sous-sources.
"""

from __future__ import annotations

import pytest

import config
from connectors import hal as hal_module
from connectors.cairn import CairnConnector
from connectors.isidore import IsidoreConnector
from connectors.isidore_scan import ScanRegistry
from engine.models import SourceResult

from test_opt08_isidore_scan import FakeIsidore as FakeIsidoreScan, make_record
from test_opt12_hal_ranking import FakeSolr, best_by_score, docids, search


# === HAL : dernier lot borné au reste à collecter ===

@pytest.mark.parametrize("cap,expected_rows", [
    (1001, [1000, 1]),
    (1200, [1000, 200]),
    (2100, [1000, 1000, 100]),
    (2000, [1000, 1000]),
])
@pytest.mark.parametrize("mode", ["bibliographie", "collecte"])
def test_hal_last_batch_is_bounded_to_what_remains(cap, expected_rows, mode):
    fake = FakeSolr(5000)
    items, report = search(fake, max_results=cap, mode=mode)
    assert [c["rows"] for c in fake.calls] == expected_rows
    # Aucun document téléchargé en trop.
    assert sum(c["rows"] for c in fake.calls) == len(items) == cap
    assert report.limit_reached
    assert [r["rank"] for r in report.native_ranks] == list(range(1, cap + 1))
    if mode == "bibliographie":
        assert docids(items) == best_by_score(fake, cap)
        assert [c["start"] for c in fake.calls] == [
            sum(expected_rows[:i]) for i in range(len(expected_rows))]
    else:
        # Collecte : mêmes notices qu'avant la correction (premiers docid).
        assert docids(items) == list(range(1, cap + 1))


def test_hal_collecte_cursor_continues_after_a_short_batch():
    """Le curseur reprend exactement après le dernier document du lot réduit."""
    fake = FakeSolr(1500)
    items, _ = search(fake, max_results=1300, mode="collecte")
    assert [c["cursorMark"] for c in fake.calls] == ["*", "c1000"]
    assert docids(items) == list(range(1, 1301))


def test_hal_replica_shortfall_is_absorbed_by_the_next_batch(monkeypatch):
    monkeypatch.setattr(hal_module, "HAL_ROWS_PER_BATCH", 100)
    fake = FakeSolr(1000, replica_shift=True)
    items, report = search(fake, max_results=250, mode="bibliographie")
    ids = docids(items)
    assert len(ids) == len(set(ids)) == 250
    # 100, puis 100 (un doublon : 99 notices), puis le reste réel (51 et non
    # 50 : le manque de la page précédente est comblé).
    assert [c["rows"] for c in fake.calls] == [100, 100, 51]
    assert report.counters["duplicates"] == 1
    assert sum(c["rows"] for c in fake.calls) - len(items) == report.counters["duplicates"]
    assert len(report.native_ranks) == len(items)


def test_hal_mapping_error_on_the_last_batch_is_filled(monkeypatch):
    real = hal_module.map_hal_to_zotero
    fake = FakeSolr(3000)
    broken = best_by_score(fake, 1100)[1050]

    def flaky(doc):
        if doc["docid"] == broken:
            raise KeyError("title_s")
        return real(doc)
    monkeypatch.setattr(hal_module, "map_hal_to_zotero", flaky)
    items, report = search(fake, max_results=1200, mode="bibliographie")
    assert [c["rows"] for c in fake.calls] == [1000, 200, 1]
    assert len(items) == 1200 and report.counters["mapping_errors"] == 1
    ranks = [r["rank"] for r in report.native_ranks]
    assert 1051 not in ranks and ranks[-1] == 1201


def test_hal_single_batch_unchanged_for_the_mcp_default():
    fake = FakeSolr(5000)
    items, _ = search(fake, max_results=200, mode="bibliographie")
    assert [(c["start"], c["rows"]) for c in fake.calls] == [(0, 200)]


# === SourceResult.aligned_ranks : identité même à longueur égale ===

def _bound(keys="abc") -> SourceResult:
    items = [{"key": k} for k in keys]
    res = SourceResult(source="hal", display_name="HAL", items=items,
                       native_ranks=[{"rank": i + 1, "score": None, "sort": "s"}
                                     for i in range(len(items))])
    res.bind_ranks()
    return res


def _pairs(res: SourceResult):
    return [(it["key"], rk["rank"]) for it, rk in zip(res.items, res.aligned_ranks())]


def test_reordered_items_keep_their_own_ranks():
    res = _bound()
    a, b, c = res.items
    res.items = [c, b, a]
    assert _pairs(res) == [("c", 3), ("b", 2), ("a", 1)]
    assert [r["rank"] for r in res.ranking_dict()["ranks"]] == [3, 2, 1]


def test_replaced_item_at_equal_length_gives_no_rank():
    res = _bound()
    a, b, _ = res.items
    res.items = [a, b, {"key": "d"}]
    assert res.aligned_ranks() == [] and res.ranking_dict()["ranks"] == []


def test_equal_copy_is_not_the_same_notice():
    """Une copie égale (autre objet) n'hérite pas du rang : identité stricte."""
    res = _bound()
    res.items = [dict(it) for it in res.items]
    assert res.aligned_ranks() == []


def test_same_objects_same_order_and_unbound_result_use_positions():
    res = _bound()
    res.items = list(res.items)          # autre liste, mêmes objets
    assert _pairs(res) == [("a", 1), ("b", 2), ("c", 3)]
    unbound = SourceResult(source="x", display_name="X",
                           items=[{"key": "a"}, {"key": "b"}],
                           native_ranks=[{"rank": 4}, {"rank": 9}])
    assert [r["rank"] for r in unbound.aligned_ranks()] == [4, 9]


def test_keep_items_after_a_reordering_keeps_identity():
    res = _bound("abcd")
    a, b, c, d = res.items
    res.items = [d, c, b, a]
    res.keep_items([d, b])
    assert [r["rank"] for r in res.native_ranks] == [4, 2]
    assert _pairs(res) == [("d", 4), ("b", 2)]


# === Isidore : un doublon laisse un trou dans les rangs ===

def _isidore_ranks(records, cap=10, cls=IsidoreConnector, registry=None, **fake_kw):
    fake = FakeIsidoreScan(records, window=200, **fake_kw)
    c = cls(http_client=fake, scan_registry=registry or ScanRegistry())
    items = c.search("q", max_results=cap)
    assert len(items) == len(c.last_status.native_ranks)
    return [r["rank"] for r in c.last_status.native_ranks], fake


def test_isidore_duplicate_in_a_page_leaves_a_gap_like_hal():
    recs = [make_record(0), make_record(1), make_record(1), make_record(2),
            make_record(3)]
    ranks, _ = _isidore_ranks(recs)
    assert ranks == [1, 2, 4, 5]


def test_isidore_duplicate_across_pages_leaves_a_gap(monkeypatch):
    monkeypatch.setattr(config, "ISIDORE_REPLIES_PER_PAGE", 100, raising=False)
    recs = [make_record(i) for i in range(200)]
    recs[150] = make_record(10)            # page 2 reprend une notice de la page 1
    ranks, fake = _isidore_ranks(recs, cap=1000)
    assert fake.calls == [1, 2]
    assert len(ranks) == 199
    assert 151 not in ranks
    assert ranks == [i + 1 for i in range(200) if i != 150]


def test_isidore_subsource_gets_the_same_raw_positions():
    """Sous-source : même rang que la source directe pour la même notice."""
    host = "www.cairn.info"
    recs = [make_record(0, host=host), make_record(1, host="www.example.org"),
            make_record(0, host=host), make_record(2, host=host)]
    registry = ScanRegistry()
    direct, _ = _isidore_ranks(recs, registry=registry)
    assert direct == [1, 2, 4]
    cairn, _ = _isidore_ranks(recs, cls=CairnConnector, registry=ScanRegistry())
    assert cairn == [1, 4]
