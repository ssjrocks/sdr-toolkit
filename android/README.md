# SDR Toolkit for Android

The receiver and ADS-B parts of SDR Toolkit on a phone, fed by an RTL-SDR plugged into the phone,
or by any [SDR Tools Relay](https://github.com/ssjrocks/sdr-tools-relay) on your network or the
internet: the same relays the desktop app's **Connect relay…** uses.

Three tabs:

- **Devices**: everything the app can listen through, found by itself.
  - **USB dongles** plugged in with a USB-C or micro-USB OTG adapter. Plugging one in offers to
    open the app. Other SDRs (HackRF, Airspy, SDRplay) are listed, marked as not supported yet.
  - **Relays on this network**, found by the same UDP scan as the desktop (port 47321), with
    their dongle, version, and whether they have a password or are in use.
  - **Saved addresses**: a relay or a plain `rtl_tcp` server by host name or IP (a Tailscale name
    works). The app tells the two apart when it connects. The relay port (47320) is the default.
  - **Relays on the internet**, listed on ssjrocks.com, fetched only when you ask.

  Tap one to connect. A relay with a password asks for it once and can remember it. The
  password never crosses the network: as on the desktop, the app answers the relay's nonce with
  HMAC-SHA256. Once connected, the app goes straight to the receiver.
- **Receiver**: spectrum and waterfall of up to 2.6 MHz of the band, with audio in WFM, NFM,
  AM, USB, LSB or CW. **Tap** the band to tune (to the nearest step), **drag** to slide along it
  (the dongle follows past the edge), **pinch** to zoom, **double-tap** to see the whole span.
  Tap the frequency to type one (`101.1`, `7100k`, `1090M`), or use ◀ ▶ to step. The meter
  shows the channel's level, with the squelch threshold marked. **Bands** has starting points
  (FM broadcast, airband, 2 m, marine, PMR446, UHF CB, HF…). **Settings** (the sliders icon) has
  bandwidth, squelch, de-emphasis, sample rate, gain (automatic, or the tuner's own steps),
  frequency correction, direct sampling, bias tee, RTL AGC, offset tuning (E4000 only), FFT size,
  averaging, waterfall range and peak hold. Every setting is an rtl_tcp command, so it works the
  same on a USB dongle and a relay.
- **Aircraft**: ADS-B on 1090 MHz, decoded on the phone. **Start tracking** moves the dongle to
  1090 MHz at 2 MS/s (the receiver picks up where it was when you go back). Callsign, altitude
  (with climb or descent), speed, track, squawk (7500/7600/7700 in red), signal and, with
  **Use my location**, distance and a radar plot, north up. Sort by nearest, newest or highest.

The radio keeps playing with the screen off or the app in the background: a notification shows
what's playing, with **Disconnect**.

## A dongle on the phone

Android apps reach USB dongles through a driver app, the free
[Rtl-sdr driver](https://play.google.com/store/apps/details?id=marto.rtl_tcp_andro)
(`marto.rtl_tcp_andro`, also used by SDR Touch and RF Analyzer). The first time you tap your
dongle, the app offers to install it. After that, tapping the dongle starts the driver, which
asks Android for permission to use it, and serves it to the app on localhost as `rtl_tcp`.

The phone has to power the dongle. Most do, but a dongle with an active antenna or LNA on the bias
tee may need a powered USB hub. 2.4 MS/s through the driver is about 4.8 MB/s over USB, which
every phone with USB host support handles.

## A relay

Run [SDR Tools Relay](https://github.com/ssjrocks/sdr-tools-relay) on the PC with the dongle,
as for the desktop app. On the same Wi-Fi it appears under **Relays on this network** within a
couple of seconds. If it doesn't, the PC's firewall may be blocking UDP 47321: add it by address
instead. Over the internet, the connection needs the bandwidth of the sample rate: about 20
Mbit/s at 1.2 MS/s, 38 at 2.4 MS/s, and 32 for ADS-B. Lower the sample rate in Settings on a
slow link.

## What's here and what isn't

The desktop's decoders mostly run outside programs (rtl_433, readsb, direwolf, multimon-ng,
WSJT-X, DSD-FME, GNSS-SDR), which don't run on Android. This version has the receiver and ADS-B,
written for the phone:

- `core/`: the rtl_tcp client and the relay protocol (hello, HMAC login, discovery, directory,
  telling a relay from rtl_tcp), the same as `relay.py` and `receiver/source.py`.
- `dsp/`: FFT and spectrum, multi-stage decimating FIR filters (each sample rate is a multiple of
  48 kHz, as on the desktop, so every step down to audio is a whole-number decimation), the
  demodulators and AGC, ported from `receiver/dsp.py`.
- `adsb/`: Mode S at 2 MS/s (preamble search, pulse-position bits, CRC-24 with one-bit repair),
  DF17/18 identity, airborne and surface position, velocity, DF4/5/20/21 altitude and squawk, and
  CPR global and local position decoding.
- `engine/`: one connection at a time. A reader thread fills a pool of buffers (the oldest are
  dropped if processing falls behind), a DSP thread makes the spectrum, waterfall, audio and
  aircraft, and a foreground service keeps it going in the background.
- `ui/`: Jetpack Compose, Material 3, dark.

Not done yet: the audio decoders (Morse, RTTY, PSK, FT8, SSTV, APRS, pagers…), ISM sensors,
AIS, LoRa, digital voice, the satellite tracker, the logbook, and a map under the aircraft.

## Building

Android Studio, or from this folder with JDK 17 or newer and the Android SDK (platform 36):

```bash
./gradlew assembleDebug          # app/build/outputs/apk/debug/app-debug.apk
./gradlew assembleRelease        # minified, signed with the debug key so it installs as is
./gradlew testDebugUnitTest      # every test below
```

GitHub Actions builds both APKs on every push (`.github/workflows/android.yml`). Download them
from the run's artifacts.

## Tests

They all run on the JVM; the UI ones run the real app under Robolectric.

| Test | What it checks |
|---|---|
| `RelayTest` | the HMAC against Python's `hmac`, rtl_tcp command bytes, address parsing, the directory's JSON, login, a wrong or missing password, a busy relay, LAN discovery, and telling a relay from an rtl_tcp server |
| `RelayLiveTest` | the real SDR Tools Relay (set `SDR_TOOLS_RELAY_SRC` to its `src`) playing an ADS-B recording: found by the scan, logged in with the password, tuned, and the aircraft decoded from its samples |
| `DspTest` | the FFT, the spectrum's dBFS scale, the filter plans' cost, and NFM, WFM, AM, USB, LSB and CW on synthetic signals: the right tone, the wrong sideband cut, the same audio whatever the block sizes, squelch |
| `AdsbTest` | the worked examples of *The 1090 MHz Riddle* (identity, CPR positions global and local, ground speed and airspeed), CRC and one-bit repair, an encoder written from the standard round-tripped around the world, the demodulator on synthesised IQ split into awkward chunks, and no aircraft from noise |
| `EngineThroughputTest` | the engine keeps up with a relay at 2.4 MS/s: nothing dropped, 25 spectrum frames a second |
| `UiWalkthroughTest` | the whole app against a simulated relay making up a band and aircraft: found by the scan, the password dialog, the band on the receiver, the bands and settings sheets, five aircraft tracked with the right positions, and back. `SCREENSHOT_DIR=… ./gradlew testDebugUnitTest` saves screenshots of each step |

Not yet tested on a real phone, with a real USB dongle through the driver, or with a relay over the
internet.
