# MIDI Staff Visualizer

A real-time scrolling sheet music display for MIDI input. Notes drift from right to left across a grand staff (treble + bass clef); when a note reaches the red **play line** on the left, the performer plays it.

Designed for sight-reading practice and live performance assistance — no bars, no time signatures, just notes flowing in time.

---

## Features

- **Grand staff display** — treble and bass clef, always in C major
- **Smooth animation** — notes scroll at a consistent real-world speed, decoupled from MIDI tempo
- **Duration tails** — each note has a thick horizontal tail showing how long to hold it
- **Accidentals on every chromatic note** — sharps shown before each note head (no key signature)
- **Adjustable lookahead** — a slider sets the preview window from 0.5 s (fast) to 10 s (slow)
- **Two input modes**:
  - **File mode** — load any `.mid` file; handles embedded tempo changes
  - **Live mode** — connects to a MIDI keyboard via USB/MIDI interface
- **Bundled demo** — ships with Chopin's *Valse Op. 69 No. 1* (`valse_69_1_(c)dery.mid`)

---

## How it works

### Scrolling model

Every note is positioned on screen according to:

```
screen_x = PLAY_LINE_X + (note_start_time − current_time) × pixels_per_second
```

where

```
pixels_per_second = usable_screen_width / lookahead_seconds
```

- A note arriving at `current_time = note_start_time` lands exactly on the play line.
- Notes to the **right** are upcoming; notes to the **left** have passed.
- The tail (duration bar) extends rightward from the note head to represent hold time.

### Tempo handling (file mode)

The MIDI parser walks all tracks simultaneously, tracking `set_tempo` events and accumulating real wall-clock seconds tick by tick using `mido.tick2second`. The resulting `NoteEvent` objects carry absolute times in seconds, so tempo changes in the file translate naturally into visual spacing — faster passages appear more bunched together, slower passages more spread out — while the lookahead slider always reflects real elapsed seconds.

### Staff layout

| Staff   | Top line | Bottom line | Middle C position                  |
|---------|----------|-------------|------------------------------------|
| Treble  | F5 (77)  | E4 (64)     | 1 ledger line below bottom line    |
| Bass    | A3 (57)  | G2 (43)     | 1 ledger line above top line       |

Notes ≥ C4 (MIDI 60) are placed on the treble staff; notes < 60 on the bass staff.

Pitch → staff y-position is computed via absolute **diatonic steps** from C-1:

```python
diatonic_step(midi) = (midi // 12) × 7 + diatonic_degree(midi % 12)
y = staff_top + (ref_top_line_step − diatonic_step(note)) × (line_spacing / 2)
```

Accidentals use the sharp convention throughout (C# not Db, F# not Gb, etc.).

### Note colours

| Colour | Meaning                              |
|--------|--------------------------------------|
| Blue   | Upcoming note                        |
| Orange/red | Note at the play line (play now!) |
| Grey   | Note just passed (recently played)   |

---

## Running from source

**Requirements:** Python 3.11+, macOS (also works on Linux/Windows with minor font differences).

```bash
pip install pygame mido python-rtmidi
python3 main.py
```

### Keyboard shortcuts

| Key     | Action              |
|---------|---------------------|
| `Space` | Play / Pause        |
| `R`     | Restart from start  |

---

## Building the macOS .app bundle

```bash
chmod +x build.sh
./build.sh
```

This runs PyInstaller and produces `dist/MIDI Staff Visualizer.app` — a self-contained double-clickable app. Copy it to `/Applications` or run it directly from `dist/`.

**Requirements for building:** `pyinstaller` (installed automatically by `build.sh`).

---

## Controls

| Control           | Description                                                   |
|-------------------|---------------------------------------------------------------|
| **Mode: File/Live** | Toggle between MIDI file playback and live MIDI keyboard input |
| **Load File…**    | Open a `.mid` file from disk (File mode only)                 |
| **Play / Stop**   | Start or stop file playback                                   |
| **Lookahead slider** | Set the preview window (0.5 s = very fast, 10 s = very slow) |

---

## Live MIDI input

Switch to **Live** mode and connect a MIDI keyboard. The app opens the first available MIDI port automatically. If no device is detected, a message is shown in the status bar.

Requires a USB MIDI interface or class-compliant MIDI keyboard. On macOS the system MIDI driver handles device enumeration — no additional drivers needed for most keyboards.

---

## Project structure

```
SQ_App2/
├── main.py                        # All application code (~400 lines)
├── requirements.txt               # Python dependencies
├── build.sh                       # PyInstaller build script
├── valse_69_1_(c)dery.mid         # Demo MIDI file (Chopin Valse Op.69 No.1)
└── dist/
    └── MIDI Staff Visualizer.app  # Built macOS application
```

---

## Dependencies

| Package          | Purpose                                |
|------------------|----------------------------------------|
| `pygame`         | Window, rendering, event loop          |
| `mido`           | MIDI file parsing                      |
| `python-rtmidi`  | Live MIDI input from hardware keyboard |
| `pyinstaller`    | Packaging into a macOS .app bundle     |

---

## Notes on fonts

Clef symbols (𝄞 𝄢) require a font with Unicode Musical Symbols (U+1D100–U+1D1FF). On macOS the app uses **Apple Symbols** automatically. On other systems it falls back to a labelled G/F approximation. Accidental symbols (♯ ♭) are in the Miscellaneous Symbols block (U+2600–U+26FF) and render correctly in Arial and most system fonts.
