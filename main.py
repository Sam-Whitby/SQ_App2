#!/usr/bin/env python3
"""
MIDI Staff Visualizer
Scrolling grand-staff display for MIDI input (file playback or live keyboard).
Notes drift from right to left; the vertical red "play line" marks when to play.
"""

import pygame
import mido
import sys
import os
import time
import threading
import subprocess
import platform
from typing import List, Optional

# ── Live MIDI (python-rtmidi) ─────────────────────────────────────────────────
try:
    import rtmidi as _rtmidi
    HAS_RTMIDI = True
except ImportError:
    HAS_RTMIDI = False

# ── Resource path (works both in dev and PyInstaller bundle) ──────────────────
def _res(rel: str) -> str:
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, rel)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), rel)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  LAYOUT & COLOURS                                                           ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

W, H  = 1440, 800
FPS   = 60

LINE_SP  = 15      # pixels between adjacent staff lines
NOTE_R   = 10      # horizontal radius of note-head ellipse
NOTE_H   = 7       # vertical radius of note-head ellipse
TAIL_H   = 5       # tail height in pixels
PLAY_X   = 210     # x-position of the play line
CLEF_W   = PLAY_X  # clef strip is [0 .. PLAY_X]

TREBLE_TOP = 115   # y of top staff line of treble (F5)
BASS_TOP   = 420   # y of top staff line of bass   (A3)

CTRL_Y   = H - 130  # top of control strip

# colours
BG       = (248, 245, 238)
STAVE_C  = ( 20,  20,  20)
CLEF_BG  = (230, 226, 214)
PLAY_C   = (210,  40,  40)
NOTE_FUT = ( 30,  70, 185)
NOTE_NOW = (220,  50,  20)
NOTE_PAS = (145, 145, 145)
TAIL_C   = ( 80, 120, 215)
CTRL_BG  = (215, 212, 204)
DARK     = ( 55,  55,  55)
LGRAY    = (170, 170, 170)
BLUE     = ( 50,  90, 205)
LBLUE    = (110, 150, 245)
WHITE    = (255, 255, 255)
BLACK    = (  0,   0,   0)
BRACE_C  = ( 30,  30,  30)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  PITCH UTILITIES                                                            ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

# Chromatic pitch-class → (diatonic degree 0-6, accidental '#'/None)
_C2D = {
    0:(0,None), 1:(0,'#'), 2:(1,None), 3:(1,'#'), 4:(2,None),
    5:(3,None), 6:(3,'#'), 7:(4,None), 8:(4,'#'), 9:(5,None),
    10:(5,'#'), 11:(6,None),
}

