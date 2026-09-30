"""
Tests OPT-18 — corrections issues de la revue (exports BibTeX, BibLaTeX, RIS).

Hors réseau. Couvre :

- troncature Google Scholar (invariant 6) : une notice marquée
  ``Scholar truncation`` porte par défaut un avertissement (``note`` en
  BibTeX et BibLaTeX, ``N1`` en RIS), compté dans les statistiques, le
  manifeste, le résumé MCP et la CLI ; ``and others`` clôt la liste
  d'auteurs seulement quand sa troncature est établie (auteurs venus de
  Scholar, aucune revue) ; jamais d'auteur fictif en RIS ;
- contrôles C1 (U+0080 à U+009F) : réparés (Windows-1252) ou retirés
  (marqueurs de non-classement NSB/NSE, positions non définies), dans les
  deux formats ;
- protection de la casse d'un mot qui commence par un caractère échappé
  (``{{\\#MeToo}}``), vérifiée par une émulation de la règle du « caractère
  spécial » de ``change.case$`` (bibtex.web) ;
- description de l'outil : ``include_provenance`` n'exporte ni rang ni
  score.

Les caractères de contrôle sont construits par ``chr`` pour rester lisibles
dans le source.
"""

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

import mcp.types as types

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import cli  # noqa: E402
import mcp_server  # noqa: E402
from mappers.bibtex_exporter import (  # noqa: E402
    SCHOLAR_TRUNCATION_WARNING, bibtex_entry_fields, export_bibtex,
    export_corpus_file, latex_escape, normalize_space, protect_title,
    scholar_truncation, verbatim_value,
)
from mappers.ris_exporter import export_ris, ris_record  # noqa: E402
from utils import manifest as mf  # noqa: E402
from utils.deduplication import format_evidence  # noqa: E402

TRUNCATION_LINE = (
    "Scholar truncation: auteurs et/ou revue tronqués par Google Scholar — "
    "à vérifier contre la source primaire avant citation")
SCHOLAR = "Sources: Google Scholar (SerpAPI)"
C1_RE = re.compile("[" + chr(0x80) + "-" + chr(0x9F) + "]")


def _author(last, first=""):
    return {"creatorType": "author", "lastName": last, "firstName": first}


def _scholar_item(**kw):
    """Notice Scholar tronquée, forme réelle (corpus_accord_inter_annotateurs)."""
    item = {
        "itemType": "conferencePaper",
        "title": "Accord inter-annotateurs",
        "creators": [_author("Alkan", "AK"), _author("Grezes", "F"),
                     _author("Grouin", "C"), _author("Schüssler", "F")],
        "date": "2024",
        "proceedingsTitle": "Actes de la 31ème",
        "extra": f"{SCHOLAR}\nCited by: 3\n{TRUNCATION_LINE}",
    }
    item.update(kw)
    return item


def _fields(item, **kw):
    return dict(bibtex_entry_fields(item, **kw)[1])


def _run(coro):
    return asyncio.run(coro)


# === Troncature Google Scholar (invariant 6) ===

