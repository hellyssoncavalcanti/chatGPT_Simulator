# sync_github.ps1 — arquivo versionado do sync automático do Windows
# Responsável por: merge opcional do PR mais recente, sync de arquivos e reinício coordenado dos processos locais.
# Log: cria um único arquivo de log por sessão (sync_github-YYYYMMDD-HHmmss.log) e
#       reutiliza-o em todos os ciclos subsequentes (modo --scheduled), com separador visual entre ciclos.
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RemainingArgs
)

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ErrorActionPreference = 'Stop'

$script:IsScheduled = $false
$script:InstallTask = $false
$script:UninstallTask = $false
$script:RestartRequested = $false
$script:RestartScope = 'none'
$script:RestartTargets = New-Object System.Collections.Generic.List[string]
$script:UpdatedFiles = New-Object System.Collections.Generic.List[string]
$script:AddedFiles = New-Object System.Collections.Generic.List[string]
$script:ProtectedFiles = New-Object System.Collections.Generic.List[string]
$script:LogFile = $null
$script:SessionLogFile = $null
$script:CycleCount = 0
$script:RepoMirror = $null
$script:GitExe = $null
$script:Config = [ordered]@{}
$script:LastMergeInfo = $null
$script:CanWriteRepo = $true
$script:GitHubAuthFailed = $false

foreach ($arg in ($RemainingArgs | Where-Object { $_ -and $_.Trim() })) {
    switch ($arg.ToLowerInvariant()) {
        '--scheduled'     { $script:IsScheduled = $true }
        'install-task'    { $script:InstallTask = $true }
        '--install-task'  { $script:InstallTask = $true }
        'uninstall-task'  { $script:UninstallTask = $true }
        '--uninstall-task'{ $script:UninstallTask = $true }
    }
}

function Write-Section([string]$Title) {
    Write-Host ""
    Write-Host "=== $Title ===" -ForegroundColor Cyan
    Write-Log "=== $Title ==="
}

function Write-Info {
    param(
        [string]$Message,
        [ConsoleColor]$Color = [ConsoleColor]::Gray,
        [switch]$NoNewline
    )
    if ($NoNewline) {
        Write-Host "[INFO] $Message " -ForegroundColor $Color -NoNewline
    } else {
        Write-Host "[INFO] $Message" -ForegroundColor $Color
    }
    Write-Log "[INFO] $Message"
}

function Write-Ok([string]$Message) {
    Write-Host "[OK]   $Message" -ForegroundColor Green
    Write-Log "[OK]   $Message"
}

function Write-Warn([string]$Message) {
    Write-Host "[WARN] $Message" -ForegroundColor Yellow
    Write-Log "[WARN] $Message"
}

function Write-Fail([string]$Message) {
    Write-Host "[ERRO] $Message" -ForegroundColor Red
    Write-Log "[ERRO] $Message"
}

function Disable-GitHubAuth([string]$Reason = '') {
    if (-not $script:GitHubAuthFailed) {
        if ([string]::IsNullOrWhiteSpace($Reason)) {
            Write-Warn 'Credenciais GitHub inválidas/expiradas. A etapa de API será ignorada neste ciclo.'
        } else {
            Write-Warn ("Credenciais GitHub inválidas/expiradas. Motivo: {0}. A etapa de API será ignorada neste ciclo." -f $Reason)
        }
    }
    $script:GitHubAuthFailed = $true
    $script:CanWriteRepo = $false
    if ($script:Config -and $script:Config.headers) {
        $script:Config.headers.Remove('Authorization') | Out-Null
    }
    Show-GitHubCredentialFixGuide
}

function Show-GitHubCredentialFixGuide {
    Write-Warn 'Como corrigir credenciais GitHub (passo a passo):'
    Write-Warn '1) Acesse: https://github.com/settings/personal-access-tokens/new'
    Write-Warn '2) Crie um token Fine-grained para o repositório alvo.'
    Write-Warn '3) Permissões mínimas: Contents=Read and write, Pull requests=Read and write.'
    Write-Warn '4) Edite Scripts\sync_github_settings.ps1 e ajuste:'
    Write-Warn '   - $githubToken = ''<seu_token>'''
    Write-Warn '   - $ghUser = ''<seu_usuario_ou_org>'''
    Write-Warn '   - $repo / $branch conforme seu repositório.'
    Write-Warn '5) Salve o arquivo e execute novamente: sync_github.bat'
    Write-Warn 'Documentação: https://docs.github.com/rest'
}

function Write-Log([string]$Message) {
    if (-not $script:LogFile) { return }
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    "$ts $Message" | Out-File -FilePath $script:LogFile -Append -Encoding utf8
}

function Resolve-TempRoot {
    $candidates = @(
        $env:TEMP,
        $env:TMP,
        $env:TMPDIR,
        [System.IO.Path]::GetTempPath()
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }

    $tempRoot = $candidates | Select-Object -First 1
    if (-not $tempRoot) {
        throw 'Nao foi possivel determinar uma pasta temporaria para o sync.'
    }

    return [System.IO.Path]::GetFullPath($tempRoot)
}

function Test-IsPlaceholderValue([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $false
    }

    return $Value -in @(
        'COLE_SEU_TOKEN_AQUI',
        'seu_usuario_ou_org'
    )
}

