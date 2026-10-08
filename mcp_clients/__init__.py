"""
Clients MCP pris en charge par ``moisson-shs setup`` (sprint « universel »).

Un adaptateur par client : Claude Desktop, Gemini CLI et Antigravity
(fichier JSON ``mcpServers``), Claude Code (CLI ``claude``), Codex (TOML),
Hermes Agent (YAML). Le paquet ne dépend jamais de ``setup_claude_desktop``,
qui en est le point d'entrée (menu, clés, rendu des messages).
"""
