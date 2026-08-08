<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import socket from '../socket.js'

const bbsCallsign = ref('')
const users = ref([])
const plugins = ref([])
const node = ref({ enabled: false })   // NET/ROM node live activity (polled)
let nodeTimer = null

const userHeaders = [
  { title: 'Callsign',   key: 'callsign'    },
  { title: 'Transport',  key: 'transport'   },
  { title: 'Auth Level', key: 'auth_level'  },
  { title: 'Idle (s)',   key: 'idle_seconds'},
]

const nodeSessionHeaders = [
  { title: 'User',         key: 'user'        },
  { title: 'Entry',        key: 'entry'       },
  { title: 'Via',          key: 'via'         },
  { title: 'Connected to', key: 'target'      },
  { title: 'Up',           key: 'connected_s' },
  { title: 'Idle',         key: 'idle_s'      },
]

const circuitHeaders = [
  { title: 'User',    key: 'user'           },
  { title: 'Dest',    key: 'dest'           },
  { title: 'Via',     key: 'via'            },
  { title: 'State',   key: 'state'          },
  { title: 'Circuit', key: 'local_circuit'  },
]
const circuits = computed(() => node.value.circuits || [])

const gwColor = computed(() => {
  const g = node.value.gateway
  if (!g) return undefined
  return g.active >= g.max ? 'warning' : 'success'
})
const recentRefusals = computed(() =>
  (node.value.gateway?.recent_refusals || []).slice().reverse().slice(0, 10)
)
function fmtTime(ts) {
  try { return new Date(ts * 1000).toLocaleTimeString() } catch { return '' }
}

async function loadNode() {
  try {
    const res = await fetch('/api/netrom/activity')
    if (res.ok) node.value = await res.json()
  } catch { /* transient — keep last snapshot */ }
}

onMounted(async () => {
  // Fetch initial state via REST so navigating back always shows fresh data.
  // (admin_dashboard_init only fires once on initial page load, not on re-navigation.)
  const [usersRes, pluginsRes, meRes] = await Promise.all([
    fetch('/api/activity/users'),
    fetch('/api/plugins'),
    fetch('/api/admin/me'),
  ])
  if (usersRes.ok)  users.value    = await usersRes.json()
  if (pluginsRes.ok) plugins.value = await pluginsRes.json()
  if (meRes.ok)   { const d = await meRes.json(); bbsCallsign.value = d.callsign || '' }

  // Socket listeners keep data live after the initial fetch.
  // admin_dashboard_init still fires on socket reconnects, so handle it too.
  socket.on('admin_dashboard_init', (data) => {
    if (data.bbs_callsign) bbsCallsign.value = data.bbs_callsign
    if (data.users)   users.value    = data.users
    if (data.plugins) plugins.value  = data.plugins
  })
  socket.on('users_snapshot',      (snap)  => { users.value   = snap  })
  socket.on('plugin_stats_update', (stats) => { plugins.value = stats })

  // NET/ROM node activity has no socket feed yet — poll it every 5 s.
  await loadNode()
  nodeTimer = setInterval(loadNode, 5000)
})

onUnmounted(() => {
  socket.off('admin_dashboard_init')
  socket.off('users_snapshot')
  socket.off('plugin_stats_update')
  if (nodeTimer) clearInterval(nodeTimer)
})
</script>

