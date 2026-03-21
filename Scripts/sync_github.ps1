# sync_github.ps1 — arquivo versionado do sync automático do Windows
# Responsável por: merge opcional do PR mais recente, sync de arquivos e reinício coordenado dos processos locais.
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
$script:UpdatedFiles = New-Object System.Collections.Generic.List[string]
$script:AddedFiles = New-Object System.Collections.Generic.List[string]
$script:ProtectedFiles = New-Object System.Collections.Generic.List[string]
$script:LogFile = $null
$script:RepoMirror = $null
$script:GitExe = $null
$script:Config = [ordered]@{}
$script:LastMergeInfo = $null
$script:CanWriteRepo = $true

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

function Initialize-Logging {
    $logDir = Join-Path $script:Config.localDir 'logs'
    if (-not (Test-Path $logDir)) {
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    }
    $script:LogFile = Join-Path $logDir ("sync_github-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    Write-Info "Log em $($script:LogFile)"
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
    $settingsCandidates = @(
        (Join-Path $scriptDir 'sync_github_settings.ps1'),
        (Join-Path $scriptDir 'sync_github.settings.ps1')
    )

    $settingsPath = $settingsCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $settingsPath) { $settingsPath = $settingsCandidates[0] }

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
    }

    if (Test-Path $settingsPath) {
        . $settingsPath
        Write-Host "Usando configuracao do sync: $settingsPath" -ForegroundColor DarkGray
    } else {
        Write-Host "Configuracao do sync nao encontrada em disco; usando defaults internos do script." -ForegroundColor DarkGray
    }

    foreach ($key in $defaults.Keys) {
        if (Get-Variable -Name $key -Scope Local -ErrorAction SilentlyContinue) {
            $script:Config[$key] = (Get-Variable -Name $key -ValueOnly)
        } else {
            $script:Config[$key] = $defaults[$key]
        }
    }

    if (Test-IsPlaceholderValue $script:Config.githubToken) {
        $script:Config.githubToken = $null
    }
    
    if (Test-IsPlaceholderValue $script:Config.ghUser) {
        throw "Configuracao invalida em ${settingsPath}: substitua 'seu_usuario_ou_org' pelo usuario real do GitHub."
    }

    $script:Config.scriptDir = $scriptDir
    $script:Config.syncBatPath = Join-Path $script:Config.localDir 'sync_github.bat'
    $script:Config.syncPs1Path = Join-Path $script:Config.localDir 'Scripts\sync_github.ps1'
    $script:Config.settingsPath = $settingsPath
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
        'Scripts\sync_github_settings.ps1',
        'Scripts\sync_github.settings.ps1',
        'chrome_profile'
    )
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
            & taskkill.exe /F /T /PID $c.ProcessId 2>&1 | Out-Null
            Write-Info "Substituindo janela CMD de sync anterior (PID $($c.ProcessId))"
            $killedOthers = $true
        } catch { }
    }

    $syncPs = Get-CimInstance Win32_Process | Where-Object { ($_.Name -match 'powershell' -or $_.Name -match 'pwsh') -and $_.CommandLine -match 'sync_github\.ps1' }
    foreach ($p in $syncPs) {
        if ($p.ProcessId -eq $myPid) { continue }
        try {
            & taskkill.exe /F /T /PID $p.ProcessId 2>&1 | Out-Null
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
    if ($script:Config.githubToken) {
        return "https://$($script:Config.ghUser):$($script:Config.githubToken)@github.com/$($script:Config.ghUser)/$($script:Config.repo).git"
    }
    return "https://github.com/$($script:Config.ghUser)/$($script:Config.repo).git"
}

function Invoke-GitHubApi {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [ValidateSet('Get','Post','Put','Patch')][string]$Method = 'Get',
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
        $params.Body = ($Body | ConvertTo-Json -Depth 10)
        $params.ContentType = 'application/json'
    }
    return Invoke-RestMethod @params
}

