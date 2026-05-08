"""Single-page HTTP test panel.

A `ThreadingHTTPServer` runs in its own daemon thread. Routes:

  GET  /              → HTML page (single file, inline CSS/JS)
  GET  /api/status    → JSON: override state, dmx-active flags, sysinfo
  POST /test/white    → set output override = 0xFF
  POST /test/half     → set output override = 0x80
  POST /test/clear    → clear override

The page polls /api/status once per second to keep dynamic bits live.
Stdlib only — no Flask/Bottle/etc. — so a Pi deploy needs no extra deps.
"""

from __future__ import annotations

import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Optional

from .overrides import IdentifyOverride
from .sysinfo import LiveInfo, StaticInfo

if TYPE_CHECKING:
    from .artnet import ArtNetReceiver
    from .controller import TestController
    from .dmx import DmxFixture
    from .poe import StripMapping


# Single-page UI. Inlined so the daemon ships as one Python package.
INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>py-artnet-blaze · {hostname}</title>
<style>
  :root {{
    --bg:#0d1117; --panel:#161b22; --border:#30363d; --fg:#c9d1d9;
    --muted:#8b949e; --accent:#58a6ff; --green:#3fb950; --red:#f85149;
    --amber:#d29922; --mono:ui-monospace,SFMono-Regular,Menlo,monospace;
  }}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.45 -apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif;}}
  header{{padding:18px 24px;border-bottom:1px solid var(--border);
         display:flex;align-items:baseline;justify-content:space-between}}
  header h1{{margin:0;font-size:18px;font-weight:600}}
  header .host{{color:var(--muted);font-family:var(--mono);font-size:13px}}
  .unit-banner{{display:inline-block;margin-top:6px;padding:4px 10px;
               border-radius:4px;background:#1a1f26;border:1px solid var(--border);
               color:var(--accent);font-family:var(--mono);font-size:13px;
               letter-spacing:.04em}}
  main{{max-width:760px;margin:0 auto;padding:24px;display:grid;gap:18px}}
  .card{{background:var(--panel);border:1px solid var(--border);
        border-radius:8px;padding:18px}}
  .card h2{{margin:0 0 12px;font-size:13px;text-transform:uppercase;
           letter-spacing:.06em;color:var(--muted);font-weight:600}}
  .row{{display:flex;justify-content:space-between;gap:16px;
       padding:4px 0;font-family:var(--mono);font-size:13px}}
  .row .k{{color:var(--muted)}}
  .row .v{{text-align:right;word-break:break-all}}
  .pill{{display:inline-flex;align-items:center;gap:6px;
        padding:4px 10px;border-radius:999px;font-size:12px;
        background:#21262d;border:1px solid var(--border)}}
  .pill::before{{content:"";width:8px;height:8px;border-radius:50%;
                background:var(--muted)}}
  .pill.on::before{{background:var(--green);box-shadow:0 0 6px var(--green)}}
  .pill.warn{{color:var(--amber)}}
  .pills{{display:flex;flex-wrap:wrap;gap:8px}}
  .btn{{appearance:none;background:#21262d;border:1px solid var(--border);
       color:var(--fg);padding:10px 18px;border-radius:6px;cursor:pointer;
       font-size:14px;font-weight:500;transition:.1s background}}
  .btn:hover{{background:#30363d}}
  .btn.primary{{background:#238636;border-color:#238636;color:#fff}}
  .btn.primary:hover{{background:#2ea043}}
  .btn.danger{{background:#da3633;border-color:#da3633;color:#fff}}
  .btn.danger:hover{{background:#f85149}}
  .btn-row{{display:flex;gap:10px;flex-wrap:wrap}}
  .override-state{{margin-top:12px;padding:10px;border-radius:6px;
                  background:#0d1117;border:1px solid var(--border);
                  font-family:var(--mono);font-size:13px;color:var(--muted)}}
  .override-state.active{{color:var(--amber);border-color:var(--amber)}}
  .strip,.fixture{{display:grid;grid-template-columns:140px 1fr;gap:10px;
         align-items:center;padding:6px 0;
         border-bottom:1px solid #1a1f26}}
  .strip:last-child,.fixture:last-child{{border-bottom:none}}
  .strip .label,.fixture .label{{font-family:var(--mono);font-size:12px;
         color:var(--muted);white-space:nowrap;overflow:hidden;
         text-overflow:ellipsis}}
  .strip .label .ch,.fixture .label .ch{{color:var(--fg);font-weight:600}}
  .leds{{display:flex;gap:1px;padding:2px;background:#000;
        border-radius:4px;border:1px solid #1a1f26}}
  .led{{flex:1 1 0;min-width:3px;height:16px;border-radius:1px;
       background:#000}}
  .render{{display:flex;gap:6px;align-items:center}}
  .bar{{display:flex;flex:1;height:18px;border-radius:3px;
       overflow:hidden;background:#000;border:1px solid #1a1f26}}
  .bar .seg{{flex:1 1 0;background:#000;border-right:1px solid rgba(0,0,0,.4)}}
  .bar .seg:last-child{{border-right:none}}
  .chip{{font-family:var(--mono);font-size:11px;padding:3px 7px;
        border-radius:4px;border:1px solid var(--border);
        background:#21262d;color:var(--muted);white-space:nowrap}}
  .chip.strobe.on{{background:var(--amber);color:#000;border-color:var(--amber);
                  animation:strobe-flash 0.4s steps(2,end) infinite}}
  @keyframes strobe-flash{{0%{{opacity:1}}50%{{opacity:.4}}100%{{opacity:1}}}}
  .raw-cells{{display:flex;flex:1;gap:2px;flex-wrap:wrap}}
  .raw-cell{{flex:0 0 auto;min-width:24px;height:18px;font-family:var(--mono);
            font-size:9px;display:flex;align-items:center;
            justify-content:center;border-radius:2px;background:#1a1f26;
            color:#888;padding:0 2px}}
  footer{{text-align:center;color:var(--muted);font-size:12px;
         padding:24px}}
</style>
</head>
<body>
<header>
  <div>
    <h1>py-artnet-blaze test panel</h1>
    <div id="unit-banner" class="unit-banner" hidden></div>
  </div>
  <span class="host">{hostname}</span>
</header>
<main>

  <section class="card">
    <h2>Test patterns</h2>
    <p style="margin:0 0 12px;color:var(--muted)">
      Overrides every output byte (POE pixels and DMX slots) for visual
      confirmation. Note: on DMX fixtures with non-RGB channels (strobe,
      master dim, mode), &ldquo;all 0xFF&rdquo; may not look like literal white.
    </p>
    <div class="btn-row">
      <button class="btn primary" onclick="post('/test/white')">All white (0xFF)</button>
      <button class="btn" onclick="post('/test/half')">50% (0x80)</button>
      <button class="btn" onclick="post('/test/identify')" id="btn-identify">Identify</button>
      <button class="btn danger" onclick="post('/test/clear')">Clear</button>
    </div>
    <div id="override" class="override-state">Live ArtNet — no override active.</div>
  </section>

  <section class="card">
    <h2>Active DMX</h2>
    <p style="margin:0 0 12px;color:var(--muted)">Green = ArtNet packet seen on this universe in the last second.</p>
    <div id="active" class="pills"></div>
  </section>

  <section class="card">
    <h2>Live LED preview</h2>
    <p style="margin:0 0 12px;color:var(--muted)">
      Each row is one POE strip (rendered RGB). Colors come from the current
      ArtNet snapshot, or the override value when a test pattern is active.
    </p>
    <div id="strips"></div>
  </section>

  <section class="card" id="fixtures-card" hidden>
    <h2>Live DMX preview</h2>
    <p style="margin:0 0 12px;color:var(--muted)">
      DMX fixtures with <code>render.kind: rgb_bar</code> are visualized as
      colored RGB sections dimmed by their intensity channel; <code>raw</code>
      fixtures show one chip per channel with the live byte value (0&ndash;255).
    </p>
    <div id="fixtures"></div>
  </section>

  <section class="card">
    <h2>System</h2>
    <div id="sysinfo"></div>
  </section>

  <section class="card">
    <h2>Devices</h2>
    <div id="devices"></div>
  </section>

</main>
<footer>py-artnet-blaze · poll 1s · use /api/status for raw JSON</footer>

<script>
const $ = id => document.getElementById(id);

function row(k, v) {{
  return `<div class="row"><span class="k">${{k}}</span><span class="v">${{v}}</span></div>`;
}}

function pill(label, on) {{
  return `<span class="pill ${{on ? 'on' : ''}}">${{label}}</span>`;
}}

async function refresh() {{
  try {{
    const r = await fetch('/api/status');
    const s = await r.json();

    // Unit banner
    const banner = $('unit-banner');
    if (s.static.unit_name) {{
      banner.hidden = false;
      banner.textContent = `unit: ${{s.static.unit_name}}`;
    }} else {{
      banner.hidden = true;
    }}

    // Override
    const o = s.override;
    const el = $('override');
    if (o.active) {{
      el.classList.add('active');
      const kind = o.kind || 'uniform';
      let detail;
      if (kind === 'uniform') {{
        detail = `value=0x${{o.value.toString(16).padStart(2,'0').toUpperCase()}}`;
      }} else if (kind === 'identify') {{
        detail = `identify pattern (${{o.unit || 'unnamed'}})`;
      }} else {{
        detail = `kind=${{kind}}`;
      }}
      el.textContent = `Override active: ${{detail}}, held ${{o.elapsed_s.toFixed(1)}}s · min hold ${{o.min_hold_s}}s · live ArtNet ${{anyDmxActive(s) ? 'is' : 'NOT'}} flowing`;
    }} else {{
      el.classList.remove('active');
      el.textContent = 'Live ArtNet — no override active.';
    }}

    // Active DMX
    const universes = Object.keys(s.dmx_active).sort((a,b) => +a - +b);
    $('active').innerHTML = universes.length
      ? universes.map(u => pill(`U${{u}}`, s.dmx_active[u])).join('')
      : '<span class="pill warn">No universes subscribed</span>';

    // System
    $('sysinfo').innerHTML = [
      row('Code',     s.static.code_version),
      row('Python',   s.static.python_version),
      row('pyserial', s.static.pyserial_version),
      row('pyyaml',   s.static.pyyaml_version),
      row('OS',       s.static.os_pretty),
      row('Hostname', s.static.hostname),
      row('IP addrs', s.live.ip_addresses.length ? s.live.ip_addresses.join(', ') : '—'),
      row('Process up', s.live.process_uptime_human),
      row('System up',  s.live.system_uptime_human),
    ].join('');

    // Devices
    $('devices').innerHTML = [
      row('POE firmware',  s.static.poe_firmware),
      row('DMX dongle',    s.static.dmx_dongle),
      row('DMX protocol',  s.static.dmx_protocol || '—'),
      row('DMX firmware',  s.static.dmx_firmware || '—'),
    ].join('');

    // Live LED preview
    renderStrips(s.strips);

    // Live DMX preview
    renderFixtures(s.fixtures || []);
  }} catch (e) {{
    console.error(e);
  }}
}}

function renderFixtures(fixtures) {{
  const card = $('fixtures-card');
  if (!fixtures.length) {{
    card.hidden = true;
    return;
  }}
  card.hidden = false;
  const container = $('fixtures');
  // Reset on schema change (different count or new render kind)
  const sig = fixtures.map(f => `${{f.name}}|${{f.length}}|${{f.render.kind}}`).join(',');
  if (container.dataset.sig !== sig) {{
    container.dataset.sig = sig;
    container.innerHTML = '';
    for (const f of fixtures) {{
      const row = document.createElement('div');
      row.className = 'fixture';
      row.dataset.name = f.name;
      const label = document.createElement('div');
      label.className = 'label';
      label.innerHTML = `<span class="ch">${{escapeHTML(f.name)}}</span> · U${{f.universe}}@${{f.offset}} → DMX ${{f.dmx_start}} (${{f.length}})`;
      const render = document.createElement('div');
      render.className = 'render';
      row.appendChild(label);
      row.appendChild(render);
      container.appendChild(row);
    }}
  }}
  // Update each row's render area with current values
  const rows = container.children;
  for (let i = 0; i < fixtures.length; i++) {{
    const f = fixtures[i];
    const renderEl = rows[i].querySelector('.render');
    paintFixture(renderEl, f);
  }}
}}

function paintFixture(el, f) {{
  const r = f.render || {{kind: 'raw'}};
  if (r.kind === 'rgb_bar') {{
    const sections = r.sections || Math.floor(f.length / 3);
    const intensityAt = (typeof r.intensity_at === 'number') ? r.intensity_at : null;
    const strobeAt = (typeof r.strobe_at === 'number') ? r.strobe_at : null;
    const intensity = intensityAt != null ? f.values[intensityAt] : 255;
    const strobe = strobeAt != null ? f.values[strobeAt] : 0;
    const dim = intensity / 255;
    const segs = [];
    for (let i = 0; i < sections; i++) {{
      const r_ = Math.round((f.values[i*3] || 0) * dim);
      const g_ = Math.round((f.values[i*3+1] || 0) * dim);
      const b_ = Math.round((f.values[i*3+2] || 0) * dim);
      segs.push(`<span class="seg" style="background:rgb(${{r_}},${{g_}},${{b_}})"></span>`);
    }}
    el.innerHTML = `<div class="bar">${{segs.join('')}}</div>` +
      `<span class="chip">DIM ${{intensity}}</span>` +
      `<span class="chip strobe ${{strobe > 0 ? 'on' : ''}}">STR ${{strobe}}</span>`;
  }} else {{
    // raw — one chip per channel showing the byte value
    const cells = f.values.map(v => `<span class="raw-cell">${{v}}</span>`);
    el.innerHTML = `<div class="raw-cells">${{cells.join('')}}</div>`;
  }}
}}

function escapeHTML(s) {{
  return String(s).replace(/[&<>"']/g, c =>
    ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]);
}}

// One preview line per strip — physically each strip is a row of LEDs,
// and the identify pattern paints staircase + text-slice + white-tip
// onto every strip independently. The text is rasterized vertically
// across all strips, so each strip shows one horizontal slice of it.
const _stripCells = new Map();   // channel → array of <span class=led>
function renderStrips(strips) {{
  if (!strips || !strips.length) {{
    $('strips').textContent = '(no strips configured)';
    return;
  }}
  const container = $('strips');
  if (container.children.length !== strips.length) {{
    container.innerHTML = '';
    _stripCells.clear();
    for (const s of strips) {{
      const rowEl = document.createElement('div');
      rowEl.className = 'strip';
      const label = document.createElement('div');
      label.className = 'label';
      label.innerHTML = `<span class="ch">C${{s.channel}}</span> · U${{s.universe}}@${{s.offset}} (${{s.pixel_count}})`;
      const leds = document.createElement('div');
      leds.className = 'leds';
      const cells = [];
      for (let i = 0; i < s.pixel_count; i++) {{
        const cell = document.createElement('span');
        cell.className = 'led';
        leds.appendChild(cell);
        cells.push(cell);
      }}
      rowEl.appendChild(label);
      rowEl.appendChild(leds);
      container.appendChild(rowEl);
      _stripCells.set(s.channel, cells);
    }}
  }}
  // Update colors
  for (const s of strips) {{
    const cells = _stripCells.get(s.channel);
    if (!cells) continue;
    const hex = s.pixels;
    for (let i = 0; i < cells.length; i++) {{
      const c = '#' + hex.substr(i * 6, 6);
      if (cells[i]._last !== c) {{
        cells[i].style.background = c;
        cells[i]._last = c;
      }}
    }}
  }}
}}

function anyDmxActive(s) {{
  return Object.values(s.dmx_active).some(Boolean);
}}

async function post(path) {{
  try {{
    await fetch(path, {{method: 'POST'}});
    refresh();
  }} catch (e) {{ console.error(e); }}
}}

refresh();
setInterval(refresh, 1000);
</script>
</body>
</html>
"""


def _strip_view_hex(
    strip: "StripMapping",
    universes: dict[int, bytes],
    override,
) -> str:
    """Hex-encoded RGB triples for one strip's current visible state.

    Returns `pixel_count * 6` hex chars. Each LED is rendered RGB even
    though the POE wire reorders to GRB — the source bytes from ArtNet
    are RGB and that's what the LED actually displays.
    """
    if override is not None:
        return override.strip_pixels(strip).hex()
    uni = universes.get(strip.universe)
    if uni is None:
        return "000000" * strip.pixel_count
    start = strip.offset
    needed = strip.pixel_count * 3
    chunk = bytes(uni[start:start + needed])
    if len(chunk) < needed:
        chunk = chunk + bytes(needed - len(chunk))
    return chunk.hex()


def _strip_views(
    strips: list["StripMapping"],
    receiver: "ArtNetReceiver",
    controller: "TestController",
) -> list[dict]:
    universes = receiver.snapshot()
    override = controller.current()
    return [
        {
            "channel": s.poe_channel,
            "universe": s.universe,
            "offset": s.offset,
            "pixel_count": s.pixel_count,
            "row": getattr(s, "row", None),
            "side": getattr(s, "side", None),
            "pixels": _strip_view_hex(s, universes, override),
        }
        for s in strips
    ]


def _fixture_values(
    fixture: "DmxFixture",
    universes: dict[int, bytes],
    override,
) -> list[int]:
    if override is not None:
        return list(override.dmx_values(fixture))
    uni = universes.get(fixture.universe)
    if uni is None:
        return [0] * fixture.length
    chunk = bytes(uni[fixture.offset:fixture.offset + fixture.length])
    if len(chunk) < fixture.length:
        chunk = chunk + bytes(fixture.length - len(chunk))
    return list(chunk)


def _fixture_views(
    fixtures: list["DmxFixture"],
    receiver: "ArtNetReceiver",
    controller: "TestController",
) -> list[dict]:
    universes = receiver.snapshot()
    override = controller.current()
    out = []
    for f in fixtures:
        values = _fixture_values(f, universes, override)
        out.append({
            "name": f.name or f"U{f.universe}@{f.offset}",
            "universe": f.universe,
            "offset": f.offset,
            "dmx_start": f.dmx_start,
            "length": f.length,
            "render": f.render or {"kind": "raw"},
            "values": values,
        })
    return out


def _build_status(
    static: StaticInfo,
    controller: "TestController",
    receiver: "ArtNetReceiver",
    strips: list["StripMapping"],
    fixtures: list["DmxFixture"],
    log: logging.Logger,
) -> dict:
    return {
        "static": static.__dict__,
        "live": LiveInfo.snapshot(static, log).__dict__,
        "override": controller.state(),
        "dmx_active": {str(u): v for u, v in controller.dmx_active().items()},
        "strips": _strip_views(strips, receiver, controller),
        "fixtures": _fixture_views(fixtures, receiver, controller),
    }


def make_handler(
    static: StaticInfo,
    controller: "TestController",
    receiver: "ArtNetReceiver",
    strips: list["StripMapping"],
    fixtures: list["DmxFixture"],
    log: logging.Logger,
):
    """Return a BaseHTTPRequestHandler subclass closed over our deps."""

    class Handler(BaseHTTPRequestHandler):
        # Quiet the default per-request access log; route into our logger.
        def log_message(self, fmt, *args):  # noqa: A003 (override stdlib)
            log.debug("http %s - %s", self.address_string(), fmt % args)

        def _respond_json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _respond_html(self, html: str) -> None:
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _respond_404(self) -> None:
            self._respond_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

        def do_GET(self) -> None:  # noqa: N802 (stdlib API)
            if self.path == "/" or self.path == "/index.html":
                self._respond_html(INDEX_HTML.format(hostname=static.hostname))
            elif self.path == "/api/status":
                self._respond_json(
                    _build_status(static, controller, receiver,
                                  strips, fixtures, log)
                )
            elif self.path == "/healthz":
                self._respond_json({"ok": True})
            else:
                self._respond_404()

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/test/white":
                controller.set_value(0xFF)
                self._respond_json({"ok": True, "value": 0xFF})
            elif self.path == "/test/half":
                controller.set_value(0x80)
                self._respond_json({"ok": True, "value": 0x80})
            elif self.path == "/test/identify":
                controller.set_override(
                    IdentifyOverride(unit_name=static.unit_name, strips=strips)
                )
                self._respond_json({"ok": True, "kind": "identify",
                                    "unit": static.unit_name})
            elif self.path == "/test/clear":
                controller.clear()
                self._respond_json({"ok": True})
            else:
                self._respond_404()

    return Handler


class HttpServerThread:
    """Daemon thread that serves the test panel.

    `start()` blocks briefly for the bind so callers can read `port`
    before returning. `stop()` shuts down the server cleanly.
    """

    def __init__(
        self,
        bind: str,
        port: int,
        static: StaticInfo,
        controller: "TestController",
        receiver: "ArtNetReceiver",
        strips: list["StripMapping"],
        fixtures: list["DmxFixture"],
        log: logging.Logger,
    ) -> None:
        self.log = log
        self.handler_cls = make_handler(
            static, controller, receiver, strips, fixtures, log
        )
        self.server = ThreadingHTTPServer((bind, port), self.handler_cls)
        self.server.daemon_threads = True
        self.thread: Optional[threading.Thread] = None

    @property
    def bind(self) -> str:
        return self.server.server_address[0]

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="http-api",
            daemon=True,
        )
        self.thread.start()
        self.log.info("HTTP test panel listening on http://%s:%d", self.bind, self.port)

    def stop(self) -> None:
        try:
            self.server.shutdown()
        except Exception as e:
            self.log.warning("http shutdown failed: %s", e)
        try:
            self.server.server_close()
        except Exception:
            pass
        if self.thread:
            self.thread.join(timeout=2.0)
