"""
Serveur MCP (Model Context Protocol) pour le moissonneur SHS.

Ce serveur expose les fonctionnalités du moissonneur multi-sources
sous forme d'outils invocables par Claude Desktop (ou tout client
MCP compatible).

Architecture :
- Le serveur s'exécute en local sur le poste de l'utilisateur,
  hors du sandbox Claude. Il dispose donc d'un accès réseau
  complet aux quatre API SHS.
- La communication avec Claude Desktop se fait via stdio
  (entrée/sortie standard), protocole JSON-RPC encapsulé MCP.
- Trois outils sont exposés : moisson_search (lance une moisson),
  corpus_analyze (analyse un corpus déjà produit), corpus_list
  (liste les corpus existants dans le répertoire de travail).

Installation dans Claude Desktop :
1. Installer le SDK MCP : pip install mcp
2. Installer les dépendances du moissonneur : pip install -r requirements.txt
3. Éditer claude_desktop_config.json :

    {
      "mcpServers": {
        "moisson-shs": {
          "command": "python",
          "args": ["/chemin/vers/moisson_shs/mcp_server.py"],
          "env": {
            "MOISSON_OUTPUT_DIR": "/chemin/vers/output"
          }
        }
      }
    }

4. Redémarrer Claude Desktop.

Le serveur peut aussi être lancé manuellement pour tests :
    python mcp_server.py

Référence MCP : https://modelcontextprotocol.io
"""

import asyncio
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

# Imports du SDK MCP officiel
from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

# Imports internes au moissonneur
# Le serveur doit être lancé depuis le répertoire racine du projet
sys.path.insert(0, str(Path(__file__).parent))

