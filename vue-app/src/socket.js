// src/socket.js — singleton Socket.IO client
// Import this everywhere — never call io() directly in components.
import { io } from 'socket.io-client'

const socket = io({ autoConnect: false })
export default socket
