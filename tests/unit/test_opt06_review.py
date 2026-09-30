"""
OPT-06, corrections de la revue : origine de chaque champ arbitré (une
fusion en deux temps choisit comme une fusion en une fois), contenants de
dépôt et champs hors type, relation partie/tout et titres courts, marqueurs
de tome ou de partie, versions distinctes d'un même dépôt HAL, liens d'URL
corroborés par le titre (idempotence), rapport d'une entrée vide, coût de la
passe floue avec des co-auteurs de nom fréquent.
"""

import copy
import itertools
import json
import random
import time

import pytest

from mappers.csl_exporter import zotero_to_csl
from utils.deduplication import (
    _Features, _fuzzy_edges, _match, _title_ratio_upper_bound, deduplicate,
    deduplicate_with_report, format_evidence, is_repository_venue,
    parse_evidence,
)
from utils.doi_resolver import best_title_ratio

T = "Les formules du discours politique contemporain"
HAL_VENUE = "HAL (Le Centre pour la Communication Scientifique Directe)"


def rec(source, title=T, authors=("Krieg",), date="2015", **kw):
    extra = kw.pop("extra", "")
    item = {
        "itemType": kw.pop("itemType", "journalArticle"),
        "title": title,
        "creators": [{"creatorType": "author", "lastName": a} for a in authors],
        "date": date,
        "extra": f"Sources: {source}" + (f"\n{extra}" if extra else ""),
    }
    item.update(kw)
    return item


def fields(record):
    """Champs Zotero d'une notice, hors ``extra`` (lignes agrégées)."""
    return json.dumps({k: v for k, v in record.items() if k != "extra"},
                      ensure_ascii=False, sort_keys=True)


def evidence_line(record):
    for line in (record.get("extra") or "").split("\n"):
        if line.startswith("Dedup evidence:"):
            return line
    return ""


# === C21 : origine de chaque champ arbitré ===

class TestTwoStageMerge:
    def test_two_stage_equals_one_stage(self):
        """Échoue avant correction : volume 13, issue 4, language en en deux
        temps (origine perdue hors des champs cités en tête de ligne)."""
        a = rec("HAL", DOI="10.1234/abc", volume="12", issue="3", language="fr")
        b = rec("Google Scholar (SerpAPI)", DOI="10.1234/abc")
        c = rec("OpenAlex", DOI="10.1234/abc", volume="13", issue="4",
                language="en")
        once = deduplicate([a, b, c])[0]
        first = deduplicate([a, b])[0]
        twice = deduplicate([first, c])[0]
        for field in ("volume", "issue", "language"):
            assert twice[field] == once[field]
        assert (once["volume"], once["issue"], once["language"]) == \
            ("12", "3", "fr")
        origins = parse_evidence(first)["origins"]
        assert origins["volume"] == ["hal"] and origins["language"] == ["hal"]

    def test_item_type_origin_traced(self):
        a = rec("HAL", DOI="10.1234/t", itemType="bookSection",
                bookTitle="Mélanges")
        b = rec("Google Scholar (SerpAPI)", DOI="10.1234/t", itemType="book")
        c = rec("OpenAlex", DOI="10.1234/t", itemType="book")
        once = deduplicate([a, b, c])[0]
        twice = deduplicate([deduplicate([a, b])[0], c])[0]
        assert once["itemType"] == twice["itemType"] == "bookSection"
        assert parse_evidence(deduplicate([a, b])[0])["origins"]["itemType"] \
            == ["hal"]

    def test_every_split_gives_the_same_fields(self):
        """Toute partition en deux sous-corpus déjà dédupliqués, puis refusion
        (corpus_merge, fetch_by_doi avec add_to) : mêmes champs et mêmes
        origines que la fusion en une fois."""
        doi = "10.1234/split"
        members = [
            rec("HAL", DOI=doi, volume="12", language="fr", pages="1-10",
                abstractNote="Résumé HAL", url="https://hal.science/hal-01234567",
                tags=[{"tag": "Discours"}, {"tag": "Presse"}]),
            rec("Google Scholar (SerpAPI)", DOI=doi, authors=("Krieg", "X"),
                publicationTitle="Mots", extra="Scholar truncation: auteurs"),
            rec("OpenAlex", DOI=doi, volume="13", issue="4", ISSN="0243-6450",
                publicationTitle="Mots. Les langages du politique",
                abstractNote="Résumé OpenAlex",
                tags=[{"tag": "Sociology"}, {"tag": "discours"}]),
            rec("Isidore", DOI=doi, language="fre", issue="5",
                publisher="ENS Éditions"),
            rec("Semantic Scholar", DOI=doi, itemType="document",
                abstractNote="Résumé S2", authors=("Krieg", "Petit", "Roy"),
                tags=[{"tag": "Philosophy"}, {"tag": "Presse"}]),
        ]
        once = deduplicate(copy.deepcopy(members))[0]
        ref_fields = fields(once)
        ref_origins = parse_evidence(once)["origins"]
        idx = range(len(members))
        for k in range(1, len(members)):
            for part in itertools.combinations(idx, k):
                left = [copy.deepcopy(members[i]) for i in part]
                right = [copy.deepcopy(members[i]) for i in idx if i not in part]
                merged = deduplicate(deduplicate(left) + deduplicate(right))
                assert len(merged) == 1
                assert fields(merged[0]) == ref_fields, part
                assert parse_evidence(merged[0])["origins"] == ref_origins, part

    def test_parked_value_is_a_candidate_again(self):
        """Un champ que le type retenu ne prévoit pas est reporté dans extra
        avec son origine ; si une refusion change le type, il redevient
        candidat, comme dans la fusion en une fois."""
        a = rec("HAL", DOI="10.1234/p", itemType="bookSection",
                bookTitle="Mélanges offerts")
        b = rec("OpenAlex", DOI="10.1234/p", publicationTitle="Revue R")
        c = rec("CrossRef", DOI="10.1234/p")
        first = deduplicate([a, b])[0]
        assert first["itemType"] == "bookSection"
        assert "publicationTitle" not in first
        assert "publicationTitle: Revue R" in first["extra"].split("\n")
        once = deduplicate([a, b, c])[0]
        twice = deduplicate([first, c])[0]
        assert once["itemType"] == twice["itemType"] == "journalArticle"
        assert once["publicationTitle"] == twice["publicationTitle"] == "Revue R"
        assert "publicationTitle: Revue R" not in twice["extra"].split("\n")


