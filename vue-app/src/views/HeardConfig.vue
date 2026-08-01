<script setup>
import { ref, computed, onMounted } from 'vue'
import NetworkGraph from '../components/NetworkGraph.vue'
import HeardGeoMap  from '../components/HeardGeoMap.vue'
import { ALIAS_COLORS } from '../utils/colorScheme'

const stations    = ref([])
const maxAge      = ref(24)
const loading     = ref(false)
const saving      = ref(false)
const clearing    = ref(false)
const clearDialog = ref(false)
const snackbar    = ref({ show: false, text: '', color: 'success' })
const activeTab   = ref('log')

// Per-callsign path drill-down
const pathsDialog  = ref(false)
const pathsCall    = ref('')
const pathsRows    = ref([])
const pathsLoading = ref(false)

// Edit station (lat / lon / nodename / comment)
const editDialog = ref(false)
const editItem   = ref({ callsign: '', transport: '', lat: '', lon: '', nodename: '', comment: '' })
const editSaving = ref(false)

// Delete station
const deleteDialog   = ref(false)
const deleteCallsign = ref('')
const deleting       = ref(false)

// Network graph
const graphData    = ref(null)
const graphLoading = ref(false)

// NETROM routing table
const netromRoutes        = ref([])
const netromRoutesLoading = ref(false)

// Physical-station entities (SSIDs grouped by base callsign)
const entities        = ref([])
const entitiesLoading = ref(false)
const entitySearch    = ref('')
const entityExpanded  = ref([])

// Entity edit (canonical nodename / notes)
const entityDialog = ref(false)
const entityItem   = ref({
  base_call: '', canonical_nodename: '', notes: '', nodename: '',
  ssids: [], aliases: [], netrom_aliases: [], kanode_aliases: [], beacon_aliases: [],
  services: [], transports: [], members: [],
  last_beacon_text: '', last_beacon_ts: 0, first_heard: 0, last_heard: 0, count: 0,
})
const entitySaving = ref(false)

const filteredEntities = computed(() => {
  // `clearable` sets the model to null, so guard against null/empty.
  const q = (entitySearch.value || '').trim().toUpperCase()
  if (!q) return entities.value
  return entities.value.filter(e =>
    e.base_call.toUpperCase().includes(q) ||
    (e.nodename || '').toUpperCase().includes(q) ||
    (e.ssids   || []).some(s => s.toUpperCase().includes(q)) ||
    (e.aliases || []).some(a => a.toUpperCase().includes(q))
  )
})

// Log search
const search = ref('')


function fmtTs(unix) {
  if (!unix) return '—'
  return new Date(unix * 1000).toLocaleString()
}

async function load() {
  loading.value = true
  const [listRes, cfgRes] = await Promise.all([
    fetch('/api/heard'),
    fetch('/api/heard/settings'),
  ])
  if (listRes.ok) stations.value = await listRes.json()
  if (cfgRes.ok) {
    const cfg = await cfgRes.json()
    maxAge.value = cfg.max_age_hours ?? 24
  }
  loading.value = false
}

async function loadGraph() {
  graphLoading.value = true
  const res = await fetch('/api/heard/graph')
  if (res.ok) graphData.value = await res.json()
  else snackbar.value = { show: true, text: 'Failed to load graph.', color: 'error' }
  graphLoading.value = false
}

async function loadNetromRoutes() {
  netromRoutesLoading.value = true
  const res = await fetch('/api/heard/netrom-routes')
  if (res.ok) netromRoutes.value = await res.json()
  else snackbar.value = { show: true, text: 'Failed to load NETROM routes.', color: 'error' }
  netromRoutesLoading.value = false
}

async function loadEntities() {
  entitiesLoading.value = true
  const res = await fetch('/api/heard/entities')
  if (res.ok) entities.value = await res.json()
  else snackbar.value = { show: true, text: 'Failed to load stations.', color: 'error' }
  entitiesLoading.value = false
}

function openEntity(item) {
  entityItem.value = {
    base_call:          item.base_call,
    canonical_nodename: item.canonical_nodename ?? '',
    notes:              item.notes ?? '',
    nodename:           item.nodename ?? '',
    ssids:              item.ssids ?? [],
    aliases:            item.aliases ?? [],
    netrom_aliases:     item.netrom_aliases ?? [],
    kanode_aliases:     item.kanode_aliases ?? [],
    beacon_aliases:     item.beacon_aliases ?? [],
    services:           item.services ?? [],
    transports:         item.transports ?? [],
    members:            item.members ?? [],
    last_beacon_text:   item.last_beacon_text ?? '',
    last_beacon_ts:     item.last_beacon_ts ?? 0,
    first_heard:        item.first_heard,
    last_heard:         item.last_heard,
    count:              item.count,
    // effective reference position (read-only) + its source
    lat:                item.lat,
    lon:                item.lon,
    position_source:    item.position_source ?? '',
    // the entity's OWN sysop override, editable (blank = no override)
    override_lat:       item.override_lat != null ? String(item.override_lat) : '',
    override_lon:       item.override_lon != null ? String(item.override_lon) : '',
  }
  entityDialog.value = true
}

