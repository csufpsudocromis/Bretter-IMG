import { useQuery } from '@tanstack/react-query'
import { getMachines, getImages, getJobs } from '../api/client'
import { formatShort } from '../utils/time'
import { Monitor, HardDrive, ClipboardList, AlertTriangle } from 'lucide-react'

function StatCard({ label, value, icon: Icon, color }: any) {
  return (
    <div className="bg-gray-900 rounded-xl border border-gray-800 p-5 flex items-center gap-4">
      <div className={`p-3 rounded-lg ${color}`}>
        <Icon size={22} className="text-white" />
      </div>
      <div>
        <div className="text-2xl font-bold">{value}</div>
        <div className="text-gray-400 text-sm">{label}</div>
      </div>
    </div>
  )
}

export default function Dashboard() {
  const { data: machines = [] } = useQuery({
    queryKey: ['machines'],
    queryFn: getMachines,
    refetchInterval: 10000,
  })
  const { data: images = [] } = useQuery({ queryKey: ['images'], queryFn: getImages })
  const { data: jobs = [] } = useQuery({
    queryKey: ['jobs'],
    queryFn: getJobs,
    refetchInterval: 5000,
  })

  const online = machines.filter((m: any) => m.status === 'online').length
  const activeJobs = jobs.filter((j: any) => ['pending', 'sent', 'running'].includes(j.status)).length
  const errors = machines.filter((m: any) => m.status === 'error').length
  const machineNames = new Map(machines.map((machine: any) => [machine.id, machine.hostname]))
  const machineName = (job: any) => machineNames.get(job.machine_id) || job.original_hostname || `${job.machine_id.slice(0, 8)}...`

  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-6">Dashboard</h1>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
        <StatCard label="Total Machines" value={machines.length} icon={Monitor} color="bg-blue-600" />
        <StatCard label="Online" value={online} icon={Monitor} color="bg-green-600" />
        <StatCard label="Images" value={images.length} icon={HardDrive} color="bg-purple-600" />
        <StatCard label="Active Jobs" value={activeJobs} icon={ClipboardList} color="bg-yellow-600" />
      </div>

      {errors > 0 && (
        <div className="bg-red-900/30 border border-red-700 rounded-xl p-4 flex items-center gap-3 mb-6">
          <AlertTriangle size={18} className="text-red-400" />
          <span className="text-red-300 text-sm">{errors} machine(s) in error state</span>
        </div>
      )}

      {/* Recent Jobs */}
      <h2 className="text-lg font-semibold mb-3">Recent Jobs</h2>
      <div className="bg-gray-900 rounded-xl border border-gray-800 overflow-hidden">
        <table className="w-full text-sm">
          <thead className="text-gray-400 border-b border-gray-800">
            <tr>
              <th className="text-left px-4 py-3">Type</th>
              <th className="text-left px-4 py-3">Machine</th>
              <th className="text-left px-4 py-3">Status</th>
              <th className="text-left px-4 py-3">Started</th>
            </tr>
          </thead>
          <tbody>
            {jobs.slice(0, 10).map((job: any) => (
              <tr key={job.id} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                <td className="px-4 py-2 font-medium capitalize">{job.type}</td>
                <td className="px-4 py-2 text-gray-300">{machineName(job)}</td>
                <td className="px-4 py-2 capitalize">{job.status}</td>
                <td className="px-4 py-2 text-gray-500">{formatShort(job.started_at)}</td>
              </tr>
            ))}
            {jobs.length === 0 && (
              <tr><td colSpan={4} className="px-4 py-6 text-center text-gray-500">No jobs yet</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
