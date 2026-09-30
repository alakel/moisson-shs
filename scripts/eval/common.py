"""
Outils communs du protocole d'évaluation SHS (OPT-15).

Ce module ne fait aucun accès réseau et n'importe ni les connecteurs ni
``utils.secrets`` : il peut être importé avant la garde d'environnement de
``pool.py`` (aucune clé chargée implicitement).

Contenu :

- chemins (``EVAL_ROOT`` = ``output/eval``, seul répertoire du projet où le
  protocole écrit) et garde d'écriture ;
- lecture et validation du fichier de requêtes (``queries_shs.json``) ;
- identifiant stable de candidat (``candidate_ids``), calculé sur le contenu
  bibliographique de la notice fusionnée : il ne dit rien de la source, du
  score ni du rang ;
- champs d'affichage des fiches de jugement (titre, auteurs, année, support,
  type, résumé tronqué, lien neutre) et catégories d'équilibre (langue, type,
  ancienneté) ;
- lecture et écriture JSON atomiques.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import quote_plus, urlsplit

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "scripts"
for _p in (str(ROOT), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from utils.cleaner import extract_year  # noqa: E402
from utils.identity import (  # noqa: E402
    extract_doi, item_urls, normalize_doi, normalize_hal_id, strong_keys,
)

# === Chemins ===

EVAL_ROOT = ROOT / "output" / "eval"
QUERIES_FILE = Path(__file__).resolve().parent / "queries_shs.json"

SNAPSHOTS = "snapshots"
POOLS = "pools"
SHEETS = "fiches"
REPORTS = "rapports"
DOI_DIR = "doi"

SET_TUNING = "reglage"
SET_DECISION = "decision"
SETS = (SET_TUNING, SET_DECISION)

# Groupes de sources admis : jamais Google Scholar (source facturée, exclue
# des groupes « francophone » et « all » par le registre).
ALLOWED_SOURCE_GROUPS = ("francophone", "all")
FORBIDDEN_SOURCES = ("google_scholar", "scholar")

SCALE = {0: "hors sujet", 1: "périphérique", 2: "utile", 3: "central"}

MIN_QUERIES = 24
MIN_TUNING = 8
MIN_DECISION = 16

CATEGORIES = (
    "theme_large", "theme_rare", "expression_exacte", "auteur_homonyme",
    "concept_fr_en", "livre", "chapitre", "article", "these",
    "periode_restrictive",
)


class EvalError(RuntimeError):
    """Erreur d'usage du protocole (fichier invalide, écriture refusée…)."""


def today() -> str:
    return date.today().isoformat()


def run_dir(day: Optional[str] = None, root: Optional[Path] = None) -> Path:
    """Répertoire d'une campagne : ``<root>/<AAAA-MM-JJ>`` (défaut ``output/eval``)."""
    base = Path(root) if root is not None else EVAL_ROOT
    return base / (day or today())


def ensure_writable(path: Path) -> Path:
    """
    Garde d'écriture : dans le dépôt, le protocole n'écrit que sous
    ``output/eval/`` ; hors du dépôt (répertoire temporaire de test, dossier
    choisi par l'utilisateur), l'écriture est libre. Pose un ``.gitignore``
    (« * ») à la racine de ``output/eval`` : les données de campagne restent
    locales.
    """
    path = Path(path).resolve()
    root = ROOT.resolve()
    inside_repo = path == root or root in path.parents
    eval_root = EVAL_ROOT.resolve()
    if inside_repo and not (path == eval_root or eval_root in path.parents):
        raise EvalError(
            f"écriture refusée hors de output/eval/ : {path} "
            f"(le protocole n'écrit que sous {eval_root})")
    if inside_repo:
        eval_root.mkdir(parents=True, exist_ok=True)
        marker = eval_root / ".gitignore"
        if not marker.exists():
            marker.write_text(
                "# Données locales du protocole d'évaluation (OPT-15)\n*\n",
                encoding="utf-8")
    return path


