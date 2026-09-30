"""
Simulateur d'API installé sous ``requests`` (``HTTPAdapter.send``).

Le simulateur répond à la place du réseau, avec la **sémantique de pagination
propre à chaque fournisseur**, de sorte que le banc reste valable quand les
connecteurs changent les URL qu'ils émettent (politique HTTP, moteur commun,
parcours Isidore mutualisé, tailles de lot, tri HAL) :

- **HAL** (Solr) : ``q``, ``fq`` (filtre de dates ``[A TO B]``, filtre de
  langue ``language_s:(fr OR en)``), ``fl`` (dont ``score``), ``rows``
  (<= 10 000), ``start``, ``sort`` (première clause : ``docid asc``,
  ``score desc``, date ; ``score desc,docid asc`` classe par score),
  ``cursorMark`` (exige un tri contenant ``docid``, comme Solr ;
  ``nextCursorMark`` inchangé en fin de parcours) ;
- **Isidore** : ``q``, ``page`` (à partir de 1), ``replies`` (<= 100 hors fenêtre), notices
  dont les URL portent les domaines des sous-sources (openedition.org,
  cairn.info, persee.fr, erudit.org, theses.fr) en proportions réglables ;
  une page d'un seul résultat est un objet, pas une liste (particularité de
  l'API). **Fenêtre de 200 résultats** (vérifiée en live le 2026-09-28 sur
  « discours numérique » et « Foucault ») : l'API annonce le total réel
  (``@items`` = 266 508) mais ne sert que les 200 premiers résultats ; toute
  page au-delà répète la dernière fenêtre (``replies=100`` : les pages >= 2
  renvoient toutes les résultats 101 à 200). Réglable par
  ``isidore_max_window`` (0 : pas de fenêtre). Avec la fenêtre, ``replies``
  peut aller jusqu'à la fenêtre entière (live : replies=200 → 200 notices,
  replies=250 → 200) ; bloc ``page`` comme en live (``@previous``,
  ``@current``, ``@next`` ; page hors fenêtre ramenée à la dernière page
  servie, sans ``@next`` ; bloc absent si tout tient en une page), vérifié le
  2026-09-28 (notes OPT-08). Volume réaliste des notices
  (~45 Ko chacune, soit ~4,6 Mo par page de 100, mesuré en live) réglable par
  ``isidore_padding`` ;
- **CrossRef** : ``rows`` (<= 1000), ``cursor`` / ``next-cursor``, ``offset``
  (<= 10 000), ``filter`` (``from-pub-date``, ``until-pub-date`` ; ``language``
  refusé en 400 ``filter-not-available`` comme en live), ``select`` validé (un
  champ inconnu renvoie 400 ``validation-failure``), ``sort`` (``score`` par
  défaut sans curseur ; sous ``cursor`` SANS ``sort``, ordre de l'index sans
  rapport avec la pertinence, et tri par date refusé en 400
  ``sort-criteria-incompatible-with-cursor``, vérifiés en live le
  2026-09-29), ``score`` par notice (pertinence du travail, quelle que soit sa
  position) ;
- **OpenAlex** : ``per-page`` (<= 200, sinon 400), ``cursor`` /
  ``meta.next_cursor``, ``page`` (<= 10 000 résultats), ``filter``
  (``publication_year``, ``from_/to_publication_date``, ``language:fr|en``),
  ``select`` validé, ``api_key`` exigée (403 sinon), ``relevance_score``
  (renvoyé seulement s'il est demandé dans ``select``, comme en live) ;
- **Semantic Scholar** : ``/paper/search/bulk`` (1000 par appel, ``token``,
  ``sort`` : ``paperId`` par défaut, ``publicationDate``, ``citationCount``,
  ``year``) et ``/paper/search`` (pertinence, ``offset`` + ``limit`` <= 1000) ;
- **CORE** : ``/v3/search/works`` avec ``limit`` (<= 100), ``offset``
  (<= 10 000), ``scroll``/``scrollId``, filtres ``yearPublished`` dans ``q``,
  jeton Bearer exigé (401 sinon) ;
- **schéma Zotero** : ``api.zotero.org/schema`` servi depuis
  ``tests/fixtures/bench/zotero_schema.json``.

OAI-PMH n'est pas simulé : aucun connecteur ne l'utilise par défaut
(``enrich_oai`` est désactivé) ; une requête OAI reçoit un 501 compté comme
« non simulé ». SerpAPI est refusé (exception comptée) : le banc ne consomme
jamais de crédit facturé. Tout autre hôte lève ``ConnectionError``.

Pannes injectables (règles ``faults``, évaluées dans l'ordre) : statut HTTP
(429 avec ``Retry-After``, 5xx, 4xx définitifs), erreur de connexion, délai de
connexion ou de lecture dépassé, latence supplémentaire, **corps invalide**
(statut 200 et page HTML au lieu de JSON, observé en live sur Isidore : 3 pages
sur 131 le 2026-09-28) ; chaque règle vise un hôte (et un chemin) et
s'applique aux ``first`` premières requêtes, à une requête sur ``every``,
après ``after`` requêtes, ou toujours.
"""

from __future__ import annotations

import base64
import io
import json
import re
import threading
import time
import zlib
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

import requests
from urllib3.response import HTTPResponse

from bench import synth
from bench.instrument import ORIGINAL_SLEEP

_perf = time.perf_counter

