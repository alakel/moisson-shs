"""
Tests OPT-18 — export BibTeX / BibLaTeX (``mappers/bibtex_exporter.py``).

Hors réseau. Couvre : types d'entrée (deux dialectes), échappement LaTeX
(``& % $ # _ { } ~ ^ \\``, accolades déséquilibrées, accents en UTF-8 ou en
commandes LaTeX), auteurs (``Nom, Prénom``, institutionnels entre accolades),
clés de citation stables, uniques et indépendantes de l'ordre (suffixes
a/b/c), champ ``annote``, absence de toute métadonnée de moisson (invariant 1),
extrait Google Scholar jamais exporté comme résumé (invariant 6), champs
migrés dans ``extra`` relus, et relecture de chaque export par un analyseur
minimal qui compte les accolades comme BibTeX (sans tenir compte des
contre-obliques).

La validation par des analyseurs de référence (bibtexparser 2, pybtex en mode
strict) sur les corpus réels est faite hors de la suite, dans un venv jetable
(compte rendu OPT-18), pour ne pas ajouter de dépendance au projet.
"""

import random
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from mappers.bibtex_exporter import (  # noqa: E402
    bibtex_entry_fields, bibtex_names, citation_key_base, entry_type,
    export_bibtex, latex_escape, make_citation_keys, parse_date,
    protect_title, split_pages,
)


# === Analyseur minimal (comptage des accolades à la manière de BibTeX) ===

_ENTRY_RE = re.compile(r"@(\w+)\{([^,\s{}]+),")
_FIELD_RE = re.compile(r"([a-z]+)\s*=\s*")
_WS = re.compile(r"\s*")


def parse_bib(text):
    """
    Relit un export : ``[(type, clé, {champ: valeur})]``. Échoue (assertion)
    sur toute entrée mal formée, en particulier des accolades déséquilibrées
    (comptées sans égard aux contre-obliques, comme BibTeX et biber).
    """
    entries = []
    pos = 0
    while True:
        at = text.find("@", pos)
        if at < 0:
            break
        m = _ENTRY_RE.match(text, at)
        assert m, f"entrée mal formée : {text[at:at + 80]!r}"
        etype, key = m.group(1), m.group(2)
        pos = m.end()
        fields = {}
        while True:
            pos = _WS.match(text, pos).end()
            if text[pos] == "}":
                pos += 1
                break
            fm = _FIELD_RE.match(text, pos)
            assert fm, f"champ mal formé : {text[pos:pos + 60]!r}"
            name = fm.group(1)
            pos = fm.end()
            if text[pos] == "{":
                depth, start = 0, pos
                while True:
                    assert pos < len(text), f"accolades déséquilibrées ({key})"
                    c = text[pos]
                    if c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0:
                            break
                    pos += 1
                value = text[start + 1:pos]
                pos += 1
            else:
                vm = re.compile(r"[a-z0-9]+").match(text, pos)
                assert vm, f"valeur mal formée : {text[pos:pos + 40]!r}"
                value = vm.group(0)
                pos = vm.end()
            assert name not in fields, f"champ en double : {name}"
            fields[name] = value
            pos = _WS.match(text, pos).end()
            if text[pos] == ",":
                pos += 1
        entries.append((etype, key, fields))
    return entries


def _balanced(value):
    depth = 0
    for c in value:
        depth += (c == "{") - (c == "}")
        if depth < 0:
            return False
    return depth == 0


def _item(**kw):
    base = {"itemType": "journalArticle", "title": "Titre", "creators": [],
            "date": "2020", "extra": "Sources: HAL"}
    base.update(kw)
    return base


def _author(last, first=""):
    return {"creatorType": "author", "lastName": last, "firstName": first}


def _fields(item, **kw):
    etype, pairs = bibtex_entry_fields(item, **kw)
    return etype, dict(pairs)


# === Types d'entrée ===

