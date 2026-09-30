# -*- coding: utf-8 -*-
"""
OPT-10 : jeu négatif de la résolution de DOI — aucun DOI erroné.

Chaque cas décrit une notice sans DOI et les candidats que CrossRef pourrait
renvoyer. Pour tous les cas négatifs, la résolution ne doit écrire **aucun**
DOI (statut rejeté, ambigu ou sans candidat) : un DOI manquant se rattrape à
la main, un DOI faux entre silencieusement dans Zotero.

Les sept premiers cas reprennent les cas négatifs de
``tests/unit/test_doi_resolver.py`` (au niveau de la boucle de résolution) ;
les suivants sont nouveaux (OPT-10, puis revue OPT-10 : règles
``doi-match/3``). Des contrôles positifs vérifient que la règle n'est pas
devenue muette, y compris quand un candidat voisin (erratum, autre partie,
prépublication) accompagne le bon : ce sont les faux négatifs que la règle
« ambigu » de la v2 produisait. Les abstentions voulues de la v3 (faux
négatifs acceptés au nom de la précision) sont consignées à part.

Revue OPT-10 : trois cas que la v2 comptait comme négatifs (« article et
erratum », « prépublication et version finale », « tomes numérotés », tous
renvoyés ensemble) ont un DOI juste, que la baseline écrivait pour le
premier ; ils sont désormais des contrôles positifs avec distracteur. Les deux
limites que la v2 consignait en ``xfail`` (erratum seul, mauvais tome seul)
sont corrigées et rejoignent le jeu négatif.
"""

from unittest.mock import patch

import pytest

import utils.doi_resolver as dr


def _item(title, year="", last="", item_type="journalArticle"):
    return {"itemType": item_type, "title": title, "date": year,
            "creators": ([{"creatorType": "author", "firstName": "X",
                           "lastName": last}] if last else []),
            "extra": "Sources: Google Scholar (SerpAPI)",
            "tags": [], "collections": [], "relations": {}}


def _cand(title, year=None, families=(), doi="10.1000/test", ctype=None):
    c = {"title": [title], "DOI": doi,
         "author": [{"family": f} for f in families]}
    if year:
        c["issued"] = {"date-parts": [[year]]}
    if ctype:
        c["type"] = ctype
    return c


def _resolve_one(item, candidates):
    def fake(url, params=None, **kwargs):
        return {"message": {"items": candidates}}

    with patch.object(dr, "get_json", side_effect=fake), \
            patch("utils.http.time.sleep", lambda s: None):
        out, rep = dr.resolve_dois([item])
    return out[0], rep["entries"][0]


T = "Digital sovereignty in Europe"

