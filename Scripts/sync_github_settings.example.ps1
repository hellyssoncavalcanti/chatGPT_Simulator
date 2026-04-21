# =============================================================================
# sync_github_settings.example.ps1 — Template limpo versionado.
# =============================================================================
#
# Este arquivo é o gabarito SEM credenciais. O `0. start.bat` copia este
# template para `Scripts/sync_github_settings.ps1` quando o real não existe.
# O real NÃO é sobrescrito em execuções subsequentes — edições locais persistem.
#
# Após a cópia inicial, edite `Scripts/sync_github_settings.ps1` com o PAT
# do GitHub e a API key de produção — ou defina via variáveis de ambiente
# (`CHATGPT_SIMULATOR_GITHUB_TOKEN`, `CHATGPT_SIMULATOR_REMOTE_PHP_API_KEY`).
# =============================================================================

$githubToken         = ''    # GitHub Personal Access Token (preencher localmente)
$ghUser              = ''    # usuário/organização dono do repositório
$repo                = ''    # nome do repositório (ex.: chatGPT_Simulator)
$branch              = 'main'
$localDir            = 'C:\chatgpt_simulator'
$taskName            = 'chatGPT_Simulator_AutoSync'
$syncIntervalMinutes = 10
$chatProcessPattern  = 'Scripts\\main.py'
$analyzerPattern     = 'Scripts\\analisador_prontuarios.py'


# Chave de API do Simulator — deve ser idêntica à SIMULATOR_API_KEY (config.py).
# Usada para atualizar/autenticar o proxy PHP remotamente. Preencher localmente.
$remotePhpApiKey     = ''