class TestEntryTypes:
    @pytest.mark.parametrize("item_type,bibtex,biblatex", [
        ("journalArticle", "article", "article"),
        ("magazineArticle", "article", "article"),
        ("newspaperArticle", "article", "article"),
        ("book", "book", "book"),
        ("bookSection", "incollection", "incollection"),
        ("conferencePaper", "inproceedings", "inproceedings"),
        ("thesis", "phdthesis", "thesis"),
        ("report", "techreport", "report"),
        ("preprint", "misc", "online"),
        ("webpage", "misc", "online"),
        ("blogPost", "misc", "online"),
        ("dataset", "misc", "dataset"),
        ("document", "misc", "misc"),
        ("presentation", "misc", "misc"),
        ("encyclopediaArticle", "incollection", "inreference"),
        ("manuscript", "unpublished", "unpublished"),
        ("computerProgram", "misc", "software"),
        ("unknownType", "misc", "misc"),
    ])
    def test_mapping(self, item_type, bibtex, biblatex):
        item = _item(itemType=item_type)
        assert entry_type(item, "bibtex") == bibtex
        assert entry_type(item, "biblatex") == biblatex

    def test_masters_thesis(self):
        item = _item(itemType="thesis", thesisType="Mémoire de master")
        assert entry_type(item, "bibtex") == "mastersthesis"
        etype, f = _fields(item, dialect="biblatex")
        assert etype == "thesis" and f["type"] == "{mathesis}"

    def test_doctoral_and_hdr_thesis(self):
        phd = _item(itemType="thesis", thesisType="Thèse de doctorat",
                    university="Université Bordeaux Montaigne")
        etype, f = _fields(phd)
        assert etype == "phdthesis"
        assert f["school"] == "{Université Bordeaux Montaigne}"
        assert f["type"] == "{Thèse de doctorat}"
        assert _fields(phd, dialect="biblatex")[1]["type"] == "{phdthesis}"
        hdr = _item(itemType="thesis",
                    thesisType="Habilitation à diriger des recherches")
        assert entry_type(hdr, "bibtex") == "phdthesis"
        f = _fields(hdr, dialect="biblatex")[1]
        assert f["type"] == "{Habilitation à diriger des recherches}"

    def test_inbook_when_book_author_known(self):
        item = _item(itemType="bookSection", bookTitle="Œuvres", creators=[
            _author("Foucault", "Michel"),
            {"creatorType": "bookAuthor", "lastName": "Foucault",
             "firstName": "Michel"}])
        assert entry_type(item, "biblatex") == "inbook"
        assert entry_type(item, "bibtex") == "incollection"
        f = _fields(item, dialect="biblatex")[1]
        assert f["bookauthor"] == "{Foucault, Michel}"
        assert "bookauthor" not in _fields(item)[1]

    def test_report_fields(self):
        item = _item(itemType="report", institution="CNRS",
                     reportNumber="12", reportType="Rapport de recherche")
        etype, f = _fields(item)
        assert etype == "techreport"
        assert f["institution"] == "{CNRS}" and f["number"] == "{12}"
        assert f["type"] == "{Rapport de recherche}"
        f2 = _fields(_item(itemType="report"), dialect="biblatex")[1]
        assert f2["type"] == "{techreport}"


# === Échappement LaTeX ===