NEGATIVE_CASES = [
    # --- Repris de test_doi_resolver.py ---
    ("annee_eloignee",
     _item(T, "2023", "Dupont"), [_cand(T, 2011, ["Dupont"], "10.1/old")]),
    ("auteur_absent",
     _item(T, "2023", "Dupont"),
     [_cand(T, 2023, ["Martin", "Durand"], "10.1/autre")]),
    ("titre_sous_le_seuil",
     _item("Une étude sur les modèles de langage", "2024", "Dupont"),
     [_cand("Une étude sur les réseaux de transport", 2024, ["Dupont"])]),
    ("sans_corroboration_titre_non_identique",
     _item("Sociologie des controverses numeriques en ligne"),
     [_cand("Sociologie des controverses numériques")]),
    ("prefixe_seul_sans_corroboration",
     _item("Your Brain on ChatGPT"),
     [_cand("Your Brain on ChatGPT: Accumulation of Cognitive Debt")]),
    ("candidat_sans_doi",
     _item("Attention is all you need", "2017", "Vaswani"),
     [_cand("Attention Is All You Need", 2017, ["Vaswani"], doi="")]),
    ("titre_court_jamais_par_prefixe",
     _item("Attention", "2017", "Vaswani"),
     [_cand("Attention is all you need", 2017, ["Vaswani"])]),
    # --- Nouveaux cas (OPT-10) ---
    ("compte_rendu_du_travail",
     _item(T, "2023", "Dupont"),
     [_cand("Compte rendu de : " + T, 2023, ["Martin", "Dupont"],
            "10.1/cr")]),
    ("chapitre_et_ouvrage_homonymes",
     _item("Penser les humanites numeriques", "2021", "Dupont"),
     [_cand("Penser les humanités numériques", 2021, ["Dupont"],
            "10.4000/books.pum.100", "book"),
      _cand("Penser les humanités numériques", 2021, ["Dupont"],
            "10.4000/books.pum.105", "book-chapter")]),
    ("homonyme_orthographe_voisine",
     _item(T, "2023", "Dupont"), [_cand(T, 2023, ["Dupond"], "10.1/dupond")]),
    ("titre_generique_introduction",
     _item("Introduction", "2020", "Dupont", "bookSection"),
     [_cand("Introduction", 2020, ["Dupont"], "10.1/intro-a"),
      _cand("Introduction", 2020, ["Dupont"], "10.1/intro-b")]),
    ("editorial_sans_corroboration",
     _item("Éditorial"),
     [_cand("Editorial", 2019, ["Martin"], "10.1/ed-1"),
      _cand("Editorial", 2020, ["Durand"], "10.1/ed-2"),
      _cand("Editorial", 2021, ["Petit"], "10.1/ed-3")]),
    ("ecart_de_deux_ans",
     _item(T, "2023", "Dupont"), [_cand(T, 2021, ["Dupont"], "10.1/2021")]),
    ("traduction",
     _item("La souveraineté numérique en Europe", "2023", "Dupont"),
     [_cand(T, 2023, ["Dupont"], "10.1/en")]),
    # Titre à 0,91 : au-dessus du seuil ordinaire, sous le seuil renforcé.
    ("sans_corroboration_titre_proche_sous_0_97",
     _item("Les communs numeriques et la ville"),
     [_cand("Les communs numériques dans la ville", None, (),
            "10.1/communs")]),
    # --- Revue OPT-10 (règles doi-match/3) ---
    # Anciennes limites v2 (xfail) : un seul candidat, qui n'est pas le bon.
    ("erratum_seul",
     _item(T, "2023", "Dupont"),
     [_cand(T + ": Erratum", 2023, ["Dupont"], "10.1/erratum")]),
    ("mauvais_tome_seul_sans_annee",
     _item("Histoire de la sexualite 2", "", "Foucault", "book"),
     [_cand("Histoire de la sexualité 1", 1976, ["Foucault"], "10.1/t1")]),
    # Prépublication seule pour un article (invariant 6).
    ("preprint_seul_pour_un_article",
     _item("Les plateformes et la fabrique du debat public", "2023",
           "Dupont"),
     [_cand("Les plateformes et la fabrique du débat public", 2022,
            ["Dupont"], "10.31235/osf.io/abcd", "posted-content")]),
    ("version_publiee_seule_pour_un_preprint",
     _item("Les plateformes et la fabrique du debat public", "2022",
           "Dupont", "preprint"),
     [_cand("Les plateformes et la fabrique du débat public", 2023,
            ["Dupont"], "10.3917/res.230.0011", "journal-article")]),
    # Parties et tomes numérotés : même année, même auteur.
    ("partie_2_contre_partie_1",
     _item("Les usages du numérique à l'école (2)", "2020", "Dupont"),
     [_cand("Les usages du numérique à l'école (1)", 2020, ["Dupont"],
            "10.1/partie1")]),
    ("tome_2_contre_tome_1_meme_annee",
     _item("Histoire des médias en France, tome 2", "2019", "Martin"),
     [_cand("Histoire des médias en France, tome 1", 2019, ["Martin"],
            "10.1/tome1")]),
    ("partie_II_contre_partie_I_prefixe_hors_mot",
     _item("Le discours politique en ligne. Partie II", "2021", "Durand"),
     [_cand("Le discours politique en ligne. Partie I", 2021, ["Durand"],
            "10.1/p-I")]),
    ("tome_2_contre_tome_1_sans_corroboration_0_972",
     _item("Histoire des médias en France, tome 2"),
     [_cand("Histoire des médias en France, tome 1", None, (),
            "10.1/tome1")]),
    ("ouvrage_complet_pour_un_tome",
     _item("Histoire des médias en France, tome 2", "2019", "Martin"),
     [_cand("Histoire des médias en France", 2019, ["Martin"],
            "10.1/ouvrage")]),
    ("tome_pour_un_titre_sans_numero",
     _item("Histoire des médias en France", "2019", "Martin"),
     [_cand("Histoire des médias en France. Tome 3", 2019, ["Martin"],
            "10.1/tome3")]),
    # Documents dérivés qui partagent titre, année et auteur.
    ("reponse_a_l_article_par_prefixe",
     _item("La fabrique des controverses numériques", "2022", "Petit"),
     [_cand("La fabrique des controverses numériques : réponse aux "
            "critiques", 2022, ["Petit"], "10.1/reponse")]),
    ("correction_en_tete",
     _item(T + " and the cloud", "2023", "Dupont"),
     [_cand("Correction: " + T + " and the cloud", 2023, ["Dupont"],
            "10.1/correction")]),
    ("avis_de_retractation",
     _item(T + " and the cloud", "2023", "Dupont"),
     [_cand("Retraction notice: " + T + " and the cloud", 2023, ["Dupont"],
            "10.1/retraction")]),
    ("introduction_de_dossier_seule",
     _item("Les réseaux sociaux et la fabrique du débat public", "2019",
           "Dupont"),
     [_cand("Les réseaux sociaux et la fabrique du débat public : "
            "introduction au dossier", 2019, ["Dupont"], "10.1/intro")]),
    # Types CrossRef qui ne sont pas une publication autonome.
    ("evaluation_par_les_pairs",
     _item(T, "2023", "Dupont"),
     [_cand(T, 2023, ["Dupont"], "10.1/review", "peer-review")]),
    ("numero_de_revue_homonyme",
     _item("Le discours numérique", "2020", "Dupont"),
     [_cand("Le discours numérique", 2020, ["Dupont"], "10.1/numero",
            "journal-issue")]),
    # Titre tronqué (Scholar) préfixe de deux candidats, dont l'un n'est
    # signalé comme partie numérotée ou comme erratum que par la suite de son
    # titre : aucun départage en faveur de l'autre (cas relevé par la mesure
    # sur notices réelles, où un tel départage écrivait un DOI faux).
    ("titre_tronque_partie_numerotee_non_departagee",
     _item("Mind Matters: Back to Work After", "2008", "Levine"),
     [_cand("Mind Matters: Back to Work After a Baby", 2008, ["Levine"],
            "10.1/baby"),
      _cand("Mind Matters: Back to Work After Baby, Part 2", 2008,
            ["Levine"], "10.1/baby-part2")]),
    ("titre_tronque_erratum_non_departage",
     _item("Les réseaux sociaux et la fabrique", "2019", "Dupont"),
     [_cand("Les réseaux sociaux et la fabrique du débat public", 2019,
            ["Dupont"], "10.1/article"),
      _cand("Les réseaux sociaux et la fabrique du débat public : erratum",
            2019, ["Dupont"], "10.1/erratum")]),
]


