# ddns-client.ps1
# Atualiza IPv4 e IPv6 no endpoint, em loop.
# Log local: (pasta do PS1)\.ddns_state\ddns-client.log

$Endpoint = "https://conexaovida.org/no-ip-dynamic_ip.php"
$Token    = "ddns_9XJkP8Qm7Vt2Rz4Hq1cN6aY0sL3uF5eD8bG2wK9pT7nM4xZ1vS6rQ0hE3yU"
$HostName = "casa"
$IntervalSeconds = 300

# Porta que você mais usa (apenas para exibir nas orientações)
$ExemploPorta = 8080

# --- Interface/Orientações ---
$Title = "Atualizando IP dinamico para o deste PC - acesse via http://conexaovida.org/no-ip-dynamic_ip.php?port=PORTA_QUE_DESEJA"
$Host.UI.RawUI.WindowTitle = $Title

# Pasta local para log/estado (ao lado do PS1)
$LocalStateDir = Join-Path -Path (Split-Path -Parent $PSCommandPath) -ChildPath ".ddns_state"
$LogFile = Join-Path -Path $LocalStateDir -ChildPath "ddns-client.log"

function Ensure-Dir([string]$dir) {
  try { New-Item -ItemType Directory -Force -Path $dir | Out-Null } catch {}
}

function Log([string]$msg) {
  $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  $line = "[$stamp] $msg"
  Write-Host $line
  try { Add-Content -Path $LogFile -Value $line -Encoding UTF8 } catch {}
}

function Get-PublicIP4 {
  try {
    $ip = (& curl.exe -4 -s "https://api.ipify.org").Trim()
    if ($ip -and ($ip -match '^\d{1,3}(\.\d{1,3}){3}$')) { return $ip }
  } catch {}
  return $null
}

function Get-PublicIP6 {
  try {
    $ip = (& curl.exe -6 -s "https://api64.ipify.org").Trim()
    if ($ip -and ($ip -match ':')) { return $ip }
  } catch {}
  return $null
}

function Post-UpdateIP([string]$ip) {
  $bodyObj = @{ token = $Token; host = $HostName; ip = $ip }
  $bodyJson = $bodyObj | ConvertTo-Json -Compress
  return Invoke-RestMethod -Method Post -Uri $Endpoint -ContentType "application/json" -Body $bodyJson -TimeoutSec 20
}

function Print-Header {
  Clear-Host
  Write-Host "======================================================================="
  Write-Host " DDNS (NO-IP INTERNO) - CONEXAOVIDA"
  Write-Host "======================================================================="
  Write-Host ""
  Write-Host "O que este script faz?"
  Write-Host " - Ele detecta o IP publico deste PC (IPv4 e, se existir, IPv6)."
  Write-Host " - Ele envia esse IP ao servidor ConexaoVida (endpoint PHP)."
  Write-Host " - Assim, voce consegue acessar servicos deste PC mesmo com IP dinamico."
  Write-Host ""
  Write-Host "Como acessar o PC remotamente (via redirect no servidor):"
  Write-Host " - Abra no navegador:"
  Write-Host "   http://conexaovida.org/no-ip-dynamic_ip.php?port=PORTA_QUE_DESEJA"
  Write-Host ""
  Write-Host "Exemplo (porta $ExemploPorta):"
  Write-Host "   http://conexaovida.org/no-ip-dynamic_ip.php?port=$ExemploPorta"
  Write-Host ""
  Write-Host "Onde fica o log deste script:"
  Write-Host " - $LogFile"
  Write-Host ""
  Write-Host "Como parar:"
  Write-Host " - Feche esta janela (ou Ctrl+C)."
  Write-Host ""
  Write-Host "Intervalo de atualizacao:"
  Write-Host " - A cada $IntervalSeconds segundos."
  Write-Host ""
  Write-Host "Obs.: durante a espera, o contador abaixo diminui para mostrar que esta ativo."
  Write-Host ""
  Write-Host "======================================================================="
  Write-Host ""
}

function Countdown([int]$seconds) {
  # Atualiza a MESMA linha, sem criar novas linhas (usa `r + -NoNewline)
  for ($i = $seconds; $i -ge 1; $i--) {
    $line = ("Proxima atualizacao em {0}s..." -f $i).PadRight(50)
    Write-Host -NoNewline ("`r" + $line)
    Start-Sleep -Seconds 1
  }
  # Finaliza a linha e pula para a próxima (para o próximo bloco de logs ficar limpo)
  Write-Host ""
  Write-Host ""
}

Ensure-Dir $LocalStateDir
Print-Header
Log "Iniciando DDNS client. Endpoint=$Endpoint Host=$HostName Interval=${IntervalSeconds}s"

while ($true) {
  try {
    $ip4 = Get-PublicIP4
    if ($ip4) {
      $r4 = Post-UpdateIP $ip4
      Log "IPv4=$ip4 -> ok=$($r4.ok) changed=$($r4.data.changed) ip4_saved=$($r4.data.ip4)"
      Write-Host ("[IPv4] {0}  (changed={1})" -f $ip4, $r4.data.changed)
    } else {
      Log "IPv4: não consegui obter (possível ausência de IPv4 público/CGNAT)."
      Write-Host "[IPv4] indisponivel"
    }

    $ip6 = Get-PublicIP6
    if ($ip6) {
      $r6 = Post-UpdateIP $ip6
      Log "IPv6=$ip6 -> ok=$($r6.ok) changed=$($r6.data.changed) ip6_saved=$($r6.data.ip6)"
      Write-Host ("[IPv6] {0}  (changed={1})" -f $ip6, $r6.data.changed)
    } else {
      Log "IPv6: não disponível/sem conectividade."
      Write-Host "[IPv6] indisponivel"
    }

  } catch {
    Log "ERRO: $($_.Exception.Message)"
    Write-Host ("ERRO: {0}" -f $_.Exception.Message)
    Write-Host ""
  }

  Countdown $IntervalSeconds
}
