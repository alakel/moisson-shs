"""
OPT-04 (revue) : les tests du chargeur de schéma Zotero ne dépendent pas des
réglages du shell.

MOISSON_SCHEMA_RETRY_AFTER et MOISSON_SCHEMA_CACHE_AGE sont lus à l'import de
mappers/zotero_schema_loader.py. Avant la correction, un
MOISSON_SCHEMA_RETRY_AFTER=0 hérité du shell faisait échouer huit tests du
chargeur, et l'un d'eux téléchargeait réellement le schéma depuis
api.zotero.org. Les fixtures automatiques des deux fichiers de tests du
chargeur fixent désormais ces réglages.

Ce test relance ces deux fichiers dans un processus où les deux variables
valent 0, sous une garde qui bloque et consigne toute sortie réseau
(résolution DNS comprise). Il exige qu'ils passent sans aucune tentative
réseau.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

LOADER_TESTS = (
    "tests/unit/test_opt04_schema_loader.py",
    "tests/unit/test_zotero_schema_loader.py",
)

# Greffon pytest chargé dans le sous-processus : toute sortie réseau est
# refusée et consignée dans le fichier désigné par OPT04_GARDE_LOG
GUARD_PLUGIN = textwrap.dedent('''
    """Garde réseau : refuse et consigne toute sortie (DNS compris)."""
    import os
    import socket

    _LOG = os.environ["OPT04_GARDE_LOG"]

    def _block(kind):
        def _refuse(*args, **kwargs):
            with open(_LOG, "a", encoding="utf-8") as fh:
                fh.write(f"{kind} {args[:2]!r}\\n")
            raise OSError(f"réseau bloqué par la garde ({kind})")
        return _refuse

    socket.getaddrinfo = _block("getaddrinfo")
    socket.create_connection = _block("create_connection")
    _connect = socket.socket.connect

    def _guarded_connect(self, addr):
        if isinstance(addr, str):   # socket Unix : sans objet ici
            return _connect(self, addr)
        return _block("connect")(addr)

    socket.socket.connect = _guarded_connect
''')


def test_tests_du_chargeur_independants_des_reglages_du_shell(tmp_path):
    plugin_dir = tmp_path / "greffon"
    plugin_dir.mkdir()
    (plugin_dir / "opt04_garde_reseau.py").write_text(
        GUARD_PLUGIN, encoding="utf-8")
    log = tmp_path / "sorties_reseau.log"

    env = dict(os.environ)
    env.update({
        "MOISSON_SCHEMA_RETRY_AFTER": "0",
        "MOISSON_SCHEMA_CACHE_AGE": "0",
        # Cache disque par défaut hors du poste, au cas où un test l'oublierait
        "MOISSON_SCHEMA_CACHE": str(tmp_path / "absent" / "schema.json"),
        "OPT04_GARDE_LOG": str(log),
        "PYTHONPATH": str(plugin_dir),
    })
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-p", "opt04_garde_reseau", "-m", "not integration", *LOADER_TESTS],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output[-3000:]
    attempts = log.read_text(encoding="utf-8") if log.exists() else ""
    assert attempts == "", f"sorties réseau tentées :\n{attempts}"
