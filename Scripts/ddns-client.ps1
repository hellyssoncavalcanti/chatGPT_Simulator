# ddns-client.ps1
# QUICK TUNNEL (trycloudflare) + POST para o PHP (tunnel_url)
# Logs: C:\chatgpt_simulator\logs\ddns-client-dd_MM_yyyy-HH_mm_ss.log
#       C:\chatgpt_simulator\logs\cloudflared-dd_MM_yyyy-HH_mm_ss.log

# ========================
# CONFIG
# ========================
# Endpoint que recebe tunnel_url (Quick Tunnel)
$PhpEndpointTunnel = "https://conexaovida.org/no-ip-dynamic_via_clouflare.php"

# Endpoint que recebe IP (IPv4/IPv6) e continua aceitando ?port=...
$PhpEndpointIp = "https://conexaovida.org/no-ip-dynamic_ip.php"
$Token       = "ddns_9XJkP8Qm7Vt2Rz4Hq1cN6aY0sL3uF5eD8bG2wK9pT7nM4xZ1vS6rQ0hE3yU"
$HostName    = "casa"

$LocalServiceUrl  = "http://127.0.0.1:3003"
$HeartbeatSeconds = 300

$WindowTitle = "Atualizando IP dinamico para o deste PC - acesse via http://conexaovida.org/no-ip-dynamic_via_clouflare.php (porta fixa 3003)"

# download automático (se cloudflared não estiver instalado)
$CloudflaredDownloadUrl = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"

# Argumentos do Quick Tunnel (debug bem verboso + sem auto-update)
# (Se a versão instalada não aceitar algum flag, isso vai aparecer no log/stderr.)
$CloudflaredArgs = "tunnel --no-autoupdate --loglevel debug --url $LocalServiceUrl"

# ========================
# Encoding / UI
# ========================
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch {}
$Host.UI.RawUI.WindowTitle = $WindowTitle

# ========================
# Pastas do projeto / logs
# ========================
$ScriptDir = Split-Path -Parent $PSCommandPath                  # C:\chatgpt_simulator\Scripts
$ProjectRootDir = Split-Path -Parent $ScriptDir                 # C:\chatgpt_simulator
$LogsDir = Join-Path -Path $ProjectRootDir -ChildPath "logs"    # C:\chatgpt_simulator\logs

$LogTimestamp = Get-Date -Format "dd_MM_yyyy-HH_mm_ss"
$LogFile = Join-Path -Path $LogsDir -ChildPath ("ddns-client-{0}.log" -f $LogTimestamp)
$CloudflaredLogFile = Join-Path -Path $LogsDir -ChildPath ("cloudflared-{0}.log" -f $LogTimestamp)
# stdout/stderr brutos do cloudflared (mesmo timestamp, mesma pasta)
$CloudflaredStdoutFile = Join-Path -Path $LogsDir -ChildPath ("cloudflared-stdout-{0}.log" -f $LogTimestamp)
$CloudflaredStderrFile = Join-Path -Path $LogsDir -ChildPath ("cloudflared-stderr-{0}.log" -f $LogTimestamp)

# Se precisar baixar, salva cloudflared.exe na pasta Scripts (ao lado do PS1)
$CloudflaredLocalExe = Join-Path -Path $ScriptDir -ChildPath "cloudflared.exe"

function Ensure-Dir([string]$dir) {
  try { New-Item -ItemType Directory -Force -Path $dir | Out-Null } catch {}
}

function Log([string]$msg) {
  $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  $line = "[$stamp] $msg"
  Write-Host $line
  try { Add-Content -Path $LogFile -Value $line -Encoding UTF8 } catch {}
}

function Log-CF([string]$msg) {
  $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  $line = "[$stamp] $msg"
  try { Add-Content -Path $CloudflaredLogFile -Value $line -Encoding UTF8 } catch {}
}

function Print-Header {
  Clear-Host
  Write-Host "======================================================================="
  Write-Host " QUICK TUNNEL (Cloudflare) - CONEXAOVIDA"
  Write-Host "======================================================================="
  Write-Host ""
  Write-Host "Publicando (origem local):"
  Write-Host " - $LocalServiceUrl"
  Write-Host ""
  Write-Host "Acesso fixo (clientless, só navegador):"
  Write-Host " - http://conexaovida.org/no-ip-dynamic_via_clouflare.php"
  Write-Host "ou"
  Write-Host " - http://conexaovida.org/no-ip-dynamic_ip.php?port=PORTA_QUE_DESEJA (geralmente 3003)"
  Write-Host ""
  Write-Host "Logs:"
  Write-Host " - $LogFile"
  Write-Host " - $CloudflaredLogFile"
  Write-Host ""
  Write-Host "Como parar: Ctrl+C ou fechar a janela."
  Write-Host ""
  Write-Host "======================================================================="
  Write-Host ""
}

