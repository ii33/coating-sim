"""Contracts for the explicit, trusted plugin registry."""
from __future__ import annotations

from typing import Any

import pytest

from coat_sim.core.plugins import PLUGIN_API_VERSION, PluginRegistry, PluginSpec


def test_original_plugin_categories_remain_usable():
    registry = PluginRegistry()
    categories = ("source_sampler", "analysis", "exporter")

    for category in categories:
        name = "fixture_" + category
        spec = PluginSpec(category, name, PLUGIN_API_VERSION, lambda value: value + 1)
        registry.register(spec, trusted=True)
        assert registry.get(category, name) is spec
        assert registry.invoke(category, name, 4) == 5


def test_registration_requires_trust_and_supported_version():
    registry = PluginRegistry()
    untrusted = PluginSpec("film_importer", "tfcalc_plaintext", 1, lambda: None)
    with pytest.raises(PermissionError):
        registry.register(untrusted)

    with pytest.raises(ValueError):
        registry.register(PluginSpec("film_importer", "tfcalc_plaintext", 2,
                                     lambda: None), trusted=True)

    # Deliberately malformed API-boundary inputs bypass static signatures so the
    # runtime strict-bool/int guards, including truthy lookalikes, are exercised.
    invalid_trust: tuple[Any, ...] = (1, "false", "true")
    for truthy in invalid_trust:
        with pytest.raises(PermissionError):
            registry.register(untrusted, trusted=truthy)
    registry.register(untrusted, trusted=True)
    invalid_versions: tuple[Any, ...] = (True, 1.0, "1")
    for version in invalid_versions:
        with pytest.raises(ValueError):
            registry.get("film_importer", "tfcalc_plaintext", api_version=version)
    invalid_enabled: tuple[Any, ...] = (1, "false", None)
    for enabled in invalid_enabled:
        with pytest.raises(TypeError):
            registry.set_enabled("film_importer", "tfcalc_plaintext", enabled)


def test_film_importer_metadata_survives_disable_and_invocation_is_gated():
    registry = PluginRegistry()
    spec = PluginSpec("film_importer", "tfcalc_plaintext", 1,
                      lambda text: {"layers": (text,)}, "TFCalc plaintext fixture")
    registry.register(spec, trusted=True)

    assert registry.get("film_importer", "tfcalc_plaintext") is spec
    assert registry.invoke("film_importer", "tfcalc_plaintext", "synthetic layer") == {
        "layers": ("synthetic layer",),
    }

    registry.set_enabled("film_importer", "tfcalc_plaintext", False)
    assert registry.is_enabled("film_importer", "tfcalc_plaintext") is False
    assert registry.get("film_importer", "tfcalc_plaintext") is spec
    assert registry.list("film_importer") == (spec,)
    with pytest.raises(PermissionError):
        registry.invoke("film_importer", "tfcalc_plaintext", "synthetic layer")

    registry.set_enabled("film_importer", "tfcalc_plaintext", True)
    assert registry.is_enabled("film_importer", "tfcalc_plaintext") is True
    assert registry.invoke("film_importer", "tfcalc_plaintext", "synthetic layer") == {
        "layers": ("synthetic layer",),
    }
