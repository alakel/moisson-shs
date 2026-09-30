"""
Univers de travaux synthétiques déterministes et rendu des notices natives.

Modèle
------
Un **univers** est une suite de travaux numérotés ``wid = 0 .. size-1``. Ses
attributs (sous-source éditoriale, année, DOI, type, titre, auteurs, revue,
résumé, citations) sont dérivés d'un hachage ``(graine, wid)`` : aucune liste
n'est matérialisée, un univers de 262 129 travaux (taille réelle d'Isidore sur
« discours numérique ») ne coûte que les notices effectivement servies.
L'ordre des ``wid`` est l'ordre de pertinence « vrai » de la requête.

Chaque **fournisseur** (HAL, Isidore, CrossRef, OpenAlex, Semantic Scholar,
CORE) voit un sous-ensemble de l'univers, défini par des taux d'inclusion
déterministes ; les recouvrements entre fournisseurs (dépôt HAL d'un article
Cairn, notice CrossRef d'un article OpenEdition...) en découlent et exercent
la déduplication comme en production.

Vérité terrain
--------------
Le numéro de travail est lisible dans chaque identifiant natif produit :
DOI ``10.5555/simw.<wid>`` (préfixe de test CrossRef), URI Isidore
``20.500.13089/simw<wid>``, identifiant HAL ``hal-4<wid sur 7 chiffres>``,
OpenAlex ``W888<wid>``, CorpusId S2 ``777<wid>``, identifiant CORE
``999<wid>`` (cf. ``bench.analysis.GT_PATTERNS``).

Variations (scénario « métadonnées contradictoires »)
------------------------------------------------------
Les attributs d'un travail sont identiques d'un fournisseur à l'autre, sauf si
``variations`` est configuré : DOI omis, sous-titre retiré ou ajouté,
ponctuation, casse, accents supprimés, année décalée d'un an, liste d'auteurs
tronquée, et travaux **homonymes** (deux travaux distincts de même titre et
même année, auteurs différents) pour mesurer les fusions erronées.
"""

from __future__ import annotations

import hashlib
import unicodedata
import zlib
from bisect import bisect_left
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

_MASK = (1 << 64) - 1

# Sous-sources éditoriales d'Isidore (domaine porté par l'URL de la notice).
SUBSOURCES = ("openedition", "cairn", "persee", "erudit", "theses", "hal", "autre")

DEFAULT_SUBSOURCE_WEIGHTS = {
    "openedition": 0.30, "cairn": 0.20, "persee": 0.05, "erudit": 0.04,
    "theses": 0.03, "hal": 0.20, "autre": 0.18,
}

# Distribution des années par sous-source : (début, fin, asymétrie). Une
# asymétrie > 1 concentre les travaux sur les années récentes, < 1 sur les
# années anciennes (Persée : fonds patrimonial).
DEFAULT_YEAR_PROFILES = {
    "openedition": (1998, 2026, 1.6),
    "cairn": (2001, 2026, 1.5),
    "persee": (1890, 2024, 0.55),
    "erudit": (1992, 2026, 1.3),
    "theses": (1985, 2026, 1.4),
    "hal": (1995, 2026, 1.8),
    "autre": (1970, 2026, 1.3),
}

DEFAULT_DOI_RATES = {
    "openedition": 0.65, "cairn": 0.75, "persee": 0.35, "erudit": 0.55,
    "theses": 0.0, "hal": 0.35, "autre": 0.40,
}

# Préfixes « réalistes » affichés dans les URL (le DOI lui-même reste sur le
# préfixe de test 10.5555 pour porter la vérité terrain).
_REVUES = {
    "openedition": ["lidil", "semen", "questionsdecommunication", "rfsic",
                    "communication", "edc", "mots", "pratiques", "corela"],
    "cairn": ["reseaux", "hermes", "le-temps-des-medias", "communication-et-langages",
              "langage-et-societe", "sociologie", "politix", "quaderni"],
    "persee": ["colan", "reso", "mots", "lfr", "hom", "rfsp"],
    "erudit": ["cs", "ms", "ref", "ac", "rs", "tc"],
    "theses": ["these"],
    "hal": ["hal"],
    "autre": ["revue-x", "cahiers-y", "etudes-z"],
}

