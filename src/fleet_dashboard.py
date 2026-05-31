"""
fleet_dashboard.py

Reads fleet_status.csv and generates a self-contained fleet_dashboard.html.
Python standard library only — no third-party packages.

Leaflet 1.9.4 CSS and JS are downloaded at run-time via urllib.request and
embedded inline so the output HTML works completely offline after generation.

Reference "now" for relative-time calculations: 2026-04-24 09:00:00 (snapshot
timestamp).
"""

import csv
import json
import logging
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
CSV_PATH = SCRIPT_DIR / "fleet_status.csv"
OUTPUT_PATH = SCRIPT_DIR / "fleet_dashboard.html"

# Snapshot reference timestamp — hardcoded per spec
REFERENCE_NOW = datetime(2026, 4, 24, 9, 0, 0)

LEAFLET_CSS_URL = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
LEAFLET_JS_URL = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
LEAFLET_DOWNLOAD_TIMEOUT = 20

# Status display priority (lower index = shown first in device table)
STATUS_ORDER = ["active", "idle", "low_battery", "offline", "maintenance", "unknown"]

STATUS_COLORS: dict[str, str] = {
    "active": "#22c55e",
    "idle": "#f59e0b",
    "offline": "#64748b",
    "low_battery": "#f97316",
    "maintenance": "#3b82f6",
    "unknown": "#9ca3af",
}

STATUS_LABELS: dict[str, str] = {
    "active": "Active",
    "idle": "Idle",
    "low_battery": "Low Battery",
    "offline": "Offline",
    "maintenance": "Maintenance",
    "unknown": "Unknown",
}

BATTERY_COLOR_GREEN = "#22c55e"
BATTERY_COLOR_AMBER = "#f59e0b"
BATTERY_COLOR_RED = "#ef4444"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Device:
    device_id: str
    name: str
    status: str
    battery_pct: int | None
    lat: float | None
    lon: float | None
    last_seen: datetime | None
    location: str
    raw_issues: list[str] = field(default_factory=list)

    @property
    def has_coordinates(self) -> bool:
        return self.lat is not None and self.lon is not None

    @property
    def display_name(self) -> str:
        return self.name if self.name else self.device_id

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status.replace("_", " ").title())

    @property
    def status_color(self) -> str:
        return STATUS_COLORS.get(self.status, STATUS_COLORS["unknown"])

    @property
    def battery_color(self) -> str:
        if self.battery_pct is None:
            return STATUS_COLORS["unknown"]
        if self.battery_pct >= 50:
            return BATTERY_COLOR_GREEN
        if self.battery_pct >= 20:
            return BATTERY_COLOR_AMBER
        return BATTERY_COLOR_RED

    @property
    def status_sort_key(self) -> int:
        try:
            return STATUS_ORDER.index(self.status)
        except ValueError:
            return len(STATUS_ORDER)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _clamp_battery(raw: str, device_id: str) -> int | None:
    """Parse battery percentage, clamping to [0, 100]. Returns None if missing."""
    if not raw.strip():
        return None
    try:
        value = int(raw.strip())
        clamped = max(0, min(100, value))
        if clamped != value:
            logger.warning(
                "%s: battery_pct %d out of range — clamped to %d",
                device_id,
                value,
                clamped,
            )
        return clamped
    except ValueError:
        logger.warning(
            "%s: battery_pct '%s' is not an integer — treated as None",
            device_id,
            raw,
        )
        return None


def _parse_coordinates(
    lat_raw: str, lon_raw: str, device_id: str
) -> tuple[float | None, float | None]:
    """Parse lat/lon strings. Returns (None, None) on any parse failure."""
    if not lat_raw.strip() or not lon_raw.strip():
        return None, None
    try:
        lat = float(lat_raw.strip())
        lon = float(lon_raw.strip())
        return lat, lon
    except ValueError:
        logger.warning(
            "%s: invalid coordinates ('%s', '%s') — device skipped from map",
            device_id,
            lat_raw,
            lon_raw,
        )
        return None, None


def _parse_last_seen(raw: str, device_id: str) -> datetime | None:
    """Parse ISO-like last_seen timestamp. Accepts future dates without error."""
    if not raw.strip():
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        logger.warning(
            "%s: last_seen '%s' could not be parsed — treated as None",
            device_id,
            raw,
        )
        return None


