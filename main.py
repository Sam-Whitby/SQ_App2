#!/usr/bin/env python3
"""
MIDI Staff Visualizer
Scrolling grand-staff display for MIDI input (file playback or live keyboard).
Notes drift from right to left; the vertical red "play line" marks when to play.
"""

import math
import pygame
import mido
import sys
import os
import time
import threading
import subprocess
import platform
from typing import List, Optional

try:
    import pygame.gfxdraw
    HAS_GFXDRAW = True
except ImportError:
    HAS_GFXDRAW = False

try:
    import rtmidi as _rtmidi
    HAS_RTMIDI = True
except ImportError:
    HAS_RTMIDI = False

def _res(rel: str) -> str:
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, rel)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), rel)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  LAYOUT & COLOURS                                                           ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

W, H   = 1440, 820
FPS    = 60

# Staff geometry — all derived from LINE_SP so everything scales together
LINE_SP  = 16        # pixels between adjacent staff lines
STAFF_H  = 4 * LINE_SP          # height of a 5-line staff (top to bottom line)
GAP      = 2 * LINE_SP          # gap between bottom of treble and top of bass
                                 # (middle C sits in the centre of this gap)

# Vertical placement — centre the grand staff in the music area
MUSIC_H  = H - 140              # music area height (above controls)
GRAND_H  = STAFF_H * 2 + GAP   # full height of grand staff
TREBLE_TOP = (MUSIC_H - GRAND_H) // 2 + 10   # y of F5 (treble top line)
BASS_TOP   = TREBLE_TOP + STAFF_H + GAP       # y of A3 (bass top line)

# Note dimensions
NOTE_W   = round(LINE_SP * 0.70)   # horizontal radius of note-head
NOTE_H_R = round(LINE_SP * 0.45)   # vertical radius of note-head
TAIL_T   = max(3, round(LINE_SP * 0.22))   # tail thickness

PLAY_X   = 210          # x of play line
CLEF_W   = PLAY_X       # clef strip [0 .. PLAY_X]
CTRL_Y   = H - 140      # top of control strip

# Colours — music elements are all pure black on a light background
BLACK    = (  0,   0,   0)
WHITE    = (255, 255, 255)
BG       = (252, 250, 245)   # slightly warm white
CLEF_BG  = (238, 235, 226)
STAFF_C  = BLACK
NOTE_C   = BLACK
TAIL_C   = BLACK
LEDGER_C = BLACK
ACC_C    = BLACK
PLAY_C   = (200,  30,  30)   # red play line

# UI colours
CTRL_BG  = (215, 212, 205)
DARK     = ( 50,  50,  50)
LGRAY    = (165, 165, 165)
BLUE     = ( 45,  90, 205)
LBLUE    = (110, 148, 245)
BRACE_C  = BLACK


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  PITCH UTILITIES                                                            ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

# Chromatic pitch-class → (diatonic degree 0-6, accidental '#' or None)
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

_TREF = _dia(77)   # F5 — treble top line
_BREF = _dia(57)   # A3 — bass   top line

def note_y(midi: int, clef: str) -> float:
    """Centre-y of a note on its staff, in pixels."""
    top = TREBLE_TOP if clef == 'treble' else BASS_TOP
    ref = _TREF      if clef == 'treble' else _BREF
    return top + (ref - _dia(midi)) * (LINE_SP / 2)

def use_treble(midi: int) -> bool:
    return midi >= 60


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  PRE-RENDERED ACCIDENTAL SURFACES                                           ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _make_sharp(lsp: int) -> pygame.Surface:
    """Render a ♯ symbol into a transparent surface."""
    sw = round(lsp * 0.95)
    sh = round(lsp * 1.65)
    surf = pygame.Surface((sw, sh), pygame.SRCALPHA)

    x1 = round(sw * 0.30)
    x2 = round(sw * 0.70)

    # vertical lines (full height)
    pygame.draw.line(surf, BLACK, (x1, 0), (x1, sh - 1), 1)
    pygame.draw.line(surf, BLACK, (x2, 0), (x2, sh - 1), 1)

    # two horizontal bars (slightly angled: rises left to right)
    by1 = round(sh * 0.30)
    by2 = round(sh * 0.62)
    bar_w = sw - 1
    slope = 2  # pixels of rise across full width

    # top bar
    for dy_px in range(2):   # 2px thick
        pygame.draw.line(surf, BLACK,
                         (0,    by1 + 1 + dy_px),
                         (bar_w, by1 - 1 + dy_px), 1)
    # bottom bar
    for dy_px in range(2):
        pygame.draw.line(surf, BLACK,
                         (0,    by2 + 1 + dy_px),
                         (bar_w, by2 - 1 + dy_px), 1)
    return surf