_WORDS_A = [
    "discours", "médiation", "numérique", "écriture", "plateformes", "réseaux",
    "énonciation", "corpus", "sémiotique", "controverse", "identité", "mémoire",
    "archives", "journalisme", "publics", "algorithmes", "traces", "pratiques",
    "territoires", "savoirs", "circulation", "représentations", "littératie",
    "éditorialisation", "visibilité", "argumentation", "polémique", "autorité",
    "transmission", "patrimoine", "dispositifs", "interfaces", "usages",
    "hashtags", "blogs", "forums", "commentaires", "médias", "images", "vidéos",
]
_WORDS_B = [
    "politique", "scientifique", "éducatif", "militant", "institutionnel",
    "médiatique", "ordinaire", "collectif", "critique", "francophone",
    "contemporain", "participatif", "professionnel", "numérisé", "populaire",
    "juridique", "urbain", "sanitaire", "environnemental", "littéraire",
]
_WORDS_C = [
    "enjeux", "formes", "mutations", "approches", "perspectives", "logiques",
    "figures", "tensions", "frontières", "régimes", "économies", "cultures",
    "trajectoires", "normes", "temporalités", "espaces", "modèles", "scènes",
]
_WORDS_D = [
    "Twitter", "Wikipédia", "YouTube", "la presse", "l'école", "l'université",
    "la recherche", "l'action publique", "les bibliothèques", "les musées",
    "la santé", "le travail", "la ville", "l'environnement", "la justice",
]
_FIRST = [
    "Marie", "Julien", "Claire", "Thomas", "Sophie", "Nicolas", "Camille",
    "Pierre", "Anne", "Lucas", "Élise", "Antoine", "Léa", "Hugo", "Inès",
    "Mathieu", "Chloé", "Olivier", "Sarah", "Benoît", "Émilie", "François",
    "Hélène", "Yves", "Zoé", "Rémi", "Agnès", "Gaëlle", "Jérôme", "Noémie",
]
_LAST = [
    "Martin", "Bernard", "Dubois", "Lefèvre", "Moreau", "Laurent", "Simon",
    "Michel", "Garcia", "Roux", "Fournier", "Girard", "Bonnet", "Dupont",
    "Lambert", "Fontaine", "Rousseau", "Vincent", "Muller", "Faure", "André",
    "Mercier", "Blanc", "Guérin", "Boyer", "Garnier", "Chevalier", "François",
    "Legrand", "Gauthier", "Perrin", "Robin", "Clément", "Morin", "Nicolas",
    "Henry", "Roussel", "Mathieu", "Gautier", "Masson", "Marchand", "Duval",
    "Denis", "Dumont", "Marie", "Lemaire", "Noël", "Meyer", "Dufour", "Meunier",
]


def _mix(x: int) -> int:
    """Mélangeur splitmix64 : entier 64 bits pseudo-aléatoire et reproductible."""
    x = (x + 0x9E3779B97F4A7C15) & _MASK
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & _MASK
    return x ^ (x >> 31)


def salt(text: str) -> int:
    return zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF


def unit(seed: int, salt_value: int, n: int) -> float:
    """Réel pseudo-aléatoire dans [0, 1) déterminé par (graine, sel, n)."""
    return _mix(seed ^ _mix((salt_value << 32) ^ (n & _MASK))) / 18446744073709551616.0


def seed_for(*parts: Any) -> int:
    h = hashlib.blake2b("|".join(str(p) for p in parts).encode("utf-8"),
                        digest_size=8)
    return int.from_bytes(h.digest(), "big")


def strip_accents(text: str) -> str:
    txt = unicodedata.normalize("NFKD", text)
    return "".join(c for c in txt if not unicodedata.combining(c))


