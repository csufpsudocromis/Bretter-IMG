import clsx from 'clsx'

const STATUS_COLORS: Record<string, string> = {
  online: 'bg-green-500',
  offline: 'bg-gray-500',
  imaging: 'bg-blue-500 animate-pulse',
  error: 'bg-red-500',
  pending: 'bg-yellow-500',
  sent: 'bg-blue-400',
  running: 'bg-blue-500 animate-pulse',
  completed: 'bg-green-500',
  failed: 'bg-red-500',
  cancelled: 'bg-gray-500',
}

export default function StatusBadge({ status }: { status: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-medium">
      <span className={clsx('w-2 h-2 rounded-full', STATUS_COLORS[status] ?? 'bg-gray-400')} />
      {status}
    </span>
  )
}