async function saveEntity() {
  entitySaving.value = true
  const { base_call, canonical_nodename, notes } = entityItem.value
  const ol = String(entityItem.value.override_lat).trim()
  const og = String(entityItem.value.override_lon).trim()
  // Override fields are pre-filled from the entity's own override, so always
  // sending them is safe: blank → clear (revert to freshest beacon), else pin.
  const lat = ol === '' ? null : Number(ol)
  const lon = og === '' ? null : Number(og)
  const res = await fetch(`/api/heard/entities/${encodeURIComponent(base_call)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ canonical_nodename: canonical_nodename.trim().toUpperCase(), notes, lat, lon }),
  })
  snackbar.value = {
    show: true,
    text: res.ok ? 'Station updated.' : 'Save failed.',
    color: res.ok ? 'success' : 'error',
  }
  entitySaving.value = false
  if (res.ok) {
    entityDialog.value = false
    await loadEntities()
  }
}

async function saveSettings() {
  saving.value = true
  const res = await fetch('/api/heard/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ max_age_hours: Number(maxAge.value) }),
  })
  const data = await res.json()
  snackbar.value = {
    show: true,
    text: res.ok ? 'Settings saved.' : (data.error ?? 'Save failed.'),
    color: res.ok ? 'success' : 'error',
  }
  saving.value = false
  if (res.ok) {
    await load()
    graphData.value = null  // invalidate graph
  }
}

async function clearAll() {
  clearing.value = true
  clearDialog.value = false
  const res = await fetch('/api/heard', { method: 'DELETE' })
  const data = await res.json()
  snackbar.value = {
    show: true,
    text: res.ok ? `Cleared ${data.removed} entries.` : (data.error ?? 'Clear failed.'),
    color: res.ok ? 'success' : 'error',
  }
  clearing.value = false
  if (res.ok) {
    await load()
    graphData.value = null  // invalidate graph
  }
}

function refresh() {
  if (activeTab.value === 'stations') return loadEntities()
  if (activeTab.value === 'network') return loadGraph()
  if (activeTab.value === 'map') return Promise.all([loadEntities(), loadGraph()])
  if (activeTab.value === 'netrom') return loadNetromRoutes()
  return load()
}

async function showPaths(callsign) {
  pathsCall.value = callsign
  pathsRows.value = []
  pathsDialog.value = true
  pathsLoading.value = true
  const res = await fetch(`/api/heard/paths?callsign=${encodeURIComponent(callsign)}`)
  if (res.ok) pathsRows.value = await res.json()
  pathsLoading.value = false
}

function openEdit(item) {
  editItem.value = {
    callsign:        item.callsign,
    transport:       item.transport,
    transports:      item.transports ?? [],
    source:          item.source,
    first_heard:     item.first_heard,
    last_heard:      item.last_heard,
    count:           item.count,
    lat:             item.lat != null ? String(item.lat) : '',
    lon:             item.lon != null ? String(item.lon) : '',
    nodename:        item.beacon_alias ?? item.nodename ?? '',
    netrom_alias:    item.netrom_alias ?? '',
    kanode_alias:    item.kanode_alias ?? '',
    comment:         item.comment ?? '',
    position_source: item.position_source ?? '',
    service:         item.service ?? '',
    last_beacon_text: item.last_beacon_text ?? '',
    last_beacon_ts:  item.last_beacon_ts ?? 0,
  }
  editDialog.value = true
}

async function saveEdit() {
  editSaving.value = true
  const { callsign } = editItem.value
  const lat          = editItem.value.lat !== '' ? Number(editItem.value.lat) : null
  const lon          = editItem.value.lon !== '' ? Number(editItem.value.lon) : null
  const comment      = editItem.value.comment
  const nodename     = editItem.value.nodename
  const kanode_alias = editItem.value.kanode_alias.trim().toUpperCase()
  const service      = editItem.value.service
  const res = await fetch(`/api/heard/${encodeURIComponent(callsign)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ lat, lon, comment, nodename, kanode_alias, service }),
  })
  const data = res.ok ? await res.json() : {}
  let text = res.ok ? 'Station updated.' : 'Save failed.'
  if (res.ok && data.merged) {
    text += ` Merged ${data.merged} (${data.events_merged ?? 0} events`
    if (data.position_transferred) text += ', position transferred'
    text += ').'
  }
  snackbar.value = { show: true, text, color: res.ok ? 'success' : 'error' }
  editSaving.value = false
  if (res.ok) {
    editDialog.value = false
    await load()
    graphData.value = null  // invalidate graph — node types may have changed
  }
}

async function deleteStation() {
  deleting.value = true
  deleteDialog.value = false
  const res = await fetch(`/api/heard/${encodeURIComponent(deleteCallsign.value)}`, {
    method: 'DELETE',
  })
  const data = await res.json()
  snackbar.value = {
    show: true,
    text: res.ok ? `Deleted ${deleteCallsign.value}.` : (data.error ?? 'Delete failed.'),
    color: res.ok ? 'success' : 'error',
  }
  deleting.value = false
  if (res.ok) {
    await load()
    graphData.value = null
  }
}

onMounted(load)
</script>