class _Rng:
    """Tirages successifs déterministes pour un travail donné."""

    __slots__ = ("seed", "wid", "k")

    def __init__(self, seed: int, wid: int, stream: str):
        self.seed = seed ^ salt(stream)
        self.wid = wid
        self.k = 0

    def random(self) -> float:
        self.k += 1
        return unit(self.seed, self.k, self.wid)

    def randint(self, lo: int, hi: int) -> int:
        return lo + int(self.random() * (hi - lo + 1))

    def choice(self, seq):
        return seq[int(self.random() * len(seq))]

    def sample(self, seq, k: int) -> list:
        pool = list(seq)
        out = []
        for _ in range(min(k, len(pool))):
            out.append(pool.pop(int(self.random() * len(pool))))
        return out


class Universe:
    """
    Univers de travaux d'une requête simulée.

    Args:
        config: dict (toutes les clés sont optionnelles) :
            ``seed`` (int ou chaîne), ``size`` (nombre de travaux),
            ``subsource_weights``, ``year_profiles``, ``doi_rates``,
            ``english_rate``, ``variations`` (cf. docstring du module).
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = dict(config)
        self.seed = seed_for("univers", config.get("seed", "defaut"))
        self.size = int(config.get("size", 5000))
        # Des poids fournis REMPLACENT les poids par défaut (une sous-source
        # non citée a un poids nul) : un univers « international » ne doit pas
        # hériter des proportions francophones.
        weights = dict(config.get("subsource_weights") or DEFAULT_SUBSOURCE_WEIGHTS)
        total = sum(weights.values()) or 1.0
        acc = 0.0
        self._sub_names: List[str] = []
        self._sub_cdf: List[float] = []
        for name in SUBSOURCES:
            w = weights.get(name, 0.0) / total
            if w <= 0:
                continue
            acc += w
            self._sub_names.append(name)
            self._sub_cdf.append(acc)
        self._sub_cdf[-1] = 1.0
        self.subsource_weights = {n: weights.get(n, 0.0) / total for n in SUBSOURCES}
        self.year_profiles = dict(DEFAULT_YEAR_PROFILES)
        self.year_profiles.update({k: tuple(v) for k, v in
                                   (config.get("year_profiles") or {}).items()})
        self.doi_rates = dict(DEFAULT_DOI_RATES)
        self.doi_rates.update(config.get("doi_rates") or {})
        self.english_rate = float(config.get("english_rate", 0.08))
        self.variations = dict(config.get("variations") or {})
        self._s_sub = salt("sous-source")
        self._s_year = salt("annee")
        self._s_doi = salt("doi")
        self._s_hom = salt("homonyme")
        self._cache: Dict[int, Dict[str, Any]] = {}

    # --- attributs bon marché (utilisés pour les filtres et l'inclusion) ---

    def subsource(self, wid: int) -> str:
        return self._sub_names[bisect_left(self._sub_cdf, unit(self.seed, self._s_sub, wid))]

    def year(self, wid: int) -> int:
        lo, hi, skew = self.year_profiles[self.subsource(wid)]
        u = unit(self.seed, self._s_year, wid)
        return min(hi, lo + int((hi - lo + 1) * (u ** (1.0 / skew))))

    def has_doi(self, wid: int) -> bool:
        return unit(self.seed, self._s_doi, wid) < self.doi_rates.get(self.subsource(wid), 0.4)

    def lang(self, wid: int) -> str:
        """
        Langue du travail (« en » ou « fr ») sans générer le travail complet :
        premier tirage du flux « travail », celui que fait ``work`` (filtres
        de langue simulés, OPT-12).
        """
        return "en" if _Rng(self.seed, wid, "travail").random() < self.english_rate else "fr"

    def homonym_of(self, wid: int) -> Optional[int]:
        """Numéro du travail dont ``wid`` reprend le titre (travail distinct)."""
        rate = float(self.variations.get("homonym_rate", 0.0))
        if wid > 0 and rate > 0 and unit(self.seed, self._s_hom, wid) < rate:
            return wid - 1
        return None

    # --- travail complet ---

    def work(self, wid: int) -> Dict[str, Any]:
        """Attributs complets du travail ``wid`` (mémoïsés)."""
        w = self._cache.get(wid)
        if w is not None:
            return w
        if len(self._cache) > 200000:
            self._cache.clear()
        rng = _Rng(self.seed, wid, "travail")
        sub = self.subsource(wid)
        year = self.year(wid)
        english = rng.random() < self.english_rate
        title_wid = self.homonym_of(wid)
        main_title, subtitle = self._title(title_wid if title_wid is not None else wid)
        n_auth = 1 + int(rng.random() ** 2 * 4)
        authors = [(rng.choice(_FIRST), rng.choice(_LAST)) for _ in range(n_auth)]
        if sub == "theses":
            kind = "thesis"
        elif sub == "hal":
            r = rng.random()
            kind = ("article" if r < 0.5 else "communication" if r < 0.7
                    else "chapter" if r < 0.85 else "book" if r < 0.92 else "thesis")
        else:
            r = rng.random()
            kind = "article" if r < 0.85 else "chapter" if r < 0.93 else "book"
        revue = rng.choice(_REVUES[sub])
        venue = revue.replace("-", " ").title()
        abstract = self._abstract(rng, main_title)
        keywords = rng.sample(_WORDS_A, 3 + int(rng.random() * 3))
        w = {
            "wid": wid,
            "subsource": sub,
            "year": year,
            "month": rng.randint(1, 12),
            "lang": "en" if english else "fr",
            "doi": f"10.5555/simw.{wid}" if self.has_doi(wid) else None,
            "title": main_title,
            "subtitle": subtitle,
            "authors": authors,
            "kind": kind,
            "revue": revue,
            "venue": venue,
            "volume": str(rng.randint(1, 60)),
            "issue": str(rng.randint(1, 4)),
            "first_page": rng.randint(1, 300),
            "abstract": abstract,
            "keywords": keywords,
            "citations": int((rng.random() ** 3) * 400),
            "oa": rng.random() < 0.6,
            "score": round(30.0 * (1.0 - wid / max(self.size, 1)) ** 2
                           + rng.random() * 3.0, 4),
        }
        self._cache[wid] = w
        return w

    def _title(self, wid: int) -> Tuple[str, Optional[str]]:
        rng = _Rng(self.seed, wid, "titre")
        a1, a2 = rng.sample(_WORDS_A, 2)
        b = rng.choice(_WORDS_B)
        c = rng.choice(_WORDS_C)
        d = rng.choice(_WORDS_D)
        form = int(rng.random() * 4)
        if form == 0:
            main = f"{a1.capitalize()} {b} et {a2}"
        elif form == 1:
            main = f"Les {c} du {a1} {b}"
        elif form == 2:
            main = f"{c.capitalize()} de la {a1} {b} sur {d}"
        else:
            main = f"{a1.capitalize()}, {a2} et {c} {b}s"
        subtitle = None
        if rng.random() < 0.55:
            subtitle = f"{rng.choice(_WORDS_C)} et {rng.choice(_WORDS_A)} dans {d}"
        # Suffixe discriminant : deux travaux distincts n'ont (presque) jamais
        # le même titre, sans pour autant rendre les titres artificiels.
        tag = _WORDS_A[(wid * 7919) % len(_WORDS_A)]
        tag2 = _WORDS_B[(wid // len(_WORDS_A)) % len(_WORDS_B)]
        main = f"{main} ({tag} {tag2} {wid % 997})"
        return main, subtitle

    @staticmethod
    def _abstract(rng: _Rng, title: str) -> str:
        parts = [f"Cet article examine {title.lower()}."]
        for _ in range(3 + int(rng.random() * 3)):
            parts.append(
                f"Nous analysons les {rng.choice(_WORDS_C)} de la "
                f"{rng.choice(_WORDS_A)} {rng.choice(_WORDS_B)} à partir d'un "
                f"corpus de {rng.randint(20, 900)} {rng.choice(_WORDS_A)}."
            )
        return " ".join(parts)

    # --- variantes par fournisseur ---

    def view(self, wid: int, provider: str) -> Dict[str, Any]:
        """
        Attributs du travail tels que les expose ``provider`` (variations
        éventuelles appliquées de façon déterministe par (fournisseur, travail)).
        """
        w = self.work(wid)
        v = self.variations
        if not v:
            return w
        rng = _Rng(self.seed ^ salt(provider), wid, "variante")
        out = dict(w)
        title = w["title"]
        sub = w["subtitle"]
        full = f"{title} : {sub}" if sub else title
        if sub and rng.random() < float(v.get("subtitle_drop", 0.0)):
            full = title
        if rng.random() < float(v.get("punctuation", 0.0)):
            full = full.replace(" : ", ". ").replace(",", "") + "."
        if rng.random() < float(v.get("case", 0.0)):
            full = full.upper() if rng.random() < 0.3 else full.lower()
        if rng.random() < float(v.get("accents", 0.0)):
            full = strip_accents(full)
        out["full_title"] = full
        # Une notice CrossRef porte toujours son DOI (c'est son identifiant).
        if (out["doi"] and provider != "crossref"
                and rng.random() < float(v.get("doi_drop", 0.0))):
            out["doi"] = None
        elif out["doi"] and rng.random() < float(v.get("doi_case", 0.0)):
            out["doi"] = out["doi"].upper()
        if rng.random() < float(v.get("year_shift", 0.0)):
            out["year"] = w["year"] + (1 if rng.random() < 0.5 else -1)
        if len(w["authors"]) > 1 and rng.random() < float(v.get("authors_truncate", 0.0)):
            out["authors"] = w["authors"][:1]
        return out

    @staticmethod
    def display_title(w: Dict[str, Any]) -> str:
        if "full_title" in w:
            return w["full_title"]
        return f"{w['title']} : {w['subtitle']}" if w["subtitle"] else w["title"]


# === Rendu au format natif de chaque API ===

_HAL_DOCTYPE = {"article": "ART", "communication": "COMM", "chapter": "COUV",
                "book": "OUV", "thesis": "THESE"}


def hal_docid(wid: int) -> int:
    """docid HAL unique, sans corrélation avec la pertinence (ordre ``docid asc``)."""
    return ((_mix(wid ^ 0x5A5A) & 0x3FF) << 20) | wid


def hal_id(wid: int) -> str:
    return f"hal-4{wid:07d}"


def _filler_text(wid: int, size: int) -> str:
    """Texte de remplissage déterministe d'environ ``size`` caractères."""
    if size <= 0:
        return ""
    words = []
    total = 0
    i = wid
    while total < size:
        word = _WORDS_A[i % len(_WORDS_A)]
        words.append(word)
        total += len(word) + 1
        i = (i * 31 + 7) % 104729
    return " ".join(words)


