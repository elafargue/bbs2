<script setup>
/**
 * HeardGeoMap.vue — Leaflet geographic map of heard stations.
 *
 * One marker per PHYSICAL STATION (SSIDs folded by base callsign, from
 * /api/heard/entities).  The marker sits at the entity's rolled-up reference
 * position (sysop override, else the freshest beacon across its SSIDs).
 *
 * RF/NET-ROM hop edges stay PER-SSID (from the graph data): each per-SSID
 * endpoint resolves to its physical station's marker, so a hop between two
 * stations draws between their collapsed markers; hops between SSIDs of the
 * same station collapse to a point and are skipped.
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
  /** Physical stations from GET /api/heard/entities — each item has
   *  base_call, nodename, lat, lon, position_source, ssids[], services[],
   *  aliases[], transports[], last_heard, count, last_beacon_text, members[]. */
  entities:  { type: Array,  default: () => [] },
  /** Graph data from GET /api/heard/graph — { bbs, nodes: {call:{type}}, edges:[{source,target}] } */
  graphData: { type: Object, default: null },
  loading:   { type: Boolean, default: false },
})

const mapEl = ref(null)
let map     = null
let markersLayer = null
let edgesLayer   = null

import { NODE_COLORS, RF_EDGE_COLOR, NETROM_EDGE_COLOR, ALIAS_COLORS }
  from '../utils/colorScheme'

const BEACON_ALIAS_COLOR = '#9CA3AF'  // muted: self-declared, unverified

// Map markers are smaller than the force-graph nodes; sizing is owned here.
const NODE_CFG = {
  bbs:     { color: NODE_COLORS.bbs.fill,     radius: 14, weight: 2.5 },
  digi:    { color: NODE_COLORS.digi.fill,    radius: 10, weight: 2.0 },
  both:    { color: NODE_COLORS.both.fill,    radius: 10, weight: 2.0 },
  station: { color: NODE_COLORS.station.fill, radius:  7, weight: 1.5 },
  netrom:  { color: NODE_COLORS.netrom.fill,  radius: 10, weight: 2.0 },
}
const DEFAULT_CFG = { color: NODE_COLORS.station.fill, radius: 7, weight: 1.5 }

// Rank used to pick a single representative role for a collapsed station from
// the (possibly mixed) roles of its member SSIDs.
const TYPE_RANK = { station: 0, netrom: 1, digi: 2, both: 3, bbs: 4 }

// ── Helpers ────────────────────────────────────────────────────────────────

function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

/** The RF/NET-ROM role of a single SSID. The graph node type is authoritative
 * for the RF role (digi/both/bbs), but only when it is a *node* role — a bare
 * 'station' must not mask a NET/ROM identity (netrom event or alias). */
function memberRole(m, cs, bbsCall, nodes) {
  if (cs === bbsCall) return 'bbs'
  const g = nodes?.[cs]?.type
  if (g && g !== 'station') return g                     // digi/both/bbs/netrom
  if (m.netrom_alias || m.transport === 'netrom') return 'netrom'
  if (m.kanode_alias) return (m.transport && m.transport !== '') ? 'digi' : 'both'
  return g || 'station'
}

/** The representative role of a physical station = the highest-ranked role
 * among its member SSIDs. */
function entityType(e, bbsCall, nodes) {
  let best = 'station'
  for (const m of e.members ?? []) {
    const t = memberRole(m, (m.callsign || '').toUpperCase(), bbsCall, nodes)
    if ((TYPE_RANK[t] ?? 0) > (TYPE_RANK[best] ?? 0)) best = t
  }
  return best
}

/** callsign / alias → { lat, lon, base } for every station with coordinates.
 *
 * Every member SSID and known alias of a physical station resolves to the same
 * collapsed marker position, so per-SSID graph edges land on the right marker.
 */
