import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { createUser, getUsers, resetUserShareAccess } from '../api/client'
import { KeyRound, RefreshCw, Shield, UserPlus, Users } from 'lucide-react'
import { formatShort } from '../utils/time'

function errorMessage(err: any) {
  return err?.response?.data?.detail || 'Request failed'
}

export default function Accounts() {
  const qc = useQueryClient()
  const { data: users = [], isLoading, error } = useQuery({ queryKey: ['users'], queryFn: getUsers, retry: 0 })
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [isAdmin, setIsAdmin] = useState(false)
  const [createdCredential, setCreatedCredential] = useState<any>(null)
  const [formError, setFormError] = useState('')

  const create = useMutation({
    mutationFn: createUser,
    onSuccess: (data) => {
      setCreatedCredential(data)
      setUsername('')
      setPassword('')
      setIsAdmin(false)
      setFormError('')
      qc.invalidateQueries({ queryKey: ['users'] })
    },
    onError: (err) => setFormError(errorMessage(err)),
  })

  const resetShare = useMutation({
    mutationFn: resetUserShareAccess,
    onSuccess: (data) => {
      setCreatedCredential(data)
      qc.invalidateQueries({ queryKey: ['users'] })
    },
    onError: (err) => setFormError(errorMessage(err)),
  })

  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    if (!username || !password || create.isPending) return
    setCreatedCredential(null)
    create.mutate({ username, password, is_admin: isAdmin })
  }

  if (isLoading) return <div className="p-6 text-gray-400">Loading...</div>
  if (error) return <div className="p-6 text-red-300">Admin access required.</div>

  return (
    <div className="p-6">
      <div className="flex items-center gap-3 mb-6">
        <Users size={24} className="text-blue-400" />
        <h1 className="text-2xl font-bold">Accounts ({users.length})</h1>
      </div>

      <form onSubmit={submit} className="bg-gray-900 border border-gray-800 rounded-xl p-4 mb-6">
        <div className="grid grid-cols-1 md:grid-cols-[1fr_1fr_auto_auto] gap-3 items-end">
          <div>
            <label className="block text-xs text-gray-400 mb-1">Username</label>
            <input
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              placeholder="tech_user"
              autoComplete="off"
            />
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">Login Password</label>
            <input
              type="password"
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete="new-password"
            />
          </div>
          <label className="flex items-center gap-2 text-sm text-gray-300 pb-2">
            <input
              type="checkbox"
              checked={isAdmin}
              onChange={(event) => setIsAdmin(event.target.checked)}
              className="h-4 w-4 rounded border-gray-600 bg-gray-800"
            />
            Admin
          </label>
          <button
            type="submit"
            disabled={!username || !password || create.isPending}
            className="inline-flex items-center justify-center gap-2 rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium transition hover:bg-blue-500 disabled:opacity-50"
          >
            <UserPlus size={14} /> {create.isPending ? 'Creating...' : 'Create'}
          </button>
        </div>
        {formError && <div className="mt-3 text-sm text-red-300">{formError}</div>}
      </form>

      {createdCredential?.smb_password && (
        <div className="mb-6 rounded-xl border border-emerald-700 bg-emerald-950/30 p-4 text-sm">
          <div className="mb-2 flex items-center gap-2 font-medium text-emerald-200">
            <KeyRound size={15} /> SMB share credential
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-emerald-100">
            <div><span className="text-emerald-400">Username:</span> {createdCredential.smb_username}</div>
            <div><span className="text-emerald-400">Password:</span> {createdCredential.smb_password}</div>
          </div>
        </div>
      )}

      <div className="overflow-hidden rounded-xl border border-gray-800 bg-gray-900">
        <table className="w-full text-sm">
          <thead className="border-b border-gray-800 text-gray-400">
            <tr>
              <th className="px-4 py-3 text-left">Username</th>
              <th className="px-4 py-3 text-left">Role</th>
              <th className="px-4 py-3 text-left">Share User</th>
              <th className="px-4 py-3 text-left">Share Access</th>
              <th className="px-4 py-3 text-left">Created</th>
              <th className="px-4 py-3"></th>
            </tr>
          </thead>
          <tbody>
            {users.map((user: any) => (
              <tr key={user.id} className="border-b border-gray-800/50">
                <td className="px-4 py-3 font-medium">{user.username}</td>
                <td className="px-4 py-3 text-gray-300">
                  {user.is_admin ? (
                    <span className="inline-flex items-center gap-1 text-blue-300"><Shield size={13} /> Admin</span>
                  ) : 'User'}
                </td>
                <td className="px-4 py-3 font-mono text-xs text-gray-400">{user.smb_username ?? '-'}</td>
                <td className="px-4 py-3">
                  <span className={user.smb_access_enabled ? 'text-emerald-300' : 'text-yellow-300'}>
                    {user.smb_access_enabled ? 'Enabled' : 'Needs setup'}
                  </span>
                </td>
                <td className="px-4 py-3 text-xs text-gray-500">{formatShort(user.created_at)}</td>
                <td className="px-4 py-3 text-right">
                  <button
                    onClick={() => resetShare.mutate(user.id)}
                    disabled={resetShare.isPending}
                    className="inline-flex items-center gap-2 rounded-md border border-gray-700 px-3 py-1.5 text-xs text-gray-300 transition hover:border-blue-500 hover:text-blue-300 disabled:opacity-50"
                    title="Reset SMB share access"
                  >
                    <RefreshCw size={13} /> Reset Share
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
