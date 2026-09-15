import { useEffect } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import Machines from './pages/Machines'
import MachineDetail from './pages/MachineDetail'
import Images from './pages/Images'
import Jobs from './pages/Jobs'
import Login from './pages/Login'
import AgentInstaller from './pages/AgentInstaller'
import BootableUsb from './pages/BootableUsb'
import WinPEFiles from './pages/WinPEFiles'
import Accounts from './pages/Accounts'
import { refreshSession } from './api/client'
import {
  clearSessionAndRedirect,
  getToken,
  hasActiveSession,
  isSessionIdleTimedOut,
  markActivity,
  updateSessionToken,
} from './auth/session'

const IDLE_CHECK_INTERVAL_MS = 30 * 1000
const TOKEN_REFRESH_INTERVAL_MS = 5 * 60 * 1000
const ACTIVITY_EVENTS = ['pointerdown', 'pointermove', 'keydown', 'scroll', 'touchstart', 'wheel']

function RequireAuth({ children }: { children: JSX.Element }) {
  return hasActiveSession() ? children : <Navigate to="/login" replace />
}

function SessionLifecycle() {
  useEffect(() => {
    const logoutIfIdle = () => {
      if (getToken() && isSessionIdleTimedOut()) {
        clearSessionAndRedirect()
        return true
      }

      return false
    }

    const handleActivity = () => {
      if (!logoutIfIdle()) {
        markActivity()
      }
    }

    const refreshActiveSession = async () => {
      if (!getToken() || logoutIfIdle()) return

      try {
        const resp = await refreshSession()
        updateSessionToken(resp.data.access_token)
      } catch {
        // The response interceptor clears the session on 401.
      }
    }

    ACTIVITY_EVENTS.forEach((eventName) => {
      window.addEventListener(eventName, handleActivity, { passive: true })
    })
    document.addEventListener('visibilitychange', handleActivity)

    const idleTimer = window.setInterval(logoutIfIdle, IDLE_CHECK_INTERVAL_MS)
    const refreshTimer = window.setInterval(refreshActiveSession, TOKEN_REFRESH_INTERVAL_MS)

    logoutIfIdle()
    void refreshActiveSession()

    return () => {
      ACTIVITY_EVENTS.forEach((eventName) => {
        window.removeEventListener(eventName, handleActivity)
      })
      document.removeEventListener('visibilitychange', handleActivity)
      window.clearInterval(idleTimer)
      window.clearInterval(refreshTimer)
    }
  }, [])

  return null
}

export default function App() {
  return (
    <>
      <SessionLifecycle />
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route
          path="/"
          element={
            <RequireAuth>
              <Layout />
            </RequireAuth>
          }
        >
          <Route index element={<Dashboard />} />
          <Route path="machines" element={<Machines />} />
          <Route path="machines/:id" element={<MachineDetail />} />
          <Route path="images" element={<Images />} />
          <Route path="jobs" element={<Jobs />} />
          <Route path="installer" element={<AgentInstaller />} />
          <Route path="bootable-usb" element={<BootableUsb />} />
          <Route path="winpe" element={<WinPEFiles />} />
          <Route path="accounts" element={<Accounts />} />
        </Route>
      </Routes>
    </>
  )
}
