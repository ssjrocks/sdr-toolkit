# SDR Toolkit

Decoders for amateur and utility radio data modes in one window, fed by whatever is playing on this
computer: a WebSDR in the browser, an SDR program, a radio on line-in. Nothing has to be routed: it
records the output device's monitor through PipeWire, so you still hear the audio.

Pick a mode on the left. **How to tune** says what to select on the SDR (USB, LSB, FM, CW), the
filter width and where to listen; **Settings** has every parameter that differs between stations
and receivers. Click a signal on the waterfall to tune the decoder to it.

## Modes (phase 1: speaker audio)

| Mode | Decoder | Adjustable |
|---|---|---|
| Morse / CW, MCW | own (from [Morse Decoder](https://github.com/ssjrocks/morse-decoder)) | tone, filter width, speed (auto, Talkpod steps, fixed 5–60 WPM), sensitivity |
| RTTY | own | mark tone, shift (170–850 Hz), speed (45.45–100 baud), reverse, stop bits, unshift on space, figures set, squelch |
| PSK31 / 63 / 125 | own | frequency (click), speed, AFC and its range, squelch |
| NAVTEX (SITOR-B) | own | centre, shift, station and subject filters |
| FT8, FT4 | WSJT-X's `jt9` | audio delay (for a WebSDR's lag) and follow-DT, depth, audio range, CQ only, highlight |
| SSTV | own (from [SSTV Decoder](https://github.com/ssjrocks/sstv-decoder)) | mode or automatic, join part way through, saving |
| Weather fax (WEFAX) | own | LPM, IOC, centre, deviation, start/stop tones, clock correction, shift, invert |
| APRS / AX.25 packet | direwolf | 300–9600 baud, mark/space tones, demodulator profile, bit repair |
| Pagers: POCSAG, FLEX | multimon-ng | each speed, invert, message type, character set, error correction, address filter |
| DTMF, CTCSS, DCS | own | each on/off, shortest digit, line gap |

Pictures go to `~/Pictures/SSTV` and `~/Pictures/WEFAX` (changeable).

**Phase 2 (planned):** modes that need raw radio samples from the RTL-SDR rather than audio: ADS-B,
UAT, ISM sensors (rtl_433), LoRa / Meshtastic / MeshCore, DMR and P25 voice, trunk tracking (driving
SDRTrunk), Mobitex. A WebSDR can't carry these: they are wider than an audio channel, or need the
receiver to be retuned.

## Install

Needs Ubuntu with PipeWire and `python3-numpy python3-scipy python3-pyqt6 python3-pil`; FT8/FT4,
APRS and pagers also need `wsjtx direwolf multimon-ng` (the app tells you if one is missing).

```bash
sudo apt install wsjtx direwolf multimon-ng
scripts/install.sh
```

Then open **SDR Toolkit** from the app menu. `scripts/install.sh --uninstall` removes it.

## Notes per mode

- **FT8/FT4** cut the recording into UTC slots, so the PC clock must be right (Ubuntu keeps it
  synced). A WebSDR stream arrives late; with **Follow DT** on, the audio delay moves until the
  decodes' time offset (DT) is near zero.
- **NAVTEX**: the decoder finds the bit order and polarity itself, from the phasing signals and
  which reading gives valid characters.
- **DCS**: one code's bit stream also reads as other codes (023N is the same signal as 047I on
  air); the decoder names a standard code and lists the equivalents. The codewords are built as in
  the Talkpod firmware's `golay()`, which matches words measured from the stock firmware.
- **CTCSS/DCS** need audio below 300 Hz; many receivers and WebSDRs filter it out.
- **Pagers, DMR, P25**: receiving them is restricted in some countries.

## Tests

```bash
python3 -m pytest tests                               # every mode, through the mode interface
QT_QPA_PLATFORM=offscreen python3 tests/gui_smoke.py  # the window: every mode runs, 3 decode
```

What the tests check, and against what:

| Mode | Test signal |
|---|---|
| RTTY | the decoder was checked against `minimodem`'s RTTY encoder; the tests use their own encoder |
| FT8, FT4 | WSJT-X's `ft8sim`, and FT4 tones from `ft4code` |
| APRS | direwolf's `gen_packets` |
| POCSAG | an encoder written from the standard, checked against multimon-ng first |
| SSTV | the SSTV Decoder's encoder, which was checked against PySSTV |
| Morse, PSK, NAVTEX, WEFAX, DTMF, CTCSS, DCS | encoders in this project (so they show the decoders agree with my reading of each format, not with other software) |

Also tested: noise must not print text in the text modes, and a live run through PipeWire (FT8 on the
real UTC clock, APRS and RTTY). Not yet tested on real off-air signals.

## Problem reports

**Report a problem…** sends your note, the app's log (`~/.local/state/sdr-toolkit/app.log`) and the
current mode's settings to the developer's server. Crashes and decoder errors are sent the same
way. Decoded messages and pictures are never sent.
