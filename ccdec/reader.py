"""Phase 2: strict parsers for the data blocks of a Construct Classic export.

Every block is parsed with an explicit field-by-field schema taken from the
exporter/runtime source (FORMAT.md sections 3-7).  The blocks carry no version
field, so runtime differences are handled with *layout profiles*: a profile is
accepted for a block only if parsing consumes the block exactly to its last
byte.  A block no profile fits is kept verbatim in ``Project.raw`` and reported;
it never aborts the other blocks.

Input is untrusted: all reads are bounds-checked, element counts are validated
against the bytes that remain before anything is allocated, and nesting depth
is capped.
"""

from __future__ import annotations

import dataclasses
import logging
import struct
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar, Union

from . import model as m
from .container import Container

log = logging.getLogger(__name__)

MAX_STRING = 64 << 20           # scripts are stored as string literals; be generous
MAX_EVENT_DEPTH = 200
MAX_ANIMATION_DEPTH = 64

# Event block markers (Runtime/CapReader.h)
BEGINEVENTLIST, BEGINEVENT, BEGINCONDITIONS, BEGINCONDITION, ENDCONDITION = 1, 2, 3, 4, 5
ENDCONDITIONS, BEGINACTIONS, BEGINACTION, ENDACTION, ENDACTIONS = 6, 7, 8, 9, 10
ENDEVENT, ENDEVENTLIST, BEGINGROUP, ENDGROUP = 13, 14, 15, 16

T = TypeVar("T")


class ParseError(Exception):
    """A block does not match the expected layout."""

    def __init__(self, block: str, offset: int, message: str) -> None:
        self.block, self.offset, self.detail = block, offset, message
        super().__init__(f"{block}: {message} (at block offset {offset:#x})")


@dataclass(frozen=True)
class Profile:
    """One known variant of the block layouts."""

    name: str
    description: str
    app_eye_distance: bool = True
    #: layers end with ``bool clearDepthBuffer`` (absent in 2009 builds)
    layer_clear_depth: bool = True


#: Tried in order; newest first.
PROFILES: tuple[Profile, ...] = (
    Profile("r2", "layout of the Classic r2 source (r321); also matches 2011 builds"),
    Profile("0.99-2009", "2009 builds (0.99.x): APPBLOCK without eye_distance; layers without clearDepthBuffer",
            app_eye_distance=False, layer_clear_depth=False),
)


class Reader:
    """Bounds-checked little-endian cursor over one block."""

    def __init__(self, data: Union[bytes, memoryview], block: str, encoding: str) -> None:
        self.data = memoryview(data)
        self.block = block
        self.encoding = encoding
        self.pos = 0

    @property
    def remaining(self) -> int:
        return len(self.data) - self.pos

    def fail(self, message: str, offset: Optional[int] = None) -> ParseError:
        return ParseError(self.block, self.pos if offset is None else offset, message)

    def _take(self, fmt: str, size: int, what: str):
        if self.remaining < size:
            raise self.fail(f"unexpected end of block while reading {what}")
        (v,) = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += size
        return v

    def u8(self, what: str) -> int:
        return self._take("<B", 1, what)

    def boolean(self, what: str) -> bool:
        return self._take("<B", 1, what) != 0

    def i32(self, what: str) -> int:
        return self._take("<i", 4, what)

    def u32(self, what: str) -> int:
        return self._take("<I", 4, what)

    def i64(self, what: str) -> int:
        return self._take("<q", 8, what)

    def f32(self, what: str) -> float:
        return self._take("<f", 4, what)

    def f64(self, what: str) -> float:
        return self._take("<d", 8, what)

    def peek(self) -> int:
        if self.remaining < 1:
            raise self.fail("unexpected end of block")
        return self.data[self.pos]

    def view(self, size: int, what: str) -> memoryview:
        if size < 0 or size > self.remaining:
            raise self.fail(f"{what}: size {size} exceeds the {self.remaining} bytes left")
        v = self.data[self.pos:self.pos + size]
        self.pos += size
        return v

    def blob(self, what: str) -> bytes:
        """``int size`` followed by that many bytes."""
        return bytes(self.view(self.i32(f"{what} size"), what))

    def count(self, what: str, min_item_size: int = 1) -> int:
        """An element count, rejected unless that many elements can still fit."""
        start = self.pos
        n = self.i32(what)
        if n < 0 or n * min_item_size > self.remaining:
            raise self.fail(f"implausible {what}: {n}", start)
        return n

    def cstr(self, what: str) -> str:
        """bin CString: ``int length`` (terminator included) + bytes."""
        start = self.pos
        n = self.i32(f"{what} length")
        if n < 0 or n > min(self.remaining, MAX_STRING):
            raise self.fail(f"{what}: implausible string length {n}", start)
        raw = bytes(self.view(n, what))
        end = raw.find(b"\0")
        if end >= 0:
            raw = raw[:end]
        # surrogateescape keeps undecodable bytes so strings round-trip exactly.
        return raw.decode(self.encoding, "surrogateescape")

    def expect(self, marker: int, what: str) -> None:
        start = self.pos
        got = self.u8(what)
        if got != marker:
            raise self.fail(f"expected {what} marker {marker}, found {got}", start)

    def finish(self) -> None:
        if self.remaining:
            raise self.fail(f"{self.remaining} unparsed bytes left at end of block")