function Normalize-RelativePath([string]$PathValue) {
    if ([string]::IsNullOrWhiteSpace($PathValue)) {
        return ''
    }
    return $PathValue.Trim('"').Replace('/', '\').Replace('\\', '\').TrimStart('.', '\').ToLowerInvariant()
}

function Test-IsRestartIgnoredPath([string]$NormalizedPath) {
    if ([string]::IsNullOrWhiteSpace($NormalizedPath)) { return $false }
    $p = Normalize-RelativePath $NormalizedPath
    if ([string]::IsNullOrWhiteSpace($p)) { return $false }

    # README.md (em qualquer pasta) e scripts do próprio sync não devem,
    # sozinhos, disparar reinício local.
    if ($p -match '(^|\\)readme\.md$') { return $true }
    if ($p -eq (Normalize-RelativePath 'scripts\sync_github.py')) { return $true }
    if ($p -eq (Normalize-RelativePath 'scripts\sync_github.ps1')) { return $true }
    return $false
}

function Add-RestartTarget([string]$Target) {
    if ([string]::IsNullOrWhiteSpace($Target)) { return }
    if ($script:RestartTargets -notcontains $Target) {
        $script:RestartTargets.Add($Target)
    }
}

function Initialize-Logging {
    $logDir = Join-Path $script:Config.localDir 'logs'
    if (-not (Test-Path $logDir)) {
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    }
    # Reutiliza o log da primeira execução da sessão (mesmo comportamento dos
    # outros sistemas). Apenas o primeiro ciclo cria o arquivo; os demais
    # continuam escrevendo no mesmo log.
    if (-not $script:SessionLogFile) {
        $script:SessionLogFile = Join-Path $logDir ("sync_github-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    }
    $script:LogFile = $script:SessionLogFile
    Write-Info "Log em $($script:LogFile)"
}

function Get-ConfigPySettings {
    param(
        [string]$ScriptDir,
        [string]$LocalDirHint
    )
    $result = @{}
    try {
        $configPath = Join-Path $ScriptDir 'config.py'
        if (-not (Test-Path $configPath) -and $LocalDirHint) {
            $candidate = Join-Path $LocalDirHint 'Scripts\config.py'
            if (Test-Path $candidate) { $configPath = $candidate }
        }
        if (-not (Test-Path $configPath)) { return $result }

        $pyCode = @"
import json, importlib.util
cfg_path = r'''$configPath'''
spec = importlib.util.spec_from_file_location('sim_cfg', cfg_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
keys = [
    'GITHUB_TOKEN','GH_USER','GITHUB_USER','GITHUB_REPO','GITHUB_BRANCH',
    'BASE_DIR','GITHUB_LOCAL_DIR','GITHUB_TASK_NAME','GITHUB_SYNC_INTERVAL_MINUTES',
    'GITHUB_CHAT_PROCESS_PATTERN','GITHUB_ANALYZER_PATTERN','GITHUB_REMOTE_PHP_API_KEY'
]
data = {k: getattr(mod, k, None) for k in keys}
print(json.dumps(data, ensure_ascii=False))
"@
        $jsonOut = (& python -c $pyCode 2>$null) | Out-String
        if ([string]::IsNullOrWhiteSpace($jsonOut)) { return $result }
        $parsed = $jsonOut | ConvertFrom-Json -ErrorAction Stop
        if ($parsed) {
            if ($parsed.GITHUB_TOKEN) { $result.githubToken = [string]$parsed.GITHUB_TOKEN }
            if ($parsed.GH_USER) { $result.ghUser = [string]$parsed.GH_USER }
            elseif ($parsed.GITHUB_USER) { $result.ghUser = [string]$parsed.GITHUB_USER }
            if ($parsed.GITHUB_REPO) { $result.repo = [string]$parsed.GITHUB_REPO }
            if ($parsed.GITHUB_BRANCH) { $result.branch = [string]$parsed.GITHUB_BRANCH }
            if ($parsed.BASE_DIR) { $result.localDir = [string]$parsed.BASE_DIR }
            if ($parsed.GITHUB_LOCAL_DIR) { $result.localDir = [string]$parsed.GITHUB_LOCAL_DIR }
            if ($parsed.GITHUB_TASK_NAME) { $result.taskName = [string]$parsed.GITHUB_TASK_NAME }
            if ($parsed.GITHUB_SYNC_INTERVAL_MINUTES) { $result.syncIntervalMinutes = [int]$parsed.GITHUB_SYNC_INTERVAL_MINUTES }
            if ($parsed.GITHUB_CHAT_PROCESS_PATTERN) { $result.chatProcessPattern = [string]$parsed.GITHUB_CHAT_PROCESS_PATTERN }
            if ($parsed.GITHUB_ANALYZER_PATTERN) { $result.analyzerPattern = [string]$parsed.GITHUB_ANALYZER_PATTERN }
            if ($parsed.GITHUB_REMOTE_PHP_API_KEY) { $result.remotePhpApiKey = [string]$parsed.GITHUB_REMOTE_PHP_API_KEY }
        }
    } catch {
        # Fallback silencioso: sync continua com settings/env.
    }
    return $result
}

function Import-Settings {
    $scriptPath = $PSCommandPath
    if ([string]::IsNullOrWhiteSpace($scriptPath)) {
        $scriptPath = $MyInvocation.MyCommand.Path
    }
    if ([string]::IsNullOrWhiteSpace($scriptPath)) {
        throw 'Nao foi possivel resolver o caminho do proprio script sync_github.ps1.'
    }

    $scriptDir = Split-Path -Parent $scriptPath

    $defaults = [ordered]@{
        githubToken         = $env:CHATGPT_SIMULATOR_GITHUB_TOKEN
        ghUser              = $env:CHATGPT_SIMULATOR_GITHUB_USER
        repo                = if ($env:CHATGPT_SIMULATOR_GITHUB_REPO) { $env:CHATGPT_SIMULATOR_GITHUB_REPO } else { 'chatGPT_Simulator' }
        branch              = if ($env:CHATGPT_SIMULATOR_GITHUB_BRANCH) { $env:CHATGPT_SIMULATOR_GITHUB_BRANCH } else { 'main' }
        localDir            = if ($env:CHATGPT_SIMULATOR_DIR) { $env:CHATGPT_SIMULATOR_DIR } else { 'C:\chatgpt_simulator' }
        taskName            = 'chatGPT_Simulator_AutoSync'
        syncIntervalMinutes = 10
        chatProcessPattern  = 'Scripts\\main.py'
        analyzerPattern     = 'Scripts\\analisador_prontuarios.py'
        pywaPattern         = 'Scripts\\acompanhamento_whatsapp.py'
        autoDevAgentPattern = 'Scripts\\auto_dev_agent.py'
        autoDevAgentBat     = '3. start_agente_autonomo.bat'
        autoDevAgentWindowTitle = 'Agente Autonomo de Melhoria Continua'
        whatsappServerBat   = '2. Start_Whatsapp_Server.bat'
        whatsappWindowTitle = 'WhatsApp Follow-up Server (Web)'
        remotePhpSaveUrl    = if ($env:CHATGPT_SIMULATOR_REMOTE_PHP_SAVE_URL) { $env:CHATGPT_SIMULATOR_REMOTE_PHP_SAVE_URL } else { 'https://conexaovida.org/editar_php.php?action=save_file_remote' }
        remotePhpApiKey      = if ($env:CHATGPT_SIMULATOR_REMOTE_PHP_API_KEY) { $env:CHATGPT_SIMULATOR_REMOTE_PHP_API_KEY } else { 'CVAPI_2b9c80c2abf94a76baf8b3e68d89cb7e' }
        remotePhpLocalFile   = if ($env:CHATGPT_SIMULATOR_REMOTE_PHP_LOCAL_FILE) { $env:CHATGPT_SIMULATOR_REMOTE_PHP_LOCAL_FILE } else { 'chatgpt_integracao_criado_pelo_gemini.js.php' }
        remotePhpTargetPath  = if ($env:CHATGPT_SIMULATOR_REMOTE_PHP_TARGET_PATH) { $env:CHATGPT_SIMULATOR_REMOTE_PHP_TARGET_PATH } else { 'scripts/js/chatgpt_integracao_criado_pelo_gemini.js.php' }
        remotePhpLocalFile2  = if ($env:CHATGPT_SIMULATOR_REMOTE_PHP_LOCAL_FILE_2) { $env:CHATGPT_SIMULATOR_REMOTE_PHP_LOCAL_FILE_2 } else { 'chatgpt_free_openai.js.php' }
        remotePhpTargetPath2 = if ($env:CHATGPT_SIMULATOR_REMOTE_PHP_TARGET_PATH_2) { $env:CHATGPT_SIMULATOR_REMOTE_PHP_TARGET_PATH_2 } else { 'scripts/js/chatgpt_free_openai.js.php' }
    }

    foreach ($key in $defaults.Keys) {
        $script:Config[$key] = $defaults[$key]
    }

    # Configuração central em Scripts/config.py (com fallback para env/defaults).
    $cfgPy = Get-ConfigPySettings -ScriptDir $scriptDir -LocalDirHint $script:Config.localDir
    foreach ($key in @('githubToken', 'ghUser', 'repo', 'branch', 'localDir', 'taskName', 'syncIntervalMinutes', 'chatProcessPattern', 'analyzerPattern', 'remotePhpApiKey')) {
        if ($cfgPy.ContainsKey($key) -and -not [string]::IsNullOrWhiteSpace([string]$cfgPy[$key])) {
            if ($key -eq 'syncIntervalMinutes') {
                $script:Config[$key] = [int]$cfgPy[$key]
            } else {
                $script:Config[$key] = [string]$cfgPy[$key]
            }
        }
    }

    # Fallback resiliente: se o dot-sourcing não popular variáveis (escopo/encoding),
    # tenta extrair pares "$chave = valor" diretamente do arquivo de settings.
    if (Test-Path $settingsPath) {
        try {
            $rawSettings = Get-Content -Path $settingsPath -Raw -Encoding UTF8
            if (-not [string]::IsNullOrWhiteSpace($rawSettings)) {
                $rawMap = @{}
                $matches = [regex]::Matches($rawSettings, '(?im)^\s*\$(\w+)\s*=\s*(.+?)\s*$')
                foreach ($m in $matches) {
                    $k = [string]$m.Groups[1].Value
                    $vRaw = [string]$m.Groups[2].Value
                    $v = $vRaw.Trim()
                    if (($v.StartsWith("'") -and $v.EndsWith("'")) -or ($v.StartsWith('"') -and $v.EndsWith('"'))) {
                        $v = $v.Substring(1, $v.Length - 2)
                    }
                    if (-not [string]::IsNullOrWhiteSpace($k)) {
                        $rawMap[$k.ToLowerInvariant()] = $v
                    }
                }

                $fallbackKeys = @(
                    @{ cfg = 'githubToken'; raw = 'githubtoken' },
                    @{ cfg = 'ghUser'; raw = 'ghuser' },
                    @{ cfg = 'repo'; raw = 'repo' },
                    @{ cfg = 'branch'; raw = 'branch' },
                    @{ cfg = 'localDir'; raw = 'localdir' },
                    @{ cfg = 'taskName'; raw = 'taskname' },
                    @{ cfg = 'syncIntervalMinutes'; raw = 'syncintervalminutes' },
                    @{ cfg = 'chatProcessPattern'; raw = 'chatprocesspattern' },
                    @{ cfg = 'analyzerPattern'; raw = 'analyzerpattern' },
                    @{ cfg = 'remotePhpApiKey'; raw = 'remotephpapikey' }
                )

                foreach ($entry in $fallbackKeys) {
                    $cfgKey = [string]$entry.cfg
                    $rawKey = [string]$entry.raw
                    if (-not $rawMap.ContainsKey($rawKey)) { continue }
                    $curr = ''
                    if ($script:Config.Contains($cfgKey) -and $null -ne $script:Config[$cfgKey]) {
                        $curr = [string]$script:Config[$cfgKey]
                    }
                    if ([string]::IsNullOrWhiteSpace($curr) -or (Test-IsPlaceholderValue $curr)) {
                        $script:Config[$cfgKey] = $rawMap[$rawKey]
                    }
                }
            }
        } catch {
            Write-Warn "Falha ao aplicar fallback de leitura direta do settings: $($_.Exception.Message)"
        }
    }

    if (Test-IsPlaceholderValue $script:Config.githubToken) {
        $script:Config.githubToken = $null
    }
    
    if (Test-IsPlaceholderValue $script:Config.ghUser) {
        throw "Configuracao invalida em Scripts/config.py: preencha GH_USER (ou CHATGPT_SIMULATOR_GITHUB_USER) com o usuario real do GitHub."
    }

    $script:Config.scriptDir = $scriptDir
    $script:Config.syncBatPath = Join-Path $script:Config.localDir 'sync_github.bat'
    $script:Config.syncPs1Path = Join-Path $script:Config.localDir 'Scripts\sync_github.ps1'
    $script:Config.settingsPath = Join-Path $scriptDir 'config.py'
    $tempRoot = Resolve-TempRoot
    $script:Config.tempDir = Join-Path $tempRoot 'sync_chatgpt'
    $script:Config.lockFile = Join-Path $script:Config.tempDir 'sync_github.lock'
    $script:Config.apiBase = if ($script:Config.ghUser -and $script:Config.repo) { "https://api.github.com/repos/$($script:Config.ghUser)/$($script:Config.repo)" } else { $null }
    $script:Config.headers = @{
        Accept       = 'application/vnd.github+json'
        'User-Agent' = 'chatGPT_Simulator-sync'
    }
    if ($script:Config.githubToken) {
        $script:Config.headers.Authorization = "Bearer $($script:Config.githubToken)"
    }
    $script:Config.protectedItems = @(
        'sync_github.bat',
        'Scripts\sync_github.ps1',
        'chrome_profile'
    )
    # Garante que Scripts\sync_github.py nunca fique protegido contra update.
    $script:Config.protectedItems = @($script:Config.protectedItems | Where-Object { (Normalize-RelativePath $_) -ne (Normalize-RelativePath 'Scripts\sync_github.py') })
}

function Assert-Configuration {
    $missing = @()
    foreach ($field in @('ghUser', 'repo', 'branch', 'localDir')) {
        if ([string]::IsNullOrWhiteSpace($script:Config[$field])) { $missing += $field }
    }
    if ($missing.Count -gt 0) {
        throw "Configuracao obrigatoria ausente: $($missing -join ', ')."
    }
}

function Acquire-Lock {
    if (-not (Test-Path $script:Config.tempDir)) {
        New-Item -ItemType Directory -Path $script:Config.tempDir -Force | Out-Null
    }

    $myPid = $PID
    $myParent = $null
    try {
        $myParent = (Get-CimInstance Win32_Process -Filter "ProcessId = $myPid").ParentProcessId
    } catch { }

    $killedOthers = $false

    $syncCmds = Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'cmd' -and $_.CommandLine -match 'sync_github\.bat' }
    foreach ($c in $syncCmds) {
        if ($myParent -and $c.ProcessId -eq $myParent) { continue }
        try {
            # REMOVIDO O PARÂMETRO /T (Tree Kill)
            # Assim mata APENAS o CMD do sync, sem tocar nos servidores BAT associados!
            & taskkill.exe /F /PID $c.ProcessId 2>&1 | Out-Null
            Write-Info "Substituindo janela CMD de sync anterior (PID $($c.ProcessId))"
            $killedOthers = $true
        } catch { }
    }

    $syncPs = Get-CimInstance Win32_Process | Where-Object { ($_.Name -match 'powershell' -or $_.Name -match 'pwsh') -and $_.CommandLine -match 'sync_github\.ps1' }
    foreach ($p in $syncPs) {
        if ($p.ProcessId -eq $myPid) { continue }
        try {
            # REMOVIDO O PARÂMETRO /T (Tree Kill)
            # Assim mata APENAS o PowerShell do sync, preservando os processos filhos!
            & taskkill.exe /F /PID $p.ProcessId 2>&1 | Out-Null
            Write-Info "Substituindo processo PowerShell de sync anterior (PID $($p.ProcessId))"
            $killedOthers = $true
        } catch { }
    }

    if (Test-Path $script:Config.lockFile) {
        Remove-Item $script:Config.lockFile -Force -ErrorAction SilentlyContinue
    }
    Set-Content -Path $script:Config.lockFile -Value $myPid -Encoding ascii
    
    if ($killedOthers) {
        Write-Ok "Processos de sync conflitantes foram anulados. Assumindo exclusividade do ciclo."
    }
}
function Release-Lock {
    if (Test-Path $script:Config.lockFile) {
        Remove-Item $script:Config.lockFile -Force -ErrorAction SilentlyContinue
    }
}

function Get-GitExe {
    foreach ($candidate in @('git.exe', 'git')) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    foreach ($path in @('C:\Program Files\Git\cmd\git.exe', 'C:\Program Files\Git\bin\git.exe', 'C:\Program Files (x86)\Git\cmd\git.exe')) {
        if (Test-Path $path) { return $path }
    }
    throw 'Git nao encontrado. Instale o Git for Windows antes de usar a automacao.'
}

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)][string[]]$Args,
        [string]$WorkingDirectory = $script:Config.tempDir,
        [switch]$AllowFailure,
        [switch]$ShowProgress,
        [string]$ProgressMessage = "Processando Git"
    )

    Push-Location $WorkingDirectory
    try {
        $previousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        
        if ($ShowProgress) {
            Write-Host -NoNewline "[INFO] $ProgressMessage [░░░░░░░░░░░░░░░░░░░░] 0% " -ForegroundColor Cyan
            Write-Log "[INFO] $ProgressMessage (Iniciando...)"
        }

        try {
            $output = & $script:GitExe @Args 2>&1 | ForEach-Object {
                $line = $_.ToString()
                if ($ShowProgress -and $line -match '(\d{1,3})%') {
                    $pct = [int]$matches[1]
                    $blocks = [math]::Floor($pct / 5)
                    $spaces = 20 - $blocks
                    
                    $bar = ('█' * $blocks) + ('░' * $spaces)
                    
                    Write-Host -NoNewline "`r[INFO] $ProgressMessage [$bar] $pct%       " -ForegroundColor Cyan
                }
                $line
            }
            if ($ShowProgress) { 
                Write-Host -NoNewline "`r[INFO] $ProgressMessage [████████████████████] 100%      " -ForegroundColor Cyan
                Write-Host ""
                Write-Log "[INFO] $ProgressMessage (Concluído)"
            }
            $exitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }

        if (-not $AllowFailure -and $exitCode -ne 0) {
            $joinedOutput = ($output -join [Environment]::NewLine)
            throw ("Git falhou: {0}{1}{2}" -f ($Args -join ' '), [Environment]::NewLine, $joinedOutput)
        }
        return $output
    } finally {
        Pop-Location
    }
}

