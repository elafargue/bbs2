<script setup>
/**
 * ChatRooms.vue — Sysop chat room management panel.
 *
 * Shows all chat rooms with online member count and stored message count.
 * Sysop can:
 *   - drill down into a room to view and delete individual messages
 *   - delete an entire room (and all its history)
 */
import { ref, onMounted } from 'vue'

const rooms = ref([])
const loading = ref(false)
const snackbar = ref({ show: false, text: '', color: 'success' })

// Messages drill-down dialog
const msgDialog = ref(false)
const msgRoom = ref(null)      // room object currently viewed
const messages = ref([])
const msgsLoading = ref(false)

// Delete-room confirm dialog
const delRoomDialog = ref(false)
const delRoomTarget = ref(null)

// Delete-message confirm dialog
const delMsgDialog = ref(false)
const delMsgTarget = ref(null)  // { room, id, line }

// ── helpers ────────────────────────────────────────────────────────────────

function fmtTs(ts) {
  if (!ts) return ''
  return new Date(ts * 1000).toLocaleString()
}

function notify(text, color = 'success') {
  snackbar.value = { show: true, text, color }
}

// ── load rooms ────────────────────────────────────────────────────────────

async function loadRooms() {
  loading.value = true
  try {
    const res = await fetch('/api/chat/rooms')
    if (res.ok) rooms.value = await res.json()
    else notify('Failed to load rooms', 'error')
  } finally {
    loading.value = false
  }
}

// ── messages drill-down ───────────────────────────────────────────────────

async function openMessages(room) {
  msgRoom.value = room
  msgDialog.value = true
  await loadMessages(room.name)
}

async function loadMessages(roomName) {
  msgsLoading.value = true
  try {
    const res = await fetch(`/api/chat/rooms/${encodeURIComponent(roomName)}/messages?n=100`)
    if (res.ok) messages.value = (await res.json()).reverse()
    else notify('Failed to load messages', 'error')
  } finally {
    msgsLoading.value = false
  }
}

function confirmDeleteMsg(msg) {
  delMsgTarget.value = { room: msgRoom.value.name, id: msg.id, line: msg.line }
  delMsgDialog.value = true
}

async function executeDeleteMsg() {
  const { room, id } = delMsgTarget.value
  delMsgDialog.value = false
  const res = await fetch(
    `/api/chat/rooms/${encodeURIComponent(room)}/messages/${id}`,
    { method: 'DELETE' },
  )
  if (res.ok) {
    notify(`Message #${id} deleted.`)
    messages.value = messages.value.filter(m => m.id !== id)
    // Update count on the room card
    const r = rooms.value.find(x => x.name === room)
    if (r) r.message_count = Math.max(0, r.message_count - 1)
  } else {
    notify('Failed to delete message', 'error')
  }
}

// ── delete room ───────────────────────────────────────────────────────────

function confirmDeleteRoom(room) {
  delRoomTarget.value = room
  delRoomDialog.value = true
}

async function executeDeleteRoom() {
  const room = delRoomTarget.value
  delRoomDialog.value = false
  if (msgDialog.value && msgRoom.value?.name === room.name) {
    msgDialog.value = false
  }
  const res = await fetch(`/api/chat/rooms/${encodeURIComponent(room.name)}`, {
    method: 'DELETE',
  })
  if (res.ok) {
    notify(`Room '${room.name}' deleted.`)
    rooms.value = rooms.value.filter(r => r.name !== room.name)
  } else {
    notify('Failed to delete room', 'error')
  }
}

onMounted(loadRooms)
</script>