class TestEvidenceFormat:
    def test_origins_grouped_by_source_roundtrip(self):
        ev = {"count": 2, "keys": ["doi:10.1/x"], "fuzzy": [],
              "origins": {"title": ["hal"], "volume": ["hal"],
                          "abstractNote": ["crossref"], "itemType": ["hal"]},
              "ambiguous": ""}
        line = format_evidence(ev)
        assert "origines title+itemType+volume=HAL, abstractNote=CrossRef" in line
        parsed = parse_evidence({"extra": line})
        assert parsed["origins"] == ev["origins"]
        assert format_evidence(parsed) == line

    def test_legacy_per_field_form_still_read(self):
        line = ("Dedup evidence: 2 notices ; clés doi:10.1/x ; origines "
                "title=HAL, creators=HAL, abstractNote=CrossRef")
        parsed = parse_evidence({"extra": line})
        assert parsed["origins"] == {"title": ["hal"], "creators": ["hal"],
                                     "abstractNote": ["crossref"]}

    def test_every_arbitrated_field_has_an_origin(self):
        a = rec("HAL", DOI="10.1/o", volume="2", language="fr", place="Lyon",
                itemType="book", publisher="PUL")
        b = rec("OpenAlex", DOI="10.1/o", issue="3", itemType="book",
                numPages="200")
        merged = deduplicate([a, b])[0]
        origins = parse_evidence(merged)["origins"]
        for field, value in merged.items():
            if field in ("extra", "tags", "collections", "relations"):
                continue
            if value not in (None, "", [], {}):
                assert field in origins, field


# === Contenants de dépôt, champs hors type ===

