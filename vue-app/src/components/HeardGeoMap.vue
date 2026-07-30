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

import { NODE_COLORS, RF_EDGE_COLOR, NETROM_EDGE_COLOR }
  from '../utils/colorScheme'

// Map markers are smaller than the force-graph nodes; sizing is owned here.
const NODE_CFG = {
  bbs:     { color: NODE_COLORS.bbs.fill,     radius: 14, weight: 2.5 },
  digi:    { color: NODE_COLORS.digi.fill,    radius: 10, weight: 2.0 },
  both:    { color: NODE_COLORS.both.fill,    radius: 10, weight: 2.0 },
  station: { color: NODE_COLORS.station.fill, radius:  7, weight: 1.5 },
  netrom:  { color: NODE_COLORS.netrom.fill,  radius: 10, weight: 2.0 },
}
const DEFAULT_CFG = { color: NODE_COLORS.station.fill, radius: 7, weight: 1.5 }

// ── Helpers ────────────────────────────────────────────────────────────────

/** Build a lookup of callsign → {lat, lon, type} for stations with coordinates.
 *
 * Type resolution priority (per callsign):
 *   1. graphData.nodes[callsign].type — computed from confirmed RF heard_paths (most
 *      accurate; correctly reflects Ka-Node merges since paths are re-attributed)
 *   2. kanode_alias set — station acts as a digi even if its own transport is non-empty
 *   3. transport === '' with source === 'heard' — directly-heard digi relay ('both')
 *   4. transport === '' — relay/digi only
 *   5. default — 'station'
 *
 * After a Ka-Node merge (e.g. K6FB absorbs KROCK), other stations' via-paths still
 * contain 'KROCK' as the intermediate hop because the historical path strings are not
 * rewritten.  We add alias entries to coordMap so that graph edge lookups for the old
 * Ka-Node alias automatically resolve to the owning callsign's coordinates.
 */
function buildCoordMap() {
  const coordMap = {}
  if (!props.stations) return coordMap
  const bbs = props.graphData?.bbs ?? null

  for (const s of props.stations) {
    if (s.lat == null || s.lon == null) continue
    const callsign = s.callsign.toUpperCase()

    // 1. Use graph-computed type when available
    const graphType = props.graphData?.nodes?.[callsign]?.type

    // 2. Fallback heuristics from the API row (schema v2: one row per callsign)
    const isRelayRow    = s.transport === ''
    const isHeardDirect = isRelayRow && s.source === 'heard'
    const hasKaNode     = !!s.kanode_alias

    let t
    if (callsign === bbs)   t = 'bbs'
    else if (graphType)     t = graphType
    else if (hasKaNode)     t = s.source === 'heard' ? 'both' : 'digi'
    else if (isHeardDirect) t = 'both'
    else if (isRelayRow)    t = 'digi'
    else                    t = 'station'

    coordMap[callsign] = { lat: s.lat, lon: s.lon, type: t, station: s }
  }

  // Add Ka-Node alias entries so graph edges that still reference the old digi
  // callsign (e.g. KROCK) resolve to the merged owner's coordinates.
  for (const s of props.stations) {
    if (!s.kanode_alias) continue
    const alias = s.kanode_alias.toUpperCase()
    const owner = s.callsign.toUpperCase()
    if (coordMap[owner] && !coordMap[alias]) {
      coordMap[alias] = coordMap[owner]
    }
  }

  return coordMap
}