function Get-RepoUrlForClone {
    if ($script:Config.githubToken -and -not $script:GitHubAuthFailed) {
        return "https://$($script:Config.ghUser):$($script:Config.githubToken)@github.com/$($script:Config.ghUser)/$($script:Config.repo).git"
    }
    return "https://github.com/$($script:Config.ghUser)/$($script:Config.repo).git"
}

function Invoke-GitHubApi {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [ValidateSet('Get','Post','Put','Patch','Delete')][string]$Method = 'Get',
        $Body = $null
    )

    if (-not $script:Config.githubToken) {
        throw 'GitHub token ausente. A automacao de PR requer token com Contents=Read/Write e Pull requests=Read/Write.'
    }

    $params = @{
        Uri         = $Uri
        Method      = $Method
        Headers     = $script:Config.headers
        ErrorAction = 'Stop'
    }
    if ($null -ne $Body) {
        # PowerShell 5.1 no Windows pode serializar Body em encoding ANSI/Windows-1252
        # quando recebe string diretamente; para evitar HTTP 400 em payload com acentos/
        # emojis, enviamos bytes UTF-8 explicitamente.
        $jsonBody = ($Body | ConvertTo-Json -Depth 10)
        $params.Body = [System.Text.Encoding]::UTF8.GetBytes($jsonBody)
        $params.ContentType = 'application/json; charset=utf-8'
    }

    try {
        return Invoke-RestMethod @params
    } catch {
        $errorMessage = $_.Exception.Message
        $responseBody = $null
        $statusCode = $null

        try {
            $response = $_.Exception.Response
            try { $statusCode = [int]$response.StatusCode.value__ } catch { }
            if ($response -and $response.GetResponseStream) {
                $stream = $response.GetResponseStream()
                if ($stream) {
                    $reader = New-Object System.IO.StreamReader($stream)
                    $responseBody = $reader.ReadToEnd()
                    $reader.Dispose()
                    $stream.Dispose()
                }
            }
        } catch { }

        $bodyText = [string]$responseBody
        if ($statusCode -in @(401, 403) -or $errorMessage -match '\(401\)|\(403\)' -or $bodyText -match 'Bad credentials') {
            Disable-GitHubAuth -Reason ("HTTP {0} / {1}" -f $statusCode, ($bodyText -replace '\s+', ' ').Trim())
        }

        if (-not [string]::IsNullOrWhiteSpace($responseBody)) {
            throw ("{0} | Body: {1}" -f $errorMessage, $responseBody)
        }
        throw
    }
}

