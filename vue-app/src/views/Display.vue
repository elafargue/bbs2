<script setup>
import { ref, onMounted, onUnmounted, computed } from 'vue'

// ── State ─────────────────────────────────────────────────────────────────────

const settings = ref({})
const status   = ref(null)
const loading  = ref(false)
const saving   = ref(false)
const waking   = ref(false)
const snackbar = ref({ show: false, text: '', color: 'success' })

const snapshotTs = ref(Date.now())
let snapshotTimer = null

// Editable copy of settings (strings kept as strings for form binding)
const form = ref({
  fb_device:          '/dev/fb0',
  width:              '480',
  height:             '320',
  refresh_interval:   '1.0',
  idle_dim_minutes:   '5',
  idle_off_minutes:   '30',
  dim_level:          '20',
  backlight_path:     '',
  backlight_max:      '255',
  font_path:          '',
  bulletin_new_hours: '24',
  max_heard_scroll:   '20',
})

// ── Computed helpers ──────────────────────────────────────────────────────────

const displayOnline = computed(() => status.value && !status.value.is_off)
const displayDimmed = computed(() => status.value && status.value.is_dimmed && !status.value.is_off)

function fmtIdleAgo(secs) {
  if (secs === undefined || secs === null) return '—'
  if (secs < 60)   return `${secs}s ago`
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`
  return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m ago`
}

function fmtTs(unix) {
  if (!unix) return '—'
  return new Date(unix * 1000).toLocaleString()
}

// ── API calls ─────────────────────────────────────────────────────────────────

async function load() {
  loading.value = true
  const [sRes, stRes] = await Promise.all([
    fetch('/api/display/settings'),
    fetch('/api/display/status'),
  ])
  if (sRes.ok) {
    settings.value = await sRes.json()
    Object.assign(form.value, settings.value)
  }
  if (stRes.ok) {
    status.value = await stRes.json()
  }
  loading.value = false
}

async function save() {
  saving.value = true
  const res  = await fetch('/api/display/settings', {
    method:  'PUT',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(form.value),
  })
  const data = await res.json()
  snackbar.value = {
    show:  true,
    text:  res.ok ? 'Settings saved.' : (data.error ?? 'Save failed.'),
    color: res.ok ? 'success' : 'error',
  }
  saving.value = false
  if (res.ok) await load()
}

async function wake() {
  waking.value = true
  const res  = await fetch('/api/display/wake', { method: 'POST' })
  const data = await res.json()
  snackbar.value = {
    show:  true,
    text:  res.ok ? 'Display woken.' : (data.error ?? 'Wake failed.'),
    color: res.ok ? 'success' : 'warning',
  }
  waking.value = false
  if (res.ok) await load()
}

onMounted(() => {
  load()
  snapshotTimer = setInterval(() => { snapshotTs.value = Date.now() }, 5000)
})

onUnmounted(() => {
  if (snapshotTimer) clearInterval(snapshotTimer)
})
</script>

