"""
OPT-06 : déduplication par union-find sur les clés fortes, garde-fou DOI,
passe floue conservatrice, fusion indépendante de l'ordre (C18, C20, C21,
C23).
"""

import json
import random

import pytest

from utils.deduplication import (
    FUZZY_ENV, deduplicate, deduplicate_with_report, format_evidence,
    is_generic_title, merge_items, parse_evidence,
)


def art(title, authors=("Rabatel",), date="2015", source="HAL", **kw):
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


def dump(items):
    return json.dumps(items, ensure_ascii=False, sort_keys=True)


def evidence_line(item):
    for line in item["extra"].split("\n"):
        if line.startswith("Dedup evidence:"):
            return line
    return ""


T1 = "Les points de vue en confrontation dans les récits de presse écrite"
T2 = "L'effacement énonciatif dans les discours rapportés et ses effets"


# === C18 : fin de la partition stricte ===

class TestStrongKeysUnion:
    def test_doi_item_and_no_doi_item_share_hal_id(self):
        """Échoue sur l'ancienne partition : un item avec DOI n'était comparé
        que par DOI."""
        with_doi = art(T1, DOI="10.4000/rev.1", url="https://doi.org/10.4000/rev.1",
                       source="OpenAlex", extra="HAL ID: halshs-00367519")
        no_doi = art(T1, url="https://shs.hal.science/halshs-00367519v1",
                     source="Isidore")
        out = deduplicate([with_doi, no_doi])
        assert len(out) == 1
        assert out[0]["DOI"] == "10.4000/rev.1"
        assert "hal:halshs-00367519" in evidence_line(out[0])

    def test_doi_item_and_no_doi_item_share_canonical_url(self):
        a = art(T1, DOI="10.1/x", url="https://www.revue.org/article/42/",
                source="CrossRef")
        b = art(T1.upper() + " !", url="http://revue.org/article/42",
                source="Isidore")
        assert len(deduplicate([a, b])) == 1

    def test_url_link_requires_concordant_titles(self):
        """Revue OPT-06 : une URL de page de revue non reconnue comme
        générique ne réunit pas deux articles distincts."""
        a = art(T1, DOI="10.1/x", url="https://www.revue.org/article/42/",
                source="CrossRef")
        b = art("Titre sans rapport apparent", url="http://revue.org/article/42",
                source="Isidore")
        assert len(deduplicate([a, b])) == 2

    def test_persee_url_joins_the_doi(self):
        a = art(T2, DOI="10.3406/lgge.2004.960", source="OpenAlex")
        b = art(T2, url="https://www.persee.fr/doc/lgge_0458-726x_2004_num_38_156_960",
                source="Isidore")
        assert len(deduplicate([a, b])) == 1

    def test_generic_url_does_not_join(self):
        a = art(T1, url="https://www.revue.fr/", source="HAL")
        b = art(T2, url="https://revue.fr", source="Isidore")
        assert len(deduplicate([a, b])) == 2


# === Garde-fou DOI ===

class TestDoiGuard:
    def test_two_dois_sharing_a_url_never_merge(self):
        a = art(T1, DOI="10.1/a", url="https://ex.org/rec/1", source="CrossRef")
        b = art(T1, DOI="10.1/b", url="https://ex.org/rec/1", source="OpenAlex")
        out, report = deduplicate_with_report([a, b])
        assert len(out) == 2
        assert report["doi_split_components"] == 1

    def test_no_doi_item_linked_to_two_dois_stays_apart(self):
        """Règle : un sous-composant sans DOI relié à deux DOI distincts reste
        à part (statut ambigu tracé), jamais rattaché par transitivité."""
        a = art(T1, DOI="10.1/a", source="CrossRef", extra="HAL ID: hal-01111111")
        b = art(T1, url="https://ex.org/rec/9", source="Isidore",
                extra="HAL ID: hal-01111111")
        c = art(T1, DOI="10.1/b", url="https://ex.org/rec/9", source="OpenAlex")
        out, report = deduplicate_with_report([a, b, c])
        assert len(out) == 3
        ambiguous = [r for r in out if not r.get("DOI")]
        assert len(ambiguous) == 1
        line = evidence_line(ambiguous[0])
        assert "ambigu" in line and "doi:10.1/a" in line and "doi:10.1/b" in line
        assert report["ambiguous"] == 1

    def test_no_doi_chain_attached_to_its_single_doi(self):
        a = art(T1, DOI="10.1/a", url="https://ex.org/u1", source="CrossRef")
        b = art(T1, url="https://ex.org/u1", source="Isidore",
                extra="HAL ID: hal-02222222")
        c = art(T1, source="HAL", extra="HAL ID: hal-02222222")
        out = deduplicate([a, b, c])
        assert len(out) == 1
        assert parse_evidence(out[0])["count"] == 3

    def test_two_doi_classes_with_a_shared_bridge(self):
        a = art(T1, DOI="10.1/a", url="https://ex.org/u1", source="CrossRef")
        b = art(T1, url="https://ex.org/u1", source="Isidore",
                extra="OpenAlex ID: https://openalex.org/W5")
        c = art(T1, DOI="10.1/b", url="https://ex.org/u2", source="CrossRef")
        d = art(T1, url="https://ex.org/u2", source="HAL",
                extra="OpenAlex ID: https://openalex.org/W5")
        out = deduplicate([a, b, c, d])
        # {b, d} relié à deux DOI : reste à part, mais b et d fusionnent.
        assert len(out) == 3
        assert sorted(parse_evidence(r)["count"] if parse_evidence(r) else 1
                      for r in out) == [1, 1, 2]


