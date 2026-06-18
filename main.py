"""
Orchestrateur principal du moissonneur SHS multi-sources.

Usage en ligne de commande :

    python main.py "discours numérique" --max 500 --output ./output

Le script effectue :
1. Lancement parallèle des quatre connecteurs sur la requête
2. Sauvegarde des réponses brutes par source dans output/raw/
3. Déduplication multi-stratégie du corpus consolidé
4. Export Zotero JSON unifié dans output/corpus_<query>_<ts>.json
5. Génération d'un fichier de statistiques

L'import dans Zotero se fait ensuite via
Fichier > Importer > Un fichier, en sélectionnant le JSON produit.
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict
from collections import Counter

from utils.http import HTTPClient
from utils.deduplication import deduplicate
from connectors.registry import (
    CONNECTORS, get_connector_class, list_sources, split_by_group,
    resolve_sources, list_source_tokens, FRANCOPHONE_SOURCES,
)
from mappers.csl_exporter import export_corpus_to_csl
from config import DEFAULT_MAX_PER_SOURCE


def setup_logging(verbose: bool = False) -> None:
    """
    Configure le logger global.

    Args:
        verbose: Si True, niveau DEBUG ; sinon INFO
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def slugify(text: str) -> str:
    """
    Produit un nom de fichier exploitable depuis une requête.

    Args:
        text: Requête utilisateur

    Returns:
        Chaîne adaptée à un nom de fichier
    """
    import re
    from unidecode import unidecode
    # Translittération vers ASCII
    ascii_text = unidecode(text)
    # Remplacement des caractères non alphanumériques par underscore
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", ascii_text)
    return slug.strip("_").lower()[:60]