class TestScholarTruncation:
    def test_warning_written_by_default_bibtex_and_biblatex(self):
        item = _scholar_item()
        for dialect in ("bibtex", "biblatex"):
            stats = {}
            f = _fields(item, dialect=dialect, stats=stats)
            assert f["note"] == "{" + SCHOLAR_TRUNCATION_WARNING + "}"
            # Revue présente : la marque « auteurs et/ou revue » ne dit pas
            # lequel a été coupé ; pas de « and others » affirmé sans preuve.
            assert "others" not in f["author"]
            assert stats["scholar_truncation_warnings"] == 1
            assert "incomplete_author_lists" not in stats
            # Ni provenance de moisson ni ligne d'extra recopiée.
            assert "Cited by" not in f["note"] and "Sources" not in f["note"]

    def test_and_others_when_author_truncation_is_established(self):
        # Aucune revue : la marque ne peut porter que sur les auteurs.
        item = _scholar_item(itemType="document", proceedingsTitle="")
        assert scholar_truncation(item) == (True, True)
        for dialect in ("bibtex", "biblatex"):
            stats = {}
            f = _fields(item, dialect=dialect, stats=stats)
            assert f["author"] == ("{Alkan, AK and Grezes, F and Grouin, C "
                                   "and Schüssler, F and others}")
            assert f["note"] == "{" + SCHOLAR_TRUNCATION_WARNING + "}"
            assert stats["incomplete_author_lists"] == 1

    def test_migrated_venue_counts_as_a_venue(self):
        # Revue migrée dans extra par validate_and_clean (type sans champ).
        item = _scholar_item(itemType="book", proceedingsTitle="",
                             extra=f"{SCHOLAR}\npublicationTitle: Revue…\n"
                                   f"{TRUNCATION_LINE}")
        assert scholar_truncation(item) == (True, False)

    def test_merged_item_with_authors_from_another_source(self):
        # Fusion : auteurs de HAL (complets), revue de Scholar ; la ligne est
        # gardée pour la revue. Avertissement, jamais « and others ».
        evidence = format_evidence({
            "count": 2, "keys": [], "fuzzy": [], "ambiguous": "",
            "origins": {"creators": ["hal"],
                        "publicationTitle": ["google_scholar"]}})
        item = _scholar_item(
            itemType="document", proceedingsTitle="",
            extra=f"Sources: HAL, Google Scholar (SerpAPI)\n"
                  f"{TRUNCATION_LINE}\n{evidence}")
        assert scholar_truncation(item) == (True, False)
        f = _fields(item)
        assert "others" not in f["author"]
        assert f["note"] == "{" + SCHOLAR_TRUNCATION_WARNING + "}"

    def test_no_mark_no_note(self):
        item = _scholar_item(extra=SCHOLAR)
        assert scholar_truncation(item) == (False, False)
        f = _fields(item)
        assert "note" not in f and "others" not in f["author"]

    def test_provenance_follows_the_warning_without_duplicating_it(self):
        f = _fields(_scholar_item(), include_provenance=True)
        assert f["note"] == ("{" + SCHOLAR_TRUNCATION_WARNING + " ; " + SCHOLAR
                             + " ; Cited by: 3}")
        assert "Scholar truncation" not in f["note"]

    def test_ascii_mode_keeps_the_warning_ascii(self):
        text = export_bibtex([_scholar_item()], ascii=True)
        assert text.isascii()
        assert "tronqu{\\'e}s par Google Scholar" in text

    def test_ris_warning_in_n1_without_fictional_author(self):
        stats = {}
        rec = ris_record(_scholar_item(itemType="document",
                                       proceedingsTitle=""), stats=stats)
        tags = [t for t, _ in rec]
        assert tags.count("AU") == 4
        assert not any(v.lower() in ("others", "et al.") for _, v in rec)
        assert ("N1", SCHOLAR_TRUNCATION_WARNING) in rec
        assert stats["scholar_truncation_warnings"] == 1
        rec = ris_record(_scholar_item(), include_provenance=True)
        n1 = [v for t, v in rec if t == "N1"]
        assert n1 == [SCHOLAR_TRUNCATION_WARNING, f"{SCHOLAR} ; Cited by: 3"]
        assert ("N1", SCHOLAR_TRUNCATION_WARNING) not in ris_record(
            _scholar_item(extra=SCHOLAR))

    def test_counters_reach_the_manifest(self, tmp_path):
        corpus = tmp_path / "corpus_x.json"
        corpus.write_text(json.dumps([
            _scholar_item(), _scholar_item(itemType="document",
                                           proceedingsTitle="", title="Autre"),
            _scholar_item(extra=SCHOLAR, title="Sans marque")],
            ensure_ascii=False), encoding="utf-8")
        res = export_corpus_file(corpus, "bibtex")
        assert res.stats["scholar_truncation_warnings"] == 2
        assert res.stats["incomplete_author_lists"] == 1
        assert res.text.count(SCHOLAR_TRUNCATION_WARNING) == 2
        event = mf.read_manifest(res.manifest_path)["history"][-1]
        assert event["export"]["scholar_truncation_warnings"] == 2
        assert event["export"]["incomplete_author_lists"] == 1
        res = export_corpus_file(corpus, "ris")
        assert res.stats["scholar_truncation_warnings"] == 2
        event = mf.read_manifest(res.manifest_path)["history"][-1]
        assert "incomplete_author_lists" not in event["export"]

    def test_mcp_summary_and_structured(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        corpus = tmp_path / "corpus_x.json"
        corpus.write_text(json.dumps(
            [_scholar_item(), _scholar_item(itemType="document",
                                            proceedingsTitle="", title="B")],
            ensure_ascii=False), encoding="utf-8")
        blocks, st = _run(mcp_server.call_tool(
            "corpus_export", {"corpus_path": str(corpus)}))
        assert st["scholar_truncation_warnings"] == 2
        assert st["incomplete_author_lists"] == 1
        assert ("Notices dont Google Scholar a tronqué les auteurs et/ou la "
                "revue : 2, avertissement écrit dans note") in blocks[0].text
        assert "1 liste(s) d'auteurs close(s) par « and others »" in \
            blocks[0].text
        blocks, st = _run(mcp_server.call_tool(
            "corpus_export", {"corpus_path": str(corpus), "format": "ris"}))
        assert "avertissement écrit dans N1" in blocks[0].text
        assert "incomplete_author_lists" not in st

    def test_cli_reports_the_warnings(self, tmp_path, capsys):
        corpus = tmp_path / "corpus_x.json"
        corpus.write_text(json.dumps([_scholar_item()], ensure_ascii=False),
                          encoding="utf-8")
        assert cli.main(["export", str(corpus)]) == 0
        out = capsys.readouterr().out
        assert ("Notices tronquées par Google Scholar (auteurs et/ou revue), "
                "avertissement en note : 1") in out


# === Contrôles C1 ===

def _c1(*codes):
    return "".join(chr(c) for c in codes)


class TestC1Controls:
    TITLE = "L" + _c1(0x92) + "âge d" + _c1(0x92) + "or" + _c1(0x85)
    ABSTRACT = ("la " + _c1(0x91) + "ciencia" + _c1(0x92) + " et le c"
                + _c1(0x9C) + "ur, " + "'scale'" + _c1(0x9D) + " fin"
                + _c1(0x81))
    BOOK = _c1(0x98) + "The " + _c1(0x9C) + "European Union in affairs"

    def _item(self):
        return {"itemType": "bookSection", "title": self.TITLE,
                "creators": [_author("Dupont", "J" + _c1(0x96))],
                "bookTitle": self.BOOK, "abstractNote": self.ABSTRACT,
                "date": "2020", "url": "https://ex.org/a" + _c1(0x85) + "b"}

    def test_repair_rules(self):
        assert normalize_space(self.TITLE) == "L’âge d’or…"
        assert normalize_space(self.ABSTRACT) == \
            "la ‘ciencia’ et le cœur, 'scale' fin"
        # Marqueurs de non-classement NSB/NSE retirés, pas traduits.
        assert normalize_space(self.BOOK) == "The European Union in affairs"
        assert normalize_space(_c1(0x88, 0x98) + "x" + _c1(0x90, 0x8D)) == "x"
        assert verbatim_value("https://ex.org/a" + _c1(0x85) + "b") == \
            "https://ex.org/ab"

    def test_bibtex_utf8_has_no_c1(self):
        for dialect in ("bibtex", "biblatex"):
            text = export_bibtex([self._item()], dialect=dialect)
            assert not C1_RE.search(text), dialect
            assert "title = {L’âge d’or…}" in text
            assert "booktitle = {The {European} {Union} in affairs}" in text
            assert "abstract = {la ‘ciencia’ et le cœur, 'scale' fin}" in text
            assert "author = {Dupont, J–}" in text

    def test_bibtex_ascii(self):
        text = export_bibtex([self._item()], ascii=True)
        assert text.isascii()
        assert "title = {L'{\\^a}ge d'or{\\ldots}}" in text
        assert latex_escape(_c1(0x93) + "x" + _c1(0x94), ascii=True) == \
            "``x''"

    def test_ris_has_no_c1(self):
        text = export_ris([self._item()])
        assert not C1_RE.search(text)
        assert "TI  - L’âge d’or…\r\n" in text
        assert "T2  - The European Union in affairs\r\n" in text
        assert "UR  - https://ex.org/ab\r\n" in text


# === Protection de la casse : règle du « caractère spécial » de BibTeX ===

def _matching(text, i):
    depth = 0
    for j in range(i, len(text)):
        depth += (text[j] == "{") - (text[j] == "}")
        if depth == 0:
            return j
    raise AssertionError("accolades déséquilibrées")


def _lower_special(group):
    """Caractère spécial : lettres converties, sauf noms de commande."""
    out, i = [], 0
    while i < len(group):
        c = group[i]
        if c == "\\":
            j = i + 1
            if j < len(group) and group[j].isalpha():
                while j < len(group) and group[j].isalpha():
                    j += 1
            else:
                j += 1
            out.append(group[i:j])
            i = j
            continue
        out.append(c.lower())
        i += 1
    return "".join(out)


def change_case_t(text):
    """
    Émulation minimale de ``change.case$`` avec « t » (bibtex.web) : au
    niveau 0, lettres abaissées sauf la première ; un groupe ``{…}`` est
    conservé, sauf s'il s'ouvre par une contre-oblique (« caractère
    spécial ») : ses lettres hors noms de commande sont alors abaissées.
    """
    out, i, first = [], 0, True
    while i < len(text):
        c = text[i]
        if c == "{":
            j = _matching(text, i)
            group = text[i:j + 1]
            special = i + 1 < len(text) and text[i + 1] == "\\"
            out.append(_lower_special(group) if special and not first
                       else group)
            i, first = j + 1, False
            continue
        out.append(c if first else c.lower())
        first = False
        i += 1
    return "".join(out)


class TestCaseProtectionSpecialCharacter:
    def test_emulation_reproduces_the_bug(self):
        # Garde : l'ancienne sortie perdait la casse sous change.case$.
        assert change_case_t("Le mouvement {\\#MeToo} en {France}") == \
            "Le mouvement {\\#metoo} en {France}"

    @pytest.mark.parametrize("title,word,protected", [
        ("Le mouvement #MeToo en France", "MeToo", "{{\\#MeToo}}"),
        ("Les réseaux &Co et l'État", "Co", "{{\\&Co}}"),
        ("Le cas _Nature et HAL", "Nature", "{{\\_Nature}}"),
        ("Signe ~Tilde et Ok", "Tilde", "{{\\textasciitilde{}Tilde}}"),
    ])
    def test_escaped_initial_keeps_its_case(self, title, word, protected):
        out = protect_title(title)
        assert protected in out
        assert word in change_case_t(out)
        # Relecture : accolades toujours équilibrées.
        assert _matching("{" + out + "}", 0) == len(out) + 1

    def test_first_word_and_ascii_accents(self):
        out = protect_title("#MeToo et la France")
        assert out.startswith("{{\\#MeToo}}")
        # Mot ouvert par une commande d'accent (mode ASCII) : le groupe
        # extérieur s'ouvre par « { », protection ordinaire.
        out = protect_title("Les écrits Étienne", ascii=True)
        assert "{{\\'E}tienne}" in out
        assert "{\\'E}tienne" in change_case_t(out)

    def test_words_without_escape_unchanged(self):
        assert protect_title("Les archives de Foucault et HAL") == \
            "Les archives de {Foucault} et {HAL}"


# === Description de l'outil (rangs et scores) ===

def test_tool_description_does_not_promise_ranks_or_scores():
    tool, = [t for t in _run(mcp_server.list_tools())
             if t.name == "corpus_export"]
    desc = tool.description
    assert "sauf include_provenance=true" not in desc
    assert "les rangs et les scores ne sont jamais exportés" in desc
    prov = tool.inputSchema["properties"]["include_provenance"]["description"]
    assert "ni rang ni score" in prov