def _normalise_status(raw: str, device_id: str) -> str:
    """Normalise status to a known key. Passes through unknown statuses."""
    normalised = raw.strip().lower()
    if normalised not in STATUS_COLORS:
        logger.warning(
            "%s: unknown status '%s' — rendered with distinct colour",
            device_id,
            raw,
        )
    return normalised if normalised else "unknown"


def _format_relative_time(last_seen: datetime | None) -> str:
    """Return human-readable relative time against REFERENCE_NOW."""
    if last_seen is None:
        return "Unknown"
    delta = REFERENCE_NOW - last_seen
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        # Future date — display as-is per spec
        return last_seen.strftime("%Y-%m-%d %H:%M")
    if total_seconds < 60:
        return "Just now"
    minutes = total_seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    if hours < 24:
        if remaining_minutes:
            return f"{hours}h {remaining_minutes}m ago"
        return f"{hours}h ago"
    days = hours // 24
    remaining_hours = hours % 24
    if remaining_hours:
        return f"{days}d {remaining_hours}h ago"
    return f"{days}d ago"


# ---------------------------------------------------------------------------
# Leaflet asset fetching
# ---------------------------------------------------------------------------


def _fetch_text(url: str, timeout: int) -> str:
    """Download a URL and return its text content. Raises on failure."""
    logger.info("Fetching %s ...", url)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            raw_bytes = response.read()
        logger.info("Downloaded %s (%d bytes)", url, len(raw_bytes))
        return raw_bytes.decode("utf-8")
    except Exception as exc:
        raise RuntimeError(
            f"Failed to download Leaflet asset from {url}: {exc}"
        ) from exc


def fetch_leaflet_assets() -> tuple[str, str]:
    """Return (leaflet_css, leaflet_js) as strings. Raises on any download error."""
    css = _fetch_text(LEAFLET_CSS_URL, LEAFLET_DOWNLOAD_TIMEOUT)
    js = _fetch_text(LEAFLET_JS_URL, LEAFLET_DOWNLOAD_TIMEOUT)
    return css, js


# ---------------------------------------------------------------------------
# CSV reader
# ---------------------------------------------------------------------------


def load_devices(csv_path: Path) -> list[Device]:
    """Load and validate all devices from the CSV. Never crashes — logs issues."""
    devices: list[Device] = []
    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            device_id = row.get("device_id", "").strip()
            if not device_id:
                logger.warning("Row missing device_id — skipped: %s", row)
                continue

            name = row.get("name", "").strip()
            raw_status = row.get("status", "unknown")
            status = _normalise_status(raw_status, device_id)

            battery_pct = _clamp_battery(row.get("battery_pct", ""), device_id)
            lat, lon = _parse_coordinates(
                row.get("lat", ""), row.get("lon", ""), device_id
            )
            last_seen = _parse_last_seen(row.get("last_seen", ""), device_id)
            location = row.get("location", "").strip()

            device = Device(
                device_id=device_id,
                name=name,
                status=status,
                battery_pct=battery_pct,
                lat=lat,
                lon=lon,
                last_seen=last_seen,
                location=location,
            )
            devices.append(device)
            logger.info(
                "Loaded %s (%s) — status=%s", device_id, device.display_name, status
            )

    logger.info("Loaded %d devices from %s", len(devices), csv_path)
    return devices


# ---------------------------------------------------------------------------
# Summary computation
# ---------------------------------------------------------------------------


