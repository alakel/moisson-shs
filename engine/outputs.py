"""
Écriture des fichiers d'une moisson, commune à la CLI et au serveur MCP.

Pour un radical ``<stem>`` (``corpus_<slug>_<horodatage>_<uuid court>``, en
CLI comme en MCP depuis OPT-16), quatre fichiers dans le répertoire de
sortie :

- ``<stem>.json`` : corpus Zotero JSON interne (format riche) ;
- ``<stem>.csl.json`` : CSL-JSON importable dans Zotero Desktop ;
- ``<stem>_stats.txt`` : statistiques (``main.compute_stats`` et
  ``main.write_stats_file``, inchangés) ;
- ``<stem>.manifest.json`` : manifeste de la production (``utils.manifest`` :
  requête, plan, statuts, versions, empreintes, rangs et scores par notice),
  publié EN DERNIER, marqueur de complétude.

Publication atomique (OPT-16, C33) : chaque fichier est écrit dans un
temporaire du même dossier, synchronisé, puis renommé (``os.replace``) ;
une interruption avant le manifeste ne laisse aucun corpus présenté comme
complet (``utils.manifest``, règle de complétude). L'index dérivé du
répertoire de sortie est mis à jour sous verrou.

Les payloads bruts par source (``raw/<source>_<run_id>.json``) sont écrits
par l'orchestrateur à la fin de chaque source et supprimés si l'exécution
est annulée ou échoue totalement (``cleanup_raw``). Ils contiennent les
notices **déjà normalisées** par les mappers (Zotero JSON), pas les réponses
natives des API : celles-ci ne sont capturées qu'en mode audit
(``MOISSON_AUDIT_NATIVE=1``, ``utils.manifest.NativeAudit``), dans le
dossier de service ``.native_audit/``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from mappers.csl_exporter import export_corpus_to_csl
from utils.manifest import (
    KIND_HARVEST, Publication, atomic_write_bytes, json_bytes,
)
from utils.output_files import ROLE_CSL, ROLE_STATS, ROLE_ZOTERO


@dataclass
class OutputFiles:
    """Fichiers écrits pour un corpus, et statistiques calculées."""

    json_path: Path
    csl_path: Path
    stats_path: Optional[Path]
    stats: Dict[str, Any] = field(default_factory=dict)
    csl_count: int = 0
    manifest_path: Optional[Path] = None
    manifest: Optional[Dict[str, Any]] = None

    @property
    def paths(self) -> List[Path]:
        """Fichiers de données (noms historiques, sans le manifeste)."""
        return [p for p in (self.json_path, self.csl_path, self.stats_path) if p]

    @property
    def all_paths(self) -> List[Path]:
        """Fichiers de données puis manifeste."""
        return self.paths + ([self.manifest_path] if self.manifest_path else [])


def corpus_stem(slug: str, run_id: str) -> str:
    """Radical des fichiers d'un corpus (noms historiques)."""
    return f"corpus_{slug}_{run_id}"


def write_outputs(items: List[Dict], output_dir: Path, stem: str,
                  query: str, *, with_stats: bool = True,
                  kind: str = KIND_HARVEST,
                  manifest: Optional[Dict[str, Any]] = None,
                  run_id: Optional[str] = None,
                  started_at: Optional[str] = None,
                  path_roots: tuple = ()) -> OutputFiles:
    """
    Publie atomiquement le corpus (Zotero JSON interne), sa version CSL-JSON,
    ses statistiques et son manifeste (en dernier).

    Args:
        items: notices (dédupliquées) à publier.
        output_dir: répertoire de sortie (créé au besoin).
        stem: radical des noms de fichiers.
        query: requête d'origine (ligne « Requête » des statistiques).
        with_stats: écrire aussi ``<stem>_stats.txt``.
        kind: type de production du manifeste (``utils.manifest.KIND_*``).
        manifest: champs du manifeste (requête, plan, statuts, records… ;
            voir ``utils.manifest.harvest_manifest_fields``). Sans eux, le
            manifeste porte la requête et les fichiers.
        run_id: identifiant d'exécution consigné dans le manifeste.
        started_at: début de la production (ISO).
        path_roots: dossiers à retirer de toute chaîne du manifeste.

    Returns:
        Les chemins écrits (manifeste compris) et les statistiques.
    """
    # Import tardif : compute_stats et write_stats_file restent dans main.py
    # (contrat historique), qui importe lui-même le moteur.
    import main as _main

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{stem}.json"
    csl_path = output_dir / f"{stem}.csl.json"
    stats_path = output_dir / f"{stem}_stats.txt" if with_stats else None

    csl_items = export_corpus_to_csl(items)
    stats: Dict[str, Any] = {}
    with Publication(output_dir, stem, kind, run_id=run_id,
                     started_at=started_at, path_roots=path_roots) as pub:
        pub.add_json(json_path, items, role=ROLE_ZOTERO)
        pub.add_json(csl_path, csl_items, role=ROLE_CSL)
        if with_stats:
            stats = _main.compute_stats(items)
            pub.add_text(stats_path, _main.stats_text(stats, query),
                         role=ROLE_STATS)
        fields = {"query": {"text": query}}
        if manifest:
            fields.update(manifest)
        manifest_path = pub.commit(fields)
    return OutputFiles(json_path=json_path, csl_path=csl_path,
                       stats_path=stats_path, stats=stats,
                       csl_count=len(csl_items), manifest_path=manifest_path,
                       manifest=pub.manifest)


def raw_path(output_dir: Path, display_name: str, run_id: str) -> Path:
    """Payload brut d'une source (nom historique ``raw/<nom>_<run_id>.json``)."""
    return Path(output_dir) / "raw" / f"{display_name.lower()}_{run_id}.json"


def write_raw(output_dir: Path, display_name: str, run_id: str,
              items: List[Dict]) -> Path:
    """Écrit les notices normalisées renvoyées par une source (audit des
    notices, pas des réponses natives), de façon atomique."""
    path = raw_path(output_dir, display_name, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, json_bytes(items))
    return path


def cleanup_raw(output_dir: Optional[Path], run_id: str) -> int:
    """
    Supprime les payloads bruts d'une exécution annulée ou en échec total
    (``<output_dir>/raw/*_<run_id>.json``).

    Returns:
        Nombre de fichiers supprimés.
    """
    if output_dir is None:
        return 0
    raw_dir = Path(output_dir) / "raw"
    if not raw_dir.is_dir():
        return 0
    removed = 0
    for p in raw_dir.glob(f"*_{run_id}.json"):
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed
