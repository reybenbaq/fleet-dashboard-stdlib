# solid-gps-dashboard

> Self-contained fleet dashboard generator in Python standard library only. No third-party packages. No setup. Double-click to run. The challenge was to build something that looks like a real product without reaching for the tools most developers assume they need.

## What this is

A single Python script that reads a CSV of GPS device records and outputs a fully self-contained HTML dashboard — interactive map, device status table, battery indicators, relative-time formatting, and status summary chips — in one file you can open in any browser, online or off.

The constraint was strict: Python standard library only. No pandas, no Jinja2, no Plotly, no chart library of any kind. The map tiles come from OpenStreetMap at run-time via `urllib.request`, and the Leaflet CSS and JS are embedded inline in the output so the result works completely offline after generation.

This is a challenge-response sample, not a stripped-down production system. The architecture decisions, validation logic, and failure-mode handling reflect how the problem would be solved in a real fleet operations context.

## The challenge

Most developers who hit a "build a dashboard from a CSV" requirement reach for pandas and a charting library before reading past the first line of the spec. That's the path of least resistance. It's also the path that adds a dependency footprint, a setup step, and a version-pinning surface to what could be a zero-friction deliverable.

The challenge added one additional constraint: the output must work with minimal or no internet connection.

That combination — no external libraries at import time, and offline-capable output — rules out the standard toolkit. Solving it requires falling back to Python fundamentals: the `csv` module, `dataclasses`, `datetime`, `json`, `pathlib`, `urllib.request`, and string manipulation. The map is embedded by fetching the Leaflet assets once at generation time and inlining them into the HTML. After that, the file is self-contained forever.

The hard part is not the data parsing. It is building a polished, functional UI without a templating engine, without a component library, and without a charting framework, while still handling edge cases — missing coordinates, out-of-range battery values, unparseable timestamps, unknown status codes — without crashing.

## What the output looks like

One HTML file. Open it in a browser. No server. No installation.

| Section | Detail |
|---|---|
| Status summary bar | Count chips for each status (active, idle, low battery, offline, maintenance), colour-coded by priority |
| Interactive map | Leaflet map with coloured circle markers per device. Click a marker for a popup: device ID, name, status, battery, location, last seen |
| Device table | All devices sorted by status priority, then ID. Columns: ID, Name, Status (badge), Battery (visual bar + percentage), Location, Last Seen (relative time) |
| Offline mode | If a device has no valid coordinates, it appears in the table only, with a warning flag on its ID |

The generated file is roughly 200 KB including the embedded Leaflet assets. It opens instantly.

## Architecture

```
fleet_status.csv
      │
      ▼
 load_devices()
 (csv.DictReader + Device dataclass)
      │
      ├── _normalise_status()
      ├── _clamp_battery()
      ├── _parse_coordinates()
      └── _parse_last_seen()
      │
      ▼
 fetch_leaflet_assets()
 (urllib.request — one HTTP fetch, then embedded)
      │
      ▼
 build_html()
 (str.replace() on <!-- TOKEN --> placeholders
  — never f-strings, never format() on the template)
      │
      ▼
 fleet_dashboard.html
 (self-contained, offline-capable)
```

The template uses `<!-- TOKEN -->` comment placeholders, not `{}` or `%s`. That is the only safe substitution strategy when the template contains CSS and JavaScript with their own curly-brace syntax. `str.format()` or f-strings on the template would corrupt the CSS at the first class name.

## What's in here

| File | Responsibility |
|---|---|
| `src/fleet_dashboard.py` | Single entry point. Data model, CSV reader, parsing helpers, HTML fragment builders, Leaflet asset fetcher, template, and `main()`. Pure standard library |
| `data/fleet_status.csv` | Sample input. 35 devices across Australian cities. Includes intentional edge cases: missing coordinates, out-of-range battery, unparseable lat, future timestamp, unknown status |
| `docs/design.md` | Design rationale — why the template substitution works the way it does, how the failure cases are handled, and what was deliberately left out |

## Running it

```bash
python src/fleet_dashboard.py
```

Output: `fleet_dashboard.html` in the same directory as the script. Requires one internet connection during generation (to fetch Leaflet). The output file works offline after that.