VOX_HAL = {
    "itemType": "journalArticle",
    "title": "La plurisémie dans les syllepses et les antanaclases",
    "creators": [{"creatorType": "author", "firstName": "Alain",
                  "lastName": "Rabatel"}],
    "date": "2015", "publicationTitle": "Vox Romanica",
    "url": "https://hal.science/hal-01312636v1",
    "extra": "Sources: HAL\nHAL ID: hal-01312636",
}
VOX_OA = {
    "itemType": "journalArticle",
    "title": "La plurisémie dans les syllepses et les antanaclases",
    "creators": [{"creatorType": "author", "firstName": "Alain",
                  "lastName": "Rabatel"}],
    "date": "2015-01-01", "publicationTitle": HAL_VENUE,
    "url": "https://hal.science/hal-01312636",
    "extra": "Sources: OpenAlex\nOpenAlex ID: https://openalex.org/W2343658078",
}
CHAPTER_HAL = {
    "itemType": "bookSection",
    "title": "Du rôle du visuel en image dans la référenciation des perceptions",
    "creators": [{"creatorType": "author", "firstName": "Alain",
                  "lastName": "Rabatel"}],
    "date": "2012", "bookTitle": "Dire le non visuel",
    "url": "https://shs.hal.science/halshs-00999999v1",
    "extra": "Sources: HAL\nHAL ID: halshs-00999999",
}
CHAPTER_OA = {
    "itemType": "journalArticle",
    "title": "Du rôle du visuel en image dans la référenciation des perceptions",
    "creators": [{"creatorType": "author", "firstName": "Alain",
                  "lastName": "Rabatel"}],
    "date": "2012-01-01", "publicationTitle": HAL_VENUE,
    "url": "https://shs.hal.science/halshs-00999999",
    "extra": "Sources: OpenAlex\nOpenAlex ID: https://openalex.org/W1",
}


class TestVenues:
    def test_repository_never_replaces_the_real_journal(self):
        """Échoue avant correction : la revue devenait « HAL (…) »."""
        for pair in ([VOX_HAL, VOX_OA], [VOX_OA, VOX_HAL]):
            out = deduplicate(copy.deepcopy(pair))
            assert len(out) == 1
            assert out[0]["publicationTitle"] == "Vox Romanica"
            assert zotero_to_csl(out[0])["container-title"] == "Vox Romanica"

    def test_repository_venue_kept_when_it_is_the_only_one(self):
        alone = copy.deepcopy(VOX_OA)
        other = rec("Isidore", title=VOX_OA["title"], authors=("Rabatel",),
                    date="2015", url="https://hal.science/hal-01312636")
        merged = deduplicate([alone, other])[0]
        assert merged["publicationTitle"] == HAL_VENUE

    def test_chapter_gets_no_publication_title(self):
        """Échoue avant correction : le chapitre recevait publicationTitle,
        lu par l'export CSL avant bookTitle."""
        out = deduplicate(copy.deepcopy([CHAPTER_HAL, CHAPTER_OA]))
        assert len(out) == 1
        merged = out[0]
        assert merged["itemType"] == "bookSection"
        assert "publicationTitle" not in merged
        assert merged["bookTitle"] == "Dire le non visuel"
        assert f"publicationTitle: {HAL_VENUE}" in merged["extra"].split("\n")
        assert zotero_to_csl(merged)["container-title"] == "Dire le non visuel"

    @pytest.mark.parametrize("value", [
        HAL_VENUE, "Dialnet (Universidad de la Rioja)", "Cairn.info",
        "OpenEdition (OpenEdition)", "PhilPapers (PhilPapers Foundation)",
        "Oskar-Bordeaux (Universite de Bordeaux)", "arXiv (Cornell University)",
        "DOAJ (DOAJ: Directory of Open Access Journals)", "Peter Lang eBooks",
        "HilDok – Institutional Repository (Universität Hildesheim)",
    ])
    def test_repository_names(self, value):
        assert is_repository_venue(value)

    @pytest.mark.parametrize("value", [
        "Vox Romanica", "Langue française", "Mots", "Cahiers de praxématique",
        "Revue Sciences Humaines (Université des Frères Mentouri Constantine 1)",
        "Clinical Chemistry and Laboratory Medicine (CCLM)",
        "Français dans le monde. Recherches et applications", "",
    ])
    def test_real_venues(self, value):
        assert not is_repository_venue(value)


# === Relation partie/tout, titres courts ===

LONG = "Les discours rapportés dans la presse écrite contemporaine"