@pytest.mark.parametrize("name, item, candidates", NEGATIVE_CASES,
                         ids=[c[0] for c in NEGATIVE_CASES])
def test_no_doi_is_written(name, item, candidates):
    out, entry = _resolve_one(item, candidates)
    assert not out.get("DOI"), (name, entry)
    assert "DOI:" not in out.get("extra", "")
    assert entry["status"] in ("rejete", "ambigu", "sans_candidat"), entry


def test_negative_set_size():
    """Au moins dix cas nouveaux au-delà des sept cas repris, plus les cas
    de la revue OPT-10."""
    assert len(NEGATIVE_CASES) >= 7 + 8 + 16


def test_whole_negative_set_as_one_corpus():
    """Même jeu en un seul corpus, requêtes parallèles : aucun DOI écrit.

    Une requête CrossRef ne porte que le titre et le premier auteur : les cas
    qui partagent ce couple partageraient la même réponse. On garde le
    premier cas de chaque couple (les autres sont couverts un par un
    ci-dessus)."""
    table, corpus = {}, []
    for _, item, candidates in NEGATIVE_CASES:
        key = (item["title"].strip(), dr._first_author_lastname(item))
        if key not in table:
            table[key] = candidates
            corpus.append(item)

    def fake(url, params=None, **kwargs):
        key = (params["query.bibliographic"], params.get("query.author", ""))
        return {"message": {"items": table[key]}}

    with patch.object(dr, "get_json", side_effect=fake), \
            patch("utils.http.time.sleep", lambda s: None):
        out, rep = dr.resolve_dois(corpus)
    assert len(corpus) >= 12
    assert rep["resolved"] == 0
    assert not any(it.get("DOI") for it in out)


