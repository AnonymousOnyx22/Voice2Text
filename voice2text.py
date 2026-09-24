try:
    import audioop
except ImportError:  # Python 3.13+ removed stdlib audioop
    try:
        import audioop_lts as audioop  # pip install audioop-lts
    except ImportError:
        import struct as _struct

        class _AudioOpFallback:
            @staticmethod
            def rms(data, width):
                if not data or width != 2:
                    return 0
                n = len(data) // 2
                if n == 0:
                    return 0
                vals = _struct.unpack("<%dh" % n, data[: n * 2])
                total = 0
                for v in vals:
                    total += v * v
                return int((total / n) ** 0.5)

        audioop = _AudioOpFallback()
import ctypes
from ctypes import wintypes
import json
import os
import queue
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

import speech_recognition as sr

CONFIG = os.path.join(os.path.expanduser("~"), ".voice2text.json")
MAX_SECONDS = 300
PHRASE_GAP = 0.6      # pause that ends a phrase, seconds
SWITCH_AFTER = 1.5    # silence before trying the next endpoint, seconds
UI_TICK = 70          # ms between repaints while recording
PHRASE_MAX = 8.0      # cut a phrase this long even without a pause
MIN_SPEECH = 350      # lowest speech threshold
MAX_SPEECH = 900      # highest: talking during calibration must not blind us
PROBE = 0.2           # seconds spent ranking each endpoint
BODY_LINES = 3        # transcript lines shown; the panel never grows
BAD_DEVICE = "This mic is not delivering audio properly. Pick another below."
MIC_BLOCKED = ("Couldn't open this microphone (error -9999). Check Settings "
               "> Privacy & security > Microphone, close apps holding the mic, "
               "or press tab to pick a different one.")

# --- panel geometry -----------------------------------------------------
W = 620
H_HEAD = 46       # icon + status row
H_BODY = 76       # transcript area
H_FOOT = 30       # hint strip
H_ROW = 26        # one device row in the picker
RADIUS = 12
PAD = 18
CLOSE = 13
KEY = "#ff00fe"   # transparency key colour (Windows only)

# --- palette ------------------------------------------------------------
BG = "#26241f"
BORDER = "#3b3831"
FG = "#ded9d1"
MUTED = "#8b857b"
FAINT = "#615c54"
HAIRLINE = "#35322c"
HILITE = "#302d27"
REC = "#d9705a"

FONT = "Consolas"

VIRTUAL = ("oculus", "vad", "sound mapper", "primary sound",
           "stereo mix", "wave", "voicemeeter")


def round_rect(canvas, x1, y1, x2, y2, r, **kw):
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, splinesteps=36, **kw)


def input_devices():
    """Capture devices, likely-virtual ones last.

    Enumerated once at startup: touching PortAudio is slow, so it must never
    happen while drawing or while switching microphones.
    """
    import pyaudio
    pa = None
    try:
        pa = pyaudio.PyAudio()
    except Exception:
        return []
    try:
        found = []
        try:
            count = pa.get_device_count()
        except Exception:
            count = 0
        for i in range(count):
            try:
                info = pa.get_device_info_by_index(i)
            except Exception:
                continue
            try:
                if info.get("maxInputChannels", 0) < 1:
                    continue
                name = info.get("name", f"Mic {i}")
                rate = int(info.get("defaultSampleRate", 44100) or 44100)
            except Exception:
                continue
            virtual = any(v in str(name).lower() for v in VIRTUAL)
            found.append({"index": i, "name": name,
                          "rate": rate,
                          "virtual": virtual})
    finally:
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                pass
    found.sort(key=lambda d: d["virtual"])
    try:
        return dedupe(found)
    except Exception:
        return found


def dedupe(devices):
    """One row per physical microphone.

    Windows lists the same device several times, and truncates the names at
    different lengths, so two rows are the same hardware when one name is a
    prefix of the other.
    """
    kept = []
    for dev in devices:
        name = dev["name"].strip()
        for other in kept:
            low, olow = name.lower(), other["name"].strip().lower()
            if low.startswith(olow) or olow.startswith(low):
                # The MME backend truncates names at 31 characters, so keep
                # whichever spelling is longest rather than whichever is first.
                if len(name) > len(other["name"].strip()):
                    other["name"] = name
                # Only one of these endpoints usually carries audio, and which
                # one cannot be told apart up front, so keep the rest as
                # fallbacks to try when the chosen one hears nothing.
                other["alts"].append((dev["index"], dev["rate"]))
                break
        else:
            dev["alts"] = [(dev["index"], dev["rate"])]
            kept.append(dev)
    return kept


def load_device():
    """The remembered microphone, by name.

    PortAudio renumbers devices between runs, so an index saved yesterday can
    point at a different microphone today; the name is what stays put.
    """
    try:
        with open(CONFIG) as fh:
            saved = json.load(fh)
        return saved.get("name"), saved.get("endpoint")
    except Exception:
        return None, None