class TestPartWhole:
    def test_chapter_and_book_of_the_same_title(self):
        chapter = rec("OpenAlex", title=LONG, authors=("Nolke",), date="2005",
                      itemType="bookSection", bookTitle="Champs linguistiques",
                      pages="111-124",
                      extra=f"publicationTitle: {LONG}")
        book = rec("Semantic Scholar", title=LONG,
                   authors=("Nolke", "Rosier", "Bres"), date="2005",
                   itemType="document")
        assert len(deduplicate([chapter, book])) == 2
        assert len(deduplicate([chapter, book], fuzzy=False)) == 2

    def test_two_copies_of_that_chapter_still_merge(self):
        one = rec("OpenAlex", title=LONG, authors=("Nolke",), date="2005",
                  itemType="bookSection", bookTitle=LONG)
        two = rec("HAL", title=LONG, authors=("Nolke",), date="2005",
                  itemType="bookSection", bookTitle=LONG)
        assert len(deduplicate([one, two])) == 1

    def test_self_titled_container_and_paged_document_still_merge(self):
        """Cas réel Rabatel : deux dépôts HAL du même chapitre (l'un répète le
        titre du chapitre comme titre d'ouvrage), sa copie OpenAlex et sa
        notice Semantic Scholar paginée restent réunis ; la communication de
        2017 du même titre reste à part."""
        title = "Diversité des points de vue et mobilité empathique"
        h1 = rec("HAL", title=title, authors=("Rabatel",), date="2016",
                 itemType="bookSection",
                 bookTitle="L'énonciation aujourd'hui, un concept-clé des "
                           "sciences du langage", pages="137-152",
                 url="https://shs.hal.science/halshs-01323292v1",
                 extra="HAL ID: halshs-01323292")
        h2 = rec("HAL", title=title, authors=("Rabatel",), date="2016",
                 itemType="bookSection", bookTitle=title, pages="135-150",
                 url="https://shs.hal.science/halshs-01338310v1",
                 extra="HAL ID: halshs-01338310")
        oa = rec("OpenAlex", title=title, authors=("Rabatel",),
                 date="2016-01-01", publicationTitle=HAL_VENUE, pages="135-150",
                 url="https://shs.hal.science/halshs-01323292")
        s2 = rec("Semantic Scholar", title=title, authors=("Rabatel",),
                 date="2016", itemType="document",
                 extra="S2 corpus id: 171696607\npages: 135-150")
        conf = rec("HAL", title=title, authors=("Rabatel",), date="2017-05-18",
                   itemType="conferencePaper",
                   url="https://shs.hal.science/halshs-02132630v1",
                   extra="HAL ID: halshs-02132630")
        out = deduplicate([h1, h2, oa, s2, conf])
        counts = sorted((parse_evidence(r) or {"count": 1})["count"] for r in out)
        assert counts == [1, 4]
        alone = [r for r in out if not parse_evidence(r)]
        assert alone[0]["itemType"] == "conferencePaper"

    def test_short_title_requires_the_same_first_author(self):
        """Échoue avant correction (cas réel Nølke) : titre court identique,
        premier auteur de l'une parmi les auteurs de l'autre."""
        a = rec("OpenAlex", title="Dialogisme et polyphonie", authors=("Nølke",),
                date="2005", itemType="document")
        b = rec("Semantic Scholar", title="Dialogisme et polyphonie",
                authors=("Rosier", "Bres", "Nølke"), date="2005",
                itemType="document")
        assert len(deduplicate([a, b])) == 2
        c = rec("HAL", title="Dialogisme et polyphonie", authors=("Nolke",),
                date="2005", itemType="document")
        assert len(deduplicate([a, c])) == 1


# === Marqueurs de tome ou de partie ===

BASE = "Histoire de la presse française contemporaine"


def book(title, source, author="Dupont"):
    return rec(source, title=title, authors=(author,), date="2019",
               itemType="book")


class TestPartMarkers:
    @pytest.mark.parametrize("other", [
        BASE + ". Tome 2", BASE + ", volume II", BASE + " : deuxième partie",
        BASE + " : suite et fin", BASE + " II", BASE + ", 2e partie",
        BASE + ". Livre 3",
    ])
    def test_volume_or_part_never_merged(self, other):
        """Échoue avant correction (sauf « II ») : appariement par préfixe."""
        assert len(deduplicate([book(BASE, "HAL"), book(other, "OpenAlex")])) == 2

    def test_long_titles_with_a_volume_marker(self):
        """Titres assez longs pour dépasser 0,93 sans préfixe."""
        long_ = ("Histoire générale de la presse française contemporaine et "
                 "de ses publics populaires en province")
        assert len(deduplicate([book(long_, "HAL"),
                                book(long_ + ". Tome 2", "OpenAlex")])) == 2

    @pytest.mark.parametrize("a, b", [
        ("Chapitre 2. Quelle place pour les émotions dans la linguistique du "
         "XXe siècle ? Remarques et aperçus", "Quelle place pour les émotions "
         "dans la linguistique du XXe siècle ? Remarques et aperçus"),
        ("Questionner la limite. Sur une lettre de Robert Antelme 1",
         "Questionner la limite. Sur une lettre de Robert Antelme"),
        (BASE + " : le volume des tirages", BASE),
    ])
    def test_labels_and_footnote_calls_still_merge(self, a, b):
        x = rec("OpenAlex", title=a, authors=("Sarfati",), date="2007")
        y = rec("Isidore", title=b, authors=("Sarfati",), date="2007")
        assert len(deduplicate([x, y])) == 1