HOST_HAL = "api.archives-ouvertes.fr"
HOST_ISIDORE = "api.isidore.science"
HOST_CROSSREF = "api.crossref.org"
HOST_OPENALEX = "api.openalex.org"
HOST_S2 = "api.semanticscholar.org"
HOST_CORE = "api.core.ac.uk"
HOST_ZOTERO = "api.zotero.org"
OAI_HOSTS = {"metadata.openedition.org", "oai.cairn.info", "oai.persee.fr",
             "oai.erudit.org"}
FORBIDDEN_HOSTS = {"serpapi.com"}

DEFAULT_SCHEMA_PATH = (Path(__file__).resolve().parents[2]
                       / "tests" / "fixtures" / "bench" / "zotero_schema.json")

# Champs « select » valides de CrossRef pour /works (liste de l'API ; le
# champ 'language' n'en fait pas partie, cf. connectors/crossref.py).
CROSSREF_SELECT = {
    "abstract", "URL", "member", "posted", "score", "created", "degree",
    "update-policy", "short-title", "license", "ISSN", "container-title",
    "issued", "update-to", "issue", "prefix", "approved", "indexed",
    "article-number", "clinical-trial-number", "accepted", "author",
    "group-title", "DOI", "is-referenced-by-count", "updated-by", "event",
    "chair", "standards-body", "original-title", "funder", "translator",
    "archive", "published-print", "alternative-id", "subject", "subtitle",
    "published-online", "publisher-location", "content-domain", "reference",
    "title", "link", "type", "publisher", "volume", "references-count",
    "ISBN", "issn-type", "assertion", "deposited", "page", "content-created",
    "short-container-title", "relation", "editor",
}

OPENALEX_SELECT = {
    "id", "doi", "title", "display_name", "publication_year",
    "publication_date", "language", "type", "type_crossref", "authorships",
    "primary_location", "locations", "best_oa_location",
    "abstract_inverted_index", "concepts", "topics", "keywords",
    "cited_by_count", "open_access", "ids", "biblio", "relevance_score",
    "is_retracted", "is_paratext", "referenced_works_count", "updated_date",
    "created_date", "fwci", "primary_topic", "sustainable_development_goals",
}

# Taux d'inclusion par défaut de chaque fournisseur, par sous-source
# (« * » : toutes). CrossRef n'inclut que les travaux porteurs d'un DOI.
DEFAULT_PROVIDERS = {
    "hal": {"include": {"hal": 0.9, "openedition": 0.06, "cairn": 0.06,
                        "persee": 0.02, "erudit": 0.02, "theses": 0.25,
                        "autre": 0.02}},
    "isidore": {"include": {"*": 1.0}},
    "crossref": {"include": {"*": 0.9}, "doi_only": True},
    "openalex": {"include": {"*": 0.8}},
    "semantic_scholar": {"include": {"*": 0.55}},
    "core": {"include": {"*": 0.35}},
}

_REASONS = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
            404: "Not Found", 429: "Too Many Requests", 500: "Internal Server Error",
            501: "Not Implemented", 502: "Bad Gateway", 503: "Service Unavailable",
            504: "Gateway Timeout"}


def _enc(tag: str, n: int) -> str:
    return base64.urlsafe_b64encode(f"{tag}:{n}".encode()).decode().rstrip("=")


def _dec(token: str, tag: str) -> Optional[int]:
    try:
        pad = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(token + pad).decode()
        t, n = raw.split(":", 1)
        return int(n) if t == tag else None
    except Exception:
        return None


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _timeouts(timeout) -> Tuple[Optional[float], Optional[float]]:
    """(connexion, lecture) depuis le paramètre ``timeout`` de requests."""
    if timeout is None:
        return None, None
    if isinstance(timeout, (tuple, list)):
        c = timeout[0] if len(timeout) > 0 else None
        r = timeout[1] if len(timeout) > 1 else c
        return c, r
    c = getattr(timeout, "connect_timeout", None)
    r = getattr(timeout, "read_timeout", None)
    if c is not None or r is not None:
        return c, r
    try:
        return float(timeout), float(timeout)
    except (TypeError, ValueError):
        return None, None


