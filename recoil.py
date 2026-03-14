#!/usr/bin/env python3
from __future__ import annotations

import ctypes
import json
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Final

try:
    import PySimpleGUI as sg
except ImportError:
    import FreeSimpleGUI as sg

from pynput import keyboard as pkeyboard
from pynput import mouse as pmouse

# ── Win32 constants ───────────────────────────────────────────────────────────

GWL_EXSTYLE        : Final[int] = -20
WS_EX_LAYERED      : Final[int] = 0x00080000
WS_EX_TRANSPARENT  : Final[int] = 0x00000020
TIMER_RESOLUTION   : Final[int] = 1          # ms — raises Windows scheduler tick from ~15 ms to 1 ms

# Setup SendInput for hardware-level mouse movement (works in games)
class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx",          ctypes.c_long),
        ("dy",          ctypes.c_long),
        ("mouseData",   ctypes.c_ulong),
        ("dwFlags",     ctypes.c_ulong),
        ("time",        ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    )

class INPUT_union(ctypes.Union):
    _fields_ = (("mi", MOUSEINPUT),)

class INPUT(ctypes.Structure):
    _fields_ = (
        ("type", ctypes.c_ulong),
        ("union", INPUT_union),
    )

_SendInput = ctypes.windll.user32.SendInput
_SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int)
_SendInput.restype = ctypes.c_uint

_INPUT_MOUSE         : Final[int] = 0
_MOUSEEVENTF_MOVE    : Final[int] = 0x0001


# ── Hotkey map ────────────────────────────────────────────────────────────────

_HOTKEY_MAP: Final[dict[str, pkeyboard.Key]] = {
    **{f"f{i}": getattr(pkeyboard.Key, f"f{i}") for i in range(1, 13)},
    "insert":   pkeyboard.Key.insert,
    "ins":      pkeyboard.Key.insert,
    "delete":   pkeyboard.Key.delete,
    "del":      pkeyboard.Key.delete,
    "home":     pkeyboard.Key.home,
    "end":      pkeyboard.Key.end,
    "pageup":   pkeyboard.Key.page_up,
    "prior":    pkeyboard.Key.page_up,    # tkinter keysym for PageUp
    "pagedown": pkeyboard.Key.page_down,
    "next":     pkeyboard.Key.page_down,  # tkinter keysym for PageDown
    "pause":    pkeyboard.Key.pause,
}

# Tkinter keysym overrides for display and modifier filtering
_KEYSYM_DISPLAY: Final[dict[str, str]]    = {"Prior": "PageUp", "Next": "PageDown"}
_KEYSYM_IGNORE:  Final[frozenset[str]]    = frozenset({
    "Shift_L", "Shift_R", "Control_L", "Control_R",
    "Alt_L",   "Alt_R",   "Super_L",   "Super_R",
    "Caps_Lock", "Num_Lock", "Scroll_Lock",
})

_CFG_PATH: Final[Path] = Path.home() / ".mouse_puller" / "config.json"

# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Profile:
    name:          str
    speed:         float
    distance:      float = 0.0
    burst_enabled: bool  = False
    burst_speed:   float = 0.0
    burst_dist:    float = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> Profile:
        return cls(
            name=          d["name"],
            speed=         float(d["speed"]),
            distance=      float(d.get("distance",    0)),
            burst_enabled= bool(d.get("burst_enabled", False)),
            burst_speed=   float(d.get("burst_speed", 0)),
            burst_dist=    float(d.get("burst_dist",  0)),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Config:
    hotkey:       str           = "F6"
    rmb_activate: bool          = True
    profiles:     list[Profile] = field(default_factory=lambda: [
        Profile("Slow",   50.0),
        Profile("Medium", 200.0),
        Profile("Fast",   500.0),
    ])

    @classmethod
    def load(cls) -> Config:
        try:
            raw = json.loads(_CFG_PATH.read_text())
            return cls(
                hotkey=       raw.get("hotkey",       "F6"),
                rmb_activate= raw.get("rmb_activate", True),
                profiles=     [Profile.from_dict(p) for p in raw.get("profiles", [])],
            )
        except (OSError, json.JSONDecodeError, KeyError):
            return cls()

    def save(self) -> None:
        _CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CFG_PATH.write_text(json.dumps({
            "hotkey":       self.hotkey,
            "rmb_activate": self.rmb_activate,
            "profiles":     [p.to_dict() for p in self.profiles],
        }, indent=2))

# ── Puller ────────────────────────────────────────────────────────────────────

class Puller:
    """Background thread that moves the mouse downward while LMB is held.

    Profile settings are written from the GUI thread; all are plain Python
    scalars, so assignments are atomic under the GIL — no lock needed.
    """

    __slots__ = (
        "speed", "distance", "burst_enabled", "burst_speed", "burst_dist",
        "lmb_held", "rmb_held", "rmb_activate", "_mc",
    )

    def __init__(self) -> None:
        self.speed         : float = 0.0
        self.distance      : float = 0.0
        self.burst_enabled : bool  = False
        self.burst_speed   : float = 0.0
        self.burst_dist    : float = 0.0
        self.lmb_held      : bool  = False
        self.rmb_held      : bool  = False
        self.rmb_activate  : bool  = False
        threading.Thread(target=self._run, daemon=True).start()

    @staticmethod
    def _move_mouse(dx: int, dy: int) -> None:
        """Send raw relative mouse movement via Win32 SendInput."""
        if not dx and not dy:
            return
        mi = MOUSEINPUT(dx, dy, 0, _MOUSEEVENTF_MOVE, 0, None)
        inp = INPUT(_INPUT_MOUSE, INPUT_union(mi=mi))
        _SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def apply(self, profile: Profile) -> None:
        """Push new profile settings into the puller (safe to call from any thread)."""
        self.speed         = profile.speed
        self.distance      = profile.distance
        self.burst_enabled = profile.burst_enabled
        self.burst_speed   = profile.burst_speed if profile.burst_enabled else 0.0
        self.burst_dist    = profile.burst_dist  if profile.burst_enabled else 0.0

    def _run(self) -> None:
        acc      = 0.0
        rem      = 0.0   # pixels remaining this press (0 = infinite)
        bpx      = 0.0   # burst pixels remaining
        done     = False  # True once distance is exhausted mid-hold
        prev_lmb = False
        t        = time.perf_counter()

        while True:
            now = time.perf_counter()
            dt  = now - t
            t   = now

            lmb    = self.lmb_held
            active = lmb and (not self.rmb_activate or self.rmb_held)

            if lmb and not prev_lmb:
                rem  = self.distance
                bpx  = self.burst_dist if (self.burst_enabled and self.burst_speed > 0) else 0.0
                acc  = 0.0
                done = False
            elif not lmb and prev_lmb:
                rem = bpx = acc = 0.0
                done = False

            prev_lmb = lmb

            if active and not done and self.speed > 0:
                spd  = self.burst_speed if bpx > 0 and self.burst_speed > 0 else self.speed
                acc += spd * dt
                if acc >= 1.0:
                    px   = int(acc)
                    acc -= px
                    if bpx > 0:
                        bpx = max(0.0, bpx - px)
                    if rem > 0:
                        px  = min(px, max(1, int(rem)))
                        rem -= px
                        if rem <= 0:
                            rem = bpx = acc = 0.0
                            done = True
                    self._move_mouse(0, px)

            time.sleep(0.001)

# ── UI theme ─────────────────────────────────────────────────────────────────

_CLR_BG     : Final[str]   = "#0f0f0f"
_CLR_SURFACE: Final[str]   = "#1c1c1c"
_CLR_ACCENT : Final[str]   = "#00e676"
_CLR_TEXT   : Final[str]   = "#cccccc"
_CLR_MUTED  : Final[str]   = "#4a4a4a"
_FONT       : Final[tuple] = ("Consolas", 10)
_FONT_BOLD  : Final[tuple] = ("Consolas", 10, "bold")

sg.theme_add_new("Cheat", {
    "BACKGROUND":     _CLR_BG,
    "TEXT":           _CLR_TEXT,
    "INPUT":          _CLR_SURFACE,
    "TEXT_INPUT":     _CLR_ACCENT,
    "SCROLL":         _CLR_SURFACE,
    "BUTTON":         (_CLR_BG, _CLR_ACCENT),
    "PROGRESS":       (_CLR_ACCENT, _CLR_SURFACE),
    "BORDER":         1,
    "SLIDER_DEPTH":   0,
    "PROGRESS_DEPTH": 0,
})
sg.theme("Cheat")

# ── Layout ────────────────────────────────────────────────────────────────────

def build_layout(cfg: Config) -> list:
    profiles_col = sg.Column([
        [sg.Listbox([], size=(20, 7), key="-LB-", enable_events=True, font=_FONT,
                    background_color=_CLR_SURFACE, text_color=_CLR_TEXT,
                    highlight_background_color=_CLR_ACCENT, highlight_text_color=_CLR_BG)],
        [sg.Button("+", key="-ADD-", size=(3, 1)), sg.Button("−", key="-DEL-", size=(3, 1)),
         sg.Button("↑", key="-UP-",  size=(3, 1)), sg.Button("↓", key="-DN-",  size=(3, 1))],
    ])

    editor_col = sg.Column([
        [sg.Text("NAME",          size=14, font=_FONT_BOLD, text_color=_CLR_MUTED),
         sg.Input(key="-NM-", size=18, font=_FONT)],
        [sg.Text("SPEED  px/s",   size=14, font=_FONT_BOLD, text_color=_CLR_MUTED),
         sg.Slider((1, 2000), key="-SPD-", orientation="h", size=(18, 14), enable_events=True,
                   trough_color=_CLR_SURFACE),
         sg.Input("100", key="-SPDN-", size=6, font=_FONT)],
        [sg.Text("DIST   px  0=∞", size=14, font=_FONT_BOLD, text_color=_CLR_MUTED),
         sg.Slider((0, 5000), key="-DST-", orientation="h", size=(18, 14), enable_events=True,
                   trough_color=_CLR_SURFACE),
         sg.Input("0", key="-DSTN-", size=6, font=_FONT)],
        [sg.Checkbox("BURST", key="-BCHK-", default=False, enable_events=True,
                     font=_FONT_BOLD, tooltip="Enable burst: faster initial pull"),
         sg.Input("0", key="-BS-", size=6, font=_FONT, disabled=True),
         sg.Text("px/s for", font=_FONT),
         sg.Input("0", key="-BD-", size=5, font=_FONT, disabled=True),
         sg.Text("px", font=_FONT)],
        [sg.Button("SAVE", key="-SAV-", expand_x=True, font=_FONT_BOLD)],
    ])

    return [
        [sg.Frame("HOTKEY", [[
             sg.Button(f"HOTKEY  {cfg.hotkey}  [click to bind]", key="-AHK-", font=_FONT_BOLD),
             sg.Text("press any key", font=_FONT, text_color=_CLR_MUTED),
         ]], font=_FONT_BOLD),
         sg.Checkbox("RMB GATE", key="-RMB-", default=cfg.rmb_activate,
                     enable_events=True, font=_FONT_BOLD,
                     tooltip="Pull only fires if RMB is also held")],
        [sg.Frame("PROFILES", [[profiles_col, editor_col]], font=_FONT_BOLD)],
        [sg.Text("", key="-ST-", font=_FONT, text_color=_CLR_ACCENT,
                 relief="flat", expand_x=True)],
    ]

# ── App ───────────────────────────────────────────────────────────────────────

class App:
    """Main application: owns the GUI windows, config, and puller."""

    def __init__(self) -> None:
        self.cfg     = Config.load()
        self.puller  = Puller()
        self.slot    = 0                                        # active profile index
        self.eidx    = 0                                        # profile open in editor
        self.hk_key  = (_HOTKEY_MAP.get(self.cfg.hotkey.lower())
                        or (pkeyboard.KeyCode.from_char(self.cfg.hotkey) if len(self.cfg.hotkey) == 1 else None))
        self.hk_flag = threading.Event()

        self._start_listeners()
        self._build_windows()

        self.puller.rmb_activate = self.cfg.rmb_activate
        self._refresh(keep=0)
        self._to_editor(0)
        self._apply_profile()

    # ── Setup ─────────────────────────────────────────────────────────────────

    def _start_listeners(self) -> None:
        def on_click(_x: int, _y: int, b: pmouse.Button, pressed: bool) -> None:
            if   b == pmouse.Button.left:  self.puller.lmb_held = pressed
            elif b == pmouse.Button.right: self.puller.rmb_held = pressed

        def on_key(key: pkeyboard.Key) -> None:
            if key == self.hk_key:
                self.hk_flag.set()

        pmouse.Listener(on_click=on_click).start()
        pkeyboard.Listener(on_press=on_key).start()

    def _build_windows(self) -> None:
        self.osd = sg.Window(
            "OSD",
            [[sg.Text("", key="-OSD-", font=("Consolas", 10, "bold"),
                       text_color="#00e676", background_color="#111111", pad=(8, 4))]],
            no_titlebar=True, keep_on_top=True, alpha_channel=0.88,
            location=(12, 12), background_color="#111111", finalize=True,
        )
        self._make_click_through(self.osd)
        self.win = sg.Window("MOUSE PULLER", build_layout(self.cfg), finalize=True, font=_FONT)

    def _on_bind_keypress(self, event) -> None:
        if event.keysym in _KEYSYM_IGNORE:
            return
        self.win.TKroot.unbind("<KeyPress>")
        keysym  = event.keysym
        display = _KEYSYM_DISPLAY.get(keysym, keysym if len(keysym) > 1 else keysym.upper())
        key     = (_HOTKEY_MAP.get(keysym.lower())
                   or (pkeyboard.KeyCode.from_char(keysym) if len(keysym) == 1 else None))
        if key is None:
            self.win["-AHK-"].update(f"HOTKEY  {self.cfg.hotkey}  [click to bind]")
            self.win["-ST-"].update("unsupported key  ·  try again")
            return
        self.cfg.hotkey = display
        self.hk_key     = key
        self.win["-AHK-"].update(f"HOTKEY  {display}  [click to bind]")
        self.win["-ST-"].update(f"hotkey  →  {display}")

    @staticmethod
    def _make_click_through(window: sg.Window) -> None:
        hwnd = ctypes.windll.user32.GetParent(window.TKroot.winfo_id())
        ex   = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED | WS_EX_TRANSPARENT)

    # ── Profile helpers ───────────────────────────────────────────────────────

    def _profile_names(self) -> list[str]:
        return [
            ("► " if i == self.slot else "  ") + p.name
            for i, p in enumerate(self.cfg.profiles)
        ]

    def _apply_profile(self) -> None:
        p = self.cfg.profiles[self.slot]
        self.puller.apply(p)
        self.osd["-OSD-"].update(f"● {p.name}")
        self.win["-ST-"].update(f"▸ {p.name}  ·  active")

    def _to_editor(self, i: int) -> None:
        p = self.cfg.profiles[i]
        self.eidx = i
        self.win["-NM-"].update(p.name)
        self.win["-SPD-"].update(int(p.speed))
        self.win["-SPDN-"].update(str(int(p.speed)))
        self.win["-DST-"].update(int(p.distance))
        self.win["-DSTN-"].update(str(int(p.distance)))
        self.win["-BCHK-"].update(p.burst_enabled)
        self.win["-BS-"].update(str(int(p.burst_speed)), disabled=not p.burst_enabled)
        self.win["-BD-"].update(str(int(p.burst_dist)),  disabled=not p.burst_enabled)

    def _refresh(self, keep: int | None = None) -> None:
        self.win["-LB-"].update(self._profile_names())
        if keep is not None:
            self.win["-LB-"].update(set_to_index=keep, scroll_to_index=keep)

    # ── Event handlers ────────────────────────────────────────────────────────

    def _next_profile(self) -> None:
        if len(self.cfg.profiles) > 1:
            self.slot = (self.slot + 1) % len(self.cfg.profiles)
            self._apply_profile()
            self._refresh()

    def _on_listbox(self) -> None:
        sel = self.win["-LB-"].Widget.curselection()
        if sel:
            self._to_editor(sel[0])

    def _on_add(self) -> None:
        p = Profile(name=f"Profile {len(self.cfg.profiles) + 1}", speed=100.0)
        self.cfg.profiles.append(p)
        i = len(self.cfg.profiles) - 1
        self._refresh(keep=i)
        self._to_editor(i)

    def _on_delete(self) -> None:
        if len(self.cfg.profiles) <= 1:
            return
        self.cfg.profiles.pop(self.eidx)
        self.slot = min(self.slot, len(self.cfg.profiles) - 1)
        new = min(self.eidx, len(self.cfg.profiles) - 1)
        self._refresh(keep=new)
        self._to_editor(new)
        self._apply_profile()

    def _on_move(self, direction: int) -> None:
        i, j = self.eidx, self.eidx + direction
        if not 0 <= j < len(self.cfg.profiles):
            return
        ps = self.cfg.profiles
        ps[i], ps[j] = ps[j], ps[i]
        if   self.slot == i: self.slot = j
        elif self.slot == j: self.slot = i
        self.eidx = j
        self._refresh(keep=j)

    def _on_save(self, vals: dict) -> None:
        def _parse_int(v: str, fallback: int = 0) -> int:
            try:
                return int(float(v))
            except (ValueError, TypeError):
                return fallback

        i = self.eidx
        burst_on = vals["-BCHK-"]
        p = Profile(
            name=          vals["-NM-"].strip() or f"Profile {i + 1}",
            speed=         max(1.0,  _parse_int(vals["-SPDN-"], int(vals["-SPD-"]))),
            distance=      max(0.0,  _parse_int(vals["-DSTN-"], int(vals["-DST-"]))),
            burst_enabled= burst_on,
            burst_speed=   max(0.0,  _parse_int(vals["-BS-"])) if burst_on else 0.0,
            burst_dist=    max(0.0,  _parse_int(vals["-BD-"]))  if burst_on else 0.0,
        )
        self.cfg.profiles[i] = p
        if self.slot == i:
            self._apply_profile()
        self._refresh(keep=i)
        self.win["-ST-"].update(f"✓  {p.name}  saved")

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        while True:
            ev, vals = self.win.read(timeout=50)


            if self.hk_flag.is_set():
                self.hk_flag.clear()
                self._next_profile()

            if   ev == sg.WIN_CLOSED: break
            elif ev == "-LB-":   self._on_listbox()
            elif ev == "-SPD-":  self.win["-SPDN-"].update(str(int(vals["-SPD-"])))
            elif ev == "-DST-":  self.win["-DSTN-"].update(str(int(vals["-DST-"])))
            elif ev == "-BCHK-":
                on = vals["-BCHK-"]
                self.win["-BS-"].update(disabled=not on)
                self.win["-BD-"].update(disabled=not on)
            elif ev == "-ADD-":  self._on_add()
            elif ev == "-DEL-":  self._on_delete()
            elif ev == "-UP-":   self._on_move(-1)
            elif ev == "-DN-":   self._on_move(+1)
            elif ev == "-SAV-":  self._on_save(vals)
            elif ev == "-RMB-":
                self.puller.rmb_activate = vals["-RMB-"]
                self.cfg.rmb_activate    = vals["-RMB-"]
            elif ev == "-AHK-":
                self.win["-AHK-"].update("LISTENING...")
                self.win["-ST-"].update("press any key to bind")
                self.win.TKroot.bind("<KeyPress>", self._on_bind_keypress, add="+")

        self.cfg.save()
        self.osd.close()
        self.win.close()
        ctypes.windll.winmm.timeEndPeriod(TIMER_RESOLUTION)
        sys.exit(0)

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ctypes.windll.winmm.timeBeginPeriod(TIMER_RESOLUTION)
    App().run()
