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
        remotePhpSaveUrl    = if ($env:CHATGPT_SIMULATOR_REMOTE_PHP_SAVE_URL) { $env:CHATGPT_SIMULATOR_REMOTE_PHP_SAVE_URL } else { 'https://conexaovida.org/editar_php.php?action=save_file_remote' }
        remotePhpApiKey     = if ($env:CHATGPT_SIMULATOR_REMOTE_PHP_API_KEY) { $env:CHATGPT_SIMULATOR_REMOTE_PHP_API_KEY } else { 'CVAPI_2b9c80c2abf94a76baf8b3e68d89cb7e' }
        remotePhpLocalFile  = if ($env:CHATGPT_SIMULATOR_REMOTE_PHP_LOCAL_FILE) { $env:CHATGPT_SIMULATOR_REMOTE_PHP_LOCAL_FILE } else { 'chatgpt_integracao_criado_pelo_gemini.js.php' }
        remotePhpTargetPath = if ($env:CHATGPT_SIMULATOR_REMOTE_PHP_TARGET_PATH) { $env:CHATGPT_SIMULATOR_REMOTE_PHP_TARGET_PATH } else { 'scripts/js/chatgpt_integracao_criado_pelo_gemini.js.php' }
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
        $params.Body = ($Body | ConvertTo-Json -Depth 10)
        $params.ContentType = 'application/json'
    }
    return Invoke-RestMethod @params
}

function New-PullRequestsForPendingBranches {
    <#
    .SYNOPSIS
        Detecta branches sem PR aberto (prefixos claude/, codex/, chatgpt/) e cria PRs
        automaticamente com titulo e corpo baseados nos commits reais da branch.
    #>
    if (-not $script:Config.githubToken) { return }

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

    if (-not $script:Config.githubToken) {
        $script:CanWriteRepo = $false
        Write-Warn 'Token GitHub nao configurado; etapa de PR sera ignorada, mas o sync dos arquivos ainda sera tentado.'
        return
    }

    # Criar PRs automaticamente para TODAS as branches pendentes (claude/, codex/, chatgpt/)
    New-PullRequestsForPendingBranches

    try {
        $prsResponse = Invoke-GitHubApi -Uri "$($script:Config.apiBase)/pulls?state=open&base=$($script:Config.branch)&per_page=100&sort=created&direction=asc"
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

    # Ordena do mais antigo ao mais novo para merge sequencial
    $ordered = @($prsArray | Sort-Object -Property number)
    Write-Info ("Encontrado(s) {0} PR(s) aberto(s). Mergeando TODOS sequencialmente..." -f $ordered.Count)

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

function Sync-RemotePhpIfNeeded {
    Write-Section 'SINCRONIZANDO PHP REMOTO'

    $localPhpRelative = Normalize-RelativePath $script:Config.remotePhpLocalFile
    if ([string]::IsNullOrWhiteSpace($localPhpRelative)) {
        Write-Info 'Arquivo PHP remoto nao configurado; etapa ignorada.'
        return
    }

    $changedFiles = @($script:AddedFiles) + @($script:UpdatedFiles) | ForEach-Object { Normalize-RelativePath $_ }
    $phpChanged = $changedFiles -contains $localPhpRelative
    if (-not $phpChanged) {
        Write-Info "Arquivo PHP monitorado nao foi alterado neste ciclo: $($script:Config.remotePhpLocalFile)"
        return
    }

    if ([string]::IsNullOrWhiteSpace($script:Config.remotePhpSaveUrl) -or [string]::IsNullOrWhiteSpace($script:Config.remotePhpApiKey)) {
        throw 'Alteracao de PHP detectada, mas remotePhpSaveUrl/remotePhpApiKey nao foram configurados.'
    }

    $localPhpPath = Join-Path $script:Config.localDir $script:Config.remotePhpLocalFile
    if (-not (Test-Path $localPhpPath -PathType Leaf)) {
        throw "Arquivo PHP alterado nao encontrado localmente para sync remoto: $localPhpPath"
    }

    $conteudo = Get-Content -Path $localPhpPath -Raw -Encoding UTF8
    $payload = @{
        api_key  = $script:Config.remotePhpApiKey
        filepath = $script:Config.remotePhpTargetPath
        conteudo = $conteudo
    } | ConvertTo-Json -Depth 6 -Compress

    $targetUrl = $script:Config.remotePhpSaveUrl
    try {
        $uriObj = [System.Uri]$targetUrl
        if ([string]::IsNullOrWhiteSpace($uriObj.Query)) {
            $targetUrl = "$($uriObj.AbsoluteUri)?filepath=$([System.Uri]::EscapeDataString($script:Config.remotePhpTargetPath))"
        } else {
            $targetUrl = "$($uriObj.AbsoluteUri)&filepath=$([System.Uri]::EscapeDataString($script:Config.remotePhpTargetPath))"
        }
    } catch { }

    Write-Info "Atualizando arquivo PHP no servidor remoto: $($script:Config.remotePhpTargetPath)"
    Write-Info "URL completa do endpoint remoto: $targetUrl"
    Write-Info ("Arquivo local origem: {0} ({1} bytes)" -f $localPhpPath, ([System.Text.Encoding]::UTF8.GetByteCount($conteudo)))
    Write-Info "Iniciando requisicao de atualizacao remota (timeout: 60s)..."

    $curlHeaders = @(
        "-H ""Content-Type: application/json""",
        "-H ""Accept: application/json"""
    ) -join ' '
    $payloadTempFile = Join-Path $script:Config.tempDir ("remote_php_payload_{0}.json" -f (Get-Date -Format 'yyyyMMdd_HHmmss_fff'))
    try { Set-Content -Path $payloadTempFile -Value $payload -Encoding UTF8 -NoNewline } catch { }
    $curlCommand = "curl -X POST `"$targetUrl`" $curlHeaders --data-binary `@$payloadTempFile`"

    try {
        $responseRaw = Invoke-WebRequest `
            -Method Post `
            -Uri $script:Config.remotePhpSaveUrl `
            -ContentType 'application/json' `
            -Body $payload `
            -TimeoutSec 60

        $statusCode = [int]$responseRaw.StatusCode
        $responseBody = ($responseRaw.Content | Out-String).Trim()
        Write-Info "Resposta HTTP remota: $statusCode"
        Write-Info "Resposta completa do servidor remoto: $responseBody"

        if ($statusCode -ge 200 -and $statusCode -lt 300 -and ($responseBody -match '"status"\s*:\s*"ok"' -or $responseBody -match '"success"\s*:\s*true')) {
            Write-Ok "PHP remoto atualizado com sucesso: $($script:Config.remotePhpTargetPath)"
            return
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

        Merge-AllPullRequests
        Fetch-RepositoryMirror
        Sync-FilesFromMirror
        Sync-RemotePhpIfNeeded

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
