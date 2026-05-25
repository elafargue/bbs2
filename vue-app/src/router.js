// src/router.js — Vue Router (multi-page admin SPA)
import { createRouter, createWebHashHistory } from 'vue-router'

const routes = [
  { path: '/',          component: () => import('./views/Dashboard.vue') },
  { path: '/users',     component: () => import('./views/Users.vue') },
  { path: '/plugins',   component: () => import('./views/Plugins.vue') },
  { path: '/activity',  component: () => import('./views/Activity.vue') },
  { path: '/terminal',  component: () => import('./views/Terminal.vue') },
  { path: '/display',   component: () => import('./views/Display.vue') },
  { path: '/bulletins', component: () => import('./views/Bulletins.vue') },
  { path: '/chat',      component: () => import('./views/ChatRooms.vue') },
  { path: '/heard',     component: () => import('./views/HeardConfig.vue') },
  { path: '/info',      component: () => import('./views/InfoEditor.vue') },
  { path: '/login',     component: () => import('./views/Login.vue') },
]

export const router = createRouter({
  history: createWebHashHistory(),
  routes,
})
