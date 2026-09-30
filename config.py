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
# Tient lieu de civilité réseau face aux serveurs publics. Depuis la politique
# HTTP commune (utils/http.py), c'est l'intervalle minimal entre deux départs
# de requête réseau vers un même hôte, tous threads confondus (auparavant : un
# sommeil fixe après chaque réponse, propre à chaque thread). Aucune attente
# pour une réponse servie par le cache. Les hôtes à débit contraint ont leur
# propre intervalle (REQUEST_DELAY_S2, _CORE, _SERPAPI, _CROSSREF ci-dessous).
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

# --- Politique HTTP commune (cf. utils/http.py, docstring du module) --------
#
# Une seule politique pour HTTPClient, utils.http_scholarly, les connecteurs,
# utils.lookups et utils.doi_resolver :
# - 4xx définitif (400, 401, 403, 404, 405, 410, 422…) : une seule tentative ;
# - 429 et transitoires (500, 502, 503, 504, délai dépassé, connexion) : au
#   plus HTTP_MAX_RETRIES tentatives, Retry-After respecté, backoff
#   exponentiel (HTTP_BACKOFF_FACTOR × 2^(n-1)), bornés par les plafonds
#   ci-dessous ;
# - politesse par hôte et par compte (REQUEST_DELAY par défaut, intervalles
#   propres à Semantic Scholar, CORE, SerpAPI et CrossRef), sommeil
#   uniquement sur accès réseau réel.

# Plus long Retry-After honoré (secondes). Au-delà, la requête est abandonnée
# (erreur HTTP 429/503 levée) plutôt que réessayée plus tôt que ne le demande
# le serveur ou mise en attente au-delà du délai d'outil MCP. Un Retry-After
# supérieur à une heure reste traité comme un épuisement de budget par
# utils.http_scholarly (BudgetExhausted).
HTTP_RETRY_AFTER_MAX = float(os.environ.get("MOISSON_HTTP_RETRY_AFTER_MAX", "60"))

# Plafond du temps d'attente cumulé entre les tentatives d'une même requête
# logique (secondes). Avec les valeurs par défaut (3 tentatives, backoff 1 s)
# les attentes cumulées valent 3 s ; le plafond ne mord que sur des
# Retry-After longs répétés.
HTTP_MAX_TOTAL_WAIT = float(os.environ.get("MOISSON_HTTP_MAX_TOTAL_WAIT", "60"))

# Budget global d'une requête logique (secondes) : tentatives, attentes et
# lectures comprises. Chaque délai de connexion/lecture est borné par le
# budget restant ; aucune tentative ne démarre budget épuisé. 0 ⇒ sans budget.
HTTP_REQUEST_BUDGET = float(os.environ.get("MOISSON_HTTP_REQUEST_BUDGET", "120"))

# Plafond de requêtes simultanées vers un même hôte, tous threads confondus
# (plusieurs workers ou proxies interrogeant Isidore ne multiplient pas la
# charge au-delà de ce nombre).
HTTP_MAX_IN_FLIGHT_PER_HOST = int(
    os.environ.get("MOISSON_HTTP_MAX_IN_FLIGHT_PER_HOST", "2")
)

# --- Cache HTTP (requests-cache) --------------------------------------------
#
# Option à trois états dans la CLI (--cache / --no-cache / absente) et dans
# l'outil MCP moisson_search (argument cache : true / false / absent) :
# - CLI, option absente ⇒ héritée de MOISSON_CACHE (0/1), lue à la création
#   du client HTTP (défaut : désactivé) ;
# - MCP, argument absent ⇒ MOISSON_CACHE s'il est défini dans l'environnement
#   du serveur (choix explicite de l'exploitant), sinon ACTIVÉ avec la durée
#   de vie MCP_CACHE_TTL (OPT-09, décision du plan) ; true / false explicites
#   priment toujours.

# Durée de vie des réponses mises en cache (secondes), défaut 24 h. Valeur et
# variable d'environnement inchangées (auparavant lues dans utils/http.py).
HTTP_CACHE_TTL = int(os.environ.get("MOISSON_CACHE_TTL", str(86400)))

# Durée de vie du cache côté serveur MCP (secondes), défaut 6 h : plus courte
# que celle de la CLI, parce qu'une moisson lancée depuis un agent doit
# refléter l'état récent des sources. Appliquée par moisson_search (OPT-09)
# dès que le cache est actif, par défaut ou sur demande. requests-cache fixe
# l'expiration d'une réponse à son écriture : pour qu'une réponse écrite par
# la CLI (24 h) ne soit jamais servie au-delà de 6 h, le serveur MCP tient son
# propre fichier de cache (MOISSON_CACHE_DIR suffixé de « _mcp »).
MCP_CACHE_TTL = int(os.environ.get("MOISSON_MCP_CACHE_TTL", str(6 * 3600)))