function New-PullRequestsForPendingBranches {
    <#
    .SYNOPSIS
        Detecta branches sem PR aberto (prefixos claude/, codex/, chatgpt/) e cria PRs
        automaticamente com titulo e corpo baseados nos commits reais da branch.
    #>
    if (-not $script:Config.githubToken -or $script:GitHubAuthFailed) { return }

    $branchesComPr = @()
    try {
        $prsResponse = Invoke-GitHubApi -Uri "$($script:Config.apiBase)/pulls?state=open&base=$($script:Config.branch)&per_page=100"
        $prsArray = @($prsResponse)
        if ($prsArray.Count -eq 1 -and $prsArray[0] -is [System.Array]) { $prsArray = @($prsArray[0]) }
        foreach ($p in $prsArray) {
            if ($p -and $p.head -and $p.head.ref) { $branchesComPr += $p.head.ref }
        }
    } catch {
        Write-Warn "Falha ao listar PRs abertos: $($_.Exception.Message). Continuando mesmo assim..."
    }

    try {
        $branchesResponse = Invoke-GitHubApi -Uri "$($script:Config.apiBase)/branches?per_page=100"
    } catch {
        Write-Warn "Falha ao listar branches: $($_.Exception.Message)"
        return
    }

    $todasBranches = @($branchesResponse)
    if ($todasBranches.Count -eq 1 -and $todasBranches[0] -is [System.Array]) { $todasBranches = @($todasBranches[0]) }

    $candidatas = @($todasBranches | Where-Object {
        -not [string]::IsNullOrWhiteSpace($_.name) -and
        $_.name -ne $script:Config.branch -and
        $branchesComPr -notcontains $_.name -and
        $_.name -match '^(claude|codex|chatgpt)/'
    })

    if ($candidatas.Count -eq 0) {
        Write-Info 'Nenhuma branch pendente de PR encontrada.'
        return
    }

    Write-Info "Avaliando $($candidatas.Count) branch(es) candidata(s) para PR automatico..."

    # Obter datas de cada branch para ordenar e identificar a mais recente
    $listaComDatas = @()
    $i = 0
    foreach ($br in $candidatas) {
        $i++
        $pct = [math]::Floor(($i / $candidatas.Count) * 100)
        $blocks = [math]::Floor($pct / 5); $spaces = 20 - $blocks
        $bar = ('█' * $blocks) + ('░' * $spaces)
        Write-Host -NoNewline "`r[INFO] Processando branches [$bar] $pct%       " -ForegroundColor Cyan
        try {
            $commitData = Invoke-GitHubApi -Uri $br.commit.url
            $data = [datetime]::Parse($commitData.commit.committer.date)
            $listaComDatas += @{ name = $br.name; date = $data }
        } catch {
            Write-Log "[WARN] Falha ao obter data do commit da branch '$($br.name)': $($_.Exception.Message)"
            # Fallback: inclui a branch com data atual para nao perder a candidata
            $listaComDatas += @{ name = $br.name; date = (Get-Date) }
        }
    }
    Write-Host ""

    if ($listaComDatas.Count -eq 0) {
        Write-Warn 'Nenhuma branch candidata pôde ser processada.'
        return
    }

    $listaOrdenada = $listaComDatas | Sort-Object date -Descending

    Write-Info "Criando PRs para $($listaOrdenada.Count) branch(es) pendente(s)..."

    foreach ($item in $listaOrdenada) {
        $nomeBranch = $item.name
        if ([string]::IsNullOrWhiteSpace($nomeBranch)) { continue }

        Write-Info "Processando branch '$nomeBranch' ($($item.date))..." -Color Cyan

        # Buscar commits da branch em relacao ao base para montar titulo e corpo do PR
        $prTitle = "Merge automatico: $nomeBranch"
        $prBodyLines = @("PR gerado automaticamente pelo sync para a branch ``$nomeBranch``.", "")

        try {
            $compare = Invoke-GitHubApi -Uri "$($script:Config.apiBase)/compare/$($script:Config.branch)...$($nomeBranch)"
            $commits = @($compare.commits)

            if ($commits.Count -gt 0) {
                # Titulo: usar a mensagem do primeiro commit (primeira linha)
                $firstMsg = ($commits[0].commit.message -split "`n")[0].Trim()
                if ($commits.Count -eq 1) {
                    $prTitle = $firstMsg
                } else {
                    # Multiplos commits: titulo resumido
                    $prTitle = $firstMsg
                    if ($prTitle.Length -gt 65) {
                        $prTitle = $prTitle.Substring(0, 62) + '...'
                    }
                    $prTitle = "$prTitle (+$($commits.Count - 1))"
                }

                $prBodyLines += "## Commits"
                $prBodyLines += ""
                foreach ($c in $commits) {
                    $msg = ($c.commit.message -split "`n")[0].Trim()
                    $sha = $c.sha.Substring(0, 7)
                    $prBodyLines += "- ``$sha`` $msg"
                }
                $prBodyLines += ""

                # Listar arquivos alterados
                $files = @($compare.files)
                if ($files.Count -gt 0) {
                    $prBodyLines += "## Arquivos alterados ($($files.Count))"
                    $prBodyLines += ""
                    foreach ($f in $files) {
                        $statusIcon = switch ($f.status) {
                            'added'    { '🆕' }
                            'removed'  { '🗑️' }
                            'modified' { '✏️' }
                            'renamed'  { '📝' }
                            default    { '📄' }
                        }
                        $prBodyLines += "- $statusIcon ``$($f.filename)`` (+$($f.additions) -$($f.deletions))"
                    }
                }
            } elseif ($commits.Count -eq 0) {
                Write-Info "Branch '$nomeBranch' sem commits novos em relacao ao base. Pulando." -Color DarkGray
                continue
            }
        } catch {
            Write-Warn "Nao foi possivel obter commits da branch '$nomeBranch': $($_.Exception.Message)"
        }

        $prBody = $prBodyLines -join "`n"

        try {
            $newPr = Invoke-GitHubApi -Uri "$($script:Config.apiBase)/pulls" -Method Post -Body @{
                title = $prTitle
                head  = $nomeBranch
                base  = $script:Config.branch
                body  = $prBody
            }
            Write-Ok "PR #$($newPr.number) criado para '$nomeBranch': $prTitle"
        } catch {
            $errMsg = $_.Exception.Message
            if ($errMsg -match 'No commits between' -or $errMsg -match 'already exists' -or $errMsg -match 'A pull request already exists') {
                Write-Info "Branch '$nomeBranch' sem modificacoes reais ou PR ja existe." -Color DarkGray
            } else {
                Write-Warn "Falha ao criar PR para '$nomeBranch': $errMsg"
            }
        }
    }
}

