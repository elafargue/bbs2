<script setup>
import { ref, onMounted } from 'vue'

const enabled = ref(false)
const maxSessions = ref(10)
const lockout = ref('')          // comma/space separated in the UI
const routes = ref([])           // array of route rows
const loading = ref(false)
const saving = ref(false)
const snackbar = ref({ show: false, text: '', color: 'success' })

const dialog = ref(false)
const editIndex = ref(-1)
const blankRoute = () => ({
  called: '', exec: '', args: '', min_auth: 'identified',
  no_digi: false, quiet: false, crlf: false, idle_timeout: 0, env: '',
})
const editRoute = ref(blankRoute())
const minAuthOptions = ['none', 'identified']

const headers = [
  { title: 'Called SSID', key: 'called' },
  { title: 'Program', key: 'exec' },
  { title: 'Args', key: 'args' },
  { title: 'Auth', key: 'min_auth' },
  { title: 'Flags', key: 'flags', sortable: false },
  { title: 'Idle', key: 'idle_timeout' },
  { title: '', key: 'actions', sortable: false, align: 'end' },
]

function routesFromApi(routesObj) {
  return Object.entries(routesObj || {}).map(([called, r]) => ({
    called,
    exec: r.exec || '',
    args: (r.args || []).join(' '),
    min_auth: r.min_auth || 'identified',
    no_digi: !!r.no_digi,
    quiet: !!r.quiet,
    crlf: !!r.crlf,
    idle_timeout: r.idle_timeout || 0,
    env: Object.entries(r.env || {}).map(([k, v]) => `${k}=${v}`).join('\n'),
  }))
}

function flagsOf(r) {
  const f = []
  if (r.no_digi) f.push('no-digi')
  if (r.quiet) f.push('quiet')
  if (r.crlf) f.push('crlf')
  return f
}

async function load() {
  loading.value = true
  const res = await fetch('/api/services')
  if (res.ok) {
    const d = await res.json()
    enabled.value = !!d.enabled
    maxSessions.value = d.max_sessions ?? 10
    lockout.value = (d.lockout || []).join(', ')
    routes.value = routesFromApi(d.routes)
  }
  loading.value = false
}

function openAdd() { editIndex.value = -1; editRoute.value = blankRoute(); dialog.value = true }
function openEdit(i) { editIndex.value = i; editRoute.value = { ...routes.value[i] }; dialog.value = true }
function removeRoute(i) { routes.value.splice(i, 1) }

function saveDialog() {
  const r = editRoute.value
  if (!r.called.trim() || !r.exec.trim()) {
    snackbar.value = { show: true, text: 'Called SSID and program path are required.', color: 'error' }
    return
  }
  if (!r.exec.trim().startsWith('/')) {
    snackbar.value = { show: true, text: 'Program path must be absolute (start with /).', color: 'error' }
    return
  }
  const clean = { ...r, called: r.called.toUpperCase().trim() }
  if (editIndex.value === -1) routes.value.push(clean)
  else routes.value[editIndex.value] = clean
  dialog.value = false
}

function parseEnv(text) {
  const env = {}
  for (const ln of (text || '').split('\n')) {
    const t = ln.trim()
    if (!t) continue
    const eq = t.indexOf('=')
    if (eq > 0) env[t.slice(0, eq).trim()] = t.slice(eq + 1).trim()
  }
  return env
}

function toPayload() {
  const routesObj = {}
  for (const r of routes.value) {
    const route = {
      exec: r.exec.trim(),
      args: r.args.trim() ? r.args.trim().split(/\s+/) : [],
      min_auth: r.min_auth,
      no_digi: !!r.no_digi,
      quiet: !!r.quiet,
      crlf: !!r.crlf,
      idle_timeout: Number(r.idle_timeout) || 0,
    }
    const env = parseEnv(r.env)
    if (Object.keys(env).length) route.env = env
    routesObj[r.called.toUpperCase().trim()] = route
  }
  return {
    enabled: !!enabled.value,
    max_sessions: Number(maxSessions.value) || 10,
    lockout: lockout.value.split(/[,\s]+/).map(s => s.trim().toUpperCase()).filter(Boolean),
    routes: routesObj,
  }
}

async function save() {
  saving.value = true
  const res = await fetch('/api/services', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(toPayload()),
  })
  const data = await res.json().catch(() => ({}))
  if (res.ok) {
    snackbar.value = {
      show: true,
      color: data.restart_required ? 'warning' : 'success',
      text: data.restart_required
        ? 'Saved. New service SSIDs register with the radio on the next reconnect/restart.'
        : 'Services configuration saved.',
    }
  } else {
    snackbar.value = { show: true, color: 'error', text: data.error || 'Save failed.' }
  }
  saving.value = false
}

onMounted(load)
</script>

