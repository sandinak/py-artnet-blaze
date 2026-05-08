# py-artnet-blaze

ArtNet bridge daemon for the Evolution Show Choir "Step" units, designed
to run on a **Raspberry Pi**. Forwards ArtDmx into two output paths
simultaneously:

- **Pixelblaze Output Expander (POE)** over the Pi's hardware UART
  (GPIO14/15 at up to 2 Mbaud) for the WS2812 LED strips
- **USB DMX dongle** (Enttec USB DMX Pro or Open DMX USB) plugged into
  any USB port for fixtures like par cans, bar lights, and movers

Plus a built-in **HTTP test panel** at `:8080` for flashing test
patterns across the whole rig and surfacing live status info (active
DMX, IPs, uptimes, firmware versions).

Replaces the Fadecandy chain while keeping the Pi as the configurable
endpoint, and adds DMX-out so the same Pi can drive non-pixel fixtures
from the same QLC+ session.

## Hardware target

Built around the Raspberry Pi:

- **Raspberry Pi 3B+** running **Raspberry Pi OS** (Debian Bookworm /
  Bullseye, systemd-based) is the reference platform. Pi 4 and Pi 5
  work without code changes; Pi Zero 2 W should be fine at 50 FPS but
  is untested.
- The **PL011 hardware UART** exposed on GPIO14 (TX → POE RX) and
  GPIO15. Bluetooth must be disabled so `/dev/serial0` maps to the
  real UART rather than the mini UART — handled in the install steps
  [below](#install-on-a-pi).
- A free **USB port** for the DMX dongle (auto-enumerated as
  `/dev/ttyUSB0`); plug it in before the daemon starts.
- **Network** for ArtNet — wired Ethernet preferred for show day, Wi-Fi
  fine for bench testing.

The daemon's hard Pi dependency is the GPIO UART for POE. If you only
need DMX-out (no LED strips), it will run on any Linux box with a USB
DMX dongle. **Development and the test suite run on macOS or plain
Linux without a Pi** — fake serial ports and an ephemeral UDP socket
on `127.0.0.1` cover the hardware paths.

## Architecture

```
                   ┌──→ UART @ 2 Mbaud → POE → 8× WS2812 strips
QLC+ → ArtNet/UDP →┤
                   └──→ USB serial    → DMX dongle → bar lights / fixtures
                          ↑
                    this daemon
```

A single `ArtNetReceiver` keeps a 512-byte rolling buffer per subscribed
universe. Each output path is a `Sink` running on its own thread and
fixed-tick FPS — POE at 50, DMX at 40. Input jitter never reaches the
output cadence; missing packets re-send the last known state rather than
stalling the tick.

DMX universes can either piggyback an existing POE universe (e.g. bytes
384..511 of universe 0 carry your bar lights, leaving 0..383 for the two
strips on that universe) or live on a dedicated universe (e.g. 4). It's
a config choice, not a code change — see `config.yaml.example`.

## Layout

```
artnet_blaze/
  artnet.py     ArtDmx receiver + universe buffers
  poe.py        Pixelblaze Output Expander wire format + sink
  dmx.py        Enttec USB DMX Pro + Open DMX USB sinks (incl. fw probe)
  sink.py       Sink base class (tick loop, lifecycle)
  controller.py Test-pattern override (powers the HTTP panel buttons)
  http_api.py   Single-page test panel (stdlib http.server)
  sysinfo.py    Versions / OS / IPs / uptime collector
  config.py     YAML loading + validation
  main.py       CLI wiring (entry point: `python -m artnet_blaze`)
tests/          pytest suite — no hardware required
systemd/        artnet-blaze.service unit
Makefile        venv / install / test / coverage / run / clean
pytest.ini      Test runner config (coverage threshold lives here)
.coveragerc     Coverage tool config
```

## Install on a Pi

```bash
# 1. Enable the real UART on GPIO14/15 (for POE).
sudo tee -a /boot/config.txt <<EOF
enable_uart=1
dtoverlay=disable-bt
EOF
sudo systemctl disable hciuart
sudo reboot

# 2. Wire POE: Pi TX (pin 8, GPIO14) → POE RX. Common ground.
#    Plug the USB DMX dongle into any USB port. It will appear as
#    /dev/ttyUSB0 (or /dev/ttyUSB1 if something else is already there).

# 3. Deploy the code.
sudo mkdir -p /opt/artnet-blaze /etc/artnet-blaze /var/log/artnet-blaze
sudo adduser --system --group --no-create-home blaze
sudo usermod -a -G dialout blaze
sudo cp -r artnet_blaze requirements.txt /opt/artnet-blaze/
sudo cp config.yaml.example /etc/artnet-blaze/config.yaml

# Build the venv inside the deploy dir.
sudo python3 -m venv /opt/artnet-blaze/venv
sudo /opt/artnet-blaze/venv/bin/pip install -r /opt/artnet-blaze/requirements.txt
sudo chown -R blaze:blaze /opt/artnet-blaze /etc/artnet-blaze /var/log/artnet-blaze

# 4. Install & start the service.
sudo cp systemd/artnet-blaze.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now artnet-blaze
sudo journalctl -u artnet-blaze -f
```

## Configuration

Top-level config sections in `config.yaml`:

- `artnet.bind` — interface to receive ArtNet on. `0.0.0.0` for all.
- `serial` — POE UART device + baudrate (`/dev/serial0`, 2 Mbaud).
- `bridge.fps` — POE output rate (50).
- `strips` — list of POE channel mappings (universe + offset + LED count).
- `dmx` — USB DMX dongle config:
  - `enabled: true|false`
  - `device: /dev/ttyUSB0`
  - `protocol: enttec_pro` or `open_dmx`
  - `fps: 40`
  - `fixtures` — list of `{universe, offset, dmx_start, length}` mappings.
- `logging` — log level + stats interval.

See `config.yaml.example` for an annotated default with both the
piggyback and dedicated-universe DMX options.

### HTTP test panel

When the daemon is running, point a browser at `http://<pi-ip>:8080/`.
Single-page panel, stdlib only (no Flask), polls every second.

What's on it:

- **Test patterns** — four buttons:
  - *All white (0xFF)* — every output byte set to 0xFF
  - *50% (0x80)* — every output byte set to 0x80
  - *Identify* — staircase on SR + white tip on SL + unit name in the
    middle row, all rendered RGB on the LED strips. See *Identify
    pattern* below.
  - *Clear* — drop the override
- **Startup identify** — by default, the daemon paints the identify
  pattern as soon as it boots, holding until live ArtNet arrives. This
  confirms a freshly-flashed Pi is reachable, wired correctly, and
  self-labeled before QLC+ even starts. Disable via
  `unit.identify_at_startup: false` if you'd rather not see it on
  show-day restarts.
- **Override behavior** — once set (manually or at startup), the
  override holds for at least 5 seconds. After that, ArtNet wins again
  *if* it's currently active; otherwise the override stays until
  traffic resumes. Exact rule:
  `expires when (elapsed ≥ 5s) AND (a packet arrived in the last 1s)`.
- **Active DMX indicator** — one pill per subscribed universe, green
  when a packet arrived in the last second.
- **System** — code / Python / pyserial / pyyaml versions, OS pretty
  name (`/etc/os-release`), hostname, IPv4 addresses, process uptime,
  system uptime.
- **Devices** — DMX dongle path, protocol, and firmware revision (read
  from the Enttec Pro at startup via "Get Widget Parameters"). POE
  firmware is `n/a (one-way protocol)` — POE has no inquiry record.

Caveats:

- *"All white" on DMX fixtures* means "all bytes 0xFF". On RGB pixels
  that's literal white; on a 24-channel bar with master-dim, strobe,
  mode, etc., it'll be visually fixture-dependent (often "everything
  full + strobing"). Useful for "is this fixture talking?", not for
  "is the white balance right?".
- *No auth.* Fine for a private show LAN, **don't** expose to the
  public internet. Put it behind a VPN or HTTP basic-auth proxy if you
  need remote access.
- Disable via `http.enabled: false` in config if you'd rather not run
  the listener at all.

### Identify pattern

For each row in the unit (taken from each strip's `row` + `side`
metadata), the identify override paints:

- **Staircase** on the SR side: row 1 → 1 amber pixel, row 2 → 2,
  row 3 → 3, row 4 → 4. Confirms row order.
- **SL tip**: rightmost 4 pixels of the SL strip lit white. Confirms
  the SL side is reaching across.
- **Unit name** rendered horizontally across the row's combined LED
  width in dim grey, using a 3×4 bitmap font (covers 0–9 + A–Z). Each
  physical row paints one slice of the glyph (row 1 = top, row 4 =
  bottom).

Set `unit.name: "US1"` (or whatever) in config; the name shows in the
HTTP panel header banner and on the LEDs. With no row/side metadata,
the identify pattern simply paints nothing — strips stay dark.

JSON API for tooling: `GET /api/status` returns a structured snapshot
of everything on the page; `POST /test/{white,half,identify,clear}`
sets/clears overrides; `GET /healthz` for liveness probes.

### Picking a DMX protocol

| Dongle says…                                        | Use protocol  |
|-----------------------------------------------------|---------------|
| "Enttec USB DMX Pro" / "DMX512-A" / "Enttec compat" | `enttec_pro`  |
| Just "Open DMX USB" or unbranded FTDI dongle        | `open_dmx`    |

The Enttec-Pro framing is more reliable on Linux because the dongle
generates DMX timing internally. Open DMX requires us to bit-bang
BREAK/MAB from userspace at 250 kbaud — it works but expect occasional
flicker on heavy-CPU moments. If you have a choice, get an Enttec-Pro
compatible dongle.

## Local development

The project ships with a Makefile that handles the venv, dependency
install, and test runs. No hardware required for tests — fake serial
ports and an ephemeral UDP socket on `127.0.0.1` cover the wire paths.

```bash
make              # create .venv, install dev deps, run tests
make test         # pytest with coverage (fails under 85%)
make coverage     # same, plus htmlcov/index.html
make run          # run the daemon locally against config.yaml.example
make lint         # import-smoke the package
make clean        # nuke .venv and caches
```

If you'd rather drive it yourself:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

### What the test suite covers

- **Unit** — ArtDmx parsing, POE wire records, Enttec Pro framing,
  Open DMX BREAK/MAB sequencing, fixture merging, config validation.
- **Sink lifecycle** — real threaded tick loop with FPS pacing,
  tx_error counting on synthetic write failures, blackout-on-stop,
  idempotent stop.
- **Real UDP socket** — `ArtNetReceiver` bound to an ephemeral port on
  `127.0.0.1`, hit with a real `sendto()`, asserts buffer state.
- **End-to-end wiring** — `main()` driven through argparse + a YAML
  config file, with injected fake serial ports; SIGTERM'd from a
  worker thread to exercise the real signal handler + blackout path.
- **HTTP panel** — server bound on an ephemeral port; urllib hits each
  route; override propagation asserted on a live PoeSink+DmxSink pair.
- **Coverage** — enforced ≥85% via `pytest-cov` (currently ~91%).

## Things to verify on step 1 before rolling to all 12 Pis

1. **POE wire format.** `poe_frame_set_channel` and `poe_frame_draw_all`
   match the protocol documented in the `pixelblaze_output_expander`
   repo. Cross-check against the firmware revision you're flashing.
2. **DMX wire format.** Plug the dongle into a single test fixture with
   a known DMX address. Set `dmx.fixtures` to drive that address from
   QLC+ universe 4; confirm the fixture responds. If it doesn't, swap
   `protocol:` (enttec_pro ↔ open_dmx) and try again — the box label
   is sometimes misleading. Quick alternative: hit the HTTP test
   panel's *All white* button and watch the fixture light up.
3. **Frame timing under load.** Run 60+ minutes with continuous patterns
   on both POE and DMX. Watch `poe_late=` and `dmx_late=` in the stats
   line. Both should stay at 0.
4. **Boot-time sync.** After `systemctl start`, both LED strips and DMX
   fixtures come up clean, not mid-garbage.
5. **ArtNet universe mapping.** Confirm QLC+ universe N maps to what
   you expect — easy test: solid red on one universe, verify only the
   correct strips/fixtures light.
6. **Blackout on exit.** `systemctl stop artnet-blaze` should turn
   strips off and zero DMX within ~1 second.

## Extending

- **Multi-step identical config via DHCP.** Bind address can stay
  `0.0.0.0` if each Pi is on its own subnet and QLC+ unicasts. For
  broadcast ArtNet, pin the bind to each step's specific IP.
- **Per-step universe offset.** If you want identical firmware on all
  12 Pis with step-specific universes, add a `universe_base: N` to
  config and add it to each strip/fixture's universe at load time.
- **Monitoring.** Stats lines are greppable; pipe to Prometheus
  textfile collector for per-sink rx/tx/error counters and you can
  alert on a step going offline mid-show.
- **More sinks.** A second DMX dongle, a sACN forwarder, or anything
  else is a new `Sink` subclass with its own thread. The receiver
  doesn't need to know about it.