# Entretien du fichier de cache du serveur MCP (revue d'OPT-09). requests-cache
# ne retire jamais une réponse expirée (il ne la remplace que si la même
# requête revient) : sans entretien, le cache actif par défaut grossirait
# d'environ 15 Mo par moisson distincte, indéfiniment. Avant une moisson, au
# plus une fois par MCP_CACHE_PRUNE_INTERVAL secondes et par processus, le
# serveur retire les réponses expirées, puis les plus anciennes au-delà de
# MCP_CACHE_MAX_MB, et compacte le fichier (VACUUM) quand l'espace libéré
# domine et que le reste est petit (cf. utils.http.prune_sqlite_cache).
# MOISSON_MCP_CACHE_PRUNE_INTERVAL : 0 = à chaque moisson, négatif = jamais.
MCP_CACHE_PRUNE_INTERVAL = int(
    os.environ.get("MOISSON_MCP_CACHE_PRUNE_INTERVAL", "3600")
)
# Plafond (Mo) des réponses conservées dans le cache du serveur MCP, appliqué à
# l'entretien (le fichier peut donc le dépasser d'une heure d'activité).
# MOISSON_MCP_CACHE_MAX_MB : 0 = aucun plafond (seules les expirées partent).
MCP_CACHE_MAX_MB = int(os.environ.get("MOISSON_MCP_CACHE_MAX_MB", "512"))

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

# Taille de lot maximale pour l'API HAL (Solr accepte des valeurs élevées ;
# l'API refuse rows > 10 000). Le connecteur demande
# rows = min(HAL_ROWS_PER_BATCH, reste à collecter) à chaque lot, dans les
# deux modes (OPT-12 et sa revue) : plus jamais 1 000 documents (4,9 Mo)
# téléchargés pour en garder 200, ni au premier lot ni au dernier (par
# exemple 1000 puis 200 pour max_results = 1200).
# Mesure live du 2026-09-29 (00 h 05, « discours numérique » depuis 2018, tri
# par score, fl complet + score) : rows 200 : 0,37 s et 0,85 Mo ; rows 500 :
# 0,55 s et 2,2 Mo ; rows 1000 : 0,76 et 0,91 s, 4,9 Mo. Une requête de 1000
# coûte donc bien moins que cinq de 200 (5 × 0,37 s + politesse de 0,3 s
# entre elles) : 1000 reste le plafond du lot. MOISSON_HAL_ROWS_PER_BATCH
# (entier de 1 à 10 000).
HAL_ROWS_PER_BATCH = max(1, min(10000, int(
    os.environ.get("MOISSON_HAL_ROWS_PER_BATCH", "1000"))))

# Borne de la pagination par rang (start/rows) de HAL en mode bibliographie
# (OPT-12, constat C25) : au plus ce nombre de documents, classés par score
# décroissant (start + rows <= borne). La pagination profonde par start
# devient coûteuse (live 2026-09-29 : start=10 000 servi en 2,3 s contre
# 0,15 à 0,3 s en tête de liste) et le guide Solr réserve le parcours
# exhaustif à cursorMark (mode collecte). Au-delà, la source est
# ``partial`` (motif ``scan_bound``). MOISSON_HAL_RANKED_MAX_RESULTS
# (entier >= 1).
HAL_RANKED_MAX_RESULTS = max(1, int(
    os.environ.get("MOISSON_HAL_RANKED_MAX_RESULTS", "10000")))

# Tri de Semantic Scholar en mode bibliographie (OPT-12, constat C26),
# paramètre `sort` de /paper/search/bulk. Sans tri, bulk renvoie les
# résultats par paperId croissant (vérifié le 2026-09-29) : la troncature à
# max_results retenait un sous-ensemble arbitraire. Le point d'accès classé
# par pertinence (/paper/search) est impraticable sans clé (8 réponses 429
# sur 8 le 2026-09-29, pool partagé) : bulk est trié par citations
# décroissantes, biais documenté (travaux anciens et très cités favorisés).
# Valeurs admises par l'API : citationCount, publicationDate, paperId
# (suffixe :asc ou :desc). En mode collecte, aucun tri n'est envoyé (ordre
# paperId de l'API, déterministe). MOISSON_S2_BIBLIO_SORT.
S2_BIBLIO_SORT = os.environ.get("MOISSON_S2_BIBLIO_SORT", "citationCount:desc").strip()

# Taille de lot pour l'API Isidore (paramètre `replies`). La documentation
# (isidore.science/api) annonce 1 000 documents au plus par requête ; en
# pratique l'API ne sert que la fenêtre de ISIDORE_RESULT_WINDOW résultats
# (vérifié en live le 2026-09-28 : replies=200 → 200 notices en une requête
# de 9,3 Mo, replies=250 → 200 ; revérifié le 2026-09-29 : 200 notices, 3,3 s,
# 9,26 Mo, sans bloc `page`). Défaut 200 depuis OPT-12 : UNE requête sert
# toute la fenêtre au flux partagé de la moisson (source Isidore et cinq
# sous-sources), au lieu de deux pages de 100 (≈ 4,7 s en live). Le rang
# natif d'une notice Isidore est sa position dans cette fenêtre (l'API
# n'expose aucun score). La recherche par DOI (utils/lookups.py) a sa propre
# taille (10). Surchargeable par MOISSON_ISIDORE_REPLIES_PER_PAGE
# (entier >= 1).
ISIDORE_REPLIES_PER_PAGE = max(
    1, int(os.environ.get("MOISSON_ISIDORE_REPLIES_PER_PAGE", "200"))
)

