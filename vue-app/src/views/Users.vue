<script setup>
import { ref, onMounted } from 'vue'
import QRCode from 'qrcode'

// ── State ─────────────────────────────────────────────────────────────────────
const users      = ref([])
const loading    = ref(false)
const pendingOnly = ref(false)
const snackbar   = ref({ show: false, text: '', color: 'success' })

// Create dialog
const createDialog = ref(false)
const createForm   = ref({ callsign: '', name: '', qth: '', approved: true })
const createError  = ref('')
const creating     = ref(false)

// Delete confirmation
const deleteDialog  = ref(false)
const deleteTarget  = ref(null)

// OTP enrollment dialog
const otpDialog    = ref(false)
const otpTarget    = ref(null)          // { id, callsign }
const otpType      = ref('totp')
const otpQrDataUrl = ref('')
const otpBase32    = ref('')
const otpUri       = ref('')
const enrolling    = ref(false)

const headers = [
  { title: 'Callsign',  key: 'callsign',   width: '120px' },
  { title: 'Name',      key: 'name'        },
  { title: 'QTH',       key: 'qth'         },
  { title: 'Status',    key: 'status',     sortable: false },
  { title: 'OTP',       key: 'has_secret', sortable: false },
  { title: 'Last Seen', key: 'last_seen'   },
  { title: 'Actions',   key: 'actions',    sortable: false, align: 'end' },
]

// ── Data loading ──────────────────────────────────────────────────────────────
async function loadUsers() {
  loading.value = true
  const url = pendingOnly.value ? '/api/users?pending=1' : '/api/users'
  const res = await fetch(url)
  if (res.ok) users.value = await res.json()
  loading.value = false
}

// ── Approve / Suspend ─────────────────────────────────────────────────────────
async function approve(id) {
  await patch(id, { approved: 1 })
}

async function toggleSuspend(user) {
  await patch(user.id, { banned: user.banned ? 0 : 1 })
}