class ProviderView:
    """
    Liste ordonnée, paresseuse, des travaux qu'un fournisseur renvoie pour
    une requête, un filtre d'années et un tri.

    Ordre « naturel » (ordre de pertinence de l'univers) : énumération
    paresseuse. Autre ordre (``docid``, score HAL, citations, paperId, date) :
    énumération complète puis tri. Le total annoncé est exact quand
    l'énumération est complète (univers <= 60 000 travaux ou tri imposé),
    sinon estimé par échantillonnage (stable d'une page à l'autre).
    """

    FULL_ENUM_LIMIT = 60000

    def __init__(self, universe: synth.Universe, include: Callable[[int], bool],
                 years: Tuple[Optional[int], Optional[int]], order: Optional[Callable]):
        self.u = universe
        self._include = include
        self.y_lo, self.y_hi = years
        self.order = order
        self.wids: List[int] = []
        self.pos = 0
        self.lock = threading.Lock()
        self.identity = order is None and years == (None, None) and include is None
        if self.identity:
            self.pos = universe.size
            self._total = universe.size
            return
        if order is not None or universe.size <= self.FULL_ENUM_LIMIT:
            self._extend(universe.size)
            if order is not None:
                self.wids.sort(key=order)
            self._total = len(self.wids)
        else:
            step = max(universe.size // 4000, 1)
            sample = range(0, universe.size, step)
            hits = sum(1 for w in sample if self._ok(w))
            self._total = int(round(hits / max(len(sample), 1) * universe.size))

    def _ok(self, wid: int) -> bool:
        if self._include is not None and not self._include(wid):
            return False
        if self.y_lo is not None or self.y_hi is not None:
            y = self.u.year(wid)
            if self.y_lo is not None and y < self.y_lo:
                return False
            if self.y_hi is not None and y > self.y_hi:
                return False
        return True

    def _extend(self, need: int) -> None:
        size = self.u.size
        while len(self.wids) < need and self.pos < size:
            w = self.pos
            self.pos += 1
            if self._ok(w):
                self.wids.append(w)

    def total(self) -> int:
        if self.pos >= self.u.size and not self.identity:
            return len(self.wids)
        return self._total

    def get(self, offset: int, count: int) -> List[int]:
        if count <= 0 or offset < 0:
            return []
        if self.identity:
            return list(range(offset, min(offset + count, self.u.size)))
        with self.lock:
            self._extend(offset + count)
            return self.wids[offset:offset + count]


class ApiSimulator:
    """
    Simulateur d'API (cf. docstring du module).

    Args:
        config: dict de configuration du scénario :
            ``universe`` (cf. ``synth.Universe``), ``providers`` (surcharges
            des taux d'inclusion et ``target_total``), ``latency_ms`` (nombre
            ou dict ``{hôte ou "*": ms}``), ``faults`` (liste de règles),
            ``isidore_max_replies`` (défaut 100), ``isidore_max_window``
            (défaut 200, 0 : aucune), ``isidore_padding`` (octets
            d'enrichissement par notice, défaut 40 000 : ~45 Ko par notice
            comme en live), ``hal_padding`` (défaut 3 500 : ~6 Ko par
            document comme en live), ``schema_path``, ``response_cache_mb``
            (défaut 256).
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = dict(config or {})
        self.universe = synth.Universe(self.config.get("universe") or {})
        self.providers = {}
        for name, spec in DEFAULT_PROVIDERS.items():
            merged = {"include": dict(spec["include"]), "doi_only": spec.get("doi_only", False)}
            over = (self.config.get("providers") or {}).get(name) or {}
            if "include" in over:
                merged["include"] = dict(over["include"])
            for k in ("doi_only", "target_total"):
                if k in over:
                    merged[k] = over[k]
            self.providers[name] = merged
        self._scales = {name: self._scale_for(name) for name in self.providers}
        lat = self.config.get("latency_ms", 0)
        self.latency = lat if isinstance(lat, dict) else {"*": lat}
        self.faults = [dict(r) for r in (self.config.get("faults") or [])]
        self._fault_counts = [0] * len(self.faults)
        self.isidore_max = int(self.config.get("isidore_max_replies", 100))
        self.isidore_window = int(self.config.get("isidore_max_window", 200) or 0)
        self.isidore_padding = int(self.config.get("isidore_padding", 40000))
        self.hal_padding = int(self.config.get("hal_padding", 3500))
        self.schema_path = Path(self.config.get("schema_path") or DEFAULT_SCHEMA_PATH)
        self._schema_bytes: Optional[bytes] = None
        self._lock = threading.Lock()
        self._views: Dict[tuple, ProviderView] = {}
        self._cache: Dict[tuple, bytes] = {}
        self._cache_meta: Dict[tuple, Tuple[int, int, dict]] = {}
        self._cache_bytes = 0
        self._cache_max = int(float(self.config.get("response_cache_mb", 256)) * 1e6)
        # Compteurs
        self.requests: Counter = Counter()
        self.pages: Counter = Counter()
        self.records: Counter = Counter()
        self.faults_injected: Counter = Counter()
        self.unsimulated: Counter = Counter()
        self.forbidden: Counter = Counter()
        self.status: Dict[str, Counter] = defaultdict(Counter)
        self.handler_time_s = 0.0
        self.latency_time_s = 0.0

    # --- inclusion par fournisseur ---

    def _raw_rate(self, provider: str, sub: str) -> float:
        inc = self.providers[provider]["include"]
        return float(inc.get(sub, inc.get("*", 0.0)))

    def _scale_for(self, provider: str) -> float:
        spec = self.providers[provider]
        target = spec.get("target_total")
        if not target:
            return 1.0
        u = self.universe
        expected = 0.0
        for sub, w in u.subsource_weights.items():
            rate = self._raw_rate(provider, sub)
            if spec.get("doi_only"):
                rate *= u.doi_rates.get(sub, 0.4)
            expected += w * rate
        expected *= u.size
        return float(target) / expected if expected > 0 else 1.0

    def _includer(self, provider: str) -> Optional[Callable[[int], bool]]:
        spec = self.providers[provider]
        inc = spec["include"]
        scale = self._scales[provider]
        if (not spec.get("doi_only") and scale >= 1.0 and set(inc) == {"*"}
                and float(inc["*"]) >= 1.0):
            return None
        u = self.universe
        seed = u.seed
        s = synth.salt("inclusion:" + provider)
        rates = {sub: min(self._raw_rate(provider, sub) * scale, 1.0)
                 for sub in synth.SUBSOURCES}
        doi_only = spec.get("doi_only", False)

        def include(wid: int) -> bool:
            if doi_only and not u.has_doi(wid):
                return False
            return synth.unit(seed, s, wid) < rates[u.subsource(wid)]

        return include

    def view(self, provider: str, years=(None, None), order: str = "natural",
             langs: Optional[frozenset] = None) -> ProviderView:
        langs = frozenset(langs) if langs else None
        key = (provider, years, order) if langs is None else (provider, years, order, langs)
        with self._lock:
            v = self._views.get(key)
        if v is not None:
            return v
        u = self.universe
        orders = {
            "natural": None,
            "hal_docid": synth.hal_docid,
            "hal_score": lambda w: (-self._hal_score(w), synth.hal_docid(w)),
            "citations_desc": lambda w: (-self._citations(w), w),
            "citations_asc": lambda w: (self._citations(w), w),
            "paper_id": synth.s2_paper_id,
            "date_desc": lambda w: (-u.year(w), w),
            "date_asc": lambda w: (u.year(w), w),
            # Ordre de l'index CrossRef (curseur sans tri) : déterministe,
            # sans rapport avec la pertinence.
            "crossref_index": lambda w: (synth.unit(u.seed, synth.salt("crossref-index"), w), w),
        }
        include = self._includer(provider)
        if langs is not None:
            base = include

            def include(wid: int, _base=base, _langs=langs) -> bool:
                if _base is not None and not _base(wid):
                    return False
                return u.lang(wid) in _langs
        v = ProviderView(u, include, years, orders[order])
        with self._lock:
            self._views.setdefault(key, v)
            return self._views[key]

    def _hal_score(self, wid: int) -> float:
        u = self.universe
        base = 30.0 * (1.0 - wid / max(u.size, 1)) ** 2
        return round(base + 6.0 * synth.unit(u.seed, synth.salt("score-hal"), wid), 4)

    def _citations(self, wid: int) -> int:
        return self.universe.work(wid)["citations"]

    # --- point d'entrée requests ---

    def send(self, adapter, request, stream=False, timeout=None, verify=True,
             cert=None, proxies=None):
        """Remplaçant de ``HTTPAdapter.send`` : réponse simulée ou exception."""
        parts = urlsplit(request.url)
        host = (parts.hostname or "").lower()
        path = parts.path or "/"
        params = parse_qs(parts.query, keep_blank_values=True)
        with self._lock:
            self.requests[host] += 1
        if host in FORBIDDEN_HOSTS or host.endswith(".serpapi.com"):
            with self._lock:
                self.forbidden[host] += 1
            raise requests.ConnectionError(
                f"hôte interdit par le banc (source facturée) : {host}")

        latency = float(self.latency.get(host, self.latency.get("*", 0)) or 0) / 1000.0
        fault = self._match_fault(host, path)
        if fault is not None:
            kind = fault.get("kind", "status")
            with self._lock:
                self.faults_injected[f"{host}:{kind}"] += 1
            if kind == "latency":
                latency += float(fault.get("latency_ms", 0)) / 1000.0
                fault = None
            elif kind in ("connect_error", "connect_timeout", "read_timeout"):
                c, r = _timeouts(timeout)
                wait = 0.0
                if kind == "connect_timeout":
                    wait = float(fault.get("wait_s", c if c is not None else 1.0))
                elif kind == "read_timeout":
                    wait = latency + float(fault.get("wait_s", r if r is not None else 1.0))
                self._sleep(wait)
                exc = {"connect_error": requests.exceptions.ConnectionError,
                       "connect_timeout": requests.exceptions.ConnectTimeout,
                       "read_timeout": requests.exceptions.ReadTimeout}[kind]
                raise exc(f"panne simulée ({kind}) sur {host}", request=request)

        self._sleep(latency)
        t0 = _perf()
        try:
            if fault is not None and fault.get("kind") == "invalid_body":
                # Statut 200 mais page HTML (ou corps tronqué) : le décodage
                # JSON du connecteur échoue.
                status = 200
                headers = {"Content-Type": "text/html; charset=utf-8"}
                body = ("<html><head><title>Erreur</title></head><body>Service "
                        "temporairement indisponible</body></html>").encode("utf-8")
                n_records = None
            elif fault is not None:
                status = int(fault.get("status", 503))
                headers = {"Content-Type": "application/json"}
                if fault.get("retry_after") is not None:
                    headers["Retry-After"] = str(fault["retry_after"])
                for k, v in (fault.get("headers") or {}).items():
                    headers[k] = str(v)
                body = json.dumps({"error": f"panne simulée {status}"}).encode()
                n_records = 0
            else:
                status, headers, body, n_records = self._dispatch(host, path, params, request)
            with self._lock:
                self.status[host][status] += 1
                if status == 200 and n_records is not None:
                    self.pages[host] += 1
                    self.records[host] += n_records
            return self._build(adapter, request, status, headers, body, n_records)
        finally:
            dt = _perf() - t0
            with self._lock:
                self.handler_time_s += dt

    def _sleep(self, seconds: float) -> None:
        if seconds and seconds > 0:
            ORIGINAL_SLEEP(seconds)
            with self._lock:
                self.latency_time_s += seconds

    def _match_fault(self, host: str, path: str) -> Optional[Dict[str, Any]]:
        for i, rule in enumerate(self.faults):
            rh = rule.get("host", "*")
            if rh != "*" and host != rh and not host.endswith("." + rh):
                continue
            if rule.get("path") and rule["path"] not in path:
                continue
            with self._lock:
                self._fault_counts[i] += 1
                n = self._fault_counts[i]
            if n <= int(rule.get("after", 0)):
                continue
            k = n - int(rule.get("after", 0))
            if rule.get("first") is not None and k > int(rule["first"]):
                continue
            if rule.get("every") is not None and k % int(rule["every"]) != 0:
                continue
            return rule
        return None

    @staticmethod
    def _build(adapter, request, status, headers, body: bytes, n_records):
        hdrs = {"Content-Length": str(len(body))}
        hdrs.update(headers or {})
        raw = HTTPResponse(
            body=io.BytesIO(body), headers=hdrs, status=status,
            reason=_REASONS.get(status, ""), preload_content=False,
            decode_content=False, request_method=request.method,
            request_url=request.url,
        )
        resp = adapter.build_response(request, raw)
        resp._bench_records = n_records
        return resp

    # --- aiguillage ---

    def _dispatch(self, host, path, params, request):
        if host == HOST_ZOTERO:
            return self._schema(path)
        if host in OAI_HOSTS:
            with self._lock:
                self.unsimulated[host] += 1
            return 501, {"Content-Type": "text/plain"}, "OAI-PMH non simulé".encode(), None
        handler = {
            HOST_HAL: self._hal,
            HOST_ISIDORE: self._isidore,
            HOST_CROSSREF: self._crossref,
            HOST_OPENALEX: self._openalex,
            HOST_S2: self._s2,
            HOST_CORE: self._core,
        }.get(host)
        if handler is None:
            with self._lock:
                self.unsimulated[host] += 1
            raise requests.ConnectionError(f"hôte non simulé par le banc : {host}")
        return handler(path, params, request)

    def _cached_body(self, key: tuple, build: Callable[[], Tuple[int, dict, bytes, Any]]):
        """
        Corps de réponse mémoïsé par clé SÉMANTIQUE (fenêtre servie), et non par
        URL : les proxies Isidore redemandent des centaines de pages qui, au-delà
        de la fenêtre de 200 résultats, portent le même contenu ; le coût de
        génération (compté à part) ne doit ni dominer ni gonfler la mémoire.
        """
        with self._lock:
            hit = self._cache.get(key)
            meta = self._cache_meta.get(key)
        if hit is not None:
            status, n, headers = meta
            return status, headers, zlib.decompress(hit), n
        status, headers, body, n = build()
        if status == 200 and self._cache_bytes < self._cache_max:
            comp = zlib.compress(body, 1)
            with self._lock:
                if key not in self._cache:
                    self._cache[key] = comp
                    self._cache_meta[key] = (status, n, headers)
                    self._cache_bytes += len(comp)
        return status, headers, body, n

    @staticmethod
    def _json(status: int, payload: Any, extra_headers: Dict[str, str] = None, n=None):
        headers = {"Content-Type": "application/json; charset=utf-8"}
        headers.update(extra_headers or {})
        return status, headers, json.dumps(payload, ensure_ascii=False).encode("utf-8"), n

    def _schema(self, path):
        if path.rstrip("/") != "/schema":
            return self._json(404, {"error": "not found"})
        if self._schema_bytes is None:
            self._schema_bytes = self.schema_path.read_bytes()
        return 200, {"Content-Type": "application/json"}, self._schema_bytes, None

    # --- HAL (Solr) ---

    _RANGE_RE = re.compile(r"\[\s*([^\s\]]+)\s+TO\s+([^\s\]]+)\s*\]")
    _HAL_LANG_RE = re.compile(r"language_s\s*:\s*\(?([^)]*)\)?")

    @classmethod
    def _years_from(cls, text: str) -> Tuple[Optional[int], Optional[int]]:
        m = cls._RANGE_RE.search(text or "")
        if not m:
            return None, None

        def y(v):
            mm = re.match(r"(\d{4})", v)
            return int(mm.group(1)) if mm else None
        return y(m.group(1)), y(m.group(2))

    def _hal(self, path, params, request):
        if not path.startswith("/search"):
            return self._json(404, {"error": "not found"})
        get = lambda k, d=None: (params.get(k) or [d])[0]
        years = (None, None)
        langs = None
        for fq in params.get("fq", []):
            m = self._HAL_LANG_RE.search(fq or "")
            if m:
                langs = frozenset(c.strip().lower() for c in m.group(1).split(" OR ")
                                  if c.strip())
            elif "date" in fq.lower() or "Y_i" in fq:
                years = self._years_from(fq)
        rows = _int(get("rows", "30"), 30)
        if rows < 0 or rows > 10000:
            return self._json(400, {"error": {"msg": "rows must be <= 10000", "code": 400}})
        sort = (get("sort") or "score desc").lower()
        cursor = get("cursorMark")
        # Première clause du tri (``score desc,docid asc`` : classement par
        # score, départagé par docid).
        primary = sort.split(",")[0].strip()
        if primary.startswith("docid"):
            order = "hal_docid"
        elif "date" in primary:
            order = "date_desc" if "desc" in primary else "date_asc"
        else:
            order = "hal_score"
        if cursor is not None and "docid" not in sort:
            return self._json(400, {"error": {
                "msg": "Cursor functionality requires a sort containing a uniqueKey "
                       "field tie breaker", "code": 400}})
        view = self.view("hal", years, order, langs)
        if cursor is not None:
            start = 0 if cursor == "*" else _dec(cursor.replace("AoE", "", 1), "hal")
            if start is None:
                return self._json(400, {"error": {"msg": "Unable to parse 'cursorMark'", "code": 400}})
        else:
            start = _int(get("start", "0"), 0)
        wids = view.get(start, rows)
        fl = [f.strip() for f in (get("fl") or "*").split(",") if f.strip()]
        want_score = "score" in fl or "*" in fl
        docs = []
        for w in wids:
            doc = synth.render_hal(self.universe, w,
                                   score=self._hal_score(w) if want_score else None,
                                   padding=self.hal_padding)
            if "*" not in fl:
                doc = {k: v for k, v in doc.items() if k in fl}
            docs.append(doc)
        payload = {
            "responseHeader": {"status": 0, "QTime": 3,
                               "params": {k: v[0] for k, v in params.items() if k != "fl"}},
            "response": {"numFound": view.total(), "start": start,
                         "numFoundExact": True, "docs": docs},
        }
        if want_score and wids:
            payload["response"]["maxScore"] = max(self._hal_score(w) for w in wids)
        if cursor is not None:
            payload["nextCursorMark"] = (cursor if not wids
                                         else "AoE" + _enc("hal", start + len(wids)))
        return self._json(200, payload, n=len(docs))

    # --- Isidore ---

    def _isidore(self, path, params, request):
        if "/resource/search" not in path:
            return self._json(404, {"error": "not found"})
        get = lambda k, d=None: (params.get(k) or [d])[0]
        # Plafond de `replies` : sans fenêtre, isidore_max_replies (100) ; avec
        # la fenêtre, l'API sert jusqu'à la fenêtre entière en une requête
        # (live 2026-09-28 : replies=200 → 200 notices, replies=250 → 200).
        cap = (max(self.isidore_max, self.isidore_window) if self.isidore_window
               else self.isidore_max)
        replies = max(1, min(_int(get("replies", "10"), 10), cap))
        page = max(1, _int(get("page", "1"), 1))
        view = self.view("isidore")
        total = view.total()
        offset = (page - 1) * replies
        if self.isidore_window:
            # Au-delà de la fenêtre, l'API répète la dernière fenêtre servie.
            offset = min(offset, max(self.isidore_window - replies, 0))
            count = min(replies, self.isidore_window - offset)
            served = min(self.isidore_window, total)
        else:
            count = replies
            served = total
        # Bloc `page` (live) : @previous / @current / @next ; une page hors
        # fenêtre est ramenée à la dernière page servie (@current = 10 pour
        # replies=20, pages 11 et 12), sans @next en fin de fenêtre ; bloc
        # absent quand tout tient en une page (replies=200).
        last_page = max(1, -(-served // replies))
        current = min(page, last_page)
        q = get("q", "")

        def build():
            wids = view.get(offset, count)
            records = [synth.render_isidore(self.universe, w, self.isidore_padding)
                       for w in wids]
            reply: Any = records[0] if len(records) == 1 else records
            replies_block: Dict[str, Any] = {
                "meta": {"@items": str(total), "@pageItems": str(len(records))},
                "content": {"reply": reply} if records else {},
            }
            if records and (current > 1 or current < last_page):
                block = {}
                if current > 1:
                    block["@previous"] = str(current - 1)
                block["@current"] = str(current)
                if current < last_page:
                    block["@next"] = str(current + 1)
                replies_block["page"] = block
            payload = {"response": {
                "header": {"query": {"@text": q, "param": [
                    {"@name": "q", "@value": q},
                    {"@name": "replies", "@value": str(replies)}]},
                    "performance": {"@durationMs": "120"}},
                "replies": replies_block,
            }}
            return self._json(200, payload, n=len(records))

        return self._cached_body(("isidore", q, offset, count, current, last_page), build)

    # --- CrossRef ---

    def _crossref(self, path, params, request):
        if path.rstrip("/") != "/works":
            return self._json(404, {"status": "error", "message": "not found"})
        get = lambda k, d=None: (params.get(k) or [d])[0]
        select = get("select")
        if select:
            bad = [f for f in select.split(",") if f and f not in CROSSREF_SELECT]
            if bad:
                return self._json(400, {"status": "failed", "message-type": "validation-failure",
                                        "message": [{"type": "field-not-allowed",
                                                     "value": bad[0],
                                                     "message": f"Field {bad[0]} is not allowed in select"}]})
        rows = _int(get("rows", "20"), 20)
        if rows < 0 or rows > 1000:
            return self._json(400, {"status": "failed", "message-type": "validation-failure",
                                    "message": [{"type": "integer-not-valid",
                                                 "message": "rows must be <= 1000"}]})
        years = [None, None]
        for part in (get("filter") or "").split(","):
            if ":" not in part:
                continue
            k, v = part.split(":", 1)
            if k in ("from-pub-date", "from-published-date", "from-issued-date"):
                years[0] = _int(v[:4], None)
            elif k in ("until-pub-date", "until-published-date", "until-issued-date"):
                years[1] = _int(v[:4], None)
            elif k == "language":
                # Live 2026-09-29 : aucun filtre de langue sur /works.
                return self._json(400, {"status": "failed", "message-type": "validation-failure",
                                        "message": [{"type": "filter-not-available",
                                                     "value": "language",
                                                     "message": "Filter 'language' specified but "
                                                                "there is no such filter for this "
                                                                "route."}]})
        cursor = get("cursor")
        sort_param = get("sort")
        sort = (sort_param or "score").lower()
        direction = (get("order") or "desc").lower()
        order = "natural"
        date_sorts = ("published", "issued", "published-print", "published-online")
        if cursor is not None and sort in date_sorts:
            return self._json(400, {"status": "failed", "message-type": "validation-failure",
                                    "message": [{"type": "sort-criteria-incompatible-with-cursor",
                                                 "value": "sort",
                                                 "message": "Sorting by [issued, published, "
                                                            "published-print, published-online] "
                                                            "is not supported when using a cursor"}]})
        if cursor is not None and sort_param is None:
            # Live 2026-09-29 : sous un curseur sans tri, ordre de l'index.
            order = "crossref_index"
        elif sort in date_sorts:
            order = "date_desc" if direction == "desc" else "date_asc"
        elif sort == "is-referenced-by-count":
            order = "citations_desc" if direction == "desc" else "citations_asc"
        view = self.view("crossref", tuple(years), order)
        if cursor is not None:
            start = 0 if cursor == "*" else _dec(cursor, "cr")
            if start is None:
                return self._json(400, {"status": "failed", "message": "invalid cursor"})
        else:
            start = _int(get("offset", "0"), 0)
            if start + rows > 10000:
                return self._json(400, {"status": "failed", "message-type": "validation-failure",
                                        "message": [{"type": "offset-too-large",
                                                     "message": "offset + rows must be <= 10000"}]})
        total = view.total()
        wids = view.get(start, rows)
        fields = set(select.split(",")) if select else None
        items = []
        for i, w in enumerate(wids):
            it = synth.render_crossref(self.universe, w, start + i, max(total, 1))
            if fields is not None:
                it = {k: v for k, v in it.items() if k in fields}
            items.append(it)
        message = {"facets": {}, "total-results": total, "items": items,
                   "items-per-page": rows,
                   "query": {"start-index": start, "search-terms": get("query.bibliographic") or get("query")}}
        if cursor is not None:
            message["next-cursor"] = _enc("cr", start + len(wids))
        return self._json(200, {"status": "ok", "message-type": "work-list",
                                "message-version": "1.0.0", "message": message},
                          n=len(items))

    # --- OpenAlex ---

    def _openalex(self, path, params, request):
        if path.rstrip("/") != "/works":
            return self._json(404, {"error": "not found"})
        get = lambda k, d=None: (params.get(k) or [d])[0]
        if not get("api_key") and "api_key" not in request.headers.get("Authorization", ""):
            return self._json(403, {"error": "API key required",
                                    "message": "OpenAlex requires an api_key parameter"})
        per_page = _int(get("per-page", get("per_page", "25")), 25)
        if per_page < 1 or per_page > 200:
            return self._json(400, {"error": "Invalid query parameters error.",
                                    "message": "per-page parameter must be between 1 and 200"})
        select = get("select")
        if select:
            bad = [f for f in select.split(",") if f and f not in OPENALEX_SELECT]
            if bad:
                return self._json(400, {"error": "Invalid query parameters error.",
                                        "message": f"{bad[0]} is not a valid select field."})
        years = [None, None]
        langs = None
        for part in (get("filter") or "").split(","):
            if ":" not in part:
                continue
            k, v = part.split(":", 1)
            if k == "language":
                langs = frozenset(c.strip().lower() for c in v.split("|") if c.strip())
            elif k == "publication_year":
                if "-" in v:
                    lo, hi = v.split("-", 1)
                    years = [_int(lo, None), _int(hi, None)]
                elif v.startswith(">"):
                    years[0] = _int(v[1:], 0) + 1
                elif v.startswith("<"):
                    years[1] = _int(v[1:], 9999) - 1
                else:
                    years = [_int(v, None), _int(v, None)]
            elif k == "from_publication_date":
                years[0] = _int(v[:4], None)
            elif k == "to_publication_date":
                years[1] = _int(v[:4], None)
        sort = (get("sort") or "relevance_score:desc").lower()
        order = "natural"
        if sort.startswith("publication_date") or sort.startswith("publication_year"):
            order = "date_asc" if sort.endswith(":asc") else "date_desc"
        elif sort.startswith("cited_by_count"):
            order = "citations_asc" if sort.endswith(":asc") else "citations_desc"
        view = self.view("openalex", tuple(years), order, langs)
        total = view.total()
        cursor = get("cursor")
        page_param = get("page")
        if cursor is not None:
            start = 0 if cursor == "*" else _dec(cursor, "oa")
            if start is None:
                return self._json(400, {"error": "Invalid cursor"})
        else:
            page = max(1, _int(page_param or "1", 1))
            start = (page - 1) * per_page
            if start + per_page > 10000:
                return self._json(400, {"error": "Invalid query parameters error.",
                                        "message": "Maximum results size of 10,000 records is exceeded. "
                                                   "Use cursor paging."})
        wids = view.get(start, per_page)
        fields = set(select.split(",")) if select else None
        results = []
        for i, w in enumerate(wids):
            r = synth.render_openalex(self.universe, w, start + i, max(total, 1))
            if fields is not None:
                r = {k: v for k, v in r.items() if k in fields}
            results.append(r)
        nxt = None
        if cursor is not None and wids and start + len(wids) < total:
            nxt = _enc("oa", start + len(wids))
        meta = {"count": total, "db_response_time_ms": 12,
                "page": None if cursor is not None else max(1, _int(page_param or "1", 1)),
                "per_page": per_page, "next_cursor": nxt, "groups_count": None}
        return self._json(200, {"meta": meta, "results": results, "group_by": []},
                          {"X-RateLimit-Prepaid-Remaining-USD": "0.9"}, n=len(results))

    # --- Semantic Scholar ---

    @staticmethod
    def _s2_years(value: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
        if not value:
            return None, None
        if "-" in value:
            lo, hi = value.split("-", 1)
            return _int(lo, None) if lo else None, _int(hi, None) if hi else None
        y = _int(value, None)
        return y, y

    def _s2(self, path, params, request):
        get = lambda k, d=None: (params.get(k) or [d])[0]
        years = self._s2_years(get("year"))
        fields = set((get("fields") or "title").split(",")) | {"paperId"}
        if path.rstrip("/").endswith("/paper/search/bulk"):
            sort = (get("sort") or "paperId").lower()
            if sort.startswith("citationcount"):
                order = "citations_asc" if sort.endswith(":asc") else "citations_desc"
            elif sort.startswith("publicationdate"):
                order = "date_asc" if sort.endswith(":asc") else "date_desc"
            else:
                order = "paper_id"
            view = self.view("semantic_scholar", years, order)
            token = get("token")
            start = 0 if not token else _dec(token, "s2")
            if start is None:
                return self._json(400, {"error": "invalid token"})
            wids = view.get(start, 1000)
            data = [{k: v for k, v in synth.render_s2(self.universe, w).items() if k in fields}
                    for w in wids]
            nxt = _enc("s2", start + len(wids)) if start + len(wids) < view.total() else None
            return self._json(200, {"total": view.total(), "token": nxt, "data": data},
                              n=len(data))
        if path.rstrip("/").endswith("/paper/search"):
            offset = _int(get("offset", "0"), 0)
            limit = _int(get("limit", "10"), 10)
            if limit > 100 or offset + limit > 1000:
                return self._json(400, {"error": "Requested data for this limit and/or "
                                                 "offset is not available"})
            view = self.view("semantic_scholar", years, "natural")
            wids = view.get(offset, limit)
            data = [{k: v for k, v in synth.render_s2(self.universe, w).items() if k in fields}
                    for w in wids]
            payload = {"total": view.total(), "offset": offset, "data": data}
            if wids and offset + len(wids) < min(view.total(), 1000):
                payload["next"] = offset + len(wids)
            return self._json(200, payload, n=len(data))
        return self._json(404, {"error": "not found"})

    # --- CORE ---

    _CORE_YEAR_RE = re.compile(r"yearPublished\s*(>=|<=|>|<|:)\s*(\d{4})")

    def _core(self, path, params, request):
        if not path.rstrip("/").endswith("/search/works"):
            return self._json(404, {"message": "not found"})
        if not (request.headers.get("Authorization") or "").startswith("Bearer "):
            return self._json(401, {"message": "Invalid or missing API key"})
        get = lambda k, d=None: (params.get(k) or [d])[0]
        q = get("q", "") or ""
        lo = hi = None
        for op, y in self._CORE_YEAR_RE.findall(q):
            y = int(y)
            if op in (">=",):
                lo = y
            elif op == ">":
                lo = y + 1
            elif op == "<=":
                hi = y
            elif op == "<":
                hi = y - 1
            else:
                lo = hi = y
        limit = max(1, min(_int(get("limit", "10"), 10), 100))
        view = self.view("core", (lo, hi), "natural")
        scroll_id = get("scrollId")
        if scroll_id or (get("scroll") or "").lower() == "true":
            start = 0 if not scroll_id else _dec(scroll_id, "core")
            if start is None:
                return self._json(400, {"message": "invalid scrollId"})
        else:
            start = _int(get("offset", "0"), 0)
            if start + limit > 10000:
                return self._json(400, {"message": "offset + limit must be <= 10000; use scroll"})
        wids = view.get(start, limit)
        results = [synth.render_core(self.universe, w) for w in wids]
        payload = {"totalHits": view.total(), "limit": limit, "offset": start,
                   "results": results, "tooks": [], "esTook": 5}
        if scroll_id or (get("scroll") or "").lower() == "true":
            payload["scrollId"] = _enc("core", start + len(wids))
        return self._json(200, payload, n=len(results))

    # --- restitution ---

    def reset_counters(self) -> None:
        """Remet les compteurs à zéro (le cache des corps générés est conservé)."""
        with self._lock:
            for c in (self.requests, self.pages, self.records, self.faults_injected,
                      self.unsimulated, self.forbidden):
                c.clear()
            self.status.clear()
            self.handler_time_s = 0.0
            self.latency_time_s = 0.0

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "requests": dict(self.requests),
                "pages": dict(self.pages),
                "records_by_host": dict(self.records),
                "records_served": sum(self.records.values()),
                "status": {h: {str(k): v for k, v in c.items()} for h, c in self.status.items()},
                "faults_injected": dict(self.faults_injected),
                "unsimulated": dict(self.unsimulated),
                "forbidden": dict(self.forbidden),
                "handler_time_s": round(self.handler_time_s, 4),
                "latency_time_s": round(self.latency_time_s, 3),
                "response_cache_bytes": self._cache_bytes,
                "universe_size": self.universe.size,
                "provider_totals": {
                    f"{k[0]}{'' if k[1] == (None, None) else k[1]}:{k[2]}": v.total()
                    for k, v in self._views.items()
                },
            }
