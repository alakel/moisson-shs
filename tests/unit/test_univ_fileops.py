"""
Sprint « universel », lot 1 : opérations sur les fichiers de configuration
des clients MCP (écriture atomique, droits, sauvegardes horodatées) et
fonctions communes (clés, masquage, mise en commun).
"""

import os
import stat
from datetime import datetime

import pytest

from mcp_clients import common, fileops


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


# === Écriture atomique ===

class TestAtomicWrite:
    def test_new_file_is_private_and_parent_created(self, tmp_path):
        path = tmp_path / "a" / "b" / "config.json"
        fileops.atomic_write(path, "{}\n", expected_fp=None)
        assert path.read_text(encoding="utf-8") == "{}\n"
        assert _mode(path) == 0o600

    def test_existing_mode_is_kept(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text("x", encoding="utf-8")
        os.chmod(path, 0o640)
        _text, fp = fileops.read_text(path)
        tightened = fileops.atomic_write(path, "y", expected_fp=fp)
        assert path.read_text(encoding="utf-8") == "y"
        assert _mode(path) == 0o640 and tightened is False

    def test_world_readable_file_with_secrets_becomes_private(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text("x", encoding="utf-8")
        os.chmod(path, 0o644)
        _text, fp = fileops.read_text(path)
        assert fileops.atomic_write(path, "y", expected_fp=fp, private=True)
        assert _mode(path) == 0o600

    def test_symlink_target_is_written(self, tmp_path):
        real = tmp_path / "real.json"
        real.write_text("x", encoding="utf-8")
        link = tmp_path / "link.json"
        link.symlink_to(real)
        _text, fp = fileops.read_text(link)
        fileops.atomic_write(link, "y", expected_fp=fp)
        assert link.is_symlink() and real.read_text(encoding="utf-8") == "y"

    def test_concurrent_change_is_refused(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text("x", encoding="utf-8")
        _text, fp = fileops.read_text(path)
        path.write_text("modifié par le client", encoding="utf-8")
        with pytest.raises(fileops.ConcurrentModification):
            fileops.atomic_write(path, "y", expected_fp=fp)
        assert path.read_text(encoding="utf-8") == "modifié par le client"

    def test_file_created_meanwhile_is_refused(self, tmp_path):
        path = tmp_path / "c.json"
        _text, fp = fileops.read_text(path)
        assert fp is None
        path.write_text("créé entre-temps", encoding="utf-8")
        with pytest.raises(fileops.ConcurrentModification):
            fileops.atomic_write(path, "y", expected_fp=fp)

    def test_failure_leaves_original_and_no_temp(self, tmp_path, monkeypatch):
        path = tmp_path / "c.json"
        path.write_text("original", encoding="utf-8")
        _text, fp = fileops.read_text(path)

        def boom(*a, **k):
            raise OSError("disque plein")
        monkeypatch.setattr(fileops.os, "replace", boom)
        with pytest.raises(OSError):
            fileops.atomic_write(path, "nouveau", expected_fp=fp)
        assert path.read_text(encoding="utf-8") == "original"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["c.json"]

    def test_read_absent(self, tmp_path):
        assert fileops.read_text(tmp_path / "absent") == (None, None)


# === Sauvegardes ===

class TestBackups:
    def test_name_and_private_copy(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text("x", encoding="utf-8")
        os.chmod(path, 0o644)
        backup = fileops.backup_file(path, now=datetime(2026, 10, 8, 19, 5, 3))
        assert backup.name == "settings.json.moisson-bak-20261008-190503"
        assert backup.read_text(encoding="utf-8") == "x"
        assert _mode(backup) == 0o600

    def test_same_second_collision(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text("x", encoding="utf-8")
        now = datetime(2026, 10, 8, 19, 5, 3)
        first = fileops.backup_file(path, now=now)
        second = fileops.backup_file(path, now=now)
        assert first != second and second.name.endswith("-2")

    def test_absent_file_has_no_backup(self, tmp_path):
        assert fileops.backup_file(tmp_path / "absent.json") is None

    def test_retention_spares_foreign_backups(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text("x", encoding="utf-8")
        foreign = [tmp_path / "config.toml.bak-2026-10-06-zotero",
                   tmp_path / "config.toml.bak"]
        for f in foreign:
            f.write_text("ancien", encoding="utf-8")
        for minute in range(8):
            fileops.backup_file(path, now=datetime(2026, 10, 8, 19, minute, 0))
        removed = fileops.prune_backups(path, keep=5)
        kept = sorted(p.name for p in tmp_path.glob("config.toml.moisson-bak-*"))
        assert len(removed) == 3 and len(kept) == 5
        assert kept[0].endswith("-190300") and kept[-1].endswith("-190700")
        assert all(f.exists() for f in foreign)

    def test_world_readable(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text("x", encoding="utf-8")
        os.chmod(path, 0o644)
        assert fileops.is_world_readable(path)
        os.chmod(path, 0o600)
        assert not fileops.is_world_readable(path)


# === Fonctions communes ===

OA, S2, CORE, SERP = common.API_KEY_ENV_VARS


class TestCommon:
    def test_secret_values_of_every_server(self):
        doc = {"mcpServers": {
            "a": {"command": "x", "env": {"K": "sk-autre-secret", "VIDE": ""}},
            "b": {"url": "u", "headers": {"Authorization": "Bearer tok-autre"}},
        }, "mcp_servers": {"c": {"env": {"T": "abc123456"}}}}
        values = common.collect_secret_values(doc)
        assert {"sk-autre-secret", "Bearer tok-autre", "tok-autre",
                "abc123456"} <= set(values)
        assert "" not in values and "x" not in values

    def test_paths_urls_and_references_are_not_secrets(self):
        doc = {"mcpServers": {"a": {"env": {
            "HOME_DIR": "/Users/x/.codex", "WIN": "C:\\Users\\x", "URL": "https://h/x",
            "REF": "${AUTRE_KEY}", "COURT": "abc", "CLE": "sk-vraie-cle"}}}}
        assert common.collect_secret_values(doc) == ["sk-vraie-cle"]

    def test_scrub_longest_first(self):
        text = "erreur Bearer tok-autre et sk-1 et sk-12"
        out = common.scrub(text, ["tok-autre", "Bearer tok-autre", "sk-1", "sk-12"])
        assert "tok-autre" not in out and "sk-1" not in out

    def test_pool_unanimous_and_conflicts(self):
        envs = [{OA: "oa1", CORE: "c1"}, {OA: "oa1", CORE: "c2", S2: ""},
                {OA: "", SERP: "sp"}]
        pooled, conflicts = common.pool_api_keys(envs)
        assert pooled == {OA: "oa1", SERP: "sp"}
        assert conflicts == [CORE]

    def test_resolve_keys_order(self):
        keys = common.resolve_api_keys(
            provided={S2: "fourni"}, previous={OA: "ancien", S2: "ancien-s2"},
            pooled={OA: "commun", CORE: "commun-c", SERP: "commun-s"},
            conflicts=[SERP])
        assert keys == {OA: "ancien", S2: "fourni", CORE: "commun-c", SERP: ""}

    def test_merge_env_keeps_unmanaged_variables(self):
        previous = {"MOISSON_KEY_FALLBACK": "openalex", "MOISSON_OUTPUT_DIR": "/a",
                    OA: "x"}
        env = common.merge_env(previous, {"MOISSON_CACHE": "1"},
                               {OA: "y", S2: "", CORE: "", SERP: ""})
        assert env == {"MOISSON_KEY_FALLBACK": "openalex",
                       "MOISSON_OUTPUT_DIR": "/a", OA: "y", "MOISSON_CACHE": "1",
                       S2: "", CORE: "", SERP: ""}

    def test_merge_env_given_output_dir_wins(self):
        env = common.merge_env({"MOISSON_OUTPUT_DIR": "/a"},
                               {"MOISSON_OUTPUT_DIR": "/b"}, {})
        assert env["MOISSON_OUTPUT_DIR"] == "/b"

    def test_raise_to(self):
        assert common.raise_to({"t": 30}, "t", 60) == 60
        assert common.raise_to({"t": 120}, "t", 60) == 120
        assert common.raise_to({"t": "nan"}, "t", 60) == 60
        assert common.raise_to({}, "t", 60) == 60
