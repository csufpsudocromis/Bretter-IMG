import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  assignMachinesToGroup,
  createJobs,
  createMachineGroup,
  deleteMachine,
  deleteMachineGroup,
  getMachineGroups,
  getMachines,
  ungroupMachines,
} from '../api/client'
import StatusBadge from '../components/StatusBadge'
import DeployWizard from '../components/DeployWizard'
import RemoteActionsModal from '../components/RemoteActionsModal'
import { Trash2, Rocket, Camera, RefreshCw, Power, FolderPlus, FolderOpen, FolderInput, Usb, FileUp, Terminal } from 'lucide-react'
import { formatRelative } from '../utils/time'

export default function Machines() {
  const qc = useQueryClient()
  const { data: machines = [], isLoading } = useQuery({
    queryKey: ['machines'],
    queryFn: getMachines,
    refetchInterval: 10000,
  })
  const { data: groups = [] } = useQuery({ queryKey: ['machine-groups'], queryFn: getMachineGroups })
  const [selected, setSelected] = useState<string[]>([])
  const [showDeploy, setShowDeploy] = useState(false)
  const [deployMachineIds, setDeployMachineIds] = useState<string[]>([])
  const [remoteAction, setRemoteAction] = useState<{ machineIds: string[]; label: string; mode: 'transfer' | 'command' } | null>(null)
  const [activeGroupId, setActiveGroupId] = useState('all')
  const [newGroupName, setNewGroupName] = useState('')
  const [moveGroupId, setMoveGroupId] = useState('')

  const groupNames = new Map<string, string>(groups.map((group: any) => [group.id, group.name]))
  const visibleMachines = machines.filter((machine: any) => {
    if (activeGroupId === 'all') return true
    if (activeGroupId === 'ungrouped') return !machine.group_id
    return machine.group_id === activeGroupId
  })
  const visibleMachineIds = visibleMachines.map((machine: any) => machine.id)
  const selectedVisibleCount = visibleMachineIds.filter((id: string) => selected.includes(id)).length
  const activeGroup = groups.find((group: any) => group.id === activeGroupId)
  const activeGroupMachineIds = activeGroupId === 'all' || activeGroupId === 'ungrouped'
    ? []
    : machines.filter((machine: any) => machine.group_id === activeGroupId).map((machine: any) => machine.id)

  const deleteM = useMutation({
    mutationFn: deleteMachine,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['machines'] })
      qc.invalidateQueries({ queryKey: ['machine-groups'] })
    },
  })

  const createGroup = useMutation({
    mutationFn: () => createMachineGroup({ name: newGroupName.trim() }),
    onSuccess: () => {
      setNewGroupName('')
      qc.invalidateQueries({ queryKey: ['machine-groups'] })
    },
  })

  const deleteGroup = useMutation({
    mutationFn: deleteMachineGroup,
    onSuccess: () => {
      setActiveGroupId('all')
      qc.invalidateQueries({ queryKey: ['machines'] })
      qc.invalidateQueries({ queryKey: ['machine-groups'] })
    },
  })

  const moveSelected = useMutation({
    mutationFn: () => moveGroupId === 'ungrouped'
      ? ungroupMachines(selected)
      : assignMachinesToGroup(moveGroupId, selected),
    onSuccess: () => {
      setSelected([])
      setMoveGroupId('')
      qc.invalidateQueries({ queryKey: ['machines'] })
      qc.invalidateQueries({ queryKey: ['machine-groups'] })
    },
  })

  const sendJob = useMutation({
    mutationFn: (payload: any) => createJobs(payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['jobs'] })
      qc.invalidateQueries({ queryKey: ['machines'] })
    },
  })

  const toggleSelect = (id: string) =>
    setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]))

  const bulkCapture = () =>
    sendJob.mutate({ type: 'capture', machine_ids: selected, capture_name: `capture-${Date.now()}` })

  const bulkPushWinPE = () => sendJob.mutate({ type: 'push_winpe', machine_ids: selected })
  const bulkReboot = () => sendJob.mutate({ type: 'reboot', machine_ids: selected })
  const bulkShutdown = () => sendJob.mutate({ type: 'shutdown', machine_ids: selected })
  const queueAgentUpdate = (machineId: string) => sendJob.mutate({ type: 'update_agent', machine_ids: [machineId] })
  const openDeploy = (machineIds: string[]) => {
    setDeployMachineIds(machineIds)
    setShowDeploy(true)
  }
  const openRemoteAction = (machineIds: string[], label: string, mode: 'transfer' | 'command') => {
    setRemoteAction({ machineIds, label, mode })
  }

  if (isLoading) return <div className="p-6 text-gray-400">Loading…</div>

  return (
    <div className="p-6">
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-2xl font-bold">Machines ({machines.length})</h1>
        {selected.length > 0 && (
          <div className="flex gap-2 flex-wrap justify-end">
            <select
              className="bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm"
              value={moveGroupId}
              onChange={(e) => setMoveGroupId(e.target.value)}
            >
              <option value="">Move selected...</option>
              <option value="ungrouped">No group</option>
              {groups.map((group: any) => (
                <option key={group.id} value={group.id}>{group.name}</option>
              ))}
            </select>
            <button
              onClick={() => moveSelected.mutate()}
              disabled={!moveGroupId || moveSelected.isPending}
              className="flex items-center gap-1.5 px-3 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg text-sm font-medium disabled:opacity-50 transition"
            >
              <FolderInput size={14} /> Move
            </button>
            <button
              onClick={() => openDeploy(selected)}
              className="flex items-center gap-1.5 px-3 py-2 bg-blue-600 hover:bg-blue-500 rounded-lg text-sm font-medium"
            >
              <Rocket size={14} /> Deploy ({selected.length})
            </button>
            <button
              onClick={bulkCapture}
              className="flex items-center gap-1.5 px-3 py-2 bg-purple-600 hover:bg-purple-500 rounded-lg text-sm font-medium"
            >
              <Camera size={14} /> Capture
            </button>
            <button
              onClick={bulkPushWinPE}
              disabled={sendJob.isPending}
              className="flex items-center gap-1.5 px-3 py-2 bg-cyan-700 hover:bg-cyan-600 rounded-lg text-sm font-medium disabled:opacity-50 transition"
            >
              <Usb size={14} /> Push WinPE
            </button>
            <button
              onClick={() => openRemoteAction(selected, 'Selected machines', 'transfer')}
              className="flex items-center gap-1.5 px-3 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg text-sm font-medium"
            >
              <FileUp size={14} /> Transfer
            </button>
            <button
              onClick={() => openRemoteAction(selected, 'Selected machines', 'command')}
              className="flex items-center gap-1.5 px-3 py-2 bg-emerald-700 hover:bg-emerald-600 rounded-lg text-sm font-medium"
            >
              <Terminal size={14} /> Command
            </button>
            <button
              onClick={bulkReboot}
              className="flex items-center gap-1.5 px-3 py-2 bg-yellow-600 hover:bg-yellow-500 rounded-lg text-sm font-medium"
            >
              <RefreshCw size={14} /> Reboot
            </button>
            <button
              onClick={bulkShutdown}
              className="flex items-center gap-1.5 px-3 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg text-sm"
            >
              <Power size={14} /> Shutdown
            </button>
          </div>
        )}
      </div>

      {/* Main Content Layout: Groups Sidebar + Machines Table */}
      <div className="grid lg:grid-cols-[260px_1fr] gap-6 items-start">
        {/* Machine Groups Sidebar */}
        <div className="bg-gray-900 rounded-xl border border-gray-800 p-4">
          <div className="flex items-center gap-2 mb-3">
            <FolderOpen size={16} className="text-blue-400" />
            <h2 className="font-semibold text-sm">Machine Groups</h2>
          </div>
          <form
            className="flex gap-2 mb-4"
            onSubmit={(e) => {
              e.preventDefault()
              if (newGroupName.trim()) createGroup.mutate()
            }}
          >
            <input
              value={newGroupName}
              onChange={(e) => setNewGroupName(e.target.value)}
              placeholder="New group name"
              className="min-w-0 flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm"
            />
            <button
              type="submit"
              disabled={!newGroupName.trim() || createGroup.isPending}
              className="inline-flex items-center justify-center p-2 rounded-lg bg-blue-600 hover:bg-blue-500 disabled:opacity-50 transition"
              title="Create group"
            >
              <FolderPlus size={16} />
            </button>
          </form>
          <div className="space-y-1">
            <button
              onClick={() => setActiveGroupId('all')}
              className={`w-full flex items-center justify-between rounded-md px-3 py-2 text-sm transition ${activeGroupId === 'all' ? 'bg-blue-600 text-white' : 'text-gray-300 hover:bg-gray-800'}`}
            >
              <span>All machines</span>
              <span className="text-xs opacity-75">{machines.length}</span>
            </button>
            <button
              onClick={() => setActiveGroupId('ungrouped')}
              className={`w-full flex items-center justify-between rounded-md px-3 py-2 text-sm transition ${activeGroupId === 'ungrouped' ? 'bg-blue-600 text-white' : 'text-gray-300 hover:bg-gray-800'}`}
            >
              <span>No group</span>
              <span className="text-xs opacity-75">{machines.filter((machine: any) => !machine.group_id).length}</span>
            </button>
            {groups.map((group: any) => (
              <div
                key={group.id}
                className={`flex items-center justify-between rounded-md px-3 py-2 text-sm transition ${activeGroupId === group.id ? 'bg-blue-600 text-white' : 'text-gray-300 hover:bg-gray-800'}`}
              >
                <button
                  onClick={() => setActiveGroupId(group.id)}
                  className="flex-1 text-left truncate flex items-center justify-between min-w-0"
                >
                  <span className="truncate">{group.name}</span>
                  <span className="text-xs opacity-75 ml-2">{group.machine_count}</span>
                </button>
                <button
                  onClick={(e) => {
                    e.stopPropagation()
                    deleteGroup.mutate(group.id)
                  }}
                  className={`ml-2 p-1 rounded transition ${activeGroupId === group.id ? 'text-white/80 hover:text-white' : 'text-gray-500 hover:text-red-400'}`}
                  title={`Delete group ${group.name}`}
                >
                  <Trash2 size={13} />
                </button>
              </div>
            ))}
          </div>
        </div>

        {/* Table Area */}
        <div className="space-y-3 min-w-0">
          <div className="flex items-center justify-between px-1">
            <div className="text-sm text-gray-400">
              Viewing: <span className="font-semibold text-white">{activeGroupId === 'all' ? 'All machines' : activeGroupId === 'ungrouped' ? 'No group' : activeGroup?.name}</span>
              <span className="ml-1.5 text-xs text-gray-500">({visibleMachines.length} {visibleMachines.length === 1 ? 'machine' : 'machines'})</span>
            </div>
            {activeGroup && activeGroupMachineIds.length > 0 && (
              <button
                onClick={() => {
                  setSelected(Array.from(new Set([...selected, ...activeGroupMachineIds])))
                }}
                className="text-xs text-blue-400 hover:text-blue-300 transition"
              >
                Select all in group
              </button>
            )}
          </div>

          <div className="bg-gray-900 rounded-xl border border-gray-800 overflow-hidden">
        <table className="w-full text-sm">
          <thead className="text-gray-400 border-b border-gray-800">
            <tr>
              <th className="px-4 py-3 w-8">
                <input
                  type="checkbox"
                  onChange={(e) => {
                    const visible = new Set(visibleMachineIds)
                    setSelected((current) => e.target.checked
                      ? Array.from(new Set([...current, ...visibleMachineIds]))
                      : current.filter((id) => !visible.has(id)))
                  }}
                  checked={visibleMachines.length > 0 && selectedVisibleCount === visibleMachines.length}
                />
              </th>
              <th className="text-left px-4 py-3">Hostname</th>
              <th className="text-left px-4 py-3">Group</th>
              <th className="text-left px-4 py-3">IP</th>
              <th className="text-left px-4 py-3">OS</th>
              <th className="text-left px-4 py-3">Agent</th>
              <th className="text-left px-4 py-3">Status</th>
              <th className="text-left px-4 py-3">Last Seen</th>
              <th className="px-4 py-3"></th>
            </tr>
          </thead>
          <tbody>
            {visibleMachines.map((m: any) => (
              <tr key={m.id} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                <td className="px-4 py-3">
                  <input
                    type="checkbox"
                    checked={selected.includes(m.id)}
                    onChange={() => toggleSelect(m.id)}
                  />
                </td>
                <td className="px-4 py-3 font-medium">
                  <Link to={`/machines/${m.id}`} className="text-blue-400 hover:underline">
                    {m.hostname}
                  </Link>
                </td>
                <td className="px-4 py-3 text-gray-400 text-xs">{groupNames.get(m.group_id) || 'No group'}</td>
                <td className="px-4 py-3 text-gray-300">{m.ip_address}</td>
                <td className="px-4 py-3 text-gray-400 text-xs">{m.os_version ?? '—'}</td>
                <td className="px-4 py-3">
                  <div className="flex flex-col gap-1">
                    <span className="text-xs text-gray-300">{m.agent_version ?? 'Unknown'}</span>
                    {m.agent_update_available ? (
                      <button
                        type="button"
                        onClick={() => queueAgentUpdate(m.id)}
                        disabled={sendJob.isPending || m.status !== 'online'}
                        className="w-fit rounded border border-yellow-500/50 px-2 py-0.5 text-[11px] font-medium text-yellow-300 hover:bg-yellow-500/10 disabled:opacity-50"
                        title="The server automatically queues this update when the agent checks in"
                      >
                        Update needed
                      </button>
                    ) : (
                      <span className="w-fit rounded border border-emerald-500/40 px-2 py-0.5 text-[11px] font-medium text-emerald-300">
                        Latest
                      </span>
                    )}
                  </div>
                </td>
                <td className="px-4 py-3"><StatusBadge status={m.status} /></td>
                <td className="px-4 py-3 text-gray-500 text-xs">
                  {m.last_seen ? formatRelative(m.last_seen) : '—'}
                </td>
                <td className="px-4 py-3">
                  <button
                    onClick={() => deleteM.mutate(m.id)}
                    className="text-gray-600 hover:text-red-400 transition"
                  >
                    <Trash2 size={14} />
                  </button>
                </td>
              </tr>
            ))}
            {visibleMachines.length === 0 && (
              <tr>
                <td colSpan={9} className="px-4 py-8 text-center text-gray-500">
                  {machines.length === 0 ? 'No machines registered yet. Install the agent on a Windows machine to get started.' : 'No machines in this group.'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
        </div>
      </div>

      {showDeploy && <DeployWizard machineIds={deployMachineIds} onClose={() => setShowDeploy(false)} />}
      {remoteAction && (
        <RemoteActionsModal
          machineIds={remoteAction.machineIds}
          targetLabel={remoteAction.label}
          initialMode={remoteAction.mode}
          onClose={() => setRemoteAction(null)}
        />
      )}
    </div>
  )
}