# Fenêtre de résultats servie par Isidore (resource/search), en notices :
# l'API annonce le total réel (@items, ex. 266 508) mais ne sert que les 200
# premiers résultats ; toute page au-delà répète la dernière fenêtre (constat
# C35, mesuré les 2026-09-28 : replies=20, pages 11 et 12 identiques à la page
# 10, dont le bloc `page` indique alors @current=10 et aucun @next). Le
# parcours mutualisé (connectors/isidore_scan.py) cesse de demander des pages
# dès que la position atteint cette fenêtre. 0 désactive la règle (si Isidore
# levait la fenêtre : la détection des pages répétées et la borne
# ISIDORE_SCAN_MAX_RECORDS restent actives). MOISSON_ISIDORE_RESULT_WINDOW.
ISIDORE_RESULT_WINDOW = max(
    0, int(os.environ.get("MOISSON_ISIDORE_RESULT_WINDOW", "200"))
)

# Borne de sécurité du parcours Isidore mutualisé : nombre maximal de notices
# distinctes lues pour une même requête au cours d'une exécution (toutes
# sous-sources confondues). Ne mord que si la fenêtre est levée ou désactivée ;
# remplace l'ancienne borne `ceil(max_results × 100 / 100) + 10` pages par
# proxy (jusqu'à 101 000 notices pour max_per_source=1000, constat C10).
# MOISSON_ISIDORE_SCAN_MAX_RECORDS (entier >= 1).
ISIDORE_SCAN_MAX_RECORDS = max(
    1, int(os.environ.get("MOISSON_ISIDORE_SCAN_MAX_RECORDS", "3000"))
)

# Reprise différée d'une page Isidore en échec, au sein d'une moisson (OPT-09,
# topologie plate). La source directe et les cinq sous-sources démarrent
# ensemble et lisent le même flux : sans reprise, une erreur passagère (après
# les HTTP_MAX_RETRIES tentatives de la politique commune) serait partagée par
# les six consommateurs pour toute l'exécution. Le flux attend donc ce délai
# (secondes) puis retente UNE fois la page (une seule tentative réseau) ; les
# consommateurs attendent l'issue et la partagent. 0 : reprise immédiate ;
# négatif : aucune reprise différée. MOISSON_ISIDORE_DEFERRED_RETRY_S.
ISIDORE_DEFERRED_RETRY_S = float(
    os.environ.get("MOISSON_ISIDORE_DEFERRED_RETRY_S", "5")
)
# Budget (secondes) de la tentative différée : borne la connexion et chaque
# attente d'octets (revue d'OPT-09 ; une page Isidore de 4,6 Mo arrive en 2 à
# 4 s en live). Sous une échéance d'exécution (transport HTTP), la reprise
# n'est tentée que s'il reste au moins délai + budget + 1 s, et son budget est
# ramené pour finir 1 s avant l'échéance : son échec est alors une erreur de
# page, jamais l'annulation de toute la moisson. Pas de reprise différée après
# un délai dépassé (le service ne répond plus : les tentatives de la politique
# commune ont déjà attendu chacune un délai complet).
# MOISSON_ISIDORE_DEFERRED_RETRY_BUDGET_S (> 0).
ISIDORE_DEFERRED_RETRY_BUDGET_S = max(0.1, float(
    os.environ.get("MOISSON_ISIDORE_DEFERRED_RETRY_BUDGET_S", "15")
))

# Plafond global de résultats par source pour éviter les corpus
# pléthoriques sur des requêtes trop larges. Peut être surchargé en CLI
# (--max). Défaut de la CLI (mode collecte) ; l'outil MCP moisson_search a le
# sien (MCP_DEFAULT_MAX_PER_SOURCE ci-dessous).
DEFAULT_MAX_PER_SOURCE = 1000

# --- Défauts de l'outil MCP moisson_search (OPT-09, décisions du plan §10) ---
#
# Une moisson lancée depuis un agent sans paramètre doit tenir sous le délai
# d'appel d'outil du client : plafond PAR source réduit (bibliographie, pas
# collecte exhaustive ; l'agent l'augmente explicitement pour un corpus
# exhaustif), sources interrogées en parallèle, cache actif (MCP_CACHE_TTL).
# La CLI garde ses défauts (DEFAULT_MAX_PER_SOURCE, --workers 1, cache hérité
# de MOISSON_CACHE). En HTTP distant, MOISSON_MAX_ITEMS_PER_SOURCE plafonne
# toujours la valeur demandée.
MCP_DEFAULT_MAX_PER_SOURCE = max(
    1, int(os.environ.get("MOISSON_MCP_MAX_PER_SOURCE", "200"))
)
MCP_DEFAULT_WORKERS = max(1, int(os.environ.get("MOISSON_MCP_WORKERS", "4")))

