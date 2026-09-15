import { useRef, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { getImages, getImageStorage, deleteImage, getImageDownloadUrl, updateImage, uploadImage, scanImageStore } from '../api/client'
import { AlertTriangle, Download, Edit3, HardDrive, RefreshCw, Save, Trash2, Upload, X } from 'lucide-react'
import { formatShort } from '../utils/time'

function formatBytes(n: number) {
  if (n > 1e9) return `${(n / 1e9).toFixed(1)} GB`
  if (n > 1e6) return `${(n / 1e6).toFixed(1)} MB`
  return `${n} B`
}

export default function Images() {
  const qc = useQueryClient()
  const { data: images = [], isLoading } = useQuery({ queryKey: ['images'], queryFn: getImages, refetchInterval: 15000 })
  const { data: storage } = useQuery({
    queryKey: ['image-storage'],
    queryFn: getImageStorage,
    refetchInterval: 30000,
  })
  const fileRef = useRef<HTMLInputElement>(null)
  const [uploading, setUploading] = useState(false)
  const [uploadName, setUploadName] = useState('')
  const [editingImage, setEditingImage] = useState<any | null>(null)
  const [pendingDeleteImage, setPendingDeleteImage] = useState<any | null>(null)
  const [editName, setEditName] = useState('')
  const [editOsVersion, setEditOsVersion] = useState('')
  const [editDescription, setEditDescription] = useState('')
  const storagePercent = storage?.total_bytes
    ? Math.min(100, Math.max(0, Math.round((storage.image_store_used_bytes / storage.total_bytes) * 100)))
    : 0

  const deleteImg = useMutation({
    mutationFn: deleteImage,
    onSuccess: () => {
      setPendingDeleteImage(null)
      qc.invalidateQueries({ queryKey: ['images'] })
      qc.invalidateQueries({ queryKey: ['image-storage'] })
    },
  })

  const saveImage = useMutation({
    mutationFn: () =>
      updateImage(editingImage.id, {
        name: editName.trim(),
        os_version: editOsVersion.trim() || null,
        description: editDescription.trim() || null,
      }),
    onSuccess: () => {
      setEditingImage(null)
      qc.invalidateQueries({ queryKey: ['images'] })
    },
  })

  const openEdit = (img: any) => {
    setEditingImage(img)
    setEditName(img.name || '')
    setEditOsVersion(img.os_version || '')
    setEditDescription(img.description || '')
  }

  const handleUpload = async () => {
    const file = fileRef.current?.files?.[0]
    if (!file || !uploadName) return
    const fd = new FormData()
    fd.append('file', file)
    fd.append('name', uploadName)
    setUploading(true)
    try {
      await uploadImage(fd)
      qc.invalidateQueries({ queryKey: ['images'] })
      qc.invalidateQueries({ queryKey: ['image-storage'] })
      setUploadName('')
      if (fileRef.current) fileRef.current.value = ''
    } finally {
      setUploading(false)
    }
  }

  const scanStore = useMutation({
    mutationFn: scanImageStore,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['images'] })
      qc.invalidateQueries({ queryKey: ['image-storage'] })
    },
  })

  if (isLoading) return <div className="p-6 text-gray-400">Loading…</div>

  return (
    <div className="p-6">
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-2xl font-bold">Image Library ({images.length})</h1>
        <button
          onClick={() => scanStore.mutate()}
          disabled={scanStore.isPending}
          className="flex items-center gap-2 px-3 py-2 bg-gray-800 hover:bg-gray-700 border border-gray-700 rounded-lg text-sm disabled:opacity-50 transition"
          title="Scan image store for unregistered images on disk"
        >
          <RefreshCw size={14} className={scanStore.isPending ? 'animate-spin' : ''} />
          {scanStore.isPending ? 'Scanning…' : 'Scan for new images'}
        </button>
      </div>

      {storage && (
        <div className="bg-gray-900 border border-gray-700 rounded-xl p-4 mb-6">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
            <div className="min-w-0">
              <div className="flex items-center gap-2 font-semibold">
                <HardDrive size={16} className="text-emerald-400" />
                Image Share Storage
              </div>
              <div className="mt-1 truncate text-xs text-gray-500">{storage.path}</div>
            </div>
            <div className="grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-3 lg:w-[520px]">
              <div>
                <div className="text-xs text-gray-500">Capacity</div>
                <div className="text-sm font-medium">{formatBytes(storage.total_bytes)}</div>
              </div>
              <div>
                <div className="text-xs text-gray-500">Images Used</div>
                <div className="text-sm font-medium">{formatBytes(storage.image_store_used_bytes)}</div>
              </div>
              <div>
                <div className="text-xs text-gray-500">Free</div>
                <div className="text-sm font-medium">{formatBytes(storage.free_bytes)}</div>
              </div>
            </div>
          </div>
          <div className="mt-4 flex items-center gap-3">
            <div className="h-2 flex-1 overflow-hidden rounded-sm bg-gray-800">
              <div
                className="h-full rounded-sm bg-emerald-500"
                style={{ width: `${storagePercent}%` }}
              />
            </div>
            <div className="w-12 text-right text-xs tabular-nums text-gray-400">
              {storagePercent}%
            </div>
          </div>
        </div>
      )}

      {/* Upload Panel */}
      <div className="bg-gray-900 border border-gray-700 rounded-xl p-4 mb-6 flex flex-wrap gap-3 items-end">
        <div className="flex-1 min-w-48">
          <label className="block text-xs text-gray-400 mb-1">Image Name</label>
          <input
            className="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm"
            placeholder="e.g. Win11-Base-2025"
            value={uploadName}
            onChange={(e) => setUploadName(e.target.value)}
          />
        </div>
        <div className="flex-1 min-w-48">
          <label className="block text-xs text-gray-400 mb-1">WIM / IMG File</label>
          <input
            ref={fileRef}
            type="file"
            accept=".wim,.img,.esd"
            className="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm file:mr-3 file:py-0.5 file:px-2 file:border-0 file:bg-blue-600 file:text-white file:rounded file:text-xs"
          />
        </div>
        <button
          onClick={handleUpload}
          disabled={uploading || !uploadName}
          className="flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-500 rounded-lg text-sm font-medium disabled:opacity-50"
        >
          <Upload size={14} /> {uploading ? 'Uploading…' : 'Upload'}
        </button>
      </div>

      {/* Image Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {images.map((img: any) => (
          <div key={img.id} className="bg-gray-900 rounded-xl border border-gray-800 p-4">
            <div className="flex justify-between items-start mb-2">
              <div className="flex items-center gap-2">
                <HardDrive size={16} className="text-purple-400" />
                <span className="font-medium">{img.name}</span>
              </div>
              <div className="flex gap-1.5">
                <a
                  href={getImageDownloadUrl(img.id)}
                  download={img.filename || `${img.name}.wim`}
                  className="inline-flex h-8 w-8 items-center justify-center rounded-md text-gray-500 hover:bg-gray-800 hover:text-green-300 transition"
                  title="Download image"
                >
                  <Download size={17} />
                </a>
                <button
                  onClick={() => openEdit(img)}
                  className="inline-flex h-8 w-8 items-center justify-center rounded-md text-gray-500 hover:bg-gray-800 hover:text-blue-300 transition"
                  title="Edit image details"
                >
                  <Edit3 size={17} />
                </button>
                <button
                  onClick={() => setPendingDeleteImage(img)}
                  className="inline-flex h-8 w-8 items-center justify-center rounded-md text-gray-600 hover:bg-red-500/10 hover:text-red-400 transition"
                  title="Delete image"
                >
                  <Trash2 size={17} />
                </button>
              </div>
            </div>
            <div className="text-xs text-gray-400 space-y-1">
              <div>{img.os_version ?? 'Unknown OS'} · {img.architecture}</div>
              <div>{formatBytes(img.size_bytes)}</div>
              <div>Created by {img.created_by ?? 'unknown'}</div>
              {img.tags && <div className="text-blue-400">{img.tags}</div>}
              <div className="text-gray-500">{formatShort(img.created_at)}</div>
            </div>
            {img.description && <p className="text-xs text-gray-500 mt-2">{img.description}</p>}
          </div>
        ))}
        {images.length === 0 && (
          <div className="col-span-3 text-center py-12 text-gray-500">
            No images yet. Upload a WIM file or capture one from a machine.
          </div>
        )}
      </div>

      {editingImage && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
          <div className="bg-gray-900 rounded-xl border border-gray-700 p-6 w-full max-w-lg shadow-2xl">
            <div className="flex items-center justify-between mb-5">
              <h2 className="text-lg font-semibold flex items-center gap-2">
                <Edit3 size={18} className="text-blue-400" /> Edit Image
              </h2>
              <button onClick={() => setEditingImage(null)}>
                <X size={18} className="text-gray-400 hover:text-white" />
              </button>
            </div>

            <div className="space-y-4">
              <div>
                <label className="block text-xs text-gray-400 mb-1">Image Name</label>
                <input
                  className="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500"
                  value={editName}
                  onChange={(e) => setEditName(e.target.value)}
                  placeholder="Image name"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">OS Type</label>
                <input
                  className="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500"
                  value={editOsVersion}
                  onChange={(e) => setEditOsVersion(e.target.value)}
                  placeholder="Windows 11 Pro 23H2"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">Notes</label>
                <textarea
                  className="w-full min-h-28 bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500 resize-y"
                  value={editDescription}
                  onChange={(e) => setEditDescription(e.target.value)}
                  placeholder="Driver pack, apps, intended lab, known issues..."
                />
              </div>
            </div>

            <div className="flex gap-3 mt-6">
              <button
                onClick={() => setEditingImage(null)}
                className="flex-1 py-2 rounded-lg border border-gray-600 text-sm hover:bg-gray-800 transition"
              >
                Cancel
              </button>
              <button
                onClick={() => saveImage.mutate()}
                disabled={!editName.trim() || saveImage.isPending}
                className="flex-1 inline-flex items-center justify-center gap-2 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 text-sm font-medium disabled:opacity-50 transition"
              >
                <Save size={14} />
                {saveImage.isPending ? 'Saving...' : 'Save'}
              </button>
            </div>
          </div>
        </div>
      )}

      {pendingDeleteImage && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
          <div className="bg-gray-900 rounded-xl border border-red-900/70 p-6 w-full max-w-md shadow-2xl">
            <div className="flex items-center justify-between mb-5">
              <h2 className="text-lg font-semibold flex items-center gap-2">
                <AlertTriangle size={18} className="text-red-400" /> Delete Image
              </h2>
              <button
                onClick={() => setPendingDeleteImage(null)}
                disabled={deleteImg.isPending}
              >
                <X size={18} className="text-gray-400 hover:text-white" />
              </button>
            </div>

            <p className="text-sm text-gray-300">
              Delete <span className="font-semibold text-white">{pendingDeleteImage.name}</span>?
            </p>
            <p className="mt-2 text-xs text-gray-500">
              This removes the image from the library and storage.
            </p>

            <div className="flex gap-3 mt-6">
              <button
                onClick={() => setPendingDeleteImage(null)}
                disabled={deleteImg.isPending}
                className="flex-1 py-2 rounded-lg border border-gray-600 text-sm hover:bg-gray-800 disabled:opacity-50 transition"
              >
                Cancel
              </button>
              <button
                onClick={() => deleteImg.mutate(pendingDeleteImage.id)}
                disabled={deleteImg.isPending}
                className="flex-1 inline-flex items-center justify-center gap-2 py-2 rounded-lg bg-red-600 hover:bg-red-500 text-sm font-medium disabled:opacity-50 transition"
              >
                <Trash2 size={14} />
                {deleteImg.isPending ? 'Deleting...' : 'Delete'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