async function patch(id, body) {
  const res = await fetch(`/api/users/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const d = await res.json()
    notify(d.error || 'Update failed', 'error')
  }
  await loadUsers()
}

// ── Delete ────────────────────────────────────────────────────────────────────
function confirmDelete(user) {
  deleteTarget.value = user
  deleteDialog.value = true
}

async function doDelete() {
  const res = await fetch(`/api/users/${deleteTarget.value.id}`, { method: 'DELETE' })
  deleteDialog.value = false
  notify(res.ok ? `${deleteTarget.value.callsign} deleted` : 'Delete failed', res.ok ? 'success' : 'error')
  await loadUsers()
}

// ── Create ────────────────────────────────────────────────────────────────────
function openCreate() {
  createForm.value = { callsign: '', name: '', qth: '', approved: true }
  createError.value = ''
  createDialog.value = true
}

async function doCreate() {
  createError.value = ''
  creating.value = true
  const res = await fetch('/api/users', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      ...createForm.value,
      callsign: createForm.value.callsign.toUpperCase().trim(),
    }),
  })
  const data = await res.json()
  creating.value = false
  if (!res.ok) {
    createError.value = data.error || 'Create failed'
    return
  }
  createDialog.value = false
  notify(`${data.callsign} created`, 'success')
  await loadUsers()
}

// ── OTP enrollment ────────────────────────────────────────────────────────────
function openOtp(user) {
  otpTarget.value   = user
  otpType.value     = 'totp'
  otpQrDataUrl.value = ''
  otpBase32.value   = ''
  otpUri.value      = ''
  otpDialog.value   = true
}

async function generateOtp() {
  enrolling.value = true
  const res = await fetch(`/api/users/${otpTarget.value.id}/secret`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: otpType.value }),
  })
  const data = await res.json()
  enrolling.value = false
  if (!res.ok) {
    notify(data.error || 'Enrollment failed', 'error')
    return
  }
  otpBase32.value = data.base32
  otpUri.value    = data.provisioning_uri
  otpQrDataUrl.value = await QRCode.toDataURL(data.provisioning_uri, {
    width: 280,
    margin: 2,
    color: { dark: '#000000', light: '#ffffff' },
  })
  await loadUsers()
}

async function clearOtp(id) {
  const res = await fetch(`/api/users/${id}/secret`, { method: 'DELETE' })
  notify(res.ok ? 'OTP secret cleared' : 'Clear failed', res.ok ? 'success' : 'error')
  await loadUsers()
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function formatTs(ts) {
  if (!ts) return 'Never'
  return new Date(ts * 1000).toLocaleString()
}

function notify(text, color = 'success') {
  snackbar.value = { show: true, text, color }
}

onMounted(loadUsers)
</script>

<template>
  <v-container fluid>

    <!-- ── Toolbar ─────────────────────────────────────────────────────────── -->
    <v-card>
      <v-card-title class="d-flex align-center gap-2 flex-wrap py-3">
        <v-icon start>mdi-account-group</v-icon>
        User Management
        <v-spacer />
        <v-switch
          v-model="pendingOnly"
          label="Pending only"
          density="compact"
          hide-details
          @change="loadUsers"
          class="mr-2"
        />
        <v-btn icon="mdi-refresh" variant="text" @click="loadUsers" />
        <v-btn
          color="primary"
          prepend-icon="mdi-account-plus"
          @click="openCreate"
        >
          New User
        </v-btn>
      </v-card-title>

      <!-- ── Table ───────────────────────────────────────────────────────── -->
      <v-data-table
        :headers="headers"
        :items="users"
        :loading="loading"
        density="compact"
        hover
      >
        <!-- Status chip -->
        <template #item.status="{ item }">
          <v-chip
            v-if="item.banned"
            size="small"
            color="error"
            prepend-icon="mdi-account-cancel"
          >Suspended</v-chip>
          <v-chip
            v-else-if="!item.approved"
            size="small"
            color="warning"
            prepend-icon="mdi-clock-outline"
          >Pending</v-chip>
          <v-chip
            v-else
            size="small"
            color="success"
            prepend-icon="mdi-check-circle-outline"
          >Active</v-chip>
        </template>

        <!-- OTP icon -->
        <template #item.has_secret="{ item }">
          <v-icon :color="item.has_secret ? 'success' : 'default'" size="small">
            {{ item.has_secret ? 'mdi-shield-key' : 'mdi-shield-off-outline' }}
          </v-icon>
        </template>

        <!-- Last seen -->
        <template #item.last_seen="{ item }">
          {{ formatTs(item.last_seen) }}
        </template>

        <!-- Actions -->
        <template #item.actions="{ item }">
          <!-- Approve (pending users only) -->
          <v-tooltip text="Approve" location="top">
            <template #activator="{ props }">
              <v-btn
                v-if="!item.approved && !item.banned"
                v-bind="props"
                size="small"
                color="success"
                variant="text"
                icon="mdi-check"
                @click="approve(item.id)"
              />
            </template>
          </v-tooltip>

          <!-- Suspend / Unsuspend -->
          <v-tooltip :text="item.banned ? 'Unsuspend' : 'Suspend'" location="top">
            <template #activator="{ props }">
              <v-btn
                v-bind="props"
                size="small"
                :color="item.banned ? 'success' : 'warning'"
                variant="text"
                :icon="item.banned ? 'mdi-account-check' : 'mdi-account-cancel'"
                @click="toggleSuspend(item)"
              />
            </template>
          </v-tooltip>

          <!-- OTP Enroll -->
          <v-tooltip text="OTP Enrollment" location="top">
            <template #activator="{ props }">
              <v-btn
                v-bind="props"
                size="small"
                color="primary"
                variant="text"
                icon="mdi-shield-key"
                @click="openOtp(item)"
              />
            </template>
          </v-tooltip>

          <!-- Clear OTP -->
          <v-tooltip text="Clear OTP secret" location="top">
            <template #activator="{ props }">
              <v-btn
                v-if="item.has_secret"
                v-bind="props"
                size="small"
                color="warning"
                variant="text"
                icon="mdi-shield-remove"
                @click="clearOtp(item.id)"
              />
            </template>
          </v-tooltip>

          <!-- Delete -->
          <v-tooltip text="Delete user" location="top">
            <template #activator="{ props }">
              <v-btn
                v-bind="props"
                size="small"
                color="error"
                variant="text"
                icon="mdi-delete"
                @click="confirmDelete(item)"
              />
            </template>
          </v-tooltip>
        </template>
      </v-data-table>
    </v-card>

    <!-- ── Create User dialog ─────────────────────────────────────────────── -->
    <v-dialog v-model="createDialog" max-width="480" persistent>
      <v-card>
        <v-card-title>
          <v-icon start>mdi-account-plus</v-icon>
          New User
        </v-card-title>
        <v-card-text>
          <v-alert v-if="createError" type="error" class="mb-3" density="compact">
            {{ createError }}
          </v-alert>
          <v-text-field
            v-model="createForm.callsign"
            label="Callsign *"
            variant="outlined"
            density="compact"
            class="mb-2"
            hint="e.g. W1ABC or N0CALL-1"
            :style="{ textTransform: 'uppercase' }"
            @input="createForm.callsign = createForm.callsign.toUpperCase()"
          />
          <v-text-field
            v-model="createForm.name"
            label="Name"
            variant="outlined"
            density="compact"
            class="mb-2"
          />
          <v-text-field
            v-model="createForm.qth"
            label="QTH / Location"
            variant="outlined"
            density="compact"
            class="mb-2"
          />
          <v-switch
            v-model="createForm.approved"
            label="Approve immediately"
            density="compact"
            hide-details
          />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn @click="createDialog = false">Cancel</v-btn>
          <v-btn
            color="primary"
            :loading="creating"
            @click="doCreate"
          >Create</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- ── Delete confirmation ─────────────────────────────────────────────── -->
    <v-dialog v-model="deleteDialog" max-width="420">
      <v-card>
        <v-card-title>Delete {{ deleteTarget?.callsign }}?</v-card-title>
        <v-card-text>
          This will permanently remove the user and all associated data.
          This cannot be undone.
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn @click="deleteDialog = false">Cancel</v-btn>
          <v-btn color="error" @click="doDelete">Delete</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- ── OTP Enrollment dialog ──────────────────────────────────────────── -->
    <v-dialog v-model="otpDialog" max-width="520">
      <v-card>
        <v-card-title>
          <v-icon start>mdi-shield-key</v-icon>
          OTP Enrollment — {{ otpTarget?.callsign }}
        </v-card-title>
        <v-card-text>
          <!-- Step 1: choose type and generate -->
          <template v-if="!otpQrDataUrl">
            <p class="text-body-2 mb-4">
              Choose the OTP algorithm, then generate a secret. Share the QR
              code with the user out-of-band so they can enroll their
              authenticator app (Google Authenticator, Aegis, etc.).
            </p>
            <v-btn-toggle v-model="otpType" mandatory density="compact" class="mb-4">
              <v-btn value="totp">
                <v-icon start>mdi-clock-outline</v-icon>
                TOTP (time-based)
              </v-btn>
              <v-btn value="hotp">
                <v-icon start>mdi-counter</v-icon>
                HOTP (counter)
              </v-btn>
            </v-btn-toggle>
            <p class="text-caption text-medium-emphasis">
              <strong>TOTP</strong> is recommended — works with any standard
              authenticator app; codes change every 30 seconds.<br>
              <strong>HOTP</strong> is counter-based; codes are only generated
              on demand (useful for hardware tokens).
            </p>
          </template>

          <!-- Step 2: show QR code -->
          <template v-else>
            <p class="text-body-2 mb-3">
              Have the user scan this QR code with their authenticator app.
              The secret will <strong>not</strong> be shown again after this
              dialog is closed.
            </p>
            <div class="d-flex justify-center mb-4">
              <v-sheet
                rounded
                border
                class="pa-2"
                style="display: inline-block; background: white;"
              >
                <img :src="otpQrDataUrl" alt="OTP QR code" width="280" height="280" />
              </v-sheet>
            </div>
            <v-text-field
              :model-value="otpBase32"
              label="Manual entry key (base32)"
              variant="outlined"
              density="compact"
              readonly
              append-inner-icon="mdi-content-copy"
              hint="Use this if the user cannot scan the QR code"
              @click:append-inner="() => navigator.clipboard.writeText(otpBase32)"
            />
          </template>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn @click="otpDialog = false">
            {{ otpQrDataUrl ? 'Done' : 'Cancel' }}
          </v-btn>
          <v-btn
            v-if="!otpQrDataUrl"
            color="primary"
            :loading="enrolling"
            prepend-icon="mdi-qrcode"
            @click="generateOtp"
          >
            Generate &amp; Show QR
          </v-btn>
          <v-btn
            v-else
            color="secondary"
            prepend-icon="mdi-refresh"
            @click="otpQrDataUrl = ''"
          >
            Regenerate
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- ── Snackbar ───────────────────────────────────────────────────────── -->
    <v-snackbar v-model="snackbar.show" :color="snackbar.color" timeout="4000">
      {{ snackbar.text }}
    </v-snackbar>

  </v-container>
</template>


async function loadUsers() {
  loading.value = true
  const url = pendingOnly.value ? '/api/users?pending=1' : '/api/users'
  const res = await fetch(url)
  if (res.ok) users.value = await res.json()
  loading.value = false
}

async function approve(id) {
  await fetch(`/api/users/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ approved: 1 }),
  })
  await loadUsers()
}

async function toggleBan(user) {
  await fetch(`/api/users/${user.id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ banned: user.banned ? 0 : 1 }),
  })
  await loadUsers()
}

function openSecretDialog(user) {
  secretUserId.value = user.id
  secretCallsign.value = user.callsign
  secretValue.value = ''
  secretDialog.value = true
}