function Merge-NewestPullRequest {
    Write-Section 'PULL REQUESTS'

    if (-not $script:Config.githubToken) {
        $script:CanWriteRepo = $false
        Write-Warn 'Token GitHub nao configurado; etapa de PR sera ignorada, mas o sync dos arquivos ainda sera tentado.'
        return
    }

    try {
        $prsResponse = Invoke-GitHubApi -Uri "$($script:Config.apiBase)/pulls?state=open&base=$($script:Config.branch)&per_page=100&sort=created&direction=desc"
    } catch {
        $statusCode = $null
        try { $statusCode = [int]$_.Exception.Response.StatusCode.value__ } catch {}

        if ($statusCode -in @(401, 403)) {
            $script:CanWriteRepo = $false
            Write-Warn ("Token sem autorizacao para processar PRs (HTTP {0}). O sync dos arquivos continuara." -f $statusCode)
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

    $ordered = @($prsArray | Sort-Object -Property number -Descending)
    $newest = $ordered[0]
    $older = @()
    if ($ordered.Count -gt 1) {
        $older = @($ordered[1..($ordered.Count - 1)])
    }

    $createdAtLog = if ($newest.created_at) { $newest.created_at } else { 'sem created_at' }
    Write-Info ("PR mais recente: #{0} - {1} ({2})" -f $newest.number, $newest.title, $createdAtLog)
    
    if ($older.Count -gt 0) {
        Write-Info ("Fechando {0} PR(s) mais antigo(s)." -f $older.Count)
        foreach ($pr in $older) {
            try {
                Invoke-GitHubApi -Uri "$($script:Config.apiBase)/issues/$($pr.number)/comments" -Method Post -Body @{ body = "Fechado automaticamente porque o PR mais recente (#$($newest.number)) sera processado pelo sync automatico." } | Out-Null
                Invoke-GitHubApi -Uri "$($script:Config.apiBase)/pulls/$($pr.number)" -Method Patch -Body @{ state = 'closed' } | Out-Null
                Write-Info ("PR #{0} fechado." -f $pr.number)
            } catch {
                Write-Warn ("Falha ao fechar PR #{0}: {1}" -f $pr.number, $_.Exception.Message)
            }
        }
    }

    $details = Invoke-GitHubApi -Uri "$($script:Config.apiBase)/pulls/$($newest.number)"
    if ($details.merged -eq $true) {
        Write-Info ("PR #{0} ja estava mergeado." -f $newest.number)
        return
    }

    try {
        $mergeResult = Invoke-GitHubApi -Uri "$($script:Config.apiBase)/pulls/$($newest.number)/merge" -Method Put -Body @{ merge_method = 'merge' }
        $script:LastMergeInfo = $mergeResult
        Write-Ok ("PR #{0} mergeado automaticamente via API (método: merge)." -f $newest.number)
    } catch {
        Write-Warn "API do GitHub recusou o merge (Erro de Conflito). Iniciando resolução automática..."
        
        try {
            $resolveDir = Join-Path $script:Config.tempDir "resolve_$($newest.number)"
            if (Test-Path $resolveDir) { Remove-Item -Path $resolveDir -Recurse -Force -ErrorAction SilentlyContinue }
            
            $repoUrl = Get-RepoUrlForClone
            
            Invoke-Git -Args @('clone', '--progress', '--branch', $script:Config.branch, $repoUrl, $resolveDir) -ShowProgress -ProgressMessage "Clonando temp para conflito" | Out-Null
            
            Invoke-Git -Args @('config', 'user.name', 'ChatGPT-AutoSync') -WorkingDirectory $resolveDir | Out-Null
            Invoke-Git -Args @('config', 'user.email', 'autosync@conexaovida.org') -WorkingDirectory $resolveDir | Out-Null
            
            Invoke-Git -Args @('fetch', '--progress', 'origin', "pull/$($newest.number)/head:pr_branch") -ShowProgress -ProgressMessage "Baixando o codigo do PR #$($newest.number)" -WorkingDirectory $resolveDir | Out-Null
            
            Write-Info "Forcando a resolucao do conflito (priorizando sempre as alteracoes novas)..."
            Invoke-Git -Args @('merge', 'pr_branch', '-X', 'theirs', '-m', "Auto-resolucao de conflitos do PR #$($newest.number)") -WorkingDirectory $resolveDir | Out-Null
            
            Invoke-Git -Args @('push', '--progress', 'origin', $script:Config.branch) -ShowProgress -ProgressMessage "Enviando alteracoes corrigidas" -WorkingDirectory $resolveDir | Out-Null
            
            Write-Ok ("Conflitos resolvidos sozinho! PR #{0} mergeado e encerrado com sucesso." -f $newest.number)
        } catch {
            Write-Fail "Nao foi possivel resolver o conflito automaticamente: $($_.Exception.Message)"
            Write-Warn "O script continuara operando apenas com o que ja foi aprovado na branch principal."
        }
    }
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

    foreach ($relPathGitRaw in $gitFiles) {
        $relativePath = $relPathGitRaw.Trim('"').Replace('/', '\').Replace('\\', '\')
        
        if (Test-IsProtectedPath -RelativePath $relativePath) {
            $protectedCount++
            $script:ProtectedFiles.Add($relativePath)
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
        $script:RestartRequested = $true
    }

    Write-Ok ("Resumo: $added novo(s), $updated atualizado(s), $unchanged inalterado(s), $protectedCount protegido(s)")
}

function Log-RunningProcessesStatus {
    Write-Section 'MONITORAMENTO DE PROCESSOS'
    $found = 0

    $cmds = Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'cmd' }
    foreach ($c in $cmds) {
        if ($c.CommandLine -match '0\. start\.bat' -or $c.CommandLine -match '1\. start_apenas_analisador_prontuarios\.bat') {
            Write-Info "Alvo encontrado [Janela Inicial] (PID $($c.ProcessId)): $($c.CommandLine)"
            $found++
        }
    }

    $mainName = [System.IO.Path]::GetFileName($script:Config.chatProcessPattern)
    $analyzerName = [System.IO.Path]::GetFileName($script:Config.analyzerPattern)

    $pys = Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -or $_.Name -match 'py' }
    foreach ($p in $pys) {
        if ($p.CommandLine -match [regex]::Escape($mainName) -or $p.CommandLine -match [regex]::Escape($analyzerName)) {
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
    Write-Section 'PARANDO PROCESSOS E JANELAS'
    $killed = 0

    $cmds = Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'cmd' }
    foreach ($c in $cmds) {
        if ($c.CommandLine -match '0\. start\.bat' -or $c.CommandLine -match '1\. start_apenas_analisador_prontuarios\.bat') {
            try {
                & taskkill.exe /F /T /PID $c.ProcessId 2>&1 | Out-Null
                $killed++
                Write-Info "Janela de inicializacao encerrada (PID $($c.ProcessId))"
            } catch { }
        }
    }

    $mainName = [System.IO.Path]::GetFileName($script:Config.chatProcessPattern)
    $analyzerName = [System.IO.Path]::GetFileName($script:Config.analyzerPattern)

    $pys = Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -or $_.Name -match 'py' }
    foreach ($p in $pys) {
        if ($p.CommandLine -match [regex]::Escape($mainName) -or $p.CommandLine -match [regex]::Escape($analyzerName)) {
            try {
                & taskkill.exe /F /T /PID $p.ProcessId 2>&1 | Out-Null
                $killed++
                Write-Info "Processo Python encerrado (PID $($p.ProcessId))"
            } catch { }
        }
    }

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

    Write-Ok ("Total de processos e janelas encontrados e encerrados: $killed")
}

function Start-ManagedProcesses {
    Write-Section 'REINICIANDO PROCESSOS'

    $batMain = Join-Path $script:Config.localDir '0. start.bat'
    $batAnalyzer = Join-Path $script:Config.localDir '1. start_apenas_analisador_prontuarios.bat'

    if (-not (Test-Path $batMain)) { throw "Arquivo .bat nao encontrado: $batMain" }
    if (-not (Test-Path $batAnalyzer)) { throw "Arquivo .bat nao encontrado: $batAnalyzer" }

    Write-Info "Iniciando $batMain..."
    Start-Process -FilePath $batMain -WorkingDirectory $script:Config.localDir | Out-Null
    
    Start-Sleep -Seconds 5
    
    Write-Info "Iniciando $batAnalyzer..."
    Start-Process -FilePath $batAnalyzer -WorkingDirectory $script:Config.localDir | Out-Null

    Write-Ok 'ChatGPT Simulator e analisador de prontuarios reiniciados via arquivos .bat originais.'
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
    Write-Host ("Log:                  {0}" -f $script:LogFile)
    Write-Host '========================================' -ForegroundColor White
}

function Reset-CycleState {
    $script:RestartRequested = $false
    $script:UpdatedFiles = New-Object System.Collections.Generic.List[string]
    $script:AddedFiles = New-Object System.Collections.Generic.List[string]
    $script:ProtectedFiles = New-Object System.Collections.Generic.List[string]
    $script:RepoMirror = $null
    $script:LogFile = $null
    $script:CanWriteRepo = $true
}

function Run-SyncCycle {
    Reset-CycleState
    Import-Settings
    Assert-Configuration
    Initialize-Logging
    Acquire-Lock
    try {
        $script:GitExe = Get-GitExe

        Write-Info "Repositorio local: $($script:Config.localDir)"
        Write-Info "Branch monitorada: $($script:Config.branch)"

        Log-RunningProcessesStatus

        Merge-NewestPullRequest
        Fetch-RepositoryMirror
        Sync-FilesFromMirror

        if ($script:RestartRequested) {
            Stop-ManagedProcesses
            Start-ManagedProcesses
        } else {
            Write-Info 'Nenhum arquivo novo/atualizado. Reinicio nao necessario.'
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
