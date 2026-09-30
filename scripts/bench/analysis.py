"""
Analyse des sorties d'une mesure : corpus produit, doublons, retour MCP.

Les fonctions de ce module ne dépendent PAS du code du moissonneur (elles
normalisent elles-mêmes titres et DOI) : une refonte de la déduplication
(OPT-06) ne change donc pas la règle de mesure, seulement le résultat mesuré.

Deux mesures de doublons résiduels :

- **titre normalisé + année** (applicable aussi en live) : groupes de notices
  du corpus final partageant le même titre normalisé (minuscules, sans
  accents ni ponctuation) et la même année. C'est la mesure du constat C18
  (645 doublons résiduels sur 209 corpus réels) ;
- **vérité terrain** (scénarios simulés) : chaque notice synthétique porte,
  dans ses identifiants natifs (DOI, URI Isidore, identifiant HAL, OpenAlex,
  Semantic Scholar, CORE), le numéro du travail dont elle est une
  manifestation. Une notice finale qui référence deux travaux distincts est
  une **fusion erronée** ; un travail présent dans deux notices finales est un
  **doublon résiduel**.

Couverture (scénarios simulés) : l'ensemble des travaux renvoyés par chaque
source et celui du corpus final sont conservés (``ids_record``) pour que
``--compare`` vérifie une « couverture identique » par identifiants, et pas
seulement par volumes (un échange de notices à nombre constant se voit).
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

# Suffixes des fichiers associés à un corpus (conventions du chantier) : ils
# ne sont jamais pris pour le corpus Zotero interne.
_ASSOCIATED_SUFFIXES = (".csl.json", ".manifest.json", ".doi_report.json")

_YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")

# Marqueurs de vérité terrain (cf. bench.synth) : le numéro de travail est
# lisible dans chaque identifiant natif synthétique.
GT_PATTERNS = (
    (re.compile(r"simw\.?(\d+)", re.IGNORECASE), 0),       # DOI, URI Isidore, URL
    (re.compile(r"hal-4(\d{7})\b"), 0),                     # identifiant HAL
    (re.compile(r"W888(\d{7})\b"), 0),                      # OpenAlex
    (re.compile(r"\b777(\d{7})\b"), 0),                     # S2 CorpusId
    (re.compile(r"\b999(\d{7})\b"), 0),                     # CORE ID
)


def normalize_title(title: str) -> str:
    """Titre normalisé : sans accents, minuscules, alphanumérique seul."""
    if not title:
        return ""
    txt = unicodedata.normalize("NFKD", str(title))
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    txt = re.sub(r"[^0-9a-zA-Z]+", " ", txt).lower()
    return " ".join(txt.split())


def item_year(item: Dict) -> str:
    m = _YEAR_RE.search(str(item.get("date", "") or ""))
    return m.group(1) if m else ""


def item_doi(item: Dict) -> str:
    """DOI du champ, ou à défaut d'une ligne ``DOI:`` de ``extra``."""
    doi = str(item.get("DOI", "") or "").strip()
    if not doi:
        for line in str(item.get("extra", "") or "").split("\n"):
            if line.strip().lower().startswith("doi:"):
                doi = line.split(":", 1)[1].strip()
                break
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi, flags=re.I)
    return doi.lower().strip()