# === Contrôles positifs : la règle reste capable d'accepter ===

POSITIVE_CASES = [
    ("accord_complet",
     _item("Attention is all you need", "2017", "Vaswani"),
     [_cand("Attention Is All You Need", 2017, ["Vaswani"], "10.1/att")],
     "10.1/att"),
    ("annee_a_un_pres",
     _item(T, "2023", "Dupont"), [_cand(T, 2024, ["Dupont"], "10.1/2024")],
     "10.1/2024"),
    ("prefixe_corrobore",
     _item("Your Brain on ChatGPT", "2025", "Kosmyna"),
     [_cand("Your Brain on ChatGPT: Accumulation of Cognitive Debt", 2025,
            ["Kosmyna"], "10.1/brain")], "10.1/brain"),
    ("seul_admissible_parmi_bruit",
     _item(T, "2023", "Dupont"),
     [_cand("Un travail sans aucun rapport", 2023, ["Dupont"], "10.1/bruit"),
      _cand(T, 2023, ["Dupont"], "10.1/bon")], "10.1/bon"),
    # --- Garde-fous v3 sans faux négatif sur des titres ordinaires ---
    ("numero_commun_dans_le_prefixe",
     _item("Le Web 2.0 et les pratiques amateurs", "2012", "Dupont"),
     [_cand("Le Web 2.0 et les pratiques amateurs : usages et publics", 2012,
            ["Dupont"], "10.1/web2")], "10.1/web2"),
    ("periode_dans_le_sous_titre",
     _item("La presse française et la guerre", "2014", "Martin"),
     [_cand("La presse française et la guerre (1870-1914)", 2014,
            ["Martin"], "10.1/presse")], "10.1/presse"),
    ("chiffres_decoupes_differemment",
     _item("COVID-19 et plateformes numériques", "2021", "Durand"),
     [_cand("COVID 19 et plateformes numériques", 2021, ["Durand"],
            "10.1/covid")], "10.1/covid"),
    ("meme_tome_ponctuation_differente",
     _item("Histoire des médias en France, tome 2", "2019", "Martin"),
     [_cand("Histoire des médias en France. Tome II", 2019, ["Martin"],
            "10.1/tome2")], "10.1/tome2"),
    ("pronom_anglais_i_n_est_pas_un_numero",
     _item("What I learned from digital fieldwork", "2020", "Smith"),
     [_cand("What I Learned from Digital Fieldwork", 2020, ["Smith"],
            "10.1/i")], "10.1/i"),
    ("mot_ordinaire_response_hors_tete",
     _item("Plant response to drought in the Sahel", "2018", "Diallo"),
     [_cand("Plant response to drought in the Sahel", 2018, ["Diallo"],
            "10.1/plant")], "10.1/plant"),
    ("prepublication_pour_une_notice_preprint",
     _item("Les plateformes et la fabrique du debat public", "2022",
           "Dupont", "preprint"),
     [_cand("Les plateformes et la fabrique du débat public", 2022,
            ["Dupont"], "10.31235/osf.io/abcd", "posted-content")],
     "10.31235/osf.io/abcd"),
    ("prepublication_pour_une_notice_de_nature_inconnue",
     _item("Les plateformes et la fabrique du debat public", "2022",
           "Dupont", "document"),
     [_cand("Les plateformes et la fabrique du débat public", 2022,
            ["Dupont"], "10.31235/osf.io/abcd", "posted-content")],
     "10.31235/osf.io/abcd"),
    ("erratum_pour_une_notice_erratum",
     _item(T + ": Erratum", "2023", "Dupont"),
     [_cand(T + ": Erratum", 2023, ["Dupont"], "10.1/erratum")],
     "10.1/erratum"),
]