<template>
  <v-container fluid>
    <!-- Page title -->
    <v-row align="center" class="mb-2">
      <v-col>
        <div class="text-h5 font-weight-bold">
          <v-icon class="mr-2">mdi-ear-hearing</v-icon>Heard Stations
        </div>
      </v-col>
    </v-row>
    <!-- Settings row -->
    <v-row align="center" class="mb-2">
      <v-col cols="12" sm="5">
        <v-text-field
          v-model.number="maxAge"
          label="Max age (hours)"
          hint="Entries older than this are pruned. 0 = keep forever."
          persistent-hint
          type="number"
          min="0"
          variant="outlined"
          density="compact"
        />
      </v-col>
      <v-col cols="12" sm="5" class="d-flex ga-2">
        <v-btn
          color="primary"
          variant="tonal"
          prepend-icon="mdi-content-save"
          :loading="saving"
          @click="saveSettings"
        >
          Save
        </v-btn>
        <v-btn
          color="error"
          variant="tonal"
          prepend-icon="mdi-delete-sweep"
          :loading="clearing"
          @click="clearDialog = true"
        >
          Clear all
        </v-btn>
      </v-col>
      <v-col cols="12" sm="2" class="d-flex justify-end">
        <v-btn icon="mdi-refresh" variant="text" :loading="loading || graphLoading" @click="refresh" />
      </v-col>
    </v-row>

    <!-- Tabs: Log / Network / Map -->
    <v-tabs v-model="activeTab" density="compact" class="mb-2">
      <v-tab value="log"      prepend-icon="mdi-table">Log</v-tab>
      <v-tab value="stations" prepend-icon="mdi-account-group" @click="!entities.length && loadEntities()">Stations</v-tab>
      <v-tab value="network" prepend-icon="mdi-graph"        @click="!graphData && loadGraph()">Network</v-tab>
      <v-tab value="map"     prepend-icon="mdi-map"          @click="!graphData && loadGraph(); !entities.length && loadEntities()">Map</v-tab>
      <v-tab value="netrom"  prepend-icon="mdi-router-network" @click="!netromRoutes.length && loadNetromRoutes()">NETROM</v-tab>
    </v-tabs>

    <v-window v-model="activeTab">
      <!-- Log tab -->
      <v-window-item value="log">
        <v-text-field
          v-model="search"
          prepend-inner-icon="mdi-magnify"
          label="Search callsign…"
          variant="outlined"
          density="compact"
          clearable
          hide-details
          class="mb-2"
          style="max-width: 320px"
        />
        <v-data-table
          :search="search"
          :headers="[
            { title: 'Callsign',    key: 'callsign',    sortable: true },
            { title: 'Dest',        key: 'dest',        sortable: true },
            { title: 'Via (last)',  key: 'via',         sortable: true },
            { title: 'Comment',    key: 'comment',     sortable: true },
            { title: 'Last Heard',  key: 'last_heard',  sortable: true },
            { title: 'Count',       key: 'count',       sortable: true },
            { title: '',            key: 'actions',     sortable: false },
          ]"
          :items="stations"
          :loading="loading"
          :row-props="({ item }) => item.expired ? { class: 'text-medium-emphasis' } : {}"
          density="compact"
          hover
        >
          <template #item.callsign="{ item }">
            <span :class="item.transport === '' && item.source !== 'heard' ? 'text-medium-emphasis' : ''">{{ item.callsign }}</span>
            <!-- Alias display: priority NETROM → Ka-Node → beacon, with a title attribute
                 for screen readers / hover so the type isn't conveyed by color alone. -->
            <span v-if="item.netrom_alias" class="ml-1 text-caption"
                  :style="`color:${ALIAS_COLORS.netrom}`"
                  title="NET/ROM alias">({{ item.netrom_alias }})</span>
            <span v-else-if="item.kanode_alias" class="ml-1 text-caption"
                  :style="`color:${ALIAS_COLORS.kanode}`"
                  title="Ka-Node alias">({{ item.kanode_alias }})</span>
            <span v-else-if="item.beacon_alias" class="ml-1 text-caption text-medium-emphasis"
                  title="Beacon alias">({{ item.beacon_alias }})</span>
            <v-chip
              v-if="item.transport === '' && item.source !== 'heard'"
              size="x-small"
              variant="outlined"
              class="ml-1"
              color="blue-grey"
            >relay</v-chip>
            <v-chip
              v-if="(item.transport === '' && item.source === 'heard') || item.kanode_alias"
              size="x-small"
              variant="outlined"
              class="ml-1"
              color="success"
            >digi</v-chip>
            <v-chip
              v-if="item.transports?.includes('netrom') || item.netrom_alias"
              size="x-small"
              variant="tonal"
              class="ml-1"
              color="deep-purple"
            >NETROM</v-chip>
          </template>
          <template #item.last_heard="{ item }">
            <span v-if="item.source === 'heard' || item.last_heard">{{ fmtTs(item.last_heard) }}</span>
            <span v-else class="text-disabled">—</span>
          </template>
          <template #item.first_heard="{ item }">
            <span v-if="item.transport !== '' || item.source === 'heard'">{{ fmtTs(item.first_heard) }}</span>
            <span v-else class="text-disabled">—</span>
          </template>
          <template #item.via="{ item }">
            <span v-if="item.transport === '' && item.source !== 'heard'" class="text-disabled">—</span>
            <span v-else-if="item.transport === '' && item.source === 'heard'" class="text-disabled">direct</span>
            <span v-else-if="item.via" class="text-mono">{{ item.via }}</span>
            <span v-else-if="item.source === 'heard'" class="text-disabled">direct</span>
            <span v-else class="text-disabled">—</span>
          </template>
          <template #item.lat="{ item }">
            <span v-if="item.lat != null">{{ Number(item.lat).toFixed(4) }}</span>
            <span v-else class="text-disabled">—</span>
          </template>
          <template #item.lon="{ item }">
            <span v-if="item.lon != null">{{ Number(item.lon).toFixed(4) }}</span>
            <span v-else class="text-disabled">—</span>
          </template>
          <template #item.comment="{ item }">
            <span v-if="item.comment">{{ item.comment }}</span>
            <span v-else class="text-disabled">—</span>
          </template>
          <template #item.actions="{ item }">
            <v-btn
              v-if="item.source === 'heard'"
              size="small"
              variant="text"
              icon="mdi-map-marker-path"
              :title="`Paths for ${item.callsign}`"
              @click="showPaths(item.callsign)"
            />
            <v-btn
              size="small"
              variant="text"
              icon="mdi-pencil"
              :title="`Edit ${item.callsign}`"
              @click="openEdit(item)"
            />
            <v-btn
              size="small"
              variant="text"
              icon="mdi-delete"
              color="error"
              :title="`Delete ${item.callsign}`"
              @click="deleteCallsign = item.callsign; deleteDialog = true"
            />
          </template>
        </v-data-table>
        <div class="mt-2 text-caption text-medium-emphasis">
          Grayed-out stations are outside the current max-age window but are retained in the database.
        </div>
      </v-window-item>

      <!-- Stations tab (physical stations: SSIDs grouped by base callsign) -->
      <v-window-item value="stations">
        <v-text-field
          v-model="entitySearch"
          prepend-inner-icon="mdi-magnify"
          label="Search callsign / alias…"
          variant="outlined"
          density="compact"
          clearable
          hide-details
          class="mb-2"
          style="max-width: 320px"
        />
        <v-data-table
          v-model:expanded="entityExpanded"
          item-value="base_call"
          show-expand
          :headers="[
            { title: 'Station',    key: 'base_call',  sortable: true },
            { title: 'Aliases',    key: 'aliases',    sortable: false },
            { title: 'SSIDs',      key: 'ssids',      sortable: false },
            { title: 'Services',   key: 'services',   sortable: false },
            { title: 'Via',        key: 'transports', sortable: false },
            { title: 'Last Heard', key: 'last_heard', sortable: true },
            { title: 'Count',      key: 'count',      sortable: true },
            { title: '',           key: 'actions',    sortable: false },
          ]"
          :items="filteredEntities"
          :loading="entitiesLoading"
          density="compact"
          hover
        >
          <template #item.base_call="{ item }">
            <span class="text-mono font-weight-medium">{{ item.base_call }}</span>
            <span class="ml-1 text-caption text-medium-emphasis">×{{ item.ssids.length }}</span>
            <span v-if="item.canonical_nodename" class="ml-1 text-caption" :style="`color:${ALIAS_COLORS.netrom}`"
                  title="Sysop-set canonical name">{{ item.canonical_nodename }}</span>
          </template>
          <template #item.aliases="{ item }">
            <template v-if="item.netrom_aliases?.length || item.kanode_aliases?.length || item.beacon_aliases?.length">
              <v-chip v-for="a in item.netrom_aliases" :key="'n'+a" size="x-small" variant="tonal"  color="deep-purple" class="mr-1 mb-1" title="NET/ROM alias (routing)">{{ a }}</v-chip>
              <v-chip v-for="a in item.kanode_aliases" :key="'k'+a" size="x-small" variant="tonal"  color="success"     class="mr-1 mb-1" title="Ka-Node alias (digi)">{{ a }}</v-chip>
              <v-chip v-for="a in item.beacon_aliases" :key="'b'+a" size="x-small" variant="outlined" color="blue-grey"  class="mr-1 mb-1" title="Beacon ID (self-declared)">{{ a }}</v-chip>
            </template>
            <span v-else class="text-disabled">—</span>
          </template>
          <template #item.ssids="{ item }">
            <v-chip
              v-for="s in item.ssids"
              :key="s"
              size="x-small"
              variant="outlined"
              class="mr-1 mb-1"
            >{{ s }}</v-chip>
          </template>
          <template #item.services="{ item }">
            <template v-if="item.services.length">
              <v-chip v-for="(sv, i) in item.services" :key="i" size="x-small" variant="tonal" color="teal" class="mr-1 mb-1">{{ sv }}</v-chip>
            </template>
            <span v-else class="text-disabled">—</span>
          </template>
          <template #item.transports="{ item }">
            <v-chip
              v-for="t in item.transports"
              :key="t"
              size="x-small"
              variant="tonal"
              :color="t === 'netrom' ? 'deep-purple' : 'blue-grey'"
              class="mr-1 mb-1"
            >{{ t }}</v-chip>
          </template>
          <template #item.last_heard="{ item }">{{ fmtTs(item.last_heard) }}</template>
          <template #item.actions="{ item }">
            <v-btn
              size="small"
              variant="text"
              icon="mdi-pencil"
              :title="`Edit ${item.base_call}`"
              @click="openEntity(item)"
            />
          </template>
          <template #expanded-row="{ columns, item }">
            <tr>
              <td :colspan="columns.length" class="pa-2 bg-surface-light">
                <div class="text-caption text-medium-emphasis mb-1">SSIDs of {{ item.base_call }}</div>
                <v-table density="compact" class="bg-transparent">
                  <thead>
                    <tr>
                      <th class="text-caption">SSID</th>
                      <th class="text-caption">Service</th>
                      <th class="text-caption">NET/ROM</th>
                      <th class="text-caption">Ka-Node</th>
                      <th class="text-caption">Beacon</th>
                      <th class="text-caption">Via</th>
                      <th class="text-caption text-right">Count</th>
                      <th class="text-caption">Last heard</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="m in item.members" :key="m.callsign">
                      <td class="text-mono">{{ m.callsign }}</td>
                      <td><span v-if="m.service">{{ m.service }}</span><span v-else class="text-disabled">—</span></td>
                      <td><span v-if="m.netrom_alias" :style="`color:${ALIAS_COLORS.netrom}`" class="text-mono">{{ m.netrom_alias }}</span><span v-else class="text-disabled">—</span></td>
                      <td><span v-if="m.kanode_alias" :style="`color:${ALIAS_COLORS.kanode}`" class="text-mono">{{ m.kanode_alias }}</span><span v-else class="text-disabled">—</span></td>
                      <td><span v-if="m.beacon_alias" class="text-mono text-medium-emphasis">{{ m.beacon_alias }}</span><span v-else class="text-disabled">—</span></td>
                      <td><span v-if="m.transport">{{ m.transport }}</span><span v-else class="text-disabled">—</span></td>
                      <td class="text-right">{{ m.count }}</td>
                      <td>{{ fmtTs(m.last_heard) }}</td>
                    </tr>
                  </tbody>
                </v-table>
              </td>
            </tr>
          </template>
        </v-data-table>
        <div class="mt-2 text-caption text-medium-emphasis">
          One row per physical station (SSIDs folded by base callsign). Expand a row to see each SSID.
          Per-SSID location, service and Ka-Node alias are edited from the <strong>Log</strong> tab.
        </div>
      </v-window-item>

      <!-- Network tab -->
      <v-window-item value="network">
        <div class="mb-2">
          <span class="text-caption text-medium-emphasis">
            Only confirmed hops are shown (up to the last ★ in each path).
            Drag nodes to reposition. Hover for details.
          </span>
        </div>
        <NetworkGraph :graph-data="graphData" :loading="graphLoading" />
      </v-window-item>

      <!-- NETROM routing table tab -->
      <v-window-item value="netrom">
        <div class="mb-2">
          <span class="text-caption text-medium-emphasis">
            NET/ROM routing table learned from NODES broadcasts.
            Quality 255 = direct neighbor; lower values indicate routes via intermediate nodes.
          </span>
        </div>
        <v-data-table
          :headers="[
            { title: 'Destination',   key: 'dest_call',     sortable: true },
            { title: 'Alias',         key: 'alias',         sortable: true },
            { title: 'Via Neighbor',  key: 'neighbor_call', sortable: true },
            { title: 'Quality',       key: 'quality',       sortable: true },
            { title: 'Advertised by', key: 'via_call',      sortable: true },
            { title: 'Last Updated',  key: 'last_seen',     sortable: true },
          ]"
          :items="netromRoutes"
          :loading="netromRoutesLoading"
          density="compact"
          hover
        >
          <template #item.dest_call="{ item }">
            <span class="text-mono font-weight-medium">{{ item.dest_call }}</span>
          </template>
          <template #item.alias="{ item }">
            <span v-if="item.alias" :style="`color:${ALIAS_COLORS.netrom}`" class="text-mono">{{ item.alias }}</span>
            <span v-else class="text-disabled">—</span>
          </template>
          <template #item.neighbor_call="{ item }">
            <span class="text-mono">{{ item.neighbor_call }}</span>
          </template>
          <template #item.quality="{ item }">
            <span class="text-mono">{{ item.quality }}</span>
            <v-progress-linear
              :model-value="item.quality"
              :max="255"
              :color="item.quality >= 200 ? 'success' : item.quality >= 100 ? 'warning' : 'error'"
              height="3"
              rounded
              class="mt-1"
              style="max-width: 60px; display: inline-block; vertical-align: middle; margin-left: 6px;"
            />
          </template>
          <template #item.via_call="{ item }">
            <span class="text-mono">{{ item.via_call }}</span>
            <span v-if="item.via_alias" class="ml-1 text-caption text-medium-emphasis">({{ item.via_alias }})</span>
          </template>
          <template #item.last_seen="{ item }">{{ fmtTs(item.last_seen) }}</template>
        </v-data-table>
        <div v-if="!netromRoutesLoading && !netromRoutes.length" class="mt-4 text-center text-medium-emphasis text-body-2">
          No NET/ROM routes learned yet. Routes are populated from NODES broadcasts.
        </div>
      </v-window-item>

      <!-- Map tab (geographic) -->
      <v-window-item value="map">
        <div class="mb-2">
          <span class="text-caption text-medium-emphasis">
            One marker per physical station (SSIDs folded by base callsign), placed at its
            reference position — the sysop override, else the freshest beacon across its SSIDs.
            RF and NET/ROM hops stay per-SSID and connect the stations.
          </span>
        </div>
        <HeardGeoMap :entities="entities" :graph-data="graphData" :loading="graphLoading || entitiesLoading" />
      </v-window-item>
    </v-window>

    <!-- Edit station dialog -->
    <v-dialog v-model="editDialog" max-width="480">
      <v-card>
        <v-card-title class="d-flex align-center">
          <v-icon start>mdi-pencil</v-icon>
          Edit {{ editItem.callsign }}
          <span v-if="editItem.transports?.length" class="text-caption text-medium-emphasis ml-2">
            ({{ editItem.transports.join(', ') }})
          </span>
          <span v-else-if="editItem.transport" class="text-caption text-medium-emphasis ml-2">({{ editItem.transport }})</span>
          <span v-else class="text-caption text-medium-emphasis ml-2">(relay node)</span>
        </v-card-title>
        <v-divider />
        <v-card-text class="pt-4">
          <!-- Read-only info -->
          <v-row dense class="mb-2 text-body-2">
            <v-col cols="4" class="text-medium-emphasis">Transport</v-col>
            <v-col cols="8">
              {{ editItem.transports?.join(', ') || editItem.transport || '—' }}
            </v-col>
            <v-col cols="4" class="text-medium-emphasis">First heard</v-col>
            <v-col cols="8">{{ (editItem.transport !== '' || editItem.source === 'heard') ? fmtTs(editItem.first_heard) : '—' }}</v-col>
            <v-col cols="4" class="text-medium-emphasis">Last heard</v-col>
            <v-col cols="8">{{ editItem.last_heard ? fmtTs(editItem.last_heard) : '—' }}</v-col>
            <v-col cols="4" class="text-medium-emphasis">Count</v-col>
            <v-col cols="8">{{ editItem.count }}</v-col>
            <template v-if="editItem.netrom_alias">
              <v-col cols="4" class="text-medium-emphasis">NET/ROM alias</v-col>
              <v-col cols="8" :style="`color:${ALIAS_COLORS.netrom}`" class="text-mono">{{ editItem.netrom_alias }}</v-col>
            </template>
          </v-row>
          <v-divider class="mb-3" />
          <v-row dense>
            <v-col cols="6">
              <v-text-field
                v-model="editItem.lat"
                label="Latitude"
                hint="Decimal degrees, e.g. 34.0522"
                persistent-hint
                clearable
                density="compact"
              />
            </v-col>
            <v-col cols="6">
              <v-text-field
                v-model="editItem.lon"
                label="Longitude"
                hint="Decimal degrees, e.g. -118.2437"
                persistent-hint
                clearable
                density="compact"
              />
            </v-col>
            <v-col cols="12" class="mt-2">
              <v-text-field
                v-model="editItem.nodename"
                label="Beacon alias"
                hint="Auto-populated from &lt;MAP:...&gt; beacons. Informational only."
                persistent-hint
                density="compact"
              />
            </v-col>
            <v-col cols="12" class="mt-2">
              <v-text-field
                v-model="editItem.kanode_alias"
                label="Ka-Node alias"
                hint="Sysop entry: the Ka-Node digipeater alias for this TNC (e.g. KROCK). If a matching station row exists it will be merged into this one."
                persistent-hint
                density="compact"
              />
            </v-col>
            <v-col cols="12" class="mt-2">
              <v-text-field
                v-model="editItem.comment"
                label="Comment"
                hint="Optional note about this station"
                persistent-hint
                density="compact"
              />
            </v-col>
            <v-col cols="12" class="mt-2">
              <v-text-field
                v-model="editItem.service"
                label="Service"
                hint="Sysop label for what this SSID provides (e.g. BBS, Node, FBB, APRS-Digi)."
                persistent-hint
                clearable
                density="compact"
              />
            </v-col>
            <v-col v-if="editItem.last_beacon_text" cols="12" class="mt-1">
              <div class="text-caption text-medium-emphasis">
                Last beacon/ID<span v-if="editItem.last_beacon_ts"> — {{ fmtTs(editItem.last_beacon_ts) }}</span>
              </div>
              <div class="text-mono text-caption" style="white-space:pre-wrap;word-break:break-word">{{ editItem.last_beacon_text }}</div>
            </v-col>
            <v-col v-if="editItem.position_source" cols="12" class="mt-1 text-caption text-medium-emphasis">
              Position source:
              <span v-if="editItem.position_source === 'beacon'">self-reported via &lt;MAP:...&gt; beacon</span>
              <span v-else-if="editItem.position_source === 'manual'">set by sysop</span>
              <span v-else>{{ editItem.position_source }}</span>
            </v-col>
          </v-row>
        </v-card-text>
        <v-card-actions class="justify-end">
          <v-btn variant="text" @click="editDialog = false">Cancel</v-btn>
          <v-btn color="primary" variant="tonal" :loading="editSaving" @click="saveEdit">Save</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- Edit physical-station entity dialog -->
    <v-dialog v-model="entityDialog" max-width="620" scrollable>
      <v-card>
        <v-card-title class="d-flex align-center">
          <v-icon start>mdi-account-group</v-icon>
          Station {{ entityItem.base_call }}
          <span v-if="entityItem.nodename" class="text-caption ml-2" :style="`color:${ALIAS_COLORS.netrom}`">{{ entityItem.nodename }}</span>
          <v-spacer />
          <v-btn icon="mdi-close" variant="text" @click="entityDialog = false" />
        </v-card-title>
        <v-divider />
        <v-card-text class="pt-4">
          <!-- Rollup summary -->
          <v-row dense class="mb-2 text-body-2">
            <v-col cols="4" class="text-medium-emphasis">SSIDs</v-col>
            <v-col cols="8">
              <v-chip v-for="s in entityItem.ssids" :key="s" size="x-small" variant="outlined" class="mr-1 mb-1">{{ s }}</v-chip>
            </v-col>
            <template v-if="entityItem.netrom_aliases.length">
              <v-col cols="4" class="text-medium-emphasis">NET/ROM</v-col>
              <v-col cols="8">
                <v-chip v-for="a in entityItem.netrom_aliases" :key="a" size="x-small" variant="tonal" color="deep-purple" class="mr-1 mb-1">{{ a }}</v-chip>
              </v-col>
            </template>
            <template v-if="entityItem.kanode_aliases.length">
              <v-col cols="4" class="text-medium-emphasis">Ka-Node</v-col>
              <v-col cols="8">
                <v-chip v-for="a in entityItem.kanode_aliases" :key="a" size="x-small" variant="tonal" color="success" class="mr-1 mb-1">{{ a }}</v-chip>
              </v-col>
            </template>
            <template v-if="entityItem.beacon_aliases.length">
              <v-col cols="4" class="text-medium-emphasis">Beacon ID</v-col>
              <v-col cols="8">
                <v-chip v-for="a in entityItem.beacon_aliases" :key="a" size="x-small" variant="outlined" color="blue-grey" class="mr-1 mb-1">{{ a }}</v-chip>
              </v-col>
            </template>
            <v-col cols="4" class="text-medium-emphasis">Via</v-col>
            <v-col cols="8">
              <v-chip v-for="t in entityItem.transports" :key="t" size="x-small" variant="tonal"
                      :color="t === 'netrom' ? 'deep-purple' : 'blue-grey'" class="mr-1 mb-1">{{ t }}</v-chip>
            </v-col>
            <v-col cols="4" class="text-medium-emphasis">First heard</v-col>
            <v-col cols="8">{{ fmtTs(entityItem.first_heard) }}</v-col>
            <v-col cols="4" class="text-medium-emphasis">Last heard</v-col>
            <v-col cols="8">{{ fmtTs(entityItem.last_heard) }}</v-col>
            <v-col cols="4" class="text-medium-emphasis">Total count</v-col>
            <v-col cols="8">{{ entityItem.count }}</v-col>
          </v-row>

          <!-- Per-SSID detail -->
          <div class="text-caption text-medium-emphasis mb-1">Per-SSID detail</div>
          <v-table density="compact" class="mb-3">
            <thead>
              <tr>
                <th class="text-caption">SSID</th>
                <th class="text-caption">Service</th>
                <th class="text-caption">NET/ROM</th>
                <th class="text-caption">Ka-Node</th>
                <th class="text-caption">Beacon</th>
                <th class="text-caption">Via</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="m in entityItem.members" :key="m.callsign">
                <td class="text-mono">{{ m.callsign }}</td>
                <td><span v-if="m.service">{{ m.service }}</span><span v-else class="text-disabled">—</span></td>
                <td><span v-if="m.netrom_alias" :style="`color:${ALIAS_COLORS.netrom}`" class="text-mono">{{ m.netrom_alias }}</span><span v-else class="text-disabled">—</span></td>
                <td><span v-if="m.kanode_alias" :style="`color:${ALIAS_COLORS.kanode}`" class="text-mono">{{ m.kanode_alias }}</span><span v-else class="text-disabled">—</span></td>
                <td><span v-if="m.beacon_alias" class="text-mono text-medium-emphasis">{{ m.beacon_alias }}</span><span v-else class="text-disabled">—</span></td>
                <td><span v-if="m.transport">{{ m.transport }}</span><span v-else class="text-disabled">—</span></td>
              </tr>
            </tbody>
          </v-table>
          <div class="text-caption text-medium-emphasis mb-3">
            Per-SSID service &amp; Ka-Node alias are edited from the <strong>Log</strong> tab.
          </div>

          <v-divider class="mb-3" />
          <!-- Sysop-editable entity fields -->
          <v-text-field
            v-model="entityItem.canonical_nodename"
            label="Canonical node name"
            hint="Overrides the auto-detected display name for this physical station. Leave blank to use the detected alias."
            persistent-hint
            clearable
            density="compact"
          />
          <v-textarea
            v-model="entityItem.notes"
            label="Notes"
            hint="Sysop notes about this physical station (owner, location, hardware…)."
            persistent-hint
            rows="2"
            auto-grow
            density="compact"
            class="mt-3"
          />

          <!-- Reference position + sysop override -->
          <div class="text-caption text-medium-emphasis mt-4 mb-1">Reference position</div>
          <div class="text-body-2 mb-2">
            <template v-if="entityItem.lat != null">
              <span class="text-mono">{{ Number(entityItem.lat).toFixed(4) }}, {{ Number(entityItem.lon).toFixed(4) }}</span>
              <span class="text-caption text-medium-emphasis ml-1">
                (<template v-if="entityItem.position_source === 'beacon'">freshest beacon</template><template v-else-if="entityItem.position_source === 'manual'">sysop-set</template><template v-else>{{ entityItem.position_source || 'unknown' }}</template>)
              </span>
            </template>
            <span v-else class="text-disabled">none yet</span>
          </div>
          <v-row dense>
            <v-col cols="6">
              <v-text-field
                v-model="entityItem.override_lat"
                label="Override latitude"
                hint="Decimal degrees"
                persistent-hint
                clearable
                density="compact"
              />
            </v-col>
            <v-col cols="6">
              <v-text-field
                v-model="entityItem.override_lon"
                label="Override longitude"
                hint="Decimal degrees"
                persistent-hint
                clearable
                density="compact"
              />
            </v-col>
          </v-row>
          <div class="text-caption text-medium-emphasis mt-1">
            Pins this station's map position (e.g. a mobile SSID or a bad beacon). Clear both to revert to the freshest beacon.
          </div>

          <div v-if="entityItem.last_beacon_text" class="mt-3">
            <div class="text-caption text-medium-emphasis">
              Last beacon/ID<span v-if="entityItem.last_beacon_ts"> — {{ fmtTs(entityItem.last_beacon_ts) }}</span>
            </div>
            <div class="text-mono text-caption" style="white-space:pre-wrap;word-break:break-word">{{ entityItem.last_beacon_text }}</div>
          </div>
        </v-card-text>
        <v-card-actions class="justify-end">
          <v-btn variant="text" @click="entityDialog = false">Cancel</v-btn>
          <v-btn color="primary" variant="tonal" :loading="entitySaving" @click="saveEntity">Save</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- Delete station dialog -->
    <v-dialog v-model="deleteDialog" max-width="420">
      <v-card>
        <v-card-title class="d-flex align-center">
          <v-icon start color="error">mdi-delete</v-icon>
          Delete {{ deleteCallsign }}?
        </v-card-title>
        <v-card-text>
          This will permanently remove <strong>{{ deleteCallsign }}</strong> and all its path history from the database.
          Any saved coordinates or comments will be lost.
        </v-card-text>
        <v-card-actions class="justify-end">
          <v-btn variant="text" @click="deleteDialog = false">Cancel</v-btn>
          <v-btn color="error" variant="tonal" :loading="deleting" @click="deleteStation">Delete</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- Confirm clear dialog -->
    <v-dialog v-model="clearDialog" max-width="400">
      <v-card>
        <v-card-title class="d-flex align-center">
          <v-icon start color="error">mdi-delete-sweep</v-icon>
          Clear heard log?
        </v-card-title>
        <v-card-text>This will permanently delete all heard-station entries and path history.</v-card-text>
        <v-card-actions class="justify-end">
          <v-btn variant="text" @click="clearDialog = false">Cancel</v-btn>
          <v-btn color="error" variant="tonal" @click="clearAll">Clear all</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- Per-callsign paths dialog -->
    <v-dialog v-model="pathsDialog" max-width="720" scrollable>
      <v-card>
        <v-card-title class="d-flex align-center">
          <v-icon start>mdi-map-marker-path</v-icon>
          Paths heard for {{ pathsCall }}
          <v-spacer />
          <v-btn icon="mdi-close" variant="text" @click="pathsDialog = false" />
        </v-card-title>
        <v-divider />
        <v-card-text class="pa-4">
          <v-data-table
            :headers="[
              { title: 'Via path',    key: 'via',        sortable: true },
              { title: 'Transport',   key: 'transport',  sortable: true },
              { title: 'Last seen',   key: 'last_seen',  sortable: true },
              { title: 'First seen',  key: 'first_seen', sortable: true },
              { title: 'Count',       key: 'count',      sortable: true },
            ]"
            :items="pathsRows"
            :loading="pathsLoading"
            density="compact"
            hover
          >
            <template #item.last_seen="{ item }">{{ fmtTs(item.last_seen) }}</template>
            <template #item.first_seen="{ item }">{{ fmtTs(item.first_seen) }}</template>
            <template #item.via="{ item }">
              <span v-if="item.via" class="text-mono">{{ item.via }}</span>
              <span v-else class="text-disabled">direct</span>
            </template>
          </v-data-table>
        </v-card-text>
      </v-card>
    </v-dialog>

    <v-snackbar v-model="snackbar.show" :color="snackbar.color" timeout="3000">
      {{ snackbar.text }}
    </v-snackbar>
  </v-container>
</template>
