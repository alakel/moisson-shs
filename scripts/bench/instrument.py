"""
Instrumentation du banc : réseau, sommeils, étapes, sockets.

Point de passage réseau
-----------------------
Tout appel ``requests`` (session partagée de ``HTTPClient``, ``requests.get``
brut des résolveurs et du chargeur de schéma, client OAI ``sickle``) aboutit à
``requests.adapters.HTTPAdapter.send``. Le cache ``requests-cache`` se place
AU-DESSUS (il surcharge ``Session.send``) : un appel qui atteint l'adaptateur
est donc un véritable accès réseau, une réponse servie par le cache n'y
parvient jamais. Les compteurs posés à cet endroit restent valables quelle que
soit la refonte de la politique HTTP (OPT-03) ou du moteur (OPT-07).

Le simulateur d'API (``bench.simulator``) s'installe au même point, SOUS les
compteurs : en mode simulé, ``send`` interroge le simulateur au lieu d'ouvrir
une connexion ; en mode live, il appelle l'adaptateur d'origine.

Définitions retenues
--------------------
- **appel logique** : l'appel instrumenté le plus interne en cours dans le fil
  (``run_source``, ``search`` d'un connecteur, étape chronométrée comme
  ``validate_and_clean`` ou ``load_zotero_schema``), cf.
  ``StageRecorder.current_call`` ;
- **reprise** (``retries``) : requête dont l'URL exacte est celle de la requête
  précédente du même fil ET du même appel logique, lorsque celle-ci a échoué
  (statut >= 400 ou exception). C'est la signature d'un retry de
  ``HTTPClient`` ou de ``http_scholarly``, qui réessaient dans le même fil,
  au sein d'un même appel ;
- **appel répété après échec** (``repeats_after_failure``) : même URL, même
  fil, précédente en échec, mais dans un AUTRE appel logique. Ce n'est pas un
  retry : premières tentatives des proxies Isidore successifs (même URL de
  page 1), validations Zotero qui retéléchargent chacune le schéma (C07) ;
- **téléchargement redondant** (``duplicate_fetches``) : requête dont l'URL
  exacte a déjà été servie avec succès plus tôt dans la mesure, quel que soit
  le fil. C'est la signature des paginations Isidore répétées par les proxies
  (constat C09), distincte d'un retry ;
- **page** : réponse 200 d'un point d'accès de recherche ;
- **erreur de lecture du corps** (``body_errors``) : réponse dont les en-têtes
  sont arrivés mais dont le corps casse pendant le transfert (connexion coupée
  en ``Transfer-Encoding: chunked``, longueur incomplète). Comptée comme une
  exception (et non comme un 200), puis RELEVÉE depuis ``send`` : ``requests``
  et ``HTTPClient`` la voient exactement comme sans banc (sinon le flux, fermé
  par urllib3, serait relu vide et le connecteur recevrait un 200 à corps vide
  sans retry).
"""

from __future__ import annotations

import itertools
import json
import socket
import sys
import threading
import time
from collections import Counter, defaultdict
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit

# Référence au sommeil d'origine, capturée AVANT toute instrumentation : le
# simulateur l'utilise pour ses latences (qui ne doivent être ni comptées
# comme politesse du code mesuré, ni mises à l'échelle par --sleep-scale).
ORIGINAL_SLEEP = time.sleep

# Référence à l'horloge monotone haute résolution.
_perf = time.perf_counter


class BenchError(RuntimeError):
    """Erreur de configuration du banc (signature, import, copie du code)."""


# === Compteurs réseau ===

def count_records(host: str, data: Any) -> Optional[int]:
    """
    Compte les notices d'une réponse JSON de recherche, selon le fournisseur.

    Utilisé en mode live (en mode simulé, le simulateur annote directement la
    réponse). Renvoie None pour une réponse d'une forme inconnue.
    """
    if not isinstance(data, dict):
        return None
    try:
        if "archives-ouvertes" in host:
            return len((data.get("response") or {}).get("docs") or [])
        if "isidore" in host:
            content = ((data.get("response") or {}).get("replies") or {}).get("content")
            reply = content.get("reply") if isinstance(content, dict) else content
            if isinstance(reply, dict):
                return 1
            return len(reply or [])
        if "crossref" in host:
            msg = data.get("message")
            items = msg.get("items") if isinstance(msg, dict) else None
            return len(items) if isinstance(items, list) else None
        if "openalex" in host or "core.ac.uk" in host:
            res = data.get("results")
            return len(res) if isinstance(res, list) else None
        if "semanticscholar" in host:
            res = data.get("data")
            return len(res) if isinstance(res, list) else None
    except Exception:
        return None
    return None


