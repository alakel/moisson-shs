"""
Résolution de DOI par appariement bibliographique contre CrossRef.

Google Scholar ne renvoie pas de DOI. Sans DOI, une notice moissonnée ne peut
pas emprunter le chemin d'import automatique dans Zotero
(``zotero_add_by_doi``, qui crée l'item dans la bonne collection, le tague et
lui joint un PDF via Unpaywall) : elle bascule dans un lot CSL-JSON à importer
à la main. Ce module rétablit ce chemin en retrouvant le DOI à partir du titre,
du premier auteur et de l'année, via ``query.bibliographic`` de CrossRef, qui
est gratuit et sans clé.

**Ce module apparie, il ne recherche pas.** CrossRef n'est pas ici une source
de découverte : on ne lui demande pas ce qui existe sur un sujet, mais si un
travail déjà trouvé ailleurs porte un DOI. La distinction compte, parce qu'elle
fixe le critère de réussite : un DOI faux est bien pire qu'un DOI manquant. Un
DOI manquant se voit, et l'item passe par l'import manuel ; un DOI faux entre
silencieusement dans Zotero, attaché à un autre travail, et ressort plus tard
dans une bibliographie sans que rien ne le signale.

D'où une règle d'acceptation délibérément stricte et entièrement traçable
(version ``RULES_VERSION``) :

1. la similarité des titres normalisés doit atteindre ``DOI_TITLE_THRESHOLD`` ;
2. si les deux années sont connues, leur écart doit rester dans
   ``DOI_YEAR_TOLERANCE`` (une année d'écart est normale entre publication en
   ligne et parution en numéro) ; un écart supérieur rejette ;
3. si le premier auteur de la notice et les auteurs du candidat sont connus,
   le nom de famille doit figurer parmi eux ; son absence rejette ;
4. si ni l'année ni les auteurs ne sont vérifiables, aucune corroboration n'est
   possible : seul un titre quasi identique
   (``DOI_TITLE_THRESHOLD_UNCORROBORATED``) est alors accepté ;
5. **tous** les candidats renvoyés par CrossRef sont examinés : si plusieurs
   travaux distincts (DOI différents) sont admissibles, la notice est déclarée
   ``ambigu`` et aucun DOI n'est écrit (chapitre et ouvrage, rééditions :
   l'appariement ne tranche pas à la place du lecteur) ;
6. garde-fous de nature (règles ``doi-match/3``), qui ne font que retirer des
   candidats admissibles : un document dérivé (erratum, rétractation,
   compte rendu, réponse, introduction de dossier…) n'est jamais assimilé au
   travail qu'il vise, ni l'inverse ; deux titres dont les numéros diffèrent
   (tome, partie, volume, chiffre final) désignent deux travaux ; une
   prépublication (type CrossRef ``posted-content``) n'est pas assimilée à
   une notice de type publié (article, chapitre, communication…), ni une
   version publiée à une notice ``preprint`` ; un type CrossRef qui n'est pas
   une publication autonome (évaluation, composant, numéro de revue…) n'est
   jamais retenu. L'appariement par préfixe exige que le préfixe s'arrête à
   une frontière de mot.

Les seuils ne peuvent pas être assouplis : un seuil de titre sous 0,90, un
seuil renforcé sous 0,97, une tolérance d'année au-delà d'un an ou moins de
cinq candidats examinés sont refusés explicitement (``DoiRuleError``), y
compris quand ils viennent de l'environnement. Ils peuvent en revanche être
durcis.

Chaque DOI résolu porte dans le champ ``extra`` de l'item une ligne
``DOI source`` qui énonce le score de titre, les corroborations obtenues et la
version des règles. Le rapport complet (accepté, rejeté, ambigu, sans
candidat, erreur, non traité, avec scores de tous les candidats) est destiné
au fichier associé ``<stem>.doi_report.json`` (``build_report_document``).
Un appariement contestable reste ainsi contestable après coup, ce qui est la
seule façon d'auditer une opération probabiliste.

Exécution : les requêtes (une par titre normalisé et premier auteur, les
doublons d'un même corpus partageant la même requête) partent en parallèle
sur un pool borné (``DOI_RESOLVE_WORKERS``, 3 par défaut), sous la politesse
par hôte de ``utils.http`` (intervalle ``REQUEST_DELAY_CROSSREF_LIST`` entre
deux départs de requêtes de liste vers CrossRef, limite annoncée par CrossRef
si elle est plus stricte, plafond de requêtes en vol), via un client HTTP
partagé. Les réponses réussies sont gardées dans un cache mémoire
(``QueryCache``) ; une erreur réseau n'y entre jamais et reste distinguée
d'une absence de candidat. Un budget en temps et en nombre arrête proprement
la résolution : les notices restantes sont déclarées ``non_traite``. Le reste
du budget en temps est transmis à la politique HTTP pour chaque requête :
aucune tentative ne part après l'échéance.
"""

from __future__ import annotations

import inspect
import logging
import math
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from config import (
    CONTACT_EMAIL, CROSSREF_API, CROSSREF_RESOLVE_ROWS, DOI_TITLE_THRESHOLD,
    DOI_TITLE_THRESHOLD_UNCORROBORATED, DOI_YEAR_TOLERANCE,
    REQUEST_DELAY_CROSSREF_LIST, DOI_RESOLVE_WORKERS, DOI_QUERY_CACHE_SIZE,
    DOI_QUERY_CACHE_TTL,
)
from mappers.zotero_schema import append_to_extra, validate_and_clean
from unidecode import unidecode

from utils.cleaner import extract_year, normalize_for_fingerprint, strip_html
from utils import http_scholarly as _http_scholarly
from utils.http import (
    CancelSignal, OperationCancelled, get_default_client, policy_get,
)
from utils.http_scholarly import BudgetExhausted, get_json
# Extracteur commun d'identifiants (OPT-06) : seul lecteur du DOI d'une notice.
from utils import identity as _identity

logger = logging.getLogger(__name__)

SELECT_FIELDS = "DOI,title,author,issued,container-title,type"

USER_AGENT = f"moisson-shs/1.0 (mailto:{CONTACT_EMAIL})"


# === Règles et bornes ===

# Version des règles d'acceptation, écrite dans le rapport et dans la ligne
# « DOI source » de chaque DOI accepté. « 1 » : premier candidat admissible
# retenu (jusqu'au 2026-09-22). « 2 » : examen de tous les candidats, statut
# ambigu dès que deux travaux distincts sont admissibles, bornes non
# assouplissables. « 3 » (2026-09-28) : garde-fous de nature (documents
# dérivés, numéros de titre discordants, prépublication et version publiée,
# types CrossRef non autonomes), préfixe arrêté à une frontière de mot, type
# CrossRef tracé ; seuils inchangés.
RULES_VERSION = "doi-match/3"

# Planchers et plafonds des paramètres de la règle (invariant de précision :
# les seuils actuels ne sont jamais abaissés). Volontairement NON surchargeables
# par l'environnement : une variable d'environnement qui les franchirait est
# refusée à l'usage (DoiRuleError), pas appliquée.
TITLE_THRESHOLD_FLOOR = 0.90
STRICT_THRESHOLD_FLOOR = 0.97
YEAR_TOLERANCE_CEILING = 1
# Moins de candidats examinés affaiblirait la détection d'ambiguïté.
ROWS_FLOOR = 5
ROWS_CEILING = 20
# Au plus trois requêtes simultanées vers CrossRef (limite de concurrence
# annoncée pour le « polite pool »).
WORKERS_CEILING = 3

# Statuts par notice (rapport et retour d'outil).
STATUS_ACCEPTED = "accepte"
STATUS_REJECTED = "rejete"
STATUS_AMBIGUOUS = "ambigu"
STATUS_NO_CANDIDATE = "sans_candidat"
STATUS_ERROR = "erreur"
STATUS_NOT_PROCESSED = "non_traite"
STATUS_NO_TITLE = "sans_titre"

# Écart toléré entre l'échéance du budget et la fin d'une requête interrompue
# par elle (secondes) : une erreur survenue à l'échéance est imputée au
# budget (notice « non traité »), pas au service.
_DEADLINE_SLACK_S = 0.01