# Contrôles positifs AVEC distracteur : le bon candidat est accompagné d'un
# voisin qui partage titre, année et auteur, mais que la notice CONTREDIT
# (numéro différent, type incompatible, marque de document dérivé en tête de
# titre). La règle v2 déclarait ces notices « ambigu » (aucun DOI) ; les deux
# premiers étaient comptés à tort dans le jeu négatif de la v2.
POSITIVE_WITH_DISTRACTOR = [
    ("preprint_et_version_finale",
     _item("Les plateformes et la fabrique du debat public", "2023", "Dupont"),
     [_cand("Les plateformes et la fabrique du débat public", 2022,
            ["Dupont"], "10.31235/osf.io/abcd", "posted-content"),
      _cand("Les plateformes et la fabrique du débat public", 2023,
            ["Dupont"], "10.3917/res.230.0011", "journal-article")],
     "10.3917/res.230.0011"),
    ("tomes_numerotes_renvoyes_ensemble",
     _item("Histoire de la sexualite 2", "", "Foucault", "book"),
     [_cand("Histoire de la sexualité 1", 1976, ["Foucault"], "10.1/t1"),
      _cand("Histoire de la sexualité 2", 1984, ["Foucault"], "10.1/t2")],
     "10.1/t2"),
    ("partie_I_parmi_parties_I_et_II",
     _item("Le discours politique en ligne. Partie I", "2021", "Durand"),
     [_cand("Le discours politique en ligne. Partie I", 2021, ["Durand"],
            "10.1/p1"),
      _cand("Le discours politique en ligne. Partie II", 2021, ["Durand"],
            "10.1/p2")], "10.1/p1"),
    ("article_et_compte_rendu",
     _item("Penser les humanités numériques aujourd'hui", "2021", "Dupont"),
     [_cand("Penser les humanités numériques aujourd'hui", 2021, ["Dupont"],
            "10.1/art"),
      _cand("Compte rendu : Penser les humanités numériques aujourd'hui",
            2021, ["Dupont", "Martin"], "10.1/cr")], "10.1/art"),
    ("article_et_evaluation_par_les_pairs",
     _item(T, "2023", "Dupont"),
     [_cand(T, 2023, ["Dupont"], "10.1/art", "journal-article"),
      _cand(T, 2023, ["Dupont"], "10.1/rev", "peer-review")], "10.1/art"),
]


@pytest.mark.parametrize("name, item, candidates, expected",
                         POSITIVE_CASES + POSITIVE_WITH_DISTRACTOR,
                         ids=[c[0] for c in POSITIVE_CASES
                              + POSITIVE_WITH_DISTRACTOR])
def test_positive_controls(name, item, candidates, expected):
    out, entry = _resolve_one(item, candidates)
    assert entry["status"] == "accepte", entry
    assert out["DOI"] == expected
    assert f"règles {dr.RULES_VERSION}" in out["extra"]


def test_positive_with_distractor_was_ambiguous_in_v2():
    """Mesure des faux négatifs de la v2 : sur les contrôles avec
    distracteur, la v2 déclarait « ambigu » (ou écrivait le distracteur) ;
    la v3 écrit le bon DOI partout. Le distracteur reste visible dans le
    rapport, non admissible, avec son motif."""
    for name, item, candidates, expected in POSITIVE_WITH_DISTRACTOR:
        out, entry = _resolve_one(item, candidates)
        rejected = [c for c in entry["candidates"] if not c["admissible"]]
        assert out["DOI"] == expected, name
        assert len(rejected) == len(candidates) - 1, (name, entry)
        assert all(c["reason"] for c in rejected)


def test_candidate_type_is_traced_in_report_and_doi_source():
    """Le type CrossRef de chaque candidat figure dans le rapport, et celui
    du DOI accepté dans la ligne « DOI source »."""
    case = next(c for c in POSITIVE_WITH_DISTRACTOR
                if c[0] == "preprint_et_version_finale")
    out, entry = _resolve_one(case[1], case[2])
    assert [c["type"] for c in entry["candidates"]] == [
        "posted-content", "journal-article"]
    assert entry["doi_type"] == "journal-article"
    assert "type CrossRef journal-article" in out["extra"]
    preprint = next(c for c in entry["candidates"]
                    if c["type"] == "posted-content")
    assert "prépublication" in preprint["reason"]


