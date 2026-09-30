"""
Opérations sur corpus de niveau supérieur : fusion, comparaison.

Ces fonctions exploitent la couche `utils.deduplication` sur des
corpus déjà produits par les moissons. Utilisées par les outils MCP
corpus_merge et (à terme) corpus_diff.
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from utils.deduplication import deduplicate_with_report

logger = logging.getLogger(__name__)


class CorpusFormatError(ValueError):
    """
    Entrée qui n'est pas un corpus Zotero JSON interne (CSL-JSON, manifeste,
    rapport DOI) donnée à une opération qui l'exige (OPT-16). Message sans
    chemin disque, destiné à l'utilisateur.
    """


def check_zotero_corpus(path: Path, items=None, *, tool: str = "l'opération",
                        loaded: bool = False) -> None:
    """
    Refuse clairement une entrée qui n'est pas un corpus Zotero JSON interne
    (nom ``.csl.json``, manifeste, rapport DOI, ou contenu CSL-JSON).

    Raises:
        CorpusFormatError: entrée refusée.
    """
    from utils.manifest import zotero_input_error

    message = zotero_input_error(Path(path), items, tool=tool, loaded=loaded)
    if message:
        raise CorpusFormatError(message)


def load_corpus(path: Path, *, tool: str = "l'opération") -> List[Dict]:
    """
    Charge un corpus Zotero JSON depuis disque.

    Raises:
        CorpusFormatError: fichier CSL-JSON, manifeste ou rapport DOI (OPT-16).
        ValueError: contenu qui n'est pas un tableau.
    """
    path = Path(path)
    check_zotero_corpus(path, tool=tool)
    items = json.loads(path.read_text(encoding="utf-8"))
    check_zotero_corpus(path, items, tool=tool, loaded=True)
    if not isinstance(items, list):
        raise ValueError(f"Corpus {path.name} : format invalide (pas un tableau)")
    return items


def _file_identity(path: Path) -> Path:
    """Identité d'un fichier : chemin résolu (liens et ``..`` suivis)."""
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError):
        return Path(path).absolute()


def _relative_label(path: Path, depth: int) -> str:
    """Nom du fichier précédé de ``depth`` dossiers parents (``a/b/c.json``)."""
    parts = path.parts
    tail = parts[-(depth + 1):] if len(parts) > depth else parts
    return "/".join(p for p in tail if p not in ("/", "\\"))


def corpus_labels(paths: Sequence[Path]) -> List[str]:
    """
    Libellés lisibles et distincts pour des fichiers d'identités distinctes.

    Un nom de fichier sans homonyme parmi les entrées reste nu (``c1.json``) ;
    des homonymes sont précédés d'autant de dossiers parents que nécessaire
    pour les distinguer (``dossierA/corpus.json``, ``dossierB/corpus.json``).
    Aucun chemin absolu : les libellés peuvent être renvoyés en HTTP.
    """
    labels = [p.name for p in paths]
    depth = [0] * len(paths)
    for _ in range(max((len(p.parts) for p in paths), default=1)):
        seen: Dict[str, List[int]] = {}
        for i, label in enumerate(labels):
            seen.setdefault(label, []).append(i)
        clashes = [idx for idx in seen.values() if len(idx) > 1]
        if not clashes:
            break
        for idx in clashes:
            for i in idx:
                depth[i] += 1
                labels[i] = _relative_label(paths[i], depth[i])
    # Dernier recours (chemins identiques jusqu'à la racine : impossible pour
    # des identités distinctes, mais le libellé doit rester unique).
    counts: Dict[str, int] = {}
    for i, label in enumerate(labels):
        counts[label] = counts.get(label, 0) + 1
        if counts[label] > 1:
            labels[i] = f"{label} ({counts[label]})"
    return labels