from utils.http import HTTPClient
from utils.deduplication import deduplicate
from utils.lookups import fetch_by_doi, search_by_author
from utils.corpus_ops import merge_corpora, format_merge_report
from connectors.registry import (
    CONNECTORS, get_connector_class, list_sources, split_by_group,
    resolve_sources, list_source_tokens, FRANCOPHONE_SOURCES,
)
from mappers.csl_exporter import export_corpus_to_csl
from utils.sandbox import (
    resolve_in_output_dir, validate_suffix, PathSecurityError,
)
from config import (
    DEFAULT_MAX_PER_SOURCE,
    HTTP_HOST, HTTP_PORT, HTTP_PATH, HTTP_STATELESS, HTTP_JSON_RESPONSE,
    ALLOWED_HOSTS, ALLOWED_ORIGINS, HTTP_MAX_BODY, HTTP_RATE_LIMIT_PER_MIN,
    DOWNLOAD_PREFIX,
    MOISSON_MAX_ITEMS_PER_SOURCE, MOISSON_MAX_CONCURRENT,
    MOISSON_ACQUIRE_TIMEOUT, MOISSON_DEADLINE_SECONDS,
    MOISSON_PROGRESS_SEND_TIMEOUT,
    OAUTH_ENABLED, OAUTH_PASSWORD,
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


# === Registre des fichiers exposés en corpus:// ===
# Le schéma d'URI `corpus://<filename>` est un espace de noms plat résolu par
# `read_resource` contre OUTPUT_DIR. Or certains outils producteurs écrivent
# AILLEURS : `corpus_filter` écrit à côté du corpus source, `corpus_to_csl` et
# `corpus_merge` honorent un `output_path` arbitraire. Sans registre, le lien
# `corpus://` émis pour ces fichiers serait irrésoluble (round-trip cassé).
# On mémorise donc, par nom de base, le chemin absolu de chaque fichier exposé,
# pour que `read_resource` retrouve les fichiers hors OUTPUT_DIR. Processus
# unique et longue durée : taille négligeable (basename → Path).
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
_METRICS = {
    "calls_total": 0,
    "errors_total": 0,
    "by_tool": {},            # tool -> {"calls": int, "errors": int}
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
    _METRICS["calls_total"] += 1
    bt = _METRICS["by_tool"].setdefault(tool, {"calls": 0, "errors": 0})
    bt["calls"] += 1
    if status == "error":
        _METRICS["errors_total"] += 1
        bt["errors"] += 1
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
        "by_tool": {k: dict(v) for k, v in _METRICS["by_tool"].items()},
        "duration_ms_p50": _percentile(d, 50),
        "duration_ms_p95": _percentile(d, 95),
    }


def _get_version_string() -> str:
    try:
        from importlib.metadata import version
        return version("moisson-shs")
    except Exception:
        return "unknown"


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


# === Instanciation du serveur MCP ===
server = Server("moisson-shs")


# === Définition des outils exposés ===

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """
    Déclare les outils exposés par le serveur au client MCP.

    Chaque outil est décrit par :
    - un nom unique
    - une description (consommée par Claude pour décider de l'appel)
    - un schéma JSON Schema validant les arguments d'entrée
    """
    return [
        types.Tool(
            name="moisson_search",
            description=(
                "Lance une moisson bibliographique multi-sources à partir d'une "
                "requête mot-clé. Sources francophones (HAL, Isidore, OpenEdition, "
                "Cairn, Persée, Theses.fr, Érudit) et internationales (CrossRef, "
                "OpenAlex, Semantic Scholar, CORE). Préréglages de groupe : "
                "'francophone', 'international', 'all'. Produit un corpus Zotero "
                "JSON consolidé et dédupliqué (recouvrement par DOI absorbé). "
                "Les sources internationales requièrent une clé API pour 3 d'entre "
                "elles (OpenAlex et CORE requises, Semantic Scholar optionnelle) ; "
                "une source sans clé est ignorée proprement. Le moissonnage peut "
                "prendre plusieurs minutes selon le volume."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Requête mot-clé. Supporte les opérateurs booléens "
                            "AND/OR/NOT et les phrases entre guillemets pour "
                            "les moteurs Solr (HAL) et Isidore."
                        ),
                    },
                    "max_per_source": {
                        "type": "integer",
                        "description": (
                            "Plafond du nombre d'items par source. "
                            f"Défaut : {DEFAULT_MAX_PER_SOURCE}. "
                            "Recommandation : 500 pour requêtes larges, "
                            "1000 à 3000 pour requêtes ciblées."
                        ),
                        "default": DEFAULT_MAX_PER_SOURCE,
                    },
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": list_source_tokens(),
                        },
                        "description": (
                            "Sources et/ou groupes à moissonner. Groupes : "
                            "'francophone' (défaut), 'international', 'all'. "
                            "Sources individuelles : "
                            f"{', '.join(sorted(CONNECTORS.keys()))}. "
                            "Restreindre à ['hal', 'isidore'] pour exclure les "
                            "sources proxy ; passer ['international'] ou ['all'] "
                            "pour inclure CrossRef/OpenAlex/Semantic Scholar/CORE."
                        ),
                        "default": list(FRANCOPHONE_SOURCES),
                    },
                    "enrich_oai": {
                        "type": "boolean",
                        "description": (
                            "Active l'enrichissement OAI sur OpenEdition et "
                            "Cairn. Plus lent (multiplie le temps par 5 à 10) "
                            "mais métadonnées plus riches (qdc, oai_openaire)."
                        ),
                        "default": False,
                    },
                    "dedup": {
                        "type": "boolean",
                        "description": (
                            "Active la déduplication multi-stratégies "
                            "(DOI, URI canonique, URL, empreinte). Désactiver "
                            "uniquement pour audit ou comparaison."
                        ),
                        "default": True,
                    },
                    "from_date": {
                        "type": "string",
                        "description": (
                            "Date minimale inclusive au format YYYY ou "
                            "YYYY-MM-DD. Filtre temporel : HAL en natif "
                            "via Solr fq, Isidore et OpenEdition/Cairn en "
                            "post-pagination."
                        ),
                    },
                    "to_date": {
                        "type": "string",
                        "description": (
                            "Date maximale inclusive YYYY ou YYYY-MM-DD."
                        ),
                    },
                    "cache": {
                        "type": "boolean",
                        "description": (
                            "Active le cache HTTP local (rejeu rapide des "
                            "moissons identiques en <30s, mode quasi-offline). "
                            "TTL: 24h par défaut."
                        ),
                        "default": False,
                    },
                    "workers": {
                        "type": "integer",
                        "description": (
                            "Nombre de workers parallèles. 1=séquentiel, "
                            "2=HAL/Isidore puis OpenEdition/Cairn en parallèle. "
                            "Gain typique : ÷2 à ÷3 sur la durée totale."
                        ),
                        "default": 1,
                    },
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="corpus_analyze",
            description=(
                "Analyse statistique d'un corpus JSON Zotero déjà produit. "
                "Renvoie : volume, répartitions par source, type, année, "
                "langue ; taux de couverture DOI et résumé ; top mots-clés, "
                "revues et auteurs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "corpus_path": {
                        "type": "string",
                        "description": (
                            "Chemin absolu vers le fichier corpus_*.json "
                            "à analyser."
                        ),
                    },
                },
                "required": ["corpus_path"],
            },
        ),
        types.Tool(
            name="corpus_list",
            description=(
                "Liste tous les corpus disponibles dans le répertoire de "
                "sortie configuré (MOISSON_OUTPUT_DIR). Renvoie pour chaque "
                "corpus : chemin, date de création, taille, nombre d'items "
                "et requête d'origine si déductible du nom."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="fetch_by_doi",
            description=(
                "Récupère un item Zotero complet à partir d'un DOI en "
                "orchestrant Crossref, HAL et Isidore. Produit un item "
                "fusionné enrichi avec toutes les métadonnées disponibles. "
                "Idéal pour ajouter ponctuellement une référence à un corpus."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "doi": {
                        "type": "string",
                        "description": (
                            "DOI canonique (ex : 10.3917/comla.204.0045) "
                            "ou URL DOI (https://doi.org/10.x/y)."
                        ),
                    },
                    "add_to_corpus": {
                        "type": "string",
                        "description": (
                            "Chemin optionnel d'un corpus existant auquel "
                            "ajouter cet item. Si fourni, l'item est mergé "
                            "et le corpus est réécrit (avec son .csl.json)."
                        ),
                    },
                },
                "required": ["doi"],
            },
        ),
        types.Tool(
            name="search_by_author",
            description=(
                "Recherche la production complète d'un chercheur sur HAL + "
                "Isidore (couvre indirectement OpenEdition, Cairn, Persée, "
                "Érudit). Renvoie un corpus Zotero dédupliqué et trié par "
                "année décroissante. Utile pour générer des bibliographies "
                "intellectuelles (alimente la skill ProfilBiblio)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Nom complet du chercheur (ex : 'Patrick Charaudeau')."
                        ),
                    },
                    "idhal": {
                        "type": "string",
                        "description": (
                            "Identifiant HAL author si connu (plus précis "
                            "que le nom textuel)."
                        ),
                    },
                    "orcid": {
                        "type": "string",
                        "description": "ORCID (0000-XXXX-XXXX-XXXX).",
                    },
                    "max_per_source": {
                        "type": "integer",
                        "description": "Plafond par source. Défaut 200.",
                        "default": 200,
                    },
                    "save_corpus": {
                        "type": "boolean",
                        "description": (
                            "Si True, sauvegarde le corpus produit dans "
                            "OUTPUT_DIR avec le format habituel "
                            "(corpus_<slug>_<ts>.json + .csl.json + _stats.txt)."
                        ),
                        "default": True,
                    },
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="corpus_merge",
            description=(
                "Fusionne plusieurs corpus en un seul, applique la "
                "déduplication multi-stratégies (DOI/URI/empreinte) et "
                "produit un rapport détaillé (items chargés, uniques, "
                "exclusifs par corpus)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "corpus_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Liste de chemins absolus vers les corpus à "
                            "fusionner (format Zotero interne, .json)."
                        ),
                    },
                    "output_path": {
                        "type": "string",
                        "description": (
                            "Chemin du corpus fusionné de sortie. Si omis, "
                            "généré dans OUTPUT_DIR avec timestamp."
                        ),
                    },
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
                "Utile pour les corpus produits avant l'ajout de l'export "
                "CSL automatique, ou pour reconvertir un corpus filtré."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "corpus_path": {
                        "type": "string",
                        "description": (
                            "Chemin absolu vers le fichier corpus_*.json "
                            "(format Zotero interne)."
                        ),
                    },
                    "output_path": {
                        "type": "string",
                        "description": (
                            "Chemin de sortie pour le fichier CSL-JSON. "
                            "Si omis, suffixe .csl.json ajouté au fichier source."
                        ),
                    },
                },
                "required": ["corpus_path"],
            },
        ),
        types.Tool(
            name="corpus_filter",
            description=(
                "Filtre un corpus existant selon des critères : année min/max, "
                "type d'item Zotero, source d'origine, langue, mot-clé dans "
                "le titre. Produit un sous-corpus dans un nouveau fichier."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "corpus_path": {
                        "type": "string",
                        "description": "Chemin vers le corpus source.",
                    },
                    "year_min": {
                        "type": "integer",
                        "description": "Année minimale inclusive.",
                    },
                    "year_max": {
                        "type": "integer",
                        "description": "Année maximale inclusive.",
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
                            "Sources d'origine à conserver "
                            "(HAL, Isidore, OpenEdition, Cairn)."
                        ),
                    },
                    "language": {
                        "type": "string",
                        "description": "Code langue (fr, en, es, etc.).",
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
                            "Suffixe à ajouter au nom du fichier de sortie. "
                            "Défaut : '_filtered'."
                        ),
                        "default": "_filtered",
                    },
                },
                "required": ["corpus_path"],
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