<template>
  <v-container fluid>
    <v-row>
      <v-col cols="12">
        <v-card>
          <v-card-title>
            <v-icon start>mdi-radio-tower</v-icon>
            {{ bbsCallsign || 'BBS' }} — Dashboard
          </v-card-title>
        </v-card>
      </v-col>
    </v-row>

    <!-- Connected users -->
    <v-row>
      <v-col cols="12" md="7">
        <v-card>
          <v-card-title>
            <v-icon start>mdi-account-multiple</v-icon>
            Connected Users ({{ users.length }})
          </v-card-title>
          <v-data-table
            :headers="userHeaders"
            :items="users"
            :items-per-page="10"
            density="compact"
          />
        </v-card>
      </v-col>

      <!-- Plugin health -->
      <v-col cols="12" md="5">
        <v-card>
          <v-card-title>
            <v-icon start>mdi-puzzle</v-icon>
            Plugins
          </v-card-title>
          <v-list density="compact">
            <v-list-item
              v-for="p in plugins"
              :key="p.name"
              :subtitle="p.name"
            >
              <template #title>
                <span>{{ p.display_name || p.name }}</span>
              </template>
              <template #append>
                <v-chip
                  size="small"
                  :color="p.enabled ? 'success' : 'error'"
                >{{ p.enabled ? 'ON' : 'OFF' }}</v-chip>
              </template>
            </v-list-item>
          </v-list>
        </v-card>
      </v-col>
    </v-row>

    <!-- NET/ROM node: live sessions + gateway-safety state -->
    <v-row v-if="node.enabled">
      <v-col cols="12">
        <v-card>
          <v-card-title>
            <v-icon start>mdi-transit-connection-variant</v-icon>
            NET/ROM Node — {{ node.node_alias }}:{{ node.node_call }}
          </v-card-title>
          <v-card-text>
            <!-- gateway-safety strip -->
            <div class="d-flex flex-wrap align-center mb-3" style="gap: 8px;">
              <v-chip label size="small" :color="gwColor">
                Circuits {{ node.gateway.active }}/{{ node.gateway.max }}
                <span class="text-medium-emphasis">&nbsp;· {{ node.gateway.max_per_user }}/user</span>
              </v-chip>
              <v-chip label size="small" variant="outlined">
                min auth: {{ node.gateway.policy.min_auth }}
              </v-chip>
              <v-chip
                label size="small"
                :variant="node.gateway.policy.interlock ? 'flat' : 'outlined'"
                :color="node.gateway.policy.interlock ? 'success' : undefined"
              >
                INTERLOCK {{ node.gateway.policy.interlock ? 'on' : 'off' }}
              </v-chip>
              <v-chip label size="small" variant="outlined">
                rate {{ node.gateway.policy.rate_limit_per_min || '∞' }}/min
              </v-chip>
              <v-chip
                v-if="node.gateway.policy.allow.length"
                label size="small" variant="outlined" color="warning"
              >allow-list ({{ node.gateway.policy.allow.length }})</v-chip>
              <v-chip
                v-if="node.gateway.policy.deny.length"
                label size="small" variant="outlined"
              >deny ({{ node.gateway.policy.deny.length }})</v-chip>
            </div>

            <v-row>
              <v-col cols="12" md="7">
                <div class="text-caption text-medium-emphasis mb-1">
                  Active sessions ({{ node.sessions.length }})
                </div>
                <v-data-table
                  :headers="nodeSessionHeaders"
                  :items="node.sessions"
                  :items-per-page="10"
                  density="compact"
                  no-data-text="No one on the node."
                >
                  <template #item.via="{ item }">{{ item.via || '—' }}</template>
                  <template #item.target="{ item }">
                    <span v-if="item.target">→ {{ item.target }}</span>
                    <span v-else class="text-medium-emphasis">=&gt; (prompt)</span>
                  </template>
                  <template #item.connected_s="{ item }">{{ item.connected_s }}s</template>
                  <template #item.idle_s="{ item }">{{ item.idle_s }}s</template>
                </v-data-table>
              </v-col>

              <v-col cols="12" md="5">
                <div class="text-caption text-medium-emphasis mb-1">
                  Recent gateway refusals
                </div>
                <v-list v-if="recentRefusals.length" density="compact" class="py-0">
                  <v-list-item
                    v-for="(r, i) in recentRefusals" :key="i" class="px-2"
                  >
                    <template #title>
                      <span class="text-body-2">
                        {{ r.user }}<span v-if="r.dest"> → {{ r.dest }}</span>
                      </span>
                    </template>
                    <template #subtitle>
                      <span class="text-caption">{{ fmtTime(r.ts) }} · {{ r.reason }}</span>
                    </template>
                  </v-list-item>
                </v-list>
                <div v-else class="text-caption text-disabled">None.</div>
              </v-col>
            </v-row>

            <!-- Live L4 circuits — surfaced separately from node sessions so a
                 circuit that outlives its session (e.g. a DISC awaiting ACK)
                 is visible instead of only showing up in the logs. -->
            <div class="text-caption text-medium-emphasis mt-4 mb-1">
              Live circuits ({{ circuits.length }})
            </div>
            <v-data-table
              :headers="circuitHeaders"
              :items="circuits"
              :items-per-page="10"
              density="compact"
              no-data-text="No open circuits."
            >
              <template #item.via="{ item }">{{ item.via || '—' }}</template>
              <template #item.state="{ item }">
                <v-chip
                  label size="x-small"
                  :color="item.state === 'CONNECTED' ? 'success'
                          : item.state === 'DISCONNECTING' ? 'warning'
                          : undefined"
                >{{ item.state }}</v-chip>
              </template>
            </v-data-table>
          </v-card-text>
        </v-card>
      </v-col>
    </v-row>
  </v-container>
</template>
