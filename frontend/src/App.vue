<script setup>
/**
 * The application shell: a thin top bar and a grouped sidebar.
 *
 * The nav used to be a single row of links that wrapped onto two lines once
 * an admin signed in — thirteen destinations with no grouping, and the row
 * moving as the window resized. Grouping them by *what you are doing*
 * (selling, stock, masters, admin) makes any one of them findable without
 * reading the whole list.
 *
 * The sidebar collapses, because the POS screen wants every pixel, and the
 * choice is remembered.
 */
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const blank = computed(() => route.meta.layout === 'blank' || !auth.isAuthenticated)

const COLLAPSE_KEY = 'pharmacy.nav.collapsed'
const collapsed = ref(localStorage.getItem(COLLAPSE_KEY) === '1')
watch(collapsed, (value) => localStorage.setItem(COLLAPSE_KEY, value ? '1' : '0'))

// `ready: false` marks a destination that is planned but not built yet
// (see docs/ROADMAP.md) — shown greyed rather than hidden, so the shape of
// the finished app is visible.
const navGroups = computed(() => {
  const groups = [
    {
      label: 'Counter',
      items: [
        { label: 'POS', to: '/pos', ready: true, need: ['pos:sell'] },
        { label: 'Sales', to: '/sales', ready: true, need: ['sales:read'] },
        { label: 'Returns', to: '/returns', ready: true, need: ['returns:create'] },
      ],
    },
    // The two halves of the shop, each complete: catalogue, stock, goods
    // inward, alerts, expiry. Somebody restocking toiletries never has to
    // pick their way past medicine screens, and vice versa.
    {
      label: 'Medicine',
      items: [
        { label: 'Medicines', to: '/medicine/catalogue', ready: true, need: ['catalog:read'] },
        { label: 'Med inventory', to: '/medicine/inventory', ready: true, need: ['inventory:read'] },
        { label: 'Alerts', to: '/medicine/alerts', ready: true, need: ['alerts:read'] },
        { label: 'Expiry medicine', to: '/medicine/expiry', ready: true, need: ['expiry:read', 'expiry:manage'] },
        { label: 'Compositions', to: '/masters/compositions', ready: true, need: ['compositions:create', 'compositions:edit'] },
      ],
    },
    {
      label: 'General item',
      items: [
        { label: 'Products', to: '/general/products', ready: true, need: ['catalog:read'] },
        { label: 'Product inventory', to: '/general/inventory', ready: true, need: ['inventory:read'] },
        { label: 'Alerts', to: '/general/alerts', ready: true, need: ['alerts:read'] },
        { label: 'Expiry item', to: '/general/expiry', ready: true, need: ['expiry:read', 'expiry:manage'] },
      ],
    },
    // Shared by both halves — one customer, one supplier, one maker,
    // whichever side of the shop they are dealing with.
    {
      label: 'Masters',
      items: [
        { label: 'Purchases', to: '/purchases', ready: true, need: ['purchases:read', 'purchases:receive'] },
        { label: 'Purchase returns', to: '/purchase-returns', ready: true, need: ['purchases:read', 'purchases:return'] },
        { label: 'Customers', to: '/masters/customers', ready: true, need: ['customers:read'] },
        { label: 'Doctors', to: '/masters/doctors', ready: true, need: ['doctors:read'] },
        { label: 'Manufacturers', to: '/masters/manufacturers', ready: true, need: ['manufacturers:create', 'manufacturers:edit'] },
        { label: 'Suppliers', to: '/masters/suppliers', ready: true, need: ['suppliers:read', 'suppliers:manage'] },
        { label: 'Order book', to: '/order-book', ready: true, need: ['order_book:read', 'order_book:manage'] },
      ],
    },
    {
      label: 'Admin',
      admin: true,
      items: [
        { label: 'Approvals', to: '/admin/approvals', ready: true, need: ['sales:approve_void'] },
        { label: 'Reports', to: '/reports', ready: true, need: ['reports:view'] },
        { label: 'Users', to: '/admin/users', ready: true, need: ['users:manage'] },
        { label: 'Settings', to: '/admin/settings', ready: true, need: ['settings:manage'] },
      ],
    },
  ]

  // A destination is shown when the role allows it *and* the person still
  // holds something the screen needs. The server refuses either way; hiding
  // it just avoids offering a door that is locked.
  const visible = (item) =>
    (!item.admin || auth.isAdmin) && (!item.need || auth.canAny(...item.need))

  return groups
    .filter((group) => !group.admin || auth.isAdmin)
    .map((group) => ({ ...group, items: group.items.filter(visible) }))
    .filter((group) => group.items.length)
})

async function signOut() {
  await auth.logout()
  router.push({ name: 'login' })
}
</script>

