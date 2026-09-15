import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  deleteWinPEDriver,
  deleteWinPEAsset,
  getWinPEAssetsStatus,
  getWinPEConfig,
  updateWinPEConfig,
  uploadWinPEAsset,
  uploadWinPEDriver,
} from '../api/client'
import { AlertTriangle, CheckCircle, FileArchive, FileCog, RefreshCw, Save, Server, Trash2, UploadCloud } from 'lucide-react'

const WINPE_FILES = ['boot.wim', 'boot.sdi', 'efisys.bin', 'etfsboot.com'] as const

function acceptForAsset(filename: string) {
  if (filename === 'boot.wim') return '.wim'
  if (filename === 'boot.sdi') return '.sdi'
  if (filename === 'etfsboot.com') return '.com'
  return '.bin'
}

function formatBytes(value: number) {
  if (!value) return 'Missing'
  if (value > 1024 * 1024 * 1024) return `${(value / (1024 * 1024 * 1024)).toFixed(1)} GB`
  if (value > 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`
  return `${Math.max(1, Math.round(value / 1024))} KB`
}

function formatUpdated(value: number | null | undefined) {
  if (!value) return 'Not uploaded'
  return new Intl.DateTimeFormat(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(new Date(value * 1000))
}

export default function WinPEFiles() {
  const qc = useQueryClient()
  const refs = {
    'boot.wim': useRef<HTMLInputElement>(null),
    'boot.sdi': useRef<HTMLInputElement>(null),
    'efisys.bin': useRef<HTMLInputElement>(null),
    'etfsboot.com': useRef<HTMLInputElement>(null),
  }
  const driverRef = useRef<HTMLInputElement>(null)
  const [bootDescription, setBootDescription] = useState('')
  const [directCaptureEnabled, setDirectCaptureEnabled] = useState(false)
  const [directCaptureShare, setDirectCaptureShare] = useState('')
  const [directCaptureUsername, setDirectCaptureUsername] = useState('')
  const [directCapturePassword, setDirectCapturePassword] = useState('')
  const [directCaptureDrive, setDirectCaptureDrive] = useState('Z:')
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const { data: status, isLoading } = useQuery({
    queryKey: ['winpe-assets'],
    queryFn: getWinPEAssetsStatus,
  })
  const { data: config } = useQuery({
    queryKey: ['winpe-config'],
    queryFn: getWinPEConfig,
  })

  useEffect(() => {
    if (!config) return
    if (config.boot_description) setBootDescription(config.boot_description)
    setDirectCaptureEnabled(String(config.direct_capture_enabled || '').toLowerCase() === 'true')
    setDirectCaptureShare(config.direct_capture_share || '')
    setDirectCaptureUsername(config.direct_capture_username || '')
    setDirectCapturePassword(config.direct_capture_password || '')
    setDirectCaptureDrive(config.direct_capture_drive || 'Z:')
  }, [config])

  const uploadAsset = useMutation({
    mutationFn: ({ filename, file }: { filename: string; file: File }) => uploadWinPEAsset(filename, file),
    onSuccess: (_data, variables) => {
      setError('')
      setMessage(`${variables.filename} uploaded`)
      qc.invalidateQueries({ queryKey: ['winpe-assets'] })
    },
    onError: (e: any) => {
      setMessage('')
      setError(e?.response?.data?.detail ?? 'Upload failed')
    },
  })

  const removeAsset = useMutation({
    mutationFn: deleteWinPEAsset,
    onSuccess: (_data, filename) => {
      setError('')
      setMessage(`${filename} removed`)
      qc.invalidateQueries({ queryKey: ['winpe-assets'] })
    },
    onError: (e: any) => {
      setMessage('')
      setError(e?.response?.data?.detail ?? 'Remove failed')
    },
  })

  const uploadDriver = useMutation({
    mutationFn: uploadWinPEDriver,
    onSuccess: () => {
      setError('')
      setMessage('WinPE driver uploaded')
      qc.invalidateQueries({ queryKey: ['winpe-assets'] })
    },
    onError: (e: any) => {
      setMessage('')
      setError(e?.response?.data?.detail ?? 'Driver upload failed')
    },
  })

  const removeDriver = useMutation({
    mutationFn: deleteWinPEDriver,
    onSuccess: (_data, filename) => {
      setError('')
      setMessage(`${filename} removed`)
      qc.invalidateQueries({ queryKey: ['winpe-assets'] })
    },
    onError: (e: any) => {
      setMessage('')
      setError(e?.response?.data?.detail ?? 'Driver remove failed')
    },
  })

  const saveConfig = useMutation({
    mutationFn: () => updateWinPEConfig({
      boot_description: bootDescription,
      direct_capture_enabled: directCaptureEnabled,
      direct_capture_share: directCaptureShare,
      direct_capture_username: directCaptureUsername,
      direct_capture_password: directCapturePassword,
      direct_capture_drive: directCaptureDrive,
    }),
    onSuccess: () => {
      setError('')
      setMessage('WinPE settings saved')
      qc.invalidateQueries({ queryKey: ['winpe-config'] })
      qc.invalidateQueries({ queryKey: ['winpe-assets'] })
    },
    onError: (e: any) => {
      setMessage('')
      setError(e?.response?.data?.detail ?? 'Save failed')
    },
  })

  const assets = status?.assets ?? []
  const drivers = status?.drivers ?? []
  const ready = Boolean(status?.ready)
  const currentServerShare = `\\\\${window.location.hostname}\\BretterIMGImages`

  if (isLoading) return <div className="p-6 text-gray-400">Loading...</div>

  return (
    <div className="p-6 max-w-4xl">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-6">
        <div>
          <h1 className="text-2xl font-bold">WinPE Files</h1>
          <p className="text-sm text-gray-400 mt-1">
            Manage the boot assets that Push WinPE sends to agent machines.
          </p>
        </div>
        <div className={`inline-flex items-center gap-2 rounded-lg px-3 py-2 text-sm border ${
          ready
            ? 'bg-green-500/10 border-green-500/30 text-green-300'
            : 'bg-yellow-500/10 border-yellow-500/30 text-yellow-300'
        }`}>
          {ready ? <CheckCircle size={16} /> : <AlertTriangle size={16} />}
          {ready ? 'Ready' : 'Missing files'}
        </div>
      </div>

      {(message || error) && (
        <div className={`mb-5 rounded-lg border px-4 py-2 text-sm ${
          error
            ? 'bg-red-500/10 border-red-500/30 text-red-300'
            : 'bg-green-500/10 border-green-500/30 text-green-300'
        }`}>
          {error || message}
        </div>
      )}

      <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden mb-6">
        <table className="w-full text-sm">
          <thead className="border-b border-gray-800 text-gray-400">
            <tr>
              <th className="text-left px-4 py-3">File</th>
              <th className="text-left px-4 py-3">Size</th>
              <th className="text-left px-4 py-3">Updated</th>
              <th className="text-left px-4 py-3">SHA-256</th>
              <th className="px-4 py-3"></th>
            </tr>
          </thead>
          <tbody>
            {WINPE_FILES.map((filename) => {
              const asset = assets.find((item: any) => item.filename === filename)
              return (
                <tr key={filename} className="border-b border-gray-800/50">
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2 font-medium">
                      <FileArchive size={16} className={asset?.available ? 'text-blue-400' : 'text-gray-600'} />
                      <span>{filename}</span>
                      {asset?.iso_optional && <span className="text-xs text-gray-500">optional BIOS ISO boot</span>}
                    </div>
                  </td>
                  <td className={asset?.available ? 'px-4 py-3 text-gray-300' : 'px-4 py-3 text-gray-500'}>
                    {formatBytes(asset?.size_bytes || 0)}
                  </td>
                  <td className="px-4 py-3 text-gray-500">{formatUpdated(asset?.updated_at)}</td>
                  <td className="px-4 py-3 max-w-xs">
                    <code className="block truncate text-xs text-gray-500">{asset?.sha256 || '-'}</code>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex justify-end gap-2">
                      <button
                        onClick={() => refs[filename as keyof typeof refs].current?.click()}
                        disabled={uploadAsset.isPending}
                        title={asset?.available ? `Replace ${filename}` : `Upload ${filename}`}
                        className="inline-flex items-center justify-center p-2 rounded-lg bg-gray-800 hover:bg-gray-700 disabled:opacity-50 transition"
                      >
                        {uploadAsset.isPending ? <RefreshCw size={16} className="animate-spin" /> : <UploadCloud size={16} />}
                      </button>
                      <button
                        onClick={() => removeAsset.mutate(filename)}
                        disabled={!asset?.available || removeAsset.isPending}
                        title={`Remove ${filename}`}
                        className="inline-flex items-center justify-center p-2 rounded-lg bg-gray-800 hover:bg-red-900/50 hover:text-red-300 disabled:opacity-40 transition"
                      >
                        <Trash2 size={16} />
                      </button>
                      <input
                        ref={refs[filename as keyof typeof refs]}
                        type="file"
                        accept={acceptForAsset(filename)}
                        className="hidden"
                        onChange={(event) => {
                          const file = event.currentTarget.files?.[0]
                          if (file) uploadAsset.mutate({ filename, file })
                          event.currentTarget.value = ''
                        }}
                      />
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
        <h2 className="font-semibold mb-4">Boot Settings</h2>
        <div className="flex flex-wrap items-end gap-3 mb-5">
          <div className="flex-1 min-w-64">
            <label className="block text-xs text-gray-400 mb-1">BCD Boot Entry Description</label>
            <input
              className="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500"
              value={bootDescription}
              onChange={(event) => setBootDescription(event.target.value)}
              maxLength={80}
              placeholder="Bretter-IMG WinPE"
            />
          </div>
          <button
            onClick={() => saveConfig.mutate()}
            disabled={saveConfig.isPending || !bootDescription.trim()}
            className="inline-flex items-center justify-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-500 rounded-lg text-sm font-medium disabled:opacity-50 transition"
          >
            {saveConfig.isPending ? <RefreshCw size={14} className="animate-spin" /> : <Save size={14} />}
            Save Settings
          </button>
        </div>
        <div className="border-t border-gray-800 pt-5">
          <label className="flex items-center gap-2 text-sm font-medium text-gray-200 mb-4">
            <input
              type="checkbox"
              checked={directCaptureEnabled}
              onChange={(event) => setDirectCaptureEnabled(event.target.checked)}
              className="accent-blue-500"
            />
            Capture WinPE images directly to server share
          </label>
          <div className="grid md:grid-cols-2 gap-4">
            <div className="md:col-span-2">
              <div className="flex items-center justify-between gap-3 mb-1">
                <label className="block text-xs text-gray-400">Server Image Share</label>
                <button
                  type="button"
                  onClick={() => setDirectCaptureShare(currentServerShare)}
                  className="inline-flex items-center gap-1 text-xs text-blue-400 hover:text-blue-300"
                >
                  <Server size={12} />
                  Use Current Server
                </button>
              </div>
              <input
                className="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:border-blue-500"
                value={directCaptureShare}
                onChange={(event) => setDirectCaptureShare(event.target.value)}
                placeholder="\\\\server\\BretterIMGImages"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-400 mb-1">Map Drive</label>
              <input
                className="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:border-blue-500"
                value={directCaptureDrive}
                onChange={(event) => setDirectCaptureDrive(event.target.value)}
                placeholder="Z:"
                maxLength={2}
              />
            </div>
            <div>
              <label className="block text-xs text-gray-400 mb-1">Fallback Username</label>
              <input
                className="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500"
                value={directCaptureUsername}
                onChange={(event) => setDirectCaptureUsername(event.target.value)}
                placeholder="DOMAIN\\user"
              />
            </div>
            <div className="md:col-span-2">
              <label className="block text-xs text-gray-400 mb-1">Fallback Password</label>
              <input
                type="password"
                className="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500"
                value={directCapturePassword}
                onChange={(event) => setDirectCapturePassword(event.target.value)}
                placeholder="Optional"
              />
            </div>
          </div>
        </div>
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-xl p-5 mt-6">
        <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
          <div>
            <h2 className="font-semibold">Storage Drivers</h2>
            <p className="text-xs text-gray-500 mt-1">
              Upload .zip, .cab, or raw .inf/.sys/.cat storage drivers when WinPE cannot see the Windows volume.
            </p>
          </div>
          <button
            onClick={() => driverRef.current?.click()}
            disabled={uploadDriver.isPending}
            className="inline-flex items-center justify-center gap-2 px-4 py-2 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm font-medium disabled:opacity-50 transition"
          >
            {uploadDriver.isPending ? <RefreshCw size={14} className="animate-spin" /> : <UploadCloud size={14} />}
            Upload Driver
          </button>
          <input
            ref={driverRef}
            type="file"
            accept=".zip,.inf,.sys,.cat,.dll,.cab"
            className="hidden"
            onChange={(event) => {
              const file = event.currentTarget.files?.[0]
              if (file) uploadDriver.mutate(file)
              event.currentTarget.value = ''
            }}
          />
        </div>

        {drivers.length > 0 ? (
          <div className="border border-gray-800 rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="border-b border-gray-800 text-gray-400">
                <tr>
                  <th className="text-left px-3 py-2">Driver File</th>
                  <th className="text-left px-3 py-2">Size</th>
                  <th className="text-left px-3 py-2">Updated</th>
                  <th className="px-3 py-2"></th>
                </tr>
              </thead>
              <tbody>
                {drivers.map((driver: any) => (
                  <tr key={driver.filename} className="border-b border-gray-800/50">
                    <td className="px-3 py-2">
                      <div className="flex items-center gap-2">
                        <FileCog size={14} className="text-blue-400" />
                        <span>{driver.filename}</span>
                      </div>
                    </td>
                    <td className="px-3 py-2 text-gray-400">{formatBytes(driver.size_bytes || 0)}</td>
                    <td className="px-3 py-2 text-gray-500">{formatUpdated(driver.updated_at)}</td>
                    <td className="px-3 py-2 text-right">
                      <button
                        onClick={() => removeDriver.mutate(driver.filename)}
                        disabled={removeDriver.isPending}
                        title={`Remove ${driver.filename}`}
                        className="inline-flex items-center justify-center p-2 rounded-lg bg-gray-800 hover:bg-red-900/50 hover:text-red-300 disabled:opacity-40 transition"
                      >
                        <Trash2 size={14} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="rounded-lg border border-gray-800 bg-gray-950 px-4 py-6 text-sm text-gray-500 text-center">
            No WinPE storage drivers uploaded.
          </div>
        )}
      </div>
    </div>
  )
}