def find_corpus_file(output_dir: Path, structured: Optional[Dict] = None) -> Optional[Path]:
    """
    Retrouve le corpus Zotero interne produit par la mesure.

    Priorité au chemin annoncé par le retour MCP (``structured['files']``),
    sinon le plus récent ``*.json`` du répertoire de sortie qui n'est ni un
    fichier associé (CSL, manifeste, rapport DOI), ni un fichier de service
    (préfixe ``.``), ni un payload de ``raw/``.
    """
    if structured and isinstance(structured.get("files"), dict):
        for name, path in structured["files"].items():
            if name.endswith(".json") and not name.endswith(_ASSOCIATED_SUFFIXES):
                p = Path(path)
                if p.is_file():
                    return p
    candidates = [
        p for p in output_dir.glob("*.json")
        if not p.name.startswith(".")
        and not p.name.endswith(_ASSOCIATED_SUFFIXES)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def gt_works(item: Dict) -> Set[int]:
    """Numéros de travaux synthétiques référencés par une notice."""
    text = json.dumps(item, ensure_ascii=False)
    found: Set[int] = set()
    for pattern, offset in GT_PATTERNS:
        for m in pattern.finditer(text):
            found.add(int(m.group(1)) + offset)
    return found


def work_ids(items: Iterable[Dict]) -> Set[int]:
    """Ensemble des travaux (vérité terrain) cités par une liste de notices."""
    out: Set[int] = set()
    for it in items or []:
        if isinstance(it, dict):
            out |= gt_works(it)
    return out


# --- Encodage compact et stable d'un ensemble de numéros de travaux ---
# Numéros triés, écarts successifs en base 36, séparés par des virgules :
# lisible, déterministe (diff propre dans un fichier versionné), ~3 octets
# par travail sur les univers du banc.

_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _to36(n: int) -> str:
    if n == 0:
        return "0"
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(_B36[r])
    return "".join(reversed(out))


def encode_ids(ids: Iterable[int]) -> str:
    """Encode un ensemble d'entiers positifs (cf. ``decode_ids``)."""
    prev = 0
    out = []
    for n in sorted({int(i) for i in ids}):
        out.append(_to36(n - prev))
        prev = n
    return ",".join(out)


def decode_ids(text: Optional[str]) -> List[int]:
    """Décode la sortie de ``encode_ids`` (liste triée)."""
    if not text:
        return []
    cur = 0
    out = []
    for tok in text.split(","):
        cur += int(tok, 36)
        out.append(cur)
    return out


def ids_record(ids: Iterable[int]) -> Dict[str, Any]:
    """Ensemble de travaux sérialisable : effectif, empreinte, liste encodée."""
    uniq = {int(i) for i in ids}
    enc = encode_ids(uniq)
    return {
        "count": len(uniq),
        "digest": hashlib.sha256(enc.encode("ascii")).hexdigest()[:16],
        "ids": enc,
    }


def gt_summary(items: Iterable[Dict]) -> Dict[str, Any]:
    """Doublons résiduels et fusions erronées d'après la vérité terrain."""
    items = list(items)
    work_items: Dict[int, int] = Counter()
    false_merges = 0
    unmarked = 0
    for it in items:
        works = gt_works(it)
        if not works:
            unmarked += 1
            continue
        if len(works) > 1:
            false_merges += 1
        for w in works:
            work_items[w] += 1
    residual = sum(n - 1 for n in work_items.values() if n > 1)
    return {
        "items": len(items),
        "works": len(work_items),
        "residual_duplicates": residual,
        "false_merges": false_merges,
        "unmarked_items": unmarked,
    }


def corpus_summary(items: List[Dict]) -> Dict[str, Any]:
    """Statistiques indépendantes du code mesuré sur le corpus final."""
    groups: Dict[tuple, int] = Counter()
    for it in items:
        t = normalize_title(it.get("title", ""))
        if t:
            groups[(t, item_year(it))] += 1
    dup_groups = [n for n in groups.values() if n > 1]
    residual = sum(n - 1 for n in dup_groups)
    with_doi = sum(1 for it in items if item_doi(it))
    by_type = Counter(it.get("itemType", "?") for it in items)
    sources = Counter()
    for it in items:
        for line in str(it.get("extra", "") or "").split("\n"):
            if line.startswith("Sources:"):
                for s in line[len("Sources:"):].split(","):
                    if s.strip():
                        sources[s.strip()] += 1
                break
    return {
        "items": len(items),
        "with_doi": with_doi,
        "doi_pct": round(100.0 * with_doi / max(len(items), 1), 1),
        "residual_title_year": residual,
        "residual_title_year_groups": len(dup_groups),
        "residual_title_year_pct": round(100.0 * residual / max(len(items), 1), 2),
        "by_type": dict(by_type.most_common()),
        "by_source_label": dict(sources.most_common()),
    }


# === Taille du retour MCP ===

def _block_to_dict(block: Any) -> Any:
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json", by_alias=True, exclude_none=True)
    return block


def measure_tool_result(result: Any) -> Dict[str, Any]:
    """
    Taille, en octets UTF-8, du retour d'un outil producteur.

    Accepte les trois formes du dispatcher : ``str`` (legacy), liste de
    ``ContentBlock``, tuple ``(blocs, structuredContent)``. Le total
    additionne la sérialisation JSON de chaque bloc et du contenu structuré,
    soit l'ordre de grandeur de ce qui entre dans le contexte de l'agent.
    """
    if isinstance(result, str):
        n = len(result.encode("utf-8"))
        # ToolError (sous-classe de str, OPT-07) : erreur d'outil (isError).
        is_error = True if type(result).__name__ == "ToolError" else None
        return {"kind": "str", "total_bytes": n, "text_bytes": n,
                "embedded_bytes": 0, "structured_bytes": 0, "blocks": {},
                "is_error": is_error}
    structured = None
    is_error = None
    if hasattr(result, "content") and hasattr(result, "isError"):
        # CallToolResult (erreur d'outil, OPT-07 ; ou retour du dispatcher).
        blocks, structured = result.content, result.structuredContent
        is_error = bool(result.isError)
    elif isinstance(result, tuple) and len(result) == 2:
        blocks, structured = result
        # ToolErrorContent (sous-classe de tuple, OPT-07) : erreur d'outil.
        is_error = type(result).__name__ == "ToolErrorContent"
    elif isinstance(result, list):
        blocks = result
        is_error = False
    else:
        return {"kind": type(result).__name__, "total_bytes": None}

    counts: Counter = Counter()
    total = 0
    text_bytes = 0
    embedded_bytes = 0
    for block in blocks or []:
        d = _block_to_dict(block)
        s = json.dumps(d, ensure_ascii=False).encode("utf-8")
        total += len(s)
        btype = d.get("type", "?") if isinstance(d, dict) else "?"
        counts[btype] += 1
        if btype == "text":
            text_bytes += len(str(d.get("text", "")).encode("utf-8"))
        elif btype == "resource":
            res = d.get("resource") or {}
            embedded_bytes += len(str(res.get("text", "") or "").encode("utf-8"))
    structured_bytes = 0
    if structured is not None:
        structured_bytes = len(
            json.dumps(structured, ensure_ascii=False, default=str).encode("utf-8")
        )
        total += structured_bytes
    return {
        "kind": "tuple" if structured is not None else "blocks",
        "total_bytes": total,
        "text_bytes": text_bytes,
        "embedded_bytes": embedded_bytes,
        "structured_bytes": structured_bytes,
        "blocks": dict(counts),
        "inline_json": (structured or {}).get("inline_json"),
        "item_count": (structured or {}).get("item_count"),
        "is_error": is_error,
    }


def structured_of(result: Any) -> Optional[Dict]:
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
        return result[1]
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    structured = getattr(result, "structured", None)  # ToolError (str)
    if isinstance(structured, dict):
        return structured
    return None
