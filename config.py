"""
Configuration globale du moissonneur SHS multi-sources.

Ce module centralise les endpoints, les paramètres de pagination,
les timeouts réseau et les politiques de retry pour faciliter
toute évolution future des API cibles.

Toute modification d'endpoint ou de comportement réseau doit
être effectuée ici et uniquement ici, afin de préserver la
séparation des préoccupations entre couche d'accès et configuration.
"""

import os

# === Endpoints ===

# HAL : API Solr REST
# Documentation : https://api.archives-ouvertes.fr/docs/search
HAL_SEARCH_URL = "https://api.archives-ouvertes.fr/search/"

# Isidore : API REST de recherche
# Documentation : https://isidore.science/api
ISIDORE_SEARCH_URL = "https://api.isidore.science/resource/search"

# OpenEdition : OAI-PMH v2 (la v1 a été supprimée en juin 2025)
# Documentation : https://oai-openedition.readthedocs.io/
OPENEDITION_OAI_URL = "https://metadata.openedition.org/oai"

# Cairn : OAI-PMH
# Documentation : https://oai.cairn.info/
# Note : le path /oai.php est obligatoire depuis la refonte du site (avant 2026
# la racine répondait, désormais elle sert une page HTML d'accueil)
CAIRN_OAI_URL = "https://oai.cairn.info/oai.php"


# === Paramètres réseau ===

# Délai entre requêtes successives, en secondes.
# Tient lieu de civilité réseau face aux serveurs publics
REQUEST_DELAY = float(os.environ.get("MOISSON_REQUEST_DELAY", "0.3"))

# Timeouts réseau, en secondes. On SÉPARE connexion et lecture : un hôte
# injoignable (ex. Isidore en panne) échoue ainsi vite sur la connexion (8 s)
# au lieu de bloquer 60 s par tentative, tout en laissant une marge confortable
# à la lecture des gros lots Solr (45 s). Indispensable côté MCP : empêche un
# moisson_search de dépasser le délai d'appel d'outil de Claude Desktop quand
# une source est lente ou morte. Surchargeables par variables d'environnement.
HTTP_CONNECT_TIMEOUT = float(os.environ.get("MOISSON_HTTP_CONNECT_TIMEOUT", "8"))
HTTP_READ_TIMEOUT = float(os.environ.get("MOISSON_HTTP_READ_TIMEOUT", "45"))
# Tuple (connect, read) consommé directement par requests.
HTTP_TIMEOUT = (HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT)

# Nombre maximal de tentatives en cas d'échec transitoire.
HTTP_MAX_RETRIES = int(os.environ.get("MOISSON_HTTP_MAX_RETRIES", "3"))

# Facteur multiplicatif pour le backoff exponentiel (en secondes).
HTTP_BACKOFF_FACTOR = float(os.environ.get("MOISSON_HTTP_BACKOFF_FACTOR", "1.0"))

# User-Agent transmis à chaque requête.
# L'identification permet aux administrateurs des plateformes
# de tracer les usages et de prévenir d'éventuels abus
USER_AGENT = "moisson_shs/1.0 (research; SHS bibliographic harvester)"


# === Réseau ENTRANT : serveur MCP distant HTTP (Streamable HTTP) ===
#
# ⚠️ À NE PAS CONFONDRE avec les paramètres réseau SORTANT ci-dessus
# (HTTP_TIMEOUT, REQUEST_DELAY… vers HAL/Isidore/…). Les constantes ci-dessous
# pilotent le serveur Streamable HTTP exposé par `moisson-shs serve --http`
# (transport distant pour agents). Inertes en mode stdio (défaut).
#
# Sécurité : le mode HTTP est pensé pour tourner derrière un reverse proxy TLS
# (Caddy) qui termine HTTPS. uvicorn binde donc 127.0.0.1 par défaut ; binder
# sur une interface publique (0.0.0.0) SANS allowlist Host est refusé au
# démarrage (anti DNS-rebinding, cf. mcp_server.serve_http).

# Interface d'écoute uvicorn. En Docker derrière proxy : 0.0.0.0 (jamais publié
# sur l'hôte, cf. `expose` vs `ports` du compose). En local : 127.0.0.1.
HTTP_HOST = os.environ.get("MOISSON_HTTP_HOST", "127.0.0.1")

