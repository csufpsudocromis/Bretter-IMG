const ACTIVE_STATUSES = new Set(['pending', 'sent', 'running'])
const PROGRESS_TYPES = new Set(['deploy', 'capture', 'push_winpe'])

function getProgressPercent(job: any) {
  const raw = Number(job.progress_percent)
  if (!Number.isFinite(raw) || raw <= 0) return null
  return Math.min(100, Math.max(1, Math.round(raw)))
}

export default function JobProgressBar({ job }: { job: any }) {
  const percent = getProgressPercent(job)
  const shouldShow = percent !== null || PROGRESS_TYPES.has(job.type)
  if (!shouldShow) return <span className="text-gray-600">—</span>

  const waiting = percent === null && ACTIVE_STATUSES.has(job.status)
  const label = percent === null ? (waiting ? 'Waiting' : '—') : `${percent}%`

  return (
    <div className="flex min-w-[150px] max-w-[220px] items-center gap-2" title={percent === null ? 'Waiting for image apply progress' : `${percent}% complete`}>
      <div className="h-2 w-full overflow-hidden rounded-sm bg-gray-800">
        <div
          className="h-full rounded-sm bg-emerald-500 transition-[width] duration-500"
          style={{ width: `${percent ?? 0}%` }}
        />
      </div>
      <span className="w-12 shrink-0 text-right text-xs tabular-nums text-gray-400">{label}</span>
    </div>
  )
}