# === Versions distinctes d'un même dépôt HAL ===

class TestHalVersions:
    def _moirand(self):
        chapter = rec("HAL", title="Des exigences théoriques de la comparaison "
                      "aux contingences d'un corpus particulier",
                      authors=("Camargo", "Moirand"), date="2021",
                      itemType="bookSection", bookTitle="Analyse du discours",
                      url="https://univ-sorbonne-nouvelle.hal.science/hal-01982659v2",
                      extra="HAL ID: hal-01982659")
        preprint = rec("OpenAlex", title="Des exigences théoriques de la "
                       "comparaison aux contingences de la pratique : mots "
                       "associés et mots construits", authors=("Moirand",),
                       date="2019-01-15", itemType="preprint",
                       url="https://hal-univ-paris3.archives-ouvertes.fr/hal-01982659",
                       extra="OpenAlex ID: https://openalex.org/W4288632957")
        return chapter, preprint

    def test_distant_years_stay_apart_with_a_note(self):
        """Échoue avant correction : la prépublication de 2019 était absorbée
        par le chapitre de 2021 (identifiant HAL sans version)."""
        out, report = deduplicate_with_report(list(self._moirand()))
        assert len(out) == 2
        assert report["version_splits"] == 2
        for record in out:
            line = evidence_line(record)
            assert "version distincte" in line and "hal:hal-01982659" in line

    def test_republication_keeps_its_own_doi(self):
        chapter = rec("HAL", title="Énonciateurs premiers, seconds, points de "
                      "vue, modalité et intentionnalité", authors=("Rabatel",),
                      date="2021", itemType="bookSection",
                      url="https://shs.hal.science/halshs-03957795v1",
                      extra="HAL ID: halshs-03957795")
        article = rec("OpenAlex", title="Énonciateurs Premiers, Seconds, Points "
                      "de Vue, Modalité et Intentionnalité", authors=("Rabatel",),
                      date="2019-09-01", DOI="10.22034/rllfut.2019.9293",
                      url="https://shs.hal.science/halshs-03957795")
        out = deduplicate([chapter, article])
        assert len(out) == 2
        by_type = {r["itemType"]: r for r in out}
        assert not by_type["bookSection"].get("DOI")

    def test_close_years_and_translated_title_still_merge(self):
        """Même dépôt, titre français (HAL) et anglais (OpenAlex, qui type
        le dépôt en prépublication) : fusion conservée."""
        hal = rec("HAL", title="L'école et l'avenir de la culture digitale",
                  authors=("Lakel",), date="2017",
                  url="https://hal.science/hal-02459672v1",
                  extra="HAL ID: hal-02459672")
        oa = rec("OpenAlex", title="School and the future of the digital culture",
                 authors=("Lakel",), date="2017", itemType="preprint",
                 url="https://hal.science/hal-02459672")
        assert len(deduplicate([hal, oa])) == 1

    def test_idempotent_with_notes(self):
        once = deduplicate(list(self._moirand()))
        twice = deduplicate(copy.deepcopy(once))
        assert json.dumps(twice, sort_keys=True) == json.dumps(once, sort_keys=True)


# === Liens d'URL ===

