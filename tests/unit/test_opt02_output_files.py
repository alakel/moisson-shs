"""
Tests OPT-02 — module `utils/output_files.py`.

Classification centralisée des fichiers de OUTPUT_DIR (corpus, statistiques,
fichiers associés, fichiers de service), espace de noms plat `corpus://` et
lecture en flux d'un tableau JSON. Aucun réseau, aucune socket.
"""

import json
import sys
from pathlib import Path

import pytest
from pydantic import AnyUrl

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from utils.output_files import (  # noqa: E402
    corpus_kind, corpus_name_from_uri, corpus_uri, is_associated_file,
    is_corpus_file, is_downloadable_file, is_publishable_file,
    is_service_path, iter_json_array, validate_flat_name,
)


# === Classification des noms ===

class TestCorpusKind:
    @pytest.mark.parametrize("name, kind", [
        ("corpus_x.json", "zotero"),
        ("corpus_x.csl.json", "csl"),
        ("corpus_x_stats.txt", "stats"),
        ("export_libre.json", "zotero"),          # output_path arbitraire
        ("corpus_x.CSL.JSON", "csl"),              # casse ignorée
        ("corpus_x.manifest.json", None),          # fichier associé
        ("corpus_x.doi_report.json", None),        # fichier associé
        ("corpus_x.MANIFEST.json", None),          # associé, casse ignorée
        (".index.json", None),                     # fichier de service
        (".oauth", None),
        ("notes.md", None),                        # hors liste blanche
        ("corpus_x.jsonl", None),
        ("", None),
    ])
    def test_kinds(self, name, kind):
        assert corpus_kind(name) == kind

    def test_kind_uses_basename(self, tmp_path):
        assert corpus_kind(tmp_path / "corpus_x.csl.json") == "csl"
        assert corpus_kind(tmp_path / "corpus_x.manifest.json") is None

    def test_corpus_file_excludes_stats_associated_service(self):
        assert is_corpus_file("corpus_x.json")
        assert is_corpus_file("corpus_x.csl.json")
        assert not is_corpus_file("corpus_x_stats.txt")
        assert not is_corpus_file("corpus_x.manifest.json")
        assert not is_corpus_file("corpus_x.doi_report.json")
        assert not is_corpus_file(".index.json")

    def test_publishable_is_whitelist_minus_associated(self):
        assert is_publishable_file("corpus_x_stats.txt")
        assert is_publishable_file("corpus_x.json")
        assert not is_publishable_file("corpus_x.doi_report.json")
        assert not is_publishable_file(".index.json")
        assert not is_publishable_file("evil.py")

    def test_downloadable_includes_associated_never_service(self):
        assert is_downloadable_file("corpus_x.manifest.json")
        assert is_downloadable_file("corpus_x.doi_report.json")
        assert is_downloadable_file("corpus_x.csl.json")
        assert not is_downloadable_file(".index.json")
        assert not is_downloadable_file(".x.manifest.json")
        assert not is_downloadable_file("evil.py")

    def test_associated(self):
        assert is_associated_file("a.manifest.json")
        assert is_associated_file("a.doi_report.json")
        assert not is_associated_file("a.json")


class TestServicePath:
    @pytest.mark.parametrize("path", [
        ".oauth/oauth_store.json", ".audit/audit-20260928.jsonl",
        ".index.json", ".env", "../x.json", "sub/.cache/x.json",
    ])
    def test_service_paths(self, path):
        assert is_service_path(path)

    @pytest.mark.parametrize("path", [
        "corpus_x.json", "raw/hal_1.json", "corpus_x.manifest.json",
    ])
    def test_non_service_paths(self, path):
        assert not is_service_path(path)

    def test_relative_to_base(self, tmp_path):
        base = tmp_path / "out"
        assert is_service_path(base / ".oauth" / "oauth_store.json", base)
        assert is_service_path(base / ".index.json", base)
        assert not is_service_path(base / "corpus_x.json", base)

    def test_base_under_hidden_directory_still_usable(self, tmp_path):
        # Un OUTPUT_DIR placé sous un dossier caché ne rend pas tous ses
        # corpus « de service ».
        base = tmp_path / ".config" / "moisson" / "output"
        assert not is_service_path(base / "corpus_x.json", base)
        assert is_service_path(base / ".audit" / "a.jsonl", base)

    def test_outside_base_only_name_counts(self, tmp_path):
        base = tmp_path / "out"
        assert not is_service_path(tmp_path / ".cache" / "corpus.json", base)
        assert is_service_path(tmp_path / "ailleurs" / ".cache.json", base)


# === Espace de noms corpus:// ===

