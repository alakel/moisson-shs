"""
Chargement centralisé des clés API des sources internationales.

Les clés sont lues depuis les variables d'environnement. Si python-dotenv
est installé, un fichier `.env` (à la racine du projet, ignoré par git) est
chargé automatiquement au premier import. On ne committe JAMAIS de clé ;
voir `.env.example` pour les variables attendues.

Sources concernées :
- OPENALEX_API_KEY      — requis (clé gratuite, openalex.org/settings/api)
- SEMANTIC_SCHOLAR_API_KEY — optionnel (sans clé : pool partagé throttlé)
- CORE_API_KEY          — requis (sur inscription, core.ac.uk/services/api)

Le MCP transmet ces clés via le bloc `env` de `claude_desktop_config.json`
(par utilisateur). Pour le CLI, elles viennent de `.env` ou du shell.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Chargement optionnel du .env (sans dépendance dure : python-dotenv est un
# extra). En son absence, on se rabat sur les variables d'environnement du
# shell / du bloc `env` MCP. Le chargement n'écrase pas une variable déjà
# définie dans l'environnement (override=False est le défaut de dotenv).
try:  # pragma: no cover - dépend de l'environnement d'exécution
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


class MissingAPIKey(RuntimeError):
    """Clé API requise mais absente de l'environnement."""


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
                f"claude_desktop_config.json). Voir .env.example."
            )
        return None
    return value


def has_api_key(env_var_name: str) -> bool:
    """Indique si une clé non vide est disponible pour ``env_var_name``."""
    return bool(get_api_key(env_var_name, required=False))
