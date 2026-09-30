"""
Registre des instances du serveur MCP qui écrivent dans un même dossier de
sortie (PAT-10, retour d'expérience du 2026-09-30).

Constat : le 30 septembre, une instance lancée le 28 (code d'avant les
modifications du 29) tournait encore à côté de l'instance à jour, et Claude
Desktop lance d'ordinaire deux instances à la fois. Deux instances du même
code publient sans perte sur un disque local (verrou de publication) ; une
instance d'un code **différent** peut ne pas participer au verrou, ni
appliquer les mêmes règles.

Chaque instance écrit au démarrage ``<OUTPUT_DIR>/.instances/<pid>.json``
(version, commit, empreinte du code, transport, heure de démarrage, hôte) et
le retire à l'arrêt. :func:`stale_instances` rend les autres instances
vivantes (même hôte, PID vivant) dont le code diffère : le serveur les signale
au démarrage et ``corpus_list`` les rappelle, sans rien refuser (décision
DP-10). Un fichier de service (nom commençant par un point) n'est jamais
listé, lu ni téléchargé.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

INSTANCES_DIR = ".instances"

_registered: Dict[str, Path] = {}


def _pid_alive(pid: int) -> Optional[bool]:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def register(output_dir: Path, transport: str) -> Optional[Path]:
    """Inscrit l'instance courante (idempotent) ; ``None`` si le dossier
    n'est pas inscriptible."""
    from utils.build_info import build_info

    key = str(Path(output_dir).resolve())
    if key in _registered:
        return _registered[key]
    info = build_info()
    doc = {
        "pid": os.getpid(), "host": socket.gethostname(),
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "transport": transport, "version": info.get("version"),
        "commit": info.get("commit"), "dirty": info.get("dirty"),
        "code_sha256": info.get("code_sha256"),
    }
    directory = Path(output_dir) / INSTANCES_DIR
    path = directory / f"{os.getpid()}.json"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / f".{os.getpid()}.json.tmp"
        tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.debug(f"Registre des instances non écrit : {exc}")
        return None
    _registered[key] = path
    atexit.register(_unregister, path)
    return path


def _unregister(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def list_instances(output_dir: Path, *, prune: bool = True) -> List[Dict[str, Any]]:
    """Instances inscrites et vivantes du même hôte ; les inscriptions d'un
    processus mort de cet hôte sont retirées (``prune``)."""
    directory = Path(output_dir) / INSTANCES_DIR
    host = socket.gethostname()
    out = []
    try:
        entries = sorted(directory.glob("*.json"))
    except OSError:
        return []
    for path in entries:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pid = doc.get("pid") if isinstance(doc, dict) else None
        if not isinstance(pid, int):
            continue
        if doc.get("host") == host:
            alive = _pid_alive(pid)
            if alive is False:
                if prune:
                    _unregister(path)
                continue
        out.append(doc)
    return out


def stale_instances(output_dir: Path) -> List[Dict[str, Any]]:
    """Autres instances vivantes dont l'empreinte du code diffère de celle
    de l'instance courante."""
    from utils.build_info import build_info

    mine = build_info().get("code_sha256")
    return [d for d in list_instances(output_dir)
            if d.get("pid") != os.getpid() and d.get("code_sha256")
            and mine and d.get("code_sha256") != mine]


def stale_warnings(output_dir: Path) -> List[str]:
    """Avertissements lisibles pour les instances périmées."""
    lines = []
    for d in stale_instances(output_dir):
        commit = (d.get("commit") or "")[:7] or "inconnu"
        lines.append(
            f"instance au code différent : PID {d.get('pid')}, lancée le "
            f"{d.get('started_at')}, version {d.get('version')} (commit "
            f"{commit}) ; quitter complètement Claude Desktop puis le relancer")
    return lines
