<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import socket from '../socket.js'

const bbsCallsign = ref('')
const users = ref([])
const plugins = ref([])
const logLines = ref([])

const userHeaders = [
  { title: 'Callsign',   key: 'callsign'    },
  { title: 'Transport',  key: 'transport'   },
  { title: 'Auth Level', key: 'auth_level'  },
  { title: 'Idle (s)',   key: 'idle_seconds'},
]

onMounted(() => {
  socket.on('admin_dashboard_init', (data) => {
    bbsCallsign.value = data.bbs_callsign || ''
    users.value = data.users || []
    plugins.value = data.plugins || []
    logLines.value = data.log || []
  })
  socket.on('users_snapshot', (snap) => { users.value = snap })
  socket.on('plugin_stats_update', (stats) => { plugins.value = stats })
  socket.on('bbs_log_line', (data) => {
    logLines.value.push(data.line)
    if (logLines.value.length > 1000) logLines.value.shift()
  })
})

onUnmounted(() => {
  socket.off('admin_dashboard_init')
  socket.off('users_snapshot')
  socket.off('plugin_stats_update')
  socket.off('bbs_log_line')
})
</script>

<template>
  <v-container fluid>
    <v-row>
      <v-col cols="12">
        <v-card>
          <v-card-title>
            <v-icon start>mdi-radio-tower</v-icon>
            {{ bbsCallsign || 'BBS' }} — Dashboard
          </v-card-title>
        </v-card>
      </v-col>
    </v-row>

    <!-- Connected users -->
    <v-row>
      <v-col cols="12" md="7">
        <v-card>
          <v-card-title>
            <v-icon start>mdi-account-multiple</v-icon>
            Connected Users ({{ users.length }})
          </v-card-title>
          <v-data-table
            :headers="userHeaders"
            :items="users"
            :items-per-page="10"
            density="compact"
          />
        </v-card>
      </v-col>

      <!-- Plugin health -->
      <v-col cols="12" md="5">
        <v-card>
          <v-card-title>
            <v-icon start>mdi-puzzle</v-icon>
            Plugins
          </v-card-title>
          <v-list density="compact">
            <v-list-item
              v-for="p in plugins"
              :key="p.name"
              :subtitle="p.name"
            >
              <template #title>
                <span>{{ p.display_name || p.name }}</span>
              </template>
              <template #append>
                <v-chip
                  size="small"
                  :color="p.enabled ? 'success' : 'error'"
                >{{ p.enabled ? 'ON' : 'OFF' }}</v-chip>
              </template>
            </v-list-item>
          </v-list>
        </v-card>
      </v-col>
    </v-row>

    <!-- Live log -->
    <v-row>
      <v-col cols="12">
        <v-card>
          <v-card-title>
            <v-icon start>mdi-text-box-outline</v-icon>
            Live Activity Log
          </v-card-title>
          <v-card-text>
            <div
              class="log-box"
              style="height:240px; overflow-y:auto; font-family:monospace; font-size:0.8rem;"
            >
              <div v-for="(line, i) in logLines" :key="i">{{ line }}</div>
            </div>
          </v-card-text>
        </v-card>
      </v-col>
    </v-row>
  </v-container>
</template>