function Merge-AllPullRequests {
    Write-Section 'PULL REQUESTS'

    if (-not $script:Config.githubToken -or $script:GitHubAuthFailed) {
        $script:CanWriteRepo = $false
        if (-not $script:Config.githubToken) {
            Write-Warn 'Token GitHub nao configurado; etapa de PR sera ignorada, mas o sync dos arquivos ainda sera tentado.'
            Show-GitHubCredentialFixGuide
        } else {
            Write-Warn 'Token GitHub invalido/expirado; etapa de PR sera ignorada, mas o sync dos arquivos ainda sera tentado.'
            Show-GitHubCredentialFixGuide
        }
        return
    }

    # Criar PRs automaticamente para TODAS as branches pendentes (claude/, codex/, chatgpt/)
    New-PullRequestsForPendingBranches

    try {
        $prsResponse = Invoke-GitHubApi -Uri "$($script:Config.apiBase)/pulls?state=open&base=$($script:Config.branch)&per_page=100&sort=created&direction=asc"
    } catch {
        $statusCode = $null
        try { $statusCode = [int]$_.Exception.Response.StatusCode.value__ } catch {}
        $errorText = [string]$_.Exception.Message

        if ($statusCode -in @(401, 403)) {
            $script:CanWriteRepo = $false
            Write-Warn ("Token sem autorizacao para processar PRs (HTTP {0}). O sync dos arquivos continuara." -f $statusCode)
            return
        }

        if (($statusCode -ge 500 -and $statusCode -lt 600) -or $errorText -match '\(503\)' -or $errorText -match 'No server is currently available') {
            Write-Warn 'GitHub indisponivel temporariamente para listar PRs (HTTP 5xx). O sync de arquivos continuara sem etapa de merge neste ciclo.'
            return
        }

        throw
    }
    $prsArray = @($prsResponse)
    if ($prsArray.Count -eq 1 -and $prsArray[0] -is [System.Array]) {
        $prsArray = @($prsArray[0])
    }
    $prsArray = @($prsArray | Where-Object { $_ -and $null -ne $_.number })

    if ($prsArray.Count -eq 0) {
        Write-Info 'Nenhum PR aberto encontrado.'
        return
    }

    # Evita merge duplicado quando houver PRs muito próximos (consecutivos) com mesmo título.
    # Regra: para o mesmo título, se created_at diferir até 120s, mantém só o mais novo.
    $deduped = @()
    $duplicatesSkipped = 0
    $duplicateWindowSec = 120

    $titleGroups = @($prsArray | Group-Object -Property { [string]$_.title })
    foreach ($titleGroup in $titleGroups) {
        $items = @($titleGroup.Group | Where-Object { $_ -and $null -ne $_.number })
        if ($items.Count -le 1) {
            if ($items.Count -eq 1) { $deduped += $items[0] }
            continue
        }

        $parsable = @()
        $nonParsable = @()
        foreach ($item in $items) {
            [datetimeoffset]$dt = [datetimeoffset]::MinValue
            if ([datetimeoffset]::TryParse([string]$item.created_at, [ref]$dt)) {
                $parsable += [pscustomobject]@{
                    pr = $item
                    createdAt = $dt
                }
            } else {
                $nonParsable += $item
            }
        }

        # Sem created_at parseável não há base temporal segura: mantém todos.
        if ($parsable.Count -eq 0) {
            $deduped += $items
            continue
        }

        # Clusteriza por proximidade temporal (janela de 120s) e mantém só o PR mais novo por cluster.
        $parsableSorted = @($parsable | Sort-Object -Property { $_.createdAt.ToUnixTimeSeconds() }, @{Expression = { $_.pr.number }; Ascending = $true })
        $cluster = @()
        $lastEpoch = $null
        foreach ($entry in $parsableSorted) {
            $currentEpoch = $entry.createdAt.ToUnixTimeSeconds()
            if ($null -eq $lastEpoch -or [math]::Abs($currentEpoch - $lastEpoch) -le $duplicateWindowSec) {
                $cluster += $entry
                $lastEpoch = $currentEpoch
                continue
            }

            $selected = @($cluster | Sort-Object -Property { $_.pr.number } -Descending)[0]
            $deduped += $selected.pr
            $skipped = @($cluster | Where-Object { $_.pr.number -ne $selected.pr.number })
            if ($skipped.Count -gt 0) {
                $duplicatesSkipped += $skipped.Count
                $skippedNums = ($skipped | ForEach-Object { "#$($_.pr.number)" }) -join ', '
                Write-Info ("Duplicidade detectada (mesmo título + proximidade temporal). Mantendo PR #{0} e ignorando {1}." -f $selected.pr.number, $skippedNums) -Color DarkGray
            }

            $cluster = @($entry)
            $lastEpoch = $currentEpoch
        }

        if ($cluster.Count -gt 0) {
            $selected = @($cluster | Sort-Object -Property { $_.pr.number } -Descending)[0]
            $deduped += $selected.pr
            $skipped = @($cluster | Where-Object { $_.pr.number -ne $selected.pr.number })
            if ($skipped.Count -gt 0) {
                $duplicatesSkipped += $skipped.Count
                $skippedNums = ($skipped | ForEach-Object { "#$($_.pr.number)" }) -join ', '
                Write-Info ("Duplicidade detectada (mesmo título + proximidade temporal). Mantendo PR #{0} e ignorando {1}." -f $selected.pr.number, $skippedNums) -Color DarkGray
            }
        }

        # Itens sem created_at parseável são mantidos para não haver falso positivo.
        if ($nonParsable.Count -gt 0) {
            $deduped += $nonParsable
        }
    }

    # Ordena do mais antigo ao mais novo para merge sequencial, já sem duplicatas
    $ordered = @($deduped | Sort-Object -Property number)
    if ($duplicatesSkipped -gt 0) {
        Write-Info ("Encontrado(s) {0} PR(s) aberto(s), {1} duplicado(s) ignorado(s). Mergeando {2} PR(s)..." -f $prsArray.Count, $duplicatesSkipped, $ordered.Count)
    } else {
        Write-Info ("Encontrado(s) {0} PR(s) aberto(s). Mergeando TODOS sequencialmente..." -f $ordered.Count)
    }

    $mergedCount = 0
    $failedCount = 0

    foreach ($pr in $ordered) {
        $createdAtLog = if ($pr.created_at) { $pr.created_at } else { 'sem created_at' }
        Write-Info ("Processando PR #{0} - {1} ({2})" -f $pr.number, $pr.title, $createdAtLog)

        # Verificar se já está mergeado
        try {
            $details = Invoke-GitHubApi -Uri "$($script:Config.apiBase)/pulls/$($pr.number)"
            if ($details.merged -eq $true) {
                Write-Info ("PR #{0} ja estava mergeado." -f $pr.number)
                continue
            }
        } catch {
            Write-Warn ("Falha ao obter detalhes do PR #{0}: {1}" -f $pr.number, $_.Exception.Message)
            $failedCount++
            continue
        }

        # Tentar merge via API
        try {
            $mergeResult = Invoke-GitHubApi -Uri "$($script:Config.apiBase)/pulls/$($pr.number)/merge" -Method Put -Body @{ merge_method = 'merge' }
            $script:LastMergeInfo = $mergeResult
            Write-Ok ("PR #{0} mergeado automaticamente via API." -f $pr.number)
            $mergedCount++
        } catch {
            Write-Warn "API recusou merge do PR #$($pr.number). Tentando resolucao automatica de conflitos..."

            try {
                $resolveDir = Join-Path $script:Config.tempDir "resolve_$($pr.number)"
                if (Test-Path $resolveDir) { Remove-Item -Path $resolveDir -Recurse -Force -ErrorAction SilentlyContinue }

                $repoUrl = Get-RepoUrlForClone

                Invoke-Git -Args @('clone', '--progress', '--branch', $script:Config.branch, $repoUrl, $resolveDir) -ShowProgress -ProgressMessage "Clonando temp para conflito PR #$($pr.number)" | Out-Null

                Invoke-Git -Args @('config', 'user.name', 'ChatGPT-AutoSync') -WorkingDirectory $resolveDir | Out-Null
                Invoke-Git -Args @('config', 'user.email', 'autosync@conexaovida.org') -WorkingDirectory $resolveDir | Out-Null

                Invoke-Git -Args @('fetch', '--progress', 'origin', "pull/$($pr.number)/head:pr_branch") -ShowProgress -ProgressMessage "Baixando PR #$($pr.number)" -WorkingDirectory $resolveDir | Out-Null

                Write-Info "Resolvendo conflitos (priorizando alteracoes do PR)..."
                Invoke-Git -Args @('merge', 'pr_branch', '-X', 'theirs', '-m', "Auto-resolucao de conflitos do PR #$($pr.number)") -WorkingDirectory $resolveDir | Out-Null

                Invoke-Git -Args @('push', '--progress', 'origin', $script:Config.branch) -ShowProgress -ProgressMessage "Enviando resolucao do PR #$($pr.number)" -WorkingDirectory $resolveDir | Out-Null

                Write-Ok ("PR #{0} mergeado com resolucao automatica de conflitos." -f $pr.number)
                $mergedCount++

                # Limpar diretorio temporario
                Remove-Item -Path $resolveDir -Recurse -Force -ErrorAction SilentlyContinue
            } catch {
                Write-Fail ("Nao foi possivel resolver conflito do PR #{0}: {1}" -f $pr.number, $_.Exception.Message)
                $failedCount++
            }
        }

        # Deletar branch remota do PR apos merge (faxina)
        try {
            $branchName = $pr.head.ref
            if ($branchName -and $branchName -match '^(claude|codex|chatgpt)/') {
                Invoke-GitHubApi -Uri "$($script:Config.apiBase)/git/refs/heads/$branchName" -Method Delete | Out-Null
                Write-Info "Branch '$branchName' deletada apos merge."
            }
        } catch { }
    }

    Write-Ok ("Resumo PRs: $mergedCount mergeado(s), $failedCount com falha, $($ordered.Count) total.")
}