def save_device(name, endpoint=None):
    try:
        with open(CONFIG, "w") as fh:
            json.dump({"name": name, "endpoint": endpoint}, fh)
    except Exception:
        pass


# --- pasting into whatever window had focus -----------------------------
VK_CONTROL, VK_V, KEYEVENTF_KEYUP = 0x11, 0x56, 0x02
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x4000
VK_SPACE = 0x20
HOTKEY_ID = 0xB001

# Tried in order; other apps may already own some of these.
HOTKEYS = [
    (MOD_CONTROL, VK_SPACE, "ctrl+space"),
    (MOD_CONTROL | MOD_SHIFT, VK_SPACE, "ctrl+shift+space"),
    (MOD_CONTROL | MOD_ALT, VK_SPACE, "ctrl+alt+space"),
    (MOD_CONTROL | MOD_ALT, 0x52, "ctrl+alt+r"),
    (MOD_CONTROL | MOD_SHIFT, 0x52, "ctrl+shift+r"),
    (MOD_CONTROL | MOD_ALT, 0xC0, "ctrl+alt+`"),
]


def make_non_activating(hwnd):
    """Stop the panel from ever taking focus.

    The whole point is to type into someone else's text box, so the panel must
    never become the foreground window - otherwise the box we want to paste
    into loses focus the moment the panel is clicked.
    """
    user32 = ctypes.windll.user32
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE)


def focus_window(hwnd):
    """Bring a window to the foreground.

    Windows refuses SetForegroundWindow across input queues, so attach to the
    target's input thread first; that is what makes the call succeed.
    """
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.AllowSetForegroundWindow(-1)

    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    this_thread = kernel32.GetCurrentThreadId()
    attached = user32.AttachThreadInput(this_thread, target_thread, True)
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)        # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetFocus(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(this_thread, target_thread, False)
    return user32.GetForegroundWindow() == hwnd


def send_paste(hwnd=None):
    """Send ctrl+v to whatever window currently has focus.

    The panel is non-activating, so the target normally still has focus and
    no window switching is needed; hwnd is only a fallback for the case where
    focus did move away.
    """
    user32 = ctypes.windll.user32
    if hwnd and user32.GetForegroundWindow() != hwnd:
        focus_window(hwnd)
        time.sleep(0.12)
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_V, 0, 0, 0)
    user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