# Borne du parallélisme d'une moisson (tous points d'entrée) : une valeur
# `workers` supérieure est ramenée à ce plafond, sans erreur (compatibilité
# des arguments existants). Le pool ne dépasse jamais non plus le nombre de
# sources, et la politesse par hôte de utils/http.py (intervalle entre deux
# départs, requêtes en vol) s'applique quel que soit le parallélisme.
# MOISSON_HARVEST_MAX_WORKERS (entier >= 1).
HARVEST_MAX_WORKERS = max(
    1, int(os.environ.get("MOISSON_HARVEST_MAX_WORKERS", "8"))
)

# --- Échéance souple de moisson_search en stdio (OPT-09 bis) ---
#
# Le client MCP annule un appel d'outil qui dépasse son délai, et une moisson
# annulée ne publie rien : une seule source muette (délais de lecture, jusqu'à
# HTTP_REQUEST_BUDGET = 120 s) faisait perdre les sources saines avec elle.
# Délais constatés (docs/FAQ.md) : Claude Desktop annule un appel d'outil au
# bout de 4 min (240 s mesurées dans ses journaux, 2026-06) ; le SDK client
# TypeScript fixe 60 s par défaut, sans report sur notification de progression
# sauf option explicite du client ; d'autres clients MCP de bureau
# annulent à 60 s (signalé en 2026-05). La borne prudente retenue est donc
# 60 s : l'échéance souple tombe à 50 s, ce qui laisse 10 s pour le délai de
# grâce ci-dessous, la déduplication, l'écriture des fichiers et le retour.
#
# À l'échéance, les sources encore en cours sont arrêtées (signal propre à
# chaque source, jamais celui de l'exécution) et marquées avec le motif
# « deadline » ; les sources qui n'ont pas démarré sont « non exécutées » ; le
# corpus des autres sources est publié (succès partiel nommé). Une source
# arrêtée garde les notices déjà reçues (« partielle » : les connecteurs
# suivent le contrat RunContext.soft_stop, OPT-09 ter) ; arrêtée pendant
# enrich_oai, elle garde toutes ses notices Isidore, celles déjà enrichies le
# restant, sauf si elle reste bloquée dans une requête OAI (GetRecord de
# sickle, non interruptible) au-delà du délai de grâce : abandonnée, ses
# notices Isidore sont alors reprises sans aucun enrichissement ; arrêtée
# avant toute notice, elle est « en échec ». Une source
# lente mais saine est donc concernée aussi (mode collecte, enrich_oai, gros
# max_per_source) : relever deadline_seconds pour elle. Stdio seulement : en HTTP, la
# deadline DURE (MOISSON_DEADLINE_SECONDS, aucune publication partielle) est
# inchangée ; la CLI n'a aucune échéance. L'argument deadline_seconds de
# moisson_search la remplace pour un appel (borné par les deux constantes
# suivantes ; 0 : aucune échéance, comportement historique).
# MOISSON_STDIO_SOFT_DEADLINE (secondes ; 0 ou négatif : désactivée).
STDIO_SOFT_DEADLINE_S = max(
    0.0, float(os.environ.get("MOISSON_STDIO_SOFT_DEADLINE", "50"))
)
# Bornes de l'argument deadline_seconds (secondes) : une valeur positive
# inférieure au minimum est relevée (en deçà, la plupart des sources n'ont pas
# le temps de répondre), une valeur supérieure au maximum est abaissée.
STDIO_SOFT_DEADLINE_MIN_S = 10
STDIO_SOFT_DEADLINE_MAX_S = 900
# Délai de grâce après l'échéance souple (secondes) : une source arrêtée rend
# normalement la main aussitôt (ses requêtes en vol ont leur délai borné par
# l'échéance) ; une source encore occupée après ce délai (travail non
# interruptible : mappage d'une grande page, transport OAI propre) est
# abandonnée, son résultat ignoré. MOISSON_STDIO_SOFT_DEADLINE_GRACE (>= 0).
STDIO_SOFT_DEADLINE_GRACE_S = max(
    0.0, float(os.environ.get("MOISSON_STDIO_SOFT_DEADLINE_GRACE", "2"))
)

# Intervalle (secondes) des notifications de progression émises pendant
# qu'une moisson attend des sources encore en cours (aucune source terminée
# depuis cet intervalle) ; seulement si le client a demandé la progression
# (progressToken). La valeur de progression croît toujours (spécification
# MCP). Aucune notification après l'échéance souple. MOISSON_PROGRESS_HEARTBEAT
# (0 ou négatif : aucune notification intermédiaire).
HARVEST_PROGRESS_HEARTBEAT_S = max(
    0.0, float(os.environ.get("MOISSON_PROGRESS_HEARTBEAT", "10"))
)


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
# contraint. Surchargeables par env selon le quota réel du compte. Appliqués
# par hôte et par compte (empreinte de la clé) par utils/http.py, entre
# threads compris, et jamais à une réponse servie par le cache.
REQUEST_DELAY_S2 = float(os.environ.get("MOISSON_REQUEST_DELAY_S2", "1.05"))
REQUEST_DELAY_CORE = float(os.environ.get("MOISSON_REQUEST_DELAY_CORE", "6.0"))

