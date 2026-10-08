"""
Une application cliente est-elle ouverte ?

Claude Desktop, l'application ChatGPT/Codex et la passerelle Hermes
réécrivent leur fichier de configuration pendant qu'elles tournent : setup
le vérifie avant d'écrire. ``None`` quand l'état ne peut pas être connu
(outil absent ou refusé, par exemple dans un bac à sable).
"""

from __future__ import annotations

import platform
import subprocess
from typing import Callable, Iterable, Optional


def is_running(names: Iterable[str], runner: Optional[Callable] = None,
               system: Optional[str] = None) -> Optional[bool]:
    """True si l'un des processus nommés tourne, False sinon, None si
    l'état est inconnu."""
    names = [n for n in (names or ()) if n]
    if not names:
        return False
    run = runner or subprocess.run
    system = system or platform.system()
    try:
        if system == "Windows":
            done = run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                       text=True, timeout=10)
            if done.returncode != 0:
                return None
            listing = (done.stdout or "").lower()
            return any(f'"{n.lower()}.exe"' in listing or f'"{n.lower()}"' in listing
                       for n in names)
        for name in names:
            done = run(["pgrep", "-x", name], capture_output=True, text=True,
                       timeout=10)
            if done.returncode == 0:
                return True
            if done.returncode != 1:
                return None
        return False
    except (OSError, subprocess.SubprocessError):
        return None