ALL_STATUSES = (
    STATUS_ACCEPTED, STATUS_REJECTED, STATUS_AMBIGUOUS, STATUS_NO_CANDIDATE,
    STATUS_ERROR, STATUS_NOT_PROCESSED, STATUS_NO_TITLE,
)

REPORT_FORMAT = "moisson-shs/doi-report"
REPORT_FORMAT_VERSION = 1


class DoiRuleError(ValueError):
    """Paramètre refusé : il assouplirait la règle d'acceptation ou sort des bornes."""


def _as_number(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise DoiRuleError(f"{name} invalide : nombre attendu, reçu {value!r}.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise DoiRuleError(
            f"{name} invalide : nombre attendu, reçu {value!r}.") from None
    if math.isnan(number) or math.isinf(number):
        raise DoiRuleError(f"{name} invalide : {value!r}.")
    return number


def _as_int(name: str, value: Any) -> int:
    number = _as_number(name, value)
    if number != int(number):
        raise DoiRuleError(f"{name} invalide : entier attendu, reçu {value!r}.")
    return int(number)


def check_rule_parameters(
    threshold: Any = DOI_TITLE_THRESHOLD,
    strict_threshold: Any = DOI_TITLE_THRESHOLD_UNCORROBORATED,
    year_tolerance: Any = DOI_YEAR_TOLERANCE,
    rows: Any = None,
) -> Tuple[float, float, int, Optional[int]]:
    """
    Valide les paramètres de la règle d'acceptation.

    Un paramètre peut durcir la règle (seuil plus haut, tolérance d'année
    nulle, plus de candidats examinés), jamais l'assouplir.

    Returns:
        ``(threshold, strict_threshold, year_tolerance, rows)`` convertis.

    Raises:
        DoiRuleError: paramètre qui assouplirait la règle, ou hors bornes. Le
            message dit pourquoi et quelle valeur est admise.
    """
    eps = 1e-9
    values = []
    for name, value, floor in (
        ("title_threshold", threshold, TITLE_THRESHOLD_FLOOR),
        ("strict_title_threshold", strict_threshold, STRICT_THRESHOLD_FLOOR),
    ):
        v = _as_number(name, value)
        if v > 1.0 + eps:
            raise DoiRuleError(
                f"{name} = {v:g} invalide : une similarité est comprise entre "
                f"{floor:.2f} (plancher) et 1."
            )
        if v < floor - eps:
            raise DoiRuleError(
                f"{name} = {v:.2f} refusé : ce seuil ne peut pas être abaissé "
                f"sous {floor:.2f}. Un DOI faux entre silencieusement dans "
                f"Zotero, un DOI manquant se voit : la résolution privilégie "
                f"la précision (seuils durcissables, jamais assouplis)."
            )
        values.append(v)

    tol = _as_int("year_tolerance", year_tolerance)
    if tol < 0:
        raise DoiRuleError(
            f"year_tolerance = {tol} invalide : écart d'années positif ou nul "
            f"attendu (0 ou {YEAR_TOLERANCE_CEILING})."
        )
    if tol > YEAR_TOLERANCE_CEILING:
        raise DoiRuleError(
            f"year_tolerance = {tol} refusé : la corroboration par l'année "
            f"tolère au plus {YEAR_TOLERANCE_CEILING} an d'écart (publication "
            f"en ligne puis parution en numéro) ; au-delà, une réédition ou un "
            f"homonyme passerait."
        )

    rows_value = None
    if rows is not None:
        rows_value = _as_int("rows", rows)
        if rows_value < ROWS_FLOOR:
            raise DoiRuleError(
                f"rows = {rows_value} refusé : au moins {ROWS_FLOOR} candidats "
                f"CrossRef sont examinés, pour détecter les ambiguïtés."
            )
        if rows_value > ROWS_CEILING:
            raise DoiRuleError(
                f"rows = {rows_value} refusé : au plus {ROWS_CEILING} "
                f"candidats par notice."
            )
    return values[0], values[1], tol, rows_value


# === DOI : normalisation et présence dans une notice ===

# Normalisation et lecture déléguées à utils.identity (OPT-06) : les
# comparaisons de DOI de ce module et de utils.lookups concordent ainsi, par
# construction, avec la déduplication et les statistiques.

def normalize_doi(value: Any) -> str:
    """
    DOI normalisé pour comparaison (``utils.identity.normalize_doi``) :
    préfixes ``https://doi.org/``, ``dx.doi.org`` et ``doi:`` retirés, ``%xx``
    décodés, minuscules, ponctuation finale retirée. Chaîne vide (et non
    ``None``) si la valeur n'a pas la forme ``10.<n>/<suffixe>``.
    """
    if not value or not isinstance(value, str):
        return ""
    return _identity.normalize_doi(value) or ""


def item_doi(item: Dict) -> str:
    """
    DOI déclaré d'une notice (champ ``DOI``, sinon ligne ``DOI: …`` du champ
    ``extra`` ; la ligne ``DOI source`` n'est jamais lue), normalisé, ou "".

    Lecture par l'extracteur commun ``utils.identity.extract_doi``.
    """
    return _identity.extract_doi(item) or ""


def declared_doi_value(item: Dict) -> str:
    """
    Valeur brute du DOI déclaré d'une notice, canonique ou non : champ
    ``DOI`` non vide, sinon première ligne ``DOI: …`` non vide d'``extra``
    (jamais ``DOI source``). Chaîne vide si la notice ne déclare aucun DOI.

    Sert à ne jamais traiter comme « sans DOI » une notice dont le DOI est
    présent mais mal formé (espace, deux DOI, texte accolé) : sa valeur
    d'origine ne doit pas être écrasée par un appariement.
    """
    raw = item.get("DOI")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    try:
        values = _identity.extra_values(item, "DOI")
    except Exception:  # noqa: BLE001 - repli : lecture directe d'extra
        extra = item.get("extra") or ""
        values = [line.split(":", 1)[1].strip()
                  for line in (extra.split("\n") if isinstance(extra, str)
                               else [])
                  if line.split(":", 1)[0].strip() == "DOI" and ":" in line
                  and line.split(":", 1)[1].strip()]
    return values[0] if values else ""


# === Comparaison ===

# Un appariement par préfixe n'est tenté que si le titre le plus court est
# assez substantiel pour être discriminant. « attention » est le préfixe de
# dizaines de titres sans rapport ; « your brain on chatgpt » ne l'est pas.
MIN_PREFIX_WORDS = 4
MIN_PREFIX_CHARS = 20

_NON_WORD = re.compile(r"[^a-z0-9]+")


def normalize_title(text: str) -> str:
    """
    Normalise un titre pour comparaison, en **conservant les espaces**.

    Distincte de ``utils.cleaner.normalize_for_fingerprint``, qui supprime les
    espaces : ici les frontières de mots servent à décider si un titre est
    assez substantiel pour autoriser un appariement par préfixe.
    """
    if not text:
        return ""
    return _NON_WORD.sub(" ", unidecode(strip_html(text)).lower()).strip()


def title_similarity(a: str, b: str) -> float:
    """
    Similarité de deux titres, entre 0 et 1, après normalisation.

    Accents, casse et ponctuation sont neutralisés : deux titres qui ne
    diffèrent que par leur typographie sont identiques pour cette mesure, ce
    qui est le comportement voulu (Google Scholar et CrossRef ne composent pas
    les sous-titres de la même façon).
    """
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def best_title_ratio(a: str, b: str) -> Tuple[float, bool]:
    """
    Meilleure similarité de deux titres, par comparaison entière ou par préfixe.

    Le second mode traite le cas fréquent où l'un des deux titres est amputé de
    son sous-titre : Google Scholar tronque les titres longs, et CrossRef sert
    parfois « Titre: sous-titre » là où la notice ne porte que « Titre ». Une
    comparaison entière donne alors un score bas pour deux désignations du même
    travail (0.57 pour « Your Brain on ChatGPT » contre « Your Brain on
    ChatGPT: Accumulation of Cognitive Debt »).

    L'appariement par préfixe n'est tenté que si le titre le plus court atteint
    ``MIN_PREFIX_CHARS`` caractères et ``MIN_PREFIX_WORDS`` mots, et l'appelant
    exige en plus une
    corroboration par l'année ou l'auteur : un préfixe seul ne suffit jamais.

    Depuis ``doi-match/3``, le préfixe doit s'arrêter à une frontière de mot
    du titre long : « … partie i » n'est pas un préfixe de « … partie ii ».
    Quand la frontière manque, la similarité entière est retenue (plus basse)
    et la corroboration reste exigée : la règle n'en devient jamais plus
    permissive.

    Returns:
        ``(similarité, par_préfixe)``.
    """
    ratio, by_prefix, _, _ = _align_titles(normalize_title(a),
                                           normalize_title(b))
    return ratio, by_prefix


def _align_titles(na: str, nb: str) -> Tuple[float, bool, str, str]:
    """
    Alignement de deux titres **normalisés** (voir ``best_title_ratio``).

    Returns:
        ``(similarité, par_préfixe, suite_a, suite_b)`` : en appariement par
        préfixe à frontière de mot, la suite du titre le plus long après le
        préfixe commun (l'autre suite est vide) ; sinon deux chaînes vides.
    """
    if not na or not nb:
        return 0.0, False, "", ""

    full = SequenceMatcher(None, na, nb).ratio()

    a_is_short = len(na) <= len(nb)
    short, long_ = (na, nb) if a_is_short else (nb, na)
    if (len(short) < MIN_PREFIX_CHARS
            or len(short.split()) < MIN_PREFIX_WORDS
            or len(short) == len(long_)):
        return full, False, "", ""

    prefix = SequenceMatcher(None, short, long_[: len(short)]).ratio()
    if prefix <= full:
        return full, False, "", ""
    if long_[len(short)] != " ":
        # Préfixe coupé au milieu d'un mot : similarité entière, corroboration
        # exigée comme pour un préfixe (jamais plus permissif qu'en v2).
        return full, True, "", ""
    tail = long_[len(short):].strip()
    return (prefix, True, "", tail) if a_is_short else (prefix, True, tail, "")


# === Garde-fous de nature (doi-match/3) ===
#
# Durcissements qui retirent des candidats admissibles, jamais n'en ajoutent
# (invariant de précision) : ils visent les faux positifs que la similarité de
# titre, l'année et l'auteur ne voient pas, parce qu'un erratum, une réponse,
# le tome voisin ou la prépublication partagent titre, année et auteur avec
# le travail visé.

# Types CrossRef qui ne sont jamais une publication autonome au sens d'une
# notice Zotero : évaluations par les pairs, composants (figures, annexes),
# conteneurs (revue, numéro, volume, collections), financements.
NON_WORK_CROSSREF_TYPES = frozenset({
    "peer-review", "component", "journal", "journal-issue", "journal-volume",
    "book-series", "book-set", "proceedings-series", "report-series",
    "grant",
})
PREPRINT_CROSSREF_TYPE = "posted-content"
# Types de notice dont la nature n'est pas établie : une prépublication peut
# leur être appariée (ce n'est pas assimiler une prépublication à un travail
# publié).
PREPRINT_COMPATIBLE_ITEM_TYPES = frozenset({"preprint", "document",
                                            "manuscript"})

# Marqueurs de document dérivé, sur titres normalisés (``normalize_title``).
# « Forts » : repérés partout dans le titre, comme mots entiers.
_DERIVED_STRONG = (
    ("erratum", "erratum"), ("errata", "erratum"),
    ("corrigendum", "erratum"), ("corrigenda", "erratum"),
    ("rectificatif", "erratum"), ("addendum", "addendum"),
    ("retraction", "rétractation"), ("retractation", "rétractation"),
    ("expression of concern", "rétractation"),
    ("compte rendu", "compte rendu"), ("comptes rendus", "compte rendu"),
    ("recension", "compte rendu"), ("book review", "compte rendu"),
    ("note de lecture", "compte rendu"), ("notes de lecture", "compte rendu"),
    ("rejoinder", "réponse"),
)
# « Positionnels » : repérés seulement en tête du titre ou en tête de la suite
# d'un appariement par préfixe (article initial facultatif), parce que ces
# mots figurent aussi dans des titres ordinaires (« Plant response to… »).
_DERIVED_LEADING = (
    ("correction", "erratum"), ("corrections", "erratum"),
    ("reply", "réponse"), ("response to", "réponse"),
    ("reponse", "réponse"), ("reponses", "réponse"),
    ("commentary on", "commentaire"), ("commentaire", "commentaire"),
    ("introduction", "introduction ou éditorial"),
    ("presentation du dossier", "introduction ou éditorial"),
    ("avant propos", "introduction ou éditorial"),
    ("editorial", "introduction ou éditorial"),
)
_LEADING_ARTICLES = frozenset({"a", "an", "the", "un", "une", "le", "la",
                               "les", "l"})


def _leading_words(text: str) -> str:
    words = text.split()
    if words and words[0] in _LEADING_ARTICLES:
        words = words[1:]
    return " ".join(words)


def _derived_markers(title_norm: str, tail_norm: str = "") -> set:
    """Natures de document dérivé signalées par un titre normalisé (et par la
    suite d'un appariement par préfixe, si ce titre est le plus long)."""
    found = set()
    padded = f" {title_norm} "
    for phrase, label in _DERIVED_STRONG:
        if f" {phrase} " in padded:
            found.add(label)
    for head in (title_norm, tail_norm):
        if not head:
            continue
        lead = _leading_words(head)
        for phrase, label in _DERIVED_LEADING:
            if lead == phrase or lead.startswith(phrase + " "):
                found.add(label)
    return found


# Numérotation : mots qui annoncent un numéro de tome, partie, volume…
_NUMBERING_MARKERS = frozenset({
    "tome", "tomes", "t", "vol", "volume", "volumes", "partie", "parties",
    "part", "parts", "livre", "chapitre", "chapter", "fascicule", "fasc",
    "numero", "no", "n", "cahier", "episode", "saison", "book", "section",
})
_DIGITS = re.compile(r"\d+")
# Chiffres romains de 1 à 39 (tomes, parties) : au-delà, confusion probable
# avec des mots ou des noms.
_ROMAN = re.compile(r"^(?=[ivx]+$)x{0,3}(ix|iv|v?i{0,3})$")
_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10}