# --- APPBLOCK ------------------------------------------------------------------
def _keys(r: Reader, what: str) -> list[m.KeyEntry]:
    out = []
    for _ in range(r.count(f"{what} count", 12)):
        key = r.cstr(f"{what} name")
        kind = r.i32(f"{what} type")
        out.append(m.KeyEntry(key, kind, r.i32(f"{what} value") if kind == 0 else r.cstr(f"{what} value")))
    return out


def parse_app(r: Reader, p: Profile) -> m.AppProperties:
    name = r.cstr("application name")
    width, height = r.i32("window width"), r.i32("window height")
    eye = r.f32("eye distance") if p.app_eye_distance else None
    show_menu = r.i32("show menu")
    screensaver = r.boolean("screensaver")
    fps_mode = r.u8("fps mode")
    fps = r.i32("fps")
    fullscreen = r.boolean("fullscreen")
    sampler = r.i32("sampler")
    globals_ = [m.GlobalVariable(r.cstr("global name"), r.i32("global type"), r.cstr("global value"))
                for _ in range(r.count("global variable count", 12))]
    controls = [m.Control(r.cstr("control name"), r.i32("control vk"), r.i32("control player"))
                for _ in range(r.count("control count", 12))]
    winkey = r.boolean("disable windows key")
    keys = _keys(r, "global key")
    return m.AppProperties(
        name=name, window_width=width, window_height=height, eye_distance=eye, show_menu=show_menu,
        screensaver=screensaver, fps_mode=fps_mode, fps=fps, fullscreen=fullscreen, sampler=sampler,
        global_variables=globals_, controls=controls, disable_windows_key=winkey, global_keys=keys,
        simulate_shaders=r.i32("simulate shaders"),
        cap_path=r.cstr("cap path"),
        fps_in_caption=r.i32("fps in caption"),
        use_motion_blur=r.boolean("motion blur"),
        motion_blur_steps=r.i32("motion blur steps"),
        text_rendering_mode=r.i32("text rendering mode"),
        override_timedelta=r.boolean("override timedelta"),
        time_delta_override=r.f32("timedelta override"),
        caption=r.boolean("caption"),
        minimize_box=r.boolean("minimize box"),
        resizable=r.i32("resizable"),
        maximize_box=r.boolean("maximize box"),
        minimum_fps=r.f32("minimum fps"),
        preview_layout=r.i32("preview layout"),
        multisamples=r.i32("multisamples"),
        texture_loading=r.i32("texture loading"),
    )


# --- LEVELBLOCK ----------------------------------------------------------------
def _ace(r: Reader) -> Optional[m.AceTable]:
    start = r.pos
    flag = r.u8("ACE table flag")
    if flag == 0:
        return None
    if flag != 1:
        raise r.fail(f"ACE table flag is {flag}, expected 0 or 1", start)
    lists = []
    for kind in ("action", "condition", "expression"):
        lists.append([m.AceEntry(r.cstr(f"{kind} script name"), r.i32(f"{kind} parameter count"))
                      for _ in range(r.count(f"{kind} table size", 8))])
    return m.AceTable(*lists)