class TestEscaping:
    @pytest.mark.parametrize("raw,expected", [
        ("a & b", r"a \& b"),
        ("100 %", r"100 \%"),
        ("5 $", r"5 \$"),
        ("#1", r"\#1"),
        ("a_b", r"a\_b"),
        ("a~b", r"a\textasciitilde{}b"),
        ("x^2", r"x\textasciicircum{}2"),
        ("a\\b", r"a\textbackslash{}b"),
        ("{HAL}", r"\{HAL\}"),
    ])
    def test_specials(self, raw, expected):
        out = latex_escape(raw)
        assert out == expected
        assert _balanced(out)

    def test_unbalanced_braces_never_break_the_file(self):
        for raw in ("a { b", "a } b", "} {", "{{ }", "x}{y"):
            out = latex_escape(raw)
            assert _balanced(out), (raw, out)
            assert r"\textbrace" in out
            entries = parse_bib(export_bibtex([_item(title=raw)]))
            assert len(entries) == 1

    def test_backslash_does_not_escape_a_brace(self):
        # BibTeX compte « \{ » comme une accolade : une accolade isolée
        # précédée d'une contre-oblique dans la source ne doit pas survivre.
        out = latex_escape("a \\{ b")
        assert _balanced(out)

    def test_utf8_preserved_by_default(self):
        s = "Élise Œuvray « écrit » ça — naïve ß"
        assert latex_escape(s) == s

    def test_ascii_mode(self):
        assert latex_escape("é", ascii=True) == r"{\'e}"
        assert latex_escape("è", ascii=True) == r"{\`e}"
        assert latex_escape("ç", ascii=True) == r"{\c{c}}"
        assert latex_escape("ô", ascii=True) == r"{\^o}"
        assert latex_escape("ü", ascii=True) == r'{\"u}'
        assert latex_escape("œ", ascii=True) == r"{\oe}"
        assert latex_escape("Æ", ascii=True) == r"{\AE}"
        assert latex_escape("ı", ascii=True) == r"{\i}"
        assert latex_escape("í", ascii=True) == r"{\'\i}"
        assert latex_escape("« a »", ascii=True) == \
            r"{\guillemotleft} a {\guillemotright}"
        assert latex_escape("a\u00a0b", ascii=True) == "a~b"
        assert latex_escape("a–b—c", ascii=True) == "a--b---c"
        assert latex_escape("ﬀ", ascii=True) == "ff"

    def test_ascii_mode_output_is_ascii(self):
        text = export_bibtex([_item(
            title="Économie « politique » : Œuvres ça naïve ﬁn ªº ´",
            creators=[_author("Łukasiewicz", "Jan"),
                      {"creatorType": "author", "name": "Organización ñ"}],
            abstractNote="Résumé ; 100 € ; ° ; ×",
            publicationTitle="Revue française")], ascii=True)
        assert text.isascii()
        assert len(parse_bib(text)) == 1

    def test_invisible_format_characters_removed(self):
        assert latex_escape("Argumentat\u200bion") == "Argumentation"
        assert latex_escape("\u202aTitre\u202c") == "Titre"
        assert latex_escape("a\u00adb") == "ab"
        # Espace insécable conservée en UTF-8.
        assert latex_escape("a\u00a0:") == "a\u00a0:"

    def test_whitespace_collapsed(self):
        assert latex_escape("  a \n\t b  ") == "a b"


class TestTitleCaseProtection:
    def test_words_with_capitals_are_protected(self):
        assert protect_title("Les archives de Foucault") == \
            "Les archives de {Foucault}"
        assert protect_title("HAL et la science ouverte") == \
            "{HAL} et la science ouverte"
        assert protect_title("Le numérique") == "Le numérique"

    def test_protection_keeps_braces_balanced(self):
        for raw in ("A {B} C", "Titre { déséquilibré", "{HAL} }", "X & Y_Z"):
            out = protect_title(raw)
            assert _balanced(out), (raw, out)


# === Auteurs ===

class TestNames:
    def test_last_first_and(self):
        creators = [_author("Foucault", "Michel"), _author("de Certeau",
                                                            "Michel")]
        assert bibtex_names(creators) == \
            "Foucault, Michel and de Certeau, Michel"

    def test_institutional_in_braces(self):
        c = [{"creatorType": "author", "name": "Organisation mondiale & co"}]
        assert bibtex_names(c) == r"{Organisation mondiale \& co}"

    def test_multiword_last_name_without_first_name(self):
        assert bibtex_names([_author("El Ayari")]) == "{El Ayari}"
        assert bibtex_names([_author("El Ayari", "S")]) == "El Ayari, S"

    def test_and_or_comma_inside_a_name_is_protected(self):
        assert bibtex_names([_author("Smith and Jones", "A")]) == \
            "{Smith and Jones}, A"
        assert bibtex_names([_author("King, Jr", "Martin")]) == \
            "{King, Jr}, Martin"

    def test_author_like_roles_only_without_authors(self):
        contrib = {"creatorType": "contributor", "lastName": "Aide",
                   "firstName": "A"}
        f = _fields(_item(creators=[contrib]))[1]
        assert f["author"] == "{Aide, A}"
        f = _fields(_item(creators=[_author("Nom", "P"), contrib]))[1]
        assert f["author"] == "{Nom, P}"

    def test_editors_and_translators(self):
        item = _item(itemType="book", creators=[
            {"creatorType": "editor", "lastName": "Dupont", "firstName": "J"},
            {"creatorType": "translator", "lastName": "Martin",
             "firstName": "L"}])
        f = _fields(item)[1]
        assert f["editor"] == "{Dupont, J}"
        assert f["translator"] == "{Martin, L}"
        assert "author" not in f


