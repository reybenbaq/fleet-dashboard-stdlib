# Design notes

Rationale behind `solid-gps-dashboard`. The README covers what the modules do. This document covers why the architecture takes this shape.

## Template substitution strategy

The HTML template is a 450-line module-level string constant. It contains CSS, JavaScript, and HTML. That combination rules out the obvious substitution approaches.

`str.format()` fails on the first CSS custom property (`var(--color-bg)` contains `{` and `}`). f-strings have the same problem. `%s` substitution is marginally better but still conflicts with CSS `content` values and JavaScript template literals.

The correct approach for injecting data into a document that already uses curly braces is to choose a token syntax that has no overlap with the document's own syntax. HTML comment tokens (`<!-- TOKEN -->`) are:

- Invisible to CSS and JavaScript parsers
- Unambiguous to `str.replace()` (no accidental collisions with template content)
- Human-readable (the template is readable on its own before substitution)

The builder replaces each token exactly once. The replacement order matters for one case: `<!-- LEAFLET_CSS -->` must be replaced before `<!-- LEAFLET_JS -->` because the Leaflet CSS references relative image paths that Leaflet JS resolves during initialisation. Getting the order wrong produces a broken map.

## Why the Device dataclass exists

The naive approach is to pass `dict` rows directly to the fragment builders. That works for five fields. It breaks down as the number of fields grows because every call site that reads a field spells out the field name as a string. A typo in a field name is a runtime `KeyError`, not a type error.

The `Device` dataclass converts the CSV row once, at parse time. From that point on, every call site uses attribute access. The type annotations let the IDE report the error at write time, not at runtime.

More importantly, the derived properties live in the class:

- `has_coordinates` — guards the map marker loop cleanly. Without it, every caller would repeat `d.lat is not None and d.lon is not None`.
- `status_color`, `battery_color` — lookup logic that would otherwise be duplicated in every fragment builder that renders a coloured element.
- `status_sort_key` — the sort function is one key expression: `key=lambda d: (d.status_sort_key, d.device_id)`. Without the property, that lambda would embed a list lookup with a fallback.
- `display_name` — falls back to `device_id` when `name` is blank. Without the property, every renderer would write `d.name if d.name else d.device_id`.

## Parsing philosophy

Each parsing helper has a single contract: convert a raw string to a typed value or `None`. Never crash. Always log a warning when something unexpected happens.

**`_clamp_battery`** — battery readings from GPS hardware can be out of range due to sensor calibration drift. Clamping preserves the information (the device is reporting something) while correcting the display value. Discarding out-of-range readings would silently remove real devices from the battery status columns.

**`_parse_coordinates`** — both lat and lon must be valid floats for the device to appear on the map. A partial coordinate is worse than no coordinate; a marker placed at `(lat, 0)` would appear in the Atlantic Ocean. The function returns `(None, None)` on any parse failure. The `Device.has_coordinates` property then guards the map marker loop.

**`_parse_last_seen`** — the format is fixed at `%Y-%m-%d %H:%M:%S`. Looser parsing (trying multiple formats) would paper over data quality issues that the operator should fix at the source. Strict parsing with a warning log surfaces them.

**`_normalise_status`** — unknown status values are passed through (not mapped to `unknown`). This preserves the raw value in the status badge so the operator can see what the device is actually reporting. The warning log flags it for investigation.

## Relative-time formatting

`_format_relative_time` computes human-readable relative time from a `timedelta`. The logic handles five ranges:

- Sub-60 seconds: "Just now"
- Minutes (< 60): "Xm ago"
- Hours with remainder: "Xh Ym ago" or "Xh ago"
- Days with remainder: "Xd Yh ago" or "Xd ago"
- Future dates (negative delta): formatted as `YYYY-MM-DD HH:MM`. Devices with unsynchronised clocks report future timestamps. A negative relative time would confuse the operator

The reference "now" is a hardcoded constant (`REFERENCE_NOW = datetime(2026, 4, 24, 9, 0, 0)`), matching the snapshot timestamp in the challenge spec. Using `datetime.now()` would cause the relative-time values to change every time the script runs, making the output non-deterministic. The hardcoded reference ensures the generated dashboard matches the expected output.

## Leaflet embedding

`fetch_leaflet_assets()` downloads the Leaflet CSS and JS from unpkg via `urllib.request`. The assets are passed as strings to `build_html()`, which inlines them into `<style>` and `<script>` blocks.

The embedding strategy means:

- The output file has no external dependencies after generation
- It works in air-gapped environments, on planes, in dev environments with restricted outbound
- There is no CDN availability risk at view time

The download happens once per run. If the CDN is unreachable, `fetch_leaflet_assets()` raises immediately with a clear error. The script does not partially generate the file and then fail mid-write.

The project pins Leaflet 1.9.4 because it is the current stable release and its CDN URL is stable. The constants hold the version. Updating it is a one-line change.

## What was deliberately left out

**Server-side rendering.** A Flask server could serve the dashboard live from a running process. That would add a deployment requirement (keep a process running, expose a port) that defeats the zero-setup goal. A static file is simpler to hand off. The consumer opens it in a browser. Done.

**Template engine.** Jinja2 would simplify the template. It is also an external dependency. The constraint was strict: standard library only. The `<!-- TOKEN -->` approach achieves the same result without the import.

**Pandas.** `pandas.read_csv()` is one line. `csv.DictReader` is also one line, plus a loop. The delta in development effort is small. The delta in the dependency footprint is not.

**Client-side interactivity beyond the map.** The map popups are interactive (click to expand). The table is not filterable. Adding a `<input>` filter would be 20 lines of JavaScript in the existing `<script>` block. It was not part of the challenge spec. The architecture supports adding it without restructuring anything.
