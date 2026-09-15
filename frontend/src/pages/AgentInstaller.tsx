import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import api from '../api/client'
import { Download, RefreshCw, AlertCircle, CheckCircle, Package, AlertTriangle } from 'lucide-react'

export default function AgentInstaller() {
  const { data: bundleStatus } = useQuery({
    queryKey: ['bundle-status'],
    queryFn: () => api.get('/installer/bundle-status').then(r => r.data),
    refetchOnWindowFocus: false,
  })

  const configuredApiUrl = import.meta.env.VITE_API_URL
  const defaultUrl = configuredApiUrl?.startsWith('http')
    ? configuredApiUrl.replace(/\/api\/?$/, '')
    : ('https://' + window.location.hostname + ':8000')

  const [serverUrl, setServerUrl] = useState(defaultUrl)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  const handleGenerate = async () => {
    setError('')
    setSuccess('')
    if (!serverUrl.startsWith('http')) {
      setError('Server URL must start with http:// or https://')
      return
    }
    setLoading(true)
    try {
      const resp = await api.post(
        '/installer/generate',
        { server_url: serverUrl },
        { responseType: 'blob' },
      )
      const cd = resp.headers['content-disposition'] || ''
      const match = cd.match(/filename="?([^"]+)"?/)
      const filename = match ? match[1] : 'BretterIMG-Agent.zip'

      const url = URL.createObjectURL(new Blob([resp.data], { type: 'application/zip' }))
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)

      const sizeMb = (resp.data.size / (1024 * 1024)).toFixed(1)
      setSuccess(`Downloaded: ${filename} (${sizeMb} MB) — extract and run install.bat as Administrator`)
    } catch (e: any) {
      setError(e?.response?.data?.detail ?? 'Failed to generate installer')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="p-6 max-w-2xl">
      <h1 className="text-2xl font-bold mb-2">Agent Installer</h1>
      <p className="text-gray-400 text-sm mb-6">
        Generate a self-contained Windows installer with Python and all dependencies bundled.
        No prerequisites needed on the target machine — just extract and run{' '}
        <code className="bg-gray-800 px-1 rounded">install.bat</code> as Administrator.
      </p>

      {/* Bundle Status Banner */}
      {bundleStatus?.available ? (
        <div className="flex items-center gap-3 bg-green-900/20 border border-green-700/50 rounded-xl px-4 py-3 mb-5 text-sm">
          <Package size={16} className="text-green-400 flex-shrink-0" />
          <div>
            <span className="text-green-300 font-medium">Python bundle ready</span>
            <span className="text-green-500 ml-2">
              Python 3.12 + requests + wmi + pywin32 ({bundleStatus.size_mb} MB)
            </span>
          </div>
        </div>
      ) : (
        <div className="flex items-start gap-3 bg-yellow-900/20 border border-yellow-700/50 rounded-xl px-4 py-3 mb-5 text-sm">
          <AlertTriangle size={16} className="text-yellow-400 flex-shrink-0 mt-0.5" />
          <div>
            <span className="text-yellow-300 font-medium">Python bundle not built yet</span>
            <p className="text-yellow-600 mt-0.5">
              Installer will work but requires Python on the target machine. Run{' '}
              <code className="bg-gray-800 px-1 rounded">python3 scripts/build_python_bundle.py</code>{' '}
              on the server to pre-bundle Python.
            </p>
          </div>
        </div>
      )}

      {/* Config Form */}
      <div className="bg-gray-900 border border-gray-700 rounded-xl p-5 mb-6 space-y-4">
        <div>
          <label className="block text-sm font-medium text-gray-300 mb-1">
            Imaging Server URL
          </label>
          <input
            className="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:border-blue-500"
            value={serverUrl}
            onChange={(e) => setServerUrl(e.target.value)}
            placeholder="https://192.168.1.10:8000"
          />
          <p className="text-xs text-gray-500 mt-1">
            Must be reachable from the Windows machines you are imaging.
          </p>
        </div>

        {error && (
          <div className="flex items-center gap-2 bg-red-900/30 border border-red-700 rounded-lg px-3 py-2 text-sm text-red-300">
            <AlertCircle size={14} /> {error}
          </div>
        )}
        {success && (
          <div className="flex items-center gap-2 bg-green-900/30 border border-green-700 rounded-lg px-3 py-2 text-sm text-green-300">
            <CheckCircle size={14} /> {success}
          </div>
        )}

        <button
          onClick={handleGenerate}
          disabled={loading}
          className="flex items-center gap-2 px-5 py-2.5 bg-blue-600 hover:bg-blue-500 rounded-lg text-sm font-medium disabled:opacity-50 transition w-full justify-center"
        >
          {loading
            ? <><RefreshCw size={14} className="animate-spin" /> Building installer…</>
            : <><Download size={14} /> Download Agent Installer (.zip
              {bundleStatus?.available ? ` ~${(bundleStatus.size_mb + 5).toFixed(0)} MB` : ''})
            </>
          }
        </button>

        {bundleStatus?.available && (
          <p className="text-xs text-gray-500 text-center">
            Includes Python 3.12 + all packages — no setup required on the target machine
          </p>
        )}
      </div>

      {/* Installation Steps */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-5 mb-6">
        <h2 className="font-semibold mb-3">Installation Steps</h2>
        <ol className="space-y-3 text-sm text-gray-300">
          {[
            'Click "Download Agent Installer" above',
            'Copy the downloaded ZIP to the target Windows machine',
            'Extract the ZIP',
            'Right-click install.bat → Run as administrator',
            'Machine appears in the Machines page within 30 seconds',
          ].map((text, i) => (
            <li key={i} className="flex items-start gap-3">
              <span className="w-6 h-6 rounded-full bg-blue-600 text-white text-xs flex items-center justify-center flex-shrink-0 mt-0.5">
                {i + 1}
              </span>
              <span>{text}</span>
            </li>
          ))}
        </ol>

        {/* What's included */}
        <div className="mt-5 pt-4 border-t border-gray-800">
          <h3 className="text-sm font-medium text-gray-300 mb-2">What's included in the ZIP</h3>
          <div className="grid grid-cols-2 gap-1 text-xs text-gray-400">
            {[
              ['python/', 'Python 3.12 embeddable runtime'],
              ['python/Lib/site-packages/', 'requests, wmi, pywin32, certifi…'],
              ['agent.py', 'Main polling loop'],
              ['imaging.py', 'DISM capture / deploy / wipe'],
              ['sysinfo.py', 'Hardware inventory (WMI)'],
              ['config.py', `Pre-configured for ${serverUrl}`],
              ['install.bat', 'One-click installer (run as Admin)'],
              ['README.txt', 'Quick-start guide'],
            ].map(([file, desc]) => (
              <div key={file} className="flex gap-2">
                <code className="text-blue-400 flex-shrink-0">{file}</code>
                <span className="text-gray-500">{desc}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

    </div>
  )
}