function Fetch-RepositoryMirror {
    Write-Section 'ATUALIZANDO ESPELHO DO REPOSITORIO'

    if (-not (Test-Path $script:Config.tempDir)) {
        New-Item -ItemType Directory -Path $script:Config.tempDir -Force | Out-Null
    }
    Get-ChildItem -Path $script:Config.tempDir -Directory -Filter 'repo_*' -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

    $mirrorPath = Join-Path $script:Config.tempDir ("repo_{0}" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
    $repoUrl = Get-RepoUrlForClone
    
    Invoke-Git -Args @('clone', '--depth', '1', '--progress', '--branch', $script:Config.branch, $repoUrl, $mirrorPath) -ShowProgress -ProgressMessage "Baixando atualizacoes do repositório" | Out-Null
    $script:RepoMirror = $mirrorPath
    Write-Ok ("Espelho atualizado em $mirrorPath")
}

function Test-IsProtectedPath([string]$RelativePath) {
    $pathLower = Normalize-RelativePath $RelativePath
    foreach ($item in $script:Config.protectedItems) {
        $protected = Normalize-RelativePath $item
        if ($pathLower -eq $protected -or $pathLower.StartsWith("$protected\")) {
            return $true
        }
    }
    return $false
}

function Test-IsCachePath([string]$RelativePath) {
    $pathLower = Normalize-RelativePath $RelativePath
    if ([string]::IsNullOrWhiteSpace($pathLower)) {
        return $false
    }

    $cachePatterns = @(
        '__pycache__\',
        '.pytest_cache\',
        '.mypy_cache\',
        '.ruff_cache\',
        'node_modules\.cache\'
    )
    foreach ($pattern in $cachePatterns) {
        if ($pathLower.Contains($pattern)) {
            return $true
        }
    }

    $fileName = [System.IO.Path]::GetFileName($pathLower)
    if ($fileName -match '\.pyc$' -or $fileName -match '\.pyo$' -or $fileName -match '\.pyd$') {
        return $true
    }

    return $false
}

function Get-FileHashSafe([string]$Path) {
    return (Get-FileHash -Path $Path -Algorithm SHA256).Hash
}

function Sync-FilesFromMirror {
    Write-Section 'SINCRONIZANDO ARQUIVOS'

    $gitFiles = @(Invoke-Git -Args @('ls-files') -WorkingDirectory $script:RepoMirror)
    if ($gitFiles.Count -eq 0) {
        throw 'Nenhum arquivo rastreado encontrado no espelho do repositório.'
    }

    $added = 0
    $updated = 0
    $unchanged = 0
    $protectedCount = 0
    $cacheIgnoredCount = 0

    foreach ($relPathGitRaw in $gitFiles) {
        $relativePath = $relPathGitRaw.Trim('"').Replace('/', '\').Replace('\\', '\')
        
        if (Test-IsProtectedPath -RelativePath $relativePath) {
            $protectedCount++
            $script:ProtectedFiles.Add($relativePath)
            continue
        }
        if (Test-IsCachePath -RelativePath $relativePath) {
            $cacheIgnoredCount++
            continue
        }

        $source = Join-Path $script:RepoMirror $relativePath
        $target = Join-Path $script:Config.localDir $relativePath
        if (-not (Test-Path $source -PathType Leaf)) {
            continue
        }

        $targetDir = Split-Path -Parent $target
        if (-not (Test-Path $targetDir)) {
            New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
        }

        $needsCopy = $false
        $isNew = -not (Test-Path $target)
        if ($isNew) {
            $needsCopy = $true
        } else {
            $srcInfo = Get-Item $source
            $dstInfo = Get-Item $target
            if ($srcInfo.Length -ne $dstInfo.Length) {
                $needsCopy = $true
            } else {
                $needsCopy = (Get-FileHashSafe $source) -ne (Get-FileHashSafe $target)
            }
        }

        if ($needsCopy) {
            Copy-Item -Path $source -Destination $target -Force
            try {
                $gitDate = Invoke-Git -Args @('log', '-1', '--format=%cI', '--', $relPathGitRaw) -WorkingDirectory $script:RepoMirror
                if ($gitDate) {
                    (Get-Item $target).LastWriteTime = [datetime]::Parse(($gitDate | Select-Object -First 1))
                }
            } catch {}

            if ($isNew) {
                $added++
                $script:AddedFiles.Add($relativePath)
                Write-Host "[NOVO] $relativePath" -ForegroundColor Green
            } else {
                $updated++
                $script:UpdatedFiles.Add($relativePath)
                Write-Host "[ATUALIZADO] $relativePath" -ForegroundColor Cyan
            }
        } else {
            $unchanged++
        }
    }

    if ($added -gt 0 -or $updated -gt 0) {
        $pywaRelative = Normalize-RelativePath $script:Config.pywaPattern
        $autoDevRelative = Normalize-RelativePath $script:Config.autoDevAgentPattern
        # Junta todos os arquivos novos e atualizados numa única lista (normalizada e sem duplicatas)
        $changedFiles = @($script:AddedFiles) + @($script:UpdatedFiles) | ForEach-Object { Normalize-RelativePath $_ }
        $changedUnique = @(
            $changedFiles |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) -and -not (Test-IsRestartIgnoredPath $_) } |
            Select-Object -Unique
        )

        $ignoredOnlyChanged = ($changedFiles.Count -gt 0) -and (($changedFiles | Where-Object { -not (Test-IsRestartIgnoredPath $_) }).Count -eq 0)
        if ($ignoredOnlyChanged) {
            Write-Info 'Apenas arquivos ignorados para reinicio (README.md e/ou scripts de sync) foram alterados neste ciclo. Nenhum reinicio sera executado.' -Color Yellow
        }

        $analyzerRelative = Normalize-RelativePath $script:Config.analyzerPattern
        $onlyPhp = ($changedUnique.Count -gt 0) -and (($changedUnique | Where-Object { -not $_.EndsWith('.php') }).Count -eq 0)
        $pyChanged = @($changedUnique | Where-Object { $_.EndsWith('.py') })
        $otherPyChanged = @($pyChanged | Where-Object { $_ -ne $analyzerRelative -and $_ -ne $pywaRelative -and $_ -ne $autoDevRelative })

        $script:RestartTargets.Clear()

        if ($changedUnique.Count -eq 0) {
            $script:RestartRequested = $false
            $script:RestartScope = 'none'
        } elseif ($onlyPhp) {
            $script:RestartRequested = $false
            $script:RestartScope = 'none'
            Write-Info "Atualizacao exclusiva de arquivos .php detectada. O reinicio dos processos locais (BATs) sera ignorado." -Color Yellow
        } elseif ($pyChanged.Count -eq 0) {
            $script:RestartRequested = $false
            $script:RestartScope = 'none'
            Write-Info "Somente README.md/PHP/outros nao-.py alterados neste ciclo. Reinicio dos servidores sera ignorado." -Color Yellow
        } else {
            $script:RestartRequested = $true
            $script:RestartScope = 'selective'

            if ($pyChanged -contains $analyzerRelative) {
                Add-RestartTarget 'analyzer'
                Write-Info "Alteracao em $analyzerRelative detectada. Reinicio seletivo do analisador sera executado." -Color Yellow
            }
            if ($pyChanged -contains $pywaRelative) {
                Add-RestartTarget 'pywa'
                Write-Info "Alteracao em $pywaRelative detectada. Reinicio seletivo do servidor WhatsApp sera executado." -Color Yellow
            }
            if ($pyChanged -contains $autoDevRelative) {
                Add-RestartTarget 'autodev'
                Write-Info "Alteracao em $autoDevRelative detectada. Reinicio seletivo do Agente Autonomo sera executado." -Color Yellow
            }
            if ($otherPyChanged.Count -gt 0) {
                Add-RestartTarget 'main'
                Write-Info "Alteracao em outros arquivos .py detectada. Reinicio seletivo do 0. start.bat sera executado." -Color Yellow
            }

            if ($script:RestartTargets.Count -eq 0) {
                $script:RestartRequested = $false
                $script:RestartScope = 'none'
            }
        }
    }

    Write-Ok ("Resumo: $added novo(s), $updated atualizado(s), $unchanged inalterado(s), $protectedCount protegido(s), $cacheIgnoredCount cache ignorado(s)")
}

function Sync-RemotePhpIfNeeded {
    Write-Section 'SINCRONIZANDO PHP REMOTO'

    $remotePhpMappings = @()
    if (-not [string]::IsNullOrWhiteSpace($script:Config.remotePhpLocalFile) -and -not [string]::IsNullOrWhiteSpace($script:Config.remotePhpTargetPath)) {
        $remotePhpMappings += [ordered]@{
            localFile = $script:Config.remotePhpLocalFile
            targetPath = $script:Config.remotePhpTargetPath
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($script:Config.remotePhpLocalFile2) -and -not [string]::IsNullOrWhiteSpace($script:Config.remotePhpTargetPath2)) {
        $remotePhpMappings += [ordered]@{
            localFile = $script:Config.remotePhpLocalFile2
            targetPath = $script:Config.remotePhpTargetPath2
        }
    }

    if ($remotePhpMappings.Count -eq 0) {
        Write-Info 'Arquivos PHP remotos nao configurados; etapa ignorada.'
        return
    }

    $changedFiles = @($script:AddedFiles) + @($script:UpdatedFiles) | ForEach-Object { Normalize-RelativePath $_ }
    if ([string]::IsNullOrWhiteSpace($script:Config.remotePhpSaveUrl) -or [string]::IsNullOrWhiteSpace($script:Config.remotePhpApiKey)) {
        throw 'Alteracao de PHP detectada, mas remotePhpSaveUrl/remotePhpApiKey nao foram configurados.'
    }

    $sentAnyFile = $false
    foreach ($mapping in $remotePhpMappings) {
        $localPhpRelative = Normalize-RelativePath $mapping.localFile
        $targetPhpPath = [string]$mapping.targetPath
        $phpChanged = $changedFiles -contains $localPhpRelative
        if (-not $phpChanged) {
            Write-Info "Arquivo PHP monitorado nao foi alterado neste ciclo: $($mapping.localFile)"
            continue
        }

        $localPhpPath = Join-Path $script:Config.localDir $mapping.localFile
        if (-not (Test-Path $localPhpPath -PathType Leaf)) {
            throw "Arquivo PHP alterado nao encontrado localmente para sync remoto: $localPhpPath"
        }

        # 1. Leitura PURA usando o núcleo do .NET (Ignora todos os metadados do PowerShell)
        $conteudo = [System.IO.File]::ReadAllText($localPhpPath, [System.Text.Encoding]::UTF8)

        # 2. Construção do pacote APENAS com o que o PHP precisa
        $payloadString = @{
            api_key  = $script:Config.remotePhpApiKey
            filepath = $targetPhpPath
            conteudo = $conteudo
        } | ConvertTo-Json -Depth 6 -Compress

        $targetUrl = $script:Config.remotePhpSaveUrl
        try {
            $uriObj = [System.Uri]$targetUrl
            if ([string]::IsNullOrWhiteSpace($uriObj.Query)) {
                $targetUrl = "$($uriObj.AbsoluteUri)?filepath=$([System.Uri]::EscapeDataString($targetPhpPath))"
            } else {
                $targetUrl = "$($uriObj.AbsoluteUri)&filepath=$([System.Uri]::EscapeDataString($targetPhpPath))"
            }
        } catch { }

        Write-Info "Atualizando arquivo PHP no servidor remoto: $targetPhpPath"
        Write-Info "URL completa do endpoint remoto: $targetUrl"
        Write-Info ("Arquivo local origem: {0} ({1} bytes)" -f $localPhpPath, ([System.Text.Encoding]::UTF8.GetByteCount($conteudo)))
        Write-Info "Iniciando requisicao de atualizacao remota (timeout: 60s)..."

        $curlHeaders = @(
            "-H ""Content-Type: application/json; charset=utf-8""",
            "-H ""Accept: application/json"""
        ) -join ' '
        $payloadTempFile = Join-Path $script:Config.tempDir ("remote_php_payload_{0}.json" -f (Get-Date -Format 'yyyyMMdd_HHmmss_fff'))
        try { Set-Content -Path $payloadTempFile -Value $payloadString -Encoding UTF8 -NoNewline } catch { }
        $curlCommand = ('curl -X POST "{0}" {1} --data-binary "@{2}"' -f $targetUrl, $curlHeaders, $payloadTempFile)

        # --- A MÁGICA ACONTECE AQUI ---
        # Convertemos a string JSON diretamente em bytes UTF-8 estritos
        # Isto impede o PowerShell de corromper os acentos durante o envio!
        $payloadBytes = [System.Text.Encoding]::UTF8.GetBytes($payloadString)

        try {
            $responseRaw = Invoke-WebRequest `
                -Method Post `
                -Uri $script:Config.remotePhpSaveUrl `
                -ContentType 'application/json; charset=utf-8' `
                -Body $payloadBytes `
                -UseBasicParsing `
                -TimeoutSec 60

            $statusCode = [int]$responseRaw.StatusCode
            $responseBody = ($responseRaw.Content | Out-String).Trim()
            Write-Info "Resposta HTTP remota: $statusCode"
            Write-Info "Resposta completa do servidor remoto: $responseBody"

            if ($statusCode -ge 200 -and $statusCode -lt 300 -and ($responseBody -match '"status"\s*:\s*"ok"' -or $responseBody -match '"success"\s*:\s*true')) {
                Write-Ok "PHP remoto atualizado com sucesso: $targetPhpPath"
                $sentAnyFile = $true
                continue
            }

            throw "Servidor remoto nao confirmou sucesso ao salvar PHP. HTTP=$statusCode; body=$responseBody"
        } catch {
            $statusCode = $null
            $errorBody = $null
            $errorRaw = $null
            if ($_.Exception.Response) {
                try { $statusCode = [int]$_.Exception.Response.StatusCode } catch { }
                try {
                    $stream = $_.Exception.Response.GetResponseStream()
                    if ($stream) {
                        $reader = New-Object System.IO.StreamReader($stream)
                        $errorBody = $reader.ReadToEnd()
                        $reader.Dispose()
                        $stream.Dispose()
                    }
                } catch { }
            }
            try {
                if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
                    $errorRaw = [string]$_.ErrorDetails.Message
                }
            } catch { }

            if (-not [string]::IsNullOrWhiteSpace($statusCode)) {
                Write-Fail "Falha HTTP ao atualizar PHP remoto. Status: $statusCode"
            } else {
                Write-Fail "Falha ao atualizar PHP remoto (sem status HTTP)."
            }
            Write-Fail "CURL completo para reproduzir a chamada remota:"
            Write-Fail $curlCommand
            if (Test-Path $payloadTempFile) {
                Write-Fail "POST completo salvo em arquivo temporario: $payloadTempFile"
            }
            if (-not [string]::IsNullOrWhiteSpace($errorBody)) {
                Write-Fail "Resposta completa de erro do servidor remoto: $errorBody"
            } elseif (-not [string]::IsNullOrWhiteSpace($errorRaw)) {
                Write-Fail "Resposta RAW de erro do servidor remoto: $errorRaw"
            }

            throw $_
        }
    }

    if (-not $sentAnyFile) {
        Write-Info 'Nenhum arquivo PHP monitorado foi alterado neste ciclo.'
    }
}



function Log-RunningProcessesStatus {
    Write-Section 'MONITORAMENTO DE PROCESSOS'
    $found = 0

    $whatsappTitle = if ([string]::IsNullOrWhiteSpace($script:Config.whatsappWindowTitle)) { 'WhatsApp Follow-up Server (Web)' } else { $script:Config.whatsappWindowTitle }
    $whatsappTitlePattern = [regex]::Escape($whatsappTitle)
    $whatsappBatPattern = 'start[\s_]*whatsapp[\s_]*server\.bat'
    $pywaScriptPattern = 'pywa_acompanhamento_server\.py'

    $cmds = Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'cmd' }
    foreach ($c in $cmds) {
        $isMainWindow = $c.CommandLine -match '0\. start\.bat'
        $isAnalyzerWindow = $c.CommandLine -match '1\. start_apenas_analisador_prontuarios\.bat'
        $isPywaWindow = ($c.CommandLine -match $whatsappBatPattern -or $c.CommandLine -match $pywaScriptPattern -or $c.CommandLine -match $whatsappTitlePattern)

        if ($isMainWindow -or $isAnalyzerWindow) {
            Write-Info "Alvo encontrado [Janela Inicial] (PID $($c.ProcessId)): $($c.CommandLine)"
            $found++
        } elseif ($isPywaWindow) {
            Write-Info "Alvo encontrado [Janela WhatsApp] (PID $($c.ProcessId)): $($c.CommandLine)"
            $found++
        }
    }

    $mainName = [System.IO.Path]::GetFileName($script:Config.chatProcessPattern)
    $analyzerName = [System.IO.Path]::GetFileName($script:Config.analyzerPattern)
    $pywaName = [System.IO.Path]::GetFileName($script:Config.pywaPattern)

    $pys = Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -or $_.Name -match 'py' }
    foreach ($p in $pys) {
        if ($p.CommandLine -match [regex]::Escape($mainName) -or $p.CommandLine -match [regex]::Escape($analyzerName) -or $p.CommandLine -match [regex]::Escape($pywaName)) {
            Write-Info "Alvo encontrado [Servidor Python] (PID $($p.ProcessId)): $($p.CommandLine)"
            $found++
        }
    }

    $browsers = Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'chrome' -or $_.Name -match 'ms-playwright' -or $_.Name -match 'chromium' }
    foreach ($b in $browsers) {
        if ($b.ExecutablePath -match 'ms-playwright' -or $b.CommandLine -match 'playwright') {
            Write-Info "Alvo encontrado [Navegador Oculto do Playwright] (PID $($b.ProcessId))"
            $found++
        }
    }

    if ($found -eq 0) {
        Write-Info "O script esta a vigiar, mas nenhum processo do simulador foi encontrado rodando."
    } else {
        Write-Ok "Total de processos sendo vigiados (e que serao mortos se houver update): $found"
    }
}

