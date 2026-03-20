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

function Write-Info([string]$Message, [ConsoleColor]$Color = [ConsoleColor]::Gray) {
    Write-Host "[INFO] $Message" -ForegroundColor $Color
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
    if (Test-Path $script:Config.lockFile) {
        $ageMinutes = ((Get-Date) - (Get-Item $script:Config.lockFile).LastWriteTime).TotalMinutes
        if ($ageMinutes -lt 120) {
            throw "Ja existe outra execucao do sync em andamento (lock: $($script:Config.lockFile))."
        }
        Remove-Item $script:Config.lockFile -Force -ErrorAction SilentlyContinue
    }
    Set-Content -Path $script:Config.lockFile -Value $PID -Encoding ascii
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
        [switch]$AllowFailure
    )

    Push-Location $WorkingDirectory
    try {
        $output = & $script:GitExe @Args 2>&1
        if (-not $AllowFailure -and $LASTEXITCODE -ne 0) {
            throw "Git falhou: $($Args -join ' ')`n$output"
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
        Write-Warn 'Token GitHub nao configurado; etapa de PR sera ignorada, mas o sync dos arquivos ainda sera tentado.'
        return
    }

    $prs = @(Invoke-GitHubApi -Uri "$($script:Config.apiBase)/pulls?state=open&base=$($script:Config.branch)&per_page=100&sort=created&direction=desc")
    if ($prs.Count -eq 0) {
        Write-Info 'Nenhum PR aberto encontrado.'
        return
    }

    $ordered = $prs | Sort-Object -Property @{ Expression = { [datetime]$_.created_at }; Descending = $true }, @{ Expression = { $_.number }; Descending = $true }
    $newest = $ordered[0]
    $older = @()
    if ($ordered.Count -gt 1) {
        $older = @($ordered[1..($ordered.Count - 1)])
    }

    Write-Info ("PR mais recente: #{0} - {1}" -f $newest.number, $newest.title)
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
        Write-Ok ("PR #{0} mergeado automaticamente." -f $newest.number)
    } catch {
        throw "Falha ao mergear PR #$($newest.number): $($_.Exception.Message)"
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
    Invoke-Git -Args @('clone', '--depth', '1', '--branch', $script:Config.branch, $repoUrl, $mirrorPath) | Out-Null
    $script:RepoMirror = $mirrorPath
    Write-Ok ("Espelho atualizado em $mirrorPath")
}

function Test-IsProtectedPath([string]$RelativePath) {
    $pathLower = $RelativePath.ToLowerInvariant()
    foreach ($item in $script:Config.protectedItems) {
        $protected = $item.ToLowerInvariant()
        if ($pathLower -eq $protected -or $pathLower.StartsWith("$protected\\")) {
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

    foreach ($relPathGit in $gitFiles) {
        $relativePath = $relPathGit.Replace('/', '\\')
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
                $gitDate = Invoke-Git -Args @('log', '-1', '--format=%cI', '--', $relPathGit) -WorkingDirectory $script:RepoMirror
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

function Get-PythonCommandLine {
    $venvPython = Join-Path $script:Config.localDir '.venv\Scripts\python.exe'
    if (Test-Path $venvPython) {
        return @($venvPython)
    }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        return @($py.Source, '-3')
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) {
        return @($python.Source)
    }
    throw 'Python nao encontrado para reiniciar os processos.'
}

function Stop-ManagedProcesses {
    Write-Section 'PARANDO PROCESSOS'

    $patterns = @($script:Config.chatProcessPattern, $script:Config.analyzerPattern)
    $killed = 0
    foreach ($proc in @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine })) {
        $commandLine = $proc.CommandLine
        foreach ($pattern in $patterns) {
            if ($commandLine -match [regex]::Escape($pattern)) {
                try {
                    Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
                    $killed++
                    Write-Info ("Processo PID $($proc.ProcessId) encerrado: $commandLine")
                } catch {
                    Write-Warn ("Falha ao encerrar PID $($proc.ProcessId): $($_.Exception.Message)")
                }
                break
            }
        }
    }

    Get-Process -Name 'ms-playwright' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Write-Ok ("Total de processos encerrados: $killed")
}

function Start-ManagedProcesses {
    Write-Section 'REINICIANDO PROCESSOS'

    $pythonCmd = Get-PythonCommandLine
    $mainScript = Join-Path $script:Config.localDir 'Scripts\main.py'
    $analyzerScript = Join-Path $script:Config.localDir 'Scripts\analisador_prontuarios.py'

    if (-not (Test-Path $mainScript)) { throw "Arquivo nao encontrado: $mainScript" }
    if (-not (Test-Path $analyzerScript)) { throw "Arquivo nao encontrado: $analyzerScript" }

    $mainArgs = @($mainScript)
    $analyzerArgs = @($analyzerScript)
    if ($pythonCmd.Count -gt 1) {
        $prefixArgs = @($pythonCmd[1..($pythonCmd.Count - 1)])
        $mainArgs = @($prefixArgs + @($mainScript))
        $analyzerArgs = @($prefixArgs + @($analyzerScript))
    }

    Start-Process -FilePath $pythonCmd[0] -ArgumentList $mainArgs -WorkingDirectory $script:Config.localDir -WindowStyle Minimized | Out-Null
    Start-Sleep -Seconds 5
    Start-Process -FilePath $pythonCmd[0] -ArgumentList $analyzerArgs -WorkingDirectory $script:Config.localDir -WindowStyle Minimized | Out-Null

    Write-Ok 'ChatGPT Simulator e analisador de prontuarios reiniciados.'
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

try {
    Import-Settings
    Assert-Configuration
    Initialize-Logging
    Acquire-Lock
    $script:GitExe = Get-GitExe

    Write-Info "Repositorio local: $($script:Config.localDir)"
    Write-Info "Branch monitorada: $($script:Config.branch)"

    if ($script:InstallTask) {
        Register-AutoSyncTask
        return
    }

    if ($script:UninstallTask) {
        Unregister-AutoSyncTask
        return
    }

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
    exit 0
} catch {
    if ($script:LogFile) {
        Write-Log $_.Exception.ToString()
    }
    Write-Fail $_.Exception.Message
    exit 1
} finally {
    Release-Lock
}
