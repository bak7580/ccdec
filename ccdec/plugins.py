"""Decoders for the property blobs (``eData``) of stock plugins and behaviors.

Each blob is whatever the plugin's ``EditExt::Serialize`` wrote; the layouts below
are transcribed from the plugin sources in vendor/construct-src.  A decoder must
consume the blob exactly, otherwise the result is discarded and the blob is kept
raw - plugin builds older or newer than the r321 sources may differ.

The blob is stored unchanged in the .cap, so Level 3 never needs these decoders;
they only make Level 2 readable and provide image/animation links for naming.
"""

from __future__ import annotations

from typing import Callable, Optional

from .reader import ParseError, Reader

Decoder = Callable[[Reader], dict]


def _version(r: Reader, newest: int) -> int:
    v = r.i32("version")
    if not 0 <= v <= newest:
        raise r.fail(f"unknown property version {v} (newest known {newest})")
    return v


def sprite(r: Reader) -> dict:
    """Plugins/Sprite/Serialization.cpp :: EditExt::Serialize (version 5)."""
    v = _version(r, 5)
    d: dict = {"version": v}
    if v < 2:
        d["angle"] = r.f32("angle")
        d["filter"] = r.u32("filter")
    d["collision_mode"] = r.i32("collision mode")
    d["auto_mirror"] = r.i32("auto mirror")
    d["auto_flip"] = r.i32("auto flip")
    d["auto_rotations"] = r.i32("auto rotations")
    d["auto_rotations_combo"] = r.i32("auto rotations combo")
    d["hide_at_start"] = r.boolean("hide at start")
    d["animation_root"] = r.i32("animation root")          # CLayout::SerializeAnimation
    if v >= 3:
        d["skew_x"], d["skew_y"] = r.f32("skew x"), r.f32("skew y")
    if v >= 4:
        d["locked_animation_angles"] = r.boolean("locked animation angles")
    if v >= 5:
        d["start_animation"] = r.cstr("start animation")
        d["start_frame"] = r.i32("start frame")
    return d


def text(r: Reader) -> dict:
    """Plugins/Text/Edittime.cpp :: EditExt::Serialize (version 2)."""
    v = _version(r, 2)
    d = {"version": v, "text": r.cstr("text"), "font": r.cstr("font face"), "size_px": r.i32("size"),
         "italic": r.i32("italic"), "bold": r.i32("bold"), "color": r.u32("color"),
         "opacity": r.f32("opacity"), "horizontal_align": r.i32("horizontal align"),
         "vertical_align": r.i32("vertical align")}
    if v >= 2:
        d["hide_at_start"] = r.boolean("hide at start")
    return d


def tiled_background(r: Reader) -> dict:
    """Plugins/Tiled background :: EditExt::Serialize (version 2)."""
    v = _version(r, 2)
    d: dict = {"version": v, "image": r.i32("image")}            # CLayout::SerializeImage
    if v < 2:
        d["opacity"] = r.f64("opacity")
        d["filter"] = r.u32("filter")
    return d


def panel(r: Reader) -> dict:
    """Plugins/Panel :: EditExt::Serialize (version 2)."""
    v = _version(r, 2)
    d: dict = {"version": v, "image": r.i32("image")}
    d["vertex_colors"] = [r.u32("vertex color") for _ in range(4)]
    d["vertex_opacity"] = [r.f32("vertex opacity") for _ in range(4)]
    d["margins"] = {k: r.i32(f"{k} margin") for k in ("left", "right", "top", "bottom")}
    d["hotspot_position"] = r.i32("hotspot position")
    if v >= 2:
        d["angled_box"] = r.boolean("angled box")
    return d


def platform(r: Reader) -> dict:
    """Behaviors/Platform :: EditExt::Serialize (versions 2-5; version 1 differs)."""
    v = _version(r, 5)
    if v < 2:
        raise r.fail("Platform property version 1 is not decoded")
    d: dict = {"version": v}
    for k in ("floor_acceleration", "floor_deceleration", "air_acceleration", "air_deceleration",
              "max_floor_speed", "max_air_speed", "max_fall_speed", "jump_strength", "jump_sustain",
              "gravity_fall"):
        d[k] = r.f32(k)
    d["gravity_direction"] = r.i32("gravity direction")
    d["inv"] = r.f32("inv")
    d["bunny_hop"] = r.boolean("bunny hop")
    if v >= 2:
        d["can_press_down"] = r.boolean("can press down")
    if v >= 3:
        d["player"] = r.i32("player")
    if v >= 4:
        d["gravity_jump"] = r.f32("gravity jump")
        d["gravity_jump_sustain"] = r.f32("gravity jump sustain")
    if v >= 5:
        d["auto_change_angle"] = r.boolean("auto change angle")
    return d


#: keyed by lower-case plugin file name
DECODERS: dict[str, Decoder] = {
    "sprite.csx": sprite,
    "text.csx": text,
    "tilebackground.csx": tiled_background,
    "panel.csx": panel,
    "platformmov.csx": platform,
}


def decode_properties(plugin_file: Optional[str], data: bytes, encoding: str) -> tuple[Optional[dict], Optional[str]]:
    """Return (properties, problem).  ``properties`` is None when not decoded."""
    if not plugin_file:
        return None, "plugin file name unknown"
    decoder = DECODERS.get(plugin_file.lower())
    if decoder is None:
        return None, None                      # no decoder: raw is the expected outcome
    r = Reader(data, f"{plugin_file} properties", encoding)
    try:
        d = decoder(r)
        r.finish()
        return d, None
    except ParseError as e:
        return None, str(e)