function Stop-ManagedProcesses {
    param(
        [ValidateSet('none', 'selective')]
        [string]$Scope = 'selective'
    )

    Write-Section 'PARANDO PROCESSOS E JANELAS'
    $killed = 0
    $killMain = $script:RestartTargets -contains 'main'
    $killAnalyzer = $script:RestartTargets -contains 'analyzer'
    $killPywa = $script:RestartTargets -contains 'pywa'
    $killAutoDev = $script:RestartTargets -contains 'autodev'

    $whatsappTitle = if ([string]::IsNullOrWhiteSpace($script:Config.whatsappWindowTitle)) { 'WhatsApp Follow-up Server (Web)' } else { $script:Config.whatsappWindowTitle }
    $whatsappTitlePattern = [regex]::Escape($whatsappTitle)
    $whatsappBatPattern = 'start[\s_]*whatsapp[\s_]*server\.bat'
    $pywaScriptPattern = 'pywa_acompanhamento_server\.py'
    $autoDevBatPattern = '3\. start_agente_autonomo\.bat'
    $autoDevTitle = if ([string]::IsNullOrWhiteSpace($script:Config.autoDevAgentWindowTitle)) { 'Agente Autonomo de Melhoria Continua' } else { $script:Config.autoDevAgentWindowTitle }
    $autoDevTitlePattern = [regex]::Escape($autoDevTitle)

    $cmds = Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'cmd' }
    foreach ($c in $cmds) {
        $isMainWindow = $c.CommandLine -match '0\. start\.bat'
        $isAnalyzerWindow = $c.CommandLine -match '1\. start_apenas_analisador_prontuarios\.bat'
        $isPywaWindow = ($c.CommandLine -match $whatsappBatPattern -or $c.CommandLine -match $pywaScriptPattern -or $c.CommandLine -match $whatsappTitlePattern)
        $isAutoDevWindow = ($c.CommandLine -match $autoDevBatPattern -or $c.CommandLine -match $autoDevTitlePattern)

        $mustKill = (($killMain -and $isMainWindow) -or
                     ($killAnalyzer -and $isAnalyzerWindow) -or
                     ($killPywa -and $isPywaWindow) -or
                     ($killAutoDev -and $isAutoDevWindow))

        if ($mustKill) {
            try {
                & taskkill.exe /F /T /PID $c.ProcessId 2>&1 | Out-Null
                $killed++
                Write-Info "Janela de inicializacao encerrada (PID $($c.ProcessId))"
            } catch { }
        }
    }

    $mainName = [System.IO.Path]::GetFileName($script:Config.chatProcessPattern)
    $analyzerName = [System.IO.Path]::GetFileName($script:Config.analyzerPattern)
    $pywaName = [System.IO.Path]::GetFileName($script:Config.pywaPattern)
    $autoDevName = [System.IO.Path]::GetFileName($script:Config.autoDevAgentPattern)

    $pys = Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -or $_.Name -match 'py' }
    foreach ($p in $pys) {
        $isMainPy = $p.CommandLine -match [regex]::Escape($mainName)
        $isAnalyzerPy = $p.CommandLine -match [regex]::Escape($analyzerName)
        $isPywaPy = $p.CommandLine -match [regex]::Escape($pywaName)
        $isAutoDevPy = $p.CommandLine -match [regex]::Escape($autoDevName)

        $mustKill = (($killMain -and $isMainPy) -or
                     ($killAnalyzer -and $isAnalyzerPy) -or
                     ($killPywa -and $isPywaPy) -or
                     ($killAutoDev -and $isAutoDevPy))

        if ($mustKill) {
            try {
                & taskkill.exe /F /T /PID $p.ProcessId 2>&1 | Out-Null
                $killed++
                Write-Info "Processo Python encerrado (PID $($p.ProcessId))"
            } catch { }
        }
    }

    if ($killMain) {
        $browsers = Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'chrome' -or $_.Name -match 'ms-playwright' -or $_.Name -match 'chromium' }
        foreach ($b in $browsers) {
            if ($b.ExecutablePath -match 'ms-playwright' -or $b.CommandLine -match 'playwright') {
                try {
                    & taskkill.exe /F /T /PID $b.ProcessId 2>&1 | Out-Null
                    $killed++
                    Write-Info "Navegador Playwright oculto encerrado (PID $($b.ProcessId))"
                } catch { }
            }
        }
    }

    Write-Ok ("Total de processos e janelas encontrados e encerrados: $killed")
}

