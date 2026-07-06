"""Tests for :mod:`neewer.catalog` — the machine-readable protocol catalogue.

The catalogue is pure data served verbatim by discovery endpoints, so the tests
pin three things: it stays **derived** from the registries it mirrors (a change
in ``commands.ACTIONS`` or ``effects.REGISTRY`` flows through automatically),
its byte-level facts stay in sync with the known wire layouts (payload
lengths per scene id), and the whole blob stays JSON-serialisable and importable
without a BLE stack.
"""
from __future__ import annotations

import inspect
import json
import subprocess
import sys

from neewer import catalog, effects
from neewer.protocol import commands, frames

# --- actions: derived from the command registry -----------------------------

def test_actions_schema_matches_registry():
    """The actions schema is a pure derivation of ``commands.ACTIONS``."""
    schema = catalog.actions()
    assert set(schema) == set(commands.ACTIONS)
    for name, spec in commands.ACTIONS.items():
        assert schema[name]["fields"] == list(spec.fields), name
        assert schema[name]["variadic"] == spec.variadic, name


def test_actions_schema_is_a_fresh_copy():
    """Mutating the returned schema must not corrupt the registry view."""
    schema = catalog.actions()
    schema["hsi"]["fields"].append("bogus")
    assert catalog.actions()["hsi"]["fields"] == list(commands.ACTIONS["hsi"].fields)


# --- scene catalogue ---------------------------------------------------------

#: Known payload length per scene id INCLUDING the id byte — the lengths the
#: official app sends per effect. The catalogue's per-param byte widths must
#: add up to exactly this.
SCENE_DATA_LENGTHS = {
    1: 4, 2: 5, 3: 5, 4: 6, 5: 6, 6: 5, 7: 6, 8: 5, 9: 6,
    10: 4, 11: 7, 12: 7, 13: 5, 14: 8, 15: 6, 16: 5, 17: 4, 18: 2,
}


def test_scene_ids_cover_the_full_catalogue():
    assert set(catalog.SCENES) == set(range(1, 19))


def test_scene_param_byte_widths_match_known_data_lengths():
    """Each scene's params (LE16 hues = 2 bytes) + the id byte = the app's length."""
    for effect_id, scene in catalog.SCENES.items():
        width = 1 + sum(p.get("bytes", 1) for p in scene["params"])
        assert width == SCENE_DATA_LENGTHS[effect_id], scene["name"]


def test_scene_params_are_complete_specs():
    for scene in catalog.SCENES.values():
        assert scene["confidence"] in (catalog.CONFIRMED, catalog.EXPERIMENTAL)
        for param in scene["params"]:
            assert set(param) >= {"name", "min", "max", "unit"}, scene["name"]
            assert param["min"] <= param["max"], (scene["name"], param["name"])


def test_scene_confidence_is_honest():
    # Only Lightning (id 1) has been human-confirmed end-to-end; everything else
    # must carry the experimental flag until a live pass verifies it.
    assert catalog.SCENES[1]["confidence"] == catalog.CONFIRMED
    assert catalog.SCENES[16]["confidence"] == catalog.EXPERIMENTAL


def test_scene_id_sets_are_pinned():
    """The per-model id subsets are pinned verbatim."""
    sets = catalog.SCENE_ID_SETS
    assert sets[9] == sets[10] == (1, 2, 3, 4, 5, 6, 8, 14, 15)
    assert sets[12] == (1, 2, 3, 4, 5, 6, 8, 11, 13, 14, 15, 16)
    assert sets[13] == (1, 2, 3, 4, 5, 6, 8, 11, 13, 14, 15, 16, 18)
    assert sets[17] == tuple(range(1, 18))
    assert sets[18] == tuple(range(1, 19))
    # Every id in every subset exists in the scene table.
    for ids in sets.values():
        assert set(ids) <= set(catalog.SCENES)


# --- pixel catalogue ---------------------------------------------------------

def test_pixel_effect_wire_ids():
    # Base Pixel wire ids: 1-7 plus the remapped 10/11/12 (internal 8/9/10).
    assert set(catalog.PIXEL_EFFECTS) == {1, 2, 3, 4, 5, 6, 7, 10, 11, 12}


