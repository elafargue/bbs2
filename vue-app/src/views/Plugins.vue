<script setup>
import { ref, onMounted } from 'vue'

const plugins = ref([])
const loading = ref(false)
const snackbar = ref({ show: false, text: '', color: 'success' })

const pluginRoutes = {
  display:   '/display',
  bulletins: '/bulletins',
  chat:      '/chat',
  heard:     '/heard',
  info:      '/info',
}

const pluginIcons = {
  display:   'mdi-monitor',
  bulletins: 'mdi-bulletin-board',
  chat:      'mdi-forum',
  heard:     'mdi-ear-hearing',
  info:      'mdi-information-outline',
  lastconn:  'mdi-history',
}

function statsLine(p) {
  const skip = new Set(['name', 'display_name', 'enabled'])
  const entries = Object.entries(p).filter(([k]) => !skip.has(k))
  if (!entries.length) return null
  return entries.map(([k, v]) => `${k}: ${v}`).join('  ·  ')
}

async function loadPlugins() {
  loading.value = true
  const res = await fetch('/api/plugins')
  if (res.ok) plugins.value = await res.json()
  loading.value = false
}

async function toggle(plugin) {
  const res = await fetch(`/api/plugins/${plugin.name}/toggle`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled: !plugin.enabled }),
  })
  const data = await res.json()
  snackbar.value = {
    show: true,
    text: res.ok ? `${plugin.name} ${data.enabled ? 'enabled' : 'disabled'}` : data.error,
    color: res.ok ? 'success' : 'error',
  }
  await loadPlugins()
  window.dispatchEvent(new Event('plugins-updated'))
}

onMounted(loadPlugins)
</script>

<template>
  <v-container fluid>
    <v-card>
      <v-card-title class="d-flex align-center">
        <v-icon start>mdi-puzzle</v-icon>
        Plugin Management
        <v-spacer />
        <v-btn icon="mdi-refresh" variant="text" :loading="loading" @click="loadPlugins" />
      </v-card-title>
      <v-divider />
      <v-list lines="two">
        <v-list-item
          v-for="p in plugins"
          :key="p.name"
          :prepend-icon="pluginIcons[p.name] || 'mdi-puzzle-outline'"
        >
          <v-list-item-title>{{ p.display_name || p.name }}</v-list-item-title>
          <v-list-item-subtitle>
            <span class="text-caption" style="font-family: monospace;">{{ p.name }}</span>
            <span v-if="statsLine(p)" class="text-caption text-medium-emphasis ml-2">· {{ statsLine(p) }}</span>
          </v-list-item-subtitle>
          <template #append>
            <div class="d-flex align-center ga-2">
              <v-chip
                size="small"
                :color="p.enabled ? 'success' : 'default'"
                variant="tonal"
              >{{ p.enabled ? 'Enabled' : 'Disabled' }}</v-chip>
              <v-btn
                :color="p.enabled ? 'error' : 'success'"
                variant="tonal"
                size="small"
                @click="toggle(p)"
                >{{ p.enabled ? 'Disable' : 'Enable' }}</v-btn>
              <v-btn
                v-if="pluginRoutes[p.name]"
                variant="tonal"
                color="primary"
                size="small"
                prepend-icon="mdi-cog"
                :to="pluginRoutes[p.name]"
              >Configure</v-btn>
            </div>
          </template>
        </v-list-item>
      </v-list>
    </v-card>

    <v-snackbar v-model="snackbar.show" :color="snackbar.color" timeout="3000">
      {{ snackbar.text }}
    </v-snackbar>
  </v-container>
</template>
