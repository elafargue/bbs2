<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useDisplay } from 'vuetify'
import socket from './socket.js'

const router = useRouter()
const { mobile } = useDisplay()
const isSysop = ref(false)
const drawer = ref(false)

const navItems = [
  { title: 'Dashboard',  icon: 'mdi-view-dashboard',   to: '/'         },
  { title: 'Users',      icon: 'mdi-account-group',    to: '/users'    },
  { title: 'Plugins',    icon: 'mdi-puzzle',           to: '/plugins'  },
  { title: 'Activity',   icon: 'mdi-text-box-outline', to: '/activity' },
  { title: 'Terminal',   icon: 'mdi-console',          to: '/terminal' },
]

onMounted(async () => {
  const res = await fetch('/api/admin/me')
  if (res.ok) {
    isSysop.value = true
    drawer.value = !mobile.value
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
  drawer.value = false
  router.push('/login')
}
</script>

<template>
  <v-app>
    <v-navigation-drawer v-if="isSysop" v-model="drawer">
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

    <v-app-bar v-if="isSysop" flat density="compact" color="surface">
      <v-app-bar-nav-icon @click="drawer = !drawer" />
      <v-app-bar-title>BBS2 Sysop</v-app-bar-title>
    </v-app-bar>

    <v-main>
      <router-view />
    </v-main>
  </v-app>
</template>