class _HostStats:
    """Compteurs d'un hôte."""

    __slots__ = (
        "requests", "ok", "status", "exceptions", "bytes_in", "net_time_s",
        "retries", "repeats_after_failure", "duplicate_fetches", "records",
        "paths", "max_in_flight", "in_flight", "non_json_200",
        "non_json_samples", "body_errors",
    )

    def __init__(self):
        self.requests = 0
        self.ok = 0
        self.status: Counter = Counter()
        self.exceptions: Counter = Counter()
        self.bytes_in = 0
        self.net_time_s = 0.0
        self.retries = 0
        self.repeats_after_failure = 0
        self.body_errors = 0
        self.duplicate_fetches = 0
        self.records = 0
        self.paths: Counter = Counter()
        self.max_in_flight = 0
        self.in_flight = 0
        self.non_json_200 = 0
        self.non_json_samples: List[str] = []

    def as_dict(self) -> Dict[str, Any]:
        return {
            "requests": self.requests,
            "ok": self.ok,
            "status": {str(k): v for k, v in sorted(self.status.items())},
            "exceptions": dict(self.exceptions),
            "bytes_in": self.bytes_in,
            "net_time_s": round(self.net_time_s, 4),
            "retries": self.retries,
            "repeats_after_failure": self.repeats_after_failure,
            "duplicate_fetches": self.duplicate_fetches,
            "records": self.records,
            "paths": dict(self.paths.most_common(10)),
            "max_in_flight": self.max_in_flight,
            "non_json_200": self.non_json_200,
            "non_json_samples": list(self.non_json_samples),
            "body_errors": self.body_errors,
        }