# Seuil de score minimal d'un concept OpenAlex retenu dans la ligne
# « OpenAlex concepts » d'extra (réduit le bruit des concepts de bas niveau
# de confiance). Les concepts n'entrent plus dans les mots-clés (PAT-04,
# retour d'expérience du 2026-09-30) : ce seuil ne règle plus que la trace.
OPENALEX_CONCEPT_SCORE_MIN = float(
    os.environ.get("MOISSON_OPENALEX_CONCEPT_SCORE_MIN", "0.3")
)

# --- Google Scholar via SerpAPI --------------------------------------------
#
# Google Scholar n'expose aucune API publique. SerpAPI (serpapi.com) sert de
# passerelle : une requête HTTP y équivaut à une page de résultats Scholar.
# Deux propriétés distinguent cette source des quatre autres sources
# internationales, et commandent toute la configuration ci-dessous :
#
# 1. Elle est *facturée à la requête*. Le compte utilisé est une offre payante
#    à 5 000 requêtes par mois, partagée avec le projet MyWebIntelligence.
#    Le coût n'est donc pas un facteur limitant à l'échelle d'une moisson (une
#    boucle de quatre requêtes à 120 items coûte 24 crédits, soit 0,5 % du
#    mois), mais il reste borné explicitement : un plafond de pages par appel
#    et un plancher de solde, pour qu'une boucle mal paramétrée ne puisse pas
#    vider le quota des deux projets d'un coup.
# 2. Elle ne fournit ni DOI, ni résumé structuré, ni liste d'auteurs complète.
#    Google Scholar renvoie une ligne de notice tronquée (« A Auteur, B Auteur…
#    - Revue …, 2020 - domaine.org ») que le mapper décompose heuristiquement.
#    Les items produits sont donc plus pauvres que ceux de CrossRef ou
#    OpenAlex : la source vaut pour sa *couverture* (littérature grise, actes,
#    chapitres, dépôts institutionnels absents des bases à DOI) et pour son
#    compteur de citations, pas pour la qualité de ses métadonnées.
SERPAPI_SEARCH = "https://serpapi.com/search.json"
SERPAPI_ACCOUNT = "https://serpapi.com/account.json"  # gratuit, hors quota

ENV_SERPAPI_KEY = "SERPAPI_API_KEY"        # requis (compte serpapi.com)

# Nombre de résultats demandés par requête SerpAPI. Google Scholar plafonne à
# 20 ; le laisser au maximum minimise le nombre de requêtes facturées.
SERPAPI_PAGE_SIZE = int(os.environ.get("MOISSON_SERPAPI_PAGE_SIZE", "20"))

# Plafond dur de requêtes facturées par appel à `search()`. À 20 résultats par
# page, la valeur par défaut borne une moisson à 200 items pour 10 crédits,
# ce qui couvre le `max_per_source=120` usuel des boucles d'augmentation
# bibliographique (6 crédits) avec de la marge.
SERPAPI_MAX_PAGES = int(os.environ.get("MOISSON_SERPAPI_MAX_PAGES", "10"))

# Espacement minimal inter-requêtes (secondes).
REQUEST_DELAY_SERPAPI = float(
    os.environ.get("MOISSON_REQUEST_DELAY_SERPAPI", "1.0")
)

# Sous ce solde de requêtes restantes, la source refuse de démarrer une moisson
# (vérification via l'Account API, gratuite). Le plancher protège la réserve de
# fin de mois, et le quota partagé avec MyWebIntelligence. 0 le désactive.
SERPAPI_MIN_CREDITS = int(os.environ.get("MOISSON_SERPAPI_MIN_CREDITS", "100"))

# --- Résolution de DOI par appariement bibliographique CrossRef -------------
#
# Google Scholar ne renvoie pas de DOI, ce qui coupe le chemin d'import
# automatique dans Zotero (`zotero_add_by_doi`). La résolution consiste à
# retrouver le DOI d'une notice à partir de son titre, de son premier auteur et
# de son année, via `query.bibliographic` de CrossRef (gratuit, sans clé).
#
# L'opération est un appariement, donc faillible. Tout le réglage ci-dessous
# vise une seule propriété : qu'un DOI faux soit plus improbable qu'un DOI
# manquant. Un seuil trop bas colle à une notice le DOI d'un homonyme, d'une
# version antérieure ou d'un compte rendu, et cette erreur se propage ensuite
# dans Zotero sans laisser de trace.
CROSSREF_RESOLVE_ROWS = int(os.environ.get("MOISSON_DOI_RESOLVE_ROWS", "5"))

