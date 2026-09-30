"""
Chargement centralisé des clés API des sources internationales et de
Google Scholar.

Les clés sont lues depuis les variables d'environnement. Si python-dotenv
est installé, le fichier `.env` de la racine du projet (ignoré par git ;
chemin calculé depuis ce module, non depuis le répertoire courant), puis un
éventuel `.env` du répertoire courant, sont chargés au premier import,
sauf si ``MOISSON_NO_DOTENV=1`` (garde posée par la suite de tests). Aucun
des deux n'écrase une variable déjà définie. On ne committe JAMAIS de clé ;
voir `.env.example` pour les variables attendues.

Sources concernées :
- OPENALEX_API_KEY      — requis (clé gratuite, openalex.org/settings/api)
- SEMANTIC_SCHOLAR_API_KEY — optionnel (sans clé : pool partagé throttlé)
- CORE_API_KEY          — requis (sur inscription, core.ac.uk/services/api)
- SERPAPI_API_KEY       — requis pour Google Scholar (compte serpapi.com,
  facturé à la requête)

Le MCP transmet ces clés via le bloc `env` de `claude_desktop_config.json`
(par utilisateur). Pour le CLI, elles viennent de `.env` ou du shell.

Limite connue : ce module est importé après ``config.py``. Les réglages
``MOISSON_*`` que ``config.py`` lit à l'import ne sont donc pas pris dans un
`.env` local ; seules les clés (lues à l'appel) le sont à coup sûr. Passer
ces réglages par le shell, le bloc `env` du client ou l'``env_file`` de
Docker Compose (voir `.env.example`).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Garde d'isolation : `MOISSON_NO_DOTENV=1` désactive tout chargement de `.env`.
# La suite de tests la pose (tests/conftest.py) avant d'importer le code du
# projet, pour qu'aucune clé du poste — en particulier la clé SerpAPI, seule
# source facturée — n'entre dans l'environnement des tests.
NO_DOTENV_ENV = "MOISSON_NO_DOTENV"


def _load_dotenv_files() -> bool:
    """
    Charge les fichiers `.env` connus, sauf si ``MOISSON_NO_DOTENV=1``.

    Chargement optionnel (sans dépendance dure : python-dotenv est un extra).
    En son absence, on se rabat sur les variables d'environnement du shell /
    du bloc `env` MCP. Le chargement n'écrase pas une variable déjà définie
    dans l'environnement (override=False est le défaut de dotenv).

    Returns:
        True si un chargement a été tenté, False s'il a été désactivé par la
        garde ou si python-dotenv est absent.
    """
    if os.environ.get(NO_DOTENV_ENV, "").strip() == "1":
        logger.debug("%s=1 : aucun fichier .env chargé", NO_DOTENV_ENV)
        return False
    try:  # pragma: no cover - dépend de l'environnement d'exécution
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return False

    # Deux chargements, dans cet ordre, aucun n'écrasant une variable déjà
    # définie (override=False) :
    #
    # 1. le `.env` de la racine du projet, résolu depuis l'emplacement de CE
    #    fichier et non depuis le répertoire courant. C'est le point important :
    #    `load_dotenv()` seul s'appuie sur le CWD, or le serveur MCP est lancé
    #    par Claude Desktop depuis un répertoire arbitraire — le `.env` était
    #    alors ignoré en silence, et la source concernée désactivée sans que
    #    rien ne l'explique ;
    # 2. la recherche par défaut, conservée pour un `.env` local au CWD (CLI
    #    lancée depuis un autre projet, tests, conteneur).
    #
    # L'environnement réel reste prioritaire sur les deux : le bloc `env` de
    # `claude_desktop_config.json` continue de faire autorité.
    project_env = Path(__file__).resolve().parent.parent / ".env"
    if project_env.is_file():
        load_dotenv(project_env)
    load_dotenv()
    return True


_load_dotenv_files()


class MissingAPIKey(RuntimeError):
    """Clé API requise mais absente de l'environnement (``env_var`` : nom de
    la variable à définir)."""

    def __init__(self, message: str, env_var: Optional[str] = None):
        super().__init__(message)
        self.env_var = env_var


def get_api_key(env_var_name: str, required: bool = False) -> Optional[str]:
    """
    Retourne la valeur de la variable d'environnement ``env_var_name``.

    Args:
        env_var_name: Nom de la variable (ex : "OPENALEX_API_KEY").
        required: Si vrai et la variable absente/vide, lève ``MissingAPIKey``
            avec un message indiquant comment la fournir.

    Returns:
        La valeur de la clé (chaîne non vide) ou ``None`` si absente et
        ``required`` est faux.

    Raises:
        MissingAPIKey: Si ``required`` est vrai et la clé est absente/vide.
    """
    value = os.environ.get(env_var_name)
    if value:
        value = value.strip()
    if not value:
        if required:
            raise MissingAPIKey(
                f"Clé API absente : définir la variable d'environnement "
                f"{env_var_name} (dans .env, le shell, ou le bloc 'env' de "
                f"claude_desktop_config.json). Voir .env.example.",
                env_var_name,
            )
        return None
    return value


def has_api_key(env_var_name: str) -> bool:
    """Indique si une clé non vide est disponible pour ``env_var_name``."""
    return bool(get_api_key(env_var_name, required=False))