def test_pixel_effect_1_matches_the_captured_scalar_block():
    """Effect 1's scalar order is the confirmed capture decode (A.20.1)."""
    scalars = [p["name"] for p in catalog.PIXEL_EFFECTS[1]["scalars"]]
    assert scalars == ["brightness", "color_number", "speed", "direction",
                       "running_status"]
    assert catalog.PIXEL_EFFECTS[1]["confidence"] == catalog.CONFIRMED


def test_pixel_moving_effects_share_the_seven_scalar_layout():
    for wire_id in (3, 4, 5):
        scalars = [p["name"] for p in catalog.PIXEL_EFFECTS[wire_id]["scalars"]]
        assert scalars == ["color_brightness", "background_brightness", "way",
                           "speed", "direction", "movement", "running_status"]
        assert catalog.PIXEL_EFFECTS[wire_id]["confidence"] == catalog.EXPERIMENTAL


def test_pixel_palette_slot_modes():
    palette = catalog.PIXEL_PALETTE
    assert palette["max_slots"] == 8
    # The shared 3-byte cell encoding: 0x00 CCT / 0x10 HSI / 0x20 off.
    assert palette["slot_modes"]["cct"]["flag"] == 0x00
    assert palette["slot_modes"]["hsi"]["flag"] == 0x10
    assert palette["slot_modes"]["off"]["flag"] == 0x20
    assert palette["slot_modes"]["off"]["fields"] == []


def test_running_status_enum():
    assert catalog.RUNNING_STATUS == {0: "stop", 1: "play", 2: "pause",
                                      3: "continue"}


# --- flow modes: pinned to the effect registry -------------------------------

def test_flow_modes_cover_the_effect_registry_exactly():
    assert set(catalog.FLOW_MODES) == set(effects.REGISTRY)
    assert set(effects.PARAMS) == set(effects.REGISTRY)


def test_flow_param_defaults_match_engine_signatures():
    """Every advertised default must equal the engine's actual keyword default.

    This is the sync guard: retuning an engine default without updating PARAMS
    (or vice versa) fails here.
    """
    for mode, engine in effects.REGISTRY.items():
        signature = inspect.signature(engine)
        for param in effects.PARAMS[mode]:
            assert param["name"] in signature.parameters, (mode, param["name"])
            default = signature.parameters[param["name"]].default
            if isinstance(default, tuple):          # multistop's stops
                default = list(default)
            assert param["default"] == default, (mode, param["name"])


def test_flow_tri_aliases_multistop_params():
    assert catalog.FLOW_MODES["tri"]["params"] is catalog.FLOW_MODES["multistop"]["params"]


# --- gel brands ---------------------------------------------------------------

def test_gel_brands_derive_from_frame_constants():
    assert catalog.GEL_BRANDS == {frames.GEL_BRAND_ROSCO: "ROSCO",
                                  frames.GEL_BRAND_LEE: "LEE"}


# --- the blob ------------------------------------------------------------------

def test_catalog_blob_shape_and_json_serialisability():
    blob = catalog.catalog()
    assert set(blob) == {"version", "actions", "scenes", "scene_id_sets",
                         "pixel_effects", "pixel_palette", "flow_modes",
                         "gel_brands", "running_status"}
    assert blob["version"] == catalog.CATALOG_VERSION
    # One JSON blob, servable as-is (int keys become string object keys).
    parsed = json.loads(json.dumps(blob))
    assert parsed["scenes"]["1"]["name"] == "lightning"
    assert parsed["gel_brands"]["1"] == "ROSCO"


def test_catalog_imports_without_bleak():
    """The catalogue must stay bleak-free: import it with bleak import-blocked.

    A subprocess gives a clean interpreter (the test process has a bleak stub
    pre-installed by conftest); ``sys.modules["bleak"] = None`` makes any
    ``import bleak`` raise ImportError immediately.
    """
    script = (
        "import sys; sys.modules['bleak'] = None; "
        "import neewer.catalog, neewer.protocol.commands, neewer.effects; "
        "print(neewer.catalog.CATALOG_VERSION)"
    )
    result = subprocess.run([sys.executable, "-c", script],
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == str(catalog.CATALOG_VERSION)
