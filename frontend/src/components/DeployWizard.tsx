import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { getImages, createJobs } from '../api/client'
import { X, Rocket, Network, Trash2 } from 'lucide-react'
import { datetimeLocalToUtcIso } from '../utils/time'

interface Props {
  machineIds: string[]
  onClose: () => void
}

export default function DeployWizard({ machineIds, onClose }: Props) {
  const qc = useQueryClient()
  const { data: images = [] } = useQuery({ queryKey: ['images'], queryFn: getImages })
  const [selectedImage, setSelectedImage] = useState('')
  const [scheduleAt, setScheduleAt] = useState('')
  const [removeWinPE, setRemoveWinPE] = useState(false)
  const [restoreHostname, setRestoreHostname] = useState(false)
  const [joinDomain, setJoinDomain] = useState(false)
  const [domainName, setDomainName] = useState('')
  const [domainUsername, setDomainUsername] = useState('')
  const [domainPassword, setDomainPassword] = useState('')

  const deploy = useMutation({
    mutationFn: () =>
      createJobs({
        type: 'deploy',
        machine_ids: machineIds,
        image_id: selectedImage,
        scheduled_at: datetimeLocalToUtcIso(scheduleAt),
        remove_winpe_after_deploy: removeWinPE,
        restore_hostname_after_deploy: restoreHostname,
        domain_name: joinDomain ? domainName.trim() : null,
        domain_username: joinDomain ? domainUsername.trim() : null,
        domain_password: joinDomain ? domainPassword : null,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['jobs'] })
      onClose()
    },
  })

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50">
      <div className="bg-gray-900 rounded-xl border border-gray-700 p-6 w-full max-w-lg max-h-[calc(100vh-2rem)] overflow-y-auto shadow-2xl">
        <div className="flex justify-between items-center mb-4">
          <h2 className="text-lg font-semibold flex items-center gap-2">
            <Rocket size={18} className="text-blue-400" /> Deploy Image
          </h2>
          <button onClick={onClose}><X size={18} className="text-gray-400 hover:text-white" /></button>
        </div>

        <p className="text-sm text-gray-400 mb-4">
          Deploying to <strong className="text-white">{machineIds.length}</strong> machine(s)
        </p>

        <label className="block text-sm font-medium text-gray-300 mb-1">Select Image</label>
        <select
          className="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm mb-4"
          value={selectedImage}
          onChange={(e) => setSelectedImage(e.target.value)}
        >
          <option value="">-- choose image --</option>
          {images.map((img: any) => (
            <option key={img.id} value={img.id}>
              {img.name} ({(img.size_bytes / 1e9).toFixed(1)} GB) — {img.os_version ?? 'Unknown OS'}
            </option>
          ))}
        </select>

        <label className="block text-sm font-medium text-gray-300 mb-1">Schedule (optional)</label>
        <input
          type="datetime-local"
          className="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm mb-6"
          value={scheduleAt}
          onChange={(e) => setScheduleAt(e.target.value)}
        />

        <div className="space-y-3 border-y border-gray-800 py-4 my-4">
          <label className="flex items-center gap-3 cursor-pointer">
            <input
              type="checkbox"
              checked={restoreHostname}
              onChange={(e) => setRestoreHostname(e.target.checked)}
              className="h-4 w-4"
            />
            <span className="text-sm text-gray-200">Restore each machine's current name after imaging</span>
          </label>
          <label className="flex items-center gap-3 cursor-pointer">
            <input
              type="checkbox"
              checked={removeWinPE}
              onChange={(e) => setRemoveWinPE(e.target.checked)}
              className="h-4 w-4"
            />
            <Trash2 size={15} className="text-gray-400" />
            <span className="text-sm text-gray-200">Remove managed WinPE after imaging</span>
          </label>
          <label className="flex items-center gap-3 cursor-pointer">
            <input
              type="checkbox"
              checked={joinDomain}
              onChange={(e) => setJoinDomain(e.target.checked)}
              className="h-4 w-4"
            />
            <Network size={15} className="text-gray-400" />
            <span className="text-sm text-gray-200">Join a Windows domain after imaging</span>
          </label>
        </div>

        {joinDomain && (
          <div className="space-y-3 mb-5">
            <input
              type="text"
              required
              placeholder="Domain (for example, corp.example.com)"
              className="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm"
              value={domainName}
              onChange={(e) => setDomainName(e.target.value)}
            />
            <input
              type="text"
              required
              placeholder="Domain join username"
              className="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm"
              value={domainUsername}
              onChange={(e) => setDomainUsername(e.target.value)}
            />
            <input
              type="password"
              required
              placeholder="Domain join password"
              className="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm"
              value={domainPassword}
              onChange={(e) => setDomainPassword(e.target.value)}
            />
          </div>
        )}

        <div className="flex gap-3">
          <button
            onClick={onClose}
            className="flex-1 py-2 rounded-lg border border-gray-600 text-sm hover:bg-gray-800 transition"
          >
            Cancel
          </button>
          <button
            onClick={() => deploy.mutate()}
            disabled={!selectedImage || deploy.isPending || (joinDomain && (!domainName.trim() || !domainUsername.trim() || !domainPassword))}
            className="flex-1 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 text-sm font-medium disabled:opacity-50 transition"
          >
            {deploy.isPending ? 'Deploying…' : 'Deploy'}
          </button>
        </div>
      </div>
    </div>
  )
}
