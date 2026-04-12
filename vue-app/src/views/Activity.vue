<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import socket from '../socket.js'

const lines = ref([])
const filter = ref('')
const autoScroll = ref(true)
const logBox = ref(null)

const connections = ref([])
const connHeaders = [
  { title: 'Callsign',   key: 'callsign',   sortable: true },
  { title: 'Transport',  key: 'transport',  sortable: true },
  { title: 'First Seen', key: 'first_seen', sortable: true },
  { title: 'Last Seen',  key: 'last_seen',  sortable: true },
  { title: 'Auth',       key: 'auth_level', sortable: true },
]

const AUTH_LABELS = ['anon', 'ident', 'auth', 'sysop']

function fmtTs(ts) {
  if (!ts) return ''
  return new Date(ts * 1000).toLocaleString(undefined, {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  })
}

function fmtAuth(level) {
  return AUTH_LABELS[level] ?? level
}

const filteredLines = computed(() => {
  if (!filter.value) return lines.value
  const f = filter.value.toUpperCase()
  return lines.value.filter(l => l.toUpperCase().includes(f))
})

function scrollToBottom() {
  if (autoScroll.value && logBox.value) {
    logBox.value.scrollTop = logBox.value.scrollHeight
  }
}

async function loadConnections() {
  const res = await fetch('/api/activity/connections')
  if (res.ok) connections.value = await res.json()
}

onMounted(async () => {
  // Load persistent log history from DB
  const res = await fetch('/api/activity?n=2000')
  if (res.ok) {
    const data = await res.json()
    lines.value = data.lines || []
    await nextTick()
    scrollToBottom()
  }

  await loadConnections()

  socket.on('bbs_log_line', async (data) => {
    lines.value.push(data.line)
    await nextTick()
    scrollToBottom()
  })

  socket.on('user_connected',    loadConnections)
  socket.on('user_disconnected', loadConnections)
})

onUnmounted(() => {
  socket.off('bbs_log_line')
  socket.off('user_connected')
  socket.off('user_disconnected')
})
</script>

<template>
  <v-container fluid>
    <v-card class="mb-4">
      <v-card-title>
        <v-icon start>mdi-text-box-outline</v-icon>
        Activity Log
        <v-spacer />
        <v-text-field
          v-model="filter"
          placeholder="Filter by callsign…"
          density="compact"
          variant="outlined"
          hide-details
          clearable
          style="max-width:200px;"
          class="mr-2"
        />
        <v-switch
          v-model="autoScroll"
          label="Auto-scroll"
          density="compact"
          hide-details
        />
      </v-card-title>
      <v-card-text>
        <div
          ref="logBox"
          style="height:60vh; overflow-y:auto; font-family:monospace; font-size:0.78rem; background:#1e1e1e; color:#d4d4d4; padding:8px; border-radius:4px;"
        >
          <div v-for="(line, i) in filteredLines" :key="i">{{ line }}</div>
        </div>
      </v-card-text>
    </v-card>

    <v-card>
      <v-card-title>
        <v-icon start>mdi-account-clock-outline</v-icon>
        Last Connections
        <v-spacer />
        <v-btn size="small" variant="text" icon="mdi-refresh" @click="loadConnections" />
      </v-card-title>
      <v-card-text>
        <v-data-table
          :headers="connHeaders"
          :items="connections"
          density="compact"
          :items-per-page="25"
        >
          <template #item.first_seen="{ item }">{{ fmtTs(item.first_seen) }}</template>
          <template #item.last_seen="{ item }">{{ fmtTs(item.last_seen) }}</template>
          <template #item.auth_level="{ item }">{{ fmtAuth(item.auth_level) }}</template>
        </v-data-table>
      </v-card-text>
    </v-card>
  </v-container>
</template>
