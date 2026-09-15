import { useEffect, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { FileUp, FolderUp, Play, Terminal, X } from 'lucide-react'
import { createFileTransferJobs, createJobs } from '../api/client'

type Mode = 'transfer' | 'command'

type Props = {
  machineIds: string[]
  targetLabel: string
  initialMode?: Mode
  onClose: () => void
}

export default function RemoteActionsModal({ machineIds, targetLabel, initialMode = 'transfer', onClose }: Props) {
  const qc = useQueryClient()
  const fileInput = useRef<HTMLInputElement>(null)
  const folderInput = useRef<HTMLInputElement>(null)
  const [mode, setMode] = useState<Mode>(initialMode)
  const [targetPath, setTargetPath] = useState('C:\\Temp')
  const [files, setFiles] = useState<File[]>([])
  const [shell, setShell] = useState<'cmd' | 'powershell'>('powershell')
  const [command, setCommand] = useState('')
  const [timeout, setTimeoutValue] = useState(60)
  const [interactive, setInteractive] = useState(false)
  const [message, setMessage] = useState('')

  useEffect(() => {
    if (!folderInput.current) return
    folderInput.current.setAttribute('webkitdirectory', '')
    folderInput.current.setAttribute('directory', '')
  }, [])

  const onQueued = (text: string) => {
    setMessage(text)
    qc.invalidateQueries({ queryKey: ['jobs'] })
    qc.invalidateQueries({ queryKey: ['machines'] })
  }

  const transfer = useMutation({
    mutationFn: () => createFileTransferJobs(machineIds, targetPath.trim(), files),
    onSuccess: (jobs: any[]) => onQueued(`Queued ${jobs.length} file transfer job${jobs.length === 1 ? '' : 's'}.`),
  })

  const runCommand = useMutation({
    mutationFn: () => createJobs({
      type: 'remote_command',
      machine_ids: machineIds,
      command_shell: shell,
      command_text: command,
      command_timeout_seconds: timeout,
      command_interactive: interactive,
    }),
    onSuccess: (jobs: any[]) => onQueued(`Queued ${jobs.length} command job${jobs.length === 1 ? '' : 's'}.`),
  })

  const addFiles = (list: FileList | null) => {
    if (!list) return
    setFiles(Array.from(list))
    setMessage('')
  }

  const canTransfer = machineIds.length > 0 && targetPath.trim() && files.length > 0 && !transfer.isPending
  const canCommand = machineIds.length > 0 && command.trim() && timeout > 0 && !runCommand.isPending

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
      <div className="w-full max-w-2xl rounded-lg border border-gray-800 bg-gray-950 shadow-2xl">
        <div className="flex items-center justify-between border-b border-gray-800 px-5 py-4">
          <div>
            <h2 className="text-lg font-semibold">Remote Actions</h2>
            <div className="text-xs text-gray-500">{targetLabel} · {machineIds.length} target{machineIds.length === 1 ? '' : 's'}</div>
          </div>
          <button onClick={onClose} className="rounded-md p-2 text-gray-400 hover:bg-gray-800 hover:text-white" title="Close">
            <X size={18} />
          </button>
        </div>

        <div className="flex border-b border-gray-800">
          <button
            onClick={() => setMode('transfer')}
            className={`flex items-center gap-2 px-5 py-3 text-sm ${mode === 'transfer' ? 'bg-gray-900 text-white' : 'text-gray-400 hover:text-white'}`}
          >
            <FileUp size={16} /> Transfer Files
          </button>
          <button
            onClick={() => setMode('command')}
            className={`flex items-center gap-2 px-5 py-3 text-sm ${mode === 'command' ? 'bg-gray-900 text-white' : 'text-gray-400 hover:text-white'}`}
          >
            <Terminal size={16} /> Run Command
          </button>
        </div>

        <div className="space-y-4 px-5 py-5">
          {mode === 'transfer' ? (
            <>
              <label className="block">
                <span className="mb-1 block text-xs text-gray-400">Target directory</span>
                <input
                  value={targetPath}
                  onChange={(event) => setTargetPath(event.target.value)}
                  className="w-full rounded-lg border border-gray-700 bg-gray-900 px-3 py-2 text-sm"
                  placeholder="C:\\Temp"
                />
              </label>

              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => fileInput.current?.click()}
                  className="inline-flex items-center gap-2 rounded-lg bg-gray-800 px-3 py-2 text-sm hover:bg-gray-700"
                >
                  <FileUp size={15} /> Choose Files
                </button>
                <button
                  type="button"
                  onClick={() => folderInput.current?.click()}
                  className="inline-flex items-center gap-2 rounded-lg bg-gray-800 px-3 py-2 text-sm hover:bg-gray-700"
                >
                  <FolderUp size={15} /> Choose Folder
                </button>
                <input ref={fileInput} type="file" multiple className="hidden" onChange={(event) => addFiles(event.currentTarget.files)} />
                <input
                  ref={folderInput}
                  type="file"
                  multiple
                  className="hidden"
                  onChange={(event) => addFiles(event.currentTarget.files)}
                />
              </div>

              <div className="max-h-40 overflow-auto rounded-lg border border-gray-800 bg-gray-900 p-3 text-xs text-gray-300">
                {files.length > 0 ? files.map((file) => (
                  <div key={`${file.name}-${file.size}-${(file as any).webkitRelativePath || ''}`} className="truncate">
                    {(file as any).webkitRelativePath || file.name}
                  </div>
                )) : <span className="text-gray-500">No files selected.</span>}
              </div>

              <button
                onClick={() => transfer.mutate()}
                disabled={!canTransfer}
                className="inline-flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium hover:bg-blue-500 disabled:opacity-50"
              >
                <FileUp size={15} /> {transfer.isPending ? 'Queueing…' : 'Queue Transfer'}
              </button>
            </>
          ) : (
            <>
              <div className="grid gap-3 sm:grid-cols-[160px_1fr]">
                <label className="block">
                  <span className="mb-1 block text-xs text-gray-400">Shell</span>
                  <select
                    value={shell}
                    onChange={(event) => setShell(event.target.value as 'cmd' | 'powershell')}
                    className="w-full rounded-lg border border-gray-700 bg-gray-900 px-3 py-2 text-sm"
                  >
                    <option value="powershell">PowerShell</option>
                    <option value="cmd">CMD</option>
                  </select>
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs text-gray-400">Timeout seconds</span>
                  <input
                    type="number"
                    min={1}
                    max={86400}
                    value={timeout}
                    onChange={(event) => setTimeoutValue(Number(event.target.value))}
                    className="w-full rounded-lg border border-gray-700 bg-gray-900 px-3 py-2 text-sm"
                  />
                </label>
              </div>
              <label className="block">
                <span className="mb-1 block text-xs text-gray-400">Command</span>
                <textarea
                  value={command}
                  onChange={(event) => setCommand(event.target.value)}
                  rows={9}
                  className="w-full rounded-lg border border-gray-700 bg-gray-900 px-3 py-2 font-mono text-sm"
                  placeholder={shell === 'powershell' ? 'Get-Service | Select-Object -First 10' : 'ipconfig /all'}
                />
              </label>
              <label className="flex items-start gap-2.5 rounded-lg border border-gray-800 bg-gray-900/60 p-3 cursor-pointer select-none hover:bg-gray-900">
                <input
                  type="checkbox"
                  checked={interactive}
                  onChange={(event) => setInteractive(event.target.checked)}
                  className="mt-0.5 h-4 w-4 rounded border-gray-700 bg-gray-800 text-emerald-600 focus:ring-emerald-500"
                />
                <div>
                  <span className="block text-sm font-medium text-gray-200">Run interactively on user desktop</span>
                  <span className="block text-xs text-gray-400 mt-0.5">
                    Launches process in the active user session (winsta0\default) so GUI apps and windows are visible on the remote monitor, instead of hidden in background Session 0.
                  </span>
                </div>
              </label>
              <button
                onClick={() => runCommand.mutate()}
                disabled={!canCommand}
                className="inline-flex items-center gap-2 rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium hover:bg-emerald-500 disabled:opacity-50"
              >
                <Play size={15} /> {runCommand.isPending ? 'Queueing…' : 'Run Command'}
              </button>
            </>
          )}

          {(message || transfer.error || runCommand.error) && (
            <div className={`rounded-lg border px-3 py-2 text-sm ${message ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300' : 'border-red-500/30 bg-red-500/10 text-red-300'}`}>
              {message || (transfer.error as any)?.response?.data?.detail || (runCommand.error as any)?.response?.data?.detail || 'Action failed.'}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