class TestUrlLinks:
    URL = "https://journals.openedition.org/lidil"
    TITLES = [("Alpha", "Les marqueurs discursifs du français parlé contemporain"),
              ("Beta", "Acquisition du lexique verbal chez l'enfant bilingue"),
              ("Gamma", "Corpus oraux et annotation prosodique automatisée")]

    def test_rededuplication_is_idempotent(self):
        """Échoue avant correction : 3 notices, puis 1 à la deuxième passe
        (l'URL n'était plus « générique » une fois les copies réunies)."""
        items = []
        for n, (author, title) in enumerate(self.TITLES):
            for source in ("OpenEdition", "Isidore", "OpenAlex"):
                items.append(rec(source, title=title, authors=(author,),
                                 date="2019", url=self.URL,
                                 extra=f"OAI Identifier: oai:revues.org:lidil/{100 + n}"))
        once = deduplicate(items)
        assert len(once) == 3
        twice = deduplicate(copy.deepcopy(once))
        assert json.dumps(twice, sort_keys=True) == json.dumps(once, sort_keys=True)

    def test_copies_of_one_work_do_not_make_an_url_generic(self):
        """Seuil compté en titres distincts, pas en notices."""
        copies = [rec(f"Source{k}", title=self.TITLES[0][1], authors=(),
                      url="https://revue.example.org/article/42")
                  for k in range(10)]
        assert len(deduplicate(copies)) == 1


def test_empty_input_report_has_groups():
    """Échoue avant correction : clé « groups » absente."""
    out, report = deduplicate_with_report([], with_groups=True)
    assert out == [] and report["groups"] == []


# === Coût de la passe floue ===

_WORDS = ("discours analyse corpus presse politique énonciation polyphonie "
          "dialogisme argumentation médias numérique récit point vue "
          "modalité lexique syntaxe pragmatique interaction genre texte "
          "sémantique référence citation mémoire controverse").split()
_FREQUENT = ["Wang", "Li", "Zhang", "Liu", "Chen", "Yang", "Huang", "Zhao",
             "Wu", "Zhou"]


def _synthetic(n, seed=11):
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        title = " ".join(rng.choice(_WORDS) for _ in range(rng.randint(6, 12)))
        authors = [rng.choice(_FREQUENT) if rng.random() < 0.12
                   else f"Nom{rng.randint(0, 40000)}"
                   for _ in range(rng.randint(3, 6))]
        out.append(rec("OpenAlex", title=title, authors=authors,
                       date=str(rng.randint(2023, 2025))))
    return out


def test_candidate_pairs_are_those_the_corroboration_can_accept():
    """Les paires candidates (premier auteur de l'une parmi les auteurs de
    l'autre) donnent exactement les appariements d'une comparaison de toutes
    les paires."""
    rng = random.Random(3)
    base = ["Les marqueurs discursifs du français parlé contemporain",
            "Acquisition du lexique verbal chez l'enfant bilingue en contexte",
            "Corpus oraux et annotation prosodique automatisée des dialogues"]
    items = []
    for k in range(80):
        title = rng.choice(base) + rng.choice(["", " : une étude", " !", "s"])
        names = rng.sample(["Adam", "Rabatel", "Wang", "Li", "Bres"],
                           rng.randint(1, 3))
        items.append(rec("HAL", title=title, authors=names,
                         date=str(rng.randint(2014, 2017)),
                         itemType=rng.choice(["journalArticle", "document"])))
    feats = [_Features(r) for r in items]
    fast = _fuzzy_edges(feats)
    brute = {}
    for i in range(len(feats)):
        for j in range(i + 1, len(feats)):
            m = _match(feats[i], feats[j])
            if m:
                brute[(i, j)] = m
    assert fast == brute
    assert brute  # le corpus produit bien des appariements


def test_title_upper_bound_dominates_the_ratio():
    rng = random.Random(5)
    titles = [" ".join(rng.choice(_WORDS) for _ in range(rng.randint(3, 12)))
              for _ in range(150)]
    titles += [t + " : une étude" for t in titles[:30]]
    titles += [t[:-3] for t in titles[:30]]
    feats = [_Features(rec("HAL", title=t)) for t in titles]
    for _ in range(3000):
        a, b = rng.sample(range(len(feats)), 2)
        ratio, _ = best_title_ratio(titles[a], titles[b])
        assert _title_ratio_upper_bound(feats[a], feats[b]) + 1e-12 >= ratio


def test_frequent_coauthor_names_stay_fast():
    """Constat de la revue : 5 000 notices dont 12 % des places d'auteur
    tirées parmi 10 noms fréquents prenaient 8,6 à 16 s (comparaisons par
    bloc de chaque co-auteur). Mesuré à 0,7 s après correction."""
    items = _synthetic(5000)
    t0 = time.perf_counter()
    deduplicate(items)
    assert time.perf_counter() - t0 < 6
