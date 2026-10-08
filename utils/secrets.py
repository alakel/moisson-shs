"""
Chargement centralisé des clés API des sources internationales et de
Google Scholar.

Sources concernées :
- OPENALEX_API_KEY      — requis (clé gratuite, openalex.org/settings/api)
- SEMANTIC_SCHOLAR_API_KEY — optionnel (sans clé : pool partagé throttlé)
- CORE_API_KEY          — requis (sur inscription, core.ac.uk/services/api)
- SERPAPI_API_KEY       — requis pour Google Scholar (compte serpapi.com,
  facturé à la requête)

Déclaration du client et réserve du `.env` (sprint « clés ») : la clé
déclarée par le client (bloc ``env`` de Claude Desktop ou de Claude Code,
inscrit par ``moisson-shs setup``, ou shell pour la CLI) fait autorité. Le
`.env` du projet, créé par le développeur à partir de `.env.example`, est une
**réserve** : ses quatre clés sont lues à part et jamais injectées dans
``os.environ``. La réserve sert :

- quand la variable n'est pas déclarée (CLI, ancienne déclaration) ;
- quand elle est déclarée vide, si la source figure dans
  ``MOISSON_KEY_FALLBACK`` (défaut : openalex, semantic_scholar, core ;
  jamais SerpAPI par défaut, seule source facturée) ;
- quand la clé du client est refusée (HTTP 401 ou 403), aux mêmes
  conditions : ``utils.http.policy_get`` refait la requête avec la réserve
  (``substitute_credential``), et le refus est mémorisé pour le reste du
  processus.

Les autres variables du `.env` sont chargées comme avant, sans écraser une
variable déjà définie. Fichiers lus, si python-dotenv est installé : le
`.env` de la racine du projet (chemin calculé depuis ce module, non depuis le
répertoire courant), puis la recherche par défaut de dotenv ; aucun si
``MOISSON_NO_DOTENV=1`` (garde posée par la suite de tests). On ne committe
JAMAIS de clé ; voir `.env.example` pour les variables attendues.

Limite connue : ce module est importé après ``config.py``. Les réglages
``MOISSON_*`` que ``config.py`` lit à l'import ne sont donc pas pris dans un
`.env` local ; seules les clés et les réglages lus à l'appel (dont
``MOISSON_KEY_FALLBACK``) le sont à coup sûr. Passer ces réglages par le
shell, le bloc `env` du client ou l'``env_file`` de Docker Compose (voir
`.env.example`).
"""

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

import config

logger = logging.getLogger(__name__)

# Garde d'isolation : `MOISSON_NO_DOTENV=1` désactive tout chargement de `.env`.
# La suite de tests la pose (tests/conftest.py) avant d'importer le code du
# projet, pour qu'aucune clé du poste — en particulier la clé SerpAPI, seule
# source facturée — n'entre dans l'environnement des tests.
NO_DOTENV_ENV = "MOISSON_NO_DOTENV"


# === Clés d'API et réserve du .env ===

# Clé de registre de la source -> variable de sa clé.
SOURCE_KEY_VARS: Dict[str, str] = {
    "openalex": config.ENV_OPENALEX_KEY,
    "semantic_scholar": config.ENV_S2_KEY,
    "core": config.ENV_CORE_KEY,
    "google_scholar": config.ENV_SERPAPI_KEY,
}
API_KEY_VARS: Tuple[str, ...] = tuple(SOURCE_KEY_VARS.values())
_VAR_SOURCE = {var: source for source, var in SOURCE_KEY_VARS.items()}

# Sources dont la clé du client peut être remplacée par celle du .env
# (déclarée vide ou refusée). Lu à l'appel : réglable aussi dans le .env.
KEY_FALLBACK_ENV = "MOISSON_KEY_FALLBACK"
KEY_FALLBACK_DEFAULT = "openalex,semantic_scholar,core"

# Origine de la clé retenue (resolve_api_key).
ORIGIN_CLIENT = "client"
ORIGIN_ENV_FILE = "env_file"
ORIGIN_ENV_FILE_AFTER_REFUSAL = "env_file_after_refusal"

# Clés lues dans le .env (variable -> valeur), refus mémorisés (variable ->
# statut HTTP). La clé du client refusée n'est pas conservée ici : elle
# reste dans os.environ, d'où elle est relue.
_RESERVE: Dict[str, str] = {}
_REFUSED: Dict[str, int] = {}
_LOCK = threading.Lock()


def _project_env_file() -> Path:
    return Path(__file__).resolve().parent.parent / ".env"


