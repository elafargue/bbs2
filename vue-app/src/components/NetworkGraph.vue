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

const NODE_CFG = {
  bbs:     { r: 18, fill: '#F59E0B', stroke: '#B45309', label: true },
  digi:    { r: 12, fill: '#3B82F6', stroke: '#1D4ED8', label: true },
  both:    { r: 12, fill: '#10B981', stroke: '#065F46', label: true },
  station: { r:  6, fill: '#6B7280', stroke: '#374151', label: false },
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

  const { bbs, nodes: nodeMap, edges } = props.graphData
  if (!nodeMap || !edges) return

  const svg = d3.select(svgEl.value)
  svg.selectAll('*').remove()

  // Convert to D3-compatible arrays
  const nodesArr = Object.entries(nodeMap).map(([id, d]) => ({ id, ...d }))
  const linksArr = edges.map(e => ({ ...e }))   // source/target are string IDs

  if (!nodesArr.length) return

  const W = width.value
  const H = height.value

  // Arrow marker defs (one per colour so arrowheads match stroke)
  const defs = svg.append('defs')
  const arrowColors = ['#3B82F6', '#10B981', '#6B7280', '#F59E0B', '#9CA3AF']
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

  const zoomLayer = svg.append('g').attr('class', 'zoom-layer')
  const edgeGroup = zoomLayer.append('g').attr('class', 'edges')
  const nodeGroup = zoomLayer.append('g').attr('class', 'nodes')

  // Edge width scale
  const maxCount = d3.max(linksArr, d => d.count) || 1
  const strokeW  = d3.scaleLog()
    .domain([1, Math.max(maxCount, 2)])
    .range([1.2, 5])
    .clamp(true)

  // Link elements
  const link = edgeGroup.selectAll('line')
    .data(linksArr)
    .enter().append('line')
      .attr('stroke', '#9CA3AF')
      .attr('stroke-opacity', 0.7)
      .attr('stroke-width', d => strokeW(d.count))
      .attr('marker-end', 'url(#arrow-9CA3AF)')

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
    .attr('r', d => NODE_CFG[d.type]?.r ?? 8)
    .attr('fill', d => NODE_CFG[d.type]?.fill ?? '#9CA3AF')
    .attr('stroke', d => NODE_CFG[d.type]?.stroke ?? '#374151')
    .attr('stroke-width', 1.5)

  // Labels: always on bbs/digi/both; on station only if degree > 0
  const degreeMap = {}
  linksArr.forEach(e => {
    degreeMap[e.source.id ?? e.source] = (degreeMap[e.source.id ?? e.source] || 0) + 1
    degreeMap[e.target.id ?? e.target] = (degreeMap[e.target.id ?? e.target] || 0) + 1
  })

  node.filter(d => NODE_CFG[d.type]?.label || (degreeMap[d.id] || 0) > 0)
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
        html: `<strong>${d.id}</strong><br/>Type: ${d.type}<br/>In: ${inDeg} &nbsp; Out: ${outDeg}<br/>Frames: ${totalFrames}`,
      }
    })
    .on('mousemove', event => {
      tooltip.value.x = event.offsetX + 12
      tooltip.value.y = event.offsetY - 8
    })
    .on('mouseleave', () => { tooltip.value.show = false })

  // ── Force simulation ───────────────────────────────────────────────────

  if (sim) sim.stop()
  sim = d3.forceSimulation(nodesArr)
    .force('link', d3.forceLink(linksArr)
      .id(d => d.id)
      .distance(d => {
        // Longer links for station→digi hops to spread the graph
        const sType = typeof d.source === 'object' ? d.source.type : 'station'
        const tType = typeof d.target === 'object' ? d.target.type : 'station'
        if (sType === 'station' || tType === 'station') return 110
        return 80
      })
      .strength(0.7)
    )
    .force('charge', d3.forceManyBody().strength(d => d.type === 'bbs' ? -400 : -180))
    .force('collide', d3.forceCollide().radius(d => (NODE_CFG[d.type]?.r ?? 8) + 8))
    .force('center', d3.forceCenter(W / 2, H / 2))

  // Pin BBS node to center
  const bbsNode = nodesArr.find(n => n.type === 'bbs')
  if (bbsNode) { bbsNode.fx = W / 2; bbsNode.fy = H / 2 }

  // ── Zoom & pan (scroll wheel + 2-finger touch) ────────────────────────
  zoomBehavior = d3.zoom()
    .scaleExtent([0.15, 8])
    .on('zoom', event => { zoomLayer.attr('transform', event.transform) })
  svg.call(zoomBehavior)
     .on('dblclick.zoom', null)   // double-click handled as node drag, not zoom reset

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
      v-if="!loading && (!graphData || !graphData.edges?.length)"
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

    <!-- Legend (bottom-left) -->
    <div style="position:absolute;bottom:12px;left:14px;display:flex;gap:14px;align-items:center;font-size:11px;color:#9CA3AF;">
      <span style="display:flex;align-items:center;gap:4px;">
        <svg width="16" height="16"><circle cx="8" cy="8" r="7" fill="#F59E0B" stroke="#B45309" stroke-width="1.5"/></svg>BBS
      </span>
      <span style="display:flex;align-items:center;gap:4px;">
        <svg width="14" height="14"><circle cx="7" cy="7" r="6" fill="#3B82F6" stroke="#1D4ED8" stroke-width="1.5"/></svg>Digi
      </span>
      <span style="display:flex;align-items:center;gap:4px;">
        <svg width="14" height="14"><circle cx="7" cy="7" r="6" fill="#10B981" stroke="#065F46" stroke-width="1.5"/></svg>Both
      </span>
      <span style="display:flex;align-items:center;gap:4px;">
        <svg width="10" height="10"><circle cx="5" cy="5" r="4" fill="#6B7280" stroke="#374151" stroke-width="1.5"/></svg>Station
      </span>
      <span style="display:flex;align-items:center;gap:4px;margin-left:4px;">
        <svg width="30" height="6"><line x1="0" y1="3" x2="30" y2="3" stroke="#9CA3AF" stroke-width="1.5"/></svg>
        <svg width="30" height="6"><line x1="0" y1="3" x2="30" y2="3" stroke="#9CA3AF" stroke-width="4"/></svg>
        edge weight
      </span>
    </div>
  </div>
</template>
