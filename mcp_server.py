"""
Serveur MCP (Model Context Protocol) pour le moissonneur SHS.

Ce serveur expose les fonctionnalités du moissonneur multi-sources
sous forme d'outils invocables par un client MCP (Claude Desktop ou tout
client compatible).

Périmètre :
- Douze sources (registre ``connectors/registry.py``) : sept francophones
  (HAL, Isidore, OpenEdition, Cairn, Persée, Theses.fr, Érudit), quatre
  internationales (CrossRef, OpenAlex, Semantic Scholar, CORE) et Google
  Scholar par SerpAPI, facturé à la requête et hors des groupes
  ``international`` et ``all``.
- Douze outils : les neuf historiques (moisson_search, corpus_analyze,
  corpus_list, fetch_by_doi, search_by_author, corpus_merge, corpus_to_csl,
  corpus_filter, corpus_resolve_dois) et corpus_export, corpus_query,
  corpus_diff. Quatre prompts et des ressources ``corpus://<nom>``
  (paginées par ``?offset=&limit=``).
- Un même ``Server`` alimente deux transports :
  * **stdio** (défaut, ``run_stdio``) : serveur local lancé par le client,
    retour en digest (``inline``), chemins de fichiers non confinés ;
  * **HTTP distant** (``serve_http``, ``moisson-shs serve --http``) :
    Streamable HTTP derrière un proxy TLS, authentification Bearer ou
    OAuth, chemins confinés sous ``OUTPUT_DIR``, aucun chemin disque ni
    JSON bibliographique dans les réponses, seulement des liens de
    téléchargement (``/download``).

Installation locale (voir MCP_INSTALL.md) :
1. Installer le paquet avec l'extra MCP, borné par les contraintes :
       pip install -e ".[mcp]" -c constraints.txt
2. Déclarer le serveur dans le client : ``moisson-shs setup`` écrit
   l'entrée ``mcpServers.moisson-shs`` de claude_desktop_config.json
   (interpréteur du venv, ``MOISSON_OUTPUT_DIR``, clés d'API dans ``env``).
3. Redémarrer le client.

Déploiement distant : REMOTE.md et DEPLOY.md.

Le serveur peut aussi être lancé manuellement pour tests :
    moisson-shs serve            (stdio)
    python mcp_server.py         (stdio)

Référence MCP : https://modelcontextprotocol.io
"""

import asyncio
import dataclasses
import functools
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

# Imports du SDK MCP officiel
from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

# Imports internes au moissonneur
# Le serveur doit être lancé depuis le répertoire racine du projet
sys.path.insert(0, str(Path(__file__).parent))

from _version import __version__
from utils.build_info import describe as describe_build
from utils.http import HTTPClient, OperationCancelled, cache_enabled_from_env
from utils.deduplication import deduplicate
from utils.lookups import fetch_by_doi, search_by_author
from utils.corpus_ops import (
    CorpusFormatError, check_zotero_corpus, format_merge_report,
    merge_corpora_detailed,
)
from connectors.registry import (
    CONNECTORS, resolve_sources, list_source_tokens, FRANCOPHONE_SOURCES,
)
from mappers.csl_exporter import export_corpus_to_csl
# Moteur commun (OPT-07) : _run_moisson_sync n'est plus qu'un adaptateur.
from engine.models import (
    MODE_BIBLIOGRAPHIE, MODES, RunContext, SearchRequest, new_run_id,
)
from engine.orchestrator import harvest
from engine.outputs import cleanup_raw, corpus_stem, write_outputs
# Manifeste, publication atomique, index dérivé, audit des réponses natives
# (OPT-16).
from utils.http import attach_native_audit
from utils.manifest import (
    KIND_AUTHOR, KIND_CSL, KIND_DOI, KIND_FILTER, KIND_LABELS, KIND_MERGE,
    STATUS_LABELS, LockTimeout, Publication, directory_lock,
    harvest_manifest_fields, inherited_records, list_corpora, load_index,
    load_manifest_for, manifest_describes, merge_input_entry, merged_records,
    native_audit_for_run, now_iso, simple_records,
)
# Classement des notices (OPT-13) : argument ranking, structuredContent.top_ranked.
from utils.ranking import RANKING_MODES, top_ranked_payload
from utils.sandbox import (
    resolve_in_output_dir, validate_suffix, PathSecurityError,
)
from utils.output_files import (
    PUBLISHABLE_SUFFIXES, ROLE_CSL, ROLE_DOI_REPORT, ROLE_STATS, ROLE_ZOTERO,
    corpus_kind, corpus_name_from_uri, corpus_stem_of, corpus_uri,
    export_media_type, is_associated_file, is_corpus_file, is_downloadable_file,
    is_publishable_file, is_service_path, iter_json_array,
    corpus_page_uri, parse_corpus_uri,
)
# Retour digest et consultation locale (OPT-17) : digest classé des
# producteurs, outil corpus_query, pages de ressource.
from utils import corpus_query as CQ
from utils.corpus_query import (
    INLINE_DIGEST, INLINE_FULL, INLINE_MODES, INLINE_NONE, DigestSpec,
    QueryError,
)
from config import (
    MCP_DEFAULT_MAX_PER_SOURCE, MCP_DEFAULT_WORKERS, MCP_CACHE_TTL,
    MCP_CACHE_PRUNE_INTERVAL, MCP_CACHE_MAX_MB,
    HARVEST_MAX_WORKERS, ISIDORE_RESULT_WINDOW,
    HTTP_HOST, HTTP_PORT, HTTP_PATH, HTTP_STATELESS, HTTP_JSON_RESPONSE,
    ALLOWED_HOSTS, ALLOWED_ORIGINS, HTTP_MAX_BODY, HTTP_RATE_LIMIT_PER_MIN,
    DOWNLOAD_PREFIX,
    MOISSON_MAX_ITEMS_PER_SOURCE, MOISSON_MAX_CONCURRENT,
    MOISSON_ACQUIRE_TIMEOUT, MOISSON_DEADLINE_SECONDS,
    MOISSON_PROGRESS_SEND_TIMEOUT,
    OAUTH_ENABLED, OAUTH_PASSWORD,
    CORPUS_LIST_DEFAULT_LIMIT, CORPUS_LIST_MAX_LIMIT,
)
from config import PUBLIC_BASE_URL as _CONFIG_PUBLIC_BASE_URL


# === Configuration du logger ===
# Important : sur stdio, tout doit aller vers stderr car stdout
# est réservé au protocole JSON-RPC MCP.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("moisson_mcp")


# === Répertoire de sortie ===
# Configurable via variable d'environnement, défaut au répertoire courant
OUTPUT_DIR = Path(os.environ.get(
    "MOISSON_OUTPUT_DIR",
    str(Path(__file__).parent / "output")
))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# === Seuil de volumétrie pour le JSON inline (retour MCP enrichi) ===
# Au-delà de ce nombre d'items, le corpus n'est PAS inliné dans la réponse
# (anti-saturation du contexte) : seul le ResourceLink est émis. L'agent
# peut alors suivre le lien via la resource corpus://, où read_resource
# applique en plus sa propre garde de 2 Mo (troncature 50 items).
# Surchargeable par la variable d'environnement MOISSON_INLINE_MAX_ITEMS.
INLINE_MAX_ITEMS = int(os.environ.get("MOISSON_INLINE_MAX_ITEMS", "300"))

# Plafond en OCTETS UTF-8 du JSON inline, en plus du plafond en items : 300
# notices aux résumés longs dépassent sans peine plusieurs mégaoctets. Mesuré
# sur le JSON effectivement émis (sérialisé une seule fois). Défaut aligné sur
# la garde de `read_resource` (RESOURCE_MAX_BYTES). Surchargeable par la
# variable d'environnement MOISSON_INLINE_MAX_BYTES.
INLINE_MAX_BYTES = int(os.environ.get("MOISSON_INLINE_MAX_BYTES", "2000000"))

# Garde de `read_resource` : au-delà de cette taille de fichier (en octets,
# connue par stat() avant toute lecture), le corpus est renvoyé tronqué à ses
# RESOURCE_TRUNCATED_ITEMS premiers items, lus par blocs, jamais en entier.
RESOURCE_MAX_BYTES = 2_000_000
RESOURCE_TRUNCATED_ITEMS = 50


# === Registre des fichiers exposés en corpus:// ===
# Le schéma d'URI `corpus://<filename>` est un espace de noms plat résolu par
# `read_resource` contre OUTPUT_DIR. Or certains outils producteurs écrivent
# AILLEURS : `corpus_filter` écrit à côté du corpus source, `corpus_to_csl` et
# `corpus_merge` honorent un `output_path` arbitraire. Sans registre, le lien
# `corpus://` émis pour ces fichiers serait irrésoluble (round-trip cassé).
# On mémorise donc, par nom de base, le chemin absolu de chaque fichier exposé,
# pour que `read_resource` retrouve les fichiers hors OUTPUT_DIR. Processus
# unique et longue durée : taille négligeable (basename → Path).
# Périmètre (OPT-02) : alimenté et consulté en STDIO seulement ; en HTTP, le
# repli est refusé (confinement strict, comme /download). Jamais de fichier de
# service ni de fichier associé.
_EMITTED_RESOURCES: dict[str, Path] = {}


# === Mode de transport (process-level, immuable après démarrage) ===
# "stdio" (défaut, Claude Desktop local) ou "http" (serveur distant). Posé UNE
# fois au démarrage par `serve_http()` ; jamais modifié en cours de process.
# ⚠️ Un GLOBAL process — pas un ContextVar — car les outils de moisson tournent
# dans un thread d'executor que les contextvars ne traversent pas (cf. Sprint
# 08). En mono-utilisateur, le mode ne change pas pendant la vie du process.
TRANSPORT_MODE = "stdio"

# Base URL HTTPS publique pour fabriquer les URLs de téléchargement en mode HTTP
# (ex. "https://moisson.example.org"). Posée par `serve_http()` depuis
# MOISSON_PUBLIC_BASE_URL. Sans valeur en stdio (None).
PUBLIC_BASE_URL = None


def _safe_path(raw, *, must_exist: bool = False) -> Path:
    """
    Résout un chemin utilisateur en l'adaptant au transport.

    - **HTTP** : confine strictement le chemin sous `OUTPUT_DIR`
      (`resolve_in_output_dir`) — refuse traversal/absolu hors zone/symlink
      sortant (`PathSecurityError`).
    - **stdio** : chemin libre (`Path(raw)`) — process local de confiance,
      comportement Sprint 05 inchangé.

    C'est le SEUL point qui conditionne la sandbox au transport ; tous les
    outils à I/O disque l'utilisent pour leur chemin d'entrée, puis dérivent
    leurs chemins d'écriture depuis le `Path` retourné (déjà confiné en HTTP).
    """
    if TRANSPORT_MODE == "http":
        return resolve_in_output_dir(raw, OUTPUT_DIR, must_exist=must_exist)
    return Path(raw)


# === Observabilité (Sprint 11) : métriques, audit, logging JSON ===

# Horloge de démarrage (uptime de /healthz). En Python régulier — sans rapport
# avec la restriction des scripts de workflow.
_START_MONOTONIC = time.monotonic()

# Compteurs en mémoire (perdus au redémarrage ; cohérent workers=1). Best-effort.
# Statut réel de chaque appel (OPT-07) : ok, partial (succès partiel nommé :
# au moins une source incomplète), error (isError), cancelled (annulation
# ou échéance, isError aussi).
_METRICS = {
    "calls_total": 0,
    "errors_total": 0,
    "partial_total": 0,
    "cancelled_total": 0,
    "by_tool": {},            # tool -> {"calls", "errors", "partial", "cancelled"}
    "durations_ms": [],       # borné (p50/p95)
}
_DURATIONS_CAP = 1000

# Redaction : jamais de jeton Bearer en clair dans les logs.
_BEARER_RE = re.compile(r"(?i)(bearer\s+)\S+")
_AUTH_RE = re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)\S+")


def _redact_secrets(text: str) -> str:
    """Masque tout `Bearer <jeton>` / `Authorization: <…>` dans un texte."""
    text = _BEARER_RE.sub(r"\1***", text)
    text = _AUTH_RE.sub(r"\1***", text)
    return text


class _JsonLogFormatter(logging.Formatter):
    """Formatter JSON sur une ligne (stderr), avec redaction des secrets et
    champs structurés optionnels (request_id, tool, duration_ms, status,
    client_id) injectés via ``extra=``."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": _redact_secrets(record.getMessage()),
        }
        for k in ("request_id", "tool", "duration_ms", "status", "client_id"):
            v = getattr(record, k, None)
            if v is not None:
                payload[k] = v
        if record.exc_info:
            payload["exc"] = _redact_secrets(
                self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_json_logging() -> None:
    """Bascule le logging racine en JSON structuré sur stderr (mode serveur
    distant). Remplace le format plat du basicConfig."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonLogFormatter())
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _record_metrics(tool: str, status: str, duration_ms: float) -> None:
    """
    Compte un appel d'outil selon son statut réel : ``ok``, ``partial``,
    ``error`` ou ``cancelled`` (une annulation est aussi une erreur MCP).
    """
    _METRICS["calls_total"] += 1
    bt = _METRICS["by_tool"].setdefault(
        tool, {"calls": 0, "errors": 0, "partial": 0, "cancelled": 0})
    for key in ("partial", "cancelled"):
        bt.setdefault(key, 0)
    bt["calls"] += 1
    if status in ("error", "cancelled"):
        _METRICS["errors_total"] += 1
        bt["errors"] += 1
    if status == "partial":
        _METRICS["partial_total"] = _METRICS.get("partial_total", 0) + 1
        bt["partial"] += 1
    if status == "cancelled":
        _METRICS["cancelled_total"] = _METRICS.get("cancelled_total", 0) + 1
        bt["cancelled"] += 1
    d = _METRICS["durations_ms"]
    d.append(duration_ms)
    if len(d) > _DURATIONS_CAP:
        del d[0:len(d) - _DURATIONS_CAP]


def _percentile(values: list, pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((pct / 100.0) * (len(s) - 1)))))
    return s[k]


def _metrics_snapshot() -> dict:
    d = _METRICS["durations_ms"]
    return {
        "calls_total": _METRICS["calls_total"],
        "errors_total": _METRICS["errors_total"],
        "partial_total": _METRICS.get("partial_total", 0),
        "cancelled_total": _METRICS.get("cancelled_total", 0),
        "by_tool": {k: dict(v) for k, v in _METRICS["by_tool"].items()},
        "duration_ms_p50": _percentile(d, 50),
        "duration_ms_p95": _percentile(d, 95),
    }


def _get_version_string() -> str:
    """Version du paquet, lue dans ``_version.py`` (PAT-03) et non dans les
    métadonnées d'installation, qui peuvent retarder sur le code."""
    return __version__


def _audit_client_id() -> str | None:
    """client_id authentifié (depuis le contexte d'auth), ou None."""
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
        tok = get_access_token()
        return tok.client_id if tok else None
    except Exception:
        return None


def _write_audit(record: dict) -> None:
    """Écrit une ligne JSONL d'audit dans OUTPUT_DIR/.audit/audit-YYYYMMDD.jsonl
    (best-effort, jamais bloquant)."""
    try:
        audit_dir = OUTPUT_DIR / ".audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now().strftime("%Y%m%d")
        path = audit_dir / f"audit-{day}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


# === Erreurs d'outil (OPT-07, constat C30) ===
#
# Un outil qui échoue renvoie un résultat MCP ``isError=True`` (le SDK 1.x
# transmet tel quel un ``CallToolResult`` renvoyé par le gestionnaire
# ``call_tool``), et non plus un texte « Erreur : … » présenté comme un succès.
# Les fonctions ``_tool_*`` signalent l'échec par deux marqueurs, que le
# dispatcher convertit ; ils restent compatibles avec les anciens retours
# (``str`` et tuple ``(blocs, structuré)``), si bien qu'un appelant interne
# qui lisait le texte ou dépliait le tuple continue de fonctionner.

class ToolError(str):
    """
    Texte d'erreur d'un outil (sous-classe de ``str``).

    Attributes:
        structured: ``structuredContent`` joint à l'erreur (facultatif), par
            exemple le statut de chaque source d'une moisson.
        cancelled: vrai pour une annulation ou une échéance (métriques).
    """

    structured = None
    cancelled = False

    def __new__(cls, text: str, structured: dict | None = None,
                cancelled: bool = False):
        obj = super().__new__(cls, text)
        obj.structured = structured
        obj.cancelled = cancelled
        return obj


class ToolErrorContent(tuple):
    """Retour enrichi ``(blocs, structuré)`` marqué comme erreur d'outil."""


def _error_result(result) -> types.CallToolResult:
    """``CallToolResult`` (``isError=True``) d'un marqueur d'erreur d'outil."""
    if isinstance(result, ToolErrorContent):
        blocks, structured = result
        text_blocks = list(blocks)
    else:
        text = str(result)
        if TRANSPORT_MODE == "http":
            text = _redact_paths_in_text(text)
        text_blocks = [types.TextContent(type="text", text=text)]
        structured = result.structured
    if structured is not None and TRANSPORT_MODE == "http":
        structured = _scrub_paths_for_http(dict(structured))
    return types.CallToolResult(content=text_blocks,
                                structuredContent=structured, isError=True)


def _is_tool_error(result) -> bool:
    return isinstance(result, (ToolError, ToolErrorContent))


# === Instanciation du serveur MCP ===
# Version du paquet (PAT-03) : sans elle, le SDK annonce sa propre version
# dans serverInfo.version (1.27.1 constaté le 2026-09-30).
server = Server("moisson-shs", version=__version__)


def _digest_arguments(export: bool = False) -> dict:
    """
    Arguments ``inline`` et ``top`` des outils producteurs (OPT-17) : retour
    digest par défaut en stdio, ``full`` = retour du sprint 05, ``none`` =
    résumé et liens ; sans effet en HTTP (liens de téléchargement seuls).
    """
    full = ("le texte exporté inliné sous les plafonds historiques "
            f"({INLINE_MAX_ITEMS} notices, {INLINE_MAX_BYTES // 1_000_000} Mo)"
            if export else
            "le corpus complet inliné en JSON sous les plafonds historiques "
            f"({INLINE_MAX_ITEMS} notices, {INLINE_MAX_BYTES // 1_000_000} Mo)")
    digest = ("résumé, lien vers le fichier et les notices exportées en tête "
              "(clé de citation, titre, auteurs, année, support, identifiant)"
              if export else
              "résumé, liens vers les fichiers et les notices en tête dans "
              "l'ordre du classement (rang, titre, trois auteurs, année, "
              "support, type, DOI ou identifiant, sources et rang natif, "
              "termes de la requête appariés, réserves)")
    return {
        "inline": {
            "type": "string",
            "enum": list(INLINE_MODES),
            "default": CQ.DEFAULT_INLINE,
            "description": (
                "Contenu du retour en local (stdio). 'digest' : " + digest
                + f", au plus {CQ.DIGEST_MAX_BYTES // 1000} Ko, métadonnées "
                "seulement ; 'full' : " + full + " ; 'none' : résumé et liens "
                f"seulement. Défaut : '{CQ.DEFAULT_INLINE}'. L'en-tête du "
                "digest (« k sur N ») donne l'appel corpus_query : notices "
                "complètes (résumé entier) par ids, suite par offset=k ; une "
                "note signale une coupe au plafond d'octets. En digest, rang, "
                "score et identifiant de chaque notice sont dans "
                "structuredContent.top_items "
                "(top_ranked n'en garde que l'en-tête), le plan de requête "
                "et les statuts sont résumés, sans l'ordre ni les filtres "
                "par source (détail avec inline='full' et au manifeste). "
                "Sans effet en mode distant (liens de téléchargement "
                "seulement)."),
        },
        "top": {
            "type": "integer",
            "minimum": 1,
            "description": (
                f"Notices du digest (défaut {CQ.DIGEST_DEFAULT_TOP}, ramené à "
                f"{CQ.DIGEST_MAX_TOP} au plus ; le plafond d'octets peut en "
                "retenir moins, ce que le retour indique)."),
        },
    }


def _inline_args(args: dict) -> tuple:
    """``(inline, top)`` d'un appel de producteur, ou ``ToolError`` (valeur
    refusée). ``inline`` absent : ``CQ.DEFAULT_INLINE`` (stdio)."""
    try:
        return (CQ.resolve_inline(args.get("inline")),
                CQ.resolve_top(args.get("top")))
    except QueryError as e:
        return ToolError(f"Paramètre refusé : {e}")


# === Définition des outils exposés ===

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """
    Déclare les outils exposés par le serveur au client MCP.

    Chaque outil est décrit par :
    - un nom unique
    - une description (consommée par l'agent client pour décider de l'appel)
    - un schéma JSON Schema validant les arguments d'entrée

    Les descriptions disent ce que fait le code livré (OPT-20) : défauts lus
    dans ``config.py`` et ``utils.corpus_query``, régimes de requête d'après
    le registre ``connectors/capabilities.py`` (vérifiés source par source
    le 2026-09-29, CORE et Google Scholar d'après leur documentation),
    statuts d'après ``engine.models.STATUS_LABELS``. Les tests
    ``tests/unit/test_opt20_descriptions.py`` vérifient cette cohérence.
    """
    import config as _config
    from connectors.capabilities import VERIFIED_ON as _VERIFIED_ON

    # Fenêtre de résultats d'Isidore : 0 la désactive (config.py) ; seule la
    # borne de parcours ISIDORE_SCAN_MAX_RECORDS reste alors active.
    if ISIDORE_RESULT_WINDOW > 0:
        isidore_window = (
            f"qui ne sert que les {ISIDORE_RESULT_WINDOW} premiers résultats "
            "d'une requête, quel que soit max_per_source (statut "
            "« partielle », limite du fournisseur, pas une panne)")
        isidore_cap = (
            f"Sans effet au-delà de la fenêtre de {ISIDORE_RESULT_WINDOW} "
            "résultats d'Isidore pour Isidore et ses sous-sources. ")
    else:
        isidore_window = (
            "dont la fenêtre de résultats est levée par la configuration du "
            "serveur ; le parcours reste borné à "
            f"{_config.ISIDORE_SCAN_MAX_RECORDS} notices par requête (statut "
            "« partielle », borne de parcours, pas une panne)")
        isidore_cap = (
            "Isidore et ses sous-sources restent bornés par le parcours "
            f"({_config.ISIDORE_SCAN_MAX_RECORDS} notices par requête). ")

    return [
        types.Tool(
            name="moisson_search",
            description=(
                "Moisson bibliographique multi-sources à partir d'une "
                "requête. Douze sources : francophones (HAL, Isidore, "
                "OpenEdition, Cairn, Persée, Theses.fr, Érudit ; groupe "
                "'francophone', défaut), internationales (CrossRef, OpenAlex, "
                "Semantic Scholar, CORE ; groupe 'international' ; 'all' = "
                "les deux groupes) et Google Scholar par SerpAPI, facturé à "
                "la requête : absent des groupes 'international' et 'all', il "
                "ne s'active que nommé ('google_scholar') ou par le groupe "
                "'scholar'. Clés d'API : OpenAlex, CORE et Google Scholar les "
                "exigent (sans clé, la source est « non exécutée » et la "
                "moisson continue), Semantic Scholar la rend facultative. "
                "Produit un corpus Zotero JSON dédupliqué, sa version CSL-JSON "
                "(import Zotero Desktop), des statistiques et un manifeste "
                "(plan, statuts, rangs). "
                f"Par défaut : au plus {MCP_DEFAULT_MAX_PER_SOURCE} notices PAR "
                "source en mode 'bibliographie' (sélection, pas une collecte "
                "exhaustive : pour un corpus exhaustif, mode='collecte' et "
                "max_per_source plus élevé), sources interrogées en parallèle "
                f"({MCP_DEFAULT_WORKERS} à la fois, politesse par hôte "
                "respectée), cache HTTP actif (réponses réutilisées pendant "
                f"{MCP_CACHE_TTL // 3600} h) : de quelques secondes à une "
                "minute environ. En local, une échéance souple "
                "(deadline_seconds) arrête les sources qui n'ont pas fini "
                "(motif « deadline » ; notices déjà reçues gardées, "
                "« partielle ») et publie le corpus des autres (succès "
                "partiel). Plan de requête complet (requête envoyée à chaque "
                "source, filtres natifs et locaux, ordre appliqué, "
                "avertissements ; régimes par source : voir query) dans "
                "structuredContent.query_plan avec plan_only=true (sans rien "
                "exécuter), inline='full' ou en mode distant, et au "
                "manifeste ; en digest, plan résumé (requête envoyée quand "
                "elle diffère, traduction, avertissements), sans l'ordre ni "
                "les filtres par source. OpenEdition, Cairn, Persée, Érudit "
                "et Theses.fr passent par Isidore, "
                + isidore_window + ". Retour "
                "en local : digest des notices en tête du classement (inline, "
                "top, ranking ; suite et résumés par corpus_query) ; en mode "
                "distant, liens de téléchargement seulement. Statut de chaque "
                "source (complète, aucun résultat, partielle, non exécutée, en "
                "échec, annulée) et motif ; si aucune source n'aboutit, "
                "l'appel est une erreur (aucun corpus écrit)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Requête mot-clé, sans guillemets autour du thème "
                            "entier (des guillemets en font une expression "
                            "exacte, bien plus restrictive). Expressions exactes "
                            "entre guillemets (\"…\" ou « … »), opérateurs "
                            "AND/OR/NOT (ou ET/OU/SAUF, en majuscules), "
                            "parenthèses, préfixe '-' pour exclure. Précédence : "
                            "NOT, puis AND (écrit ou mots juxtaposés), puis "
                            "OR : « a b OR c » se lit « (a b) OR c » ; une "
                            "exclusion écrite au niveau d'une alternative porte "
                            "sur toute l'alternative (« a OR b -c » : « (a OR "
                            "b) -c ») ; mettre des parenthèses en cas de doute "
                            "(un avertissement du plan signale ces lectures). "
                            "Traduite dans la syntaxe de chaque source ; "
                            "régimes vérifiés source par source (sondes du "
                            f"{_VERIFIED_ON}) : HAL (syntaxe Solr conservée : "
                            "champ:valeur, plages champ:[a TO b], proximité "
                            "\"…\"~N, échappements) et OpenAlex honorent "
                            "guillemets, AND/OR/NOT et parenthèses ; Isidore "
                            "et ses sous-sources honorent guillemets, ET/OU/"
                            "SAUF et parenthèses (AND, OR, NOT y seraient lus "
                            "comme des mots : ils sont traduits) ; Semantic "
                            "Scholar honore guillemets, + | - et parenthèses "
                            "(AND et NOT écrits y sont ignorés : traduits) ; "
                            "CrossRef traite toute requête en sac de mots : il "
                            "reçoit une variante dérivée, termes exclus "
                            "retirés. D'après leur documentation seulement "
                            "(non vérifié en direct) : CORE honore guillemets, "
                            "AND/OR et parenthèses, mais pas NOT (exclusions "
                            "retirées) ; Google Scholar, guillemets, OR et "
                            "-terme. Accents significatifs chez CrossRef et "
                            "OpenAlex (écrire les mots accentués). Ce qui est "
                            "adapté ou retiré est signalé par un "
                            "avertissement ; la requête envoyée à chaque source "
                            "figure dans structuredContent.query_plan (filtres "
                            "natifs : plan complet, voir plan_only). Une "
                            "requête de mots "
                            "seuls est envoyée telle quelle partout."
                        ),
                    },
                    "max_per_source": {
                        "type": "integer",
                        "description": (
                            "Plafond du nombre de notices PAR source (chaque "
                            "source en rend au plus autant ; le corpus final, "
                            "dédupliqué, peut donc en compter plusieurs fois "
                            f"plus). Défaut : {MCP_DEFAULT_MAX_PER_SOURCE}, "
                            "adapté à une bibliographie et au délai d'un "
                            "appel d'outil. Pour un corpus exhaustif, "
                            "l'augmenter (1000 à 3000) : la moisson dure "
                            "alors plus longtemps. " + isidore_cap
                            + "Google Scholar est en outre borné à "
                            f"{_config.SERPAPI_MAX_PAGES} pages de "
                            f"{_config.SERPAPI_PAGE_SIZE} notices "
                            "(MOISSON_SERPAPI_MAX_PAGES ; un crédit SerpAPI "
                            "par page). En mode distant, plafonné par le "
                            "serveur. Atteint, le plafond est signalé dans le "
                            "statut de la source (liste non exhaustive)."
                        ),
                        "default": MCP_DEFAULT_MAX_PER_SOURCE,
                    },
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": list_source_tokens(),
                        },
                        "description": (
                            "Sources et/ou groupes à moissonner. Groupes : "
                            "'francophone' (défaut : HAL, Isidore et ses cinq "
                            "sous-sources), 'international' (CrossRef, "
                            "OpenAlex, Semantic Scholar, CORE), 'all' (les deux "
                            "groupes), 'scholar' (Google Scholar seul). Sources "
                            "individuelles : "
                            f"{', '.join(sorted(CONNECTORS.keys()))}. Google "
                            "Scholar, facturé à la requête (clé SerpAPI), "
                            "n'entre dans aucun autre groupe : le nommer, par "
                            "exemple ['all', 'google_scholar']. ['hal', "
                            "'isidore'] exclut les sous-sources d'Isidore."
                        ),
                        "default": list(FRANCOPHONE_SOURCES),
                    },
                    "enrich_oai": {
                        "type": "boolean",
                        "description": (
                            "Enrichit par OAI-PMH les notices d'OpenEdition, "
                            "Cairn, Persée et Érudit (métadonnées plus riches, "
                            "formats qdc, oai_openaire). Bien plus lent (temps "
                            "multiplié par 5 à 10) : en local, relever "
                            "deadline_seconds."
                        ),
                        "default": False,
                    },
                    "dedup": {
                        "type": "boolean",
                        "description": (
                            "Déduplication : identifiants partagés (DOI, y "
                            "compris celui porté par extra, identifiants HAL, "
                            "OAI, Isidore, OpenAlex, Semantic Scholar, CORE, "
                            "Scholar, URL canonique, ISBN d'un livre), sans "
                            "jamais fusionner deux DOI différents (sauf règles "
                            "optionnelles désactivées par défaut : co-diffusion "
                            "d'un même article, versions Zenodo) ; puis titre "
                            "égal ou très proche (similarité d'au moins 0,93) "
                            "corroboré par l'année (à un an près) et le "
                            "premier auteur. Chaque fusion est tracée dans "
                            "extra (Dedup evidence). Désactiver uniquement pour "
                            "audit ou comparaison."
                        ),
                        "default": True,
                    },
                    "from_date": {
                        "type": "string",
                        "description": (
                            "Date minimale incluse (AAAA ou AAAA-MM-JJ). "
                            "Filtre natif chez HAL et CrossRef (au jour), "
                            "OpenAlex, Semantic Scholar, CORE et Google "
                            "Scholar (à l'année : bornes ramenées à l'année, "
                            "signalé) ; filtre local sur l'année des notices "
                            "pour Isidore et ses sous-sources (notices sans "
                            "année écartées)."
                        ),
                    },
                    "to_date": {
                        "type": "string",
                        "description": (
                            "Date maximale incluse (AAAA ou AAAA-MM-JJ) ; "
                            "mêmes règles que from_date."
                        ),
                    },
                    "cache": {
                        "type": "boolean",
                        "description": (
                            "Cache HTTP local des réponses des sources "
                            "(rejeu rapide d'une moisson identique ou "
                            "voisine). ACTIVÉ par défaut, avec une durée de "
                            f"vie de {MCP_CACHE_TTL // 3600} h : une réponse "
                            "plus ancienne est redemandée. false : réponses "
                            "fraîches (aucune lecture ni écriture du cache) ; "
                            "true : activé ; absent : activé, sauf si le "
                            "serveur fixe MOISSON_CACHE."
                        ),
                    },
                    "workers": {
                        "type": "integer",
                        "description": (
                            "Nombre de sources interrogées en même temps "
                            f"(défaut : {MCP_DEFAULT_WORKERS} ; 1 = une à "
                            f"une ; ramené à {HARVEST_MAX_WORKERS} au plus). "
                            "Un seul pool sur toutes les sources, sans "
                            "attendre la fin des unes pour lancer les "
                            "autres ; la politesse envers chaque serveur "
                            "(intervalle entre requêtes, requêtes "
                            "simultanées) s'applique quel que soit ce nombre."
                        ),
                        "default": MCP_DEFAULT_WORKERS,
                    },
                    "mode": {
                        "type": "string",
                        "enum": list(MODES),
                        "description": (
                            "Mode de moisson. 'bibliographie' (défaut) : "
                            "sélection plafonnée par source, dans l'ordre "
                            "natif de chacune : pertinence chez HAL (score), "
                            "CrossRef, OpenAlex, Google Scholar et Isidore "
                            "(ordre par défaut, présumé), citations "
                            "décroissantes chez Semantic Scholar (pas un "
                            "ordre de pertinence), ordre de l'API chez CORE. "
                            "'collecte' : ordre déterministe et reproductible "
                            "(HAL par identifiant de dépôt, CrossRef dans "
                            "l'ordre de l'index, Semantic Scholar par "
                            "identifiant), exhaustivité dans la limite du "
                            "plafond : une source plafonnée garde alors ses "
                            "premiers enregistrements dans cet ordre, pas les "
                            "plus pertinents. L'ordre appliqué par chaque "
                            "source (ordering) figure dans le plan complet "
                            "(structuredContent.query_plan avec "
                            "plan_only=true ou inline='full' ; manifeste), "
                            "pas dans le plan résumé du digest."
                        ),
                        "default": MODE_BIBLIOGRAPHIE,
                    },
                    "query_variants": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": (
                            "Variantes explicites de la requête par langue "
                            "(codes ISO 639-1), ex. {\"en\": \"\\\"digital "
                            "discourse\\\"\"}. Une source dont le fonds est "
                            "majoritairement dans cette langue reçoit la "
                            "variante ('fr' : sources francophones ; 'en' : "
                            "CrossRef, OpenAlex, Semantic Scholar, CORE). "
                            "AUCUNE traduction automatique : sans variante "
                            "'en', les sources internationales reçoivent la "
                            "requête d'origine (le plan le signale), même avec "
                            "sources=['all']. Google Scholar, au fonds "
                            "multilingue, reçoit toujours la requête "
                            "d'origine."
                        ),
                    },
                    "languages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Langues des documents (codes ISO 639-1, ex. "
                            "['fr', 'en']). Filtre natif là où la source le "
                            "permet (HAL, OpenAlex, Google Scholar), filtre "
                            "local sur la langue des notices ailleurs "
                            "(Isidore et ses sous-sources, CORE : notices sans "
                            "langue conservées), impossible pour CrossRef et "
                            "Semantic Scholar (signalé). Absent : aucun "
                            "filtre."
                        ),
                    },
                    "plan_only": {
                        "type": "boolean",
                        "description": (
                            "Aperçu du plan de requête SANS exécution : "
                            "requête envoyée à chaque source, filtres natifs "
                            "et locaux, ordre appliqué, avertissements (plan "
                            "complet, dans structuredContent.query_plan) ; "
                            "aucune requête réseau, aucun fichier écrit."
                        ),
                        "default": False,
                    },
                    "hal_fulltext": {
                        "type": "boolean",
                        "description": (
                            "HAL cherche aussi dans le texte intégral des "
                            "dépôts (fulltext_t ; 36 % des dépôts ont un "
                            "fichier indexé). Sans lui, HAL ne cherche que "
                            "dans les métadonnées (titre, résumé, mots-clés). "
                            "Ensemble bien plus large et plus bruité : "
                            "« \"modèle actantiel\" » rend 7 notices sans, "
                            "228 avec. Sans effet sur les autres sources."
                        ),
                        "default": False,
                    },
                    "on_ambiguity": {
                        "type": "string",
                        "enum": ["warn", "refuse"],
                        "description": (
                            "Requête ambiguë (OR mêlé sans parenthèses à des "
                            "termes juxtaposés, à AND ou à une exclusion) : "
                            "« warn » l'envoie avec un avertissement en tête "
                            "du retour, « refuse » ne l'envoie pas. Défaut : "
                            "« refuse » quand une source facturée (Google "
                            "Scholar) est demandée, « warn » sinon. plan_only "
                            "ne refuse jamais."
                        ),
                    },
                    # Échéance souple en stdio (OPT-09 bis).
                    "deadline_seconds": _soft_deadline_schema(),
                    "ranking": {
                        "type": "string",
                        "enum": list(RANKING_MODES),
                        "description": (
                            "Classement des notices après déduplication, "
                            "rendu à part (structuredContent.top_ranked et "
                            "manifeste : rang, score, identifiant, position "
                            "dans le corpus, version) : ni les notices ni "
                            "l'ordre du fichier de corpus ne changent ; il "
                            "fixe l'ordre du digest ('off' : ordre du "
                            "fichier). 'native' (défaut) : "
                            "fusion des rangs natifs des sources qui classent "
                            "réellement par pertinence (HAL et CrossRef en "
                            "mode bibliographie, OpenAlex ; Isidore avec un "
                            "poids moindre, ordre présumé) ; Semantic Scholar "
                            "(trié par citations), CORE et Google Scholar "
                            "(aucun rang de pertinence publié) n'y pèsent pas, "
                            "et les citations n'entrent jamais dans le score. "
                            "'local' : correspondance des concepts de la "
                            "requête dans titre, mots-clés et résumé, combinée "
                            "au classement natif ; expérimental, désactivé "
                            "par défaut tant que son évaluation sur des "
                            "jugements de pertinence n'est pas faite. 'off' : "
                            "aucun classement. Absent : valeur du serveur "
                            "(MOISSON_RANKING), 'native' sinon."
                        ),
                    },
                    # Retour digest (OPT-17).
                    **_digest_arguments(),
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="corpus_analyze",
            description=(
                "Statistiques d'un corpus Zotero JSON interne déjà produit "
                "(.json) : volume ; couverture DOI (DOI porté par extra "
                "compris) et résumé (un extrait Google Scholar ne compte pas "
                "comme résumé) ; répartitions par source, type, langue et "
                "année ; les 20 revues et les 30 auteurs les plus fréquents. "
                "Les recouvrements entre paires de sources figurent dans le "
                "fichier de statistiques (_stats.txt) écrit avec chaque "
                "moisson. Refuse un fichier CSL-JSON (.csl.json), un "
                "manifeste ou un rapport."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "corpus_path": {
                        "type": "string",
                        "description": (
                            "Corpus Zotero JSON interne (corpus_*.json) : "
                            "chemin du fichier (structuredContent.files du "
                            "producteur, en local) ; en mode distant, nom du "
                            "fichier dans le répertoire des corpus (celui de "
                            "corpus_list)."
                        ),
                    },
                },
                "required": ["corpus_path"],
            },
        ),
        types.Tool(
            name="corpus_list",
            description=(
                "Liste les corpus disponibles dans le répertoire de sortie "
                "configuré (MOISSON_OUTPUT_DIR), du plus récent au plus "
                "ancien, par défaut les "
                f"{CORPUS_LIST_DEFAULT_LIMIT} derniers. Renvoie pour chaque "
                "corpus : nom, date, type de production (moisson, filtre, "
                "fusion…), statut de publication (complet, incomplet, "
                "ancien), nombre d'items, requête d'origine et fichiers "
                "(Zotero JSON interne, CSL-JSON, statistiques). Lecture d'un "
                "index tenu à jour, sans relire chaque corpus."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "default": CORPUS_LIST_DEFAULT_LIMIT,
                        "description": (
                            "Nombre maximal de corpus affichés (défaut "
                            f"{CORPUS_LIST_DEFAULT_LIMIT}, au plus "
                            f"{CORPUS_LIST_MAX_LIMIT})."
                        ),
                    },
                    "pattern": {
                        "type": "string",
                        "description": (
                            "Filtre : motif (* et ? acceptés) ou fragment, "
                            "insensible à la casse et aux accents, cherché "
                            "dans le nom du corpus, ses fichiers et sa "
                            "requête."
                        ),
                    },
                    "since": {
                        "type": "string",
                        "description": (
                            "Filtre : corpus créés à partir de cette date "
                            "(AAAA, AAAA-MM, AAAA-MM-JJ ou date-heure ISO)."
                        ),
                    },
                    "rebuild": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Reconstruit entièrement l'index des corpus "
                            "(relit chaque manifeste et recompte les corpus "
                            "anciens) avant de lister."
                        ),
                    },
                },
            },
        ),
        types.Tool(
            name="fetch_by_doi",
            description=(
                "Récupère une notice Zotero complète à partir d'un DOI : "
                "CrossRef, HAL, Isidore, OpenAlex, Semantic Scholar et CORE "
                "(OpenAlex et CORE seulement si leur clé est configurée, "
                "sinon status 'ignore'), interrogés en parallèle ; une notice "
                "qui ne "
                "porte pas le DOI demandé est écartée ; les notices trouvées "
                "sont fusionnées selon la priorité des sources, quel que soit "
                "l'ordre des réponses. Idéal pour ajouter ponctuellement une "
                "référence à un corpus. Le retour donne l'issue de chaque "
                "source et distingue un DOI inconnu des sources (status "
                "'non_trouve') d'une erreur réseau (status 'erreur', absence "
                "non établie)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "doi": {
                        "type": "string",
                        "description": (
                            "DOI canonique (ex : 10.4000/lidil.6033) "
                            "ou URL DOI (https://doi.org/10.x/y)."
                        ),
                    },
                    "add_to_corpus": {
                        "type": "string",
                        "description": (
                            "Chemin optionnel d'un corpus Zotero JSON interne "
                            "existant auquel ajouter la notice : elle y est "
                            "fusionnée (déduplication), puis le corpus et son "
                            ".csl.json sont réécrits."
                        ),
                    },
                },
                "required": ["doi"],
            },
        ),
        types.Tool(
            name="search_by_author",
            description=(
                "Recherche la production d'un chercheur sur HAL, Isidore "
                "(qui couvre aussi OpenEdition, Cairn, Persée, Érudit), "
                "OpenAlex (clé requise) et Semantic Scholar. Renvoie un "
                "corpus Zotero dédupliqué, trié par année décroissante (digest "
                "en local), et le statut de chaque source (plafond atteint, "
                "panne, source non exécutée, homonymes). production_complete "
                "vrai dit seulement que les sources interrogées ont été "
                "parcourues jusqu'au bout, sans plafond, panne ni identité "
                "incertaine ; la production réelle peut les dépasser. "
                "Les identifiants (idhal, orcid, openalex_author_id, "
                "s2_author_id, hal_person_id) priment sur le nom : une "
                "source dotée de son identifiant n'est pas interrogée par "
                "le nom. Sans nom : HAL par idhal, hal_person_id ou orcid ; "
                "OpenAlex par openalex_author_id ou orcid ; Semantic Scholar "
                "par s2_author_id seulement ; Isidore n'est pas interrogé. "
                "Homonymes : en recherche par le nom seul, si plusieurs "
                "profils d'au moins 5 travaux portent le nom, rien n'est "
                "moissonné (harvest_status 'ambiguous', aucun fichier) ; le "
                "retour liste les candidats (structuredContent.candidates : "
                "identifiant, affiliation, travaux, période, exemples de "
                "titres, argument à repasser) et les profils trouvés par les "
                "autres sources (profiles_to_confirm) ; il faut rappeler "
                "l'outil avec, pour chaque source, l'argument du candidat "
                "correspondant à l'identité choisie. Avec un identifiant, une "
                "source interrogée par le nom n'est retenue que si son profil "
                "porte l'ORCID fourni (sinon profil proposé à confirmer dans "
                "profiles_to_confirm). Isidore, sans identifiant d'auteur, ne "
                "garde que les notices dont les auteurs portent le nom "
                "(compteur rejected_not_author) et n'est pas séparé des "
                "homonymes. En digest, statuts et profils sont résumés "
                "(report_compact ; rapport complet au manifeste, indiqué par "
                "report_detail). Alimente la skill ProfilBiblio."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Nom complet du chercheur, sous la forme "
                            "« Prénom Nom » (ex : 'Patrick Charaudeau') ; "
                            "non vide sauf si un identifiant est fourni."
                        ),
                    },
                    "idhal": {
                        "type": "string",
                        "description": (
                            "Identifiant HAL de l'auteur (idHal) si connu : "
                            "plus précis que le nom pour HAL."
                        ),
                    },
                    "orcid": {
                        "type": "string",
                        "description": (
                            "ORCID (0000-XXXX-XXXX-XXXX, clé de contrôle "
                            "vérifiée) : HAL et OpenAlex par identifiant, "
                            "profil Semantic Scholar qui le porte. Fiable "
                            "pour OpenAlex ; rarement renseigné dans HAL et "
                            "Semantic Scholar : y préférer idhal ou "
                            "hal_person_id et s2_author_id."
                        ),
                    },
                    "openalex_author_id": {
                        "type": "string",
                        "description": (
                            "Identifiant d'auteur OpenAlex (A5023888391) ; "
                            "plusieurs séparés par des virgules si la même "
                            "personne a plusieurs profils."
                        ),
                    },
                    "s2_author_id": {
                        "type": "string",
                        "description": (
                            "Identifiant d'auteur Semantic Scholar (chiffres) ; "
                            "plusieurs séparés par des virgules."
                        ),
                    },
                    "hal_person_id": {
                        "type": "string",
                        "description": (
                            "Identifiant de personne HAL (chiffres), pour un "
                            "auteur HAL sans idHal (proposé parmi les "
                            "candidats)."
                        ),
                    },
                    "max_per_source": {
                        "type": "integer",
                        "description": (
                            "Plafond par source (OpenAlex et Semantic Scholar "
                            "paginés jusqu'à ce plafond). Défaut 200 ; "
                            "atteint, il est signalé (liste incomplète)."
                        ),
                        "default": 200,
                    },
                    "save_corpus": {
                        "type": "boolean",
                        "description": (
                            "Si vrai (défaut), enregistre le corpus dans le "
                            "répertoire des corpus sous un nom unique "
                            "(corpus_author_<nom>_<horodatage>_<uuid8>.json, "
                            "avec .csl.json, _stats.txt et .manifest.json). "
                            "Faux : aucun fichier ; le retour donne les "
                            "statuts et les dix publications les plus "
                            "récentes, sans digest."
                        ),
                        "default": True,
                    },
                    **_digest_arguments(),
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="corpus_merge",
            description=(
                "Fusionne plusieurs corpus Zotero JSON internes en un seul, "
                "avec la déduplication de moisson_search (identifiants "
                "partagés, jamais deux DOI différents, puis titre corroboré "
                "par l'année et le premier auteur), et produit un rapport "
                "(notices chargées, uniques, exclusives à chaque corpus). "
                "Écrit le corpus fusionné, sa version CSL-JSON et un "
                "manifeste. Refuse un fichier CSL-JSON (.csl.json), un "
                "manifeste ou un rapport en entrée."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "corpus_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Chemins des corpus à fusionner (Zotero JSON "
                            "interne, .json) ; en mode distant, noms de "
                            "fichiers du répertoire des corpus."
                        ),
                    },
                    "output_path": {
                        "type": "string",
                        "description": (
                            "Chemin du corpus fusionné (un fichier existant "
                            "est remplacé). Si omis : nom unique "
                            "corpus_merged_<horodatage>_<uuid8>.json dans le "
                            "répertoire des corpus."
                        ),
                    },
                    **_digest_arguments(),
                },
                "required": ["corpus_paths"],
            },
        ),
        types.Tool(
            name="corpus_to_csl",
            description=(
                "Convertit un corpus Zotero JSON interne en CSL-JSON "
                "(Citation Style Language JSON), format standard importable "
                "directement dans Zotero Desktop via Fichier > Importer. "
                "Utile pour les corpus produits sans version CSL (corpus "
                "anciens, corpus filtré par corpus_filter). En digest, les "
                "notices décrites sont celles du corpus Zotero source (même "
                "ordre), que corpus_query interroge. Refuse un fichier "
                "CSL-JSON, un manifeste ou un rapport en entrée."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "corpus_path": {
                        "type": "string",
                        "description": (
                            "Corpus Zotero JSON interne (corpus_*.json) : "
                            "chemin du fichier ; en mode distant, nom du "
                            "fichier dans le répertoire des corpus."
                        ),
                    },
                    "output_path": {
                        "type": "string",
                        "description": (
                            "Chemin du fichier CSL-JSON produit. Si omis : "
                            "<radical>.csl.json à côté du corpus source "
                            "(remplacé s'il existe, ajouté au manifeste du "
                            "corpus)."
                        ),
                    },
                    **_digest_arguments(),
                },
                "required": ["corpus_path"],
            },
        ),
        types.Tool(
            name="corpus_filter",
            description=(
                "Filtre un corpus Zotero JSON interne selon des critères "
                "cumulés : années, types Zotero, sources d'origine, langue, "
                "mot du titre. Écrit le sous-corpus "
                "<radical><output_suffix>.json à côté du corpus source, avec "
                "un manifeste qui garde les rangs de la moisson ; pas de "
                "version CSL (corpus_to_csl la produit). Refuse un fichier "
                "CSL-JSON, un manifeste ou un rapport en entrée."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "corpus_path": {
                        "type": "string",
                        "description": (
                            "Corpus Zotero JSON interne source : chemin du "
                            "fichier ; en mode distant, nom du fichier dans "
                            "le répertoire des corpus."
                        ),
                    },
                    "year_min": {
                        "type": "integer",
                        "description": (
                            "Année minimale incluse (notices sans année "
                            "écartées)."
                        ),
                    },
                    "year_max": {
                        "type": "integer",
                        "description": (
                            "Année maximale incluse (notices sans année "
                            "écartées)."
                        ),
                    },
                    "item_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Liste de types Zotero à conserver "
                            "(journalArticle, bookSection, book, thesis, "
                            "conferencePaper, etc.)."
                        ),
                    },
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Sources d'origine à conserver, comparées telles "
                            "quelles aux libellés de la ligne « Sources: » du "
                            "champ extra (casse, accents et parenthèses "
                            "compris, sans normalisation). Libellés écrits par "
                            "ce serveur : HAL, Isidore, OpenEdition, Cairn, "
                            "Persée, Theses.fr, Érudit, CrossRef, OpenAlex, "
                            "Semantic Scholar, CORE, Google Scholar (SerpAPI). "
                            "Un corpus ancien peut porter d'autres graphies "
                            "(« Persee », « Crossref ») : les reprendre "
                            "telles quelles (corpus_query, lui, les ramène à "
                            "la même source). Une notice est gardée si l'une "
                            "de ses sources figure dans la liste."
                        ),
                    },
                    "language": {
                        "type": "string",
                        "description": (
                            "Code langue (fr, en, es… ; « fre », « French » "
                            "reconnus), lu dans le champ language des notices. "
                            "Les notices sans langue lisible sont gardées et "
                            "comptées, comme dans la moisson (CrossRef, "
                            "Google Scholar n'en donnent pas) ; "
                            "keep_unknown_language: false les écarte."
                        ),
                    },
                    "keep_unknown_language": {
                        "type": "boolean",
                        "description": (
                            "Avec language : garder les notices sans langue "
                            "lisible (défaut vrai)."
                        ),
                        "default": True,
                    },
                    "title_contains": {
                        "type": "string",
                        "description": (
                            "Sous-chaîne (insensible à la casse et aux "
                            "accents) qui doit apparaître dans le titre."
                        ),
                    },
                    "output_suffix": {
                        "type": "string",
                        "description": (
                            "Suffixe ajouté au radical du fichier produit. "
                            "Défaut : '_filtered'. Un filtrage de même "
                            "suffixe sur le même corpus remplace le fichier "
                            "précédent : choisir un autre suffixe pour garder "
                            "les deux."
                        ),
                        "default": "_filtered",
                    },
                    **_digest_arguments(),
                },
                "required": ["corpus_path"],
            },
        ),
        types.Tool(
            name="corpus_resolve_dois",
            description=(
                "Complète les DOI manquants d'un corpus par appariement "
                "bibliographique contre CrossRef (gratuit, sans clé). Conçu "
                "pour les corpus Google Scholar, qui ne portent pas de DOI et "
                "ne peuvent donc pas passer par un ajout à Zotero par DOI "
                "(outil zotero_add_by_doi d'un serveur Zotero). "
                "L'appariement est volontairement strict : similarité de titre "
                "élevée, plus corroboration par l'année et le premier auteur. "
                "Chaque DOI résolu porte dans 'extra' une ligne 'DOI source' "
                "qui énonce le score obtenu, afin que l'appariement reste "
                "vérifiable après coup. Un DOI manquant est préférable à un "
                "DOI faux : en cas de doute, le champ reste vide. Tous les "
                "candidats CrossRef sont examinés : si plusieurs travaux "
                "distincts sont admissibles, la notice est déclarée ambiguë et "
                "aucun DOI n'est écrit. Erratum, compte rendu, réponse, autre "
                "tome ou partie, et prépublication pour une notice qui n'en "
                "est pas une ne sont jamais retenus ; une notice qui porte "
                "déjà un DOI, même mal formé, n'est pas modifiée. Écrit le "
                "corpus enrichi <radical><output_suffix>.json à côté du "
                "corpus source (avec sa version CSL-JSON si also_csl et un "
                "manifeste) ; le rapport complet (acceptés, rejetés, ambigus, "
                "sans candidat, erreurs, non traités, scores) est écrit dans "
                "<radical><output_suffix>.doi_report.json. Refuse un fichier "
                "CSL-JSON, un manifeste ou un rapport en entrée."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "corpus_path": {
                        "type": "string",
                        "description": (
                            "Corpus Zotero JSON interne source : chemin du "
                            "fichier ; en mode distant, nom du fichier dans "
                            "le répertoire des corpus."
                        ),
                    },
                    # Bornes alignées sur utils/doi_resolver.py
                    # (TITLE_THRESHOLD_FLOOR, STRICT_THRESHOLD_FLOOR,
                    # YEAR_TOLERANCE_CEILING) : les seuils se durcissent,
                    # ils ne s'abaissent jamais (refus explicite).
                    "title_threshold": {
                        "type": "number",
                        "minimum": 0.90,
                        "maximum": 1,
                        "description": (
                            "Similarité minimale des titres normalisés. Défaut "
                            "0.90, qui est aussi le plancher : une valeur "
                            "inférieure est refusée (un DOI faux est pire "
                            "qu'un DOI manquant). Une valeur supérieure durcit "
                            "l'appariement."
                        ),
                    },
                    "strict_title_threshold": {
                        "type": "number",
                        "minimum": 0.97,
                        "maximum": 1,
                        "description": (
                            "Seuil de titre exigé quand ni l'année ni l'auteur "
                            "ne sont vérifiables. Défaut et plancher 0.97."
                        ),
                    },
                    "year_tolerance": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 1,
                        "description": (
                            "Écart d'années toléré. Défaut 1 (publication en "
                            "ligne puis parution en numéro), qui est aussi le "
                            "plafond ; 0 durcit."
                        ),
                    },
                    "max_items": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Plafond de notices interrogées. Par défaut, "
                            "toutes celles qui n'ont pas de DOI. Les suivantes "
                            "sont déclarées 'non traité' dans le rapport."
                        ),
                    },
                    "max_seconds": {
                        "type": "number",
                        "minimum": 1,
                        "description": (
                            "Budget en temps (secondes). Défaut 240, plafond "
                            "900 (MOISSON_DOI_RESOLVE_MAX_SECONDS[_CEILING]). "
                            "À l'échéance, les notices restantes sont "
                            "déclarées 'non traité' ; relancer l'outil sur le "
                            "corpus produit les reprend."
                        ),
                    },
                    "output_suffix": {
                        "type": "string",
                        "description": (
                            "Suffixe ajouté au radical du corpus enrichi. "
                            "Défaut : '_doi'. Une résolution de même suffixe "
                            "sur le même corpus remplace les fichiers "
                            "précédents."
                        ),
                        "default": "_doi",
                    },
                    "also_csl": {
                        "type": "boolean",
                        "description": (
                            "Produire aussi la version CSL-JSON du corpus "
                            "enrichi, importable dans Zotero Desktop. "
                            "Défaut : vrai."
                        ),
                        "default": True,
                    },
                    **_digest_arguments(),
                },
                "required": ["corpus_path"],
            },
        ),
        # Exports BibTeX, BibLaTeX et RIS (OPT-18, tâche #20 du sprint 03).
        types.Tool(
            name="corpus_export",
            description=(
                "Exporte un corpus Zotero JSON interne en BibTeX, BibLaTeX "
                "ou RIS : fichiers importables dans Zotero Desktop (Fichier "
                "> Importer), JabRef ou EndNote, et utilisables avec LaTeX "
                "(BibTeX ou biber). Clés de citation stables et "
                "déterministes (nom + année + premier mot du titre, "
                "désambiguïsées a, b, c), échappement LaTeX, auteurs "
                "« Nom, Prénom » (auteurs institutionnels protégés), DOI, "
                "URL, ISSN/ISBN, pages, volume, numéro, langue, mots-clés ; "
                "champ 'annote' (note N1 en RIS) alimenté par une ligne "
                "'Annote:' du champ extra. Aucune métadonnée de moisson "
                "n'est exportée ; include_provenance=true ajoute seulement "
                "les lignes de provenance du champ extra (Sources, "
                "identifiants natifs, Cited by…) en note ; les rangs et les "
                "scores ne sont jamais exportés (ils restent dans le "
                "manifeste). Un extrait Google Scholar n'est jamais exporté "
                "comme résumé ; une notice dont Google Scholar a tronqué les "
                "auteurs ou la revue porte toujours un avertissement (note, "
                "N1 en RIS). L'entrée "
                "doit être le corpus Zotero interne (.json), pas sa version "
                "CSL-JSON (.csl.json) ni un fichier associé (manifeste, "
                "rapport DOI)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "corpus_path": {
                        "type": "string",
                        "description": (
                            "Chemin du corpus Zotero interne (corpus_*.json)."
                        ),
                    },
                    "format": {
                        "type": "string",
                        "enum": ["bibtex", "biblatex", "ris"],
                        "default": "bibtex",
                        "description": (
                            "Format produit : 'bibtex' (défaut : BibTeX "
                            "classique, le plus sûr pour Zotero, lu aussi "
                            "par biber), 'biblatex' (types et champs natifs "
                            "BibLaTeX : date ISO, journaltitle, langid) ou "
                            "'ris'."
                        ),
                    },
                    "output_path": {
                        "type": "string",
                        "description": (
                            "Fichier produit (extension .bib, ou .ris pour "
                            "le format RIS). Si omis : nom unique "
                            "<corpus>_<identifiant>.bib ou .ris, à côté du "
                            "corpus (dans le répertoire des corpus sur le "
                            "serveur distant)."
                        ),
                    },
                    "ascii": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "BibTeX et BibLaTeX : écrire les caractères "
                            "accentués en commandes LaTeX ({\\'e}, {\\c{c}}) "
                            "pour une chaîne BibTeX 8 bits ancienne. Défaut "
                            "faux : UTF-8 conservé (biber, Zotero). Sans "
                            "effet en RIS."
                        ),
                    },
                    "include_provenance": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Exporter aussi les lignes de provenance de "
                            "moisson du champ extra (Sources, identifiants "
                            "natifs…) dans 'note' (BibTeX) ou N1 (RIS). "
                            "N'exporte ni rang ni score (manifeste). "
                            "Défaut faux."
                        ),
                    },
                    **_digest_arguments(export=True),
                },
                "required": ["corpus_path"],
            },
        ),
        # Consultation locale d'un corpus (OPT-17).
        types.Tool(
            name="corpus_query",
            description=(
                "Consulte un corpus déjà produit sans le charger dans la "
                "conversation : recherche locale classée (mêmes formes que "
                "la requête de moisson : expressions exactes entre "
                "guillemets, OR, troncature « numéri* » ; casse, accents, "
                "élisions et pluriels ignorés ; titre, mots-clés et résumé ; "
                "un terme exclu par '-' ou NOT ne retire pas la notice : il "
                "divise son score par deux s'il figure dans le titre ou les "
                "mots-clés, et la réserve « terme exclu présent » la "
                "signale), filtres (année, type, langue, "
                "source, DOI ou résumé présents), choix des champs, tri et "
                "pagination stable (offset, limit : ni trou ni doublon d'une "
                "page à l'autre tant que le corpus ne change pas), et notices "
                f"complètes par identifiants (ids, au plus {CQ.QUERY_MAX_IDS} : "
                "seul moyen d'obtenir les résumés entiers). Identifiants : "
                "ceux du digest des outils producteurs (clé forte, ex. "
                "'doi:10.…', 'hal:hal-…') ; un DOI nu est accepté. Le rang du "
                "classement de la moisson (manifeste) est rendu à côté des "
                "notices, jamais dans les notices. Corpus Zotero interne "
                "(.json) seulement, pas sa version CSL-JSON. En mode distant, "
                "la sélection est écrite dans un fichier téléchargeable (URL) "
                "et le retour ne donne que ses métadonnées ; ce fichier est "
                f"éphémère (les {CQ.QUERY_HTTP_KEEP_SELECTIONS} sélections les "
                "plus récentes de chaque corpus sont gardées)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "corpus": {
                        "type": "string",
                        "description": (
                            "Corpus Zotero interne : argument donné par "
                            "l'en-tête du digest des producteurs, URI "
                            "corpus://<nom>, nom de fichier du répertoire "
                            "des corpus (celui de corpus_list) ou chemin "
                            "(local seulement)."
                        ),
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Recherche locale classée (score : titre 3, "
                            "mots-clés 2, résumé 1 ; un terme exclu abaisse "
                            "le rang sans retirer la notice). Absente : "
                            "toutes les notices qui passent les filtres."
                        ),
                    },
                    "filters": {
                        "type": "object",
                        "description": (
                            "Filtres : year_min, year_max (entiers), "
                            "item_types (types Zotero), languages (codes "
                            "ISO 639-1), sources (HAL, Isidore, CrossRef… ; "
                            "casse, accents et mention entre parenthèses "
                            "ignorés), has_doi, has_abstract, "
                            "keep_unknown_language (booléens). Les "
                            "filtres d'année écartent les notices sans année ; "
                            "avec languages, les notices sans langue lisible "
                            "(celles venues seulement de "
                            f"{_labels_without_language('ou')}, et certaines "
                            "d'autres sources) sont gardées, comme dans la "
                            "moisson ; keep_unknown_language: false les écarte."
                        ),
                        "properties": {
                            "year_min": {"type": "integer"},
                            "year_max": {"type": "integer"},
                            "item_types": {"type": "array",
                                           "items": {"type": "string"}},
                            "languages": {"type": "array",
                                          "items": {"type": "string"}},
                            "sources": {"type": "array",
                                        "items": {"type": "string"}},
                            "has_doi": {"type": "boolean"},
                            "has_abstract": {"type": "boolean"},
                            "keep_unknown_language": {"type": "boolean"},
                        },
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Champs rendus par notice : "
                            f"{', '.join(CQ.FIELDS)}. Défaut : "
                            f"{', '.join(CQ.DEFAULT_FIELDS)}. Le résumé "
                            f"('abstract') est tronqué à "
                            f"{CQ.QUERY_ABSTRACT_CHARS} caractères : notice "
                            "complète par ids."
                        ),
                    },
                    "sort": {
                        "type": "string",
                        "enum": list(CQ.SORTS),
                        "description": (
                            "Tri : 'relevance' (défaut avec query : score "
                            "local puis classement de la moisson), 'rank' "
                            "(défaut sans query : classement de la moisson "
                            "au manifeste, à défaut position), 'position' "
                            "(ordre du fichier), 'year_desc', 'year_asc', "
                            "'title'. Départage final par position."
                        ),
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                        "description": "Rang de départ de la page (0 = début).",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "default": CQ.QUERY_DEFAULT_LIMIT,
                        "description": (
                            f"Notices par page (défaut {CQ.QUERY_DEFAULT_LIMIT}, "
                            f"ramené à {CQ.QUERY_MAX_LIMIT} au plus)."
                        ),
                    },
                    "ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            f"Au plus {CQ.QUERY_MAX_IDS} identifiants : "
                            "notices complètes (résumé entier), dans l'ordre "
                            "demandé ; query, filters, sort, offset et limit "
                            "sont alors ignorés."
                        ),
                    },
                },
                "required": ["corpus"],
            },
        ),
        # Comparaison de deux corpus, veille (OPT-19).
        types.Tool(
            name="corpus_diff",
            description=(
                "Compare deux corpus Zotero JSON internes par identifiants : "
                "corpus_a = référence (en veille, la moisson précédente de "
                "la même requête), corpus_b = corpus comparé (la nouvelle). "
                "Rend les travaux présents dans B seulement (ajouts), dans A "
                "seulement (retraits) et dans les deux (inchangés dans la "
                "sélection), dont les notices enrichies (DOI ajouté, résumé "
                "ajouté, auteurs complétés, date précisée…, liste des "
                "champs). Chaque ajout est classé : nouvelle publication "
                "(datée après la moisson A), nouveau dépôt HAL, indéterminé "
                "(même année que A, date imprécise), notice nouvellement "
                "récupérée (publiée avant A) ; chaque retrait porte son motif "
                "probable (hors de la fenêtre de dates de B, source non "
                "interrogée, en échec ou plafonnée). Rappelle le mode et le "
                "tri de chaque moisson (manifestes) : en mode bibliographie, "
                "un changement de classement chez le fournisseur ressemble à "
                "des ajouts et des retraits. Identité : clés fortes "
                "partagées (DOI, identifiants HAL, OAI, Isidore, OpenAlex, "
                "S2, CORE, Scholar, URL canonique, ISBN de livre) avec le "
                "garde-fou DOI de la déduplication ; sans clé commune, "
                "titre identique une fois normalisé, année et premier "
                "auteur (régime exact de la déduplication) ; passe floue "
                "seulement avec fuzzy=true. Retour : résumé, compteurs et "
                "échantillons bornés (identifiants et titres) ; rapport "
                "complet '<corpus_b>.diff.json' à côté de B, jamais "
                "remplacé par une autre comparaison (nom unique "
                "'<corpus_b>_<uuid8>.diff.json' si le premier est pris). "
                "Notices complètes des ajouts (résumé compris) : "
                "corpus_query avec corpus = corpus_b et ids = identifiants "
                "des échantillons ou du rapport. Sur le serveur "
                "distant : échantillons d'identifiants sans titre et URL de "
                "téléchargement du rapport. Refuse un fichier CSL-JSON "
                "(.csl.json), un manifeste ou un rapport."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "corpus_a": {
                        "type": "string",
                        "description": (
                            "Corpus de référence, Zotero interne "
                            "(corpus_*.json) : le plus ancien en veille. "
                            "Chemin, ou nom de fichier tel que listé par "
                            "corpus_list."
                        ),
                    },
                    "corpus_b": {
                        "type": "string",
                        "description": (
                            "Corpus comparé, Zotero interne (corpus_*.json) : "
                            "le plus récent en veille. Chemin, ou nom de "
                            "fichier tel que listé par corpus_list."
                        ),
                    },
                    "fuzzy": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Apparier aussi les notices sans identifiant "
                            "commun par la passe floue de la déduplication "
                            "(titre au moins 0,93 de similarité, année à un "
                            "an près et premier auteur obligatoires). Défaut "
                            "faux : identifiants, et titre identique une "
                            "fois normalisé avec année et premier auteur, "
                            "comme la déduplication sans passe floue."
                        ),
                    },
                    "sample": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "default": 20,
                        "description": (
                            "Taille des échantillons rendus par catégorie "
                            "(ajouts, retraits, enrichies, modifiées ; défaut "
                            "20, au plus 100). Retour plafonné en octets : "
                            f"{CQ.DIGEST_MAX_BYTES // 1000} Ko sans sample, "
                            f"{CQ.QUERY_MAX_BYTES // 1000} Ko avec sample ; "
                            "au-delà, échantillons réduits "
                            "(structuredContent.byte_cap). Le rapport complet "
                            "n'est pas borné."
                        ),
                    },
                    "output_path": {
                        "type": "string",
                        "description": (
                            "Rapport produit (suffixe .diff.json), nom "
                            "stable remplacé à chaque comparaison. Si omis : "
                            "<radical de corpus_b>.diff.json à côté de "
                            "corpus_b (dans le répertoire des corpus sur le "
                            "serveur distant), ou, s'il existe déjà, "
                            "<radical de corpus_b>_<uuid8>.diff.json : une "
                            "comparaison ne remplace jamais le rapport "
                            "d'une autre."
                        ),
                    },
                },
                "required": ["corpus_a", "corpus_b"],
            },
        ),
    ]


# === Couche de résultat enrichi (retour MCP : transport-aware) ===

def _download_url(name: str) -> str:
    """URL de téléchargement d'un fichier (absolue si PUBLIC_BASE_URL connue,
    sinon relative `<DOWNLOAD_PREFIX>/<name>` en repli)."""
    base = (PUBLIC_BASE_URL or "").rstrip("/")
    return f"{base}{DOWNLOAD_PREFIX}/{name}"


def _http_download_summary(summary_text: str, urls: dict) -> str:
    """
    Reconstruit un résumé **sans aucun chemin disque** pour le mode HTTP.

    Les résumés des outils contiennent des chemins absolus (« Chemin complet :
    /…/output/… », « Fichiers produits : … »). À distance, on ne doit pas les
    fuiter : on retire toute ligne contenant le chemin de `OUTPUT_DIR` (tous les
    fichiers produits sont confinés sous OUTPUT_DIR — garanti par le Sprint 07),
    puis on ajoute la liste des URLs de téléchargement.
    """
    base_dir = str(OUTPUT_DIR.resolve())
    base_dir_raw = str(OUTPUT_DIR)
    kept = [
        line for line in summary_text.splitlines()
        if base_dir not in line and base_dir_raw not in line
    ]
    body = "\n".join(kept).rstrip()
    if urls:
        body += "\n\nFichiers (URLs de téléchargement) :\n" + "\n".join(
            f"  - {name} : {url}" for name, url in urls.items()
        )
    return body


def _redact_paths_in_text(text: str) -> str:
    """
    Neutralise tout chemin disque sous `OUTPUT_DIR` dans un texte libre (mode
    HTTP). Remplace le **répertoire** `OUTPUT_DIR` par un placeholder neutre →
    seul le basename d'un éventuel fichier reste visible, jamais l'arborescence
    serveur. Filet de sécurité appliqué à TOUTE ``str`` legacy renvoyée par un
    outil (correctif revue : `corpus_list` fuyait les chemins absolus en HTTP).
    """
    for needle in (str(OUTPUT_DIR.resolve()), str(OUTPUT_DIR)):
        if needle:
            text = text.replace(needle, "[corpus]")
    return text


def _scrub_paths_for_http(structured: dict) -> dict:
    """
    Remplace récursivement toute valeur chaîne portant un chemin disque (sous
    `OUTPUT_DIR`) par son **basename** — pour ne jamais fuiter de chemin absolu
    dans `structuredContent` en HTTP (ex. `source_corpus`, `input_corpora`).
    """
    base_dir = str(OUTPUT_DIR.resolve())
    base_dir_raw = str(OUTPUT_DIR)

    def scrub(v):
        if isinstance(v, str) and (base_dir in v or base_dir_raw in v):
            return Path(v).name
        if isinstance(v, list):
            return [scrub(x) for x in v]
        if isinstance(v, dict):
            return {k: scrub(x) for k, x in v.items()}
        return v

    return {k: scrub(v) for k, v in structured.items()}


def _measure_result(blocks: list, structured: dict | None) -> int:
    """
    Taille en octets UTF-8 d'un retour d'outil : sérialisation JSON de chaque
    bloc et du ``structuredContent`` (même mesure que le banc d'OPT-00,
    ``scripts/bench/analysis.measure_tool_result``).
    """
    total = 0
    for block in blocks:
        data = (block.model_dump(mode="json", by_alias=True, exclude_none=True)
                if hasattr(block, "model_dump") else block)
        total += len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    if structured is not None:
        total += len(json.dumps(structured, ensure_ascii=False,
                                default=str).encode("utf-8"))
    return total


def _json_len(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def _fit_digest(blocks: list, structured: dict, cap: int, digest=None) -> None:
    """
    Plafond en octets du retour en mode digest (OPT-17). Tant que le retour
    complet dépasse ``cap`` : les dernières notices passent en ligne courte
    (rang, titre, année, premier auteur, identifiant ; au moins
    ``DIGEST_MIN_FULL`` restent complètes), puis les dernières notices sont
    retirées (ligne de texte et entrée de ``top_items``, en parallèle).
    L'en-tête du digest le dit. Sans ``digest`` (revérification après la
    ligne d'échéance souple), seules les dernières lignes sont retirées.
    Sans effet hors digest.
    """
    info = structured.get("digest")
    items = structured.get("top_items")
    if not isinstance(info, dict) or not isinstance(items, list):
        return
    idx = next((i for i, b in enumerate(blocks)
                if getattr(b, "type", None) == "text"
                and b.text.startswith(CQ.DIGEST_HEADER)), None)
    if idx is None:
        return
    # La taille mesurée est rendue dans le retour lui-même : sa place est
    # réservée avant la mesure, puis sa valeur stabilisée.
    info["total_bytes"] = max(int(cap), 0)
    total = _measure_result(blocks, structured)
    if total <= cap:
        _settle_total_bytes(blocks, structured, info, total)
        return
    wanted = min(int(info.get("top") or 0), int(info.get("total") or 0))
    if digest is not None:
        entries = digest.entries
        count = min(len(entries), len(items))
        full = min(int(info.get("full") or count), count)

        def rebuild():
            blocks[idx] = types.TextContent(
                type="text", text=digest.text(count=count, full=full))
            structured["top_items"] = [e.data for e in entries[:count]]
            info.update({"returned": count, "full": full, "truncated": True})

        floor = min(CQ.DIGEST_MIN_FULL, count)
        while total > cap and full > floor:
            excess = total - cap
            while full > floor and excess > 0:
                full -= 1
                e = entries[full]
                excess -= _json_len(e.line) - _json_len(e.short or e.line)
            rebuild()
            total = _measure_result(blocks, structured)
        while total > cap and count > 0:
            excess = total - cap
            while count > 0 and excess > 0:
                count -= 1
                e = entries[count]
                shown = e.line if count < full else (e.short or e.line)
                excess -= _json_len(shown) + _json_len(e.data) + 2
            full = min(full, count)
            rebuild()
            total = _measure_result(blocks, structured)
        if total > cap:
            # Résumé et structuredContent dépassent seuls le plafond : aucun
            # digest ne peut tenir (à alléger chez le producteur).
            logger.warning(f"Digest vide : le retour hors notices pèse {total} "
                           f"octets, au-delà du plafond de {cap} octets")
        _settle_total_bytes(blocks, structured, info, total)
        return
    lines = blocks[idx].text.split("\n")
    split = len(lines) - len(items)
    head = [line for line in lines[:split]
            if not line.startswith(CQ.DIGEST_TRUNCATED_PREFIX)]
    entry_lines = lines[split:]
    full = int(info.get("full") or len(entry_lines))
    while total > cap and entry_lines:
        excess = total - cap
        while entry_lines and excess > 0:
            excess -= _json_len(entry_lines.pop()) + _json_len(items.pop()) + 2
        full = min(full, len(entry_lines))
        note = CQ.truncation_note(len(entry_lines), wanted, full)
        # En-tête ramené aux notices rendues (nombre et offset de la suite).
        head = CQ.retarget_header(head, len(entry_lines))
        blocks[idx] = types.TextContent(
            type="text", text="\n".join(head + [note] + entry_lines))
        info.update({"returned": len(entry_lines), "full": full,
                     "truncated": True})
        total = _measure_result(blocks, structured)
    _settle_total_bytes(blocks, structured, info, total)


def _settle_total_bytes(blocks: list, structured: dict, info: dict,
                        total: int) -> None:
    """``digest.total_bytes`` = taille du retour qui la contient (point fixe
    atteint en une ou deux mesures)."""
    for _ in range(3):
        info["total_bytes"] = total
        measured = _measure_result(blocks, structured)
        if measured == total:
            return
        total = measured
    info["total_bytes"] = total


def _link_description(path: Path, compact: bool) -> str:
    """Description d'un ``ResourceLink`` : historique (nom et chemin) ou
    compacte (rôle du fichier, mode digest)."""
    if not compact:
        return f"{path.name} — {path}"
    kind = corpus_kind(path.name)
    if kind == "csl":
        return "CSL-JSON (import Zotero Desktop)"
    if kind == "stats":
        return "statistiques"
    if kind == "zotero":
        return "Zotero JSON interne"
    return path.name


def _build_corpus_tool_result(
    summary_text: str,
    primary_items: list,
    primary_path: Path,
    primary_mime: str,
    extra_files: list,
    structured: dict,
    *,
    inline: str = INLINE_FULL,
    top: int | None = None,
    digest: DigestSpec | None = None,
    reserve_bytes: int = 0,
) -> tuple[list, dict]:
    """
    Construit le retour enrichi des outils producteurs, **conscient du
    transport** (CombinationContent — tuple ``(content_blocks, structured)``).

    - **STDIO**, ``inline="full"`` (Sprint 05, défaut de ce helper) :
      ``TextContent`` de résumé + ``ResourceLink corpus://`` par fichier +
      ``EmbeddedResource`` du corpus PRIMAIRE si ``<= INLINE_MAX_ITEMS`` items
      **et** si son JSON sérialisé pèse ``<= INLINE_MAX_BYTES`` octets UTF-8
      (double garde anti-saturation, OPT-02 ; le JSON est sérialisé une
      seule fois).
    - **STDIO**, ``inline="digest"`` (OPT-17, défaut des outils en stdio) :
      résumé + liens (description courte) + un second ``TextContent`` : le
      digest des ``top`` premières notices dans l'ordre du classement
      (``utils.corpus_query.build_digest`` d'après ``digest``), et
      ``structuredContent.top_items`` (une entrée par notice, même ordre) ;
      aucun ``EmbeddedResource``. Le retour complet (texte, liens et
      ``structuredContent``) est borné à ``DIGEST_MAX_BYTES - reserve_bytes``
      octets : les notices de la fin sont retirées au besoin (``_fit_digest``).
    - **STDIO**, ``inline="none"`` : résumé et liens seulement.
    - **HTTP distant** (Sprint 08, quel que soit ``inline``) : ``TextContent``
      SANS chemin disque + ``structuredContent["download_urls"]`` (URLs
      HTTPS) ; **jamais** de JSON inliné, **jamais** de chemin disque (ni
      texte, ni structured), **pas** de ``ResourceLink corpus://``
      (non téléchargeable hors MCP, porteur de chemin), pas de digest.

    Classement des fichiers (``utils.output_files``) : un fichier de service
    n'est jamais exposé ; un fichier associé (manifeste, rapport DOI) figure
    dans ``download_urls`` en HTTP et dans ``files`` en stdio, mais ne devient
    ni ``ResourceLink`` ni ressource ``corpus://`` (``read_resource`` le
    refuse).

    Args:
        summary_text: Résumé texte formaté par l'outil (peut contenir des
            chemins — filtrés en HTTP).
        primary_items: Items du corpus PRIMAIRE (inlinés en stdio sous le seuil).
        primary_path: Fichier primaire (URI inline en stdio).
        primary_mime: Type MIME du contenu inliné.
        extra_files: Autres fichiers produits.
        structured: Métadonnées machine, complétées ici.
        inline: ``full`` (défaut du helper), ``digest`` ou ``none``.
        top: notices du digest (défaut ``DIGEST_DEFAULT_TOP``).
        digest: ce que l'outil sait de son corpus (notices Zotero, rangs,
            requête…) ; sans lui, ``digest`` se réduit à ``none``.
        reserve_bytes: octets réservés sous le plafond du digest pour un
            complément ajouté ensuite (ligne d'échéance souple).

    Returns:
        Tuple ``(content_blocks, structured)``.
    """
    # Fichiers réellement produits, hors fichiers de service (jamais exposés).
    file_paths = [
        p for p in [primary_path, *extra_files]
        if p and p.exists() and not _is_output_service_file(p)
    ]
    # Fichiers exposables comme ressources corpus:// (hors fichiers associés).
    resource_paths = [p for p in file_paths if not is_associated_file(p)]

    n = len(primary_items)

    # === Branche HTTP distant : URLs seules, aucun chemin, aucun inline ===
    if TRANSPORT_MODE == "http":
        urls = {p.name: _download_url(quote(p.name, safe=""))
                for p in file_paths}
        text = _http_download_summary(summary_text, urls)
        blocks = [types.TextContent(type="text", text=text)]
        structured = _scrub_paths_for_http(structured)
        structured.update({
            "item_count": n,
            "download_urls": urls,
            "transport": "http",
        })
        structured.pop("files", None)        # ne JAMAIS fuiter les chemins
        return blocks, structured

    # === Branche STDIO ===
    # Le registre ne sert qu'au repli stdio de read_resource.
    for path in resource_paths:
        _EMITTED_RESOURCES[path.name] = path.resolve()

    if inline not in INLINE_MODES:
        inline = INLINE_FULL
    compact = inline != INLINE_FULL
    # Séparateurs (B8) : un client qui met les blocs bout à bout collait le
    # résumé, les liens et le digest (« …/outputcorpus_… »,
    # « ….jsoncorpus://… ») ; le résumé finit par un saut de ligne et chaque
    # lien est suivi d'un bloc « \n ».
    blocks = [types.TextContent(type="text",
                                text=summary_text.rstrip("\n") + "\n")]

    # (a) LIENS : un ResourceLink par fichier → cliquable dans l'interface
    for path in resource_paths:
        blocks.append(types.ResourceLink(
            type="resource_link",
            uri=corpus_uri(path.name),
            name=path.stem,
            mimeType=(
                "text/plain" if path.suffix == ".txt" else "application/json"
            ),
            description=_link_description(path, compact),
        ))
        blocks.append(types.TextContent(type="text", text="\n"))
    primary_uri = corpus_uri(primary_path.name)

    if compact:
        # === Digest (OPT-17) ou résumé et liens seuls ===
        structured.update({
            "item_count": n,
            "inline_json": False,
            "inline_mode": inline,
            "inline_threshold": INLINE_MAX_ITEMS,
            "inline_max_bytes": INLINE_MAX_BYTES,
            "files": {p.name: str(p) for p in file_paths},
            "resources": [corpus_uri(p.name) for p in resource_paths],
        })
        if inline == INLINE_NONE or digest is None:
            structured["inline_mode"] = INLINE_NONE
            structured["inline_skipped_reason"] = (
                "inline='none' : résumé et liens seulement (notices par "
                "corpus_query)")
            return blocks, structured
        if digest.corpus_path is not None:
            # Argument corpus de l'appel conseillé : résolu en stdio quel que
            # soit le répertoire courant du processus (revue d'OPT-17).
            digest = dataclasses.replace(
                digest, corpus_name=_query_reference(digest.corpus_path))
        d = CQ.build_digest(digest, top or CQ.DIGEST_DEFAULT_TOP)
        blocks.append(types.TextContent(type="text", text=d.text()))
        structured["top_items"] = [e.data for e in d.entries]
        structured["digest"] = {
            "top": d.top,
            "returned": len(d.entries),
            "full": len(d.entries),
            "total": d.total,
            "order": d.order.get("label"),
            "max_bytes": CQ.DIGEST_MAX_BYTES,
        }
        structured["inline_skipped_reason"] = (
            "inline='digest' (notices suivantes : corpus_query)")
        _fit_digest(blocks, structured,
                    max(0, CQ.DIGEST_MAX_BYTES - int(reserve_bytes or 0)),
                    digest=d)
        return blocks, structured

    # (b) JSON INLINE : corpus primaire complet sous les DEUX plafonds (items,
    # puis octets UTF-8 du JSON sérialisé une seule fois) ; sinon lien seul.
    inline_text = None
    inline_bytes = None
    skipped_reason = None
    skipped_note = None
    if n > INLINE_MAX_ITEMS:
        skipped_reason = f"{n} items > seuil {INLINE_MAX_ITEMS}"
        skipped_note = skipped_reason
    else:
        inline_text = json.dumps(primary_items, ensure_ascii=False, indent=2)
        inline_bytes = len(inline_text.encode("utf-8"))
        if inline_bytes > INLINE_MAX_BYTES:
            inline_text = None
            skipped_reason = (
                f"{inline_bytes} octets > plafond {INLINE_MAX_BYTES} octets"
            )
            skipped_note = (
                f"{n} items, mais {inline_bytes} octets UTF-8 > plafond "
                f"{INLINE_MAX_BYTES} octets (MOISSON_INLINE_MAX_BYTES)"
            )
    inline_ok = inline_text is not None

    if inline_ok:
        blocks.append(types.EmbeddedResource(
            type="resource",
            resource=types.TextResourceContents(
                uri=primary_uri,
                mimeType=primary_mime,
                text=inline_text,
            ),
        ))
    else:
        # Message explicite pour les clients qui n'affichent pas les blocs
        blocks[0] = types.TextContent(
            type="text",
            text=summary_text.rstrip("\n") + (
                f"\n\n(JSON non inliné : {skipped_note}. "
                f"Corpus complet via la resource {primary_uri})\n"
            ),
        )

    structured.update({
        "item_count": n,
        "inline_json": inline_ok,
        "inline_mode": INLINE_FULL,
        "inline_threshold": INLINE_MAX_ITEMS,
        "inline_max_bytes": INLINE_MAX_BYTES,
        "files": {p.name: str(p) for p in file_paths},
        "resources": [corpus_uri(p.name) for p in resource_paths],
    })
    if inline_bytes is not None:
        structured["inline_bytes"] = inline_bytes
    if not inline_ok:
        structured["inline_skipped_reason"] = skipped_reason
    return blocks, structured


def _is_output_service_file(path: Path) -> bool:
    """
    Vrai si `path` est un fichier de service de `OUTPUT_DIR` (`.oauth/`,
    `.audit/`, `.index.json`…), après résolution des liens symboliques.

    Sous `OUTPUT_DIR`, tout composant relatif commençant par un point compte ;
    hors de `OUTPUT_DIR` (fichiers écrits ailleurs en stdio), seul le nom de
    fichier compte.
    """
    try:
        resolved = Path(path).resolve()
    except (OSError, RuntimeError):
        return True
    return (
        is_service_path(Path(path).name)
        or is_service_path(resolved, OUTPUT_DIR.resolve())
    )


# === Implémentation des outils ===

@server.call_tool()
async def call_tool(
    name: str,
    arguments: dict[str, Any],
) -> list[types.ContentBlock] | tuple[list, dict] | types.CallToolResult:
    """
    Dispatcher principal des appels d'outils.

    Args:
        name: Nom de l'outil invoqué (correspond à list_tools)
        arguments: Arguments validés selon inputSchema

    Returns:
        Selon l'outil, l'une des formes acceptées par le SDK :
        - ``str`` (legacy) → enveloppé en un unique ``TextContent`` ;
        - ``list[ContentBlock]`` → renvoyée telle quelle ;
        - ``tuple(content_blocks, structured)`` → *CombinationContent*
          (peuple à la fois ``content`` et ``structuredContent``) ;
        - ``CallToolResult`` à ``isError=True`` (OPT-07) : outil inconnu,
          exception, et tout échec signalé par un outil (``ToolError``,
          ``ToolErrorContent`` : fichier introuvable, paramètre refusé,
          serveur occupé, échéance dépassée, annulation, moisson dont
          aucune source n'a pu être exécutée…), même texte qu'avant.
    """
    # Anti-fuite (Sprint 07) : en HTTP, ne JAMAIS dumper les arguments bruts
    # (ils peuvent contenir des chemins ; principe de moindre exposition). On
    # ne loggue que le nom de l'outil et les CLÉS d'arguments. En stdio (local
    # de confiance), on conserve le log verbeux historique.
    req_id = None
    client_id = None
    if TRANSPORT_MODE == "http":
        try:
            req_id = getattr(server.request_context, "request_id", None)
        except Exception:
            req_id = None
        client_id = _audit_client_id()
        logger.info("Appel outil : %s (clés args : %s)",
                    name, sorted((arguments or {}).keys()),
                    extra={"tool": name, "request_id": req_id,
                           "client_id": client_id})
    else:
        logger.info(f"Appel outil : {name} avec arguments : {arguments}")

    started = time.monotonic()
    try:
        if name == "moisson_search":
            result = await _tool_moisson_search(arguments)
        elif name == "corpus_analyze":
            result = await _tool_corpus_analyze(arguments)
        elif name == "corpus_list":
            result = await _tool_corpus_list(arguments)
        elif name == "corpus_filter":
            result = await _tool_corpus_filter(arguments)
        elif name == "corpus_resolve_dois":
            result = await _tool_corpus_resolve_dois(arguments)
        elif name == "corpus_to_csl":
            result = await _tool_corpus_to_csl(arguments)
        elif name == "fetch_by_doi":
            result = await _tool_fetch_by_doi(arguments)
        elif name == "search_by_author":
            result = await _tool_search_by_author(arguments)
        elif name == "corpus_merge":
            result = await _tool_corpus_merge(arguments)
        elif name == "corpus_export":
            result = await _tool_corpus_export(arguments)
        elif name == "corpus_query":
            result = await _tool_corpus_query(arguments)
        elif name == "corpus_diff":
            result = await _tool_corpus_diff(arguments)
        else:
            result = ToolError(f"Outil inconnu : {name}")
    except OperationCancelled as e:
        # Annulation remontée d'un fil de travail : jamais d'exception de
        # base vers le SDK (elle interromprait la session).
        logger.warning(f"Outil {name} annulé ({e.reason})")
        result = ToolError(f"Erreur : opération annulée ({e}).", cancelled=True)
    except Exception as e:
        logger.exception(f"Erreur dans l'outil {name}")
        result = ToolError(f"Erreur : {type(e).__name__} : {e}")

    # Statut réel de l'appel (métriques, audit) et item_count.
    status_ = _call_status(result)
    item_count = None
    structured = None
    if _is_tool_error(result):
        structured = (result[1] if isinstance(result, ToolErrorContent)
                      else result.structured)
    elif isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
        structured = result[1]
    if isinstance(structured, dict):
        item_count = structured.get("item_count")

    duration_ms = round((time.monotonic() - started) * 1000, 1)
    _record_metrics(name, status_, duration_ms)

    # Observabilité serveur distant : log structuré + audit JSONL.
    if TRANSPORT_MODE == "http":
        logger.info("Outil terminé", extra={
            "tool": name, "request_id": req_id, "client_id": client_id,
            "duration_ms": duration_ms, "status": status_,
        })
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "request_id": str(req_id) if req_id is not None else None,
            "client_id": client_id,
            "tool": name,
            "arg_keys": sorted((arguments or {}).keys()),
            "duration_ms": duration_ms,
            "status": status_,
            "item_count": item_count,
        }
        source_statuses = _source_statuses(structured)
        if source_statuses:
            record["source_statuses"] = source_statuses
        _write_audit(record)

    # Normalisation du retour (rétro-compatible) :
    if _is_tool_error(result):          # échec → isError=True (C30)
        return _error_result(result)
    if isinstance(result, tuple):       # (content_blocks, structured_dict)
        return result                   # → CombinationContent (SDK)
    if isinstance(result, str):         # legacy → un seul TextContent
        # Filet de sécurité (correctif revue) : en HTTP, AUCUNE str ne doit
        # fuiter de chemin disque (corpus_list, messages d'erreur…). On rédige
        # systématiquement avant l'enveloppe — ferme la classe entière, pas
        # seulement outil par outil.
        if TRANSPORT_MODE == "http":
            result = _redact_paths_in_text(result)
        return [types.TextContent(type="text", text=result)]
    return result                       # déjà une liste de ContentBlock


def _call_status(result) -> str:
    """
    Statut réel d'un appel pour les métriques et l'audit : ``cancelled``,
    ``error`` (isError), ``partial`` (moisson dont au moins une source est
    incomplète : succès partiel nommé) ou ``ok``.
    """
    if isinstance(result, ToolError):
        return "cancelled" if result.cancelled else "error"
    if isinstance(result, ToolErrorContent):
        return "error"
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
        if result[1].get("harvest_status") == "partial":
            return "partial"
    return "ok"


def _source_statuses(structured) -> dict | None:
    """``{source: statut}`` d'une moisson (audit), ou None."""
    if not isinstance(structured, dict):
        return None
    by_status = structured.get("by_source_status")
    if not isinstance(by_status, dict):
        return None
    return {name: (info or {}).get("status") for name, info in by_status.items()}


# === Opérations longues : progression, annulation, concurrence (Sprint 09) ===

# Annulation coopérative d'une moisson (échéance ou demande du client) : la
# classe commune du moteur et de la couche HTTP (``OperationCancelled``, qui
# hérite de BaseException pour traverser les ``except Exception`` des
# connecteurs). Nom historique conservé.
MoissonCancelled = OperationCancelled


class ProgressReporter:
    """
    Pont **thread → asyncio** pour la progression d'une moisson.

    `_run_moisson_sync` tourne dans un thread d'executor (les connecteurs sont
    synchrones). Pour notifier la session asyncio depuis ce thread, on poste la
    coroutine `send_progress_notification` sur la boucle via
    `run_coroutine_threadsafe`. L'annulation est **coopérative** : le thread
    vérifie `cancel_event` entre les sources et à chaque `report()` ; le
    moteur la vérifie aussi entre les pages et pendant les attentes (OPT-07).
    """

    def __init__(self, loop, session, token, req_id, cancel_event):
        self._loop = loop
        self._session = session
        self._token = token
        self._req_id = req_id
        self._cancel = cancel_event

    def check_cancelled(self) -> None:
        """Lève MoissonCancelled si l'annulation a été demandée."""
        if self._cancel is not None and self._cancel.is_set():
            raise MoissonCancelled()

    def report(self, progress: float, total: float | None, message: str) -> None:
        """Émet une notification de progression (best-effort). Vérifie d'abord
        l'annulation."""
        self.check_cancelled()
        if self._token is None:
            return
        coro = self._session.send_progress_notification(
            progress_token=self._token,
            progress=progress,
            total=total,
            message=message,
            related_request_id=(str(self._req_id)
                                if self._req_id is not None else None),
        )
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
            fut.result(timeout=MOISSON_PROGRESS_SEND_TIMEOUT)
        except Exception:
            # Progression best-effort : ne jamais faire échouer la moisson.
            pass


def _make_progress_reporter(loop, cancel_event):
    """
    Construit un `ProgressReporter` depuis le `request_context` courant.

    Renvoie un reporter **toujours** porteur du `cancel_event` (pour
    l'annulation coopérative), mais qui n'émet des notifications que si le
    client a fourni un `progressToken`. Hors contexte de requête (tests
    appelant `_run_moisson_sync` en direct), renvoie un reporter au token None
    si `cancel_event` est fourni, sinon None.
    """
    token = None
    session = None
    req_id = None
    try:
        ctx = server.request_context
        session = ctx.session
        req_id = getattr(ctx, "request_id", None)
        meta = getattr(ctx, "meta", None)
        token = getattr(meta, "progressToken", None) if meta else None
    except (LookupError, Exception):
        session = None
    if cancel_event is None and token is None:
        return None
    return ProgressReporter(loop, session, token, req_id, cancel_event)


# Sémaphore de concurrence, créé en LAZY dans la boucle uvicorn (un Semaphore
# créé au niveau module pourrait être lié à une autre boucle → RuntimeError).
# Partagé par les opérations longues du transport HTTP (OPT-07) :
# moisson_search, search_by_author, corpus_resolve_dois et fetch_by_doi.
_MOISSON_SEM = None
# Sémaphore créé ici et boucle à laquelle il appartient : un sémaphore créé
# pour une autre boucle (autre asyncio.run, tests) est recréé ; un sémaphore
# posé de l'extérieur (tests) est utilisé tel quel.
_SEM_CREATED = None
_SEM_LOOP = None


def _get_moisson_semaphore():
    global _MOISSON_SEM, _SEM_CREATED, _SEM_LOOP
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _MOISSON_SEM is None or (
            _MOISSON_SEM is _SEM_CREATED and _SEM_LOOP is not loop):
        _MOISSON_SEM = asyncio.Semaphore(MOISSON_MAX_CONCURRENT)
        _SEM_CREATED = _MOISSON_SEM
        _SEM_LOOP = loop
    return _MOISSON_SEM


# Dernière mesure du délai entre une annulation (échéance ou client) et la
# fin effective du fil de travail (secondes), exposée pour le diagnostic.
LAST_CANCEL_STOP_S: float | None = None


def _busy_error() -> ToolError:
    return ToolError(
        "Erreur : serveur occupé (trop de moissons simultanées). "
        "Réessayez dans un instant."
    )


def _shield_scope():
    """
    Portée anyio protégée des annulations (``CancelScope(shield=True)``), ou
    portée neutre si anyio est absent.

    Le SDK MCP annule une requête (``notifications/cancelled``, fermeture du
    transport) en annulant une portée anyio ; sur la boucle asyncio, anyio
    RÉPÈTE alors l'annulation à chaque ``await`` tant que la portée reste
    annulée. Seule une portée protégée l'interrompt.
    """
    try:
        import anyio
        return anyio.CancelScope(shield=True)
    except Exception:  # noqa: BLE001 - anyio absent ou hors tâche
        import contextlib
        return contextlib.nullcontext()


async def _join_worker(fut) -> None:
    """
    Attend la fin RÉELLE du fil de travail ``fut`` après une annulation,
    malgré des annulations répétées : portée anyio protégée (annulation du
    SDK) et nouvelle attente après chaque annulation asyncio native. Ne lève
    jamais : l'issue du fil se lit ensuite sur ``fut``.
    """
    with _shield_scope():
        while not fut.done():
            try:
                await asyncio.wait({fut})
            except asyncio.CancelledError:
                continue   # annulation répétée : le fil n'est pas fini
    if not fut.cancelled():
        fut.exception()   # issue marquée lue (pas d'alerte « never retrieved »)


def _completed_normally(fut) -> bool:
    """Le fil s'est terminé sans exception (il a rendu un résultat)."""
    return fut.done() and not fut.cancelled() and fut.exception() is None


async def _run_guarded(build, *, cancel_event: threading.Event,
                       label: str = "Moisson",
                       deadline_note: str = "aucun résultat partiel publié"):
    """
    Exécute dans un fil de travail la fonction synchrone ``build(deadline)``
    (sans argument), avec les garanties des opérations longues (Sprint 09,
    étendues par OPT-07). ``deadline`` est l'échéance monotone de
    l'opération, fixée APRÈS l'obtention de la place (HTTP), ou None (stdio).

    - **HTTP** : concurrence bornée par le sémaphore partagé (refus « serveur
      occupé » après ``MOISSON_ACQUIRE_TIMEOUT``), échéance
      ``MOISSON_DEADLINE_SECONDS`` : l'annulation coopérative est demandée
      (``cancel_event``), puis la fin RÉELLE du fil est attendue avant de
      libérer la place (aucun fil fantôme) ; le résultat est une erreur
      d'outil, aucun résultat partiel n'est publié. Exception : si le fil
      avait déjà passé son dernier contrôle et a publié, son résultat réel
      est rendu (jamais une erreur affirmant à tort que rien n'est publié).
    - **stdio** : ni sémaphore ni échéance (comportement historique) ; une
      annulation par le client est propagée au fil de la même façon (rien
      n'est publié après elle).

    L'attente de fin du fil après une annulation est protégée
    (``_join_worker``) : le SDK répète l'annulation d'une requête à chaque
    ``await`` ; sans protection, le gestionnaire rendrait la main (et la
    place du sémaphore) avant la fin du fil.

    Une annulation remontée du fil (``OperationCancelled``) devient une
    erreur d'outil, jamais une exception de base vers le SDK.
    """
    global LAST_CANCEL_STOP_S
    loop = asyncio.get_running_loop()
    http = TRANSPORT_MODE == "http"
    deadline_text = (
        f"Erreur : deadline serveur dépassée "
        f"({int(MOISSON_DEADLINE_SECONDS)} s). {label} annulée "
        f"({deadline_note})."
    )
    sem = None
    if http:
        sem = _get_moisson_semaphore()
        try:
            await asyncio.wait_for(sem.acquire(),
                                   timeout=MOISSON_ACQUIRE_TIMEOUT)
        except asyncio.TimeoutError:
            return _busy_error()
    try:
        deadline = (time.monotonic() + MOISSON_DEADLINE_SECONDS
                    if http else None)
        fut = loop.run_in_executor(None, build(deadline))
        try:
            # asyncio.wait : ni l'échéance ni l'annulation de l'appelant ne
            # touchent le future du fil, attendu explicitement ensuite (et,
            # contrairement à asyncio.shield annulé, rien n'est journalisé
            # en erreur quand le fil finit sur OperationCancelled).
            done, _ = await asyncio.wait(
                {fut}, timeout=MOISSON_DEADLINE_SECONDS if http else None)
            if not done:
                raise asyncio.TimeoutError
            return fut.result()
        except OperationCancelled as exc:
            if exc.reason == "deadline":
                return ToolError(deadline_text, cancelled=True)
            return ToolError(f"Erreur : {label.lower()} annulée ({deadline_note}).",
                             cancelled=True)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            cancel_event.set()
            t0 = time.monotonic()
            # Fin RÉELLE du fil avant de rendre la main et de libérer la
            # place du sémaphore, à l'abri des annulations répétées du SDK
            # (un simple ``await fut`` serait annulé aussitôt : fil fantôme,
            # place rendue trop tôt, mesure nulle).
            await _join_worker(fut)
            LAST_CANCEL_STOP_S = round(time.monotonic() - t0, 3)
            logger.info(f"{label} annulée : fil arrêté "
                        f"{LAST_CANCEL_STOP_S} s après l'annulation")
            if isinstance(exc, asyncio.TimeoutError):
                if _completed_normally(fut):
                    # Le fil avait passé son dernier contrôle d'annulation
                    # (juste avant d'écrire) quand l'échéance est tombée : il
                    # a publié. Rendre ce résultat réel plutôt qu'une erreur
                    # affirmant à tort que rien n'a été publié.
                    logger.warning(
                        f"{label} terminée après l'échéance ({LAST_CANCEL_STOP_S}"
                        f" s de plus) : résultat rendu tel quel")
                    return fut.result()
                return ToolError(deadline_text, cancelled=True)
            raise  # CancelledError : honorer l'annulation du client
    finally:
        if sem is not None:
            sem.release()


def _soft_deadline_schema() -> dict:
    """Propriété ``deadline_seconds`` du schéma de moisson_search (OPT-09 bis)."""
    import config as _config

    default = float(_config.STDIO_SOFT_DEADLINE_S or 0.0)
    default_text = (f"{default:g} s" if default > 0
                    else "aucune (désactivée par le serveur)")
    # Pas de « maximum » dans le schéma (revue OPT-09 bis) : le SDK
    # refuserait la valeur au lieu de la ramener à la borne haute, comme le
    # promet la description ; la borne est appliquée par le serveur
    # (_soft_deadline_seconds).
    return {
        "type": "number",
        "minimum": 0,
        "description": (
            "Échéance souple de la collecte, en secondes (serveur local "
            f"seulement). Défaut : {default_text}. À l'échéance, les sources "
            "qui n'ont pas fini sont arrêtées et marquées (motif "
            "« deadline ») et le corpus des autres sources est publié : le "
            "retour arrive avant le délai d'appel d'outil du client, au lieu "
            "d'une annulation qui ne publierait rien. Une source arrêtée "
            "garde les notices déjà reçues (« partielle ») ; arrêtée pendant "
            "enrich_oai, elle garde toutes ses notices Isidore, celles déjà "
            "enrichies le restant, sauf si elle reste bloquée dans une "
            "requête OAI au-delà du délai de grâce : ses notices Isidore sont "
            "alors reprises sans aucun enrichissement ; arrêtée avant toute "
            "notice, elle est « en échec » ; une source non démarrée est "
            "« non exécutée ». "
            "Une source lente mais saine est aussi concernée : avec "
            "mode='collecte', enrich_oai ou un max_per_source élevé, relever "
            "cette valeur. Ramenée entre "
            f"{_config.STDIO_SOFT_DEADLINE_MIN_S} et "
            f"{_config.STDIO_SOFT_DEADLINE_MAX_S} s ; 0 : aucune échéance "
            "(une source muette peut alors retenir l'appel jusqu'à deux "
            "minutes). L'augmenter seulement si le client accepte des "
            "appels plus longs (Claude Desktop : 4 min mesurées ; d'autres "
            "clients : 60 s). Sans effet en mode distant (échéance du "
            "serveur, sans publication partielle)."
        ),
    }


def _soft_deadline_seconds(value: Any) -> float | None | ToolError:
    """
    Échéance souple de ``moisson_search`` en stdio (OPT-09 bis), en secondes.

    Argument ``deadline_seconds`` absent : ``config.STDIO_SOFT_DEADLINE_S``
    (``MOISSON_STDIO_SOFT_DEADLINE``). 0 : aucune échéance (comportement
    historique). Une valeur positive est ramenée entre
    ``STDIO_SOFT_DEADLINE_MIN_S`` et ``STDIO_SOFT_DEADLINE_MAX_S``. Une valeur
    non numérique ou négative est refusée (``ToolError``).

    Returns:
        La durée en secondes, None (aucune échéance), ou une ``ToolError``.
    """
    import math

    import config as _config

    if value is None:
        seconds = float(_config.STDIO_SOFT_DEADLINE_S or 0.0)
        return seconds if seconds > 0 else None
    refused = ToolError(
        f"Paramètre refusé : deadline_seconds {value!r} invalide (nombre de "
        f"secondes attendu, 0 pour aucune échéance, "
        f"{_config.STDIO_SOFT_DEADLINE_MIN_S} à "
        f"{_config.STDIO_SOFT_DEADLINE_MAX_S} sinon).")
    if isinstance(value, bool):
        return refused
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return refused
    if not math.isfinite(seconds) or seconds < 0:
        return refused
    if seconds == 0:
        return None
    return float(min(max(seconds, _config.STDIO_SOFT_DEADLINE_MIN_S),
                     _config.STDIO_SOFT_DEADLINE_MAX_S))


def _note_soft_deadline(result, seconds: float, at: float | None = None):
    """
    Retour de ``moisson_search`` complété par l'échéance souple (OPT-09 bis,
    stdio seulement) : ``settings.soft_deadline_seconds`` et
    ``structuredContent.soft_deadline`` (durée ; ``reached`` : échéance
    passée au moment du retour ; sources arrêtées ou non démarrées, motif
    ``deadline`` ; parmi elles, sources arrêtées AVANT l'échéance parce que
    leur prochain créneau de requête tombait au-delà, ``intenable``) ; si
    des sources ont été arrêtées, une ligne le dit sous l'en-tête du texte,
    en distinguant les deux cas. La ligne nomme aussi les sources arrêtées
    dont les notices déjà reçues sont publiées (« partielle », avec leur
    nombre) et, s'il y en a aussi, celles qui n'ont rien rendu ; son conseil
    dit « compléter » ou « attendre » selon le cas (revue OPT-09 ter).
    ``structuredContent.soft_deadline`` ne change pas : le statut et le
    nombre de notices de chaque source sont dans ``by_source_status``.
    Tout autre retour est rendu tel quel.

    Args:
        at: échéance absolue (``time.monotonic()``) ; None : ``reached`` est
            déduit des sources (au moins une arrêtée à l'échéance).
    """
    from engine.orchestrator import SOFT_DEADLINE_UNREACHABLE

    if isinstance(result, tuple) and len(result) == 2:
        blocks, structured = result
    elif isinstance(result, ToolError):
        blocks, structured = None, result.structured
    else:
        return result
    if not isinstance(structured, dict):
        return result
    by_status = structured.get("by_source_status") or {}
    stopped = [name for name, info in by_status.items()
               if isinstance(info, dict) and info.get("reason") == "deadline"]
    unreachable = [name for name in stopped
                   if (by_status[name].get("counters") or {}).get(
                       SOFT_DEADLINE_UNREACHABLE)]
    if at is not None:
        reached = time.monotonic() >= at
    else:
        reached = len(stopped) > len(unreachable)
    settings = structured.get("settings")
    if isinstance(settings, dict):
        settings["soft_deadline_seconds"] = seconds
    structured["soft_deadline"] = {
        "seconds": seconds,
        "reached": reached,
        "stopped_sources": stopped,
        "unreachable_sources": unreachable,
    }
    if not stopped:
        return result
    if reached:
        line = (f"Échéance souple de {seconds:g} s atteinte : {len(stopped)} "
                f"source(s) arrêtée(s) ou non démarrée(s) (motif « deadline » : "
                f"{', '.join(stopped)})")
        if unreachable:
            line += (f", dont {len(unreachable)} avant l'échéance, leur "
                     f"prochain créneau de requête tombant au-delà "
                     f"({', '.join(unreachable)})")
    else:
        line = (f"Échéance souple de {seconds:g} s intenable pour "
                f"{len(stopped)} source(s) (motif « deadline » : "
                f"{', '.join(stopped)}) : arrêtée(s) avant l'échéance, leur "
                f"prochain créneau de requête tombant au-delà")
    line += " ; "
    # Sources arrêtées qui ont rendu des notices (« partielle », contrat
    # RunContext.soft_stop, OPT-09 ter) : ces notices sont dans le corpus
    # publié ; les autres (en échec, non exécutées) n'ont rien rendu. La
    # ligne les distingue (revue OPT-09 ter) : un agent client ne doit pas
    # croire absentes des notices publiées.
    kept = []
    for name in stopped:
        try:
            n_items = int(by_status[name].get("items") or 0)
        except (TypeError, ValueError):
            n_items = 0
        if by_status[name].get("status") == "partial" and n_items > 0:
            kept.append((name, n_items))
    kept_names = {name for name, _ in kept}
    empty = [name for name in stopped if name not in kept_names]
    # Conseil à l'agent client : relancer avec une échéance plus longue,
    # pour attendre les sources sans notice ou compléter les partielles.
    if kept and empty:
        advice = " Pour attendre ou compléter ces sources"
    elif kept:
        advice = " Pour les compléter"
    else:
        advice = " Pour les attendre"
    advice += (" : deadline_seconds plus grand, si le client accepte des "
               "appels plus longs.")
    # Valeur concrète (OPT-17), sur une ligne à part : la ligne d'échéance
    # reste celle d'OPT-09 bis et ter.
    suggestion = _soft_deadline_suggestion(seconds)
    if isinstance(result, ToolError):
        line += "aucune autre source n'a abouti, aucun corpus n'a été écrit."
        line += advice + "\n" + suggestion
        head, _, rest = str(result).partition("\n")
        return ToolError(f"{head}\n{line}\n{rest}" if rest else f"{head}\n{line}",
                         structured=structured, cancelled=result.cancelled)
    line += "le corpus des sources qui ont répondu à temps est publié"
    if kept:
        line += (", avec les notices déjà reçues des sources arrêtées ("
                 + ", ".join(f"{name} : {n}" for name, n in kept)
                 + " ; « partielle(s) »)")
        if empty:
            line += f" ; sans notice : {', '.join(empty)}"
    line += "."
    line += advice
    line += "\n" + suggestion
    new_blocks = list(blocks)
    for i, block in enumerate(new_blocks):
        if getattr(block, "type", None) == "text":
            head, _, rest = block.text.partition("\n")
            new_blocks[i] = types.TextContent(
                type="text",
                text=f"{head}\n{line}\n{rest}" if rest else f"{head}\n{line}")
            break
    if isinstance(blocks, list):
        blocks[:] = new_blocks
        return result
    return type(result)((new_blocks, structured))


# Délai d'appel d'outil mesuré chez Claude Desktop (4 min, OPT-09 bis) : la
# valeur suggérée après une échéance souple reste en deçà.
_SOFT_DEADLINE_SUGGESTION_CEILING_S = 230.0


def _soft_deadline_suggestion(seconds: float) -> str:
    """Ligne de conseil chiffrée après une échéance souple (OPT-17)."""
    import config as _config

    ceiling = min(_SOFT_DEADLINE_SUGGESTION_CEILING_S,
                  float(_config.STDIO_SOFT_DEADLINE_MAX_S))
    if seconds >= ceiling:
        return (f"Échéance déjà proche du délai d'appel de Claude Desktop "
                f"(environ 4 min) : réduire le périmètre (sources, "
                f"max_per_source, enrich_oai) ou moissonner par la CLI.")
    suggested = min(max(2 * seconds, seconds + 30), ceiling)
    return (f"Valeur suggérée : deadline_seconds={suggested:g} (Claude Desktop "
            f"accepte des appels d'environ 4 min, d'autres clients 60 s ; "
            f"sinon réduire le périmètre : sources, max_per_source).")


async def _tool_moisson_search(args: dict[str, Any]) -> tuple[list, dict] | str:
    """
    Implémentation de l'outil moisson_search.

    Exécute le moissonnage (moteur commun, ``engine.harvest``) dans un thread
    de travail pour ne pas bloquer la boucle asyncio MCP. En **HTTP** :
    plafond serveur sur ``max_per_source``, concurrence bornée (sémaphore),
    **deadline serveur** avec annulation propre (propagée jusqu'aux pages et
    aux attentes réseau), progression MCP best-effort. En **stdio** : ni
    sémaphore ni deadline dure, l'annulation par le client étant propagée ;
    **échéance souple** (OPT-09 bis, ``deadline_seconds``, défaut
    ``MOISSON_STDIO_SOFT_DEADLINE``) : à l'échéance, les sources encore en
    cours sont arrêtées et marquées (motif ``deadline``), et le corpus des
    autres sources est publié.
    """
    query = args["query"]
    # Défauts MCP (OPT-09) : plafond PAR source réduit et sources en
    # parallèle ; la CLI garde les siens (1000 par source, séquentiel).
    raw_max = int(args.get("max_per_source", MCP_DEFAULT_MAX_PER_SOURCE))
    sources = args.get("sources", list(FRANCOPHONE_SOURCES))
    enrich_oai = args.get("enrich_oai", False)
    do_dedup = args.get("dedup", True)
    from_date = args.get("from_date")
    to_date = args.get("to_date")
    # Option cache à trois états : absente (None) ⇒ défaut MCP, résolu par
    # _run_moisson_sync (mcp_cache_option : MOISSON_CACHE s'il est défini,
    # sinon activé, TTL MCP_CACHE_TTL) ; true / false ⇒ explicite.
    cache = args.get("cache")
    workers = args.get("workers", MCP_DEFAULT_WORKERS)
    mode = args.get("mode") or MODE_BIBLIOGRAPHIE
    if mode not in MODES:
        return ToolError(f"Paramètre refusé : mode {mode!r} inconnu "
                         f"(attendu : {', '.join(MODES)}).")
    # Classement (OPT-13) : absent ⇒ défaut du serveur (MOISSON_RANKING).
    ranking = args.get("ranking")
    if ranking is not None and ranking not in RANKING_MODES:
        return ToolError(f"Paramètre refusé : classement {ranking!r} inconnu "
                         f"(attendu : {', '.join(RANKING_MODES)}).")
    # Retour (OPT-17) : inline et top vérifiés avant toute moisson ; transmis
    # seulement s'ils sont fournis (défaut : CQ.DEFAULT_INLINE, top 40).
    parsed_inline = _inline_args(args)
    if isinstance(parsed_inline, ToolError):
        return parsed_inline

    # Plan de requête (OPT-11) : calculé sans réseau ; une requête, une
    # variante, une langue ou des bornes invalides sont refusées ici, avant
    # toute moisson. plan_only : aperçu seul (aucune requête, aucun fichier).
    from utils.query_plan import build_query_plan

    query_variants = args.get("query_variants") or None
    languages = args.get("languages") or None
    hal_fulltext = args.get("hal_fulltext", False)
    if not isinstance(hal_fulltext, bool):
        return ToolError("Paramètre refusé : hal_fulltext doit être un "
                         "booléen (true ou false)")
    try:
        plan = build_query_plan(
            query, resolve_sources(sources), query_variants=query_variants,
            languages=languages, mode=mode, from_date=from_date,
            to_date=to_date, hal_fulltext=hal_fulltext,
        )
    except ValueError as exc:     # QueryPlanError, source inconnue
        return ToolError(f"Paramètre refusé : {exc}")
    if args.get("plan_only"):
        return _plan_only_result(plan)
    # Requête ambiguë (PAT-19, décision DP-7) : refusée par défaut quand une
    # source facturée est demandée (la lecture retenue partait telle quelle
    # vers SerpAPI), envoyée avec un avertissement en tête sinon.
    from connectors.registry import is_metered_source
    on_ambiguity = args.get("on_ambiguity")
    if on_ambiguity is not None and on_ambiguity not in ("warn", "refuse"):
        return ToolError(f"Paramètre refusé : on_ambiguity {on_ambiguity!r} "
                         f"inconnu (attendu : warn, refuse).")
    metered = [e.label for e in plan.sources if is_metered_source(e.source)]
    if on_ambiguity is None:
        on_ambiguity = "refuse" if metered else "warn"
    if plan.ambiguities and on_ambiguity == "refuse":
        readings = " ; ".join(f"« {a['reading']} »" for a in plan.ambiguities)
        why = (f"source facturée demandée ({', '.join(metered)}) : "
               if metered else "")
        return ToolError(
            f"Requête ambiguë, non envoyée : lue {readings}. {why}ajouter des "
            f"parenthèses pour fixer la lecture, ou passer "
            f"on_ambiguity='warn' pour l'envoyer telle quelle (plan_only "
            f"montre la lecture sans rien envoyer).",
            structured={"query": query, "ambiguities": plan.ambiguities,
                        "query_plan": plan.as_dict()})

    # Plafond serveur DUR (anti-DoS) : clamp HTTP-only, indépendant de l'arg
    # client. En stdio (local), pas de clamp.
    http = TRANSPORT_MODE == "http"
    if http:
        max_per_source = min(raw_max, MOISSON_MAX_ITEMS_PER_SOURCE)
    else:
        max_per_source = raw_max

    # Échéance souple (OPT-09 bis) : stdio seulement ; comptée dès maintenant
    # (l'entretien du cache et la préparation font partie de l'appel). En
    # HTTP, deadline_seconds est sans effet : la deadline dure
    # (MOISSON_DEADLINE_SECONDS) reste le seul contrat.
    soft_seconds = None
    if not http:
        soft_seconds = _soft_deadline_seconds(args.get("deadline_seconds"))
        if isinstance(soft_seconds, ToolError):
            return soft_seconds
    soft_at = (time.monotonic() + soft_seconds) if soft_seconds else None

    loop = asyncio.get_running_loop()
    cancel_event = threading.Event()
    reporter = _make_progress_reporter(loop, cancel_event)
    # Résultat partiel d'une moisson annulée (``OperationCancelled.result``),
    # capté dans le fil : le plan de requête accompagne aussi l'erreur
    # d'annulation ou d'échéance (OPT-11).
    partial: dict[str, Any] = {}

    def build(deadline):
        # Échéance du moteur (HTTP) : les délais réseau et les attentes s'y
        # bornent, si bien que le fil s'arrête de lui-même à l'échéance.
        extra = {"mode": mode}
        if query_variants:
            extra["query_variants"] = query_variants
        if languages:
            extra["languages"] = languages
        if hal_fulltext:
            extra["hal_fulltext"] = True
        if ranking is not None:
            extra["ranking"] = ranking
        if args.get("inline") is not None:
            extra["inline"] = parsed_inline[0]
        if args.get("top") is not None:
            extra["top"] = parsed_inline[1]
        if deadline is not None:
            extra["deadline"] = deadline
        run = functools.partial(
            _run_moisson_sync,
            query, max_per_source, sources, enrich_oai, do_dedup,
            from_date, to_date, cache, workers, reporter, cancel_event,
            **extra,
        )

        def run_keeping_partial():
            # Portée de l'échéance souple (OPT-09 bis), ouverte dans le fil de
            # travail : le contexte d'exécution créé par _run_moisson_sync
            # la prend ; neutre sans échéance (HTTP, deadline_seconds=0).
            from engine.models import soft_deadline_scope
            try:
                with soft_deadline_scope(soft_at, soft_seconds):
                    return run()
            except OperationCancelled as exc:
                partial["result"] = getattr(exc, "result", None)
                raise
        return run_keeping_partial

    result = await _run_guarded(build, cancel_event=cancel_event,
                                deadline_note="aucun corpus partiel publié")
    if (isinstance(result, ToolError) and result.cancelled
            and result.structured is None):
        result.structured = _cancelled_structured(
            query, mode, plan, partial.get("result"))
    if soft_seconds:
        # « Atteinte » d'après l'horloge au retour (revue OPT-09 bis) : une
        # source arrêtée plus tôt, faute de créneau de requête avant
        # l'échéance, ne la rend pas « atteinte ».
        result = _note_soft_deadline(result, soft_seconds, at=soft_at)
        # Plafond du digest revérifié après la ligne d'échéance (OPT-17).
        if isinstance(result, tuple) and len(result) == 2 \
                and isinstance(result[1], dict):
            _fit_digest(result[0], result[1], CQ.DIGEST_MAX_BYTES)
    return result


def _cancelled_structured(query: str, mode: str, plan,
                          harvest_result=None) -> dict:
    """
    ``structuredContent`` d'une moisson annulée ou arrivée à échéance : le
    plan de requête (complété de ce qui a été appliqué si le moteur a rendu
    un résultat partiel) et le statut des sources déjà traitées. Aucun
    chemin disque (identique en stdio et en HTTP) ; aucun corpus publié.
    """
    structured = {
        "query": query,
        "mode": mode,
        "harvest_status": "cancelled",
        "item_count": 0,
        "query_plan": plan.as_dict(),
    }
    if harvest_result is not None:
        if harvest_result.query_plan:
            structured["query_plan"] = harvest_result.query_plan
        structured["by_source_status"] = harvest_result.by_source_status
    if TRANSPORT_MODE == "http":
        structured["transport"] = "http"
    return structured


def _plan_only_result(plan) -> tuple[list, dict]:
    """
    Retour de ``moisson_search`` en aperçu (``plan_only``) : le plan de
    requête en texte et dans ``structuredContent.query_plan`` ; aucune
    requête réseau, aucun fichier, aucun chemin (identique en stdio et en
    HTTP).
    """
    lines = [
        "Aperçu du plan de requête : aucune moisson exécutée, aucun fichier "
        "écrit.",
        *plan.ambiguity_lines(),
        "",
        *plan.text_lines(),
    ]
    structured = {
        "query": plan.original_query,
        "mode": plan.mode,
        "plan_only": True,
        "query_plan": plan.as_dict(),
        "item_count": 0,
    }
    if plan.ambiguities:
        structured["warnings"] = plan.ambiguity_lines()
    if TRANSPORT_MODE == "http":
        structured["transport"] = "http"
    return [types.TextContent(type="text", text="\n".join(lines))], structured


def _cleanup_partial_raw(run_id: str) -> None:
    """Supprime les payloads bruts partiels d'une moisson annulée
    (``OUTPUT_DIR/raw/*_{run_id}.json``). Le moteur le fait lui-même à
    l'annulation (``engine.outputs.cleanup_raw``) ; conservé pour les
    appelants historiques."""
    cleanup_raw(OUTPUT_DIR, run_id)


def mcp_cache_option(value: bool | None) -> bool:
    """
    Option cache de moisson_search, à trois états (OPT-09) : ``True`` /
    ``False`` explicites priment ; absente (``None``), elle vaut
    ``MOISSON_CACHE`` si l'exploitant l'a défini dans l'environnement du
    serveur, sinon ACTIVÉ (défaut MCP, durée de vie ``MCP_CACHE_TTL``).
    """
    if value is not None:
        return bool(value)
    if os.environ.get("MOISSON_CACHE", "").strip():
        return cache_enabled_from_env()
    return True


def mcp_cache_dir() -> str:
    """
    Fichier de cache du serveur MCP : celui de la CLI (``MOISSON_CACHE_DIR``,
    défaut ``~/.cache/moisson_shs/http``) suffixé de « _mcp ». requests-cache
    fixe l'expiration d'une réponse à son écriture : un fichier propre au
    serveur garantit qu'aucune réponse écrite par la CLI (24 h) ne lui est
    servie au-delà de ``MCP_CACHE_TTL``.
    """
    base = os.environ.get(
        "MOISSON_CACHE_DIR",
        str(Path.home() / ".cache" / "moisson_shs" / "http"),
    )
    return base.rstrip("/\\") + "_mcp"


# Entretien du cache MCP : dernier passage (horloge monotone) par fichier, pour
# ce processus. Le verrou ne sert qu'à décider : deux moissons simultanées
# n'entretiennent pas le même fichier en même temps.
_MCP_CACHE_MAINTENANCE_LOCK = threading.Lock()
_MCP_CACHE_MAINTAINED_AT: dict[str, float] = {}


def _maintain_mcp_cache(http_client, cache_dir: str) -> dict | None:
    """
    Entretien du fichier de cache du serveur MCP (revue d'OPT-09).

    requests-cache ne retire jamais une réponse expirée : sans entretien, le
    cache actif par défaut grossirait d'environ 15 Mo par moisson distincte,
    indéfiniment. Au plus une fois par ``MCP_CACHE_PRUNE_INTERVAL`` secondes
    et par fichier dans ce processus (toujours à la première moisson ; 0 : à
    chaque moisson ; négatif : jamais), avant la moisson : réponses expirées
    retirées, plafond ``MCP_CACHE_MAX_MB`` (0 : aucun), compactage éventuel
    (``HTTPClient.prune_cache``). Un échec est journalisé et n'empêche jamais
    la moisson.

    Returns:
        Le bilan de ``prune_cache``, ou ``None`` (cache inactif, entretien
        récent ou désactivé, échec).
    """
    if not getattr(http_client, "cache_enabled", False):
        return None
    interval = MCP_CACHE_PRUNE_INTERVAL
    if interval < 0:
        return None
    now = time.monotonic()
    with _MCP_CACHE_MAINTENANCE_LOCK:
        last = _MCP_CACHE_MAINTAINED_AT.get(cache_dir)
        if last is not None and now - last < interval:
            return None
        _MCP_CACHE_MAINTAINED_AT[cache_dir] = now
    max_bytes = MCP_CACHE_MAX_MB * 1024 * 1024 if MCP_CACHE_MAX_MB > 0 else None
    t0 = time.monotonic()
    try:
        stats = http_client.prune_cache(max_bytes=max_bytes)
    except Exception as exc:  # noqa: BLE001 - l'entretien ne bloque rien
        logger.warning(
            f"Entretien du cache HTTP MCP impossible ({type(exc).__name__}: "
            f"{exc}) : moisson poursuivie"
        )
        return None
    if stats:
        logger.info(
            "Entretien du cache HTTP MCP : %s réponse(s) expirée(s) et %s "
            "au-delà du plafond retirées, %s → %s octets%s (%.2f s)",
            stats.get("expired_removed"), stats.get("size_removed"),
            stats.get("bytes_before"), stats.get("bytes_after"),
            ", compacté" if stats.get("vacuumed") else "",
            time.monotonic() - t0,
        )
    return stats


def _run_moisson_sync(
    query: str,
    max_per_source: int,
    sources: list[str],
    enrich_oai: bool,
    do_dedup: bool,
    from_date: str = None,
    to_date: str = None,
    cache: bool | None = None,
    workers: int | None = None,
    progress=None,
    cancel_event=None,
    mode: str = MODE_BIBLIOGRAPHIE,
    deadline: float | None = None,
    query_variants: dict | None = None,
    languages: list | None = None,
    ranking: str | None = None,
    inline: str | None = None,
    top: int | None = None,
    hal_fulltext: bool = False,
) -> tuple[list, dict] | ToolError:
    """
    Exécution synchrone de la moisson (dans un thread d'executor).

    Adaptateur du moteur commun (``engine.harvest``, même orchestration que
    la CLI) : construit la demande et le contexte d'exécution, écrit les
    fichiers (``engine.outputs.write_outputs``) et le retour MCP. Progression
    par source (``progress``) et annulation coopérative (``cancel_event``,
    ``deadline``) : toutes deux optionnelles (None ⇒ comportement historique).

    Plan de requête (OPT-11) : ``query_variants`` et ``languages`` passent
    dans la demande ; le plan (requête envoyée à chaque source, filtres
    natifs et locaux, avertissements, filtre local appliqué) est toujours
    renvoyé dans ``structuredContent.query_plan``, succès comme échec, et
    résumé dans le texte (sans aucun chemin disque).

    Défauts MCP (OPT-09) : ``workers=None`` ⇒ ``MCP_DEFAULT_WORKERS`` ;
    ``cache=None`` ⇒ ``mcp_cache_option`` (activé sauf ``MOISSON_CACHE``
    défini), durée de vie ``MCP_CACHE_TTL``, fichier ``mcp_cache_dir()``,
    entretenu avant la moisson (``_maintain_mcp_cache``).

    Classement (OPT-13) : ``ranking`` (``native``, ``local``, ``off`` ;
    None ⇒ défaut du moteur, ``MOISSON_RANKING``) ; l'extrait du classement
    (rang, score, identifiant, position dans le corpus, version) va dans
    ``structuredContent.top_ranked``, jamais dans les notices ni dans
    l'ordre du corpus.

    Retour (OPT-17) : ``inline`` (``digest`` par défaut en stdio, ``full`` =
    sprint 05, ``none``) et ``top`` ; en mode digest, le plan de requête et
    le statut par source sont rendus sous forme compacte (détail complet au
    manifeste) et les ``top`` premières notices du classement vont dans le
    texte et ``structuredContent.top_items``, le tout sous
    ``DIGEST_MAX_BYTES`` octets. Le résumé distingue une limite du
    fournisseur (fenêtre de 200 résultats d'Isidore) d'une perte : « succès
    partiel » est réservé aux sources en échec, partielles sur panne ou
    échéance, ou non exécutées.

    Raises:
        MoissonCancelled: si l'annulation a été demandée (deadline/client). Les
            ``raw/`` partiels sont alors nettoyés et AUCUN corpus final n'est écrit.

    Returns:
        Le tuple ``(content_blocks, structured)`` construit par
        ``_build_corpus_tool_result`` ; ou une ``ToolError`` (isError) si
        aucune source n'a répondu sans panne et qu'aucune notice n'a été
        obtenue (``HarvestResult.all_failed``) : rien n'est alors écrit, et
        le texte comme ``structuredContent`` donnent le statut et le motif
        de chaque source. Une sous-source d'Isidore absente de la fenêtre
        de 200 résultats n'est pas une panne (corpus vide publié).
    """
    from main import slugify

    # Mode de retour (OPT-17), vérifié avant toute requête.
    try:
        inline = CQ.resolve_inline(inline)
        top = CQ.resolve_top(top)
    except QueryError as e:
        return ToolError(f"Paramètre refusé : {e}")

    # Développe les groupes (francophone/international/all) en noms de sources
    sources = resolve_sources(sources)

    # Identifiant de run = timestamp seconde + uuid court : évite toute collision
    # de noms de fichiers sous concurrence (correctif revue 06–09). Les raw/ par
    # source héritent du même run_id.
    run_id = new_run_id()
    started_at = now_iso()
    stem = corpus_stem(slugify(query), run_id)

    # Défauts MCP (OPT-09) : cache actif (TTL 6 h, fichier propre au
    # serveur) sauf refus explicite ou MOISSON_CACHE défini ; workers.
    use_cache = mcp_cache_option(cache)
    if workers is None:
        workers = MCP_DEFAULT_WORKERS
    # Demande et plan de requête avant le client HTTP : une demande invalide
    # échoue sans rien ouvrir (ni cache, ni connexion).
    request = SearchRequest(
        query=query, sources=sources, from_date=from_date, to_date=to_date,
        mode=mode, max_per_source=max_per_source, enrich_oai=enrich_oai,
        dedup=do_dedup, cache=use_cache, workers=workers or 1,
        query_variants=query_variants or {}, languages=languages or [],
        hal_fulltext=bool(hal_fulltext),
    )
    plan = request.query_plan()
    cache_dir = mcp_cache_dir()
    http_client = HTTPClient(use_cache=use_cache, cache_ttl=MCP_CACHE_TTL,
                             cache_dir=cache_dir)
    # Entretien du fichier de cache (expirées, plafond), au plus une fois par
    # MCP_CACHE_PRUNE_INTERVAL : sans lui, il grossirait sans limite.
    _maintain_mcp_cache(http_client, cache_dir)
    # Audit des réponses natives (OPT-16, C32 ; MOISSON_AUDIT_NATIVE=1) :
    # capture bornée et expurgée dans OUTPUT_DIR/.native_audit/ (service).
    native_audit = native_audit_for_run(OUTPUT_DIR, run_id)
    if native_audit is not None:
        attach_native_audit(http_client, native_audit)
    budgets = {"max_per_source": max_per_source}
    if deadline is not None:
        budgets["deadline_s"] = round(max(0.0, deadline - time.monotonic()), 1)
    ctx = RunContext(
        run_id=run_id, deadline=deadline, cancel_event=cancel_event,
        progress=(progress.report if progress is not None else None),
        http_client=http_client, output_dir=OUTPUT_DIR, budgets=budgets,
    )
    try:
        # ranking transmis seulement s'il est fixé : l'appel historique
        # harvest(request, ctx) reste celui des doublures de test.
        result = (harvest(request, ctx) if ranking is None
                  else harvest(request, ctx, ranking=ranking))
    finally:
        http_client.close()

    duration = result.duration_s
    cache_on = bool(getattr(http_client, "cache_enabled", use_cache))
    settings = {
        "max_per_source": max_per_source,
        "workers": request.workers,
        "cache": cache_on,
        "cache_ttl_seconds": MCP_CACHE_TTL if cache_on else None,
    }
    settings_line = (
        f"Réglages : {max_per_source} notices au plus par source, "
        f"{request.workers} source(s) à la fois, cache "
        + (f"actif ({MCP_CACHE_TTL // 3600} h)" if cache_on else "inactif")
    )
    structured = {
        "query": query,
        "mode": mode,
        "settings": settings,
        "by_source": result.by_source,
        "by_source_status": result.by_source_status,
        "harvest_status": result.overall_status,
        "total_before_dedup": result.total_before_dedup,
        "total_unique": len(result.items),
        "duplicates_removed": result.duplicates_removed,
        "duration_seconds": round(duration, 1),
        # Plan de requête (OPT-11) : toujours présent après une moisson.
        "query_plan": result.query_plan or plan.as_dict(),
    }
    plan_lines = plan.summary_lines()

    if result.all_failed:
        # Toutes les sources demandées ont échoué (erreur, clé absente,
        # annulation, partielle sur erreur) sans aucune notice : une panne,
        # jamais « aucun article ». Aucun fichier écrit (payloads bruts
        # supprimés par le moteur) ; isError=True.
        lines = [
            "Moisson en échec : aucune source n'a pu être moissonnée ; aucun "
            "corpus n'a été écrit.",
            *plan.ambiguity_lines(),
            *(f"Attention : {line}" for line in result.flow_outage_lines()),
            "",
            f"Requête : {query}",
            f"Durée : {duration:.1f} s",
            settings_line,
            *plan_lines,
            "",
            "Statut par source :",
            *result.status_lines(),
        ]
        structured["item_count"] = 0
        return ToolError("\n".join(lines), structured=structured)

    all_items = result.items
    # Dernier contrôle avant publication (comme search_by_author,
    # fetch_by_doi et corpus_resolve_dois) : une annulation ou une échéance
    # tombée depuis la fin de la moisson empêche d'écrire le corpus.
    try:
        ctx.check()
    except OperationCancelled as exc:
        cleanup_raw(OUTPUT_DIR, run_id)
        # Statuts et plan de la moisson (rien n'est publié) : repris dans
        # l'erreur d'annulation par _tool_moisson_search.
        if getattr(exc, "result", None) is None:
            exc.result = result
        raise
    # Publication atomique, manifeste en dernier (OPT-16) : plan, statuts,
    # versions, empreintes, rangs et scores par notice (jamais dans les
    # notices) ; index de OUTPUT_DIR mis à jour sous verrou.
    manifest_fields = _harvest_manifest(result, settings, ctx, native_audit)
    files = write_outputs(
        all_items, OUTPUT_DIR, stem, query, run_id=run_id,
        started_at=started_at, path_roots=(OUTPUT_DIR,),
        manifest=manifest_fields,
    )
    stats = files.stats
    output_json, output_csl, output_stats = (
        files.json_path, files.csl_path, files.stats_path)
    output_manifest = getattr(files, "manifest_path", None)

    http = TRANSPORT_MODE == "http"
    compact = inline != INLINE_FULL and not http
    # Limite du fournisseur (fenêtre d'Isidore) et pertes réelles (OPT-17) :
    # « succès partiel » est réservé aux secondes.
    losses = _lossy_sources(result)
    limited = [r for r in result.sources if _provider_limited(r)]
    if losses:
        head = (f"Moisson terminée (succès partiel : {len(losses)} source(s) "
                f"en échec, partielle(s) ou non exécutée(s), voir le statut "
                f"par source).")
    elif limited:
        head = (f"Moisson terminée, sans panne : {len(limited)} source(s) "
                f"limitée(s) par le fournisseur, voir le statut par source.")
    else:
        head = "Moisson terminée."
    # Panne d'un flux partagé (Isidore et ses cinq sous-sources) dite en une
    # ligne, et sources non configurées nommées à part (PAT-16).
    outages = result.flow_outages()
    if outages:
        structured["flow_outages"] = outages
    unconfigured = [r.display_name for r in result.unconfigured]
    if unconfigured:
        structured["unconfigured_sources"] = unconfigured
    # === Construction de la réponse pour Claude ===
    if plan.ambiguities:
        structured["warnings"] = plan.ambiguity_lines()
    lines = [
        head,
        *plan.ambiguity_lines(),
        *(f"Attention : {line}" for line in result.flow_outage_lines()),
        *([f"Sources non configurées (clé d'API absente) : "
           f"{', '.join(unconfigured)}."] if unconfigured else []),
        f"",
        f"Requête : {query}",
        f"Durée : {duration:.1f} s",
        settings_line,
        *plan_lines,
        f"",
        f"Items collectés par source (statut) :",
        *_harvest_status_lines(result, languages, grouped=compact),
    ]
    if not compact and any(r.reason == "provider_window" for r in limited):
        lines.append(
            f"Limite du fournisseur, pas une panne : Isidore ne sert que les "
            f"{ISIDORE_RESULT_WINDOW} premiers résultats d'une requête, "
            f"partagés avec ses sous-sources ; au-delà, préciser la requête "
            f"ou restreindre les dates.")
    if compact:
        # Mode digest ou none : bilan sur deux lignes, radical des fichiers
        # et dossier une seule fois.
        stem_ = output_json.name[:-len(".json")]
        lines.extend([
            f"",
            f"Notices : {result.total_before_dedup} reçues, {len(all_items)} "
            f"après déduplication ({result.duplicates_removed} doublons "
            f"fusionnés) ; avec DOI : {stats['with_doi']} "
            f"({stats['doi_coverage_pct']} %), avec résumé : "
            f"{stats['with_abstract']} ({stats['abstract_coverage_pct']} %).",
            _compact_files_line(output_json, output_manifest,
                                "plan, statuts, rangs et scores"),
        ])
    else:
        lines.extend([
            f"",
            f"Total avant déduplication : {result.total_before_dedup}",
            f"Total après déduplication : {len(all_items)}",
            f"Doublons fusionnés : {result.duplicates_removed}",
            f"",
            f"Couverture qualitative :",
            f"  - Items avec DOI : {stats['with_doi']} "
            f"({stats['doi_coverage_pct']} %)",
            f"  - Items avec résumé : {stats['with_abstract']} "
            f"({stats['abstract_coverage_pct']} %)",
            f"",
            f"Fichiers produits :",
            f"  - Zotero JSON (interne)   : {output_json}",
            f"  - CSL-JSON (import Zotero) : {output_csl}",
            f"  - Statistiques            : {output_stats}",
            *([f"  - Manifeste (traçabilité, rangs et scores) : "
               f"{output_manifest}"] if output_manifest else []),
            f"",
            f"Import Zotero : Fichier > Importer > Un fichier > sélectionner",
            f"le fichier .csl.json (Zotero détecte automatiquement le format",
            f"CSL JSON). Le .json sans suffixe csl est le format interne du",
            f"moissonneur, exploitable par le MCP pour analyses ultérieures.",
        ])

    summary = "\n".join(lines)
    structured.update({
        "doi_coverage_pct": stats.get("doi_coverage_pct"),
        "abstract_coverage_pct": stats.get("abstract_coverage_pct"),
        # Pertes réelles et limites du fournisseur (OPT-17) : harvest_status
        # reste « partial » dès qu'une source n'est pas complète.
        "lossy_sources": [r.display_name for r in losses],
        "provider_limited_sources": [r.display_name for r in limited],
    })
    if output_manifest:
        structured["manifest"] = output_manifest.name
    if result.dedup_report:
        structured["dedup_report"] = {
            k: v for k, v in result.dedup_report.items() if k != "groups"
        }
    # Classement (OPT-13) : extrait compact (rang, score, identifiant,
    # position dans le corpus, version), sans chemin ni notice ; absent si
    # ranking='off'. Le corpus garde son ordre déterministe. En mode digest,
    # top_items (même rang, score et identifiant, plus la notice) le
    # remplace : top_ranked n'y garde que son en-tête.
    top_ranked = top_ranked_payload(getattr(result, "ranking", None))
    digest_spec = None
    reserve = 0
    if compact:
        manifest_name_ = output_manifest.name if output_manifest else None
        structured["query_plan"] = _compact_query_plan(
            structured.get("query_plan"), manifest_name_)
        structured["by_source_status"] = _compact_source_status(
            structured.get("by_source_status"))
        if top_ranked is not None and inline == INLINE_DIGEST:
            top_ranked = {k: v for k, v in top_ranked.items() if k != "items"}
            top_ranked["items_in"] = "top_items"
        digest_spec = DigestSpec(
            items=all_items, records=(manifest_fields or {}).get("records"),
            ranking=getattr(result, "ranking", None), query=query,
            query_variants=query_variants, corpus_path=output_json,
            source_status=result.by_source_status)
        reserve = _soft_deadline_reserve(result)
    if top_ranked is not None:
        structured["top_ranked"] = top_ranked
    # Retour enrichi : le JSON inliné (mode full) est le corpus Zotero INTERNE
    # (format riche), conformément au cadrage. Le CSL-JSON et les stats
    # restent des ResourceLink (extra_files).
    return _build_corpus_tool_result(
        summary_text=summary,
        primary_items=all_items,
        primary_path=output_json,
        primary_mime="application/json",
        extra_files=[output_csl, output_stats,
                     *([output_manifest] if output_manifest else [])],
        structured=structured,
        inline=inline, top=top, digest=digest_spec, reserve_bytes=reserve,
    )


# === Résumé de moisson (OPT-17) : limites du fournisseur, pertes, compacité ===

def _provider_limited(r) -> bool:
    """Source tronquée par une limite du fournisseur (fenêtre de 200
    résultats d'Isidore, borne de parcours) : exécutée, pas une panne."""
    from engine.models import BOUNDED_REASONS
    return r.status == "partial" and r.reason in BOUNDED_REASONS


def _lossy_sources(result) -> list:
    """Sources qui ont réellement perdu des notices : en échec, annulées,
    non exécutées, ou partielles pour une panne ou l'échéance. Une source
    non configurée (clé d'API absente) n'en est pas : elle est nommée à part
    (PAT-16 ; toute moisson « all » sans clé affichait « succès partiel »)."""
    return [r for r in result.sources
            if not r.succeeded and not _provider_limited(r)
            and not (r.status == "skipped" and r.reason == "missing_key")]


def _language_filter_text(r, languages) -> str:
    """« filtre local de langue (fr, en) : 141 → 139 notice(s) (…) » d'une
    source filtrée localement (avant et après le filtre), chaîne vide sinon."""
    counters = r.counters or {}
    try:
        removed = int(counters.get("language_filter_removed") or 0)
        unknown = int(counters.get("language_filter_unknown_kept") or 0)
    except (TypeError, ValueError):
        return ""
    if not (removed or unknown):
        return ""
    langs = ", ".join(languages or []) or "langues demandées"
    return (f"filtre local de langue ({langs}) : {r.item_count + removed} → "
            f"{r.item_count} notice(s) ({removed} écartée(s), {unknown} sans "
            f"langue lisible conservée(s))")


def _harvest_status_lines(result, languages, grouped: bool = False) -> list[str]:
    """
    Statut par source : notices, statut et motif. Une limite du fournisseur
    est dite comme telle (« pas une panne ») et la fenêtre commune n'est
    détaillée qu'une fois ; une panne, une échéance ou une source non
    exécutée garde le texte du moteur (``SourceResult.describe``). Après un
    filtre local de langue : notices avant et après le filtre.

    ``grouped`` (mode digest) : les sources limitées par une même fenêtre
    tiennent sur une ligne (« Isidore 129, OpenEdition 50 — limite du
    fournisseur… »).
    """
    from engine.models import REASON_LABELS

    out = []
    windows = set()
    groups: dict = {}
    for r in result.sources:
        filt = _language_filter_text(r, languages)
        if _provider_limited(r):
            detail = r.detail or ""
            common, _sep, specific = detail.partition(" ; ")
            label = REASON_LABELS.get(r.reason, r.reason)
            if grouped:
                key = (r.reason, common)
                if key not in groups:
                    groups[key] = {"line": len(out), "label": label,
                                   "common": common, "members": [],
                                   "filters": []}
                    out.append(None)
                groups[key]["members"].append(f"{r.display_name} {r.item_count}")
                if filt:
                    groups[key]["filters"].append(f"{r.display_name} : {filt}")
                continue
            if common and common in windows:
                text = ("limite du fournisseur, pas une panne (même fenêtre"
                        + (f" : {specific}" if specific else "") + ")")
            else:
                if common:
                    windows.add(common)
                text = (f"limite du fournisseur, pas une panne ({label}"
                        + (f" : {detail}" if detail else "") + ")")
        else:
            text = r.describe()
        if filt:
            text += f" ; {filt}"
        out.append(f"  - {r.display_name} : {r.item_count} — {text}")
    for g in groups.values():
        text = (f"  - {', '.join(g['members'])} — limite du fournisseur, pas "
                f"une panne ({g['label']}"
                + (f" : {g['common']}" if g["common"] else "")
                + " ; fenêtre partagée par ces sources ; au-delà, préciser la "
                  "requête ou restreindre les dates)")
        if g["filters"]:
            text += " ; " + " ; ".join(g["filters"])
        out[g["line"]] = text
    return out


# Statut compact par source (mode digest) : clés toujours présentes, clés
# booléennes rendues seulement si vraies, compteurs utiles non nuls ; le
# statut complet (tri natif, filtres, pages, durées) est au manifeste.
_COMPACT_STATUS_ALWAYS = ("source", "status", "reason", "items")
_COMPACT_STATUS_FLAGS = ("limit_reached", "exhausted", "provider_window")
_COMPACT_COUNTERS = ("retained", "total_available", "language_filter_removed",
                     "language_filter_unknown_kept",
                     "soft_deadline_unreachable")


def _compact_source_status(by_status) -> dict:
    """
    Statut par source allégé (mode digest, OPT-17) : statut, motif et
    notices ; plafond, épuisement et fenêtre du fournisseur quand ils sont
    vrais ; détail (tronqué) seulement pour une perte (une limite du
    fournisseur est dite dans le texte et au manifeste) ; compteurs de
    fenêtre, de filtre de langue et d'échéance non nuls ; au plus trois
    avertissements tronqués.
    """
    if not isinstance(by_status, dict):
        return by_status
    from engine.models import BOUNDED_REASONS

    out = {}
    for name, info in by_status.items():
        if not isinstance(info, dict):
            out[name] = info
            continue
        entry = {k: info.get(k) for k in _COMPACT_STATUS_ALWAYS}
        bounded = (info.get("status") == "partial"
                   and info.get("reason") in BOUNDED_REASONS)
        # Fenêtre du fournisseur : dite par le motif (provider_window), son
        # détail et ses compteurs sont dans le texte et au manifeste.
        entry.update({k: True for k in _COMPACT_STATUS_FLAGS
                      if info.get(k) and not (bounded and k == "provider_window")})
        detail = info.get("detail")
        if detail and not bounded:
            entry["detail"] = detail if len(detail) <= 200 else detail[:199] + "…"
        keep = _COMPACT_COUNTERS if not bounded else _COMPACT_COUNTERS[2:]
        counters = {k: v for k, v in (info.get("counters") or {}).items()
                    if k in keep and v}
        if counters:
            entry["counters"] = counters
        warnings = [w if len(w) <= 200 else w[:199] + "…"
                    for w in (info.get("warnings") or []) if isinstance(w, str)]
        if warnings:
            entry["warnings"] = warnings[:3]
        out[name] = entry
    return out


def _compact_query_plan(plan, manifest_name) -> dict | None:
    """
    Plan de requête résumé (mode digest, OPT-17) : requête d'origine,
    variantes, langues, mode, dates (clés vides omises), avertissements et,
    par source, sa traduction, la requête envoyée quand elle diffère de la
    requête d'origine (absente : requête d'origine transmise) et ce qui a
    été appliqué (filtre local de langue) ; les filtres par source sont dans
    le texte du plan et, avec le plan complet, au manifeste (``detail``).
    """
    if not isinstance(plan, dict):
        return plan
    sources = []
    for entry in plan.get("sources") or []:
        if not isinstance(entry, dict):
            continue
        item = {"source": entry.get("source"),
                "translation": entry.get("translation")}
        if entry.get("query_sent") != plan.get("original_query"):
            item["query_sent"] = entry.get("query_sent")
        if entry.get("warnings"):
            item["warnings"] = list(entry["warnings"])
        if entry.get("applied"):
            item["applied"] = entry["applied"]
        sources.append(item)
    out = {k: plan.get(k) for k in ("version", "original_query",
                                    "query_variants", "languages", "mode",
                                    "from_date", "to_date",
                                    "automatic_translation")
           if plan.get(k) not in (None, "", [], {})}
    out["sources"] = sources
    out["warnings"] = list(plan.get("warnings") or [])
    out["detail"] = ({"manifest": manifest_name, "key": "query_plan"}
                     if manifest_name else None)
    out["compact"] = True
    return out


# Recherche par auteur en mode digest (OPT-17) : champs d'un profil gardés
# dans structuredContent (identification et argument du second appel) ; les
# exemples de titres, co-auteurs, période, affiliation et indicateurs
# bibliométriques restent dans le texte (lignes des candidats et des profils
# à confirmer) et au manifeste (author_report).
_COMPACT_PROFILE_KEYS = ("source", "id", "name", "idhal", "orcid",
                         "works_count", "argument", "source_status")
# Statut compact d'une source de recherche par auteur : clés rendues quand
# elles sont renseignées ; booléens rendus seulement s'ils sont vrais.
_COMPACT_AUTHOR_STATUS_KEYS = ("method", "pages", "identity_basis",
                               "available", "rejected_not_author", "name_hint")
_COMPACT_AUTHOR_STATUS_FLAGS = ("limit_reached", "exhausted",
                                "provider_window", "identity_verified",
                                "unseparated")
# Autres profils compatibles d'un profil à confirmer : les premiers seulement
# (la liste entière est au manifeste).
_COMPACT_OTHER_PROFILES = 5


def _compact_profile(profile) -> dict:
    """Profil d'auteur allégé (candidat, identité retenue, profil à
    confirmer) : identification et argument ; le nombre des autres profils
    compatibles, et les premiers d'entre eux."""
    if not isinstance(profile, dict):
        return profile
    out = {k: profile[k] for k in _COMPACT_PROFILE_KEYS
           if profile.get(k) not in (None, "", [], {})}
    others = profile.get("other_compatible_profiles")
    if isinstance(others, list) and others:
        out["other_compatible_profiles"] = [
            {k: p[k] for k in ("id", "works_count", "argument")
             if isinstance(p, dict) and p.get(k) not in (None, "", {})}
            for p in others[:_COMPACT_OTHER_PROFILES]]
        if len(others) > _COMPACT_OTHER_PROFILES:
            out["other_compatible_profiles_total"] = len(others)
    return out


def _compact_author_status(by_status) -> dict:
    """
    Statut par source allégé d'une recherche par auteur (mode digest,
    OPT-17) : statut, motif, notices, méthode, pages, fondement de
    l'identité et identité retenue (identifiant, nom, argument) ; nombre de
    candidats et de profils compatibles écartés (listes : ``candidates`` au
    premier niveau, texte et manifeste) ; détail tronqué hors succès,
    compteurs non nuls, au plus trois avertissements tronqués.
    """
    if not isinstance(by_status, dict):
        return by_status
    from engine.models import SUCCESS_STATUSES

    out = {}
    for name, info in by_status.items():
        if not isinstance(info, dict):
            out[name] = info
            continue
        entry = {k: info.get(k) for k in _COMPACT_STATUS_ALWAYS}
        entry.update({k: info[k] for k in _COMPACT_AUTHOR_STATUS_KEYS
                      if info.get(k) not in (None, "", 0, [], {})})
        entry.update({k: True for k in _COMPACT_AUTHOR_STATUS_FLAGS
                      if info.get(k)})
        if "identity_established" in info:
            entry["identity_established"] = bool(info["identity_established"])
        detail = info.get("detail")
        if detail and info.get("status") not in SUCCESS_STATUSES:
            entry["detail"] = detail if len(detail) <= 200 else detail[:199] + "…"
        if isinstance(info.get("identity"), dict):
            entry["identity"] = _compact_profile(info["identity"])
        for key in ("candidates", "excluded_profiles"):
            if isinstance(info.get(key), list) and info[key]:
                entry[f"{key}_count"] = len(info[key])
        counters = {k: v for k, v in (info.get("counters") or {}).items() if v}
        if counters:
            entry["counters"] = counters
        warnings = [w if len(w) <= 200 else w[:199] + "…"
                    for w in (info.get("warnings") or []) if isinstance(w, str)]
        if warnings:
            entry["warnings"] = warnings[:3]
        out[name] = entry
    return out


def _compact_author_structured(structured: dict, manifest_name) -> None:
    """
    ``structuredContent`` d'une recherche par auteur allégé en place (mode
    digest ou none en stdio, OPT-17) : statut par source et profils
    (``candidates``, ``profiles_to_confirm``) compacts, avec le renvoi au
    manifeste qui garde le rapport complet (``sources`` et
    ``author_report``). Sur la recherche live du lot C (191 notices), le
    rapport complet pesait 11,7 Ko : seul, il dépassait presque le plafond
    du digest.
    """
    if isinstance(structured.get("by_source_status"), dict):
        structured["by_source_status"] = _compact_author_status(
            structured["by_source_status"])
    for key in ("candidates", "profiles_to_confirm"):
        if isinstance(structured.get(key), list):
            structured[key] = [_compact_profile(p) for p in structured[key]]
    structured["report_detail"] = (
        {"manifest": manifest_name,
         "keys": {"by_source_status": "sources",
                  "candidates": "author_report.candidates",
                  "profiles_to_confirm": "author_report.profiles_to_confirm"}}
        if manifest_name else None)
    structured["report_compact"] = True


def _compact_files_line(json_path: Path, manifest_path,
                        manifest_note: str = "rapport complet") -> str:
    """Fichiers d'un corpus (mode digest ou none, stdio) : radical et rôles
    des fichiers sur une ligne, dossier sur la suivante, terminé par « / »
    (retour d'expérience B8 : le dossier nu, collé au bloc suivant par le
    client, donnait « …/outputcorpus_… »)."""
    stem_ = json_path.name[:-len(".json")] if json_path.name.endswith(".json") \
        else json_path.stem
    folder = str(json_path.parent).rstrip("/\\") + os.sep
    return (f"Fichiers {stem_} : .json (Zotero interne, lu par les outils), "
            f".csl.json (import Zotero Desktop : Fichier > Importer), "
            f"_stats.txt"
            + (f", .manifest.json ({manifest_note})" if manifest_path else "")
            + f"\nDossier : {folder}")


def _soft_deadline_reserve(result) -> int:
    """Octets réservés sous le plafond du digest pour ce que l'échéance
    souple ajoute ensuite (``_note_soft_deadline`` : ligne de texte si une
    source a été arrêtée, ``structuredContent.soft_deadline`` sinon)."""
    from engine.models import current_soft_deadline
    if current_soft_deadline() is None:
        return 0
    stopped = any(getattr(r, "reason", None) == "deadline"
                  for r in result.sources)
    return 1200 if stopped else 300


def _harvest_manifest(result, settings: dict, ctx, native_audit) -> dict:
    """
    Champs du manifeste d'une moisson (``utils.manifest``) ; une erreur de
    construction (doublure de test incomplète) ne bloque jamais la
    publication : le manifeste le signale.
    """
    try:
        return harvest_manifest_fields(result, settings=settings, ctx=ctx,
                                       native_audit=native_audit)
    except Exception as exc:  # noqa: BLE001 - la publication reste prioritaire
        logger.warning(f"Manifeste de moisson incomplet ({type(exc).__name__}: "
                       f"{exc})", exc_info=True)
        return {"notes": [f"métadonnées de moisson incomplètes "
                          f"({type(exc).__name__})"]}


def _load_zotero_input(corpus_path: Path, tool: str):
    """
    Lecture d'un corpus d'entrée exigé au format Zotero JSON interne
    (OPT-16) : refus clair (``ToolError``, isError) d'un fichier CSL-JSON
    (nom ``.csl.json`` ou contenu CSL), d'un manifeste, d'un rapport DOI ou
    de comparaison, et d'un contenu qui n'est pas un tableau de notices
    (objet, chaîne, tableau d'autre chose que des objets : auparavant une
    ``AttributeError`` dans corpus_filter).

    Returns:
        ``(notices, octets lus)``, ou une ``ToolError`` (refus, JSON
        invalide).
    """
    try:
        check_zotero_corpus(corpus_path, tool=tool)
    except CorpusFormatError as e:
        return ToolError(str(e))
    raw = corpus_path.read_bytes()
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as e:
        return ToolError(f"Erreur de parsing JSON : {e}")
    try:
        check_zotero_corpus(corpus_path, items, tool=tool, loaded=True)
    except CorpusFormatError as e:
        return ToolError(str(e))
    # Message historique de corpus_analyze, corpus_resolve_dois et
    # corpus_to_csl, désormais commun à tous les appelants.
    if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
        return ToolError("Le fichier ne contient pas un tableau d'items Zotero.")
    return items, raw


def _input_description(corpus_path: Path, items, raw: bytes) -> dict:
    """Entrée d'un corpus dérivé pour son manifeste : nom de base, notices,
    empreinte et manifeste source (jamais de chemin disque)."""
    import hashlib
    source_manifest = load_manifest_for(corpus_path)
    return {
        "name": corpus_path.name,
        "items": len(items) if isinstance(items, list) else None,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "manifest": (f"{corpus_stem_of(corpus_path.name)}.manifest.json"
                     if source_manifest is not None else None),
    }


def _manifest_query_text(doc) -> str | None:
    """Requête d'une moisson d'après son manifeste (termes appariés du
    digest, OPT-17) ; None pour une recherche par auteur ou sans requête."""
    query = (doc or {}).get("query")
    if isinstance(query, dict):
        if query.get("author"):
            return None
        query = query.get("text")
    if isinstance(query, str) and query.strip() \
            and not query.startswith("author:"):
        return query
    return None


def _publication_roots(*paths) -> tuple:
    """Dossiers à retirer des chaînes d'un manifeste (filet de sécurité)."""
    roots = [OUTPUT_DIR]
    for p in paths:
        if p is not None:
            roots.append(Path(p).parent)
    return tuple(roots)


async def _tool_corpus_analyze(args: dict[str, Any]) -> str:
    """
    Implémentation de l'outil corpus_analyze.

    Charge un fichier JSON Zotero et produit un rapport statistique. Lecture
    et calculs hors de la boucle asyncio (fil de travail, OPT-07, C17).
    """
    return await asyncio.to_thread(_corpus_analyze_sync, args)


def _corpus_analyze_sync(args: dict[str, Any]) -> str:
    """Corps synchrone de corpus_analyze (lecture disque et statistiques)."""
    corpus_path = _safe_path(args["corpus_path"])  # confiné en HTTP
    if not corpus_path.exists():
        return ToolError(f"Fichier introuvable : {corpus_path.name}")

    # Format Zotero interne exigé : un CSL-JSON donnerait des statistiques
    # fausses (types, sources, années absents), un manifeste n'est pas un
    # corpus (OPT-16).
    loaded = _load_zotero_input(corpus_path, "corpus_analyze")
    if isinstance(loaded, ToolError):
        return loaded
    items, _raw = loaded

    if not isinstance(items, list):
        return ToolError("Le fichier ne contient pas un tableau d'items Zotero.")

    from main import compute_stats
    stats = compute_stats(items)

    # Formatage du rapport
    lines = [
        f"=== Analyse du corpus : {corpus_path.name} ===",
        f"",
        f"Total items : {stats['total_items']}",
        f"",
        f"Couverture :",
        f"  - DOI : {stats['with_doi']} ({stats['doi_coverage_pct']} %)",
        f"  - Résumé : {stats['with_abstract']} ({stats['abstract_coverage_pct']} %)",
        f"",
        f"Par source d'origine :",
    ]
    for source, count in stats["by_source"].items():
        lines.append(f"  - {source} : {count}")

    lines.append("")
    lines.append("Par type d'item Zotero :")
    for itype, count in stats["by_type"].items():
        lines.append(f"  - {itype} : {count}")

    lines.append("")
    lines.append("Par langue :")
    for lang, count in stats["by_language"].items():
        lines.append(f"  - {lang} : {count}")

    lines.append("")
    lines.append("Distribution chronologique :")
    for year, count in stats["by_year"].items():
        lines.append(f"  - {year} : {count}")

    # Enrichissements : top revues, top auteurs. Noms de revue regroupés par
    # une clé normalisée (casse, accents, « & » et « et ») et dépôts écartés
    # (« HAL (…) » n'est pas une revue) ; auteurs comptés par nom et initiale
    # du prénom (retour d'expérience B7). Les notices ne sont pas modifiées.
    from utils.corpus_ops import author_key, journal_key, top_forms
    from utils.deduplication import is_repository_venue
    venue_pairs, repositories = [], 0
    for item in items:
        name = str(item.get("publicationTitle") or "").strip()
        if not name:
            continue
        if is_repository_venue(name):
            repositories += 1
            continue
        venue_pairs.append((journal_key(name), name))
    journals = top_forms(venue_pairs, 20)
    if journals:
        lines.append("")
        lines.append("Top 20 revues :")
        for journal, count in journals:
            lines.append(f"  - {journal} : {count}")
    if repositories:
        lines.append(f"  ({repositories} notice(s) dont le support indiqué est "
                     f"un dépôt, non comptées comme revues)")

    authors = top_forms((author_key(c) for item in items
                         for c in item.get("creators", []) or []
                         if isinstance(c, dict)), 30)
    if authors:
        lines.append("")
        lines.append("Top 30 auteurs (par nombre d'items) :")
        for author, count in authors:
            lines.append(f"  - {author} : {count}")

    return "\n".join(lines)


async def _tool_corpus_list(args: dict[str, Any]) -> str:
    """
    Implémentation de l'outil corpus_list.

    Liste les corpus de OUTPUT_DIR d'après l'index dérivé
    (``utils.manifest.list_corpora`` : ``OUTPUT_DIR/.index.json``, mis à jour
    paresseusement et à chaque publication) : aucune relecture de chaque
    corpus (C15), sauf la première fois pour les corpus anciens sans
    manifeste (nombre d'items calculé puis mémorisé). Fichiers associés et
    de service jamais listés. Travail hors de la boucle asyncio (fil de
    travail, OPT-07, C17). En HTTP, aucun chemin disque.
    """
    return await asyncio.to_thread(_corpus_list_sync, args)


def _corpus_visible(entry: dict, http: bool) -> bool:
    """Corpus listable : au moins un fichier de données visible."""
    return any(_index_file_visible(info, http)
               for info in (entry.get("files") or {}).values())


def _index_file_visible(info: dict, http: bool) -> bool:
    """
    Fichier d'index listable : jamais un lien symbolique vers un fichier de
    service ; en HTTP, jamais un lien sortant de OUTPUT_DIR (illisible à
    distance). Fichiers associés et de service : absents de l'index.
    """
    link = info.get("link")
    if link and link.get("service"):
        return False
    if http and link and not link.get("inside"):
        return False
    return True


def _corpus_list_sync(args: dict[str, Any]) -> str:
    """Corps synchrone de corpus_list (index des corpus, OPT-16)."""
    http = TRANSPORT_MODE == "http"
    limit = args.get("limit")
    if limit is None:
        limit = CORPUS_LIST_DEFAULT_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, (int, float)) \
            or limit != int(limit) or limit < 1:
        return ToolError(f"Paramètre refusé : limit = {limit!r} (entier >= 1 "
                         f"attendu).")
    limit = min(int(limit), CORPUS_LIST_MAX_LIMIT)
    pattern = args.get("pattern") or None
    if pattern is not None and not isinstance(pattern, str):
        return ToolError("Paramètre refusé : pattern doit être une chaîne.")
    try:
        listing = list_corpora(
            OUTPUT_DIR, limit=limit, pattern=pattern, since=args.get("since"),
            rebuild=bool(args.get("rebuild", False)),
            accept=lambda e: _corpus_visible(e, http))
    except ValueError as e:
        return ToolError(f"Paramètre refusé : since : {e}")

    where = "le répertoire de sortie" if http else str(OUTPUT_DIR)
    if listing.total == 0:
        return (
            f"Aucun corpus trouvé dans {where}.\n"
            f"Lancez d'abord une moisson avec l'outil moisson_search."
        )
    header = ("=== Corpus disponibles ===" if http
              else f"=== Corpus disponibles dans {OUTPUT_DIR} ===")
    lines = [header]
    if not http:
        # Instance au code différent sur le même dossier (PAT-10) : rappel
        # en tête, sans refus (décision DP-10).
        from utils.instances import stale_warnings
        lines.extend(f"Attention : {w}." for w in stale_warnings(OUTPUT_DIR))
    filtered = bool(pattern or args.get("since"))
    count = (f"{len(listing.entries)} corpus affiché(s) sur {listing.matched}"
             + (f" correspondant aux filtres ({listing.total} au total)"
                if filtered else ""))
    lines.append(count + ", du plus récent au plus ancien.")
    if listing.matched > len(listing.entries):
        lines.append("Arguments limit, pattern et since pour afficher les "
                     "autres.")
    if not listing.entries:
        lines.append("Aucun corpus ne correspond aux filtres.")
    for entry in listing.entries:
        lines.append("")
        lines.extend(_corpus_list_lines(entry, http))
    return "\n".join(lines)


def _corpus_list_lines(entry: dict, http: bool) -> list[str]:
    """Lignes d'un corpus dans corpus_list (noms de base seulement)."""
    created = entry.get("created_ts")
    date = (datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M:%S")
            if created else "?")
    kind = KIND_LABELS.get(entry.get("kind"), entry.get("kind"))
    status = STATUS_LABELS.get(entry.get("status"), entry.get("status"))
    head = f"    Date : {date}" + (f" · {kind}" if kind else "") + f" · {status}"
    lines = [f"  {entry['stem']}", head]
    if entry.get("query"):
        lines.append(f"    Requête : {entry['query']}")
    elif entry.get("query_from_name"):
        lines.append(f"    Requête (d'après le nom) : {entry['query_from_name']}")
    items = entry.get("item_count")
    lines.append(f"    Items : {items if items is not None else '?'}")
    shown = []
    order = {ROLE_ZOTERO: 0, ROLE_CSL: 1, ROLE_STATS: 2}
    files = sorted((entry.get("files") or {}).items(),
                   key=lambda kv: (order.get(kv[1].get("role"), 9), kv[0]))
    for name, info in files:
        role = info.get("role")
        if role not in order or not _index_file_visible(info, http):
            continue
        bits = []
        fmt = info.get("format")
        if role == ROLE_CSL or fmt == "csl":
            bits.append("CSL-JSON")
        elif role == ROLE_ZOTERO:
            bits.append("Zotero JSON interne" if fmt in (None, "zotero", "empty")
                        else f"format {fmt}")
        if info.get("items") is not None and role != ROLE_STATS:
            bits.append(f"{info['items']} items")
        bits.append(f"{info.get('bytes', 0) / 1024:.1f} ko")
        shown.append(f"{name} ({', '.join(bits)})")
    if shown:
        lines.append("    Fichiers : " + " ; ".join(shown))
    notes = [n for n in entry.get("notes") or [] if "fiche" not in n]
    if notes and entry.get("status") != "legacy":
        lines.append("    Remarques : " + " ; ".join(notes))
    return lines


async def _tool_corpus_filter(args: dict[str, Any]) -> str | tuple[list, dict]:
    """
    Implémentation de l'outil corpus_filter.

    Filtre un corpus selon des critères et produit un sous-corpus.

    En cas d'erreur (fichier introuvable, JSON invalide) renvoie une
    ``ToolError`` (texte historique, ``isError`` côté MCP) ; en cas de
    succès, le retour enrichi (tuple) via le helper. Lecture, filtrage et
    écriture hors de la boucle asyncio (fil de travail, OPT-07, C17).
    """
    return await asyncio.to_thread(_corpus_filter_sync, args)


def _corpus_filter_sync(args: dict[str, Any]) -> str | tuple[list, dict]:
    """Corps synchrone de corpus_filter."""
    corpus_path = _safe_path(args["corpus_path"])  # confiné en HTTP
    if not corpus_path.exists():
        return ToolError(f"Fichier introuvable : {corpus_path.name}")

    # Format Zotero interne exigé (OPT-16) : refus clair d'un CSL-JSON, d'un
    # manifeste ou d'un rapport DOI.
    loaded = _load_zotero_input(corpus_path, "corpus_filter")
    if isinstance(loaded, ToolError):
        return loaded
    items, raw = loaded
    started_at = now_iso()
    parsed_inline = _inline_args(args)      # retour (OPT-17)
    if isinstance(parsed_inline, ToolError):
        return parsed_inline
    inline, top = parsed_inline

    year_min = args.get("year_min")
    year_max = args.get("year_max")
    item_types = set(args.get("item_types", []))
    sources_filter = set(args.get("sources", []))
    language = args.get("language", "").lower()
    keep_unknown_language = args.get("keep_unknown_language", True) is not False
    title_contains = args.get("title_contains", "").lower()
    suffix = args.get("output_suffix", "_filtered")
    # En HTTP : valider le charset du suffixe (anti-traversal via un '/' ou '..').
    if TRANSPORT_MODE == "http":
        suffix = validate_suffix(suffix)

    from utils.cleaner import extract_year, normalize_for_fingerprint
    from utils.query_plan import item_languages, notice_languages
    # Langue demandée ramenée à un code ISO (« fre », « French » → fr) ; une
    # valeur non reconnue garde l'ancienne recherche par sous-chaîne.
    wanted_languages = set(item_languages(language)) if language else set()
    unknown_language = {"kept": 0, "removed": 0}
    filtered = []
    for item in items:
        # Filtre par année
        if year_min or year_max:
            y = extract_year(item.get("date", ""))
            if not y:
                continue
            y = int(y)
            if year_min and y < year_min:
                continue
            if year_max and y > year_max:
                continue

        # Filtre par type
        if item_types and item.get("itemType") not in item_types:
            continue

        # Filtre par source (lu dans 'extra')
        if sources_filter:
            extra = item.get("extra", "")
            found_sources = set()
            for line in extra.split("\n"):
                if line.startswith("Sources:"):
                    found_sources = {
                        s.strip() for s in line[len("Sources:"):].split(",")
                    }
                    break
            if not sources_filter & found_sources:
                continue

        # Filtre par langue (PAT-21, décision DP-8) : notice sans langue
        # lisible gardée et comptée, comme dans la moisson, sauf
        # keep_unknown_language=False.
        if language:
            langs = notice_languages(item)
            if not langs:
                if not keep_unknown_language:
                    unknown_language["removed"] += 1
                    continue
                unknown_language["kept"] += 1
            elif wanted_languages:
                if not wanted_languages & set(langs):
                    continue
            elif language not in str(item.get("language") or "").lower():
                continue

        # Filtre par mot dans titre (normalisé)
        if title_contains:
            title_norm = normalize_for_fingerprint(item.get("title", ""))
            target_norm = normalize_for_fingerprint(title_contains)
            if target_norm not in title_norm:
                continue

        filtered.append(item)

    # Écriture du sous-corpus — chemin dérivé du chemin source DÉJÀ confiné
    # (en HTTP, corpus_path est sous OUTPUT_DIR ; with_name reste dans le même
    # dossier). Re-confinement en défense en profondeur.
    output_path = corpus_path.with_name(
        corpus_path.stem + suffix + corpus_path.suffix
    )
    if TRANSPORT_MODE == "http":
        output_path = resolve_in_output_dir(output_path, OUTPUT_DIR)
    filters = {
        "year_min": year_min,
        "year_max": year_max,
        "item_types": sorted(item_types),
        "sources": sorted(sources_filter),
        "language": language or None,
        "title_contains": title_contains or None,
    }
    if language:
        filters["keep_unknown_language"] = keep_unknown_language
    # Publication atomique et manifeste (OPT-16). Le nom reste dérivé du
    # corpus source et du suffixe (contrat de l'outil) : un nouveau filtrage
    # de même suffixe remplace le précédent, atomiquement.
    stem = corpus_stem_of(output_path.name) or output_path.stem
    source_doc = load_manifest_for(corpus_path)
    records = inherited_records(source_doc, filtered, by="id")
    with Publication(output_path.parent, stem, KIND_FILTER,
                     started_at=started_at,
                     path_roots=_publication_roots(corpus_path)) as pub:
        pub.add_json(output_path, filtered, role=ROLE_ZOTERO)
        manifest_path = pub.commit({
            "query": (source_doc or {}).get("query"),
            "filters": filters,
            "inputs": [_input_description(corpus_path, items, raw)],
            "records": records,
        })

    retention = round(100 * len(filtered) / max(len(items), 1), 1)
    unknown_line = ""
    if language and unknown_language["kept"]:
        unknown_line = (f"Notices sans langue lisible gardées : "
                        f"{unknown_language['kept']} (keep_unknown_language: "
                        f"false pour les écarter)\n")
    elif language and unknown_language["removed"]:
        unknown_line = (f"Notices sans langue lisible écartées : "
                        f"{unknown_language['removed']}\n")
    summary = (
        f"Filtrage appliqué.\n"
        f"Corpus source : {corpus_path.name} ({len(items)} items)\n"
        f"Sous-corpus : {output_path.name} ({len(filtered)} items)\n"
        f"{unknown_line}"
        f"Taux de rétention : {retention:.1f} %\n"
        f"Chemin complet : {output_path}\n"
        f"Manifeste : {manifest_path.name}"
    )
    structured = {
        "source_corpus": str(corpus_path),
        "source_count": len(items),
        "filtered_count": len(filtered),
        "retention_pct": retention,
        "filters": filters,
        "manifest": manifest_path.name,
    }
    if language:
        structured["unknown_language"] = dict(unknown_language)
    return _build_corpus_tool_result(
        summary_text=summary,
        primary_items=filtered,
        primary_path=output_path,
        primary_mime="application/json",
        extra_files=[manifest_path],
        structured=structured,
        inline=inline, top=top,
        digest=DigestSpec(items=filtered, records=records,
                          query=_manifest_query_text(source_doc),
                          corpus_path=output_path,
                          source_status=(source_doc or {}).get("sources")),
    )


async def _tool_corpus_resolve_dois(
    args: dict[str, Any],
) -> str | tuple[list, dict]:
    """
    Implémentation de l'outil corpus_resolve_dois.

    Complète les DOI manquants par appariement bibliographique CrossRef. La
    résolution est synchrone et bloquante (requêtes HTTP en parallèle sur un
    pool de 3, sous la politesse CrossRef) : elle est exécutée dans un thread
    pour ne pas figer la boucle d'événements du serveur pendant la durée de
    l'opération.

    OPT-10 : client HTTP partagé et cache mémoire partagé des requêtes
    transmis au résolveur ; paramètres bornés (aucun seuil abaissable, refus
    explicite) ; budget en temps ; rapport complet écrit dans
    ``<stem>.doi_report.json`` à côté du corpus produit (sans chemin disque)
    et exposé comme fichier associé ; le résumé ne tronque plus les rejets
    sans le dire.

    OPT-07 : lecture, résolution et écriture dans un fil de travail ; en
    HTTP, même concurrence bornée et même échéance que ``moisson_search``
    (le budget ``max_seconds`` est ramené sous l'échéance serveur pour que
    la résolution s'arrête d'elle-même et publie ses « non traité ») ;
    l'annulation (client ou échéance) est branchée sur ``stop_event`` du
    résolveur et interrompt ses attentes réseau ; rien n'est publié après
    elle. Refus de paramètre et fichier introuvable : erreurs d'outil.
    """
    cancel_event = threading.Event()
    return await _run_guarded(
        lambda deadline: functools.partial(_corpus_resolve_dois_sync, args,
                                           cancel_event, deadline),
        cancel_event=cancel_event, label="Résolution des DOI",
        deadline_note="aucun corpus partiel publié")


# Marge réservée à l'écriture des fichiers entre la fin du budget de
# résolution et l'échéance serveur (HTTP).
_DOI_PUBLISH_MARGIN_S = 10.0


def _corpus_resolve_dois_sync(args: dict[str, Any], cancel_event=None,
                              deadline: float | None = None,
                              ) -> str | tuple[list, dict]:
    """Corps synchrone de corpus_resolve_dois."""
    corpus_path = _safe_path(args["corpus_path"])  # confiné en HTTP
    if not corpus_path.exists():
        return ToolError(f"Fichier introuvable : {corpus_path.name}")

    # Format Zotero interne exigé (OPT-16) : refus clair d'un CSL-JSON, d'un
    # manifeste ou d'un rapport DOI.
    loaded = _load_zotero_input(corpus_path, "corpus_resolve_dois")
    if isinstance(loaded, ToolError):
        return loaded
    items, raw = loaded
    if not isinstance(items, list):
        return ToolError("Le fichier ne contient pas un tableau d'items Zotero.")
    started_at = now_iso()
    parsed_inline = _inline_args(args)      # retour (OPT-17)
    if isinstance(parsed_inline, ToolError):
        return parsed_inline
    inline, top = parsed_inline

    from config import (
        DOI_TITLE_THRESHOLD, DOI_TITLE_THRESHOLD_UNCORROBORATED,
        DOI_YEAR_TOLERANCE, CROSSREF_RESOLVE_ROWS, DOI_RESOLVE_WORKERS,
        DOI_RESOLVE_MAX_SECONDS, DOI_RESOLVE_MAX_SECONDS_CEILING,
    )
    from utils.doi_resolver import (
        DoiRuleError, build_report_document, check_rule_parameters,
        item_doi, resolve_dois, shared_query_cache,
    )
    import math
    from utils.http import get_default_client
    from utils.output_files import DOI_REPORT_SUFFIX

    def _arg(name: str, default):
        value = args.get(name)
        return default if value is None else value

    # Paramètres bornés : un seuil se durcit, il ne s'abaisse jamais.
    try:
        threshold, strict, tolerance, rows = check_rule_parameters(
            _arg("title_threshold", DOI_TITLE_THRESHOLD),
            _arg("strict_title_threshold", DOI_TITLE_THRESHOLD_UNCORROBORATED),
            _arg("year_tolerance", DOI_YEAR_TOLERANCE),
            CROSSREF_RESOLVE_ROWS,
        )
        max_items = args.get("max_items")
        if max_items is not None:
            if (isinstance(max_items, bool)
                    or not isinstance(max_items, (int, float))
                    or not math.isfinite(max_items)
                    or max_items != int(max_items) or max_items < 1):
                raise DoiRuleError(
                    f"max_items = {max_items!r} invalide : entier >= 1 attendu."
                )
            max_items = int(max_items)
        max_seconds = _arg("max_seconds", DOI_RESOLVE_MAX_SECONDS)
        if (isinstance(max_seconds, bool)
                or not isinstance(max_seconds, (int, float))
                or not math.isfinite(max_seconds)
                or not max_seconds > 0):
            raise DoiRuleError(
                f"max_seconds = {max_seconds!r} invalide : nombre > 0 attendu."
            )
        if max_seconds > DOI_RESOLVE_MAX_SECONDS_CEILING:
            raise DoiRuleError(
                f"max_seconds = {max_seconds:g} refusé : au plus "
                f"{DOI_RESOLVE_MAX_SECONDS_CEILING:g} s par appel ; relancer "
                f"l'outil sur le corpus produit pour poursuivre."
            )
        max_seconds = float(max_seconds)
    except DoiRuleError as e:
        return ToolError(f"Paramètre refusé : {e}")

    suffix = args.get("output_suffix", "_doi")
    if TRANSPORT_MODE == "http":
        suffix = validate_suffix(suffix)
    also_csl = args.get("also_csl", True)

    if deadline is not None:
        # Budget ramené sous l'échéance serveur : la résolution s'arrête
        # d'elle-même (notices « non traité ») et le corpus est publié.
        room = deadline - time.monotonic() - _DOI_PUBLISH_MARGIN_S
        max_seconds = max(1.0, min(max_seconds, room))
    stop = _cancel_signal(cancel_event, deadline)
    stop_kwargs = {"stop_event": stop} if stop is not None else {}

    try:
        enriched, report = resolve_dois(
            items,
            threshold=threshold,
            strict_threshold=strict,
            year_tolerance=tolerance,
            rows=rows,
            max_items=max_items,
            http_client=get_default_client(),
            workers=DOI_RESOLVE_WORKERS,
            max_seconds=max_seconds,
            query_cache=shared_query_cache(),
            **stop_kwargs,
        )
    except DoiRuleError as e:  # ex. valeur d'environnement sous un plancher
        return ToolError(f"Paramètre refusé : {e}")
    if stop is not None and stop.is_set():
        # Annulation (client ou échéance serveur) : aucune publication.
        raise OperationCancelled(stop.reason())

    output_path = corpus_path.with_name(
        corpus_path.stem + suffix + corpus_path.suffix
    )
    if TRANSPORT_MODE == "http":
        output_path = resolve_in_output_dir(output_path, OUTPUT_DIR)

    extra_files = []
    csl_path = None
    if also_csl:
        csl_path = output_path.parent / (output_path.stem + ".csl.json")
        if TRANSPORT_MODE == "http":
            csl_path = resolve_in_output_dir(csl_path, OUTPUT_DIR)

    # Rapport complet, fichier associé au corpus produit : noms de fichiers
    # seulement, aucun chemin disque (téléchargeable en HTTP).
    report_path = output_path.parent / (output_path.stem + DOI_REPORT_SUFFIX)
    if TRANSPORT_MODE == "http":
        report_path = resolve_in_output_dir(report_path, OUTPUT_DIR)
    report_doc = build_report_document(
        report, source_corpus=corpus_path.name, output_corpus=output_path.name,
    )
    report_doc["csl_corpus"] = csl_path.name if csl_path else None

    # Publication atomique (OPT-16) : corpus enrichi, CSL-JSON, rapport DOI,
    # puis manifeste en dernier (référence au rapport, règles, résumé).
    stem = corpus_stem_of(output_path.name) or output_path.stem
    source_doc = load_manifest_for(corpus_path)
    records = inherited_records(source_doc, enriched, by="position")
    with Publication(output_path.parent, stem, KIND_DOI,
                     started_at=started_at,
                     path_roots=_publication_roots(corpus_path)) as pub:
        pub.add_json(output_path, enriched, role=ROLE_ZOTERO)
        if csl_path is not None:
            pub.add_json(csl_path, export_corpus_to_csl(enriched), role=ROLE_CSL)
            extra_files.append(csl_path)
        pub.add_json(report_path, report_doc, role=ROLE_DOI_REPORT)
        extra_files.append(report_path)
        manifest_path = pub.commit({
            "query": (source_doc or {}).get("query"),
            "inputs": [_input_description(corpus_path, items, raw)],
            "doi_report": report_path.name,
            "doi_resolution": {
                k: report.get(k) for k in (
                    "rules_version", "total", "already_had_doi", "attempted",
                    "resolved", "ambiguous", "rejected", "no_candidate",
                    "errors", "not_processed", "stop_reason",
                    "resolution_rate_pct")
            },
            "settings": {
                "title_threshold": threshold,
                "strict_title_threshold": strict,
                "year_tolerance": tolerance,
                "rows": rows,
                "max_items": max_items,
                "max_seconds": max_seconds,
            },
            "records": records,
        })
    extra_files.append(manifest_path)

    with_doi = sum(1 for it in enriched if item_doi(it))
    lines = [
        f"Résolution de DOI par appariement CrossRef "
        f"(règles {report['rules_version']}).",
        f"Corpus source : {corpus_path.name} ({report['total']} items)",
        f"Déjà pourvus d'un DOI : {report['already_had_doi']}"
        + (f" (dont {report['non_canonical_doi']} DOI non canonique, "
           f"conservé tel quel, à vérifier)"
           if report.get("non_canonical_doi") else ""),
        f"Interrogés : {report['attempted']} (requêtes CrossRef : "
        f"{report['queries_sent']}, réponses en cache mémoire : "
        f"{report['query_cache_hits']})",
        f"Acceptés : {report['resolved']} "
        f"({report['resolution_rate_pct']} % des interrogés)",
        f"Ambigus (plusieurs travaux admissibles, aucun DOI écrit) : "
        f"{report['ambiguous']}",
        f"Rejetés (aucun candidat admissible) : {report['rejected']}",
        f"Sans candidat CrossRef : {report['no_candidate']}",
        f"Erreurs réseau ou de service : {report['errors']}",
        f"Non traités : {report['not_processed']}"
        + (f" ({report['stop_reason']})" if report["not_processed"] else ""),
    ]
    if report["skipped_no_title"]:
        lines.append(f"Sans titre : {report['skipped_no_title']}")
    lines += [
        f"Seuils : titre >= {threshold:.2f}, année à {tolerance} près, premier "
        f"auteur ; sans corroboration, titre >= {strict:.2f}",
        f"Durée : {report['duration_s']} s",
        f"Corpus enrichi : {output_path.name} — {with_doi} items avec DOI",
    ]
    if csl_path:
        lines.append(f"Version CSL-JSON : {csl_path.name}")
    lines.append(
        f"Rapport complet (chaque notice, tous les candidats et leurs "
        f"scores) : {report_path.name}"
    )
    lines.append(f"Manifeste : {manifest_path.name}")

    def _section(title: str, rows_: list, fmt) -> None:
        if not rows_:
            return
        shown = rows_[:10]
        lines.append("")
        if len(rows_) > len(shown):
            lines.append(f"{title} ({len(shown)} premiers sur {len(rows_)} ; "
                         f"liste complète dans {report_path.name}) :")
        else:
            lines.append(f"{title} ({len(rows_)}) :")
        lines.extend(fmt(row) for row in shown)

    _section("Ambiguïtés, à trancher à la main", report["ambiguities"],
             lambda r: f"  - {r[0][:80]} — {r[2]}")
    _section("Rejets, à vérifier à la main si une référence attendue manque",
             report["rejections"], lambda r: f"  - {r[0][:80]} — {r[2]}")
    _section("Erreurs (à relancer)", report["error_list"],
             lambda r: f"  - {r[0][:80]} — {r[1]}")
    summary = "\n".join(lines)

    legacy_keys = (
        "total", "already_had_doi", "attempted", "resolved", "rejected",
        "no_candidate", "skipped_no_title", "resolution_rate_pct",
        "ambiguous", "errors", "not_processed", "queries_sent",
        "query_cache_hits", "duration_s", "stopped_early", "stop_reason",
        "status_counts", "rules_version", "non_canonical_doi",
    )
    structured = {
        "source_corpus": str(corpus_path),
        "output_corpus": str(output_path),
        "csl_corpus": str(csl_path) if csl_path else None,
        "doi_report": report_path.name,
        "manifest": manifest_path.name,
        "title_threshold": threshold,
        "strict_title_threshold": strict,
        "year_tolerance": tolerance,
        "rows": rows,
        "max_items": max_items,
        "max_seconds": max_seconds,
        **{k: report[k] for k in legacy_keys if k in report},
        "items_with_doi": with_doi,
    }
    return _build_corpus_tool_result(
        summary_text=summary,
        primary_items=enriched,
        primary_path=output_path,
        primary_mime="application/json",
        extra_files=extra_files,
        structured=structured,
        inline=inline, top=top,
        digest=DigestSpec(items=enriched, records=records,
                          query=_manifest_query_text(source_doc),
                          corpus_path=output_path,
                          source_status=(source_doc or {}).get("sources")),
    )


async def _tool_corpus_to_csl(args: dict[str, Any]) -> str | tuple[list, dict]:
    """
    Implémentation de l'outil corpus_to_csl.

    Convertit un corpus Zotero JSON interne en CSL-JSON. Le produit PRIMAIRE
    (donc le JSON inliné) est ici le CSL-JSON, conformément au cadrage.
    Lecture, conversion et écriture hors de la boucle asyncio (OPT-07, C17).
    """
    return await asyncio.to_thread(_corpus_to_csl_sync, args)


def _corpus_to_csl_sync(args: dict[str, Any]) -> str | tuple[list, dict]:
    """Corps synchrone de corpus_to_csl."""
    corpus_path = _safe_path(args["corpus_path"])  # confiné en HTTP
    if not corpus_path.exists():
        return ToolError(f"Fichier introuvable : {corpus_path.name}")

    # Format Zotero interne exigé (OPT-16) : reconvertir un CSL-JSON
    # produirait des notices vides.
    loaded = _load_zotero_input(corpus_path, "corpus_to_csl")
    if isinstance(loaded, ToolError):
        return loaded
    items, raw = loaded

    if not isinstance(items, list):
        return ToolError("Le fichier ne contient pas un tableau d'items Zotero.")
    started_at = now_iso()
    parsed_inline = _inline_args(args)      # retour (OPT-17)
    if isinstance(parsed_inline, ToolError):
        return parsed_inline
    inline, top = parsed_inline

    output_path = args.get("output_path")
    if output_path:
        # Chemin de sortie fourni par le client → confiné en HTTP.
        output = _safe_path(output_path)
    else:
        # Ajouter .csl avant .json : corpus_X.json → corpus_X.csl.json
        # Dérivé du chemin source DÉJÀ confiné.
        output = corpus_path.parent / (corpus_path.stem + ".csl.json")

    csl_items = export_corpus_to_csl(items)

    # Publication atomique (OPT-16). Sortie de même radical que la source,
    # dans le même dossier (cas par défaut) : AJOUT au corpus existant, dont
    # le manifeste est complété sous verrou (un corpus ancien sans manifeste
    # en reçoit un qui décrit aussi son fichier Zotero). Sinon, corpus dérivé
    # avec son propre manifeste.
    source_stem = corpus_stem_of(corpus_path.name)
    out_stem = corpus_stem_of(output.name) or output.stem
    try:
        same_dir = output.parent.resolve() == corpus_path.parent.resolve()
    except OSError:
        same_dir = False
    append = same_dir and out_stem == source_stem
    source_doc = load_manifest_for(corpus_path)
    inputs = [_input_description(corpus_path, items, raw)]
    # Rangs des notices (manifeste d'un corpus dérivé ; digest, OPT-17) :
    # ceux du manifeste source.
    records = (inherited_records(source_doc, items, by="position")
               if not append or inline == INLINE_DIGEST else None)
    with Publication(output.parent, out_stem, KIND_CSL, started_at=started_at,
                     append=append,
                     path_roots=_publication_roots(corpus_path)) as pub:
        pub.add_json(output, csl_items, role=ROLE_CSL)
        fields = {"inputs": inputs, "format": "csl-json"}
        if append and source_doc is None:
            pub.describe_existing(corpus_path, role=ROLE_ZOTERO, items=len(items))
            fields.update({"primary": corpus_path.name, "item_count": len(items),
                           "records": simple_records(items),
                           "notes": ["corpus antérieur au manifeste : fichier "
                                     "Zotero décrit lors de la conversion CSL"]})
        elif not append:
            fields.update({
                "query": (source_doc or {}).get("query"),
                "records": records,
            })
        manifest_path = pub.commit(fields)

    summary = (
        f"Conversion CSL-JSON réussie.\n"
        f"Source : {corpus_path.name} ({len(items)} items Zotero)\n"
        f"Sortie : {output.name} ({len(csl_items)} items CSL)\n"
        f"Chemin complet : {output}\n"
        f"Manifeste : {manifest_path.name}\n"
        f"\n"
        f"Importable dans Zotero Desktop via Fichier > Importer."
    )
    structured = {
        "source_corpus": str(corpus_path),
        "source_count": len(items),
        "csl_count": len(csl_items),
        "format": "csl-json",
        "manifest": manifest_path.name,
    }
    # Produit primaire = CSL-JSON : c'est lui qu'on inline (mode full). Le
    # digest décrit les notices Zotero source (même ordre, mêmes notices),
    # que corpus_query interroge.
    return _build_corpus_tool_result(
        summary_text=summary,
        primary_items=csl_items,
        primary_path=output,
        primary_mime="application/json",
        extra_files=[manifest_path],
        structured=structured,
        inline=inline, top=top,
        digest=DigestSpec(items=items, records=records,
                          query=_manifest_query_text(source_doc),
                          corpus_path=corpus_path,
                          source_status=(source_doc or {}).get("sources")),
    )


async def _tool_corpus_export(args: dict[str, Any]) -> str | tuple[list, dict]:
    """
    Implémentation de l'outil corpus_export (OPT-18, tâche #20 du sprint 03).

    Corpus Zotero interne → fichier BibTeX, BibLaTeX ou RIS
    (``mappers.bibtex_exporter``, ``mappers.ris_exporter``). Lecture, rendu et
    écriture hors de la boucle asyncio (OPT-07, C17).
    """
    return await asyncio.to_thread(_corpus_export_sync, args)


def _corpus_export_sync(args: dict[str, Any]) -> str | tuple[list, dict]:
    """
    Corps synchrone de corpus_export.

    Chemins : entrée et ``output_path`` résolus par ``_safe_path`` (confinés
    sous OUTPUT_DIR en HTTP, libres en stdio comme pour corpus_to_csl). En
    HTTP, la sortie doit être un nom plat de OUTPUT_DIR, d'extension exacte
    (``.bib``, ``.ris``) : ``/download`` ne sert rien d'autre, et l'outil
    n'annonce jamais un lien que ``/download`` refuserait. Sans
    ``output_path`` : nom unique ``<radical>_<uuid8>.<ext>`` à côté du corpus
    (dans OUTPUT_DIR en HTTP). Entrée CSL-JSON, fichier associé ou contenu
    qui n'est pas un tableau de notices Zotero : refus clair (isError).

    Retour via ``_build_corpus_tool_result`` : lien vers le fichier ; en
    HTTP, URL de téléchargement seule (ni chemin, ni contenu inliné) ; en
    stdio, le contenu inliné est le texte exporté lui-même
    (``_retarget_export_inline``).

    Publication atomique tracée (OPT-16, ``publish_export``) : l'export est
    ajouté au manifeste du corpus source (rôle ``export``) quand il est écrit
    dans son dossier ; ``structuredContent.manifest`` en donne le nom.
    """
    from mappers.bibtex_exporter import (
        EXPORT_FORMATS, EXPORT_LABELS, EXPORT_MIME, ExportInputError,
        check_export_output, export_corpus_file,
    )

    fmt = str(args.get("format") or "bibtex").strip().lower()
    if fmt not in EXPORT_FORMATS:
        return ToolError(
            f"Format inconnu : {fmt!r} (attendu : bibtex, biblatex ou ris).")
    parsed_inline = _inline_args(args)      # retour (OPT-17)
    if isinstance(parsed_inline, ToolError):
        return parsed_inline
    inline, top = parsed_inline
    http = TRANSPORT_MODE == "http"
    corpus_path = _safe_path(args["corpus_path"])  # confiné en HTTP
    if not corpus_path.exists():
        return ToolError(f"Fichier introuvable : {corpus_path.name}")

    output = None
    raw_output = args.get("output_path")
    try:
        if raw_output:
            output = _safe_path(raw_output)  # confiné en HTTP
            check_export_output(output, fmt, strict_suffix=http)
            if http and output.parent != OUTPUT_DIR.resolve():
                return ToolError(
                    "En mode distant, output_path doit désigner un fichier "
                    "du répertoire des corpus (nom simple, sans sous-dossier).")
    except ExportInputError as e:
        return ToolError(f"Erreur : {e}")
    if http:
        # Contrat HTTP (Sprint 08) : le fichier doit être servi par /download.
        probe = output.name if output is not None else \
            f"export{EXPORT_FORMATS[fmt]}"
        if not is_downloadable_file(probe):
            return ToolError(
                f"Export {EXPORT_LABELS[fmt]} indisponible en mode distant : "
                f"les fichiers {EXPORT_FORMATS[fmt]} ne sont pas encore servis "
                f"par le téléchargement (classification utils.output_files).")

    ascii_ = bool(args.get("ascii", False)) and fmt != "ris"
    provenance = bool(args.get("include_provenance", False))
    directory = OUTPUT_DIR if http else corpus_path.parent
    try:
        result = export_corpus_file(
            corpus_path, fmt, output, output_dir=directory, ascii=ascii_,
            include_provenance=provenance, strict_suffix=http,
            path_roots=_publication_roots(corpus_path))
    except ExportInputError as e:
        return ToolError(f"Erreur : {e}")

    n = result.entries
    st = result.stats
    label = EXPORT_LABELS[fmt]
    lines = [
        f"Export {label} réussi.",
        f"Source : {corpus_path.name} ({n} notice(s) Zotero)",
        f"Sortie : {result.path.name} ({n} entrée(s), "
        f"{result.size_bytes / 1024:.1f} ko"
        f"{', ASCII (commandes LaTeX)' if ascii_ else ''})",
        f"Chemin complet : {result.path}",
    ]
    if result.manifest_path is not None:
        lines.append(f"Manifeste : {result.manifest_path.name}")
    if fmt != "ris":
        lines.append(
            f"Clés de citation stables (nom + année + premier mot du titre) ; "
            f"{st.get('disambiguated_keys', 0)} désambiguïsée(s) par a, b, c.")
    if st.get("annotes"):
        lines.append(f"Annotations exportées (ligne Annote:) : {st['annotes']}.")
    if st.get("snippet_abstracts_skipped"):
        lines.append(
            f"Extraits Google Scholar non exportés comme résumés : "
            f"{st['snippet_abstracts_skipped']} notice(s).")
    warned = st.get("scholar_truncation_warnings", 0)
    incomplete = st.get("incomplete_author_lists", 0)
    if warned:
        # Invariant 6 : mise en garde de fiabilité, exportée par défaut.
        line = (
            f"Notices dont Google Scholar a tronqué les auteurs et/ou la "
            f"revue : {warned}, avertissement écrit dans "
            f"{'N1' if fmt == 'ris' else 'note'} (à vérifier avant citation)")
        if incomplete:
            line += (f" ; {incomplete} liste(s) d'auteurs close(s) par "
                     f"« and others »")
        lines.append(line + ".")
    lines.append(
        "Provenance de moisson exportée (lignes du champ extra, "
        "include_provenance) ; rangs et scores restent dans le manifeste."
        if provenance
        else "Aucune métadonnée de moisson exportée (sources, identifiants "
             "natifs, citations, rangs, scores).")
    lines.append("")
    lines.append(
        "Importable dans Zotero Desktop via Fichier > Importer"
        + ("." if fmt == "ris"
           else " ; utilisable avec BibTeX ou biber (BibLaTeX)."))
    structured = {
        "source_corpus": str(corpus_path),
        "source_count": n,
        "export_count": n,
        "format": fmt,
        "media_type": EXPORT_MIME[fmt],
        "size_bytes": result.size_bytes,
        "ascii": ascii_,
        "include_provenance": provenance,
        "disambiguated_keys": st.get("disambiguated_keys", 0),
        "annotes": st.get("annotes", 0),
        "snippet_abstracts_skipped": st.get("snippet_abstracts_skipped", 0),
        "scholar_truncation_warnings": warned,
    }
    if fmt != "ris":
        structured["incomplete_author_lists"] = incomplete
    if result.manifest_path is not None:
        # Nom de base seulement (contrat HTTP) : manifeste du corpus source
        # complété, ou manifeste propre d'un export écrit ailleurs.
        structured["manifest"] = result.manifest_path.name
    summary_text = "\n".join(lines)
    digest = None
    if inline == INLINE_DIGEST and not http:
        # Digest (OPT-17) : notices exportées dans l'ordre du classement du
        # corpus source, avec leur clé de citation (BibTeX, BibLaTeX).
        from mappers.bibtex_exporter import make_citation_keys
        source_doc = load_manifest_for(corpus_path)
        # Rangs du manifeste seulement s'il décrit le fichier lu (taille et
        # empreinte : republication concurrente, PAT-10).
        from utils.manifest import manifest_describes
        digest = DigestSpec(
            items=result.items,
            records=((source_doc or {}).get("records")
                     if manifest_describes(source_doc, corpus_path) else None),
            query=_manifest_query_text(source_doc),
            corpus_path=corpus_path,
            source_status=(source_doc or {}).get("sources"),
            keys=(make_citation_keys(result.items) if fmt != "ris" else None))
    blocks, structured = _build_corpus_tool_result(
        summary_text=summary_text,
        primary_items=result.items,
        primary_path=result.path,
        primary_mime=EXPORT_MIME[fmt],
        extra_files=[],
        structured=structured,
        inline=inline, top=top, digest=digest, reserve_bytes=500,
    )
    if not http:
        blocks, structured = _retarget_export_inline(
            blocks, structured, result, EXPORT_MIME[fmt], summary_text,
            mode=inline)
        if digest is not None:
            # Plafond et taille du digest remesurés après la note propre à
            # l'export (OPT-17 ; place réservée par reserve_bytes).
            _fit_digest(blocks, structured, CQ.DIGEST_MAX_BYTES)
    return blocks, structured


def _retarget_export_inline(blocks: list, structured: dict, result,
                            mime: str, summary_text: str | None = None,
                            mode: str = INLINE_FULL,
                            ) -> tuple[list, dict]:
    """
    Stdio : retour propre à un export, indépendant des décisions que le
    helper commun prend sur le JSON des notices.

    - Contenu inliné : le texte BibTeX ou RIS lui-même, seul contenu
      cohérent avec l'URI ``corpus://<export>`` ; jamais le JSON des
      notices. La décision porte sur l'export : au plus INLINE_MAX_ITEMS
      notices (garde anti-saturation du sprint 05) et au plus
      INLINE_MAX_BYTES octets UTF-8 de texte exporté.
    - Lien : ``ResourceLink`` au type MIME du format tant que
      ``read_resource`` peut rendre le fichier (au plus RESOURCE_MAX_BYTES
      octets). Au-delà, ``read_resource`` refuse un export (sa lecture
      tronquée ne vaut que pour un tableau JSON) : aucun lien n'est promis,
      le fichier s'ouvre depuis son chemin (« Chemin complet », ``files``).
    - Texte : la note « JSON non inliné » du helper est remplacée par une
      note propre à l'export.
    - ``structuredContent`` : ``inline_json`` toujours faux (le bloc inliné
      n'est jamais du JSON) ; ``inline_export`` et ``inline_format`` +
      ``inline_bytes``, ou ``inline_skipped_reason``, décrivent l'export ;
      ``resources`` ne liste que des liens lisibles.
    - ``mode`` (OPT-17) : hors ``full`` (``digest``, ``none``), le texte
      exporté n'est jamais inliné (motif ``inline='digest'`` ou
      ``inline='none'``) ; le digest éventuel des notices reste en place.
    """
    name = result.path.name
    uri = corpus_uri(name)
    n = result.entries
    size = result.size_bytes
    readable = size <= RESOURCE_MAX_BYTES
    if mode != INLINE_FULL:
        reason = f"inline='{mode}'"
    elif n > INLINE_MAX_ITEMS:
        reason = f"{n} notices > seuil {INLINE_MAX_ITEMS}"
    elif size > INLINE_MAX_BYTES:
        reason = f"{size} octets > plafond {INLINE_MAX_BYTES} octets"
    else:
        reason = None

    def _names_export(value) -> bool:
        try:
            return corpus_name_from_uri(value) == name
        except ValueError:
            return False

    out = []
    for block in blocks:
        if isinstance(block, types.ResourceLink) and _names_export(block.uri):
            if not readable:
                continue            # lien que read_resource refuserait
            block = block.model_copy(update={"mimeType": mime})
        elif isinstance(block, types.EmbeddedResource) and \
                _names_export(block.resource.uri):
            continue                # JSON des notices : jamais pour un export
        out.append(block)
    if reason is None:
        out.append(types.EmbeddedResource(
            type="resource",
            resource=types.TextResourceContents(
                uri=uri, mimeType=mime, text=result.text),
        ))

    if out and isinstance(out[0], types.TextContent):
        text = out[0].text
        start = text.find("\n\n(JSON non inliné")
        if start >= 0:
            end = text.find(f"{uri})", start)
            if end >= 0:
                text = text[:start] + text[end + len(uri) + 1:]
            elif summary_text is not None and text.startswith(summary_text):
                text = summary_text
        if reason is not None and readable:
            text += (f"\n\n(Export non inliné : {reason}. Fichier complet via "
                     f"la resource {uri})")
        elif reason is not None:
            text += (f"\n\n(Export non inliné : {reason}. Fichier de {size} "
                     f"octets, au-delà du plafond de lecture des ressources "
                     f"({RESOURCE_MAX_BYTES} octets) : aucun lien corpus:// "
                     f"n'est émis ; ouvrir le fichier depuis son chemin "
                     f"complet ci-dessus.)")
        elif not readable:
            text += (f"\n\n(Export inliné ci-dessous ; aucun lien corpus:// : "
                     f"fichier au-delà du plafond de lecture des ressources "
                     f"({RESOURCE_MAX_BYTES} octets).)")
        out[0] = types.TextContent(type="text", text=text)

    structured["inline_json"] = False
    structured["inline_export"] = reason is None
    if reason is None:
        structured["inline_format"] = result.format
        structured["inline_bytes"] = size
        structured.pop("inline_skipped_reason", None)
    else:
        structured["inline_skipped_reason"] = reason
        structured.pop("inline_format", None)
        structured.pop("inline_bytes", None)
    if not readable:
        structured["resources"] = [
            r for r in structured.get("resources") or [] if r != uri]
        structured["resource_skipped_reason"] = (
            f"{size} octets > plafond de lecture {RESOURCE_MAX_BYTES} octets")
    return out, structured


# === Comparaison de corpus (OPT-19) ===

async def _tool_corpus_diff(args: dict[str, Any]) -> str | tuple[list, dict]:
    """
    Implémentation de l'outil corpus_diff (OPT-19, veille).

    Compare deux corpus Zotero internes par identifiants
    (``utils.corpus_diff``) et écrit le rapport complet
    ``<radical de B>.diff.json`` (fichier associé). Lecture, comparaison et
    écriture hors de la boucle asyncio (OPT-07, C17).
    """
    return await asyncio.to_thread(_corpus_diff_sync, args)


def _corpus_diff_sync(args: dict[str, Any]) -> str | tuple[list, dict]:
    """
    Corps synchrone de corpus_diff.

    Chemins : entrées et ``output_path`` résolus par ``_safe_path`` (confinés
    sous OUTPUT_DIR en HTTP, refus sans écho du chemin reçu ; libres en
    stdio, où un nom nu absent du répertoire courant est cherché dans
    OUTPUT_DIR, voir ``_diff_input_path``). Rapport par défaut à côté de B
    en stdio, dans OUTPUT_DIR en HTTP (nom plat : ``/download`` ne sert rien
    d'autre), jamais remplacé par une autre comparaison :
    ``<radical de B>.diff.json`` s'il est libre, sinon
    ``<radical de B>_<uuid8>.diff.json`` ; ``output_path`` donne un nom
    stable, remplacé. ``structuredContent.report`` et ``created_at``
    désignent le rapport écrit par cet appel. Entrée qui n'est pas un corpus
    Zotero interne : refus clair (isError), rien d'écrit.

    Retour : résumé texte et ``structuredContent`` (contexte des deux corpus,
    compteurs, avertissements, échantillons bornés d'identifiants et de
    titres). Le rapport est un fichier associé : en stdio, son chemin dans
    ``files`` (pas de lien ``corpus://`` : ``read_resource`` ne lit pas les
    fichiers associés) ; en HTTP, son URL de téléchargement seule, aucun
    chemin disque, aucun contenu inliné, et des échantillons réduits aux
    identifiants (aucun titre, ni dans le texte ni dans ``structuredContent``).
    """
    from utils.corpus_diff import (
        SAMPLE_DEFAULT, SAMPLE_MAX, CorpusDiffInputError, check_report_output,
        default_report_path, diff_corpus_files, structured_summary,
        summary_lines,
    )

    http = TRANSPORT_MODE == "http"
    raw_a, raw_b = args.get("corpus_a"), args.get("corpus_b")
    if not raw_a or not raw_b:
        return ToolError("Paramètres requis : corpus_a (référence) et "
                         "corpus_b (corpus comparé).")
    sample = args.get("sample", SAMPLE_DEFAULT)
    if sample is None:
        sample = SAMPLE_DEFAULT
    if isinstance(sample, bool) or not isinstance(sample, (int, float)) \
            or sample != int(sample) or not 0 <= sample <= SAMPLE_MAX:
        return ToolError(f"Paramètre refusé : sample = {sample!r} (entier de 0 "
                         f"à {SAMPLE_MAX} attendu).")
    sample = int(sample)
    fuzzy = args.get("fuzzy", False)
    if not isinstance(fuzzy, bool):
        return ToolError("Paramètre refusé : fuzzy doit être un booléen.")

    # Refus de confinement (HTTP) : message sans le chemin reçu.
    paths = {}
    for label, raw in (("corpus_a", raw_a), ("corpus_b", raw_b),
                       ("output_path", args.get("output_path"))):
        if label == "output_path" and not raw:
            continue
        try:
            paths[label] = (_diff_input_path(raw) if label != "output_path"
                            else _safe_path(raw))
        except PathSecurityError:
            return ToolError(f"Chemin refusé ({label}) : hors du répertoire "
                             f"des corpus du serveur.")
    path_a, path_b = paths["corpus_a"], paths["corpus_b"]
    for label, path in (("corpus_a", path_a), ("corpus_b", path_b)):
        if not path.is_file():
            return ToolError(f"Fichier introuvable ({label}) : {path.name}")

    raw_output = args.get("output_path")
    report_path = None
    # Dossier du rapport par défaut : à côté de B en stdio, OUTPUT_DIR en
    # HTTP (``/download`` ne sert que des noms plats de OUTPUT_DIR).
    report_dir = OUTPUT_DIR if http else None
    try:
        if raw_output:
            report_path = paths["output_path"]  # confiné en HTTP
            check_report_output(report_path)
            if http and report_path.parent != OUTPUT_DIR.resolve():
                return ToolError(
                    "En mode distant, output_path doit désigner un fichier "
                    "du répertoire des corpus (nom simple, sans sous-dossier).")
        # Sans output_path, un rapport n'est jamais remplacé par une autre
        # comparaison : <radical de B>.diff.json s'il est libre, sinon nom
        # unique <radical de B>_<uuid8>.diff.json (le lien rendu sert
        # toujours le rapport de cet appel).
        result = diff_corpus_files(
            path_a, path_b, fuzzy=fuzzy, report_path=report_path,
            report_dir=report_dir,
            path_roots=_publication_roots(path_a, path_b))
    except CorpusDiffInputError as e:
        return ToolError(str(e))
    except OSError as e:
        # Écriture du rapport impossible (dossier en lecture seule…) :
        # message sans chemin disque.
        target = (report_path if report_path is not None
                  else default_report_path(path_b, report_dir))
        return ToolError(f"Écriture du rapport impossible : "
                         f"{target.name} ({type(e).__name__}).")

    report = result.report
    name = result.report_path.name
    # Plafond en octets du retour (même contrat que le digest et
    # corpus_query, OPT-17) : échantillons par défaut sous DIGEST_MAX_BYTES,
    # comme un digest ; échantillons demandés explicitement (``sample``)
    # sous QUERY_MAX_BYTES, comme une page de corpus_query. Au-delà, les
    # échantillons sont réduits ; le rapport garde les listes complètes.
    cap = CQ.QUERY_MAX_BYTES if args.get("sample") is not None \
        else CQ.DIGEST_MAX_BYTES

    def build(n: int) -> tuple[list, dict]:
        # En HTTP (contrat du Sprint 08, invariant 3) : aucune donnée
        # bibliographique inline, ni titre dans le texte ni dans les
        # échantillons ; identifiants, compteurs et URL du rapport complet.
        lines = summary_lines(report, n, titles=not http)
        lines.append("")
        if n < sample:
            lines.append(f"Échantillons ramenés à {n} par catégorie (plafond "
                         f"de {cap} octets du retour) ; listes complètes "
                         f"dans le rapport.")
        lines.append(f"Rapport complet (liste de tous les travaux) : {name}")
        structured = structured_summary(report, n, titles=not http)
        structured["byte_cap"] = {"max_bytes": cap, "reduced": n < sample,
                                  "sample_requested": sample}
        if http:
            url = _download_url(quote(name, safe=""))
            lines.append("Échantillons d'identifiants : "
                         "structuredContent.samples (titres dans le rapport).")
            lines.append(f"Rapport (URL de téléchargement) : {url}")
            structured = _scrub_paths_for_http(structured)
            structured["download_urls"] = {name: url}
            structured["transport"] = "http"
            text = _redact_paths_in_text("\n".join(lines))
        else:
            lines.append(f"Chemin complet : {result.report_path}")
            structured["files"] = {name: str(result.report_path)}
            text = "\n".join(lines)
        return [types.TextContent(type="text", text=text)], structured

    n = sample
    blocks, structured = build(n)
    size = _measure_result(blocks, structured)
    while n > 0 and size > cap:
        # Saut proportionnel, puis pas à pas : quelques reconstructions.
        n = max(0, min(n - 1, int(n * cap / size)))
        blocks, structured = build(n)
        size = _measure_result(blocks, structured)
    if size > cap:
        logger.warning(f"corpus_diff : retour de {size} octets sans "
                       f"échantillon, au-delà du plafond de {cap} octets")
    return blocks, structured


def _diff_input_path(raw) -> Path:
    """
    Chemin d'un corpus comparé par corpus_diff.

    HTTP : ``_safe_path`` (confiné sous OUTPUT_DIR ; un nom nu y est déjà
    cherché). Stdio : chemin tel quel ; à défaut, un **nom nu** (sans
    dossier) absent du répertoire courant est cherché dans OUTPUT_DIR, où
    ``corpus_list`` le trouve : le prompt veille-mensuelle enchaîne ainsi
    corpus_list et corpus_diff avec les mêmes noms dans les deux transports.
    """
    path = _safe_path(raw)
    if TRANSPORT_MODE == "http" or path.exists():
        return path
    text = str(raw)
    # Nom nu seulement, jamais un fichier de service (point initial).
    if text and Path(text).name == text and not text.startswith("."):
        candidate = OUTPUT_DIR / text
        if candidate.is_file():
            return candidate
    return path


# === Consultation locale : corpus_query (OPT-17) ===

async def _tool_corpus_query(args: dict[str, Any]) -> str | tuple[list, dict]:
    """
    Implémentation de l'outil corpus_query (OPT-17) : recherche locale
    classée, filtres, projection, pagination stable, notices complètes par
    identifiants (``utils.corpus_query.query_corpus``). Lecture en flux, hors
    de la boucle asyncio (fil de travail, OPT-07).
    """
    return await asyncio.to_thread(_corpus_query_sync, args)


def _query_reference(path: Path) -> str:
    """
    Argument ``corpus`` de corpus_query qui désigne ``path`` en stdio (appel
    conseillé par l'en-tête du digest), quel que soit le répertoire courant
    du processus : lancé par le client MCP local, ce n'est en général pas
    OUTPUT_DIR, et un nom nu y serait cherché en vain.

    - fichier placé directement dans OUTPUT_DIR : URI ``corpus://<nom>``,
      résolue par ``_resolve_resource_path`` sans le registre (elle survit
      donc à un redémarrage du serveur) ;
    - ailleurs (sortie de corpus_filter à côté de sa source, corpus source
      de corpus_to_csl ou corpus_export, ``output_path`` arbitraire) :
      chemin absolu. Le digest n'existe qu'en stdio (jamais de chemin en
      HTTP).
    """
    path = Path(path)
    try:
        resolved = path.resolve()
        candidate = OUTPUT_DIR / path.name
        if candidate.is_file() and candidate.resolve() == resolved:
            return corpus_uri(path.name)
    except (OSError, RuntimeError):
        pass
    return str(path.absolute())


def _query_corpus_path(raw: str):
    """
    Fichier interrogé par corpus_query : URI ``corpus://<nom>`` (résolue
    comme par ``read_resource`` : confinement, fichiers de service et
    associés refusés) ou chemin (``_safe_path`` : confiné en HTTP).

    Stdio : un **nom nu** (sans dossier) absent du répertoire courant est
    cherché dans OUTPUT_DIR, puis dans le registre des fichiers exposés
    (``_EMITTED_RESOURCES`` : sortie de corpus_filter à côté de sa source,
    ``output_path`` arbitraire), comme par ``corpus_diff`` : le nom que
    donnent corpus_list et les producteurs se résout donc dans les deux
    transports.
    """
    text = raw.strip()
    if text.startswith("corpus://"):
        try:
            name = corpus_name_from_uri(text)
        except ValueError as e:
            return ToolError(f"Paramètre refusé : corpus : URI invalide ({e}).")
        path = _resolve_resource_path(name, http=TRANSPORT_MODE == "http")
        if path is None:
            return ToolError(f"Fichier introuvable : {name}")
        return path
    path = _safe_path(text)
    if TRANSPORT_MODE == "http" or path.exists():
        return path
    # Nom nu seulement, jamais un fichier de service (point initial).
    if text and Path(text).name == text and not text.startswith("."):
        candidate = OUTPUT_DIR / text
        if candidate.is_file():
            return candidate
        registered = _EMITTED_RESOURCES.get(text)
        if registered is not None and registered.is_file():
            return registered
    return path


def _query_input_error(path: Path) -> ToolError | None:
    """Corpus Zotero interne exigé : refus clair (nom ou contenu) d'un
    CSL-JSON, d'un manifeste, d'un rapport DOI ou d'un fichier qui n'est pas
    un tableau JSON de notices (message sans chemin disque)."""
    try:
        check_zotero_corpus(path, tool="corpus_query")
    except CorpusFormatError as e:
        return ToolError(str(e))
    gen = iter_json_array(path)
    empty = object()
    try:
        first = next(gen, empty)
    except ValueError:
        # Objet JSON (manifeste, rapport DOI ou de comparaison renommé) :
        # message propre au format, lu par le contenu si le fichier est petit.
        try:
            doc = (json.loads(path.read_bytes())
                   if path.stat().st_size <= RESOURCE_MAX_BYTES else None)
        except (OSError, UnicodeDecodeError, ValueError):
            doc = None
        if isinstance(doc, dict):
            try:
                check_zotero_corpus(path, doc, tool="corpus_query", loaded=True)
            except CorpusFormatError as e:
                return ToolError(str(e))
        return ToolError(f"Corpus illisible : {path.name} n'est pas un tableau "
                         f"JSON de notices Zotero.")
    except (OSError, UnicodeDecodeError):
        return ToolError(f"Lecture impossible : {path.name}")
    finally:
        gen.close()
    if first is empty:
        return None
    try:
        check_zotero_corpus(path, [first], tool="corpus_query", loaded=True)
    except CorpusFormatError as e:
        return ToolError(str(e))
    if not isinstance(first, dict):
        # Tableau de nombres, de chaînes ou de null : même refus que les
        # autres outils (``_load_zotero_input``), au lieu d'un succès vide.
        # Les éléments suivants sont vérifiés pendant la lecture
        # (``CQ.NotNoticesError``).
        return ToolError(CQ.NOT_NOTICES_MESSAGE)
    return None


def _corpus_query_sync(args: dict[str, Any]) -> str | tuple[list, dict]:
    """
    Corps synchrone de corpus_query.

    Stdio : page de résultats (une ligne compacte par notice dans le texte,
    champs demandés dans ``structuredContent.items``) ou notices complètes
    par ``ids`` (JSON dans un second bloc de texte, métadonnées dans
    ``structuredContent``), sous ``QUERY_MAX_BYTES`` octets : les dernières
    notices sont retirées au besoin, sans trou de pagination
    (``next_offset`` suit le nombre de notices rendues).

    HTTP (contrat du sprint 08) : la sélection (notices complètes de la page
    ou des identifiants) est publiée comme corpus dérivé téléchargeable
    (``<radical>_sel_<uuid>.json`` et son manifeste) ; le retour ne donne
    que ses métadonnées et l'URL, jamais de JSON bibliographique ni de
    chemin.
    """
    raw = args.get("corpus")
    if not isinstance(raw, str) or not raw.strip():
        return ToolError("Paramètre refusé : corpus attendu (chemin, nom ou URI "
                         "corpus:// d'un corpus Zotero interne).")
    path = _query_corpus_path(raw)
    if isinstance(path, ToolError):
        return path
    if not path.is_file():
        return ToolError(f"Fichier introuvable : {path.name}")
    refused = _query_input_error(path)
    if refused is not None:
        return refused
    http = TRANSPORT_MODE == "http"
    try:
        result = CQ.query_corpus(
            path, query=args.get("query"), filters=args.get("filters"),
            fields=args.get("fields"), sort=args.get("sort"),
            offset=args.get("offset"), limit=args.get("limit"),
            ids=args.get("ids"), with_notices=http)
    except QueryError as e:
        return ToolError(f"Paramètre refusé : {e}")
    except CQ.NotNoticesError:
        return ToolError(CQ.NOT_NOTICES_MESSAGE)
    except CQ.CorpusChangedError:
        return ToolError(f"Corpus modifié pendant la lecture : {path.name} a "
                         f"changé à chaque tentative ; relancer corpus_query "
                         f"quand il ne change plus.")
    except (ValueError, UnicodeDecodeError) as e:
        return ToolError(f"Corpus illisible : {path.name} n'est pas un tableau "
                         f"JSON de notices Zotero ({e}).")
    if http:
        return _corpus_query_http(path, result)
    return _corpus_query_stdio(result)


def _corpus_query_stdio(result) -> tuple[list, dict]:
    """Retour stdio de corpus_query sous ``QUERY_MAX_BYTES`` octets."""
    structured = result.as_dict()
    if result.mode == "ids":
        notices = [dict(entry) for entry in result.items]
        structured["items"] = [{k: v for k, v in entry.items() if k != "notice"}
                               for entry in notices]
        structured["notices"] = "second bloc de texte (JSON)"

        def build(count: int) -> list:
            head = CQ.query_lines(result)
            if count < len(notices):
                head.append(f"Plafond de {CQ.QUERY_MAX_BYTES} octets : "
                            f"{len(notices) - count} notice(s) non rendue(s) "
                            f"(redemander avec moins d'identifiants).")
            return [types.TextContent(type="text", text="\n".join(head)),
                    types.TextContent(type="text", text=json.dumps(
                        notices[:count], ensure_ascii=False, indent=1))]

        count = len(notices)
        blocks = build(count)
        while count > 1 and _measure_result(blocks, structured) > CQ.QUERY_MAX_BYTES:
            count -= 1
            structured["items"] = structured["items"][:count]
            structured["not_returned_ids"] = [e["requested"] for e in notices[count:]]
            structured["found"] = count
            blocks = build(count)
        return blocks, structured

    lines = CQ.query_lines(result)
    blocks = [types.TextContent(type="text", text="\n".join(lines))]
    while len(result.items) > 1 and \
            _measure_result(blocks, structured) > CQ.QUERY_MAX_BYTES:
        # Retrait de la fin de page : la page suivante reprend exactement
        # après la dernière notice rendue (ni trou ni doublon).
        result.items = result.items[:-1]
        result.positions = result.positions[:-1]
        structured = result.as_dict()
        structured["byte_cap_reached"] = True
        lines = CQ.query_lines(result)
        blocks = [types.TextContent(type="text", text="\n".join(lines))]
    return blocks, structured


# Sélections HTTP de corpus_query : publication et purge sérialisées dans le
# processus (un appel en lecture seule écrit deux fichiers ; le verrou borne
# le débit d'écriture, la purge le nombre de fichiers gardés).
_SELECTION_LOCK = threading.Lock()


def _is_query_selection(doc) -> bool:
    """Manifeste d'une sélection publiée par corpus_query (type filtre,
    ``filters.corpus_query``) : seule production que la purge supprime."""
    return (isinstance(doc, dict) and doc.get("kind") == KIND_FILTER
            and isinstance((doc.get("filters") or {}).get("corpus_query"), dict))


def _purge_query_selections(source_stem: str, keep: int) -> int:
    """
    Supprime les sélections HTTP d'un même corpus source au-delà des
    ``keep`` plus récentes (date du manifeste). Seuls fichiers touchés :
    ``<source>_sel_<8 hex>.json`` et son manifeste, dans OUTPUT_DIR, quand ce
    manifeste atteste une sélection de corpus_query (``_is_query_selection``)
    ; jamais un lien symbolique, jamais un autre corpus. Sous le verrou du
    dossier (l'index, relu sous ce verrou, ne voit pas d'état intermédiaire ;
    il retire les radicaux disparus à sa prochaine lecture).

    Returns:
        Nombre de sélections supprimées (0 si le verrou n'est pas obtenu).
    """
    stem_re = re.compile(re.escape(source_stem) + r"_sel_[0-9a-f]{8}")
    suffix = ".manifest.json"
    try:
        with directory_lock(OUTPUT_DIR):
            found = []
            for entry in os.scandir(OUTPUT_DIR):
                name = entry.name
                if not name.endswith(suffix) or \
                        not stem_re.fullmatch(name[:-len(suffix)]):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                    doc = (json.loads(Path(entry.path).read_bytes())
                           if st.st_size <= RESOURCE_MAX_BYTES else None)
                except (OSError, UnicodeDecodeError, ValueError):
                    continue
                if _is_query_selection(doc):
                    found.append((st.st_mtime_ns, name[:-len(suffix)]))
            found.sort(reverse=True)
            removed = 0
            for _mtime, stem in found[max(1, int(keep)):]:
                data = OUTPUT_DIR / f"{stem}.json"
                try:
                    if data.is_symlink():
                        continue
                    data.unlink(missing_ok=True)
                    (OUTPUT_DIR / f"{stem}{suffix}").unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning(f"Sélection {stem} non supprimée "
                                   f"({type(exc).__name__})")
                    continue
                removed += 1
            return removed
    except (OSError, LockTimeout) as exc:
        logger.warning(f"Purge des sélections de {source_stem} reportée "
                       f"({type(exc).__name__})")
        return 0


def _corpus_query_http(path: Path, result) -> tuple[list, dict]:
    """
    HTTP : sélection publiée comme corpus dérivé (notices complètes, ordre
    rendu, manifeste de type filtre) dans OUTPUT_DIR ; retour : URL et
    métadonnées (compteurs, pagination, identifiants introuvables), aucune
    notice ni chemin.

    La sélection ne reprend pas la requête de la moisson source dans le
    champ ``query`` de son manifeste (elle est gardée sous
    ``filters.corpus_query.source_query``) : corpus_list(pattern=<thème>),
    que suit le prompt veille-mensuelle, ne retient donc pas les
    sélections, qui sinon occuperaient la liste et en chasseraient la
    moisson. Au plus ``QUERY_HTTP_KEEP_SELECTIONS`` sélections sont gardées
    par corpus source (``_purge_query_selections``).
    """
    notices = ([e["notice"] for e in result.items] if result.mode == "ids"
               else list(result.notices))
    source_stem = corpus_stem_of(path.name) or path.stem
    source_doc = load_manifest_for(path)
    meta = result.as_dict()
    meta.pop("items", None)
    selection = {k: meta.get(k) for k in (
        "mode", "query", "filters", "sort", "offset", "limit", "matched",
        "requested_ids", "missing_ids") if meta.get(k) not in (None, [], {})}
    source_query = _manifest_query_text(source_doc)
    if source_query:
        selection["source_query"] = source_query
    with _SELECTION_LOCK:
        run_id = new_run_id()
        stem = f"{source_stem}_sel_{run_id.rsplit('_', 1)[-1]}"
        output = resolve_in_output_dir(f"{stem}.json", OUTPUT_DIR)
        with Publication(OUTPUT_DIR, stem, KIND_FILTER, run_id=run_id,
                         started_at=now_iso(),
                         path_roots=_publication_roots(path)) as pub:
            pub.add_json(output, notices, role=ROLE_ZOTERO)
            manifest_path = pub.commit({
                "filters": {"corpus_query": selection},
                "inputs": [{"name": path.name, "items": result.total_items}],
                "records": inherited_records(source_doc, notices, by="id"),
            })
        purged = _purge_query_selections(source_stem,
                                         CQ.QUERY_HTTP_KEEP_SELECTIONS)
    meta.update({"selection": output.name, "selection_count": len(notices),
                 "manifest": manifest_path.name,
                 "selections_kept": CQ.QUERY_HTTP_KEEP_SELECTIONS})
    if purged:
        meta["selections_purged"] = purged
    if result.mode == "search":
        meta["returned"] = len(notices)
    else:
        meta["found"] = len(notices)
    what = (f"{len(notices)} notice(s) complète(s) sur "
            f"{len(result.requested_ids)} identifiant(s) demandé(s)"
            if result.mode == "ids" else
            f"notices {result.offset + 1} à {result.offset + len(notices)} "
            f"sur {result.matched} retenue(s) (tri : {result.order_label})")
    lines = [f"corpus_query : sélection de {path.name} publiée : {what}."]
    if result.mode == "ids" and result.missing_ids:
        lines.append(f"Identifiants introuvables : {len(result.missing_ids)} "
                     f"(liste : structuredContent.missing_ids).")
    if result.next_offset is not None:
        lines.append(f"Suite : offset={result.next_offset}.")
    lines.append(f"Sélection éphémère : les {CQ.QUERY_HTTP_KEEP_SELECTIONS} "
                 f"plus récentes de ce corpus sont gardées, télécharger "
                 f"sans tarder.")
    return _build_corpus_tool_result(
        summary_text="\n".join(lines),
        primary_items=notices,
        primary_path=output,
        primary_mime="application/json",
        extra_files=[manifest_path],
        structured=meta,
        inline=INLINE_NONE,
    )


# === Outils MCP avancés ===

async def _tool_fetch_by_doi(args: dict[str, Any]) -> tuple[list, dict]:
    """Implémentation de fetch_by_doi.

    OPT-10 : résolveurs en parallèle via le client HTTP partagé
    (``utils.lookups.lookup_doi``), notices vérifiées sur le DOI demandé. Le
    retour distingue un DOI inconnu des sources (``non_trouve``) d'une erreur
    réseau ou de service (``erreur`` : l'absence n'est pas établie), dans le
    texte et dans ``structuredContent`` (``status`` et issue de chaque
    source). Aucun chemin disque ni JSON bibliographique dans
    ``structuredContent``.

    OPT-07 : un DOI invalide ou une erreur réseau sont des erreurs d'outil
    (``isError``, même texte, même ``structuredContent``) ; en HTTP, même
    concurrence bornée et même échéance que ``moisson_search`` ; annulation
    propagée aux requêtes (vue du client partagé portant le signal de
    l'appel) ; tout le travail (réseau, fusion, écriture) hors de la boucle
    asyncio.
    """
    cancel_event = threading.Event()
    return await _run_guarded(
        lambda deadline: functools.partial(_fetch_by_doi_sync, args,
                                           cancel_event, deadline),
        cancel_event=cancel_event, label="Recherche par DOI",
        deadline_note="aucun corpus modifié")


def _cancel_signal(cancel_event, deadline: float | None = None):
    """Signal d'annulation d'un appel (événement et échéance), ou None."""
    from utils.http import CancelSignal
    if cancel_event is None and deadline is None:
        return None
    return CancelSignal(cancel_event, deadline)


def _fetch_by_doi_sync(args: dict[str, Any], cancel_event=None,
                       deadline: float | None = None) -> tuple[list, dict]:
    """Corps synchrone de fetch_by_doi."""
    from utils import lookups
    from utils.http import get_default_client

    doi = args["doi"]
    add_to = args.get("add_to_corpus")

    client = get_default_client()
    signal = _cancel_signal(cancel_event, deadline)
    if signal is not None:
        # Vue du client partagé : même session, signal propre à cet appel.
        client = client.with_cancel(signal)
    result = lookups.lookup_doi(doi, client)
    item = result["item"]
    status = result["status"]
    labels = {
        lookups.LOOKUP_FOUND: "trouvé",
        lookups.LOOKUP_NOT_FOUND: "non trouvé",
        lookups.LOOKUP_DOI_MISMATCH: "notice écartée (DOI non concordant)",
        lookups.LOOKUP_ERROR: "erreur",
        lookups.LOOKUP_SKIPPED: "non interrogé",
        lookups.LOOKUP_ABANDONED: "abandonné",
    }
    shown_detail = {lookups.LOOKUP_ERROR, lookups.LOOKUP_DOI_MISMATCH,
                    lookups.LOOKUP_ABANDONED, lookups.LOOKUP_SKIPPED}
    sources_line = "Sources : " + " ; ".join(
        f"{r['name']} : {labels.get(r['status'], r['status'])}"
        + (f" ({r['detail']})" if r["detail"] and r["status"] in shown_detail
           else "")
        for r in result["resolvers"]
    )
    structured = {
        "doi": result["doi"],
        "status": status,
        "resolvers": result["resolvers"],
        "duration_s": result["duration_s"],
    }

    def _reply(text: str, error: bool = False) -> tuple[list, dict]:
        if TRANSPORT_MODE == "http":
            text = _redact_paths_in_text(text)
        reply = ([types.TextContent(type="text", text=text)], structured)
        return ToolErrorContent(reply) if error else reply

    if status == lookups.DOI_INVALID:
        return _reply(
            f"DOI invalide : {doi!r} (forme attendue : 10.xxxx/suffixe, "
            f"éventuellement précédé de doi: ou https://doi.org/).",
            error=True,
        )
    if not item:
        if status == lookups.DOI_ERROR:
            return _reply(error=True, text=
                f"Erreur réseau : aucune métadonnée obtenue pour "
                f"DOI={result['doi']}. Au moins une source n'a pas pu répondre "
                f"(erreur réseau, de service ou délai dépassé) : l'absence de "
                f"ce DOI n'est pas établie, réessayer plus tard.\n"
                f"{sources_line}"
            )
        return _reply(
            f"Aucune métadonnée trouvée pour DOI={result['doi']} : aucune "
            f"source interrogée ne connaît ce DOI.\n{sources_line}"
        )

    lines = [
        f"DOI : {doi}",
        f"itemType : {item.get('itemType')}",
        f"Titre : {item.get('title', '')}",
        f"Auteurs : "
        + ", ".join(
            f"{c.get('lastName', '')} {c.get('firstName', '')}"
            for c in item.get("creators", [])
        ),
        f"Date : {item.get('date', '')}",
    ]
    if item.get("publicationTitle"):
        lines.append(f"Revue : {item['publicationTitle']}")
    if item.get("url"):
        lines.append(f"URL : {item['url']}")

    # Ajout optionnel à un corpus existant (jamais après une annulation)
    if add_to:
        if signal is not None:
            signal.check()
        corpus_path = _safe_path(add_to)  # confiné en HTTP
        if corpus_path.exists():
            existing = json.loads(corpus_path.read_text(encoding="utf-8"))
            existing.append(item)
            merged = deduplicate(existing)
            corpus_path.write_text(
                json.dumps(merged, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            # Régénérer le .csl.json en parallèle — dérivé du chemin DÉJÀ confiné
            # (correctif revue critique : sandboxer le résolu PUIS dériver).
            csl_path = corpus_path.parent / (corpus_path.stem + ".csl.json")
            csl_path.write_text(
                json.dumps(
                    export_corpus_to_csl(merged),
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
            lines.append("")
            lines.append(
                f"Ajouté à : {corpus_path.name} (taille {len(merged)} items)"
            )
            structured["added_to"] = corpus_path.name
            structured["corpus_item_count"] = len(merged)
        else:
            lines.append(f"Corpus introuvable : {add_to}")

    lines.append("")
    lines.append(sources_line)
    return _reply("\n".join(lines))


async def _tool_search_by_author(
    args: dict[str, Any],
) -> str | tuple[list, dict]:
    """Implémentation de search_by_author.

    Renvoie le retour enrichi (tuple) lorsque le corpus est sauvegardé
    (``save_corpus=True`` et items non vides) ; sinon le texte et
    ``structuredContent`` sans fichier (``str`` legacy si la fonction de
    recherche ne rend pas de rapport).

    OPT-10 : les sources sont interrogées en parallèle (pool borné) avec le
    client HTTP partagé ; l'ordre de fusion reste l'ordre séquentiel.

    OPT-07 : en HTTP, plafond serveur sur ``max_per_source``, concurrence
    bornée et échéance comme ``moisson_search`` ; annulation propagée aux
    connecteurs et aux requêtes ; aucun corpus écrit après une annulation ;
    recherche et écriture hors de la boucle asyncio.

    OPT-14 : identifiants explicites (``openalex_author_id``,
    ``s2_author_id``, ``hal_person_id`` en plus d'``idhal`` et ``orcid``),
    statut par source (``by_source_status``, ``harvest_status``,
    ``production_complete``, ``caveats``) ; homonymie en recherche par nom
    seul : retour non-erreur, aucun fichier, candidats dans
    ``structuredContent.candidates`` ; échec de toutes les sources :
    ``isError``, aucun fichier ; identifiant mal formé : paramètre refusé.

    Revue OPT-14 : nom vide sans identifiant et ORCID à clé de contrôle
    fausse refusés ; profils choisis sur le nom et non reliés aux
    identifiants fournis proposés à confirmer
    (``structuredContent.profiles_to_confirm``), jamais retenus en silence ;
    conseil ORCID nuancé (fiable pour OpenAlex seulement).
    """
    cancel_event = threading.Event()
    return await _run_guarded(
        lambda deadline: functools.partial(_search_by_author_sync, args,
                                           cancel_event, deadline),
        cancel_event=cancel_event, label="Recherche par auteur",
        deadline_note="aucun corpus partiel publié")


def _search_by_author_sync(args: dict[str, Any], cancel_event=None,
                           deadline: float | None = None
                           ) -> str | tuple[list, dict]:
    """Corps synchrone de search_by_author."""
    from utils.http import get_default_client
    from utils.lookups import normalize_author_identifiers

    name = args["name"]
    idhal = args.get("idhal")
    orcid = args.get("orcid")
    max_per_source = args.get("max_per_source", 200)
    save = args.get("save_corpus", True)
    # Identifiants ajoutés par OPT-14, transmis seulement s'ils sont fournis.
    new_ids = {key: args[key] for key in
               ("openalex_author_id", "s2_author_id", "hal_person_id")
               if args.get(key) not in (None, "", [])}
    try:
        # ORCID : forme stricte et clé de contrôle (faute de frappe refusée).
        normalize_author_identifiers(orcid=orcid, **new_ids)
    except ValueError as e:
        return ToolError(f"Paramètre refusé : {e}")
    if not str(name or "").strip() and not (
            str(idhal or "").strip() or orcid or new_ids):
        # Le schéma exige la clé, pas une valeur : un nom vide sans
        # identifiant n'interrogerait aucune source.
        return ToolError("Paramètre refusé : name vide ; indiquer le nom de "
                         "l'auteur (« Prénom Nom ») ou au moins un "
                         "identifiant (idhal, orcid, hal_person_id, "
                         "openalex_author_id, s2_author_id).")
    if TRANSPORT_MODE == "http":
        # Plafond serveur DUR (anti-DoS), comme moisson_search.
        max_per_source = min(int(max_per_source), MOISSON_MAX_ITEMS_PER_SOURCE)
    parsed_inline = _inline_args(args)      # retour (OPT-17)
    if isinstance(parsed_inline, ToolError):
        return parsed_inline
    inline, top = parsed_inline

    client = get_default_client()
    started_at = now_iso()
    signal = _cancel_signal(cancel_event, deadline)
    kwargs = {"cancel": signal} if signal is not None else {}
    kwargs.update(new_ids)
    try:
        items = search_by_author(
            name, idhal=idhal, orcid=orcid,
            max_per_source=max_per_source,
            http_client=client, **kwargs,
        )
    except ValueError as e:
        return ToolError(f"Paramètre refusé : {e}")
    # Rapport de la recherche (statuts, candidats) porté par la liste ; absent
    # si la fonction a été remplacée (retour historique).
    report = getattr(items, "report", None)
    head = (f"Recherche : {name}"
            + (f" (idhal={idhal})" if idhal else "")
            + (f" (orcid={orcid})" if orcid else "")
            + "".join(f" ({key}={value})" for key, value in new_ids.items()))
    structured = {
        "author": name,
        "idhal": idhal,
        "orcid": orcid,
        **new_ids,
        "item_count_raw": len(items),
    }
    if report is not None:
        structured.update(report.as_dict())

    if report is not None and report.ambiguous:
        # Homonymie en recherche par nom seul : aucune notice retenue, aucun
        # fichier ; retour non-erreur qui demande l'identité (second appel).
        lines = [
            head,
            "Items uniques : 0",
            "",
            "Homonymie : plusieurs profils d'auteur correspondent à ce nom. "
            "Aucune notice n'a été retenue ni enregistrée (jamais le premier "
            "candidat par défaut).",
            "Choisissez l'identité, puis relancez search_by_author avec le nom "
            "et, pour chaque source, l'argument du candidat qui correspond à "
            "la personne choisie (idhal ou hal_person_id pour HAL, "
            "openalex_author_id, s2_author_id ; plusieurs identifiants "
            "OpenAlex ou Semantic Scholar séparés par des virgules si la même "
            "personne a plusieurs profils). Une source laissée sans argument "
            "est interrogée par le nom et son profil n'est retenu que s'il "
            "porte l'ORCID fourni. L'ORCID n'identifie une source que si ses "
            "notices ou profils le portent : fiable pour OpenAlex, rarement "
            "renseigné dans HAL et Semantic Scholar (préférer l'argument du "
            "candidat).",
            "",
            "Candidats :",
            *report.candidate_lines(),
        ]
        confirm = report.confirm_lines()
        if confirm:
            lines.extend([
                "",
                "Profils trouvés par le nom dans les autres sources (à "
                "ajouter par leur argument s'il s'agit de la personne "
                "choisie) :",
                *confirm,
            ])
        lines.extend(["", "Statut par source :", *report.status_lines()])
        structured["item_count"] = 0
        structured["next_step"] = (
            "Rappeler search_by_author avec name et, pour chaque source, "
            "l'argument (argument) du candidat correspondant à la personne "
            "choisie, y compris ceux de profiles_to_confirm qui la désignent.")
        return ([types.TextContent(type="text", text="\n".join(lines))],
                structured)

    if report is not None and report.all_failed:
        # Toutes les sources en panne (ou non exécutées) : jamais « aucune
        # publication » ; isError, aucun fichier.
        lines = [
            "Recherche par auteur en échec : aucune source n'a pu être "
            "interrogée ; aucun corpus n'a été écrit.",
            "",
            head,
            "",
            "Statut par source :",
            *report.status_lines(),
        ]
        structured["item_count"] = 0
        return ToolError("\n".join(lines), structured=structured)

    json_path = csl_path = stats_path = manifest_path = None
    records = None
    if save and items:
        from main import slugify
        if signal is not None:
            signal.check()   # aucune publication après une annulation
        # Nom unique (OPT-16, C33) : horodatage + uuid court.
        run_id = new_run_id()
        slug = slugify(f"author_{name}")
        records = simple_records(items)
        files = write_outputs(
            items, OUTPUT_DIR, corpus_stem(slug, run_id), f"author:{name}",
            kind=KIND_AUTHOR, run_id=run_id, started_at=started_at,
            path_roots=(OUTPUT_DIR,),
            manifest={
                "query": {"text": f"author:{name}", "author": name,
                          "identifiers": {k: v for k, v in {
                              "idhal": idhal, "orcid": orcid, **new_ids,
                          }.items() if v}},
                "settings": {"max_per_source": max_per_source},
                "sources": (structured.get("by_source_status")
                            if report is not None else None),
                "harvest_status": structured.get("harvest_status"),
                "author_report": {k: v for k, v in structured.items()
                                  if k not in ("by_source_status",)},
                "records": records,
            },
        )
        json_path, csl_path, stats_path = (
            files.json_path, files.csl_path, files.stats_path)
        manifest_path = getattr(files, "manifest_path", None)
        if inline != INLINE_FULL and TRANSPORT_MODE != "http":
            # Mode digest ou none (OPT-17) : fichiers sur une ligne, rapport
            # de recherche compact (complet au manifeste, écrit ci-dessus).
            save_info = "\n" + _compact_files_line(json_path, manifest_path)
            _compact_author_structured(
                structured, manifest_path.name if manifest_path else None)
        else:
            save_info = (
                f"\nCorpus sauvegardé :\n"
                f"  Zotero JSON    : {json_path}\n"
                f"  CSL-JSON       : {csl_path}\n"
                f"  Statistiques   : {stats_path}"
                + (f"\n  Manifeste      : {manifest_path}" if manifest_path
                   else "")
            )
    else:
        save_info = ""

    lines = [head, f"Items uniques : {len(items)}"]
    if report is not None:
        # Aucune promesse de production complète : plafonds, pannes, sources
        # ignorées, homonymes et identités non vérifiées sont nommés.
        caveats = report.caveats()
        if report.production_complete and not caveats:
            lines.append(
                "Sources interrogées parcourues jusqu'au bout (aucun plafond "
                "atteint, aucune panne) ; la production réelle peut dépasser "
                "les sources interrogées.")
        else:
            lines.append("Production possiblement incomplète ou à vérifier :")
            lines.extend(f"  - {c}" for c in caveats or
                         ["voir le statut par source"])
        lines.extend(["", "Statut par source :", *report.status_lines()])
        if report.ambiguous_sources:
            lines.extend([
                "",
                "Sources écartées pour homonymie (relancer avec l'argument du "
                "candidat choisi pour les inclure) :",
                *report.candidate_lines(),
            ])
        confirm = report.confirm_lines()
        if confirm:
            lines.extend([
                "",
                "Profils trouvés par le nom, non retenus faute de lien avec "
                "les identifiants fournis (relancer avec leur argument s'il "
                "s'agit de la même personne) :",
                *confirm,
            ])
    # Les dix plus récentes, sauf en mode digest (le digest les donne, avec
    # les suivantes) quand le corpus est enregistré (OPT-17).
    if not (inline == INLINE_DIGEST and save and items
            and TRANSPORT_MODE != "http"):
        lines.extend(["", "10 publications les plus récentes :"])
        for i, item in enumerate(items[:10]):
            lines.append(
                f"  {i + 1}. [{item.get('date', '?')[:4]}] "
                f"{item.get('title', '')[:90]}"
            )
    lines.append(save_info)
    summary = "\n".join(lines)

    # Pas de fichier produit → texte et statuts (str legacy sans rapport).
    if not (save and items):
        if report is None:
            return summary
        structured["item_count"] = len(items)
        return ([types.TextContent(type="text", text=summary)], structured)

    if manifest_path is not None:
        structured["manifest"] = manifest_path.name
    return _build_corpus_tool_result(
        summary_text=summary,
        primary_items=items,
        primary_path=json_path,
        primary_mime="application/json",
        extra_files=[csl_path, stats_path,
                     *([manifest_path] if manifest_path else [])],
        structured=structured,
        inline=inline, top=top,
        digest=DigestSpec(items=items, records=records,
                          corpus_path=json_path,
                          order_label="année décroissante"),
    )


async def _tool_corpus_merge(args: dict[str, Any]) -> str | tuple[list, dict]:
    """Implémentation de corpus_merge (lecture, fusion et écriture hors de
    la boucle asyncio, OPT-07)."""
    return await asyncio.to_thread(_corpus_merge_sync, args)


def _corpus_merge_sync(args: dict[str, Any]) -> str | tuple[list, dict]:
    """Corps synchrone de corpus_merge."""
    corpus_paths = [_safe_path(p) for p in args["corpus_paths"]]  # confinés en HTTP
    missing = [p.name for p in corpus_paths if not p.exists()]
    if missing:
        return ToolError(f"Corpus introuvables : {', '.join(missing)}")
    started_at = now_iso()
    parsed_inline = _inline_args(args)      # retour (OPT-17)
    if isinstance(parsed_inline, ToolError):
        return parsed_inline
    inline, top = parsed_inline

    run_id = None
    output_path = args.get("output_path")
    if output_path:
        output = _safe_path(output_path)  # confiné en HTTP
    else:
        # Nom unique (OPT-16, C33) : horodatage + uuid court ; deux fusions
        # de la même seconde n'écrivent plus le même fichier.
        run_id = new_run_id()
        output = OUTPUT_DIR / f"corpus_merged_{run_id}.json"

    # Entrées au format Zotero interne seulement (OPT-16) : refus clair d'un
    # CSL-JSON, d'un manifeste ou d'un rapport DOI, avant toute écriture.
    try:
        merged, report, detail = merge_corpora_detailed(corpus_paths)
    except CorpusFormatError as e:
        return ToolError(str(e))

    # Contexte de chaque entrée (PAT-18) : requête, mode, sources, dates,
    # statuts, plafonds et tris de la moisson d'origine ; origine de chaque
    # notice dans ``records`` (rangs natifs repris seulement d'un manifeste
    # qui décrit encore son fichier).
    inputs, input_docs = [], []
    for k, p in enumerate(corpus_paths):
        doc = load_manifest_for(p)
        inputs.append(merge_input_entry(p, items=detail["loaded"][k], doc=doc))
        input_docs.append(doc if manifest_describes(doc, p) else None)
    csl_path = output.parent / (output.stem + ".csl.json")
    stem = corpus_stem_of(output.name) or output.stem
    # Publication atomique et manifeste (OPT-16).
    with Publication(output.parent, stem, KIND_MERGE, run_id=run_id,
                     started_at=started_at,
                     path_roots=_publication_roots(*corpus_paths)) as pub:
        pub.add_json(output, merged, role=ROLE_ZOTERO)
        pub.add_json(csl_path, export_corpus_to_csl(merged), role=ROLE_CSL)
        records = merged_records(merged, detail["origins"], input_docs)
        manifest_path = pub.commit({
            "inputs": inputs,
            "merge_report": report,
            "records": records,
        })

    summary = (
        f"{format_merge_report(report)}\n\n"
        f"Corpus fusionné écrit :\n"
        f"  Zotero JSON : {output}\n"
        f"  CSL-JSON    : {csl_path}\n"
        f"  Manifeste   : {manifest_path.name}"
    )
    structured = {
        "input_corpora": [str(p) for p in corpus_paths],
        "merged_count": len(merged),
        "report": report,
        "manifest": manifest_path.name,
    }
    return _build_corpus_tool_result(
        summary_text=summary,
        primary_items=merged,
        primary_path=output,
        primary_mime="application/json",
        extra_files=[csl_path, manifest_path],
        structured=structured,
        inline=inline, top=top,
        digest=DigestSpec(items=merged, records=records,
                          corpus_path=output,
                          order_label="ordre de la fusion"),
    )


# === Resources MCP : corpus exposés comme ressources ===

def _listed_corpus_paths() -> list[Path]:
    """
    Corpus listables de OUTPUT_DIR (compatibilité) : fichiers Zotero et
    CSL-JSON de l'index dérivé (``utils.manifest``), motif historique
    ``corpus_*.json``, hors fichiers associés et de service (y compris
    derrière un lien symbolique) ; en HTTP, hors liens symboliques sortant de
    OUTPUT_DIR (illisibles à distance).
    """
    http = TRANSPORT_MODE == "http"
    corpora, _state = load_index(OUTPUT_DIR)
    return [OUTPUT_DIR / name
            for entry in corpora.values()
            for name, info in (entry.get("files") or {}).items()
            if info.get("role") in (ROLE_ZOTERO, ROLE_CSL)
            and _index_file_visible(info, http)]


@server.list_resources()
async def list_resources() -> list[types.Resource]:
    """
    Expose chaque corpus du OUTPUT_DIR comme une Resource MCP.

    Permet à Claude de lire directement un corpus sans appel d'outil,
    via l'URI corpus://<nom_de_fichier>. La distinction format Zotero
    interne (.json) vs CSL-JSON (.csl.json) est explicitée dans la
    description.

    Seuls les corpus sont listés (``utils.output_files``) : ni fichiers
    associés (manifeste, rapport DOI), ni fichiers de service. En HTTP, la
    description ne porte aucun chemin disque (contrat du Sprint 08) et un
    lien symbolique sortant de OUTPUT_DIR, illisible à distance, n'est pas
    listé ; en stdio, le chemin local reste indiqué.

    Lecture de l'index dérivé (``OUTPUT_DIR/.index.json``, OPT-16) au lieu
    de chaque corpus (C15), hors de la boucle asyncio (fil de travail,
    OPT-07, C17).
    """
    return await asyncio.to_thread(_list_resources_sync)


def _list_resources_sync() -> list[types.Resource]:
    """Corps synchrone de list_resources : index dérivé des corpus (OPT-16),
    sans relire chaque corpus (C15)."""
    http = TRANSPORT_MODE == "http"
    corpora, _state = load_index(OUTPUT_DIR)
    listed = [
        (name, info)
        for entry in corpora.values()
        for name, info in (entry.get("files") or {}).items()
        if info.get("role") in (ROLE_ZOTERO, ROLE_CSL)
        and _index_file_visible(info, http)
    ]
    resources = []
    for name, info in sorted(listed, reverse=True):
        path = OUTPUT_DIR / name
        is_csl = info.get("role") == ROLE_CSL or info.get("format") == "csl"
        n_items = info.get("items") or 0
        kind = "CSL-JSON (BibTeX JSON, import Zotero)" if is_csl else "Zotero JSON interne"
        description = (f"{kind} — {n_items} items — pages : ?offset=0&limit="
                       f"{CQ.RESOURCE_PAGE_DEFAULT}")
        if not http:
            description += f" — {path}"
        resources.append(
            types.Resource(
                uri=corpus_uri(path.name),
                name=path.stem,
                mimeType="application/json",
                description=description,
            )
        )
    return resources


async def read_resource(uri: str) -> str:
    """
    Lit le contenu d'un corpus exposé comme Resource.

    URI attendu : `corpus://<filename>` (espace de noms PLAT). Garde de
    volumétrie : au-delà de RESOURCE_MAX_BYTES octets, corpus tronqué à ses
    RESOURCE_TRUNCATED_ITEMS premiers items (avec l'URI de la page suivante).

    Pages (OPT-17) : `corpus://<filename>?offset=<n>&limit=<n>` rend les
    notices ``offset`` à ``offset + limit`` d'un corpus JSON (``limit`` :
    ``RESOURCE_PAGE_DEFAULT`` par défaut, ramené à ``RESOURCE_PAGE_MAX``),
    lues en flux sous le plafond d'octets, avec le total, la page suivante
    (``_next_offset``, ``_next_uri``) et l'indicateur de plafond ; seuls ces
    deux paramètres, entiers et bornés, sont acceptés
    (``utils.output_files.parse_corpus_uri``). Mêmes confinement et refus
    que la lecture entière.

    Renvoie le texte (compatibilité des appelants) ; le gestionnaire MCP
    ``_read_resource_contents`` l'enveloppe dans la forme non dépréciée du
    SDK (``ReadResourceContents``, avec le type MIME du fichier).

    Note : le SDK passe l'URI sous forme d'objet ``AnyUrl`` (pydantic),
    pas d'une ``str`` ; les deux formes sont acceptées. Le SDK ne protège
    PAS contre la traversée (vérifié, OPT-02) : ``corpus://../x`` lui
    parvient intact (``..`` est l'hôte opaque de l'URL), de même que
    ``corpus:///chemin/absolu`` et les hôtes encodés ``%2e%2e`` ; seuls
    les segments de CHEMIN (``corpus://a/../b``) sont normalisés. Le nom est
    donc décodé une fois puis validé (``corpus_name_from_uri``) : aucun
    séparateur, aucun nom commençant par un point.

    Contrôles (OPT-02, C03) :
    - **les deux transports** : fichiers de service (``.oauth/``,
      ``.audit/``, ``.index.json``…) et fichiers associés (manifeste,
      rapport DOI) jamais lisibles, y compris derrière un lien symbolique ;
    - **HTTP** : confinement identique à ``/download`` — résolution sous
      OUTPUT_DIR (``utils.sandbox``), lien sortant refusé, liste blanche
      ``.json`` / ``.csl.json`` / ``_stats.txt``, PAS de repli
      ``_EMITTED_RESOURCES`` ; mêmes réponses pour « refusé » et
      « introuvable » (pas d'oracle d'existence), aucun chemin disque ;
    - **stdio** : repli ``_EMITTED_RESOURCES`` conservé pour les fichiers
      émis hors OUTPUT_DIR (périmètre du Sprint 05).

    Lecture (C05) : la taille est connue par ``stat()`` AVANT toute lecture ;
    un petit fichier est lu avec une borne ; un gros fichier est parcouru en
    flux (``iter_json_array``), jamais chargé en entier.
    """
    try:
        filename, page = parse_corpus_uri(uri)
    except ValueError as e:
        return json.dumps({"error": f"URI invalide : {e}"}, ensure_ascii=False)

    http = TRANSPORT_MODE == "http"
    # Refus sur le seul NOM (avant tout accès disque) : fichier associé en
    # stdio comme en HTTP ; en HTTP, tout ce qui sort de la liste blanche.
    if is_associated_file(filename) or (
        http and not is_publishable_file(filename)
    ):
        return json.dumps(
            {"error": f"Ressource non exposée : {filename}"},
            ensure_ascii=False,
        )

    # Résolution (accès disque) et lecture dans un fil de travail (OPT-07).
    if page is not None:
        return await asyncio.to_thread(_read_resource_page_sync, filename,
                                       http, page)
    return await asyncio.to_thread(_read_resource_sync, filename, http)


def _resource_mime(uri, text: str) -> str:
    """Type MIME d'une réponse de ``read_resource`` : texte brut pour des
    statistiques, type de l'export, JSON sinon (corpus, page, erreur)."""
    try:
        name, page = parse_corpus_uri(uri)
    except ValueError:
        return "application/json"
    if page is None and not text.startswith('{"error"'):
        if corpus_kind(name) == "stats":
            return "text/plain"
        media = export_media_type(name)
        if media:
            return media.split(";")[0]
    return "application/json"


@server.read_resource()
async def _read_resource_contents(uri) -> list:
    """
    Gestionnaire MCP de ``resources/read`` : contenu de ``read_resource``
    dans la forme non dépréciée du SDK 1.x (``ReadResourceContents``, avec
    le type MIME du fichier ; renvoyer une ``str`` est déprécié depuis le
    SDK 1.x, vérifié sur 1.27.1).
    """
    from mcp.server.lowlevel.helper_types import ReadResourceContents

    text = await read_resource(uri)
    return [ReadResourceContents(content=text, mime_type=_resource_mime(uri, text))]


@server.list_resource_templates()
async def list_resource_templates() -> list[types.ResourceTemplate]:
    """Modèle d'URI des pages d'un corpus (OPT-17)."""
    return [types.ResourceTemplate(
        uriTemplate="corpus://{name}?offset={offset}&limit={limit}",
        name="corpus-page",
        description=(
            "Page d'un corpus JSON : notices offset à offset + limit "
            f"(limit {CQ.RESOURCE_PAGE_DEFAULT} par défaut, au plus "
            f"{CQ.RESOURCE_PAGE_MAX}), avec le total et l'URI de la page "
            "suivante. Recherche et notices complètes : outil corpus_query."),
        mimeType="application/json",
    )]


def _read_resource_page_sync(filename: str, http: bool, page: dict) -> str:
    """
    Page d'un corpus JSON (OPT-17), hors boucle asyncio : notices
    ``offset`` à ``offset + limit`` lues en flux, sous RESOURCE_MAX_BYTES
    octets (au moins une notice ; au-delà, page raccourcie et page suivante
    reprise exactement après la dernière notice rendue) ; total repris du
    manifeste quand il décrit le fichier tel qu'il est (lecture arrêtée
    après la page), compté en flux sinon.
    """
    path = _resolve_resource_path(filename, http=http)
    if path is None:
        return json.dumps(
            {"error": f"Corpus introuvable : {filename}"}, ensure_ascii=False)
    if corpus_kind(path.name) not in ("zotero", "csl"):
        return json.dumps(
            {"error": f"Pagination réservée aux corpus JSON : {filename}"},
            ensure_ascii=False)
    offset, limit = CQ.resolve_page(page.get("offset"), page.get("limit"))
    known = CQ.ManifestView.load(path).item_count
    try:
        items, total, capped = CQ.iter_page(
            path, offset, limit, item_bytes=_sample_item_bytes,
            max_bytes=RESOURCE_MAX_BYTES, known_total=known)
    except ValueError:
        return json.dumps(
            {"error": f"Corpus non lisible comme tableau JSON : {filename}"},
            ensure_ascii=False)
    except (OSError, UnicodeDecodeError):
        return json.dumps(
            {"error": f"Lecture impossible : {filename}"}, ensure_ascii=False)
    end = offset + len(items)
    next_offset = end if (items and (total is None or end < total)) else None
    doc = {
        "_page": True,
        "_offset": offset,
        "_limit": limit,
        "_returned": len(items),
        "_total_items": total,
        "_next_offset": next_offset,
        "_next_uri": (corpus_page_uri(filename, next_offset, limit)
                      if next_offset is not None else None),
        "_byte_cap_reached": capped,
        "items": items,
    }
    if total is not None and offset >= total and total:
        doc["_message"] = f"offset {offset} au-delà de la fin ({total} notices)"
    return json.dumps(doc, ensure_ascii=False, indent=2)


def _read_resource_sync(filename: str, http: bool) -> str:
    """Résolution et lecture bornée d'une ressource (hors boucle asyncio)."""
    path = _resolve_resource_path(filename, http=http)
    if path is None:
        return json.dumps(
            {"error": f"Corpus introuvable : {filename}"}, ensure_ascii=False
        )
    try:
        return _read_resource_text(path, filename)
    except (OSError, UnicodeDecodeError):
        # Message sans chemin disque (les exceptions en portent un).
        return json.dumps(
            {"error": f"Lecture impossible : {filename}"}, ensure_ascii=False
        )


def _resolve_resource_path(filename: str, *, http: bool) -> Path | None:
    """
    Résout le nom plat d'une ressource en fichier lisible, ou None.

    HTTP : ``resolve_in_output_dir`` (confinement, liens sortants refusés),
    aucun repli. Stdio : ``OUTPUT_DIR/<nom>`` puis repli sur le registre
    ``_EMITTED_RESOURCES``. Dans les deux cas, la cible résolue ne doit être
    ni un fichier de service ni un fichier associé.
    """
    if http:
        try:
            path = resolve_in_output_dir(filename, OUTPUT_DIR, must_exist=True)
        except PathSecurityError:
            return None
    else:
        path = OUTPUT_DIR / filename
        if not path.exists():
            # Repli : fichier exposé par un outil producteur hors OUTPUT_DIR
            # (corpus_filter écrit à côté de la source, corpus_to_csl/merge
            # honorent un output_path arbitraire). Le registre garde le chemin
            # absolu, ce qui rend le round-trip du lien corpus:// fiable.
            registered = _EMITTED_RESOURCES.get(filename)
            if registered is None or not registered.exists():
                return None
            path = registered
    if not path.is_file() or _is_output_service_file(path):
        return None
    resolved = path.resolve()
    if is_associated_file(resolved):
        return None
    if http and not is_publishable_file(resolved):
        return None
    return path


def _read_resource_text(path: Path, filename: str) -> str:
    """
    Contenu d'une ressource sous la garde de RESOURCE_MAX_BYTES octets.

    Décision sur ``stat()`` : un fichier sous le plafond est lu avec une
    borne (plafond + 1 octet, en cas de croissance entre stat et lecture) ;
    au-delà, échantillon des premiers items lu en flux.
    """
    size = path.stat().st_size
    if size <= RESOURCE_MAX_BYTES:
        with path.open("rb") as fh:
            data = fh.read(RESOURCE_MAX_BYTES + 1)
        if len(data) <= RESOURCE_MAX_BYTES:
            return data.decode("utf-8")
        size = max(size, len(data))
    return _truncated_resource_text(path, filename, size)


def _sample_item_bytes(item) -> int:
    """
    Taille en octets UTF-8 d'une notice **telle qu'émise** dans l'échantillon
    tronqué : ``json.dumps(..., indent=2)`` de l'enveloppe place chaque item à
    la profondeur 2 (quatre espaces de plus sur chaque ligne après la
    première), précédé d'un séparateur ``,\\n    `` (six octets).

    Mesurer en JSON compact sous-estimait la réponse : l'indentation gonfle
    de 11 à 25 % les notices réelles, et presque du double une notice faite
    de nombreux champs courts (longues listes de ``creators``).
    """
    text = json.dumps(item, ensure_ascii=False, indent=2)
    return len(text.encode("utf-8")) + 4 * text.count("\n") + 6


def _truncated_resource_text(path: Path, filename: str, size: int) -> str:
    """
    Échantillon d'un corpus trop volumineux, lu en flux (mémoire bornée par
    un bloc de lecture et une notice ; jamais de ``read_text`` ni de
    ``json.loads`` du fichier entier).

    Sémantique historique conservée : ``_truncated``, ``_total_items``
    (décompte exact, d'où un parcours complet du fichier, mais en flux),
    ``_returned``, ``_message``, ``items`` (les RESOURCE_TRUNCATED_ITEMS
    premiers). Ajout : l'échantillon cesse de croître dès que ses items,
    mesurés **dans le format d'émission** (``_sample_item_bytes`` : indent=2
    et imbrication dans l'enveloppe), atteignent RESOURCE_MAX_BYTES octets,
    pour qu'un échantillon de notices énormes ne dépasse pas à son tour la
    garde. Plafond souple : la réponse fait au plus RESOURCE_MAX_BYTES, plus
    la dernière notice admise, plus l'enveloppe (moins de 1 Ko). Un contenu
    volumineux qui n'est pas un tableau JSON est refusé (auparavant renvoyé
    en entier, ou exception).
    """
    sample: list = []
    sample_bytes = 0
    total = 0
    try:
        for item in iter_json_array(path):
            if (len(sample) < RESOURCE_TRUNCATED_ITEMS
                    and sample_bytes < RESOURCE_MAX_BYTES):
                sample.append(item)
                sample_bytes += _sample_item_bytes(item)
            total += 1
    except ValueError:
        return json.dumps(
            {"error": (
                f"Ressource trop volumineuse ({size // 1024} ko) pour être "
                f"renvoyée intégralement, et non lisible comme tableau "
                f"JSON : {filename}"
            )},
            ensure_ascii=False,
        )
    message = (
        f"Corpus trop volumineux pour être renvoyé intégralement "
        f"({size // 1024} ko). {len(sample)} premiers items inclus ci-dessous."
    )
    byte_cap_reached = len(sample) < min(RESOURCE_TRUNCATED_ITEMS, total)
    if byte_cap_reached:
        message += (
            f" Échantillon arrêté au plafond de {RESOURCE_MAX_BYTES} octets "
            f"(notices volumineuses)."
        )
    message += (" Suite par pages : _next_uri (corpus://<nom>?offset=&limit=) "
                "ou l'outil corpus_query.")
    return json.dumps(
        {
            "_truncated": True,
            "_total_items": total,
            "_returned": len(sample),
            "_message": message,
            "_byte_cap_reached": byte_cap_reached,
            "_next_uri": (corpus_page_uri(filename, len(sample),
                                          CQ.RESOURCE_PAGE_DEFAULT)
                          if len(sample) < total else None),
            "items": sample,
        },
        ensure_ascii=False, indent=2,
    )


# === Prompts MCP : workflows pré-configurés ===
#
# Quatre prompts historiques, réécrits par OPT-20 pour dire vrai sur le code
# livré :
# - aucun réglage qui contredise les défauts du serveur (plus de ``workers``,
#   plus de ``max_per_source=1000`` imposé) ;
# - thème ou question passés tels quels : entre guillemets, la requête
#   deviendrait une expression exacte (OPT-11, constat 4) ;
# - statuts des sources, limites du fournisseur et plafonds signalés ;
#   sélection expliquée (mode, tri natif, classement) ;
# - résultats bibliographiques distincts de l'interprétation ; résumés lus par
#   ``corpus_query(ids=…)`` et jamais pris pour le texte intégral ;
# - un argument obligatoire absent est demandé à l'utilisateur (un client peut
#   appeler ``get_prompt`` sans arguments).

_PROMPT_ASK = "demande d'abord à l'utilisateur"


def _labels_without_language(conj: str = "et") -> str:
    """Sources dont les notices ne portent aucune langue (registre des
    capacités, ``language_in_records``), en liste française (« CrossRef,
    Semantic Scholar et Google Scholar ») : le filtre ``languages`` de
    ``corpus_query`` écarte leurs notices. Sert aussi à ``list_tools``."""
    from connectors.capabilities import CAPABILITIES
    labels = [c.label for c in CAPABILITIES.values()
              if not c.language_in_records]
    if len(labels) < 2:
        return "".join(labels)
    return ", ".join(labels[:-1]) + f" {conj} " + labels[-1]


# Sélection d'une moisson (moisson_search) : mode, tri natif, classement. En
# digest (défaut en local), structuredContent.query_plan est résumé et ne
# porte ni l'ordre ni les filtres par source : l'ordre se lit dans le plan
# complet d'un appel plan_only=true (mêmes arguments, rien d'exécuté).
_PROMPT_SELECTION_HARVEST = (
    "Sélection : explique comment les références ont été retenues : mode de "
    "moisson (bibliographie : les premières notices de chaque source dans son "
    "ordre natif, au plus max_per_source par source ; collecte : ordre "
    "déterministe, exhaustif dans la limite du plafond), tri appliqué par "
    "chaque source (champ ordering de chaque source dans le plan complet, "
    "structuredContent.query_plan d'un appel avec plan_only=true et les "
    "mêmes arguments, qui n'exécute rien ; le plan résumé du retour en digest "
    "ne le donne pas) et classement du digest (top_ranked : 'native' par "
    "défaut, fusion des rangs des sources qui classent par pertinence ; "
    "Semantic Scholar, trié par citations, CORE et Google Scholar n'y pèsent "
    "pas). Un rang n'est ni un jugement de qualité ni une probabilité de "
    "pertinence."
)

# Sélection d'une recherche par auteur (search_by_author).
_PROMPT_SELECTION_AUTHOR = (
    "Sélection : les notices viennent des profils retenus source par source "
    "(par identifiant, ou seul profil compatible avec le nom) ; dis sur quelle "
    "base chaque source a été retenue (identity_basis dans by_source_status). "
    "Le digest range les notices par année décroissante, sans classement de "
    "pertinence. Isidore, sans identifiant d'auteur, ne garde que les notices "
    "dont les auteurs portent le nom et n'est pas séparé des homonymes."
)


def _prompt_rules(selection: str) -> str:
    """Règles de restitution communes aux quatre prompts (OPT-20)."""
    # Fenêtre d'Isidore : 0 la désactive (config.py) ; reste la borne de
    # parcours, dite comme telle.
    if ISIDORE_RESULT_WINDOW > 0:
        bound = (f"Isidore ne sert que les {ISIDORE_RESULT_WINDOW} premiers "
                 "résultats d'une requête, fenêtre partagée par OpenEdition, "
                 "Cairn, Persée, Érudit et Theses.fr")
    else:
        from config import ISIDORE_SCAN_MAX_RECORDS
        bound = ("fenêtre de résultats d'Isidore levée par le serveur, mais "
                 f"parcours borné à {ISIDORE_SCAN_MAX_RECORDS} notices par "
                 "requête, borne partagée par OpenEdition, Cairn, Persée, "
                 "Érudit et Theses.fr")
    return (
        "Règles de restitution :\n"
        "- Sources et limites : donne le statut de chaque source tel que le "
        "retour l'indique (complète, aucun résultat, partielle, non exécutée, "
        "en échec, annulée) et son motif. Nomme les sources indisponibles "
        "(clé d'API absente, panne, quota, échéance atteinte). Distingue une "
        f"limite du fournisseur, qui n'est pas une panne ({bound}), d'une "
        "source qui a perdu des notices. Signale tout plafond atteint : la "
        "liste n'est alors pas exhaustive.\n"
        f"- {selection}\n"
        "- Lecture : le digest du retour ne donne que des métadonnées (titre, "
        "auteurs, année, support, identifiant, termes appariés, réserves). "
        "Pour lire des résumés, appelle corpus_query avec l'argument corpus "
        f"et les identifiants donnés par le digest (ids, {CQ.QUERY_MAX_IDS} "
        "au plus par appel) ; pour les notices suivantes, corpus_query avec "
        "offset ; pour chercher dans le corpus, corpus_query avec query. Sur "
        "un serveur distant, le retour ne contient pas les notices (liens de "
        "téléchargement seulement) : ne décris que ce que tu as effectivement "
        "lu.\n"
        "- Prudence : un résumé n'est pas le texte intégral : ne prétends "
        "jamais avoir lu un article. Un extrait Google Scholar n'est pas un "
        "résumé. Reprends les réserves du digest (sans résumé, auteurs "
        "tronqués, identité ambiguë). N'invente ni référence, ni DOI, ni "
        "résumé.\n"
        "- Présentation : sépare nettement les résultats bibliographiques "
        "(références, comptes et statuts rendus par les outils) de ton "
        "interprétation (regroupements, tendances, recommandations), "
        "présentée comme telle."
    )


def _prompt_args(arguments) -> dict[str, str]:
    """Arguments d'un prompt : valeurs textuelles non vides, sans espaces de
    bord ; une valeur vide vaut une valeur absente."""
    out = {}
    for key, value in (arguments or {}).items():
        if value is None:
            continue
        text = str(value).strip()
        if text:
            out[key] = text
    return out


@server.list_prompts()
async def list_prompts() -> list[types.Prompt]:
    """Liste les prompts pré-configurés pour les cas d'usage SHS récurrents."""
    return [
        types.Prompt(
            name="bibliographie-thematique",
            description=(
                "Bibliographie thématique : moisson multi-sources sur un "
                "thème (sélection classée par source, pas une collecte "
                "exhaustive), statut et limites de chaque source, analyse "
                "(revues, auteurs, chronologie), puis lectures proposées "
                "d'après les métadonnées et les résumés lus."
            ),
            arguments=[
                types.PromptArgument(
                    name="theme",
                    description=(
                        "Thème ou requête SHS, passé tel quel à "
                        "moisson_search (ex : discours numérique ; des "
                        "guillemets en feraient une expression exacte)"
                    ),
                    required=True,
                ),
                types.PromptArgument(
                    name="max_per_source",
                    description=(
                        "Plafond de notices par source (défaut du serveur : "
                        f"{MCP_DEFAULT_MAX_PER_SOURCE}) ; plus élevé, "
                        "bibliographie plus large et moisson plus longue"
                    ),
                    required=False,
                ),
            ],
        ),
        types.Prompt(
            name="profil-chercheur",
            description=(
                "Profil bibliographique d'un chercheur SHS (search_by_author : "
                "HAL, Isidore, OpenAlex, Semantic Scholar) : homonymes "
                "départagés avec l'utilisateur (candidats, identité choisie, "
                "second appel), statut de chaque source et limites de la "
                "liste rappelées."
            ),
            arguments=[
                types.PromptArgument(
                    name="nom",
                    description=(
                        "Nom complet « Prénom Nom » (ex : Patrick Charaudeau) ; "
                        "facultatif si idhal ou orcid est fourni"
                    ),
                    required=False,
                ),
                types.PromptArgument(
                    name="idhal",
                    description=(
                        "Identifiant HAL de l'auteur (idHal, optionnel) : plus "
                        "précis que le nom pour HAL"
                    ),
                    required=False,
                ),
                types.PromptArgument(
                    name="orcid",
                    description=(
                        "ORCID (optionnel) : interroge HAL et OpenAlex par "
                        "identifiant ; fiable pour OpenAlex, rarement "
                        "renseigné dans HAL"
                    ),
                    required=False,
                ),
            ],
        ),
        types.Prompt(
            name="veille-mensuelle",
            description=(
                "Veille bibliographique sur un thème : nouvelle moisson sur "
                "la période (from_date, to_date), puis comparaison "
                "(corpus_diff) avec le dernier corpus de la même requête "
                "trouvé par corpus_list, en distinguant nouvelles "
                "publications, notices nouvellement récupérées et notices "
                "enrichies, et en rappelant les limites (sources "
                "partielles, fenêtre du fournisseur, mode)."
            ),
            arguments=[
                types.PromptArgument(
                    name="theme",
                    description=(
                        "Thème SHS (requête), passé tel quel, sans guillemets "
                        "ajoutés"
                    ),
                    required=True,
                ),
                types.PromptArgument(
                    name="from_date",
                    description="Date de début AAAA-MM-JJ",
                    required=True,
                ),
                types.PromptArgument(
                    name="to_date",
                    description="Date de fin AAAA-MM-JJ",
                    required=True,
                ),
            ],
        ),
        types.Prompt(
            name="etat-de-l-art",
            description=(
                "État de l'art multilingue sur une question de recherche : "
                "variantes française et anglaise explicites (aucune "
                "traduction automatique), plan de requête montré avant la "
                "moisson, sources francophones et internationales sur les "
                "cinq dernières années, sous-thèmes et publications phares "
                "d'après les métadonnées et les résumés lus."
            ),
            arguments=[
                types.PromptArgument(
                    name="question",
                    description=(
                        "Question de recherche (ex : régulation des "
                        "plateformes numériques), passée telle quelle, sans "
                        "guillemets ajoutés"
                    ),
                    required=True,
                ),
                types.PromptArgument(
                    name="question_en",
                    description=(
                        "Formulation anglaise de la question (variante 'en' "
                        "des sources internationales) ; à défaut, l'agent en "
                        "propose une et la fait valider"
                    ),
                    required=False,
                ),
            ],
        ),
    ]


def _prompt_bibliographie(args: dict[str, str]) -> str:
    """Texte du prompt bibliographie-thematique."""
    theme = args.get("theme")
    mps = args.get("max_per_source")
    cap = mps or str(MCP_DEFAULT_MAX_PER_SOURCE)
    head = ("" if theme else
            f"Aucun thème fourni : {_PROMPT_ASK} le thème (requête) de la "
            "bibliographie, puis suis la démarche ci-dessous.\n\n")
    return (
        head
        + "Construis une bibliographie thématique.\n"
        "Thème (requête, à passer tel quel, sans guillemets ajoutés) : "
        + (theme or "à demander à l'utilisateur") + "\n\n"
        "1. Appelle moisson_search avec query = le thème"
        + (f", max_per_source={mps}" if mps else "")
        + ". Laisse les autres réglages par défaut : sources francophones, "
        f"mode bibliographie (au plus {cap} par source, dans l'ordre natif de "
        "chaque source), cache et parallélisme du serveur. Si le thème "
        "contient des opérateurs (AND, OR, NOT, guillemets, parenthèses), "
        "vérifie d'abord leur traduction par source avec plan_only=true. Si "
        "l'utilisateur veut aussi la littérature internationale, ajoute "
        "sources=[\"all\"] et une variante anglaise explicite, "
        "query_variants={\"en\": <formulation anglaise>}, montrée à "
        "l'utilisateur (le serveur ne traduit rien) ; Google Scholar "
        "seulement à sa demande (source facturée, à nommer : "
        "google_scholar).\n"
        "2. Appelle corpus_analyze sur le corpus Zotero JSON interne produit "
        "(.json, jamais le .csl.json ; chemin dans structuredContent.files en "
        "local, nom du fichier sur un serveur distant).\n"
        "3. Avant de recommander une lecture, lis les résumés des 10 à 20 "
        "premières notices du digest avec corpus_query (argument corpus et "
        "ids donnés par le digest).\n"
        "4. Présente, dans cet ordre :\n"
        "   a. les résultats bibliographiques : statut de chaque source, "
        "nombre de notices, top 20 revues, top 30 auteurs, distribution "
        "chronologique ;\n"
        "   b. les références en tête du classement, avec leurs réserves ;\n"
        "   c. des recommandations de lecture, présentées comme ton "
        "interprétation (fondée sur les métadonnées et les résumés lus).\n"
        "Pour une collecte exhaustive plutôt qu'une bibliographie, propose "
        "mode=\"collecte\", un max_per_source plus élevé et, en local, un "
        "deadline_seconds plus long : la durée croît avec le volume.\n\n"
        + _prompt_rules(_PROMPT_SELECTION_HARVEST)
    )


def _prompt_profil(args: dict[str, str]) -> str:
    """Texte du prompt profil-chercheur (flux d'homonymie d'OPT-14)."""
    nom = args.get("nom")
    idhal = args.get("idhal")
    orcid = args.get("orcid")
    if not (nom or idhal or orcid):
        head = (f"Ni nom ni identifiant fourni : {_PROMPT_ASK} le nom du "
                "chercheur (« Prénom Nom ») ou un identifiant (idHal, ORCID) ; "
                "search_by_author refuse un nom vide sans identifiant.\n\n")
        who = "à demander à l'utilisateur"
        call = ("1. Appelle search_by_author avec name=<Prénom Nom> (name=\"\" "
                "si seul un identifiant est connu), plus idhal=<idHal> ou "
                "orcid=<ORCID> si l'utilisateur les donne (valeurs telles "
                "quelles, sans guillemets ajoutés).")
        note = ("À savoir : sans nom, l'ORCID seul interroge HAL et OpenAlex "
                "par identifiant (Isidore et Semantic Scholar ne sont pas "
                "interrogés) et un idHal seul, HAL seulement.")
    else:
        head = ""
        who = (nom or "(nom non fourni)") \
            + (f" ; idHal : {idhal}" if idhal else "") \
            + (f" ; ORCID : {orcid}" if orcid else "")
        parts = [f"name={nom}" if nom else "name=\"\""]
        if idhal:
            parts.append(f"idhal={idhal}")
        if orcid:
            parts.append(f"orcid={orcid}")
        call = ("1. Appelle search_by_author avec " + ", ".join(parts)
                + " (valeurs telles quelles, sans guillemets ajoutés).")
        # Priorité des identifiants pour HAL : idhal, puis hal_person_id,
        # puis ORCID, puis nom (utils.lookups._build_hal_author_query). Sans
        # nom : HAL et OpenAlex par identifiant seulement ; en mode
        # identifiant, un profil choisi sur le nom et relié à aucun
        # identifiant fourni est mis de côté et proposé à confirmer.
        if not nom and orcid and not idhal:
            note = ("Sans nom, l'ORCID seul interroge HAL et OpenAlex par "
                    "identifiant ; Isidore et Semantic Scholar ne sont pas "
                    "interrogés (aucune recherche par ORCID sans nom) : dis-le "
                    "à l'utilisateur. L'ORCID est fiable dans OpenAlex, "
                    "rarement renseigné dans HAL : un HAL vide ne prouve pas "
                    "l'absence de dépôts (demander l'idHal).")
        elif not nom and orcid:
            note = ("Sans nom, HAL est interrogé par l'idhal et OpenAlex par "
                    "l'ORCID ; Isidore et Semantic Scholar ne sont pas "
                    "interrogés (aucune recherche par ORCID sans nom) : dis-le "
                    "à l'utilisateur.")
        elif not nom:
            note = ("Sans nom, seul HAL est interrogé (par l'idhal) ; "
                    "OpenAlex, Semantic Scholar et Isidore ne le sont pas : "
                    "dis-le à l'utilisateur.")
        elif orcid:
            note = (("HAL est interrogé par l'idhal et OpenAlex par l'ORCID"
                     if idhal else
                     "L'ORCID interroge HAL et OpenAlex par identifiant")
                    + " ; Semantic Scholar et Isidore restent interrogés par "
                    "le nom : un profil Semantic Scholar n'est retenu que s'il "
                    "porte l'ORCID (sinon il est proposé à confirmer) et "
                    "Isidore n'est pas séparé des homonymes.")
        elif idhal:
            note = ("HAL est interrogé par l'idhal ; OpenAlex et Semantic "
                    "Scholar le sont par le nom, mais un profil trouvé ainsi, "
                    "relié à aucun identifiant fourni, est mis de côté et "
                    "proposé à confirmer (profiles_to_confirm) ; Isidore, "
                    "interrogé par le nom, n'est pas séparé des homonymes.")
        else:
            note = ("Recherche par le nom seul : en cas d'homonymie, rien "
                    "n'est moissonné (étape 2).")
    return (
        head
        + "Construis le profil bibliographique d'un chercheur.\n"
        f"Chercheur : {who}\n\n"
        f"{call}\n   {note}\n"
        "2. Homonymie : si le retour l'annonce (harvest_status 'ambiguous', "
        "texte « Homonymie »), aucune notice n'a été retenue ni enregistrée. "
        "Présente les candidats (structuredContent.candidates et texte : "
        "identifiant, affiliation, nombre de travaux, période, exemples de "
        "titres) et les profils trouvés par les autres sources "
        "(profiles_to_confirm), puis demande à l'utilisateur de choisir "
        "l'identité : ne choisis jamais toi-même, et jamais le premier "
        "candidat par défaut. Fais ensuite un second appel : search_by_author "
        "avec le même name et, pour chaque source, l'argument du candidat "
        "choisi (champ argument : idhal ou hal_person_id pour HAL, "
        "openalex_author_id, s2_author_id ; plusieurs identifiants séparés "
        "par des virgules si la même personne a plusieurs profils), y compris "
        "ceux de profiles_to_confirm qui désignent la même personne.\n"
        "3. Hors homonymie, lis aussi profiles_to_confirm : profils trouvés "
        "par le nom mais non reliés aux identifiants fournis. Propose-les à "
        "l'utilisateur et ne les ajoute qu'après sa confirmation (nouvel "
        "appel avec leur argument).\n"
        "4. Complétude : ne présente jamais la liste comme la « production "
        "complète » du chercheur. production_complete vrai signifie "
        "seulement que les sources interrogées ont été parcourues jusqu'au "
        "bout ; la production réelle peut les dépasser (ouvrages, chapitres, "
        "travaux non indexés). Reprends les réserves du retour (caveats) : "
        "plafond atteint (max_per_source, 200 par défaut : relancer plus haut "
        "si c'est signalé), source en panne ou non exécutée (OpenAlex sans "
        "clé), notices Isidore écartées faute de porter le nom "
        "(rejected_not_author), Isidore non séparé des homonymes.\n"
        "5. Présente la production (digest par année décroissante ; suite et "
        "résumés par corpus_query) en sections par décennie ou par "
        "thématique ; corpus_analyze sur le corpus produit donne les revues "
        "et les coauteurs récurrents. Des phases de carrière (thématiques, "
        "méthodes, supports) relèvent de l'interprétation : présente-les "
        "comme telles.\n\n"
        + _prompt_rules(_PROMPT_SELECTION_AUTHOR)
    )


def _prompt_veille(args: dict[str, str]) -> str:
    """Texte du prompt veille-mensuelle (OPT-19 : corpus_list, moisson,
    corpus_diff ; harmonisé par OPT-20)."""
    # Le thème est écrit sans guillemets : entre guillemets, la requête
    # deviendrait une expression exacte (OPT-11, constat 4).
    theme = args.get("theme")
    fd = args.get("from_date")
    td = args.get("to_date")
    missing = []
    if not theme:
        missing.append("le thème (requête)")
    if not fd or not td:
        missing.append("la période (dates de début et de fin, AAAA-MM-JJ ; "
                       "par exemple le mois écoulé)")
    head = (f"Argument manquant : {_PROMPT_ASK} {' et '.join(missing)}, puis "
            "suis la démarche ci-dessous.\n\n" if missing else "")
    period = (f"entre {fd} et {td}" if fd and td else
              f"sur la période {fd or '(début à préciser)'} – "
              f"{td or '(fin à préciser)'}")
    dates = ((f"from_date={fd}" if fd else "from_date = date de début choisie")
             + ", "
             + (f"to_date={td}" if td else "to_date = date de fin choisie"))
    theme_text = theme or "à demander à l'utilisateur"
    return (
        head
        + f"Effectue une veille bibliographique sur le thème suivant, "
        f"{period}.\n"
        f"Thème (requête, à passer tel quel, sans guillemets ajoutés) : "
        f"{theme_text}\n\n"
        "1. Corpus de référence, AVANT la nouvelle moisson : appelle "
        "corpus_list avec pattern égal au thème (le filtre ignore la "
        "casse et les accents ; ajoute since, par exemple l'année "
        "précédente, si la liste est longue). La liste va du plus "
        "récent au plus ancien : retiens le premier corpus dont la "
        "requête est exactement le thème, de type moisson et au statut "
        "complet. À défaut, retiens le plus récent corpus au statut "
        "« ancien (métadonnées calculées) » (produit avant les "
        "manifestes) dont la « Requête (d'après le nom) » correspond au "
        "thème, casse, accents et ponctuation ignorés (le nom tronque "
        "une requête longue à 60 caractères) ; son mode et ses sources "
        "sont inconnus : corpus_diff le signalera, rappelle-le dans les "
        "limites. Note sa date et le nom de son fichier Zotero JSON "
        "interne (.json, jamais le .csl.json). S'il n'y en a aucun, "
        "signale qu'aucune comparaison n'est possible et limite la "
        "veille aux étapes 2 et 4.\n"
        f"2. Appelle moisson_search avec query = le thème, {dates}. Si tu "
        "connais les sources et le mode du corpus de référence (veille "
        "précédente), reprends-les ; à défaut, sources par défaut et "
        "mode=\"collecte\" (ordre déterministe, plus stable d'une veille à "
        "l'autre). En mode collecte, une source plafonnée garde ses plus "
        "anciens dépôts (HAL) et peut écarter les nouveautés : si le retour "
        "signale un plafond atteint, relance avec un max_per_source plus "
        "élevé. corpus_diff rappellera le mode et les sources des deux "
        "moissons.\n"
        "3. Appelle corpus_diff avec corpus_a = le corpus de référence "
        "et corpus_b = le corpus Zotero JSON interne produit à l'étape "
        "2 (noms de fichiers tels que listés, ou chemins).\n"
        "4. Restitue, en séparant nettement les résultats bibliographiques "
        "de ton interprétation. Avant de commenter une référence, lis sa "
        "notice complète (résumé compris) avec corpus_query : corpus = le "
        "corpus produit à l'étape 2, ids = identifiants donnés par "
        f"corpus_diff ({CQ.QUERY_MAX_IDS} au plus par appel).\n"
        "   a. les nouvelles publications (datées après la moisson de "
        "référence) et les nouveaux dépôts HAL : 5 à 10 références "
        "prioritaires, avec date et source ;\n"
        "   b. les notices nouvellement récupérées (publiées avant la "
        "moisson de référence) : ce ne sont pas des nouveautés, mais "
        "des notices que les sources rendent désormais ;\n"
        "   c. les notices enrichies (DOI ou résumé ajouté, auteurs "
        "complétés) : même travail, métadonnées meilleures ;\n"
        "   d. les retraits : ne les présente pas comme des "
        "suppressions ; donne le motif indiqué (hors de la fenêtre de "
        "dates, source en échec ou plafonnée, sélection). Quand les "
        "fenêtres de dates des deux moissons diffèrent (cas ordinaire "
        "d'une veille sur une nouvelle période), les retraits hors "
        "fenêtre sont attendus : donne-en seulement le nombre ;\n"
        "   e. une synthèse de trois paragraphes au plus, présentée comme "
        "ton interprétation : ce qui est nouveau, qui publie, où ça se "
        "publie.\n"
        "5. Rappelle les limites : sources partielles ou en échec "
        "(statuts de moisson_search, avertissements de corpus_diff), "
        "fenêtre de résultats du fournisseur et plafond par source, "
        "mode de chaque moisson (en mode bibliographie, un changement "
        "de classement chez le fournisseur fait entrer ou sortir des "
        "notices sans nouvelle publication), dates imprécises (année "
        "seule : classement indéterminé). Sur un serveur distant, "
        "corpus_diff ne rend que des identifiants : les titres sont dans "
        "le rapport téléchargeable (ne les invente pas).\n\n"
        + _prompt_rules(_PROMPT_SELECTION_HARVEST)
    )


def _prompt_etat_de_l_art(args: dict[str, str]) -> str:
    """Texte du prompt etat-de-l-art : variantes fr et en explicites (aucune
    traduction cachée), plan montré avant la moisson."""
    from datetime import datetime as _dt

    question = args.get("question")
    question_en = args.get("question_en")
    since = _dt.now().year - 5
    head = ("" if question else
            f"Aucune question fournie : {_PROMPT_ASK} la question de "
            "recherche, puis suis la démarche ci-dessous.\n\n")
    english = (
        "Formulation anglaise fournie (variante « en », à passer telle "
        f"quelle) : {question_en}\n" if question_en else
        "Formulation anglaise : non fournie.\n")
    propose = ("" if question_en else
               "Propose une formulation anglaise fidèle (termes du domaine), "
               "montre-la à l'utilisateur et attends son accord. ")
    return (
        head
        + "Construis un état de l'art SHS multilingue sur les cinq dernières "
        f"années (depuis {since}).\n"
        "Question (requête française, à passer telle quelle, sans guillemets "
        "ajoutés) : " + (question or "à demander à l'utilisateur") + "\n"
        + english + "\n"
        "1. Variantes de requête : les sources internationales (CrossRef, "
        "OpenAlex, Semantic Scholar, CORE) indexent surtout des travaux en "
        "anglais, et le serveur ne traduit rien. " + propose
        + "Passe les deux variantes explicitement : query_variants={\"fr\": "
        "<la question>, \"en\": <la formulation anglaise>}.\n"
        "2. Appelle moisson_search avec query = la question, ces "
        f"query_variants, sources=[\"all\"] (francophones et internationales, "
        f"sans Google Scholar), from_date={since} et plan_only=true : montre "
        "à l'utilisateur la requête envoyée à chaque source "
        "(structuredContent.query_plan, plan complet : variante reçue, sac de "
        "mots de CrossRef, filtres natifs ou locaux, avertissements) et note "
        "l'ordre appliqué par chaque source (ordering), absent du plan résumé "
        "de la moisson.\n"
        "3. Relance le même appel sans plan_only. Garde max_per_source par "
        f"défaut ({MCP_DEFAULT_MAX_PER_SOURCE} par source) sauf demande "
        "contraire (la durée croît avec le volume ; en local, relever alors "
        "deadline_seconds). OpenAlex et CORE exigent une clé d'API : sans "
        "clé, ces sources sont « non exécutées » et la moisson continue ; "
        "sans clé, Semantic Scholar partage un quota public (erreurs 429 "
        "possibles). N'ajoute Google Scholar (sources=[\"all\", "
        "\"google_scholar\"]) qu'à la demande de l'utilisateur : source "
        "facturée à la requête.\n"
        "4. Appelle corpus_analyze sur le corpus Zotero JSON interne produit "
        "(.json ; chemin dans structuredContent.files en local, nom du "
        "fichier sur un serveur distant) : répartition par source, type, "
        "langue et année, revues et auteurs principaux ; les recouvrements "
        "entre paires de sources sont dans le fichier de statistiques "
        "(_stats.txt) de la moisson.\n"
        "5. Identifie 3 à 5 sous-thèmes (interprétation) ; pour chacun, "
        "cherche les notices avec corpus_query (query = termes du sous-thème, "
        "SANS filtre de langue, en demandant le champ language : "
        "fields=[\"id\", \"title\", \"authors\", \"year\", \"venue\", "
        "\"language\", \"sources\"]). Le filtre languages ne trancherait "
        "pas : il garde dans chaque passe les notices sans langue, dont "
        "toutes celles venues seulement de "
        f"{_labels_without_language('ou')}, qui n'en portent pas. Classe "
        "ensuite les candidates d'après leur champ language : français, "
        "anglais, ou langue non indiquée (ne la devine pas). Lis les résumés "
        "des candidates avec corpus_query (ids) avant de les retenir.\n"
        "6. Pour chaque sous-thème, retiens 5 à 10 publications phares en "
        "distinguant travaux francophones et anglophones, et donne la langue "
        "de chaque référence telle que la notice l'indique (« langue non "
        "indiquée » sinon, groupe à part). Les citations (OpenAlex, Semantic "
        "Scholar) "
        "forment une dimension à part : elles ne mesurent pas la pertinence ; "
        "en mode bibliographie, Semantic Scholar trie par citations et "
        "favorise les travaux anciens et très cités.\n\n"
        + _prompt_rules(_PROMPT_SELECTION_HARVEST)
    )


_PROMPT_BUILDERS = {
    "bibliographie-thematique": _prompt_bibliographie,
    "profil-chercheur": _prompt_profil,
    "veille-mensuelle": _prompt_veille,
    "etat-de-l-art": _prompt_etat_de_l_art,
}


@server.get_prompt()
async def get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
    """Génère le texte du prompt pour un cas d'usage. Arguments absents ou
    vides : le texte demande d'abord à l'utilisateur les arguments
    obligatoires."""
    builder = _PROMPT_BUILDERS.get(name)
    text = builder(_prompt_args(arguments)) if builder else \
        f"Prompt inconnu : {name}"

    return types.GetPromptResult(
        description=f"Workflow {name}",
        messages=[
            types.PromptMessage(
                role="user",
                content=types.TextContent(type="text", text=text),
            )
        ],
    )


# === Registre des instances (PAT-10) ===

def _register_instance(transport: str) -> None:
    """Inscrit l'instance dans ``OUTPUT_DIR/.instances`` et signale les
    autres instances vivantes dont le code diffère (ancienne instance restée
    ouverte après une mise à jour)."""
    try:
        from utils.instances import register, stale_warnings
        register(OUTPUT_DIR, transport)
        for warning in stale_warnings(OUTPUT_DIR):
            logger.warning(warning)
    except Exception as exc:  # noqa: BLE001 - le registre n'arrête jamais le serveur
        logger.debug(f"Registre des instances indisponible : {exc}")


# === Transport STDIO (défaut, Claude Desktop local) ===

async def run_stdio():
    """
    Démarre le serveur en mode stdio (transport par défaut).

    Le client MCP (Claude Desktop) communique avec ce processus
    via stdin/stdout en utilisant le protocole JSON-RPC. Comportement
    historique inchangé (Sprint 05).
    """
    logger.info("Démarrage serveur MCP moisson-shs (stdio) : %s ; PID %d",
                describe_build(), os.getpid())
    logger.info(f"OUTPUT_DIR = {OUTPUT_DIR}")
    _register_instance("stdio")

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


# Alias rétro-compatible : `from mcp_server import main` (cli.py historique,
# `python mcp_server.py`). Pointe sur le transport stdio.
main = run_stdio


# === Transport HTTP distant (Streamable HTTP) ===

async def _send_plain(send, status: int, body: str) -> None:
    """Émet une réponse texte minimale en ASGI brut (pour les middlewares)."""
    payload = body.encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(payload)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": payload})


class _BodyTooLarge(Exception):
    """Sentinelle interne : corps de requête au-delà de la limite (chunked)."""


class ContentLengthLimitMiddleware:
    """
    Middleware ASGI **pur** : rejette (413) un corps de requête trop gros.

    Deux gardes complémentaires (correctif revue — bypass `Transfer-Encoding:
    chunked`) :
    1. **Rapide** : si l'en-tête `Content-Length` annoncé dépasse `max_body` → 413
       immédiat (sans lire le corps).
    2. **Robuste** : pour les requêtes à corps (POST/PUT/PATCH) **sans**
       Content-Length (chunked), on enveloppe `receive` pour **compter les octets
       réellement reçus** et rejeter (413) au-delà de `max_body` — sinon le SDK
       lit tout le corps en mémoire sans borne.

    Ne bufferise jamais le corps (compte au fil de l'eau) → ne casse pas le flux
    SSE des réponses (GET). Monté en tête de pile (avant l'auth) → s'applique à
    toute requête. La limite de prod doit AUSSI être posée au reverse proxy
    (Caddy `request_body { max_size }`), point d'entrée réel.
    """

    def __init__(self, app, max_body: int):
        self.app = app
        self.max_body = max_body

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 1) Garde rapide sur Content-Length annoncé.
        for k, v in scope.get("headers", []):
            if k == b"content-length":
                try:
                    if int(v) > self.max_body:
                        await _send_plain(send, 413, "Payload Too Large")
                        return
                except ValueError:
                    pass
                break

        # 2) Garde robuste (chunked) : compter les octets pour les requêtes à corps.
        if scope.get("method", "GET") in ("POST", "PUT", "PATCH"):
            received = 0
            started = False
            max_body = self.max_body

            async def counting_receive():
                nonlocal received
                msg = await receive()
                if msg.get("type") == "http.request":
                    received += len(msg.get("body", b""))
                    if received > max_body:
                        raise _BodyTooLarge()
                return msg

            async def tracking_send(message):
                nonlocal started
                if message.get("type") == "http.response.start":
                    started = True
                await send(message)

            try:
                await self.app(scope, counting_receive, tracking_send)
            except _BodyTooLarge:
                if not started:   # n'émettre 413 que si rien n'a déjà été envoyé
                    await _send_plain(send, 413, "Payload Too Large")
            return

        await self.app(scope, receive, send)


class RateLimitMiddleware:
    """
    Middleware ASGI **pur** : token-bucket par IP cliente → 429 + `Retry-After`.

    ⚠️ Garde-fou de DERNIER recours : derrière un reverse proxy, toutes les
    requêtes portent l'IP du proxy (préférer le rate-limit AU proxy). État en
    mémoire (cohérent `workers=1`). `per_min` = capacité du seau et débit de
    recharge par minute.

    Sans horloge utilisable au niveau module (les scripts de test figent le
    temps), on s'appuie sur `time.monotonic()` au runtime — jamais à l'import.
    """

    def __init__(self, app, per_min: int):
        self.app = app
        self.capacity = float(per_min)
        self.refill_per_sec = per_min / 60.0
        self._buckets: dict[str, tuple[float, float]] = {}  # ip -> (tokens, ts)
        # Borne mémoire (correctif revue) : au-delà, on purge les seaux PLEINS
        # (IPs inactives depuis assez longtemps pour avoir tout rechargé — une
        # entrée pleine est équivalente à une entrée absente).
        self._max_buckets = 10000

    def _purge_full(self, now: float) -> None:
        full_after = (self.capacity / self.refill_per_sec
                      if self.refill_per_sec else 0.0)
        stale = [k for k, (_, ts) in self._buckets.items()
                 if now - ts >= full_after]
        for k in stale:
            del self._buckets[k]

    def _allow(self, ip: str) -> bool:
        import time as _time
        now = _time.monotonic()
        if len(self._buckets) > self._max_buckets:
            self._purge_full(now)
        tokens, ts = self._buckets.get(ip, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - ts) * self.refill_per_sec)
        if tokens < 1.0:
            self._buckets[ip] = (tokens, now)
            return False
        self._buckets[ip] = (tokens - 1.0, now)
        return True

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            client = scope.get("client")
            ip = client[0] if client else "unknown"
            if not self._allow(ip):
                payload = b"Too Many Requests"
                await send({
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"content-type", b"text/plain; charset=utf-8"),
                        (b"content-length", str(len(payload)).encode()),
                        (b"retry-after", b"60"),
                    ],
                })
                await send({"type": "http.response.body", "body": payload})
                return
        await self.app(scope, receive, send)


class _MCPASGIApp:
    """
    Enveloppe ASGI (classe-instance) autour de
    ``StreamableHTTPSessionManager.handle_request``.

    ⚠️ Indispensable : un endpoint Starlette défini par une **fonction** n'est
    routé que pour GET ; une **instance ASGI appelable** est routée pour toutes
    les méthodes (POST/GET/DELETE), ce qu'exige le protocole Streamable HTTP.
    C'est exactement le patron de ``FastMCP.streamable_http_app`` répliqué à la
    main autour de notre ``Server`` bas-niveau existant.
    """

    def __init__(self, session_manager):
        self._sm = session_manager

    async def __call__(self, scope, receive, send) -> None:
        await self._sm.handle_request(scope, receive, send)


def _default_loopback_hosts(host: str, port: int) -> list[str]:
    """
    Allowlist Host par défaut quand le bind est loopback.

    Sur 127.0.0.1/localhost, on autorise ces deux hôtes (avec wildcard de port
    ``host:*``, supporté par le SDK) pour que le client de test local passe
    l'anti DNS-rebinding sans configuration. Sur un bind non-loopback, on ne
    devine RIEN : l'opérateur DOIT fournir MOISSON_ALLOWED_HOSTS (cf.
    ``serve_http``), sinon refus de démarrer.
    """
    return ["127.0.0.1:*", "localhost:*"]


# Extensions téléchargeables via /download (whitelist stricte). Ordre important
# pour le test de suffixe : `.csl.json` avant `.json` (endswith). Source
# unique : `utils.output_files.PUBLISHABLE_SUFFIXES` (même liste blanche pour
# read_resource en HTTP).
_ALLOWED_DOWNLOAD_SUFFIXES = PUBLISHABLE_SUFFIXES


async def _download_endpoint(request):
    """
    Sert un fichier de corpus en téléchargement (mode HTTP distant).

    Sécurité (correctifs revue critiques) :
    - rejet dur de tout nom contenant un séparateur / `..` (`name == Path(name).name`)
      ou un caractère de contrôle (403) ;
    - **whitelist d'extensions** (`.json`, `.csl.json`, `_stats.txt`, et les
      exports `.bib` / `.ris` de ``corpus_export``, servis avec leur type
      MIME) ;
    - **fichiers de service jamais servis** (OPT-02) : nom commençant par un
      point (`.index.json`…) ou cible résolue sous un dossier caché de
      OUTPUT_DIR (lien symbolique vers `.oauth/`, `.audit/`) → 404, comme un
      fichier absent ;
    - **fichiers associés servis** (`.manifest.json`, `.doi_report.json`) :
      même sensibilité que le corpus qu'ils décrivent, seul accès distant à la
      traçabilité de la moisson (cf. `utils.output_files.is_downloadable_file`) ;
    - **confinement DUR sous OUTPUT_DIR** — PAS de repli `_EMITTED_RESOURCES`
      (qui peut pointer hors zone en stdio) ; en HTTP, le Sprint 07 garantit
      qu'aucun outil n'écrit hors OUTPUT_DIR ;
    - `X-Content-Type-Options: nosniff`.

    Protégée par l'auth Bearer (la route est enveloppée dans
    `RequireAuthMiddleware` lors du montage).
    """
    from starlette.responses import FileResponse, PlainTextResponse

    name = request.path_params["filename"]
    # Rejet dur des séparateurs, de la traversal et des caractères de contrôle
    # (un NUL faisait lever `resolve()` : erreur 500 au lieu d'un refus).
    if name != Path(name).name or re.search(r"[\\\x00-\x1f\x7f]", name):
        return PlainTextResponse("forbidden", status_code=403)
    if not is_downloadable_file(name):
        return PlainTextResponse("not found", status_code=404)

    base = OUTPUT_DIR.resolve()
    target = (base / name)
    # Confinement strict (défense en profondeur) + existence + cible résolue
    # ni fichier de service, ni hors liste blanche (lien symbolique interne).
    resolved = target.resolve()
    if (not resolved.is_relative_to(base) or not target.is_file()
            or _is_output_service_file(target)
            or not is_downloadable_file(resolved)):
        return PlainTextResponse("not found", status_code=404)

    media = export_media_type(name) or (
        "text/plain" if name.endswith(".txt") else "application/json")
    return FileResponse(
        target, media_type=media, filename=name,
        headers={"X-Content-Type-Options": "nosniff"},
    )


def build_http_app(
    *,
    path: str = "/mcp",
    json_response: bool = False,
    stateless: bool = False,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
    dns_rebinding_protection: bool = True,
    token_verifier=None,
    max_body: int | None = None,
    rate_limit_per_min: int = 0,
    oauth_provider=None,
    public_base_url: str = "",
):
    """
    Construit l'application **Starlette** servant le `Server` MCP existant sur
    le transport **Streamable HTTP**, via ``StreamableHTTPSessionManager``.

    C'est la SEULE fabrique d'app HTTP du projet (signature stable par
    mots-clés) : les sprints ultérieurs l'**enrichissent** (``/download``,
    ``/metrics``) au lieu d'en créer des variantes divergentes.

    Le **même** objet ``server`` alimente stdio et HTTP → comportement d'outils
    rigoureusement identique sur les deux transports (zéro réécriture).

    Auth (Sprint 07) : si ``token_verifier`` est fourni, l'app monte
    ``AuthenticationMiddleware(BearerAuthBackend(verifier))`` + ``AuthContextMiddleware``
    (niveau app) et enveloppe la route ``/mcp`` dans ``RequireAuthMiddleware``
    (401 sans/avec mauvais jeton). ``/healthz`` reste **public**. Sans verifier
    (tests bas niveau / mode socle), aucune auth — exactement le comportement
    Sprint 06.

    Args:
        path: Chemin de la route Streamable (défaut ``/mcp``).
        json_response: ``True`` ⇒ réponses JSON simples (désactive le SSE).
        stateless: ``True`` ⇒ pas de sessions persistantes (désactive le SSE
            et la progression — déconseillé).
        allowed_hosts: Allowlist Host (anti DNS-rebinding). ``[]`` = aucun
            autorisé (tout Host rejeté) quand la protection est active.
        allowed_origins: Allowlist Origin.
        dns_rebinding_protection: Active la validation Host/Origin du SDK.
        token_verifier: ``TokenVerifier`` du SDK (ex. ``StaticTokenVerifier``).
            ``None`` ⇒ pas d'auth (socle Sprint 06).
        max_body: Si fourni, monte ``ContentLengthLimitMiddleware`` (413 au-delà).
        rate_limit_per_min: Si > 0, monte ``RateLimitMiddleware`` (429).

    Returns:
        Une instance ``starlette.applications.Starlette`` prête pour uvicorn.
    """
    import contextlib

    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.authentication import AuthenticationMiddleware
    from starlette.routing import Route, request_response
    from starlette.responses import JSONResponse
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings
    from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
    from mcp.server.auth.middleware.bearer_auth import (
        BearerAuthBackend, RequireAuthMiddleware,
    )

    security_settings = TransportSecuritySettings(
        enable_dns_rebinding_protection=dns_rebinding_protection,
        allowed_hosts=allowed_hosts or [],
        allowed_origins=allowed_origins or [],
    )

    session_manager = StreamableHTTPSessionManager(
        app=server,                      # ← le Server EXISTANT (8 outils inchangés)
        json_response=json_response,
        stateless=stateless,
        security_settings=security_settings,
    )

    async def healthz(_request):
        # Route PUBLIQUE : statut + métadonnées NON sensibles (aucun chemin
        # disque, aucun secret). Requise par le reverse proxy / le HEALTHCHECK
        # Docker. Enrichie au Sprint 11 (version/mode/uptime/writable).
        try:
            writable = os.access(OUTPUT_DIR, os.W_OK)
        except Exception:
            writable = False
        from utils.build_info import build_info
        return JSONResponse({
            "status": "ok",
            "version": _get_version_string(),
            # Commit du code (PAT-03) : public comme le dépôt, jamais un chemin.
            "commit": build_info().get("commit"),
            "mode": TRANSPORT_MODE,
            "uptime_s": round(time.monotonic() - _START_MONOTONIC, 1),
            "output_dir_writable": writable,
        })

    async def metrics(_request):
        # Révèle l'activité/surface → DERRIÈRE l'auth (cf. montage ci-dessous).
        return JSONResponse(_metrics_snapshot())

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        # OBLIGATOIRE : `session_manager.run()` est un @asynccontextmanager
        # appelable UNE SEULE FOIS par instance ; sans lui, handle_request lève
        # RuntimeError("Task group is not initialized. Make sure to use run().").
        async with session_manager.run():
            logger.info("Gestionnaire de sessions Streamable HTTP démarré")
            yield
            logger.info("Arrêt du gestionnaire de sessions Streamable HTTP")

    mcp_asgi = _MCPASGIApp(session_manager)

    # === OAuth 2.1 (Sprint 12) : routes serveur d'autorisation + métadonnées ===
    # Le SDK gère TOUT le protocole sensible (PKCE, échange de code, DCR). On
    # monte ses routes + une page de consentement, et on signale aux clients
    # (claude.ai) l'URL de métadonnées via WWW-Authenticate (resource_metadata).
    oauth_routes = []
    resource_metadata_url = None
    if oauth_provider is not None and public_base_url:
        from pydantic import AnyHttpUrl
        from starlette.responses import HTMLResponse, RedirectResponse
        from mcp.server.auth.routes import (
            create_auth_routes, create_protected_resource_routes,
            build_resource_metadata_url,
        )
        from mcp.server.auth.settings import (
            ClientRegistrationOptions, RevocationOptions,
        )
        from oauth_provider import ConsentError

        issuer = AnyHttpUrl(public_base_url.rstrip("/"))
        resource = AnyHttpUrl(f"{public_base_url.rstrip('/')}{path}")
        resource_metadata_url = build_resource_metadata_url(resource)

        oauth_routes += create_auth_routes(
            provider=oauth_provider,
            issuer_url=issuer,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        )
        oauth_routes += create_protected_resource_routes(
            resource_url=resource, authorization_servers=[issuer],
        )

        async def consent_get(request):
            return HTMLResponse(
                oauth_provider.consent_page_html(request.query_params.get("rid", "")))

        async def consent_post(request):
            form = await request.form()
            rid = form.get("rid", "")
            try:
                url = oauth_provider.approve_consent(rid, form.get("password", ""))
            except ConsentError as e:
                page = oauth_provider.consent_page_html(rid)
                page += f"<p style='color:#c00'>{e}</p>"
                return HTMLResponse(page, status_code=403)
            return RedirectResponse(url, status_code=302)

        oauth_routes.append(Route("/oauth/consent", consent_get, methods=["GET"]))
        oauth_routes.append(Route("/oauth/consent", consent_post, methods=["POST"]))

    # === Route /mcp : protégée par auth si un verifier est fourni ===
    if token_verifier is not None:
        # RequireAuthMiddleware lit scope['user'] peuplé par AuthenticationMiddleware
        # (ordre CRITIQUE : Authentication AVANT, sinon 401 systématique). On
        # enveloppe l'instance ASGI → toutes méthodes routées. resource_metadata_url
        # (si OAuth) → 401 advertise l'endpoint de métadonnées pour claude.ai.
        mcp_endpoint = RequireAuthMiddleware(
            mcp_asgi, required_scopes=[],
            resource_metadata_url=resource_metadata_url)
    else:
        mcp_endpoint = mcp_asgi

    routes = [
        Route(path, endpoint=mcp_endpoint),
        Route("/healthz", endpoint=healthz, methods=["GET"]),
    ]
    # Routes OAuth (publiques : authorize/token/register/metadata/consent).
    routes += oauth_routes

    # === Route /download/{filename} : remote-only, authentifiée ===
    # ⚠️ endpoint FONCTION → `request_response(...)` pour le convertir en ASGI,
    # SINON l'enveloppe RequireAuthMiddleware ne route pas correctement
    # (correctif revue critique). Présente uniquement quand l'auth est active.
    if token_verifier is not None:
        download_asgi = request_response(_download_endpoint)
        routes.append(Route(
            f"{DOWNLOAD_PREFIX}/{{filename}}",
            endpoint=RequireAuthMiddleware(
                download_asgi, required_scopes=[],
                resource_metadata_url=resource_metadata_url),
            methods=["GET"],
        ))
        # /metrics : DERRIÈRE l'auth (révèle l'activité). Même patron ASGI.
        routes.append(Route(
            "/metrics",
            endpoint=RequireAuthMiddleware(
                request_response(metrics), required_scopes=[],
                resource_metadata_url=resource_metadata_url),
            methods=["GET"],
        ))

    # === Pile de middlewares app-level (premier = le plus EXTERNE) ===
    # Ordre : limites/quotas d'abord (rejet le moins cher), puis auth.
    middleware: list = []
    if max_body is not None:
        middleware.append(Middleware(ContentLengthLimitMiddleware, max_body=max_body))
    if rate_limit_per_min > 0:
        middleware.append(Middleware(RateLimitMiddleware, per_min=rate_limit_per_min))
    if token_verifier is not None:
        middleware.append(Middleware(
            AuthenticationMiddleware,
            backend=BearerAuthBackend(token_verifier),
        ))
        middleware.append(Middleware(AuthContextMiddleware))

    return Starlette(routes=routes, lifespan=lifespan, middleware=middleware or None)


def serve_http(
    host: str | None = None,
    port: int | None = None,
    *,
    path: str | None = None,
    json_response: bool | None = None,
    stateless: bool | None = None,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
    public_base_url: str | None = None,
):
    """
    Lance le serveur MCP en mode **HTTP distant** (Streamable HTTP) avec uvicorn.

    Pose le mode de transport process-level (``TRANSPORT_MODE = "http"``) et
    ``PUBLIC_BASE_URL`` (pour fabriquer les URLs de téléchargement), applique le
    garde-fou d'exposition (refus si bind public sans allowlist Host) et le
    garde-fou auth (refus sans jeton).

    Args:
        host, port, path, json_response, stateless: défauts config.py (env
            MOISSON_HTTP_*) si non fournis.
        allowed_hosts, allowed_origins: allowlists Host/Origin.
        public_base_url: base HTTPS publique pour les URLs de téléchargement.
            Défaut config.PUBLIC_BASE_URL (env MOISSON_PUBLIC_BASE_URL).
    """
    global TRANSPORT_MODE, PUBLIC_BASE_URL

    import uvicorn

    host = host if host is not None else HTTP_HOST
    port = port if port is not None else HTTP_PORT
    path = path if path is not None else HTTP_PATH
    json_response = HTTP_JSON_RESPONSE if json_response is None else json_response
    stateless = HTTP_STATELESS if stateless is None else stateless
    allowed_hosts = list(allowed_hosts) if allowed_hosts else list(ALLOWED_HOSTS)
    allowed_origins = list(allowed_origins) if allowed_origins else list(ALLOWED_ORIGINS)

    loopback = host in ("127.0.0.1", "localhost", "::1")

    # Garde-fou d'exposition (correctif revue critique 06) : pas de bind public
    # sans allowlist Host explicite.
    if not loopback and not allowed_hosts:
        sys.exit(
            f"[moisson-shs serve --http] REFUS : bind non-loopback ({host}) sans "
            f"MOISSON_ALLOWED_HOSTS (ni --allowed-host). Exposer un serveur sans "
            f"allowlist Host ouvre une faille DNS-rebinding. Fournissez la liste "
            f"des hôtes publics autorisés (ex. votre domaine) puis relancez."
        )

    # Sur bind loopback sans allowlist explicite : autoriser localhost par défaut
    # (confort du dev local + client de test). Anodin (jamais exposé au réseau).
    if loopback and not allowed_hosts:
        allowed_hosts = _default_loopback_hosts(host, port)

    # === Base URL publique (Sprint 08, requise par OAuth/Sprint 12) ===
    base_url = (public_base_url
                if public_base_url is not None else _CONFIG_PUBLIC_BASE_URL)
    base_url = (base_url or "").strip().rstrip("/")
    if base_url and not base_url.startswith("https://"):
        logger.warning(
            "MOISSON_PUBLIC_BASE_URL='%s' n'est pas en https:// — les URLs de "
            "téléchargement risquent d'être incorrectes derrière le proxy TLS.",
            base_url,
        )
    PUBLIC_BASE_URL = base_url

    # === Auth : Bearer statique (Claude Desktop/Code) + OAuth optionnel (claude.ai) ===
    from auth import StaticTokenVerifier, CompositeTokenVerifier
    static_verifier = StaticTokenVerifier()

    oauth_provider = None
    if OAUTH_ENABLED:
        if not OAUTH_PASSWORD:
            sys.exit(
                "[serve --http] REFUS : MOISSON_OAUTH_ENABLED=1 sans "
                "MOISSON_OAUTH_PASSWORD (la page de consentement DOIT être "
                "protégée). Définissez un mot de passe d'autorisation."
            )
        if not base_url:
            sys.exit(
                "[serve --http] REFUS : OAuth nécessite MOISSON_PUBLIC_BASE_URL "
                "(issuer/endpoints). Renseignez l'URL HTTPS publique."
            )
        from oauth_provider import FileOAuthProvider
        from mcp.server.auth.provider import ProviderTokenVerifier
        store = OUTPUT_DIR / ".oauth" / "oauth_store.json"
        oauth_provider = FileOAuthProvider(
            base_url=base_url, resource_url=f"{base_url}{path}",
            password=OAUTH_PASSWORD, store_path=store,
        )
        oauth_verifier = ProviderTokenVerifier(oauth_provider)
    else:
        oauth_verifier = None

    verifiers = []
    if static_verifier.has_tokens():
        verifiers.append(static_verifier)
    if oauth_verifier is not None:
        verifiers.append(oauth_verifier)
    if not verifiers:
        sys.exit(
            "[serve --http] REFUS : aucun moyen d'authentification. Définissez "
            "MOISSON_API_TOKEN (Bearer) et/ou activez OAuth "
            "(MOISSON_OAUTH_ENABLED=1 + MOISSON_OAUTH_PASSWORD)."
        )
    token_verifier = CompositeTokenVerifier(verifiers)

    TRANSPORT_MODE = "http"

    # Logging structuré JSON sur stderr (observabilité prod, Sprint 11).
    configure_json_logging()

    logger.info("Démarrage serveur MCP moisson-shs (HTTP / Streamable HTTP) : "
                "%s ; PID %d", describe_build(), os.getpid())
    logger.info(f"OUTPUT_DIR = {OUTPUT_DIR}")
    _register_instance("http")
    logger.info(f"Écoute uvicorn : http://{host}:{port}{path}")
    logger.info(f"Allowed hosts : {allowed_hosts or '(aucun)'}")
    logger.info(f"Base URL publique : {PUBLIC_BASE_URL or '(relative)'}")
    logger.info("Bearer statique : %s | OAuth : %s",
                "ACTIF" if static_verifier.has_tokens() else "inactif",
                "ACTIF" if oauth_provider is not None else "inactif")

    app = build_http_app(
        path=path,
        json_response=json_response,
        stateless=stateless,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        token_verifier=token_verifier,
        max_body=HTTP_MAX_BODY,
        rate_limit_per_min=HTTP_RATE_LIMIT_PER_MIN,
        oauth_provider=oauth_provider,
        public_base_url=base_url,
    )
    # log_config=None : les logs uvicorn restent sur la config racine (stderr).
    uvicorn.run(app, host=host, port=port, log_config=None)


if __name__ == "__main__":
    asyncio.run(run_stdio())