# === Passe floue ===

class TestFuzzyPass:
    def test_subtitle_prefix_same_year(self):
        a = art("Your brain on ChatGPT: accumulation of cognitive debt when "
                "using an AI assistant", authors=("Kosmyna",), date="2025",
                source="CrossRef", DOI="10.1/brain")
        b = art("Your brain on ChatGPT: accumulation of cognitive debt",
                authors=("Kosmyna",), date="2025", source="Google Scholar (SerpAPI)")
        out = deduplicate([a, b])
        assert len(out) == 1
        assert "préfixe" in evidence_line(out[0])

    def test_prefix_requires_same_year(self):
        a = art("Your brain on ChatGPT: accumulation of cognitive debt when "
                "using an AI assistant", authors=("Kosmyna",), date="2025")
        b = art("Your brain on ChatGPT: accumulation of cognitive debt",
                authors=("Kosmyna",), date="2024", source="OpenAlex")
        assert len(deduplicate([a, b])) == 2

    def test_year_tolerance_is_one(self):
        base = art(T1, date="2015")
        assert len(deduplicate([base, art(T1, date="2016", source="OpenAlex")])) == 1
        assert len(deduplicate([base, art(T1, date="2017", source="OpenAlex")])) == 2

    def test_year_and_author_are_both_required(self):
        base = art(T1, date="2015")
        assert len(deduplicate([base, art(T1, date="", source="OpenAlex")])) == 2
        assert len(deduplicate([base, art(T1, authors=(), source="OpenAlex")])) == 2
        assert len(deduplicate([base, art(T1, authors=("Martin",),
                                          source="OpenAlex")])) == 2

    def test_first_author_found_among_the_other_authors(self):
        a = art(T1, authors=("Moody", "Alagic", "Apon"), source="Google Scholar")
        b = art(T1, authors=("Alagic", "Alperin-Sheriff"), source="OpenAlex")
        assert len(deduplicate([a, b])) == 1

    def test_particles_and_accents_in_author_names(self):
        a = art(T1, authors=("de Chanay",), source="OpenAlex", DOI="10.1/c")
        b = art(T1, authors=("Chanay",), source="HAL")
        assert len(deduplicate([a, b])) == 1
        c = art(T1, authors=("Nølke",), source="OpenAlex")
        d = art(T1, authors=("Nolke",), source="Semantic Scholar")
        assert len(deduplicate([c, d])) == 1

    def test_threshold_093(self):
        a = art("Analyse énonciative de la connivence représentée dans les récits",
                date="2016")
        b = art("Analyse pragma-énonciative de la connivence représentée dans les récits",
                date="2016", source="OpenAlex")
        c = art("Analyse sémiotique de la complicité représentée dans les contes",
                date="2016", source="OpenAlex")
        assert len(deduplicate([a, b])) == 1        # 0,95
        assert len(deduplicate([a, c])) == 2        # < 0,93

    @pytest.mark.parametrize("title", [
        "Introduction", "Éditorial", "Compte rendu", "Avant-propos",
        "Conclusion", "Préface", "Introduction générale", "Notes de lecture",
    ])
    def test_generic_titles_never_matched(self, title):
        """Échoue sur l'ancienne empreinte exacte (titre + auteur + année)."""
        assert is_generic_title(title)
        a = art(title, date="2018")
        b = art(title, date="2018", source="Isidore")
        assert len(deduplicate([a, b])) == 2

    def test_short_title_exact_same_year_only(self):
        assert len(deduplicate([art("La conversation", date="1990"),
                                art("La conversation", date="1990",
                                    source="OpenAlex")])) == 1
        assert len(deduplicate([art("La conversation", date="1990"),
                                art("La conversation", date="1991",
                                    source="OpenAlex")])) == 2
        # Titre court : jamais d'appariement approché.
        assert len(deduplicate([art("La conversation orale", date="1990"),
                                art("La conversation morale", date="1990",
                                    source="OpenAlex")])) == 2

    @pytest.mark.parametrize("ta,tb", [
        ("book", "bookSection"), ("thesis", "book"), ("preprint", "journalArticle"),
        ("conferencePaper", "journalArticle"), ("dataset", "journalArticle"),
    ])
    def test_incompatible_types(self, ta, tb):
        a = art(T1, itemType=ta)
        b = art(T1, itemType=tb, source="OpenAlex")
        assert len(deduplicate([a, b])) == 2

    def test_generic_type_is_compatible(self):
        a = art(T1, itemType="book")
        b = art(T1, itemType="document", source="Semantic Scholar")
        assert len(deduplicate([a, b])) == 1

    def test_review_versus_article(self):
        a = art("Compte rendu de : " + T1, source="Isidore")
        b = art(T1, source="HAL")
        assert len(deduplicate([a, b])) == 2

    def test_paratext_difference(self):
        a = art(T1, itemType="book")
        b = art(T1 + " : introduction", itemType="document", source="OpenAlex")
        assert len(deduplicate([a, b])) == 2
        c = art("Erratum: " + T1, source="CrossRef")
        assert len(deduplicate([art(T1), c])) == 2

    def test_discordant_numbers(self):
        a = art("Histoire des théories de l'énonciation en France tome 1",
                itemType="book")
        b = art("Histoire des théories de l'énonciation en France tome 2",
                itemType="book", source="OpenAlex")
        assert len(deduplicate([a, b])) == 2

    def test_distinct_hal_deposits(self):
        # Titre identique, même année : dépôt HAL en double, fusionné.
        a = art(T1, extra="HAL ID: tel-03209752", itemType="thesis")
        b = art(T1, extra="HAL ID: tel-03209461", itemType="thesis")
        assert len(deduplicate([a, b])) == 1
        # Titres seulement proches : deux dépôts distincts.
        c = art(T1.replace("écrite", "écrite régionale"), extra="HAL ID: tel-03209461",
                itemType="thesis")
        assert len(deduplicate([a, c])) == 2

    def test_generic_type_cannot_bridge_book_and_chapter(self):
        """Contraintes dures vérifiées paire à paire dans un regroupement."""
        book = art("Confidence : dévoilement de soi dans l'interaction verbale",
                   itemType="book", extra="HAL ID: halshs-00357683")
        chapter = art("Confidence : dévoilement de soi dans l'interaction verbale",
                      itemType="bookSection", source="HAL",
                      extra="HAL ID: halshs-00658833")
        bridge = art("Confidence : dévoilement de soi dans l'interaction verbale",
                     itemType="document", source="Semantic Scholar")
        out = deduplicate([book, chapter, bridge])
        types = sorted(r["itemType"] for r in out)
        assert len(out) == 2 and "bookSection" in types

    def test_fuzzy_can_be_disabled(self, monkeypatch):
        a = art("Analyse énonciative de la connivence représentée dans les récits")
        b = art("Analyse pragma-énonciative de la connivence représentée dans les récits",
                source="OpenAlex")
        exact_a = art(T1)
        exact_b = art(T1, source="OpenAlex")
        assert len(deduplicate([a, b], fuzzy=False)) == 2
        # L'égalité exacte (empreinte historique) reste active.
        assert len(deduplicate([exact_a, exact_b], fuzzy=False)) == 1
        monkeypatch.setenv(FUZZY_ENV, "0")
        assert len(deduplicate([a, b])) == 2
        assert deduplicate_with_report([a, b])[1]["fuzzy"] is False
        # Le paramètre explicite l'emporte sur la variable.
        assert len(deduplicate([a, b], fuzzy=True)) == 1