# Port d'écoute uvicorn.
HTTP_PORT = int(os.environ.get("MOISSON_HTTP_PORT", "8000"))

# Chemin de la route Streamable HTTP (endpoint MCP).
HTTP_PATH = os.environ.get("MOISSON_HTTP_PATH", "/mcp")

# stateless=True désactive la resumabilité ET le flux SSE de progression
# (déconseillé — cf. Sprint 09). Laisser à 0 en production.
HTTP_STATELESS = os.environ.get("MOISSON_HTTP_STATELESS", "0") == "1"

# json_response=True ⇒ réponses JSON simples (pas de SSE). Idem : désactive la
# progression. Laisser à 0.
HTTP_JSON_RESPONSE = os.environ.get("MOISSON_HTTP_JSON_RESPONSE", "0") == "1"

# Allowlist Host (anti DNS-rebinding). Liste séparée par virgules. OBLIGATOIRE
# dès que le bind n'est pas loopback. Les motifs « host:* » sont acceptés par le
# SDK (wildcard de port). Ex. : "moisson.example.org,moisson.example.org:443".
ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get("MOISSON_ALLOWED_HOSTS", "").split(",") if h.strip()
]

# Allowlist Origin (anti DNS-rebinding pour clients navigateur). Origin absent
# (clients non-navigateur, agents) est toujours accepté par le SDK.
ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("MOISSON_ALLOWED_ORIGINS", "").split(",") if o.strip()
]

# Taille maximale du corps de requête (octets). Au-delà : 413. Garde-fou
# mémoire/DoS ; le reverse proxy en pose en général un aussi.
HTTP_MAX_BODY = int(os.environ.get("MOISSON_HTTP_MAX_BODY", str(1_000_000)))

# Rate-limit par IP (requêtes par minute). 0 / vide ⇒ désactivé. ⚠️ Derrière un
# reverse proxy, toutes les requêtes portent l'IP du proxy → garde-fou de
# DERNIER recours seulement (préférer le rate-limit AU proxy). En mémoire,
# cohérent avec workers=1.
_rate = os.environ.get("MOISSON_RATE_LIMIT", "").strip()
HTTP_RATE_LIMIT_PER_MIN = int(_rate) if _rate.isdigit() and int(_rate) > 0 else 0

# Base HTTPS publique pour fabriquer les URLs de téléchargement en mode HTTP
# (Sprint 08). Ex. "https://moisson.example.org". Validée au démarrage HTTP
# (schéma https + hôte) ; invalide/vide ⇒ repli sur URLs relatives.
PUBLIC_BASE_URL = os.environ.get("MOISSON_PUBLIC_BASE_URL", "").strip()

# Préfixe de la route de téléchargement (Sprint 08). Les URLs émises sont
# `<base>{DOWNLOAD_PREFIX}/<nom>`.
DOWNLOAD_PREFIX = os.environ.get("MOISSON_DOWNLOAD_PREFIX", "/download").rstrip("/")


# === Opérations longues (Sprint 09) — moisson sous HTTP distant ===
#
# La moisson dure des minutes : on borne la concurrence, on impose une deadline
# serveur, on plafonne la taille d'une moisson (anti-DoS). Constantes HTTP-only
# (la moisson stdio reste best-effort, sans deadline).

# Plafond serveur DUR du nombre d'items par source (clamp indépendant de l'arg
# client : un client ne peut pas demander 1 000 000). HTTP-only.
MOISSON_MAX_ITEMS_PER_SOURCE = int(
    os.environ.get("MOISSON_MAX_ITEMS_PER_SOURCE", "3000")
)

# Nombre maximum de moissons simultanées (sémaphore). HTTP-only.
MOISSON_MAX_CONCURRENT = int(os.environ.get("MOISSON_MAX_CONCURRENT", "2"))

# Délai d'attente d'un créneau de moisson (acquisition du sémaphore) avant refus.
MOISSON_ACQUIRE_TIMEOUT = float(os.environ.get("MOISSON_ACQUIRE_TIMEOUT", "30"))

# Deadline serveur globale d'une moisson (secondes) → annulation propre.
MOISSON_DEADLINE_SECONDS = float(os.environ.get("MOISSON_DEADLINE_SECONDS", "900"))