def _dia(midi: int) -> int:
    """Absolute diatonic step from C-1 (MIDI 0)."""
    return (midi // 12) * 7 + _C2D[midi % 12][0]

def _acc(midi: int) -> Optional[str]:
    return _C2D[midi % 12][1]

# Diatonic step of each clef's top staff line
_TREF = _dia(77)  # F5 — treble top
_BREF = _dia(57)  # A3 — bass   top

def note_y(midi: int, clef: str) -> float:
    """Centre-y of a note on its staff (in pixels)."""
    top = TREBLE_TOP if clef == 'treble' else BASS_TOP
    ref = _TREF      if clef == 'treble' else _BREF
    return top + (ref - _dia(midi)) * (LINE_SP / 2)

def use_treble(midi: int) -> bool:
    """Notes >= C4 (MIDI 60) go on the treble staff."""
    return midi >= 60


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  NOTE EVENT                                                                 ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class NoteEvent:
    __slots__ = ('pitch', 'start', 'end', 'vel')
    def __init__(self, pitch: int, start: float, vel: int = 80):
        self.pitch = pitch
        self.start = start
        self.end: Optional[float] = None
        self.vel  = vel


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  MIDI FILE PARSER                                                           ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def load_midi(path: str) -> List[NoteEvent]:
    """Parse a .mid file into NoteEvents with absolute wall-clock times (seconds)."""
    mid = mido.MidiFile(path)
    tpb = mid.ticks_per_beat

    # Merge all tracks, sort by absolute tick
    raw = []
    for track in mid.tracks:
        t = 0
        for msg in track:
            t += msg.time
            raw.append((t, msg))
    raw.sort(key=lambda x: x[0])

    notes: List[NoteEvent] = []
    active = {}      # (ch, note) → NoteEvent
    cur_tick  = 0
    cur_sec   = 0.0
    tempo     = 500_000  # µs per beat (120 BPM default)

    for abs_tick, msg in raw:
        delta = abs_tick - cur_tick
        if delta:
            cur_sec += mido.tick2second(delta, tpb, tempo)
        cur_tick = abs_tick

        if msg.type == 'set_tempo':
            tempo = msg.tempo
        elif msg.type == 'note_on' and msg.velocity > 0:
            k = (msg.channel, msg.note)
            if k in active:           # re-trigger: close old note
                active[k].end = cur_sec
            ne = NoteEvent(msg.note, cur_sec, msg.velocity)
            active[k] = ne
            notes.append(ne)
        elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
            k = (msg.channel, msg.note)
            if k in active:
                active[k].end = cur_sec
                del active[k]

    for ne in active.values():        # close any unterminated notes
        ne.end = ne.start + 0.5

    return notes


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  LIVE MIDI INPUT                                                            ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class LiveInput:
    def __init__(self):
        self.notes: List[NoteEvent] = []
        self._active = {}
        self._lock   = threading.Lock()
        self._port   = None
        self._t0     = 0.0

    def ports(self) -> List[str]:
        if not HAS_RTMIDI:
            return []
        try:
            m = _rtmidi.MidiIn()
            p = m.get_ports()
            del m
            return p
        except Exception:
            return []

    def start(self, idx: int = 0) -> bool:
        if not HAS_RTMIDI:
            return False
        try:
            self._port = _rtmidi.MidiIn()
            self._port.open_port(idx)
            self._port.set_callback(self._cb)
            self._t0 = time.time()
            return True
        except Exception as e:
            print(f'MIDI open error: {e}')
            return False

    def stop(self):
        if self._port:
            try:
                self._port.close_port()
            except Exception:
                pass
            self._port = None

    def _cb(self, event, _=None):
        msg, _ = event
        if len(msg) < 3:
            return
        t      = time.time() - self._t0
        status = msg[0] & 0xF0
        note   = msg[1]
        vel    = msg[2]
        with self._lock:
            if status == 0x90 and vel > 0:
                if note in self._active:
                    self._active[note].end = t
                ne = NoteEvent(note, t, vel)
                self._active[note] = ne
                self.notes.append(ne)
            elif status == 0x80 or (status == 0x90 and vel == 0):
                if note in self._active:
                    self._active[note].end = t
                    del self._active[note]

    def get_notes(self) -> List[NoteEvent]:
        with self._lock:
            return list(self.notes)

    def prune(self, before: float):
        """Discard notes that ended well before `before`."""
        with self._lock:
            self.notes = [n for n in self.notes
                          if n.end is None or n.end > before]


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  UI WIDGETS                                                                 ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class Slider:
    def __init__(self, x: int, y: int, w: int,
                 mn: float, mx: float, val: float, label: str):
        self.x, self.y, self.w = x, y, w
        self.mn, self.mx = mn, mx
        self.val   = val
        self.label = label
        self._drag = False

    def _frac(self) -> float:
        return (self.val - self.mn) / (self.mx - self.mn)

    @property
    def hx(self) -> int:
        return int(self.x + self._frac() * self.w)

    def handle(self, ev):
        tr = pygame.Rect(self.x, self.y - 4, self.w, 8)
        hh = pygame.Rect(self.hx - 10, self.y - 13, 20, 26)
        if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            if hh.collidepoint(ev.pos) or tr.collidepoint(ev.pos):
                self._drag = True
                self._move(ev.pos[0])
        elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
            self._drag = False
        elif ev.type == pygame.MOUSEMOTION and self._drag:
            self._move(ev.pos[0])

    def _move(self, mx: int):
        f = max(0.0, min(1.0, (mx - self.x) / self.w))
        self.val = self.mn + f * (self.mx - self.mn)

    def draw(self, surf, font):
        # track
        tr = pygame.Rect(self.x, self.y - 4, self.w, 8)
        pygame.draw.rect(surf, LGRAY, tr, border_radius=4)
        pygame.draw.rect(surf, BLUE, pygame.Rect(self.x, self.y - 4,
                          self.hx - self.x, 8), border_radius=4)
        # handle
        col = LBLUE if self._drag else BLUE
        pygame.draw.rect(surf, col,   (self.hx-10, self.y-13, 20, 26), border_radius=5)
        pygame.draw.rect(surf, DARK,  (self.hx-10, self.y-13, 20, 26), 1, border_radius=5)
        # label
        lbl = font.render(f'{self.label}: {self.val:.1f}s', True, DARK)
        surf.blit(lbl, (self.x, self.y - 34))


class Button:
    def __init__(self, x: int, y: int, w: int, h: int,
                 text: str, sticky: bool = False):
        self.rect    = pygame.Rect(x, y, w, h)
        self.text    = text
        self.sticky  = sticky
        self.on      = False
        self._hover  = False
        self.pressed = False

    def handle(self, ev):
        self.pressed = False
        if ev.type == pygame.MOUSEMOTION:
            self._hover = self.rect.collidepoint(ev.pos)
        elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            if self.rect.collidepoint(ev.pos):
                self.pressed = True
                if self.sticky:
                    self.on = not self.on

    def draw(self, surf, font):
        bg = BLUE  if self.on    else (LBLUE if self._hover else LGRAY)
        fg = WHITE if (self.on or self._hover) else DARK
        pygame.draw.rect(surf, bg,   self.rect, border_radius=7)
        pygame.draw.rect(surf, DARK, self.rect, 1, border_radius=7)
        ts = font.render(self.text, True, fg)
        surf.blit(ts, (self.rect.centerx - ts.get_width()  // 2,
                       self.rect.centery - ts.get_height() // 2))


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  APPLICATION                                                                ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class App:
    def __init__(self):
        pygame.init()
        self.surf  = pygame.display.set_mode((W, H))
        pygame.display.set_caption('MIDI Staff Visualizer')
        self.clock = pygame.time.Clock()

        # ── fonts ─────────────────────────────────────────────────────────────
        self.uf  = pygame.font.SysFont('Arial', 18)
        self.sf  = pygame.font.SysFont('Arial', 14)
        self.cf  = self._find_music_font(82)   # for clef glyphs
        self.af  = self._find_acc_font(24)     # ♯ ♭

        # ── state ─────────────────────────────────────────────────────────────
        self.mode: str = 'file'
        self.notes: List[NoteEvent] = []
        self.cur_t:  float = 0.0
        self._wall0: Optional[float] = None
        self.playing = False
        self.file_path: Optional[str] = None
        self.status = 'Load a MIDI file, or switch to Live mode'
        self._dialog_open = False

        self.live    = LiveInput()
        self._live_t0: Optional[float] = None

        # ── widgets ───────────────────────────────────────────────────────────
        BY = CTRL_Y + 18
        self.btn_mode = Button( 20, BY,      145, 38, 'Mode: File', sticky=True)
        self.btn_load = Button( 20, BY + 50, 145, 30, 'Load File...')
        self.btn_play = Button(180, BY,       80, 38, 'Play')
        self.btn_stop = Button(272, BY,       80, 38, 'Stop')
        self.sld = Slider(420, BY + 26, 340, 0.5, 10.0, 4.0, 'Lookahead')

        # auto-load bundled valse
        default = _res('valse_69_1_(c)dery.mid')
        if os.path.exists(default):
            self._load(default)

    # ── font helpers ──────────────────────────────────────────────────────────

    def _find_music_font(self, sz: int):
        """Return a font that can render the treble/bass clef Unicode chars."""
        for name in ('Apple Symbols', 'FreeSerif', 'DejaVu Serif',
                     'Symbola', 'Arial Unicode MS', None):
            try:
                f = (pygame.font.SysFont(name, sz) if name
                     else pygame.font.Font(None, sz))
                if f.render('𝄞', True, BLACK).get_width() > 5:
                    return f
            except Exception:
                pass
        return None

    def _find_acc_font(self, sz: int):
        """Return a font that renders ♯ and ♭."""
        for name in ('Apple Symbols', 'DejaVu Sans', 'Arial', None):
            try:
                f = (pygame.font.SysFont(name, sz) if name
                     else pygame.font.Font(None, sz))
                if f.render('♯', True, BLACK).get_width() > 3:
                    return f
            except Exception:
                pass
        return pygame.font.SysFont(None, sz)

    # ── file loading ──────────────────────────────────────────────────────────

    def _load(self, path: str):
        try:
            self.notes     = load_midi(path)
            self.file_path = path
            self.cur_t     = 0.0
            self.playing   = False
            self._wall0    = None
            fname = os.path.basename(path)
            self.status = f'Loaded: {fname}  ({len(self.notes)} notes)'
        except Exception as e:
            self.status = f'Error loading file: {e}'

    def _open_dialog(self):
        """Open a native file-open dialog (non-blocking)."""
        if self._dialog_open:
            return
        self._dialog_open = True

        def _run():
            path = None
            if platform.system() == 'Darwin':
                script = (
                    'set f to choose file with prompt "Select MIDI file" '
                    'of type {"mid", "midi", "public.data"}\n'
                    'return POSIX path of f'
                )
                r = subprocess.run(['osascript', '-e', script],
                                   capture_output=True, text=True)
                if r.returncode == 0:
                    path = r.stdout.strip()
            else:
                try:
                    import tkinter as tk
                    from tkinter import filedialog
                    root = tk.Tk()
                    root.withdraw()
                    path = filedialog.askopenfilename(
                        title='Select MIDI File',
                        filetypes=[('MIDI files', '*.mid *.midi'),
                                   ('All files', '*.*')])
                    root.destroy()
                except Exception:
                    pass
            if path:
                self._load(path)
            self._dialog_open = False

        threading.Thread(target=_run, daemon=True).start()

    # ── mode ──────────────────────────────────────────────────────────────────

    def _set_mode(self, mode: str):
        if mode == self.mode:
            return
        self.mode = mode
        if mode == 'live':
            self.btn_mode.text = 'Mode: Live'
            self.live.notes.clear()
            self._live_t0 = time.time()
            ports = self.live.ports()
            if not ports:
                self.status = 'No MIDI input devices found'
            elif self.live.start(0):
                self.status = f'Live MIDI: {ports[0]}'
            else:
                self.status = 'Could not open MIDI port'
        else:
            self.btn_mode.text = 'Mode: File'
            self.live.stop()
            self.status = ('File loaded — press Play or Space'
                           if self.notes else 'Load a MIDI file to begin')

    # ── playback ──────────────────────────────────────────────────────────────

    def _play(self):
        if self.notes and not self.playing:
            self.playing = True
            self._wall0  = time.time() - self.cur_t
            self.status  = 'Playing…'

    def _pause(self):
        self.playing = False
        self.status  = 'Paused'

    def _stop(self):
        self.playing = False
        self.cur_t   = 0.0
        self._wall0  = None
        self.live.notes.clear()
        self.status  = 'Stopped'

    # ── time update ───────────────────────────────────────────────────────────

    def _update(self):
        if self.mode == 'file' and self.playing and self._wall0 is not None:
            self.cur_t = time.time() - self._wall0
            if self.notes:
                end_t = max((n.end or n.start) for n in self.notes)
                if self.cur_t > end_t + 2.5:
                    self._stop()
        elif self.mode == 'live' and self._live_t0 is not None:
            self.cur_t = time.time() - self._live_t0
            self.live.prune(self.cur_t - 8.0)

    # ── note drawing ──────────────────────────────────────────────────────────

    def _nx(self, t: float, la: float) -> float:
        """Convert a note time to screen x-coordinate."""
        uw = W - PLAY_X - 12
        return PLAY_X + (t - self.cur_t) * (uw / la)

    def _draw_ledger_lines(self, x: float, ny: float, top_y: int):
        lw = NOTE_R + 7
        xi = int(x)
        bot_y = top_y + 4 * LINE_SP

        # below staff
        ly = bot_y + LINE_SP
        while ly <= ny + LINE_SP * 0.45:
            pygame.draw.line(self.surf, STAVE_C,
                             (xi - lw, round(ly)), (xi + lw, round(ly)), 1)
            ly += LINE_SP

        # above staff
        ly = top_y - LINE_SP
        while ly >= ny - LINE_SP * 0.45:
            pygame.draw.line(self.surf, STAVE_C,
                             (xi - lw, round(ly)), (xi + lw, round(ly)), 1)
            ly -= LINE_SP

    def _draw_notes(self, notes: List[NoteEvent], la: float):
        vis_start = self.cur_t - 3.5
        vis_end   = self.cur_t + la + 0.6

        for n in notes:
            end_t = n.end if n.end is not None else self.cur_t
            if end_t < vis_start or n.start > vis_end:
                continue

            clef  = 'treble' if use_treble(n.pitch) else 'bass'
            top_y = TREBLE_TOP if clef == 'treble' else BASS_TOP
            ny    = note_y(n.pitch, clef)
            sx    = self._nx(n.start, la)
            ex    = self._nx(end_t,   la)

            # ── tail (thick horizontal bar = note duration) ────────────────
            dsx = max(sx, float(CLEF_W))
            dex = min(ex, float(W - 12))
            if dex > dsx:
                pygame.draw.rect(
                    self.surf, TAIL_C,
                    (int(dsx), round(ny) - TAIL_H // 2,
                     int(dex - dsx), TAIL_H))

            # ── note head ─────────────────────────────────────────────────
            if CLEF_W - NOTE_R <= sx <= W:
                dt  = n.start - self.cur_t
                col = (NOTE_NOW if abs(dt) < 0.15
                       else NOTE_PAS if dt < 0
                       else NOTE_FUT)
                r = pygame.Rect(int(sx) - NOTE_R,
                                round(ny) - NOTE_H,
                                NOTE_R * 2, NOTE_H * 2)
                pygame.draw.ellipse(self.surf, col,   r)
                pygame.draw.ellipse(self.surf, BLACK, r, 1)

                # ledger lines
                self._draw_ledger_lines(sx, ny, top_y)

                # accidental (to the left of note head)
                acc = _acc(n.pitch)
                if acc and sx > CLEF_W - 35 and self.af:
                    sym  = '♯' if acc == '#' else '♭'
                    asurf = self.af.render(sym, True, NOTE_FUT)
                    self.surf.blit(
                        asurf,
                        (int(sx) - NOTE_R - asurf.get_width() - 2,
                         round(ny) - asurf.get_height() // 2))

    # ── staff drawing ─────────────────────────────────────────────────────────

    def _draw_staff(self, top_y: int):
        for i in range(5):
            y = top_y + i * LINE_SP
            pygame.draw.line(self.surf, STAVE_C, (CLEF_W, y), (W - 12, y), 1)

    def _draw_clefs(self):
        # Draw clef glyphs if the font supports them
        if self.cf:
            tc = self.cf.render('𝄞', True, STAVE_C)
            bc = self.cf.render('𝄢', True, STAVE_C)
            # position treble clef so bottom curl sits near E4 (line 1)
            self.surf.blit(tc, (CLEF_W - tc.get_width() - 6,
                                TREBLE_TOP - 32))
            self.surf.blit(bc, (CLEF_W - bc.get_width() - 6,
                                BASS_TOP - 10))
        else:
            f = pygame.font.SysFont('serif', 52)
            self.surf.blit(f.render('G', True, STAVE_C),
                           (CLEF_W - 50, TREBLE_TOP - 5))
            self.surf.blit(f.render('F', True, STAVE_C),
                           (CLEF_W - 50, BASS_TOP - 5))

    def _draw_brace(self):
        """Simple square-bracket brace connecting both staves."""
        y0 = TREBLE_TOP
        y1 = BASS_TOP + 4 * LINE_SP
        bx = 8
        pygame.draw.line(self.surf, BRACE_C, (bx, y0), (bx, y1), 3)
        pygame.draw.line(self.surf, BRACE_C, (bx, y0), (bx + 10, y0), 3)
        pygame.draw.line(self.surf, BRACE_C, (bx, y1), (bx + 10, y1), 3)

    # ── main loop ─────────────────────────────────────────────────────────────

    def run(self):
        while True:
            self.clock.tick(FPS)
            self._update()

            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    self.live.stop()
                    pygame.quit()
                    sys.exit()

                if ev.type == pygame.KEYDOWN:
                    if ev.key == pygame.K_SPACE and self.mode == 'file':
                        self._pause() if self.playing else self._play()
                    elif ev.key == pygame.K_r and self.mode == 'file':
                        self.cur_t = 0.0
                        if self.playing:
                            self._wall0 = time.time()

                self.sld.handle(ev)
                self.btn_mode.handle(ev)
                self.btn_load.handle(ev)
                self.btn_play.handle(ev)
                self.btn_stop.handle(ev)

                if self.btn_mode.pressed:
                    self._set_mode('live' if self.mode == 'file' else 'file')
                if self.btn_load.pressed and self.mode == 'file':
                    self._open_dialog()
                if self.btn_play.pressed and self.mode == 'file':
                    self._pause() if self.playing else self._play()
                if self.btn_stop.pressed:
                    self._stop()

            # ── render ────────────────────────────────────────────────────────
            self.surf.fill(BG)

            # clef background strip (left of play line)
            top_y0 = TREBLE_TOP - 60
            bot_y1 = BASS_TOP + 4 * LINE_SP + 60
            pygame.draw.rect(self.surf, CLEF_BG,
                             (0, top_y0, CLEF_W, bot_y1 - top_y0))
            pygame.draw.line(self.surf, LGRAY,
                             (CLEF_W, top_y0), (CLEF_W, bot_y1), 1)

            # staves (extend into clef area for the lines)
            self._draw_staff(TREBLE_TOP)
            self._draw_staff(BASS_TOP)

            # brace & clef symbols
            self._draw_brace()
            self._draw_clefs()

            # play line
            pygame.draw.line(self.surf, PLAY_C,
                             (PLAY_X, TREBLE_TOP - 55),
                             (PLAY_X, BASS_TOP + 4 * LINE_SP + 55), 2)

            # notes
            la = self.sld.val
            src = self.live.get_notes() if self.mode == 'live' else self.notes
            self._draw_notes(src, la)

            # ── control panel ─────────────────────────────────────────────────
            pygame.draw.rect(self.surf, CTRL_BG, (0, CTRL_Y, W, H - CTRL_Y))
            pygame.draw.line(self.surf, LGRAY, (0, CTRL_Y), (W, CTRL_Y), 1)

            self.btn_mode.draw(self.surf, self.uf)
            if self.mode == 'file':
                self.btn_load.draw(self.surf, self.uf)
                self.btn_play.draw(self.surf, self.uf)
                self.btn_stop.draw(self.surf, self.uf)

            self.sld.draw(self.surf, self.uf)

            # status + time
            self.surf.blit(self.sf.render(self.status, True, DARK),
                           (600, CTRL_Y + 12))
            ts = f't = {self.cur_t:.1f}s'
            self.surf.blit(self.sf.render(ts, True, DARK), (W - 130, CTRL_Y + 12))
            hints = 'Space: play/pause    R: restart'
            self.surf.blit(self.sf.render(hints, True, LGRAY), (600, CTRL_Y + 34))

            # mode indicator
            mode_lbl = ('● LIVE' if self.mode == 'live' else '● FILE')
            mode_col  = (210, 50, 50) if self.mode == 'live' else (50, 130, 50)
            self.surf.blit(self.uf.render(mode_lbl, True, mode_col),
                           (600, CTRL_Y + 56))

            pygame.display.flip()


def main():
    app = App()
    app.run()


if __name__ == '__main__':
    main()
