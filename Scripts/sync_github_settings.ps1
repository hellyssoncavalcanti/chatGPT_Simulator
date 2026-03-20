# Arquivo versionado com valores-base de exemplo para o sync automático.
# Na máquina Windows de produção este arquivo pode ser personalizado localmente,
# porque o Scripts\sync_github.ps1 o trata como protegido e não o sobrescreve.

$githubToken         = 'COLE_SEU_TOKEN_AQUI'
$ghUser              = 'seu_usuario_ou_org'
$repo                = 'chatGPT_Simulator'
$branch              = 'main'
$localDir            = 'C:\chatgpt_simulator'
$taskName            = 'chatGPT_Simulator_AutoSync'
$syncIntervalMinutes = 10
$chatProcessPattern  = 'Scripts\\main.py'
$analyzerPattern     = 'Scripts\\analisador_prontuarios.py'