def run_source(
    name: str,
    connector_cls,
    query: str,
    max_per_source: int,
    output_dir: Path,
    timestamp: str,
    http_client: HTTPClient,
    from_date: str = None,
    to_date: str = None,
    **kwargs,
) -> List[Dict]:
    """
    Exécute une source en isolant les erreurs.

    Args:
        name: Nom canonique de la source
        connector_cls: Classe Connector à instancier
        query: Requête
        max_per_source: Plafond par source
        output_dir: Répertoire de sortie
        timestamp: Horodatage pour les noms de fichier
        http_client: Client HTTP partagé
        **kwargs: Arguments supplémentaires pour le connecteur

    Returns:
        Liste d'items Zotero issus de cette source (vide si échec)
    """
    logger = logging.getLogger("main")
    logger.info(f"=== Démarrage source: {name} ===")
    start = time.time()

    try:
        # Tous les connecteurs ont désormais une signature uniforme
        # (http_client, **kwargs) ; les kwargs non pertinents sont
        # ignorés par le connecteur cible.
        connector = connector_cls(http_client=http_client, **kwargs)
        # from_date/to_date sont propagés aux connecteurs qui les supportent
        # (HAL natif, Isidore post-pagination, OpenEdition/Cairn via proxy).
        # Les connecteurs qui ignorent ces kwargs n'en seront pas affectés.
        search_kwargs = {}
        if from_date:
            search_kwargs["from_date"] = from_date
        if to_date:
            search_kwargs["to_date"] = to_date
        items = connector.search(
            query, max_results=max_per_source, **search_kwargs
        )
        connector.close()

        # Sauvegarde du résultat brut par source pour audit
        raw_dir = output_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_file = raw_dir / f"{name.lower()}_{timestamp}.json"
        raw_file.write_text(
            json.dumps(items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        elapsed = time.time() - start
        logger.info(
            f"=== {name} terminé : {len(items)} items en {elapsed:.1f}s ==="
        )
        return items
    except Exception as e:
        logger.error(f"=== Échec source {name}: {e} ===", exc_info=True)
        return []


def compute_stats(items: List[Dict]) -> Dict:
    """
    Calcule des statistiques descriptives sur le corpus consolidé.

    Args:
        items: Items Zotero

    Returns:
        Dict de statistiques
    """
    # Type d'item
    types = Counter(item.get("itemType", "unknown") for item in items)

    # Sources
    sources = Counter()
    for item in items:
        extra = item.get("extra", "")
        for line in extra.split("\n"):
            if line.startswith("Sources:"):
                for s in line[len("Sources:"):].split(","):
                    sources[s.strip()] += 1
                break

    # Années
    years = Counter()
    for item in items:
        date = item.get("date", "")
        # Extraction d'une année à 4 chiffres
        import re
        match = re.search(r"\b(1[5-9]\d{2}|20\d{2})\b", date)
        if match:
            years[match.group(1)] += 1

    # Langues
    languages = Counter(
        item.get("language", "?")[:3] or "?" for item in items
    )

    # Items avec DOI
    with_doi = sum(1 for item in items if item.get("DOI"))

    # Items avec résumé non vide
    with_abstract = sum(1 for item in items if item.get("abstractNote"))

    # Recouvrements par paire de sources (mesure la valeur ajoutée de chaque
    # source par rapport aux autres après déduplication).
    from utils.deduplication import source_overlap_matrix
    overlap = source_overlap_matrix(items)

    return {
        "total_items": len(items),
        "by_type": dict(types.most_common()),
        "by_source": dict(sources.most_common()),
        "by_year": dict(sorted(years.items())),
        "by_language": dict(languages.most_common()),
        "with_doi": with_doi,
        "with_abstract": with_abstract,
        "doi_coverage_pct": round(100 * with_doi / max(len(items), 1), 1),
        "abstract_coverage_pct": round(100 * with_abstract / max(len(items), 1), 1),
        "source_overlap": overlap,
    }


def write_stats_file(stats: Dict, output_path: Path, query: str) -> None:
    """
    Sauvegarde les statistiques dans un fichier texte lisible.

    Args:
        stats: Dict de stats
        output_path: Chemin du fichier de stats
        query: Requête d'origine
    """
    lines = [
        f"=== Statistiques du corpus ===",
        f"Requête : {query}",
        f"Date : {datetime.now().isoformat(timespec='seconds')}",
        f"",
        f"Total items : {stats['total_items']}",
        f"Items avec DOI : {stats['with_doi']} ({stats['doi_coverage_pct']}%)",
        f"Items avec résumé : {stats['with_abstract']} ({stats['abstract_coverage_pct']}%)",
        f"",
        f"--- Répartition par source ---",
    ]
    for source, count in stats["by_source"].items():
        lines.append(f"  {source} : {count}")
    lines.append("")
    lines.append("--- Répartition par type ---")
    for itype, count in stats["by_type"].items():
        lines.append(f"  {itype} : {count}")
    lines.append("")
    lines.append("--- Répartition par langue ---")
    for lang, count in stats["by_language"].items():
        lines.append(f"  {lang} : {count}")
    lines.append("")
    lines.append("--- Répartition par année ---")
    for year, count in stats["by_year"].items():
        lines.append(f"  {year} : {count}")

    overlap = stats.get("source_overlap") or {}
    if overlap:
        lines.append("")
        lines.append("--- Recouvrements par paire de sources (items partagés) ---")
        for pair, count in overlap.items():
            lines.append(f"  {pair} : {count}")

    output_path.write_text("\n".join(lines), encoding="utf-8")


def main():
    """Point d'entrée CLI."""
    parser = argparse.ArgumentParser(
        description="Moissonneur SHS multi-sources vers Zotero JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Sources moissonnées :
  - HAL (api.archives-ouvertes.fr) : recherche full-text native
  - Isidore (api.isidore.science) : recherche full-text native
  - OpenEdition : recherche via proxy Isidore + enrichissement OAI optionnel
  - Cairn : recherche via proxy Isidore + enrichissement OAI optionnel

Exemples :
  python main.py "discours numérique"
  python main.py "intelligence artificielle ET société" --max 2000
  python main.py "Foucault" --sources hal isidore --enrich-oai
        """,
    )
    parser.add_argument(
        "query",
        help="Requête mot-clé (peut contenir des opérateurs booléens)",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=DEFAULT_MAX_PER_SOURCE,
        help=f"Plafond d'items par source (défaut: {DEFAULT_MAX_PER_SOURCE})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("./output"),
        help="Répertoire de sortie (défaut: ./output)",
    )
    parser.add_argument(
        "--sources",
        nargs="+",
        choices=list_source_tokens(),
        default=list(FRANCOPHONE_SOURCES),
        metavar="SOURCE",
        help=(
            "Sources et/ou groupes à moissonner. Sources : "
            f"{', '.join(sorted(CONNECTORS.keys()))}. "
            "Groupes : francophone (défaut), international, all. "
            "Les sources internationales (crossref/openalex/semantic_scholar/"
            "core) requièrent une clé API pour 3 d'entre elles (voir .env.example)."
        ),
    )
    parser.add_argument(
        "--enrich-oai",
        action="store_true",
        help="Active l'enrichissement OAI pour OpenEdition et Cairn "
             "(plus lent mais métadonnées plus riches)",
    )
    parser.add_argument(
        "--no-dedup",
        action="store_true",
        help="Désactive la déduplication post-fusion",
    )
    parser.add_argument(
        "--from-date",
        type=str,
        default=None,
        help="Date minimale inclusive (YYYY ou YYYY-MM-DD)",
    )
    parser.add_argument(
        "--to-date",
        type=str,
        default=None,
        help="Date maximale inclusive (YYYY ou YYYY-MM-DD)",
    )
    parser.add_argument(
        "--cache",
        action="store_true",
        help="Active le cache HTTP local (rejeu rapide, mode quasi-offline). "
             "TTL configurable via MOISSON_CACHE_TTL (défaut 24h)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Nombre de workers parallèles pour le moissonnage des sources "
             "(défaut: 1 = séquentiel). Recommandé: 2 (HAL+Isidore parallèles, "
             "puis OpenEdition+Cairn parallèles)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Active les logs DEBUG",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)
    logger = logging.getLogger("main")

    # Développe les groupes (francophone/international/all) en noms de sources
    args.sources = resolve_sources(args.sources)

    # Préparation des chemins de sortie
    args.output.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    query_slug = slugify(args.query)
    output_json = args.output / f"corpus_{query_slug}_{timestamp}.json"
    output_csl = args.output / f"corpus_{query_slug}_{timestamp}.csl.json"
    output_stats = args.output / f"corpus_{query_slug}_{timestamp}_stats.txt"

    logger.info("=" * 60)
    logger.info(f"Moissonneur SHS multi-sources")
    logger.info(f"Requête : {args.query}")
    logger.info(f"Sources : {', '.join(args.sources)}")
    logger.info(f"Max par source : {args.max}")
    logger.info(f"Enrichissement OAI : {args.enrich_oai}")
    logger.info("=" * 60)

    # Client HTTP partagé entre tous les connecteurs
    # pour réutiliser les connexions TCP. Cache optionnel via --cache.
    http_client = HTTPClient(use_cache=args.cache)
    if args.cache:
        logger.info("Cache HTTP activé pour cette moisson")

    # Topologie de moisson : sources natives (HAL, Isidore, Theses.fr...)
    # exécutées en parallèle dans un premier groupe, puis sources proxy
    # (OpenEdition, Cairn, Persée, Érudit) qui passent par Isidore dans
    # un second groupe. Cf. connectors/registry.split_by_group.
    native_names, proxy_names = split_by_group(args.sources)

    def _build_group(names):
        group = []
        # Sources qui supportent l'enrichissement OAI
        enrich_oai_capable = {"openedition", "cairn", "persee", "erudit"}
        for name in names:
            cls = get_connector_class(name)
            kwargs = {}
            if name in enrich_oai_capable:
                kwargs["enrich_oai"] = args.enrich_oai
            # Capitalisation pour affichage (préserve l'historique des logs)
            display_name = {
                "hal": "HAL", "isidore": "Isidore",
                "openedition": "OpenEdition", "cairn": "Cairn",
                "persee": "Persée", "theses_fr": "Theses.fr",
                "erudit": "Érudit", "crossref": "CrossRef",
                "openalex": "OpenAlex", "semantic_scholar": "Semantic Scholar",
                "core": "CORE",
            }.get(name, name.capitalize())
            group.append((display_name, cls, kwargs))
        return group

    group_native = _build_group(native_names)
    group_proxy = _build_group(proxy_names)

    all_items = []

    def _run_group(group):
        """Exécute un groupe de sources en série ou en parallèle."""
        results = []
        if args.workers > 1 and len(group) > 1:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {
                    pool.submit(
                        run_source, name, cls, args.query, args.max,
                        args.output, timestamp, http_client,
                        from_date=args.from_date, to_date=args.to_date,
                        **kw,
                    ): name
                    for name, cls, kw in group
                }
                for f in as_completed(futures):
                    name = futures[f]
                    try:
                        results.extend(f.result())
                    except Exception as e:
                        logger.error(f"Source {name} en erreur : {e}")
        else:
            for name, cls, kw in group:
                items = run_source(
                    name, cls, args.query, args.max,
                    args.output, timestamp, http_client,
                    from_date=args.from_date, to_date=args.to_date,
                    **kw,
                )
                results.extend(items)
        return results

    try:
        # Groupe 1 : sources natives (HAL + Isidore en parallèle si workers>1)
        all_items.extend(_run_group(group_native))
        # Groupe 2 : proxies Isidore (en parallèle entre eux si workers>1)
        all_items.extend(_run_group(group_proxy))
    finally:
        http_client.close()

    logger.info(f"Total avant déduplication : {len(all_items)} items")

    # Déduplication multi-stratégies
    if not args.no_dedup:
        all_items = deduplicate(all_items)

    # Export Zotero JSON natif (format interne riche, consommable par MCP /
    # outils programmatiques)
    output_json.write_text(
        json.dumps(all_items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"Corpus Zotero JSON : {output_json} ({len(all_items)} items)")

    # Export CSL-JSON (format BibTeX JSON standard, importable directement
    # dans Zotero Desktop via Fichier > Importer)
    csl_items = export_corpus_to_csl(all_items)
    output_csl.write_text(
        json.dumps(csl_items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"Corpus CSL-JSON : {output_csl} ({len(csl_items)} items)")

    # Statistiques
    stats = compute_stats(all_items)
    write_stats_file(stats, output_stats, args.query)
    logger.info(f"Statistiques : {output_stats}")

    # Récapitulatif sortie standard
    print("\n" + "=" * 60)
    print(f"Terminé. {len(all_items)} items uniques.")
    print(f"Zotero JSON (interne)  : {output_json}")
    print(f"CSL-JSON (Zotero IMPORT) : {output_csl}")
    print(f"Statistiques            : {output_stats}")
    print("=" * 60)
    print("\nPour importer dans Zotero :")
    print("  1. Ouvrir Zotero Desktop")
    print("  2. Menu : Fichier > Importer")
    print("  3. Sélectionner 'Un fichier (BibTeX, RIS, Zotero RDF...)'")
    print(f"  4. Choisir : {output_csl}")
    print(f"     (Zotero détectera automatiquement le format CSL JSON)")
    print(f"  5. Une nouvelle collection sera créée")

    return 0


if __name__ == "__main__":
    sys.exit(main())