# === Abstentions voulues de la v3 (faux négatifs acceptés, mesurés) ===
#
# Ces notices ont un DOI juste que la v3 n'écrit pas : c'est le prix de la
# précision (invariant 5 : une règle plus stricte mesure ses faux négatifs).
# Elles sont consignées ici pour que tout assouplissement ultérieur soit une
# décision visible, pas une dérive.

KNOWN_ABSTENTIONS = [
    # Scholar a typé journalArticle une prépublication SocArXiv : son DOI
    # posted-content est refusé (prépublication non assimilée d'office).
    ("preprint_type_journal_article_par_scholar",
     _item("Algorithmic moderation and public debate", "2022", "Lee"),
     [_cand("Algorithmic moderation and public debate", 2022, ["Lee"],
            "10.31235/osf.io/xyz", "posted-content")]),
    # Titre tronqué par Scholar juste avant un sous-titre qui commence par
    # « réponse » : le travail est bien une réponse, mais la notice ne le dit
    # pas ; refusé.
    ("titre_tronque_avant_reponse",
     _item("Les plateformes et la démocratie délibérative", "2021", "Roux"),
     [_cand("Les plateformes et la démocratie délibérative : réponse à "
            "mes critiques", 2021, ["Roux"], "10.1/rep")]),
    # Nombre écrit en toutes lettres d'un côté, en chiffres de l'autre.
    ("nombre_en_lettres_contre_chiffres",
     _item("Les dix ans du règlement européen sur les données", "2026",
           "Petit"),
     [_cand("Les 10 ans du règlement européen sur les données", 2026,
            ["Petit"], "10.1/dix")]),
]


@pytest.mark.parametrize("name, item, candidates", KNOWN_ABSTENTIONS,
                         ids=[c[0] for c in KNOWN_ABSTENTIONS])
def test_known_abstentions_of_v3(name, item, candidates):
    out, entry = _resolve_one(item, candidates)
    assert not out.get("DOI"), (name, entry)
    assert entry["status"] == "rejete"


# La notice est égale au titre du bon candidat, mais un autre candidat ne se
# distingue d'elle que par la SUITE de son titre (« : erratum »,
# « : introduction au dossier ») : la notice pourrait en être un titre
# tronqué. Aucun départage ne préfère le titre le plus court : ambigu, aucun
# DOI (la baseline écrivait le bon DOI dans le premier cas, par chance : elle
# retenait le premier candidat admissible).
KNOWN_AMBIGUOUS = [
    ("article_et_erratum_renvoyes_ensemble",
     _item(T, "2023", "Dupont"),
     [_cand(T, 2023, ["Dupont"], "10.1/article"),
      _cand(T + ": Erratum", 2023, ["Dupont"], "10.1/erratum")],
     "10.1/erratum"),
    ("article_et_introduction_de_dossier_homonyme",
     _item("Les réseaux sociaux et la fabrique du débat public", "2019",
           "Dupont"),
     [_cand("Les réseaux sociaux et la fabrique du débat public", 2019,
            ["Dupont"], "10.1/article"),
      _cand("Les réseaux sociaux et la fabrique du débat public : "
            "introduction au dossier", 2019, ["Dupont"], "10.1/intro")],
     "10.1/intro"),
]


@pytest.mark.parametrize("name, item, candidates, plausible", KNOWN_AMBIGUOUS,
                         ids=[c[0] for c in KNOWN_AMBIGUOUS])
def test_known_ambiguities_of_v3(name, item, candidates, plausible):
    out, entry = _resolve_one(item, candidates)
    assert not out.get("DOI"), (name, entry)
    assert entry["status"] == "ambigu"
    flagged = [c for c in entry["candidates"] if c["plausible"]]
    assert [c["doi"] for c in flagged] == [plausible]
    assert not flagged[0]["admissible"]
    assert "tronqué" in entry["reason"]