<template>
  <RouterView v-if="blank" />

  <div v-else :class="['shell', { 'shell--collapsed': collapsed }]">
    <a class="skip" href="#main">Skip to content</a>

    <header class="shell__top">
      <button
        class="shell__toggle"
        :aria-label="collapsed ? 'Show navigation' : 'Hide navigation'"
        :aria-expanded="!collapsed"
        @click="collapsed = !collapsed"
      >
        <span aria-hidden="true">☰</span>
      </button>

      <RouterLink class="shell__brand" to="/">
        {{ auth.store?.name || 'Pharmacy POS' }}
      </RouterLink>

      <span class="shell__page">{{ route.meta.title }}</span>

      <div class="shell__user">
        <span class="shell__who">
          {{ auth.user?.full_name }}
          <span :class="['badge', auth.isAdmin ? 'badge--ok' : 'badge--warn']">
            {{ auth.isAdmin ? 'Admin' : 'Sales' }}
          </span>
        </span>
        <RouterLink class="btn btn--sm" to="/change-password">Password</RouterLink>
        <button class="btn btn--sm" @click="signOut">Sign out</button>
      </div>
    </header>

    <nav class="shell__side" aria-label="Main">
      <RouterLink class="nav__link nav__link--home" to="/">Dashboard</RouterLink>

      <div v-for="group in navGroups" :key="group.label" class="nav__group">
        <p class="nav__heading">{{ group.label }}</p>
        <template v-for="item in group.items" :key="item.to">
          <RouterLink v-if="item.ready" class="nav__link" :to="item.to">
            {{ item.label }}
          </RouterLink>
          <span v-else class="nav__link nav__link--pending" title="Not built yet">
            {{ item.label }}
          </span>
        </template>
      </div>
    </nav>

    <main id="main" class="shell__main">
      <RouterView />
    </main>
  </div>
</template>

<style scoped>
.shell {
  display: grid;
  grid-template-columns: var(--sidebar-w) minmax(0, 1fr);
  grid-template-rows: var(--topbar-h) minmax(0, 1fr);
  grid-template-areas:
    'top top'
    'side main';
  min-height: 100%;
}

.shell--collapsed {
  grid-template-columns: 0 minmax(0, 1fr);
}

.shell--collapsed .shell__side {
  display: none;
}

/* Keyboard users get to the content without tabbing the whole nav. */
.skip {
  position: absolute;
  left: -999px;
  z-index: 100;
  background: var(--surface);
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--radius);
}

.skip:focus {
  left: var(--sp-2);
  top: var(--sp-2);
}

/* --- top bar --- */
.shell__top {
  grid-area: top;
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: 0 var(--sp-3);
  background: var(--surface);
  border-bottom: 1px solid var(--border);
}

.shell__toggle {
  font: inherit;
  font-size: 1.05rem;
  line-height: 1;
  cursor: pointer;
  background: none;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  padding: 0.25rem 0.45rem;
  color: var(--text-muted);
}

.shell__toggle:hover {
  border-color: var(--border-strong);
  color: var(--text);
}

.shell__brand {
  font-weight: 700;
  letter-spacing: -0.015em;
  color: var(--accent);
  text-decoration: none;
  white-space: nowrap;
}

/* Which screen you are on, once, in the bar — so page bodies do not each
   have to spend a heading on it. */
.shell__page {
  color: var(--text-muted);
  font-size: 0.85rem;
  padding-left: var(--sp-3);
  border-left: 1px solid var(--border);
  white-space: nowrap;
}

.shell__user {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  white-space: nowrap;
}

.shell__who {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: 0.85rem;
}

/* --- sidebar --- */
.shell__side {
  grid-area: side;
  position: sticky;
  top: var(--topbar-h);
  align-self: start;
  max-height: calc(100vh - var(--topbar-h));
  overflow-y: auto;
  padding: var(--sp-3) var(--sp-2);
  background: var(--surface);
  border-right: 1px solid var(--border);
}

.nav__group + .nav__group {
  margin-top: var(--sp-3);
}

.nav__heading {
  margin: 0 0 var(--sp-1);
  padding: 0 var(--sp-2);
  font-size: 0.68rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.07em;
  color: var(--text-muted);
}

.nav__link {
  display: block;
  padding: 0.28rem var(--sp-2);
  border-radius: var(--radius-sm);
  color: var(--text);
  text-decoration: none;
  font-size: 0.88rem;
  border-left: 2px solid transparent;
}

.nav__link:hover {
  background: var(--accent-soft);
}

.nav__link--home {
  font-weight: 600;
  margin-bottom: var(--sp-3);
}

.nav__link.router-link-active {
  background: var(--accent-soft);
  border-left-color: var(--accent);
  color: var(--accent);
  font-weight: 600;
}

.nav__link--pending {
  color: #a9b2bd;
  cursor: not-allowed;
}

.nav__link--pending:hover {
  background: none;
}

/* --- content --- */
.shell__main {
  grid-area: main;
  padding: var(--sp-4);
  width: 100%;
  max-width: 1400px;
  margin: 0 auto;
  min-width: 0;
}

@media (max-width: 760px) {
  .shell,
  .shell--collapsed {
    grid-template-columns: minmax(0, 1fr);
    grid-template-areas:
      'top'
      'side'
      'main';
  }

  .shell__side {
    position: static;
    max-height: none;
    border-right: 0;
    border-bottom: 1px solid var(--border);
    display: flex;
    flex-wrap: wrap;
    gap: var(--sp-2);
  }

  .shell--collapsed .shell__side {
    display: none;
  }

  .nav__group {
    display: flex;
    flex-wrap: wrap;
    gap: var(--sp-1);
    align-items: center;
  }

  .nav__group + .nav__group {
    margin-top: 0;
  }

  .shell__page {
    display: none;
  }
}
</style>