def write_json(path: Path, data: Any) -> Path:
    """Écriture atomique (fichier temporaire puis renommage)."""
    path = ensure_writable(Path(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def write_text(path: Path, text: str, *, bom: bool = False) -> Path:
    """Écriture atomique d'un texte UTF-8 (BOM optionnel pour les tableurs)."""
    path = ensure_writable(Path(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig" if bom else "utf-8",
                       newline="") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# === Requêtes ===

def ref_keys(ref: Dict[str, Any]) -> List[str]:
    """
    Clés fortes d'une référence connue (même forme que
    ``utils.identity.strong_keys`` : ``doi:…``, ``hal:…``). Champs admis :
    ``doi``, ``hal``, ``ids`` (liste de clés préfixées, alternatives dont une
    seule suffit : éditions, dépôts multiples).
    """
    keys: List[str] = []

    def _add(kind: str, value: Any) -> None:
        if not value:
            return
        if kind == "doi":
            norm = normalize_doi(str(value))
        elif kind == "hal":
            norm = normalize_hal_id(str(value))
        else:
            norm = None
        if not norm:
            raise EvalError(f"identifiant invalide dans une référence : {kind}:{value}")
        key = f"{kind}:{norm}"
        if key not in keys:
            keys.append(key)

    _add("doi", ref.get("doi"))
    _add("hal", ref.get("hal"))
    for raw in ref.get("ids") or []:
        kind, _, value = str(raw).partition(":")
        _add(kind.strip().lower(), value.strip())
    return keys


def validate_queries(doc: Dict[str, Any]) -> List[str]:
    """Erreurs du fichier de requêtes (liste vide : fichier conforme)."""
    errors: List[str] = []
    queries = doc.get("queries")
    if not isinstance(queries, list):
        return ["clé « queries » absente ou invalide"]
    seen = set()
    counts = {s: 0 for s in SETS}
    for q in queries:
        qid = q.get("id")
        where = f"requête {qid!r}"
        if not qid or not re.fullmatch(r"[A-Z]\d{2}", str(qid)):
            errors.append(f"{where} : identifiant attendu de la forme R01 / D01")
        if qid in seen:
            errors.append(f"{where} : identifiant en double")
        seen.add(qid)
        if q.get("set") not in SETS:
            errors.append(f"{where} : jeu inconnu {q.get('set')!r}")
        else:
            counts[q["set"]] += 1
        if not str(q.get("query") or "").strip():
            errors.append(f"{where} : requête vide")
        if q.get("sources") not in ALLOWED_SOURCE_GROUPS:
            errors.append(f"{where} : groupe de sources {q.get('sources')!r} "
                          f"non admis ({', '.join(ALLOWED_SOURCE_GROUPS)})")
        cats = q.get("categories") or []
        for cat in cats:
            if cat not in CATEGORIES:
                errors.append(f"{where} : catégorie inconnue {cat!r}")
        if not cats:
            errors.append(f"{where} : aucune catégorie")
        if "concept_fr_en" in cats and not (q.get("query_variants") or {}).get("en"):
            errors.append(f"{where} : concept FR/EN sans variante anglaise explicite")
        if len(str(q.get("besoin") or "")) < 80:
            errors.append(f"{where} : besoin d'information absent ou trop court")
        refs = q.get("references_connues")
        if not isinstance(refs, list):
            errors.append(f"{where} : « references_connues » doit être une liste")
            refs = []
        if not refs and not str(q.get("references_note") or "").strip():
            errors.append(f"{where} : liste de références vide non signalée "
                          f"(« references_note »)")
        for ref in refs:
            try:
                if not ref_keys(ref):
                    errors.append(f"{where} : référence sans identifiant ({ref.get('titre')!r})")
            except EvalError as exc:
                errors.append(f"{where} : {exc}")
            if not ref.get("verification"):
                errors.append(f"{where} : référence sans mention de vérification "
                              f"({ref.get('titre')!r})")
        for bound in ("from_date", "to_date"):
            value = q.get(bound)
            if value is not None and not re.fullmatch(r"\d{4}(-\d{2}-\d{2})?", str(value)):
                errors.append(f"{where} : {bound} invalide {value!r}")
    if len(queries) < MIN_QUERIES:
        errors.append(f"{len(queries)} requêtes (au moins {MIN_QUERIES} attendues)")
    if counts[SET_TUNING] < MIN_TUNING:
        errors.append(f"{counts[SET_TUNING]} requêtes de réglage (au moins {MIN_TUNING})")
    if counts[SET_DECISION] < MIN_DECISION:
        errors.append(f"{counts[SET_DECISION]} requêtes de décision (au moins {MIN_DECISION})")
    return errors


def load_queries(path: Optional[Path] = None, *, strict: bool = True) -> Dict[str, Any]:
    """
    Fichier de requêtes, validé (``EvalError`` s'il n'est pas conforme et que
    ``strict``). Chaque référence reçoit ``keys`` (clés fortes normalisées).
    """
    doc = read_json(Path(path) if path else QUERIES_FILE)
    errors = validate_queries(doc)
    if errors and strict:
        raise EvalError("fichier de requêtes invalide :\n  - " + "\n  - ".join(errors))
    for q in doc.get("queries", []):
        for ref in q.get("references_connues") or []:
            try:
                ref["keys"] = ref_keys(ref)
            except EvalError:
                ref["keys"] = []
    return doc


def select_queries(doc: Dict[str, Any], ids: Optional[Iterable[str]] = None,
                   subset: Optional[str] = None) -> List[Dict[str, Any]]:
    """Requêtes retenues par identifiants et/ou par jeu, dans l'ordre du fichier."""
    wanted = {i.strip().upper() for i in ids} if ids else None
    out = []
    for q in doc.get("queries", []):
        if wanted is not None and q["id"].upper() not in wanted:
            continue
        if subset and q.get("set") != subset:
            continue
        out.append(q)
    if wanted:
        missing = wanted - {q["id"].upper() for q in out}
        if missing:
            raise EvalError(f"requête(s) inconnue(s) : {', '.join(sorted(missing))}")
    return out


# === Identifiant de candidat ===

_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)


def _fold(text: Any) -> str:
    from unidecode import unidecode
    return _WORD_RE.sub(" ", unidecode(str(text or "")).lower()).strip()


def first_author_lastname(item: Dict[str, Any]) -> str:
    for c in item.get("creators") or []:
        if not isinstance(c, dict):
            continue
        name = c.get("lastName") or c.get("name") or ""
        if name:
            return _fold(name)
    return ""


def year_of(item: Dict[str, Any]) -> Optional[int]:
    y = extract_year(str(item.get("date") or ""))
    try:
        return int(y) if y else None
    except (TypeError, ValueError):
        return None


def canonical_dump(item: Dict[str, Any]) -> str:
    return json.dumps(item, ensure_ascii=False, sort_keys=True)


def candidate_ids(items: Sequence[Dict[str, Any]]) -> List[str]:
    """
    Identifiants stables des notices fusionnées, alignés sur ``items``.

    Empreinte du contenu bibliographique (clés fortes, titre replié, année,
    nom du premier auteur, type) : le même corpus fusionné redonne les mêmes
    identifiants, quel que soit l'ordre des notices ou le classeur. Deux
    notices d'empreinte identique (versions tenues à part) sont départagées
    par leur contenu complet (suffixe ``-2``, ``-3``…), jamais par leur
    position. L'identifiant ne porte ni source, ni score, ni rang.
    """
    bases = []
    for item in items:
        basis = json.dumps([sorted(strong_keys(item)), _fold(item.get("title")),
                            year_of(item), first_author_lastname(item),
                            item.get("itemType") or ""], ensure_ascii=False)
        bases.append("c" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:10])
    groups: Dict[str, List[int]] = {}
    for idx, base in enumerate(bases):
        groups.setdefault(base, []).append(idx)
    out = list(bases)
    for base, members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda i: canonical_dump(items[i]))
        for n, idx in enumerate(members):
            out[idx] = base if n == 0 else f"{base}-{n + 1}"
    return out


