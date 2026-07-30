<script setup>
import { ref, onMounted } from 'vue'
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
  if (activeTab.value === 'network') return loadGraph()
  if (activeTab.value === 'map') return Promise.all([load(), loadGraph()])
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
  const res = await fetch(`/api/heard/${encodeURIComponent(callsign)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ lat, lon, comment, nodename, kanode_alias }),
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
      <v-tab value="log"     prepend-icon="mdi-table">Log</v-tab>
      <v-tab value="network" prepend-icon="mdi-graph"        @click="!graphData && loadGraph()">Network</v-tab>
      <v-tab value="map"     prepend-icon="mdi-map"          @click="!graphData && loadGraph()">Map</v-tab>
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
            Stations are plotted at their stored coordinates.
            RF hop edges are drawn when both endpoints have coordinates.
            Faded markers are outside the current max-age window.
          </span>
        </div>
        <HeardGeoMap :stations="stations" :graph-data="graphData" :loading="graphLoading || loading" />
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
