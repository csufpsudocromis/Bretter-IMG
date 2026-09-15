import { useParams, Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { getMachine, getJobs, createJobs, cancelJob, getWinPEAssetsStatus, uploadWinPEAsset } from '../api/client'
import StatusBadge from '../components/StatusBadge'
import JobProgressBar from '../components/JobProgressBar'
import DeployWizard from '../components/DeployWizard'
import CaptureWizard from '../components/CaptureWizard'
import RemoteActionsModal from '../components/RemoteActionsModal'
import { useState } from 'react'
import { ArrowLeft, Rocket, Camera, MonitorDown, RefreshCw, Square, UploadCloud, XCircle, FileUp, Terminal } from 'lucide-react'
import { formatDateTime, formatRelative } from '../utils/time'

export default function MachineDetail() {
  const { id } = useParams<{ id: string }>()
  const qc = useQueryClient()
  const { data: machine, isLoading } = useQuery({
    queryKey: ['machine', id],
    queryFn: () => getMachine(id!),
    refetchInterval: 10000,
  })
  const { data: allJobs = [] } = useQuery({
    queryKey: ['jobs'],
    queryFn: getJobs,
    refetchInterval: 5000,
  })
  const { data: winpeAssets } = useQuery({ queryKey: ['winpe-assets'], queryFn: getWinPEAssetsStatus })
  const [showDeploy, setShowDeploy] = useState(false)
  const [showCapture, setShowCapture] = useState(false)
  const [remoteAction, setRemoteAction] = useState<'transfer' | 'command' | null>(null)

  const machineJobs = allJobs.filter((j: any) => j.machine_id === id)
  const winpeReady = Boolean(winpeAssets?.ready)

  const pushWinPE = useMutation({
    mutationFn: () => createJobs({ type: 'push_winpe', machine_ids: [id!] }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['jobs'] }),
  })

  const updateAgent = useMutation({
    mutationFn: () => createJobs({ type: 'update_agent', machine_ids: [id!] }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['jobs'] })
      qc.invalidateQueries({ queryKey: ['machine', id] })
      qc.invalidateQueries({ queryKey: ['machines'] })
    },
  })

  const cancel = useMutation({
    mutationFn: cancelJob,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['jobs'] }),
  })

  const uploadAsset = useMutation({
    mutationFn: ({ filename, file }: { filename: string; file: File }) => uploadWinPEAsset(filename, file),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['winpe-assets'] }),
  })

  if (isLoading || !machine) return <div className="p-6 text-gray-400">Loading…</div>

  const diskInfo = (() => {
    try { return JSON.parse(machine.disk_info || '[]') } catch { return [] }
  })()

  const formatBytes = (value: number) => {
    if (!value) return 'Missing'
    if (value > 1024 * 1024 * 1024) return `${(value / (1024 * 1024 * 1024)).toFixed(1)} GB`
    return `${Math.max(1, Math.round(value / (1024 * 1024)))} MB`
  }

  return (
    <div className="p-6 max-w-4xl">
      <Link to="/machines" className="flex items-center gap-1 text-gray-400 hover:text-white text-sm mb-4">
        <ArrowLeft size={14} /> Back to Machines
      </Link>

      <div className="flex justify-between items-start mb-6">
        <div>
          <h1 className="text-2xl font-bold">{machine.hostname}</h1>
          <div className="text-gray-400 text-sm mt-1">{machine.id}</div>
        </div>
        <div className="flex gap-2 flex-wrap justify-end">
          <button
            onClick={() => updateAgent.mutate()}
            disabled={updateAgent.isPending}
            title="Push agent update — agent will download latest files from server and restart"
            className="flex items-center gap-1.5 px-3 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg text-sm font-medium disabled:opacity-50 transition"
          >
            <RefreshCw size={14} className={updateAgent.isPending ? 'animate-spin' : ''} />
            {updateAgent.isPending ? 'Updating…' : 'Update Agent'}
          </button>
          <button
            onClick={() => pushWinPE.mutate()}
            disabled={pushWinPE.isPending || !winpeReady}
            title={winpeReady ? 'Copy boot.wim and boot.sdi to this machine and prepare the WinPE boot entry' : 'Upload boot.wim and boot.sdi before pushing WinPE'}
            className="flex items-center gap-1.5 px-3 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg text-sm font-medium disabled:opacity-50 transition"
          >
            <MonitorDown size={14} />
            {pushWinPE.isPending ? 'Pushing…' : 'Push WinPE'}
          </button>
          <button
            onClick={() => setShowDeploy(true)}
            className="flex items-center gap-1.5 px-3 py-2 bg-blue-600 hover:bg-blue-500 rounded-lg text-sm font-medium"
          >
            <Rocket size={14} /> Deploy Image
          </button>
          <button
            onClick={() => setShowCapture(true)}
            className="flex items-center gap-1.5 px-3 py-2 bg-purple-600 hover:bg-purple-500 rounded-lg text-sm font-medium"
          >
            <Camera size={14} /> Capture Image
          </button>
          <button
            onClick={() => setRemoteAction('transfer')}
            className="flex items-center gap-1.5 px-3 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg text-sm font-medium"
          >
            <FileUp size={14} /> Transfer
          </button>
          <button
            onClick={() => setRemoteAction('command')}
            className="flex items-center gap-1.5 px-3 py-2 bg-emerald-700 hover:bg-emerald-600 rounded-lg text-sm font-medium"
          >
            <Terminal size={14} /> Command
          </button>
        </div>
      </div>

      {/* Update Agent / Push WinPE feedback */}
      {updateAgent.isSuccess && (
        <div className="mb-4 bg-blue-500/10 border border-blue-500/30 rounded-lg px-4 py-2 text-sm text-blue-300">
          ✓ Update Agent job queued — agent will download latest files and restart within 30 seconds.
        </div>
      )}
      {pushWinPE.isSuccess && (
        <div className="mb-4 bg-green-500/10 border border-green-500/30 rounded-lg px-4 py-2 text-sm text-green-300">
          ✓ Push WinPE job queued — agent will copy boot.wim and boot.sdi to this machine, customize WinPE, and create the boot entry.
        </div>
      )}

      <div className="bg-gray-900 rounded-xl border border-gray-800 p-4 mb-6">
        <div className="flex items-center justify-between gap-3 mb-3">
          <div>
            <h2 className="font-semibold">WinPE Assets</h2>
            <div className="text-xs text-gray-500 mt-1">
              {winpeReady ? 'Ready for Push WinPE' : 'Upload boot.wim and boot.sdi from the Windows ADK WinPE media.'}
            </div>
          </div>
          <StatusBadge status={winpeReady ? 'online' : 'offline'} />
        </div>
        <div className="grid sm:grid-cols-2 gap-3">
          {['boot.wim', 'boot.sdi'].map((filename) => {
            const asset = winpeAssets?.assets?.find((item: any) => item.filename === filename)
            return (
              <div key={filename} className="flex items-center justify-between gap-3 bg-gray-950 rounded-lg border border-gray-800 px-3 py-2">
                <div>
                  <div className="text-sm font-medium">{filename}</div>
                  <div className={asset?.available ? 'text-xs text-green-400' : 'text-xs text-gray-500'}>
                    {formatBytes(asset?.size_bytes || 0)}
                  </div>
                </div>
                <label
                  className="inline-flex items-center justify-center p-2 rounded-lg bg-gray-800 hover:bg-gray-700 cursor-pointer transition"
                  title={`Upload ${filename}`}
                >
                  <UploadCloud size={16} />
                  <input
                    type="file"
                    className="hidden"
                    disabled={uploadAsset.isPending}
                    onChange={(event) => {
                      const file = event.currentTarget.files?.[0]
                      if (file) uploadAsset.mutate({ filename, file })
                      event.currentTarget.value = ''
                    }}
                  />
                </label>
              </div>
            )
          })}
        </div>
      </div>

      {/* Info Grid */}
      <div className="grid grid-cols-2 gap-4 mb-6">
        {[
          ['Status', <StatusBadge status={machine.status} />],
          ['IP Address', machine.ip_address],
          ['MAC Address', machine.mac_address],
          ['OS', machine.os_version],
          ['CPU', machine.cpu],
          ['RAM', machine.ram_gb ? `${machine.ram_gb} GB` : '—'],
          ['Agent Version', machine.agent_version],
          ['Last Seen', machine.last_seen ? formatRelative(machine.last_seen) : '—'],
        ].map(([label, value]) => (
          <div key={label as string} className="bg-gray-900 rounded-lg border border-gray-800 p-4">
            <div className="text-xs text-gray-500 mb-1">{label}</div>
            <div className="text-sm">{value || '—'}</div>
          </div>
        ))}
      </div>

      {/* Disk Info */}
      {diskInfo.length > 0 && (
        <div className="bg-gray-900 rounded-xl border border-gray-800 p-4 mb-6">
          <h3 className="font-semibold mb-3">Disks</h3>
          <div className="space-y-2">
            {diskInfo.map((d: any, i: number) => (
              <div key={i} className="flex justify-between text-sm">
                <span className="text-gray-300">{d.model}</span>
                <span className="text-gray-400">{d.size_gb} GB · {d.interface}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Job History */}
      <h2 className="text-lg font-semibold mb-3">Job History</h2>
      <div className="bg-gray-900 rounded-xl border border-gray-800 overflow-hidden">
        <table className="w-full text-sm">
          <thead className="text-gray-400 border-b border-gray-800">
            <tr>
              <th className="text-left px-4 py-3">Type</th>
              <th className="text-left px-4 py-3">Status</th>
              <th className="text-left px-4 py-3">Progress</th>
              <th className="text-left px-4 py-3">Started</th>
              <th className="text-left px-4 py-3">Log</th>
              <th className="px-4 py-3"></th>
            </tr>
          </thead>
          <tbody>
            {machineJobs.map((j: any) => (
              <tr key={j.id} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                <td className="px-4 py-2 capitalize font-medium">{j.type.replace('_', ' ')}</td>
                <td className="px-4 py-2"><StatusBadge status={j.status} /></td>
                <td className="px-4 py-2"><JobProgressBar job={j} /></td>
                <td className="px-4 py-2 text-gray-500">{formatDateTime(j.started_at)}</td>
                <td className="px-4 py-2 max-w-xs">
                  {j.log ? (
                    <details className="cursor-pointer">
                      <summary className="text-xs text-gray-400 hover:text-white">View log</summary>
                      <pre className="mt-1 text-xs text-gray-300 bg-gray-950 rounded p-2 max-h-32 overflow-auto whitespace-pre-wrap">{j.log}</pre>
                    </details>
                  ) : '—'}
                </td>
                <td className="px-4 py-2 text-right">
                  {['pending', 'sent', 'running'].includes(j.status) && (
                    <button
                      onClick={() => cancel.mutate(j.id)}
                      disabled={cancel.isPending}
                      className="inline-flex items-center justify-center text-gray-600 hover:text-red-400 transition disabled:opacity-40"
                      title={j.status === 'running' ? 'Stop running job' : 'Cancel job'}
                    >
                      {j.status === 'running' ? <Square size={14} /> : <XCircle size={14} />}
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {machineJobs.length === 0 && (
              <tr><td colSpan={6} className="px-4 py-6 text-center text-gray-500">No jobs yet</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {showCapture && (
        <CaptureWizard
          machineId={id!}
          hostname={machine.hostname}
          onClose={() => setShowCapture(false)}
        />
      )}
      {showDeploy && <DeployWizard machineIds={[id!]} onClose={() => setShowDeploy(false)} />}
      {remoteAction && (
        <RemoteActionsModal
          machineIds={[id!]}
          targetLabel={machine.hostname}
          initialMode={remoteAction}
          onClose={() => setRemoteAction(null)}
        />
      )}
    </div>
  )
}