def _roman_to_int(token: str) -> int:
    total, prev = 0, 0
    for ch in reversed(token):
        value = _ROMAN_VALUES[ch]
        total = total - value if value < prev else total + value
        prev = max(prev, value)
    return total


def _token_numbers(tokens: List[str], i: int) -> List[int]:
    """
    Nombres portés par le mot ``tokens[i]`` : suites de chiffres arabes ;
    chiffre romain de deux lettres ou plus ; chiffre romain d'une lettre
    (i, v, x) seulement après un mot de numérotation ou en fin de titre (le
    pronom anglais « I » n'est pas un numéro).
    """
    token = tokens[i]
    digits = _DIGITS.findall(token)
    if digits:
        return [int(d) for d in digits]
    if _ROMAN.match(token):
        previous = tokens[i - 1] if i > 0 else ""
        if (len(token) >= 2 or previous in _NUMBERING_MARKERS
                or i == len(tokens) - 1):
            return [_roman_to_int(token)]
    return []


def _tail_numbering(tail: str) -> Optional[str]:
    """Motif si la suite d'un appariement par préfixe est une numérotation
    (« 2 », « ii », « tome 2 », « part 5 … »), sinon ``None``."""
    words = tail.split()
    if not words:
        return None
    if len(words) == 1 and _token_numbers(words, 0):
        return f"suite du titre numérotée (« {tail} »)"
    for k in range(len(words) - 1):
        if (words[k] in _NUMBERING_MARKERS
                and (_DIGITS.search(words[k + 1])
                     or _ROMAN.match(words[k + 1]))):
            return (f"suite du titre numérotée "
                    f"(« {words[k]} {words[k + 1]} »)")
    return None


