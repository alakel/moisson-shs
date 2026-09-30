"""
Classeurs comparés par le protocole d'évaluation (OPT-15).

Interface commune
-----------------

Un classeur est une fonction ``rank(items, request, harvest)`` qui reçoit :

- ``items`` : les notices fusionnées d'une moisson (JSON Zotero, après
  déduplication) ;
- ``request`` : la ``engine.models.SearchRequest`` de la moisson ;
- ``harvest`` : le ``engine.models.HarvestResult`` reconstitué (sources, rangs
  natifs de chaque source, plan de requête), plus l'attribut ``eval_groups``
  (pour chaque notice fusionnée, les couples ``[indice de source, position
  dans la source]`` de ses membres) ;

et renvoie un ordre des notices. ``Ranker.rank`` normalise cet ordre en
**liste ordonnée d'identifiants de candidats** (``common.candidate_ids``).
Sorties admises : identifiants, indices, notices elles-mêmes, couples
``(notice ou indice, score)``, dictionnaires ou objets portant ``index`` /
``position`` / ``item`` / ``cid``, liste de scores alignée sur les notices,
ou objet (dict) portant l'une de ces listes sous ``order``, ``ranking``,
``ranked``, ``entries``, ``results`` ou ``items``.

Classeurs fournis
-----------------

- ``native`` : ordre par défaut quand le classement local est désactivé,
  c'est-à-dire la référence du gate. Si ``utils.ranking`` (OPT-13) est
  présent : ``rank_records(mode="native")`` (fusion de rangs réciproques des
  rangs natifs pondérés par la nature de l'ordre de chaque source, voir ce
  module). Sinon : repli sur ``entrelacement`` (signalé dans les notes).
- ``entrelacement`` : entrelacement des ordres natifs, sans dépendance à
  OPT-13. Chaque notice fusionnée prend la meilleure position qu'une de ses
  sources lui a donnée (position 1 = premier résultat servi par la source,
  après le filtre local éventuel) ; tri par (meilleure position, ordre des
  sources demandées, identifiant). Chaque source garde son propre ordre,
  sans score commun.
- ``local`` (et ``local:<variante>``) : classement local d'OPT-13
  (``rank_records(mode="local", lexical_model=<variante>)``, variantes
  ``simple`` et ``bm25``), découvert dans ``utils.ranking`` s'il existe
  (adaptateur tolérant à son absence et à une API différente : voir
  ``discover_ranking_module``). Crochet prioritaire, si OPT-13 le publie :
  ``eval_rankers()`` (ou ``EVAL_RANKERS``) renvoyant ``{nom: fonction}`` à
  l'interface ci-dessus.
- externe : ``nom=paquet.module:fonction`` (option ``--ranker`` de
  ``pool.py build``), même interface.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import inspect
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:  # exécution comme script ou import comme paquet « eval »
    from eval.common import candidate_ids, canonical_dump
except ImportError:  # pragma: no cover
    from common import candidate_ids, canonical_dump  # type: ignore

logger = logging.getLogger("moisson.eval.rankers")

NATIVE = "native"
INTERLEAVE = "entrelacement"
LOCAL = "local"
INTERLEAVE_VERSION = "entrelacement/1"
LOCAL_MODULE = "utils.ranking"


class AdapterError(RuntimeError):
    """Sortie ou signature de classeur non reconnue."""


@dataclass
class Ranker:
    """Classeur nommé (voir le docstring du module pour l'interface)."""

    name: str
    kind: str
    func: Callable[..., Any]
    version: Optional[str] = None
    description: str = ""
    # Faux pour les seuls classeurs du protocole (lecture seule garantie) ;
    # vrai pour un classeur tiers, qui reçoit des copies des notices.
    isolate: bool = True
    # Empreinte du fichier source du module de classement (``module_fingerprint``) :
    # consignée dans les ensembles de candidats, elle révèle une modification du
    # code faite sans changement de version.
    fingerprint: Optional[str] = None

    def rank(self, items: Sequence[Dict], request: Any, harvest: Any) -> List[str]:
        cids = candidate_ids(items)
        if not self.isolate:
            raw = self.func(items, request, harvest)
            return normalize_ranking(raw, items, cids)
        # Classeur tiers : il travaille sur des copies (aucune mutation des
        # notices du protocole) ; l'ordre est ramené aux indices d'origine.
        work = copy.deepcopy(list(items))
        harvest_copy = _harvest_view(harvest, work)
        before = [id(it) for it in work]
        raw = self.func(work, request, harvest_copy)
        if raw is None:
            # Classement en place (liste réordonnée ou HarvestResult modifié).
            reordered = getattr(harvest_copy, "items", None)
            if isinstance(reordered, list) and [id(i) for i in reordered] != before:
                raw = reordered
            elif [id(it) for it in work] != before:
                raw = work
            else:
                ranking = getattr(harvest_copy, "ranking", None)
                if ranking is None:
                    raise AdapterError(f"{self.name} : aucune sortie ni classement en place")
                raw = ranking
        return normalize_ranking(raw, work, cids)


def _harvest_view(harvest: Any, items: List[Dict]) -> Any:
    """Copie superficielle du résultat de moisson portant les notices copiées."""
    if harvest is None:
        return None
    try:
        view = copy.copy(harvest)
        view.items = items
        return view
    except Exception:  # noqa: BLE001 - objet non copiable : tel quel
        return harvest


# === Normalisation d'une sortie de classeur ===

_CONTAINER_KEYS = ("order", "ordering", "ranking", "ranked", "entries",
                   "results", "items")
_INDEX_KEYS = ("index", "idx", "position", "item_index")
_ID_KEYS = ("cid", "candidate_id", "id")


def _unwrap(raw: Any) -> Any:
    for _ in range(3):
        if isinstance(raw, dict):
            for key in _CONTAINER_KEYS:
                if key in raw and isinstance(raw[key], (list, tuple)):
                    raw = raw[key]
                    break
            else:
                return raw
        elif not isinstance(raw, (list, tuple, str, bytes)) and raw is not None:
            for key in _CONTAINER_KEYS:
                value = getattr(raw, key, None)
                if isinstance(value, (list, tuple)):
                    raw = value
                    break
            else:
                return raw
        else:
            return raw
    return raw


def normalize_ranking(raw: Any, items: Sequence[Dict], cids: Sequence[str]) -> List[str]:
    """
    Ordre rendu par un classeur → liste ordonnée d'identifiants (sans
    doublon ; un élément non rattachable à une notice lève ``AdapterError``).
    """
    raw = _unwrap(raw)
    if raw is None or isinstance(raw, (str, bytes, dict)):
        raise AdapterError(f"sortie de classeur non reconnue : {type(raw).__name__}")
    seq = list(raw)
    n = len(items)
    by_identity = {id(it): i for i, it in enumerate(items)}
    by_cid = {c: i for i, c in enumerate(cids)}
    by_dump: Optional[Dict[str, int]] = None

    # Liste de scores alignée sur les notices : tri décroissant stable.
    if seq and len(seq) == n and all(
            isinstance(v, float) and not isinstance(v, bool) for v in seq):
        order = sorted(range(n), key=lambda i: (-seq[i], i))
        return [cids[i] for i in order]

    def resolve(el: Any, depth: int = 0) -> int:
        nonlocal by_dump
        if depth > 3:
            raise AdapterError("élément de classement trop imbriqué")
        if isinstance(el, bool):
            raise AdapterError("booléen dans un classement")
        if isinstance(el, int):
            if 0 <= el < n:
                return el
            raise AdapterError(f"indice hors bornes : {el}")
        if isinstance(el, str):
            if el in by_cid:
                return by_cid[el]
            raise AdapterError(f"identifiant inconnu : {el!r}")
        if isinstance(el, dict):
            if id(el) in by_identity:
                return by_identity[id(el)]
            for key in _ID_KEYS:
                if isinstance(el.get(key), str) and el[key] in by_cid:
                    return by_cid[el[key]]
            for key in _INDEX_KEYS:
                if isinstance(el.get(key), int) and not isinstance(el.get(key), bool):
                    return resolve(el[key], depth + 1)
            if isinstance(el.get("item"), dict):
                return resolve(el["item"], depth + 1)
            if "itemType" in el:
                if by_dump is None:
                    by_dump = {}
                    for i, it in enumerate(items):
                        by_dump.setdefault(canonical_dump(it), i)
                dump = canonical_dump(el)
                if dump in by_dump:
                    return by_dump[dump]
            raise AdapterError("notice rendue introuvable parmi les candidats")
        if isinstance(el, (list, tuple)) and el:
            return resolve(el[0], depth + 1)
        if id(el) in by_identity:
            return by_identity[id(el)]
        for key in _ID_KEYS:
            value = getattr(el, key, None)
            if isinstance(value, str) and value in by_cid:
                return by_cid[value]
        for key in _INDEX_KEYS:
            value = getattr(el, key, None)
            if isinstance(value, int) and not isinstance(value, bool):
                return resolve(value, depth + 1)
        item = getattr(el, "item", None)
        if isinstance(item, dict):
            return resolve(item, depth + 1)
        raise AdapterError(f"élément de classement non reconnu : {type(el).__name__}")

    out: List[str] = []
    seen = set()
    for el in seq:
        idx = resolve(el)
        if idx not in seen:
            seen.add(idx)
            out.append(cids[idx])
    return out


# === Classeur natif ===

def harvest_groups(items: Sequence[Dict], harvest: Any) -> List[List[Tuple[int, int]]]:
    """
    Membres de chaque notice fusionnée : couples ``(indice de source,
    position dans la source)``. Lus dans ``harvest.eval_groups`` (posé par
    ``pool.py``), sinon recalculés par la déduplication du moteur sur les
    notices des sources (déterministe) et rattachés par contenu.
    """
    groups = getattr(harvest, "eval_groups", None)
    if groups is not None and len(groups) == len(items):
        return [[(int(s), int(p)) for s, p in members] for members in groups]
    from utils.deduplication import deduplicate_with_report

    sources = list(getattr(harvest, "sources", None) or [])
    flat: List[Tuple[int, int]] = []
    all_items: List[Dict] = []
    for s, res in enumerate(sources):
        for p, it in enumerate(getattr(res, "items", None) or []):
            flat.append((s, p))
            all_items.append(it)
    records, report = deduplicate_with_report(copy.deepcopy(all_items), with_groups=True)
    by_dump: Dict[str, List[int]] = {}
    for r, rec in enumerate(records):
        by_dump.setdefault(canonical_dump(rec), []).append(r)
    out: List[List[Tuple[int, int]]] = []
    for it in items:
        rs = by_dump.get(canonical_dump(it)) or []
        members = [flat[g] for g in report["groups"][rs[0]]] if rs else []
        out.append(members)
    return out


def native_order(items: Sequence[Dict], request: Any, harvest: Any) -> List[int]:
    """Entrelacement des ordres natifs (voir le docstring du module)."""
    cids = candidate_ids(items)
    groups = harvest_groups(items, harvest)
    inf = float("inf")
    keys = []
    for i, members in enumerate(groups):
        if members:
            best = min(p + 1 for _s, p in members)
            src = min(s for s, p in members if p + 1 == best)
        else:
            best, src = inf, inf
        keys.append((best, src, cids[i]))
    return sorted(range(len(items)), key=lambda i: keys[i])


def interleave_ranker(name: str = INTERLEAVE) -> Ranker:
    return Ranker(name=name, kind="native", func=native_order,
                  version=INTERLEAVE_VERSION, isolate=False,
                  description="entrelacement des ordres natifs des sources")


def flat_groups(items: Sequence[Dict], harvest: Any) -> Optional[List[List[int]]]:
    """
    Groupes de la déduplication sous la forme attendue par
    ``utils.ranking.rank_records`` : pour chaque notice fusionnée, indices de
    ses membres dans la concaténation des notices des sources (ordre des
    sources du résultat). None si la composition n'est pas connue.
    """
    groups = getattr(harvest, "eval_groups", None)
    if groups is None or len(groups) != len(items):
        return None
    offsets, total = [], 0
    for res in getattr(harvest, "sources", None) or []:
        offsets.append(total)
        total += len(getattr(res, "items", None) or [])
    try:
        return [[offsets[int(s)] + int(p) for s, p in members] for members in groups]
    except (IndexError, TypeError, ValueError):
        return None


# === Classement local (OPT-13) : adaptateur tolérant ===

_FUNCTION_NAMES = ("rank_items", "rank", "rank_corpus", "rank_harvest",
                   "local_rank", "rank_records", "score_items", "apply_ranking")
_VARIANT_LISTS = ("VARIANTS", "METHODS", "SCORERS", "RANKING_METHODS",
                  "LEXICAL_METHODS", "AVAILABLE_METHODS")
_VERSION_NAMES = ("RANKING_VERSION", "VERSION", "__version__")

ITEM_PARAMS = ("items", "records", "notices", "corpus", "docs", "documents")
REQUEST_PARAMS = ("request", "search_request", "req")
HARVEST_PARAMS = ("harvest", "result", "harvest_result", "harvested")
QUERY_PARAMS = ("query", "q", "query_text")
PLAN_PARAMS = ("plan", "query_plan")
SOURCES_PARAMS = ("sources", "source_results", "results_by_source")
VARIANT_PARAMS = ("variant", "method", "scorer", "lexical", "weighting",
                  "algorithm", "model", "scoring")

_MISSING = object()


def call_flexible(func: Callable, items: List[Dict], request: Any, harvest: Any,
                  variant: Optional[str] = None) -> Any:
    """
    Appelle une fonction de classement d'API inconnue en rattachant ses
    paramètres par leur nom (notices, demande, résultat, requête, plan,
    sources, variante). Un premier paramètre positionnel au nom inconnu
    reçoit les notices ; un autre paramètre obligatoire inconnu lève
    ``AdapterError``.
    """
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return func(items, request, harvest)
    args: List[Any] = []
    kwargs: Dict[str, Any] = {}
    for i, p in enumerate(sig.parameters.values()):
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        name = p.name.lower()
        value: Any = _MISSING
        if name in ITEM_PARAMS:
            value = items
        elif name in REQUEST_PARAMS:
            value = request
        elif name in HARVEST_PARAMS:
            value = harvest
        elif name in QUERY_PARAMS:
            value = getattr(request, "query", None)
        elif name in PLAN_PARAMS:
            plan = getattr(request, "query_plan", None)
            value = plan() if callable(plan) else plan
        elif name in SOURCES_PARAMS:
            value = getattr(harvest, "sources", None)
        elif name in VARIANT_PARAMS and variant is not None:
            value = variant
        if value is _MISSING:
            if i == 0 and p.default is p.empty and p.kind in (
                    p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
                args.append(items)
                continue
            if p.default is p.empty:
                raise AdapterError(f"paramètre obligatoire non reconnu : {p.name}")
            continue
        if p.kind == p.POSITIONAL_ONLY:
            args.append(value)
        else:
            kwargs[p.name] = value
    return func(*args, **kwargs)


def _variant_param(func: Callable) -> Optional[str]:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return None
    for p in params.values():
        if p.name.lower() in VARIANT_PARAMS:
            return p.name
    return None


def _opt13_rankers(module: Any, version: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Adaptateur de l'API d'OPT-13 : ``rank_records(records, sources, *, mode,
    plan, query, query_variants, lexical_model, groups, reference_year)``,
    qui rend un dictionnaire dont ``entries`` (par rang croissant) porte
    l'``index`` de chaque notice. None si le module n'a pas cette API.
    """
    rank_records = getattr(module, "rank_records", None)
    if not callable(rank_records):
        return None
    try:
        params = inspect.signature(rank_records).parameters
    except (TypeError, ValueError):
        return None
    if "mode" not in params:
        return None
    native_mode = getattr(module, "RANKING_NATIVE", NATIVE)
    local_mode = getattr(module, "RANKING_LOCAL", LOCAL)
    models = [str(m) for m in (getattr(module, "LEXICAL_MODELS", None) or ())]
    name = getattr(module, "__name__", LOCAL_MODULE)

    def make(mode: str, model: Optional[str] = None) -> Callable[..., Any]:
        def run(items, request, harvest):
            kwargs: Dict[str, Any] = {"mode": mode}
            if request is not None:
                if "plan" in params:
                    plan = getattr(request, "query_plan", None)
                    kwargs["plan"] = plan() if callable(plan) else plan
                if "query" in params:
                    kwargs["query"] = getattr(request, "query", None)
                if "query_variants" in params:
                    kwargs["query_variants"] = dict(getattr(request, "query_variants", None) or {})
            if model is not None and "lexical_model" in params:
                kwargs["lexical_model"] = model
            groups = flat_groups(items, harvest)
            if groups is not None and "groups" in params:
                kwargs["groups"] = groups
            year = getattr(harvest, "eval_reference_year", None)
            if year and "reference_year" in params:
                kwargs["reference_year"] = int(year)
            sources = list(getattr(harvest, "sources", None) or [])
            return rank_records(items, sources, **kwargs)
        return run

    native = Ranker(name=NATIVE, kind="native", func=make(native_mode), version=version,
                    description=f"{name}.rank_records(mode={native_mode!r})")
    local = [Ranker(name=f"{LOCAL}:{m}", kind="local", func=make(local_mode, m),
                    version=version,
                    description=f"{name}.rank_records(mode={local_mode!r}, lexical_model={m!r})")
             for m in models] or [
        Ranker(name=LOCAL, kind="local", func=make(local_mode), version=version,
               description=f"{name}.rank_records(mode={local_mode!r})")]
    return {"native": native, "local": local}


def module_fingerprint(module: Any) -> Optional[str]:
    """
    Empreinte du code d'un module de classement : SHA-256 (12 premiers
    caractères hexadécimaux) de son fichier source ; None si le fichier est
    introuvable. Toute modification du fichier la change, même sans
    changement de ``RANKING_VERSION``.
    """
    path = getattr(module, "__file__", None)
    if not path:
        return None
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]
    except OSError:
        return None


def _stamped(found: Tuple[Optional[Ranker], List[Ranker], Optional[str]],
             fingerprint: Optional[str]) -> Tuple[Optional[Ranker], List[Ranker], Optional[str]]:
    native, local, message = found
    for ranker in ([native] if native else []) + list(local):
        ranker.fingerprint = fingerprint
    return native, local, message


def discover_ranking_module(module_name: str = LOCAL_MODULE
                            ) -> Tuple[Optional[Ranker], List[Ranker], Optional[str]]:
    """Voir ``_discover_ranking_module`` ; chaque classeur trouvé porte
    l'empreinte du fichier source du module (``module_fingerprint``)."""
    found = _discover_ranking_module(module_name)
    module = sys.modules.get(module_name)
    return _stamped(found, module_fingerprint(module) if module else None)


def _discover_ranking_module(module_name: str = LOCAL_MODULE
                             ) -> Tuple[Optional[Ranker], List[Ranker], Optional[str]]:
    """
    Classeurs d'OPT-13 trouvés dans ``module_name`` : ``(natif, locaux,
    message)``.

    Ordre de recherche : crochet ``eval_rankers()`` / ``EVAL_RANKERS``
    (``{nom: fonction(items, request, harvest)}`` ; ``native`` y désigne
    l'ordre par défaut) ; API d'OPT-13 (``rank_records`` avec ``mode``) ;
    sinon première fonction connue (``rank_items``, ``rank``…), déclinée par
    variante si le module publie une liste (``VARIANTS``, ``METHODS``…) et
    que la fonction a un paramètre de variante. Ne lève jamais : le message
    explique une absence (module absent, aucune fonction reconnue), et le
    protocole reste utilisable avec l'entrelacement des ordres natifs.
    """
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        return None, [], f"classement local indisponible : module {module_name} absent ({exc})"
    except Exception as exc:  # noqa: BLE001 - module en cours d'écriture
        return None, [], (f"classement local indisponible : import de {module_name} "
                          f"en échec ({exc!r})")
    version = None
    for name in _VERSION_NAMES:
        value = getattr(module, name, None)
        if isinstance(value, str) and value:
            version = value
            break

    hook = getattr(module, "eval_rankers", None) or getattr(module, "EVAL_RANKERS", None)
    if hook is not None:
        try:
            table = hook() if callable(hook) else hook
        except Exception as exc:  # noqa: BLE001
            return None, [], f"crochet eval_rankers de {module_name} en échec ({exc!r})"
        native, local = None, []
        for name, func in dict(table).items():
            wrapped = (lambda items, request, harvest, _f=func: _f(items, request, harvest))
            if name == NATIVE:
                native = Ranker(name=NATIVE, kind="native", func=wrapped, version=version,
                                description=f"{module_name} (crochet eval_rankers)")
                continue
            label = name if str(name).startswith(LOCAL) else f"{LOCAL}:{name}"
            local.append(Ranker(name=label, kind="local", func=wrapped, version=version,
                                description=f"{module_name} (crochet eval_rankers)"))
        return native, local, None if (native or local) else f"crochet eval_rankers de {module_name} vide"

    opt13 = _opt13_rankers(module, version)
    if opt13 is not None:
        return opt13["native"], opt13["local"], None

    func = None
    for name in _FUNCTION_NAMES:
        candidate = getattr(module, name, None)
        if callable(candidate):
            func = candidate
            break
    if func is None:
        return None, [], (f"classement local indisponible : aucune fonction reconnue dans "
                          f"{module_name} ({', '.join(_FUNCTION_NAMES)})")
    variants: List[str] = []
    for name in _VARIANT_LISTS:
        value = getattr(module, name, None)
        if isinstance(value, dict):
            variants = [str(k) for k in value]
        elif isinstance(value, (list, tuple, set, frozenset)):
            variants = [str(v) for v in value]
        if variants:
            break
    param = _variant_param(func)
    if variants and param:
        rankers = [
            Ranker(name=f"{LOCAL}:{v}", kind="local",
                   func=lambda items, request, harvest, _v=v: call_flexible(
                       func, items, request, harvest, variant=_v),
                   version=version, description=f"{module_name}.{func.__name__} ({param}={v})")
            for v in sorted(set(variants), key=variants.index)
        ]
        return None, rankers, None
    return None, [Ranker(name=LOCAL, kind="local",
                         func=lambda items, request, harvest: call_flexible(func, items, request, harvest),
                         version=version, description=f"{module_name}.{func.__name__}")], None


def discover_local_rankers(module_name: str = LOCAL_MODULE
                           ) -> Tuple[List[Ranker], Optional[str]]:
    """Classeurs locaux seuls (voir ``discover_ranking_module``)."""
    _native, local, message = discover_ranking_module(module_name)
    return local, message


def load_external(spec: str) -> Ranker:
    """Classeur externe ``nom=paquet.module:fonction`` (interface commune)."""
    name, sep, target = spec.partition("=")
    if not sep or ":" not in target:
        raise AdapterError(f"classeur externe attendu sous la forme nom=module:fonction ({spec!r})")
    module_name, _, attr = target.partition(":")
    module = importlib.import_module(module_name.strip())
    func = getattr(module, attr.strip())
    return Ranker(name=name.strip(), kind="externe",
                  func=lambda items, request, harvest: call_flexible(func, items, request, harvest),
                  version=getattr(module, "RANKING_VERSION", None),
                  description=target.strip(), fingerprint=module_fingerprint(module))


def resolve_rankers(names: Optional[Sequence[str]] = None,
                    external: Optional[Sequence[str]] = None,
                    module_name: str = LOCAL_MODULE) -> Tuple[List[Ranker], List[str]]:
    """
    Classeurs demandés (défaut : ``native`` et tous les ``local``
    disponibles) et notes (absences et replis expliqués). ``native`` : ordre
    par défaut d'OPT-13, ou entrelacement à défaut ; ``entrelacement`` :
    toujours l'entrelacement ; ``local`` : toutes les variantes locales ;
    ``local:<variante>`` : une seule.
    """
    names = list(names or [NATIVE, LOCAL])
    notes: List[str] = []
    out: List[Ranker] = []
    discovered: Optional[Tuple[Optional[Ranker], List[Ranker]]] = None

    def _discover() -> Tuple[Optional[Ranker], List[Ranker]]:
        nonlocal discovered
        if discovered is None:
            native, local, message = discover_ranking_module(module_name)
            if message:
                notes.append(message)
            discovered = (native, local)
        return discovered

    for name in names:
        taken = {r.name for r in out}
        if name == INTERLEAVE:
            if INTERLEAVE not in taken:
                out.append(interleave_ranker())
            continue
        if name == NATIVE:
            native, _local = _discover()
            if native is None:
                notes.append("ordre natif d'OPT-13 indisponible : « native » = "
                             "entrelacement des ordres natifs")
                native = interleave_ranker(NATIVE)
            if NATIVE not in taken:
                out.append(native)
            continue
        if name == LOCAL or name.startswith(LOCAL + ":"):
            _native, local = _discover()
            chosen = [r for r in local if name == LOCAL or r.name == name]
            if not chosen and local:
                notes.append(f"variante locale inconnue : {name} "
                             f"(disponibles : {', '.join(r.name for r in local)})")
            out.extend(r for r in chosen if r.name not in taken)
            continue
        notes.append(f"classeur inconnu ignoré : {name}")
    for spec in external or []:
        ranker = load_external(spec)
        if ranker.name in {r.name for r in out}:
            raise AdapterError(f"nom de classeur en double : {ranker.name}")
        out.append(ranker)
    return out, notes