def _build_corpus_tool_result(
    summary_text: str,
    primary_items: list,
    primary_path: Path,
    primary_mime: str,
    extra_files: list,
    structured: dict,
) -> tuple[list, dict]:
    """
    Construit le retour enrichi des outils producteurs, **conscient du
    transport** (CombinationContent — tuple ``(content_blocks, structured)``).

    - **STDIO** (Sprint 05, inchangé) : ``TextContent`` de résumé +
      ``ResourceLink corpus://`` par fichier + ``EmbeddedResource`` du corpus
      PRIMAIRE si ``<= INLINE_MAX_ITEMS`` (garde anti-saturation).
    - **HTTP distant** (Sprint 08) : ``TextContent`` SANS chemin disque +
      ``structuredContent["download_urls"]`` (URLs HTTPS) ; **jamais** de JSON
      inliné, **jamais** de chemin disque (ni texte, ni structured), **pas** de
      ``ResourceLink corpus://`` (non téléchargeable hors MCP, porteur de chemin).

    Args:
        summary_text: Résumé texte formaté par l'outil (peut contenir des
            chemins — filtrés en HTTP).
        primary_items: Items du corpus PRIMAIRE (inlinés en stdio sous le seuil).
        primary_path: Fichier primaire (URI inline en stdio).
        primary_mime: Type MIME du contenu inliné.
        extra_files: Autres fichiers produits.
        structured: Métadonnées machine, complétées ici.

    Returns:
        Tuple ``(content_blocks, structured)``.
    """
    # Fichiers réellement produits (les deux modes alimentent le registre, qui
    # sert à read_resource pour les clients MCP).
    file_paths = [p for p in [primary_path, *extra_files] if p and p.exists()]
    for path in file_paths:
        _EMITTED_RESOURCES[path.name] = path.resolve()

    n = len(primary_items)

    # === Branche HTTP distant : URLs seules, aucun chemin, aucun inline ===
    if TRANSPORT_MODE == "http":
        urls = {p.name: _download_url(p.name) for p in file_paths}
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

    # === Branche STDIO : comportement Sprint 05 STRICTEMENT inchangé ===
    blocks = [types.TextContent(type="text", text=summary_text)]

    # (a) LIENS : un ResourceLink par fichier → cliquable dans l'interface
    for path in file_paths:
        blocks.append(types.ResourceLink(
            type="resource_link",
            uri=f"corpus://{path.name}",
            name=path.stem,
            mimeType=(
                "text/plain" if path.suffix == ".txt" else "application/json"
            ),
            description=f"{path.name} — {path}",
        ))

    # (b) JSON INLINE : corpus primaire complet si sous le seuil ; sinon lien seul
    inline = n <= INLINE_MAX_ITEMS
    if inline:
        blocks.append(types.EmbeddedResource(
            type="resource",
            resource=types.TextResourceContents(
                uri=f"corpus://{primary_path.name}",
                mimeType=primary_mime,
                text=json.dumps(primary_items, ensure_ascii=False, indent=2),
            ),
        ))
    else:
        # Message explicite pour les clients qui n'affichent pas les blocs
        blocks[0] = types.TextContent(
            type="text",
            text=summary_text + (
                f"\n\n(JSON non inliné : {n} items > seuil {INLINE_MAX_ITEMS}. "
                f"Corpus complet via la resource corpus://{primary_path.name})"
            ),
        )

    structured.update({
        "item_count": n,
        "inline_json": inline,
        "inline_threshold": INLINE_MAX_ITEMS,
        "files": {p.name: str(p) for p in file_paths},
        "resources": [f"corpus://{p.name}" for p in file_paths],
    })
    if not inline:
        structured["inline_skipped_reason"] = (
            f"{n} items > seuil {INLINE_MAX_ITEMS}"
        )
    return blocks, structured


# === Implémentation des outils ===

@server.call_tool()
async def call_tool(
    name: str,
    arguments: dict[str, Any],
) -> list[types.ContentBlock] | tuple[list, dict]:
    """
    Dispatcher principal des appels d'outils.

    Args:
        name: Nom de l'outil invoqué (correspond à list_tools)
        arguments: Arguments validés selon inputSchema

    Returns:
        Selon l'outil, l'une des trois formes acceptées par le SDK :
        - ``str`` (legacy) → enveloppé en un unique ``TextContent`` ;
        - ``list[ContentBlock]`` → renvoyée telle quelle ;
        - ``tuple(content_blocks, structured)`` → *CombinationContent*
          (peuple à la fois ``content`` et ``structuredContent``).
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
    status_ = "ok"
    try:
        if name == "moisson_search":
            result = await _tool_moisson_search(arguments)
        elif name == "corpus_analyze":
            result = await _tool_corpus_analyze(arguments)
        elif name == "corpus_list":
            result = await _tool_corpus_list(arguments)
        elif name == "corpus_filter":
            result = await _tool_corpus_filter(arguments)
        elif name == "corpus_to_csl":
            result = await _tool_corpus_to_csl(arguments)
        elif name == "fetch_by_doi":
            result = await _tool_fetch_by_doi(arguments)
        elif name == "search_by_author":
            result = await _tool_search_by_author(arguments)
        elif name == "corpus_merge":
            result = await _tool_corpus_merge(arguments)
        else:
            result = f"Outil inconnu : {name}"
            status_ = "error"
    except Exception as e:
        logger.exception(f"Erreur dans l'outil {name}")
        result = f"Erreur : {type(e).__name__} : {e}"
        status_ = "error"

    # item_count depuis le structuredContent si l'outil l'a produit.
    item_count = None
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
        item_count = result[1].get("item_count")

    duration_ms = round((time.monotonic() - started) * 1000, 1)
    _record_metrics(name, status_, duration_ms)

    # Observabilité serveur distant : log structuré + audit JSONL.
    if TRANSPORT_MODE == "http":
        logger.info("Outil terminé", extra={
            "tool": name, "request_id": req_id, "client_id": client_id,
            "duration_ms": duration_ms, "status": status_,
        })
        _write_audit({
            "ts": datetime.now(timezone.utc).isoformat(),
            "request_id": str(req_id) if req_id is not None else None,
            "client_id": client_id,
            "tool": name,
            "arg_keys": sorted((arguments or {}).keys()),
            "duration_ms": duration_ms,
            "status": status_,
            "item_count": item_count,
        })

    # Normalisation du retour (rétro-compatible) :
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


# === Opérations longues : progression, annulation, concurrence (Sprint 09) ===

class MoissonCancelled(Exception):
    """Annulation coopérative d'une moisson (deadline ou demande client)."""


