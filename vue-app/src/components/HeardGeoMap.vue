<script setup>
/**
 * HeardGeoMap.vue — Leaflet geographic map of heard stations.
 *
 * Shows every station that has lat/lon coordinates stored in the DB as a
 * circle marker.  RF hop edges from the graph data are drawn as polylines
 * between pairs of stations that both have coordinates.
 *
 * Node colour scheme mirrors NetworkGraph.vue:
 *   bbs     — amber   #F59E0B
 *   digi    — blue    #3B82F6
 *   both    — teal    #10B981
 *   station — grey    #6B7280
 */
import { ref, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'

// Leaflet ships broken default icon paths when bundled with Vite — fix them.
import iconUrl       from 'leaflet/dist/images/marker-icon.png'
import iconRetinaUrl from 'leaflet/dist/images/marker-icon-2x.png'
import shadowUrl     from 'leaflet/dist/images/marker-shadow.png'
delete L.Icon.Default.prototype._getIconUrl
L.Icon.Default.mergeOptions({ iconUrl, iconRetinaUrl, shadowUrl })

const props = defineProps({
  /** Full stations list from GET /api/heard — each item has callsign, lat, lon, type? */
  stations:  { type: Array,  default: () => [] },
  /** Graph data from GET /api/heard/graph — { bbs, nodes: {call: {type}}, edges: [{source,target}] } */
  graphData: { type: Object, default: null },
  loading:   { type: Boolean, default: false },
})

const mapEl = ref(null)
let map     = null
let markersLayer = null
let edgesLayer   = null

const NODE_CFG = {
  bbs:     { color: '#F59E0B', radius: 14, weight: 2.5 },
  digi:    { color: '#3B82F6', radius: 10, weight: 2.0 },
  both:    { color: '#10B981', radius: 10, weight: 2.0 },
  station: { color: '#6B7280', radius:  7, weight: 1.5 },
}
const DEFAULT_CFG = { color: '#6B7280', radius: 7, weight: 1.5 }

// ── Helpers ────────────────────────────────────────────────────────────────

/** Build a lookup of callsign → {lat, lon, type} for stations with coordinates.
 *
 * Type resolution priority:
 *   1. graphData.nodes[callsign].type  — computed from confirmed RF paths (most accurate)
 *   2. source === 'via'               — relay-only node, call it 'digi' even if not in a
 *                                       confirmed path (e.g. unstarred in all frames seen)
 *   3. default                        — 'station'
 *
 * A callsign may have multiple rows (different transports). We group by
 * callsign first, then:
 *   - transport === '' rows are relay nodes (seeded from via paths)
 *   - source === 'heard' on a transport='' row means it was the last-starred
 *     digi (BBS received RF directly from it) — show as 'both' (green)
 */
function buildCoordMap() {
  const coordMap = {}
  if (!props.stations) return coordMap
  const bbs = props.graphData?.bbs ?? null

  // Group rows by callsign
  const byCall = {}
  for (const s of props.stations) {
    if (!byCall[s.callsign]) byCall[s.callsign] = []
    byCall[s.callsign].push(s)
  }

  for (const [callsign, rows] of Object.entries(byCall)) {
    const withCoords = rows.filter(r => r.lat != null && r.lon != null)
    if (!withCoords.length) continue
    // Prefer the non-relay row for coordinates (it's the primary heard record)
    const s = withCoords.find(r => r.transport !== '') ?? withCoords[0]

    // A node is a relay if it has any transport='' row (via-seeded)
    const isRelayNode = rows.some(r => r.transport === '')
    // A relay node is 'directly heard' when it was the last starred digi
    // in a path: on_heard() marks those with source='heard'
    const isHeardDirect = isRelayNode && rows.some(r => r.transport === '' && r.source === 'heard')

    const t = callsign === bbs      ? 'bbs'
            : isRelayNode && isHeardDirect ? 'both'
            : isRelayNode           ? 'digi'
            : 'station'

    coordMap[callsign] = { lat: s.lat, lon: s.lon, type: t, station: s }
  }
  return coordMap
}

/** Render (or re-render) all markers and edge lines. */
function renderLayers() {
  if (!map) return

  markersLayer.clearLayers()
  edgesLayer.clearLayers()

  const coordMap = buildCoordMap()
  const entries  = Object.entries(coordMap)
  if (!entries.length) return

  // ── Edge lines ────────────────────────────────────────────────────────
  if (props.graphData?.edges) {
    for (const edge of props.graphData.edges) {
      const a = coordMap[edge.source]
      const b = coordMap[edge.target]
      if (!a || !b) continue
      L.polyline(
        [[a.lat, a.lon], [b.lat, b.lon]],
        { color: '#60A5FA', weight: 2.5, opacity: 0.85 }
      ).addTo(edgesLayer)
    }
  }

  // ── Station markers ───────────────────────────────────────────────────
  for (const [callsign, info] of entries) {
    const cfg     = NODE_CFG[info.type] ?? DEFAULT_CFG
    const s       = info.station
    const expired = s.expired ?? false

    const popupLines = [
      `<strong>${callsign}</strong>`,
      `Type: ${info.type}`,
      s.transport ? `Transport: ${s.transport}` : null,
      s.last_heard ? `Last heard: ${new Date(s.last_heard * 1000).toLocaleString()}` : null,
      s.count ? `Count: ${s.count}` : null,
      s.comment ? `Comment: ${s.comment}` : null,
    ].filter(Boolean).join('<br/>')

    L.circleMarker([info.lat, info.lon], {
      radius:      cfg.radius,
      color:       cfg.color,
      weight:      cfg.weight,
      fillColor:   cfg.color,
      fillOpacity: expired ? 0.25 : 0.75,
      opacity:     expired ? 0.45 : 1.0,
    })
      .bindPopup(popupLines, { maxWidth: 260 })
      .bindTooltip(callsign, { permanent: false, direction: 'top', offset: [0, -cfg.radius - 2] })
      .addTo(markersLayer)
  }

  // Auto-fit map bounds to the markers present
  const bounds = markersLayer.getBounds()
  if (bounds.isValid()) {
    map.fitBounds(bounds, { padding: [40, 40], maxZoom: 10 })
  }
}

// ── Lifecycle ──────────────────────────────────────────────────────────────

onMounted(() => {
  nextTick(() => {
    if (!mapEl.value) return

    map = L.map(mapEl.value, {
      center:  [20, 0],
      zoom:    2,
      minZoom: 1,
    })

    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxZoom: 19,
    }).addTo(map)

    edgesLayer   = L.layerGroup().addTo(map)
    markersLayer = L.layerGroup().addTo(map)

    renderLayers()
  })
})