def _private_vars(r: Reader) -> list[m.PrivateVariable]:
    return [m.PrivateVariable(r.cstr("private variable name"), r.i32("private variable type"))
            for _ in range(r.count("private variable count", 8))]


def _oids(r: Reader, what: str) -> list[int]:
    return [r.i32(what) for _ in range(r.count(f"{what} count", 4))]


def _instance(r: Reader) -> m.Instance:
    return m.Instance(
        unused_key=r.i32("instance key"), x=r.i32("x"), y=r.i32("y"),
        width=r.i32("width"), height=r.i32("height"), angle=r.f32("angle"), filter=r.u32("filter"),
        oid=r.i32("instance oid"), instance_id=r.i32("instance id"), oid_repeat=r.i32("instance oid (repeat)"),
        private_values=[r.cstr("private value") for _ in range(r.count("private value count", 4))],
        data=r.blob("instance data"),
    )


def _layer(r: Reader, p: Profile) -> m.Layer:
    new = p.layer_clear_depth
    return m.Layer(
        id=r.i32("layer id"), name=r.cstr("layer name"), type=r.u8("layer type"),
        filter=r.u32("layer filter"), opacity=r.f32("opacity"), angle=r.f32("layer angle"),
        scroll_x_factor=r.f32("scroll x factor"), scroll_y_factor=r.f32("scroll y factor"),
        scroll_x=r.f32("scroll x"), scroll_y=r.f32("scroll y"),
        zoom_x_factor=r.f32("zoom x factor"), zoom_y_factor=r.f32("zoom y factor"),
        zoom_x=r.f32("zoom x"), zoom_y=r.f32("zoom y"),
        clear_background=r.u8("clear background"), background_color=r.u32("background color"),
        force_own_texture=r.boolean("force own texture"),
        sampler=r.i32("layer sampler"),
        enable_3d=r.boolean("enable 3d"),
        clear_depth_buffer=r.boolean("clear depth buffer") if new else None,
        instances=[_instance(r) for _ in range(r.count("instance count", 48))],
    )


def _layout(r: Reader, p: Profile) -> m.Layout:
    return m.Layout(
        width=r.i32("layout width"), height=r.i32("layout height"), name=r.cstr("layout name"),
        color=r.u32("layout color"), unbounded_scrolling=r.boolean("unbounded scrolling"),
        application_background=r.boolean("application background"),
        keys=_keys(r, "layout key"),
        layers=[_layer(r, p) for _ in range(r.count("layer count", 60))],
        image_ids=_oids(r, "layout image id"),
        texture_loading=r.i32("layout texture loading"),
    )


def _animations(r: Reader, depth: int = 0) -> list[m.Animation]:
    if depth > MAX_ANIMATION_DEPTH:
        raise r.fail("animations nested too deeply")
    out = []
    for _ in range(r.count("animation count", 41)):
        a = m.Animation(
            id=r.i32("animation id"), name=r.cstr("animation name"), tag=r.i32("animation tag"),
            speed=r.f32("animation speed"), is_angle=r.u8("is angle") == 1, angle=r.f32("animation angle"),
            repeat=r.i32("repeat"), repeat_to=r.i32("repeat to"), pingpong=r.i32("pingpong"),
            frames=[m.AnimationFrame(r.f32("frame time"), r.i32("frame image id"))
                    for _ in range(r.count("frame count", 8))],
            sub_animations=[],
        )
        a.sub_animations = _animations(r, depth + 1)
        out.append(a)
    return out


