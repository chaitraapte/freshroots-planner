import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

// The dashboard used to live at "/". Old links such as "/#orders" now forward to "/dashboard/#orders".
const DASHBOARD_PAGES = ['today', 'orders', 'whatif', 'skus', 'scoreboard', 'rules',
  'actions', 'pos', 'exceptions', 'scenario', 'decisions', 'quality', 'assumptions', 'config']
if (DASHBOARD_PAGES.includes(window.location.hash.slice(1))) {
  window.location.replace('/dashboard/' + window.location.hash)
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