def merge_corpora(
    corpus_paths: List[Path],
) -> Tuple[List[Dict], Dict[str, Dict[str, int]]]:
    """
    Fusionne plusieurs corpus avec rapport détaillé.

    Les corpus sont indexés par **identité de fichier** (chemin résolu) et non
    par nom : deux fichiers homonymes de dossiers différents sont deux corpus
    distincts ; un même fichier cité deux fois n'est chargé qu'une fois.

    Args:
        corpus_paths: Liste de chemins vers des fichiers corpus_*.json

    Raises:
        CorpusFormatError: une entrée est un fichier CSL-JSON, un manifeste
            ou un rapport DOI (OPT-16) : rien n'est fusionné.

    Returns:
        (corpus_fusionné, rapport)

        rapport = {
            "<libellé>": {
                "loaded": <n_items_chargés>,
                "unique_ids": <travaux distincts du corpus après fusion>,
                "only_to_this_corpus": <travaux présents dans ce seul corpus>,
                "file": "<dossier parent>/<nom>"
            },
            "_summary": {
                "total_loaded": ...,
                "total_unique": ...,
                "duplicates_removed": ...,
                "corpora_count": <fichiers distincts>,
                "inputs_count": <chemins fournis>,
                "duplicate_inputs": <chemins désignant un fichier déjà cité>
            }
        }

        Le libellé est le nom du fichier, précédé de dossiers parents s'il a
        des homonymes parmi les entrées (voir :func:`corpus_labels`).
    """
    if not corpus_paths:
        return [], {}

    # Identité de fichier : chemin résolu ; ordre de première citation.
    unique_paths: List[Path] = []
    identities: List[Path] = []
    duplicate_inputs = 0
    for path in corpus_paths:
        path = Path(path)
        identity = _file_identity(path)
        if identity in identities:
            duplicate_inputs += 1
            continue
        identities.append(identity)
        unique_paths.append(path)

    labels = corpus_labels(identities)

    # Charger tous les corpus. Un corpus d'un autre format (CSL-JSON,
    # manifeste) est refusé clairement (CorpusFormatError, OPT-16) au lieu
    # d'être fusionné comme des notices Zotero ; un fichier illisible compte
    # pour un corpus vide (comportement historique).
    per_corpus: List[List[Dict]] = []
    for path in unique_paths:
        try:
            per_corpus.append(load_corpus(path, tool="corpus_merge"))
        except CorpusFormatError:
            raise
        except (OSError, json.JSONDecodeError, ValueError) as e:
            logger.error(f"Échec chargement {path.name} : {e}")
            per_corpus.append([])

    # Concaténation puis déduplication, avec le suivi des notices d'origine :
    # chaque notice fusionnée sait de quels corpus viennent ses membres.
    all_items: List[Dict] = []
    origin: List[int] = []
    for ci, items in enumerate(per_corpus):
        all_items.extend(items)
        origin.extend([ci] * len(items))
    merged, dedup_report = deduplicate_with_report(all_items, with_groups=True)

    contributors = [{origin[i] for i in group}
                    for group in dedup_report.get("groups", [])]

    # Construire le rapport
    report: Dict[str, Dict[str, int]] = {}
    for ci, items in enumerate(per_corpus):
        report[labels[ci]] = {
            "loaded": len(items),
            "unique_ids": sum(1 for c in contributors if ci in c),
            "only_to_this_corpus": sum(1 for c in contributors if c == {ci}),
            "file": _relative_label(identities[ci], 1),
        }

    report["_summary"] = {
        "total_loaded": len(all_items),
        "total_unique": len(merged),
        "duplicates_removed": len(all_items) - len(merged),
        "corpora_count": len(unique_paths),
        "inputs_count": len(corpus_paths),
        "duplicate_inputs": duplicate_inputs,
    }

    return merged, report


def _synth_id(item: Dict) -> str:
    """Identifiant synthétique pour items sans DOI/URI (API historique)."""
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
    ]
    if summary.get("duplicate_inputs"):
        lines.append(
            f"Fichiers cités plusieurs fois (chargés une fois) : "
            f"{summary['duplicate_inputs']}"
        )
    lines += ["", "Détail par corpus :"]
    for name, stats in report.items():
        if name.startswith("_"):
            continue
        where = stats.get("file")
        header = f"  {name}" + (f"  [{where}]" if where and where != name else "")
        lines.append(
            f"{header}\n"
            f"    Chargés       : {stats['loaded']}\n"
            f"    IDs uniques   : {stats['unique_ids']}\n"
            f"    Exclusifs     : {stats['only_to_this_corpus']}"
        )
    return "\n".join(lines)


# === Comptages de corpus_analyze (retour d'expérience B7) ===

def journal_key(name: str) -> str:
    """
    Clé de comptage d'un nom de revue : translittération, casse, « & » et
    « and » ramenés à « et », ponctuation et espaces normalisés.
    « Communication et organisation » et « Communication & Organisation »
    comptent ensemble ; les notices ne sont pas modifiées.
    """
    import re
    from unidecode import unidecode
    text = unidecode(str(name or "")).lower().replace("&", " et ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\band\b", "et", text)
    return re.sub(r"\s+", " ", text).strip()


def author_key(creator: Dict) -> Tuple[str, str]:
    """
    Clé de comptage d'un auteur : nom de famille normalisé et initiale du
    prénom (deux auteurs de même nom et de prénoms différents ne sont plus
    confondus), et libellé affiché « Nom P. ».
    """
    from unidecode import unidecode
    last = str(creator.get("lastName") or creator.get("name") or "").strip()
    first = str(creator.get("firstName") or "").strip()
    initial = unidecode(first[:1]).upper() if first else ""
    key = unidecode(last).lower().strip() + ("|" + initial if initial else "")
    label = last + (f" {initial}." if initial else "")
    return key, label


def top_forms(pairs, limit: int) -> List[Tuple[str, int]]:
    """
    Regroupe des couples ``(clé, forme affichée)`` par clé ; rend les
    ``limit`` groupes les plus fréquents, chacun sous sa forme la plus
    fréquente (à égalité, ordre alphabétique).
    """
    from collections import Counter, defaultdict
    counts: Counter = Counter()
    forms: Dict[str, Counter] = defaultdict(Counter)
    for key, form in pairs:
        if not key:
            continue
        counts[key] += 1
        forms[key][form] += 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    out = []
    for key, n in ranked:
        best = sorted(forms[key].items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        out.append((best, n))
    return out