# === Clés de citation ===

class TestCitationKeys:
    def test_base_format(self):
        item = _item(title="Surveiller et punir", date="1975-02-01",
                     creators=[_author("Foucault", "Michel")])
        assert citation_key_base(item) == "foucault1975surveiller"

    def test_stopwords_particles_accents(self):
        item = _item(title="L'écriture de l'histoire", date="1975",
                     creators=[_author("de Certeau", "Michel")])
        assert citation_key_base(item) == "certeau1975ecriture"
        item = _item(title="Œuvres complètes", date="",
                     creators=[_author("Lévi-Strauss", "C.")])
        assert citation_key_base(item) == "levistraussndoeuvres"

    def test_institutional_and_anonymous(self):
        item = _item(title="Rapport annuel", creators=[
            {"creatorType": "author", "name": "Organisation mondiale de la santé"}])
        assert citation_key_base(item) == "organisation2020rapport"
        # « sans » est un mot vide : le premier mot significatif est retenu.
        assert citation_key_base(_item(title="Sans auteur connu")) == \
            "anon2020auteur"

    def test_charset_is_ascii_alnum(self):
        items = [_item(title=t, creators=[_author(n, "x")]) for t, n in [
            ("Ça ira", "Ñúñez"), ("Über alles", "Øster"), ("汉字", "李"),
            ("", ""), ("---", "O'Brien")]]
        for key in make_citation_keys(items):
            assert re.fullmatch(r"[a-z0-9]+", key), key

    def test_disambiguation_abc_and_uniqueness(self):
        items = [_item(title=f"Le numérique {suffix}", date="2020",
                       creators=[_author("Martin", "A")], url=f"u{suffix}")
                 for suffix in ("x", "y", "z")]
        keys = make_citation_keys(items)
        assert sorted(keys) == ["martin2020numeriquea", "martin2020numeriqueb",
                                "martin2020numeriquec"]
        assert len(set(keys)) == 3

    def test_single_key_stays_bare(self):
        keys = make_citation_keys([
            _item(title="Numérique", creators=[_author("Martin", "A")]),
            _item(title="Autre", creators=[_author("Martin", "A")])])
        assert keys == ["martin2020numerique", "martin2020autre"]

    def test_keys_do_not_depend_on_order(self):
        items = [_item(title=f"Le numérique {i % 3}", date="2020",
                       creators=[_author("Martin", "A")],
                       DOI=f"10.1000/{i}") for i in range(9)]
        items += [_item(title="Autre", creators=[_author("Durand", "B")])]
        reference = dict(zip((id(i) for i in items), make_citation_keys(items)))
        rng = random.Random(18)
        for _ in range(20):
            shuffled = items[:]
            rng.shuffle(shuffled)
            keys = make_citation_keys(shuffled)
            assert {id(i): k for i, k in zip(shuffled, keys)} == reference
            assert len(set(keys)) == len(keys)

    def test_suffix_skips_an_existing_key(self):
        # « martin2020numeriquea » existe déjà comme clé de base : les
        # doublons de « martin2020numerique » ne doivent pas la reprendre.
        items = [
            _item(title="Numériquea", creators=[_author("Martin", "A")]),
            _item(title="Numérique", creators=[_author("Martin", "A")],
                  url="1"),
            _item(title="Numérique", creators=[_author("Martin", "A")],
                  url="2"),
        ]
        keys = make_citation_keys(items)
        assert keys[0] == "martin2020numeriquea"
        assert sorted(keys[1:]) == ["martin2020numeriqueb",
                                    "martin2020numeriquec"]

    def test_many_collisions_beyond_z(self):
        items = [_item(title="Même titre", creators=[_author("Martin", "A")],
                       url=str(i)) for i in range(30)]
        keys = make_citation_keys(items)
        assert len(set(keys)) == 30
        assert "martin2020memeaa" in keys and "martin2020memez" in keys


# === Champs ===

