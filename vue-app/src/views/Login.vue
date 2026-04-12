<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import socket from '../socket.js'

const router = useRouter()
const password = ref('')
const error = ref('')
const loading = ref(false)
const showPass = ref(false)

async function login() {
  error.value = ''
  loading.value = true
  try {
    const res = await fetch('/api/admin/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: password.value }),
    })
    if (res.ok) {
      socket.connect()
      socket.emit('join_admin', {})
      router.push('/')
    } else {
      const data = await res.json()
      error.value = data.error || 'Login failed'
    }
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <v-container fluid class="fill-height">
    <v-row justify="center" align="center">
      <v-col cols="12" sm="6" md="4">
        <v-card>
          <v-card-title class="text-center pa-4">
            <v-icon size="48" color="primary">mdi-radio-tower</v-icon>
            <div class="mt-2">BBS2 Sysop Login</div>
          </v-card-title>
          <v-card-text>
            <v-alert v-if="error" type="error" class="mb-4">{{ error }}</v-alert>
            <v-text-field
              v-model="password"
              label="Sysop password"
              :type="showPass ? 'text' : 'password'"
              :append-inner-icon="showPass ? 'mdi-eye-off' : 'mdi-eye'"
              variant="outlined"
              @click:append-inner="showPass = !showPass"
              @keyup.enter="login"
            />
          </v-card-text>
          <v-card-actions class="px-4 pb-4">
            <v-btn
              block
              color="primary"
              :loading="loading"
              @click="login"
            >Login</v-btn>
          </v-card-actions>
        </v-card>
      </v-col>
    </v-row>
  </v-container>
</template>
