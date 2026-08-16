import { useState, useCallback, useEffect } from "react"
import { api } from "@/lib/api-client"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Badge } from "@/components/ui/badge"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { ShieldCheck, Info, Check, Globe, ToggleLeft, ToggleRight, Loader2, AlertTriangle, RefreshCw } from "lucide-react"
import { usePolling } from "@/hooks/use-polling"
import type { ConfigPayload, GeoInfo } from "@/types"

export default function SettingsPage() {
  const { data, refresh } = usePolling(() => api.config(), 30000)
  const [adminKey, setAdminKey] = useState("")
  const [busy, setBusy] = useState(false)
  const [success, setSuccess] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Geo / device fingerprint state
  const [geo, setGeo] = useState<GeoInfo | null>(null)
  const [geoBusy, setGeoBusy] = useState(false)
  const [geoSuccess, setGeoSuccess] = useState(false)
  const [geoError, setGeoError] = useState<string | null>(null)

  // Proxy state
  const [proxyType, setProxyType] = useState("socks5")
  const [proxyHost, setProxyHost] = useState("")
  const [proxyPort, setProxyPort] = useState("1080")
  const [proxyUsername, setProxyUsername] = useState("")
  const [proxyPassword, setProxyPassword] = useState("")
  const [proxyBusy, setProxyBusy] = useState(false)
  const [proxySuccess, setProxySuccess] = useState(false)
  const [proxyError, setProxyError] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<{ ok: boolean; ip?: string; country?: string; city?: string; org?: string; latency_ms?: number; error?: string } | null>(null)
  const [testBusy, setTestBusy] = useState(false)
  const [authOpen, setAuthOpen] = useState(false)

  const config: ConfigPayload | null = data

  const loadGeo = useCallback(async () => {
    try {
      const g = await api.geo()
      setGeo(g)
      setGeoError(null)
    } catch (err: unknown) {
      setGeoError(err instanceof Error ? err.message : "获取时区信息失败")
    }
  }, [])

  useEffect(() => {
    loadGeo()
  }, [loadGeo])

  const handleRefreshGeo = useCallback(async () => {
    setGeoBusy(true)
    setGeoError(null)
    setGeoSuccess(false)
    try {
      await api.refreshGeo()
      await loadGeo()
      setGeoSuccess(true)
      refresh()
      setTimeout(() => setGeoSuccess(false), 3000)
    } catch (err: unknown) {
      setGeoError(err instanceof Error ? err.message : "刷新失败")
    } finally {
      setGeoBusy(false)
    }
  }, [loadGeo, refresh])

  const handleUpdate = useCallback(async () => {
    if (!adminKey.trim() || adminKey.trim().length < 8) {
      setError("密钥至少需要 8 个字符")
      return
    }
    setBusy(true)
    setError(null)
    setSuccess(false)
    try {
      await api.updateSecurity(adminKey.trim())
      setSuccess(true)
      setAdminKey("")
      refresh()
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "修改失败"
      setError(msg)
    } finally {
      setBusy(false)
    }
  }, [adminKey, refresh])

  const handleSaveProxy = useCallback(async () => {
    if (!proxyHost.trim()) {
      setProxyError("请填写代理地址")
      return
    }
    setProxyBusy(true)
    setProxyError(null)
    setProxySuccess(false)
    try {
      await api.saveProxy({
        proxy_enabled: proxyHost.trim().length > 0,
        proxy_type: proxyType,
        proxy_host: proxyHost.trim(),
        proxy_port: parseInt(proxyPort) || 1080,
        proxy_username: proxyUsername.trim() || undefined,
        proxy_password: proxyPassword || undefined,
      })
      setProxySuccess(true)
      refresh()
      setTimeout(() => setProxySuccess(false), 3000)
    } catch (err: unknown) {
      setProxyError(err instanceof Error ? err.message : "保存失败")
    } finally {
      setProxyBusy(false)
    }
  }, [proxyType, proxyHost, proxyPort, proxyUsername, proxyPassword, refresh])

  const handleTestProxy = useCallback(async () => {
    if (!proxyHost.trim()) {
      setProxyError("请先填写代理地址")
      return
    }
    setTestBusy(true)
    setTestResult(null)
    setProxyError(null)
    try {
      const r = await api.testProxy({
        proxy_type: proxyType,
        proxy_host: proxyHost.trim(),
        proxy_port: parseInt(proxyPort) || 1080,
        proxy_username: proxyUsername.trim() || undefined,
        proxy_password: proxyPassword || undefined,
      })
      setTestResult(r)
    } catch (err: unknown) {
      setTestResult({ ok: false, error: err instanceof Error ? err.message : "测试失败" })
    } finally {
      setTestBusy(false)
    }
  }, [proxyType, proxyHost, proxyPort, proxyUsername, proxyPassword])

  const handleToggle = () => {
    setProxyBusy(true)
    const enabled = !config?.proxy_enabled
    api.saveProxy({
      proxy_enabled: enabled,
      proxy_type: config?.proxy_type || proxyType || "socks5",
      proxy_host: config?.proxy_host || proxyHost || "",
      proxy_port: config?.proxy_port || parseInt(proxyPort) || 1080,
      proxy_username: (config?.proxy_username || proxyUsername?.trim()) || undefined,
      proxy_password: proxyPassword || undefined,
    }).then(() => { refresh(); setProxyBusy(false) }).catch(() => setProxyBusy(false))
  }

  const proxyEnabled = config?.proxy_enabled ?? false

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">设置</h1>
        <p className="text-sm text-muted-foreground">修改管理员密钥、设备指纹与代理配置</p>
      </div>

      {/* Admin Key */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <ShieldCheck className="h-4 w-4" />
            修改管理员密钥
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <Alert>
            <Info className="h-4 w-4" />
            <AlertDescription className="flex items-center gap-2">
              修改后需要重新登录。当前状态:
              {config?.using_default_admin_key ? (
                <Badge variant="destructive">使用默认密钥</Badge>
              ) : (
                <Badge>已自定义</Badge>
              )}
            </AlertDescription>
          </Alert>

          <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
            <Input
              type="password"
              placeholder="新的管理员密钥（至少8位）"
              value={adminKey}
              onChange={(e) => setAdminKey(e.target.value)}
              className="max-w-md w-full"
            />
            <Button onClick={handleUpdate} disabled={busy} className="sm:w-auto">
              {busy ? "保存中..." : "保存"}
            </Button>
          </div>

          {success && (
            <div className="flex items-center gap-2 text-sm text-green-600">
              <Check className="h-4 w-4" />
              修改成功，请重新登录
            </div>
          )}
          {error && (
            <p className="text-sm text-destructive">{error}</p>
          )}
        </CardContent>
      </Card>

      {/* Geo / Device Fingerprint */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Globe className="h-4 w-4" />
            设备时区指纹
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <Alert>
            <Info className="h-4 w-4" />
            <AlertDescription>
              广告请求会携带设备时区与语言。错误的时区（如亚洲时区）会被上游判定为受限访问层，导致 pro/luna 等 premium 模型被风控。服务启动时会自动检测一次；如果出口 IP 变了，可手动重新检测。
            </AlertDescription>
          </Alert>

          <div className="grid gap-2 sm:grid-cols-3 text-sm">
            <div>
              <span className="text-muted-foreground">时区:</span>{" "}
              <span className="font-mono">{geo?.timezone ?? config?.timezone ?? "..."}</span>
            </div>
            <div>
              <span className="text-muted-foreground">语言:</span>{" "}
              <span className="font-mono">{geo?.locale ?? config?.locale ?? "..."}</span>
            </div>
            <div>
              <span className="text-muted-foreground">系统:</span>{" "}
              <span className="font-mono">{geo?.os_name ?? config?.os_name ?? "windows"}</span>
            </div>
          </div>

          {geo?.detected && (
            <div className="text-xs text-muted-foreground">
              最近检测结果: {geo.detected.timezone} / {geo.detected.locale}
            </div>
          )}

          <div className="flex gap-2">
            <Button size="sm" onClick={handleRefreshGeo} disabled={geoBusy}>
              {geoBusy ? (
                <Loader2 className="mr-1.5 h-4 w-4 animate-spin" />
              ) : (
                <RefreshCw className="mr-1.5 h-4 w-4" />
              )}
              重新检测
            </Button>
          </div>

          {geoSuccess && (
            <div className="flex items-center gap-2 text-sm text-green-600">
              <Check className="h-4 w-4" />时区已更新
            </div>
          )}
          {geoError && (
            <div className="flex items-center gap-2 text-sm text-destructive">
              <AlertTriangle className="h-4 w-4" />{geoError}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Proxy Configuration */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Globe className="h-4 w-4" />
            代理配置
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex items-center gap-2">
            <button onClick={handleToggle} className="flex items-center gap-1 text-sm" disabled={proxyBusy}>
              {proxyEnabled ? (
                <ToggleRight className="h-6 w-6 text-primary" />
              ) : (
                <ToggleLeft className="h-6 w-6 text-muted-foreground" />
              )}
            </button>
            <span className="text-sm">
              代理: <Badge variant={proxyEnabled ? "default" : "secondary"}>{proxyEnabled ? "已启用" : "已禁用"}</Badge>
            </span>
            {proxyEnabled && config?.proxy_display && (
              <span className="font-mono text-xs text-muted-foreground">{config.proxy_display}</span>
            )}
          </div>

          <div className="flex flex-col gap-2 sm:flex-row">
            <Select value={proxyType} onValueChange={(v) => setProxyType(v ?? "socks5")}>
              <SelectTrigger className="w-full sm:w-28">
                <SelectValue placeholder="类型" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="socks5">socks5</SelectItem>
                <SelectItem value="socks5h">socks5h</SelectItem>
                <SelectItem value="http">http</SelectItem>
                <SelectItem value="https">https</SelectItem>
              </SelectContent>
            </Select>
            <Input
              placeholder="主机地址"
              value={proxyHost}
              onChange={(e) => setProxyHost(e.target.value)}
              className="flex-1 font-mono text-sm"
            />
            <Input
              placeholder="端口"
              value={proxyPort}
              onChange={(e) => setProxyPort(e.target.value)}
              className="w-full font-mono text-sm sm:w-24"
            />
          </div>

          {/* Auth toggle */}
          {!authOpen ? (
            <button
              onClick={() => setAuthOpen(true)}
              className="text-sm text-muted-foreground hover:text-foreground text-left w-fit"
            >
              + 添加认证（可选）
            </button>
          ) : (
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
              <Input
                placeholder="用户名（可选）"
                value={proxyUsername}
                onChange={(e) => setProxyUsername(e.target.value)}
                className="flex-1 font-mono text-sm"
              />
              <Input
                type="password"
                placeholder="密码（可选）"
                value={proxyPassword}
                onChange={(e) => setProxyPassword(e.target.value)}
                className="flex-1 font-mono text-sm"
              />
              <Button size="sm" variant="ghost" className="shrink-0" onClick={() => { setAuthOpen(false); setProxyUsername(""); setProxyPassword("") }}>
                移除
              </Button>
            </div>
          )}

          <div className="flex gap-2">
            <Button size="sm" onClick={handleSaveProxy} disabled={proxyBusy}>
              {proxyBusy ? "保存中..." : "保存"}
            </Button>
            <Button size="sm" variant="outline" onClick={handleTestProxy} disabled={testBusy}>
              {testBusy ? (
                <Loader2 className="mr-1.5 h-4 w-4 animate-spin" />
              ) : (
                <Globe className="mr-1.5 h-4 w-4" />
              )}
              测试代理
            </Button>
          </div>

          {proxySuccess && (
            <div className="flex items-center gap-2 text-sm text-green-600">
              <Check className="h-4 w-4" />代理配置已保存
            </div>
          )}
          {proxyError && (
            <div className="flex items-center gap-2 text-sm text-destructive">
              <AlertTriangle className="h-4 w-4" />{proxyError}
            </div>
          )}

          {testResult && (
            <Card className="bg-muted/50">
              <CardContent className="p-4">
                {testResult.ok ? (
                  <div className="grid gap-2 sm:grid-cols-2 text-sm">
                    {testResult.ip && <div><span className="text-muted-foreground">IP:</span> <span className="font-mono">{testResult.ip}</span></div>}
                    {testResult.country && <div><span className="text-muted-foreground">国家:</span> {testResult.country}</div>}
                    {testResult.city && <div><span className="text-muted-foreground">城市:</span> {testResult.city}</div>}
                    {testResult.org && <div><span className="text-muted-foreground">运营商:</span> {testResult.org}</div>}
                    {testResult.latency_ms !== undefined && <div><span className="text-muted-foreground">延迟:</span> <span className="font-mono">{testResult.latency_ms}ms</span></div>}
                  </div>
                ) : (
                  <div className="flex items-center gap-2 text-sm text-destructive">
                    <AlertTriangle className="h-4 w-4" />
                    代理测试失败: {testResult.error}
                  </div>
                )}
              </CardContent>
            </Card>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