# Délai max d'envoi d'une notification de progression (évite un deadlock si la
# boucle est saturée). La progression est best-effort.
MOISSON_PROGRESS_SEND_TIMEOUT = float(
    os.environ.get("MOISSON_PROGRESS_SEND_TIMEOUT", "5")
)


# === OAuth 2.1 pour connecteur claude.ai web (Sprint 12) ===
#
# claude.ai (web) n'accepte QUE OAuth pour les connecteurs distants. Activer
# OAuth ajoute le serveur d'autorisation (authorize/token/register/metadata) en
# parallèle du Bearer statique (qui reste valable pour Claude Desktop/Code).
# HTTP-only.

# Active la couche OAuth 2.1. Sans cela, seul le Bearer statique est exposé.
OAUTH_ENABLED = os.environ.get("MOISSON_OAUTH_ENABLED", "0") == "1"

# Mot de passe de la page de consentement OAuth (OBLIGATOIRE si OAuth activé :
# sinon n'importe qui connaissant l'URL obtiendrait un jeton). À ne pas confondre
# avec MOISSON_API_TOKEN (jeton Bearer statique).
OAUTH_PASSWORD = os.environ.get("MOISSON_OAUTH_PASSWORD", "").strip()


# === Paramètres de pagination ===

# Taille de lot pour l'API HAL (Solr accepte des valeurs élevées)
HAL_ROWS_PER_BATCH = 1000

# Taille de lot pour l'API Isidore (max 100 selon documentation)
ISIDORE_REPLIES_PER_PAGE = 100

# Plafond global de résultats par source pour éviter les corpus
# pléthoriques sur des requêtes trop larges. Peut être surchargé en CLI
DEFAULT_MAX_PER_SOURCE = 1000


# === Identifiants de sources Isidore pour le filtrage proxy ===

# IMPORTANT (constat 2026) : Isidore n'indexe pas par agrégateur (OpenEdition,
# Cairn.info) mais par REVUE individuelle (Semen, Lidil, Protée, etc.).
# Le filtrage par nom de source ne capte donc plus aucun item OpenEdition
# ou Cairn. La stratégie de proxy doit s'appuyer sur le domaine de l'URL
# associée à chaque item (champ isidore.url + isidore.ore.aggregates).
#
# Les constantes ci-dessous restent disponibles pour un éventuel fallback
# nominal, mais le filtrage opérationnel utilise OPENEDITION_URL_MARKER
# et CAIRN_URL_MARKER ci-dessous.

OPENEDITION_URL_MARKER = "openedition.org"
CAIRN_URL_MARKER = "cairn.info"
PERSEE_URL_MARKER = "persee.fr"
THESES_FR_URL_MARKER = "theses.fr"
ERUDIT_URL_MARKER = "erudit.org"

ISIDORE_SOURCE_OPENEDITION = "OpenEdition"
ISIDORE_SOURCE_CAIRN = "Cairn.info"


# === Endpoints OAI des sources additionnelles ===

# Persée : périodiques scientifiques anciens + récents (Lyon)
# Documentation : https://www.persee.fr/openaire
PERSEE_OAI_URL = "https://oai.persee.fr/oai"
PERSEE_OAI_FORMAT = "oai_dc"

# Érudit : revues SHS francophones (Québec/Canada)
# Documentation : https://www.erudit.org/fr/aide/oai-pmh
ERUDIT_OAI_URL = "https://oai.erudit.org/oai/request"
ERUDIT_OAI_FORMAT = "oai_dc"

# Theses.fr : Sudoc/ABES, base nationale des thèses françaises
# Note 2026 : l'API REST publique a été retirée (404 sur /api/v1/*).
# Seul le proxy Isidore reste exploitable pour la recherche full-text.
# Pas d'endpoint OAI public direct connu.


# === Formats OAI pour l'enrichissement ===

# Format prioritaire pour OpenEdition (riche, conforme OpenAIRE v4)
OPENEDITION_OAI_FORMAT = "oai_openaire"

# Format pour Cairn (Dublin Core simple, seul format garanti)
CAIRN_OAI_FORMAT = "oai_dc"


