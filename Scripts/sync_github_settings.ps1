# Arquivo versionado com valores-base de exemplo para o sync automático.
# Na máquina Windows de produção este arquivo pode ser personalizado localmente,
# porque o Scripts\sync_github.ps1 o trata como protegido e não o sobrescreve.


$githubToken         = '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'
$ghUser              = 'hellyssoncavalcanti'
$repo                = 'chatGPT_Simulator'
$branch              = 'main'
$localDir            = 'C:\chatgpt_simulator'
$taskName            = 'chatGPT_Simulator_AutoSync'
$syncIntervalMinutes = 10
$chatProcessPattern  = 'Scripts\\main.py'
$analyzerPattern     = 'Scripts\\analisador_prontuarios.py'


#Chave de API para atualizar o PHP remotamente
$remotePhpApiKey     = 'CVAPI_2b9c80c2abf94a76baf8b3e68d89cb7e'