def render_hal(u: Universe, wid: int, score: Optional[float] = None,
               padding: int = 0) -> Dict[str, Any]:
    w = u.view(wid, "hal")
    title = w.get("full_title") or w["title"]
    first = [a[0] for a in w["authors"]]
    last = [a[1] for a in w["authors"]]
    doc = {
        "docid": str(hal_docid(wid)),
        "halId_s": hal_id(wid),
        "uri_s": f"https://hal.science/{hal_id(wid)}v1",
        "title_s": [title],
        "authFullName_s": [f"{f} {l}" for f, l in w["authors"]],
        "authLastName_s": last,
        "authFirstName_s": first,
        "producedDate_s": f"{w['year']}-{w['month']:02d}-15",
        "producedDateY_i": w["year"],
        "publicationDateY_i": w["year"],
        "submittedDate_s": f"{min(w['year'] + 1, 2026)}-02-01 10:00:00",
        "docType_s": _HAL_DOCTYPE.get(w["kind"], "ART"),
        "language_s": [w["lang"]],
        "abstract_s": [w["abstract"]],
        "keyword_s": list(w["keywords"]),
        "domain_s": ["0.shs", "1.shs.info"],
        "openAccess_bool": bool(w["oa"]),
        "labStructAcronym_s": ["LAB-SIM"],
    }
    if "full_title" not in w and w["subtitle"]:
        doc["subTitle_s"] = [w["subtitle"]]
    if w["doi"]:
        doc["doiId_s"] = w["doi"]
    if w["kind"] == "article":
        doc.update({"journalTitle_s": w["venue"], "volume_s": w["volume"],
                    "issue_s": [w["issue"]], "page_s": f"{w['first_page']}-{w['first_page'] + 20}"})
    elif w["kind"] == "communication":
        doc.update({"conferenceTitle_s": f"Colloque {w['venue']}",
                    "conferenceStartDate_s": f"{w['year']}-06-01"})
    elif w["kind"] == "chapter":
        doc.update({"bookTitle_s": f"Ouvrage collectif {w['venue']}",
                    "publisher_s": ["Presses simulées"]})
    elif w["kind"] == "book":
        doc.update({"publisher_s": ["Presses simulées"], "isbn_s": f"978-2-{wid % 100000:05d}-000-0"})
    elif w["kind"] == "thesis":
        doc.update({"thesisSupervisor_s": [f"{w['authors'][0][0]} Directeur"],
                    "instStructName_s": ["Université simulée"]})
    if w["oa"]:
        doc["fileMain_s"] = f"https://hal.science/{hal_id(wid)}/document"
    if padding:
        # Résumé anglais et structures : volume d'un document HAL réel avec la
        # liste de champs du connecteur (~6 Ko), sans effet sur le mapping
        # (abstract_s est prioritaire).
        doc["en_abstract_s"] = [_filler_text(wid, padding)]
        doc["labStructName_s"] = ["Laboratoire simulé de sciences de l'information"]
    if score is not None:
        doc["score"] = score
    return doc


