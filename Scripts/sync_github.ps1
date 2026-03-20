# sync_github.ps1 — Sync chatGPT_Simulator + auto-merge PRs
# Local: C:\chatgpt_simulator\Scripts\sync_github.ps1
# Chamado por: C:\chatgpt_simulator\sync_github.bat

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ErrorActionPreference = 'Continue'

# ============================================================
# CONFIGURACAO
# ============================================================
$token        = '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'
$ghUser       = 'hellyssoncavalcanti'
$repo         = 'chatGPT_Simulator'
$branch       = 'main'
$localDir     = 'C:\chatgpt_simulator'
$repoUrl      = "https://${ghUser}:${token}@github.com/${ghUser}/${repo}.git"
$apiBase      = "https://api.github.com/repos/$ghUser/$repo"
$headers      = @{ Authorization = "Bearer $token"; Accept = "application/vnd.github+json" }

# Resolve temp para path longo (evita HELLYS~2 vs nome completo)
$tempDir      = "$env:TEMP\sync_chatgpt"
if (!(Test-Path $tempDir)) { New-Item -ItemType Directory -Path $tempDir -Force | Out-Null }
$tempDir      = (Get-Item $tempDir).FullName
$gitInstaller = "$tempDir\Git-installer.exe"
$repoTemp     = "$tempDir\repo_mirror"

