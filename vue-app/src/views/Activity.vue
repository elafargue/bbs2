<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import socket from '../socket.js'

const lines = ref([])
const filter = ref('')
const autoScroll = ref(true)
const logBox = ref(null)

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

onMounted(async () => {
  // Load recent history
  const res = await fetch('/api/activity?n=200')
  if (res.ok) {
    const data = await res.json()
    lines.value = data.lines || []
    await nextTick()
    scrollToBottom()
  }

  socket.on('bbs_log_line', async (data) => {
    lines.value.push(data.line)
    if (lines.value.length > 2000) lines.value.shift()
    await nextTick()
    scrollToBottom()
  })
})

onUnmounted(() => {
  socket.off('bbs_log_line')
})
</script>

<template>
  <v-container fluid>
    <v-card>
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
  </v-container>
</template>
