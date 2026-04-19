<script setup>
import { ref, onMounted } from 'vue'
import NetworkGraph from '../components/NetworkGraph.vue'

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
        <v-btn icon="mdi-refresh" variant="text" :loading="loading || graphLoading" @click="activeTab === 'network' ? loadGraph() : load()" />
      </v-col>
    </v-row>

    <!-- Tabs: Log / Network -->
    <v-tabs v-model="activeTab" density="compact" class="mb-2">
      <v-tab value="log"     prepend-icon="mdi-table">Log</v-tab>
      <v-tab value="network" prepend-icon="mdi-graph" @click="!graphData && loadGraph()">Network</v-tab>
    </v-tabs>

    <v-window v-model="activeTab">
      <!-- Log tab -->
      <v-window-item value="log">
        <v-data-table
          :headers="[
            { title: 'Callsign',    key: 'callsign',    sortable: true },
            { title: 'Dest',        key: 'dest',        sortable: true },
            { title: 'Transport',   key: 'transport',   sortable: true },
            { title: 'Via (last)',  key: 'via',         sortable: true },
            { title: 'Last Heard',  key: 'last_heard',  sortable: true },
            { title: 'First Heard', key: 'first_heard', sortable: true },
            { title: 'Count',       key: 'count',       sortable: true },
            { title: 'Paths',       key: 'actions',     sortable: false },
          ]"
          :items="stations"
          :loading="loading"
          density="compact"
          hover
        >
          <template #item.last_heard="{ item }">{{ fmtTs(item.last_heard) }}</template>
          <template #item.first_heard="{ item }">{{ fmtTs(item.first_heard) }}</template>
          <template #item.via="{ item }">
            <span v-if="item.via" class="text-mono">{{ item.via }}</span>
            <span v-else class="text-disabled">direct</span>
          </template>
          <template #item.actions="{ item }">
            <v-btn
              size="small"
              variant="text"
              icon="mdi-map-marker-path"
              :title="`Paths for ${item.callsign}`"
              @click="showPaths(item.callsign)"
            />
          </template>
        </v-data-table>
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
    </v-window>

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