/** Render (or re-render) all markers and edge lines. */
function renderLayers() {
  if (!map) return

  markersLayer.clearLayers()
  edgesLayer.clearLayers()

  const coordMap = buildCoordMap()
  if (!Object.keys(coordMap).length) return

  // ── RF edge lines ─────────────────────────────────────────────────────
  if (props.graphData?.edges) {
    for (const edge of props.graphData.edges) {
      const a = coordMap[edge.source]
      const b = coordMap[edge.target]
      if (!a || !b) continue
      L.polyline(
        [[a.lat, a.lon], [b.lat, b.lon]],
        { color: RF_EDGE_COLOR, weight: 2.5, opacity: 0.85 }
      ).addTo(edgesLayer)
    }
  }

  // ── NETROM routing edges (dashed violet) ──────────────────────────────
  if (props.graphData?.netrom_edges) {
    for (const edge of props.graphData.netrom_edges) {
      const a = coordMap[edge.source]
      const b = coordMap[edge.target]
      if (!a || !b) continue
      const opacity = 0.3 + 0.5 * (edge.quality ?? 128) / 255
      L.polyline(
        [[a.lat, a.lon], [b.lat, b.lon]],
        { color: NETROM_EDGE_COLOR, weight: 1.8, opacity, dashArray: '6 4' }
      ).addTo(edgesLayer)
    }
  }

  // ── Station markers ───────────────────────────────────────────────────
  // Iterate props.stations directly — coordMap may contain Ka-Node alias
  // entries (e.g. KROCK → K6FB coords) used only for edge resolution; those
  // must NOT generate their own markers.
  for (const s of props.stations) {
    if (s.lat == null || s.lon == null) continue
    const callsign = s.callsign.toUpperCase()
    const info     = coordMap[callsign]
    if (!info) continue

    const cfg     = NODE_CFG[info.type] ?? DEFAULT_CFG
    const expired = s.expired ?? false

    // When a Ka-Node alias is set, lead with it (that's the RF-visible digi
    // name) and show the database callsign as the secondary label.
    // Otherwise fall back to the nodename-in-parens behaviour.
    const displayName = s.kanode_alias || callsign
    const subName     = s.kanode_alias ? callsign
                      : (s.nodename   ? s.nodename : null)

    const titleHtml = subName
      ? `<strong>${displayName}</strong> <span style="color:#9CA3AF">(${subName})</span>`
      : `<strong>${displayName}</strong>`

    const sourceLabel = s.position_source === 'beacon'
      ? 'self-reported via beacon'
      : s.position_source === 'manual'
        ? 'set by sysop'
        : null
    const popupLines = [
      titleHtml,
      `Type: ${info.type}`,
      s.netrom_alias ? `NET/ROM: ${s.netrom_alias}` : null,
      s.transport ? `Transport: ${s.transport}` : null,
      s.last_heard ? `Last heard: ${new Date(s.last_heard * 1000).toLocaleString()}` : null,
      s.count ? `Count: ${s.count}` : null,
      sourceLabel ? `Position: ${sourceLabel}` : null,
      s.comment ? `Comment: ${s.comment}` : null,
    ].filter(Boolean).join('<br/>')

    const tooltipText = subName ? `${displayName} (${subName})` : displayName

    L.circleMarker([info.lat, info.lon], {
      radius:      cfg.radius,
      color:       cfg.color,
      weight:      cfg.weight,
      fillColor:   cfg.color,
      fillOpacity: expired ? 0.25 : 0.75,
      opacity:     expired ? 0.45 : 1.0,
    })
      .bindPopup(popupLines, { maxWidth: 260 })
      .bindTooltip(tooltipText, { permanent: false, direction: 'top', offset: [0, -cfg.radius - 2] })
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
        Stations appear here when they beacon a <code style="color:#9CA3AF;">&lt;MAP:lat,lon,CALL[,NODE]&gt;</code> tag,
        or after a sysop adds lat/lon via the pencil icon in the Log tab.
      </span>
    </div>

    <!-- Leaflet map container -->
    <div ref="mapEl" style="width:100%;height:100%;" />

    <!-- Legend -->
    <div style="position:absolute;bottom:30px;left:10px;z-index:500;background:rgba(17,24,39,.82);border-radius:6px;padding:6px 10px;font-size:11px;color:#9CA3AF;display:flex;flex-direction:column;gap:4px;line-height:1.5;">
      <span style="display:flex;align-items:center;gap:6px;">
        <svg width="14" height="14"><circle cx="7" cy="7" r="6"
          :fill="NODE_COLORS.bbs.fill" :stroke="NODE_COLORS.bbs.stroke" stroke-width="1.5"/></svg>BBS
      </span>
      <span style="display:flex;align-items:center;gap:6px;">
        <svg width="14" height="14"><circle cx="7" cy="7" r="6"
          :fill="NODE_COLORS.digi.fill" :stroke="NODE_COLORS.digi.stroke" stroke-width="1.5"/></svg>Digi
      </span>
      <span style="display:flex;align-items:center;gap:6px;">
        <svg width="14" height="14"><circle cx="7" cy="7" r="6"
          :fill="NODE_COLORS.both.fill" :stroke="NODE_COLORS.both.stroke" stroke-width="1.5"/></svg>Heard &amp; Digi
      </span>
      <span style="display:flex;align-items:center;gap:6px;">
        <svg width="14" height="14"><circle cx="7" cy="7" r="5"
          :fill="NODE_COLORS.station.fill" :stroke="NODE_COLORS.station.stroke" stroke-width="1.5"/></svg>Station
      </span>
      <span style="display:flex;align-items:center;gap:6px;">
        <svg width="28" height="8"><line x1="0" y1="4" x2="28" y2="4"
          :stroke="RF_EDGE_COLOR" stroke-width="2"/></svg>RF path
      </span>
      <span style="display:flex;align-items:center;gap:6px;">
        <svg width="28" height="8"><line x1="0" y1="4" x2="28" y2="4"
          :stroke="NETROM_EDGE_COLOR" stroke-width="1.5" stroke-dasharray="4 2"/></svg>NETROM route
      </span>
    </div>
  </div>
</template>
