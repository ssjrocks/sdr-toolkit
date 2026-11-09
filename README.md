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

## Modes (phase 2: RTL-SDR)

These need the radio samples themselves, which a WebSDR can't give: the toolkit runs the RTL-SDR
directly. Each can also read an **IQ recording** instead (unsigned 8-bit, as `rtl_sdr` writes
it), set under **Settings → Input**: handy without a dongle, or to decode something again.

| Mode | Decoder | Adjustable |
|---|---|---|
| ISM sensors (433/868/915/315 MHz) | rtl_433 | frequency or several to hop between, sample rate, protocol filter |
| ADS-B aircraft (1090 MHz) | readsb | your position (for distance and bearing), units, how long aircraft stay listed |
| AIS ships (161.975 / 162.025 MHz) | rtl_fm + direwolf | channel, NMEA display |
| LoRa / Meshtastic / MeshCore | gr-lora_sdr (built separately, see below) | network preset, region, frequency, SF/BW/CR/sync word, channel keys |
| DMR, P25, NXDN, D-Star, YSF, dPMR, M17 | DSD-FME with mbelib (built separately, see below) | frequency, protocol, channel bandwidth, squelch, play voice, save each call as WAV |

The LoRa decoder corrects the transmitter's and dongle's crystal error (tested to ±20 ppm, an 18 kHz
offset at 920 MHz) as long as the frequency setting is the network's real centre frequency.

Every RTL-SDR mode also has device, gain and ppm correction settings. With no dongle plugged in
it says so instead of hanging.

**LoRa** decodes Meshtastic texts, positions and node names on the default channel (key `AQ==`, and
any private channel you add the key for), and MeshCore adverts and Public channel texts. MeshCore
direct messages are end-to-end encrypted and show only as sender → receiver. The LoRa demodulator is
[gr-lora_sdr](https://github.com/tapparelj/gr-lora_sdr) (EPFL), which isn't packaged for Ubuntu:

(The installer builds it.)

**Digital voice** shows calls from DSD-FME's event log (talkgroups, radio IDs, callsigns) and plays
the voice. Encrypted calls stay encrypted: the toolkit doesn't use DSD-FME's decryption-key options.
DSD-FME and the mbelib voice codecs aren't packaged for Ubuntu either:

(The installer builds them, after asking about mbelib's patent notice.)

mbelib's README: "Executable objects compiled or derived from this package may be covered by one or
more patents." It's provided for educational purposes; check what applies where you are.

**Not done yet:** UAT 978 MHz (US only; dump978 isn't packaged), trunk tracking through SDRTrunk, and
Mobitex (no open decoder known).

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/ssjrocks/sdr-toolkit/master/scripts/install.sh | bash
```

(or `scripts/install.sh` from a checkout). It sets up everything the modes use, asking for your
password once:

- the Ubuntu packages: the app's libraries, WSJT-X, direwolf, multimon-ng, rtl_433, readsb, the
  RTL-SDR tools and GNU Radio, plus compilers for the next step (readsb's own service is turned off:
  it would hold the RTL-SDR, and the toolkit runs readsb itself);
- gr-lora_sdr (LoRa), and mbelib + DSD-FME (DMR, P25), built from source into
  `~/.local/share/sdr-toolkit`, which takes a few minutes. Before building mbelib it shows mbelib's
  patent notice and asks (`--accept-mbelib` to say yes in advance, `--no-dmr` to leave it out);
- the app and its app-menu entry.

If a mode still lacks something (an older install, or DMR/P25 left out), the app says which part and
offers an **Install** button, which does the same with the desktop's password prompt.

`scripts/install.sh --uninstall` removes the app and everything it built; saved pictures and calls stay.

## Updates

The app looks for a new release on GitHub at most every 12 hours, and **Check for updates** looks now.
**Update to …** downloads the release's wheel (only from this repository's GitHub releases, and only
if it matches the release's SHA-256), installs it into the app's venv and restarts. A version that
needs new outside parts offers to install them after the restart.

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

| ISM | a Nexus-TH sensor synthesized from rtl_433's protocol description, decoded by rtl_433 |
| ADS-B | DF17 squitters (CRC-24, identification, CPR positions) encoded from the standard, decoded by readsb |
| AIS | type 1 reports (HDLC, CRC-16, NRZI, GMSK) encoded from the standard, decoded by direwolf |
| Meshtastic, MeshCore | packets built to the published formats (Meshtastic firmware and protobufs, MeshCore docs), sent as real LoRa IQ by gr-lora_sdr's transmitter (`tests/lora_tx.py`) with a 5 ppm crystal error and noise, and decoded through the mode; the LongFast channel hash (8), MeshCore Public hash (0x11) and Meshtastic's documented default frequencies come out right. MeshCore's sync word (0x12, RadioLib's default) is assumed on both sides, so that value isn't checked |

DMR/P25 has no test signal here (nothing on this PC generates those protocols): the tests check that
noise gives no calls (Ubuntu's DSDcc reported false DMR frames on the same test, so it isn't used),
the command line, and the no-dongle message. Real decoding is untested.

Also tested: noise must not print text in the text modes; a live run through PipeWire (FT8 on the
real UTC clock, APRS and RTTY); each RTL-SDR mode with no dongle plugged in. Not yet tested: any
mode on real off-air signals, and the RTL-SDR modes with a dongle.

## Problem reports

**Report a problem…** sends your note, the app's log (`~/.local/state/sdr-toolkit/app.log`) and the
current mode's settings to the developer's server. Crashes and decoder errors are sent the same
way. Decoded messages and pictures are never sent.
