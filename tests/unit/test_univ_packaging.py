"""
Sprint « universel », lot 9 : le paquet ``mcp_clients`` est distribué et ses
deux bibliothèques d'écriture (tomlkit, ruamel.yaml) sont déclarées,
bornées et épinglées.
"""

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 (matrice CI)
    import tomli as tomllib

from utils import build_info

ROOT = Path(__file__).resolve().parents[2]


def _pyproject():
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_package_is_distributed():
    include = _pyproject()["tool"]["setuptools"]["packages"]["find"]["include"]
    assert "mcp_clients*" in include
    assert "mcp_clients" in build_info.PACKAGED_PACKAGES


def test_writers_in_mcp_extra():
    extra = _pyproject()["project"]["optional-dependencies"]["mcp"]
    names = {d.split(">")[0].split("=")[0].strip() for d in extra}
    assert {"tomlkit", "ruamel.yaml"} <= names


def test_writers_pinned_and_listed():
    constraints = (ROOT / "constraints.txt").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    for name in ("tomlkit", "ruamel.yaml"):
        assert f"\n{name}==" in constraints
        assert f"\n{name}>=" in requirements
