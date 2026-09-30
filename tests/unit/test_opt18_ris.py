"""
Tests OPT-18 — export RIS (``mappers/ris_exporter.py``).

Hors réseau. Couvre : types RIS, structure des enregistrements (``TY`` en
tête, ``ER`` en fin, ``TAG  - valeur``, fins de ligne CRLF, une valeur par
ligne), balises attendues (AU, A2, TI, T2, PY, DA, VL, IS, SP, EP, PB, CY,
SN, DO, UR, LA, KW, AB, N1), note ``N1`` issue d'une ligne ``Annote:``,
absence de métadonnée de moisson (invariant 1), extrait Google Scholar
jamais exporté en ``AB`` (invariant 6).

La relecture par un analyseur de référence (rispy) sur les corpus réels est
faite hors de la suite, dans un venv jetable (compte rendu OPT-18).
"""

import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from mappers.ris_exporter import (  # noqa: E402
    RIS_EOL, RIS_TYPES, export_ris, ris_record, ris_type,
)

_LINE_RE = re.compile(r"^([A-Z][A-Z0-9])  - (.*)$")


def parse_ris(text):
    """Relit un export RIS : liste d'enregistrements ``[(balise, valeur)]``.
    Échoue sur toute ligne mal formée ou tout enregistrement mal délimité."""
    assert "\n" not in text.replace("\r\n", ""), "fin de ligne non CRLF"
    records, current = [], None
    for line in text.split("\r\n"):
        if not line:
            continue
        m = _LINE_RE.match(line)
        assert m, f"ligne mal formée : {line!r}"
        tag, value = m.group(1), m.group(2)
        if tag == "TY":
            assert current is None, "TY sans ER précédent"
            current = [(tag, value)]
        else:
            assert current is not None, f"{tag} hors enregistrement"
            current.append((tag, value))
            if tag == "ER":
                assert value == ""
                records.append(current)
                current = None
    assert current is None, "enregistrement non fermé"
    return records


def _tags(rec, tag):
    return [v for t, v in rec if t == tag]


def _item(**kw):
    base = {"itemType": "journalArticle", "title": "Titre", "creators": [],
            "date": "2020", "extra": "Sources: HAL"}
    base.update(kw)
    return base


def _author(last, first="", role="author"):
    return {"creatorType": role, "lastName": last, "firstName": first}


class TestTypes:
    @pytest.mark.parametrize("item_type,expected", [
        ("journalArticle", "JOUR"), ("book", "BOOK"), ("bookSection", "CHAP"),
        ("conferencePaper", "CPAPER"), ("thesis", "THES"), ("report", "RPRT"),
        ("preprint", "UNPB"), ("dataset", "DATA"), ("document", "GEN"),
        ("webpage", "ELEC"), ("blogPost", "BLOG"), ("presentation", "SLIDE"),
        ("magazineArticle", "MGZN"), ("newspaperArticle", "NEWS"),
        ("encyclopediaArticle", "ENCYC"), ("letter", "PCOMM"),
        ("computerProgram", "COMP"), ("videoRecording", "VIDEO"),
        ("unknownType", "GEN"),
    ])
    def test_mapping(self, item_type, expected):
        assert ris_type(_item(itemType=item_type)) == expected

    def test_all_types_are_four_or_less_uppercase(self):
        for value in RIS_TYPES.values():
            assert re.fullmatch(r"[A-Z]{2,7}", value)