class TestFields:
    def test_annote_from_extra(self):
        item = _item(extra="Sources: HAL\nAnnote: Lecture prioritaire\n"
                           "Cited by: 3")
        assert _fields(item)[1]["annote"] == "{Lecture prioritaire}"
        item = _item(extra="annote: minuscule")
        assert _fields(item)[1]["annote"] == "{minuscule}"
        assert "annote" not in _fields(_item())[1]

    def test_no_harvest_metadata_by_default(self):
        item = _item(extra=(
            "Sources: HAL, Cairn\nHAL ID: hal-01234567\nCited by: 12\n"
            "OpenAlex ID: https://openalex.org/W1\nDedup evidence: doi\n"
            "DOI source: appariement bibliographique CrossRef\n"
            "Scholar cluster ID: 42\nAnnote: garder"))
        text = export_bibtex([item])
        for marker in ("Sources", "HAL ID", "Cited by", "OpenAlex",
                       "Dedup evidence", "DOI source", "Scholar", "hal-0123"):
            assert marker not in text, marker
        fields = parse_bib(text)[0][2]
        assert "note" not in fields
        assert fields["annote"] == "garder"

    def test_provenance_only_on_explicit_request(self):
        item = _item(extra="Sources: HAL\nHAL ID: hal-01\nAnnote: garder")
        f = _fields(item, include_provenance=True)[1]
        assert f["note"] == "{Sources: HAL ; HAL ID: hal-01}"
        assert "Annote" not in f["note"]

    def test_identifiers_and_numbering(self):
        item = _item(
            title="Article", publicationTitle="Revue française", volume="70",
            issue="3", pages="p. 12 - 34", DOI="https://doi.org/10.3917/RFSP.703",
            ISSN="0035-2950", url="https://ex.org/a_b?x=1%20y#frag",
            language="fr", tags=[{"tag": "archives"}, {"tag": "pouvoir, savoir"},
                                 {"tag": "Archives"}])
        f = _fields(item)[1]
        assert f["journal"] == "{Revue française}"
        assert f["volume"] == "{70}" and f["number"] == "{3}"
        assert f["pages"] == "{12--34}"
        assert f["doi"] == "{10.3917/RFSP.703}"
        assert f["issn"] == "{0035-2950}"
        # URL écrite telle quelle (champ verbatim de biber).
        assert f["url"] == "{https://ex.org/a_b?x=1%20y#frag}"
        assert f["language"] == "{fr}"
        assert f["keywords"] == "{archives, {pouvoir, savoir}}"
        fb = _fields(item, dialect="biblatex")[1]
        assert fb["journaltitle"] == "{Revue française}"
        assert fb["langid"] == "{french}"

    def test_url_unsafe_characters_encoded(self):
        f = _fields(_item(url="https://ex.org/a b{c}\\d"))[1]
        assert f["url"] == "{https://ex.org/a%20b%7Bc%7D%5Cd}"

    def test_doi_read_from_extra_never_from_doi_source(self):
        item = _item(itemType="book", extra=(
            "Sources: OpenAlex\nDOI source: 10.9999/faux\nDOI: 10.1000/Livre.1"))
        assert _fields(item)[1]["doi"] == "{10.1000/Livre.1}"
        item = _item(itemType="book", extra="DOI source: 10.9999/faux")
        assert "doi" not in _fields(item)[1]

    def test_migrated_fields_read_from_extra(self):
        item = _item(itemType="letter", extra=(
            "Sources: OpenAlex\npublicationTitle: Critical Care\nvolume: 11\n"
            "issue: 3\npages: 135-135\nISSN: 1364-8535\nlanguage: en"))
        f = _fields(item)[1]
        assert f["howpublished"] == "{Critical Care}"
        assert f["volume"] == "{11}" and f["number"] == "{3}"
        assert f["pages"] == "{135}"
        assert f["issn"] == "{1364-8535}"

    def test_scholar_snippet_not_exported_as_abstract(self):
        snippet = _item(
            abstractNote="… extrait tronqué",
            extra="Sources: Google Scholar (SerpAPI)\nScholar snippet: le "
                  "champ résumé contient un extrait Google Scholar, non le "
                  "résumé de l'éditeur")
        stats = {}
        f = _fields(snippet, stats=stats)[1]
        assert "abstract" not in f
        assert stats["snippet_abstracts_skipped"] == 1
        real = _item(abstractNote="Un vrai résumé.")
        assert _fields(real)[1]["abstract"] == "{Un vrai résumé.}"

    def test_dates(self):
        f = _fields(_item(date="2020-05-15"))[1]
        assert f["year"] == "{2020}" and f["month"] == "may"
        assert _fields(_item(date="2020-05-15"),
                       dialect="biblatex")[1]["date"] == "{2020-05-15}"
        assert _fields(_item(date="2019"), dialect="biblatex")[1]["date"] == \
            "{2019}"
        f = _fields(_item(date="[s.d.]"))[1]
        assert "year" not in f and "month" not in f
        assert parse_date("2021-08-01T00:00:00+00:00") == (2021, 8, 1)
        assert parse_date("mai 2019") == (2019, None, None)

    def test_pages_normalization(self):
        assert split_pages("12-34") == ("12", "34")
        assert split_pages("p. 12 - 34") == ("12", "34")
        assert split_pages("pp 12–34") == ("12", "34")
        assert split_pages("e123-e130") == ("e123", "e130")
        assert split_pages("135-135") == ("135", "")
        assert split_pages("45") == ("45", "")
        assert split_pages("250 p.") == ("250 p.", "")

    def test_language_und_omitted(self):
        assert "language" not in _fields(_item(language="und"))[1]
        assert "langid" not in _fields(_item(language="xx"),
                                       dialect="biblatex")[1]

    def test_conference_and_book_section(self):
        conf = _item(itemType="conferencePaper",
                     proceedingsTitle="Actes du colloque JADT",
                     conferenceName="JADT 2022")
        f = _fields(conf)[1]
        assert f["booktitle"] == "{Actes du colloque {JADT}}"
        fb = _fields(conf, dialect="biblatex")[1]
        assert fb["eventtitle"] == "{{JADT} 2022}"
        only_name = _item(itemType="conferencePaper", conferenceName="CMLF")
        assert _fields(only_name)[1]["booktitle"] == "{{CMLF}}"
        chap = _item(itemType="bookSection", bookTitle="Manuel", pages="11-26",
                     publisher="PUF", place="Paris", ISBN="978-2-13-000000-0")
        f = _fields(chap)[1]
        assert f["booktitle"] == "{Manuel}" and f["publisher"] == "{PUF}"
        assert f["address"] == "{Paris}"
        assert _fields(chap, dialect="biblatex")[1]["location"] == "{Paris}"


