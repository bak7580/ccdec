"""Data model of a Construct Classic project as stored in an exported .exe.

Field names follow FORMAT.md.  Everything here is plain data: no behaviour, no
references back into the input file except ``bytes`` payloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Union

# --- expression tokens (Runtime/CExpression.h :: enum ExpToken) ----------------
TOKEN_NAMES: tuple[str, ...] = (
    "NULL", "ANY_BINARY_OPERATOR", "ANY_FUNCTION", "ANY_VALUE",
    "INTEGER", "FLOAT", "STRINGLITERAL", "IDENTIFIER", "ARRAY", "VARIABLENAME",
    "LEFTBRACKET", "RIGHTBRACKET", "COMMA", "DOT", "LEFTCURLY", "RIGHTCURLY", "AT",
    "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE", "MOD", "POWER",
    "SIN", "COS", "TAN", "SQRT", "FUNC_STR", "FUNC_INT", "FUNC_FLOAT",
    "EQUAL", "LESS", "GREATER", "LESSEQUAL", "GREATEREQUAL", "NOTEQUAL",
    "CONDITIONAL", "COLON", "AND", "OR",
    "ASIN", "ACOS", "ATAN", "ABS", "EXP", "LN", "LOG10", "FLOOR", "CEIL", "ROUND",
    "RANDOM", "LEN",
)
T_INTEGER, T_FLOAT, T_STRINGLITERAL, T_IDENTIFIER, T_VARIABLENAME = 4, 5, 6, 7, 9


@dataclass
class Token:
    type: int
    value: Union[None, int, float, str] = None

    @property
    def name(self) -> str:
        return TOKEN_NAMES[self.type] if 0 <= self.type < len(TOKEN_NAMES) else f"UNKNOWN_{self.type}"


# --- APPBLOCK ------------------------------------------------------------------
@dataclass
class GlobalVariable:
    name: str
    type: int           # 0 = number, 1 = text
    value: str


@dataclass
class Control:
    name: str
    vk: int
    player: int


@dataclass
class KeyEntry:
    key: str
    type: int           # 0 = int payload, otherwise string payload
    value: Union[int, str]


@dataclass
class AppProperties:
    name: str
    window_width: int
    window_height: int
    eye_distance: Optional[float]      # absent in pre-r2 runtimes
    show_menu: int
    screensaver: bool
    fps_mode: int
    fps: int
    fullscreen: bool
    sampler: int
    global_variables: list[GlobalVariable]
    controls: list[Control]
    disable_windows_key: bool
    global_keys: list[KeyEntry]
    simulate_shaders: int
    cap_path: str                      # path of the original .cap on the author's disk
    fps_in_caption: int
    use_motion_blur: bool
    motion_blur_steps: int
    text_rendering_mode: int
    override_timedelta: bool
    time_delta_override: float
    caption: bool
    minimize_box: bool
    resizable: int
    maximize_box: bool
    minimum_fps: float
    preview_layout: int
    multisamples: int
    texture_loading: int


# --- LEVELBLOCK ----------------------------------------------------------------
@dataclass
class AceEntry:
    script_name: str
    param_count: int


@dataclass
class AceTable:
    """Script names and parameter counts a plugin exposes (always exported once per plugin)."""
    actions: list[AceEntry]
    conditions: list[AceEntry]
    expressions: list[AceEntry]


@dataclass
class PrivateVariable:
    name: str
    type: int


@dataclass
class ObjectType:
    oid: int
    name: str
    plugin_index: int               # index into Container.plugins
    is_global: bool
    destroy_when: int
    private_variables: list[PrivateVariable]
    reserved: bytes                 # 16 bytes, zero in every known export
    ace: Optional[AceTable]


@dataclass
class Behavior:
    owner_oid: int
    plugin_index: int
    index: int                      # ordinal among the owner's behaviors
    name: str
    data: bytes                     # plugin-defined property blob
    ace: Optional[AceTable]


@dataclass
class Trait:
    name: str
    oids: list[int]


@dataclass
class Family:
    name: str
    oids: list[int]
    private_variables: list[PrivateVariable]


@dataclass
class Instance:
    unused_key: int                 # garbage written by the exporter, ignored by the runtime
    x: int
    y: int
    width: int
    height: int
    angle: float
    filter: int
    oid: int
    instance_id: int
    oid_repeat: int
    private_values: list[str]
    data: bytes                     # plugin-defined property blob (same bytes as in the .cap)


@dataclass
class Layer:
    id: int
    name: str
    type: int
    filter: int
    opacity: float
    angle: float
    scroll_x_factor: float
    scroll_y_factor: float
    scroll_x: float
    scroll_y: float
    zoom_x_factor: float
    zoom_y_factor: float
    zoom_x: float
    zoom_y: float
    clear_background: int
    background_color: int
    force_own_texture: bool
    sampler: int
    enable_3d: bool
    clear_depth_buffer: Optional[bool]  # absent in 2009 builds
    instances: list[Instance]


@dataclass
class Layout:
    width: int
    height: int
    name: str
    color: int
    unbounded_scrolling: bool
    application_background: bool
    keys: list[KeyEntry]
    layers: list[Layer]             # runtime order: the editor's first layer is stored last
    image_ids: list[int]
    texture_loading: int


@dataclass
class AnimationFrame:
    time_factor: float
    image_id: int


@dataclass
class Animation:
    id: int
    name: str
    tag: int
    speed: float
    is_angle: bool
    angle: float
    repeat: int                     # -1 = forever
    repeat_to: int
    pingpong: int
    frames: list[AnimationFrame]
    sub_animations: list["Animation"]


@dataclass
class LevelData:
    object_types: list[ObjectType]
    behaviors: list[Behavior]
    traits: list[Trait]
    families: list[Family]
    containers: list[list[int]]
    layouts: list[Layout]
    animations: list[Animation]


# --- EVENTBLOCK ----------------------------------------------------------------
Parameter = List[Token]           # typing.List: plain list[...] needs Python 3.9 at run time


@dataclass
class Condition:
    oid: int                        # -1 = system object
    cnd_id: int
    negated: bool
    movement_id: int                # -1 = the object itself, >=0 behavior ordinal
    params: list[Parameter]


@dataclass
class Action:
    oid: int
    act_id: int
    movement_id: int                # <= -2 = effect (index -2 - movement_id)
    params: list[Parameter]


@dataclass
class Event:
    line_number: int
    sheet_index: int
    conditions: list[Condition]
    actions: list[Action]
    children: list[Union["Event", "EventGroup"]]


@dataclass
class EventGroup:
    active: bool
    name: str
    children: list[Union[Event, "EventGroup"]]


@dataclass
class EventData:
    sheet_names: list[str]
    layout_events: list[list[Union[Event, EventGroup]]]   # one list per layout


# --- IMAGEBLOCK ----------------------------------------------------------------
@dataclass
class ActionPoint:
    x: int
    y: int
    name: str


@dataclass
class ImageEntry:
    id: int
    hotspot_x: int
    hotspot_y: int
    action_points: list[ActionPoint]
    data: memoryview = field(repr=False)        # PNG (DirectX runtime) or BMP file bytes
    mask_width: int = 0
    mask_height: int = 0
    mask_pitch: int = 0
    mask: memoryview = field(default=memoryview(b""), repr=False)   # 1 bpp, MSB first

    @property
    def format(self) -> str:
        head = bytes(self.data[:8])
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if head.startswith(b"BM"):
            return "bmp"
        return "bin"


# --- HLSL ----------------------------------------------------------------------
@dataclass
class Shader:
    path: str                       # e.g. "Effects\\Blur.fx"
    major: int
    minor: int
    cross_sampling: bool
    border_mode: bool
    fullscreen_mode: bool
    bytecode: bytes = field(repr=False)


@dataclass
class EffectParameter:
    type: int
    name: str
    variable_name: str
    value: str


@dataclass
class EffectUse:
    shader_index: int
    name: str
    disable_mode: int
    params: list[EffectParameter]


@dataclass
class ObjectEffects:
    oid: int
    effects: list[EffectUse]


@dataclass
class LayerEffects:
    layout_index: int
    layer_index: int
    effects: list[EffectUse]


@dataclass
class Transition:
    name: str
    effect: EffectUse


@dataclass
class HlslData:
    run_without_ps: int
    shaders: list[Shader]
    object_effects: list[ObjectEffects]
    layer_effects: list[LayerEffects]
    transitions: list[Transition]


# --- whole project -------------------------------------------------------------
@dataclass
class Project:
    app: Optional[AppProperties] = None
    level: Optional[LevelData] = None
    events: Optional[EventData] = None
    images: Optional[list[ImageEntry]] = None
    hlsl: Optional[HlslData] = None
    #: code page the strings were decoded with
    encoding: str = "cp1252"
    #: layout profile each block was parsed with (see reader.PROFILES)
    profiles: dict[str, str] = field(default_factory=dict)
    #: blocks that could not be parsed, kept verbatim
    raw: dict[str, bytes] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