# Similarité minimale des titres normalisés (SequenceMatcher, 0 à 1).
DOI_TITLE_THRESHOLD = float(
    os.environ.get("MOISSON_DOI_TITLE_THRESHOLD", "0.90")
)

# Seuil relevé appliqué quand ni l'année ni l'auteur ne sont vérifiables :
# sans corroboration, seul un titre quasi identique est accepté.
DOI_TITLE_THRESHOLD_UNCORROBORATED = float(
    os.environ.get("MOISSON_DOI_TITLE_THRESHOLD_STRICT", "0.97")
)

# Écart d'année toléré (publication en ligne d'abord, puis en numéro).
DOI_YEAR_TOLERANCE = int(os.environ.get("MOISSON_DOI_YEAR_TOLERANCE", "1"))

# Bornes (OPT-10) : les variables d'environnement ci-dessus peuvent DURCIR la
# règle, jamais l'assouplir. Une valeur sous les planchers (titre 0.90, seuil
# renforcé 0.97, candidats 5) ou au-delà du plafond (tolérance d'année 1) est
# refusée à l'usage par utils/doi_resolver.py (DoiRuleError), pas appliquée.
# Les planchers vivent dans ce module et ne sont pas surchargeables.

# Politesse CrossRef (OPT-09), appliquée par utils/http.py à api.crossref.org.
# CrossRef distingue deux familles de requêtes, comptées dans deux pools
# (limites en vigueur depuis le 2025-12-01, billet « Announcing changes to
# REST API rate limits », et en-têtes relevés en live le 2026-09-28 à 21 h 17
# et 21 h 18 : x-rate-limit-limit, x-rate-limit-interval, x-concurrency-limit,
# x-api-pool) :
# - requêtes de LISTE (/works?query…, filter, rows : connecteur CrossRef,
#   résolution de DOI par query.bibliographic), pool poli avec mailto
#   (« polite-array ») : 3 requêtes/s, 3 simultanées ; sans mailto : 1/s, 1 ;
# - requêtes d'un TRAVAIL UNIQUE (/works/{doi} : fetch_by_doi), pool poli
#   (« polite-single ») : 10 requêtes/s, 3 simultanées ; sans mailto : 5/s, 1.
# Les intervalles ci-dessous sont des planchers : utils/http.py lit aussi ces
# en-têtes à chaque réponse et applique la limite annoncée si elle est plus
# stricte (jamais pour relâcher le plancher).

# Espacement minimal entre deux départs de requêtes d'un travail unique
# (/works/{doi}), en secondes : 0,15 s = 6,7/s, sous les 10/s annoncés.
# Valeur et variable inchangées ; c'est aussi l'intervalle par défaut de l'hôte.
REQUEST_DELAY_CROSSREF = float(
    os.environ.get("MOISSON_REQUEST_DELAY_CROSSREF", "0.15")
)

# Espacement minimal entre deux départs de requêtes de liste (secondes) :
# 0,34 s = 2,94/s, sous les 3/s annoncés pour le pool poli, quelle que soit la
# latence. Auparavant 0,15 s pour toutes les requêtes CrossRef, ce qui
# dépassait la limite des listes dès que leur latence descendait sous 0,67 s.
# Conséquence mesurée (OPT-09) : le plancher de la résolution de 100 DOI passe
# de 15,15 s à 34 s (critère « 100 notices < 15 s » du plan inatteignable sous
# la limite annoncée). MOISSON_REQUEST_DELAY_CROSSREF_LIST.
REQUEST_DELAY_CROSSREF_LIST = float(
    os.environ.get("MOISSON_REQUEST_DELAY_CROSSREF_LIST", "0.34")
)

# Requêtes simultanées vers api.crossref.org (toutes familles confondues) :
# 3, la concurrence annoncée des deux pools polis (au lieu du plafond générique
# HTTP_MAX_IN_FLIGHT_PER_HOST=2). Ramenée automatiquement à la valeur de
# l'en-tête x-concurrency-limit si CrossRef en annonce une plus basse (pool
# public : 1). MOISSON_CROSSREF_MAX_IN_FLIGHT (entier >= 1).
CROSSREF_MAX_IN_FLIGHT = max(
    1, int(os.environ.get("MOISSON_CROSSREF_MAX_IN_FLIGHT", "3"))
)

# Requêtes CrossRef simultanées de la résolution de DOI (OPT-10). Plafonnée à
# 3 dans utils/doi_resolver.py (concurrence annoncée du polite pool CrossRef),
# toujours sous la politesse par hôte de utils/http.py (depuis OPT-09 :
# REQUEST_DELAY_CROSSREF_LIST entre deux départs de requêtes de liste,
# CROSSREF_MAX_IN_FLIGHT requêtes en vol) : le pool recouvre les latences, il
# n'augmente pas le débit autorisé.
DOI_RESOLVE_WORKERS = int(os.environ.get("MOISSON_DOI_RESOLVE_WORKERS", "3"))

