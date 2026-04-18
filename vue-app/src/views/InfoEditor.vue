<script setup>
import { ref, onMounted } from 'vue'

const message = ref('')
const loading = ref(false)
const saving = ref(false)
const snackbar = ref({ show: false, text: '', color: 'success' })

async function load() {
  loading.value = true
  const res = await fetch('/api/info')
  if (res.ok) {
    const data = await res.json()
    message.value = data.message ?? ''
  }
  loading.value = false
}

async function save() {
  saving.value = true
  const res = await fetch('/api/info', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: message.value }),
  })
  const data = await res.json()
  snackbar.value = {
    show: true,
    text: res.ok ? 'Info message saved.' : (data.error ?? 'Save failed.'),
    color: res.ok ? 'success' : 'error',
  }
  saving.value = false
}

onMounted(load)
</script>

<template>
  <v-container fluid class="pa-0">
    <v-textarea
      v-model="message"
      label="BBS Info message"
      hint="Displayed to users when they type 'I' at the main menu. Plain text; one line per row."
      persistent-hint
      rows="12"
      auto-grow
      :loading="loading"
      :disabled="loading"
      variant="outlined"
      class="mb-4 font-monospace"
      style="font-family: monospace;"
    />
    <div class="d-flex justify-end">
      <v-btn
        color="primary"
        variant="tonal"
        prepend-icon="mdi-content-save"
        :loading="saving"
        @click="save"
      >
        Save
      </v-btn>
    </div>
    <v-snackbar v-model="snackbar.show" :color="snackbar.color" timeout="3000">
      {{ snackbar.text }}
    </v-snackbar>
  </v-container>
</template>