No virtual environment. No `pip install`. No configuration.

## Data format

The script reads `fleet_status.csv` from the same directory as the script. Column expectations:

| Column | Type | Notes |
|---|---|---|
| `device_id` | string | Required. Rows missing this are skipped with a warning |
| `name` | string | Optional. Falls back to `device_id` in the UI if blank |
| `status` | string | Expected values: `active`, `idle`, `low_battery`, `offline`, `maintenance`. Unknown values render with a distinct colour and a warning log |
| `battery_pct` | integer | Optional. Clamped to [0, 100]. Out-of-range values are clamped with a warning. Non-integer values treated as missing |
| `lat` | float | Optional. Both `lat` and `lon` must be valid for a device to appear on the map |
| `lon` | float | Optional. Same |
| `last_seen` | `YYYY-MM-DD HH:MM:SS` | Optional. Future dates display as a formatted timestamp, not relative time |
| `location` | string | Optional. Displayed in the table and the map popup |

Missing or malformed values are logged and handled gracefully. The script never crashes on bad input.

## Design decisions worth calling out

**`<!-- TOKEN -->` placeholders, not f-strings.** The HTML template is a module-level string constant. Applying `str.format()` or an f-string to it would fail on the first CSS rule that uses `{}` — every custom property definition, every keyframe, every calc expression. Comment-style tokens are invisible to CSS and JavaScript parsers and unambiguous to `str.replace()`. The template is injected in the correct order: Leaflet CSS first (it uses `url()` references that must appear before Leaflet JS initialisation), then JS, then the data payloads.

**`dataclass` as the data model.** A plain `dict` per row works, but it pushes the field-name strings to every call site. The `Device` dataclass centralises field names, gives type annotations for IDE support, and puts derived properties (`has_coordinates`, `status_color`, `battery_color`, `status_sort_key`) in one place. Adding a new display column means editing one class, not hunting through fragment builders.

**Clamping, not rejecting, on bad battery values.** Out-of-range battery readings (`-5`, `150`) come from real GPS hardware. The sensor calculates percentage from raw voltage against a lookup table. Calibration drift and temperature can push the reading slightly outside [0, 100]. Rejecting these as invalid would silently remove real devices from the status bars. Clamping gives the operator a usable reading and a log warning.

**Relative-time formatting without `dateutil`.** `_format_relative_time()` computes "5m ago" / "3h 12m ago" / "2d ago" purely from `datetime.timedelta` arithmetic. It handles the full range: sub-60-second, minute, hour-with-remainder, day-with-hour-remainder. Future timestamps (devices with clocks not yet synced) display as a formatted date instead of a negative relative time.

**Embedding Leaflet, not linking it.** Linking to a CDN is one line. It also means the dashboard breaks when the machine has no internet. The challenge explicitly required offline-capable output. `urllib.request.urlopen()` downloads the CSS and JS at generation time. `str.replace()` inlines them into `<style>` and `<script>` blocks. The output file carries its own map renderer.

**Status sort priority, not alphabetical.** The device table sorts by `STATUS_ORDER` index first, then by `device_id`. Active devices appear at the top because fleet operators care most about active vehicles. Offline devices appear after low-battery because offline means already down, not about-to-go-down. The sort is deterministic and reproducible.

## Not in scope

- **Real-time updates.** The dashboard is a snapshot, not a live feed. Refreshing the data means re-running the script. A live dashboard would require a server, a WebSocket connection, or a polling refresh — all of which add a deployment requirement that defeats the zero-setup goal.
- **Filtering and search.** The table renders all devices. Client-side filtering is straightforward to add (a `<input>` that filters rows by `textContent`) but was not part of the challenge.
- **Authentication.** The output file is a static HTML file. It has no auth surface. If the data is sensitive, the protection is at the file-system level, not in the dashboard.
- **Database or API input.** The script reads a CSV. Adapting it to read from a REST API or a database query is a two-line change to `load_devices()`. The CSV is the simplest possible input format that demonstrates the full pipeline.
- **Tests.** The parsing helpers are unit-testable pure functions. A test file was not part of the challenge deliverable.

## License

MIT. See `LICENSE`.