class TestRecord:
    def test_structure_and_order(self):
        rec = ris_record(_item())
        assert rec[0][0] == "TY" and rec[-1] == ("ER", "")

    def test_article_tags(self):
        item = _item(
            title="Les archives de Foucault",
            creators=[_author("de Certeau", "Michel"),
                      {"creatorType": "author", "name": "OMS"},
                      _author("Dupont", "Jean", role="editor")],
            date="2020-05-15", publicationTitle="Revue française",
            volume="70", issue="3", pages="p. 12 - 34", ISSN="0035-2950",
            DOI="https://doi.org/10.3917/RFSP.703", url="https://ex.org/a",
            language="fr", publisher="Presses de Sciences Po", place="Paris",
            tags=[{"tag": "archives"}, {"tag": "pouvoir, savoir"}],
            abstractNote="Un résumé\nsur deux lignes.")
        rec = ris_record(item)
        assert _tags(rec, "TY") == ["JOUR"]
        assert _tags(rec, "AU") == ["de Certeau, Michel", "OMS"]
        assert _tags(rec, "A2") == ["Dupont, Jean"]
        assert _tags(rec, "TI") == ["Les archives de Foucault"]
        assert _tags(rec, "T2") == ["Revue française"]
        assert _tags(rec, "PY") == ["2020"]
        assert _tags(rec, "DA") == ["2020/05/15"]
        assert _tags(rec, "VL") == ["70"] and _tags(rec, "IS") == ["3"]
        assert _tags(rec, "SP") == ["12"] and _tags(rec, "EP") == ["34"]
        assert _tags(rec, "SN") == ["0035-2950"]
        assert _tags(rec, "DO") == ["10.3917/RFSP.703"]
        assert _tags(rec, "UR") == ["https://ex.org/a"]
        assert _tags(rec, "LA") == ["fr"]
        assert _tags(rec, "PB") == ["Presses de Sciences Po"]
        assert _tags(rec, "CY") == ["Paris"]
        assert _tags(rec, "KW") == ["archives", "pouvoir, savoir"]
        assert _tags(rec, "AB") == ["Un résumé sur deux lignes."]

    def test_thesis_and_report(self):
        rec = ris_record(_item(itemType="thesis", university="Université X",
                               thesisType="Thèse de doctorat"))
        assert _tags(rec, "PB") == ["Université X"]
        assert _tags(rec, "M3") == ["Thèse de doctorat"]
        rec = ris_record(_item(itemType="report", institution="CNRS",
                               reportNumber="12"))
        assert _tags(rec, "PB") == ["CNRS"] and _tags(rec, "IS") == ["12"]

    def test_book_isbn_and_issn(self):
        rec = ris_record(_item(itemType="book", ISBN="978-2-13-000000-0",
                               extra="ISSN: 1234-5678"))
        assert _tags(rec, "SN") == ["1234-5678", "978-2-13-000000-0"]

    def test_conference_name_as_container(self):
        rec = ris_record(_item(itemType="conferencePaper",
                               conferenceName="JADT 2022"))
        assert _tags(rec, "T2") == ["JADT 2022"]

    def test_annote_note(self):
        stats = {}
        rec = ris_record(_item(extra="Sources: HAL\nAnnote: à relire"),
                         stats=stats)
        assert _tags(rec, "N1") == ["à relire"]
        assert stats["annotes"] == 1

    def test_no_harvest_metadata_by_default(self):
        item = _item(extra=("Sources: HAL, Cairn\nHAL ID: hal-01\nCited by: 3\n"
                            "OpenAlex ID: W1\nDOI source: x"))
        text = export_ris([item])
        for marker in ("Sources", "HAL ID", "Cited by", "OpenAlex", "DOI source"):
            assert marker not in text
        assert _tags(parse_ris(text)[0], "N1") == []

    def test_provenance_on_explicit_request(self):
        rec = ris_record(_item(extra="Sources: HAL\nAnnote: garder"),
                         include_provenance=True)
        assert _tags(rec, "N1") == ["garder", "Sources: HAL"]

    def test_scholar_snippet_not_exported(self):
        item = _item(abstractNote="… extrait", extra=(
            "Sources: Google Scholar (SerpAPI)\nScholar snippet: le champ "
            "résumé contient un extrait Google Scholar, non le résumé de "
            "l'éditeur"))
        stats = {}
        assert _tags(ris_record(item, stats=stats), "AB") == []
        assert stats["snippet_abstracts_skipped"] == 1

    def test_migrated_fields_and_doi_from_extra(self):
        item = _item(itemType="document", extra=(
            "Sources: CrossRef\nDOI: 10.1126/x\npublicationTitle: AAAS\n"
            "publisher: American Association"))
        rec = ris_record(item)
        assert _tags(rec, "DO") == ["10.1126/x"]
        assert _tags(rec, "T2") == ["AAAS"]
        assert _tags(rec, "PB") == ["American Association"]

    def test_language_und_omitted_and_single_page(self):
        rec = ris_record(_item(language="und", pages="e123"))
        assert _tags(rec, "LA") == []
        assert _tags(rec, "SP") == ["e123"] and _tags(rec, "EP") == []


class TestExport:
    def test_parses_and_counts(self):
        items = [_item(title=f"T{i}", creators=[_author("N", "P")])
                 for i in range(5)]
        stats = {}
        text = export_ris(items, stats=stats)
        records = parse_ris(text)
        assert len(records) == 5
        assert stats["entries"] == 5
        assert text.endswith("ER  - " + RIS_EOL)

    def test_values_are_single_line(self):
        text = export_ris([_item(title="a\nb\r\nc", abstractNote="x\n\ny")])
        rec = parse_ris(text)[0]
        assert _tags(rec, "TI") == ["a b c"]
        assert _tags(rec, "AB") == ["x y"]

    def test_empty_corpus(self):
        assert export_ris([]) == ""

    def test_deterministic(self):
        items = [_item(title="A"), _item(title="B")]
        assert export_ris(items) == export_ris(items)