def _make_flat(lsp: int) -> pygame.Surface:
    """Render a ♭ symbol into a transparent surface."""
    fw = round(lsp * 0.72)
    fh = round(lsp * 2.05)
    surf = pygame.Surface((fw, fh), pygame.SRCALPHA)

    stem_x = round(fw * 0.18)

    # vertical stem
    pygame.draw.line(surf, BLACK, (stem_x, 0), (stem_x, fh - 1), 1)

    # belly — filled polygon approximating the D-shape
    belly_top_y  = round(fh * 0.38)
    belly_bot_y  = round(fh * 0.82)
    belly_cx     = stem_x
    belly_rx     = fw - stem_x - 1
    belly_ry     = (belly_bot_y - belly_top_y) // 2
    belly_cy     = (belly_top_y + belly_bot_y) // 2

    # Right-facing D: flat left edge at stem_x, curve opening to the right
    pts = [(stem_x, belly_cy - belly_ry)]
    for deg in range(-90, 91, 6):
        rad = deg * math.pi / 180
        px = stem_x + round(belly_rx * math.cos(rad))
        py = belly_cy + round(belly_ry * math.sin(rad))
        pts.append((px, py))
    pts.append((stem_x, belly_cy + belly_ry))

    if len(pts) >= 3:
        pygame.draw.polygon(surf, BLACK, pts)

    # re-draw the stem over the polygon to keep it clean
    pygame.draw.line(surf, BLACK, (stem_x, 0), (stem_x, fh - 1), 1)

    return surf


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
    mid = mido.MidiFile(path)
    tpb = mid.ticks_per_beat

    raw = []
    for track in mid.tracks:
        t = 0
        for msg in track:
            t += msg.time
            raw.append((t, msg))
    raw.sort(key=lambda x: x[0])

    notes: List[NoteEvent] = []
    active = {}
    cur_tick = 0
    cur_sec  = 0.0
    tempo    = 500_000

    for abs_tick, msg in raw:
        delta = abs_tick - cur_tick
        if delta:
            cur_sec += mido.tick2second(delta, tpb, tempo)
        cur_tick = abs_tick

        if msg.type == 'set_tempo':
            tempo = msg.tempo
        elif msg.type == 'note_on' and msg.velocity > 0:
            k = (msg.channel, msg.note)
            if k in active:
                active[k].end = cur_sec
            ne = NoteEvent(msg.note, cur_sec, msg.velocity)
            active[k] = ne
            notes.append(ne)
        elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
            k = (msg.channel, msg.note)
            if k in active:
                active[k].end = cur_sec
                del active[k]

    for ne in active.values():
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
        with self._lock:
            self.notes = [n for n in self.notes
                          if n.end is None or n.end > before]


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  UI WIDGETS                                                                 ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class Slider:
    def __init__(self, x, y, w, mn, mx, val, label):
        self.x, self.y, self.w = x, y, w
        self.mn, self.mx = mn, mx
        self.val   = val
        self.label = label
        self._drag = False

    def _frac(self):
        return (self.val - self.mn) / (self.mx - self.mn)

    @property
    def hx(self):
        return int(self.x + self._frac() * self.w)

    def handle(self, ev):
        tr = pygame.Rect(self.x, self.y - 5, self.w, 10)
        hh = pygame.Rect(self.hx - 11, self.y - 14, 22, 28)
        if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            if hh.collidepoint(ev.pos) or tr.collidepoint(ev.pos):
                self._drag = True
                self._move(ev.pos[0])
        elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
            self._drag = False
        elif ev.type == pygame.MOUSEMOTION and self._drag:
            self._move(ev.pos[0])

    def _move(self, mx):
        f = max(0.0, min(1.0, (mx - self.x) / self.w))
        self.val = self.mn + f * (self.mx - self.mn)

    def draw(self, surf, font):
        tr = pygame.Rect(self.x, self.y - 4, self.w, 8)
        pygame.draw.rect(surf, LGRAY, tr, border_radius=4)
        pygame.draw.rect(surf, BLUE,
                         pygame.Rect(self.x, self.y - 4, self.hx - self.x, 8),
                         border_radius=4)
        col = LBLUE if self._drag else BLUE
        pygame.draw.rect(surf, col,  (self.hx - 11, self.y - 14, 22, 28), border_radius=5)
        pygame.draw.rect(surf, DARK, (self.hx - 11, self.y - 14, 22, 28), 1, border_radius=5)
        lbl = font.render(f'{self.label}: {self.val:.1f}s', True, DARK)
        surf.blit(lbl, (self.x, self.y - 35))