def _numbering_conflict(na: str, nb: str, tail_a: str = "",
                        tail_b: str = "") -> Optional[str]:
    """
    Motif de rejet si les deux titres normalisés portent des numéros
    différents (« tome 1 » / « tome 2 », « (1) » / « (2) », « partie i » /
    « partie ii », « rapport 2020 » / « rapport 2021 »), ou si la suite
    ``tail_a`` du titre ``na`` (le plus long, en appariement par préfixe) est
    une numérotation ; ``None`` sinon. Les numéros communs aux deux titres,
    et les suites de chiffres découpées différemment (« covid19 » /
    « covid 19 »), ne comptent pas. La suite ``tail_b`` n'est pas examinée
    ici (voir ``_nature_check``).
    """
    ta, tb = na.split(), nb.split()
    # En appariement par préfixe, le titre long est comparé sur son préfixe ;
    # sa suite est examinée à part.
    if tail_a:
        ta = ta[: len(ta) - len(tail_a.split())]
    if tail_b:
        tb = tb[: len(tb) - len(tail_b.split())]
    full_a, full_b = na.split(), nb.split()
    values_a = [_token_numbers(full_a, i) for i in range(len(ta))]
    values_b = [_token_numbers(full_b, j) for j in range(len(tb))]
    matcher = SequenceMatcher(None, ta, tb, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        va = sorted(n for i in range(i1, i2) for n in values_a[i])
        vb = sorted(n for j in range(j1, j2) for n in values_b[j])
        if va != vb:
            left = " ".join(map(str, va)) or "aucun"
            right = " ".join(map(str, vb)) or "aucun"
            return f"numéros de titre discordants ({left} contre {right})"
    return _tail_numbering(tail_a) if tail_a else None


def _candidate_type(candidate: Dict) -> str:
    """Type CrossRef du candidat (``journal-article``…), ou ""."""
    value = candidate.get("type")
    return value.strip().lower() if isinstance(value, str) else ""


def _nature_check(item: Dict, candidate: Dict, na: str, nb: str,
                  tail_a: str, tail_b: str) -> Tuple[Optional[str],
                                                     Optional[str]]:
    """
    Garde-fous ``doi-match/3`` : nature du candidat comparée à celle de la
    notice.

    Deux issues distinctes :

    - **exclusion** : la notice contredit le candidat (type CrossRef non
      autonome ou incompatible, numéros discordants, marque de document
      dérivé présente d'un côté seulement dans la partie commune des titres,
      suite numérotée ou dérivée du titre de la notice). Le candidat ne
      désigne pas la notice ;
    - **réserve** : seule la suite du titre du candidat, au-delà d'un
      préfixe égal au titre de la notice, le signale comme dérivé ou
      numéroté (« T : erratum », « T, tome 3 » pour la notice « T »). La
      notice peut être un titre tronqué (Google Scholar) de ce même
      candidat : il n'est pas retenu, mais il n'est pas écarté non plus, et
      sa présence rend la notice ambiguë. Aucun départage ne préfère ainsi
      le titre le plus court.

    Returns:
        ``(motif_d_exclusion, motif_de_réserve)``, chacun ou ``None``.
    """
    ctype = _candidate_type(candidate)
    item_type = str(item.get("itemType") or "")
    if ctype in NON_WORK_CROSSREF_TYPES:
        return (f"type CrossRef « {ctype} » : pas une publication "
                f"autonome"), None
    if (ctype == PREPRINT_CROSSREF_TYPE
            and item_type not in PREPRINT_COMPATIBLE_ITEM_TYPES):
        return (f"prépublication (type CrossRef {ctype}) non assimilée à "
                f"une notice {item_type or 'sans type'}"), None
    if (item_type == "preprint" and ctype
            and ctype != PREPRINT_CROSSREF_TYPE):
        return (f"version publiée (type CrossRef {ctype}) non assimilée à "
                f"une prépublication"), None

    # Documents dérivés. La suite du titre de la notice (quand elle est la
    # plus longue) fait partie de ce que la notice affirme ; celle du
    # candidat ne vaut que réserve.
    derived_item = _derived_markers(na, tail_a)
    head_b = nb[: len(nb) - len(tail_b)].strip() if tail_b else nb
    derived_cand_head = _derived_markers(head_b)
    derived_cand = _derived_markers(nb, tail_b)
    only_item = sorted(derived_item - derived_cand)
    if only_item:
        return (f"notice de nature dérivée ({', '.join(only_item)}) : le "
                f"candidat n'en porte pas la marque"), None
    only_cand_head = sorted(derived_cand_head - derived_item)
    if only_cand_head:
        return (f"document dérivé ({', '.join(only_cand_head)}) : le "
                f"candidat n'est pas le travail de la notice"), None

    conflict = _numbering_conflict(na, nb, tail_a, tail_b)
    if conflict:
        return conflict, None

    only_cand_tail = sorted(derived_cand - derived_cand_head - derived_item)
    if only_cand_tail:
        return None, (f"document dérivé ({', '.join(only_cand_tail)}) "
                      f"annoncé par la suite du titre du candidat")
    tail_numbering = _tail_numbering(tail_b) if tail_b else None
    if tail_numbering:
        return None, f"{tail_numbering} dans le titre du candidat"
    return None, None


def _first_author_lastname(item: Dict) -> str:
    """Nom de famille du premier auteur de l'item Zotero, normalisé."""
    for creator in item.get("creators") or []:
        last = (creator.get("lastName") or "").strip()
        if last:
            return normalize_for_fingerprint(last)
    return ""


def _candidate_lastnames(candidate: Dict) -> List[str]:
    """Noms de famille du candidat CrossRef, normalisés."""
    out = []
    for author in candidate.get("author") or []:
        family = (author.get("family") or "").strip()
        if family:
            out.append(normalize_for_fingerprint(family))
    return out


def _candidate_title(candidate: Dict) -> str:
    titles = candidate.get("title") or []
    return titles[0] if titles else ""


def _candidate_year(candidate: Dict) -> Optional[str]:
    parts = ((candidate.get("issued") or {}).get("date-parts") or [[]])[0]
    if parts and parts[0]:
        return str(parts[0])
    return None


def evaluate_candidate(
    item: Dict,
    candidate: Dict,
    threshold: float = DOI_TITLE_THRESHOLD,
    strict_threshold: float = DOI_TITLE_THRESHOLD_UNCORROBORATED,
    year_tolerance: int = DOI_YEAR_TOLERANCE,
) -> Tuple[bool, float, str]:
    """
    Décide si ``candidate`` (enregistrement CrossRef) désigne bien ``item``.

    Les paramètres peuvent durcir la règle, jamais l'assouplir
    (``check_rule_parameters`` lève ``DoiRuleError`` sinon).

    Returns:
        ``(accepté, similarité_de_titre, motif)``. Le motif est destiné au
        champ ``extra`` en cas d'acceptation, et au journal en cas de rejet.
        Un candidat « plausible » (voir ``_evaluate``) n'est pas accepté.
    """
    accepted, ratio, reason, _ = _evaluate(
        item, candidate, threshold, strict_threshold, year_tolerance)
    return accepted, ratio, reason


def _evaluate(
    item: Dict,
    candidate: Dict,
    threshold: float = DOI_TITLE_THRESHOLD,
    strict_threshold: float = DOI_TITLE_THRESHOLD_UNCORROBORATED,
    year_tolerance: int = DOI_YEAR_TOLERANCE,
) -> Tuple[bool, float, str, bool]:
    """
    Évaluation complète d'un candidat.

    Returns:
        ``(admissible, similarité, motif, plausible)``. ``plausible`` : le
        candidat satisfait toute la règle mais seule la suite de son titre le
        signale comme dérivé ou numéroté (réserve de ``_nature_check``) ; il
        n'est jamais retenu, et il rend la notice ambiguë s'il accompagne un
        candidat admissible.
    """
    threshold, strict_threshold, year_tolerance, _ = check_rule_parameters(
        threshold, strict_threshold, year_tolerance
    )
    na = normalize_title(item.get("title", ""))
    nb = normalize_title(_candidate_title(candidate))
    ratio, by_prefix, tail_a, tail_b = _align_titles(na, nb)
    if ratio < threshold:
        return (False, ratio,
                f"titre {ratio:.2f} sous le seuil {threshold:.2f}", False)

    # --- Nature du document (doi-match/3) ---
    exclusion, reserve = _nature_check(item, candidate, na, nb, tail_a,
                                       tail_b)
    if exclusion:
        return False, ratio, exclusion, False

    corroborations: List[str] = []

    # --- Année ---
    y_item = extract_year(item.get("date", ""))
    y_cand = _candidate_year(candidate)
    if y_item and y_cand:
        gap = abs(int(y_item) - int(y_cand))
        if gap > year_tolerance:
            return False, ratio, f"année {y_item} contre {y_cand}", False
        corroborations.append(
            "année exacte" if gap == 0 else f"année à {gap} près"
        )

    # --- Premier auteur ---
    last = _first_author_lastname(item)
    cand_lasts = _candidate_lastnames(candidate)
    if last and cand_lasts:
        if last not in cand_lasts:
            return False, ratio, f"auteur '{last}' absent du candidat", False
        corroborations.append("auteur")

    # --- Corroboration minimale ---
    # Un appariement par préfixe n'est jamais accepté seul : le titre y est
    # incomplet des deux côtés du test, donc non discriminant à lui seul.
    if by_prefix and not corroborations:
        return False, ratio, (
            "appariement par préfixe sans corroboration d'année ni d'auteur"
        ), False
    if not corroborations and ratio < strict_threshold:
        return False, ratio, (
            f"aucune corroboration possible et titre {ratio:.2f} sous le "
            f"seuil renforcé {strict_threshold:.2f}"
        ), False

    motif = f"titre {ratio:.2f}" + (" (préfixe)" if by_prefix else "")
    if corroborations:
        motif += ", " + ", ".join(corroborations)
    else:
        motif += ", sans corroboration"
    if reserve:
        return False, ratio, (
            f"{reserve} : non retenu, mais la notice peut en être un titre "
            f"tronqué ({motif})"
        ), True
    return True, ratio, motif, False


def decide(
    item: Dict,
    candidates: List[Dict],
    threshold: float = DOI_TITLE_THRESHOLD,
    strict_threshold: float = DOI_TITLE_THRESHOLD_UNCORROBORATED,
    year_tolerance: int = DOI_YEAR_TOLERANCE,
) -> Dict[str, Any]:
    """
    Examine **tous** les candidats CrossRef d'une notice et rend la décision.

    Returns:
        ``{"status", "doi", "reason", "best_score", "candidates"}`` où
        ``status`` vaut ``accepte`` (un seul travail admissible et aucun autre
        travail plausible, DOI écrit), ``ambigu`` (plusieurs travaux distincts
        admissibles, ou un admissible accompagné d'un candidat plausible —
        dérivé ou numéroté selon la seule suite de son titre, dont la notice
        peut être un titre tronqué : aucun DOI écrit), ``rejete`` (aucun
        candidat admissible, ou candidat admissible sans DOI) ou
        ``sans_candidat``. ``candidates`` détaille chaque candidat : DOI,
        titre, année, type CrossRef, score de titre, préfixe, admissibilité,
        plausibilité et motif ; ``doi_type`` donne le type CrossRef du DOI
        accepté.
    """
    if not candidates:
        return {"status": STATUS_NO_CANDIDATE, "doi": None,
                "reason": "aucun candidat CrossRef", "best_score": None,
                "candidates": []}

    evaluated = []
    for cand in candidates:
        accepted, ratio, reason, plausible = _evaluate(
            item, cand, threshold, strict_threshold, year_tolerance
        )
        _, by_prefix = best_title_ratio(item.get("title", ""),
                                        _candidate_title(cand))
        evaluated.append({
            "doi": _clean_doi(cand.get("DOI")) or None,
            "title": _candidate_title(cand),
            "year": _candidate_year(cand),
            "type": _candidate_type(cand) or None,
            "score": round(ratio, 4),
            "prefix": bool(by_prefix),
            "admissible": bool(accepted),
            "plausible": bool(plausible),
            "reason": reason,
        })

    best = max(evaluated, key=lambda c: c["score"])
    admissible = [c for c in evaluated if c["admissible"]]
    # Travaux distincts : un DOI normalisé = un travail ; un candidat
    # admissible sans DOI compte comme un travail non identifiable.
    distinct: "OrderedDict[str, Dict]" = OrderedDict()
    without_doi = 0
    for cand in admissible:
        key = normalize_doi(cand["doi"] or "")
        if key:
            distinct.setdefault(key, cand)
        else:
            without_doi += 1

    result = {"best_score": best["score"], "candidates": evaluated}
    if not admissible:
        result.update(status=STATUS_REJECTED, doi=None, reason=best["reason"],
                      best_doi=best["doi"])
        return result
    if not distinct:
        result.update(status=STATUS_REJECTED, doi=None,
                      reason="candidat admissible sans DOI",
                      best_doi=None)
        return result
    # Candidats plausibles d'un autre travail : ils interdisent le choix.
    plausible_others = [
        c for c in evaluated
        if c["plausible"] and (not normalize_doi(c["doi"] or "")
                               or normalize_doi(c["doi"]) not in distinct)
    ]
    if len(distinct) == 1 and without_doi == 0 and not plausible_others:
        chosen = next(iter(distinct.values()))
        result.update(status=STATUS_ACCEPTED, doi=chosen["doi"],
                      reason=chosen["reason"], best_score=chosen["score"],
                      best_doi=chosen["doi"], doi_type=chosen["type"])
        return result
    listed = ", ".join(f"{c['doi']} ({c['score']:.2f})"
                       for c in distinct.values())
    if without_doi:
        listed += f", plus {without_doi} candidat(s) admissible(s) sans DOI"
    count = len(distinct) + without_doi
    reason = f"{count} travaux distincts admissibles : {listed}"
    if plausible_others:
        shown = ", ".join(f"{c['doi'] or 'sans DOI'} ({c['score']:.2f})"
                          for c in plausible_others)
        reason = (f"{count} travail(aux) admissible(s) : {listed} ; "
                  f"{len(plausible_others)} autre(s) plausible(s), non "
                  f"retenu(s) mais dont la notice peut être un titre tronqué "
                  f": {shown}")
    result.update(
        status=STATUS_AMBIGUOUS, doi=None,
        reason=reason,
        best_doi=None,
        ambiguous_dois=[c["doi"] for c in distinct.values()],
        plausible_dois=[c["doi"] for c in plausible_others],
    )
    return result


# === Cache mémoire des requêtes ===

class QueryCache:
    """
    Cache mémoire LRU borné, avec durée de vie, des candidats CrossRef par
    requête bibliographique normalisée (titre normalisé, premier auteur,
    nombre de candidats).

    Seules les réponses réussies y entrent (une liste vide de candidats en
    est une) ; une erreur réseau n'y entre jamais, pour être retentée.
    Partageable entre threads et entre appels (voir
    ``shared_query_cache``).
    """

    def __init__(self, max_entries: int = DOI_QUERY_CACHE_SIZE,
                 ttl: float = DOI_QUERY_CACHE_TTL):
        self.max_entries = max(1, int(max_entries))
        self.ttl = float(ttl)
        self._lock = threading.Lock()
        self._data: "OrderedDict[Tuple, Tuple[float, List[Dict]]]" = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: Tuple) -> Optional[List[Dict]]:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self.misses += 1
                return None
            stored_at, candidates = entry
            if self.ttl > 0 and time.monotonic() - stored_at > self.ttl:
                del self._data[key]
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return list(candidates)

    def put(self, key: Tuple, candidates: List[Dict]) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), list(candidates))
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self.hits = 0
            self.misses = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