class Recorder:
    def __init__(self, root):
        self.root = root
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=KEY)
        try:
            root.attributes("-transparentcolor", KEY)
        except tk.TclError:
            root.configure(bg=BG)

        self.recognizer = sr.Recognizer()
        self.devices = []
        self.device = None
        self.rate = 44100
        self.listening = False
        self.picking = False
        self.transcript = ""
        self.is_error = False
        self.note = ""
        self.level = 0
        self.worker = None
        self.moved = False
        self.hover_close = False
        self.hover = None
        self.hits = []
        self.target = None
        self.hotkey = ""
        self.words = []
        self.done = {}
        self.seq = 0
        self.next_seq = 0
        self.pending = 0
        self.session = 0  # bumps every recording; stale threads must ignore
        self.lock = threading.Lock()
        self.ui = queue.Queue()
        self.body_font = None
        self.foot_font = None
        self.trying = ""
        # Pick the mic right now: plain enumeration is instant (no audio
        # I/O), so there is no loading state. Endpoint probing happens
        # at record time, when it is actually needed.
        self.pick_initial_mic()

        self.canvas = tk.Canvas(root, width=W, height=self.height(),
                                highlightthickness=0, bd=0, bg=KEY)
        self.canvas.pack()

        self.draw()
        self.center()
        self.root.update_idletasks()
        try:
            self.hwnd = ctypes.windll.user32.GetAncestor(
                int(root.winfo_id()), 2)
            make_non_activating(self.hwnd)
        except Exception:
            self.hwnd = None
        self.register_hotkey()
        self.root.after(400, self.watch_focus)
        # One perpetual UI loop from the start. It drains the queue from
        # worker threads, so late transcriptions can never get stranded,
        # and a single bad callback can never kill future updates.
        self.root.after(UI_TICK, self.tick)

        root.bind("<Escape>", self.escape)
        root.bind("<space>", lambda e: self.toggle())
        root.bind("<Return>", lambda e: self.insert())
        root.bind("<Control-c>", lambda e: self.copy())
        root.bind("<Tab>", lambda e: self.toggle_picker())
        root.bind("<Down>", lambda e: self.step(1))
        root.bind("<Up>", lambda e: self.step(-1))
        self.canvas.bind("<Button-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.canvas.bind("<Motion>", self.motion)

    # -- geometry --------------------------------------------------------
    def height(self):
        if self.picking:
            # Always room for at least one row so the empty message
            # is visible instead of a zero-height middle.
            rows = min(max(len(self.devices), 1), 12)
            middle = rows * H_ROW
        else:
            middle = H_BODY
        return H_HEAD + middle + H_FOOT

    def center(self):
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - W) // 2
        self.root.geometry(f"{W}x{self.height()}+{x}+160")

    # -- drawing ---------------------------------------------------------
    def draw(self):
        c = self.canvas
        c.delete("all")
        self.hits = []

        h = self.height()
        if int(c["height"]) != h:
            c.config(height=h)
            self.root.geometry(f"{W}x{h}")

        round_rect(c, 1, 1, W - 1, h - 1, RADIUS, fill=BG,
                   outline=BORDER, width=1)

        self.mic(PAD + 8, H_HEAD / 2)
        c.create_text(PAD + 26, H_HEAD / 2 + 1, anchor="w", text=self.status(),
                      fill=FG if self.listening else MUTED, font=(FONT, 11))
        if self.listening:
            self.meter_bars(W - PAD - 34, H_HEAD / 2)
        self.close_x(W - PAD - 2, H_HEAD / 2)

        c.create_line(0, H_HEAD, W, H_HEAD, fill=HAIRLINE)
        c.create_line(0, h - H_FOOT, W, h - H_FOOT, fill=HAIRLINE)

        if self.picking:
            self.draw_devices()
        else:
            self.draw_body()
        self.draw_footer(h)

    def draw_devices(self):
        c = self.canvas
        if not self.devices:
            tid = c.create_text(PAD, H_HEAD + H_ROW / 2, anchor="w",
                                text="   No mic found - click here to rescan",
                                fill=MUTED, font=(FONT, 9))
            try:
                x1, y1, x2, y2 = c.bbox(tid)
                self.hits.append(("rescan", x1 - 4, y1 - 3, x2 + 4, y2 + 3))
            except Exception:
                pass
            return
        cur = self.current()
        cur_name = cur["name"] if cur else None
        # Window the list so the panel never grows off-screen; keep the
        # current mic visible. Up/Down still cycles the full list.
        visible = self.devices[:12]
        if cur and cur not in visible:
            try:
                idx = self.devices.index(cur)
            except ValueError:
                idx = 0
            start = max(0, min(idx - 6, len(self.devices) - 12))
            visible = self.devices[start:start + 12]
        for row, dev in enumerate(visible):
            y = H_HEAD + row * H_ROW
            active = cur_name is not None and dev["name"] == cur_name
            key = ("dev", dev["index"])
            if self.hover == key or active:
                round_rect(c, 6, y + 2, W - 6, y + H_ROW - 2, 6,
                           fill=HILITE, outline="")
            c.create_text(PAD, y + H_ROW / 2, anchor="w",
                          text=("=  " if active else "   ") + dev["name"],
                          fill=FG if active else MUTED, font=(FONT, 9))
            if dev["virtual"]:
                c.create_text(W - PAD, y + H_ROW / 2, anchor="e",
                              text="virtual", fill=FAINT, font=(FONT, 8))
            self.hits.append((key, 6, y, W - 6, y + H_ROW))

    def draw_footer(self, h):
        """Footer entries are clickable, so each records a hit box."""
        c = self.canvas
        items = [("record", f"{self.hotkey or 'click'}  record"),
                 ("copy", "copy"), ("mic", "mic")]
        x = PAD
        for key, label in items:
            colour = FG if self.hover == key else FAINT
            tid = c.create_text(x, h - H_FOOT / 2, anchor="w", text=label,
                                fill=colour, font=(FONT, 8))
            x1, y1, x2, y2 = c.bbox(tid)
            self.hits.append((key, x1 - 4, y1 - 3, x2 + 4, y2 + 3))
            x = x2 + 20

        label = self.note or self.short_name()
        if label:
            # Long device names would run into the footer buttons, so
            # truncate to whatever space is left and add an ellipsis.
            if self.foot_font is None:
                self.foot_font = tkfont.Font(family=FONT, size=8)
            full, max_w = label, W - PAD - x - 8
            while label and self.foot_font.measure(label + "\u2026") > max_w:
                label = label[:-1]
            if label != full:
                label = label.rstrip() + "\u2026"
            c.create_text(W - PAD, h - H_FOOT / 2, anchor="e", text=label,
                          fill=FAINT, font=(FONT, 8))

    def draw_body(self):
        """Show the tail of the transcript, clipped to a fixed number of
        lines so a long dictation scrolls instead of resizing the panel."""
        if self.body_font is None:
            self.body_font = tkfont.Font(family=FONT, size=10)

        width = W - PAD * 2
        text = self.visible_text(self.body_font, width)
        self.canvas.create_text(PAD, H_HEAD + H_BODY / 2, anchor="w",
                                text=text, width=width,
                                fill=FG if (self.words or self.transcript)
                                else MUTED,
                                font=(FONT, 10))

    def visible_text(self, font, width):
        if self.listening or self.words:
            words = list(self.words)
            if not words:
                return "Listening. Speak now."
            # Drop words off the front until the tail fits the fixed body.
            while words and self.line_count(font, width, words) > BODY_LINES:
                words.pop(0)
            return " ".join(words)
        return self.body()

    def line_count(self, font, width, words):
        lines, current = 1, ""
        for word in words:
            trial = f"{current} {word}".strip()
            if font.measure(trial) > width:
                lines += 1
                current = word
            else:
                current = trial
        return lines

    def meter_bars(self, right, cy):
        """Live input level, so a silent endpoint is obvious at a glance."""
        c = self.canvas
        bars = 10
        lit = min(bars, int((self.level / 2500) * bars))
        for i in range(bars):
            x = right - (bars - 1 - i) * 5
            hh = 2 + i * 0.9
            c.create_line(x, cy - hh, x, cy + hh,
                          fill=REC if i < lit else HAIRLINE, width=2)

    def close_x(self, cx, cy):
        c = self.canvas
        col = FG if self.hover_close else FAINT
        r = 4
        c.create_line(cx - r, cy - r, cx + r, cy + r, fill=col, width=1.3)
        c.create_line(cx - r, cy + r, cx + r, cy - r, fill=col, width=1.3)

    def on_close(self, x, y):
        return abs(x - (W - PAD - 2)) <= CLOSE and abs(y - H_HEAD / 2) <= CLOSE

    def mic(self, cx, cy):
        c = self.canvas
        col = REC if self.listening else MUTED
        c.create_rectangle(cx - 3, cy - 8, cx + 3, cy + 1,
                           outline=col, width=1.3)
        c.create_arc(cx - 6, cy - 5, cx + 6, cy + 6, start=200, extent=140,
                     style=tk.ARC, outline=col, width=1.3)
        c.create_line(cx, cy + 6, cx, cy + 9, fill=col, width=1.3)

    # -- text ------------------------------------------------------------
    def current(self):
        """The chosen microphone, found by any of its endpoints."""
        for dev in self.devices:
            alts = [i for i, _ in (dev.get("alts") or [])]
            if dev["index"] == self.device or self.device in alts:
                return dev
        return None

    def short_name(self):
        dev = self.current()
        if not dev:
            return "no mic"
        return dev["name"].split("(")[-1].rstrip(")")

    def status(self):
        if self.picking:
            return "Choose a microphone"
        if self.listening:
            if self.level >= 60 or self.trying:
                return "Recording."
            return "Recording.  no input - press tab to switch mic"
        if self.device is None or not self.devices:
            return "No mic found - click mic"
        return f"{self.hotkey or 'Click'} To Record"

    def body(self):
        if self.transcript:
            return self.transcript
        key = self.hotkey or "click record"
        return (f"Press {key} to record. The transcript is copied to your "
                "clipboard when you stop.")

    def refresh(self):
        self.draw()

    # -- devices ---------------------------------------------------------
    def pick_initial_mic(self):
        """Select the mic instantly at startup. No probing, no waiting."""
        try:
            self.devices = input_devices()
        except Exception:
            self.devices = []
        self.apply_saved_or_first()

    def apply_saved_or_first(self):
        """Saved mic if still plugged in, else first physical mic."""
        if not self.devices:
            self.device = None
            return
        try:
            saved, endpoint = load_device()
        except Exception:
            saved, endpoint = None, None
        pick = next((d for d in self.devices if d["name"] == saved), None)
        if pick is None:
            real = [d for d in self.devices if not d.get("virtual")]
            pick = real[0] if real else self.devices[0]
        self.device, self.rate = pick["index"], pick["rate"]
        for index, rate in pick.get("alts") or []:
            if index == endpoint:
                self.device, self.rate = index, rate
                break

    def rescan(self):
        """Re-list microphones instantly (plug/unplug). No probing."""
        if self.listening:
            return
        try:
            self.devices = input_devices()
        except Exception:
            self.devices = []
        if not self.devices:
            self.device = None
            self.picking = True
            self.note = "no mic found"
            self.refresh()
            return
        # Keep the current selection if still present, else saved/first.
        if self.current() is None:
            self.apply_saved_or_first()
        self.picking = True
        self.note = ""
        self.refresh()

    def toggle_picker(self):
        if self.listening:
            return
        if not self.devices:
            self.rescan()
            return
        self.picking = not self.picking
        self.refresh()

    def choose(self, index, close=True):
        """Switching is only a number change - no probing, so no stalling."""
        dev = next((d for d in self.devices if d["index"] == index), None)
        if dev is None:
            # self.device may currently be an alt endpoint; still allow
            # picking by matching any alt.
            for d in self.devices:
                alts = [i for i, _ in (d.get("alts") or [])]
                if index in alts:
                    dev = d
                    break
        if not dev:
            return
        self.device, self.rate = dev["index"], dev["rate"]
        self.is_error = False
        self.note = ""
        save_device(dev["name"], dev["index"])
        if close:
            self.picking = False
        self.refresh()

    def step(self, delta):
        if not self.devices:
            self.rescan()
            return
        if self.listening:
            return
        self.picking = True
        order = [d["index"] for d in self.devices]
        cur = self.current()
        cur_index = cur["index"] if cur else self.device
        i = order.index(cur_index) if cur_index in order else 0
        self.choose(order[(i + delta) % len(order)], close=False)

    # -- window ----------------------------------------------------------
    def shutdown(self):
        try:
            self.session += 1  # orphan any live worker/transcribe threads
            self.listening = False
        except Exception:
            pass
        try:
            ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_ID)
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def escape(self, _=None):
        if self.picking:
            self.picking = False
            self.refresh()
            return
        self.shutdown()

    def press(self, e):
        self.dx, self.dy = e.x, e.y
        self.moved = False

    def drag(self, e):
        if abs(e.x - self.dx) > 2 or abs(e.y - self.dy) > 2:
            self.moved = True
        self.root.geometry(
            f"+{self.root.winfo_x() + e.x - self.dx}"
            f"+{self.root.winfo_y() + e.y - self.dy}")

    def release(self, e):
        if self.moved:
            return
        if self.on_close(e.x, e.y):
            self.shutdown()
            return
        for key, x1, y1, x2, y2 in self.hits:
            if x1 <= e.x <= x2 and y1 <= e.y <= y2:
                self.activate(key)
                return
        if e.y < H_HEAD and not self.picking:
            self.toggle()

    def activate(self, key):
        if isinstance(key, tuple):
            self.choose(key[1])
        elif key == "record":
            self.toggle()
        elif key == "insert":
            self.insert()
        elif key == "copy":
            self.copy()
        elif key == "mic":
            if not self.devices:
                self.rescan()
            else:
                self.toggle_picker()
        elif key == "rescan":
            self.rescan()

    def motion(self, e):
        close = self.on_close(e.x, e.y)
        hover = None
        for key, x1, y1, x2, y2 in self.hits:
            if x1 <= e.x <= x2 and y1 <= e.y <= y2:
                hover = key
                break
        if close != self.hover_close or hover != self.hover:
            self.hover_close, self.hover = close, hover
            self.canvas.config(cursor="hand2" if (close or hover) else "")
            self.refresh()

    def register_hotkey(self):
        """ctrl+alt+space records from anywhere, since clicking the panel no
        longer focuses it and plain key bindings would not reach us."""
        try:
            user32 = ctypes.windll.user32
            for mods, vk, label in HOTKEYS:
                try:
                    if user32.RegisterHotKey(None, HOTKEY_ID,
                                             mods | MOD_NOREPEAT, vk):
                        self.hotkey = label
                        self.root.after(60, self.pump)
                        return
                except Exception:
                    continue
        except Exception:
            pass
        self.hotkey = ""

    def pump(self):
        """Drain hotkey messages; Tk has no idea about RegisterHotKey."""
        try:
            user32 = ctypes.windll.user32
            msg = wintypes.MSG()
            while user32.PeekMessageW(ctypes.byref(msg), None,
                                      WM_HOTKEY, WM_HOTKEY, 1):
                if msg.wParam == HOTKEY_ID:
                    try:
                        self.toggle()
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            try:
                self.root.after(60, self.pump)
            except Exception:
                pass

    def watch_focus(self):
        """Remember the last window that was not ours, to paste back into."""
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            try:
                mine = int(self.root.winfo_id())
            except Exception:
                mine = None
            try:
                root_of_mine = user32.GetAncestor(mine, 2) if mine else None
            except Exception:
                root_of_mine = None
            if hwnd and hwnd not in (mine, root_of_mine, self.hwnd):
                # Only remember usable windows; closed ones would make
                # paste silently do nothing later.
                if user32.IsWindow(hwnd):
                    self.target = hwnd
                # Drop a dead target so insert() reports it instead of
                # trying to focus a ghost window.
                elif self.target and not user32.IsWindow(self.target):
                    self.target = None
        except Exception:
            pass
        finally:
            try:
                self.root.after(400, self.watch_focus)
            except Exception:
                pass

    # -- recording -------------------------------------------------------
    def toggle(self):
        if self.listening:
            self.listening = False
            self.note = "finishing"
            self.refresh()
            return
        if self.device is None or not self.devices:
            self.note = "no mic"
            self.refresh()
            self.rescan()
            return
        if self.worker and self.worker.is_alive():
            # Previous take is still unwinding (closing the stream,
            # posting 'stopped'). Starting now would orphan that thread
            # and its 'stopped' would instantly kill the new take.
            self.note = "finishing"
            self.refresh()
            return
        self.session += 1
        sess = self.session
        self.picking = False
        self.listening = True
        self.transcript = ""
        self.is_error = False
        self.words = []
        self.done = {}
        self.seq = self.next_seq = self.pending = 0
        self.note = ""
        self.level = 0
        self.trying = ""
        self.refresh()
        self.worker = threading.Thread(target=self.record, args=(sess,),
                                       daemon=True)
        self.worker.start()
        # tick is perpetual; no need to (re)start it here.

    def record(self, sess):
        """Record, moving to the next endpoint if this one hears nothing.

        A microphone is exposed several times by Windows and typically only
        one of those endpoints carries audio, so a silent one is not an error
        to report - it is a cue to try the next.
        """
        endpoints = self.endpoints(sess)
        if not endpoints:
            self.post(self.fail,
                      "This microphone could not be opened. Press tab to "
                      "choose a different microphone.", sess)
            self.post(self.stopped, sess)
            return
        for position, (index, rate) in enumerate(endpoints):
            if sess != self.session or not self.listening:
                break
            last = position == len(endpoints) - 1
            self.post(self.set_trying,
                      "" if last else f"{position + 1}/{len(endpoints)}",
                      sess)
            if self.capture(index, rate, allow_switch=not last, sess=sess):
                if sess == self.session:
                    self.remember_endpoint(index, self.rate)
                break
            if sess != self.session or not self.listening:
                break
            self.post(self.switching, sess)
        else:
            if sess == self.session and not self.words:
                dev = self.current()
                name = dev["name"] if dev else "this microphone"
                self.post(self.fail,
                          f"No input from {name} on any of its "
                          f"{len(endpoints)} endpoints. Press tab to "
                          f"choose a different microphone.", sess)
        if sess == self.session:
            self.post(self.stopped, sess)

    def endpoints(self, sess=None):
        """The chosen microphone's endpoints, loudest first.

        Only one endpoint of a device usually carries audio, so each is
        sampled briefly and ranked by what it actually hears; broken ones are
        dropped rather than waited on.
        """
        dev = self.current()
        if not dev:
            return []
        alts = list(dev.get("alts") or [(dev["index"], dev["rate"])])
        ranked = []
        for index, rate in alts:
            if sess is not None and sess != self.session:
                return []
            if not self.listening:
                # Stopped while probing: abort early instead of opening
                # more endpoints that would delay the stop.
                break
            try:
                probed = self.probe(index, rate)
            except Exception:
                probed = None
            if probed is None:
                continue                       # will not open, or floods
            level, rate = probed
            ranked.append((level, index, rate))
        if not ranked:
            return alts                        # nothing probed cleanly; try all
        ranked.sort(key=lambda r: (-r[0], r[1] != self.device))
        return [(index, rate) for _, index, rate in ranked]

    def probe(self, index, rate):
        """Peak level and working rate, or None if the endpoint is broken.

        Intel SST and other built-in mics sometimes refuse their own
        reported rate with OSError -9999, so sibling rates are tried too;
        the rate that worked is reported back for capture to use.
        """
        for attempt in dict.fromkeys([rate, 48000, 44100, 16000]):
            peak = self.try_probe(index, attempt)
            if peak is not None:
                return peak, attempt
        return None

    def try_probe(self, index, rate):
        """Peak level over a short sample, or None if the endpoint is broken."""
        import pyaudio
        try:
            pa = pyaudio.PyAudio()
        except Exception:
            return None
        stream = None
        try:
            stream = pa.open(format=pyaudio.paInt16, channels=1, rate=rate,
                             input=True, input_device_index=index,
                             frames_per_buffer=1024)
            peak, reads, started = 0, 0, time.time()
            while time.time() - started < PROBE:
                try:
                    chunk = stream.read(1024, exception_on_overflow=False)
                except Exception:
                    return None
                try:
                    peak = max(peak, audioop.rms(chunk, 2))
                except Exception:
                    return None
                reads += 1
                if reads > (time.time() - started + 0.5) * rate / 1024 * 4:
                    return None                # floods instead of pacing
            return peak
        except Exception:
            return None
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            try:
                pa.terminate()
            except Exception:
                pass

    def set_trying(self, label, sess=None):
        if sess is not None and sess != self.session:
            return
        self.trying = label

    def remember_endpoint(self, index, rate):
        self.device, self.rate = index, rate
        dev = self.current()
        if dev:
            save_device(dev["name"], index)

    def switching(self, sess=None):
        if sess is not None and sess != self.session:
            return
        self.note = "trying next endpoint"
        self.refresh()

    @staticmethod
    def open_stream(pa, pyaudio, index, rate):
        """Open the endpoint, retrying common sample rates.

        Some drivers report a default rate they then refuse with OSError
        -9999; a sibling rate usually opens fine. Returns the stream and
        the rate that worked. Non--9999 errors are raised immediately.
        """
        last = OSError("could not open microphone")
        for attempt in dict.fromkeys([rate, 48000, 44100, 16000]):
            try:
                return pa.open(format=pyaudio.paInt16, channels=1,
                               rate=attempt, input=True,
                               input_device_index=index,
                               frames_per_buffer=1024), attempt
            except OSError as e:
                last = e
                if "-9999" not in str(e):
                    raise
            except Exception:
                raise
        raise last

    def capture(self, index, rate, allow_switch, sess=None):
        """Stream one endpoint. False means it never heard anything."""
        import pyaudio
        pa = None
        try:
            pa = pyaudio.PyAudio()
        except Exception as e:
            if sess is not None and sess != self.session:
                return True
            if allow_switch:
                return False
            self.post(self.fail, f"{type(e).__name__}: {e}",
                      sess if sess is not None else self.session)
            return True
        stream = None
        heard = False
        try:
            stream, rate = self.open_stream(pa, pyaudio, index, rate)
            if sess is not None and sess != self.session:
                return True
            self.rate = rate
            chunk = 1024 / rate
            floor = self.measure_floor(stream, rate, sess=sess)
            if sess is not None and sess != self.session:
                return True
            if not self.listening:
                return True
            speech = min(MAX_SPEECH, max(MIN_SPEECH, floor * 3))

            segment, quiet, voiced = [], 0.0, False
            started = time.time()
            while self.listening:
                if sess is not None and sess != self.session:
                    return True
                try:
                    data = stream.read(1024, exception_on_overflow=False)
                except Exception as e:
                    # Stream died mid-take (unplugged/sleep). Let the
                    # endpoint logic move on instead of hanging.
                    if "Input overflowed" in str(type(e).__name__) or \
                            "overflow" in str(e).lower():
                        continue
                    raise
                level = audioop.rms(data, 2)
                self.level = level
                segment.append(data)

                if level > speech:
                    heard = voiced = True
                    quiet = 0.0
                else:
                    quiet += chunk

                # A dead endpoint never rises above its own noise; give up on
                # it early so another can be tried while the user is talking.
                if allow_switch and not heard:
                    if time.time() - started > SWITCH_AFTER:
                        return False

                long_enough = len(segment) * chunk > PHRASE_MAX
                if voiced and (quiet > PHRASE_GAP or long_enough):
                    self.dispatch(b"".join(segment), rate, sess)
                    segment, quiet, voiced = [], 0.0, False
                elif not voiced and len(segment) * chunk > 1.5:
                    segment = segment[-int(0.3 / chunk):]   # drop dead air

                elapsed = time.time() - started
                if elapsed > MAX_SECONDS:
                    break
                # Some endpoints never block and flood buffers as fast as the
                # loop can read them; that audio is junk, so refuse it rather
                # than let it grow without bound.
                if len(segment) > (elapsed + 1) * rate / 1024 * 3:
                    if allow_switch:
                        return False
                    self.post(self.fail, BAD_DEVICE,
                              sess if sess is not None else self.session)
                    return True
            if voiced and (sess is None or sess == self.session):
                self.dispatch(b"".join(segment), rate, sess)
            return True
        except Exception as e:
            if sess is not None and sess != self.session:
                return True
            if allow_switch:
                return False
            if isinstance(e, OSError) and "-9999" in str(e):
                self.post(self.fail, MIC_BLOCKED,
                          sess if sess is not None else self.session)
            else:
                self.post(self.fail, f"{type(e).__name__}: {e}",
                          sess if sess is not None else self.session)
            return True
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            if pa is not None:
                try:
                    pa.terminate()
                except Exception:
                    pass

    def measure_floor(self, stream, rate, seconds=0.3, sess=None):
        """Room noise, so the speech threshold suits the actual microphone.

        The quietest chunk is used, not the average: recording often starts
        with the user already talking, and an average would then measure their
        voice as the noise floor and set a threshold nothing could cross.
        """
        levels = []
        for _ in range(max(1, int(rate / 1024 * seconds))):
            if sess is not None and sess != self.session:
                break
            if not self.listening:
                break
            try:
                levels.append(audioop.rms(
                    stream.read(1024, exception_on_overflow=False), 2))
            except Exception:
                break
            self.level = levels[-1] if levels else 0
        return min(levels) if levels else 150

    def dispatch(self, raw, rate, sess=None):
        """Transcribe one phrase without blocking capture."""
        if sess is None:
            sess = self.session
        with self.lock:
            self.pending += 1
            seq = self.seq
            self.seq += 1
        threading.Thread(target=self.transcribe, args=(raw, rate, seq, sess),
                         daemon=True).start()

    def transcribe(self, raw, rate, seq, sess):
        text = ""
        try:
            text = self.recognizer.recognize_google(
                sr.AudioData(raw, rate, 2))
        except sr.UnknownValueError:
            pass
        except sr.RequestError as e:
            # Only the live take reports network trouble; a stale thread
            # from a previous take must never kill the new recording.
            if sess == self.session:
                self.post(self.fail, f"Network unavailable: {e}", sess)
        except Exception as e:
            if sess == self.session:
                self.post(self.fail, f"{type(e).__name__}: {e}", sess)
        self.post(self.add_words, text, seq, sess)

    def add_words(self, text, seq, sess=None):
        """Phrases can come back out of order, so hold them until their turn."""
        if sess is not None and sess != self.session:
            return
        self.done[seq] = text
        while self.next_seq in self.done:
            part = self.done.pop(self.next_seq)
            self.next_seq += 1
            if part:
                self.words.extend(part.split())
        with self.lock:
            self.pending = max(0, self.pending - 1)
        if self.words:
            self.is_error = False
            self.transcript = " ".join(self.words)
        elif not self.is_error:
            # Empty phrase (silence / unintelligible): keep whatever real
            # words we already have, don't wipe the transcript.
            if not self.transcript:
                self.transcript = ""
        self.refresh()

    def stopped(self, sess=None):
        if sess is not None and sess != self.session:
            return
        self.listening = False
        self.trying = ""
        self.level = 0
        if self.transcript and not self.is_error:
            if self.to_clipboard(self.transcript):
                self.note = "copied"
            else:
                self.note = "copy failed - press copy"
        elif self.is_error and not self.transcript:
            self.note = ""
        self.refresh()

    def fail(self, message, sess=None):
        if sess is not None and sess != self.session:
            return
        self.listening = False
        self.trying = ""
        self.level = 0
        self.words = []
        with self.lock:
            self.pending = 0
        self.done = {}
        self.transcript = message
        self.is_error = True
        self.refresh()

    def post(self, fn, *args):
        """Hand work back to the main thread.

        Tk may only be touched from the thread that created it, so the capture
        and transcription threads queue their updates instead of calling into
        the widget directly.
        """
        try:
            self.ui.put((fn, args))
        except Exception:
            pass

    def tick(self):
        """Drain queued updates and repaint at a fixed, sane rate.

        Perpetual: always reschedules, so late transcriptions posted after
        a quiet period are still processed. One bad callback can never
        kill the loop.
        """
        try:
            dirty = False
            while True:
                try:
                    fn, args = self.ui.get_nowait()
                except queue.Empty:
                    break
                except Exception:
                    break
                try:
                    fn(*args)
                except Exception:
                    pass
                dirty = True
            try:
                if dirty or self.listening:
                    self.refresh()
            except Exception:
                pass
        finally:
            try:
                self.root.after(UI_TICK, self.tick)
            except Exception:
                pass

    # -- output ----------------------------------------------------------
    def copy(self):
        if not self.transcript or self.is_error:
            return
        if self.to_clipboard(self.transcript):
            self.note = "copied"
        else:
            self.note = "copy failed"
        self.refresh()

    def insert(self):
        """Paste the transcript into whatever box last had focus."""
        if not self.transcript or self.is_error:
            return
        try:
            import ctypes as _ct
            alive = _ct.windll.user32.IsWindow(self.target) if self.target \
                else False
        except Exception:
            alive = bool(self.target)
        if not self.target or not alive:
            self.note = "no target window"
            self.refresh()
            return
        if not self.to_clipboard(self.transcript):
            self.note = "copy failed"
            self.refresh()
            return
        self.note = "inserted"
        self.refresh()
        try:
            target = self.target
            self.root.after(10, lambda t=target: send_paste(t))
        except Exception:
            pass

    def to_clipboard(self, text):
        """Copy text; never let a clipboard error kill the UI loop."""
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            # update() flushes the clipboard claim, but it also pumps all
            # pending Tk events (re-entrancy). update_idletasks is enough
            # to keep geometry correct; the clipboard is claimed on
            # Windows without a full update().
            try:
                self.root.update_idletasks()
            except Exception:
                pass
            return True
        except Exception:
            return False


def already_running():
    """A second copy would open a duplicate panel and lose the race for the
    hotkey, so only the first instance is allowed to start."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW(None, False, "Voice2Text.SingleInstance")
    return kernel32.GetLastError() == 183          # ERROR_ALREADY_EXISTS


def enable_crisp_rendering():
    """Opt out of Windows' DPI virtualization.

    Without this, on any display scaling above 100% Windows renders the
    panel at 96 dpi and stretches the bitmap, which looks pixelated.
    Must run before tk.Tk() is created.
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor V2
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()  # pre-8.1
            except Exception:
                pass


if __name__ == "__main__":
    enable_crisp_rendering()
    if already_running():
        raise SystemExit(0)
    root = tk.Tk()
    Recorder(root)
    root.mainloop()