class Button:
    def __init__(self, x, y, w, h, text, sticky=False):
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

        # UI fonts
        self.uf = pygame.font.SysFont('Arial', 18)
        self.sf = pygame.font.SysFont('Arial', 14)

        # Clef font — needs Unicode Musical Symbols support
        self.cf = self._find_music_font(86)

        # Pre-render accidental symbols
        self._sharp_surf = _make_sharp(LINE_SP)
        self._flat_surf  = _make_flat(LINE_SP)

        # App state
        self.mode      = 'file'
        self.notes: List[NoteEvent] = []
        self.cur_t     = 0.0
        self._wall0    = None
        self.playing   = False
        self.file_path = None
        self.status    = 'Load a MIDI file, or switch to Live mode'
        self._dialog_open = False

        self.live      = LiveInput()
        self._live_t0  = None

        # Widgets
        BY = CTRL_Y + 18
        self.btn_mode = Button( 20, BY,       145, 38, 'Mode: File', sticky=True)
        self.btn_load = Button( 20, BY + 50,  145, 30, 'Load File...')
        self.btn_play = Button(180, BY,        80, 38, 'Play')
        self.btn_stop = Button(272, BY,        80, 38, 'Stop')
        self.sld      = Slider(420, BY + 26,  340,  0.5, 10.0, 4.0, 'Lookahead')

        # Pre-build static staff surface (background lines don't change)
        self._staff_surf = self._build_staff_surface()

        # Auto-load demo
        default = _res('valse_69_1_(c)dery.mid')
        if os.path.exists(default):
            self._load(default)

    # ── static staff background ───────────────────────────────────────────────

    def _build_staff_surface(self) -> pygame.Surface:
        """Render the fixed background (staves, clef strip, clefs, brace) once."""
        surf = pygame.Surface((W, H))
        surf.fill(BG)

        # Clef area background
        top_y0 = TREBLE_TOP - 3 * LINE_SP
        bot_y1 = BASS_TOP   + STAFF_H + 3 * LINE_SP
        pygame.draw.rect(surf, CLEF_BG, (0, top_y0, CLEF_W, bot_y1 - top_y0))

        # Draw both staves across full width
        for top_y in (TREBLE_TOP, BASS_TOP):
            for i in range(5):
                y = top_y + i * LINE_SP
                pygame.draw.line(surf, STAFF_C, (CLEF_W, y), (W - 10, y), 1)

        # Dividing line between clef strip and scroll area
        pygame.draw.line(surf, (180, 178, 168),
                         (CLEF_W, top_y0), (CLEF_W, bot_y1), 1)

        # Clef symbols
        if self.cf:
            tc = self.cf.render('𝄞', True, BLACK)
            bc = self.cf.render('𝄢', True, BLACK)
            # Treble: align so curl is on G4 (2nd line from bottom)
            surf.blit(tc, (CLEF_W - tc.get_width() - 4,
                           TREBLE_TOP - round(LINE_SP * 2.0)))
            # Bass: align so dots straddle F3 (2nd line from top)
            surf.blit(bc, (CLEF_W - bc.get_width() - 4,
                           BASS_TOP - round(LINE_SP * 0.6)))
        else:
            f = pygame.font.SysFont('serif', 52)
            surf.blit(f.render('G', True, BLACK), (CLEF_W - 50, TREBLE_TOP - 8))
            surf.blit(f.render('F', True, BLACK), (CLEF_W - 50, BASS_TOP - 8))

        # Brace — vertical bar with serifs connecting both staves
        y0 = TREBLE_TOP
        y1 = BASS_TOP + STAFF_H
        bx = 10
        pygame.draw.line(surf, BRACE_C, (bx, y0), (bx, y1), 3)
        pygame.draw.line(surf, BRACE_C, (bx, y0), (bx + 12, y0), 3)
        pygame.draw.line(surf, BRACE_C, (bx, y1), (bx + 12, y1), 3)

        # Left barline spanning both staves
        pygame.draw.line(surf, STAFF_C,
                         (CLEF_W, TREBLE_TOP),
                         (CLEF_W, BASS_TOP + STAFF_H), 1)

        return surf

    # ── font helpers ──────────────────────────────────────────────────────────

    def _find_music_font(self, sz: int):
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

    # ── file loading ──────────────────────────────────────────────────────────

    def _load(self, path: str):
        try:
            self.notes     = load_midi(path)
            self.file_path = path
            self.cur_t     = 0.0
            self.playing   = False
            self._wall0    = None
            self.status    = f'Loaded: {os.path.basename(path)}  ({len(self.notes)} notes)'
        except Exception as e:
            self.status = f'Error: {e}'

    def _open_dialog(self):
        if self._dialog_open:
            return
        self._dialog_open = True

        def _run():
            path = None
            if platform.system() == 'Darwin':
                r = subprocess.run(
                    ['osascript', '-e',
                     'set f to choose file with prompt "Select MIDI file" '
                     'of type {"mid","midi","public.data"}\nreturn POSIX path of f'],
                    capture_output=True, text=True)
                if r.returncode == 0:
                    path = r.stdout.strip()
            else:
                try:
                    import tkinter as tk
                    from tkinter import filedialog
                    root = tk.Tk(); root.withdraw()
                    path = filedialog.askopenfilename(
                        title='Select MIDI File',
                        filetypes=[('MIDI', '*.mid *.midi'), ('All', '*.*')])
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
            self.status = ('Press Play or Space'
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
                if self.cur_t > end_t + 3.0:
                    self._stop()
        elif self.mode == 'live' and self._live_t0 is not None:
            self.cur_t = time.time() - self._live_t0
            self.live.prune(self.cur_t - 10.0)

    # ── note drawing ──────────────────────────────────────────────────────────

    def _note_x(self, t: float, ref_t: float, la: float) -> float:
        """Screen x for a note at time t, given reference time ref_t and lookahead la."""
        uw = W - PLAY_X - 12
        return PLAY_X + (t - ref_t) * (uw / la)

    def _draw_ledger_lines(self, xi: int, ny: float, top_y: int):
        """Draw ledger lines above and/or below the staff for note at ny."""
        bot_y = top_y + STAFF_H
        lw    = NOTE_W + 7   # ledger line half-width

        # Below staff: first ledger at bot_y + LINE_SP
        ly = bot_y + LINE_SP
        while ly <= ny + LINE_SP * 0.4:
            pygame.draw.line(self.surf, LEDGER_C,
                             (xi - lw, round(ly)), (xi + lw, round(ly)), 1)
            ly += LINE_SP

        # Above staff: first ledger at top_y - LINE_SP
        ly = top_y - LINE_SP
        while ly >= ny - LINE_SP * 0.4:
            pygame.draw.line(self.surf, LEDGER_C,
                             (xi - lw, round(ly)), (xi + lw, round(ly)), 1)
            ly -= LINE_SP

    def _draw_note_head(self, xi: int, yi: int):
        """Draw a filled black ellipse note head with a clean 1-px outline."""
        r = pygame.Rect(xi - NOTE_W, yi - NOTE_H_R, NOTE_W * 2, NOTE_H_R * 2)
        if HAS_GFXDRAW:
            pygame.gfxdraw.filled_ellipse(self.surf, xi, yi, NOTE_W, NOTE_H_R, NOTE_C)
            pygame.gfxdraw.aaellipse(     self.surf, xi, yi, NOTE_W, NOTE_H_R, NOTE_C)
        else:
            pygame.draw.ellipse(self.surf, NOTE_C, r)

    def _draw_notes(self, notes: List[NoteEvent], la: float, is_live: bool = False):
        """
        Draw all visible notes.

        In live mode the reference time is shifted back by `la` so that a note
        played right now enters at the right edge and drifts to the play line
        over the next `la` seconds — exactly like file mode.
        """
        ref_t = (self.cur_t - la) if is_live else self.cur_t

        vis_start = ref_t - 3.0
        vis_end   = ref_t + la + 0.5

        for n in notes:
            end_t = n.end if n.end is not None else self.cur_t
            if end_t < vis_start or n.start > vis_end:
                continue

            clef  = 'treble' if use_treble(n.pitch) else 'bass'
            top_y = TREBLE_TOP if clef == 'treble' else BASS_TOP
            ny    = note_y(n.pitch, clef)
            sx    = self._note_x(n.start, ref_t, la)
            ex    = self._note_x(end_t,   ref_t, la)

            # ── duration tail ─────────────────────────────────────────────
            # Tail runs from the right edge of the note head to end_x,
            # clamped to the visible scroll area.
            tail_x0 = max(sx + NOTE_W, float(CLEF_W))
            tail_x1 = min(ex,          float(W - 12))
            if tail_x1 > tail_x0:
                pygame.draw.rect(
                    self.surf, TAIL_C,
                    (round(tail_x0), round(ny) - TAIL_T // 2,
                     round(tail_x1 - tail_x0), TAIL_T))

            # ── note head (only if within visible area) ───────────────────
            if CLEF_W - NOTE_W <= sx <= W + NOTE_W:
                xi = round(sx)
                yi = round(ny)

                self._draw_note_head(xi, yi)
                self._draw_ledger_lines(xi, ny, top_y)

                # ── accidental ────────────────────────────────────────────
                acc = _acc(n.pitch)
                if acc and xi > CLEF_W - 40:
                    if acc == '#':
                        s = self._sharp_surf
                    else:
                        s = self._flat_surf
                    ax = xi - NOTE_W - s.get_width() - 2
                    ay = yi - s.get_height() // 2
                    self.surf.blit(s, (ax, ay))

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

            # ── render ────────────────────────────────────────────────────
            # Blit the static background (staves, clefs, brace)
            self.surf.blit(self._staff_surf, (0, 0))

            # Play line
            pygame.draw.line(self.surf, PLAY_C,
                             (PLAY_X, TREBLE_TOP - 3 * LINE_SP),
                             (PLAY_X, BASS_TOP + STAFF_H + 3 * LINE_SP), 2)

            # Animated notes
            la  = self.sld.val
            src = self.live.get_notes() if self.mode == 'live' else self.notes
            self._draw_notes(src, la, is_live=(self.mode == 'live'))

            # ── control panel ─────────────────────────────────────────────
            pygame.draw.rect(self.surf, CTRL_BG, (0, CTRL_Y, W, H - CTRL_Y))
            pygame.draw.line(self.surf, LGRAY, (0, CTRL_Y), (W, CTRL_Y), 1)

            self.btn_mode.draw(self.surf, self.uf)
            if self.mode == 'file':
                self.btn_load.draw(self.surf, self.uf)
                self.btn_play.draw(self.surf, self.uf)
                self.btn_stop.draw(self.surf, self.uf)
            self.sld.draw(self.surf, self.uf)

            self.surf.blit(self.sf.render(self.status, True, DARK), (600, CTRL_Y + 12))
            self.surf.blit(self.sf.render(f't = {self.cur_t:.1f}s', True, DARK),
                           (W - 130, CTRL_Y + 12))
            self.surf.blit(self.sf.render('Space: play/pause    R: restart', True, LGRAY),
                           (600, CTRL_Y + 34))

            mode_lbl = '● LIVE' if self.mode == 'live' else '● FILE'
            mode_col = (200, 40, 40) if self.mode == 'live' else (40, 130, 40)
            self.surf.blit(self.uf.render(mode_lbl, True, mode_col), (600, CTRL_Y + 56))

            pygame.display.flip()


def main():
    app = App()
    app.run()


if __name__ == '__main__':
    main()