_SHARED_QUERY_CACHE = QueryCache()


def shared_query_cache() -> QueryCache:
    """Cache mémoire partagé par les appels successifs d'un même processus
    (serveur MCP) : une deuxième résolution du même corpus ne réinterroge
    pas CrossRef pour les notices déjà examinées."""
    return _SHARED_QUERY_CACHE


def query_key(title: str, author_lastname: str = "",
              rows: int = CROSSREF_RESOLVE_ROWS) -> Tuple[str, str, int]:
    """Clé normalisée d'une requête bibliographique."""
    return (normalize_title(title), author_lastname or "", int(rows))


# === Requête CrossRef ===

def _clean_doi(doi: Optional[str]) -> str:
    return (doi or "").replace("https://doi.org/", "").replace(
        "http://doi.org/", "").strip()


def _accepts_budget(fn: Any) -> bool:
    """Vrai si ``fn`` accepte un argument ``budget`` (nommé ou ``**kwargs``)."""
    try:
        parameters = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "budget" or p.kind is inspect.Parameter.VAR_KEYWORD
               for p in parameters)


def _get_json_within_budget(url: str, *, budget: Optional[float],
                            **kwargs) -> Any:
    """
    ``get_json`` borné par un budget global de requête logique (secondes).

    Le budget est transmis à la politique HTTP commune
    (``utils.http.policy_get``), qui borne par lui l'attente d'une place en
    vol, le créneau de politesse, le délai de chaque tentative et les
    réessais, et ne laisse partir aucune tentative une fois le budget épuisé.
    ``utils.http_scholarly.get_json`` n'exposant pas encore ce paramètre, la
    requête bornée passe alors directement par ``policy_get`` avec le même
    contrôle de quota (``_budget_hook``) ; dès que ``get_json`` l'accepte,
    il lui est simplement transmis.
    """
    if budget is None:
        return get_json(url, **kwargs)
    if _accepts_budget(get_json):
        return get_json(url, budget=budget, **kwargs)
    session = kwargs.get("session")
    sess = session if session is not None else get_default_client().session
    response = policy_get(
        sess, url, params=kwargs.get("params"), headers=kwargs.get("headers"),
        timeout=kwargs.get("timeout"), max_attempts=kwargs.get("max_retries"),
        min_interval=kwargs.get("min_interval"),
        response_hook=_http_scholarly._budget_hook(url), budget=budget,
        cancel=kwargs.get("cancel"),
    )
    return response.json()