class NetworkRecorder:
    """
    Compteurs de toutes les requêtes qui atteignent ``HTTPAdapter.send``.

    Thread-safe : les moissons parallèles (``workers > 1``) appellent ``send``
    depuis plusieurs fils.
    """

    def __init__(self, parse_records: bool = True, keep_trace: bool = False,
                 max_trace: int = 50000):
        self._lock = threading.Lock()
        self._hosts: Dict[str, _HostStats] = defaultdict(_HostStats)
        self._url_ok: Counter = Counter()
        self._last_by_thread: Dict[int, tuple] = {}
        self._by_source: Dict[str, Counter] = defaultdict(Counter)
        self._in_flight_total = 0
        self.max_in_flight_total = 0
        self.parse_records = parse_records
        self.overhead_s = 0.0
        self.keep_trace = keep_trace
        self.max_trace = max_trace
        self.trace: List[Dict[str, Any]] = []
        self.t0 = _perf()
        # Fonction qui renvoie la source courante du fil (posée par le
        # chronométrage de run_source) ; None si inconnue.
        self.current_source: Callable[[], Optional[str]] = lambda: None
        # Fonction qui renvoie l'identifiant de l'appel logique courant du fil
        # (StageRecorder.current_call) ; None hors de tout appel instrumenté.
        self.current_call: Callable[[], Optional[int]] = lambda: None

    def reset(self) -> None:
        """Remet les compteurs à zéro (après une moisson d'amorçage)."""
        with self._lock:
            self._hosts.clear()
            self._url_ok.clear()
            self._last_by_thread.clear()
            self._by_source.clear()
            self.max_in_flight_total = 0
            self.overhead_s = 0.0
            self.trace.clear()
            self.t0 = _perf()

    # --- cycle d'une requête ---

    def begin(self, url: str) -> Dict[str, Any]:
        host = (urlsplit(url).hostname or "").lower()
        ctx = {"url": url, "host": host, "t": _perf(),
               "tid": threading.get_ident(),
               "source": self.current_source(),
               "call": self.current_call()}
        with self._lock:
            hs = self._hosts[host]
            hs.requests += 1
            hs.paths[urlsplit(url).path] += 1
            hs.in_flight += 1
            hs.max_in_flight = max(hs.max_in_flight, hs.in_flight)
            self._in_flight_total += 1
            self.max_in_flight_total = max(
                self.max_in_flight_total, self._in_flight_total
            )
            # last = (url, échec, appel logique) de la requête précédente du fil.
            last = self._last_by_thread.get(ctx["tid"])
            if last and last[0] == url and last[1]:
                if last[2] == ctx["call"]:
                    hs.retries += 1
                else:
                    hs.repeats_after_failure += 1
            elif self._url_ok.get(url):
                hs.duplicate_fetches += 1
            src = ctx["source"] or "(hors source)"
            self._by_source[src][host] += 1
        return ctx

    def end(self, ctx: Dict[str, Any], response=None, exc: BaseException = None,
            stream: bool = False) -> Optional[BaseException]:
        """
        Clôt le décompte d'une requête.

        Returns:
            L'exception levée par la lecture du corps, s'il y en a eu une : la
            requête est alors comptée comme une exception (pas comme un 200),
            et l'appelant (``send``) DOIT la relever pour que ``requests`` et
            le code mesuré la voient comme sans banc. None sinon.
        """
        host = ctx["host"]
        status = None
        nbytes = 0
        records = None
        bad_body = None
        content = None
        body_exc = None
        # Une redirection n'est pas prélue : requests lit (et, en cas
        # d'erreur, ignore) son corps lui-même dans resolve_redirects.
        if (response is not None and not stream
                and not getattr(response, "is_redirect", False)):
            # Lecture du corps : c'est le téléchargement réel (requests l'aurait
            # fait juste après, dans Session.send) ; il compte dans le temps
            # réseau, PAS dans le surcoût du banc. Une erreur de transfert
            # n'est JAMAIS avalée (cf. docstring du module).
            try:
                content = response.content or b""
            except Exception as e:
                content = None
                body_exc = e
                exc = e
        dt = _perf() - ctx["t"]
        if response is not None and body_exc is None:
            status = response.status_code
            records = getattr(response, "_bench_records", None)
            if content is not None:
                t_over = _perf()
                try:
                    nbytes = len(content)
                    head = content[:64].lstrip()[:1]
                    ctype = response.headers.get("Content-Type", "")
                    # Réponse 200 annoncée ou attendue en JSON mais dont le
                    # corps n'en est pas (page d'erreur HTML, corps tronqué) :
                    # contrôle bon marché, sans décodage.
                    if (status == 200 and head not in (b"{", b"[")
                            and ("json" in ctype or host.startswith("api."))):
                        bad_body = content[:160].decode("utf-8", "replace")
                    if (records is None and self.parse_records
                            and status == 200 and head in (b"{", b"[")):
                        records = count_records(host, json.loads(content))
                except Exception:
                    pass
                self.overhead_s += _perf() - t_over
        failed = exc is not None or (status is not None and status >= 400)
        with self._lock:
            hs = self._hosts[host]
            hs.in_flight -= 1
            self._in_flight_total -= 1
            hs.net_time_s += dt
            hs.bytes_in += nbytes
            if exc is not None:
                hs.exceptions[type(exc).__name__] += 1
                if body_exc is not None:
                    hs.body_errors += 1
            else:
                hs.status[status] += 1
                if 200 <= status < 300:
                    hs.ok += 1
                    self._url_ok[ctx["url"]] += 1
            if records:
                hs.records += records
            if bad_body is not None:
                hs.non_json_200 += 1
                if len(hs.non_json_samples) < 3:
                    hs.non_json_samples.append(bad_body)
            self._last_by_thread[ctx["tid"]] = (ctx["url"], failed, ctx.get("call"))
            if self.keep_trace and len(self.trace) < self.max_trace:
                self.trace.append({
                    "t": round(ctx["t"] - self.t0, 4),
                    "dt": round(dt, 4),
                    "host": host,
                    "url": ctx["url"][:300],
                    "status": status,
                    "exc": type(exc).__name__ if exc is not None else None,
                    "source": ctx["source"],
                })
        return body_exc

    # --- restitution ---

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            hosts = {h: s.as_dict() for h, s in sorted(self._hosts.items())}
            by_source = {s: dict(c) for s, c in sorted(self._by_source.items())}
        total = {
            "requests": sum(h["requests"] for h in hosts.values()),
            "ok": sum(h["ok"] for h in hosts.values()),
            "retries": sum(h["retries"] for h in hosts.values()),
            "repeats_after_failure": sum(h["repeats_after_failure"] for h in hosts.values()),
            "duplicate_fetches": sum(h["duplicate_fetches"] for h in hosts.values()),
            "body_errors": sum(h["body_errors"] for h in hosts.values()),
            "status_429": sum(h["status"].get("429", 0) for h in hosts.values()),
            "errors_http": sum(
                v for h in hosts.values() for k, v in h["status"].items()
                if int(k) >= 400
            ),
            "exceptions": sum(sum(h["exceptions"].values()) for h in hosts.values()),
            "bytes_in": sum(h["bytes_in"] for h in hosts.values()),
            "records": sum(h["records"] for h in hosts.values()),
            "non_json_200": sum(h["non_json_200"] for h in hosts.values()),
            "max_in_flight": self.max_in_flight_total,
        }
        return {
            "total": total,
            "hosts": hosts,
            "by_source": by_source,
            "instrument_overhead_s": round(self.overhead_s, 4),
        }