# Budget en temps par défaut de l'outil MCP corpus_resolve_dois (secondes). À
# l'échéance, aucune nouvelle requête ne part et les notices restantes sont
# déclarées « non traité » dans le rapport (une nouvelle passe sur le corpus
# produit les reprend, les notices déjà examinées étant servies par le cache
# mémoire). L'argument max_seconds de l'outil est borné par
# DOI_RESOLVE_MAX_SECONDS_CEILING. La fonction resolve_dois, appelée en Python,
# n'a pas de budget par défaut.
DOI_RESOLVE_MAX_SECONDS = float(
    os.environ.get("MOISSON_DOI_RESOLVE_MAX_SECONDS", "240")
)
DOI_RESOLVE_MAX_SECONDS_CEILING = float(
    os.environ.get("MOISSON_DOI_RESOLVE_MAX_SECONDS_CEILING", "900")
)

# Cache mémoire des requêtes bibliographiques normalisées (titre normalisé,
# premier auteur, nombre de candidats) : nombre d'entrées (LRU) et durée de vie
# (secondes, défaut 6 h). Seules les réponses réussies y entrent ; une erreur
# réseau est toujours retentée.
DOI_QUERY_CACHE_SIZE = int(os.environ.get("MOISSON_DOI_QUERY_CACHE_SIZE", "4096"))
DOI_QUERY_CACHE_TTL = float(
    os.environ.get("MOISSON_DOI_QUERY_CACHE_TTL", str(6 * 3600))
)

# --- Recherches ciblées : fetch_by_doi et search_by_author (OPT-10) --------
#
# Les résolveurs (CrossRef, HAL, Isidore, OpenAlex, Semantic Scholar, CORE)
# et les sources de search_by_author (HAL, Isidore, OpenAlex, S2) sont
# interrogés en parallèle sur un pool borné ; chacun vise un hôte distinct et
# reste soumis à la politesse par hôte de utils/http.py.
LOOKUP_MAX_WORKERS = int(os.environ.get("MOISSON_LOOKUP_MAX_WORKERS", "6"))

# fetch_by_doi : délai de grâce (secondes) accordé aux résolveurs moins
# prioritaires une fois le résultat prioritaire connu (tous les résolveurs de
# rang supérieur ont répondu et l'un d'eux a trouvé la notice). Passé ce délai,
# les retardataires sont abandonnés (statut « abandonne ») : leur
# enrichissement est perdu, la notice prioritaire est rendue. 0 = abandon
# immédiat.
FETCH_BY_DOI_GRACE = float(os.environ.get("MOISSON_FETCH_BY_DOI_GRACE", "5"))

# fetch_by_doi : durée maximale totale (secondes), résolveur prioritaire
# compris. Au-delà, les résolveurs encore en cours sont abandonnés. Ce reste
# de durée est aussi le budget transmis à la politique HTTP pour chaque
# requête d'un résolveur : aucune ne part après l'échéance.
FETCH_BY_DOI_MAX_SECONDS = float(
    os.environ.get("MOISSON_FETCH_BY_DOI_MAX_SECONDS", "60")
)

# fetch_by_doi : places par hôte réservées aux résolveurs, tous appels du
# processus confondus (revue OPT-10). Une requête de résolveur attend sa place
# dans une file interruptible : abandonnée (délai de grâce, durée maximale),
# elle n'est jamais envoyée. Ainsi une rafale d'appels fetch_by_doi sur un
# hôte lent (ex. Isidore) n'empile pas de requêtes inutiles dans les places
# en vol de utils/http.py (HTTP_MAX_IN_FLIGHT_PER_HOST) et n'affame pas une
# moisson concurrente : au plus ce nombre de requêtes de résolveur occupe ces
# places, le reste est abandonné sans envoi. Au moins 1.
FETCH_BY_DOI_HOST_SLOTS = max(
    1, int(os.environ.get("MOISSON_FETCH_BY_DOI_HOST_SLOTS", "1"))
)


# === Classement des notices d'une moisson (OPT-13) ===
#
# Classeur appliqué après la déduplication quand la demande n'en fixe pas
# (argument ranking de moisson_search, option --ranking de la CLI) :
#   - native (défaut) : fusion de rangs réciproques (k = 60) des rangs natifs
#     publiés par les sources, pondérée par l'ordre réellement appliqué :
#     pertinence vérifiée (HAL et CrossRef en mode bibliographie, OpenAlex)
#     poids 1 ; ordre par défaut présumé d'Isidore et de ses sous-sources
#     (non vérifié, sous-sources comptées une fois) poids 0,5 ; autres ordres
#     (docid, index CrossRef, paperId, citations) et sources sans rang (CORE,
#     Google Scholar) en queue, dans leur ordre natif ;
#   - local : correspondance des concepts du plan de requête (titre,
#     mots-clés, résumé ; pondération simple et BM25 comparées) combinée au
#     classement natif ; DÉSACTIVÉ par défaut jusqu'à satisfaction des
#     critères du protocole d'évaluation (OPT-15) ;
#   - off : aucun classement.
# Les paramètres du classement sont figés avec sa version
# (utils/ranking.py, RANKING_VERSION) et ne se surchargent pas ici. Le
# classement ne modifie ni les notices (aucun champ ajouté, invariant 1) ni
# l'ordre des fichiers de corpus ; il est rendu dans HarvestResult.ranking et
# structuredContent.top_ranked. Valeur inconnue : native (avertissement dans
# le journal). MOISSON_RANKING (native, local ou off).
RANKING_DEFAULT = os.environ.get("MOISSON_RANKING", "native").strip().lower() or "native"