def query_crossref_detailed(
    title: str,
    author_lastname: str = "",
    rows: int = CROSSREF_RESOLVE_ROWS,
    session: Optional[requests.Session] = None,
    min_interval: Optional[float] = None,
    timeout: Any = 30,
    max_retries: int = 3,
    budget: Optional[float] = None,
    cancel: Any = None,
) -> Tuple[List[Dict], Optional[str]]:
    """
    Interroge ``query.bibliographic`` de CrossRef.

    L'accès suit la politique HTTP commune (``utils.http``) : une tentative
    sur 4xx définitif, réessais bornés sur 429/5xx, politesse par hôte
    (``min_interval`` ; défaut : intervalle des requêtes de liste CrossRef,
    ``REQUEST_DELAY_CROSSREF_LIST``, fixé par ``utils.http``) appliquée aux seuls
    accès réseau réels, cache selon la session. Sans session, le client
    partagé de ``utils.http`` est utilisé. ``budget`` (secondes) borne la
    requête logique entière, attentes et réessais compris : aucune tentative
    ne part après son épuisement (défaut : budget de la politique commune).
    ``cancel`` (signal d'annulation, OPT-07) rend les attentes de la
    politique HTTP interruptibles ; une annulation lève
    ``utils.http.OperationCancelled``.

    Returns:
        ``(candidats, erreur)`` : ``erreur`` vaut ``None`` si CrossRef a
        répondu (liste éventuellement vide : aucun candidat), sinon un motif
        court sans URL (``HTTP 503``, ``ConnectionError``…).

    Raises:
        BudgetExhausted: quota épuisé (arrêt de la résolution).
        OperationCancelled: annulation (``cancel``).
    """
    params = {
        "query.bibliographic": title,
        "rows": rows,
        "select": SELECT_FIELDS,
        "mailto": CONTACT_EMAIL,
    }
    if author_lastname:
        params["query.author"] = author_lastname
    extra = {"cancel": cancel} if cancel is not None else {}
    try:
        payload = _get_json_within_budget(
            CROSSREF_API, budget=budget, params=params,
            headers={"User-Agent": USER_AGENT},
            session=session, timeout=timeout, max_retries=max_retries,
            min_interval=min_interval, **extra,
        )
    except BudgetExhausted:
        raise
    except requests.HTTPError as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        logger.warning(f"[DOI] Échec de requête CrossRef : HTTP {status}")
        return [], f"HTTP {status}"
    except Exception as e:  # noqa: BLE001 - incident isolé, non bloquant
        logger.warning(f"[DOI] Échec de requête CrossRef : {type(e).__name__}")
        return [], type(e).__name__
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, dict):
        return [], "réponse CrossRef inattendue (champ message absent)"
    items = message.get("items") or []
    if not isinstance(items, list):
        return [], "réponse CrossRef inattendue (items)"
    return [c for c in items if isinstance(c, dict)], None


def query_crossref(
    title: str,
    author_lastname: str = "",
    rows: int = CROSSREF_RESOLVE_ROWS,
    session: Optional[requests.Session] = None,
    min_interval: Optional[float] = None,
) -> List[Dict]:
    """
    Interroge ``query.bibliographic`` de CrossRef. Retourne les candidats bruts.

    Forme historique : une erreur réseau ne lève pas et renvoie une liste
    vide. ``resolve_dois`` utilise ``query_crossref_detailed``, qui distingue
    une erreur d'une absence de candidat.
    """
    candidates, _ = query_crossref_detailed(
        title, author_lastname=author_lastname, rows=rows, session=session,
        min_interval=min_interval,
    )
    return candidates


# === Résolution ===

def _new_report(total: int) -> Dict[str, Any]:
    return {
        "rules_version": RULES_VERSION,
        "total": total,
        "already_had_doi": 0,
        # Dont DOI déclaré non canonique (conservé tel quel, jamais écrasé).
        "non_canonical_doi": 0,
        "non_canonical_dois": [],   # {index, title, value}
        "attempted": 0,
        "resolved": 0,
        "rejected": 0,
        "ambiguous": 0,
        "no_candidate": 0,
        "errors": 0,
        "not_processed": 0,
        "skipped_no_title": 0,
        "queries_sent": 0,
        "query_cache_hits": 0,
        "stopped_early": False,
        "stop_reason": None,
        "resolutions": [],   # (titre, doi, motif)
        "rejections": [],    # (titre, meilleur doi écarté, motif)
        "ambiguities": [],   # (titre, [doi admissibles], motif)
        "error_list": [],    # (titre, motif)
        "entries": [],       # une entrée par notice sans DOI (rapport)
    }


