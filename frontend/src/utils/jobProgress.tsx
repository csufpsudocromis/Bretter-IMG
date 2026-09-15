export function getWinPEApplyProgress(log?: string | null): number | null {
  if (!log) return null

  const patterns = [
    /WinPE image apply progress:[^\n]*?(\d+(?:\.\d+)?)%/gi,
    /WinPE DISM Apply-Image:[^\n]*?(\d+(?:\.\d+)?)%/gi,
  ]
  let latest: number | null = null

  for (const pattern of patterns) {
    let match: RegExpExecArray | null
    while ((match = pattern.exec(log)) !== null) {
      const value = Number(match[1])
      if (Number.isFinite(value)) latest = Math.max(0, Math.min(100, value))
    }
  }

  return latest
}

export function WinPEApplyProgress({ value }: { value: number | null }) {
  if (value === null) return <span className="text-gray-600">-</span>

  return (
    <div className="min-w-32">
      <div className="mb-1 flex items-center justify-between gap-2 text-xs">
        <span className="text-gray-400">WinPE apply</span>
        <span className="font-mono text-gray-200">{value.toFixed(0)}%</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded bg-gray-800">
        <div className="h-full bg-blue-500 transition-all" style={{ width: `${value}%` }} />
      </div>
    </div>
  )
}
