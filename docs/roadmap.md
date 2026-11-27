# Roadmap

What's being added, section by section. Each section is built, tested, released and reported on before the
next starts. Hardware: **RTL** works on an RTL-SDR, **HF** needs the HackRF, **WiFi** / **BT** the PC's own
Wi-Fi card (monitor mode) and Bluetooth, **+hw** extra hardware.

Running through all of it: a **"what my place gives away"** view, everything heard from your own devices in
one list, with how often each transmits, how strongly, and on what, so you can see what someone passing by
could pick up.

## 1. Widen what's there
- [x] ISM on every band: 433.92 and 915-928 MHz (Australia), 868 and 315 elsewhere, chosen by region; hopping
      on an RTL-SDR, the whole band at once on a HackRF (one stream split into several rtl_433) — RTL/HF
- [x] Every device heard, listed by itself (nothing to set up): what it is, how often it sends, signal
      strength, its readings over time, and a note when a new one appears — RTL/HF
- [x] Unknown-signal analyser: a burst nobody decodes shown as modulation, timings and bits, saved as a
      decoder of your own — RTL/HF
- [x] DSD-FME's other modes offered: NXDN, dPMR, YSF, D-STAR, M17, EDACS, ProVoice, X2-TDMA; the explorer tries
      each in turn — RTL/HF
- [x] Trunk following: a P25 or DMR control channel's calls followed onto their voice channels (DSD-FME's own;
      not yet tried on a live trunked system) — RTL/HF
- [x] Meters: Itron ERT (rtlamr), Wireless M-Bus (wmbusmeters) — RTL/HF
- [ ] Z-Wave (waving-z): its build failed and couldn't be looked into yet

## 2. Aviation, marine, emergency
- [x] ACARS and VDL Mode 2 messages, joined to the ADS-B aircraft — RTL (VDL2 not yet decoded from a signal)
- [ ] FLARM / OGN: gliders, light aircraft and drones on the aircraft map — RTL/HF (open decoders are EU 868 MHz
      only; Australia's 917-927 MHz hopping band and the newer FLARM protocol need more work)
- [x] VOR bearings, ILS and ATIS — RTL (ATIS: AM in the receiver)
- [x] Marine DSC (VHF channel 70) next to AIS — RTL
- [x] 406 MHz EPIRB / PLB beacons — RTL (first generation; second generation needs dec406)
- [x] HFDL — HF reception (+hw for good results) (not yet decoded from a signal)

## 3. Broadcast
- [ ] FM RDS: station name, radiotext, traffic — RTL/HF
- [ ] DAB+ radio: stations, listening, slideshow, programme info — RTL/HF
- [ ] DRM shortwave — HF reception

## 4. Satellites and space
- [ ] METEOR-M weather pictures (SatDump) — RTL
- [ ] Iridium: pager messages, ACARS, ring alerts, beams on the map — HF + patch antenna
- [ ] Inmarsat STD-C and AERO — HF + patch antenna and LNA
- [ ] Hydrogen line (1420 MHz) and meteor scatter — HF + LNA / dish

## 5. Wi-Fi, Bluetooth, drones
- [ ] Wi-Fi map: networks, connected devices, probe requests, channel use, live Wireshark, your own WPA2
      network decrypted with its password — WiFi
- [ ] Bluetooth / BLE: every device, trackers following you, beacons, drone Remote ID — BT
- [ ] Bluetooth Classic devices (ice9) — HF
- [ ] DJI DroneID: drone and pilot positions — HF
- [ ] 5.8 GHz analog FPV video — HF
- [ ] Zigbee / Thread: smart-home devices and channels — HF

## 6. Cellular
- [ ] LTE / 5G cell scanner: towers, bands, operators, cell IDs, signal map, tower locations — HF

## To do: left over and blocked
Built-in decoders that couldn't be built here yet, and checks still owed:
- [ ] **dec406** (second-generation 406 MHz beacons): its build was blocked by Claude Code's auto mode
      ("Undermining Oversight"); needs the user's approval or an allow rule. Use its file decoders only
      (`dec406_iq`, `dec406_fgb_iq`): its live scanner resets the RTL-SDR's USB.
- [ ] **Z-Wave** (waving-z): the build failed; looking into it was blocked the same way.
- [ ] **FLARM / OGN**: port flare's decoders (GPL) and add Australia's 917-927 MHz hopping band.
- [ ] Decode from real signals: VDL2, HFDL, ACARS (one real burst so far, one bit error too many), ERT
      meters, trunk following, VOR / ILS, DSC, 406 MHz.
- [ ] The relay PC: SDR Tools Relay hasn't come back since the 0.3.1 update; it needs a look there (with
      the user's OK first).
- [ ] GitHub: failed release drafts 0.18.0, 0.18.1 and 0.20.0 can be deleted (with the user's OK).
- [ ] The flaky GPS test.

## Later (sections 7-8 of the wishlist)
The map and timeline of everything seen, alerts, autopilot, two SDRs at once, the IQ time machine,
direction finding, portable mode, exports, Flipper Zero.