def resolve_dois(
    items: List[Dict],
    threshold: float = DOI_TITLE_THRESHOLD,
    strict_threshold: float = DOI_TITLE_THRESHOLD_UNCORROBORATED,
    year_tolerance: int = DOI_YEAR_TOLERANCE,
    rows: int = CROSSREF_RESOLVE_ROWS,
    min_interval: float = REQUEST_DELAY_CROSSREF_LIST,
    max_items: Optional[int] = None,
    session: Optional[requests.Session] = None,
    *,
    http_client=None,
    workers: int = DOI_RESOLVE_WORKERS,
    max_seconds: Optional[float] = None,
    query_cache: Optional[QueryCache] = None,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[List[Dict], Dict]:
    """
    Complète les DOI manquants d'un corpus par appariement CrossRef.

    Args:
        items: Corpus au format Zotero JSON interne.
        threshold: Similarité de titre minimale (plancher 0,90, durcissable).
        strict_threshold: Seuil renforcé en l'absence de corroboration
            (plancher 0,97).
        year_tolerance: Écart d'années toléré (0 ou 1).
        rows: Candidats demandés et examinés par notice (5 à 20).
        min_interval: Espacement minimal entre deux accès réseau réels à
            CrossRef (secondes), appliqué par la politesse par hôte de
            ``utils.http`` : une réponse servie par le cache n'attend pas.
        max_items: Plafond de notices interrogées (``None`` = toutes) ; les
            suivantes sont déclarées ``non_traite``.
        session: Session requests à réutiliser (forme historique).
        http_client: Client ``utils.http.HTTPClient`` partagé (prioritaire
            sur le client par défaut ; ``session`` reste prioritaire s'il est
            fourni).
        workers: Requêtes simultanées (1 à 3), toujours sous la politesse
            par hôte.
        max_seconds: Budget en temps (secondes). Chaque requête reçoit le
            reste du budget, transmis à la politique HTTP : l'attente d'une
            place en vol et du créneau de politesse, le délai de chaque
            tentative et les réessais y sont bornés, et aucune tentative ne
            part après l'échéance. Une requête interrompue par l'échéance et
            les notices restantes sont déclarées ``non_traite`` ; la durée
            totale ne dépasse l'échéance que du délai minimal d'une tentative
            (0,1 s) et du temps de décision.
        query_cache: Cache mémoire des requêtes (défaut : cache propre à
            l'appel, qui fusionne les requêtes identiques du corpus).
        stop_event: Arrêt coopératif (même effet que l'échéance) : vérifié
            avant chaque requête et transmis à la politique HTTP comme signal
            d'annulation (attentes de politesse et de réessai interrompues,
            OPT-07) ; les notices non interrogées sont déclarées
            ``non_traite``.

    Returns:
        ``(corpus, rapport)``. Le corpus est une nouvelle liste dans l'ordre
        d'entrée ; les items résolus sont revalidés (un DOI posé sur un type
        Zotero qui n'accepte pas ce champ migre vers ``extra``, où la ligne
        ``DOI`` figure de toute façon). Le rapport détaille chaque notice sans
        DOI (``entries``) et les compteurs par statut, pour que la sélection
        reste contestable ; ``build_report_document`` en fait le fichier
        ``<stem>.doi_report.json``.

    Raises:
        DoiRuleError: paramètre qui assouplirait la règle ou hors bornes.
    """
    threshold, strict_threshold, year_tolerance, rows = check_rule_parameters(
        threshold, strict_threshold, year_tolerance, rows
    )
    workers = _as_int("workers", workers)
    if workers < 1:
        raise DoiRuleError(f"workers = {workers} invalide : au moins 1.")
    workers = min(workers, WORKERS_CEILING)
    if max_items is not None:
        max_items = _as_int("max_items", max_items)
        if max_items < 0:
            raise DoiRuleError(f"max_items = {max_items} invalide.")
    if max_seconds is not None:
        max_seconds = _as_number("max_seconds", max_seconds)
        if max_seconds <= 0:
            raise DoiRuleError(f"max_seconds = {max_seconds:g} invalide.")

    started = time.monotonic()
    deadline = started + max_seconds if max_seconds else None
    cache = query_cache if query_cache is not None else QueryCache()
    # Arrêt coopératif transmis à chaque requête (OPT-07).
    cancel = CancelSignal.coerce(stop_event)
    if session is None and http_client is not None:
        session = getattr(http_client, "session", None)

    report = _new_report(len(items))
    report["parameters"] = {
        "title_threshold": threshold,
        "strict_title_threshold": strict_threshold,
        "year_tolerance": year_tolerance,
        "rows": rows,
        "max_items": max_items,
        "max_seconds": max_seconds,
        "workers": workers,
        "min_interval_s": min_interval,
    }
    out: List[Dict] = list(items)
    entries: Dict[int, Dict[str, Any]] = {}

    # --- 1) Plan : notices à interroger, dans l'ordre du corpus ---
    selected: List[int] = []
    for idx, item in enumerate(items):
        if item_doi(item):
            report["already_had_doi"] += 1
            continue
        raw_doi = declared_doi_value(item)
        if raw_doi:
            # DOI déclaré mais non canonique : la notice est pourvue, sa
            # valeur n'est jamais écrasée ; elle est signalée pour vérification.
            report["already_had_doi"] += 1
            report["non_canonical_doi"] += 1
            report["non_canonical_dois"].append({
                "index": idx,
                "title": (item.get("title") or "").strip(),
                "value": raw_doi[:200],
            })
            continue
        title = (item.get("title") or "").strip()
        entry = {
            "index": idx,
            "title": title,
            "first_author": _first_author_lastname(item) or None,
            "year": extract_year(item.get("date", "")) or None,
            "status": None,
            "doi": None,
            "reason": "",
            "best_score": None,
            "candidates": [],
        }
        entries[idx] = entry
        if not title:
            entry.update(status=STATUS_NO_TITLE, reason="notice sans titre")
            continue
        if max_items is not None and len(selected) >= max_items:
            entry.update(status=STATUS_NOT_PROCESSED,
                         reason=f"plafond max_items={max_items} atteint")
            report["stopped_early"] = True
            report["stop_reason"] = report["stop_reason"] or "max_items"
            continue
        selected.append(idx)

    # Une requête par clé normalisée : les doublons d'un corpus la partagent.
    groups: "OrderedDict[Tuple, List[int]]" = OrderedDict()
    for idx in selected:
        item = items[idx]
        key = query_key(item["title"].strip(), _first_author_lastname(item),
                        rows)
        groups.setdefault(key, []).append(idx)

    # --- 2) Exécution : pool borné, budget en temps, arrêt propre ---
    halt = threading.Event()
    halt_reason: Dict[str, str] = {}
    counters_lock = threading.Lock()

    def _stop_reason() -> Optional[str]:
        if halt.is_set():
            return halt_reason.get("reason", "arrêt")
        if stop_event is not None and stop_event.is_set():
            return "arrêt demandé"
        if deadline is not None and time.monotonic() >= deadline:
            return f"budget de temps de {max_seconds:g} s épuisé"
        return None

    def _run(key: Tuple, idxs: List[int]) -> Dict[str, Any]:
        reason = _stop_reason()
        if reason:
            return {"state": "not_processed", "reason": reason}
        cached = cache.get(key)
        if cached is not None:
            with counters_lock:
                report["query_cache_hits"] += 1
            return {"state": "ok", "candidates": cached, "cached": True}
        item = items[idxs[0]]
        # Reste du budget, transmis à la politique HTTP : il borne l'attente
        # d'une place en vol et du créneau de politesse, chaque tentative et
        # les réessais ; aucune tentative ne part après l'échéance.
        budget: Optional[float] = None
        if deadline is not None:
            budget = deadline - time.monotonic()
            if budget <= 0:
                return {"state": "not_processed",
                        "reason": f"budget de temps de {max_seconds:g} s "
                                  f"épuisé"}
        with counters_lock:
            report["queries_sent"] += 1
        extra = {"cancel": cancel} if cancel is not None else {}
        try:
            candidates, error = query_crossref_detailed(
                item["title"].strip(),
                author_lastname=_first_author_lastname(item), rows=rows,
                session=session, min_interval=min_interval,
                timeout=30, max_retries=3, budget=budget, **extra,
            )
        except OperationCancelled:
            # Arrêt demandé pendant l'attente ou l'envoi : la notice n'est
            # pas examinée (jamais un faux « sans candidat »).
            return {"state": "not_processed", "reason": "arrêt demandé"}
        except BudgetExhausted as e:
            logger.error(f"[DOI] Arrêt de la résolution — {e}")
            halt_reason.setdefault("reason", "quota CrossRef épuisé")
            halt.set()
            return {"state": "error", "reason": "quota CrossRef épuisé"}
        if error is not None:
            if (deadline is not None
                    and time.monotonic() >= deadline - _DEADLINE_SLACK_S):
                # Interrompue (ou jamais partie) faute de budget : la notice
                # n'est pas examinée, le service n'est pas en cause.
                return {"state": "not_processed",
                        "reason": f"budget de temps de {max_seconds:g} s "
                                  f"épuisé (requête interrompue : {error})"}
            return {"state": "error", "reason": error}
        cache.put(key, candidates)
        return {"state": "ok", "candidates": candidates, "cached": False}

    results: Dict[Tuple, Dict[str, Any]] = {}
    if workers == 1 or len(groups) <= 1:
        for key, idxs in groups.items():
            results[key] = _run(key, idxs)
    else:
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="doi-resolve") as pool:
            futures = {key: pool.submit(_run, key, idxs)
                       for key, idxs in groups.items()}
            for key, fut in futures.items():
                try:
                    results[key] = fut.result()
                except Exception as e:  # noqa: BLE001 - garde-fou
                    results[key] = {"state": "error",
                                    "reason": type(e).__name__}

    # --- 3) Décisions, dans l'ordre du corpus (déterministes) ---
    for key, idxs in groups.items():
        res = results[key]
        for idx in idxs:
            item = items[idx]
            entry = entries[idx]
            title = entry["title"]
            if res["state"] == "not_processed":
                entry.update(status=STATUS_NOT_PROCESSED, reason=res["reason"])
                report["stopped_early"] = True
                if report["stop_reason"] in (None, "max_items"):
                    report["stop_reason"] = res["reason"]
                continue
            report["attempted"] += 1
            if res["state"] == "error":
                entry.update(status=STATUS_ERROR,
                             reason=f"erreur réseau ou de service : "
                                    f"{res['reason']}")
                report["error_list"].append((title, res["reason"]))
                continue
            decision = decide(item, res["candidates"], threshold,
                              strict_threshold, year_tolerance)
            entry.update(
                status=decision["status"], reason=decision["reason"],
                best_score=decision["best_score"],
                candidates=decision["candidates"],
                query_cached=bool(res.get("cached")),
            )
            if decision["status"] == STATUS_NO_CANDIDATE:
                continue
            if decision["status"] == STATUS_REJECTED:
                report["rejections"].append(
                    (title, decision.get("best_doi") or "",
                     decision["reason"]))
                continue
            if decision["status"] == STATUS_AMBIGUOUS:
                report["ambiguities"].append(
                    (title, decision.get("ambiguous_dois") or [],
                     decision["reason"]))
                continue
            doi = decision["doi"]
            entry["doi"] = doi
            entry["doi_type"] = decision.get("doi_type")
            enriched = dict(item)
            append_to_extra(enriched, "DOI", doi)
            type_note = (f" ; type CrossRef {decision['doi_type']}"
                         if decision.get("doi_type") else "")
            append_to_extra(
                enriched, "DOI source",
                f"appariement bibliographique CrossRef "
                f"({decision['reason']}{type_note} ; règles {RULES_VERSION})",
            )
            enriched["DOI"] = doi
            out[idx] = validate_and_clean(enriched)
            report["resolutions"].append((title, doi, decision["reason"]))

    # --- 4) Compteurs ---
    ordered = [entries[i] for i in sorted(entries)]
    report["entries"] = ordered
    status_counts = {s: 0 for s in ALL_STATUSES}
    for entry in ordered:
        status_counts[entry["status"]] += 1
    report["status_counts"] = status_counts
    report["resolved"] = status_counts[STATUS_ACCEPTED]
    report["rejected"] = status_counts[STATUS_REJECTED]
    report["ambiguous"] = status_counts[STATUS_AMBIGUOUS]
    report["no_candidate"] = status_counts[STATUS_NO_CANDIDATE]
    report["errors"] = status_counts[STATUS_ERROR]
    report["not_processed"] = status_counts[STATUS_NOT_PROCESSED]
    report["skipped_no_title"] = status_counts[STATUS_NO_TITLE]
    rate = (
        100 * report["resolved"] / report["attempted"]
        if report["attempted"] else 0.0
    )
    report["resolution_rate_pct"] = round(rate, 1)
    report["duration_s"] = round(time.monotonic() - started, 3)
    logger.info(
        f"[DOI] {report['resolved']} résolus sur {report['attempted']} tentés "
        f"({rate:.1f} %), {report['rejected']} rejetés, "
        f"{report['ambiguous']} ambigus, {report['no_candidate']} sans "
        f"candidat, {report['errors']} en erreur, {report['not_processed']} "
        f"non traités, {report['already_had_doi']} déjà pourvus"
    )
    return out, report


