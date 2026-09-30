"""
Tests OPT-01 — épinglage du SDK MCP, contraintes de versions, marqueurs.

Garde-fous statiques (lecture des fichiers du dépôt, aucune installation) :
- `mcp>=1.27,<2` dans les extras `mcp`/`http`/`remote` et `requirements.txt` :
  sans borne haute, une installation neuve tirait mcp 2.x, dont le `Server`
  n'a plus les décorateurs utilisés par mcp_server.py ;
- `constraints.txt` épingle une version de mcp compatible avec cette borne et
  couvre toutes les dépendances directes ; le Dockerfile et la CI l'utilisent ;
- `httpx` déclaré dans l'extra [dev] (importé par les tests de transport) ;
- plus aucun usage du client déprécié `streamablehttp_client` ;
- marqueurs pytest identiques dans pytest.ini et pyproject.toml.

Aucun appel réseau, aucune socket.
"""

import configparser
import re
import sys
from pathlib import Path

import pytest

try:  # Python ≥ 3.11
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 (tomli vient avec pytest)
    tomllib = pytest.importorskip("tomli")

from packaging.requirements import Requirement
from packaging.version import Version

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

# Borne attendue pour le SDK MCP (série 1 uniquement).
MCP_LOWER = Version("1.27")
MCP_UPPER = Version("2")


def _pyproject() -> dict:
    return tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _requirement_lines(path: Path) -> list:
    """Lignes de dépendances d'un fichier pip (commentaires et vides exclus)."""
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def _constraints() -> dict:
    """{nom normalisé: Requirement} pour constraints.txt."""
    out = {}
    for line in _requirement_lines(PROJECT_ROOT / "constraints.txt"):
        req = Requirement(line)
        out[_norm(req.name)] = req
    return out


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pinned_version(req: Requirement) -> Version:
    specs = list(req.specifier)
    assert len(specs) == 1 and specs[0].operator == "==", str(req)
    return Version(specs[0].version)


# === Borne du SDK MCP ===

class TestMcpPin:
    @pytest.mark.parametrize("extra", ["mcp", "http", "remote"])
    def test_extras_bound_mcp_below_2(self, extra):
        deps = _pyproject()["project"]["optional-dependencies"][extra]
        mcp_reqs = [Requirement(d) for d in deps if _norm(Requirement(d).name) == "mcp"]
        assert len(mcp_reqs) == 1, deps
        spec = mcp_reqs[0].specifier
        assert MCP_LOWER in spec
        assert Version("1.99") in spec
        assert MCP_UPPER not in spec
        assert Version("2.2.0") not in spec
        assert Version("1.26") not in spec

    def test_requirements_txt_bounds_mcp(self):
        reqs = [Requirement(line) for line in _requirement_lines(PROJECT_ROOT / "requirements.txt")]
        mcp_reqs = [r for r in reqs if _norm(r.name) == "mcp"]
        assert len(mcp_reqs) == 1
        assert MCP_LOWER in mcp_reqs[0].specifier
        assert Version("2.0") not in mcp_reqs[0].specifier

    def test_installed_mcp_satisfies_pin(self):
        """Le SDK réellement importé par la suite respecte la borne."""
        from importlib.metadata import PackageNotFoundError, version

        try:
            installed = Version(version("mcp"))
        except PackageNotFoundError:  # pragma: no cover
            pytest.skip("mcp non installé")
        assert MCP_LOWER <= installed < MCP_UPPER

    def test_streamable_http_client_available(self):
        """Le client non déprécié existe dans la version installée."""
        pytest.importorskip("mcp")
        from mcp.client.streamable_http import streamable_http_client  # noqa: F401


# === constraints.txt ===