def install_network_layer(recorder: NetworkRecorder,
                          inner_send: Optional[Callable] = None,
                          guard: Optional[Callable[[str, Any], None]] = None):
    """
    Remplace ``HTTPAdapter.send`` par une version comptée.

    Args:
        recorder: Compteurs à alimenter.
        inner_send: Fonction ``(adapter, request, **kw) -> Response`` appelée à
            la place de l'envoi réel (simulateur). None : envoi réel.
        guard: Fonction ``(host, request)`` qui lève pour refuser une requête
            (liste blanche du mode live, refus de SerpAPI). La requête refusée
            est comptée comme une exception.

    Returns:
        Fonction sans argument qui restaure l'adaptateur d'origine.
    """
    import requests.adapters

    original = requests.adapters.HTTPAdapter.send
    target = inner_send or original

    def send(adapter, request, stream=False, timeout=None, verify=True,
             cert=None, proxies=None):
        ctx = recorder.begin(request.url)
        try:
            if guard is not None:
                guard(ctx["host"], request)
            response = target(adapter, request, stream=stream, timeout=timeout,
                              verify=verify, cert=cert, proxies=proxies)
        except BaseException as exc:
            recorder.end(ctx, exc=exc)
            raise
        body_exc = recorder.end(ctx, response=response, stream=stream)
        if body_exc is not None:
            # Sans banc, Session.send aurait levé cette même exception en
            # lisant le corps (r.content) : on la relève pour ne modifier ni
            # le comportement du code mesuré (retry de HTTPClient) ni ce qu'il
            # observe.
            raise body_exc
        return response

    send._bench_wrapped = True  # repère pour les tests
    requests.adapters.HTTPAdapter.send = send

    def restore():
        requests.adapters.HTTPAdapter.send = original

    return restore


