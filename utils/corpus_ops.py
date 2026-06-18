"""
Opérations sur corpus de niveau supérieur : fusion, comparaison.

Ces fonctions exploitent la couche `utils.deduplication` sur des
corpus déjà produits par les moissons. Utilisées par les outils MCP
corpus_merge et (à terme) corpus_diff.
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Tuple

from utils.deduplication import deduplicate, get_doi, get_source_uri

logger = logging.getLogger(__name__)


def load_corpus(path: Path) -> List[Dict]:
    """Charge un corpus Zotero JSON depuis disque."""
    items = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(items, list):
        raise ValueError(f"Corpus {path} : format invalide (pas un tableau)")
    return items


def merge_corpora(
    corpus_paths: List[Path],
) -> Tuple[List[Dict], Dict[str, Dict[str, int]]]:
    """
    Fusionne plusieurs corpus avec rapport détaillé.

    Args:
        corpus_paths: Liste de chemins vers des fichiers corpus_*.json

    Returns:
        (corpus_fusionné, rapport)

        rapport = {
            "<corpus_name>": {
                "loaded": <n_items_chargés>,
                "unique_to_corpus": <items_uniques_à_ce_corpus_après_fusion>
            },
            "_summary": {
                "total_loaded": ...,
                "total_unique": ...,
                "duplicates_removed": ...
            }
        }
    """
    if not corpus_paths:
        return [], {}

    # Charger tous les corpus
    per_corpus: Dict[str, List[Dict]] = {}
    for path in corpus_paths:
        try:
            per_corpus[path.name] = load_corpus(path)
        except (OSError, json.JSONDecodeError, ValueError) as e:
            logger.error(f"Échec chargement {path} : {e}")
            per_corpus[path.name] = []

    # Calculer les identifiants uniques par corpus AVANT fusion
    # (DOI + URI canonique pour avoir une notion d'unicité comparable)
    ids_per_corpus: Dict[str, set] = {}
    for name, items in per_corpus.items():
        ids = set()
        for item in items:
            doi = get_doi(item)
            uri = get_source_uri(item)
            ids.add(doi or uri or _synth_id(item))
        ids_per_corpus[name] = ids

    # Concaténation puis déduplication
    all_items = [item for items in per_corpus.values() for item in items]
    merged = deduplicate(all_items)

    # Calculer les identifiants uniques après fusion
    merged_ids = set()
    for item in merged:
        doi = get_doi(item)
        uri = get_source_uri(item)
        merged_ids.add(doi or uri or _synth_id(item))

    # Construire le rapport
    report: Dict[str, Dict[str, int]] = {}
    for name, items in per_corpus.items():
        # Items dans CE corpus uniquement (présents dans aucun autre)
        other_ids = set().union(
            *(ids for n, ids in ids_per_corpus.items() if n != name)
        )
        only_here = ids_per_corpus[name] - other_ids
        report[name] = {
            "loaded": len(items),
            "unique_ids": len(ids_per_corpus[name]),
            "only_to_this_corpus": len(only_here),
        }

    report["_summary"] = {
        "total_loaded": len(all_items),
        "total_unique": len(merged),
        "duplicates_removed": len(all_items) - len(merged),
        "corpora_count": len(corpus_paths),
    }

    return merged, report


def _synth_id(item: Dict) -> str:
    """Identifiant synthétique pour items sans DOI/URI."""
    from utils.deduplication import compute_fingerprint
    return f"fp:{compute_fingerprint(item)}"


def format_merge_report(report: Dict[str, Dict[str, int]]) -> str:
    """Met en forme le rapport de fusion en texte lisible."""
    if not report:
        return "Rapport vide."

    summary = report.get("_summary", {})
    lines = [
        "=== Rapport de fusion ===",
        f"Corpus fusionnés : {summary.get('corpora_count', 0)}",
        f"Items chargés totaux : {summary.get('total_loaded', 0)}",
        f"Items uniques après fusion : {summary.get('total_unique', 0)}",
        f"Doublons éliminés : {summary.get('duplicates_removed', 0)}",
        "",
        "Détail par corpus :",
    ]
    for name, stats in report.items():
        if name.startswith("_"):
            continue
        lines.append(
            f"  {name}\n"
            f"    Chargés       : {stats['loaded']}\n"
            f"    IDs uniques   : {stats['unique_ids']}\n"
            f"    Exclusifs     : {stats['only_to_this_corpus']}"
        )
    return "\n".join(lines)
