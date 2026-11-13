from __future__ import annotations

from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from simmanager.kernel import (
    ConfigError,
    ConfigLayer,
    compose,
    deep_merge,
    discover,
    load_yaml_file,
)

from .helpers import make_plugin


def test_deep_merge_replaces_lists_and_scalars() -> None:
    base = {"a": {"b": 1, "c": [1, 2]}, "d": 1}
    over = {"a": {"c": [3]}, "e": 2}
    assert deep_merge(base, over) == {"a": {"b": 1, "c": [3]}, "d": 1, "e": 2}
    assert base == {"a": {"b": 1, "c": [1, 2]}, "d": 1}  # inputs untouched


def test_three_layers_and_origins() -> None:
    composed = compose(
        [
            ConfigLayer(
                "defaults", {"plugins": {"m": {"enabled": True, "poll": 60}}, "log": "INFO"}
            ),
            ConfigLayer("user", {"plugins": {"m": {"poll": 120}}}),
            ConfigLayer("project", {"log": "DEBUG"}),
        ]
    )
    assert composed.data == {"plugins": {"m": {"enabled": True, "poll": 120}}, "log": "DEBUG"}
    assert composed.origins[("plugins", "m", "poll")] == "user"
    assert composed.origins[("plugins", "m", "enabled")] == "defaults"
    assert composed.origins[("log",)] == "project"


def test_origin_dropped_when_mapping_replaced_by_scalar() -> None:
    composed = compose([ConfigLayer("a", {"x": {"y": 1}}), ConfigLayer("b", {"x": None})])
    assert composed.origins == {("x",): "b"}


def test_load_yaml_file(tmp_path: Path) -> None:
    assert load_yaml_file(tmp_path / "missing.yaml") == {}
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    assert load_yaml_file(empty) == {}
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_yaml_file(bad)
    broken = tmp_path / "broken.yaml"
    broken.write_text("a: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_yaml_file(broken)


class _FakeDist:
    name = "fake-dist"


def _ep(name: str, value: str) -> EntryPoint:
    ep = EntryPoint(name=name, value=value, group="simmanager.plugins")
    return ep._for(_FakeDist())  # type: ignore[attr-defined, no-any-return]


GoodExternal = make_plugin("good-external", [])
TwoPlugins = [make_plugin("one", []), make_plugin("two", [])]
NotAPlugin = object()


def test_discovery_builtins_entry_points_and_broken_ones() -> None:
    builtin = make_plugin("builtin", [])
    eps = [
        _ep("good", f"{__name__}:GoodExternal"),
        _ep("many", f"{__name__}:TwoPlugins"),
        _ep("broken", "module_that_does_not_exist_xyz:Thing"),
        _ep("wrong", f"{__name__}:NotAPlugin"),
        _ep("dupe", f"{__name__}:GoodExternal"),
    ]
    result = discover([builtin], load_entry_points=lambda group: eps)
    assert [p.name for p in result.plugins] == ["builtin", "good-external", "one", "two"]
    assert result.plugins[1].source == "fake-dist:good"
    assert len(result.errors) == 3
    assert any("import failed" in e for e in result.errors)
    assert any("not a Plugin" in e for e in result.errors)
    assert any("already provided" in e for e in result.errors)