class CacheCounter:
    """
    Succès et échecs du cache HTTP ``requests-cache`` (option ``--cache``).

    Posé AU-DESSUS du cache (``CacheMixin.send``), alors que les compteurs
    réseau sont en dessous : un succès n'atteint jamais l'adaptateur.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.installed = False

    def reset(self) -> None:
        with self._lock:
            self.hits = 0
            self.misses = 0

    def install(self) -> None:
        try:
            from requests_cache.session import CacheMixin
        except Exception:  # requests-cache absent : rien à compter
            return
        original = CacheMixin.send
        if getattr(original, "_bench_wrapped", False):
            return
        counter = self

        def send(self_, request, *args, **kwargs):
            response = original(self_, request, *args, **kwargs)
            with counter._lock:
                if getattr(response, "from_cache", False):
                    counter.hits += 1
                else:
                    counter.misses += 1
            return response

        send._bench_wrapped = True
        CacheMixin.send = send
        self.installed = True

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"installed": self.installed, "hits": self.hits,
                    "misses": self.misses}


# === Sommeils du code mesuré ===

class SleepRecorder:
    """
    Comptabilise (et peut mettre à l'échelle) les ``time.sleep`` du code mesuré.

    Les délais de politesse (``REQUEST_DELAY``, espacements S2/CORE) et les
    attentes de backoff/Retry-After passent par ``time.sleep`` : leur cumul par
    module appelant est une mesure directe du temps « perdu à attendre ».
    ``scale=0`` rend ces attentes instantanées (tests rapides) tout en les
    comptant ; la durée mesurée n'est alors plus réaliste.

    Limite : une attente implémentée par ``threading.Event.wait`` ou
    ``Condition.wait`` (annulation interruptible, OPT-07) n'est ni comptée ni
    mise à l'échelle.
    """

    def __init__(self, scale: float = 1.0):
        self.scale = float(scale)
        self._lock = threading.Lock()
        self.total_s: Counter = Counter()
        self.calls: Counter = Counter()

    def sleep(self, seconds):
        try:
            caller = sys._getframe(1).f_globals.get("__name__", "?")
        except Exception:  # pragma: no cover - défensif
            caller = "?"
        try:
            secs = float(seconds)
        except (TypeError, ValueError):
            return ORIGINAL_SLEEP(seconds)
        with self._lock:
            self.total_s[caller] += max(secs, 0.0)
            self.calls[caller] += 1
        if secs > 0 and self.scale > 0:
            ORIGINAL_SLEEP(secs * self.scale)

    def reset(self) -> None:
        with self._lock:
            self.total_s.clear()
            self.calls.clear()

    def install(self, modules: List[Any] = ()) -> None:
        """Remplace ``time.sleep`` et les références importées par nom."""
        time.sleep = self.sleep
        for mod in modules:
            # Jamais ce module : y remplacer ORIGINAL_SLEEP rendrait sleep()
            # récursif (arbre mesuré = racine du dépôt, qui contient le banc).
            if getattr(mod, "__name__", None) == __name__:
                continue
            for attr, value in list(vars(mod).items()):
                if value is ORIGINAL_SLEEP:
                    setattr(mod, attr, self.sleep)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "scale": self.scale,
                "total_s": round(sum(self.total_s.values()), 3),
                "calls": sum(self.calls.values()),
                "by_module": {
                    m: {"s": round(s, 3), "calls": self.calls[m]}
                    for m, s in self.total_s.most_common()
                },
            }


# === Chronométrage des étapes ===

class StageRecorder:
    """
    Chronomètre des fonctions du moissonneur sans modifier son code.

    Chaque fonction instrumentée est remplacée par un enveloppant dans TOUS les
    modules de la copie mesurée qui la référencent (``from x import f`` crée
    une référence par module) : un seul enveloppant, donc aucun double compte.
    Une fonction absente (renommée par une refonte) est signalée dans
    ``missing`` au lieu de faire échouer la mesure.

    Chaque entrée dans un enveloppant ouvre aussi un **appel logique** (pile
    par fil, identifiant unique) : ``current_call`` renvoie l'appel le plus
    interne, que ``NetworkRecorder`` utilise pour distinguer une reprise (même
    appel) d'un nouvel appel indépendant sur la même URL.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.stages: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"calls": 0, "total_s": 0.0}
        )
        self.sources: List[Dict[str, Any]] = []
        self.connector_calls: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"calls": 0, "items": 0, "total_s": 0.0}
        )
        self.missing: List[str] = []
        self.hooks: Dict[str, Callable] = {}
        self._local = threading.local()
        self._call_ids = itertools.count(1)

    def reset(self) -> None:
        with self._lock:
            self.stages.clear()
            self.sources.clear()
            self.connector_calls.clear()

    # --- source courante du fil ---

    def current_source(self) -> Optional[str]:
        stack = getattr(self._local, "sources", None)
        return stack[-1] if stack else None

    def _push_source(self, name: str) -> None:
        stack = getattr(self._local, "sources", None)
        if stack is None:
            stack = self._local.sources = []
        stack.append(name)

    def _pop_source(self) -> None:
        stack = getattr(self._local, "sources", None)
        if stack:
            stack.pop()

    # --- appel logique courant du fil ---

    def current_call(self) -> Optional[int]:
        stack = getattr(self._local, "calls", None)
        return stack[-1] if stack else None

    def _push_call(self) -> None:
        stack = getattr(self._local, "calls", None)
        if stack is None:
            stack = self._local.calls = []
        stack.append(next(self._call_ids))

    def _pop_call(self) -> None:
        stack = getattr(self._local, "calls", None)
        if stack:
            stack.pop()

    # --- enveloppants ---

    def _record(self, stage: str, dt: float) -> None:
        with self._lock:
            st = self.stages[stage]
            st["calls"] += 1
            st["total_s"] += dt

    def wrap_everywhere(self, modules: List[Any], module_name: str, attr,
                        stage: str, kind: str = "plain") -> bool:
        """
        Enveloppe ``module_name.attr`` partout où cet objet est référencé.

        Args:
            modules: Modules de la copie mesurée (``sys.modules`` filtré).
            module_name: Module de référence où chercher la fonction.
            attr: Nom de la fonction, ou tuple de noms : le premier présent
                dans le module est retenu (fonction renommée ou ajoutée par
                une refonte, ex. ``deduplicate_with_report`` puis
                ``deduplicate`` pour une copie plus ancienne).
            stage: Nom d'étape dans les métriques.
            kind: "plain", "run_source" (source courante + compte d'items) ou
                "dedup" (tailles d'entrée/sortie + crochet d'analyse).

        Returns:
            True si la fonction a été trouvée et enveloppée.
        """
        mod = sys.modules.get(module_name)
        if isinstance(attr, (tuple, list)):
            chosen = next((a for a in attr if mod is not None
                           and getattr(mod, a, None) is not None), attr[-1])
            attr = chosen
        original = getattr(mod, attr, None) if mod else None
        if original is None or getattr(original, "_bench_wrapped", False):
            if original is None:
                self.missing.append(f"{module_name}.{attr}")
            return False
        wrapper = self._make_wrapper(original, stage, kind)
        for m in modules:
            for name, value in list(vars(m).items()):
                if value is original:
                    setattr(m, name, wrapper)
        return True

    def _make_wrapper(self, original: Callable, stage: str, kind: str) -> Callable:
        recorder = self

        if kind == "run_source":
            def wrapper(*args, **kwargs):
                name = str(args[0]) if args else str(kwargs.get("name", "?"))
                recorder._push_source(name)
                recorder._push_call()
                t = _perf()
                result = None
                err = None
                try:
                    result = original(*args, **kwargs)
                    return result
                except BaseException as e:
                    err = type(e).__name__
                    raise
                finally:
                    dt = _perf() - t
                    recorder._pop_call()
                    recorder._pop_source()
                    recorder._record(stage, dt)
                    # Moteur (OPT-07) : run_source renvoie un SourceResult
                    # (notices + statut) ; copie d'avant : une liste.
                    items, status, reason = source_payload(result)
                    n = _safe_len(items)
                    with recorder._lock:
                        entry = {
                            "source": name, "items": n,
                            "duration_s": round(dt, 4), "error": err,
                        }
                        if status is not None:
                            entry["status"] = status
                            entry["reason"] = reason
                        recorder.sources.append(entry)
                    # Crochet d'analyse (couverture par source en simulé) :
                    # appelé hors chronométrage de la source.
                    hook = recorder.hooks.get("source_items")
                    if hook is not None and items is not None:
                        hook(name, items)
        elif kind == "dedup":
            def wrapper(*args, **kwargs):
                items = args[0] if args else kwargs.get("items")
                hook = recorder.hooks.get("dedup_input")
                if hook is not None:
                    hook(items)
                recorder._push_call()
                t = _perf()
                try:
                    out = original(*args, **kwargs)
                finally:
                    recorder._pop_call()
                dt = _perf() - t
                recorder._record(stage, dt)
                # deduplicate_with_report renvoie (notices, rapport).
                out_items = (out[0] if isinstance(out, tuple) and len(out) == 2
                             and isinstance(out[0], list) else out)
                with recorder._lock:
                    st = recorder.stages[stage]
                    st["items_in"] = st.get("items_in", 0) + (_safe_len(items) or 0)
                    st["items_out"] = st.get("items_out", 0) + (_safe_len(out_items) or 0)
                return out
        else:
            def wrapper(*args, **kwargs):
                recorder._push_call()
                t = _perf()
                try:
                    return original(*args, **kwargs)
                finally:
                    recorder._record(stage, _perf() - t)
                    recorder._pop_call()

        wrapper._bench_wrapped = True
        wrapper.__wrapped__ = original
        wrapper.__name__ = getattr(original, "__name__", stage)
        wrapper.__doc__ = getattr(original, "__doc__", None)
        return wrapper

    def wrap_connector_classes(self, classes: List[type]) -> None:
        """Enveloppe la méthode ``search`` propre à chaque classe de connecteur."""
        recorder = self
        for cls in classes:
            original = cls.__dict__.get("search")
            if original is None or getattr(original, "_bench_wrapped", False):
                continue
            label = cls.__name__

            def make(orig, label):
                def search(self_, *args, **kwargs):
                    recorder._push_call()
                    t = _perf()
                    out = None
                    try:
                        out = orig(self_, *args, **kwargs)
                        return out
                    finally:
                        dt = _perf() - t
                        recorder._pop_call()
                        with recorder._lock:
                            st = recorder.connector_calls[label]
                            st["calls"] += 1
                            st["items"] += _safe_len(out) or 0
                            st["total_s"] += dt
                search._bench_wrapped = True
                search.__wrapped__ = orig
                return search

            setattr(cls, "search", make(original, label))

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "stages": {
                    k: {kk: (round(vv, 4) if isinstance(vv, float) else vv)
                        for kk, vv in v.items()}
                    for k, v in self.stages.items()
                },
                "sources": list(self.sources),
                "connector_calls": {
                    k: {kk: (round(vv, 4) if isinstance(vv, float) else vv)
                        for kk, vv in v.items()}
                    for k, v in sorted(self.connector_calls.items())
                },
                "missing": list(self.missing),
            }


