<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import socket from '../socket.js'

// ── Refs ──────────────────────────────────────────────────────────────────────
const terminalEl = ref(null)
const connected  = ref(false)
const statusText = ref('Connecting…')
const statusColor = ref('warning')

// ── xterm instances (not reactive — managed imperatively) ─────────────────────
let xterm        = null
let fitAddon     = null
let resizeObs    = null

// ── Lifecycle ─────────────────────────────────────────────────────────────────
onMounted(() => {
  xterm = new Terminal({
    cursorBlink: true,
    cursorStyle: 'block',
    scrollback: 2000,
    // convertEol: false — the BBS sends \r\n; let xterm handle it natively
    fontFamily: '"Cascadia Code", "JetBrains Mono", Menlo, Monaco, "Courier New", monospace',
    fontSize: 14,
    lineHeight: 1.2,
    theme: {
      background:    '#0d1117',
      foreground:    '#c9d1d9',
      cursor:        '#58a6ff',
      selectionBackground: '#264f78',
      black:         '#0d1117',
      red:           '#ff7b72',
      green:         '#3fb950',
      yellow:        '#d29922',
      blue:          '#58a6ff',
      magenta:       '#bc8cff',
      cyan:          '#39c5cf',
      white:         '#b1bac4',
      brightBlack:   '#6e7681',
      brightRed:     '#ffa198',
      brightGreen:   '#56d364',
      brightYellow:  '#e3b341',
      brightBlue:    '#79c0ff',
      brightMagenta: '#d2a8ff',
      brightCyan:    '#56d4dd',
      brightWhite:   '#f0f6fc',
    },
  })

  fitAddon = new FitAddon()
  xterm.loadAddon(fitAddon)
  xterm.open(terminalEl.value)
  fitAddon.fit()

  // ── Input: user types → BBS ────────────────────────────────────────────────
  xterm.onData(data => {
    if (connected.value) {
      socket.emit('web_terminal_input', { data })
    }
  })

  // ── Socket.IO events ───────────────────────────────────────────────────────
  socket.on('web_terminal_ready', () => {
    connected.value  = true
    statusText.value = 'Connected'
    statusColor.value = 'success'
    // Send initial terminal dimensions
    const dims = fitAddon.proposeDimensions()
    if (dims) {
      socket.emit('web_terminal_resize', { cols: dims.cols, rows: dims.rows })
    }
    xterm.focus()
  })

  socket.on('web_terminal_output', ({ data }) => {
    xterm.write(data)
  })

  socket.on('web_terminal_closed', () => {
    connected.value  = false
    statusText.value = 'Disconnected'
    statusColor.value = 'error'
    xterm.write('\r\n\r\n\x1b[90m[Session ended — click Reconnect to start a new one]\x1b[0m\r\n')
  })

  socket.on('web_terminal_error', ({ message }) => {
    statusText.value  = `Error: ${message}`
    statusColor.value = 'error'
    xterm.write(`\r\n\x1b[91mError: ${message}\x1b[0m\r\n`)
  })

  // ── Auto-resize when container size changes ────────────────────────────────
  resizeObs = new ResizeObserver(() => {
    fitAddon.fit()
    if (connected.value) {
      socket.emit('web_terminal_resize', { cols: xterm.cols, rows: xterm.rows })
    }
  })
  resizeObs.observe(terminalEl.value)

  // ── Start the BBS session ──────────────────────────────────────────────────
  socket.emit('web_terminal_connect', {})
})

onUnmounted(() => {
  socket.emit('web_terminal_disconnect', {})
  socket.off('web_terminal_ready')
  socket.off('web_terminal_output')
  socket.off('web_terminal_closed')
  socket.off('web_terminal_error')
  if (resizeObs) resizeObs.disconnect()
  if (xterm) xterm.dispose()
})

// ── Actions ───────────────────────────────────────────────────────────────────
function reconnect() {
  if (connected.value) return
  if (xterm) xterm.clear()
  statusText.value  = 'Connecting…'
  statusColor.value = 'warning'
  socket.emit('web_terminal_connect', {})
}

function disconnect() {
  if (!connected.value) return
  socket.emit('web_terminal_disconnect', {})
  connected.value  = false
  statusText.value = 'Disconnected'
  statusColor.value = 'error'
}
</script>

<template>
  <div class="terminal-page">
    <!-- Toolbar -->
    <div class="terminal-toolbar">
      <span class="text-subtitle-2 mr-3">BBS Terminal</span>
      <v-chip :color="statusColor" size="small" class="mr-3">
        {{ statusText }}
      </v-chip>
      <v-btn
        v-if="!connected"
        size="small"
        variant="outlined"
        prepend-icon="mdi-refresh"
        @click="reconnect"
      >
        Reconnect
      </v-btn>
      <v-btn
        v-else
        size="small"
        variant="outlined"
        color="error"
        prepend-icon="mdi-close"
        @click="disconnect"
      >
        Disconnect
      </v-btn>
    </div>

    <!-- xterm.js container -->
    <div ref="terminalEl" class="terminal-container" />
  </div>
</template>

<style scoped>
.terminal-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: #0d1117;
}

.terminal-toolbar {
  display: flex;
  align-items: center;
  padding: 6px 12px;
  background: #161b22;
  border-bottom: 1px solid #30363d;
  flex-shrink: 0;
}

.terminal-container {
  flex: 1;
  min-height: 0;
  padding: 4px;
  overflow: hidden;
}

/* Let xterm fill the container */
.terminal-container :deep(.xterm) {
  height: 100%;
}

.terminal-container :deep(.xterm-viewport) {
  /* Hide native scrollbar — xterm manages its own */
  overflow-y: hidden !important;
}
</style>
