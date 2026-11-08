import { createRouter, createWebHistory } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import HomeView from '@/views/HomeView.vue'
import LoginView from '@/views/LoginView.vue'

const routes = [
  {
    path: '/login',
    name: 'login',
    component: LoginView,
    meta: { title: 'Sign in', requiresAuth: false, layout: 'blank' },
  },
  {
    path: '/change-password',
    name: 'change-password',
    component: () => import('@/views/ChangePasswordView.vue'),
    meta: { title: 'Change password' },
  },
  {
    path: '/',
    name: 'home',
    component: HomeView,
    meta: { title: 'Dashboard' },
  },
  {
    path: '/pos',
    name: 'pos',
    component: () => import('@/views/PosView.vue'),
    meta: { title: 'POS', permissions: ['pos:sell'] },
  },
  {
    path: '/sales',
    name: 'sales',
    component: () => import('@/views/SalesView.vue'),
    meta: { title: 'Sales', permissions: ['sales:read'] },
  },
  {
    path: '/sales/:id/invoice',
    name: 'invoice',
    component: () => import('@/views/InvoiceView.vue'),
    meta: { title: 'Invoice', permissions: ['sales:read'] },
  },
  {
    path: '/returns',
    name: 'returns',
    component: () => import('@/views/ReturnsView.vue'),
    meta: { title: 'Returns', permissions: ['returns:create'] },
  },
  {
    path: '/order-book',
    name: 'order-book',
    component: () => import('@/views/admin/OrderBookView.vue'),
    meta: { title: 'Order book', permissions: ['order_book:read', 'order_book:manage'] },
  },
  {
    path: '/admin/approvals',
    name: 'admin-approvals',
    component: () => import('@/views/admin/ApprovalsView.vue'),
    meta: { title: 'Approvals', adminOnly: true, permissions: ['sales:approve_void', 'sales:request_void'] },
  },
  {
    path: '/masters/customers',
    name: 'masters-customers',
    component: () => import('@/views/masters/CustomersView.vue'),
    meta: { title: 'Customers', permissions: ['customers:read'] },
  },
  {
    path: '/masters/doctors',
    name: 'masters-doctors',
    component: () => import('@/views/masters/DoctorsView.vue'),
    meta: { title: 'Doctors', permissions: ['doctors:read'] },
  },
  // --- Medicine ---------------------------------------------------------
  //
  // The two halves of the shop are stocked, watched and re-ordered by
  // different people looking at different shelves, so each gets its own
  // catalogue, stock, goods inward, alerts and expiry register. The screens
  // are the same components; `meta.itemType` scopes them, and the API takes
  // the same `item_type` filter, so there is one implementation of each
  // rather than two that drift apart.
  {
    path: '/medicine/catalogue',
    name: 'medicine-catalogue',
    component: () => import('@/views/masters/MedicinesView.vue'),
    meta: { title: 'Medicines', permissions: ['catalog:read'] },
  },
  {
    path: '/medicine/inventory',
    name: 'medicine-inventory',
    component: () => import('@/views/InventoryView.vue'),
    meta: { title: 'Med inventory', permissions: ['inventory:read'], itemType: 'MEDICINE' },
  },
  {
    path: '/medicine/alerts',
    name: 'medicine-alerts',
    component: () => import('@/views/AlertsView.vue'),
    meta: { title: 'Alerts', permissions: ['alerts:read'], itemType: 'MEDICINE' },
  },
  {
    path: '/medicine/expiry',
    name: 'medicine-expiry',
    component: () => import('@/views/admin/ExpiryRegisterView.vue'),
    meta: {
      title: 'Expiry medicine',
      permissions: ['expiry:read', 'expiry:manage'],
      itemType: 'MEDICINE',
    },
  },

  // --- General item -----------------------------------------------------
  {
    path: '/general/products',
    name: 'general-products',
    component: () => import('@/views/masters/GeneralItemsView.vue'),
    meta: { title: 'Products', permissions: ['catalog:read'] },
  },
  {
    path: '/general/inventory',
    name: 'general-inventory',
    component: () => import('@/views/InventoryView.vue'),
    meta: {
      title: 'Product inventory',
      permissions: ['inventory:read'],
      itemType: 'GENERAL',
    },
  },
  {
    path: '/general/alerts',
    name: 'general-alerts',
    component: () => import('@/views/AlertsView.vue'),
    meta: { title: 'Alerts', permissions: ['alerts:read'], itemType: 'GENERAL' },
  },
  {
    path: '/general/expiry',
    name: 'general-expiry',
    component: () => import('@/views/admin/ExpiryRegisterView.vue'),
    meta: {
      title: 'Expiry item',
      permissions: ['expiry:read', 'expiry:manage'],
      itemType: 'GENERAL',
    },
  },

  {
    // One screen, not two: a distributor's invoice regularly carries
    // medicines and general goods on the same document, and splitting the
    // register by kind would mean deciding which half such an invoice
    // belongs to. It belongs to both, so it lives in one place.
    path: '/purchases',
    name: 'purchases',
    component: () => import('@/views/PurchasesView.vue'),
    meta: { title: 'Purchases', permissions: ['purchases:read', 'purchases:receive'] },
  },
  {
    // Beside Purchases for the same reason it is one screen: a debit note can
    // carry medicines and general goods together, because a distributor takes
    // back whatever they sold.
    path: '/purchase-returns',
    name: 'purchase-returns',
    component: () => import('@/views/PurchaseReturnsView.vue'),
    meta: {
      title: 'Purchase returns',
      permissions: ['purchases:read', 'purchases:return'],
    },
  },

  // Where the screens used to live. Kept as redirects so a bookmark, or a
  // link inside the app that has not been updated, still lands somewhere.
  { path: '/inventory', redirect: { name: 'medicine-inventory' } },
  { path: '/medicine/purchases', redirect: { name: 'purchases' } },
  { path: '/general/purchases', redirect: { name: 'purchases' } },
  { path: '/alerts', redirect: { name: 'medicine-alerts' } },
  { path: '/expiry-register', redirect: { name: 'medicine-expiry' } },
  { path: '/masters/medicines', redirect: { name: 'medicine-catalogue' } },
  { path: '/masters/general', redirect: { name: 'general-products' } },
  {
    path: '/masters/compositions',
    name: 'masters-compositions',
    component: () => import('@/views/masters/CompositionsView.vue'),
    meta: { title: 'Compositions', permissions: ['compositions:create', 'compositions:edit'] },
  },
  {
    path: '/masters/manufacturers',
    name: 'masters-manufacturers',
    component: () => import('@/views/masters/ManufacturersView.vue'),
    meta: { title: 'Manufacturers', permissions: ['manufacturers:create', 'manufacturers:edit'] },
  },
  {
    path: '/masters/suppliers',
    name: 'masters-suppliers',
    component: () => import('@/views/masters/SuppliersView.vue'),
    meta: { title: 'Suppliers', permissions: ['suppliers:read', 'suppliers:manage'] },
  },
  {
    path: '/admin/users',
    name: 'admin-users',
    component: () => import('@/views/admin/UsersView.vue'),
    meta: { title: 'Users', adminOnly: true, permissions: ['users:manage'] },
  },
  {
    path: '/admin/settings',
    name: 'admin-settings',
    component: () => import('@/views/admin/SettingsView.vue'),
    meta: { title: 'Settings', adminOnly: true, permissions: ['settings:manage'] },
  },
  {
    path: '/reports',
    name: 'reports',
    component: () => import('@/views/ReportsView.vue'),
    meta: { title: 'Reports', adminOnly: true, permissions: ['reports:view'] },
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/NotFoundView.vue'),
    meta: { title: 'Not found' },
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()

  // On a hard refresh the store is empty; try the persisted refresh token
  // before deciding the visitor is a stranger.
  if (!auth.ready) await auth.restore()

  const requiresAuth = to.meta.requiresAuth !== false

  if (requiresAuth && !auth.isAuthenticated) {
    return { name: 'login', query: to.fullPath === '/' ? {} : { next: to.fullPath } }
  }
  if (!requiresAuth && auth.isAuthenticated) {
    return { name: 'home' }
  }
  // A temporary password must be replaced before anything else is reachable.
  if (auth.isAuthenticated && auth.mustChangePassword && to.name !== 'change-password') {
    return { name: 'change-password' }
  }
  if (to.meta.adminOnly && !auth.isAdmin) {
    return { name: 'home' }
  }
  // The admin can switch any of these off for one person. The API refuses
  // them too, so this only avoids landing on a screen that cannot load.
  if (to.meta.permissions && !auth.canAny(...to.meta.permissions)) {
    return { name: 'home' }
  }
  return true
})

router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} · Pharmacy POS` : 'Pharmacy POS'
})

export default router
