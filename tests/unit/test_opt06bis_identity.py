"""
OPT-06 bis : portails HAL du CCSD dans ``utils.identity``.

Cas réel (moisson live du 2026-09-28) : le mémoire DUMAS ``dumas-01920660``
restait en double, la notice Isidore (``dumas.ccsd.cnrs.fr/dumas-01920660``)
ne portant pas la clé ``hal:`` de la notice HAL (``HAL ID: dumas-01920660``).
Relevé sur output/ : ``dumas.ccsd.cnrs.fr`` (29 URL) et
``archivesic.ccsd.cnrs.fr`` (6 URL, identifiants historiques ``sic_…``)
n'étaient pas reconnus.
"""

import copy
import json
from pathlib import Path

import pytest

from utils.deduplication import deduplicate_with_report, parse_evidence
from utils.identity import hal_id_from_url, normalize_hal_id, strong_keys

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "dedup_pairs.json"
PAIRS = {p["id"]: p for group in ("positives", "negatives")
         for p in json.loads(FIXTURE.read_text(encoding="utf-8"))[group]}


class TestCcsdPortals:
    @pytest.mark.parametrize("url,expected", [
        # Nouveaux : portails *.ccsd.cnrs.fr (échouent avant OPT-06 bis).
        ("https://dumas.ccsd.cnrs.fr/dumas-01920660", "dumas-01920660"),
        ("https://dumas.ccsd.cnrs.fr/dumas-01920660v1/document", "dumas-01920660"),
        ("https://dumas.ccsd.cnrs.fr/dumas-01920660v1/file/memoire.pdf",
         "dumas-01920660"),
        ("https://archivesic.ccsd.cnrs.fr/sic_01002311v1", "sic_01002311"),
        ("https://archivesic.ccsd.cnrs.fr/sic_01808316/document", "sic_01808316"),
        ("https://memsic.ccsd.cnrs.fr/mem_00000123v1", "mem_00000123"),
        ("https://hal-emse.ccsd.cnrs.fr/emse-03260848v1", "emse-03260848"),
        # Déjà reconnus : non-régression.
        ("https://tel.archives-ouvertes.fr/tel-01234567v2", "tel-01234567"),
        ("https://halshs.archives-ouvertes.fr/halshs-01526584", "halshs-01526584"),
        ("https://medihal.archives-ouvertes.fr/medihal-01234567", "medihal-01234567"),
        ("https://hal.archives-ouvertes.fr/hal-01950381", "hal-01950381"),
        ("https://dumas.hal.science/dumas-04312111", "dumas-04312111"),
        ("https://theses.hal.science/tel-03689721/", "tel-03689721"),
        ("https://hal.science/hal-02533314/", "hal-02533314"),
        ("https://hal.univ-lorraine.fr/hal-01684217v1", "hal-01684217"),
    ])
    def test_hal_id_from_portal_url(self, url, expected):
        assert hal_id_from_url(url) == expected

    @pytest.mark.parametrize("url", [
        "https://www.ccsd.cnrs.fr/2020/01/actualite",   # site du CCSD
        "https://ccsd.cnrs.fr/hal-01234567",            # pas un portail
        "https://fakearchives-ouvertes.fr/hal-01234567",
        "https://example.org/sic_01002311",
        "https://dumas.ccsd.cnrs.fr/search/index/?q=forums",
        "https://dumas.ccsd.cnrs.fr/dumas-01920660/document#page=3",
        "https://archivesic.ccsd.cnrs.fr/",
    ])
    def test_not_a_deposit(self, url):
        assert hal_id_from_url(url) is None

    def test_legacy_underscore_identifier(self):
        assert normalize_hal_id("sic_01002311v1") == "sic_01002311"
        assert normalize_hal_id("SIC_01002311") == "sic_01002311"
        assert normalize_hal_id("hal-01234567v3") == "hal-01234567"
        assert normalize_hal_id("sic_0100231") is None      # sept chiffres

    def test_declared_and_derived_keys_meet(self):
        """ArchiveSIC : ligne ``HAL ID: sic_…`` d'une notice HAL et URL
        ``archivesic.ccsd.cnrs.fr/sic_…v1`` d'une notice Isidore."""
        hal = {"itemType": "journalArticle", "title": "T",
               "extra": "Sources: HAL\nHAL ID: sic_01002311"}
        isidore = {"itemType": "journalArticle", "title": "T",
                   "url": "https://archivesic.ccsd.cnrs.fr/sic_01002311v1",
                   "extra": "Sources: Isidore"}
        assert "hal:sic_01002311" in strong_keys(hal)
        assert "hal:sic_01002311" in strong_keys(isidore)


class TestDumasRealCase:
    def test_isidore_record_gets_the_hal_key(self):
        """Échoue avant OPT-06 bis : aucune clé hal: sur la notice Isidore."""
        assert "hal:dumas-01920660" in strong_keys(PAIRS["P51"]["a"])

    def test_pair_merges_through_the_hal_key(self):
        pair = PAIRS["P51"]
        out, report = deduplicate_with_report([copy.deepcopy(pair["a"]),
                                               copy.deepcopy(pair["b"])])
        assert len(out) == 1
        ev = parse_evidence(out[0])
        assert ev["count"] == 2 and "hal:dumas-01920660" in ev["keys"]
        assert report["strong_merges"] == 1

    def test_hal_key_still_bound_by_the_year_gap(self):
        """Le lien hal: garde sa contrainte d'années (versions distinctes)."""
        pair = PAIRS["P51"]
        a, b = copy.deepcopy(pair["a"]), copy.deepcopy(pair["b"])
        b["date"] = "2021"
        out, report = deduplicate_with_report([a, b])
        assert len(out) == 2 and report["version_splits"] == 2
