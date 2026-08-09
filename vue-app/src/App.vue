<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
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
  { title: 'Services',   icon: 'mdi-lan-connect',      to: '/services' },
  { title: 'Activity',   icon: 'mdi-text-box-outline', to: '/activity' },
  { title: 'Terminal',   icon: 'mdi-console',          to: '/terminal' },
]

const pluginNavMap = {
  display:   { title: 'Display',   icon: 'mdi-monitor',             to: '/display'   },
  bulletins: { title: 'Bulletins', icon: 'mdi-bulletin-board',      to: '/bulletins' },
  chat:      { title: 'Chat',      icon: 'mdi-forum',               to: '/chat'      },
  heard:     { title: 'Heard',     icon: 'mdi-ear-hearing',         to: '/heard'     },
  info:      { title: 'Info',      icon: 'mdi-information-outline', to: '/info'      },
}

const pluginList = ref([])
const pluginNavItems = computed(() =>
  pluginList.value
    .filter(p => p.enabled && pluginNavMap[p.name])
    .map(p => pluginNavMap[p.name])
)

async function loadPluginNav() {
  const res = await fetch('/api/plugins')
  if (res.ok) pluginList.value = await res.json()
}

// Enter the authenticated state: reveal the app-bar + drawer and wire up the
// live feeds.  Called both when a fresh page load finds a valid session and
// when Login.vue signals a successful login (App.vue does not re-mount on the
// in-app navigation from /login → /, so without the event the chrome would
// stay hidden until a manual page reload).
async function enterAuthenticated() {
  isSysop.value = true
  drawer.value = !mobile.value
  socket.connect()
  socket.emit('join_admin', {})
  await loadPluginNav()
}

onMounted(async () => {
  window.addEventListener('plugins-updated', loadPluginNav)
  window.addEventListener('admin-authenticated', enterAuthenticated)
  const res = await fetch('/api/admin/me')
  if (res.ok) {
    await enterAuthenticated()
  } else {
    router.push('/login')
  }
})

onUnmounted(() => {
  window.removeEventListener('plugins-updated', loadPluginNav)
  window.removeEventListener('admin-authenticated', enterAuthenticated)
})

async function logout() {
  await fetch('/api/admin/logout', { method: 'POST' })
  socket.disconnect()
  isSysop.value = false
  pluginList.value = []
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
      <template v-if="pluginNavItems.length">
        <v-divider class="mt-1 mb-1" />
        <v-list density="compact" nav>
          <v-list-item
            v-for="item in pluginNavItems"
            :key="item.to"
            :prepend-icon="item.icon"
            :title="item.title"
            :to="item.to"
            exact
          />
        </v-list>
      </template>
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