# === Publication des corpus : manifeste, écriture atomique, index (OPT-16) ===
#
# Chaque production (moisson CLI et MCP, corpus_filter, corpus_to_csl,
# corpus_merge, search_by_author, corpus_resolve_dois) écrit ses fichiers dans
# des temporaires du même dossier (``.<nom>.<uuid>.tmp``, jamais listés), les
# synchronise sur disque, les renomme atomiquement (os.replace) puis publie le
# manifeste ``<stem>.manifest.json`` EN DERNIER, marqueur de complétude
# (utils/manifest.py). L'index dérivé OUTPUT_DIR/.index.json sert corpus_list
# et list_resources sans relire chaque corpus.

# Synchronisation disque (fsync) des temporaires et du dossier avant et après
# les renommages : 1 (défaut) ; 0 la désactive (disque jetable, tests de
# charge), au prix de la durabilité en cas de coupure de courant.
PUBLISH_FSYNC = os.environ.get("MOISSON_PUBLISH_FSYNC", "1") != "0"

# Âge (secondes) au-delà duquel un temporaire d'écriture orphelin du
# moissonneur (publication interrompue) est supprimé lors d'une publication ou
# d'une mise à jour de l'index. Seul le motif exact de ses temporaires est
# concerné (.<nom publié>.<12 chiffres hexadécimaux>.tmp) : un fichier caché
# .<x>.tmp d'un autre logiciel n'est jamais supprimé, même dans un dossier de
# l'utilisateur. Même âge pour retirer, hors verrou, un marqueur de
# publication périmé (manifeste publié, marqueur resté après un arrêt
# brutal). Défaut 3600 s : une publication en cours n'est jamais touchée.
PUBLISH_TEMP_MAX_AGE_S = float(os.environ.get("MOISSON_PUBLISH_TEMP_MAX_AGE", "3600"))

# Attente maximale (secondes) du verrou de publication d'un dossier
# (.moisson.lock) : renommages, manifestes et index. Au-delà, l'opération
# échoue proprement au lieu de bloquer (erreur d'outil « verrou de publication
# du dossier non obtenu… ; rien n'a été publié », jamais présentée comme une
# échéance dépassée).
PUBLISH_LOCK_TIMEOUT_S = float(os.environ.get("MOISSON_PUBLISH_LOCK_TIMEOUT", "30"))

# corpus_list : nombre de corpus affichés par défaut (argument limit) et
# plafond de cet argument.
CORPUS_LIST_DEFAULT_LIMIT = max(1, int(os.environ.get("MOISSON_CORPUS_LIST_LIMIT", "20")))
CORPUS_LIST_MAX_LIMIT = max(1, int(os.environ.get("MOISSON_CORPUS_LIST_MAX_LIMIT", "1000")))

# Mode audit des réponses natives des fournisseurs (C32) : 1 pour capturer, au
# niveau HTTP, les réponses brutes des API lors d'une moisson (CLI et
# moisson_search) dans OUTPUT_DIR/.native_audit/<run_id>.jsonl, dossier de
# service jamais listé ni téléchargeable. Défaut 0 (désactivé). Distinct de
# raw/, qui garde les notices déjà normalisées par les mappers. Chaque réponse
# est expurgée (clés d'API, jetons, adresse mailto, en-têtes
# d'authentification jamais écrits) et bornée. Limite : l'enrichissement
# OAI-PMH (enrich_oai : Cairn, Érudit, OpenEdition, Persée) passe par Sickle,
# hors du client HTTP de la moisson ; ses réponses ne sont pas capturées
# (signalé dans le manifeste, native_audit.not_captured).
AUDIT_NATIVE = os.environ.get("MOISSON_AUDIT_NATIVE", "0") == "1"

# Bornes du mode audit : corps conservé par réponse (Kio, défaut 256), total
# écrit par exécution (Mio, défaut 20 ; au-delà, une ligne signale l'arrêt de
# la capture) et nombre de fichiers d'exécution conservés (défaut 50, les plus
# anciens supprimés ; 0 : aucune suppression).
AUDIT_NATIVE_MAX_RESPONSE_BYTES = int(os.environ.get("MOISSON_AUDIT_NATIVE_MAX_RESPONSE_KB", "256")) * 1024
AUDIT_NATIVE_MAX_RUN_BYTES = int(os.environ.get("MOISSON_AUDIT_NATIVE_MAX_RUN_MB", "20")) * 1024 * 1024
AUDIT_NATIVE_KEEP_RUNS = max(0, int(os.environ.get("MOISSON_AUDIT_NATIVE_KEEP", "50")))