# === Champs d'affichage (fiches en aveugle) ===

TYPE_LABELS = {
    "journalArticle": "Article de revue",
    "magazineArticle": "Article de magazine",
    "newspaperArticle": "Article de presse",
    "book": "Livre",
    "bookSection": "Chapitre",
    "thesis": "Thèse",
    "conferencePaper": "Communication",
    "presentation": "Présentation",
    "report": "Rapport",
    "preprint": "Prépublication",
    "encyclopediaArticle": "Article d'encyclopédie",
    "dictionaryEntry": "Entrée de dictionnaire",
    "webpage": "Page web",
    "blogPost": "Billet de blog",
    "manuscript": "Manuscrit",
    "dataset": "Jeu de données",
    "document": "Document",
}

TYPE_CATEGORIES = {
    "journalArticle": "article", "magazineArticle": "article",
    "newspaperArticle": "article",
    "book": "livre",
    "bookSection": "chapitre", "encyclopediaArticle": "chapitre",
    "dictionaryEntry": "chapitre",
    "thesis": "these",
    "conferencePaper": "communication", "presentation": "communication",
    "report": "rapport",
    "preprint": "prepublication",
}

SUPPORT_FIELDS = ("publicationTitle", "bookTitle", "proceedingsTitle",
                  "conferenceName", "university", "institution",
                  "websiteTitle", "blogTitle", "repository", "publisher",
                  "series")

# Hôtes de moteurs de découverte : un lien vers eux révélerait la source de
# la notice (Isidore, Semantic Scholar, OpenAlex, CORE, CrossRef, Scholar).
# Les dépôts et éditeurs (HAL, OpenEdition, Cairn, Persée, Érudit,
# theses.fr…) sont l'adresse du document lui-même et restent affichés.
AGGREGATOR_HOSTS = (
    "isidore.science", "semanticscholar.org", "openalex.org", "core.ac.uk",
    "api.crossref.org", "search.crossref.org", "scholar.google",
    "serpapi.com",
)

NEUTRAL_SEARCH = "https://duckduckgo.com/?q="

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def clean_text(value: Any) -> str:
    """Texte d'affichage : balises retirées, entités décodées, espaces réduits."""
    text = html.unescape(_TAG_RE.sub(" ", str(value or "")))
    return _SPACE_RE.sub(" ", text).strip()


