"""
Point d'entrée CLI du moissonneur SHS multi-sources.

Usage en ligne de commande :

    python main.py "discours numérique" --max 500 --output ./output

Le script effectue :
1. Moisson des sources demandées par le moteur commun (``engine``) : même
   orchestration, mêmes statuts par source que le serveur MCP
2. Sauvegarde des notices par source dans output/raw/ (notices déjà
   normalisées ; les réponses natives des API ne sont capturées qu'en mode
   audit, MOISSON_AUDIT_NATIVE=1, dans output/.native_audit/)
3. Déduplication multi-stratégie du corpus consolidé
4. Export Zotero JSON unifié dans output/corpus_<query>_<ts>_<uuid>.json
   (et CSL-JSON)
5. Génération d'un fichier de statistiques
6. Manifeste output/corpus_<query>_<ts>_<uuid>.manifest.json, publié en
   dernier (publication atomique, OPT-16)

Chaque source reçoit un statut (complète, vide, partielle, non exécutée, en
échec, annulée) affiché en fin de moisson : une panne n'est jamais présentée
comme une absence de résultat. Si aucune source n'a répondu sans panne
(toutes en échec, non exécutées, annulées ou partielles sur erreur) et
qu'aucune notice n'a été obtenue, aucun corpus n'est écrit et le code de
sortie vaut 3 (``EXIT_ALL_SOURCES_FAILED``). Une sous-source d'Isidore
absente de la fenêtre de 200 résultats n'est pas une panne : corpus vide,
code 0, limite nommée dans son statut.

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
from connectors.registry import (
    CONNECTORS, resolve_sources, list_source_tokens, FRANCOPHONE_SOURCES,
)
from config import DEFAULT_MAX_PER_SOURCE
# Moteur commun (OPT-07) : main.py n'est plus qu'un adaptateur CLI.
# ``run_source`` (exécution isolée d'une source) vit désormais dans le moteur ;
# le nom reste exporté ici pour compatibilité (il renvoie un SourceResult,
# dont ``items`` porte les notices).
from engine.models import (
    MODE_COLLECTE, MODES, RunContext, SearchRequest, new_run_id,
)
from engine.orchestrator import harvest, run_source  # noqa: F401
from engine.outputs import corpus_stem, write_outputs
# Manifeste et audit des réponses natives (OPT-16).
from utils.http import attach_native_audit
from utils.manifest import harvest_manifest_fields, native_audit_for_run, now_iso
# Classement des notices (OPT-13) : option --ranking.
from utils.ranking import RANKING_MODES, ranking_text_lines

# Code de sortie quand aucune source n'a répondu sans panne et qu'aucune
# notice n'a été obtenue (pannes, clés absentes ; voir
# ``HarvestResult.all_failed``) : rien n'est écrit.
EXIT_ALL_SOURCES_FAILED = 3


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


def compute_stats(items: List[Dict]) -> Dict:
    """
    Calcule des statistiques descriptives sur le corpus consolidé.

    Les comptes s'appuient sur l'extracteur commun d'identifiants et sur la
    provenance tracée par les mappers :

    - DOI : champ ``DOI`` **ou** ligne ``DOI:`` de ``extra`` (DOI migré par
      ``validate_and_clean`` pour les types sans champ DOI) ;
    - résumé : un ``abstractNote`` qui n'est pas l'extrait Google Scholar
      (ligne ``Scholar snippet`` et résumé venu de Scholar) ; les extraits
      sont comptés à part ;
    - auteurs tronqués : ligne ``Scholar truncation`` et auteurs venus de
      Scholar (la marque couvre « auteurs et/ou revue » : majorant) ;
    - sources : libellés ramenés à la table unique ``utils.source_labels``
      (« Crossref » et « CrossRef » comptent pour une seule source).

    Toutes les clés historiques sont conservées ; ``with_abstract`` compte
    désormais les seuls vrais résumés (``with_abstract_field`` garde le
    compte brut des champs ``abstractNote`` non vides).

    Args:
        items: Items Zotero

    Returns:
        Dict de statistiques
    """
    import re

    from utils.deduplication import (
        abstract_is_scholar_snippet, authors_truncated, parse_evidence,
        source_overlap_matrix,
    )
    from utils.identity import extract_doi, normalize_doi
    from utils.source_labels import canonical_source_key, display_name

    total = len(items)

    # Type d'item
    types = Counter(item.get("itemType", "unknown") for item in items)

    # Sources (libellés normalisés, une source comptée une fois par item)
    sources = Counter()
    source_keys = Counter()
    for item in items:
        extra = item.get("extra", "") or ""
        for line in extra.split("\n"):
            if line.startswith("Sources:"):
                seen = set()
                for s in line[len("Sources:"):].split(","):
                    key = canonical_source_key(s)
                    if key and key not in seen:
                        seen.add(key)
                        source_keys[key] += 1
                        sources[display_name(s)] += 1
                break

    # Années
    years = Counter()
    for item in items:
        date = item.get("date", "") or ""
        # Extraction d'une année à 4 chiffres
        match = re.search(r"\b(1[5-9]\d{2}|20\d{2})\b", date)
        if match:
            years[match.group(1)] += 1

    # Langues
    languages = Counter(
        (item.get("language") or "?")[:3] or "?" for item in items
    )

    # DOI : champ ou extra
    with_doi_field = sum(1 for item in items if normalize_doi(item.get("DOI")))
    with_doi = sum(1 for item in items if extract_doi(item))

    # Résumés : vrai résumé contre extrait Google Scholar
    with_abstract_field = 0
    with_snippet = 0
    for item in items:
        if (item.get("abstractNote") or "").strip():
            with_abstract_field += 1
            if abstract_is_scholar_snippet(item):
                with_snippet += 1
    with_abstract = with_abstract_field - with_snippet

    # Auteurs possiblement tronqués par Google Scholar
    truncated = sum(1 for item in items if authors_truncated(item))

    # Traces de déduplication
    merged_items = ambiguous_items = 0
    for item in items:
        ev = parse_evidence(item)
        if ev:
            if ev["count"] > 1:
                merged_items += 1
            if ev["ambiguous"]:
                ambiguous_items += 1

    # Recouvrements par paire de sources (mesure la valeur ajoutée de chaque
    # source par rapport aux autres après déduplication).
    overlap = source_overlap_matrix(items)

    def pct(n: int) -> float:
        return round(100 * n / max(total, 1), 1)

    return {
        "total_items": total,
        "by_type": dict(types.most_common()),
        "by_source": dict(sources.most_common()),
        "by_year": dict(sorted(years.items())),
        "by_language": dict(languages.most_common()),
        "with_doi": with_doi,
        "with_abstract": with_abstract,
        "doi_coverage_pct": pct(with_doi),
        "abstract_coverage_pct": pct(with_abstract),
        "source_overlap": overlap,
        # Ajouts OPT-06 (C19, C24)
        "by_source_key": dict(source_keys.most_common()),
        "with_doi_field": with_doi_field,
        "with_doi_extra_only": with_doi - with_doi_field,
        "with_abstract_field": with_abstract_field,
        "with_scholar_snippet": with_snippet,
        "scholar_snippet_pct": pct(with_snippet),
        "truncated_authors": truncated,
        "truncated_authors_pct": pct(truncated),
        "merged_items": merged_items,
        "ambiguous_items": ambiguous_items,
    }


def write_stats_file(stats: Dict, output_path: Path, query: str) -> None:
    """
    Sauvegarde les statistiques dans un fichier texte lisible.

    Args:
        stats: Dict de stats
        output_path: Chemin du fichier de stats
        query: Requête d'origine
    """
    output_path.write_text(stats_text(stats, query), encoding="utf-8")


def stats_text(stats: Dict, query: str) -> str:
    """
    Texte du fichier de statistiques (format historique), publié tel quel
    par ``engine.outputs.write_outputs`` (écriture atomique, OPT-16).

    Args:
        stats: Dict de stats
        query: Requête d'origine
    """
    from utils.build_info import describe
    lines = [
        f"=== Statistiques du corpus ===",
        f"Requête : {query}",
        f"Date : {datetime.now().isoformat(timespec='seconds')}",
        f"Version : moisson-shs {describe()}",
        f"",
        f"Total items : {stats['total_items']}",
        f"Items avec DOI : {stats['with_doi']} ({stats['doi_coverage_pct']}%)",
    ]
    # Détails ajoutés par OPT-06 : lus avec .get() pour accepter un dict de
    # statistiques plus ancien.
    if stats.get("with_doi_extra_only"):
        lines.append(
            f"  dont DOI lu dans 'extra' seulement : "
            f"{stats['with_doi_extra_only']}"
        )
    lines.append(
        f"Items avec résumé : {stats['with_abstract']} "
        f"({stats['abstract_coverage_pct']}%)"
    )
    if stats.get("with_scholar_snippet"):
        lines.append(
            f"Items avec extrait Google Scholar (pas un résumé) : "
            f"{stats['with_scholar_snippet']} ({stats['scholar_snippet_pct']}%)"
        )
    if stats.get("truncated_authors"):
        lines.append(
            f"Items à auteurs et/ou revue tronqués par Google Scholar : "
            f"{stats['truncated_authors']} ({stats['truncated_authors_pct']}%)"
        )
    if stats.get("merged_items") or stats.get("ambiguous_items"):
        lines.append(
            f"Notices fusionnées par la déduplication : "
            f"{stats.get('merged_items', 0)} ; laissées à part (DOI "
            f"contradictoires ou versions distinctes d'un même dépôt) : "
            f"{stats.get('ambiguous_items', 0)}"
        )
    lines += [
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

    return "\n".join(lines)


def main():
    """Point d'entrée CLI."""
    parser = argparse.ArgumentParser(
        description="Moissonneur SHS multi-sources vers Zotero JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Sources moissonnées (douze ; --sources, groupe francophone par défaut) :
  Francophones (sans clé) :
  - HAL (api.archives-ouvertes.fr) : API Solr native
  - Isidore (api.isidore.science) : API native, 200 premiers résultats servis
  - OpenEdition, Cairn, Persée, Érudit : flux Isidore partagé
    + enrichissement OAI optionnel (--enrich-oai)
  - Theses.fr : flux Isidore partagé
  Internationales (groupe international ; all = francophone + international) :
  - CrossRef (sans clé), OpenAlex (clé requise), Semantic Scholar (clé
    facultative), CORE (clé requise)
  Facturée à la requête (hors des groupes international et all) :
  - Google Scholar par SerpAPI (clé requise) : à nommer (google_scholar)
    ou groupe scholar

Exemples :
  python main.py "discours numérique"
  python main.py "intelligence artificielle ET société" --max 2000
  python main.py "Foucault" --sources hal isidore --enrich-oai
  python main.py '"discours numérique" AND (twitter OR facebook)' --sources all --query-en '"digital discourse"' --lang fr en --plan-only
        """,
    )
    parser.add_argument(
        "query",
        help=("Requête mot-clé (peut contenir des opérateurs booléens ; "
              "précédence NOT > AND > OR, une exclusion écrite au niveau "
              "d'une alternative porte sur toute l'alternative : parenthéser "
              "en cas de doute, --plan-only pour vérifier la lecture)"),
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
            "Groupes : francophone (défaut), international, all "
            "(francophone + international), scholar (google_scholar seul). "
            "Clés d'API : openalex, core et google_scholar les exigent, "
            "semantic_scholar la rend facultative, crossref n'en a pas "
            "(voir .env.example). google_scholar est facturé à la requête et "
            "n'appartient à aucun autre groupe : le nommer pour l'activer."
        ),
    )
    parser.add_argument(
        "--enrich-oai",
        action="store_true",
        help="Active l'enrichissement OAI pour OpenEdition, Cairn, Persée "
             "et Érudit (plus lent mais métadonnées plus riches)",
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
    # Option cache à trois états : --cache (activé), --no-cache (désactivé),
    # absente (None ⇒ héritée de MOISSON_CACHE, cf.
    # utils.http.resolve_cache_option). Ne jamais transformer l'absence en
    # False explicite : cela neutraliserait MOISSON_CACHE=1.
    cache_group = parser.add_mutually_exclusive_group()
    cache_group.add_argument(
        "--cache",
        dest="cache",
        action="store_true",
        default=None,
        help="Active le cache HTTP local (rejeu rapide, mode quasi-offline). "
             "TTL configurable via MOISSON_CACHE_TTL (défaut 24h). Sans "
             "--cache ni --no-cache : valeur de MOISSON_CACHE (0/1)",
    )
    cache_group.add_argument(
        "--no-cache",
        dest="cache",
        action="store_false",
        help="Désactive le cache HTTP local, même si MOISSON_CACHE=1",
    )
    parser.set_defaults(cache=None)
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Nombre de sources moissonnées en même temps, dans un pool "
             "unique sans barrière entre sources (défaut: 1 = séquentiel ; "
             "4 recommandé, valeur par défaut de l'outil MCP ; borné par "
             "MOISSON_HARVEST_MAX_WORKERS). La politesse par serveur "
             "s'applique quel que soit ce nombre",
    )
    parser.add_argument(
        "--mode",
        choices=list(MODES),
        default=MODE_COLLECTE,
        help="Mode de moisson : collecte (défaut en CLI : ordre déterministe, "
             "exhaustivité dans la limite du plafond) ou bibliographie (défaut "
             "du serveur MCP : meilleures notices selon l'ordre natif de "
             "chaque source). Transmis aux sources qui l'exploitent.",
    )
    # Plan de requête (OPT-11) : langues, variantes explicites, aperçu.
    parser.add_argument(
        "--lang",
        nargs="+",
        default=None,
        metavar="CODE",
        help="Langues des documents (codes ISO 639-1, ex. --lang fr en) : "
             "filtre natif là où la source le permet (HAL, OpenAlex, Google "
             "Scholar), local ailleurs (notices sans langue conservées), "
             "impossible pour CrossRef et Semantic Scholar (signalé)",
    )
    parser.add_argument(
        "--query-en",
        default=None,
        metavar="REQUETE",
        help="Variante anglaise de la requête, envoyée aux sources "
             "majoritairement anglophones (CrossRef, OpenAlex, Semantic "
             "Scholar, CORE). Sans elle, ces sources reçoivent la requête "
             "d'origine : aucune traduction automatique",
    )
    parser.add_argument(
        "--variant",
        action="append",
        default=[],
        metavar="LANG=REQUETE",
        help="Variante explicite de la requête pour une langue (répétable), "
             "ex. --variant en='\"digital discourse\"' --variant fr=\"discours "
             "numérique\"",
    )
    parser.add_argument(
        "--ranking",
        choices=list(RANKING_MODES),
        default=None,
        help="Classement des notices après déduplication (OPT-13), affiché "
             "en fin de moisson sans modifier le fichier de corpus : native "
             "(fusion des rangs natifs des sources qui classent par "
             "pertinence), local (concepts de la requête dans titre, "
             "mots-clés et résumé, combinés au classement natif ; "
             "expérimental), off (aucun). Défaut : MOISSON_RANKING, sinon "
             "native",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Affiche le plan de requête (requête envoyée à chaque source, "
             "filtres natifs et locaux, avertissements) sans rien exécuter : "
             "aucune requête réseau, aucun fichier",
    )
    parser.add_argument(
        "--on-ambiguity",
        choices=("warn", "refuse"),
        default=None,
        help="Requête ambiguë (OR mêlé sans parenthèses à des termes "
             "juxtaposés, à AND ou à une exclusion) : warn l'envoie avec un "
             "avertissement, refuse ne l'envoie pas. Défaut : refuse si une "
             "source facturée (Google Scholar) est demandée, warn sinon",
    )
    parser.add_argument(
        "--hal-fulltext",
        action="store_true",
        help="HAL cherche aussi dans le texte intégral des dépôts "
             "(fulltext_t, 36 %% des dépôts ont un fichier indexé) ; sans "
             "l'option, dans les métadonnées seulement (titre, résumé, "
             "mots-clés). Ensemble bien plus large et plus bruité",
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

    # Variantes explicites (--variant LANG=REQUETE, --query-en).
    variants = {}
    for spec in args.variant:
        lang, sep, text = spec.partition("=")
        if not sep:
            parser.error(f"--variant {spec!r} : forme attendue LANG=REQUETE")
        variants[lang.strip().lower()] = text
    if args.query_en is not None:
        variants["en"] = args.query_en

    # Demande et plan de requête, sans réseau : une demande invalide (requête
    # vide, langue, variante, dates) est refusée avant toute écriture.
    try:
        request = SearchRequest(
            query=args.query,
            sources=args.sources,
            from_date=args.from_date,
            to_date=args.to_date,
            mode=args.mode,
            max_per_source=args.max,
            enrich_oai=args.enrich_oai,
            dedup=not args.no_dedup,
            cache=args.cache,
            workers=args.workers,
            query_variants=variants,
            languages=args.lang or [],
            hal_fulltext=args.hal_fulltext,
        )
        plan = request.query_plan()
    except ValueError as exc:
        parser.error(str(exc))
    if args.plan_only:
        print("Aperçu : aucune moisson exécutée, aucun fichier écrit.")
        for line in plan.ambiguity_lines():
            print(line)
        for line in plan.text_lines():
            print(line)
        return 0
    # Requête ambiguë (PAT-19, décision DP-7) : refusée par défaut quand une
    # source facturée est demandée ; avertissement en tête sinon.
    from connectors.registry import is_metered_source
    metered = [e.label for e in plan.sources if is_metered_source(e.source)]
    on_ambiguity = args.on_ambiguity or ("refuse" if metered else "warn")
    if plan.ambiguities:
        for line in plan.ambiguity_lines():
            print(line)
        if on_ambiguity == "refuse":
            parser.error(
                "requête ambiguë, non envoyée"
                + (f" (source facturée demandée : {', '.join(metered)})"
                   if metered else "")
                + " : ajouter des parenthèses, ou --on-ambiguity warn")

    # Préparation des chemins de sortie. Identifiant d'exécution = horodatage
    # à la seconde + uuid court (OPT-16, C33) : deux moissons lancées dans la
    # même seconde n'écrivent jamais les mêmes fichiers (corpus et raw/).
    args.output.mkdir(parents=True, exist_ok=True)
    run_id = new_run_id()
    started_at = now_iso()
    query_slug = slugify(args.query)
    stem = corpus_stem(query_slug, run_id)

    logger.info("=" * 60)
    logger.info(f"Moissonneur SHS multi-sources")
    logger.info(f"Requête : {args.query}")
    logger.info(f"Sources : {', '.join(args.sources)}")
    logger.info(f"Max par source : {args.max}")
    logger.info(f"Enrichissement OAI : {args.enrich_oai}")
    logger.info("=" * 60)

    # Client HTTP partagé entre tous les connecteurs pour réutiliser les
    # connexions TCP. Cache : --cache / --no-cache, ou hérité de MOISSON_CACHE
    # quand l'option est absente (args.cache is None).
    http_client = HTTPClient(use_cache=args.cache)
    if http_client.cache_enabled:
        logger.info("Cache HTTP activé pour cette moisson")
    # Audit des réponses natives (MOISSON_AUDIT_NATIVE=1) : capture bornée et
    # expurgée dans <sortie>/.native_audit/<run_id>.jsonl, distincte de raw/.
    native_audit = native_audit_for_run(args.output, run_id)
    if native_audit is not None:
        attach_native_audit(http_client, native_audit)
        logger.info("Audit des réponses natives activé")
    # run_id : suffixe des fichiers de l'exécution
    # (corpus_<slug>_<run_id>.json, raw/<source>_<run_id>.json).
    ctx = RunContext(run_id=run_id, http_client=http_client,
                     output_dir=args.output)
    try:
        # --ranking transmis seulement s'il est fixé (défaut du moteur :
        # MOISSON_RANKING, sinon native).
        result = (harvest(request, ctx) if args.ranking is None
                  else harvest(request, ctx, ranking=args.ranking))
    finally:
        http_client.close()

    all_items = result.items
    status_lines = result.status_lines()

    if result.all_failed:
        # Aucune source n'a répondu sans panne, aucune notice : une panne,
        # pas une absence de résultat. Rien n'est écrit (payloads bruts
        # supprimés par le moteur).
        logger.error("Aucune source n'a pu être moissonnée : aucun corpus écrit")
        print("\n" + "=" * 60)
        print("ÉCHEC : aucune source n'a pu être moissonnée ; aucun corpus "
              "n'a été écrit.")
        print("Statut par source :")
        for line in status_lines:
            print(line)
        if not plan.trivial:
            for line in plan.text_lines():
                print(line)
        print("=" * 60)
        return EXIT_ALL_SOURCES_FAILED

    # Exports : Zotero JSON natif (format interne riche, consommable par MCP
    # / outils programmatiques), CSL-JSON (importable directement dans Zotero
    # Desktop via Fichier > Importer) et statistiques.
    # Publication atomique, manifeste en dernier (OPT-16) : requête, plan,
    # statuts, versions, empreintes, rangs et scores par notice ; jamais dans
    # les notices elles-mêmes.
    files = write_outputs(
        all_items, args.output, stem, args.query, run_id=run_id,
        started_at=started_at,
        manifest=harvest_manifest_fields(result, ctx=ctx,
                                         native_audit=native_audit),
    )
    output_json, output_csl, output_stats = (
        files.json_path, files.csl_path, files.stats_path)
    logger.info(f"Corpus Zotero JSON : {output_json} ({len(all_items)} items)")
    logger.info(f"Corpus CSL-JSON : {output_csl} ({files.csl_count} items)")
    logger.info(f"Statistiques : {output_stats}")
    logger.info(f"Manifeste : {files.manifest_path}")

    # Récapitulatif sortie standard
    print("\n" + "=" * 60)
    print(f"Terminé. {len(all_items)} items uniques.")
    print("Statut par source :")
    for line in status_lines:
        print(line)
    for line in result.flow_outage_lines():
        print(f"ATTENTION : {line}")
    incomplete = result.not_complete
    if incomplete:
        print(f"ATTENTION : {len(incomplete)} source(s) incomplète(s) ou non "
              f"exécutée(s) ; le corpus ne couvre pas tout le périmètre demandé.")
    if not plan.trivial:
        # Plan de requête (OPT-11) : requête envoyée par source, filtres,
        # limites de traduction.
        for line in plan.text_lines():
            print(line)
    # Classement (OPT-13) : premières notices et leur justification ; le
    # fichier de corpus garde son ordre.
    for line in ranking_text_lines(getattr(result, "ranking", None), all_items):
        print(line)
    print(f"Zotero JSON (interne)  : {output_json}")
    print(f"CSL-JSON (Zotero IMPORT) : {output_csl}")
    print(f"Statistiques            : {output_stats}")
    print(f"Manifeste               : {files.manifest_path}")
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