def parse_level(r: Reader, p: Profile) -> m.LevelData:
    types = []
    for _ in range(r.count("object type count", 38)):
        types.append(m.ObjectType(
            oid=r.i32("object type id"), name=r.cstr("object type name"),
            plugin_index=r.i32("plugin index"), is_global=r.u8("global flag") != 0,
            destroy_when=r.i32("destroy when"), private_variables=_private_vars(r),
            reserved=bytes(r.view(16, "reserved padding")), ace=_ace(r)))
    behaviors = []
    for _ in range(r.count("behavior count", 21)):
        behaviors.append(m.Behavior(
            owner_oid=r.i32("behavior owner"), plugin_index=r.i32("behavior plugin index"),
            index=r.i32("behavior ordinal"), name=r.cstr("behavior name"),
            data=r.blob("behavior data"), ace=_ace(r)))
    traits = [m.Trait(r.cstr("trait name"), _oids(r, "trait member"))
              for _ in range(r.count("trait count", 8))]
    families = [m.Family(r.cstr("family name"), _oids(r, "family member"), _private_vars(r))
                for _ in range(r.count("family count", 12))]
    containers = [_oids(r, "container member") for _ in range(r.count("container count", 4))]
    layouts = [_layout(r, p) for _ in range(r.count("layout count", 30))]
    return m.LevelData(types, behaviors, traits, families, containers, layouts, _animations(r))


# --- EVENTBLOCK ----------------------------------------------------------------
def _params(r: Reader) -> list[m.Parameter]:
    out: list[m.Parameter] = []
    for _ in range(r.count("parameter count", 4)):
        tokens: list[m.Token] = []
        for _ in range(r.count("token count", 4)):
            start = r.pos
            kind = r.i32("token type")
            if kind < 0 or kind >= len(m.TOKEN_NAMES):
                raise r.fail(f"unknown expression token type {kind}", start)
            if kind == m.T_INTEGER:
                tokens.append(m.Token(kind, r.i64("integer token")))
            elif kind == m.T_FLOAT:
                tokens.append(m.Token(kind, r.f64("float token")))
            elif kind in (m.T_STRINGLITERAL, m.T_IDENTIFIER, m.T_VARIABLENAME):
                tokens.append(m.Token(kind, r.cstr("token text")))
            else:
                tokens.append(m.Token(kind))
        out.append(tokens)
    return out


def _event_lines(r: Reader, depth: int) -> list[Union[m.Event, m.EventGroup]]:
    if depth > MAX_EVENT_DEPTH:
        raise r.fail("events nested too deeply")
    out: list[Union[m.Event, m.EventGroup]] = []
    while r.peek() in (BEGINEVENT, BEGINGROUP):
        if r.u8("marker") == BEGINGROUP:
            active = r.u8("group active flag") != 0
            name = r.cstr("group name")
            children = _event_lines(r, depth + 1)
            r.expect(ENDGROUP, "end group")
            out.append(m.EventGroup(active, name, children))
            continue
        line, sheet = r.i32("event line number"), r.i32("event sheet index")
        r.expect(BEGINCONDITIONS, "begin conditions")
        conditions = []
        while r.peek() == BEGINCONDITION:
            r.pos += 1
            conditions.append(m.Condition(
                oid=r.i32("condition oid"), cnd_id=r.i32("condition id"),
                negated=r.u8("negated flag") != 0, movement_id=r.i32("condition movement id"),
                params=_params(r)))
            r.expect(ENDCONDITION, "end condition")
        r.expect(ENDCONDITIONS, "end conditions")
        r.expect(BEGINACTIONS, "begin actions")
        actions = []
        while r.peek() == BEGINACTION:
            r.pos += 1
            actions.append(m.Action(
                oid=r.i32("action oid"), act_id=r.i32("action id"),
                movement_id=r.i32("action movement id"), params=_params(r)))
            r.expect(ENDACTION, "end action")
        r.expect(ENDACTIONS, "end actions")
        children = _event_lines(r, depth + 1)
        r.expect(ENDEVENT, "end event")
        out.append(m.Event(line, sheet, conditions, actions, children))
    return out


def parse_events(r: Reader, p: Profile) -> m.EventData:
    names = [r.cstr("event sheet name") for _ in range(r.count("event sheet count", 4))]
    lists = []
    for _ in range(r.count("event list count", 2)):
        r.expect(BEGINEVENTLIST, "begin event list")
        lists.append(_event_lines(r, 0))
        r.expect(ENDEVENTLIST, "end event list")
    return m.EventData(names, lists)