# === C20, C21 : fusion indépendante de l'ordre et origine des valeurs ===

class TestMergeRules:
    def test_most_complete_author_list_whatever_the_order(self):
        """Échoue sur l'ancienne fusion : le premier item imposait ses auteurs."""
        one = art(T1, authors=("Rabatel",), source="Isidore", DOI="10.1/z")
        two = art(T1, authors=("Rabatel", "Rosier"), source="OpenAlex", DOI="10.1/z")
        for order in ([one, two], [two, one]):
            merged = deduplicate(order)[0]
            assert [c["lastName"] for c in merged["creators"]] == ["Rabatel", "Rosier"]

    def test_truncated_scholar_list_is_set_aside(self):
        scholar = art(T1, authors=("Rabatel", "Rosier", "Petit"),
                      source="Google Scholar (SerpAPI)", DOI="10.1/t",
                      extra="Scholar truncation: auteurs et/ou revue tronqués")
        crossref = art(T1, authors=("Rabatel", "Rosier"), source="CrossRef",
                       DOI="10.1/t")
        merged = deduplicate([scholar, crossref])[0]
        assert len(merged["creators"]) == 2
        # L'avertissement ne qualifie plus aucun champ retenu : retiré.
        assert "Scholar truncation" not in merged["extra"]

    def test_value_credited_to_its_real_source(self):
        """C21 : une valeur venue de HAL, portée par une notice déjà fusionnée
        (Sources: CrossRef, HAL), n'est pas créditée à CrossRef."""
        crossref = art(T1, source="CrossRef", DOI="10.1/c21", abstractNote="")
        hal = art(T1, source="HAL", DOI="10.1/c21", abstractNote="Résumé HAL")
        first = deduplicate([crossref, hal])[0]
        assert first["abstractNote"] == "Résumé HAL"
        assert "abstractNote=HAL" in evidence_line(first)
        openalex = art(T1, source="OpenAlex", DOI="10.1/c21",
                       abstractNote="Résumé OpenAlex")
        second = deduplicate([first, openalex])[0]
        # Priorité du résumé : CrossRef > OpenAlex > … > HAL.
        assert second["abstractNote"] == "Résumé OpenAlex"
        assert parse_evidence(second)["count"] == 3

    def test_legacy_merged_item_is_never_credited_to_a_listed_source(self):
        legacy = art(T1, source="CrossRef, HAL", DOI="10.1/leg",
                     abstractNote="Résumé d'origine inconnue")
        openalex = art(T1, source="OpenAlex", DOI="10.1/leg",
                       abstractNote="Résumé OpenAlex")
        assert deduplicate([legacy, openalex])[0]["abstractNote"] == "Résumé OpenAlex"

    def test_scholar_snippet_line_follows_the_abstract(self):
        snippet = "Scholar snippet: le champ résumé contient un extrait Google Scholar"
        scholar = art(T1, source="Google Scholar (SerpAPI)", DOI="10.1/s",
                      abstractNote="extrait…", extra=snippet)
        crossref = art(T1, source="CrossRef", DOI="10.1/s",
                       abstractNote="Vrai résumé de l'éditeur")
        merged = deduplicate([scholar, crossref])[0]
        assert merged["abstractNote"] == "Vrai résumé de l'éditeur"
        assert "Scholar snippet" not in merged["extra"]
        alone = art(T1, source="HAL", DOI="10.1/s")
        kept = deduplicate([scholar, alone])[0]
        assert kept["abstractNote"] == "extrait…" and "Scholar snippet" in kept["extra"]

    def test_tags_identifiers_and_urls_aggregated(self):
        a = art(T1, source="HAL", DOI="10.1/agg", url="https://hal.science/hal-01234567",
                tags=[{"tag": "Discours"}], extra="HAL ID: hal-01234567")
        b = art(T1, source="OpenAlex", DOI="10.1/agg", url="https://doi.org/10.1/agg",
                tags=[{"tag": "discours"}, {"tag": "Médias"}],
                extra="OpenAlex ID: https://openalex.org/W9")
        merged = deduplicate([a, b])[0]
        assert [t["tag"] for t in merged["tags"]] == ["Discours", "Médias"]
        assert "HAL ID: hal-01234567" in merged["extra"]
        assert "OpenAlex ID: https://openalex.org/W9" in merged["extra"]
        other = "https://doi.org/10.1/agg" if merged["url"].startswith("https://hal") \
            else "https://hal.science/hal-01234567"
        assert f"Alternative URLs: {other}" in merged["extra"]

    def test_sources_line_normalized_labels(self):
        a = art(T1, source="CrossRef", DOI="10.1/src")
        b = art(T1, source="Crossref", DOI="10.1/src")
        c = art(T1, source="Google Scholar (SerpAPI)", DOI="10.1/src")
        line = deduplicate([a, b, c])[0]["extra"].split("\n")[0]
        assert line == "Sources: CrossRef, Google Scholar (SerpAPI)"

    def test_no_arbitrary_zotero_field(self):
        a = art(T1, source="HAL", DOI="10.1/f", url="https://hal.science/hal-01234567")
        b = art(T1, source="OpenAlex", DOI="10.1/f", publicationTitle="Revue")
        merged = deduplicate([a, b])[0]
        assert set(merged) <= set(a) | set(b)

    def test_evidence_roundtrip(self):
        ev = {"count": 3, "keys": ["doi:10.1/x", "hal:hal-01234567"],
              "fuzzy": ["titre 0.97 année exacte auteur rabatel"],
              "origins": {"title": ["hal"], "abstractNote": ["crossref"]},
              "ambiguous": ""}
        line = format_evidence(ev)
        assert line.startswith("Dedup evidence: 3 notices ; clés doi:10.1/x")
        parsed = parse_evidence({"extra": line})
        assert parsed["count"] == 3 and parsed["keys"] == ev["keys"]
        assert parsed["fuzzy"] == ev["fuzzy"]
        assert parsed["origins"] == {"title": ["hal"], "abstractNote": ["crossref"]}
        assert format_evidence(parsed) == line

    def test_single_item_returned_unchanged(self):
        item = art(T1)
        assert merge_items([item]) is item


