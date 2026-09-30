"""
Version du paquet moisson-shs : source unique (PAT-03).

Lue par ``pyproject.toml`` (``[tool.setuptools.dynamic]``), par la CLI
(``moisson-shs version``), par le serveur MCP (``serverInfo.version``,
``/healthz``) et par les manifestes. Importée directement plutôt que lue
dans les métadonnées d'installation, qui peuvent retarder sur le code : un
serveur lancé par ``python mcp_server.py`` lit l'``egg-info`` de la racine,
la CLI la ``dist-info`` du venv, et les deux ne changent qu'à la
réinstallation.
"""

__version__ = "0.4.0"