<template>
  <v-container fluid>
    <v-row align="center" class="mb-1">
      <v-col>
        <div class="text-h5 font-weight-bold">
          <v-icon class="mr-2">mdi-lan-connect</v-icon>External Services
        </div>
        <div class="text-caption text-medium-emphasis">
          Route callers to external programs by callsign-SSID (an <code>ax25d</code>
          replacement). A caller who connects to a mapped SSID is handed straight to
          the program's stdin/stdout — no BBS menu. Requires the AGWPE transport.
        </div>
      </v-col>
    </v-row>

    <v-card variant="outlined" class="mb-4">
      <v-card-text>
        <v-row align="center">
          <v-col cols="12" sm="4">
            <v-switch
              v-model="enabled"
              color="primary"
              label="Enable external-service dispatch"
              hide-details
              :loading="loading"
            />
          </v-col>
          <v-col cols="6" sm="3">
            <v-text-field
              v-model.number="maxSessions"
              type="number" min="1"
              label="Max concurrent sessions"
              variant="outlined" density="compact" hide-details
            />
          </v-col>
          <v-col cols="12" sm="5">
            <v-text-field
              v-model="lockout"
              label="Lockout callsigns"
              hint="Always refused (e.g. NOCALL, N0CALL). Comma/space separated."
              persistent-hint
              variant="outlined" density="compact"
            />
          </v-col>
        </v-row>
      </v-card-text>
    </v-card>

    <v-row align="center" class="mb-1">
      <v-col><div class="text-subtitle-1 font-weight-medium">Routes</div></v-col>
      <v-col class="text-right">
        <v-btn color="primary" variant="tonal" prepend-icon="mdi-plus" @click="openAdd">
          Add route
        </v-btn>
      </v-col>
    </v-row>

    <v-data-table
      :headers="headers"
      :items="routes"
      :loading="loading"
      density="compact"
      class="mb-4"
      no-data-text="No routes configured."
    >
      <template #item.exec="{ item }">
        <span class="font-monospace">{{ item.exec }}</span>
      </template>
      <template #item.args="{ item }">
        <span class="font-monospace text-medium-emphasis">{{ item.args }}</span>
      </template>
      <template #item.flags="{ item }">
        <v-chip v-for="f in flagsOf(item)" :key="f" size="x-small" class="mr-1" label>{{ f }}</v-chip>
      </template>
      <template #item.idle_timeout="{ item }">
        {{ item.idle_timeout ? item.idle_timeout + 's' : '—' }}
      </template>
      <template #item.actions="{ index }">
        <v-btn icon="mdi-pencil" size="small" variant="text" @click="openEdit(index)" />
        <v-btn icon="mdi-delete" size="small" variant="text" color="error" @click="removeRoute(index)" />
      </template>
    </v-data-table>

    <div class="d-flex justify-end">
      <v-btn color="primary" prepend-icon="mdi-content-save" :loading="saving" @click="save">
        Save configuration
      </v-btn>
    </div>

    <v-dialog v-model="dialog" max-width="640" scrollable>
      <v-card>
        <v-card-title class="d-flex align-center">
          <v-icon start>mdi-lan-connect</v-icon>
          {{ editIndex === -1 ? 'Add route' : 'Edit route' }}
          <v-spacer />
          <v-btn icon="mdi-close" variant="text" @click="dialog = false" />
        </v-card-title>
        <v-divider />
        <v-card-text class="pa-4">
          <v-text-field
            v-model="editRoute.called"
            label="Called callsign-SSID"
            hint="The SSID callers dial, e.g. W6ELA-2 (not the BBS's own callsign)."
            persistent-hint class="mb-2" variant="outlined" density="compact"
          />
          <v-text-field
            v-model="editRoute.exec"
            label="Program (absolute path)"
            hint="e.g. /usr/bin/xfbbd"
            persistent-hint class="mb-2 font-monospace" variant="outlined" density="compact"
          />
          <v-text-field
            v-model="editRoute.args"
            label="Arguments (argv, incl. argv[0])"
            hint="Whitespace-separated. %U/%u caller (no SSID), %S/%s (with SSID), %d port, %% literal %."
            persistent-hint class="mb-2 font-monospace" variant="outlined" density="compact"
          />
          <v-row>
            <v-col cols="6">
              <v-select
                v-model="editRoute.min_auth" :items="minAuthOptions"
                label="Minimum auth" variant="outlined" density="compact" hide-details
              />
            </v-col>
            <v-col cols="6">
              <v-text-field
                v-model.number="editRoute.idle_timeout" type="number" min="0"
                label="Idle timeout (s, 0 = off)" variant="outlined" density="compact" hide-details
              />
            </v-col>
          </v-row>
          <div class="mt-3">
            <v-switch v-model="editRoute.no_digi" color="primary" density="compact" hide-details
                      label="Refuse if arrived via a digipeater (no-digi)" />
            <v-switch v-model="editRoute.quiet" color="primary" density="compact" hide-details
                      label="Quiet — suppress connection logging" />
            <v-switch v-model="editRoute.crlf" color="primary" density="compact" hide-details
                      label="Translate line endings (Unix LF ↔ AX.25 CR)" />
            <div class="text-caption text-medium-emphasis mt-1">
              Enable line-ending translation for most line-oriented programs (anything
              reading with <code>readline</code>/<code>fgets</code>). Without it, the radio's
              bare CR never satisfies a newline read and the program hangs silently on input.
            </div>
          </div>
          <v-textarea
            v-model="editRoute.env"
            label="Environment (optional)"
            hint="One KEY=VALUE per line. Layered on top of a minimal PATH."
            persistent-hint rows="2" auto-grow
            class="mt-3 font-monospace" variant="outlined" density="compact"
          />
        </v-card-text>
        <v-divider />
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="dialog = false">Cancel</v-btn>
          <v-btn color="primary" variant="tonal" @click="saveDialog">
            {{ editIndex === -1 ? 'Add' : 'Update' }}
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-snackbar v-model="snackbar.show" :color="snackbar.color" timeout="4000">
      {{ snackbar.text }}
    </v-snackbar>
  </v-container>
</template>