def _default_env_files() -> list:
    """Le `.env` de la racine du projet, puis la recherche par défaut de
    dotenv (`.env` du répertoire courant d'une CLI lancée ailleurs, tests,
    conteneur). La racine d'abord : le serveur MCP est lancé par le client
    depuis un répertoire arbitraire, où `load_dotenv()` seul ignorait le
    `.env` du projet en silence."""
    paths = [_project_env_file()]
    try:  # pragma: no cover - dépend de l'environnement d'exécution
        from dotenv import find_dotenv
        found = find_dotenv()
    except Exception:  # noqa: BLE001 - recherche facultative
        found = ""
    if found:
        paths.append(Path(found))
    return paths


def _load_dotenv_files(paths: Optional[Iterable] = None) -> bool:
    """
    Lit les fichiers `.env` connus, sauf si ``MOISSON_NO_DOTENV=1``.

    Les quatre clés d'API (``API_KEY_VARS``) vont dans la réserve
    (``_RESERVE``, premier fichier qui la donne), jamais dans ``os.environ`` :
    une variable déclarée vide par le client ne masque donc plus la clé du
    `.env`, et une clé du client refusée peut être remplacée. Les autres
    variables sont posées dans ``os.environ`` sans écraser une variable déjà
    définie (comme ``load_dotenv``). Un fichier absent ou illisible (évincé
    du disque, droits refusés) est passé.

    Args:
        paths: fichiers à lire, dans l'ordre (défaut : ``_default_env_files``).

    Returns:
        True si une lecture a été tentée, False si elle a été désactivée par
        la garde ou si python-dotenv est absent.
    """
    if os.environ.get(NO_DOTENV_ENV, "").strip() == "1":
        logger.debug("%s=1 : aucun fichier .env chargé", NO_DOTENV_ENV)
        return False
    try:  # pragma: no cover - dépend de l'environnement d'exécution
        from dotenv import dotenv_values
    except ImportError:  # pragma: no cover
        return False

    seen = set()
    for path in (_default_env_files() if paths is None else paths):
        path = Path(path)
        try:
            key = path.resolve()
            if key in seen or not path.is_file():
                continue
            seen.add(key)
            values = dotenv_values(path)
        except Exception as exc:  # noqa: BLE001 - fichier illisible : passé
            logger.warning("Fichier .env illisible (%s) : %s",
                           type(exc).__name__, path)
            continue
        for name, value in (values or {}).items():
            if name in API_KEY_VARS:
                value = (value or "").strip()
                if value:
                    _RESERVE.setdefault(name, value)
            elif value is not None and name not in os.environ:
                os.environ[name] = value
    return True


_load_dotenv_files()


def fallback_allowed(env_var_name: str, policy: Optional[str] = None) -> bool:
    """La clé du client de cette variable peut-elle être remplacée par celle
    du `.env` (``MOISSON_KEY_FALLBACK``, sources séparées par des virgules
    ou des espaces ; vide : aucune) ? ``policy`` : valeur à évaluer à la
    place de celle de l'environnement (état d'une déclaration de client)."""
    source = _VAR_SOURCE.get(env_var_name)
    if source is None:
        return False
    raw = (policy if policy is not None
           else os.environ.get(KEY_FALLBACK_ENV, KEY_FALLBACK_DEFAULT))
    tokens = {t.strip().lower() for t in re.split(r"[,\s]+", raw or "") if t.strip()}
    return source in tokens


def reserve_value(env_var_name: str) -> Optional[str]:
    """Clé de réserve lue dans le `.env` (None : absente)."""
    return _RESERVE.get(env_var_name) or None


def refused_status(env_var_name: str) -> Optional[int]:
    """Statut du refus mémorisé de la clé du client (None : aucun)."""
    with _LOCK:
        return _REFUSED.get(env_var_name)


def reset_key_state() -> None:
    """Oublie les refus mémorisés (tests ; la réserve est conservée)."""
    with _LOCK:
        _REFUSED.clear()


def _client_value(env_var_name: str) -> Tuple[Optional[str], bool]:
    """``(valeur nettoyée ou "", déclarée ?)`` de la variable du client."""
    raw = os.environ.get(env_var_name)
    if raw is None:
        return "", False
    return raw.strip(), True