$logDir = "$localDir\logs"
if (!(Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$logFile = "$logDir\sync_github-$(Get-Date -Format 'dd_MM_yyyy-HH_mm_ss').log"

$script:canWriteRepo = $false

# ============================================================
# FUNCOES AUXILIARES
# ============================================================
function Log($msg) { $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'; "$ts - $msg" | Out-File -Append -FilePath $logFile -Encoding UTF8 }

function Bar([int]$pct, [string]$status) {
    $f = [math]::Floor(40 * $pct / 100); $e = 40 - $f
    $bar = ([string][char]9608) * $f + ([string][char]9617) * $e
    Write-Host -NoNewline ("`r  $bar $pct% - $status".PadRight(110))
    [Console]::Out.Flush()
    if ($pct -eq 100) { Write-Host "" }
    Log "[$pct%] $status"
}

function SpinBar([int]$pct, [string]$status, [string]$spin) {
    $f = [math]::Floor(40 * $pct / 100); $e = 40 - $f
    Write-Host -NoNewline ("`r  $([string][char]9608 * $f)$([string][char]9617 * $e) $pct% $spin $status".PadRight(110))
    [Console]::Out.Flush()
}

function Step($text) { Write-Host ""; Write-Host $text -ForegroundColor Cyan; [Console]::Out.Flush(); Log $text }
function Label($text) { Write-Host ""; Write-Host "  $text" -ForegroundColor Yellow; [Console]::Out.Flush() }
function Msg([string]$text, [string]$color = 'White') { Write-Host ""; Write-Host "  $text" -ForegroundColor $color; [Console]::Out.Flush(); Log $text }

function Show-TokenInstructions {
    Write-Host ""
    Write-Host "  ============================================================" -ForegroundColor Red
    Write-Host "  ERRO DE PERMISSAO DO TOKEN" -ForegroundColor Red
    Write-Host "  ============================================================" -ForegroundColor Red
    Write-Host ""
    Write-Host "  O navegador vai abrir na sua lista de tokens." -ForegroundColor White
    Write-Host ""
    Write-Host "  PASSO 1: Clique no titulo do token 'chatGPT_Simulator'" -ForegroundColor Cyan
    Write-Host "  PASSO 2: Clique no botao 'Edit'" -ForegroundColor Cyan
    Write-Host "  PASSO 3: Em 'Repository permissions', altere:" -ForegroundColor Cyan
    Write-Host "           Contents .............. Read and Write" -ForegroundColor Green
    Write-Host "           Pull requests ......... Read and Write" -ForegroundColor Green
    Write-Host "  PASSO 4: Clique 'Regenerate token' e copie o token" -ForegroundColor Cyan
    Write-Host "  PASSO 5: Edite $localDir\Scripts\sync_github.ps1" -ForegroundColor Cyan
    Write-Host "           Substitua na linha 15: `$token = 'NOVO_TOKEN'" -ForegroundColor White
    Write-Host "  PASSO 6: Execute novamente." -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  ============================================================" -ForegroundColor Red
    Write-Host ""
    [Console]::Out.Flush()
    Start-Process "https://github.com/settings/personal-access-tokens"
}

function Test-TokenPermissions {
    Label 'VERIFICANDO PERMISSOES DO TOKEN:'
    Bar 10 'Consultando...'
    try { $repoInfo = Invoke-RestMethod -Uri $apiBase -Headers $headers }
    catch { Bar 100 'Erro ao consultar repo.'; return $false }
    $canPush = $false
    if ($repoInfo.permissions) { $canPush = $repoInfo.permissions.push -eq $true }
    Log "Permissions: push=$canPush"
    if ($canPush) { Bar 100 'Token OK!'; return $true }
    else { Bar 100 'Token SEM permissao de escrita'; return $false }
}

# ============================================================
# GITHUB API: RESOLVE CONFLITOS
# ============================================================
function Resolve-PR-Via-API {
    param([int]$prNum, [string]$headSha, [string]$prBranchName)

    try { $headCommit = Invoke-RestMethod -Uri "$apiBase/git/commits/$headSha" -Headers $headers }
    catch { Log "PR #$prNum GET commit ERRO"; return $false }
    $treeSha = $headCommit.tree.sha

    try { $mainRef = Invoke-RestMethod -Uri "$apiBase/git/ref/heads/$branch" -Headers $headers }
    catch { Log "PR #$prNum GET main ERRO"; return $false }
    $mainSha = $mainRef.object.sha
    Log "PR #$prNum tree=$treeSha main=$mainSha"

    $commitBody = @{ message = "Merge '$branch' into $prBranchName (auto: accept current change)"; tree = $treeSha; parents = @($headSha, $mainSha) } | ConvertTo-Json
    try { $newCommit = Invoke-RestMethod -Uri "$apiBase/git/commits" -Method Post -Headers $headers -Body $commitBody -ContentType 'application/json' }
    catch { Log "PR #$prNum POST commit ERRO"; return $false }
    $newSha = $newCommit.sha
    Log "PR #$prNum commit=$newSha"

    $refBody = @{ sha = $newSha; force = $true } | ConvertTo-Json
    try { Invoke-RestMethod -Uri "$apiBase/git/refs/heads/$prBranchName" -Method Patch -Headers $headers -Body $refBody -ContentType 'application/json' | Out-Null }
    catch {
        try { Invoke-RestMethod -Uri "$apiBase/git/refs" -Method Post -Headers $headers -Body (@{ ref = "refs/heads/$prBranchName"; sha = $newSha } | ConvertTo-Json) -ContentType 'application/json' | Out-Null }
        catch { Log "PR #$prNum ref ERRO"; return $false }
    }
    return $true
}

# ============================================================
# ETAPA 1: GIT
# ============================================================
function Find-Git {
    if (Get-Command git -ErrorAction SilentlyContinue) { return $true }
    foreach ($p in @("C:\Program Files\Git\cmd", "C:\Program Files (x86)\Git\cmd")) {
        if (Test-Path "$p\git.exe") { $env:PATH += ";$p"; return $true }
    }
    return $false
}

function Download-Git {
    Label 'DOWNLOAD:'
    if (Test-Path $gitInstaller) {
        $sz = (Get-Item $gitInstaller).Length
        if ($sz -gt 30MB) { Bar 100 "Cache ($(([math]::Round($sz/1MB,1)))MB)"; return $true }
        Remove-Item $gitInstaller -Force -ErrorAction SilentlyContinue
    }
    Bar 0 'Consultando versao...'
    try { $rel = Invoke-RestMethod 'https://api.github.com/repos/git-for-windows/git/releases/latest' } catch { Bar 0 "ERRO: $_"; return $false }
    $url = ($rel.assets | Where-Object { $_.name -match '64-bit\.exe$' -and $_.name -notmatch 'portable' } | Select-Object -First 1).browser_download_url
    Log "URL: $url"; Bar 2 'Baixando...'
    try {
        $req = [System.Net.HttpWebRequest]::Create($url); $req.Timeout = 60000
        $resp = $req.GetResponse(); $tB = $resp.ContentLength; $tMB = [math]::Round($tB/1MB,1)
        $s = $resp.GetResponseStream(); $fs = [System.IO.File]::Create($gitInstaller)
        $buf = New-Object byte[] 65536; $tr = 0; $lp = -1; $sp = @('|','/','=','\'); $si = 0
        while (($br = $s.Read($buf,0,$buf.Length)) -gt 0) {
            $fs.Write($buf,0,$br); $tr += $br
            $p = [math]::Floor($tr/$tB*100); $dm = [math]::Round($tr/1MB,1)
            SpinBar $p "${dm}MB / ${tMB}MB" $sp[$si++%4]
            if ($p -ge $lp+10) { $lp=$p; Log "[DL $p%] ${dm}/${tMB}MB" }
        }
        $fs.Close(); $s.Close(); $resp.Close()
    } catch { Write-Host ""; Bar 0 "ERRO: $_"; return $false }
    Write-Host ""; Bar 100 "Download OK (${tMB}MB)"; return $true
}

function Install-GitExe {
    Label 'INSTALACAO:'
    $sp = @('|','/','=','\'); $si = 0; Bar 0 'Instalando...'
    $proc = Start-Process -FilePath $gitInstaller -ArgumentList '/VERYSILENT','/NORESTART','/NOCANCEL','/SP-','/CLOSEAPPLICATIONS' -PassThru
    $to = 300; $el = 0
    while (-not $proc.HasExited -and $el -lt $to) {
        Start-Sleep 1; $el++
        SpinBar ([math]::Min(95,[math]::Floor($el/$to*95))) "Instalando... ($([math]::Floor($el/60))m$($el%60)s)" $sp[$si++%4]
    }
    if (-not $proc.HasExited) { $proc.Kill(); Write-Host ""; Bar 0 'Timeout!'; return $false }
    $env:PATH += ";C:\Program Files\Git\cmd"; Write-Host ""; Bar 100 "Git OK (${el}s)"; return $true
}

function Install-Git {
    Step '[1/4] INSTALANDO GIT'
    if (!(Download-Git)) { return $false }
    if (!(Install-GitExe)) { return $false }
    Remove-Item $gitInstaller -Force -ErrorAction SilentlyContinue; return $true
}

# ============================================================
# ETAPA 2: PULL REQUESTS (so o mais recente, fecha os demais)
# ============================================================
function Merge-PRs {
    Step '[2/4] VERIFICANDO PULL REQUESTS'

    $script:canWriteRepo = Test-TokenPermissions
    if (-not $script:canWriteRepo) { Show-TokenInstructions; return }

    Bar 5 'Consultando PRs abertos...'
    try { $prs = Invoke-RestMethod -Uri "$apiBase/pulls?state=open&base=$branch&per_page=100&sort=created&direction=desc" -Headers $headers }
    catch { Msg "[!] Erro ao consultar PRs." 'Red'; return }

    if ($null -eq $prs -or $prs.Count -eq 0) {
        Bar 100 'Consulta concluida.'
        Msg "[OK] Nenhum Pull Request pendente encontrado." 'Green'
        return
    }

    # Transforma o resultado em um array e ordena pelo número do PR (mais alto = mais recente)
    $prsArray = @($prs)
    $total = $prsArray.Count
    $prsArray = $prsArray | Sort-Object -Property number -Descending
    
    # O PR mais atual passa a ser o índice 0 após a ordenação
    $newest = $prsArray[0]

    # Separa todos os outros PRs para serem fechados
    $older = @()
    if ($total -gt 1) { $older = $prsArray[1..($total-1)] }

    Bar 10 "Encontrados $total PR(s)!"
    Msg "[!] $total Pull Request(s) pendente(s):" 'Yellow'
    foreach ($pr in $prsArray) {
        if ($pr.number -eq $newest.number) {
            Write-Host "   >> PR #$($pr.number) - $($pr.title) [MAIS RECENTE]" -ForegroundColor Green
        } else {
            Write-Host "      PR #$($pr.number) - $($pr.title) [sera fechado]" -ForegroundColor DarkGray
        }
        [Console]::Out.Flush()
    }

    # Fecha sumariamente os PRs que não são o mais atual
    if ($older.Count -gt 0) {
        Msg "Fechando $($older.Count) PR(s) antigo(s)..." 'Yellow'
        foreach ($pr in $older) {
            $num = $pr.number
            try {
                Invoke-RestMethod -Uri "$apiBase/issues/$num/comments" -Method Post -Headers $headers -Body (@{ body = "Fechado automaticamente. O PR mais recente (#$($newest.number)) sera mergeado." } | ConvertTo-Json) -ContentType 'application/json' | Out-Null
                Invoke-RestMethod -Uri "$apiBase/pulls/$num" -Method Patch -Headers $headers -Body (@{ state = 'closed' } | ConvertTo-Json) -ContentType 'application/json' | Out-Null
                Write-Host "    [FECHADO] PR #$num" -ForegroundColor DarkGray
                Log "PR #$num fechado"
            } catch { Write-Host "    [ERRO]   PR #$num" -ForegroundColor Red }
            [Console]::Out.Flush()
        }
    }

    $num = $newest.number; $prBranch = $newest.head.ref; $headSha = $newest.head.sha; $title = $newest.title
    Write-Host ""; Write-Host "  MERGEANDO PR MAIS RECENTE:" -ForegroundColor Yellow
    Write-Host "  PR #$num - $title" -ForegroundColor White
    Log "Merge PR #$num ($title) sha=$headSha"

    Bar 50 "PR #$num : Verificando..."
    try { $det = Invoke-RestMethod -Uri "$apiBase/pulls/$num" -Headers $headers } catch { Bar 100 "Erro"; return }
    if ($null -eq $det.mergeable) { Start-Sleep 1; try { $det = Invoke-RestMethod -Uri "$apiBase/pulls/$num" -Headers $headers } catch {} }

    if ($det.mergeable -eq $true) {
        Bar 70 "PR #$num : Mergeando..."
        try {
            Invoke-RestMethod -Uri "$apiBase/pulls/$num/merge" -Method Put -Headers $headers -Body (@{ merge_method='merge' } | ConvertTo-Json) -ContentType 'application/json' | Out-Null
            Bar 100 "PR #$num : Mergeado!"
            Msg "[OK] PR #$num mergeado!" 'Green'
        } catch { Bar 100 "PR #$num : Falha"; Msg "[X] Falhou." 'Red' }
        return
    }

    Bar 60 "PR #$num : Conflitos! Resolvendo..."
    $resolved = Resolve-PR-Via-API -prNum $num -headSha $headSha -prBranchName $prBranch
    if (-not $resolved) { Bar 100 "Falha"; Msg "[X] Nao resolvido." 'Red'; return }

    Start-Sleep 2; Bar 80 "PR #$num : Merge..."
    $mergeOk = $false
    for ($a = 1; $a -le 3; $a++) {
        try {
            Invoke-RestMethod -Uri "$apiBase/pulls/$num/merge" -Method Put -Headers $headers -Body (@{ merge_method='merge' } | ConvertTo-Json) -ContentType 'application/json' | Out-Null
            $mergeOk = $true; break
        } catch { if ($a -lt 3) { Start-Sleep 2 } }
    }
    if ($mergeOk) { Bar 100 "PR #$num : Mergeado!"; Msg "[OK] PR #$num mergeado (conflitos resolvidos)!" 'Green' }
    else { Bar 100 "Merge falhou"; Msg "[!] Merge manual no GitHub." 'DarkYellow' }
}

# ============================================================
# ETAPA 3: FETCH REPO (Sempre faz um download limpo)
# ============================================================
function Fetch-Repo {
    Step '[3/4] OBTENDO REPOSITORIO ATUALIZADO'
    $env:GIT_TERMINAL_PROMPT = '0'
    Set-Location $env:TEMP

    Bar 10 'Limpando lixo temporario...'
    # Tenta apagar pastas antigas de clone que possam ter ficado para trás
    Get-ChildItem -Path $tempDir -Filter "repo_*" -Directory | ForEach-Object {
        Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
    }

    $uniqueRepoTemp = Join-Path $tempDir "repo_$(Get-Date -Format 'HHmmss')"
    $gitErrLog = Join-Path $tempDir "git_error.log"
    if (Test-Path $gitErrLog) { Remove-Item $gitErrLog -Force -ErrorAction SilentlyContinue }

    # Git clone rodando em segundo plano com barra de progresso
    $sp = @('|','/','=','\'); $si = 0
    $gitArgs = @("clone", "--branch", $branch, "--progress", $repoUrl, $uniqueRepoTemp)
    
    $proc = Start-Process -FilePath "git" -ArgumentList $gitArgs -WindowStyle Hidden -RedirectStandardError $gitErrLog -PassThru

    $to = 600 # Timeout de 10 minutos
    $el = 0
    
    while (-not $proc.HasExited -and $el -lt $to) {
        Start-Sleep -Seconds 1
        $el++
        $pct = [math]::Min(95, 30 + [math]::Floor(($el/$to) * 65))
        $min = [math]::Floor($el / 60)
        $sec = $el % 60
        SpinBar $pct "Baixando arquivos do repositorio... (${min}m${sec}s)" $sp[$si++%4]
    }

    if (-not $proc.HasExited) { 
        $proc.Kill()
        Write-Host ""
        Bar 0 'ERRO: Timeout! O download demorou mais de 10 minutos.'
        return $false 
    }

    # -------------------------------------------------------------------------
    # MODIFICAÇÃO: Aguarda o processo entregar o ExitCode formalmente e valida
    # -------------------------------------------------------------------------
    $proc.WaitForExit()

    # Só considera erro se o código for diferente de nulo e diferente de 0
    if ($null -ne $proc.ExitCode -and $proc.ExitCode -ne 0) {
        Write-Host ""
        Bar 0 "ERRO: Falha no git clone (Codigo: $($proc.ExitCode))."
        
        if (Test-Path $gitErrLog) {
            $realError = (Get-Content $gitErrLog -Raw).Trim()
            Msg "Detalhes do Git:" 'Red'
            Write-Host "  $realError" -ForegroundColor Yellow
        }
        return $false
    }

    # Resolve para path longo usando a pasta nova
    $script:repoTempResolved = (Get-Item $uniqueRepoTemp).FullName
    
    Set-Location $script:repoTempResolved
    $gitFiles = & git ls-files
    $fc = $gitFiles.Count
    
    Write-Host "" 
    Log "Clone: $fc arquivos (path=$($script:repoTempResolved)), tempo=${el}s"
    Bar 100 "Clone OK! ($fc arquivos limpos)"
    return $true
}
# ============================================================
# ETAPA 4: SYNC (so baixa/atualiza, nunca exclui)
# ============================================================
function Sync-Files {
    Step '[4/4] SINCRONIZANDO ARQUIVOS'

    if (!(Test-Path $localDir)) { New-Item -ItemType Directory -Path $localDir -Force | Out-Null }

    # Usa o path longo resolvido
    $mirrorPath = $script:repoTempResolved
    if (-not $mirrorPath -or !(Test-Path $mirrorPath)) {
        Msg "[ERRO] Mirror nao encontrado: $mirrorPath" 'Red'
        Log "ERRO: mirror path=$mirrorPath"
        return
    }

    Set-Location $mirrorPath
    $gitFiles = & git ls-files
    $total = $gitFiles.Count

    if ($total -eq 0) { Msg "[ERRO] Mirror vazio ou falha ao ler arquivos do Git!" 'Red'; return }

    Label "COMPARANDO $total ARQUIVO(S) DO REPOSITORIO:"
    Write-Host ""

    $copied = 0; $skipped = 0; $added = 0; $ignored = 0; $i = 0
    
    # -----------------------------------------------------------------------------------------
    # MODIFICAÇÃO: Lista de itens bloqueados. Agora bloqueia PASTAS inteiras e ARQUIVOS
    # -----------------------------------------------------------------------------------------
    $itensIgnorados = @(
        "sync_github.bat", 
        "scripts\sync_github.ps1",
        "chrome_profile" # Bloqueia a pasta inteira e tudo dentro dela
    )

    foreach ($relPath in $gitFiles) {
        $i++
        
        # Padroniza as barras de diretório para o padrão Windows (\)
        $rel = $relPath.Replace('/', '\')
        $srcFullPath = Join-Path $mirrorPath $rel

        # Verifica se o arquivo ou a pasta atual estão na lista de protegidos
        $isIgnored = $false
        foreach ($ignorado in $itensIgnorados) {
            # Se for exatamente o nome OU se o caminho começar com a pasta bloqueada (ex: chrome_profile\...)
            if ($rel.ToLower() -eq $ignorado.ToLower() -or $rel.ToLower().StartsWith("$($ignorado.ToLower())\")) {
                $isIgnored = $true
                break
            }
        }

        if ($isIgnored) {
            # Descomente a linha abaixo se quiser ver TODOS os arquivos bloqueados na tela
            # Write-Host "    [PROTEGIDO]  $rel" -ForegroundColor DarkYellow
            Log "[PROTEGIDO] $rel"
            $ignored++
        } 
        else {
            if (-not (Test-Path $srcFullPath)) { continue }

            $dst = Join-Path $localDir $rel
            $srcFile = Get-Item $srcFullPath

            if (Test-Path $dst) {
                $df = Get-Item $dst
                
                # Avalia APENAS se a data do repositório é maior que a do arquivo local
                if ($srcFile.LastWriteTimeUtc -gt $df.LastWriteTimeUtc) {
                    $dd = Split-Path $dst -Parent
                    if (!(Test-Path $dd)) { New-Item -ItemType Directory -Path $dd -Force | Out-Null }
                    Copy-Item $srcFullPath $dst -Force; $copied++
                    $kb = [math]::Round($srcFile.Length/1KB,1)
                    Write-Host "    [ATUALIZADO] $rel (${kb}KB)" -ForegroundColor Cyan
                    [Console]::Out.Flush(); Log "[ATUALIZADO] $rel"
                } else { $skipped++ }
            } else {
                $dd = Split-Path $dst -Parent
                if (!(Test-Path $dd)) { New-Item -ItemType Directory -Path $dd -Force | Out-Null }
                Copy-Item $srcFullPath $dst -Force; $added++
                $kb = [math]::Round($srcFile.Length/1KB,1)
                Write-Host "    [NOVO]       $rel (${kb}KB)" -ForegroundColor Green
                [Console]::Out.Flush(); Log "[NOVO] $rel"
            }
        }

        # Cálculo de porcentagem
        if ($i % 10 -eq 0 -or $i -eq $total) { 
            $pct = [math]::Floor(($i / $total) * 100)
            SpinBar $pct "$i / $total" ([string][char]9654) 
        }
    }

    Write-Host ""
    Bar 100 "$added novo(s), $copied atualizado(s), $skipped inalterado(s), $ignored protegido(s)"

    Write-Host ""
    Write-Host "  ============================================" -ForegroundColor Yellow
    Write-Host "  RESUMO:" -ForegroundColor Yellow
    Write-Host "    Novos:        $added" -ForegroundColor Green
    Write-Host "    Atualizados:  $copied" -ForegroundColor Cyan
    Write-Host "    Inalterados:  $skipped" -ForegroundColor DarkGray
    if ($ignored -gt 0) { Write-Host "    Protegidos:   $ignored" -ForegroundColor DarkYellow }
    Write-Host "  ============================================" -ForegroundColor Yellow
    [Console]::Out.Flush()
    Log "Sync: $added novos, $copied atualizados, $skipped inalterados, $ignored protegidos"
    
    # Retorna para a pasta temporária base para liberar o uso da pasta clone
    Set-Location $env:TEMP
}


# ============================================================
# MAIN
# ============================================================
Write-Host ""
Write-Host " ========================================================" -ForegroundColor White
Write-Host "    SYNC chatGPT_Simulator" -ForegroundColor White
Write-Host " ========================================================" -ForegroundColor White
Write-Host ""
Write-Host "  Log:  $logFile" -ForegroundColor DarkGray
Write-Host "  Temp: $tempDir" -ForegroundColor DarkGray
Log "========== SYNC INICIADO =========="
Log "tempDir=$tempDir"
Log "repoTemp=$repoTemp"

if (-not (Find-Git)) {
    $ok = Install-Git
    if (-not $ok -or -not (Find-Git)) { Msg "[ERRO] Git indisponivel." 'Red'; Read-Host "  Enter"; exit 1 }
}
Log "Git: $(& git --version 2>&1)"

Merge-PRs

if (-not $script:canWriteRepo) { Msg "Sync de arquivos continuara (so leitura)." 'DarkGray' }

$ok = Fetch-Repo
if (-not $ok) { Msg "[ERRO] Falha ao obter repo." 'Red'; Read-Host "  Enter"; exit 1 }

Sync-Files

Log "========== SYNC CONCLUIDO =========="

Write-Host ""
Write-Host " ========================================================" -ForegroundColor Green
Write-Host "    CONCLUIDO! $localDir" -ForegroundColor Green
Write-Host "    Log: $logFile" -ForegroundColor Green
if (-not $script:canWriteRepo) { Write-Host "    [!] PRs NAO processados - corrija o token!" -ForegroundColor Yellow }
Write-Host " ========================================================" -ForegroundColor Green
Write-Host ""
Read-Host "  Pressione Enter para sair"