def _isidore_urls(w: Dict[str, Any]) -> Tuple[str, List[str]]:
    """URL primaire et agrégats d'une notice Isidore selon sa sous-source."""
    wid = w["wid"]
    sub = w["subsource"]
    revue = w["revue"]
    doi_url = f"https://doi.org/{w['doi']}" if w["doi"] else None
    if sub == "openedition":
        site = f"https://journals.openedition.org/{revue}/simw{wid}"
        primary = doi_url or site
        aggs = [primary, f"https://hdl.handle.net/20.500.13089/simw{wid}", site]
    elif sub == "cairn":
        site = f"https://www.cairn.info/revue-{revue}-{w['year']}-{w['issue']}-page-{w['first_page']}-simw{wid}.htm"
        primary = site
        aggs = [site] + ([doi_url] if doi_url else [])
    elif sub == "persee":
        site = f"https://www.persee.fr/doc/{revue}_simw{wid}"
        primary = site
        aggs = [site] + ([doi_url] if doi_url else [])
    elif sub == "erudit":
        site = f"https://id.erudit.org/iderudit/simw{wid}ar"
        primary = site
        aggs = [site, f"https://www.erudit.org/fr/revues/{revue}/simw{wid}"]
    elif sub == "theses":
        site = f"https://theses.fr/{w['year']}SIMW{wid}"
        primary = site
        aggs = [site]
    elif sub == "hal":
        site = f"https://hal.science/{hal_id(wid)}"
        primary = site
        aggs = [site] + ([doi_url] if doi_url else [])
    else:
        site = f"https://www.{revue}.example.fr/article/simw{wid}"
        primary = doi_url or site
        aggs = [primary, site]
    seen = []
    for a in aggs:
        if a and a not in seen:
            seen.append(a)
    return primary, seen


