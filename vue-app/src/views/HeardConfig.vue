<script setup>
import { ref, onMounted } from 'vue'
import NetworkGraph from '../components/NetworkGraph.vue'
import HeardGeoMap  from '../components/HeardGeoMap.vue'

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

// Edit station (lat / lon / comment)
const editDialog = ref(false)
const editItem   = ref({ callsign: '', transport: '', lat: '', lon: '', comment: '' })
const editSaving = ref(false)

// Delete station
const deleteDialog   = ref(false)
const deleteCallsign = ref('')
const deleting       = ref(false)

// Network graph
const graphData    = ref(null)
const graphLoading = ref(false)

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
    callsign:    item.callsign,
    transport:   item.transport,
    source:      item.source,
    first_heard: item.first_heard,
    last_heard:  item.last_heard,
    count:       item.count,
    lat:         item.lat != null ? String(item.lat) : '',
    lon:         item.lon != null ? String(item.lon) : '',
    comment:     item.comment ?? '',
  }
  editDialog.value = true
}

async function saveEdit() {
  editSaving.value = true
  const { callsign, transport } = editItem.value
  const lat     = editItem.value.lat !== '' ? Number(editItem.value.lat) : null
  const lon     = editItem.value.lon !== '' ? Number(editItem.value.lon) : null
  const comment = editItem.value.comment
  const res = await fetch(`/api/heard/${encodeURIComponent(callsign)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ transport, lat, lon, comment }),
  })
  const data = await res.json()
  snackbar.value = {
    show: true,
    text: res.ok ? 'Station updated.' : (data.error ?? 'Save failed.'),
    color: res.ok ? 'success' : 'error',
  }
  editSaving.value = false
  if (res.ok) {
    editDialog.value = false
    await load()
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
  <v-container fluid class="pa-0">
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
      <v-col cols="12" sm="3">
        <v-btn
          color="primary"
          variant="tonal"
          prepend-icon="mdi-content-save"
          :loading="saving"
          @click="saveSettings"
        >
          Save
        </v-btn>
      </v-col>
      <v-col cols="12" sm="4" class="d-flex justify-end ga-2">
        <v-btn
          color="error"
          variant="tonal"
          prepend-icon="mdi-delete-sweep"
          :loading="clearing"
          @click="clearDialog = true"
        >
          Clear all
        </v-btn>
        <v-btn icon="mdi-refresh" variant="text" :loading="loading || graphLoading" @click="['network','map'].includes(activeTab) ? loadGraph() : load()" />
      </v-col>
    </v-row>

    <!-- Tabs: Log / Network / Map -->
    <v-tabs v-model="activeTab" density="compact" class="mb-2">
      <v-tab value="log"     prepend-icon="mdi-table">Log</v-tab>
      <v-tab value="network" prepend-icon="mdi-graph" @click="!graphData && loadGraph()">Network</v-tab>
      <v-tab value="map"     prepend-icon="mdi-map"   @click="!graphData && loadGraph()">Map</v-tab>
    </v-tabs>

    <v-window v-model="activeTab">
      <!-- Log tab -->
      <v-window-item value="log">
        <v-data-table
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
            <v-chip
              v-if="item.transport === '' && item.source !== 'heard'"
              size="x-small"
              variant="outlined"
              class="ml-1"
              color="blue-grey"
            >relay</v-chip>
            <v-chip
              v-if="item.transport === '' && item.source === 'heard'"
              size="x-small"
              variant="outlined"
              class="ml-1"
              color="success"
            >digi</v-chip>
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
            <span v-else class="text-disabled">direct</span>
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
        <div class="mb-2 d-flex align-center ga-2">
          <span class="text-caption text-medium-emphasis">
            Only confirmed hops are shown (up to the last ★ in each path).
            Drag nodes to reposition. Hover for details.
          </span>
          <v-spacer />
          <v-btn
            size="small"
            variant="tonal"
            prepend-icon="mdi-refresh"
            :loading="graphLoading"
            @click="loadGraph"
          >Refresh</v-btn>
        </div>
        <NetworkGraph :graph-data="graphData" :loading="graphLoading" />
      </v-window-item>

      <!-- Map tab (geographic) -->
      <v-window-item value="map">
        <div class="mb-2 d-flex align-center ga-2">
          <span class="text-caption text-medium-emphasis">
            Stations are plotted at their stored coordinates.
            RF hop edges are drawn when both endpoints have coordinates.
            Faded markers are outside the current max-age window.
          </span>
          <v-spacer />
          <v-btn
            size="small"
            variant="tonal"
            prepend-icon="mdi-refresh"
            :loading="graphLoading || loading"
            @click="Promise.all([load(), loadGraph()])"
          >Refresh</v-btn>
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
          <span v-if="editItem.transport" class="text-caption text-medium-emphasis ml-2">({{ editItem.transport }})</span>
          <span v-else class="text-caption text-medium-emphasis ml-2">(relay node)</span>
        </v-card-title>
        <v-divider />
        <v-card-text class="pt-4">
          <!-- Read-only info -->
          <v-row dense class="mb-2 text-body-2">
            <v-col cols="4" class="text-medium-emphasis">Transport</v-col>
            <v-col cols="8">{{ editItem.transport || '—' }}</v-col>
            <v-col cols="4" class="text-medium-emphasis">First heard</v-col>
            <v-col cols="8">{{ (editItem.transport !== '' || editItem.source === 'heard') ? fmtTs(editItem.first_heard) : '—' }}</v-col>
            <v-col cols="4" class="text-medium-emphasis">Last heard</v-col>
            <v-col cols="8">{{ editItem.last_heard ? fmtTs(editItem.last_heard) : '—' }}</v-col>
            <v-col cols="4" class="text-medium-emphasis">Count</v-col>
            <v-col cols="8">{{ editItem.count }}</v-col>
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
                v-model="editItem.comment"
                label="Comment"
                hint="Optional note about this station"
                persistent-hint
                density="compact"
              />
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