function CountdownInline([int]$seconds, [string]$label) {
  for ($i = $seconds; $i -ge 1; $i--) {
    $line = ("{0} em {1}s..." -f $label, $i).PadRight(95)
    Write-Host -NoNewline ("`r" + $line)
    Start-Sleep -Seconds 1
  }
  Write-Host ""
}

function Ensure-Tls12 {
  try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.ServicePointManager]::SecurityProtocol
  } catch {}
}

function Resolve-CloudflaredPath {
  if (Test-Path $CloudflaredLocalExe) { return $CloudflaredLocalExe }
  $cmd = Get-Command cloudflared -ErrorAction SilentlyContinue
  if ($cmd -and $cmd.Source) { return $cmd.Source }
  return $null
}

function Install-CloudflaredIfMissing {
  Write-Host "Verificando dependencias... (cloudflared)"
  $path = Resolve-CloudflaredPath
  if ($path) {
    Log "Dependencia OK: cloudflared encontrado em: $path"
    return $path
  }

  Write-Host "cloudflared nao encontrado. Baixando automaticamente..."
  Log "cloudflared nao encontrado. Download: $CloudflaredDownloadUrl"

  Ensure-Tls12
  try {
    Invoke-WebRequest -Uri $CloudflaredDownloadUrl -OutFile $CloudflaredLocalExe -UseBasicParsing
    if (!(Test-Path $CloudflaredLocalExe)) { throw "Falha ao gravar cloudflared.exe" }
    Log "cloudflared baixado em: $CloudflaredLocalExe"
    return $CloudflaredLocalExe
  } catch {
    Log "ERRO baixando cloudflared: $($_.Exception.Message)"
    throw
  }
}

function Post-TunnelUrl([string]$tunnelUrl) {
  $body = @{
    token      = $Token
    host       = $HostName
    tunnel_url = $tunnelUrl
  } | ConvertTo-Json -Compress

  return Invoke-RestMethod -Method Post -Uri $PhpEndpointTunnel -ContentType "application/json" -Body $body -TimeoutSec 20
}

function Get-PublicIP4 {
  try {
    $ip = (& curl.exe -4 -s "https://api.ipify.org").Trim()
    if ($ip -and ($ip -match '^\d{1,3}(\.\d{1,3}){3}$')) { return $ip }
  } catch {}
  try {
    $ip = (Invoke-RestMethod -Uri "https://api.ipify.org" -TimeoutSec 10).ToString().Trim()
    if ($ip -and ($ip -match '^\d{1,3}(\.\d{1,3}){3}$')) { return $ip }
  } catch {}
  return $null
}

function Get-PublicIP6 {
  try {
    $ip = (& curl.exe -6 -s "https://api64.ipify.org").Trim()
    if ($ip -and ($ip -match ':')) { return $ip }
  } catch {}
  try {
    $ip = (Invoke-RestMethod -Uri "https://api64.ipify.org" -TimeoutSec 10).ToString().Trim()
    if ($ip -and ($ip -match ':')) { return $ip }
  } catch {}
  return $null
}

function Post-UpdateIP([string]$ip) {
  $body = @{
    token = $Token
    host  = $HostName
    ip    = $ip
  } | ConvertTo-Json -Compress

  return Invoke-RestMethod -Method Post -Uri $PhpEndpointIp -ContentType "application/json" -Body $body -TimeoutSec 20
}

function Update-LegacyIpNow {
  try {
    $ip4 = Get-PublicIP4
    if ($ip4) {
      $r4 = Post-UpdateIP $ip4
      Log "DDNS-IP (IPv4) -> ip=$ip4 ok=$($r4.ok) changed=$($r4.data.changed) ip4_saved=$($r4.data.ip4)"
    } else {
      Log "DDNS-IP (IPv4) -> indisponível"
    }
  } catch {
    Log "DDNS-IP (IPv4) ERRO: $($_.Exception.Message)"
  }

  try {
    $ip6 = Get-PublicIP6
    if ($ip6) {
      $r6 = Post-UpdateIP $ip6
      Log "DDNS-IP (IPv6) -> ip=$ip6 ok=$($r6.ok) changed=$($r6.data.changed) ip6_saved=$($r6.data.ip6)"
    } else {
      Log "DDNS-IP (IPv6) -> indisponível"
    }
  } catch {
    Log "DDNS-IP (IPv6) ERRO: $($_.Exception.Message)"
  }
}