onBeforeUnmount(() => {
  if (map) { map.remove(); map = null }
})

watch([() => props.stations, () => props.graphData], renderLayers, { deep: true })
</script>

<template>
  <div style="position: relative; width: 100%; height: 560px; border-radius: 8px; overflow: hidden;">
    <!-- Loading overlay -->
    <div
      v-if="loading"
      style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(17,24,39,.7);z-index:1000;"
    >
      <v-progress-circular indeterminate color="primary" />
    </div>

    <!-- Empty state (no stations have coordinates) -->
    <div
      v-if="!loading && !stations.some(s => s.lat != null && s.lon != null)"
      style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;z-index:1000;background:rgba(17,24,39,.55);"
    >
      <span style="color:#9CA3AF;font-size:14px;">No stations with coordinates yet.</span>
      <span style="color:#6B7280;font-size:12px;margin-top:6px;">
        Use the pencil icon in the Log tab to add lat/lon to a station.
      </span>
    </div>

    <!-- Leaflet map container -->
    <div ref="mapEl" style="width:100%;height:100%;" />

    <!-- Legend -->
    <div style="position:absolute;bottom:30px;left:10px;z-index:500;background:rgba(17,24,39,.82);border-radius:6px;padding:6px 10px;font-size:11px;color:#9CA3AF;display:flex;flex-direction:column;gap:4px;line-height:1.5;">
      <span style="display:flex;align-items:center;gap:6px;">
        <svg width="14" height="14"><circle cx="7" cy="7" r="6" fill="#F59E0B" stroke="#B45309" stroke-width="1.5"/></svg>BBS
      </span>
      <span style="display:flex;align-items:center;gap:6px;">
        <svg width="14" height="14"><circle cx="7" cy="7" r="6" fill="#3B82F6" stroke="#1D4ED8" stroke-width="1.5"/></svg>Digi
      </span>
      <span style="display:flex;align-items:center;gap:6px;">
        <svg width="14" height="14"><circle cx="7" cy="7" r="6" fill="#10B981" stroke="#065F46" stroke-width="1.5"/></svg>Heard &amp; Digi
      </span>
      <span style="display:flex;align-items:center;gap:6px;">
        <svg width="14" height="14"><circle cx="7" cy="7" r="5" fill="#6B7280" stroke="#374151" stroke-width="1.5"/></svg>Station
      </span>
      <span style="display:flex;align-items:center;gap:6px;">
        <svg width="28" height="8"><line x1="0" y1="4" x2="28" y2="4" stroke="#9CA3AF" stroke-width="1.5" stroke-dasharray="4 4"/></svg>RF path
      </span>
    </div>
  </div>
</template>