def authors_text(item: Dict[str, Any], limit: int = 6) -> str:
    names = []
    for c in item.get("creators") or []:
        if not isinstance(c, dict):
            continue
        if c.get("name"):
            name = str(c["name"]).strip()
        else:
            name = " ".join(p for p in (str(c.get("firstName") or "").strip(),
                                        str(c.get("lastName") or "").strip()) if p)
        if name:
            names.append(name)
    if len(names) > limit:
        return ", ".join(names[:limit]) + f" et al. ({len(names)} auteurs)"
    return ", ".join(names)


def support_of(item: Dict[str, Any]) -> str:
    for field in SUPPORT_FIELDS:
        value = clean_text(item.get(field))
        if value:
            return value
    return ""


def type_label(item: Dict[str, Any]) -> str:
    it = item.get("itemType") or ""
    return TYPE_LABELS.get(it, it or "Document")


def type_category(item: Dict[str, Any]) -> str:
    return TYPE_CATEGORIES.get(item.get("itemType") or "", "autre")


def abstract_excerpt(text: Any, limit: int = 700) -> str:
    """
    Résumé tronqué proprement : jamais au milieu d'un mot ; à la fin d'une
    phrase si elle tombe dans le dernier tiers de la limite ; « […] » signale
    la coupe.
    """
    text = clean_text(text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sentence_end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if sentence_end >= int(limit * 2 / 3):
        return cut[:sentence_end + 1] + " […]"
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    return cut.rstrip(" ,;:-–—") + " […]"


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def is_aggregator_url(url: str) -> bool:
    host = _host(url)
    return any(host == h or host.endswith("." + h) or h in host
               for h in AGGREGATOR_HOSTS)


def neutral_link(item: Dict[str, Any]) -> str:
    """
    Lien d'accès qui ne révèle pas la source de la notice : DOI d'abord
    (``https://doi.org/…``), sinon URL du dépôt ou de l'éditeur ; si seules
    des URL de moteurs de découverte existent, lien de recherche web sur le
    titre (le juge retrouve le document sans apprendre d'où vient la notice).
    """
    doi = extract_doi(item)
    if doi:
        return f"https://doi.org/{doi}"
    for url in item_urls(item):
        if url.startswith(("http://", "https://")) and not is_aggregator_url(url):
            return url
    title = clean_text(item.get("title"))
    if not title:
        return ""
    author = first_author_lastname(item)
    query = f"\"{title}\"" + (f" {author}" if author else "")
    return NEUTRAL_SEARCH + quote_plus(query)


def display_fields(item: Dict[str, Any], abstract_chars: int = 700) -> Dict[str, Any]:
    """Champs montrés au juge : aucune source, aucun score, aucun rang."""
    year = year_of(item)
    return {
        "titre": clean_text(item.get("title")) or "(sans titre)",
        "auteurs": authors_text(item),
        "annee": str(year) if year else "",
        "type": type_label(item),
        "support": support_of(item),
        "resume": abstract_excerpt(item.get("abstractNote"), abstract_chars),
        "lien": neutral_link(item),
    }


# === Catégories d'équilibre ===

def language_bucket(item: Dict[str, Any]) -> str:
    """``fr``, ``en``, ``multilingue`` (fr et en), ``autre`` ou ``inconnue``."""
    from utils.query_plan import item_languages
    langs = item_languages(item.get("language"))
    if not langs:
        return "inconnue"
    has_fr, has_en = "fr" in langs, "en" in langs
    if has_fr and has_en:
        return "multilingue"
    if has_fr:
        return "fr"
    if has_en:
        return "en"
    return "autre"


AGE_BUCKETS = ("avant 2000", "2000-2009", "2010-2019", "2020 et après", "inconnue")


def age_bucket(year: Optional[int]) -> str:
    if not year:
        return "inconnue"
    if year < 2000:
        return "avant 2000"
    if year < 2010:
        return "2000-2009"
    if year < 2020:
        return "2010-2019"
    return "2020 et après"


def balance_fields(item: Dict[str, Any]) -> Dict[str, str]:
    return {"langue": language_bucket(item),
            "type": type_category(item),
            "anciennete": age_bucket(year_of(item))}


# === Divers ===

def mean(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    return sum(vals) / len(vals) if vals else None


def fmt(value: Optional[float], digits: int = 3) -> str:
    return "–" if value is None else f"{value:.{digits}f}"


def pct(value: Optional[float], digits: int = 1) -> str:
    return "–" if value is None else f"{100 * value:.{digits}f} %"


def parse_id_list(value: Optional[str]) -> Optional[List[str]]:
    if not value:
        return None
    return [v.strip() for v in value.replace(";", ",").split(",") if v.strip()]