# === Déterminisme ===

def _mixed_corpus():
    items = []
    for k in range(6):
        t = f"{T1} numéro {k}" if k % 2 else f"{T2} volume {k}"
        items.append(art(t, date=str(2010 + k), source="HAL",
                         url=f"https://hal.science/hal-0{k}000000v1"))
        items.append(art(t, date=str(2010 + k), source="Isidore",
                         url=f"https://hal.science/hal-0{k}000000",
                         abstractNote=f"Résumé {k}"))
        items.append(art(t, date=str(2010 + k), source="OpenAlex",
                         authors=("Rabatel", "Rosier"), DOI=f"10.9/{k}",
                         extra=f"HAL ID: hal-0{k}000000"))
        items.append(art(t + " : une approche énonciative", date=str(2010 + k),
                         source="Semantic Scholar", itemType="document"))
    items.append(art("Introduction", source="HAL"))
    items.append(art("Introduction", source="Isidore"))
    items.append(art(T1, source="CrossRef", DOI="10.9/zz", date="1990"))
    return items


class TestDeterminism:
    def test_permutations_give_identical_output(self):
        """Échoue sur l'ancienne fusion (premier item comme base)."""
        items = _mixed_corpus()
        ref = dump(deduplicate([dict(x) for x in items]))
        for seed in range(10):
            shuffled = [dict(x) for x in items]
            random.Random(seed).shuffle(shuffled)
            assert dump(deduplicate(shuffled)) == ref, f"graine {seed}"

    def test_idempotent(self):
        once = deduplicate(_mixed_corpus())
        assert dump(deduplicate([dict(x) for x in once])) == dump(once)

    def test_output_order_is_author_date(self):
        out = deduplicate([art(T2, authors=("Zola",), date="2001"),
                           art(T1, authors=("Adam",), date="2005"),
                           art(T1, authors=(), date="1999"),
                           art(T2, authors=("Adam",), date="2003")])
        keys = [((r["creators"] or [{}])[0].get("lastName", ""), r["date"])
                for r in out]
        assert keys == [("Adam", "2003"), ("Adam", "2005"), ("Zola", "2001"),
                        ("", "1999")]

    def test_groups_report_maps_outputs_to_inputs(self):
        items = [art(T1, DOI="10.1/g"), art(T2), art(T1, DOI="10.1/G",
                                                      source="OpenAlex")]
        out, report = deduplicate_with_report(items, with_groups=True)
        assert len(out) == 2
        assert sorted(map(sorted, report["groups"])) == [[0, 2], [1]]


def test_largest_author_block_is_fast():
    """Pire cas des corpus d'auteur : un seul bloc d'auteur (plus gros corpus
    réel, 610 notices, mesuré à 0,7 s)."""
    import time
    items = []
    for k in range(600):
        items.append(art(f"Étude numéro {k} sur la polyphonie énonciative et "
                         f"le dialogisme dans le corpus {k % 37}",
                         date=str(1990 + k % 30), source="OpenAlex" if k % 2 else "HAL"))
    t0 = time.perf_counter()
    deduplicate(items)
    assert time.perf_counter() - t0 < 10
