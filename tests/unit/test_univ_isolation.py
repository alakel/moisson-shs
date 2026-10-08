"""
Sprint « universel », lot 0 : la fixture ``fake_home`` isole les tests du
setup des vraies configurations du poste (Claude, Codex, Gemini, Hermes) et
de toute CLI réelle.
"""

import os
import shutil
from pathlib import Path


def test_home_points_to_tmp(fake_home):
    assert Path.home() == fake_home
    assert os.path.expanduser("~") == str(fake_home)


def test_client_homes_are_cleared(fake_home):
    for var in ("HERMES_HOME", "CODEX_HOME", "XDG_CONFIG_HOME", "APPDATA",
                "LOCALAPPDATA"):
        assert var not in os.environ


def test_no_real_cli_reachable(fake_home):
    for name in ("claude", "codex", "gemini", "hermes", "agy", "pgrep"):
        assert shutil.which(name) is None
