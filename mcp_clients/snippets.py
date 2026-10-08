"""
Extraits à coller (JSON, TOML, YAML), produits sans bibliothèque externe.

Utilisés quand setup ne peut pas écrire lui-même (bibliothèque absente,
fichier avec commentaires, mise en forme non reproductible) : les clés
renseignées y sont remplacées par des marqueurs ``<NOM_DE_LA_CLÉ>``, jamais
par leur valeur.
"""

from __future__ import annotations

import json
import re

from mcp_clients.common import API_KEY_ENV_VARS, SERVER_KEY

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def masked_entry(entry: dict) -> dict:
    """Copie de l'entrée où chaque clé d'API renseignée devient un marqueur."""
    out = dict(entry)
    env = out.get("env")
    if isinstance(env, dict):
        env = dict(env)
        for var in API_KEY_ENV_VARS:
            if env.get(var):
                env[var] = f"<{var}>"
        out["env"] = env
    return out


def json_snippet(servers_key: str, entry: dict) -> str:
    return json.dumps({servers_key: {SERVER_KEY: masked_entry(entry)}},
                      indent=2, ensure_ascii=False)


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.match(key) else json.dumps(key, ensure_ascii=False)


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    # Les échappements d'une chaîne JSON sont ceux d'une chaîne de base TOML.
    return json.dumps(str(value), ensure_ascii=False)


def toml_snippet(entry: dict, table: str = "mcp_servers") -> str:
    entry = masked_entry(entry)
    head = f"[{table}.{_toml_key(SERVER_KEY)}]"
    lines = [head]
    for key, value in entry.items():
        if key != "env" and not isinstance(value, dict):
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    env = entry.get("env")
    if isinstance(env, dict):
        lines += ["", f"[{table}.{_toml_key(SERVER_KEY)}.env]"]
        lines += [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in env.items()]
    return "\n".join(lines) + "\n"


def _yaml_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    # Une chaîne JSON est un scalaire YAML entre guillemets doubles valide.
    return json.dumps(str(value), ensure_ascii=False)


def yaml_snippet(entry: dict, top: str = "mcp_servers") -> str:
    entry = masked_entry(entry)
    lines = [f"{top}:", f"  {SERVER_KEY}:"]
    for key, value in entry.items():
        if isinstance(value, dict):
            lines.append(f"    {key}:")
            lines += [f"      {k}: {_yaml_scalar(v)}" for k, v in value.items()]
        elif isinstance(value, (list, tuple)):
            lines.append(f"    {key}:")
            lines += [f"      - {_yaml_scalar(v)}" for v in value]
        else:
            lines.append(f"    {key}: {_yaml_scalar(value)}")
    return "\n".join(lines) + "\n"