function Start-ManagedProcesses {
    param(
        [ValidateSet('none', 'selective')]
        [string]$Scope = 'selective'
    )

    Write-Section 'REINICIANDO PROCESSOS'

    $batMain = Join-Path $script:Config.localDir '0. start.bat'
    $batAnalyzer = Join-Path $script:Config.localDir '1. start_apenas_analisador_prontuarios.bat'
    $batWhatsapp = Join-Path $script:Config.localDir $script:Config.whatsappServerBat
    $autoDevBatName = if ([string]::IsNullOrWhiteSpace($script:Config.autoDevAgentBat)) { '3. start_agente_autonomo.bat' } else { $script:Config.autoDevAgentBat }
    $batAutoDev = Join-Path $script:Config.localDir $autoDevBatName

    if ($script:RestartTargets -contains 'main') {
        if (-not (Test-Path $batMain)) { throw "Arquivo .bat nao encontrado: $batMain" }
        Write-Info "Iniciando $batMain..."
        Start-Process -FilePath $batMain -WorkingDirectory $script:Config.localDir | Out-Null
        Start-Sleep -Seconds 3
    }

    if ($script:RestartTargets -contains 'analyzer') {
        if (-not (Test-Path $batAnalyzer)) { throw "Arquivo .bat nao encontrado: $batAnalyzer" }
        Write-Info "Iniciando $batAnalyzer..."
        Start-Process -FilePath $batAnalyzer -WorkingDirectory $script:Config.localDir | Out-Null
        Start-Sleep -Seconds 2
    }

    if ($script:RestartTargets -contains 'pywa') {
        if (-not (Test-Path $batWhatsapp)) { throw "Arquivo .bat nao encontrado: $batWhatsapp" }
        Write-Info "Iniciando $batWhatsapp..."
        Start-Process -FilePath $batWhatsapp -WorkingDirectory $script:Config.localDir | Out-Null
        Start-Sleep -Seconds 2
    }

    if ($script:RestartTargets -contains 'autodev') {
        if (-not (Test-Path $batAutoDev)) { throw "Arquivo .bat nao encontrado: $batAutoDev" }
        Write-Info "Iniciando $batAutoDev..."
        Start-Process -FilePath $batAutoDev -WorkingDirectory $script:Config.localDir | Out-Null
    }

    Write-Ok ("Reinicio seletivo concluido para: {0}" -f (($script:RestartTargets | Sort-Object) -join ', '))
}

function Register-AutoSyncTask {
    Write-Section 'AGENDAMENTO'

    $taskName = $script:Config.taskName
    $ps1Path = $script:Config.syncPs1Path
    if (-not (Test-Path $ps1Path)) {
        throw "Arquivo nao encontrado para agendamento: $ps1Path"
    }

    $escapedPs1 = $ps1Path.Replace('"', '""')
    $command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""$escapedPs1"" --scheduled"
    $startTime = (Get-Date).AddMinutes(1).ToString('HH:mm')

    & schtasks.exe /Create /TN $taskName /SC MINUTE /MO $script:Config.syncIntervalMinutes /TR $command /RL HIGHEST /F /ST $startTime | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Falha ao criar a tarefa agendada '$taskName'."
    }

    Write-Ok ("Tarefa '$taskName' criada para rodar a cada $($script:Config.syncIntervalMinutes) minuto(s).")
}

function Unregister-AutoSyncTask {
    Write-Section 'AGENDAMENTO'

    & schtasks.exe /Delete /TN $script:Config.taskName /F | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Falha ao remover a tarefa agendada '$($script:Config.taskName)'."
    }

    Write-Ok ("Tarefa '$($script:Config.taskName)' removida.")
}

function Show-Summary {
    Write-Host ''
    Write-Host '========================================' -ForegroundColor White
    Write-Host 'SYNC FINALIZADO' -ForegroundColor White
    Write-Host '========================================' -ForegroundColor White
    Write-Host ("Arquivos novos:       {0}" -f $script:AddedFiles.Count)
    Write-Host ("Arquivos atualizados: {0}" -f $script:UpdatedFiles.Count)
    Write-Host ("Arquivos protegidos:  {0}" -f $script:ProtectedFiles.Count)
    Write-Host ("Reinicio executado:   {0}" -f ($(if ($script:RestartRequested) { 'sim' } else { 'nao' })))
    Write-Host ("Escopo do reinicio:   {0}" -f $script:RestartScope)
    Write-Host ("Alvos de reinicio:    {0}" -f $(if ($script:RestartTargets.Count -gt 0) { (($script:RestartTargets | Sort-Object) -join ', ') } else { '-' }))
    Write-Host ("Log:                  {0}" -f $script:LogFile)
    Write-Host '========================================' -ForegroundColor White
}

function Reset-CycleState {
    $script:RestartRequested = $false
    $script:RestartScope = 'none'
    $script:RestartTargets = New-Object System.Collections.Generic.List[string]
    $script:UpdatedFiles = New-Object System.Collections.Generic.List[string]
    $script:AddedFiles = New-Object System.Collections.Generic.List[string]
    $script:ProtectedFiles = New-Object System.Collections.Generic.List[string]
    $script:RepoMirror = $null
    # Nota: $script:LogFile NÃO é resetado — o log da sessão é reutilizado
    # entre ciclos (assim como os outros sistemas do projeto).
    $script:CanWriteRepo = $true
    $script:GitHubAuthFailed = $false
}

function Run-SyncCycle {
    Reset-CycleState
    Import-Settings
    Assert-Configuration
    Initialize-Logging

    # Separador visual entre ciclos no mesmo log (a partir do 2.º ciclo)
    if ($script:CycleCount -gt 0) {
        Write-Log ""
        Write-Log ("=" * 60)
        Write-Log "[CICLO] Novo ciclo iniciado em $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
        Write-Log ("=" * 60)
    }
    $script:CycleCount++

    Acquire-Lock
    try {
        $script:GitExe = Get-GitExe

        Write-Info "Repositorio local: $($script:Config.localDir)"
        Write-Info "Branch monitorada: $($script:Config.branch)"

        Log-RunningProcessesStatus

        Merge-AllPullRequests
        Fetch-RepositoryMirror
        Sync-FilesFromMirror
        Sync-RemotePhpIfNeeded

        if ($script:RestartRequested) {
            Stop-ManagedProcesses -Scope 'selective'
            Start-ManagedProcesses -Scope 'selective'
        } else {
            Write-Info 'Nenhum reinicio necessario neste ciclo.'
        }

        Show-Summary
    } finally {
        Release-Lock
    }
}

try {
    # --- VOLTANDO AO PRETO PADRÃO SEGURO E INFALÍVEL ---
    try {
        [Console]::Title = "🔄 GitHub Auto-Sync [ChatGPT Simulator]"
        [Console]::BackgroundColor = 'Black'
        [Console]::ForegroundColor = 'Gray'
        Clear-Host
    } catch { }

    if ($script:InstallTask) {
        Import-Settings
        Assert-Configuration
        Register-AutoSyncTask
        exit 0
    }

    if ($script:UninstallTask) {
        Import-Settings
        Assert-Configuration
        Unregister-AutoSyncTask
        exit 0
    }

    do {
        try {
            Run-SyncCycle
            if (-not $script:IsScheduled) {
                exit 0
            }
        } catch {
            if ($script:LogFile) {
                Write-Log $_.Exception.ToString()
            }
            Write-Fail $_.Exception.Message
            if (-not $script:IsScheduled) {
                exit 1
            }
        }

        $intervalMinutes = [math]::Max(1, [int]$script:Config.syncIntervalMinutes)
        $totalSeconds = $intervalMinutes * 60
        Write-Log "[INFO] Aguardando $intervalMinutes minuto(s) para a proxima conferencia."
        
        for ($i = $totalSeconds; $i -gt 0; $i--) {
            $m = [int][math]::Floor($i / 60)
            $s = [int]($i % 60)
            $timeFmt = "{0:D2}:{1:D2}" -f $m, $s
            
            Write-Host -NoNewline "`r[AGUARDANDO] Proxima verificacao no GitHub em: $timeFmt          " -ForegroundColor Yellow
            Start-Sleep -Seconds 1
        }
        
        Write-Host "`r[INICIANDO] Buscando atualizacoes agora...                                  " -ForegroundColor Green

    } while ($script:IsScheduled)
} catch {
    if ($script:LogFile) {
        Write-Log $_.Exception.ToString()
    }
    Write-Fail $_.Exception.Message
    
    Write-Host "`nO script encontrou um erro e vai fechar em 30 segundos..." -ForegroundColor Red
    Start-Sleep -Seconds 30
    
    exit 1
}
