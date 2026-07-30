<script setup>
/**
 * NetworkGraph.vue — D3 force-directed AX.25 network map.
 *
 * Renders confirmed digipeater paths as a directed graph.
 * Only hops up to and including the last starred digi are shown;
 * unconfirmed path tails are omitted.
 *
 * Node types:
 *   bbs     — the BBS station (large, amber, pinned at center)
 *   digi    — pure digipeater (medium, blue)
 *   station — heard source only (small, grey)
 *   both    — heard source that also appears as a digi (medium, teal)
 *
 * Edge width encodes log(count+1) — thicker = more confirmed frames.
 */
import { ref, onMounted, onBeforeUnmount, watch } from 'vue'
import * as d3 from 'd3'

const props = defineProps({
  graphData: { type: Object, default: null }, // { bbs, nodes, edges }
  loading:   { type: Boolean, default: false },
})

const svgEl  = ref(null)
const width  = ref(800)
const height = ref(560)

// ── Visual constants ───────────────────────────────────────────────────────

import { NODE_COLORS, RF_EDGE_COLOR, NETROM_EDGE_COLOR }
  from '../utils/colorScheme'

// Node config layers force-graph-specific sizing/labeling on top of the
// shared color scheme.  Radii are intentionally larger here than on the
// geographic map.
const NODE_CFG = {
  bbs:     { ...NODE_COLORS.bbs,     r: 18, label: true  },
  digi:    { ...NODE_COLORS.digi,    r: 12, label: true  },
  both:    { ...NODE_COLORS.both,    r: 12, label: true  },
  station: { ...NODE_COLORS.station, r:  6, label: false },
  netrom:  { ...NODE_COLORS.netrom,  r: 11, label: true  },
}

// ── Visibility toggles ─────────────────────────────────────────────────────

// hiddenTypes: Set of node type strings currently hidden.
// Always replace with a new Set (not mutate) so Vue detects the change.
const hiddenTypes     = ref(new Set())
const showRfEdges     = ref(true)
const showNetromEdges = ref(true)

function toggleType(type) {
  if (type === 'bbs') return  // BBS node is always visible
  const s = new Set(hiddenTypes.value)
  if (s.has(type)) s.delete(type); else s.add(type)
  hiddenTypes.value = s
}

// D3 selections stored after render so applyVisibility() can update them
// without triggering a full re-render and simulation restart.
let _nodeSelection       = null
let _rfLinkSelection     = null
let _netromLinkSelection = null

function applyVisibility() {
  if (!_nodeSelection) return
  const ht          = hiddenTypes.value
  const netromShown = !ht.has('netrom')

  // A node is "rescued" if its primary type is toggled off but it is also a
  // NETROM routing node and NETROM is still visible.  It stays on screen in
  // purple so NETROM edges don't become orphaned.
  function isRescued(d) {
    return ht.has(d.type) && d.is_netrom && netromShown
  }

  _nodeSelection.each(function(d) {
    const rescued = isRescued(d)
    d3.select(this).attr('display', ht.has(d.type) && !rescued ? 'none' : null)
    // Recolour: rescued nodes → NETROM purple; others → original render colour.
    const cfg = rescued ? NODE_CFG.netrom : NODE_CFG[d.type]
    d3.select(this).select('circle')
      .attr('fill',         cfg?.fill   ?? NODE_COLORS.station.fill)
      .attr('stroke',       rescued ? NODE_CFG.netrom.stroke
                                    : (d.is_netrom && d.type === 'station' ? NODE_COLORS.netrom.fill
                                                                           : (cfg?.stroke ?? NODE_COLORS.station.stroke)))
      .attr('stroke-width', rescued || (d.is_netrom && d.type === 'station') ? 2.5 : 1.5)
  })

  if (_rfLinkSelection) {
    // RF edges are hidden when either endpoint's primary type is toggled off
    // (rescued nodes keep NETROM edges but lose their RF edges).
    _rfLinkSelection.attr('display', d => {
      if (!showRfEdges.value) return 'none'
      const st = d.source?.type, tt = d.target?.type
      return (st && ht.has(st)) || (tt && ht.has(tt)) ? 'none' : null
    })
  }

  if (_netromLinkSelection) {
    // NETROM edges survive as long as both endpoints are effectively visible
    // (either not hidden, or rescued as NETROM).
    _netromLinkSelection.attr('display', d => {
      if (!showNetromEdges.value) return 'none'
      const srcOk = !ht.has(d.source?.type) || isRescued(d.source)
      const tgtOk = !ht.has(d.target?.type) || isRescued(d.target)
      return srcOk && tgtOk ? null : 'none'
    })
  }
}

