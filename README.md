# SDR Toolkit

Decoders for amateur and utility radio data modes in one window, fed by whatever is playing on this
computer: a WebSDR in the browser, an SDR program, a radio on line-in. Nothing has to be routed: it
records the output device's monitor through PipeWire (on Windows, WASAPI loopback), so you still
hear the audio. It runs on Ubuntu and on Windows 10 and 11 (see [Windows](#windows)).

Pick a mode on the left. **How to tune** says what to select on the SDR (USB, LSB, FM, CW), the
filter width and where to listen; **Settings** has every parameter that differs between stations
and receivers. Click a signal on the waterfall to tune the decoder to it.

**Decoded log** and **Raw data** open in their own windows: a rolling, timestamped history of
everything decoded (also what the aircraft and ship tables update in place), and what the decoder
itself outputs (rtl_433's JSON, NMEA sentences, LoRa frame bytes, direwolf, multimon-ng, jt9 and
DSD-FME output). Both keep the last 5,000 lines while closed, and can be paused, cleared and saved.

**Logbook** keeps everything received in every mode, for good, in
`~/.local/share/sdr-toolkit/logbook.sqlite`: each decoded line, row, message and picture title,
and every aircraft and ship seen (first and last seen, how many times, its details). Search it,
filter by mode, export to CSV, or clear it. ADS-B and AIS positions are kept too (one per craft
every 10 s or 300 m, for 30 days), for **Replay**: pick a time range and play the traffic back on
a map at up to 1000× speed.

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
| ADS-B aircraft (1090 MHz) | readsb | units, how long aircraft stay listed |
| AIS ships (161.975 / 162.025 MHz) | rtl_fm + direwolf, sentences decoded by the toolkit | channel, how long ships stay listed |
| Radiosondes (weather balloons, 400–406 MHz) | rs1729's decoders (RS41, DFM, M10/M20, RS92, iMet-54, Meisei...) and dft_detect | centre frequency, frequencies or found by themselves, ground height for the landing estimate, how long sondes stay listed |
| LoRa / Meshtastic / MeshCore | gr-lora_sdr (built separately, see below) | network preset, region, frequency, SF/BW/CR/sync word, channel keys |
| DMR / P25 / digital voice (the explorer) | DSD-FME with mbelib (built separately, see below), on every signal found | everything Spectrum & receiver has, how strong a signal must be to be tried, listen to everything or one channel, talkgroup or radio |
| Spectrum & receiver | the toolkit's own (rtl_tcp to the dongle) | frequency and step, mode (WFM, NFM, AM, USB, LSB, CW), bandwidth, de-emphasis, squelch, volume, sample rate, gain mode and gain steps, RTL AGC, ppm, direct sampling (HF), offset tuning, bias tee, FFT size, averaging, peak hold |
| GPS position fix (1575.42 MHz) | GNSS-SDR | search range, satellites tracked at once, fill in my location |

The LoRa decoder corrects the transmitter's and dongle's crystal error (tested to ±20 ppm, an 18 kHz
offset at 920 MHz) as long as the frequency setting is the network's real centre frequency.

Device, gain, frequency correction (ppm) and your location are set once for all RTL-SDR modes.
Above the output, each mode shows what it's doing: the frequency, how long it's listened, the noise
or signal level and what it has seen (for ISM, every transmission rtl_433 notices, decoded or not),
so a quiet band looks quiet rather than broken. With no dongle plugged in it says so instead of
hanging.

**Frequency correction from ADS-B:** readsb counts the samples the dongle delivers against the PC's
clock (which Ubuntu keeps right over the internet) and reports the difference every 30 seconds,
which is the dongle's crystal error. After two readings that agree within 3 ppm, ADS-B shows
**Set frequency correction to +N ppm**; clicking it saves the value for every RTL-SDR mode and
restarts ADS-B with it. The readings are what's left after the current correction, so it adds to
the value you have, and a second round shows whether it's right. It only works on a live dongle,
not on a recording.


**ADS-B and AIS draw a map** (OpenStreetMap) above their tables: aircraft and ships with their
heading, a label and a short trail, and your location if set. Drag to move, wheel to zoom, **Fit**
to follow everything again. Tiles are cached in `~/.cache/sdr-toolkit/tiles` for 30 days and
fetched at most two at a time, as OSM's tile usage policy asks. AIS decodes position reports
(classes A and B) and static reports, so ships get their names, callsigns, IMO numbers, types,
size, draught, destinations and ETAs. **All tracks** shows every track heard this session; craft
no longer heard stay, dimmed, where they were last heard.

**Details** (next to Settings) shows everything known about the aircraft or ship you click in the
table or on the map, with a photo. For aircraft the registration, type, operator and year come from
[tar1090-db](https://github.com/wiedehopf/tar1090-db) (built from public registers; the app
downloads it, 9 MB, and refreshes it monthly). The photo is that aircraft's own from
[Planespotters.net](https://www.planespotters.net), with the photographer's credit and a link,
or else a Wikipedia photo of its type. No free service has photos of individual ships, so ships
get a photo of their type. Looking up a photo sends the aircraft's ICAO address or the type to
those sites.

**On the map, aircraft and ships move on between reports:** from their last position along their
course at their speed, redrawn ten times a second. Once a track has missed three of its usual
updates (at least 5 s), its position is a prediction: the icon flashes, with a dashed line back to
the last position heard; it goes on moving while other messages (speed, altitude) still arrive,
and an aircraft leaves the map 60 s after its last message of any kind. Ships coast on for up to
10 minutes, as their reports are further apart.

**The ADS-B table** shows each aircraft's **Msg/s** (messages heard a second over the last
30 s) and **Missed**: the share of what an airborne ADS-B transponder sends (about 6.2 a second:
2 positions, 2 velocities, an all-call reply and identity/status messages) that didn't arrive.
Aircraft near a ground radar answer its interrogations too, so they can show 0%; Mode S-only and
taxiing aircraft send less, so they get no estimate.

**ADS-B range rings** show how far your station hears in each direction: the furthest position
heard (red, dashed) and how far reception is reliable (green: at least 80% of an aircraft's
position updates still arrive within 10 s of the one before). Purple (dotted) is an estimate of
the real reception range: an aircraft flying away often goes on sending other messages after its
last position, and one flying in is often heard before its first, so it's dead-reckoned from that
position, its ground speed and track to its last message (or back to its first), for gaps of 5 s
to 5 minutes. Green is drawn over red, and red over purple. They build up over days, from the
live dongle only (this PC's, or a relay on this network, each with its own rings), and are kept
per location in the logbook. **Range** turns them off.

**A dongle on another PC:** **Connect relay…** (next to Start) uses an RTL-SDR plugged into another
Windows or Linux PC running [SDR Tools Relay](https://github.com/ssjrocks/sdr-tools-relay). **On this
network** scans the LAN for relays, **Internet** shows the relays listed on ssjrocks.com, and **By
address** takes a host name or IP (a Tailscale name works too). While connected, every RTL-SDR mode
on "RTL-SDR" input uses the relay's dongle: rtl_433, DSD-FME and GNSS-SDR talk rtl_tcp to a local
bridge, and readsb, LoRa and AIS read its samples on stdin. The relay applies its own frequency
correction, so ADS-B's correction button is off while it's in use. Distances are from your
location, not the relay's, and range rings are drawn for a relay on this network (a private LAN
address, so at your location), kept apart from this PC's dongle's (another antenna); not for one
reached over the internet or Tailscale, which could be anywhere. A connection over the internet needs the bandwidth of the
sample rate: about 38 Mbit/s for ADS-B, 4 for AIS and ISM, 16 for LoRa. While connected, the relay
button's menu says whether the relay is up to date and offers **Update the relay to …** when a
newer SDR Tools Relay is out (the relay must have a password, and relays from 0.1.2 on).

**On Android:** [`android/`](android/README.md) has a phone version of Spectrum & receiver and
ADS-B, with an RTL-SDR plugged into the phone (through the free Rtl-sdr driver app) or any relay
on the network or the internet, found the same way as here. GitHub Actions builds its APK.

**Spectrum & receiver** is a receiver like SDR++ or GQRX: a spectrum (with peak hold) over a
waterfall of up to 2.6 MHz of the band, and a demodulator on the frequency you click, played
through your speakers. Click or drag to tune (rounded to the tuning step), wheel to zoom, right-drag
to pan, double-click for the whole span; typing a frequency outside the span retunes the dongle,
keeping the signal a quarter of the span off its DC spike. Every dongle setting is an rtl_tcp
command: the toolkit runs `rtl_tcp` for a dongle on this PC, or uses a connected relay's, so it
works the same on both. Sample rates are whole multiples of 48 kHz (2.88 to 0.24 MS/s), so every
step down to 48 kHz audio is a whole-number decimation with no clicks between blocks. The bias tee
puts 4.5 V on the antenna socket: only for active antennas or LNAs, on dongles that have one.

Drag the grab bar along the top of the spectrum to slide the dongle's span along the band (the
frequency you're listening to stays put while it's in view); the dongle follows a few times a
second, and the bar shows the frequencies at its ends. Its ◀ ▶ buttons step 80% of what's on
screen. Clicking anywhere in the spectrum, right up to its edges, tunes there without moving the
band. **Identify** works like Shazam for signal types: it records 8 s of the channel you're tuned to
and puts it through the decoders: DSD-FME (DMR, P25, NXDN, D-Star, YSF), multimon-ng (POCSAG and
FLEX pagers), direwolf (APRS, and AIS near 162 MHz), the CTCSS / DCS / DTMF decoder and, in the
ISM bands, rtl_433; broadcast FM is recognised by its 19 kHz stereo pilot (and RDS carrier). What
decodes is the answer, marked ✓ with what was decoded (say "DMR signal: slot 1, colour code 1"),
with the demodulator and decoder to use. After it come guesses from the signal's measurements (its
width, constant or varying envelope, FSK levels and baud rate, deviation, carrier, AFSK tones,
keying) checked against an Australian band plan. With **Identify automatically** on, the
measured guess is made every few seconds when something is there (no decoding). A guess is only a
guess: weak and unusual signals often come out as "2-level FSK" or "narrow FM".
When it can't tell, pick a decoder from **Decode with**: Morse, RTTY, PSK31, NAVTEX, FT8/FT4,
SSTV, weather fax, APRS, pagers, DTMF/CTCSS/DCS tones or DMR/P25 (DSD-FME) run on the receiver's
audio, with their own audio waterfall and output below the spectrum. DMR/P25 shows a line as soon as
DSD-FME locks on to a signal (protocol, slot, colour code), every 10 s while it lasts and a summary
when it ends, before any call is decoded; the decoded voice has its own **Voice** volume slider. **Tag…** (or right-click the
spectrum) saves a frequency with a label, demodulator and decoder in the logbook; tags show on the
spectrum and the survey, and right-clicking one offers to listen with its settings and decoder.

**Decode points** decode several channels at the same time, alongside the one you're listening
to: right-click the spectrum and choose **Decode … MHz too** (DMR/P25, pagers, APRS or tones), or
**Decode the tagged channels in view** for tags that have one of those decoders. Each point is a
cyan line on the spectrum (with ● while it's decoding something); its output appears under the
decode area, each line marked with its frequency, and in the decoded log. The channels are cut
out of the dongle's samples together, from one FFT per block, so a dozen cost less CPU than the
receiver's own demodulator; each point runs its own decoder program, though, so up to 24 at once.
Points outside the band the dongle sends wait until it's back there; they're kept between sessions. DMR voice from every point plays at the Voice volume (mute one
from its right-click menu); right-click a point to stop it, or **Stop all channels**.

**DMR / P25 / digital voice** is the receiver with an explorer on top, for mapping the digital
voice around you. Slide the spectrum to a busy part of the band (430-520 MHz, say) and leave it:
every narrow signal that appears gets a decode point running DSD-FME for about 20 s. One that
turns out to be DMR, P25, NXDN, D-Star, YSF, dPMR or M17 stays, and is listed; anything else is let
go and left alone for 10 minutes, and a channel silent for 10 minutes has its decoder stopped until
it's back. A much weaker signal right beside a strong one is taken for its side band, not tried.
The lists under the spectrum fill as things are heard:

- **Channels**: each frequency and what it is in plain words ("DMR voice channel", "P25 phase 1
  control channel", "DMR repeater, idle"), its colour code or NAC, slots, calls, who's talking on it
  now and what its data carried (GPS positions, text messages, control messages). Hover a row for
  what that kind of channel means.
- **Systems**: channels that belong together (same protocol and access code, or a P25 system's
  WACN and system ID), with their control and voice channels.
- **Talkgroups** and **Radios**: every talkgroup and radio ID heard, their calls, which radios use
  a talkgroup and which talkgroups a radio calls, and a radio's last GPS position.
- **Activity**: what happened, as it happened ("Group call to 505 from radio 3125678, slot 1").

All channels' voice plays at once by default. Select a channel, talkgroup or radio and press
**Only the selected** to hear just that one (a talkgroup or radio is let through on whichever
channel its call is on), or **Listen to everything** to go back; double-click a channel to single it
out. The lists are kept between sessions (in `explorer.json` in the app's data folder), so
exploring builds up a picture of your area. Encrypted calls are listed but can't be heard. The
single-frequency DMR / P25 decoder is still there as **Decode with → DMR / P25** in Spectrum &
receiver.

**Survey** sweeps the dongle over its whole tuning range and stitches the slices into one spectrum
and waterfall. After each retune it throws away the samples of a settle time (150 ms by default,
250 ms more through a relay, where samples from the old frequency are still on their way), then
measures for the **Dwell** time (the slider beside Survey) and keeps the middle 80% of the slice.
The dwell keeps the strongest moment of the slice, so a longer one catches signals that are only on
now and then (pagers, DMR, data bursts), at the cost of slower passes. Everything captured is kept
at the dongle's full detail (about 2.3 kHz at 2.4 MS/s) while the window is open: zoom and pan to
look closer at it, waterfall included, without the sweep changing. **Sweep what's shown** then
surveys only the range on screen, so passes get faster (narrower than one slice, the dongle just
stays there); **Sweep all** goes back to the whole range. A pass over 80-180 MHz through a relay
takes about 20 s. Double-click to listen there.

**GPS position fix** works out where you are from the GPS satellites' own signals, with the
RTL-SDR on 1575.42 MHz, and fills in "Your location" for the other modes (averaged over the
latest fixes, and updated when you move 30 m). The signals are far below the noise, so it needs an
active GPS antenna (with an LNA, powered by a bias tee or its own supply) with a clear view of the
sky; a passive antenna may manage outdoors. From cold a fix takes 1-5 minutes. Set the frequency
correction first (ADS-B's button): a cheap dongle 57 ppm off puts the satellites 90 kHz away,
outside the normal search. The correction is applied as GNSS-SDR's RTL-SDR example does it: the
real sample rate and the signal's offset.

**Radiosondes** follows weather balloons. The dongle watches 2.2 MHz of the 400–406 MHz band; any
narrow signal well above the noise gets its own channel (cut out with the decode points'
channelizer), rs1729's `dft_detect` says which sonde type it is and its polarity, and that type's
decoder (from [rs1729/RS](https://github.com/rs1729/RS), built by the installer) decodes it, so
several sondes can be followed at once. Each one is a row (serial, type, frequency, altitude, climb,
speed, temperature, humidity, pressure, battery, GPS satellites, distance) and a balloon on the map
with its track. Once it's coming down, a cross marks where it should land: a straight line at its
descent rate on the wind it has now, so rough; SondeHub's prediction (linked in Details) uses wind
forecasts. In Australia the Bureau of Meteorology launches Vaisala RS41s around 9 am and 9 pm from
its stations, on 400.5–403.5 MHz.

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

## Windows

Download `SDR-Toolkit-Setup-<version>.exe` from the
[latest release](https://github.com/ssjrocks/sdr-toolkit/releases/latest) and run it. It installs
for your user only (no administrator password), with a Start menu entry. The app updates itself as
on Linux: **Update to …** downloads the release's installer (only from this repository, and only if
it matches the release's SHA-256), runs it and starts the new version.

What works on Windows:

- every speaker-audio mode with the app's own decoders: Morse, RTTY, PSK31/63/125, NAVTEX, SSTV,
  weather fax, DTMF/CTCSS/DCS. The audio comes from an output device's loopback (WASAPI), the same
  idea as PipeWire's monitor: pick the output under the mode list;
- **Spectrum & receiver**, with an RTL-SDR on this PC or a relay, and the receiver's own decoders;
- **Connect relay…**: the scan, the ssjrocks.com list and addresses, as on Linux;
- the **Satellite tracker**, the **Logbook** and **Replay**.

The RTL-SDR tools (`rtl_tcp`, `rtl_fm`, `rtl_sdr` and the rest, from RTL-SDR Blog's V1.4.0 release)
come with the app. A dongle on Windows needs the WinUSB driver once: install it with
[Zadig](https://zadig.akeo.ie), or with SDR Tools Relay's installer, which sets it up.

Modes that run outside programs work once the program is there. **How to get it** (where Linux has
**Install**) says where each comes from, and opens the folder the app looks in,
`%LOCALAPPDATA%\SDR Toolkit\tools`. It also finds them on PATH, and WSJT-X's `jt9` in WSJT-X's own
install folder:

| Needs | For | On Windows |
|---|---|---|
| WSJT-X (jt9) | FT8, FT4 | install WSJT-X |
| direwolf | APRS, AIS | its Windows release, in the tools folder |
| multimon-ng | pagers | a Windows build, in the tools folder |
| rtl_433 | ISM sensors | its Windows release, in the tools folder |
| DSD-FME | DMR, P25 | a Windows build, in the tools folder |
| readsb, GNSS-SDR, gr-lora_sdr | ADS-B, the GPS fix, LoRa | no Windows versions: use the Linux app |

Files live in `%LOCALAPPDATA%\SDR Toolkit` (the logbook, the aircraft database, `State\app.log`,
`Cache`) instead of `~/.local/share`, `~/.local/state` and `~/.cache`. Pictures still go to
`Pictures\SSTV` and `Pictures\WEFAX` in your user folder.

The Windows build is tested on GitHub's Windows Server 2022 machines
(`.github/workflows/windows.yml`): the tests run from source, then the built app and the installed
app each run `--self-test` (a Morse transmission decoded, the receiver's demodulator, the helper
processes, the bundled RTL-SDR tools, the logbook and the window). Those machines have no sound
card and no dongle, so WASAPI capture and playback and a real dongle aren't tested there.

## Updates

The app looks for a new release on GitHub at most every 12 hours, and **Check for updates** looks now.
**Update to …** downloads the release's wheel (only from this repository's GitHub releases, and only
if it matches the release's SHA-256), installs it into the app's venv and restarts. A version that
needs new outside parts offers to install them after the restart.

**Tables** (aircraft, ships, pagers, sensors, the logbook, relays) sort by a column when you click
its heading, and the other way on a second click. Numbers sort as numbers, units and all ("9 s"
before "10 s"), and live tables stay sorted, with the same row selected, as they update.

**Satellite tracker** (its own group) works like N2YO for the satellites you can hear: weather,
amateur and space-station satellites, from the SatNOGS DB (the community's database of satellite
radios and their orbits) and CelesTrak's group lists, cached for a day (orbits) or a week. Pick a
satellite for its passes over you (the next days, or **best first**: the highest), which ones you
can see with your eyes (lit by the Sun against a dark sky), where it is now on the map with its
ground track and footprint, a sky plot of the pass, and every radio it carries: frequency, mode,
baud rate, whether it's still heard, and what you can do with it here (listen in NFM or SSB,
decode APRS, SSTV or Morse) or what it needs instead (SatDump for weather pictures, gr-satellites
for telemetry). **Listen** opens Spectrum & receiver tuned to it and follows its Doppler shift
(up to ±3.5 kHz on 2 m, ±10 kHz on 70 cm) through the pass. **Coming over** lists every pass in
the next 12 hours. Set your location in Settings first; it's shared with the RTL-SDR modes.

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

Tests that need an outside program which isn't installed (on a fresh PC, or one that doesn't exist
for Windows) are reported as skipped, naming the program (`tests/conftest.py`).
`SDR_TOOLKIT_ALL_TOOLS=1` makes them fail instead, to check a fully set-up PC.

```bash
python3 -m pytest tests                               # every mode, through the mode interface
QT_QPA_PLATFORM=offscreen python3 tests/gui_smoke.py  # the window: every mode runs, 3 decode
uv run --with pytest --with skyfield python -m pytest tests/test_satellites.py   # needs sgp4 (the app's env)
```

The satellite tests check orbits, passes and Doppler against [Skyfield](https://rhodesmill.org/skyfield/)
(positions within 0.02°, rises and peaks within 3 s), and read a few satellites cut from the SatNOGS DB
(`tests/data/satellites`) instead of downloading.

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
| GPS | [gps-sdr-sim](https://github.com/osqzss/gps-sdr-sim) signals for a position in Sydney from a real broadcast ephemeris (1 Jan 2022), made to look like a dongle 54 ppm fast (sample clock and tuning) with noise above the satellites; the fix comes out within 20 m (3 m in practice). The test runs when gps-sdr-sim is built in `build/gps-sdr-sim` |
| AIS | type 1 and 5 reports (HDLC, CRC-16, NRZI, GMSK) encoded from the standard, decoded by direwolf; the sentence decoder also against the AIVDM documentation's example (MMSI 366053209 in San Francisco Bay) |
| Meshtastic, MeshCore | packets built to the published formats (Meshtastic firmware and protobufs, MeshCore docs), sent as real LoRa IQ by gr-lora_sdr's transmitter (`tests/lora_tx.py`) with a 5 ppm crystal error and noise, and decoded through the mode; the LongFast channel hash (8), MeshCore Public hash (0x11) and Meshtastic's documented default frequencies come out right. MeshCore's sync word (0x12, RadioLib's default) is assumed on both sides, so that value isn't checked |

The ADS-B frequency correction is tested on readsb's figures fed in by
hand, not yet with a dongle.

DMR/P25 has no test signal here (nothing on this PC generates those protocols): the tests check that
noise gives no calls (Ubuntu's DSDcc reported false DMR frames on the same test, so it isn't used),
the command line, and the no-dongle message. Real decoding is untested.

Also tested: the logbook, range rings, replay, Details (aircraft from readsb with the aircraft
database, ships from their static reports) and the window's picking, session tracks and rings;
photo answers are tested on their real shapes, and the photo lookup was tried once for real.
The relay is tested with SDR Tools Relay playing test recordings as its dongle (`tests/test_relay.py`,
when its checkout is beside this one): ISM, ADS-B, AIS and LoRa decode through it, and the
password, busy and LAN-scan cases work. Not yet tested: a real dongle on a relay, or a relay
over the internet.
The receiver's demodulators are tested on synthetic WFM, NFM, AM, USB, LSB and CW signals (the
right tone, the wrong sideband cut, no clicks at block edges, faster than real time), and the
receiver on a recording and through a relay. It was also tried on a real R820T dongle through a
relay: WFM on 104.5 MHz (a strong channel, healthy audio, the 19 kHz stereo pilot clear in the
demodulated signal), manual gain (0 to 49.6 dB moved the station 32 dB), and a change of sample
rate; not yet: AM, SSB and CW off air, direct sampling, offset tuning and the bias tee.
LoRa at 125 kHz is tested through the resampling the live dongle needs (it can't sample at
500 kHz, so it runs at 1 MHz). Not yet tested with a dongle: the GPS fix, range rings and
LoRa's new sample rates.

Also tested: noise must not print text in the text modes; a live run through PipeWire (FT8 on the
real UTC clock, APRS and RTTY); each RTL-SDR mode with no dongle plugged in. Not yet tested: any
mode on real off-air signals, and the RTL-SDR modes with a dongle.

## Problem reports

**Report a problem…** sends your note, the app's log (`~/.local/state/sdr-toolkit/app.log`) and the
current mode's settings to the developer's server. Crashes and decoder errors are sent the same
way. Decoded messages, pictures and the logbook are never sent.
