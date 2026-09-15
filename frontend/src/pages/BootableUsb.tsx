import { useEffect, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import {
  generateBootableUsbPackage,
  getBootableUsbStatus,
} from '../api/client'
import {
  AlertCircle,
  CheckCircle,
  Download,
  Network,
  RefreshCw,
  Server,
  ShieldAlert,
  Usb,
} from 'lucide-react'

export default function BootableUsb() {
  const configuredApiUrl = import.meta.env.VITE_API_URL
  const defaultServerUrl = configuredApiUrl?.startsWith('http')
    ? configuredApiUrl.replace(/\/api\/?$/, '')
    : `${window.location.protocol}//${window.location.hostname}:8000`

  const { data: status, isLoading } = useQuery({
    queryKey: ['bootable-usb-status'],
    queryFn: getBootableUsbStatus,
  })

  const [serverUrl, setServerUrl] = useState(defaultServerUrl)
  const [imageShare, setImageShare] = useState('')
  const [smbUsername, setSmbUsername] = useState('')
  const [smbPassword, setSmbPassword] = useState('')
  const [mapDrive, setMapDrive] = useState('Z:')
  const [success, setSuccess] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    const config = status?.config
    if (!config) return
    setImageShare((current) => current || config.direct_capture_share || `\\\\${window.location.hostname}\\BretterIMGImages`)
    setSmbUsername((current) => current || config.direct_capture_username || '')
    setSmbPassword((current) => current || config.direct_capture_password || '')
    setMapDrive((current) => current || config.direct_capture_drive || 'Z:')
  }, [status])

  const generate = useMutation({
    mutationFn: () => generateBootableUsbPackage({
      server_url: serverUrl,
      image_share: imageShare,
      smb_username: smbUsername,
      smb_password: smbPassword,
      map_drive: mapDrive,
    }),
    onSuccess: (resp) => {
      setError('')
      const cd = resp.headers['content-disposition'] || ''
      const match = cd.match(/filename="?([^"]+)"?/)
      const filename = match ? match[1] : 'BretterIMG-WinPE.iso'
      const url = URL.createObjectURL(new Blob([resp.data], { type: 'application/x-iso9660-image' }))
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
      setSuccess(`Downloaded ${filename}`)
    },
    onError: (e: any) => {
      setSuccess('')
      setError(e?.response?.data?.detail ?? 'Failed to build ISO')
    },
  })

  const canGenerate = imageShare.startsWith('\\\\') && mapDrive.trim() && !generate.isPending

  if (isLoading) return <div className="p-6 text-gray-400">Loading...</div>

  return (
    <div className="p-6 max-w-3xl">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-6">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-3">
            <Usb size={24} className="text-blue-400" />
            Bootable USB
          </h1>
          <p className="text-sm text-gray-400 mt-1">
            Generate and download a standalone Bretter-IMG WinPE ISO.
          </p>
        </div>
      </div>

      {(success || error) && (
        <div className={`mb-5 flex items-center gap-2 rounded-lg border px-4 py-2 text-sm ${
          error
            ? 'border-red-500/30 bg-red-500/10 text-red-300'
            : 'border-green-500/30 bg-green-500/10 text-green-300'
        }`}>
          {error ? <AlertCircle size={15} /> : <CheckCircle size={15} />}
          {error || success}
        </div>
      )}

      <div className="space-y-5">
        <div className="rounded-xl border border-gray-800 bg-gray-900 p-5">
          <h2 className="mb-4 flex items-center gap-2 font-semibold">
            <Server size={17} className="text-blue-400" />
            ISO Settings
          </h2>

          <div className="grid gap-4 md:grid-cols-2">
              <div className="md:col-span-2">
                <label className="mb-1 block text-xs text-gray-400">Imaging Server URL</label>
                <input
                  className="w-full rounded-lg border border-gray-600 bg-gray-800 px-3 py-2 font-mono text-sm focus:outline-none focus:border-blue-500"
                  value={serverUrl}
                  onChange={(event) => setServerUrl(event.target.value)}
                  placeholder="https://192.168.1.10:8000"
                />
              </div>

              <div className="md:col-span-2">
                <div className="mb-1 flex items-center justify-between gap-3">
                  <label className="block text-xs text-gray-400">SMB Image Share</label>
                  <button
                    type="button"
                    onClick={() => setImageShare(`\\\\${window.location.hostname}\\BretterIMGImages`)}
                    className="inline-flex items-center gap-1 text-xs text-blue-400 hover:text-blue-300"
                  >
                    <Network size={12} />
                    Use Current Server
                  </button>
                </div>
                <input
                  className="w-full rounded-lg border border-gray-600 bg-gray-800 px-3 py-2 font-mono text-sm focus:outline-none focus:border-blue-500"
                  value={imageShare}
                  onChange={(event) => setImageShare(event.target.value)}
                  placeholder="\\\\server\\BretterIMGImages"
                />
              </div>

              <div>
                <label className="mb-1 block text-xs text-gray-400">Share Username</label>
                <input
                  className="w-full rounded-lg border border-gray-600 bg-gray-800 px-3 py-2 text-sm focus:outline-none focus:border-blue-500"
                  value={smbUsername}
                  onChange={(event) => setSmbUsername(event.target.value)}
                  placeholder="DOMAIN\\user"
                />
              </div>

              <div>
                <label className="mb-1 block text-xs text-gray-400">Share Password</label>
                <input
                  type="password"
                  className="w-full rounded-lg border border-gray-600 bg-gray-800 px-3 py-2 text-sm focus:outline-none focus:border-blue-500"
                  value={smbPassword}
                  onChange={(event) => setSmbPassword(event.target.value)}
                  placeholder="Optional"
                />
              </div>

              <div className="md:col-span-2">
                <label className="mb-1 block text-xs text-gray-400">Map Drive</label>
                <input
                  className="w-full rounded-lg border border-gray-600 bg-gray-800 px-3 py-2 font-mono text-sm focus:outline-none focus:border-blue-500"
                  value={mapDrive}
                  onChange={(event) => setMapDrive(event.target.value.toUpperCase())}
                  maxLength={2}
                  placeholder="Z:"
                />
              </div>
          </div>

          <button
            onClick={() => generate.mutate()}
            disabled={!canGenerate}
            className="mt-5 inline-flex w-full items-center justify-center gap-2 rounded-lg bg-blue-600 px-5 py-2.5 text-sm font-medium transition hover:bg-blue-500 disabled:opacity-50"
          >
            {generate.isPending
              ? <><RefreshCw size={15} className="animate-spin" /> Building ISO...</>
              : <><Download size={15} /> Download ISO</>
            }
          </button>
        </div>

        <div className="rounded-xl border border-red-900/60 bg-red-950/20 p-5">
          <h2 className="mb-3 flex items-center gap-2 font-semibold text-red-200">
            <ShieldAlert size={17} />
            Deployment Safety
          </h2>
          <div className="grid gap-3 text-sm text-red-100/80 md:grid-cols-2">
            <div className="rounded-lg border border-red-900/60 bg-red-950/30 p-3">Deploy wipes the selected target disk.</div>
            <div className="rounded-lg border border-red-900/60 bg-red-950/30 p-3">Captured WIMs save to the share root.</div>
          </div>
        </div>
      </div>
    </div>
  )
}
