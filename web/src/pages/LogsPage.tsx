import { useState, useCallback } from "react"
import { api } from "@/lib/api-client"
import { Card, CardContent } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { RefreshCw, Copy, Trash2 } from "lucide-react"
import { usePolling } from "@/hooks/use-polling"
import { Skeleton } from "@/components/ui/skeleton"
import { TableSkeleton } from "@/components/shared/PageSkeletons"
import type { LogsData, LogItem } from "@/types"

const LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

const levelColors: Record<string, string> = {
  DEBUG: "bg-muted text-muted-foreground",
  INFO: "bg-info/15 text-info",
  WARNING: "bg-warning/15 text-warning-foreground",
  ERROR: "bg-destructive/15 text-destructive",
  CRITICAL: "bg-destructive text-destructive-foreground",
}

export default function LogsPage() {
  const [level, setLevel] = useState<string>("")
  const [limit, setLimit] = useState(200)
  const [clearing, setClearing] = useState(false)
  const fetcher = useCallback(
    () => api.logs({ level: level || undefined, limit }),
    [level, limit],
  )
  const { data, loading, refresh } = usePolling(fetcher, 5000)

  const logsData: LogsData | null = data
  const items: LogItem[] = logsData?.items || []

  if (loading && !logsData) {
    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <div className="space-y-2">
            <Skeleton className="h-7 w-36" />
            <Skeleton className="h-4 w-32" />
          </div>
          <div className="flex items-center gap-2">
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} className="h-9 w-20" />
            ))}
          </div>
        </div>
        <TableSkeleton rows={8} columns={4} />
      </div>
    )
  }

  const copyAll = () => {
    const text = items
      .map((l) => `${l.time} ${l.level} [${l.logger}] ${l.message}`)
      .join("\n")
    navigator.clipboard.writeText(text)
  }

  const clearAll = async () => {
    setClearing(true)
    try {
      await api.clearLogs()
      await refresh()
    } finally {
      setClearing(false)
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">运行日志</h1>
          <p className="text-sm text-muted-foreground">
            共 {items.length} 条（最多 {limit} 条）
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Select
            value={level}
            onValueChange={(v) => setLevel(v ?? "")}
          >
            <SelectTrigger className="w-32">
              <SelectValue placeholder="全部等级" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部等级</SelectItem>
              {LEVELS.map((l) => (
                <SelectItem key={l} value={l}>
                  {l}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select
            value={String(limit)}
            onValueChange={(v) => setLimit(Number(v ?? 200))}
          >
            <SelectTrigger className="w-24">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="100">100</SelectItem>
              <SelectItem value="200">200</SelectItem>
              <SelectItem value="500">500</SelectItem>
            </SelectContent>
          </Select>
          <Button size="sm" variant="outline" onClick={copyAll}>
            <Copy className="mr-1 h-4 w-4" />
            复制
          </Button>
          <Button size="sm" variant="outline" onClick={refresh} disabled={loading}>
            <RefreshCw className={`mr-1 h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            刷新
          </Button>
          <Button size="sm" variant="destructive" onClick={clearAll} disabled={clearing}>
            <Trash2 className="mr-1 h-4 w-4" />
            清除
          </Button>
        </div>
      </div>

      <Card>
        <CardContent className="p-0">
          {items.length === 0 ? (
            <div className="py-20 text-center text-muted-foreground">暂无日志</div>
          ) : (
            <div className="max-h-[70vh] overflow-auto font-mono text-xs">
              {items.map((log) => (
                <div
                  key={log.id}
                  className="flex flex-col gap-0.5 border-b border-border px-3 py-2 hover:bg-muted/50 sm:flex-row sm:items-center sm:gap-2 sm:px-4"
                >
                  <div className="flex shrink-0 items-center gap-2">
                    <span className="shrink-0 text-muted-foreground/70">
                      {log.time}
                    </span>
                    <Badge
                      variant="outline"
                      className={`shrink-0 px-1.5 py-0 text-[10px] ${levelColors[log.level] || ""}`}
                    >
                      {log.level}
                    </Badge>
                    <span
                      className="max-w-[45vw] truncate text-muted-foreground/70 sm:max-w-none"
                      title={`[${log.logger}]`}
                    >
                      [{log.logger}]
                    </span>
                  </div>
                  <span className="min-w-0 break-all">{log.message}</span>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
