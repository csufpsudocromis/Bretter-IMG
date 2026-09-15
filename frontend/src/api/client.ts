import axios from 'axios'
import { clearSessionAndRedirect, getToken } from '../auth/session'

const api = axios.create({ baseURL: import.meta.env.VITE_API_URL ?? '/api' })

api.interceptors.request.use((config) => {
  const token = getToken()
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

api.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err.response?.status === 401) {
      clearSessionAndRedirect()
    }
    return Promise.reject(err)
  },
)

export default api

// ── Auth ──────────────────────────────────────────────────
export const login = (username: string, password: string) => {
  const form = new URLSearchParams()
  form.append('username', username)
  form.append('password', password)
  return api.post('/auth/login', form, { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } })
}
export const getMe = () => api.get('/auth/me').then((r) => r.data)
export const refreshSession = () => api.post('/auth/refresh')

// ── Users ─────────────────────────────────────────────────
export const getUsers = () => api.get('/users/').then((r) => r.data)
export const createUser = (data: { username: string; password: string; is_admin: boolean }) =>
  api.post('/users/', data).then((r) => r.data)
export const resetUserShareAccess = (id: string) =>
  api.post(`/users/${id}/share-access`).then((r) => r.data)

// ── Machines ─────────────────────────────────────────────
export const getMachines = () => api.get('/machines/').then((r) => r.data)
export const getMachine = (id: string) => api.get(`/machines/${id}`).then((r) => r.data)
export const deleteMachine = (id: string) => api.delete(`/machines/${id}`)
export const updateMachine = (id: string, data: any) =>
  api.patch(`/machines/${id}`, data).then((r) => r.data)

// ── Machine Groups ───────────────────────────────────────
export const getMachineGroups = () => api.get('/machine-groups/').then((r) => r.data)
export const createMachineGroup = (data: { name: string; description?: string }) =>
  api.post('/machine-groups/', data).then((r) => r.data)
export const updateMachineGroup = (id: string, data: { name?: string; description?: string }) =>
  api.patch(`/machine-groups/${id}`, data).then((r) => r.data)
export const deleteMachineGroup = (id: string) => api.delete(`/machine-groups/${id}`)
export const assignMachinesToGroup = (groupId: string, machineIds: string[]) =>
  api.post(`/machine-groups/${groupId}/machines`, { machine_ids: machineIds }).then((r) => r.data)
export const ungroupMachines = (machineIds: string[]) =>
  api.post('/machine-groups/ungroup', { machine_ids: machineIds }).then((r) => r.data)

// ── Images ────────────────────────────────────────────────
export const getImages = () => api.get('/images/').then((r) => r.data)
export const getImageStorage = () => api.get('/images/storage/status').then((r) => r.data)
export const deleteImage = (id: string) => api.delete(`/images/${id}`)
export const updateImage = (id: string, data: any) =>
  api.patch(`/images/${id}`, data).then((r) => r.data)
export const getImageDownloadUrl = (id: string) =>
  `${String(api.defaults.baseURL || '/api').replace(/\/$/, '')}/images/${encodeURIComponent(id)}/download`
export const uploadImage = (formData: FormData) =>
  api.post('/images/upload', formData, { headers: { 'Content-Type': 'multipart/form-data' } })
export const scanImageStore = () => api.post('/images/scan').then((r) => r.data)

// ── Jobs ──────────────────────────────────────────────────
export const getJobs = () => api.get('/jobs/').then((r) => r.data)
export const createJobs = (data: any) => api.post('/jobs/', data).then((r) => r.data)
export const createFileTransferJobs = (machineIds: string[], targetPath: string, files: File[]) => {
  const form = new FormData()
  form.append('machine_ids', JSON.stringify(machineIds))
  form.append('target_path', targetPath)
  form.append('file_paths', JSON.stringify(files.map((file) => (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name)))
  files.forEach((file) => {
    form.append('files', file, file.name)
  })
  return api.post('/jobs/file-transfer', form, { headers: { 'Content-Type': 'multipart/form-data' } }).then((r) => r.data)
}
export const cancelJob = (id: string) => api.delete(`/jobs/${id}`)
export const clearJobHistory = () => api.delete('/jobs/history').then((r) => r.data)

// ── WinPE ────────────────────────────────────────────────
export const getWinPEAssetsStatus = () => api.get('/winpe/assets/status').then((r) => r.data)
export const getWinPEConfig = () => api.get('/winpe/config').then((r) => r.data)
export const getBootableUsbStatus = () => api.get('/winpe/bootable-usb/status').then((r) => r.data)
export const generateBootableUsbPackage = (data: {
  server_url?: string
  image_share: string
  smb_username?: string
  smb_password?: string
  map_drive: string
}) =>
  api.post('/winpe/bootable-usb/package', data, { responseType: 'blob' })
export const updateWinPEConfig = (data: {
  boot_description: string
  direct_capture_enabled?: boolean
  direct_capture_share?: string
  direct_capture_username?: string
  direct_capture_password?: string
  direct_capture_drive?: string
}) =>
  api.patch('/winpe/config', data).then((r) => r.data)
export const uploadWinPEAsset = (filename: string, file: File) => {
  const form = new FormData()
  form.append('file', file)
  return api.post(`/winpe/assets/${filename}`, form, { headers: { 'Content-Type': 'multipart/form-data' } })
}
export const deleteWinPEAsset = (filename: string) => api.delete(`/winpe/assets/${filename}`)
export const uploadWinPEDriver = (file: File) => {
  const form = new FormData()
  form.append('file', file)
  return api.post('/winpe/drivers', form, { headers: { 'Content-Type': 'multipart/form-data' } })
}
export const deleteWinPEDriver = (filename: string) => api.delete(`/winpe/drivers/${filename}`)