class TestFlatName:
    @pytest.mark.parametrize("name", [
        "", ".", "..", ".oauth", ".index.json", "a/b.json", "a\\b.json",
        "../x.json", "x\x00.json", "x\n.json",
    ])
    def test_refused(self, name):
        with pytest.raises(ValueError):
            validate_flat_name(name)

    @pytest.mark.parametrize("name", [
        "corpus_x.json", "corpus_é.json", "mon corpus.json", "50%.json",
        "a..b.json",
    ])
    def test_accepted(self, name):
        assert validate_flat_name(name) == name


class TestCorpusUri:
    @pytest.mark.parametrize("name", [
        "corpus_x.json", "corpus_x.csl.json", "corpus_x_stats.txt",
        "corpus_é.json", "mon corpus.json", "50%.json", "a?b#c.json",
    ])
    def test_round_trip_str_and_anyurl(self, name):
        uri = corpus_uri(name)
        assert corpus_name_from_uri(uri) == name
        # Forme transmise par le SDK : AnyUrl pydantic (valide même avec une
        # espace, désormais encodée).
        assert corpus_name_from_uri(AnyUrl(uri)) == name

    def test_slug_names_unchanged(self):
        assert corpus_uri("corpus_x.json") == "corpus://corpus_x.json"

    def test_sdk_encoded_non_ascii_is_decoded(self):
        # Le SDK encode les noms non ASCII : corpus_é.json → corpus_%C3%A9.json.
        assert corpus_name_from_uri(AnyUrl("corpus://corpus_é.json")) == \
            "corpus_é.json"

    @pytest.mark.parametrize("uri", [
        "corpus://../x.json",
        "corpus://..",
        "corpus://../../etc/passwd",
        "corpus:///etc/passwd",
        "corpus://%2e%2e/x.json",
        "corpus://%2E%2E%2Fetc%2Fpasswd",
        "corpus://..%2f.oauth%2foauth_store.json",
        "corpus://a%2fb.json",
        "corpus://a%5c..%5cb.json",
        "corpus://.oauth/oauth_store.json",
        "corpus://.audit/x.jsonl",
        "corpus://.index.json",
        "corpus://./x.json",
        "corpus://sub/corpus_x.json",
        "corpus://corpus_x.json?offset=5",
        "corpus://corpus_x.json#frag",
        "corpus://%ff.json",
        "corpus://%00.json",
        "corpus://",
        "file:///etc/passwd",
    ])
    def test_refused_uris_str_and_anyurl(self, uri):
        with pytest.raises(ValueError):
            corpus_name_from_uri(uri)
        try:
            parsed = AnyUrl(uri)
        except Exception:
            return  # refusé par pydantic lui-même
        with pytest.raises(ValueError):
            corpus_name_from_uri(parsed)

    def test_double_encoding_decoded_once(self):
        # %252e%252e → %2e%2e (nom littéral, inoffensif), jamais « .. ».
        assert corpus_name_from_uri("corpus://%252e%252e") == "%2e%2e"


# === Lecture en flux d'un tableau JSON ===

class TestIterJsonArray:
    @pytest.mark.parametrize("data", [
        [],
        [1, 22, -4.5e10, 1e-7, True, False, None, "s"],
        [{"t": "é" * 40, "l": [1, {"x": "\\\"]"}]} for _ in range(20)],
        ["€𝄞" * 7, {"a": []}, [], {}],
    ])
    @pytest.mark.parametrize("indent", [None, 2])
    @pytest.mark.parametrize("chunk", [1, 3, 7, 64, 65536])
    def test_matches_json_loads(self, tmp_path, data, indent, chunk):
        p = tmp_path / "a.json"
        p.write_text(json.dumps(data, ensure_ascii=False, indent=indent),
                     encoding="utf-8")
        assert list(iter_json_array(p, chunk_chars=chunk)) == data

    def test_number_split_before_fraction(self, tmp_path):
        # Cas limite : « -45000 » | « 000000.0 » — décodé trop tôt sans la
        # vérification du caractère suivant.
        p = tmp_path / "n.json"
        p.write_text("[-45000000000.0, 1]", encoding="utf-8")
        assert list(iter_json_array(p, chunk_chars=7)) == [-45000000000.0, 1]

    @pytest.mark.parametrize("bad", [
        "{}", "", "  ", "[1,", "[1 2]", '[{"a": }]', "[1,]", "[1x]", '["abc',
    ])
    def test_invalid_raises_value_error(self, tmp_path, bad):
        p = tmp_path / "bad.json"
        p.write_text(bad, encoding="utf-8")
        with pytest.raises(ValueError):
            list(iter_json_array(p, chunk_chars=2))

    def test_max_item_guard(self, tmp_path):
        p = tmp_path / "big.json"
        p.write_text(json.dumps(["x" * 10_000]), encoding="utf-8")
        with pytest.raises(ValueError):
            list(iter_json_array(p, chunk_chars=64, max_item_chars=1_000))