class ProgressReporter:
    """
    Pont **thread → asyncio** pour la progression d'une moisson.

    `_run_moisson_sync` tourne dans un thread d'executor (les connecteurs sont
    synchrones). Pour notifier la session asyncio depuis ce thread, on poste la
    coroutine `send_progress_notification` sur la boucle via
    `run_coroutine_threadsafe`. L'annulation est **coopérative** : le thread
    vérifie `cancel_event` entre les sources et à chaque `report()`.
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
_MOISSON_SEM = None


def _get_moisson_semaphore():
    global _MOISSON_SEM
    if _MOISSON_SEM is None:
        _MOISSON_SEM = asyncio.Semaphore(MOISSON_MAX_CONCURRENT)
    return _MOISSON_SEM


async def _tool_moisson_search(args: dict[str, Any]) -> tuple[list, dict] | str:
    """
    Implémentation de l'outil moisson_search.

    Exécute le moissonnage dans un thread de travail pour ne pas bloquer la
    boucle asyncio MCP. En **HTTP** : plafond serveur sur ``max_per_source``,
    concurrence bornée (sémaphore), **deadline serveur** avec annulation propre,
    progression MCP best-effort. En **stdio** : comportement historique
    (best-effort, sans deadline ni sémaphore).
    """
    query = args["query"]
    raw_max = int(args.get("max_per_source", DEFAULT_MAX_PER_SOURCE))
    sources = args.get("sources", list(FRANCOPHONE_SOURCES))
    enrich_oai = args.get("enrich_oai", False)
    do_dedup = args.get("dedup", True)
    from_date = args.get("from_date")
    to_date = args.get("to_date")
    cache = args.get("cache", False)
    workers = args.get("workers", 1)

    # Plafond serveur DUR (anti-DoS) : clamp HTTP-only, indépendant de l'arg
    # client. En stdio (local), pas de clamp.
    if TRANSPORT_MODE == "http":
        max_per_source = min(raw_max, MOISSON_MAX_ITEMS_PER_SOURCE)
    else:
        max_per_source = raw_max

    loop = asyncio.get_event_loop()

    # === stdio : comportement historique (best-effort, sans deadline) ===
    if TRANSPORT_MODE != "http":
        reporter = _make_progress_reporter(loop, None)  # None si pas de token
        return await loop.run_in_executor(
            None, _run_moisson_sync,
            query, max_per_source, sources, enrich_oai, do_dedup,
            from_date, to_date, cache, workers, reporter, None,
        )

    # === HTTP : concurrence bornée + deadline + annulation coopérative ===
    cancel_event = threading.Event()
    reporter = _make_progress_reporter(loop, cancel_event)

    sem = _get_moisson_semaphore()
    try:
        await asyncio.wait_for(sem.acquire(), timeout=MOISSON_ACQUIRE_TIMEOUT)
    except asyncio.TimeoutError:
        return (
            "Erreur : serveur occupé (trop de moissons simultanées). "
            "Réessayez dans un instant."
        )

    fut = loop.run_in_executor(
        None, _run_moisson_sync,
        query, max_per_source, sources, enrich_oai, do_dedup,
        from_date, to_date, cache, workers, reporter, cancel_event,
    )
    try:
        # shield : un timeout/annulation de wait_for ne doit PAS détruire le
        # future sous-jacent ; on l'attend explicitement ensuite (join du thread).
        return await asyncio.wait_for(
            asyncio.shield(fut), timeout=MOISSON_DEADLINE_SECONDS
        )
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        cancel_event.set()
        try:
            await fut  # attendre la fin RÉELLE du thread avant de libérer le slot
        except Exception:
            pass
        if isinstance(exc, asyncio.TimeoutError):
            return (
                f"Erreur : deadline serveur dépassée "
                f"({int(MOISSON_DEADLINE_SECONDS)} s). Moisson annulée "
                f"(aucun corpus partiel publié)."
            )
        raise  # CancelledError : honorer l'annulation du client
    finally:
        sem.release()


def _cleanup_partial_raw(run_id: str) -> None:
    """Supprime les payloads bruts partiels d'une moisson annulée
    (``OUTPUT_DIR/raw/*_{run_id}.json``)."""
    raw_dir = OUTPUT_DIR / "raw"
    if not raw_dir.is_dir():
        return
    for p in raw_dir.glob(f"*_{run_id}.json"):
        try:
            p.unlink()
        except OSError:
            pass


def _run_moisson_sync(
    query: str,
    max_per_source: int,
    sources: list[str],
    enrich_oai: bool,
    do_dedup: bool,
    from_date: str = None,
    to_date: str = None,
    cache: bool = False,
    workers: int = 1,
    progress=None,
    cancel_event=None,
) -> tuple[list, dict]:
    """
    Exécution synchrone de la moisson (dans un thread d'executor).

    Réplique la logique de main.py sans interface CLI. Instrumentée (Sprint 09)
    pour la **progression** (par source, via ``progress``) et l'**annulation
    coopérative** (``cancel_event`` vérifié entre les sources) — tous deux
    optionnels (None ⇒ comportement historique).

    Raises:
        MoissonCancelled: si l'annulation a été demandée (deadline/client). Les
            ``raw/`` partiels sont alors nettoyés et AUCUN corpus final n'est écrit.

    Returns:
        Le tuple ``(content_blocks, structured)`` construit par
        ``_build_corpus_tool_result``.
    """
    from main import slugify, compute_stats, write_stats_file, run_source

    # Développe les groupes (francophone/international/all) en noms de sources
    sources = resolve_sources(sources)

    # Identifiant de run = timestamp seconde + uuid court : évite toute collision
    # de noms de fichiers sous concurrence (correctif revue 06–09). Les raw/ par
    # source héritent du même run_id (passé comme « timestamp » à run_source).
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = f"{timestamp}_{uuid.uuid4().hex[:8]}"
    query_slug = slugify(query)
    output_json = OUTPUT_DIR / f"corpus_{query_slug}_{run_id}.json"
    output_csl = OUTPUT_DIR / f"corpus_{query_slug}_{run_id}.csl.json"
    output_stats = OUTPUT_DIR / f"corpus_{query_slug}_{run_id}_stats.txt"

    http_client = HTTPClient(use_cache=cache)
    all_items = []
    per_source_counts = {}
    start_time = datetime.now()

    # Topologie : sources natives en parallèle, puis sources proxy ensuite
    native_names, proxy_names = split_by_group(sources)

    def _build_group(names):
        group = []
        enrich_oai_capable = {"openedition", "cairn", "persee", "erudit"}
        for name in names:
            cls = get_connector_class(name)
            kwargs = {}
            if name in enrich_oai_capable:
                kwargs["enrich_oai"] = enrich_oai
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

    total_sources = len(group_native) + len(group_proxy)
    done_counter = {"n": 0}

    def _note(name):
        """Avance le compteur et émet une notification de progression."""
        done_counter["n"] += 1
        if progress is not None:
            progress.report(
                float(done_counter["n"]), float(total_sources),
                f"Source {name} terminée ({done_counter['n']}/{total_sources})",
            )

    def _check_cancel():
        if cancel_event is not None and cancel_event.is_set():
            raise MoissonCancelled()

    def _run_group(group):
        results = {}
        _check_cancel()
        if workers > 1 and len(group) > 1:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(
                        run_source, name, cls, query, max_per_source,
                        OUTPUT_DIR, run_id, http_client,
                        from_date=from_date, to_date=to_date, **kw,
                    ): name
                    for name, cls, kw in group
                }
                for f in as_completed(futures):
                    name = futures[f]
                    try:
                        items = f.result()
                    except Exception as e:
                        logger.error(f"Source {name} en erreur : {e}")
                        items = []
                    results[name] = items
                    _note(name)
        else:
            for name, cls, kw in group:
                _check_cancel()  # annulation AVANT de démarrer la source suivante
                results[name] = run_source(
                    name, cls, query, max_per_source,
                    OUTPUT_DIR, run_id, http_client,
                    from_date=from_date, to_date=to_date, **kw,
                )
                _note(name)
        return results

    try:
        for src_name, items in _run_group(group_native).items():
            per_source_counts[src_name] = len(items)
            all_items.extend(items)
        _check_cancel()  # entre les deux groupes
        for src_name, items in _run_group(group_proxy).items():
            per_source_counts[src_name] = len(items)
            all_items.extend(items)
    except MoissonCancelled:
        # Annulation : pas de corpus final écrit + nettoyage des raw/ partiels.
        _cleanup_partial_raw(run_id)
        raise
    finally:
        http_client.close()

    total_before_dedup = len(all_items)

    # Déduplication
    if do_dedup:
        all_items = deduplicate(all_items)

    # Export Zotero JSON natif (format interne riche)
    output_json.write_text(
        json.dumps(all_items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Export CSL-JSON (format standard importable Zotero Desktop ET
    # lisible par les LLMs pour exploitation bibliographique)
    csl_items = export_corpus_to_csl(all_items)
    output_csl.write_text(
        json.dumps(csl_items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Statistiques
    stats = compute_stats(all_items)
    write_stats_file(stats, output_stats, query)

    duration = (datetime.now() - start_time).total_seconds()

    # === Construction de la réponse pour Claude ===
    lines = [
        f"Moisson terminée.",
        f"",
        f"Requête : {query}",
        f"Durée : {duration:.1f} s",
        f"",
        f"Items collectés par source :",
    ]
    for src, count in per_source_counts.items():
        lines.append(f"  - {src} : {count}")
    lines.extend([
        f"",
        f"Total avant déduplication : {total_before_dedup}",
        f"Total après déduplication : {len(all_items)}",
        f"Doublons fusionnés : {total_before_dedup - len(all_items)}",
        f"",
        f"Couverture qualitative :",
        f"  - Items avec DOI : {stats['with_doi']} ({stats['doi_coverage_pct']} %)",
        f"  - Items avec résumé : {stats['with_abstract']} "
        f"({stats['abstract_coverage_pct']} %)",
        f"",
        f"Fichiers produits :",
        f"  - Zotero JSON (interne)   : {output_json}",
        f"  - CSL-JSON (import Zotero) : {output_csl}",
        f"  - Statistiques            : {output_stats}",
        f"",
        f"Import Zotero : Fichier > Importer > Un fichier > sélectionner",
        f"le fichier .csl.json (Zotero détecte automatiquement le format",
        f"CSL JSON). Le .json sans suffixe csl est le format interne du",
        f"moissonneur, exploitable par le MCP pour analyses ultérieures.",
    ])

    summary = "\n".join(lines)
    structured = {
        "query": query,
        "by_source": per_source_counts,
        "total_before_dedup": total_before_dedup,
        "total_unique": len(all_items),
        "duplicates_removed": total_before_dedup - len(all_items),
        "duration_seconds": round(duration, 1),
        "doi_coverage_pct": stats.get("doi_coverage_pct"),
        "abstract_coverage_pct": stats.get("abstract_coverage_pct"),
    }
    # Retour enrichi : le JSON inliné est le corpus Zotero INTERNE (format
    # riche), conformément au cadrage. Le CSL-JSON et les stats restent des
    # ResourceLink (extra_files).
    return _build_corpus_tool_result(
        summary_text=summary,
        primary_items=all_items,
        primary_path=output_json,
        primary_mime="application/json",
        extra_files=[output_csl, output_stats],
        structured=structured,
    )


async def _tool_corpus_analyze(args: dict[str, Any]) -> str:
    """
    Implémentation de l'outil corpus_analyze.

    Charge un fichier JSON Zotero et produit un rapport statistique.
    """
    corpus_path = _safe_path(args["corpus_path"])  # confiné en HTTP
    if not corpus_path.exists():
        return f"Fichier introuvable : {corpus_path.name}"

    try:
        items = json.loads(corpus_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return f"Erreur de parsing JSON : {e}"

    if not isinstance(items, list):
        return "Le fichier ne contient pas un tableau d'items Zotero."

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

    # Enrichissements : top revues, top auteurs
    from collections import Counter
    journals = Counter(
        item.get("publicationTitle", "")
        for item in items
        if item.get("publicationTitle")
    )
    if journals:
        lines.append("")
        lines.append("Top 20 revues :")
        for journal, count in journals.most_common(20):
            lines.append(f"  - {journal} : {count}")

    authors = Counter()
    for item in items:
        for creator in item.get("creators", []):
            name = creator.get("lastName") or creator.get("name", "")
            if name:
                authors[name] += 1
    if authors:
        lines.append("")
        lines.append("Top 30 auteurs (par nombre d'items) :")
        for author, count in authors.most_common(30):
            lines.append(f"  - {author} : {count}")

    return "\n".join(lines)


async def _tool_corpus_list(args: dict[str, Any]) -> str:
    """
    Implémentation de l'outil corpus_list.

    Liste les fichiers corpus_*.json présents dans OUTPUT_DIR.
    """
    corpora = sorted(
        OUTPUT_DIR.glob("corpus_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if not corpora:
        return (
            f"Aucun corpus trouvé dans {OUTPUT_DIR}.\n"
            f"Lancez d'abord une moisson avec l'outil moisson_search."
        )

    lines = [f"=== Corpus disponibles dans {OUTPUT_DIR} ==="]
    for path in corpora:
        size_kb = path.stat().st_size / 1024
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        # Tentative de comptage rapide des items
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            n_items = len(data) if isinstance(data, list) else 0
        except Exception:
            n_items = "?"
        lines.append("")
        lines.append(f"  {path.name}")
        lines.append(f"    Chemin : {path}")
        lines.append(f"    Date : {mtime.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"    Taille : {size_kb:.1f} ko")
        lines.append(f"    Items : {n_items}")
    return "\n".join(lines)


async def _tool_corpus_filter(args: dict[str, Any]) -> str | tuple[list, dict]:
    """
    Implémentation de l'outil corpus_filter.

    Filtre un corpus selon des critères et produit un sous-corpus.

    En cas d'erreur (fichier introuvable, JSON invalide) renvoie une ``str``
    legacy ; en cas de succès, le retour enrichi (tuple) via le helper.
    """
    corpus_path = _safe_path(args["corpus_path"])  # confiné en HTTP
    if not corpus_path.exists():
        return f"Fichier introuvable : {corpus_path.name}"

    try:
        items = json.loads(corpus_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return f"Erreur de parsing JSON : {e}"

    year_min = args.get("year_min")
    year_max = args.get("year_max")
    item_types = set(args.get("item_types", []))
    sources_filter = set(args.get("sources", []))
    language = args.get("language", "").lower()
    title_contains = args.get("title_contains", "").lower()
    suffix = args.get("output_suffix", "_filtered")
    # En HTTP : valider le charset du suffixe (anti-traversal via un '/' ou '..').
    if TRANSPORT_MODE == "http":
        suffix = validate_suffix(suffix)

    from utils.cleaner import extract_year, normalize_for_fingerprint
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

        # Filtre par langue
        if language and language not in item.get("language", "").lower():
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
    output_path.write_text(
        json.dumps(filtered, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    retention = round(100 * len(filtered) / max(len(items), 1), 1)
    summary = (
        f"Filtrage appliqué.\n"
        f"Corpus source : {corpus_path.name} ({len(items)} items)\n"
        f"Sous-corpus : {output_path.name} ({len(filtered)} items)\n"
        f"Taux de rétention : {retention:.1f} %\n"
        f"Chemin complet : {output_path}"
    )
    structured = {
        "source_corpus": str(corpus_path),
        "source_count": len(items),
        "filtered_count": len(filtered),
        "retention_pct": retention,
        "filters": {
            "year_min": year_min,
            "year_max": year_max,
            "item_types": sorted(item_types),
            "sources": sorted(sources_filter),
            "language": language or None,
            "title_contains": title_contains or None,
        },
    }
    return _build_corpus_tool_result(
        summary_text=summary,
        primary_items=filtered,
        primary_path=output_path,
        primary_mime="application/json",
        extra_files=[],
        structured=structured,
    )


async def _tool_corpus_to_csl(args: dict[str, Any]) -> str | tuple[list, dict]:
    """
    Implémentation de l'outil corpus_to_csl.

    Convertit un corpus Zotero JSON interne en CSL-JSON. Le produit PRIMAIRE
    (donc le JSON inliné) est ici le CSL-JSON, conformément au cadrage.
    """
    corpus_path = _safe_path(args["corpus_path"])  # confiné en HTTP
    if not corpus_path.exists():
        return f"Fichier introuvable : {corpus_path.name}"

    try:
        items = json.loads(corpus_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return f"Erreur de parsing JSON : {e}"

    if not isinstance(items, list):
        return "Le fichier ne contient pas un tableau d'items Zotero."

    output_path = args.get("output_path")
    if output_path:
        # Chemin de sortie fourni par le client → confiné en HTTP.
        output = _safe_path(output_path)
    else:
        # Ajouter .csl avant .json : corpus_X.json → corpus_X.csl.json
        # Dérivé du chemin source DÉJÀ confiné.
        output = corpus_path.parent / (corpus_path.stem + ".csl.json")

    csl_items = export_corpus_to_csl(items)
    output.write_text(
        json.dumps(csl_items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    summary = (
        f"Conversion CSL-JSON réussie.\n"
        f"Source : {corpus_path.name} ({len(items)} items Zotero)\n"
        f"Sortie : {output.name} ({len(csl_items)} items CSL)\n"
        f"Chemin complet : {output}\n"
        f"\n"
        f"Importable dans Zotero Desktop via Fichier > Importer."
    )
    structured = {
        "source_corpus": str(corpus_path),
        "source_count": len(items),
        "csl_count": len(csl_items),
        "format": "csl-json",
    }
    # Produit primaire = CSL-JSON : c'est lui qu'on inline.
    return _build_corpus_tool_result(
        summary_text=summary,
        primary_items=csl_items,
        primary_path=output,
        primary_mime="application/json",
        extra_files=[],
        structured=structured,
    )


# === Outils MCP avancés ===

async def _tool_fetch_by_doi(args: dict[str, Any]) -> str:
    """Implémentation de fetch_by_doi."""
    doi = args["doi"]
    add_to = args.get("add_to_corpus")

    loop = asyncio.get_event_loop()
    item = await loop.run_in_executor(None, fetch_by_doi, doi)
    if not item:
        return f"Aucune métadonnée trouvée pour DOI={doi}"

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

    # Ajout optionnel à un corpus existant
    if add_to:
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
        else:
            lines.append(f"Corpus introuvable : {add_to}")

    return "\n".join(lines)


async def _tool_search_by_author(
    args: dict[str, Any],
) -> str | tuple[list, dict]:
    """Implémentation de search_by_author.

    Renvoie le retour enrichi (tuple) lorsque le corpus est sauvegardé
    (``save_corpus=True`` et items non vides) ; sinon une ``str`` legacy,
    aucun fichier n'étant produit à exposer en lien.
    """
    name = args["name"]
    idhal = args.get("idhal")
    orcid = args.get("orcid")
    max_per_source = args.get("max_per_source", 200)
    save = args.get("save_corpus", True)

    loop = asyncio.get_event_loop()
    items = await loop.run_in_executor(
        None,
        lambda: search_by_author(
            name, idhal=idhal, orcid=orcid,
            max_per_source=max_per_source,
        ),
    )

    json_path = csl_path = stats_path = None
    if save and items:
        from main import slugify, compute_stats, write_stats_file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = slugify(f"author_{name}")
        json_path = OUTPUT_DIR / f"corpus_{slug}_{timestamp}.json"
        csl_path = OUTPUT_DIR / f"corpus_{slug}_{timestamp}.csl.json"
        stats_path = OUTPUT_DIR / f"corpus_{slug}_{timestamp}_stats.txt"
        json_path.write_text(
            json.dumps(items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        csl_path.write_text(
            json.dumps(export_corpus_to_csl(items), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        write_stats_file(compute_stats(items), stats_path, f"author:{name}")
        save_info = (
            f"\nCorpus sauvegardé :\n"
            f"  Zotero JSON    : {json_path}\n"
            f"  CSL-JSON       : {csl_path}\n"
            f"  Statistiques   : {stats_path}"
        )
    else:
        save_info = ""

    lines = [
        f"Recherche : {name}"
        + (f" (idhal={idhal})" if idhal else "")
        + (f" (orcid={orcid})" if orcid else ""),
        f"Items uniques : {len(items)}",
        "",
        "10 publications les plus récentes :",
    ]
    for i, item in enumerate(items[:10]):
        lines.append(
            f"  {i + 1}. [{item.get('date', '?')[:4]}] "
            f"{item.get('title', '')[:90]}"
        )
    lines.append(save_info)
    summary = "\n".join(lines)

    # Pas de fichier produit → retour legacy str (rien à exposer en lien).
    if not (save and items):
        return summary

    structured = {
        "author": name,
        "idhal": idhal,
        "orcid": orcid,
        "item_count_raw": len(items),
    }
    return _build_corpus_tool_result(
        summary_text=summary,
        primary_items=items,
        primary_path=json_path,
        primary_mime="application/json",
        extra_files=[csl_path, stats_path],
        structured=structured,
    )


async def _tool_corpus_merge(args: dict[str, Any]) -> str | tuple[list, dict]:
    """Implémentation de corpus_merge."""
    corpus_paths = [_safe_path(p) for p in args["corpus_paths"]]  # confinés en HTTP
    missing = [p.name for p in corpus_paths if not p.exists()]
    if missing:
        return f"Corpus introuvables : {', '.join(missing)}"

    output_path = args.get("output_path")
    if output_path:
        output = _safe_path(output_path)  # confiné en HTTP
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = OUTPUT_DIR / f"corpus_merged_{timestamp}.json"

    loop = asyncio.get_event_loop()
    merged, report = await loop.run_in_executor(
        None, merge_corpora, corpus_paths,
    )

    # Sauvegarde
    output.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    csl_path = output.parent / (output.stem + ".csl.json")
    csl_path.write_text(
        json.dumps(export_corpus_to_csl(merged), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    summary = (
        f"{format_merge_report(report)}\n\n"
        f"Corpus fusionné écrit :\n"
        f"  Zotero JSON : {output}\n"
        f"  CSL-JSON    : {csl_path}"
    )
    structured = {
        "input_corpora": [str(p) for p in corpus_paths],
        "merged_count": len(merged),
        "report": report,
    }
    return _build_corpus_tool_result(
        summary_text=summary,
        primary_items=merged,
        primary_path=output,
        primary_mime="application/json",
        extra_files=[csl_path],
        structured=structured,
    )


# === Resources MCP : corpus exposés comme ressources ===

@server.list_resources()
async def list_resources() -> list[types.Resource]:
    """
    Expose chaque corpus du OUTPUT_DIR comme une Resource MCP.

    Permet à Claude de lire directement un corpus sans appel d'outil,
    via l'URI corpus://<nom_de_fichier>. La distinction format Zotero
    interne (.json) vs CSL-JSON (.csl.json) est explicitée dans la
    description.
    """
    resources = []
    for path in sorted(OUTPUT_DIR.glob("corpus_*.json"), reverse=True):
        is_csl = path.name.endswith(".csl.json")
        # Lecture rapide pour le nombre d'items
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            n_items = len(data) if isinstance(data, list) else 0
        except Exception:
            n_items = 0
        kind = "CSL-JSON (BibTeX JSON, import Zotero)" if is_csl else "Zotero JSON interne"
        resources.append(
            types.Resource(
                uri=f"corpus://{path.name}",
                name=path.stem,
                mimeType="application/json",
                description=f"{kind} — {n_items} items — {path}",
            )
        )
    return resources


@server.read_resource()
async def read_resource(uri: str) -> str:
    """
    Lit le contenu d'un corpus exposé comme Resource.

    URI attendu : `corpus://<filename>`. Le fichier doit exister dans
    OUTPUT_DIR. Limite à 2 Mo de contenu pour éviter de saturer le
    contexte Claude.

    Note : le SDK passe l'URI sous forme d'objet ``AnyUrl`` (pydantic),
    pas d'une ``str``. On normalise donc en ``str`` en entrée — ce qui
    rend la fonction robuste aux deux formes (appel direct en test avec
    une str, ou appel SDK avec un AnyUrl). Les URIs ``corpus://`` sont
    préservées telles quelles par ``str(AnyUrl)`` (ni minuscule forcée,
    ni slash final).
    """
    uri = str(uri)
    prefix = "corpus://"
    if not uri.startswith(prefix):
        return json.dumps({"error": f"URI invalide : {uri}"})

    filename = uri[len(prefix):]
    path = OUTPUT_DIR / filename
    if not path.exists():
        # Repli : fichier exposé par un outil producteur hors OUTPUT_DIR
        # (corpus_filter écrit à côté de la source, corpus_to_csl/merge
        # honorent un output_path arbitraire). Le registre garde le chemin
        # absolu, ce qui rend le round-trip du lien corpus:// fiable.
        registered = _EMITTED_RESOURCES.get(filename)
        if registered and registered.exists():
            path = registered
        else:
            return json.dumps({"error": f"Corpus introuvable : {filename}"})

    content = path.read_text(encoding="utf-8")
    # Garde-fou contre saturation du contexte
    if len(content) > 2_000_000:
        items = json.loads(content)
        if isinstance(items, list):
            # Renvoyer un échantillon des 50 premiers items + métadonnées
            return json.dumps(
                {
                    "_truncated": True,
                    "_total_items": len(items),
                    "_returned": min(50, len(items)),
                    "_message": (
                        f"Corpus trop volumineux pour être renvoyé "
                        f"intégralement ({len(content)//1024} ko). "
                        f"50 premiers items inclus ci-dessous."
                    ),
                    "items": items[:50],
                },
                ensure_ascii=False, indent=2,
            )
    return content


# === Prompts MCP : workflows pré-configurés ===

@server.list_prompts()
async def list_prompts() -> list[types.Prompt]:
    """Liste les prompts pré-configurés pour les cas d'usage SHS récurrents."""
    return [
        types.Prompt(
            name="bibliographie-thematique",
            description=(
                "Construit une bibliographie thématique exhaustive : "
                "moisson multi-sources sur un mot-clé puis analyse "
                "(top revues, top auteurs, distribution chronologique)."
            ),
            arguments=[
                types.PromptArgument(
                    name="theme",
                    description="Mot-clé ou requête SHS (ex : 'discours numérique')",
                    required=True,
                ),
                types.PromptArgument(
                    name="max_per_source",
                    description="Plafond par source (défaut 500)",
                    required=False,
                ),
            ],
        ),
        types.Prompt(
            name="profil-chercheur",
            description=(
                "Production bibliographique complète d'un chercheur SHS, "
                "multi-sources, dédupliquée et triée par année."
            ),
            arguments=[
                types.PromptArgument(
                    name="nom",
                    description="Nom complet (ex : 'Patrick Charaudeau')",
                    required=True,
                ),
                types.PromptArgument(
                    name="idhal",
                    description="Identifiant HAL author (optionnel, plus précis)",
                    required=False,
                ),
                types.PromptArgument(
                    name="orcid",
                    description="ORCID (optionnel)",
                    required=False,
                ),
            ],
        ),
        types.Prompt(
            name="veille-mensuelle",
            description=(
                "Veille bibliographique sur les nouvelles publications "
                "d'un mois sur un thème donné. À combiner avec from-date "
                "et to-date pour fenêtrage temporel."
            ),
            arguments=[
                types.PromptArgument(
                    name="theme",
                    description="Mot-clé SHS",
                    required=True,
                ),
                types.PromptArgument(
                    name="from_date",
                    description="Date de début YYYY-MM-DD",
                    required=True,
                ),
                types.PromptArgument(
                    name="to_date",
                    description="Date de fin YYYY-MM-DD",
                    required=True,
                ),
            ],
        ),
        types.Prompt(
            name="etat-de-l-art",
            description=(
                "État de l'art multilingue sur une question de recherche : "
                "moisson large (sources francophones + internationales) sur les "
                "5 dernières années + analyse des regroupements thématiques et "
                "des auteurs influents."
            ),
            arguments=[
                types.PromptArgument(
                    name="question",
                    description=(
                        "Question de recherche (ex : 'régulation des "
                        "plateformes numériques')"
                    ),
                    required=True,
                ),
            ],
        ),
    ]


@server.get_prompt()
async def get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
    """Génère le texte du prompt pour un cas d'usage."""
    args = arguments or {}

    if name == "bibliographie-thematique":
        theme = args.get("theme", "")
        max_per_source = args.get("max_per_source", "500")
        text = (
            f"Construis une bibliographie thématique sur « {theme} » :\n"
            f"1. Appelle moisson_search avec query=« {theme} », "
            f"max_per_source={max_per_source}, workers=2, cache=true.\n"
            f"2. Appelle corpus_analyze sur le corpus produit.\n"
            f"3. Présente le résultat : top 20 revues, top 30 auteurs, "
            f"distribution chronologique, recommandations de lecture."
        )

    elif name == "profil-chercheur":
        nom = args.get("nom", "")
        idhal = args.get("idhal")
        orcid = args.get("orcid")
        text = (
            f"Construis le profil bibliographique du chercheur « {nom} ».\n"
            f"1. Appelle search_by_author avec name=« {nom} »"
            + (f", idhal=« {idhal} »" if idhal else "")
            + (f", orcid=« {orcid} »" if orcid else "")
            + ".\n"
            f"2. Présente la production en sections par décennie ou par "
            f"thématique. Identifie les revues récurrentes et les "
            f"coauteurs principaux.\n"
            f"3. Si tu détectes des phases de carrière distinctes "
            f"(thématiques, méthodologies, supports), organise la "
            f"bibliographie en conséquence."
        )

    elif name == "veille-mensuelle":
        theme = args.get("theme", "")
        fd = args.get("from_date", "")
        td = args.get("to_date", "")
        text = (
            f"Effectue une veille bibliographique sur « {theme} » entre "
            f"{fd} et {td} :\n"
            f"1. moisson_search query=« {theme} », from_date=« {fd} », "
            f"to_date=« {td} », workers=2.\n"
            f"2. Synthèse en 3 paragraphes maximum : ce qui est nouveau, "
            f"qui publie, où ça se publie.\n"
            f"3. Liste 5-10 références prioritaires à lire."
        )

    elif name == "etat-de-l-art":
        question = args.get("question", "")
        from datetime import datetime as _dt
        current_year = _dt.now().year
        text = (
            f"Construis un état de l'art SHS **multilingue** sur la question "
            f"« {question} » sur les 5 dernières années :\n"
            f"1. moisson_search query=« {question} », sources=[\"all\"] "
            f"(francophones + internationales : CrossRef/OpenAlex/Semantic "
            f"Scholar/CORE), from_date={current_year - 5}, max_per_source=1000, "
            f"workers=2. NB : les sources internationales OpenAlex/CORE "
            f"requièrent une clé API ; sans clé, elles sont ignorées et la "
            f"moisson francophone se poursuit.\n"
            f"2. corpus_analyze pour avoir la vue d'ensemble (le rapport "
            f"indique la répartition par langue et les recouvrements "
            f"FR/internationale par paire de sources).\n"
            f"3. Identifie 3-5 sous-thèmes émergents et regroupe les "
            f"références par sous-thème, en distinguant explicitement les "
            f"travaux francophones et anglophones.\n"
            f"4. Pour chaque sous-thème, sélectionne 5-10 publications "
            f"phares (récurrence d'auteurs, revues réputées, compte de "
            f"citations OpenAlex/Semantic Scholar). Signale la langue de "
            f"chaque référence retenue."
        )

    else:
        text = f"Prompt inconnu : {name}"

    return types.GetPromptResult(
        description=f"Workflow {name}",
        messages=[
            types.PromptMessage(
                role="user",
                content=types.TextContent(type="text", text=text),
            )
        ],
    )


# === Transport STDIO (défaut, Claude Desktop local) ===

async def run_stdio():
    """
    Démarre le serveur en mode stdio (transport par défaut).

    Le client MCP (Claude Desktop) communique avec ce processus
    via stdin/stdout en utilisant le protocole JSON-RPC. Comportement
    historique inchangé (Sprint 05).
    """
    logger.info("Démarrage serveur MCP moisson-shs (stdio)")
    logger.info(f"OUTPUT_DIR = {OUTPUT_DIR}")

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
# pour le test de suffixe : `.csl.json` avant `.json` (endswith).
_ALLOWED_DOWNLOAD_SUFFIXES = (".csl.json", ".json", "_stats.txt")


async def _download_endpoint(request):
    """
    Sert un fichier de corpus en téléchargement (mode HTTP distant).

    Sécurité (correctifs revue critiques) :
    - rejet dur de tout nom contenant un séparateur / `..` (`name == Path(name).name`) ;
    - **whitelist d'extensions** (`.json`, `.csl.json`, `_stats.txt`) ;
    - **confinement DUR sous OUTPUT_DIR** — PAS de repli `_EMITTED_RESOURCES`
      (qui peut pointer hors zone en stdio) ; en HTTP, le Sprint 07 garantit
      qu'aucun outil n'écrit hors OUTPUT_DIR ;
    - `X-Content-Type-Options: nosniff`.

    Protégée par l'auth Bearer (la route est enveloppée dans
    `RequireAuthMiddleware` lors du montage).
    """
    from starlette.responses import FileResponse, PlainTextResponse

    name = request.path_params["filename"]
    # Rejet dur des séparateurs et de la traversal.
    if name != Path(name).name:
        return PlainTextResponse("forbidden", status_code=403)
    if not name.endswith(_ALLOWED_DOWNLOAD_SUFFIXES):
        return PlainTextResponse("not found", status_code=404)

    base = OUTPUT_DIR.resolve()
    target = (base / name)
    # Confinement strict (défense en profondeur) + existence.
    if not target.resolve().is_relative_to(base) or not target.is_file():
        return PlainTextResponse("not found", status_code=404)

    media = "text/plain" if name.endswith(".txt") else "application/json"
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
        return JSONResponse({
            "status": "ok",
            "version": _get_version_string(),
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

    logger.info("Démarrage serveur MCP moisson-shs (HTTP / Streamable HTTP)")
    logger.info(f"OUTPUT_DIR = {OUTPUT_DIR}")
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