<template>
  <v-container fluid>

    <!-- Page title + actions -->
    <v-row align="center" class="mb-2">
      <v-col>
        <div class="text-h5 font-weight-bold">
          <v-icon class="mr-2">mdi-monitor</v-icon>Framebuffer Display
        </div>
        <div class="text-caption text-medium-emphasis">
          480×320 status screen on /dev/fb0
        </div>
      </v-col>
      <v-col cols="auto">
        <v-btn
          prepend-icon="mdi-refresh"
          variant="text"
          :loading="loading"
          @click="load"
        >Refresh</v-btn>
        <v-btn
          prepend-icon="mdi-brightness-5"
          variant="outlined"
          class="ml-2"
          :loading="waking"
          :disabled="displayOnline && !displayDimmed"
          @click="wake"
        >Wake</v-btn>
      </v-col>
    </v-row>

    <!-- Live preview + status in one row -->
    <v-row class="mb-4" align="start">
      <!-- Display state (narrow column) -->
      <v-col v-if="status" cols="12" sm="auto">
        <v-card variant="outlined" height="100%">
          <v-card-text class="text-center pa-3">
            <v-icon
              size="36"
              :color="status.is_off ? 'grey' : status.is_dimmed ? 'orange' : 'green'"
            >
              {{ status.is_off ? 'mdi-monitor-off' : 'mdi-monitor' }}
            </v-icon>
            <div class="text-subtitle-2 mt-1">
              <span v-if="status.is_off">OFF</span>
              <span v-else-if="status.is_dimmed">DIMMED</span>
              <span v-else class="text-success">ACTIVE</span>
            </div>
            <div class="text-caption text-medium-emphasis">
              Last activity: {{ fmtIdleAgo(status.last_activity_ago) }}
            </div>
          </v-card-text>
        </v-card>
      </v-col>

      <!-- Live preview -->
      <v-col cols="12" sm="auto">
        <v-card variant="outlined">
          <v-card-title class="text-subtitle-2 pb-0 d-flex align-center">
            <v-icon size="small" class="mr-1">mdi-television-play</v-icon>
            Live Preview
            <span class="text-caption text-medium-emphasis ml-2">(auto-refreshes every 5s)</span>
            <v-btn
              icon="mdi-refresh"
              size="x-small"
              variant="text"
              class="ml-auto"
              @click="snapshotTs = Date.now()"
            />
          </v-card-title>
          <v-card-text class="pa-2">
            <img
              :src="`/api/display/snapshot.png?t=${snapshotTs}`"
              alt="Display snapshot"
              style="display:block; image-rendering:pixelated; border:1px solid rgba(255,255,255,0.1); border-radius:4px;"
            />
          </v-card-text>
        </v-card>
      </v-col>
    </v-row>

    <!-- Settings form -->
    <v-card>
      <v-card-title class="text-subtitle-1">Settings</v-card-title>
      <v-divider />
      <v-card-text>
        <v-row>
          <!-- Hardware -->
          <v-col cols="12">
            <div class="text-overline text-medium-emphasis mb-1">Hardware</div>
          </v-col>
          <v-col cols="12" sm="4">
            <v-text-field
              v-model="form.fb_device"
              label="Framebuffer device"
              hint="/dev/fb0"
              persistent-hint
              density="compact"
              variant="outlined"
            />
          </v-col>
          <v-col cols="6" sm="2">
            <v-text-field
              v-model="form.width"
              label="Width (px)"
              density="compact"
              variant="outlined"
              type="number"
            />
          </v-col>
          <v-col cols="6" sm="2">
            <v-text-field
              v-model="form.height"
              label="Height (px)"
              density="compact"
              variant="outlined"
              type="number"
            />
          </v-col>
          <v-col cols="12" sm="4">
            <v-text-field
              v-model="form.backlight_path"
              label="Backlight sysfs path (optional)"
              hint="/sys/class/backlight/soc:backlight/brightness"
              persistent-hint
              density="compact"
              variant="outlined"
            />
          </v-col>
          <v-col cols="6" sm="2">
            <v-text-field
              v-model="form.backlight_max"
              label="Backlight max"
              density="compact"
              variant="outlined"
              type="number"
            />
          </v-col>

          <!-- Rendering -->
          <v-col cols="12">
            <v-divider class="my-2" />
            <div class="text-overline text-medium-emphasis mb-1">Rendering</div>
          </v-col>
          <v-col cols="6" sm="3">
            <v-text-field
              v-model="form.refresh_interval"
              label="Refresh interval (s)"
              density="compact"
              variant="outlined"
              type="number"
              step="0.5"
              min="0.2"
            />
          </v-col>
          <v-col cols="12" sm="5">
            <v-text-field
              v-model="form.font_path"
              label="Font path (empty = auto-detect)"
              hint="Leave empty to use Terminus/DejaVu from system fonts"
              persistent-hint
              density="compact"
              variant="outlined"
            />
          </v-col>
          <v-col cols="6" sm="4">
            <v-text-field
              v-model="form.bulletin_new_hours"
              label="'New' bulletin window (hours)"
              density="compact"
              variant="outlined"
              type="number"
              min="1"
            />
          </v-col>
          <v-col cols="6" sm="3">
            <v-text-field
              v-model="form.max_heard_scroll"
              label="Heard scroll buffer"
              density="compact"
              variant="outlined"
              type="number"
              min="5"
              max="100"
            />
          </v-col>

          <!-- Idle / power saving -->
          <v-col cols="12">
            <v-divider class="my-2" />
            <div class="text-overline text-medium-emphasis mb-1">
              Idle &amp; Power saving
              <span class="text-caption font-weight-regular ml-2">
                (0 = disabled)
              </span>
            </div>
          </v-col>
          <v-col cols="6" sm="3">
            <v-text-field
              v-model="form.idle_dim_minutes"
              label="Dim after (minutes)"
              density="compact"
              variant="outlined"
              type="number"
              min="0"
            />
          </v-col>
          <v-col cols="6" sm="3">
            <v-slider
              v-model="form.dim_level"
              label="Dim level %"
              min="1"
              max="80"
              step="1"
              thumb-label
              density="compact"
            />
          </v-col>
          <v-col cols="6" sm="3">
            <v-text-field
              v-model="form.idle_off_minutes"
              label="Screen off after (minutes)"
              density="compact"
              variant="outlined"
              type="number"
              min="0"
            />
          </v-col>
        </v-row>
      </v-card-text>
      <v-divider />
      <v-card-actions>
        <v-spacer />
        <v-btn
          prepend-icon="mdi-content-save"
          color="primary"
          variant="elevated"
          :loading="saving"
          @click="save"
        >Save settings</v-btn>
      </v-card-actions>
    </v-card>

    <v-snackbar v-model="snackbar.show" :color="snackbar.color" timeout="3000">
      {{ snackbar.text }}
    </v-snackbar>

  </v-container>
</template>
