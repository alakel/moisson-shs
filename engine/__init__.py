"""
Moteur commun de moisson (OPT-07).

Contrats internes (``engine.models``) : ``SearchRequest`` (ce qui est
demandé), ``RunContext`` (identité, échéance, annulation, budgets,
progression, client HTTP et registre des flux Isidore d'une exécution),
``SourceResult`` (notices et statut d'une source), ``HarvestResult`` (résultat
consolidé). Orchestration (``engine.orchestrator.harvest``) et écriture des
fichiers (``engine.outputs.write_outputs``) sont partagées par la CLI
(``main.py``) et le serveur MCP (``mcp_server._run_moisson_sync``), qui n'en
sont plus que des adaptateurs.

Ce module n'importe rien : ``engine.models`` est importé par les connecteurs
(``connectors.base``), sans dépendre du registre.
"""
