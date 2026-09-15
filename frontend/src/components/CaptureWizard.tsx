import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { createJobs } from '../api/client'
import { X, Camera, AlertTriangle, FileCog } from 'lucide-react'
import { datetimeLocalToUtcIso } from '../utils/time'

interface Props {
  machineId: string
  hostname: string
  onClose: () => void
}

export default function CaptureWizard({ machineId, hostname, onClose }: Props) {
  const qc = useQueryClient()
  const [imageName, setImageName] = useState(`${hostname}-${new Date().toISOString().slice(0, 10)}`)
  const [scheduleAt, setScheduleAt] = useState('')
  const [winpeConfirmed, setWinpeConfirmed] = useState(false)
  const [sysprepBeforeCapture, setSysprepBeforeCapture] = useState(false)
  const [unattendedFilePath, setUnattendedFilePath] = useState('')

  const capture = useMutation({
    mutationFn: () =>
      createJobs({
        type: 'capture',
        machine_ids: [machineId],
        capture_name: imageName.trim() || `capture-${Date.now()}`,
        capture_method: 'winpe',
        scheduled_at: datetimeLocalToUtcIso(scheduleAt),
        sysprep_before_capture: sysprepBeforeCapture,
        unattended_file_path: unattendedFilePath.trim() || null,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['jobs'] })
      onClose()
    },
  })

  const canSubmit = imageName.trim() && !capture.isPending && winpeConfirmed

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50">
      <div className="bg-gray-900 rounded-xl border border-gray-700 p-6 w-full max-w-lg shadow-2xl">

        {/* Header */}
        <div className="flex justify-between items-center mb-5">
          <h2 className="text-lg font-semibold flex items-center gap-2">
            <Camera size={18} className="text-purple-400" />
            Capture Image
          </h2>
          <button onClick={onClose}>
            <X size={18} className="text-gray-400 hover:text-white" />
          </button>
        </div>

        <p className="text-sm text-gray-400 mb-5">
          Capturing from <strong className="text-white">{hostname}</strong>
        </p>

        {/* Image Name */}
        <label className="block text-sm font-medium text-gray-300 mb-1">Image Name</label>
        <input
          type="text"
          className="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm mb-5 focus:outline-none focus:border-purple-500"
          value={imageName}
          onChange={(e) => setImageName(e.target.value)}
          placeholder="e.g. Win11-Baseline-2024-08"
        />

        <div className="bg-gray-800 border border-gray-700 rounded-xl p-4 mb-4 space-y-3">
            <div className="flex gap-2 text-yellow-300 text-xs font-semibold">
              <AlertTriangle size={14} className="shrink-0 mt-0.5" />
              WinPE Pre-flight Checklist
            </div>
            <div className="text-xs text-gray-300 space-y-1.5">
              <div className="flex gap-2">
                <span className="text-gray-500">1.</span>
                <span>Upload <code className="bg-gray-700 px-1 rounded">boot.wim</code> and <code className="bg-gray-700 px-1 rounded">boot.sdi</code> in the WinPE Assets panel.</span>
              </div>
              <div className="flex gap-2">
                <span className="text-gray-500">2.</span>
                <span>Click <strong className="text-white">Push WinPE</strong> for this machine so the agent creates the local boot entry.</span>
              </div>
              <div className="flex gap-2">
                <span className="text-gray-500">3.</span>
                <span>The target must be able to boot a local WinPE ramdisk from its Windows volume.</span>
              </div>
              <div className="flex gap-2">
                <span className="text-gray-500">4.</span>
                <span>The machine must be able to reach the imaging server after booting into WinPE.</span>
              </div>
            </div>
            <label className="flex items-start gap-2 cursor-pointer mt-1">
              <input
                type="checkbox"
                checked={winpeConfirmed}
                onChange={(e) => setWinpeConfirmed(e.target.checked)}
                className="mt-0.5 accent-blue-500"
              />
              <span className="text-xs text-gray-300">
                I confirm Push WinPE has completed successfully on this machine.
                The machine will <strong className="text-white">reboot immediately</strong>.
              </span>
            </label>
        </div>

        <div className="border-y border-gray-800 py-4 mb-5 space-y-3">
          <label className="flex items-start gap-3 cursor-pointer">
            <input
              type="checkbox"
              checked={sysprepBeforeCapture}
              onChange={(e) => setSysprepBeforeCapture(e.target.checked)}
              className="mt-0.5 accent-blue-500"
            />
            <span className="text-sm text-gray-200">
              Sysprep before capture
              <span className="block text-xs text-gray-500 mt-1">Generalizes Windows, then reboots into WinPE for offline capture.</span>
            </span>
          </label>
          <div>
            <label className="flex items-center gap-2 text-sm text-gray-300 mb-1.5">
              <FileCog size={15} className="text-gray-400" />
              Local unattended file (optional)
            </label>
            <input
              type="text"
              disabled={!sysprepBeforeCapture}
              placeholder="C:\Windows\Panther\unattend.xml"
              className="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm disabled:opacity-50"
              value={unattendedFilePath}
              onChange={(e) => setUnattendedFilePath(e.target.value)}
            />
          </div>
        </div>

        {/* Schedule */}
        <label className="block text-sm font-medium text-gray-300 mb-1">Schedule (optional)</label>
        <input
          type="datetime-local"
          className="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm mb-6 focus:outline-none focus:border-purple-500"
          value={scheduleAt}
          onChange={(e) => setScheduleAt(e.target.value)}
        />

        {/* Actions */}
        <div className="flex gap-3">
          <button
            onClick={onClose}
            className="flex-1 py-2 rounded-lg border border-gray-600 text-sm hover:bg-gray-800 transition"
          >
            Cancel
          </button>
          <button
            onClick={() => capture.mutate()}
            disabled={!canSubmit}
            className="flex-1 py-2 rounded-lg text-sm font-medium disabled:opacity-40 transition flex items-center justify-center gap-2 bg-blue-600 hover:bg-blue-500"
          >
            <Camera size={14} />
            {capture.isPending ? 'Queuing…' : 'Capture (WinPE — will reboot)'}
          </button>
        </div>

        {capture.isError && (
          <p className="mt-3 text-xs text-red-400">
            Failed to queue capture. Check server connection.
          </p>
        )}
      </div>
    </div>
  )
}