// Tooltip
const tooltip = ref({ show: false, x: 0, y: 0, html: '' })

// ResizeObserver to keep SVG responsive
let ro = null

// Current simulation (kept so we can stop it on unmount)
let sim = null
let zoomBehavior = null

// ── Render ─────────────────────────────────────────────────────────────────

function render() {
  if (!svgEl.value || !props.graphData) return

  const { bbs, nodes: nodeMap, edges, netrom_edges } = props.graphData
  if (!nodeMap || !edges) return

  const svg = d3.select(svgEl.value)
  svg.selectAll('*').remove()

  // Convert to D3-compatible arrays
  const nodesArr = Object.entries(nodeMap).map(([id, d]) => ({ id, ...d }))
  const linksArr = edges.map(e => ({ ...e }))          // RF edges (source/target are string IDs)
  const netromLinksArr = (netrom_edges || []).map(e => ({ ...e }))  // NETROM routing edges

  if (!nodesArr.length) return

  // Mark nodes that participate in NETROM routing so the visibility logic can
  // rescue them (show as purple) when the "station" type is toggled off.
  const netromNodeIds = new Set()
  netromLinksArr.forEach(e => { netromNodeIds.add(e.source); netromNodeIds.add(e.target) })
  nodesArr.forEach(d => { d.is_netrom = netromNodeIds.has(d.id) })

  const W = width.value
  const H = height.value

  // Arrow marker defs (one per colour so arrowheads match stroke)
  const defs = svg.append('defs')
  const arrowColors = [
    NODE_COLORS.digi.fill,
    NODE_COLORS.both.fill,
    NODE_COLORS.station.fill,
    NODE_COLORS.bbs.fill,
    RF_EDGE_COLOR,
    NETROM_EDGE_COLOR,
  ]
  arrowColors.forEach(col => {
    const id = `arrow-${col.replace('#', '')}`
    defs.append('marker')
      .attr('id', id)
      .attr('viewBox', '0 -5 10 10')
      .attr('refX', 22)   // offset past node radius
      .attr('refY', 0)
      .attr('markerWidth', 6)
      .attr('markerHeight', 6)
      .attr('orient', 'auto')
      .append('path')
        .attr('d', 'M0,-5L10,0L0,5')
        .attr('fill', col)
  })

  const zoomLayer      = svg.append('g').attr('class', 'zoom-layer')
  const netromEdgeGroup = zoomLayer.append('g').attr('class', 'netrom-edges')
  const edgeGroup      = zoomLayer.append('g').attr('class', 'edges')
  const nodeGroup      = zoomLayer.append('g').attr('class', 'nodes')

  // Edge width scale
  const maxCount = d3.max(linksArr, d => d.count) || 1
  const strokeW  = d3.scaleLog()
    .domain([1, Math.max(maxCount, 2)])
    .range([1.2, 5])
    .clamp(true)

  // NETROM routing edges (dashed violet, drawn beneath RF edges)
  const netromLink = netromEdgeGroup.selectAll('line')
    .data(netromLinksArr)
    .enter().append('line')
      .attr('stroke', NETROM_EDGE_COLOR)
      .attr('stroke-opacity', d => 0.25 + 0.55 * (d.quality ?? 128) / 255)
      .attr('stroke-width', 1.5)
      .attr('stroke-dasharray', '5,3')
      .attr('marker-end', `url(#arrow-${NETROM_EDGE_COLOR.slice(1)})`)

  // RF link elements
  const link = edgeGroup.selectAll('line')
    .data(linksArr)
    .enter().append('line')
      .attr('stroke', RF_EDGE_COLOR)
      .attr('stroke-opacity', 0.7)
      .attr('stroke-width', d => strokeW(d.count))
      .attr('marker-end', `url(#arrow-${RF_EDGE_COLOR.slice(1)})`)

  // Node circles
  const node = nodeGroup.selectAll('g')
    .data(nodesArr)
    .enter().append('g')
      .attr('class', 'node')
      .call(
        d3.drag()
          .on('start', (event, d) => {
            if (!event.active) sim.alphaTarget(0.3).restart()
            d.fx = d.x; d.fy = d.y
          })
          .on('drag', (event, d) => { d.fx = event.x; d.fy = event.y })
          .on('end', (event, d) => {
            if (!event.active) sim.alphaTarget(0)
            if (d.type !== 'bbs') { d.fx = null; d.fy = null }
          })
      )

  node.append('circle')
    .attr('r', d => (d.type === 'station' && d.is_netrom) ? 9 : (NODE_CFG[d.type]?.r ?? 8))
    .attr('fill', d => NODE_CFG[d.type]?.fill ?? NODE_COLORS.station.fill)
    .attr('stroke', d => (d.type === 'station' && d.is_netrom) ? NODE_COLORS.netrom.fill : (NODE_CFG[d.type]?.stroke ?? NODE_COLORS.station.stroke))
    .attr('stroke-width', d => (d.type === 'station' && d.is_netrom) ? 2.5 : 1.5)

  // Labels: always on bbs/digi/both/netrom; on station only if degree > 0 or also NETROM
  const degreeMap = {}
  linksArr.forEach(e => {
    degreeMap[e.source.id ?? e.source] = (degreeMap[e.source.id ?? e.source] || 0) + 1
    degreeMap[e.target.id ?? e.target] = (degreeMap[e.target.id ?? e.target] || 0) + 1
  })

  node.filter(d => NODE_CFG[d.type]?.label || (degreeMap[d.id] || 0) > 0 || d.is_netrom)
    .append('text')
      .text(d => d.id)
      .attr('font-size', d => d.type === 'bbs' ? '11px' : '9px')
      .attr('font-family', 'monospace')
      .attr('fill', '#E5E7EB')
      .attr('text-anchor', 'middle')
      .attr('dy', d => (NODE_CFG[d.type]?.r ?? 8) + 11)
      // Text outline for legibility
      .clone(true).lower()
        .attr('stroke', '#111827')
        .attr('stroke-width', 3)
        .attr('stroke-linejoin', 'round')

  // Tooltip interactions
  node
    .on('mouseenter', (event, d) => {
      const inDeg  = linksArr.filter(e => (e.target.id ?? e.target) === d.id).length
      const outDeg = linksArr.filter(e => (e.source.id ?? e.source) === d.id).length
      const totalFrames = linksArr
        .filter(e => (e.source.id ?? e.source) === d.id || (e.target.id ?? e.target) === d.id)
        .reduce((s, e) => s + e.count, 0)
      tooltip.value = {
        show: true,
        x: event.offsetX + 12,
        y: event.offsetY - 8,
        html: `<strong>${d.id}</strong>${d.nodename ? ` <span style="color:${NETROM_EDGE_COLOR}">(${d.nodename})</span>` : ''}<br/>Type: ${d.type}<br/>In: ${inDeg} &nbsp; Out: ${outDeg}<br/>Frames: ${totalFrames}`,
      }
    })
    .on('mousemove', event => {
      tooltip.value.x = event.offsetX + 12
      tooltip.value.y = event.offsetY - 8
    })
    .on('mouseleave', () => { tooltip.value.show = false })

  // ── Force simulation ───────────────────────────────────────────────────

  // Combine RF + NETROM edges for the force simulation so NETROM nodes
  // are positioned relative to their routing neighbours.
  const allLinksArr = [...linksArr, ...netromLinksArr]

  if (sim) sim.stop()

  // Radial target rings keep the layout readable without manual dragging:
  //   BBS   → pinned at center
  //   digi / both / netrom → middle ring (~35 % of half-min-dimension)
  //   station               → outer ring  (~70 % of half-min-dimension)
  const halfMin = Math.min(W, H) / 2
  function radialTarget(d) {
    if (d.type === 'bbs')     return 0
    if (d.type === 'station') return halfMin * 0.72
    return halfMin * 0.38    // digi / both / netrom
  }

  sim = d3.forceSimulation(nodesArr)
    .force('link', d3.forceLink(allLinksArr)
      .id(d => d.id)
      .distance(d => {
        const sType = typeof d.source === 'object' ? d.source.type : 'station'
        const tType = typeof d.target === 'object' ? d.target.type : 'station'
        if (sType === 'station' || tType === 'station') return 130
        if (sType === 'netrom'  || tType === 'netrom')  return 110
        return 90
      })
      .strength(0.5)
    )
    .force('charge', d3.forceManyBody().strength(d => {
      if (d.type === 'bbs')     return -600
      if (d.type === 'station') return -120
      return -280   // digi / both / netrom
    }))
    .force('collide', d3.forceCollide().radius(d => (NODE_CFG[d.type]?.r ?? 8) + 14))
    .force('radial',  d3.forceRadial(d => radialTarget(d), W / 2, H / 2).strength(0.25))
    .force('center',  d3.forceCenter(W / 2, H / 2).strength(0.04))

  // Pin BBS node to center
  const bbsNode = nodesArr.find(n => n.type === 'bbs')
  if (bbsNode) { bbsNode.fx = W / 2; bbsNode.fy = H / 2 }

  // ── Zoom & pan (scroll wheel + 2-finger touch) ────────────────────────
  zoomBehavior = d3.zoom()
    .scaleExtent([0.15, 8])
    .on('zoom', event => { zoomLayer.attr('transform', event.transform) })
  svg.call(zoomBehavior)
     .on('dblclick.zoom', null)   // double-click handled as node drag, not zoom reset

  // Store selections for toggle-driven visibility updates
  _nodeSelection       = node
  _rfLinkSelection     = link
  _netromLinkSelection = netromLink
  applyVisibility()

  sim.on('tick', () => {
    // Clamp nodes within SVG bounds
    const pad = 24
    nodesArr.forEach(d => {
      d.x = Math.max(pad, Math.min(W - pad, d.x))
      d.y = Math.max(pad, Math.min(H - pad, d.y))
    })

    link
      .attr('x1', d => d.source.x).attr('y1', d => d.source.y)
      .attr('x2', d => d.target.x).attr('y2', d => d.target.y)

    netromLink
      .attr('x1', d => d.source.x).attr('y1', d => d.source.y)
      .attr('x2', d => d.target.x).attr('y2', d => d.target.y)

    node.attr('transform', d => `translate(${d.x},${d.y})`)
  })
}