def build_summary(devices: list[Device]) -> dict[str, int]:
    """Return count per status, preserving STATUS_ORDER sort."""
    counts: dict[str, int] = {}
    for device in devices:
        counts[device.status] = counts.get(device.status, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# HTML fragment builders
# ---------------------------------------------------------------------------


def _battery_bar_html(battery_pct: int | None, color: str) -> str:
    """Render a battery bar with percentage label, or N/A if missing."""
    if battery_pct is None:
        return '<span class="battery-unknown">N/A</span>'
    pct = battery_pct
    return (
        f'<div class="battery-bar-wrapper" title="{pct}%">'
        f'<div class="battery-bar-fill" style="width:{pct}%;background:{color}"></div>'
        f"</div>"
        f'<span class="battery-label">{pct}%</span>'
    )


def _status_badge_html(status: str, label: str, color: str) -> str:
    """Render a coloured status pill badge."""
    return (
        f'<span class="status-badge" '
        f'style="background:{color}20;color:{color};border:1px solid {color}40">'
        f"{label}</span>"
    )


def _map_markers_json(devices: list[Device]) -> str:
    """Build a JSON array of marker data for Leaflet (mapped devices only)."""
    markers = []
    for d in devices:
        if not d.has_coordinates:
            continue
        markers.append(
            {
                "id": d.device_id,
                "name": d.display_name,
                "status": d.status,
                "statusLabel": d.status_label,
                "color": d.status_color,
                "lat": d.lat,
                "lon": d.lon,
                "battery": d.battery_pct,
                "location": d.location,
                "lastSeen": _format_relative_time(d.last_seen),
            }
        )
    return json.dumps(markers)


def _summary_chips_html(summary: dict[str, int]) -> str:
    """Render summary count chips ordered by STATUS_ORDER, then any extras."""
    chips: list[str] = []

    def _chip(status: str, count: int) -> str:
        color = STATUS_COLORS.get(status, STATUS_COLORS["unknown"])
        label = STATUS_LABELS.get(status, status.replace("_", " ").title())
        return (
            f'<div class="summary-chip" style="border-left:4px solid {color}">'
            f'<span class="chip-count" style="color:{color}">{count}</span>'
            f'<span class="chip-label">{label}</span>'
            f"</div>"
        )

    for status in STATUS_ORDER:
        count = summary.get(status, 0)
        if count > 0:
            chips.append(_chip(status, count))

    for status, count in summary.items():
        if status not in STATUS_ORDER and count > 0:
            chips.append(_chip(status, count))

    return "\n".join(chips)


def _device_rows_html(devices: list[Device]) -> str:
    """Render all device rows sorted by status priority."""
    sorted_devices = sorted(devices, key=lambda d: (d.status_sort_key, d.device_id))
    rows: list[str] = []
    for d in sorted_devices:
        badge = _status_badge_html(d.status, d.status_label, d.status_color)
        battery_html = _battery_bar_html(d.battery_pct, d.battery_color)
        relative_time = _format_relative_time(d.last_seen)
        location = d.location if d.location else "—"
        map_flag = (
            ""
            if d.has_coordinates
            else ' <span class="no-map-flag" title="Not shown on map — no valid coordinates">&#9888;</span>'
        )
        rows.append(
            f"<tr>"
            f'<td class="col-id">{d.device_id}{map_flag}</td>'
            f'<td class="col-name">{d.display_name}</td>'
            f'<td class="col-status">{badge}</td>'
            f'<td class="col-battery">{battery_html}</td>'
            f'<td class="col-location">{location}</td>'
            f'<td class="col-lastseen">{relative_time}</td>'
            f"</tr>"
        )
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# HTML template and builder
# ---------------------------------------------------------------------------

# NOTE: Template uses <!-- TOKEN --> placeholders exclusively.
# str.replace() is used for substitution to avoid any conflict with CSS/JS
# curly braces. f-strings are NEVER applied to the template string.

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Fleet Dashboard &mdash; SolidGPS</title>
<style>
<!-- LEAFLET_CSS -->
</style>
<style>
/* =========================================================================
   Fleet Dashboard — custom styles
   Reduced-motion: all transitions use the opt-in form (prefers-reduced-motion:
   no-preference) so they fail safe for users who prefer no motion.
   ========================================================================= */

/* ---- Reset & Base ---- */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { font-size: 14px; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
               "Helvetica Neue", Arial, sans-serif;
  background: #0f172a;
  color: #e2e8f0;
  min-height: 100vh;
}

/* ---- Header ---- */
.site-header {
  background: #1e293b;
  border-bottom: 1px solid #334155;
  padding: 0 1.5rem;
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 56px;
}
.site-header__brand {
  display: flex;
  align-items: center;
  gap: 0.625rem;
}
.brand-icon {
  width: 28px;
  height: 28px;
  background: #22c55e;
  border-radius: 6px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.brand-icon svg { width: 16px; height: 16px; }
.brand-name {
  font-size: 1rem;
  font-weight: 700;
  color: #f1f5f9;
}
.brand-sub {
  font-size: 0.75rem;
  color: #64748b;
}
.header-meta {
  font-size: 0.75rem;
  color: #64748b;
}

/* ---- Dashboard grid ---- */
.dashboard {
  display: grid;
  grid-template-rows: auto auto 1fr auto;
  min-height: calc(100vh - 56px);
  padding: 1.25rem 1.5rem;
  gap: 1.25rem;
  max-width: 1600px;
  margin: 0 auto;
  width: 100%;
}

/* ---- Summary banner ---- */
.summary-banner {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
  align-items: center;
}
.summary-title {
  font-size: 0.7rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: #64748b;
  margin-right: 0.25rem;
  flex-shrink: 0;
}
.summary-chip {
  background: #1e293b;
  border: 1px solid #334155;
  border-radius: 8px;
  padding: 0.5rem 0.875rem;
  display: flex;
  align-items: center;
  gap: 0.5rem;
  min-width: 80px;
}
.chip-count {
  font-size: 1.25rem;
  font-weight: 700;
  line-height: 1;
}
.chip-label {
  font-size: 0.75rem;
  color: #94a3b8;
  line-height: 1.2;
}

/* ---- Main content grid (map + table side-by-side on desktop) ---- */
.main-content {
  display: grid;
  grid-template-columns: 1fr;
  gap: 1.25rem;
}

@media (min-width: 900px) {
  .main-content {
    grid-template-columns: 1fr 1fr;
  }
}

/* ---- Card (BEM) ---- */
.card {
  background: #1e293b;
  border: 1px solid #334155;
  border-radius: 10px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.card__header {
  padding: 0.75rem 1rem;
  border-bottom: 1px solid #334155;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-shrink: 0;
}
.card__title {
  font-size: 0.8125rem;
  font-weight: 600;
  color: #cbd5e1;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
.card__count {
  font-size: 0.75rem;
  color: #64748b;
  background: #0f172a;
  border-radius: 12px;
  padding: 0.125rem 0.5rem;
}

/* ---- Map ---- */
#map {
  flex: 1;
  height: 480px;
}
.leaflet-popup-content-wrapper {
  background: #1e293b;
  color: #e2e8f0;
  border: 1px solid #334155;
  border-radius: 8px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.4);
}
.leaflet-popup-tip { background: #1e293b; }
.leaflet-popup-content { margin: 10px 14px; }
.popup-id { font-size: 0.7rem; color: #64748b; }
.popup-name { font-size: 0.9rem; font-weight: 600; margin: 2px 0 6px; }
.popup-row { font-size: 0.8rem; color: #94a3b8; margin: 2px 0; }
.popup-row span { color: #e2e8f0; }
.popup-status-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 5px;
  vertical-align: middle;
}

/* ---- Device table ---- */
.table-wrapper {
  flex: 1;
  overflow-y: auto;
  overflow-x: auto;
  min-height: 0;
}
.device-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.8125rem;
}
.device-table thead th {
  position: sticky;
  top: 0;
  background: #1e293b;
  padding: 0.625rem 0.875rem;
  text-align: left;
  font-size: 0.7rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: #64748b;
  border-bottom: 1px solid #334155;
  white-space: nowrap;
  z-index: 1;
}
.device-table tbody tr {
  border-bottom: 1px solid #1a2438;
}
@media (prefers-reduced-motion: no-preference) {
  .device-table tbody tr {
    transition: background 0.12s ease;
  }
  .battery-bar-fill {
    transition: width 0.3s ease;
  }
}
.device-table tbody tr:hover { background: #253047; }
.device-table tbody td {
  padding: 0.5rem 0.875rem;
  vertical-align: middle;
  color: #cbd5e1;
}

/* Column widths */
.col-id       { width: 100px; font-family: monospace; font-size: 0.8rem; color: #64748b; }
.col-name     { min-width: 140px; color: #e2e8f0; font-weight: 500; }
.col-status   { width: 120px; }
.col-battery  { width: 140px; }
.col-location { width: 110px; color: #94a3b8; }
.col-lastseen { width: 100px; white-space: nowrap; color: #94a3b8; }

/* ---- Status badge ---- */
.status-badge {
  display: inline-block;
  padding: 0.2rem 0.55rem;
  border-radius: 20px;
  font-size: 0.7rem;
  font-weight: 600;
  white-space: nowrap;
  letter-spacing: 0.03em;
}

/* ---- Battery bar ---- */
.battery-bar-wrapper {
  display: inline-block;
  width: 60px;
  height: 8px;
  background: #334155;
  border-radius: 4px;
  overflow: hidden;
  vertical-align: middle;
  margin-right: 6px;
}
.battery-bar-fill {
  height: 100%;
  border-radius: 4px;
}
.battery-label {
  font-size: 0.75rem;
  color: #94a3b8;
  vertical-align: middle;
}
.battery-unknown {
  font-size: 0.75rem;
  color: #475569;
}

/* ---- No-map warning flag ---- */
.no-map-flag {
  color: #f59e0b;
  font-size: 0.85rem;
  cursor: help;
  margin-left: 3px;
}

/* ---- Footer ---- */
.site-footer {
  font-size: 0.7rem;
  color: #475569;
  text-align: right;
  padding: 0.5rem 0;
  border-top: 1px solid #1e293b;
}

/* ---- Scrollbar styling (WebKit) ---- */
.table-wrapper::-webkit-scrollbar { width: 6px; height: 6px; }
.table-wrapper::-webkit-scrollbar-track { background: #0f172a; }
.table-wrapper::-webkit-scrollbar-thumb { background: #334155; border-radius: 3px; }
</style>
</head>
<body>

<header class="site-header">
  <div class="site-header__brand">
    <div class="brand-icon" aria-hidden="true">
      <svg viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M8 1.5C5.5 1.5 3.5 3.5 3.5 6c0 3.5 4.5 8.5 4.5 8.5S12.5 9.5 12.5 6c0-2.5-2-4.5-4.5-4.5Z"
              fill="#0f172a" stroke="#0f172a" stroke-width="0.5"/>
        <circle cx="8" cy="6" r="1.5" fill="#22c55e"/>
      </svg>
    </div>
    <div>
      <div class="brand-name">SolidGPS Fleet</div>
      <div class="brand-sub">Fleet Operations Dashboard</div>
    </div>
  </div>
  <div class="header-meta">Snapshot: 2026-04-24 09:00 AEST</div>
</header>

<main class="dashboard">

  <section class="summary-banner" aria-label="Fleet status summary">
    <span class="summary-title">Fleet Status</span>
    <!-- SUMMARY_CHIPS -->
  </section>

  <div class="main-content">

    <div class="card" aria-label="Fleet map">
      <div class="card__header">
        <h2 class="card__title">Live Map</h2>
        <span class="card__count"><!-- MAP_DEVICE_COUNT --> plotted</span>
      </div>
      <div id="map" role="application" aria-label="Interactive fleet map showing device locations"></div>
    </div>

    <div class="card" aria-label="Device list">
      <div class="card__header">
        <h2 class="card__title">Device List</h2>
        <span class="card__count"><!-- TOTAL_DEVICE_COUNT --> devices</span>
      </div>
      <div class="table-wrapper">
        <table class="device-table" aria-label="Fleet devices status table">
          <thead>
            <tr>
              <th scope="col" class="col-id">ID</th>
              <th scope="col" class="col-name">Name</th>
              <th scope="col" class="col-status">Status</th>
              <th scope="col" class="col-battery">Battery</th>
              <th scope="col" class="col-location">Location</th>
              <th scope="col" class="col-lastseen">Last Seen</th>
            </tr>
          </thead>
          <tbody>
            <!-- DEVICE_ROWS -->
          </tbody>
        </table>
      </div>
    </div>

  </div>

  <footer class="site-footer">
    Generated <!-- GENERATED_AT --> &bull; <!-- TOTAL_DEVICE_COUNT --> devices
    &bull; Reference now: 2026-04-24 09:00:00
  </footer>

</main>

<script>
<!-- LEAFLET_JS -->
</script>
<script>
// ---------------------------------------------------------------------------
// Leaflet map initialisation
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
  const markers = <!-- MARKERS_JSON -->;

  const mapEl = document.getElementById('map');

  if (!markers.length) {
    mapEl.innerHTML =
      '<p style="padding:1rem;color:#64748b">No devices with valid coordinates.</p>';
    return;
  }

  const map = L.map('map', { zoomControl: true });

  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 19,
  }).addTo(map);

  const bounds = L.latLngBounds();

  markers.forEach((d) => {
    const iconHtml = [
      '<div style="',
      'width:14px;height:14px;',
      'background:', d.color, ';',
      'border:2.5px solid rgba(255,255,255,0.85);',
      'border-radius:50%;',
      'box-shadow:0 0 0 2px ', d.color, '55;',
      '"></div>',
    ].join('');

    const icon = L.divIcon({
      className: '',
      html: iconHtml,
      iconSize: [14, 14],
      iconAnchor: [7, 7],
      popupAnchor: [0, -10],
    });

    const batteryLine = (d.battery !== null && d.battery !== undefined)
      ? '<div class="popup-row">Battery: <span>' + d.battery + '%</span></div>'
      : '';

    const popupContent = [
      '<div class="popup-id">', d.id, '</div>',
      '<div class="popup-name">',
        '<span class="popup-status-dot" style="background:', d.color, '"></span>',
        d.name,
      '</div>',
      '<div class="popup-row">Status: <span>', d.statusLabel, '</span></div>',
      batteryLine,
      '<div class="popup-row">Location: <span>', d.location, '</span></div>',
      '<div class="popup-row">Last seen: <span>', d.lastSeen, '</span></div>',
    ].join('');

    L.marker([d.lat, d.lon], { icon })
      .bindPopup(popupContent, { maxWidth: 220 })
      .addTo(map);

    bounds.extend([d.lat, d.lon]);
  });

  map.fitBounds(bounds, { padding: [40, 40], maxZoom: 13 });

  setTimeout(() => { map.invalidateSize(); }, 100);
});
</script>

</body>
</html>
"""


def build_html(devices: list[Device], leaflet_css: str, leaflet_js: str) -> str:
    """Render the full self-contained HTML dashboard.

    Uses only str.replace() with <!-- TOKEN --> placeholders to avoid any
    conflict with CSS/JS curly-brace syntax.
    """
    summary = build_summary(devices)
    map_count = sum(1 for d in devices if d.has_coordinates)
    total_count = len(devices)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    html = HTML_TEMPLATE
    html = html.replace("<!-- LEAFLET_CSS -->", leaflet_css)
    html = html.replace("<!-- LEAFLET_JS -->", leaflet_js)
    html = html.replace("<!-- SUMMARY_CHIPS -->", _summary_chips_html(summary))
    html = html.replace("<!-- MARKERS_JSON -->", _map_markers_json(devices))
    html = html.replace("<!-- DEVICE_ROWS -->", _device_rows_html(devices))
    html = html.replace("<!-- MAP_DEVICE_COUNT -->", str(map_count))
    html = html.replace("<!-- TOTAL_DEVICE_COUNT -->", str(total_count))
    html = html.replace("<!-- GENERATED_AT -->", generated_at)
    return html


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    if not CSV_PATH.exists():
        logger.error("CSV not found at %s — aborting", CSV_PATH)
        sys.exit(1)

    devices = load_devices(CSV_PATH)
    leaflet_css, leaflet_js = fetch_leaflet_assets()
    html = build_html(devices, leaflet_css, leaflet_js)

    OUTPUT_PATH.write_text(html, encoding="utf-8")
    size_kb = OUTPUT_PATH.stat().st_size / 1024
    line_count = html.count("\n") + 1
    logger.info(
        "Dashboard written to %s (%.1f KB, %d lines)", OUTPUT_PATH, size_kb, line_count
    )
    print(f"\nOutput:  {OUTPUT_PATH}")
    print(f"Size:    {size_kb:.1f} KB")
    print(f"Lines:   {line_count}")
    print(
        f"Devices: {len(devices)} total, "
        f"{sum(1 for d in devices if d.has_coordinates)} on map"
    )


if __name__ == "__main__":
    main()
