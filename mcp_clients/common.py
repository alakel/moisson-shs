"""
Éléments communs aux adaptateurs de clients MCP : nom du serveur, clés
d'API, composition du bloc ``env``, masquage des secrets.

Règle des clés (sprint « clés ») : la déclaration du client porte toujours
les quatre clés, renseignées ou vides ; le `.env` du projet reste la réserve
du serveur et n'est jamais copié chez un client.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

SERVER_KEY = "moisson-shs"

# Clés d'API déclarées chez chaque client.
API_KEY_ENV_VARS = (
    "OPENALEX_API_KEY",
    "SEMANTIC_SCHOLAR_API_KEY",
    "CORE_API_KEY",
    "SERPAPI_API_KEY",
)

# Sous-blocs d'une entrée de serveur qui portent des secrets, quel que soit
# le client (env Claude/Codex/Gemini/Hermes, en-têtes HTTP, envs de Goose).
SECRET_CONTAINERS = ("env", "headers", "envs")


def complete_api_key_env(provided: Optional[dict],
                         previous_env: Optional[dict]) -> dict:
    """
    Les quatre clés d'API de la déclaration du client : valeur fournie à
    cette exécution, sinon valeur déjà déclarée, sinon chaîne vide. Une
    variable vide reste visible (à renseigner) et le serveur la traite comme
    absente : la réserve du `.env` peut alors servir (``utils.secrets``).
    """
    return resolve_api_keys(provided, previous_env, {}, [])


def resolve_api_keys(provided: Optional[dict], previous: Optional[dict],
                     pooled: Optional[dict], conflicts: Iterable[str]) -> dict:
    """
    Les quatre clés pour un client : valeur fournie, sinon déjà déclarée chez
    ce client, sinon valeur commune aux autres clients (sauf clé en
    conflit), sinon vide.
    """
    provided = provided or {}
    previous = previous if isinstance(previous, dict) else {}
    pooled = pooled or {}
    conflicts = set(conflicts or ())
    out = {}
    for var in API_KEY_ENV_VARS:
        value = (str(provided.get(var) or "").strip()
                 or str(previous.get(var) or "").strip())
        if not value and var not in conflicts:
            value = str(pooled.get(var) or "").strip()
        out[var] = value
    return out


def pool_api_keys(envs: Iterable[Optional[dict]]) -> Tuple[dict, List[str]]:
    """
    Mise en commun des clés déjà déclarées chez les clients : pour chaque
    clé, la valeur commune si tous les clients qui la déclarent non vide
    portent la même ; sinon la clé est en conflit (rien n'est mis en commun,
    aucune valeur n'est citée).

    Returns:
        ``(valeurs communes, clés en conflit)``.
    """
    seen: Dict[str, set] = {var: set() for var in API_KEY_ENV_VARS}
    for env in envs:
        if not isinstance(env, dict):
            continue
        for var in API_KEY_ENV_VARS:
            value = str(env.get(var) or "").strip()
            if value:
                seen[var].add(value)
    pooled = {var: next(iter(values)) for var, values in seen.items()
              if len(values) == 1}
    conflicts = [var for var in API_KEY_ENV_VARS if len(seen[var]) > 1]
    return pooled, conflicts


def merge_env(previous_env: Optional[dict], managed: Optional[dict],
              keys: Optional[dict]) -> dict:
    """
    Bloc ``env`` d'une nouvelle déclaration : les variables déjà déclarées
    sont conservées (réglages posés à la main, ``MOISSON_OUTPUT_DIR`` non
    repassé), les réglages fournis par ``setup`` (``managed``) les
    remplacent, les quatre clés suivent.
    """
    env = dict(previous_env) if isinstance(previous_env, dict) else {}
    env.update(managed or {})
    env.update(keys or {})
    return env


def api_keys_filled(env: Optional[dict]) -> int:
    """Nombre de clés d'API renseignées dans un bloc ``env``."""
    env = env if isinstance(env, dict) else {}
    return sum(1 for var in API_KEY_ENV_VARS if str(env.get(var) or "").strip())


def has_secret_values(entry: Optional[dict]) -> bool:
    """L'entrée porte-t-elle au moins une valeur secrète non vide ?"""
    return bool(collect_secret_values({"x": {"s": entry or {}}}))


def redact_env(entry: dict) -> dict:
    """
    Copie d'une entrée de serveur dont les VALEURS de clés d'API sont
    masquées, pour l'affichage uniquement ; une clé vide s'affiche
    « (vide) ».
    """
    redacted = dict(entry)
    env = redacted.get("env")
    if isinstance(env, dict):
        env_copy = dict(env)
        for var in API_KEY_ENV_VARS:
            if env_copy.get(var):
                n = len(str(env_copy[var]))
                env_copy[var] = f"*** masqué ({n} caractères) ***"
            elif var in env_copy:
                env_copy[var] = "(vide)"
        redacted["env"] = env_copy
    return redacted


_NOT_SECRET = re.compile(r"^(?:[/~.]|[A-Za-z]:[\\/]|\$\{)|://")


def _maybe_secret(value: str) -> bool:
    """Valeur susceptible d'être secrète : ni chemin, ni URL, ni référence
    ``${VAR}``, ni valeur trop courte (un chemin masqué rendrait les
    messages illisibles sans rien protéger)."""
    value = value.strip()
    return len(value) >= 6 and not _NOT_SECRET.search(value)


def collect_secret_values(doc: Any) -> List[str]:
    """
    Les valeurs des sous-blocs ``env``, ``headers`` et ``envs`` d'un document
    de configuration, quel que soit le serveur, qui peuvent être secrètes
    (``_maybe_secret``), plus le jeton nu d'un en-tête ``Bearer`` : de quoi
    nettoyer toute sortie (``scrub``) avant affichage.
    """
    found: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in SECRET_CONTAINERS and isinstance(value, dict):
                    for secret in value.values():
                        if isinstance(secret, str) and _maybe_secret(secret):
                            found.append(secret)
                            if secret.lower().startswith("bearer "):
                                found.append(secret[7:].strip())
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(doc)
    return list(dict.fromkeys(v for v in found if v))


def scrub(text: str, values: Iterable[str]) -> str:
    """Retire d'un message toute valeur secrète connue (la plus longue
    d'abord, pour qu'un préfixe commun ne laisse pas de reste)."""
    text = str(text or "").strip()
    for value in sorted({str(v) for v in values if v}, key=len, reverse=True):
        text = text.replace(value, "***")
    return text


def raise_to(previous: Optional[dict], key: str, minimum: int) -> int:
    """Délai relevé à ``minimum`` au moins, jamais abaissé."""
    previous = previous if isinstance(previous, dict) else {}
    try:
        value = int(previous.get(key) or 0)
    except (TypeError, ValueError):
        value = 0
    return max(value, int(minimum))
