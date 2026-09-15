import { Outlet, NavLink, useNavigate } from 'react-router-dom'
import { Monitor, HardDrive, ClipboardList, LayoutDashboard, LogOut, Download, Clock, FileArchive, Users, Usb } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import clsx from 'clsx'
import { localTimezone } from '../utils/time'
import { getMe } from '../api/client'
import { clearSession } from '../auth/session'

const nav = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true },
  { to: '/machines', label: 'Machines', icon: Monitor },
  { to: '/images', label: 'Images', icon: HardDrive },
  { to: '/jobs', label: 'Jobs', icon: ClipboardList },
  { to: '/installer', label: 'Agent Installer', icon: Download },
  { to: '/bootable-usb', label: 'Bootable USB', icon: Usb },
  { to: '/winpe', label: 'WinPE Files', icon: FileArchive },
  { to: '/accounts', label: 'Accounts', icon: Users, adminOnly: true },
]

export default function Layout() {
  const navigate = useNavigate()
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: getMe })
  const logout = () => { clearSession(); navigate('/login') }

  return (
    <div className="flex h-screen">
      {/* Sidebar */}
      <aside className="w-56 bg-gray-900 border-r border-gray-800 flex flex-col">
        <div className="p-4 border-b border-gray-800">
          <span className="text-blue-400 font-bold text-lg">🖥 Bretter-IMG</span>
          <div className="text-gray-500 text-xs mt-0.5">Remote Imaging Console</div>
          <div className="flex items-center gap-1 text-gray-600 text-xs mt-1">
            <Clock size={10} />
            <span>{localTimezone()}</span>
          </div>
        </div>
        <nav className="flex-1 p-2 space-y-1">
          {nav.filter((item) => !item.adminOnly || me?.is_admin).map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                clsx(
                  'flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-colors',
                  isActive
                    ? 'bg-blue-600 text-white'
                    : 'text-gray-400 hover:bg-gray-800 hover:text-gray-100',
                )
              }
            >
              <Icon size={16} />
              {label}
            </NavLink>
          ))}
        </nav>
        <button
          onClick={logout}
          className="flex items-center gap-2 p-4 text-sm text-gray-500 hover:text-red-400 border-t border-gray-800 transition-colors"
        >
          <LogOut size={16} /> Logout
        </button>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-auto bg-gray-950">
        <Outlet />
      </main>
    </div>
  )
}