function buildCoordMap() {
  const coordMap = {}
  if (!props.entities) return coordMap
  const bbs   = props.graphData?.bbs ?? null
  const nodes = props.graphData?.nodes ?? null

  for (const e of props.entities) {
    if (e.lat == null || e.lon == null) continue
    const pos = { lat: e.lat, lon: e.lon, base: e.base_call, type: entityType(e, bbs, nodes) }
    const keys = new Set([e.base_call.toUpperCase()])
    for (const s of e.ssids ?? []) keys.add(s.toUpperCase())
    for (const m of e.members ?? []) {
      for (const a of [m.kanode_alias, m.netrom_alias, m.beacon_alias]) {
        if (a) keys.add(a.toUpperCase())
      }
    }
    for (const k of keys) if (!coordMap[k]) coordMap[k] = pos
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

  // ── RF edge lines (per-SSID; skip intra-station hops) ─────────────────
  if (props.graphData?.edges) {
    for (const edge of props.graphData.edges) {
      const a = coordMap[(edge.source || '').toUpperCase()]
      const b = coordMap[(edge.target || '').toUpperCase()]
      if (!a || !b || a.base === b.base) continue
      L.polyline(
        [[a.lat, a.lon], [b.lat, b.lon]],
        { color: RF_EDGE_COLOR, weight: 2.5, opacity: 0.85 }
      ).addTo(edgesLayer)
    }
  }

  // ── NETROM routing edges (dashed violet) ──────────────────────────────
  if (props.graphData?.netrom_edges) {
    for (const edge of props.graphData.netrom_edges) {
      const a = coordMap[(edge.source || '').toUpperCase()]
      const b = coordMap[(edge.target || '').toUpperCase()]
      if (!a || !b || a.base === b.base) continue
      const opacity = 0.3 + 0.5 * (edge.quality ?? 128) / 255
      L.polyline(
        [[a.lat, a.lon], [b.lat, b.lon]],
        { color: NETROM_EDGE_COLOR, weight: 1.8, opacity, dashArray: '6 4' }
      ).addTo(edgesLayer)
    }
  }

  // ── Physical-station markers (one per entity) ─────────────────────────
  for (const e of props.entities) {
    if (e.lat == null || e.lon == null) continue
    const type = entityType(e, props.graphData?.bbs ?? null, props.graphData?.nodes ?? null)
    const cfg  = NODE_CFG[type] ?? DEFAULT_CFG

    const name    = e.nodename || e.base_call
    const subName = (e.nodename && e.nodename.toUpperCase() !== e.base_call.toUpperCase())
      ? e.base_call : null
    const titleHtml = subName
      ? `<strong>${esc(name)}</strong> <span style="color:#9CA3AF">(${esc(subName)})</span>`
      : `<strong>${esc(name)}</strong>`

    const sourceLabel = e.position_source === 'beacon'
      ? 'self-reported via beacon'
      : e.position_source === 'manual'
        ? 'set by sysop'
        : null

    const aliasLine = (label, arr, color) =>
      (arr?.length) ? `<span style="color:${color}">${label}:</span> ${esc(arr.join(', '))}` : null

    const popupLines = [
      titleHtml,
      (e.ssids?.length)      ? `SSIDs: ${esc(e.ssids.join(', '))}`         : null,
      (e.services?.length)   ? `Services: ${esc(e.services.join(', '))}`   : null,
      aliasLine('NET/ROM',   e.netrom_aliases, ALIAS_COLORS.netrom),
      aliasLine('Ka-Node',   e.kanode_aliases, ALIAS_COLORS.kanode),
      aliasLine('Beacon ID', e.beacon_aliases, BEACON_ALIAS_COLOR),
      (e.transports?.length) ? `Via: ${esc(e.transports.join(', '))}`      : null,
      e.last_heard ? `Last heard: ${new Date(e.last_heard * 1000).toLocaleString()}` : null,
      e.count ? `Count: ${e.count}` : null,
      sourceLabel ? `Position: ${sourceLabel}` : null,
      e.last_beacon_text ? `Beacon: ${esc(e.last_beacon_text)}` : null,
    ].filter(Boolean).join('<br/>')

    const tooltipText = subName ? `${name} (${subName})` : name

    L.circleMarker([e.lat, e.lon], {
      radius:      cfg.radius,
      color:       cfg.color,
      weight:      cfg.weight,
      fillColor:   cfg.color,
      fillOpacity: 0.75,
      opacity:     1.0,
    })
      .bindPopup(popupLines, { maxWidth: 280 })
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

watch([() => props.entities, () => props.graphData], renderLayers, { deep: true })
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

    <!-- Empty state (no station has coordinates) -->
    <div
      v-if="!loading && !entities.some(e => e.lat != null && e.lon != null)"
      style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;z-index:1000;background:rgba(17,24,39,.55);"
    >
      <span style="color:#9CA3AF;font-size:14px;">No stations with coordinates yet.</span>
      <span style="color:#6B7280;font-size:12px;margin-top:6px;">
        Stations appear here when any of their SSIDs beacons a <code style="color:#9CA3AF;">&lt;MAP:lat,lon,CALL[,NODE]&gt;</code> tag,
        or after a sysop sets lat/lon (per-SSID in the Log tab, or per-station in the Stations tab).
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
