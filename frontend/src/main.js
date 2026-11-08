import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from './App.vue'
import router from './router'
import { installAuthInterceptors } from './stores/auth'
import './assets/main.css'

const app = createApp(App)
app.use(createPinia())

// Must run before the router, whose first guard restores the session.
installAuthInterceptors(router)

app.use(router).mount('#app')