_ISIDORE_TYPES = {
    "article": ["article", "info:eu-repo/semantics/article"],
    "communication": ["conferenceObject", "info:eu-repo/semantics/conferenceObject"],
    "chapter": ["bookPart", "info:eu-repo/semantics/bookPart"],
    "book": ["book", "info:eu-repo/semantics/book"],
    "thesis": ["doctoralThesis", "info:eu-repo/semantics/doctoralThesis"],
}


def _isidore_entities(wid: int, size: int) -> List[Dict[str, str]]:
    """
    Bloc ``extraction.entites_nommees`` d'environ ``size`` octets : beaucoup
    de petits objets, comme les notices réelles (le coût de décodage JSON
    dépend du nombre d'objets, pas seulement du volume).
    """
    n = max(size // 58, 0)
    return [{"@type": "REGEXP", "@items": str(1 + (wid + k) % 5),
             "$": _WORDS_A[(wid * 13 + k) % len(_WORDS_A)]} for k in range(n)]


def render_isidore(u: Universe, wid: int, padding: int = 40000) -> Dict[str, Any]:
    w = u.view(wid, "isidore")
    title = Universe.display_title(w)
    primary, aggs = _isidore_urls(w)
    creators = [{
        "@origin": f"{l}, {f}", "@normalizedAuthor": f"{l}, {f}",
        "@idAuthor": strip_accents(f"{l}_{f}").lower(),
        "firstname": f, "lastname": l, "title": [], "sameAs": [],
    } for f, l in w["authors"]]
    rec = {
        "@uri": f"20.500.13089/simw{wid}",
        "isidore": {
            "title": [{"@xml:lang": w["lang"], "$": title},
                      {"@added": "true", "@xml:lang": "en", "$": title}, title],
            "url": primary,
            "enrichedCreators": {"creator": creators[0] if len(creators) == 1 else creators},
            "date": {"@origin": str(w["year"]), "@startDate": f"{w['year']}-01-01",
                     "@endDate": f"{w['year']}-12-31",
                     "normalizedDate": f"{w['year']}-01-01",
                     "year": str(w["year"])},
            "ore": {"aggregates": [{"@crawl": "true", "$": a} for a in aggs]},
            "abstract": [{"@xml:lang": w["lang"], "$": w["abstract"]}],
            "types": {"type": list(_ISIDORE_TYPES.get(w["kind"], ["article"]))},
            "source": {"@handle": f"10670/2.sim{w['revue']}",
                       "name": [{"@xml:lang": "fr", "$": w["venue"]}],
                       "url": f"https://example.org/{w['revue']}"},
            "subject": [{"@xml:lang": "fr", "$": k} for k in w["keywords"]],
            # Champ de langue des notices réelles (sondes du 2026-09-29).
            "dc:language": w["lang"],
            # Blocs d'enrichissement et d'extraction : volume comparable aux
            # notices réelles (~45 Ko), ignorés par le mapper.
            "enrichment": {"categories": {"category": {
                "@proba": "1.000",
                "prefLabel": [{"@xml:lang": "fr", "$": "Sciences de l'information"}],
            }}},
            "extraction": {"entites_nommees": {
                "entite_nommee": _isidore_entities(wid, padding)}},
        },
    }
    return rec


_CROSSREF_TYPES = {"article": "journal-article", "communication": "proceedings-article",
                   "chapter": "book-chapter", "book": "book", "thesis": "dissertation"}


def render_crossref(u: Universe, wid: int, rank: int, total: int) -> Dict[str, Any]:
    w = u.view(wid, "crossref")
    title = w.get("full_title") or w["title"]
    it = {
        "DOI": w["doi"] or f"10.5555/simw.{wid}",
        "title": [title],
        "author": [{"given": f, "family": l, "sequence": "first" if i == 0 else "additional"}
                   for i, (f, l) in enumerate(w["authors"])],
        "abstract": f"<jats:p>{w['abstract']}</jats:p>",
        "container-title": [w["venue"]],
        "issued": {"date-parts": [[w["year"], w["month"], 15]]},
        "volume": w["volume"],
        "issue": w["issue"],
        "page": f"{w['first_page']}-{w['first_page'] + 20}",
        "ISSN": ["1234-5678"],
        "type": _CROSSREF_TYPES.get(w["kind"], "journal-article"),
        "URL": f"https://doi.org/{w['doi'] or f'10.5555/simw.{wid}'}",
        "publisher": "Éditions simulées",
        "subject": list(w["keywords"][:2]),
        "is-referenced-by-count": w["citations"],
        # Pertinence du travail pour la requête (ordre naturel de l'univers),
        # indépendante de sa position dans la réponse : sous un curseur sans
        # tri, CrossRef sert l'ordre de son index, pas la pertinence (live
        # 2026-09-29, OPT-12), et le score doit le montrer.
        "score": round(80.0 * (1.0 - wid / max(u.size, 1)) + 5.0, 3),
    }
    if "full_title" not in w and w["subtitle"]:
        it["subtitle"] = [w["subtitle"]]
    return it


def render_openalex(u: Universe, wid: int, rank: int, total: int) -> Dict[str, Any]:
    w = u.view(wid, "openalex")
    title = Universe.display_title(w)
    doi_url = f"https://doi.org/{w['doi']}" if w["doi"] else None
    inv: Dict[str, List[int]] = {}
    for i, tok in enumerate(w["abstract"].split()[:60]):
        inv.setdefault(tok, []).append(i)
    oid = f"https://openalex.org/W888{wid:07d}"
    ids = {"openalex": oid}
    if doi_url:
        ids["doi"] = doi_url
    return {
        "id": oid,
        "doi": doi_url,
        "title": title,
        "display_name": title,
        "publication_year": w["year"],
        "publication_date": f"{w['year']}-{w['month']:02d}-15",
        "language": w["lang"],
        "type": {"article": "article", "communication": "article", "chapter": "book-chapter",
                 "book": "book", "thesis": "dissertation"}.get(w["kind"], "article"),
        "authorships": [{"author": {"display_name": f"{f} {l}"}} for f, l in w["authors"]],
        "primary_location": {
            "landing_page_url": doi_url or f"https://example.org/w{wid}",
            "source": {"display_name": w["venue"], "issn_l": "1234-5678", "issn": ["1234-5678"]},
        },
        "abstract_inverted_index": inv,
        "concepts": [{"display_name": k, "score": 0.6} for k in w["keywords"][:3]],
        "cited_by_count": w["citations"],
        "open_access": {"is_oa": bool(w["oa"]), "oa_url": doi_url if w["oa"] else None},
        "ids": ids,
        "biblio": {"volume": w["volume"], "issue": w["issue"],
                   "first_page": str(w["first_page"]), "last_page": str(w["first_page"] + 20)},
        "relevance_score": round(900.0 * (1.0 - rank / max(total, 1)) + 10.0, 3),
    }


def s2_paper_id(wid: int) -> str:
    return hashlib.sha1(f"simw{wid}".encode()).hexdigest()


def render_s2(u: Universe, wid: int) -> Dict[str, Any]:
    w = u.view(wid, "semantic_scholar")
    title = Universe.display_title(w)
    ext = {"CorpusId": int(f"777{wid:07d}")}
    if w["doi"]:
        ext["DOI"] = w["doi"]
    pid = s2_paper_id(wid)
    return {
        "paperId": pid,
        "title": title,
        "abstract": w["abstract"],
        "year": w["year"],
        "publicationDate": f"{w['year']}-{w['month']:02d}-15",
        "authors": [{"authorId": str(1000 + i), "name": f"{f} {l}"}
                    for i, (f, l) in enumerate(w["authors"])],
        "externalIds": ext,
        "citationCount": w["citations"],
        "influentialCitationCount": w["citations"] // 10,
        "publicationTypes": ["JournalArticle"] if w["kind"] == "article" else ["Book"],
        "publicationVenue": {"name": w["venue"], "issn": "1234-5678"},
        "venue": w["venue"],
        "journal": {"name": w["venue"], "volume": w["volume"],
                    "pages": f"{w['first_page']}-{w['first_page'] + 20}"},
        "openAccessPdf": {"url": f"https://example.org/pdf/simw{wid}.pdf"} if w["oa"] else None,
        "s2FieldsOfStudy": [{"category": "Linguistics"}, {"category": "Sociology"}],
        "fieldsOfStudy": ["Linguistics"],
        "url": f"https://www.semanticscholar.org/paper/{pid}",
    }


def render_core(u: Universe, wid: int) -> Dict[str, Any]:
    w = u.view(wid, "core")
    title = Universe.display_title(w)
    cid = int(f"999{wid:07d}")
    return {
        "id": cid,
        "title": title,
        "authors": [{"name": f"{l}, {f}"} for f, l in w["authors"]],
        "abstract": w["abstract"],
        "yearPublished": w["year"],
        "publishedDate": f"{w['year']}-{w['month']:02d}-15T00:00:00",
        "doi": w["doi"],
        "documentType": "research" if w["kind"] != "thesis" else "thesis",
        "language": {"code": w["lang"], "name": "French" if w["lang"] == "fr" else "English"},
        "journals": [{"title": w["venue"]}],
        "downloadUrl": f"https://core.ac.uk/download/{cid}.pdf",
        "sourceFulltextUrls": [f"https://repo.example.org/simw{wid}"],
        "fullText": None,
        "topics": list(w["keywords"][:3]),
    }