# === Champs HAL retournés par l'API ===

# Liste exhaustive des champs Solr à demander à l'API HAL.
# Limiter cette liste réduit le volume transféré ;
# l'inclure exhaustivement préserve toutes les métadonnées possibles
HAL_FIELDS = [
    # Identifiants
    "docid", "halId_s", "uri_s", "doiId_s",
    # Titres
    "title_s", "subTitle_s", "en_title_s", "fr_title_s",
    # Auteurs
    "authFullName_s", "authLastName_s", "authFirstName_s",
    "authIdHal_s", "authIdHal_i", "authOrcidId_s",
    # Date
    "producedDate_s", "submittedDate_s", "publicationDate_s",
    "producedDateY_i", "submittedDateY_i",
    # Type
    "docType_s", "docSubType_s",
    # Publication
    "journalTitle_s", "journalIssn_s", "journalEissn_s",
    "publisherImprint_s", "publisher_s", "publicationLocation_s",
    "volume_s", "issue_s", "page_s",
    "conferenceTitle_s", "conferenceStartDate_s",
    "conferenceLocation_s", "conferenceOrganizer_s",
    "bookTitle_s", "isbn_s", "issn_s",
    # Contenu
    "abstract_s", "en_abstract_s", "fr_abstract_s",
    "keyword_s", "en_keyword_s", "fr_keyword_s",
    "language_s",
    # Accès
    "fileMain_s", "linkExtId_s", "linkExtUrl_s",
    "openAccess_bool", "submitType_s",
    # Domaines et structures
    "domain_s", "domainAllCode_s",
    "labStructName_s", "labStructAcronym_s",
    "instStructName_s", "instStructAcronym_s",
    # Thèses
    "thesisSupervisor_s", "thesisDefenseDate_s",
    "thesisField_s", "thesisDiscipline_s",
    # Divers
    "publicationDateY_i", "label_s",
]


# === Champs Isidore retournés par l'API ===

# L'API Isidore renvoie par défaut un ensemble structuré ;
# pas besoin de paramètre 'fl' équivalent à Solr


# === Sources internationales (REST natif scholarly) ===
#
# Contrairement aux sources francophones (HAL natif, autres via proxy Isidore),
# ces quatre sources sont des API REST natives avec recherche plein-texte et
# métadonnées riches (citations, concepts, accès ouvert). Trois exigent une clé.
# Spécifications datées de juin 2026 — voir docs/API_SPECS.md (révision annuelle).

# Contact déclaré dans les User-Agent et le polite pool CrossRef / identification
# OpenAlex. Surchargeable par env pour le déploiement.
CONTACT_EMAIL = os.environ.get(
    "MOISSON_CONTACT_EMAIL", "amar.lakel@u-bordeaux-montaigne.fr"
)

# Endpoints internationaux
CROSSREF_API = "https://api.crossref.org/works"
OPENALEX_API = "https://api.openalex.org/works"
S2_GRAPH_API = "https://api.semanticscholar.org/graph/v1"
CORE_API = "https://api.core.ac.uk/v3"
CORE_SEARCH_WORKS = f"{CORE_API}/search/works"

# Noms des variables d'environnement attendues (cf. utils/secrets.py)
ENV_OPENALEX_KEY = "OPENALEX_API_KEY"      # requis (clé gratuite)
ENV_S2_KEY = "SEMANTIC_SCHOLAR_API_KEY"    # optionnel (recommandé)
ENV_CORE_KEY = "CORE_API_KEY"              # requis (sur inscription)

# Espacement minimal inter-requêtes (secondes) pour les sources à débit
# contraint. Surchargeables par env selon le quota réel du compte.
REQUEST_DELAY_S2 = float(os.environ.get("MOISSON_REQUEST_DELAY_S2", "1.05"))
REQUEST_DELAY_CORE = float(os.environ.get("MOISSON_REQUEST_DELAY_CORE", "6.0"))

# Seuil de score minimal pour retenir un concept OpenAlex comme mot-clé
# (réduit le bruit des concepts de bas niveau de confiance).
OPENALEX_CONCEPT_SCORE_MIN = float(
    os.environ.get("MOISSON_OPENALEX_CONCEPT_SCORE_MIN", "0.3")
)
