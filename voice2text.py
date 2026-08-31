import audioop
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
    pa = pyaudio.PyAudio()
    try:
        found = []
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info["maxInputChannels"] < 1:
                continue
            name = info["name"]
            virtual = any(v in name.lower() for v in VIRTUAL)
            found.append({"index": i, "name": name,
                          "rate": int(info["defaultSampleRate"]),
                          "virtual": virtual})
    finally:
        pa.terminate()
    found.sort(key=lambda d: d["virtual"])
    return dedupe(found)


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
        self.lock = threading.Lock()
        self.ui = queue.Queue()
        self.body_font = None
        self.trying = ""

        self.canvas = tk.Canvas(root, width=W, height=self.height(),
                                highlightthickness=0, bd=0, bg=KEY)
        self.canvas.pack()

        self.draw()
        self.center()
        self.root.update_idletasks()
        self.hwnd = ctypes.windll.user32.GetAncestor(int(root.winfo_id()), 2)
        make_non_activating(self.hwnd)
        self.register_hotkey()
        self.root.after(30, self.load_devices)
        self.root.after(400, self.watch_focus)

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
        middle = len(self.devices) * H_ROW if self.picking else H_BODY
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
        for row, dev in enumerate(self.devices):
            y = H_HEAD + row * H_ROW
            active = dev["index"] == self.device
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
    def load_devices(self):
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        devices = input_devices()
        self.post(self._loaded, devices)
        self.root.after(0, self.tick)

    def _loaded(self, devices):
        self.devices = devices
        saved, endpoint = load_device()
        pick = next((d for d in devices if d["name"] == saved), None)
        if pick is None:
            pick = devices[0] if devices else None
        if pick:
            self.device, self.rate = pick["index"], pick["rate"]
            # Prefer the endpoint that actually worked last time.
            for index, rate in pick.get("alts") or []:
                if index == endpoint:
                    self.device, self.rate = index, rate
                    break
        self.refresh()

    def toggle_picker(self):
        if self.listening:
            return
        self.picking = not self.picking
        self.refresh()

    def choose(self, index, close=True):
        """Switching is only a number change - no probing, so no stalling."""
        dev = next((d for d in self.devices if d["index"] == index), None)
        if not dev:
            return
        self.device, self.rate = dev["index"], dev["rate"]
        save_device(dev["name"], dev["index"])
        if close:
            self.picking = False
        self.refresh()

    def step(self, delta):
        if not self.devices:
            return
        self.picking = True
        order = [d["index"] for d in self.devices]
        i = order.index(self.device) if self.device in order else 0
        self.choose(order[(i + delta) % len(order)], close=False)

    # -- window ----------------------------------------------------------
    def shutdown(self):
        try:
            ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_ID)
        except Exception:
            pass
        self.root.destroy()

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
            self.toggle_picker()

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
        user32 = ctypes.windll.user32
        for mods, vk, label in HOTKEYS:
            if user32.RegisterHotKey(None, HOTKEY_ID, mods | MOD_NOREPEAT, vk):
                self.hotkey = label
                self.root.after(60, self.pump)
                return
        self.hotkey = ""

    def pump(self):
        """Drain hotkey messages; Tk has no idea about RegisterHotKey."""
        user32 = ctypes.windll.user32
        msg = wintypes.MSG()
        while user32.PeekMessageW(ctypes.byref(msg), None,
                                  WM_HOTKEY, WM_HOTKEY, 1):
            if msg.wParam == HOTKEY_ID:
                self.toggle()
        self.root.after(60, self.pump)

    def watch_focus(self):
        """Remember the last window that was not ours, to paste back into."""
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            mine = int(self.root.winfo_id())
            root_of_mine = user32.GetAncestor(mine, 2)   # GA_ROOT
            if hwnd and hwnd not in (mine, root_of_mine):
                self.target = hwnd
        except Exception:
            pass
        self.root.after(400, self.watch_focus)

    # -- recording -------------------------------------------------------
    def toggle(self):
        if self.listening:
            self.listening = False
            self.note = "finishing"
            self.refresh()
            return
        if self.device is None:
            return
        if self.worker and self.worker.is_alive():
            return
        self.picking = False
        self.listening = True
        self.transcript = ""
        self.words = []
        self.done = {}
        self.seq = self.next_seq = self.pending = 0
        self.note = ""
        self.refresh()
        self.worker = threading.Thread(target=self.record, daemon=True)
        self.worker.start()
        self.root.after(UI_TICK, self.tick)

    def record(self):
        """Record, moving to the next endpoint if this one hears nothing.

        A microphone is exposed several times by Windows and typically only
        one of those endpoints carries audio, so a silent one is not an error
        to report - it is a cue to try the next.
        """
        endpoints = self.endpoints()
        for position, (index, rate) in enumerate(endpoints):
            last = position == len(endpoints) - 1
            self.post(self.set_trying,
                      "" if last else f"{position + 1}/{len(endpoints)}")
            if self.capture(index, rate, allow_switch=not last):
                self.remember_endpoint(index, rate)
                break
            if not self.listening:
                break
            self.post(self.switching)
        else:
            if not self.words:
                dev = self.current()
                name = dev["name"] if dev else "this microphone"
                self.post(self.fail,
                          f"No input from {name} on any of its "
                          f"{len(endpoints)} endpoints. Press tab to "
                          f"choose a different microphone.")
        self.post(self.stopped)

    def endpoints(self):
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
            level = self.probe(index, rate)
            if level is None:
                continue                       # will not open, or floods
            ranked.append((level, index, rate))
        if not ranked:
            return alts                        # nothing probed cleanly; try all
        ranked.sort(key=lambda r: (-r[0], r[1] != self.device))
        return [(index, rate) for _, index, rate in ranked]

    def probe(self, index, rate):
        """Peak level over a short sample, or None if the endpoint is broken."""
        import pyaudio
        pa = pyaudio.PyAudio()
        stream = None
        try:
            stream = pa.open(format=pyaudio.paInt16, channels=1, rate=rate,
                             input=True, input_device_index=index,
                             frames_per_buffer=1024)
            peak, reads, started = 0, 0, time.time()
            while time.time() - started < PROBE:
                peak = max(peak, audioop.rms(
                    stream.read(1024, exception_on_overflow=False), 2))
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
            pa.terminate()

    def set_trying(self, label):
        self.trying = label

    def remember_endpoint(self, index, rate):
        self.device, self.rate = index, rate
        dev = self.current()
        if dev:
            save_device(dev["name"], index)

    def switching(self):
        self.note = "trying next endpoint"
        self.refresh()

    def capture(self, index, rate, allow_switch):
        """Stream one endpoint. False means it never heard anything."""
        import pyaudio
        pa = pyaudio.PyAudio()
        stream = None
        heard = False
        try:
            stream = pa.open(format=pyaudio.paInt16, channels=1, rate=rate,
                             input=True, input_device_index=index,
                             frames_per_buffer=1024)
            chunk = 1024 / rate
            floor = self.measure_floor(stream, rate)
            speech = min(MAX_SPEECH, max(MIN_SPEECH, floor * 3))

            segment, quiet, voiced = [], 0.0, False
            started = time.time()
            while self.listening:
                data = stream.read(1024, exception_on_overflow=False)
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
                    self.dispatch(b"".join(segment), rate)
                    segment, quiet, voiced = [], 0.0, False
                elif not voiced and len(segment) * chunk > 1.5:
                    segment = segment[-int(0.3 / chunk):]   # drop dead air

                pass

                elapsed = time.time() - started
                if elapsed > MAX_SECONDS:
                    break
                # Some endpoints never block and flood buffers as fast as the
                # loop can read them; that audio is junk, so refuse it rather
                # than let it grow without bound.
                if len(segment) > (elapsed + 1) * rate / 1024 * 3:
                    if allow_switch:
                        return False
                    self.post(self.fail, BAD_DEVICE)
                    return True
            if voiced:
                self.dispatch(b"".join(segment), rate)
            return True
        except Exception as e:
            if allow_switch:
                return False
            self.post(self.fail, f"{type(e).__name__}: {e}")
            return True
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            pa.terminate()

    def measure_floor(self, stream, rate, seconds=0.3):
        """Room noise, so the speech threshold suits the actual microphone.

        The quietest chunk is used, not the average: recording often starts
        with the user already talking, and an average would then measure their
        voice as the noise floor and set a threshold nothing could cross.
        """
        levels = []
        for _ in range(max(1, int(rate / 1024 * seconds))):
            levels.append(audioop.rms(
                stream.read(1024, exception_on_overflow=False), 2))
        return min(levels)

    def dispatch(self, raw, rate):
        """Transcribe one phrase without blocking capture."""
        with self.lock:
            self.pending += 1
            seq = self.seq
            self.seq += 1
        threading.Thread(target=self.transcribe, args=(raw, rate, seq),
                         daemon=True).start()

    def transcribe(self, raw, rate, seq):
        text = ""
        try:
            text = self.recognizer.recognize_google(
                sr.AudioData(raw, rate, 2))
        except sr.UnknownValueError:
            pass
        except sr.RequestError as e:
            self.post(self.fail, f"Network unavailable: {e}")
        except Exception as e:
            self.post(self.fail, f"{type(e).__name__}: {e}")
        self.post(self.add_words, text, seq)

    def add_words(self, text, seq):
        """Phrases can come back out of order, so hold them until their turn."""
        self.done[seq] = text
        while self.next_seq in self.done:
            part = self.done.pop(self.next_seq)
            self.next_seq += 1
            if part:
                self.words.extend(part.split())
        with self.lock:
            self.pending -= 1
        self.transcript = " ".join(self.words)
        self.refresh()

    def stopped(self):
        self.listening = False
        self.trying = ""
        self.level = 0
        if self.transcript:
            self.to_clipboard(self.transcript)
            self.note = "copied"
        self.refresh()

    def fail(self, message):
        self.listening = False
        self.level = 0
        self.words = []
        self.transcript = message
        self.refresh()

    def post(self, fn, *args):
        """Hand work back to the main thread.

        Tk may only be touched from the thread that created it, so the capture
        and transcription threads queue their updates instead of calling into
        the widget directly.
        """
        self.ui.put((fn, args))

    def tick(self):
        """Drain queued updates and repaint at a fixed, sane rate."""
        dirty = False
        while True:
            try:
                fn, args = self.ui.get_nowait()
            except queue.Empty:
                break
            fn(*args)
            dirty = True
        if dirty or self.listening:
            self.refresh()
        if self.listening or not self.ui.empty():
            self.root.after(UI_TICK, self.tick)

    def finish(self, text, ok):
        self.listening = False
        self.level = 0
        self.transcript = text
        self.note = ""
        if ok and text:
            self.to_clipboard(text)
            self.note = "copied"
        self.refresh()

    # -- output ----------------------------------------------------------
    def copy(self):
        if self.transcript:
            self.to_clipboard(self.transcript)
            self.note = "copied"
            self.refresh()

    def insert(self):
        """Paste the transcript into whatever box last had focus."""
        if not self.transcript:
            return
        if not self.target:
            self.note = "no target window"
            self.refresh()
            return
        self.to_clipboard(self.transcript)
        self.note = "inserted"
        self.refresh()
        self.root.after(10, lambda: send_paste(self.target))

    def to_clipboard(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()


def already_running():
    """A second copy would open a duplicate panel and lose the race for the
    hotkey, so only the first instance is allowed to start."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW(None, False, "Voice2Text.SingleInstance")
    return kernel32.GetLastError() == 183          # ERROR_ALREADY_EXISTS


if __name__ == "__main__":
    if already_running():
        raise SystemExit(0)
    root = tk.Tk()
    Recorder(root)
    root.mainloop()