class TestConstraintsFile:
    def test_every_line_is_an_exact_pin(self):
        for name, req in _constraints().items():
            _pinned_version(req)  # lève si pas « == » unique

    def test_mcp_pinned_inside_supported_range(self):
        cons = _constraints()
        assert "mcp" in cons
        assert MCP_LOWER <= _pinned_version(cons["mcp"]) < MCP_UPPER

    def test_direct_dependencies_all_constrained(self):
        """Chaque dépendance directe (runtime + tous les extras) est figée."""
        project = _pyproject()["project"]
        direct = list(project["dependencies"])
        for deps in project["optional-dependencies"].values():
            direct.extend(deps)
        cons = _constraints()
        missing = sorted({_norm(Requirement(d).name) for d in direct} - set(cons))
        assert missing == []

    def test_pins_satisfy_declared_ranges(self):
        """Aucune contrainte ne contredit une borne déclarée dans pyproject."""
        project = _pyproject()["project"]
        direct = list(project["dependencies"])
        for deps in project["optional-dependencies"].values():
            direct.extend(deps)
        cons = _constraints()
        for dep in direct:
            req = Requirement(dep)
            pinned = _pinned_version(cons[_norm(req.name)])
            assert pinned in req.specifier, f"{req} vs {pinned}"

    def test_python310_backports_are_marker_guarded(self):
        cons = _constraints()
        for name in ("exceptiongroup", "tomli"):
            assert name in cons
            assert cons[name].marker is not None
            assert "python_version" in str(cons[name].marker)


# === Consommateurs de constraints.txt ===

class TestConstraintsConsumers:
    def test_dockerfile_builds_wheels_with_constraints(self):
        text = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
        # Commandes `pip wheel` (pas « pip install --upgrade pip wheel »).
        wheel_lines = [ln for ln in text.splitlines()
                       if re.match(r"\s*(RUN\s+|&&\s+)?pip wheel\s", ln)]
        assert wheel_lines, "aucune ligne `pip wheel` dans le Dockerfile"
        assert all("-c constraints.txt" in ln for ln in wheel_lines)

    def test_ci_installs_with_constraints(self):
        text = (PROJECT_ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
        installs = [ln for ln in text.splitlines()
                    if re.search(r"\bpip install\b.*-e\b", ln)]
        assert installs
        assert all("-c constraints.txt" in ln for ln in installs)

    def test_ci_runs_the_suite_once_with_coverage_threshold(self):
        text = (PROJECT_ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
        suite_runs = [ln for ln in text.splitlines()
                      if re.search(r"\bpytest tests/ ", ln) and "not integration" in ln]
        assert len(suite_runs) == 1, suite_runs
        assert "--cov-fail-under=" in suite_runs[0]
        # Les tests `sockets` s'exécutent en CI : pas d'exclusion du marqueur.
        assert "not sockets" not in suite_runs[0]

    def test_constraints_not_excluded_from_docker_context(self):
        ignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        assert "constraints.txt" not in [ln.strip() for ln in ignore]


# === Dépendances de test et client MCP ===

class TestTestDependencies:
    def test_httpx_in_dev_extra(self):
        dev = _pyproject()["project"]["optional-dependencies"]["dev"]
        assert "httpx" in {_norm(Requirement(d).name) for d in dev}

    def test_no_deprecated_streamablehttp_client_left(self):
        offenders = []
        for base in ("tests", "scripts"):
            for path in (PROJECT_ROOT / base).rglob("*.py"):
                if path.name == Path(__file__).name:
                    continue
                if "streamablehttp_client" in path.read_text(encoding="utf-8"):
                    offenders.append(str(path.relative_to(PROJECT_ROOT)))
        for path in PROJECT_ROOT.glob("*.py"):
            if "streamablehttp_client" in path.read_text(encoding="utf-8"):
                offenders.append(path.name)
        assert offenders == []


# === Marqueurs pytest ===

class TestMarkers:
    @staticmethod
    def _names(lines) -> set:
        return {ln.split(":", 1)[0].strip() for ln in lines if ln.strip()}

    def test_markers_identical_in_pytest_ini_and_pyproject(self):
        ini = configparser.ConfigParser()
        ini.read(PROJECT_ROOT / "pytest.ini", encoding="utf-8")
        ini_lines = ini["pytest"]["markers"].splitlines()
        toml_lines = _pyproject()["tool"]["pytest"]["ini_options"]["markers"]
        assert self._names(ini_lines) == self._names(toml_lines)
        assert {"integration", "sockets"} <= self._names(ini_lines)
