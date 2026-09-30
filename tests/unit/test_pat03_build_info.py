"""
PAT-03 : source unique de version et identité du code (commit, état de
l'arbre, empreinte des modules) dans la CLI, le serveur MCP, les manifestes
et ``_stats.txt``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    tomllib = None

ROOT = Path(__file__).resolve().parents[2]

from _version import __version__  # noqa: E402
from utils import build_info as BI  # noqa: E402

SHA = "e61c2b9b3231a141ea63bad2218627c9138f7e5a"
SHA2 = "3ff57e14c01019eb5bad1cdca1974fb1c109bf25"


@pytest.fixture
def fresh_build_info(monkeypatch):
    """Vide les caches (identité du code et versions du manifeste) avant et
    après le test, pour que l'environnement du test soit relu."""
    from utils import manifest as M
    monkeypatch.delenv(BI.BUILD_COMMIT_ENV, raising=False)
    BI.build_info.cache_clear()
    M.software_versions.cache_clear()
    yield
    BI.build_info.cache_clear()
    M.software_versions.cache_clear()


def _pyproject() -> dict:
    if tomllib is None:  # pragma: no cover
        pytest.skip("tomllib indisponible")
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


# === Source unique de version ===

class TestSingleVersionSource:
    def test_pyproject_reads_version_from_version_module(self):
        data = _pyproject()
        assert "version" not in data["project"]
        assert "version" in data["project"]["dynamic"]
        assert data["tool"]["setuptools"]["dynamic"]["version"] == {
            "attr": "_version.__version__"}

    def test_packaged_modules_match_pyproject(self):
        data = _pyproject()
        assert tuple(data["tool"]["setuptools"]["py-modules"]) == \
            BI.PACKAGED_MODULES
        include = data["tool"]["setuptools"]["packages"]["find"]["include"]
        assert tuple(p.rstrip("*") for p in include) == BI.PACKAGED_PACKAGES

    def test_server_info_announces_package_version(self):
        import mcp_server
        options = mcp_server.server.create_initialization_options()
        assert options.server_version == __version__

    def test_healthz_version_is_package_version(self):
        import mcp_server
        assert mcp_server._get_version_string() == __version__

    def test_cli_version_names_version_and_commit(self, capsys,
                                                  fresh_build_info):
        import cli
        assert cli.main(["version"]) == 0
        out = capsys.readouterr().out
        assert out.startswith(f"moisson-shs {__version__} (")
        assert "commit" in out


# === Commit lu dans .git ===

def _fake_repo(root: Path, head: str, loose=None, packed=None) -> Path:
    git = root / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text(head + "\n", encoding="utf-8")
    for ref, sha in (loose or {}).items():
        path = git / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sha + "\n", encoding="utf-8")
    if packed:
        lines = ["# pack-refs with: peeled fully-peeled sorted"]
        lines += [f"{sha} {ref}" for ref, sha in packed.items()]
        (git / "packed-refs").write_text("\n".join(lines) + "\n",
                                         encoding="utf-8")
    return git


class TestGitCommit:
    def test_loose_ref(self, tmp_path):
        _fake_repo(tmp_path, "ref: refs/heads/main",
                   loose={"refs/heads/main": SHA})
        assert BI.git_commit(tmp_path) == SHA

    def test_packed_ref(self, tmp_path):
        _fake_repo(tmp_path, "ref: refs/heads/main",
                   packed={"refs/heads/other": SHA2, "refs/heads/main": SHA})
        assert BI.git_commit(tmp_path) == SHA

    def test_detached_head(self, tmp_path):
        _fake_repo(tmp_path, SHA)
        assert BI.git_commit(tmp_path) == SHA

    def test_worktree_file(self, tmp_path):
        main = tmp_path / "main"
        main.mkdir()
        common = _fake_repo(main, "ref: refs/heads/main",
                            loose={"refs/heads/feature": SHA2})
        wt_git = common / "worktrees" / "wt"
        wt_git.mkdir(parents=True)
        (wt_git / "HEAD").write_text("ref: refs/heads/feature\n",
                                     encoding="utf-8")
        (wt_git / "commondir").write_text("../..\n", encoding="utf-8")
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {wt_git}\n", encoding="utf-8")
        assert BI.git_commit(wt) == SHA2

    def test_no_git_directory(self, tmp_path):
        assert BI.git_commit(tmp_path) is None
        assert BI.git_dirty(tmp_path) is None

    def test_unknown_ref(self, tmp_path):
        _fake_repo(tmp_path, "ref: refs/heads/absent")
        assert BI.git_commit(tmp_path) is None

    def test_dirty_unknown_without_git_binary(self, tmp_path, monkeypatch):
        _fake_repo(tmp_path, SHA)
        monkeypatch.setattr(BI.shutil, "which", lambda _name: None)
        assert BI.git_dirty(tmp_path) is None