function Test-LocalServicePort {
  param(
    [int]$TcpTimeoutMs = 3000,
    [int]$HttpTimeoutMs = 3000
  )

  # 1) Parse do URL
  try {
    $uri = [Uri]$LocalServiceUrl
  } catch {
    Log "ORIGIN CHECK: URL invalida em LocalServiceUrl='$LocalServiceUrl' erro=$($_.Exception.Message)"
    return $false
  }

  $port = $uri.Port
  if ($port -le 0) {
    Log "ORIGIN CHECK: porta invalida em LocalServiceUrl='$LocalServiceUrl' (port=$port)"
    return $false
  }

  # 2) Monta lista de hosts equivalentes (resolve casos localhost/IPv6)
  $hosts = New-Object System.Collections.Generic.List[string]
  $hosts.Add($uri.Host)

  if ($uri.Host -eq "127.0.0.1") {
    $hosts.Add("localhost")
    $hosts.Add("::1")
  } elseif ($uri.Host -eq "localhost") {
    $hosts.Add("127.0.0.1")
    $hosts.Add("::1")
  } elseif ($uri.Host -eq "::1") {
    $hosts.Add("127.0.0.1")
    $hosts.Add("localhost")
  }

  # remove duplicados
  $hostsUnique = $hosts | Select-Object -Unique

  # helper: TCP check
  function Try-Tcp([string]$h, [int]$p, [int]$timeoutMs) {
    try {
      $client = New-Object System.Net.Sockets.TcpClient
      $task = $client.ConnectAsync($h, $p)
      $ok = $task.Wait($timeoutMs)
      $connected = $ok -and $client.Connected
      try { $client.Close() } catch {}
      return $connected
    } catch {
      return $false
    }
  }

  # helper: HTTP check (qualquer status = online)
  function Try-Http([string]$testUrl, [int]$timeoutMs) {
    $handler = $null
    $http = $null
    try {
      $handler = New-Object System.Net.Http.HttpClientHandler
      $handler.AllowAutoRedirect = $true

      $http = New-Object System.Net.Http.HttpClient($handler)
      $http.Timeout = [TimeSpan]::FromMilliseconds($timeoutMs)

      # HEAD às vezes é bloqueado; usamos GET só de headers
      $resp = $http.GetAsync($testUrl, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
      $code = [int]$resp.StatusCode

      # Se respondeu qualquer coisa, está online (200-599)
      if ($code -ge 100 -and $code -le 599) { return $true }
      return $false
    } catch {
      return $false
    } finally {
      try { if ($http) { $http.Dispose() } } catch {}
      try { if ($handler) { $handler.Dispose() } } catch {}
    }
  }

  # 3) Tenta por HTTP primeiro (mais “real” do que só TCP)
  foreach ($h in $hostsUnique) {
    $testUrl = "{0}://{1}:{2}{3}" -f $uri.Scheme, $h, $port, $uri.PathAndQuery
    if (Try-Http $testUrl $HttpTimeoutMs) {
      Log "ORIGIN CHECK: ONLINE via HTTP -> $testUrl"
      return $true
    }
  }

  # 4) Fallback: TCP
  foreach ($h in $hostsUnique) {
    if (Try-Tcp $h $port $TcpTimeoutMs) {
      Log ("ORIGIN CHECK: ONLINE via TCP -> {0}:{1}" -f $h, $port)
      return $true
    }
  }

  Log "ORIGIN CHECK: OFFLINE (HTTP+TCP falharam) url=$LocalServiceUrl hosts=[$($hostsUnique -join ',')]"
  return $false
}

function Dump-CloudflaredDebug([string]$reason, [int]$tailLines = 160) {
  Log "==================== CLOUDFLARED DEBUG ===================="
  Log "Motivo: $reason"
  Log "CloudflaredLogFile: $CloudflaredLogFile"

  if (Test-Path $CloudflaredLogFile) {
    try {
      Log "---- tail ($tailLines linhas) do CloudflaredLogFile ----"
      $tail = Get-Content -Path $CloudflaredLogFile -Tail $tailLines -ErrorAction Stop
      foreach ($l in $tail) { Log "CF> $l" }
      Log "---- fim tail ----"
    } catch {
      Log "Falha lendo tail do CloudflaredLogFile: $($_.Exception.Message)"
    }
  } else {
    Log "CloudflaredLogFile ainda nao existe."
  }

  Log "==========================================================="
}

function Start-QuickTunnelRobust([string]$cloudflaredExe) {
  Log "==================== START QUICK TUNNEL (FILE-REDIRECT DIAG) ===================="
  Log "Exe: $cloudflaredExe"
  Log "Args: $CloudflaredArgs"
  Log "WorkingDir: $ScriptDir"
  Log "StdoutFile: $CloudflaredStdoutFile"
  Log "StderrFile: $CloudflaredStderrFile"
  Log "CloudflaredLogFile: $CloudflaredLogFile"

  # limpa arquivos antigos do mesmo timestamp (por segurança)
  foreach ($f in @($CloudflaredStdoutFile, $CloudflaredStderrFile)) {
    try { if (Test-Path $f) { Remove-Item -Force $f } } catch {}
  }

  # versão
  try {
    $ver = & $cloudflaredExe --version 2>&1 | Out-String
    $ver = $ver.Trim()
    if ($ver) { Log "cloudflared --version: $ver" }
  } catch {
    Log "cloudflared --version falhou: $($_.Exception.Message)"
  }

  # inicia processo com redirect real de stdout/stderr
  Log "DIAG: Start-Process (redirect stdout/stderr)..."
  $p = Start-Process `
    -FilePath $cloudflaredExe `
    -ArgumentList $CloudflaredArgs `
    -WorkingDirectory $ScriptDir `
    -NoNewWindow `
    -PassThru `
    -RedirectStandardOutput $CloudflaredStdoutFile `
    -RedirectStandardError  $CloudflaredStderrFile

  Log "DIAG: Start-Process OK. PID=$($p.Id)"

  # lê incrementalmente stdout/stderr e grava no CloudflaredLogFile com timestamp
  $outPos = 0L
  $errPos = 0L
  $url = $null

  function Read-NewText([string]$path, [ref]$pos) {
    if (!(Test-Path $path)) { return "" }
    $fs = [System.IO.File]::Open($path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    try {
      $fs.Seek($pos.Value, [System.IO.SeekOrigin]::Begin) | Out-Null
      $sr = New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::UTF8, $true, 4096, $true)
      $txt = $sr.ReadToEnd()
      $pos.Value = $fs.Position
      return $txt
    } catch {
      return ""
    } finally {
      try { $fs.Close() } catch {}
    }
  }

  $deadline = (Get-Date).AddSeconds(120)

  while (-not $url) {
    $p.Refresh()

    # captura qualquer saída nova e joga no cloudflared log (linha a linha)
    $newOut = Read-NewText $CloudflaredStdoutFile ([ref]$outPos)
    if ($newOut) {
      foreach ($line in ($newOut -split "`r?`n")) {
        if ($line -ne "") { Log-CF $line }
        if (-not $url) {
          $m = [regex]::Match($line, 'https://[a-z0-9-]+\.trycloudflare\.com', 'IgnoreCase')
          if ($m.Success) { $url = $m.Value }
        }
      }
    }

    $newErr = Read-NewText $CloudflaredStderrFile ([ref]$errPos)
    if ($newErr) {
      foreach ($line in ($newErr -split "`r?`n")) {
        if ($line -ne "") { Log-CF ("[stderr] " + $line) }
        if (-not $url) {
          $m = [regex]::Match($line, 'https://[a-z0-9-]+\.trycloudflare\.com', 'IgnoreCase')
          if ($m.Success) { $url = $m.Value }
        }
      }
    }

    if ($p.HasExited) {
      # garante leitura final do que ficou no buffer
      $newOut2 = Read-NewText $CloudflaredStdoutFile ([ref]$outPos)
      if ($newOut2) { foreach ($l in ($newOut2 -split "`r?`n")) { if ($l) { Log-CF $l } } }

      $newErr2 = Read-NewText $CloudflaredStderrFile ([ref]$errPos)
      if ($newErr2) { foreach ($l in ($newErr2 -split "`r?`n")) { if ($l) { Log-CF ("[stderr] " + $l) } } }

      Log "DIAG: cloudflared encerrou. ExitCode=$($p.ExitCode)"
      Dump-CloudflaredDebug "cloudflared encerrou antes de gerar URL (ExitCode=$($p.ExitCode))."
      throw "cloudflared encerrou (ExitCode=$($p.ExitCode)) antes de gerar URL. Veja $CloudflaredLogFile"
    }

    if ((Get-Date) -gt $deadline) {
      Log "DIAG: TIMEOUT aguardando URL."
      Dump-CloudflaredDebug "Timeout aguardando URL (120s)."
      try { $p.Kill() } catch {}
      throw "Timeout aguardando URL do trycloudflare. Veja $CloudflaredLogFile"
    }

    Start-Sleep -Milliseconds 200
  }

  Log "DIAG: URL capturada: $url"
  Log "==================== START QUICK TUNNEL (OK) ===================="

  return @{
    Process = $p
    Sync    = [hashtable]::Synchronized(@{ Url = $url })
    Writer  = $null
  }
}


# ========================
# MAIN
# ========================
Ensure-Dir $LogsDir
Print-Header
Log "Iniciando. Host=$HostName EndpointPHP=$PhpEndpoint LocalService=$LocalServiceUrl Heartbeat=${HeartbeatSeconds}s LogsDir=$LogsDir"

while ($true) {
  try {
    # alerta se porta local não estiver ouvindo
    if (-not (Test-LocalServicePort)) {
      Log "ORIGIN OFFLINE: a porta local não está aceitando conexão em $LocalServiceUrl"
      Write-Host "ATENCAO: o servico local NAO esta rodando/respondendo em $LocalServiceUrl"
      Write-Host "Vou aguardar a aplicacao subir antes de abrir o tunnel..."
      Write-Host ""
      CountdownInline 10 "Aguardando servico local"
      continue
    }

    $cloudflaredExe = Install-CloudflaredIfMissing
    $tunnel = Start-QuickTunnelRobust $cloudflaredExe
    $currentUrl = $tunnel.Sync.Url

    Log "Quick Tunnel ativo: $currentUrl"
    Write-Host ""
    Write-Host "==============================================================="
    Write-Host "TUNEL ATIVO:"
    Write-Host " - URL publica direta: $currentUrl"
    Write-Host " - URL fixa via ConexaoVida (redirect): http://conexaovida.org/no-ip-dynamic_via_clouflare.php"
    Write-Host "==============================================================="
    Write-Host "ACESSO VIA IP DIRETO (roteador do servidor precisa estar com portas abertas/liberadas ao acesso remoto):"
    Write-Host " - URL fixa via ConexaoVida (redirect): http://conexaovida.org/no-ip-dynamic_ip.php?port=PORTA_QUE_DESEJA (geralmente 3003)"
    Write-Host "==============================================================="
    Write-Host ""

    # POST inicial
    try {
      $resp = Post-TunnelUrl $currentUrl
      Log "POST tunnel_url -> ok=$($resp.ok) changed=$($resp.data.changed) tunnel_url_saved=$($resp.data.tunnel_url)"
      Update-LegacyIpNow
    } catch {
      Log "ERRO ao enviar tunnel_url ao PHP: $($_.Exception.Message)"
    }

    # Heartbeat / monitor
    while ($true) {
      if ($tunnel.Process.HasExited) {
        Log "cloudflared encerrou. Reiniciando..."
        try { $tunnel.Writer.Close() } catch {}
        break
      }

      # contador regressivo na mesma linha
      for ($i = $HeartbeatSeconds; $i -ge 1; $i--) {
        if ($tunnel.Process.HasExited) { break }
        $line = ("Heartbeat / proxima atualizacao do redirect em {0}s..." -f $i).PadRight(95)
        Write-Host -NoNewline ("`r" + $line)
        Start-Sleep -Seconds 1
      }
      Write-Host ""

      if ($tunnel.Process.HasExited) {
        Log "cloudflared encerrou durante o countdown. Reiniciando..."
        try { $tunnel.Writer.Close() } catch {}
        break
      }

      # se URL mudar (raro), atualiza
      $newUrl = $tunnel.Sync.Url
      if ($newUrl -and $newUrl -ne $currentUrl) {
        $currentUrl = $newUrl
        Log "URL do tunel mudou: $currentUrl"
      }

      try {
        $resp = Post-TunnelUrl $currentUrl
        Log "Heartbeat POST -> ok=$($resp.ok) changed=$($resp.data.changed) tunnel_url_saved=$($resp.data.tunnel_url)"
        Update-LegacyIpNow
      } catch {
        Log "ERRO no Heartbeat POST: $($_.Exception.Message)"
      }

      Write-Host ""
    }

  } catch {
    Log "ERRO principal: $($_.Exception.Message)"
    Write-Host ""
    Write-Host "ERRO: $($_.Exception.Message)"
    Write-Host "Vou tentar novamente..."
    CountdownInline 10 "Reiniciando"
  }

  CountdownInline 3 "Reabrindo tunel"
}
