<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import socket from './socket.js'

const router = useRouter()
const isSysop = ref(false)
const drawer = ref(true)

const navItems = [
  { title: 'Dashboard',  icon: 'mdi-view-dashboard',   to: '/'        },
  { title: 'Users',      icon: 'mdi-account-group',    to: '/users'   },
  { title: 'Plugins',    icon: 'mdi-puzzle',           to: '/plugins' },
  { title: 'Activity',   icon: 'mdi-text-box-outline', to: '/activity'},
]

onMounted(async () => {
  const res = await fetch('/api/admin/me')
  if (res.ok) {
    isSysop.value = true
    socket.connect()
    socket.emit('join_admin', {})
  } else {
    router.push('/login')
  }
})

async function logout() {
  await fetch('/api/admin/logout', { method: 'POST' })
  socket.disconnect()
  isSysop.value = false
  router.push('/login')
}
</script>

<template>
  <v-app>
    <v-navigation-drawer v-if="isSysop" v-model="drawer" permanent>
      <v-list-item
        prepend-icon="mdi-radio-tower"
        title="BBS2 Sysop"
        subtitle="Ham Radio BBS"
        nav
      />
      <v-divider />
      <v-list density="compact" nav>
        <v-list-item
          v-for="item in navItems"
          :key="item.to"
          :prepend-icon="item.icon"
          :title="item.title"
          :to="item.to"
          exact
        />
      </v-list>
      <template #append>
        <v-divider />
        <v-list density="compact" nav>
          <v-list-item
            prepend-icon="mdi-logout"
            title="Logout"
            @click="logout"
          />
        </v-list>
      </template>
    </v-navigation-drawer>

    <v-main>
      <router-view />
    </v-main>
  </v-app>
</template>