// ── Lifecycle ──────────────────────────────────────────────────────────────

function resetZoom() {
  if (!svgEl.value || !zoomBehavior) return
  d3.select(svgEl.value)
    .transition().duration(300)
    .call(zoomBehavior.transform, d3.zoomIdentity)
}

onMounted(() => {
  ro = new ResizeObserver(entries => {
    const entry = entries[0]
    if (entry) {
      width.value  = entry.contentRect.width  || 800
      height.value = entry.contentRect.height || 560
      render()
    }
  })
  if (svgEl.value?.parentElement) ro.observe(svgEl.value.parentElement)
  render()
})

onBeforeUnmount(() => {
  if (sim)  sim.stop()
  if (ro)   ro.disconnect()
})

watch(() => props.graphData, render)
watch([hiddenTypes, showRfEdges, showNetromEdges], applyVisibility)
</script>

<template>
  <div class="network-graph-wrap" style="position: relative; width: 100%; height: 560px; background: #111827; border-radius: 8px; overflow: hidden;">
    <!-- Loading overlay -->
    <div
      v-if="loading"
      style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(17,24,39,.7);z-index:10;"
    >
      <v-progress-circular indeterminate color="primary" />
    </div>

    <!-- Empty state -->
    <div
      v-if="!loading && (!graphData || (!graphData.edges?.length && !graphData.netrom_edges?.length))"
      style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;"
    >
      <span style="color:#6B7280;">No confirmed paths yet.</span>
    </div>

    <!-- SVG canvas -->
    <svg
      ref="svgEl"
      :width="width"
      :height="height"
      style="width:100%;height:100%;cursor:grab;"
    />

    <!-- Reset zoom button -->
    <button
      v-if="graphData && graphData.edges?.length"
      @click="resetZoom"
      title="Reset zoom"
      style="position:absolute;top:10px;right:10px;background:rgba(31,41,55,.85);border:1px solid #374151;border-radius:5px;color:#9CA3AF;font-size:13px;padding:3px 8px;cursor:pointer;z-index:15;line-height:1.4;"
    >⊙ Reset</button>

    <!-- Hover tooltip -->
    <div
      v-if="tooltip.show"
      :style="{
        position: 'absolute',
        left: tooltip.x + 'px',
        top:  tooltip.y + 'px',
        background: 'rgba(17,24,39,.92)',
        border: '1px solid #374151',
        borderRadius: '6px',
        padding: '6px 10px',
        fontSize: '12px',
        color: '#F9FAFB',
        pointerEvents: 'none',
        zIndex: 20,
        lineHeight: '1.6',
      }"
      v-html="tooltip.html"
    />

    <!-- Legend (bottom-left) — click/Enter/Space to toggle visibility -->
    <div style="position:absolute;bottom:12px;left:14px;display:flex;gap:10px;align-items:center;font-size:11px;color:#9CA3AF;flex-wrap:wrap;">
      <!-- Node type toggles -->
      <span style="display:flex;align-items:center;gap:4px;">
        <svg width="16" height="16"><circle cx="8" cy="8" r="7"
          :fill="NODE_COLORS.bbs.fill" :stroke="NODE_COLORS.bbs.stroke" stroke-width="1.5"/></svg>BBS
      </span>
      <button
        v-for="[type, label, w] in [
          ['digi',    'Digi',    14],
          ['both',    'Both',    14],
          ['station', 'Station', 10],
          ['netrom',  'NETROM',  13],
        ]"
        :key="type"
        type="button"
        @click="toggleType(type)"
        :aria-pressed="!hiddenTypes.has(type)"
        :title="type === 'station'
          ? (hiddenTypes.has(type) ? 'Show stations (NETROM-capable stations remain as purple)' : 'Hide stations (NETROM-capable stations stay visible as purple)')
          : (hiddenTypes.has(type) ? `Show ${label}` : `Hide ${label}`)"
        :style="{
          display: 'flex', alignItems: 'center', gap: '4px',
          background: 'transparent', border: 'none', padding: 0,
          color: 'inherit', font: 'inherit', cursor: 'pointer',
          opacity: hiddenTypes.has(type) ? 0.3 : 1,
          textDecoration: hiddenTypes.has(type) ? 'line-through' : 'none',
        }"
      >
        <svg :width="w" :height="w">
          <circle :cx="w/2" :cy="w/2" :r="w/2-1"
            :fill="NODE_COLORS[type].fill" :stroke="NODE_COLORS[type].stroke" stroke-width="1.5"/>
        </svg>
        <template v-if="type === 'station'">
          <svg width="12" height="12" style="margin-left:1px;">
            <circle cx="6" cy="6" r="5"
              :fill="NODE_COLORS.station.fill" :stroke="NODE_COLORS.netrom.fill" stroke-width="2"/>
          </svg>
        </template>
        {{ label }}
      </button>
      <!-- Edge type toggles -->
      <button
        type="button"
        @click="showRfEdges = !showRfEdges"
        :aria-pressed="showRfEdges"
        :title="showRfEdges ? 'Hide RF edges' : 'Show RF edges'"
        :style="{
          display: 'flex', alignItems: 'center', gap: '4px', marginLeft: '4px',
          background: 'transparent', border: 'none', padding: 0,
          color: 'inherit', font: 'inherit', cursor: 'pointer',
          opacity: showRfEdges ? 1 : 0.3,
          textDecoration: showRfEdges ? 'none' : 'line-through',
        }"
      >
        <svg width="30" height="6"><line x1="0" y1="3" x2="30" y2="3" :stroke="RF_EDGE_COLOR" stroke-width="1.5"/></svg>
        <svg width="30" height="6"><line x1="0" y1="3" x2="30" y2="3" :stroke="RF_EDGE_COLOR" stroke-width="4"/></svg>
        RF
      </button>
      <button
        type="button"
        @click="showNetromEdges = !showNetromEdges"
        :aria-pressed="showNetromEdges"
        :title="showNetromEdges ? 'Hide NETROM edges' : 'Show NETROM edges'"
        :style="{
          display: 'flex', alignItems: 'center', gap: '4px',
          background: 'transparent', border: 'none', padding: 0,
          color: 'inherit', font: 'inherit', cursor: 'pointer',
          opacity: showNetromEdges ? 1 : 0.3,
          textDecoration: showNetromEdges ? 'none' : 'line-through',
        }"
      >
        <svg width="30" height="6"><line x1="0" y1="3" x2="30" y2="3" :stroke="NETROM_EDGE_COLOR" stroke-width="1.5" stroke-dasharray="4,2"/></svg>
        NETROM route
      </button>
    </div>
  </div>
</template>