def _safe_len(obj) -> Optional[int]:
    try:
        return len(obj)
    except Exception:
        return None


def source_payload(result: Any):
    """
    Notices, statut et motif du retour de ``run_source``.

    Le moteur (OPT-07) renvoie un ``SourceResult`` (attributs ``items``,
    ``status``, ``reason``) ; une copie plus ancienne renvoie la liste des
    notices, sans statut.

    Returns:
        ``(notices, statut ou None, motif ou None)``.
    """
    if result is None or isinstance(result, (list, tuple, dict)):
        return result, None, None
    items = getattr(result, "items", None)
    status = getattr(result, "status", None)
    if isinstance(items, list) and isinstance(status, str):
        return items, status, getattr(result, "reason", None)
    return result, None, None


# === Garde-socket ===

class RealSocketBlocked(OSError):
    """Tentative de connexion réelle alors que le banc l'interdit."""


class SocketGuard:
    """
    Interdit toute connexion réseau réelle (mode simulé, tests).

    Remplace ``socket.socket.connect``/``connect_ex`` et
    ``socket.create_connection`` : une tentative lève ``RealSocketBlocked`` et
    est comptée. Les paires de sockets locales (``socketpair`` d'asyncio) ne
    passent pas par ``connect`` sous Unix et restent utilisables.
    """

    def __init__(self):
        self.attempts: List[str] = []
        self._saved = None

    def install(self) -> None:
        guard = self
        self._saved = (socket.socket.connect, socket.socket.connect_ex,
                       socket.create_connection)

        def refuse(target) -> None:
            guard.attempts.append(repr(target)[:200])
            raise RealSocketBlocked(
                f"connexion réseau réelle interdite par le banc : {target!r}"
            )

        def connect(self_, address):
            refuse(address)

        def connect_ex(self_, address):
            refuse(address)

        def create_connection(address, *args, **kwargs):
            refuse(address)

        socket.socket.connect = connect
        socket.socket.connect_ex = connect_ex
        socket.create_connection = create_connection

    def uninstall(self) -> None:
        if self._saved:
            (socket.socket.connect, socket.socket.connect_ex,
             socket.create_connection) = self._saved
            self._saved = None
