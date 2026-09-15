import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { getJobs, getMachines, cancelJob, clearJobHistory } from '../api/client'
import StatusBadge from '../components/StatusBadge'
import JobProgressBar from '../components/JobProgressBar'
import { XCircle, ChevronDown, ChevronUp, Trash2, Square } from 'lucide-react'
import { formatDateTime, formatShort } from '../utils/time'
import { Fragment, useState } from 'react'

export default function Jobs() {
  const qc = useQueryClient()
  const { data: jobs = [], isLoading } = useQuery({
    queryKey: ['jobs'],
    queryFn: getJobs,
    refetchInterval: 5000,
  })
  const { data: machines = [] } = useQuery({
    queryKey: ['machines'],
    queryFn: getMachines,
    refetchInterval: 10000,
  })
  const [expanded, setExpanded] = useState<string | null>(null)
  const historyCount = jobs.filter((job: any) => ['completed', 'failed', 'cancelled'].includes(job.status)).length
  const machineNames = new Map(machines.map((machine: any) => [machine.id, machine.hostname]))
  const machineName = (job: any) => machineNames.get(job.machine_id) || job.original_hostname || `${job.machine_id.slice(0, 8)}...`

  const cancel = useMutation({
    mutationFn: cancelJob,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['jobs'] }),
  })

  const clearHistory = useMutation({
    mutationFn: clearJobHistory,
    onSuccess: () => {
      setExpanded(null)
      qc.invalidateQueries({ queryKey: ['jobs'] })
    },
  })

  const handleClearHistory = () => {
    if (historyCount === 0 || clearHistory.isPending) return
    if (window.confirm(`Clear ${historyCount} completed, failed, or cancelled job${historyCount === 1 ? '' : 's'} from history?`)) {
      clearHistory.mutate()
    }
  }

  if (isLoading) return <div className="p-6 text-gray-400">Loading…</div>

  return (
    <div className="p-6">
      <div className="flex items-center justify-between gap-4 mb-6">
        <h1 className="text-2xl font-bold">Job History ({jobs.length})</h1>
        <button
          onClick={handleClearHistory}
          disabled={historyCount === 0 || clearHistory.isPending}
          className="inline-flex items-center gap-2 rounded-md border border-gray-700 px-3 py-2 text-sm text-gray-300 transition hover:border-red-500/60 hover:text-red-300 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:border-gray-700 disabled:hover:text-gray-300"
          title="Clear completed, failed, and cancelled jobs"
        >
          <Trash2 size={15} />
          {clearHistory.isPending ? 'Clearing...' : 'Clear History'}
        </button>
      </div>

      <div className="bg-gray-900 rounded-xl border border-gray-800 overflow-hidden">
        <table className="w-full text-sm">
          <thead className="text-gray-400 border-b border-gray-800">
            <tr>
              <th className="text-left px-4 py-3">Type</th>
              <th className="text-left px-4 py-3">Machine</th>
              <th className="text-left px-4 py-3">Status</th>
              <th className="text-left px-4 py-3">Progress</th>
              <th className="text-left px-4 py-3">Scheduled</th>
              <th className="text-left px-4 py-3">Started</th>
              <th className="text-left px-4 py-3">By</th>
              <th className="px-4 py-3"></th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((job: any) => (
              <Fragment key={job.id}>
                <tr
                  key={job.id}
                  className="border-b border-gray-800/50 hover:bg-gray-800/30 cursor-pointer"
                  onClick={() => setExpanded(expanded === job.id ? null : job.id)}
                >
                  <td className="px-4 py-2 font-medium capitalize">{job.type}</td>
                  <td className="px-4 py-2 text-gray-300">{machineName(job)}</td>
                  <td className="px-4 py-2"><StatusBadge status={job.status} /></td>
                  <td className="px-4 py-2"><JobProgressBar job={job} /></td>
                  <td className="px-4 py-2 text-gray-500 text-xs">
                    {job.scheduled_at ? formatDateTime(job.scheduled_at) : 'Immediate'}
                  </td>
                  <td className="px-4 py-2 text-gray-500 text-xs">{formatShort(job.started_at)}</td>
                  <td className="px-4 py-2 text-gray-400">{job.created_by ?? '—'}</td>
                  <td className="px-4 py-2 flex items-center gap-2">
                    {['pending', 'sent', 'running'].includes(job.status) && (
                      <button
                        onClick={(e) => { e.stopPropagation(); cancel.mutate(job.id) }}
                        className="text-gray-600 hover:text-red-400 transition"
                        title={job.status === 'running' ? 'Stop running job' : 'Cancel job'}
                      >
                        {job.status === 'running' ? <Square size={14} /> : <XCircle size={14} />}
                      </button>
                    )}
                    {expanded === job.id ? <ChevronUp size={14} className="text-gray-500" /> : <ChevronDown size={14} className="text-gray-500" />}
                  </td>
                </tr>
                {expanded === job.id && job.log && (
                  <tr key={`${job.id}-log`} className="border-b border-gray-800/50">
                    <td colSpan={8} className="px-4 py-3 bg-gray-950">
                      <pre className="text-xs text-gray-400 whitespace-pre-wrap max-h-48 overflow-auto font-mono">
                        {job.log}
                      </pre>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
            {jobs.length === 0 && (
              <tr><td colSpan={8} className="px-4 py-8 text-center text-gray-500">No jobs</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
