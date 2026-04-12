<script setup>
import { ref, onMounted } from 'vue'

// ── State ─────────────────────────────────────────────────────────────────────
const areas    = ref([])
const loading  = ref(false)
const snackbar = ref({ show: false, text: '', color: 'success' })

// Create dialog
const createDialog = ref(false)
const createForm   = ref({ name: '', description: '', is_default: false })
const createError  = ref('')
const creating     = ref(false)

// Edit dialog
const editDialog = ref(false)
const editForm   = ref({ id: null, name: '', description: '', is_default: false })
const editError  = ref('')
const saving     = ref(false)

// Delete dialog
const deleteDialog = ref(false)
const deleteTarget = ref(null)

const headers = [
  { title: 'Name',        key: 'name',          width: '140px' },
  { title: 'Description', key: 'description'    },
  { title: 'Messages',    key: 'message_count', width: '110px', align: 'end' },
  { title: 'Default',     key: 'is_default',    width: '100px', sortable: false },
  { title: 'Actions',     key: 'actions',       width: '120px', sortable: false, align: 'end' },
]

// ── Load ──────────────────────────────────────────────────────────────────────
async function loadAreas() {
  loading.value = true
  const res = await fetch('/api/bulletins/areas')
  if (res.ok) areas.value = await res.json()
  loading.value = false
}

// ── Create ────────────────────────────────────────────────────────────────────
function openCreate() {
  createForm.value = { name: '', description: '', is_default: false }
  createError.value = ''
  createDialog.value = true
}

async function doCreate() {
  createError.value = ''
  creating.value = true
  const res = await fetch('/api/bulletins/areas', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      ...createForm.value,
      name: createForm.value.name.toUpperCase().trim(),
    }),
  })
  creating.value = false
  if (!res.ok) {
    const d = await res.json()
    createError.value = d.error || 'Create failed'
    return
  }
  createDialog.value = false
  notify('Area created')
  await loadAreas()
}

// ── Edit ──────────────────────────────────────────────────────────────────────
function openEdit(area) {
  editForm.value = { id: area.id, name: area.name, description: area.description, is_default: !!area.is_default }
  editError.value = ''
  editDialog.value = true
}

async function doSave() {
  editError.value = ''
  saving.value = true
  const { id, ...body } = editForm.value
  body.name = body.name.toUpperCase().trim()
  const res = await fetch(`/api/bulletins/areas/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  saving.value = false
  if (!res.ok) {
    const d = await res.json()
    editError.value = d.error || 'Save failed'
    return
  }
  editDialog.value = false
  notify('Area updated')
  await loadAreas()
}

// ── Set / clear default ───────────────────────────────────────────────────────
async function toggleDefault(area) {
  const res = await fetch(`/api/bulletins/areas/${area.id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ is_default: area.is_default ? 0 : 1 }),
  })
  if (!res.ok) { notify('Update failed', 'error'); return }
  notify(area.is_default ? 'Default cleared' : `${area.name} set as default`)
  await loadAreas()
}

// ── Delete ────────────────────────────────────────────────────────────────────
function confirmDelete(area) {
  deleteTarget.value = area
  deleteDialog.value = true
}

async function doDelete() {
  const res = await fetch(`/api/bulletins/areas/${deleteTarget.value.id}`, { method: 'DELETE' })
  deleteDialog.value = false
  notify(res.ok ? `${deleteTarget.value.name} deleted` : 'Delete failed', res.ok ? 'success' : 'error')
  await loadAreas()
}

// ── Notify ────────────────────────────────────────────────────────────────────
function notify(text, color = 'success') {
  snackbar.value = { show: true, text, color }
}

onMounted(loadAreas)
</script>

<template>
  <div>
    <v-row class="mb-2" align="center">
      <v-col><h2 class="text-h5">Bulletin Areas</h2></v-col>
      <v-col cols="auto">
        <v-btn color="primary" prepend-icon="mdi-plus" @click="openCreate">New Area</v-btn>
      </v-col>
    </v-row>

    <v-data-table
      :headers="headers"
      :items="areas"
      :loading="loading"
      density="compact"
      hover
    >
      <template #item.is_default="{ item }">
        <v-tooltip :text="item.is_default ? 'Default area — click to clear' : 'Set as default'">
          <template #activator="{ props }">
            <v-btn
              v-bind="props"
              :icon="item.is_default ? 'mdi-star' : 'mdi-star-outline'"
              :color="item.is_default ? 'amber' : 'grey'"
              variant="text"
              size="small"
              @click="toggleDefault(item)"
            />
          </template>
        </v-tooltip>
      </template>

      <template #item.actions="{ item }">
        <v-tooltip text="Edit">
          <template #activator="{ props }">
            <v-btn v-bind="props" icon="mdi-pencil" variant="text" size="small" @click="openEdit(item)" />
          </template>
        </v-tooltip>
        <v-tooltip text="Delete area and all messages">
          <template #activator="{ props }">
            <v-btn v-bind="props" icon="mdi-delete" variant="text" size="small" color="error" @click="confirmDelete(item)" />
          </template>
        </v-tooltip>
      </template>
    </v-data-table>

    <!-- ── Create dialog ── -->
    <v-dialog v-model="createDialog" max-width="460">
      <v-card title="New Bulletin Area">
        <v-card-text>
          <v-alert v-if="createError" type="error" density="compact" class="mb-3">{{ createError }}</v-alert>
          <v-text-field
            v-model="createForm.name"
            label="Name (e.g. DX)"
            :rules="[v => !!v || 'Required']"
            maxlength="20"
            @input="createForm.name = createForm.name.toUpperCase()"
          />
          <v-text-field v-model="createForm.description" label="Description" maxlength="120" />
          <v-switch v-model="createForm.is_default" label="Set as default area" color="primary" />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn @click="createDialog = false">Cancel</v-btn>
          <v-btn color="primary" :loading="creating" @click="doCreate">Create</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- ── Edit dialog ── -->
    <v-dialog v-model="editDialog" max-width="460">
      <v-card title="Edit Bulletin Area">
        <v-card-text>
          <v-alert v-if="editError" type="error" density="compact" class="mb-3">{{ editError }}</v-alert>
          <v-text-field
            v-model="editForm.name"
            label="Name"
            maxlength="20"
            @input="editForm.name = editForm.name.toUpperCase()"
          />
          <v-text-field v-model="editForm.description" label="Description" maxlength="120" />
          <v-switch v-model="editForm.is_default" label="Default area" color="primary" />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn @click="editDialog = false">Cancel</v-btn>
          <v-btn color="primary" :loading="saving" @click="doSave">Save</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- ── Delete confirm ── -->
    <v-dialog v-model="deleteDialog" max-width="420">
      <v-card title="Delete Area?">
        <v-card-text>
          <strong>{{ deleteTarget?.name }}</strong> and all its messages will be permanently deleted.
          This cannot be undone.
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn @click="deleteDialog = false">Cancel</v-btn>
          <v-btn color="error" @click="doDelete">Delete</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-snackbar v-model="snackbar.show" :color="snackbar.color" timeout="3000">
      {{ snackbar.text }}
    </v-snackbar>
  </div>
</template>
