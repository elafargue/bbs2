<script setup>
import { ref, onMounted } from 'vue'

const plugins = ref([])
const loading = ref(false)
const snackbar = ref({ show: false, text: '', color: 'success' })

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
}

onMounted(loadPlugins)
</script>

<template>
  <v-container fluid>
    <v-card>
      <v-card-title>
        <v-icon start>mdi-puzzle</v-icon>
        Plugin Management
        <v-spacer />
        <v-btn icon="mdi-refresh" variant="text" :loading="loading" @click="loadPlugins" />
      </v-card-title>
      <v-card-text>
        <v-row>
          <v-col
            v-for="p in plugins"
            :key="p.name"
            cols="12"
            sm="6"
            md="4"
          >
            <v-card variant="outlined">
              <v-card-title>
                {{ p.display_name || p.name }}
                <v-chip
                  size="small"
                  class="ml-2"
                  :color="p.enabled ? 'success' : 'error'"
                >{{ p.enabled ? 'Enabled' : 'Disabled' }}</v-chip>
              </v-card-title>
              <v-card-subtitle>{{ p.name }}</v-card-subtitle>
              <v-card-text>
                <!-- Extra stats from plugin (e.g. rooms for chat) -->
                <pre
                  v-if="Object.keys(p).filter(k => !['name','display_name','enabled'].includes(k)).length"
                  class="text-body-2"
                  style="white-space:pre-wrap;"
                >{{ JSON.stringify(Object.fromEntries(Object.entries(p).filter(([k]) => !['name','display_name','enabled'].includes(k))), null, 2) }}</pre>
              </v-card-text>
              <v-card-actions>
                <v-btn
                  :color="p.enabled ? 'error' : 'success'"
                  variant="tonal"
                  @click="toggle(p)"
                >
                  {{ p.enabled ? 'Disable' : 'Enable' }}
                </v-btn>
              </v-card-actions>
            </v-card>
          </v-col>
        </v-row>
      </v-card-text>
    </v-card>

    <v-snackbar v-model="snackbar.show" :color="snackbar.color" timeout="3000">
      {{ snackbar.text }}
    </v-snackbar>
  </v-container>
</template>
