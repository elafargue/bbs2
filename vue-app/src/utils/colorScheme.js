/**
 * Shared color scheme for the heard-stations views.
 *
 * Single source of truth for node colors, edge colors, and alias-type colors
 * used in HeardConfig.vue (table chips), NetworkGraph.vue (D3 force graph),
 * and HeardGeoMap.vue (Leaflet map).  Keep this file in sync with any visual
 * legend changes in those views.
 *
 * Sizing (node radius, edge weight) is left to each component since the
 * force graph wants larger nodes than the geographic map.
 */

// Fill + stroke per node type.
export const NODE_COLORS = {
  bbs:     { fill: '#F59E0B', stroke: '#B45309' },
  digi:    { fill: '#3B82F6', stroke: '#1D4ED8' },
  both:    { fill: '#10B981', stroke: '#065F46' },
  station: { fill: '#6B7280', stroke: '#374151' },
  netrom:  { fill: '#7C3AED', stroke: '#5B21B6' },
}

// Edge colors used for RF hops and NETROM routing edges, consistent across
// the network graph and the geo map.
export const RF_EDGE_COLOR     = '#60A5FA'   // bright blue, readable on dark
export const NETROM_EDGE_COLOR = '#A78BFA'   // light purple

// Inline text colors for alias display in the heard log callsign cell.
// Distinct from the graph/edge colors so each context can be tuned
// independently.
export const ALIAS_COLORS = {
  netrom:  '#A78BFA',   // matches NETROM edges + NETROM chip
  kanode:  '#4ADE80',   // matches "digi" chip
  beacon:  '#9CA3AF',   // grey/muted — beacon alias is informational only
}
