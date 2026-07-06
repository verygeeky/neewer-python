"""Tests for :mod:`neewer.protocol.models` — the fixture capability table + inference."""
from __future__ import annotations

from neewer.protocol import models


def test_capabilities_known_models():
    tl120 = models.capabilities("TL120C")
    assert tl120.pixel is True and tl120.scene_legacy is False   # firmware-confirmed
    assert models.capabilities("TL90C").pixel is False


def test_capabilities_by_mac_colour_modes_on_tl120c():
    for model in ("TL120C", "TL120C-2"):
        caps = models.capabilities(model)
        assert caps.rgbcw is True and caps.xy is True and caps.gel is True


def test_capabilities_by_mac_colour_modes_on_tl90c():
    # Human-confirmed live: TL90C renders by-MAC 0xA9/0xB7/0xAD and ignores
    # the direct forms, exactly like the TL120C (by-MAC-only is a CE-line trait).
    caps = models.capabilities("TL90C")
    assert caps.rgbcw is True and caps.xy is True and caps.gel is True


def test_capabilities_by_mac_colour_modes_on_generic():
    # Permissive fallback: an unidentified fixture gets the by-MAC colour modes ON.
    # Direct forms are dead across the CE line and by-MAC no-ops harmlessly if unsupported,
    # so this never makes the daemon less capable than before it knew the model.
    assert (models.GENERIC.rgbcw, models.GENERIC.xy, models.GENERIC.gel) == (True, True, True)


def test_capabilities_unknown_is_permissive_generic():
    assert models.capabilities(None) is models.GENERIC
    assert models.capabilities("Unknownium") is models.GENERIC
    # GENERIC assumes scene works (don't get *less* capable when the model is unknown)
    assert models.GENERIC.scene_legacy is True
    assert models.GENERIC.pixel is False


def test_infer_model_from_firmware_version():
    assert models.infer_model("2.0.5") == "TL120C-2"
    assert models.infer_model("1.1.11") == "TL90C"
    assert models.infer_model("9.9.9") is None
    assert models.infer_model(None) is None


def test_tl60_recognized_and_streamer_capable():
    # fw 3.0.3 self-reports "RGB-3" (verified live); 3.0.5 already mapped.
    assert models.infer_model("3.0.3") == "TL60 RGB-3"
    assert models.infer_model("3.0.5") == "TL60 RGB-3"
    assert models.infer_model("2.4.8") == "TL60 RGB-2"
    # streamer (0xC0/0xBF) is TL60-only, confirmed via the 0xC4 support query.
    assert models.capabilities("TL60 RGB-3").streamer is True
    assert models.capabilities("TL60 RGB-2").streamer is True
    assert models.GENERIC.streamer is False
    assert models.capabilities("TL120C-2").streamer is False


def test_tl90c_older_firmware_1_1_9_recognized():
    # CA (NW-20240012, fw 1.1.9) was showing generic — same TL90C family as E9 (1.1.11).
    assert models.infer_model("1.1.9") == "TL90C"
    assert models.infer_model("1.1.11") == "TL90C"


def test_name_model_decodes_advertised_name():
    # The advertised name resolves straight to a model (no query needed) — this is the
    # top-priority, connection-free identifier used at discovery.
    assert models.name_model("NW-20240012&00000000") == "TL90C"
    assert models.name_model("NW-20240047&FFFFFFFF") == "TL120C-2"
    assert models.name_model("NW-20240061&00000000") == "TL60 RGB-3"
    assert models.name_model("NW-99999999&00000000") is None  # unlisted serial
    assert models.name_model(None) is None


def test_name_model_feeds_capabilities():
    # The whole point: name -> model -> capabilities, with no round-trip.
    caps = models.capabilities(models.name_model("NW-20240061&00000000"))
    assert caps.streamer is True                     # TL60 RGB-3 is streamer-capable
    caps90 = models.capabilities(models.name_model("NW-20240012&00000000"))
    assert (caps90.rgbcw, caps90.xy, caps90.gel) == (True, True, True)  # TL90C by-MAC colour