# --- IMAGEBLOCK ----------------------------------------------------------------
def parse_images(r: Reader, p: Profile) -> list[m.ImageEntry]:
    out = []
    for _ in range(r.count("image count", 32)):
        image_id = r.i32("image id")
        hx, hy = r.i32("hotspot x"), r.i32("hotspot y")
        points = [m.ActionPoint(r.i32("action point x"), r.i32("action point y"), r.cstr("action point name"))
                  for _ in range(r.count("action point count", 12))]
        data = r.view(r.i32("image data size"), "image data")
        mw, mh, pitch = r.i32("mask width"), r.i32("mask height"), r.i32("mask pitch")
        if mh < 0 or pitch < 0:
            raise r.fail(f"negative collision mask dimensions {mh} x {pitch}")
        mask = r.view(pitch * mh, "collision mask")
        out.append(m.ImageEntry(image_id, hx, hy, points, data, mw, mh, pitch, mask))
    return out


# --- HLSL ----------------------------------------------------------------------
def _effect_use(r: Reader) -> m.EffectUse:
    return m.EffectUse(
        shader_index=r.i32("effect shader index"), name=r.cstr("effect name"),
        disable_mode=r.i32("effect disable mode"),
        params=[m.EffectParameter(r.i32("effect parameter type"), r.cstr("effect parameter name"),
                                  r.cstr("effect variable name"), r.cstr("effect parameter value"))
                for _ in range(r.count("effect parameter count", 16))])


def _effect_uses(r: Reader) -> list[m.EffectUse]:
    return [_effect_use(r) for _ in range(r.count("effect count", 16))]


def parse_hlsl(r: Reader, p: Profile) -> m.HlslData:
    run_without_ps = r.i32("run without pixel shaders")
    shaders = []
    for _ in range(r.count("shader count", 19)):
        shaders.append(m.Shader(
            path=r.cstr("shader path"), major=r.i32("shader major version"),
            minor=r.i32("shader minor version"), cross_sampling=r.boolean("cross sampling"),
            border_mode=r.boolean("border mode"), fullscreen_mode=r.boolean("fullscreen mode"),
            bytecode=r.blob("shader bytecode")))
    objects = [m.ObjectEffects(r.i32("effect object oid"), _effect_uses(r))
               for _ in range(r.count("object effect count", 8))]
    layers = [m.LayerEffects(r.i32("effect layout index"), r.i32("effect layer index"), _effect_uses(r))
              for _ in range(r.count("layer effect count", 12))]
    transitions = [m.Transition(r.cstr("transition name"), _effect_use(r))
                   for _ in range(r.count("transition count", 20))]
    # When shader compilation failed the editor emits 100 zero ints instead (GenerateLayout).
    if r.remaining and not shaders and not any(r.data[r.pos:]):
        r.pos = len(r.data)
    return m.HlslData(run_without_ps, shaders, objects, layers, transitions)


# --- driver --------------------------------------------------------------------
_PARSERS: dict[str, tuple[str, Callable[[Reader, Profile], object]]] = {
    "APPBLOCK": ("app", parse_app),
    "LEVELBLOCK": ("level", parse_level),
    "EVENTBLOCK": ("events", parse_events),
    "IMAGEBLOCK": ("images", parse_images),
    "HLSL": ("hlsl", parse_hlsl),
}


#: Blocks whose layout is known to differ between runtime builds.
PROFILE_SENSITIVE = frozenset({"APPBLOCK", "LEVELBLOCK"})


def iter_strings(obj: object):
    """Yield every ``str`` inside a parsed model object (dataclasses, lists, tuples)."""
    stack = [obj]
    while stack:
        x = stack.pop()
        if isinstance(x, str):
            yield x
        elif isinstance(x, (list, tuple)):
            stack.extend(x)
        elif dataclasses.is_dataclass(x) and not isinstance(x, type):
            stack.extend(getattr(x, f.name) for f in dataclasses.fields(x))