def resolve_api_key(env_var_name: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Clé à utiliser pour ``env_var_name`` et son origine.

    Returns:
        ``(valeur, origine)`` : ``ORIGIN_CLIENT`` (déclaration du client ou
        shell), ``ORIGIN_ENV_FILE`` (réserve : variable non déclarée, ou
        déclarée vide et repli permis), ``ORIGIN_ENV_FILE_AFTER_REFUSAL``
        (réserve à la place d'une clé du client refusée) ; ``(None, None)``
        si aucune clé n'est disponible.
    """
    client, declared = _client_value(env_var_name)
    reserve = reserve_value(env_var_name)
    if client:
        if (reserve and reserve != client and refused_status(env_var_name)
                and fallback_allowed(env_var_name)):
            return reserve, ORIGIN_ENV_FILE_AFTER_REFUSAL
        return client, ORIGIN_CLIENT
    if reserve and (not declared or fallback_allowed(env_var_name)):
        return reserve, ORIGIN_ENV_FILE
    return None, None


class MissingAPIKey(RuntimeError):
    """Clé API requise mais absente de l'environnement (``env_var`` : nom de
    la variable à définir)."""

    def __init__(self, message: str, env_var: Optional[str] = None):
        super().__init__(message)
        self.env_var = env_var


def get_api_key(env_var_name: str, required: bool = False) -> Optional[str]:
    """
    Retourne la clé à utiliser pour ``env_var_name`` (voir
    ``resolve_api_key`` : déclaration du client, sinon réserve du `.env`).

    Args:
        env_var_name: Nom de la variable (ex : "OPENALEX_API_KEY").
        required: Si vrai et aucune clé disponible, lève ``MissingAPIKey``
            avec un message indiquant comment la fournir.

    Returns:
        La valeur de la clé (chaîne non vide) ou ``None`` si absente et
        ``required`` est faux.

    Raises:
        MissingAPIKey: Si ``required`` est vrai et aucune clé n'est
            disponible.
    """
    value, _origin = resolve_api_key(env_var_name)
    if value:
        return value
    if required:
        if reserve_value(env_var_name):
            # Déclarée vide, réserve présente mais non permise (SerpAPI par
            # défaut) : le dire, sans jamais citer la valeur.
            raise MissingAPIKey(
                f"Clé API absente : {env_var_name} est déclarée vide par le "
                f"client, et la clé du .env ne la remplace pas pour cette "
                f"source (hors {KEY_FALLBACK_ENV}). Renseigner la clé dans la "
                f"déclaration du client.",
                env_var_name,
            )
        raise MissingAPIKey(
            f"Clé API absente : définir la variable d'environnement "
            f"{env_var_name} (dans .env, le shell, ou le bloc 'env' du "
            f"client MCP). Voir .env.example.",
            env_var_name,
        )
    return None


def missing_key_detail(env_var_name: str) -> str:
    """Détail court du statut « clé d'API absente » d'une source (sans
    valeur) : que renseigner, et où."""
    _client, declared = _client_value(env_var_name)
    if declared and reserve_value(env_var_name) and not fallback_allowed(env_var_name):
        return (f"{env_var_name} déclarée vide par le client ; la clé du .env "
                f"ne la remplace pas pour cette source (hors {KEY_FALLBACK_ENV})")
    return (f"définir {env_var_name} (déclaration du client MCP, shell ou "
            f".env)")


def reserve_note(env_var_name: str) -> Optional[str]:
    """Avertissement à joindre au statut d'une source dont la clé du client a
    été refusée et remplacée par la réserve du `.env` (None : aucun)."""
    status = refused_status(env_var_name)
    if status is None:
        return None
    if resolve_api_key(env_var_name)[1] != ORIGIN_ENV_FILE_AFTER_REFUSAL:
        return None
    return (f"clé du client ({env_var_name}) refusée (HTTP {status}) : clé de "
            f"réserve du .env utilisée")


def has_api_key(env_var_name: str) -> bool:
    """Indique si une clé non vide est disponible pour ``env_var_name``."""
    return bool(get_api_key(env_var_name, required=False))


def substitute_credential(credential: str, *, on_refusal: bool = False,
                          status: Optional[int] = None) -> Optional[Tuple[str, str]]:
    """
    Remplacement d'un identifiant de compte (paramètre ``api_key``, en-tête
    ``x-api-key`` ou ``Authorization: Bearer …``) qui porte la clé du client
    par la clé de réserve du `.env`.

    Args:
        credential: valeur envoyée (clé seule ou ``Bearer <clé>``).
        on_refusal: vrai juste après un refus 401 ou 403 de cette requête :
            le remplacement est accordé si le repli est permis et la réserve
            différente, et le refus est mémorisé. Faux : remplacement
            seulement pour une clé déjà refusée (requêtes suivantes).
        status: statut HTTP du refus (mémorisé).

    Returns:
        ``(variable, nouvelle valeur)`` ou None (aucun remplacement).
    """
    value = str(credential or "").strip()
    if not value:
        return None
    prefix = ""
    if value.lower().startswith("bearer "):
        prefix, value = value[:7], value[7:].strip()
    for var in API_KEY_VARS:
        client, _declared = _client_value(var)
        reserve = reserve_value(var)
        if not client or value != client or not reserve or reserve == client:
            continue
        if not fallback_allowed(var):
            return None
        with _LOCK:
            if var not in _REFUSED:
                if not on_refusal:
                    return None
                _REFUSED[var] = int(status or 0)
        return var, prefix + reserve
    return None