def build_report_document(
    report: Dict[str, Any],
    source_corpus: Optional[str] = None,
    output_corpus: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Document JSON du fichier associé ``<stem>.doi_report.json``.

    Ne contient **aucun chemin disque** (le fichier est téléchargeable en
    HTTP) : les corpus sont désignés par leur seul nom de fichier.

    Le taux d'enrichissement (DOI acceptés / notices interrogées) est publié
    séparément de la précision, qui ne se mesure pas ici : elle se mesure sur
    un jeu témoin (cas négatifs), les ambiguïtés et abstentions étant le prix
    payé pour elle.
    """
    counts = dict(report.get("status_counts") or {})
    counts["deja_pourvu"] = report.get("already_had_doi", 0)
    counts["deja_pourvu_doi_non_canonique"] = report.get("non_canonical_doi", 0)
    counts["total"] = report.get("total", 0)
    counts["interroges"] = report.get("attempted", 0)
    without_doi = report.get("total", 0) - report.get("already_had_doi", 0)
    attempted = report.get("attempted", 0)
    resolved = report.get("resolved", 0)
    params = dict(report.get("parameters") or {})
    return {
        "format": REPORT_FORMAT,
        "format_version": REPORT_FORMAT_VERSION,
        "rules_version": report.get("rules_version", RULES_VERSION),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_corpus": Path(source_corpus).name if source_corpus else None,
        "output_corpus": Path(output_corpus).name if output_corpus else None,
        "parameters": params,
        "floors": {
            "title_threshold_min": TITLE_THRESHOLD_FLOOR,
            "strict_title_threshold_min": STRICT_THRESHOLD_FLOOR,
            "year_tolerance_max": YEAR_TOLERANCE_CEILING,
            "rows_min": ROWS_FLOOR,
            "rows_max": ROWS_CEILING,
        },
        "rules": [
            "similarité de titre >= title_threshold",
            "année à year_tolerance près si les deux années sont connues",
            "premier auteur présent parmi les auteurs du candidat si connus",
            "sans corroboration : titre >= strict_title_threshold",
            "préfixe de titre jamais accepté sans corroboration, arrêté à une "
            "frontière de mot",
            "document dérivé (erratum, rétractation, compte rendu, réponse, "
            "introduction ou éditorial) jamais assimilé au travail visé",
            "numéros de titre discordants (tome, partie, volume, chiffre "
            "final) : travaux distincts",
            "prépublication (posted-content) non assimilée à une notice de "
            "type publié, ni une version publiée à une notice preprint",
            "types CrossRef non autonomes (peer-review, component, "
            "journal-issue…) jamais retenus",
            "plusieurs travaux distincts admissibles : ambigu, aucun DOI écrit",
            "DOI déclaré non canonique : notice considérée pourvue, valeur "
            "conservée",
        ],
        "counts": counts,
        "rates": {
            "enrichment_pct_of_attempted": round(
                100 * resolved / attempted, 1) if attempted else 0.0,
            "enrichment_pct_of_missing": round(
                100 * resolved / without_doi, 1) if without_doi else 0.0,
            "precision": "non mesurée ici (jeu témoin de cas négatifs)",
        },
        "queries_sent": report.get("queries_sent", 0),
        "query_cache_hits": report.get("query_cache_hits", 0),
        "duration_s": report.get("duration_s"),
        "stopped_early": report.get("stopped_early", False),
        "stop_reason": report.get("stop_reason"),
        "non_canonical_dois": report.get("non_canonical_dois", []),
        "entries": report.get("entries", []),
    }


if __name__ == "__main__":  # pragma: no cover
    import json
    import sys

    logging.basicConfig(level=logging.INFO)
    corpus = json.loads(open(sys.argv[1], encoding="utf-8").read())
    enriched, rep = resolve_dois(corpus)
    doc = build_report_document(rep, source_corpus=sys.argv[1])
    print(json.dumps({k: v for k, v in doc.items() if k != "entries"},
                     indent=2, ensure_ascii=False))
