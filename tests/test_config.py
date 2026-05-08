import tomllib
from pathlib import Path
import pytest
import platformdirs

from spoofloc import config as cfg_mod


def test_defaults_have_expected_keys():
    d = cfg_mod.DEFAULTS
    assert "device" in d
    assert "tunnel" in d
    assert "web" in d
    assert "route" in d
    assert "geocoding" in d


def test_deep_merge_override():
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    override = {"a": {"x": 99}}
    result = cfg_mod._deep_merge(base, override)
    assert result["a"]["x"] == 99
    assert result["a"]["y"] == 2  # preserved
    assert result["b"] == 3


def test_deep_merge_new_key():
    base = {"a": 1}
    override = {"b": 2}
    result = cfg_mod._deep_merge(base, override)
    assert result == {"a": 1, "b": 2}


def test_load_returns_defaults_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg_mod, "config_path", lambda: tmp_path / "nonexistent.toml")
    cfg = cfg_mod.load()
    assert cfg["route"]["default_speed_mph"] == 30.0
    assert cfg["web"]["port"] == 4780


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    test_path = tmp_path / "config.toml"
    monkeypatch.setattr(cfg_mod, "config_path", lambda: test_path)

    cfg = cfg_mod.load()
    cfg["route"]["default_speed_mph"] = 120.0
    cfg_mod.save(cfg)

    loaded = cfg_mod.load()
    assert loaded["route"]["default_speed_mph"] == 120.0


def test_set_key_float(tmp_path, monkeypatch):
    test_path = tmp_path / "config.toml"
    monkeypatch.setattr(cfg_mod, "config_path", lambda: test_path)

    cfg_mod.set_key("route.default_speed_mph", "75.5")
    assert cfg_mod.get_key("route.default_speed_mph") == pytest.approx(75.5)


def test_set_key_bool(tmp_path, monkeypatch):
    test_path = tmp_path / "config.toml"
    monkeypatch.setattr(cfg_mod, "config_path", lambda: test_path)

    cfg_mod.set_key("route.loop", "true")
    assert cfg_mod.get_key("route.loop") is True

    cfg_mod.set_key("route.loop", "false")
    assert cfg_mod.get_key("route.loop") is False


def test_get_key_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg_mod, "config_path", lambda: tmp_path / "nonexistent.toml")
    with pytest.raises(KeyError):
        cfg_mod.get_key("does.not.exist")