class TestBuildCommitEnv:
    def test_env_commit_wins_over_git(self, monkeypatch, fresh_build_info):
        monkeypatch.setenv(BI.BUILD_COMMIT_ENV, SHA2.upper())
        info = BI.build_info()
        assert info["commit"] == SHA2
        assert info["commit_source"] == "env"
        assert info["dirty"] is None

    def test_invalid_env_commit_is_ignored(self, monkeypatch,
                                           fresh_build_info):
        monkeypatch.setenv(BI.BUILD_COMMIT_ENV, "pas-un-commit")
        info = BI.build_info()
        assert info["commit_source"] != "env"


# === Empreinte du code ===

class TestCodeFingerprint:
    def _tree(self, root: Path) -> None:
        (root / "utils").mkdir()
        (root / "cli.py").write_text("x = 1\n", encoding="utf-8")
        (root / "utils" / "__init__.py").write_text("", encoding="utf-8")
        (root / "utils" / "a.py").write_text("y = 2\n", encoding="utf-8")

    def test_changes_with_content_only(self, tmp_path):
        self._tree(tmp_path)
        first = BI.code_sha256(tmp_path)
        assert first and len(first) == 64
        cache = tmp_path / "utils" / "__pycache__"
        cache.mkdir()
        (cache / "a.cpython-314.pyc.py").write_text("z", encoding="utf-8")
        (tmp_path / "notes.py").write_text("hors paquet", encoding="utf-8")
        assert BI.code_sha256(tmp_path) == first
        (tmp_path / "utils" / "a.py").write_text("y = 3\n", encoding="utf-8")
        assert BI.code_sha256(tmp_path) != first

    def test_empty_tree(self, tmp_path):
        assert BI.code_sha256(tmp_path) is None

    def test_repository_fingerprint_is_stable(self):
        assert BI.code_sha256() == BI.code_sha256()


# === Manifeste et statistiques ===

class TestOutputsCarryIdentity:
    def test_manifest_versions(self, fresh_build_info):
        from utils.manifest import software_versions
        versions = software_versions()
        assert versions["package"] == __version__
        for key in ("commit", "dirty", "code_sha256"):
            assert key in versions
        assert versions["code_sha256"] == BI.code_sha256()

    def test_manifest_versions_with_env_commit(self, monkeypatch,
                                                fresh_build_info):
        from utils.manifest import software_versions
        monkeypatch.setenv(BI.BUILD_COMMIT_ENV, SHA)
        assert software_versions()["commit"] == SHA

    def test_stats_text_has_version_line(self, fresh_build_info):
        from main import compute_stats, stats_text
        text = stats_text(compute_stats([]), "q")
        assert f"Version : moisson-shs {__version__} (" in text

    def test_describe(self):
        info = {"version": "9.9", "commit": SHA, "dirty": True,
                "code_sha256": "a" * 64}
        assert BI.describe(info) == \
            "9.9 (commit e61c2b9, arbre modifié, code aaaaaaaaaaaa)"
        assert BI.describe({"version": "9.9", "commit": None,
                            "dirty": None, "code_sha256": None}) == \
            "9.9 (commit inconnu)"