# === Fichier complet ===

class TestExport:
    def _torture(self):
        return [
            _item(title="Les archives de Foucault & le 100 % {HAL} _x_ #1 ~ ^ \\",
                  creators=[_author("de Certeau", "Michel"),
                            {"creatorType": "author", "name": "OMS"}],
                  abstractNote="Résumé\navec { accolade", tags=[{"tag": "a,b"}],
                  extra="Sources: HAL\nAnnote: {note} importante"),
            _item(itemType="thesis", title="Thèse } déséquilibrée",
                  thesisType="Mémoire de master"),
            _item(itemType="document", title="", creators=[]),
            _item(itemType="preprint", title="Même", url="1"),
            _item(itemType="preprint", title="Même", url="2"),
        ]

    @pytest.mark.parametrize("dialect", ["bibtex", "biblatex"])
    @pytest.mark.parametrize("ascii", [False, True])
    def test_every_export_parses(self, dialect, ascii):
        items = self._torture()
        stats = {}
        text = export_bibtex(items, dialect=dialect, ascii=ascii, stats=stats)
        entries = parse_bib(text)
        assert len(entries) == len(items)
        keys = [k for _t, k, _f in entries]
        assert len(set(keys)) == len(keys)
        for _t, _k, fields in entries:
            for value in fields.values():
                assert _balanced(value)
        assert stats["entries"] == len(items)
        assert stats["disambiguated_keys"] == 2
        assert "Sources" not in text

    def test_deterministic(self):
        items = self._torture()
        assert export_bibtex(items) == export_bibtex(items)

    def test_header_is_a_comment_outside_entries(self):
        text = export_bibtex([_item()])
        assert text.startswith("% Export BibTeX produit par moisson-shs")
        assert "@" not in text.split("\n\n")[0]
        assert "% Export" not in export_bibtex([_item()], header=False)

    def test_empty_corpus(self):
        assert parse_bib(export_bibtex([])) == []

    def test_unknown_dialect_refused(self):
        with pytest.raises(ValueError):
            export_bibtex([_item()], dialect="bibtext")