def guess_encoding(container: Container) -> str:
    """Pick cp1251 or cp1252 for the project strings.

    Nothing in the export records the author's ANSI code page.  Cyrillic text is
    written almost entirely with bytes >= 0xC0 standing next to each other, while
    Western text has isolated accented letters among ASCII ones, so the share of
    high bytes that have a high-byte neighbour separates the two well.
    """
    paired = lonely = 0
    for name in ("APPBLOCK", "LEVELBLOCK", "EVENTBLOCK"):
        res = container.blocks.get(name)
        if res is None:
            continue
        try:
            value, _ = parse_block(name, res.data, "latin-1")
        except ParseError:
            continue
        for s in iter_strings(value):
            b = s.encode("latin-1", "replace")
            for i, ch in enumerate(b):
                if ch >= 0xC0:
                    near = (i > 0 and b[i - 1] >= 0xC0) or (i + 1 < len(b) and b[i + 1] >= 0xC0)
                    paired, lonely = paired + near, lonely + (not near)
    return "cp1251" if paired > 2 * lonely and paired >= 4 else "cp1252"


def parse_block(name: str, data: bytes, encoding: str = "cp1252") -> tuple[object, Profile]:
    """Parse one block, trying each profile.  Raises the most advanced ParseError."""
    _, parser = _PARSERS[name]
    best: Optional[ParseError] = None
    for profile in PROFILES:
        r = Reader(data, name, encoding)
        try:
            result = parser(r, profile)
            r.finish()
            return result, profile
        except ParseError as e:
            log.debug("%s: profile %s rejected: %s", name, profile.name, e)
            if best is None or e.offset > best.offset:
                best = e
        except RecursionError:
            best = ParseError(name, r.pos, "nesting too deep")
    assert best is not None
    raise best


def parse_project(container: Container, encoding: Optional[str] = None) -> m.Project:
    """Parse every data block of ``container``.  Never raises for bad block content.

    ``encoding`` is the ANSI code page of the strings; ``None`` guesses it.
    """
    if encoding is None:
        encoding = guess_encoding(container)
    project = m.Project(encoding=encoding)
    for name, (attr, _) in _PARSERS.items():
        res = container.blocks.get(name)
        if res is None:
            if name != "HLSL":
                project.issues.append(f"{name}: block is missing")
            continue
        data = res.data
        try:
            value, profile = parse_block(name, data, encoding)
        except ParseError as e:
            project.raw[name] = data
            project.issues.append(f"{e}; block kept as raw bytes")
            log.warning("%s", e)
            continue
        setattr(project, attr, value)
        project.profiles[name] = profile.name if name in PROFILE_SENSITIVE else "-"
    _cross_check(project, container)
    return project


def _cross_check(p: m.Project, c: Container) -> None:
    """Consistency between blocks; findings are reported, never fatal."""
    lv = p.level
    if lv is None:
        return
    n_plugins = len(c.plugins) or len(c.preview_plugin_names)
    for t in lv.object_types:
        if not 0 <= t.plugin_index < n_plugins:
            p.issues.append(f"object type {t.name!r}: plugin index {t.plugin_index} out of range")
    for b in lv.behaviors:
        if not 0 <= b.plugin_index < n_plugins:
            p.issues.append(f"behavior {b.name!r}: plugin index {b.plugin_index} out of range")
    if [t.oid for t in lv.object_types] != list(range(len(lv.object_types))):
        p.issues.append("object type ids are not the dense sequence 0..n-1")
    if p.events is not None and len(p.events.layout_events) != len(lv.layouts):
        p.issues.append(f"{len(p.events.layout_events)} event lists for {len(lv.layouts)} layouts")
    if p.images is not None:
        known = {i.id for i in p.images}
        missing = sorted({f.image_id for a in _walk_animations(lv.animations) for f in a.frames} - known)
        if missing:
            p.issues.append(f"animation frames reference unknown image ids: {missing[:20]}")


def _walk_animations(anims: list[m.Animation]):
    stack = list(anims)
    while stack:
        a = stack.pop()
        yield a
        stack.extend(a.sub_animations)