<template>
  <!-- Rooms table -->
  <v-data-table
    :headers="[
      { title: 'Room',         key: 'name',           sortable: true  },
      { title: 'Description',  key: 'description',    sortable: false },
      { title: 'Online',       key: 'members_online', sortable: true, align: 'end' },
      { title: 'Stored msgs',  key: 'message_count',  sortable: true, align: 'end' },
      { title: 'Actions',      key: 'actions',        sortable: false, align: 'end' },
    ]"
    :items="rooms"
    :loading="loading"
    density="compact"
    class="elevation-0"
  >
    <template #top>
      <div class="d-flex align-center pa-2">
        <span class="text-subtitle-2 text-medium-emphasis">Chat Rooms</span>
        <v-spacer />
        <v-btn icon="mdi-refresh" variant="text" size="small" :loading="loading" @click="loadRooms" />
      </div>
    </template>

    <template #item.name="{ item }">
      <v-btn
        variant="text"
        color="primary"
        size="small"
        :prepend-icon="item.members_online > 0 ? 'mdi-forum' : 'mdi-forum-outline'"
        @click="openMessages(item)"
      >{{ item.name }}</v-btn>
    </template>

    <template #item.members_online="{ item }">
      <v-chip
        size="x-small"
        :color="item.members_online > 0 ? 'success' : 'default'"
      >{{ item.members_online }}</v-chip>
    </template>

    <template #item.actions="{ item }">
      <v-btn
        icon="mdi-delete"
        variant="text"
        size="small"
        color="error"
        @click="confirmDeleteRoom(item)"
      />
    </template>

    <template #no-data>
      <div class="text-center text-medium-emphasis py-4">No chat rooms found.</div>
    </template>
  </v-data-table>

  <!-- Messages drill-down dialog -->
  <v-dialog v-model="msgDialog" max-width="800" scrollable>
    <v-card>
      <v-card-title class="d-flex align-center">
        <v-icon start>mdi-forum</v-icon>
        Room: {{ msgRoom?.name }}
        <v-spacer />
        <v-btn
          icon="mdi-refresh"
          variant="text"
          size="small"
          :loading="msgsLoading"
          @click="loadMessages(msgRoom.name)"
        />
        <v-btn icon="mdi-close" variant="text" @click="msgDialog = false" />
      </v-card-title>
      <v-divider />
      <v-card-text class="pa-0">
        <v-data-table
          :headers="[
            { title: '#',        key: 'id',   sortable: true,  width: '70px', align: 'end' },
            { title: 'Time',     key: 'ts',   sortable: true,  width: '160px' },
            { title: 'Message',  key: 'line', sortable: false },
            { title: '',         key: 'del',  sortable: false, width: '48px', align: 'end' },
          ]"
          :items="messages"
          :loading="msgsLoading"
          density="compact"
          class="elevation-0"
          :items-per-page="50"
        >
          <template #item.ts="{ item }">
            <span class="text-caption text-medium-emphasis">{{ fmtTs(item.ts) }}</span>
          </template>
          <template #item.line="{ item }">
            <span class="text-body-2" style="font-family: monospace; white-space: pre-wrap;">{{ item.line }}</span>
          </template>
          <template #item.del="{ item }">
            <v-btn
              icon="mdi-delete"
              variant="text"
              size="x-small"
              color="error"
              @click="confirmDeleteMsg(item)"
            />
          </template>
          <template #no-data>
            <div class="text-center text-medium-emphasis py-4">No messages stored.</div>
          </template>
        </v-data-table>
      </v-card-text>
      <v-divider />
      <v-card-actions>
        <v-btn
          color="error"
          variant="tonal"
          prepend-icon="mdi-delete-sweep"
          @click="confirmDeleteRoom(msgRoom)"
        >Delete Room</v-btn>
        <v-spacer />
        <v-btn variant="tonal" @click="msgDialog = false">Close</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>

  <!-- Confirm delete message -->
  <v-dialog v-model="delMsgDialog" max-width="480">
    <v-card>
      <v-card-title>Delete message?</v-card-title>
      <v-card-text>
        <p class="text-body-2 mb-2">
          Message <strong>#{{ delMsgTarget?.id }}</strong> will be permanently deleted.
        </p>
        <v-sheet
          color="surface-variant"
          rounded
          class="pa-2 text-caption"
          style="font-family: monospace; white-space: pre-wrap;"
        >{{ delMsgTarget?.line }}</v-sheet>
      </v-card-text>
      <v-card-actions>
        <v-spacer />
        <v-btn variant="text" @click="delMsgDialog = false">Cancel</v-btn>
        <v-btn color="error" variant="tonal" @click="executeDeleteMsg">Delete</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>

  <!-- Confirm delete room -->
  <v-dialog v-model="delRoomDialog" max-width="440">
    <v-card>
      <v-card-title>Delete room?</v-card-title>
      <v-card-text>
        Room <strong>{{ delRoomTarget?.name }}</strong> and all its stored messages
        will be permanently deleted. Connected users will be notified.
      </v-card-text>
      <v-card-actions>
        <v-spacer />
        <v-btn variant="text" @click="delRoomDialog = false">Cancel</v-btn>
        <v-btn color="error" variant="tonal" @click="executeDeleteRoom">Delete Room</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>

  <v-snackbar v-model="snackbar.show" :color="snackbar.color" timeout="3000">
    {{ snackbar.text }}
  </v-snackbar>
</template>
