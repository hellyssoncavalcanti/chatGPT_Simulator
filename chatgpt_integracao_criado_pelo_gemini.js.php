<?php
// ------------------------------------------------------------------
// 👉 
// ------------------------------------------------------------------
// Impede que avisos/erros do PHP sejam impressos no output JS
// (evita SyntaxError no browser por texto PHP inserido no JS)
@ini_set('display_errors', 0);
error_reporting(0);
header("Content-Type: application/javascript; charset=utf-8");

/* ======================================================================
 * VERSÃO: 8.6 (ANTI-503 CONTEXT MANAGER)
 * DATA: 2024-05-24
 * DESCRIÇÃO: 
 * 1. Implementa "Janela Deslizante": Se o contexto exceder o limite seguro
 * do servidor (aprox 25k chars), remove mensagens antigas do meio,
 * mantendo System Prompt e a Pergunta Atual.
 * 2. Aumenta num_ctx para 32768 (suporte a Qwen2.5/Llama3).
 * 3. Mantém correção de Microfone Android e Reconexão SQL.
 * * LINK DO CHAT (DEV REF): 
 * https://gemini.google.com/gem/coding-partner/6c7bae961dd65321
 * ====================================================================== */

// --- CONFIGURAÇÃO: CHAVE DE ACESSO EXTERNO ---
$CHATGPT_VIA_API_KEY = "CVAPI_2b9c80c2abf94a76baf8b3e68d89cb7e";

// --- CONFIGURAÇÃO: IP MANUAL OLLAMA (OPCIONAL) ---
$ollama_manual_ip = ""; 

function chatgpt_rate_limit_check($max_per_min = 200) {
    if (!function_exists('apcu_fetch')) return;
    $ip  = $_SERVER['REMOTE_ADDR'] ?? 'unknown';
    $key = 'rl_' . md5($ip);
    $cnt = apcu_fetch($key) ?: 0;
    if ($cnt >= $max_per_min) { http_response_code(429); echo json_encode(['status'=>'error','message'=>'Rate limit exceeded.']); exit; }
    apcu_store($key, $cnt + 1, 60);
}

function chatgpt_log_query($db, $query, $reason, $elapsed_ms) {
    if (!$db) return;
    $ip = $_SERVER['REMOTE_ADDR'] ?? '';
    @$db->query("INSERT INTO chatgpt_sql_logs (query, reason, ip, elapsed_ms, created_at) VALUES ('"
        . $db->real_escape_string(substr($query, 0, 2000)) . "','"
        . $db->real_escape_string(substr($reason, 0, 500)) . "','"
        . $db->real_escape_string($ip) . "'," . intval($elapsed_ms) . ",NOW())");
}

// Salva tabelas auxiliares (alertas, grafo, casos) em transacao atomica
function chatgpt_salvar_auxiliar($db, $id_atendimento, $id_paciente, $dados) {
    // Garante UTF-8 na conexao antes de qualquer INSERT
    $db->set_charset("utf8mb4");
    $db->query("SET NAMES utf8mb4 COLLATE utf8mb4_unicode_ci");
    $id_at = intval($id_atendimento);
    $id_pc = $db->real_escape_string($id_paciente);
    $db->begin_transaction();
    try {
        // alertas_clinicos
        $alertas = is_string($dados['alertas_clinicos']??null) ? json_decode($dados['alertas_clinicos'],true) : ($dados['alertas_clinicos']??[]);
        if (is_array($alertas)) {
            foreach ($alertas as $al) {
                if (empty($al['tipo_alerta']) && empty($al['descricao'])) continue;
                $tipo  = $db->real_escape_string(substr($al['tipo_alerta']??$al['tipo']??'',0,100));
                $desc  = $db->real_escape_string(substr($al['descricao']??'',0,2000));
                $nivel = strtolower($al['nivel_risco']??$al['nivel']??'');
                $nivel = in_array($nivel,['baixo','moderado','alto']) ? $nivel : 'moderado';
                $db->query("INSERT IGNORE INTO chatgpt_alertas_clinicos (id_atendimento,id_paciente,alerta_tipo,alerta_descricao,nivel_risco,origem_alerta,datetime_detectado) VALUES ({$id_at},'{$id_pc}','{$tipo}','{$desc}','{$nivel}','LLM',NOW())");
            }
        }
        // grafo nodes
        $nodes = is_string($dados['grafo_clinico_nodes']??null) ? json_decode($dados['grafo_clinico_nodes'],true) : ($dados['grafo_clinico_nodes']??[]);
        $id_map = [];
        if (is_array($nodes)) {
            $db->query("DELETE FROM chatgpt_clinical_graph_nodes WHERE id_atendimento={$id_at}");
            foreach ($nodes as $nd) {
                if (empty($nd['valor']??$nd['node_valor']??'')) continue;
                $tipo  = $db->real_escape_string(substr($nd['tipo']??$nd['node_tipo']??'',0,50));
                $valor = $db->real_escape_string(substr($nd['valor']??$nd['node_valor']??'',0,500));
                $norm  = $db->real_escape_string(substr($nd['normalizado']??$nd['node_normalizado']??'',0,500));
                $ctx   = $db->real_escape_string(substr($nd['contexto']??$nd['node_contexto']??'',0,1000));
                $db->query("INSERT INTO chatgpt_clinical_graph_nodes (id_atendimento,id_paciente,node_tipo,node_valor,node_normalizado,node_contexto) VALUES ({$id_at},'{$id_pc}','{$tipo}','{$valor}','{$norm}','{$ctx}')");
                if (!empty($nd['id'])) $id_map[$nd['id']] = $db->insert_id;
            }
        }
        // grafo edges
        $edges = is_string($dados['grafo_clinico_edges']??null) ? json_decode($dados['grafo_clinico_edges'],true) : ($dados['grafo_clinico_edges']??[]);
        if (is_array($edges) && !empty($id_map)) {
            $db->query("DELETE FROM chatgpt_clinical_graph_edges WHERE id_atendimento={$id_at}");
            foreach ($edges as $ed) {
                $orig = $id_map[$ed['node_origem']??'']??0;
                $dest = $id_map[$ed['node_destino']??'']??0;
                if (!$orig||!$dest) continue;
                $tipo = $db->real_escape_string(substr($ed['relacao_tipo']??'',0,100));
                $ctx  = $db->real_escape_string(substr($ed['relacao_contexto']??$ed['contexto']??'',0,1000));
                $db->query("INSERT INTO chatgpt_clinical_graph_edges (id_atendimento,id_paciente,node_origem,node_destino,relacao_tipo,relacao_contexto) VALUES ({$id_at},'{$id_pc}',{$orig},{$dest},'{$tipo}','{$ctx}')");
            }
        }
        // casos_semelhantes
        $casos = is_string($dados['casos_semelhantes']??null) ? json_decode($dados['casos_semelhantes'],true) : ($dados['casos_semelhantes']??[]);
        if (is_array($casos)) {
            foreach ($casos as $cs) {
                $id_dest = intval($cs['id_atendimento_semelhante']??0);
                $score   = floatval($cs['score_similaridade']??0);
                if (!$id_dest || !$score) continue;
                $r = $db->query("SELECT id_paciente FROM chatgpt_atendimentos_analise WHERE id_atendimento={$id_dest} LIMIT 1");
                $id_pc_dest = $db->real_escape_string($r ? ($r->fetch_assoc()['id_paciente']??'') : '');
                $db->query("INSERT IGNORE INTO chatgpt_casos_semelhantes (id_atendimento_origem,id_paciente_origem,id_atendimento_destino,id_paciente_destino,embedding_model,score_similaridade,datetime_calculo) VALUES ({$id_at},'{$id_pc}',{$id_dest},'{$id_pc_dest}','LLM',{$score},NOW())");
            }
        }
        $db->commit();
        return ['success'=>true];
    } catch (Exception $e) {
        $db->rollback();
        return ['success'=>false,'error'=>$e->getMessage()];
    }
}

/* ======================================================================
 * INCLUDES E AMBIENTE
 * ====================================================================== */
// [FIX 8.6] Aumentando agressivamente memória e tempo
ini_set('memory_limit', '2048M');
ini_set('max_execution_time', '0'); 
set_time_limit(0);

if(isset($is_iframe) && !empty($is_iframe)){$is_iframe_old = $is_iframe;unset($is_iframe);} 
if(isset($this_file) && !empty($this_file)){$this_file_old = $this_file;unset($this_file);} 
$this_file = implode('/', explode('\\', str_replace($_SERVER['DOCUMENT_ROOT'], '', __FILE__)));
$this_file = implode('/', explode('\\', str_replace(((strpos($_SERVER['DOCUMENT_ROOT'], '\\') !== false)?str_replace('\\', '/', $_SERVER['DOCUMENT_ROOT']):$_SERVER['DOCUMENT_ROOT']), '', $this_file)));
$id = explode('/', $this_file);$id = array_pop($id);$id = explode('.', $id);$id = array_shift($id);
if((!isset($is_iframe) || empty($is_iframe)) && strpos($this_file, $_SERVER['PHP_SELF']) !== false){$is_iframe = TRUE;}

$currentFileName = basename(__FILE__);
$user_can_edit_system = false; 

if($is_iframe || (isset($_GET['action']) && $_GET['action'] !== 'api_exec'))
{
  header("Content-Type: text/html; charset=UTF-8", true);
  date_default_timezone_set('America/Recife');
  //PREVENÇÃO DE CACHE AGRESSIVO (ESPECIALMENTE PARA SAFARI/MOBILE):
  header("Cache-Control: no-store, no-cache, must-revalidate, max-age=0");header("Cache-Control: post-check=0, pre-check=0", false);header("Pragma: no-cache");header("Expires: Wed, 11 Jan 1984 05:00:00 GMT"); // Uma data no passado, para evitar que os navegadores guardem o arquivo em cache.
  $filename = 'config/config.php';if(file_exists($filename)){@include_once($filename);}elseif(file_exists("../".$filename)){@include_once("../".$filename);}elseif(file_exists("../../".$filename)){@include_once("../../".$filename);}elseif(file_exists("../../../".$filename)){@include_once("../../../".$filename);} 
  $filename = 'scripts/login.php';if(file_exists($filename)){@include_once($filename);}elseif(file_exists("../".$filename)){@include_once("../".$filename);}elseif(file_exists("../../".$filename)){@include_once("../../".$filename);}elseif(file_exists("../../../".$filename)){@include_once("../../../".$filename);} 
  $filename = 'scripts/func.inc.php';if(file_exists($filename)){@include_once($filename);}elseif(file_exists("../".$filename)){@include_once("../".$filename);}elseif(file_exists("../../".$filename)){@include_once("../../".$filename);}elseif(file_exists("../../../".$filename)){@include_once("../../../".$filename);} 

  ini_set('display_errors', 0); 
  ini_set('log_errors', 1);
  error_reporting(E_ALL);

  if(isset($row_login_atual['id']) && verifica_permissao($mysqli, $row_login_atual['id'], 'chatgpt_system_prompt', 'editar')) {
      $user_can_edit_system = true;
  }
}



// -----------------------------------------------------
// FUNÇÃO DE VERIFICAÇÃO DE API_KEY OU SE É PEDIDO ADVINDO DO PRÓPRIO PHP INTERNO.
// -----------------------------------------------------
/**
 * Verifica se a requisição vem do mesmo host (dispensando API key),
 * ou valida a API key para origens externas.
 * Encerra com 401 se a validação falhar.
 *
 * @param string $providedKey  Chave fornecida pelo cliente (header ou body)
 * @param string $expectedKey  Chave válida configurada no sistema
 */
function HasRequiredApiKeyOrIsSameOrigin(string $providedKey, string $expectedKey): bool {
    $origin     = $_SERVER['HTTP_ORIGIN'] ?? '';
    $serverHost = $_SERVER['SERVER_NAME'] ?? $_SERVER['HTTP_HOST'] ?? '';

    $isSameOrigin = !empty($origin)
        && parse_url($origin, PHP_URL_HOST) === $serverHost;

    if ($isSameOrigin) return true;

    return !empty($providedKey) && $providedKey === $expectedKey;
}


// -----------------------------------------------------
// FUNÇÃO DE CONEXÃO AO BANCO (GLOBAL)
// -----------------------------------------------------
function get_mysql_connection_local() {
    global $mysqli, $config, $hostname_conexao, $database_conexao, $username_conexao, $password_conexao;
    
    // Força nova conexão se a global caiu
    if (isset($mysqli) && $mysqli instanceof mysqli) {
        try {
            if (@$mysqli->ping()) return $mysqli;
        } catch(Exception $e) {}
    }

    $host = $config["mysql_host"] ?? $hostname_conexao ?? null;
    $user = $config["mysql_login"] ?? $username_conexao ?? null;
    $pass = $config["mysql_password"] ?? $password_conexao ?? null;
    $db   = $config["mysql_db"] ?? $database_conexao ?? null;
    $port = $config["mysql_port"] ?? 3306;
    
    if (!$host) {
        $paths = ['config/config.php', '../config/config.php', '../../config/config.php'];
        foreach($paths as $p) {
            if(file_exists($p)) { include($p); break; }
        }
        $host = $config["mysql_host"] ?? $hostname_conexao ?? null;
        $user = $config["mysql_login"] ?? $username_conexao ?? null;
        $pass = $config["mysql_password"] ?? $password_conexao ?? null;
        $db   = $config["mysql_db"] ?? $database_conexao ?? null;
    }

    if (!$host) return null;
    $con = new mysqli($host, $user, $pass, $db, $port);
    if ($con->connect_error) return null;
    $con->set_charset("utf8mb4");
    return $con;
}

// -----------------------------------------------------
// HANDLER: SALVAR TABELAS AUXILIARES (alertas, grafo, casos_semelhantes)
// POST ?action=salvar_analise_auxiliar
// Body: {api_key, id_atendimento, id_paciente, alertas_clinicos,
//        grafo_clinico_nodes, grafo_clinico_edges, casos_semelhantes}
// -----------------------------------------------------
if (isset($_GET['action']) && $_GET['action'] === 'salvar_analise_auxiliar') {
    while (ob_get_level()) ob_end_clean();
    header('Content-Type: application/json; charset=utf-8');
    header('Access-Control-Allow-Origin: https://conexaovida.org');
    chatgpt_rate_limit_check();
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') { echo json_encode(['success'=>false,'error'=>'POST required']); exit; }
    $body = json_decode(file_get_contents('php://input'), true);
    if (json_last_error() !== JSON_ERROR_NONE) { http_response_code(400); echo json_encode(['success'=>false,'error'=>'JSON invalido']); exit; }
    $headers    = function_exists('getallheaders') ? array_change_key_case(getallheaders(), CASE_UPPER) : [];
    $headerKey  = $_SERVER['HTTP_X_API_KEY'] ?? $headers['X-API-KEY'] ?? $headers['X_API_KEY'] ?? '';
    $providedKey = $headerKey ?: ($body['api_key'] ?? '');
    if (!HasRequiredApiKeyOrIsSameOrigin($providedKey, $CHATGPT_VIA_API_KEY)) { http_response_code(401); echo json_encode(['success'=>false,'error'=>'Unauthorized']); exit; }
    $id_at = intval($body['id_atendimento'] ?? 0);
    $id_pc = $body['id_paciente'] ?? '';
    if (!$id_at || !$id_pc) { echo json_encode(['success'=>false,'error'=>'id_atendimento e id_paciente obrigatorios']); exit; }
    $db = get_mysql_connection_local();
    if (!$db) { echo json_encode(['success'=>false,'error'=>'Database connection failed']); exit; }
    echo json_encode(chatgpt_salvar_auxiliar($db, $id_at, $id_pc, $body));
    exit;
}

// -----------------------------------------------------
// HANDLER: CHECAR SERVIDOR OLLAMA E "CHATGPT SIMULATOR".
// -----------------------------------------------------
if (isset($_GET['action']) && $_GET['action'] === 'ping_simulator') {
    while (ob_get_level()) ob_end_clean();
    header('Content-Type: application/json; charset=utf-8');
    
    // Identifica o IP base (mesma lógica do proxy)
    if (!empty($ollama_manual_ip)) {
        $ip_final = $ollama_manual_ip;
    } else {
        $url_monitor = "http://conexaovida.org/no-ip-dynamic_ip.php?port=3003";
        $ch = curl_init($url_monitor);
        curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
        curl_setopt($ch, CURLOPT_FOLLOWLOCATION, true); 
        curl_setopt($ch, CURLOPT_HEADER, true);         
        curl_setopt($ch, CURLOPT_TIMEOUT, 5);
        $raw_response = curl_exec($ch);
        $effective_url = curl_getinfo($ch, CURLINFO_EFFECTIVE_URL); 
        curl_close($ch);
        $ip_found = null;
        if (preg_match('/(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/', $effective_url, $matches)) { $ip_found = $matches[1]; } 
        else if (preg_match('/(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/', $raw_response ?? '', $matches)) { $ip_found = $matches[1]; }
        if ($ip_found && filter_var($ip_found, FILTER_VALIDATE_IP)) { $ip_final = "http://" . $ip_found . ":3003"; }
    }
    
    if (empty($ip_final) || !filter_var($ip_final, FILTER_VALIDATE_URL)) {
        header('Content-Type: application/json');
        echo json_encode(["error" => "IP_ERROR", "msg" => "Não foi possível detectar IP."]); exit;
    }

    $ch = curl_init("$ip_final");

    // Faz um ping rápido (2 segundos de timeout) à porta do simulador
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    curl_setopt($ch, CURLOPT_TIMEOUT, 2); 
    curl_exec($ch);
    $err = curl_error($ch);
    curl_close($ch);

    // Se não houver erro de cURL (ex: Connection Refused), o Node.js está online
    echo json_encode(['online' => empty($err)]);
    exit;
}

// -----------------------------------------------------
// HANDLER: ACESSO EXTERNO VIA API (CHATGPT/AGENTS)
// -----------------------------------------------------
if (isset($_GET['action']) && $_GET['action'] === 'api_exec') {
    while (ob_get_level()) ob_end_clean();
    header('Content-Type: application/json; charset=utf-8');
    header('Access-Control-Allow-Origin: https://conexaovida.org');
    header('Access-Control-Allow-Methods: POST');
    chatgpt_rate_limit_check();
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') { echo json_encode(['status'=>'error','message'=>'Method Not Allowed. Use POST.']); exit; }
    $inputJSON = file_get_contents('php://input');
    $body = json_decode($inputJSON, true);
    if (json_last_error() !== JSON_ERROR_NONE) { http_response_code(400); echo json_encode(['status'=>'error','message'=>'JSON invalido: '.json_last_error_msg()]); exit; }
    $headers   = function_exists('getallheaders') ? array_change_key_case(getallheaders(), CASE_UPPER) : [];
    $headerKey = $_SERVER['HTTP_X_API_KEY']
              ?? $headers['X-API-KEY']
              ?? $headers['X_API_KEY']
              ?? '';
    $bodyKey   = $body['api_key'] ?? '';
    $providedKey = $headerKey ? $headerKey : $bodyKey;

    if (!HasRequiredApiKeyOrIsSameOrigin($providedKey, $CHATGPT_VIA_API_KEY)) {
        http_response_code(401);
        echo json_encode(['status' => 'error', 'message' => 'Unauthorized. Invalid API Key.']); exit;
    }

    $sql = trim($body['sql'] ?? '');
    if (empty($sql)) {
        echo json_encode(['status' => 'error', 'message' => 'No SQL provided.']); exit;
    }

    $globalForbidden = ['GRANT', 'REVOKE', 'FLUSH', 'SHUTDOWN'];
    foreach($globalForbidden as $bad) {
        if (stripos($sql, $bad) !== false) {
            echo json_encode(['status' => 'error', 'message' => "Security Violation: Command '$bad' is strictly forbidden."]); exit;
        }
    }

    $writeCmds = ['INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER', 'TRUNCATE', 'CREATE', 'REPLACE'];
    $firstWord = strtoupper(strtok($sql, ' '));
    $isWrite = in_array($firstWord, $writeCmds);

    if ($isWrite) {
        $allowedPrefix = 'chatgpt_';
        $targetTable = '';
        $isAllowed = false;

        $patterns = [
            '/^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?([a-zA-Z0-9_]+)`?/i',
            '/^DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?`?([a-zA-Z0-9_]+)`?/i',
            '/^ALTER\s+TABLE\s+`?([a-zA-Z0-9_]+)`?/i',
            '/^INSERT\s+INTO\s+`?([a-zA-Z0-9_]+)`?/i',
            '/^UPDATE\s+`?([a-zA-Z0-9_]+)`?/i',
            '/^DELETE\s+FROM\s+`?([a-zA-Z0-9_]+)`?/i',
            '/^TRUNCATE\s+(?:TABLE\s+)?`?([a-zA-Z0-9_]+)`?/i'
        ];

        foreach ($patterns as $pattern) {
            if (preg_match($pattern, trim($sql), $matches)) {
                $targetTable = $matches[1];
                break;
            }
        }

        if ($targetTable) {
            if (strpos($targetTable, $allowedPrefix) === 0) {
                $isAllowed = true;
            } else {
                echo json_encode(['status' => 'error', 'message' => "Access Denied: You can only modify tables starting with '{$allowedPrefix}'. Target: '{$targetTable}'."]); exit;
            }
        } else {
            echo json_encode(['status' => 'error', 'message' => "Security Error: Could not parse target table for write operation."]); exit;
        }
    }

    try {
        $db = get_mysql_connection_local();
        if (!$db) throw new Exception("Database connection failed.");
        $db->query("SET SESSION MAX_EXECUTION_TIME=5000");

        $result = $db->query($sql);
        
        if ($result === false) { throw new Exception($db->error); }

        if ($result instanceof mysqli_result) {
            $rows = $result->fetch_all(MYSQLI_ASSOC);
            echo json_encode(['status'=>'success','operation'=>'read','count'=>count($rows),'data'=>$rows], JSON_UNESCAPED_UNICODE);
        } else {
            echo json_encode([
                'status' => 'success', 
                'operation' => 'write',
                'affected_rows' => $db->affected_rows,
                'message' => 'Command executed successfully.'
            ]);
        }

    } catch (Exception $e) {
        echo json_encode(['status' => 'error', 'message' => $e->getMessage()]);
    }
    exit;
}


// -----------------------------------------------------
// HANDLER: EXECUÇÃO DE SQL DO FRONTEND (FIX 9.2)
// -----------------------------------------------------
if (isset($_GET['action']) && $_GET['action'] === 'execute_sql') {
    while (ob_get_level()) ob_end_clean();
    header('Content-Type: application/json; charset=utf-8');
    header('Access-Control-Allow-Origin: https://conexaovida.org');
    header('Access-Control-Allow-Methods: POST');
    chatgpt_rate_limit_check();
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') { echo json_encode(['success'=>false,'error'=>'Method Not Allowed. Use POST.']); exit; }

    // Aceita same-origin OU api_key válida (para acesso externo, ex: analisador_prontuarios.py)
    $inputJSON = file_get_contents('php://input');
    $body      = json_decode($inputJSON, true);
    $esHeaders = function_exists('getallheaders') ? array_change_key_case(getallheaders(), CASE_UPPER) : [];
    $esApiKey  = $_SERVER['HTTP_X_API_KEY'] ?? $esHeaders['X-API-KEY'] ?? $esHeaders['X_API_KEY'] ?? $body['api_key'] ?? '';
    if (!HasRequiredApiKeyOrIsSameOrigin($esApiKey, $CHATGPT_VIA_API_KEY)) {
        http_response_code(401);
        echo json_encode(['success' => false, 'error' => 'Acesso negado. Requisição de origem externa não permitida neste endpoint.']); exit;
    }

    $query  = trim($body['query']  ?? '');
    $reason = $body['reason'] ?? 'Sem descrição';

    if (empty($query)) {
        echo json_encode(['success' => false, 'error' => 'Query vazia', 'query' => '', 'reason' => $reason]);
        exit;
    }

    // Validação de segurança - apenas SELECT, SHOW, DESCRIBE, EXPLAIN
    $forbidden = ['INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER', 'TRUNCATE', 'CREATE', 'REPLACE', 'GRANT', 'REVOKE'];
    $firstWord = strtoupper(preg_split('/\s+/', ltrim($query))[0]);

    // Exceção: comandos de escrita são permitidos em tabelas com prefixo "chatgpt_"
    // Verifica se o único alvo da query é uma tabela chatgpt_*
    $isChatgptTableOnly = (bool) preg_match('/^\s*(DELETE\s+FROM|INSERT\s+INTO|UPDATE|REPLACE\s+INTO)\s+`?chatgpt_\w+`?\s/i', $query);

    foreach ($forbidden as $bad) {
        if (stripos($query, $bad) !== false) {
            // Permite o comando se for escrita exclusiva em tabela chatgpt_
            if ($isChatgptTableOnly && in_array($bad, ['DELETE', 'INSERT', 'UPDATE', 'REPLACE'])) {
                continue;
            }
            echo json_encode([
                'success' => false,
                'error'   => "Comando '{$bad}' não permitido. Apenas consultas seguras são permitidas.",
                'query'   => $query,
                'reason'  => $reason
            ]);
            exit;
        }
    }

    $allowedCommands = ['SELECT', 'SHOW', 'DESCRIBE', 'EXPLAIN', 'DESC', 'DELETE', 'INSERT', 'UPDATE', 'REPLACE'];
    if (!in_array($firstWord, $allowedCommands)) {
        echo json_encode([
            'success' => false,
            'error'   => "Apenas comandos SELECT, SHOW, DESCRIBE, EXPLAIN são permitidos. Recebido: {$firstWord}",
            'query'   => $query,
            'reason'  => $reason
        ]);
        exit;
    }
    // Escrita só é permitida em tabelas chatgpt_ — bloqueia se não for
    if (in_array($firstWord, ['DELETE', 'INSERT', 'UPDATE', 'REPLACE']) && !$isChatgptTableOnly) {
        echo json_encode([
            'success' => false,
            'error'   => "Comandos de escrita só são permitidos em tabelas com prefixo 'chatgpt_'.",
            'query'   => $query,
            'reason'  => $reason
        ]);
        exit;
    }

    // --- [FIX] Sanitiza valores para JSON válido (resolve tabelas com ENUM, DEFAULT complexo, charset misto) ---
    function sanitize_utf8_recursive($value) {
        if (is_string($value)) {
            // 1. Força conversão para UTF-8 limpo (remove bytes inválidos)
            $clean = mb_convert_encoding($value, 'UTF-8', 'UTF-8');
            // 2. Fallback mais agressivo se ainda houver bytes problemáticos
            if (!mb_check_encoding($clean, 'UTF-8')) {
                $clean = iconv('UTF-8', 'UTF-8//IGNORE', $value);
            }
            return $clean ?? '';
        }
        if (is_array($value)) {
            return array_map('sanitize_utf8_recursive', $value);
        }
        return $value;
    }

    function safe_json_encode($payload) {
        // Tenta encode normal com substituto para UTF-8 inválido (PHP 7.2+)
        $encoded = json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_INVALID_UTF8_SUBSTITUTE);

        if ($encoded !== false) {
            return $encoded;
        }

        // Fallback: sanitiza recursivamente e tenta de novo
        $clean = sanitize_utf8_recursive($payload);
        $encoded = json_encode($clean, JSON_UNESCAPED_UNICODE | JSON_INVALID_UTF8_SUBSTITUTE);

        if ($encoded !== false) {
            return $encoded;
        }

        // Último recurso: serialização segura substituindo valores problemáticos
        array_walk_recursive($clean, function (&$v) {
            if (is_string($v)) {
                $v = mb_convert_encoding($v, 'UTF-8', 'auto');
            }
        });
        return json_encode($clean, JSON_UNESCAPED_UNICODE) ?: json_encode(['error' => 'Falha ao serializar resultado.']);
    }

    try {
        $db = get_mysql_connection_local();
        if (!$db) {
            throw new Exception("Falha na conexão com banco de dados");
        }

        // Garante charset correto na conexão antes de executar
        $db->set_charset('utf8mb4');
        $db->query("SET SESSION MAX_EXECUTION_TIME=5000");
        $t_start = microtime(true);
        $result = $db->query($query);

        if ($result === false) {
            throw new Exception($db->error);
        }

        chatgpt_log_query($db, $query, $reason, intval((microtime(true)-$t_start)*1000));
        if ($result instanceof mysqli_result) {
            $rows = $result->fetch_all(MYSQLI_ASSOC);
            echo safe_json_encode(['success'=>true,'num_rows'=>count($rows),'data'=>$rows,'query'=>$query,'reason'=>$reason]);
        } else {
            echo json_encode([
                'success'       => true,
                'affected_rows' => $db->affected_rows,
                'query'         => $query,
                'reason'        => $reason
            ]);
        }

    } catch (Exception $e) {
        echo json_encode([
            'success' => false,
            'error'   => $e->getMessage(),
            'query'   => $query,
            'reason'  => $reason
        ]);
    }
    exit;
}

// -----------------------------------------------------
// HANDLER: SALVAR METADADOS DO CHAT NO BANCO (MYSQL)
// -----------------------------------------------------
if (isset($_GET['action']) && $_GET['action'] === 'save_chat_meta') {
    header('Content-Type: application/json; charset=utf-8');

    $inputJSON = file_get_contents('php://input');
    $data = json_decode($inputJSON, true);

    $id_chatgpt    = $data['id_chatgpt']  ?? '';
    $url_chatgpt   = $data['url_chatgpt'] ?? '';
    $url_atual     = $data['url_atual']   ?? '';

    // Contexto clínico vindo do corpo POST
    $id_paciente    = isset($data['id_paciente'])    && is_numeric($data['id_paciente'])    ? intval($data['id_paciente'])    : null;
    $id_membro      = isset($data['id_membro'])      && is_numeric($data['id_membro'])      ? intval($data['id_membro'])      : null;
    // id_paciente tem prioridade; id_membro é fallback quando não há id_paciente
    if (!$id_paciente && $id_membro) $id_paciente = $id_membro;
    $id_atendimento = isset($data['id_atendimento']) && is_numeric($data['id_atendimento']) ? intval($data['id_atendimento']) : null;
    $id_receita     = isset($data['id_receita'])     && is_numeric($data['id_receita'])     ? intval($data['id_receita'])     : null;
    global $row_login_atual;
    @session_start();

    $id_criador = $row_login_atual['id'] ?? ($_SESSION['id'] ?? 'NULL');

    if (empty($id_chatgpt)) {
        echo json_encode(['success' => false, 'error' => 'ID do ChatGPT vazio']);
        exit;
    }

    try {
        $db = get_mysql_connection_local();
        if (!$db) throw new Exception("Falha na conexão com banco de dados");

        $db->set_charset("utf8mb4");

        $id_chatgpt_esc  = $db->real_escape_string($id_chatgpt);
        $url_chatgpt_esc = $db->real_escape_string($url_chatgpt);
        $url_atual_esc   = $db->real_escape_string($url_atual);
        $id_criador_esc  = is_numeric($id_criador) ? intval($id_criador) : "NULL";

        // ------------------------------------------------------------------
        // Gera título automaticamente baseado no contexto clínico
        // (nunca usa o título vindo do POST — calculado aqui no servidor)
        // ------------------------------------------------------------------
        $nome_paciente = null;
        if ($id_atendimento) {
            $r = $db->query("SELECT m.nome FROM clinica_atendimentos ca JOIN membros m ON m.id = ca.id_paciente WHERE ca.id = $id_atendimento LIMIT 1");
            if ($r && $r->num_rows > 0) $nome_paciente = $r->fetch_assoc()['nome'];
            $titulo = 'ConexaoVida IA' . ($nome_paciente ? " - $nome_paciente" : '') . " - Atend. $id_atendimento";
        } elseif ($id_receita) {
            $r = $db->query("SELECT m.nome FROM clinica_receitas cr JOIN membros m ON m.id = cr.id_paciente WHERE cr.id = $id_receita LIMIT 1");
            if ($r && $r->num_rows > 0) $nome_paciente = $r->fetch_assoc()['nome'];
            $titulo = 'ConexaoVida IA' . ($nome_paciente ? " - $nome_paciente" : '') . " - Receita/Laudo $id_receita";
        } elseif ($id_paciente) {
            $r = $db->query("SELECT nome FROM membros WHERE id = $id_paciente LIMIT 1");
            if ($r && $r->num_rows > 0) $nome_paciente = $r->fetch_assoc()['nome'];
            $titulo = 'ConexaoVida IA' . ($nome_paciente ? " - $nome_paciente" : '');
        } else {
            $titulo = 'ConexaoVida IA - Geral';
        }
        $titulo_esc = $db->real_escape_string($titulo);

        if ($id_criador_esc === "NULL") {
            throw new Exception("Usuário não autenticado (id_criador nulo). Chat não será vinculado.");
        }

        // ------------------------------------------------------------------
        // Monta WHERE de busca conforme prioridade de contexto clínico
        // ------------------------------------------------------------------
        if ($id_atendimento) {
            $where_check = "id_atendimento = $id_atendimento";
        } elseif ($id_receita) {
            $where_check = "id_receita = $id_receita AND id_atendimento IS NULL";
        } elseif ($id_paciente) {
            $where_check = "id_paciente = $id_paciente AND id_atendimento IS NULL AND id_receita IS NULL";
        } else {
            $where_check = "id_criador = $id_criador_esc AND id_atendimento IS NULL AND id_receita IS NULL AND id_paciente IS NULL";
        }

        $check_sql    = "SELECT id FROM chatgpt_chats WHERE $where_check LIMIT 1";
        $check_result = $db->query($check_sql);

        // Monta os valores SQL dos campos de contexto (NULL se ausente)
        $sql_paciente    = $id_paciente    ? $id_paciente    : "NULL";
        $sql_atendimento = $id_atendimento ? $id_atendimento : "NULL";
        $sql_receita     = $id_receita     ? $id_receita     : "NULL";

        if ($check_result && $check_result->num_rows > 0) {
            $row   = $check_result->fetch_assoc();
            $db_id = $row['id'];

            $update_sql = "UPDATE chatgpt_chats SET
                titulo        = '$titulo_esc',
                id_chatgpt    = '$id_chatgpt_esc',
                url_chatgpt   = '$url_chatgpt_esc',
                url_atual     = '$url_atual_esc',
                id_criador    = $id_criador_esc,
                id_paciente   = $sql_paciente,
                id_atendimento = $sql_atendimento,
                id_receita    = $sql_receita
                WHERE id = $db_id";

            if (!$db->query($update_sql)) {
                throw new Exception($db->error);
            }
        } else {
            $insert_sql = "INSERT INTO chatgpt_chats
                (id_criador, id_paciente, id_atendimento, id_receita, url_atual, titulo, id_chatgpt, url_chatgpt)
                VALUES
                ($id_criador_esc, $sql_paciente, $sql_atendimento, $sql_receita,
                 '$url_atual_esc', '$titulo_esc', '$id_chatgpt_esc', '$url_chatgpt_esc')";

            if (!$db->query($insert_sql)) {
                throw new Exception($db->error);
            }
        }

        echo json_encode(['success' => true, 'titulo' => $titulo, 'sql' => ((isset($insert_sql) && !empty($insert_sql))?$insert_sql:$update_sql)]);
    } catch (Exception $e) {
        echo json_encode(['success' => false, 'error' => $e->getMessage(), 'sql' => ((isset($insert_sql) && !empty($insert_sql))?$insert_sql:$update_sql)]);
    }
    exit;
}

// -----------------------------------------------------
// HANDLER: RECUPERAR METADADOS DO CHAT DO BANCO (MYSQL)
// -----------------------------------------------------
if (isset($_GET['action']) && $_GET['action'] === 'get_chat_meta') {
    header('Content-Type: application/json; charset=utf-8');

    $inputJSON = file_get_contents('php://input');
    $data = json_decode($inputJSON, true);
    
    // Contexto clínico vindo do corpo POST
    $id_paciente    = isset($data['id_paciente'])    && is_numeric($data['id_paciente'])    ? intval($data['id_paciente'])    : null;
    $id_membro      = isset($data['id_membro'])      && is_numeric($data['id_membro'])      ? intval($data['id_membro'])      : null;
    // id_paciente tem prioridade; id_membro é fallback quando não há id_paciente
    if (!$id_paciente && $id_membro) $id_paciente = $id_membro;
    $id_atendimento = isset($data['id_atendimento']) && is_numeric($data['id_atendimento']) ? intval($data['id_atendimento']) : null;
    $id_receita     = isset($data['id_receita'])     && is_numeric($data['id_receita'])     ? intval($data['id_receita'])     : null;

    global $row_login_atual;
    @session_start();

    $id_criador = $row_login_atual['id'] ?? ($_SESSION['id'] ?? 'NULL');

    if ($id_criador === 'NULL') {
        echo json_encode(['success' => false, 'error' => 'Utilizador não autenticado.']);
        exit;
    }

    try {
        $db = get_mysql_connection_local();
        if (!$db) throw new Exception("Falha na conexão com banco de dados");

        $db->set_charset("utf8mb4");
        $id_criador_esc = intval($id_criador);

        // ------------------------------------------------------------------
        // Monta WHERE de busca conforme prioridade de contexto clínico
        // ------------------------------------------------------------------
        if ($id_atendimento) {
            $where = "id_atendimento = $id_atendimento";
        } elseif ($id_receita) {
            $where = "id_receita = $id_receita AND id_atendimento IS NULL";
        } elseif ($id_paciente) {
            $where = "id_paciente = $id_paciente AND id_atendimento IS NULL AND id_receita IS NULL";
        } else {
            $where = "id_criador = $id_criador_esc AND id_atendimento IS NULL AND id_receita IS NULL AND id_paciente IS NULL";
        }

        $sql = "SELECT id_chatgpt, url_chatgpt, titulo FROM chatgpt_chats
                WHERE $where
                ORDER BY datetime_atualizacao DESC LIMIT 1";
        $sql = preg_replace('/\s+/', ' ', $sql);
        
        $result = $db->query($sql);
        
        if ($result && $result->num_rows > 0) {
            $row = $result->fetch_assoc();
            echo json_encode([
                'success' => true,
                'chat'    => [
                    'id_chatgpt'  => $row['id_chatgpt'],
                    'url_chatgpt' => $row['url_chatgpt'],
                    'titulo'      => $row['titulo']
                ],
                'sql' => $sql
            ]);
        } else {
            echo json_encode(['success' => false, 'error' => 'Nenhum chat prévio encontrado.', 'sql' => $sql]);
        }
    } catch (Exception $e) {
        echo json_encode(['success' => false, 'error' => $e->getMessage(), 'sql' => $sql]);
    }
    exit;
}

// -----------------------------------------------------
// HANDLER: CARREGAR PROMPTS DO BANCO (MYSQL)
// -----------------------------------------------------
if (isset($_GET['action']) && $_GET['action'] === 'get_prompt') {
    header('Content-Type: application/json; charset=utf-8');
    global $row_login_atual;
    @session_start();
    $id_criador = isset($row_login_atual['id']) ? intval($row_login_atual['id']) : null;

    try {
        $db = get_mysql_connection_local();
        if (!$db) throw new Exception("Falha na conexão com banco de dados");
        $db->set_charset("utf8mb4");

        // System prompt (somente se tiver permissão)
        $system_prompt = null;
        if ($id_criador && verifica_permissao($mysqli, $id_criador, 'chatgpt_system_prompt', 'editar')) {
            $r = $db->query("SELECT conteudo FROM chatgpt_prompts WHERE tipo='system' AND id_criador='default' LIMIT 1");
            if ($r && $row_sp = $r->fetch_assoc()) $system_prompt = $row_sp['conteudo'];
        }

        // User prompt do usuário logado
        $user_prompt = null;
        if ($id_criador) {
            $r = $db->query("SELECT conteudo FROM chatgpt_prompts WHERE tipo='user' AND id_criador=$id_criador LIMIT 1");
            if ($r && $row_up = $r->fetch_assoc()) $user_prompt = $row_up['conteudo'];
        }

        echo json_encode(['success' => true, 'system_prompt' => $system_prompt, 'user_prompt' => $user_prompt]);
    } catch (Exception $e) {
        echo json_encode(['success' => false, 'error' => $e->getMessage()]);
    }
    exit;
}

// -----------------------------------------------------
// HANDLER: SALVAR PROMPTS NO BANCO (MYSQL)
// -----------------------------------------------------
if (isset($_GET['action']) && $_GET['action'] === 'save_prompt') {
    header('Content-Type: application/json; charset=utf-8');
    $body = json_decode(file_get_contents('php://input'), true);
    $tipo    = $body['tipo']    ?? '';
    $conteudo = trim($body['conteudo'] ?? '');

    global $row_login_atual;
    @session_start();
    $id_criador = isset($row_login_atual['id']) ? intval($row_login_atual['id']) : null;

    if (!$id_criador) { echo json_encode(['success' => false, 'error' => 'Não autenticado']); exit; }
    if (!in_array($tipo, ['system', 'user'])) { echo json_encode(['success' => false, 'error' => 'Tipo inválido']); exit; }

    // system prompt: exige permissão; id_criador fica NULL (registro global)
    if ($tipo === 'system') {
        if (!verifica_permissao($mysqli, $id_criador, 'chatgpt_system_prompt', 'editar')) {
            http_response_code(403);
            echo json_encode(['success' => false, 'error' => 'Sem permissão']);
            exit;
        }
        $id_criador_sql = "'default'";
    } else {
        $id_criador_sql = "'$id_criador'";
    }

    try {
        $db = get_mysql_connection_local();
        if (!$db) throw new Exception("Falha na conexão");
        $db->set_charset("utf8mb4");
        $conteudo_esc = $db->real_escape_string($conteudo);

        // id_criador é agora VARCHAR(10): 'default' para system prompt, string numérica para user prompt.
        // ON DUPLICATE KEY funciona normalmente pois não há mais NULL na UNIQUE KEY (tipo, id_criador).
        $db->query("INSERT INTO chatgpt_prompts (tipo, id_criador, conteudo)
                    VALUES ('$tipo', $id_criador_sql, '$conteudo_esc')
                    ON DUPLICATE KEY UPDATE conteudo='$conteudo_esc', datetime_atualizacao=NOW()");

        echo json_encode(['success' => true]);
    } catch (Exception $e) {
        echo json_encode(['success' => false, 'error' => $e->getMessage()]);
    }
    exit;
}

// -----------------------------------------------------
// HANDLER: PESQUISA WEB VIA BROWSER.PY (Google)
// -----------------------------------------------------
if (isset($_GET['action']) && $_GET['action'] === 'web_search') {
    header('Content-Type: application/json; charset=utf-8');

    $body    = json_decode(file_get_contents('php://input'), true);
    $queries = $body['queries'] ?? [];

    if (empty($queries) || !is_array($queries)) {
        echo json_encode(['success' => false, 'error' => 'queries array ausente']); exit;
    }

    global $ollama_manual_ip, $CHATGPT_VIA_API_KEY;
    $ip_final = '';
    if (!empty($ollama_manual_ip)) {
        $ip_final = $ollama_manual_ip;
    } else {
        $url_monitor = 'http://conexaovida.org/no-ip-dynamic_ip.php?port=3003';
        $ch = curl_init($url_monitor);
        curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
        curl_setopt($ch, CURLOPT_FOLLOWLOCATION, true);
        curl_setopt($ch, CURLOPT_HEADER, true);
        curl_setopt($ch, CURLOPT_TIMEOUT, 10);
        $raw_response = curl_exec($ch);
        $effective_url = curl_getinfo($ch, CURLINFO_EFFECTIVE_URL);
        curl_close($ch);
        $ip_found = null;
        if (preg_match('/(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/', $effective_url, $matches)) { $ip_found = $matches[1]; }
        else if (preg_match('/(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/', $raw_response ?? '', $matches)) { $ip_found = $matches[1]; }
        if ($ip_found && filter_var($ip_found, FILTER_VALIDATE_IP)) { $ip_final = 'http://' . $ip_found . ':3003'; }
    }

    if (empty($ip_final) || !filter_var($ip_final, FILTER_VALIDATE_URL)) {
        echo json_encode(['success' => false, 'error' => 'Não foi possível detectar IP do servidor.']); exit;
    }

    $ch = curl_init(rtrim($ip_final, '/') . '/api/web_search');
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    curl_setopt($ch, CURLOPT_POST, true);
    curl_setopt($ch, CURLOPT_SSL_VERIFYPEER, false);
    curl_setopt($ch, CURLOPT_TIMEOUT, 120);
    curl_setopt($ch, CURLOPT_HTTPHEADER, [
        'Content-Type: application/json',
        'Authorization: Bearer ' . $CHATGPT_VIA_API_KEY
    ]);
    curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode(['queries' => $queries]));
    $response  = curl_exec($ch);
    $http_code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $curl_err  = curl_error($ch);
    curl_close($ch);

    if ($curl_err || $response === false) {
        echo json_encode(['success' => false, 'error' => $curl_err ?: 'Sem resposta do servidor']); exit;
    }

    $data = json_decode($response, true);
    echo json_encode($data ?: ['success' => false, 'error' => 'Resposta inválida do servidor']);
    exit;
}

// -----------------------------------------------------
// CONFIGURAÇÃO DO SCHEMA DISCOVERY
// -----------------------------------------------------
$db_name_prompt = "desconhecido";
if (isset($config) && isset($config["mysql_db"])) {
    $db_name_prompt = $config["mysql_db"];
} elseif (isset($database_conexao)) {
    $db_name_prompt = $database_conexao;
}

$table_list_str = "Não foi possível listar as tabelas.";
try {
    $con_prompt = get_mysql_connection_local();
    if ($con_prompt) {
        $res_tables = $con_prompt->query("SHOW TABLES");
        $tables_found = [];
        if ($res_tables) {
            while ($row_t = $res_tables->fetch_array()) {
                $tables_found[] = $row_t[0];
            }
            if(count($tables_found) > 0){
                $table_list_str = implode(", ", $tables_found);
            }
        }
    }
} catch (Exception $e) {}

// -----------------------------------------------------
// SYSTEM PROMPT PADRÃO
// -----------------------------------------------------
$default_system_prompt = <<<EOT
####################################################################
### ASSISTENTE CLÍNICO + SQL + PESQUISA WEB + RAG + CDSS V10.0   ###
####################################################################

IDIOMA
Responder sempre em Português do Brasil.

O sistema pertence a uma clínica de neuropediatria associada ao Dr. Hellysson Cavalcanti.

O assistente atua integrado ao prontuário eletrônico da clínica.

FUNÇÕES PRINCIPAIS

1) consultar dados estruturados no banco quando necessário
2) interpretar evoluções clínicas
3) priorizar a tabela de análises estruturadas dos atendimentos
4) usar fallback para o prontuário bruto quando não houver análise estruturada
5) gerar mensagens de acompanhamento pós-consulta
6) utilizar alertas clínicos, casos semelhantes, grafo clínico e embeddings
7) pesquisar informações atualizadas na web quando necessário

O assistente NUNCA deve inventar informações médicas.



####################################################################
### PRINCÍPIO CENTRAL
####################################################################

A LLM atua como assistente clínico integrado ao prontuário eletrônico.

Deve sempre:

• consultar dados estruturados do banco
• priorizar dados clínicos já analisados
• usar a menor quantidade possível de queries
• interpretar evolução clínica bruta apenas quando necessário
• gerar mensagens de acompanhamento
• utilizar apenas informações explicitamente registradas
• pesquisar na web apenas quando precisar de informações externas ou atualizadas

Nunca inventar dados clínicos.
Nunca completar lacunas com inferência.
Segurança clínica sempre vem antes de completude textual.



####################################################################
### REGRA CRÍTICA — SQL MÍNIMO NECESSÁRIO
####################################################################

Sempre gerar a MENOR quantidade possível de queries.

Evitar completamente:

• SHOW TABLES desnecessário
• DESCRIBE desnecessário
• queries exploratórias
• queries repetidas
• consultas redundantes ao prontuário bruto
• consultas separadas quando uma única query resolve

Priorizar consultas diretas nas tabelas clínicas estruturadas.



####################################################################
### SCHEMA CONHECIDO DO SISTEMA
####################################################################

As seguintes tabelas são consideradas conhecidas:

membros
hospitais
clinica_atendimentos
chatgpt_atendimentos_analise
chatgpt_alertas_clinicos
chatgpt_casos_semelhantes
chatgpt_clinical_graph_nodes
chatgpt_clinical_graph_edges
chatgpt_embeddings_prontuario
chatgpt_chats
chatgpt_prompts
chatgpt_sql_logs

Portanto NÃO executar:

SHOW TABLES
DESCRIBE membros
DESCRIBE hospitais
DESCRIBE clinica_atendimentos
DESCRIBE chatgpt_atendimentos_analise
DESCRIBE chatgpt_alertas_clinicos
DESCRIBE chatgpt_casos_semelhantes
DESCRIBE chatgpt_clinical_graph_nodes
DESCRIBE chatgpt_clinical_graph_edges
DESCRIBE chatgpt_embeddings_prontuario

Essas tabelas já são conhecidas pela LLM.



####################################################################
### TABELA CLÍNICA PRIORITÁRIA (ANÁLISE ESTRUTURADA)
####################################################################

Tabela principal para interpretação clínica:

chatgpt_atendimentos_analise

Essa tabela contém dados do prontuário já estruturados e analisados
automaticamente pelo sistema.

Sempre que possível utilizar esta tabela em vez de interpretar
o prontuário bruto.

Campos principais disponíveis em chatgpt_atendimentos_analise:

resumo_texto
dados_json

diagnosticos_citados
sinais_nucleares
eventos_comportamentais
pontos_chave
mudancas_relevantes

terapias_referidas
exames_citados
pendencias_clinicas
condutas_no_prontuario

medicacoes_em_uso
medicacoes_iniciadas
medicacoes_suspensas

condutas_especificas_sugeridas
condutas_gerais_sugeridas

seguimento_retorno_estimado
seguimento_observacao
mensagens_acompanhamento

gravidade_clinica
idade_paciente_valor
idade_paciente_unidade
score_risco

alertas_clinicos
casos_semelhantes

grafo_clinico_nodes
grafo_clinico_edges
raciocinio_clinico
dataset_qa

modelo_llm
prompt_version
hash_prontuario



####################################################################
### TABELAS AUXILIARES E COMO USÁ-LAS
####################################################################

chatgpt_alertas_clinicos
→ alertas clínicos persistidos por atendimento e paciente
→ usar para painéis de risco, monitoramento ativo e priorização de retorno

chatgpt_casos_semelhantes
→ relação persistida de similaridade semântica entre atendimentos
→ usar para recuperar top-N casos semelhantes sem recalcular similaridade

chatgpt_clinical_graph_nodes
→ entidades clínicas estruturadas por atendimento
→ usar quando a pergunta envolver sintomas, diagnósticos, medicamentos,
   exames, terapias, pendências ou condutas como entidades do grafo

chatgpt_clinical_graph_edges
→ relações semânticas entre entidades clínicas
→ usar quando a pergunta envolver associação entre diagnóstico e sintoma,
   tratamento, exame, efeito adverso ou evolução

chatgpt_embeddings_prontuario
→ embeddings semânticos do prontuário
→ usar apenas quando a pergunta exigir raciocínio sobre busca semântica,
   atualização de embedding ou auditoria do vetor

chatgpt_chats
→ metadados das conversas do sistema, vinculáveis a paciente,
   atendimento e receita

chatgpt_prompts
→ armazena prompts ativos do sistema
→ NÃO consultar a menos que a pergunta seja sobre prompts ou configuração

chatgpt_sql_logs
→ log técnico de queries SQL executadas
→ NÃO consultar a menos que a pergunta seja sobre auditoria técnica



####################################################################
### RELAÇÃO ENTRE TABELAS
####################################################################

Relacionamento de análise com atendimento:

chatgpt_atendimentos_analise.id_atendimento
=
clinica_atendimentos.id

Relacionamento do paciente:

clinica_atendimentos.id_paciente
=
membros.id

Relacionamento de alertas:

chatgpt_alertas_clinicos.id_atendimento
=
clinica_atendimentos.id

Relacionamento de casos semelhantes:

chatgpt_casos_semelhantes.id_atendimento_origem
=
clinica_atendimentos.id

chatgpt_casos_semelhantes.id_atendimento_destino
=
clinica_atendimentos.id

Relacionamento do grafo clínico:

chatgpt_clinical_graph_nodes.id_atendimento
=
clinica_atendimentos.id

chatgpt_clinical_graph_edges.id_atendimento
=
clinica_atendimentos.id

VIEW unificada disponível (preferir quando possível):

vw_chatgpt_atendimento_unificado
→ JOIN já feito entre clinica_atendimentos e chatgpt_atendimentos_analise

vw_chatgpt_historico_paciente
→ timeline longitudinal do paciente já montada



####################################################################
### PRIORIDADE DE CONSULTA CLÍNICA
####################################################################

Sempre que a pergunta envolver:

• evolução clínica
• diagnóstico
• medicamentos
• terapias
• exames
• pendências
• condutas
• seguimento
• retorno do paciente
• resumo do atendimento
• score de risco
• gravidade clínica
• alertas clínicos
• casos semelhantes

A LLM deve PRIORITARIAMENTE consultar:

1) chatgpt_atendimentos_analise
2) vw_chatgpt_atendimento_unificado
3) vw_chatgpt_historico_paciente
4) chatgpt_alertas_clinicos
5) chatgpt_casos_semelhantes

Evitar consultar diretamente:

clinica_atendimentos.consulta_conteudo

Pois este campo contém:

• HTML
• texto livre
• informações não estruturadas



####################################################################
### FALLBACK AUTOMÁTICO PARA PRONTUÁRIO BRUTO
####################################################################

Alguns atendimentos antigos podem NÃO possuir registro em
chatgpt_atendimentos_analise, ou podem possuir registro incompleto.

Nesses casos, a LLM deve usar fallback para clinica_atendimentos.

ATIVAR FALLBACK quando ocorrer qualquer uma das situações:

• não existe registro correspondente em chatgpt_atendimentos_analise
• status da análise = pendente
• status da análise = erro
• status da análise = cancelado
• resumo_texto está vazio ou NULL e os principais campos clínicos também estão vazios
• a pergunta exige atendimento específico e apenas o prontuário bruto está disponível

Nesses casos, consultar clinica_atendimentos.consulta_conteudo e interpretar
o texto bruto com as regras clínicas abaixo.

Quando houver análise estruturada e prontuário bruto ao mesmo tempo:

• priorizar SEMPRE os dados estruturados
• usar o texto bruto apenas como complemento ou auditoria



####################################################################
### QUANDO GERAR SQL
####################################################################

Gerar SQL apenas quando a pergunta exigir dados do banco.

Exemplos válidos:

• listar atendimentos
• obter evolução clínica
• identificar responsável
• identificar telefone
• identificar medicações registradas
• consultar histórico de paciente
• identificar retornos clínicos
• gerar mensagens de acompanhamento
• buscar casos semelhantes
• listar alertas clínicos
• recuperar timeline do paciente
• verificar existência de análise estruturada
• buscar prontuário bruto em fallback

Não gerar SQL para:

• explicações médicas
• perguntas conceituais
• matemática
• estimativas
• conhecimento geral consolidado

Nestes casos responder diretamente em português.



####################################################################
### PESQUISA WEB (QUANDO NÃO SOUBER A RESPOSTA)
####################################################################

Quando a pergunta exigir informação que NÃO está no banco de dados
e que a LLM NÃO possui com certeza, a LLM deve solicitar pesquisa web.

Exemplos típicos:

• pessoa, serviço, clínica, instituição
• notícia recente
• diretriz recente
• bula ou regulamentação atualizada
• evidência científica específica
• preço, evento, disponibilidade externa

FORMATO OBRIGATÓRIO para solicitar pesquisa web:

{
  "search_queries": [
    {
      "query": "termos de busca no Google",
      "reason": "motivo da pesquisa"
    }
  ]
}

REGRAS:

• Retornar SOMENTE o JSON acima, sem texto antes ou depois
• Máximo de 3 queries por vez
• Queries curtas e objetivas
• Nunca misturar sql_queries e search_queries no mesmo JSON
• Após receber os resultados, responder com base neles
• Ao citar fontes na resposta final, expor a URL explícita da fonte (em link markdown ou URL literal)
• Nunca citar apenas o nome do site sem mostrar o respectivo URL quando ele estiver disponível nos resultados
• Sempre citar as fontes encontradas na resposta final
• Priorizar fontes confiáveis

QUANDO NÃO USAR:

• perguntas sobre dados do banco
• perguntas já respondíveis com o contexto atual
• perguntas conceituais consolidadas que não exigem atualização



####################################################################
### BOAS PRÁTICAS PARA QUERIES DE PESQUISA WEB
####################################################################

Para buscas médicas/científicas:

• Usar termos em inglês para resultados mais abrangentes
• Adicionar site:pubmed.ncbi.nlm.nih.gov para artigos científicos
• Adicionar pediatric ou children para contexto pediátrico
• Incluir systematic review, meta-analysis, guidelines ou consensus quando fizer sentido

Para buscas regulatórias brasileiras:

• Preferir anvisa
• preferir bula profissional
• usar termos em português

Exemplos:

{
 "search_queries":[
   {
     "query":"methylphenidate children adverse effects systematic review site:pubmed.ncbi.nlm.nih.gov",
     "reason":"buscar revisão sistemática sobre efeitos adversos do metilfenidato em crianças"
   }
 ]
}

{
 "search_queries":[
   {
     "query":"risperidone autism pediatric dosing guidelines",
     "reason":"verificar posologia da risperidona para autismo em crianças"
   },
   {
     "query":"risperidone metabolic side effects children monitoring",
     "reason":"verificar efeitos metabólicos e monitoramento necessário"
   }
 ]
}

{
 "search_queries":[
   {
     "query":"clonidina bula profissional anvisa posologia pediátrica",
     "reason":"verificar posologia pediátrica da clonidina na bula aprovada pela ANVISA"
   }
 ]
}



####################################################################
### REGRA — SQL E PESQUISA WEB NÃO SE MISTURAM
####################################################################

Nunca enviar sql_queries e search_queries no mesmo JSON.

Se a resposta exigir banco de dados:
→ usar somente sql_queries

Se a resposta exigir informação externa atualizada:
→ usar somente search_queries

Se exigir ambos:
→ primeiro resolver SQL
→ depois, se ainda necessário, pesquisar na web



####################################################################
### COMANDOS SQL PERMITIDOS
####################################################################

O sistema aceita apenas:

SELECT
SHOW
DESCRIBE
EXPLAIN

Nunca enviar:

UPDATE
DELETE
INSERT
ALTER
DROP



####################################################################
### VALIDAÇÃO OBRIGATÓRIA DA QUERY
####################################################################

Antes de enviar SQL validar:

1) Query está completa
2) Não possui "..."
3) Não possui placeholders
4) Utiliza apenas colunas existentes
5) Utiliza apenas comandos permitidos
6) Possui FROM válido
7) Possui JOIN correto quando necessário
8) Não contém múltiplas instruções
9) Não depende de tabela desconhecida

Se qualquer item falhar → regenerar a query.



####################################################################
### VARIÁVEIS DE CONTEXTO DO SISTEMA
####################################################################

Quando fornecidas pelo ambiente, usar:

id_profissional_atual
→ profissional logado

id_criador
→ profissional que criou o documento

Regra:

Se id_profissional_atual existir
→ utilizar diretamente na consulta

Evitar buscar profissional por nome.



####################################################################
### PADRÕES DE CONSULTA OTIMIZADA
####################################################################

Consulta recomendada para dados clínicos estruturados:

SELECT
ca.id,
ca.datetime_consulta_inicio,
m.nome,
m.mae_nome,
COALESCE(m.telefone1,m.telefone2,m.telefone1pais,m.telefone2pais) AS telefone,
caa.resumo_texto,
caa.diagnosticos_citados,
caa.medicacoes_em_uso,
caa.terapias_referidas,
caa.seguimento_retorno_estimado,
caa.alertas_clinicos,
caa.score_risco
FROM clinica_atendimentos ca
JOIN membros m
ON m.id = ca.id_paciente
LEFT JOIN chatgpt_atendimentos_analise caa
ON caa.id_atendimento = ca.id
WHERE ca.id_criador = ID_PROFISSIONAL
ORDER BY ca.datetime_consulta_inicio DESC;

Consulta recomendada para um atendimento específico com fallback:

SELECT
ca.id,
ca.id_paciente,
ca.datetime_consulta_inicio,
ca.consulta_conteudo,
caa.status,
caa.resumo_texto,
caa.diagnosticos_citados,
caa.sinais_nucleares,
caa.eventos_comportamentais,
caa.medicacoes_em_uso,
caa.medicacoes_iniciadas,
caa.medicacoes_suspensas,
caa.terapias_referidas,
caa.exames_citados,
caa.pendencias_clinicas,
caa.condutas_no_prontuario,
caa.seguimento_retorno_estimado,
caa.mensagens_acompanhamento,
caa.gravidade_clinica,
caa.idade_paciente_valor,
caa.idade_paciente_unidade,
caa.score_risco,
caa.alertas_clinicos,
caa.casos_semelhantes
FROM clinica_atendimentos ca
LEFT JOIN chatgpt_atendimentos_analise caa
ON caa.id_atendimento = ca.id
WHERE ca.id = ID_ATENDIMENTO
LIMIT 1;

Consulta recomendada para alertas ativos:

SELECT
id,
id_atendimento,
id_paciente,
alerta_tipo,
alerta_descricao,
nivel_risco,
origem_alerta,
datetime_detectado
FROM chatgpt_alertas_clinicos
WHERE resolvido = 0
ORDER BY
CASE nivel_risco
  WHEN 'alto' THEN 1
  WHEN 'moderado' THEN 2
  WHEN 'baixo' THEN 3
  ELSE 4
END,
datetime_detectado DESC;

Consulta recomendada para casos semelhantes:

SELECT
cs.id_atendimento_destino,
cs.id_paciente_destino,
cs.score_similaridade,
cs.ranking_posicao,
a.resumo_texto,
a.diagnosticos_citados,
a.sinais_nucleares,
a.medicacoes_em_uso,
a.terapias_referidas
FROM chatgpt_casos_semelhantes cs
LEFT JOIN chatgpt_atendimentos_analise a
ON a.id_atendimento = cs.id_atendimento_destino
WHERE cs.id_atendimento_origem = ID_ATENDIMENTO
ORDER BY cs.score_similaridade DESC, cs.ranking_posicao ASC
LIMIT 20;



####################################################################
### FORMATO OBRIGATÓRIO DA RESPOSTA SQL
####################################################################

Quando SQL for necessário retornar SOMENTE:

{
 "sql_queries":[
   {
     "query":"SELECT ...",
     "reason":"motivo da consulta"
   }
 ]
}

Nunca escrever texto fora do JSON quando estiver em modo SQL.



####################################################################
### ESTRUTURA DAS EVOLUÇÕES MÉDICAS
####################################################################

Campo bruto:

clinica_atendimentos.consulta_conteudo

Estrutura padrão:

#HD   → hipóteses diagnósticas
#HDA  → história da doença atual
#ATUAL → exame atual
#CD   → conduta

Se for necessário interpretar diretamente a evolução:

1 remover HTML
2 decodificar entidades
3 converter <br> em quebra de linha
4 remover style
5 remover classes
6 manter apenas texto legível

Ao usar fallback com texto bruto:

• extrair o máximo possível do texto
• manter fidelidade literal
• marcar ausência quando algo não estiver descrito
• não inventar campos ausentes



####################################################################
### EXTRAÇÃO DE MEDICAÇÕES
####################################################################

Extrair prioritariamente da seção:

#CD

Também pode reconhecer medicações descritas em:

• EM USO
• MEDICAÇÕES EM USO
• FEZ USO
• CONDUTA
• PRESCRIÇÃO
• ORIENTAÇÕES
• MANTER
• ASSOCIO
• INICIO
• INICIAR
• ELEVO
• AUMENTO
• REDUZO
• SUSPENDO
• RETIRO
• RODO
• TROCO
• MANTIDO
• INTRODUZIDO

Preservar exatamente a posologia descrita.

Nunca normalizar dose por conta própria.
Nunca completar dose ausente.



####################################################################
### INTERPRETAÇÃO DE POSOLOGIA
####################################################################

Formato padrão:

(manhã + tarde + noite)

Exemplos:

(1+0+0) manhã
(0+0+1) noite
(1+0+1) manhã e noite
(1+1+1) manhã tarde noite
(0+0+2) dois comprimidos à noite

Unidades possíveis:

cp
cap
gts
ml

Posologias equivalentes em texto corrido também devem ser preservadas
literalmente, por exemplo:

1cp 12/12h
5mg 1x/dia
0+0+5 à 10gts



####################################################################
### REGRA CRÍTICA — PROIBIDO INFERÊNCIA
####################################################################

A LLM deve sempre:

• usar somente texto explicitamente presente
• nunca deduzir medicamento
• nunca deduzir dose
• nunca deduzir responsável
• nunca completar informação ausente
• nunca assumir CID-10 se não houver base mínima
• nunca assumir exame não mencionado

Se algo não estiver claro:
declarar explicitamente que não foi possível identificar.



####################################################################
### REGRA DE DESTINATÁRIO
####################################################################

Enviar mensagem para RESPONSÁVEL quando:

• paciente menor de idade
• evolução citar responsável
• evolução citar incapacidade
• evolução citar deficiência intelectual grave
• evolução citar ausência de autonomia

Enviar para PACIENTE quando:

• maior de idade
• sem incapacidade descrita

Se responsável não estiver citado:
usar mãe registrada em membros.mae_nome.



####################################################################
### GERAÇÃO DE MENSAGEM DE ACOMPANHAMENTO
####################################################################

Fluxo obrigatório:

1 identificar atendimento e paciente
2 obter análise estruturada do atendimento
3 se não houver análise estruturada, usar fallback no prontuário bruto
4 extrair medicações registradas
5 interpretar posologia
6 identificar destinatário
7 gerar mensagem

OBJETIVOS DA MENSAGEM

A mensagem deve:

• confirmar administração correta da medicação
• investigar evolução clínica
• identificar efeitos adversos
• manter vínculo com a família

TOM DA MENSAGEM

humano
acolhedor
profissional
simples
claro

FORMATO DA RESPOSTA FINAL PARA MENSAGENS

Paciente:

Destinatário:

Telefone:

Mensagem:



####################################################################
### JSON CLÍNICO ESTRUTURADO (QUANDO A TAREFA FOR ANALISAR PRONTUÁRIO)
####################################################################

Quando a tarefa for interpretar uma evolução clínica e produzir análise estruturada,
a resposta deve conter SOMENTE um JSON válido, sem markdown e sem texto antes ou depois.

Todos os campos abaixo devem existir.
Se não houver informação:
• usar [] para listas
• usar null para números/desconhecidos
• usar "" para strings vazias

SCHEMA OBRIGATÓRIO:

{
  "metadata_extracao": {
    "modelo_analise": "prompt_clinico_v10",
    "data_analise": "",
    "confianca_global": null
  },

  "identificacao_paciente": {
    "idade_paciente": {
      "valor": null,
      "unidade": ""
    },
    "sexo_paciente": null
  },

  "diagnosticos_citados": [
    {
      "diagnostico": "",
      "cid10_sugerido": "",
      "status": "",
      "confianca": null
    }
  ],

  "sinais_nucleares": [
    {
      "descricao": "",
      "categoria_clinica": "",
      "intensidade": "",
      "confianca": null
    }
  ],

  "eventos_comportamentais": [
    {
      "evento": "",
      "categoria": "",
      "frequencia": ""
    }
  ],

  "pontos_chave": [],

  "mudancas_relevantes": [
    {
      "descricao": "",
      "contexto": "",
      "periodo": ""
    }
  ],

  "terapias_referidas": [
    {
      "terapia": "",
      "status": "",
      "objetivo": ""
    }
  ],

  "exames_citados": [
    {
      "exame": "",
      "status": "",
      "motivo": ""
    }
  ],

  "pendencias_clinicas": [
    {
      "pendencia": "",
      "prioridade": "",
      "justificativa": ""
    }
  ],

  "condutas_no_prontuario": [],

  "medicacoes_em_uso": [
    {
      "medicacao": "",
      "dose": "",
      "indicacao": ""
    }
  ],

  "medicacoes_iniciadas": [
    {
      "medicacao": "",
      "dose": "",
      "motivo_inicio": ""
    }
  ],

  "medicacoes_suspensas": [
    {
      "medicacao": "",
      "motivo_suspensao": ""
    }
  ],

  "condutas_especificas_sugeridas": [
    {
      "conduta": "",
      "justificativa_clinica": "",
      "nivel_prioridade": ""
    }
  ],

  "condutas_gerais_sugeridas": [],

  "seguimento_retorno_estimado": {
    "intervalo_estimado": "",
    "data_estimada": "",
    "motivo_clinico": "",
    "base_clinica": "",
    "parametros_a_avaliar": [],
    "nivel_prioridade": ""
  },

  "mensagens_acompanhamento": {
    "mensagem_1_semana": "",
    "mensagem_1_mes": "",
    "mensagem_pre_retorno": ""
  },

  "gravidade_clinica": {
    "nivel": "",
    "score_estimado": null,
    "justificativa": ""
  },

  "score_risco": null,

  "alertas_clinicos": [
    {
      "tipo_alerta": "",
      "descricao": "",
      "nivel_risco": ""
    }
  ],

  "casos_semelhantes": [
    {
      "id_atendimento_semelhante": null,
      "score_similaridade": null
    }
  ],

  "grafo_clinico_nodes": [
    {
      "id": "",
      "tipo": "",
      "valor": "",
      "normalizado": "",
      "contexto": ""
    }
  ],

  "grafo_clinico_edges": [
    {
      "node_origem": "",
      "node_destino": "",
      "relacao_tipo": "",
      "contexto": ""
    }
  ],

  "raciocinio_clinico": {
    "hipoteses_consideradas": [],
    "evidencias_utilizadas": [],
    "diagnosticos_descartados": []
  },

  "dataset_qa": [
    {
      "pergunta": "",
      "raciocinio": "",
      "resposta": ""
    }
  ],

  "resumo_texto": ""
}

REGRAS DO JSON ESTRUTURADO

• Nunca gerar campos fora deste schema
• Nunca retornar texto fora do JSON
• Nunca inventar dados ausentes
• Preferir precisão clínica sobre inferência
• Em modo estruturado, o resumo final deve ser compatível com resumo_texto
• Em modo fallback, o mesmo schema deve ser preenchido a partir do prontuário bruto



####################################################################
### MISSÃO DO ASSISTENTE
####################################################################

Auxiliar na análise segura dos dados clínicos do sistema
e gerar mensagens de acompanhamento pós-consulta
para pacientes de neuropediatria.

Sempre utilizar exclusivamente informações registradas
no prontuário, na análise estruturada do atendimento,
ou obtidas via pesquisa web quando necessário.

Nunca inferir dados médicos.

EOT;

$active_system_prompt = $default_system_prompt;
// Carrega prompts do banco (chatgpt_prompts), com fallback para o padrão
if (function_exists('get_mysql_connection_local')) {
    try {
        $_db_p = get_mysql_connection_local();
        if ($_db_p) {
            $_db_p->set_charset("utf8mb4");
            // System prompt (somente se o admin tiver editado)
            $_r = $_db_p->query("SELECT conteudo FROM chatgpt_prompts WHERE tipo='system' AND id_criador='default' LIMIT 1");
            if ($_r && $_row = $_r->fetch_assoc()) {
                if (!empty(trim($_row['conteudo']))) $active_system_prompt = $_row['conteudo'];
            }
            // User prompt do usuário logado
            if (isset($row_login_atual['id']) && !empty($row_login_atual['id'])) {
                $_uid = intval($row_login_atual['id']);
                $_r2 = $_db_p->query("SELECT conteudo FROM chatgpt_prompts WHERE tipo='user' AND id_criador=$_uid LIMIT 1");
                if ($_r2 && $_row2 = $_r2->fetch_assoc()) {
                    if (!empty(trim($_row2['conteudo']))) {
                        $active_system_prompt .= "\n\n[PREFERÊNCIAS DO USUÁRIO]\n" . $_row2['conteudo'];
                    }
                }
            }
        }
    } catch (Exception $_e) { /* silencioso — usa padrão */ }
}

/* * Encapsulamento final do Contexto e Prompt do Sistema
 * Assim, a LLM saberá onde inicia e termina o seu bloco de instruções raiz.
 */
if(isset($active_system_prompt) && !empty($active_system_prompt))
{
    $active_system_prompt = "\n[INICIO_PROMPT_SISTEMA]\n" . $active_system_prompt . "\n[FIM_PROMPT_SISTEMA]\n";
}

// -----------------------------------------------------
// HELPER: EXTRAIR JSON
// -----------------------------------------------------
function extract_json_from_text($text) {
    $text = trim($text);
    $json = json_decode($text, true);
    if (json_last_error() === JSON_ERROR_NONE && isset($json['sql_queries'])) return $json;
    if (preg_match('/```(?:\w+)?\s*(\{.*?"sql_queries".*?\})\s*```/s', $text, $matches)) {
        $candidate = sanitize_json_string($matches[1]);
        $j = json_decode($candidate, true);
        if ($j && isset($j['sql_queries'])) return $j;
    }
    if (preg_match_all('/\{(?:[^{}]|(?R))*\}/s', $text, $matches)) {
        foreach($matches[0] as $candidate) {
            if (strpos($candidate, '"sql_queries"') !== false) {
                $candidateClean = sanitize_json_string($candidate);
                $j = json_decode($candidateClean, true);
                if ($j && isset($j['sql_queries'])) return $j;
            }
        }
    }
    return null;
}

function sanitize_json_string($str) {
    $str = preg_replace('/\n\s*\/\/[^\n]*/', '', $str);
    $str = preg_replace('!/\*.*?\*/!s', '', $str);
    return $str; 
}

// -----------------------------------------------------
// FUNÇÕES AUXILIARES OLLAMA
// -----------------------------------------------------
function send_sse_message($text) {
    echo "data: " . json_encode([
        'id' => 'system-' . time(),
        'object' => 'chat.completion.chunk',
        'choices' => [[ 'delta' => ['content' => $text], 'index' => 0, 'finish_reason' => null ]]
    ]) . "\n\n";
    if (ob_get_length()) ob_flush();
    flush();
}

function send_sse_js_log($label, $data) {
    echo "data: " . json_encode([
        'js_log' => [
            'label' => $label,
            'data' => $data
        ]
    ], JSON_UNESCAPED_UNICODE) . "\n\n";
    if (ob_get_length()) ob_flush();
    flush();
}

function call_ollama($url, $data, $method = 'POST', $stream = false, $apiToken = "") {
    global $CHATGPT_VIA_API_KEY; // Acessa a chave definida.
    if(!isset($apiToken) || empty($apiToken)){$apiToken = $CHATGPT_VIA_API_KEY;}
    if (!filter_var($url, FILTER_VALIDATE_URL)) return json_encode(["error" => "CURL_ERROR", "msg" => "URL Inválida: $url"]);
    $ch = curl_init($url);
    curl_setopt($ch, CURLOPT_FOLLOWLOCATION, true); 
    curl_setopt($ch, CURLOPT_CUSTOMREQUEST, $method);
    curl_setopt($ch, CURLOPT_SSL_VERIFYPEER, false);
    curl_setopt($ch, CURLOPT_TIMEOUT, 0); 
    curl_setopt($ch, CURLOPT_CONNECTTIMEOUT, 10); 
    curl_setopt($ch, CURLOPT_TCP_KEEPALIVE, 1); 
    
    $headers = [ "Content-Type: application/json" ];
    if (!empty($apiToken)) $headers[] = "Authorization: Bearer $apiToken";
    curl_setopt($ch, CURLOPT_HTTPHEADER, $headers);
    if ($stream) {
        curl_setopt($ch, CURLOPT_WRITEFUNCTION, function($curl, $data) {
            echo $data;
            if (ob_get_length()) ob_flush();
            flush();
            return strlen($data);
        });
    } else {
        curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    }
    if (!empty($data)) curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($data));
    $response = curl_exec($ch);
    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $curlError = curl_error($ch);
    curl_close($ch);
    if ($response === false && !$stream) return json_encode([ "error" => "CURL_CONNECTION_FAILED", "msg" => $curlError ? $curlError : "Sem resposta.", "target_url" => $url ]);
    if (!$stream) {
        if ($httpCode >= 400) {
            json_decode($response);
            if (json_last_error() === JSON_ERROR_NONE) return $response;
            return json_encode(["error" => "HTTP_ERROR $httpCode", "details" => strip_tags(substr($response, 0, 200)), "url" => $url]);
        }
        return $response;
    }
    return true;
}

// -----------------------------------------------------
// CARREGAR HISTÓRICO REMOTO (CHAT MODE)
// -----------------------------------------------------
if (isset($_GET['action']) && $_GET['action'] === 'sync_simulator') {
    while (ob_get_level()) ob_end_clean();
    header('Content-Type: application/json; charset=utf-8');
    global $ollama_manual_ip;
    
    $json_input = file_get_contents('php://input');
    $req = json_decode($json_input, true);
    
    $chat_id = $req['chat_id'] ?? '';
    $url = $req['url'] ?? '';
    
    // Identifica o IP base (mesma lógica do proxy principal)

    $ip_final = "";
    if (!empty($ollama_manual_ip)) {
        $ip_final = $ollama_manual_ip;
    } else {
        $url_monitor = "http://conexaovida.org/no-ip-dynamic_ip.php?port=3003";
        $ch = curl_init($url_monitor);
        curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
        curl_setopt($ch, CURLOPT_FOLLOWLOCATION, true); 
        curl_setopt($ch, CURLOPT_HEADER, true);         
        curl_setopt($ch, CURLOPT_TIMEOUT, 200);
        $raw_response = curl_exec($ch);
        $effective_url = curl_getinfo($ch, CURLINFO_EFFECTIVE_URL); 
        curl_close($ch);
        $ip_found = null;
        if (preg_match('/(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/', $effective_url, $matches)) { $ip_found = $matches[1]; } 
        else if (preg_match('/(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/', $raw_response ?? '', $matches)) { $ip_found = $matches[1]; }
        if ($ip_found && filter_var($ip_found, FILTER_VALIDATE_IP)) { $ip_final = "http://" . $ip_found . ":3003"; }
    }
    
    if (empty($ip_final) || !filter_var($ip_final, FILTER_VALIDATE_URL)) {
        header('Content-Type: application/json');
        echo json_encode(["error" => "IP_ERROR", "msg" => "Não foi possível detectar IP."]); exit;
    }

    $ch = curl_init("$ip_final/api/sync");

    $payload = json_encode([
        "api_key" => $GLOBALS['CHATGPT_VIA_API_KEY'],
        "chat_id" => $chat_id,
        "id_membro_solicitante" => ((isset($row_login_atual['id']) && !empty($row_login_atual['id']))?$row_login_atual['id']:null),
        "nome_membro_solicitante" => ((isset($row_login_atual['nome']) && !empty($row_login_atual['nome']))?$row_login_atual['nome']:null),
        "url" => $url
    ]);

    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    curl_setopt($ch, CURLOPT_POST, true);
    curl_setopt($ch, CURLOPT_POSTFIELDS, $payload);
    curl_setopt($ch, CURLOPT_TIMEOUT, 90); 

    curl_setopt($ch, CURLOPT_HTTPHEADER, [
        'Authorization: Bearer ' . $GLOBALS['CHATGPT_VIA_API_KEY'], 
        'Content-Type: application/json'
    ]);

    $response = curl_exec($ch);
    curl_close($ch);

    echo $response ?: json_encode(['error' => 'Sem resposta do servidor de sync']);
    exit;
}

// -----------------------------------------------------
// PROXY HANDLER (CHAT MODE)
// -----------------------------------------------------
if (isset($_GET['action']) && $_GET['action'] === 'proxy') {
    
    ignore_user_abort(true); 
    set_time_limit(0);       
    ini_set('memory_limit', '-1'); 
    ini_set('max_execution_time', 0); 
    ini_set('default_socket_timeout', 7200); 

    while (ob_get_level()) ob_end_clean();
    ob_implicit_flush(true);
    
    global $ollama_manual_ip, $active_system_prompt, $CHATGPT_VIA_API_KEY, $hostname_conexao, $username_conexao, $password_conexao, $database_conexao;
    $ip_final = "";

    if (!empty($ollama_manual_ip)) {
        $ip_final = $ollama_manual_ip;
    } else {
        $url_monitor = "http://conexaovida.org/no-ip-dynamic_ip.php?port=11434";
        $ch = curl_init($url_monitor);
        curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
        curl_setopt($ch, CURLOPT_FOLLOWLOCATION, true); 
        curl_setopt($ch, CURLOPT_HEADER, true);         
        curl_setopt($ch, CURLOPT_TIMEOUT, 5);
        $raw_response = curl_exec($ch);
        $effective_url = curl_getinfo($ch, CURLINFO_EFFECTIVE_URL); 
        curl_close($ch);
        $ip_found = null;
        if (preg_match('/(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/', $effective_url, $matches)) { $ip_found = $matches[1]; } 
        else if (preg_match('/(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/', $raw_response ?? '', $matches)) { $ip_found = $matches[1]; }
        if ($ip_found && filter_var($ip_found, FILTER_VALIDATE_IP)) { $ip_final = "http://" . $ip_found . ":11434"; }
    }
    
    if (empty($ip_final) || !filter_var($ip_final, FILTER_VALIDATE_URL)) {
        header('Content-Type: application/json');
        echo json_encode(["error" => "IP_ERROR", "msg" => "Não foi possível detectar IP."]); exit;
    }
    $ip_final = rtrim($ip_final, '/');
    $json_input = file_get_contents('php://input');
    $req = json_decode($json_input, true);
    $endpoint = $req['endpoint'] ?? '/v1/chat/completions';
    $method = $req['method'] ?? 'POST';
    if (substr($endpoint, 0, 1) !== '/') $endpoint = '/' . $endpoint;
    $requestData = $req['data'] ?? [];
    
    // ====================================================================================
    // ROTA EXCLUSIVA: CHATGPT SIMULATOR (Porta 3003)
    // ====================================================================================
    if(isset($req['data']['model']) && !empty($req['data']['model']) && $req['data']['model'] === 'ChatGPT Simulator') {
        
        $ip_final = str_replace('11434', '3003', $ip_final);
        $url_destino = $ip_final . "/v1/chat/completions"; 
        
        $chat_id = $req['data']['chat_id'] ?? null;
        $url_context = $req['data']['url'] ?? null;
        $stream = $req['data']['stream'] ?? false;
        
        // 🔧 FIX: Busca a última mensagem USER com conteúdo real (ignora assistants vazios)
        $msg_content = '';
        if (isset($req['data']['messages']) && is_array($req['data']['messages'])) {
            foreach (array_reverse($req['data']['messages']) as $m) {
                if (($m['role'] ?? '') === 'user' && !empty(trim($m['content'] ?? ''))) {
                    $msg_content = $m['content'];
                    break;
                }
            }
        }
        
        $payload = [
            "api_key" => $CHATGPT_VIA_API_KEY,
            "message" => $msg_content,
            "chat_id" => $chat_id,
            "id_membro_solicitante" => ((isset($row_login_atual['id']) && !empty($row_login_atual['id']))?$row_login_atual['id']:null),
            "nome_membro_solicitante" => ((isset($row_login_atual['nome']) && !empty($row_login_atual['nome']))?$row_login_atual['nome']:null),
            "url" => $url_context,
            "stream" => $stream,
            "attachments" => $req['data']['attachments'] ?? []
        ];

        // Conexão direta com Timeout de 5 Minutos
        $ch = curl_init($url_destino);
        curl_setopt($ch, CURLOPT_POST, true);
        curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($payload));
        curl_setopt($ch, CURLOPT_HTTPHEADER, [
            'Content-Type: application/json',
            'Authorization: Bearer ' . $CHATGPT_VIA_API_KEY
        ]);
        curl_setopt($ch, CURLOPT_TIMEOUT, 620); // margem sobre os 600s do Python
        
        if ($stream) {
            header('Content-Type: text/event-stream');
            header('Cache-Control: no-cache, no-transform');
            header('X-Accel-Buffering: no');
            
            curl_setopt($ch, CURLOPT_RETURNTRANSFER, false);
            // O WRITEFUNCTION descarrega o buffer em tempo real para o JS, prevenindo o 500.
            curl_setopt($ch, CURLOPT_WRITEFUNCTION, function($curl, $data) {
                echo $data;
                if (ob_get_level() > 0) ob_flush();
                flush();
                return strlen($data);
            });
        } else {
            header('Content-Type: application/json');
            curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
        }

        $response = curl_exec($ch);
        
        if(curl_errno($ch)) {
            http_response_code(500);
            echo json_encode(["error" => "Falha de Conexão ou Timeout: " . curl_error($ch)]);
        } elseif (!$stream) {
            echo $response;
        }
        
        curl_close($ch);
        exit; // INTERROMPE AQUI (Não passa para a lógica de SQL do Ollama)
    }
    // ====================================================================================

    $url_destino = $ip_final . $endpoint;

    // LÓGICA PADRÃO OLLAMA / BD
    if (strpos($endpoint, 'chat/completions') !== false && isset($requestData['messages'])) {
        
        // [FIX 8.6] AUMENTO DE CONTEXTO E PARAMETROS
        if (!isset($requestData['options'])) { $requestData['options'] = []; }
        $requestData['options']['num_ctx'] = 32768; // Suporte a contexto expandido (32k)
        $requestData['options']['temperature'] = 0.1;   
        $requestData['options']['repeat_penalty'] = 1.1; 
        
        if (empty($requestData['messages']) || $requestData['messages'][0]['role'] !== 'system') {
            array_unshift($requestData['messages'], ['role' => 'system', 'content' => $active_system_prompt]);
        }
        
        // [FIX 8.6] JANELA DESLIZANTE INTELIGENTE (Anti-503)
        // Se o payload for muito grande, remove mensagens antigas do meio para não estourar o servidor web.
        // Mantém System Prompt [0] e Última Pergunta [last].
        $totalChars = 0;
        foreach ($requestData['messages'] as $m) { $totalChars += strlen($m['content']); }
        
        $SAFE_LIMIT_CHARS = 35000; // Limite de caracteres de "segurança" para HTTP Request (aprox 8-9k tokens de entrada)
        
        if ($totalChars > $SAFE_LIMIT_CHARS && count($requestData['messages']) > 2) {
            $cleanedMessages = [];
            $cleanedMessages[] = $requestData['messages'][0]; // Mantém System
            
            // Pega a última mensagem (o prompt atual do usuário)
            $lastMsg = array_pop($requestData['messages']);
            
            // Lógica de corte: Mantém apenas as N mensagens mais recentes que cabem
            $tempBuffer = [];
            $currentLen = strlen($cleanedMessages[0]['content']) + strlen($lastMsg['content']);
            
            // Itera de trás para frente no histórico
            $history = array_reverse(array_slice($requestData['messages'], 1)); 
            foreach($history as $histMsg) {
                if (($currentLen + strlen($histMsg['content'])) < $SAFE_LIMIT_CHARS) {
                    array_unshift($tempBuffer, $histMsg);
                    $currentLen += strlen($histMsg['content']);
                } else {
                    break; // Parou de caber
                }
            }
            
            $requestData['messages'] = array_merge($cleanedMessages, $tempBuffer);
            $requestData['messages'][] = $lastMsg;
        }

        $userWantsStream = $req['data']['stream'] ?? false;
        
        $lastUserMsg = "";
        
        foreach(array_reverse($requestData['messages'], true) as $k => $m) {
            if ($m['role'] === 'user') {
                $lastUserMsg = $m['content'];
                if (strpos($lastUserMsg, 'Baseado APENAS no caso clínico') === false) {
                    if (strpos($lastUserMsg, '[DITADO CLINICO]') === false) {
                        $requestData['messages'][$k]['content'] .= "\n\n[INSTRUCAO PRIORITARIA] Baseado APENAS no caso acima, responda a pergunta.";
                    }
                }
                break;
            }
        }

        $pass1Data = $requestData;
        $pass1Data['stream'] = false; 

        $responsePass1 = call_ollama($url_destino, $pass1Data, 'POST', false);
        $jsonResponse = json_decode($responsePass1, true);

        if (!$jsonResponse || !isset($jsonResponse['choices'])) {
            header('Content-Type: application/json'); echo $responsePass1; exit;
        }

        $content = $jsonResponse['choices'][0]['message']['content'] ?? '';
        $sqlRequest = extract_json_from_text($content);

        if ($sqlRequest && isset($sqlRequest['sql_queries']) && is_array($sqlRequest['sql_queries'])) {
            
            if ($userWantsStream) {
                header('Content-Type: text/event-stream');
                header('Cache-Control: no-cache, no-transform');
                header('X-Accel-Buffering: no'); 
                echo ": connection established\n\n"; flush();
                
                send_sse_message("<think>");

                $introText = $content;
                $introText = preg_replace('/```(?:[\w]+)?.*?```/s', '', $introText);
                $introText = preg_replace('/(\{.*?"sql_queries".*?\})/s', '', $introText);
                $introText = trim($introText);

                if (!empty($introText)) {
                    send_sse_message($introText . "\n\n");
                }

                $qtdQueries = count($sqlRequest['sql_queries']);
                send_sse_message("⚙️ Analisando: {$qtdQueries} consulta(s) gerada(s)...\n");
            } else {
                 header('Content-Type: application/json');
            }

            $sqlResults = [];
            
            try { 
                $db = get_mysql_connection_local();
                if (!$db || !$db->ping()) {
                    $db = new mysqli($hostname_conexao, $username_conexao, $password_conexao, $database_conexao);
                }
            } catch (Exception $e) { $sqlResults[] = ["error" => "Falha BD: " . $e->getMessage()]; $db = null; }

            if ($db) {
                foreach ($sqlRequest['sql_queries'] as $index => $q) {
                    $query = trim($q['query']);
                    $reason = $q['reason'] ?? "Sem descrição";
                    
                    if ($userWantsStream) {
                        send_sse_message("\n🔹 [" . ($index+1) . "] " . $reason . "\n");
                        send_sse_message("`" . $query . "`\n");
                    }
                    
                    $forbidden = ['INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER', 'TRUNCATE', 'CREATE', 'REPLACE'];
                    $cmd = strtoupper(strtok($query, ' '));
                    $isSafe = true;
                    foreach($forbidden as $bad) {
                        if (stripos($query, $bad) !== false) {
                            $sqlResults[] = ["query" => $query, "error" => "SEGURANÇA: '$bad' proibido."];
                            $isSafe = false;
                            break;
                        }
                    }
                    
                    if ($isSafe && in_array($cmd, ['SELECT', 'SHOW', 'DESCRIBE', 'EXPLAIN'])) {
                        try {
                            $result = $db->query($query);
                            if ($result) {
                                if ($result instanceof mysqli_result) {
                                    $rows = $result->fetch_all(MYSQLI_ASSOC);
                                    $count = count($rows);
                                    $sqlResults[] = ["query" => $query, "rows_count" => $count, "data" => $rows];
                                    if ($userWantsStream) {
                                        send_sse_message("✅ " . $count . " registro(s).\n");
                                    }
                                } else {
                                    $sqlResults[] = ["query" => $query, "result" => "OK."];
                                }
                            } else {
                                $sqlResults[] = ["query" => $query, "error" => $db->error];
                                if ($userWantsStream) send_sse_message("❌ Erro SQL: " . $db->error . "\n");
                            }
                        } catch (Exception $e) {
                            $sqlResults[] = ["query" => $query, "error" => $e->getMessage()];
                        }
                    }
                }
            }
            
            if ($userWantsStream) {
                send_sse_message("</think>");
            }

            $requestData['messages'][] = ['role' => 'assistant', 'content' => $content];
            
            $finalSystemPrompt = "[RESULTADOS SQL]\n" . json_encode($sqlResults, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE);
            $finalSystemPrompt .= "\n\n[INSTRUCAO FINAL]";
            $finalSystemPrompt .= "\n1. Use os dados SQL.";
            $finalSystemPrompt .= "\n2. IDIOMA: Português do Brasil.";

            $requestData['messages'][] = ['role' => 'system', 'content' => $finalSystemPrompt];

            if ($userWantsStream) {
                send_sse_js_log("📤 PAYLOAD FINAL (CONTEXTO + RESULTADO SQL)", $requestData);
                usleep(50000); 
            }

            $finalResponse = call_ollama($url_destino, $requestData, 'POST', $userWantsStream);
            
            if (!$userWantsStream) {
                header('Content-Type: application/json');
                $finalArr = json_decode($finalResponse, true);
                if (is_array($finalArr)) {
                    $finalArr['debug_sql_data'] = $sqlResults;
                    echo json_encode($finalArr);
                } else {
                    echo $finalResponse; 
                }
            }
            exit;

        } else {
            if ($userWantsStream) {
                header('Content-Type: text/event-stream');
                header('Cache-Control: no-cache, no-transform');
                echo "data: " . json_encode(['id'=>'chatcmpl-cache', 'choices'=>[['delta'=>['role'=>'assistant'], 'index'=>0]]]) . "\n\n";
                echo "data: " . json_encode(['id'=>'chatcmpl-cache', 'choices'=>[['delta'=>['content'=>$content], 'index'=>0]]]) . "\n\n";
                echo "data: [DONE]\n\n";
                flush();
            } else {
                header('Content-Type: application/json'); echo $responsePass1;
            }
            exit;
        }
    } 
    else {
        $wantsStream = $req['stream'] ?? ($req['data']['stream'] ?? false);
        if ($wantsStream) {
            header('Content-Type: text/event-stream');
            header('Cache-Control: no-cache');
            call_ollama($url_destino, $requestData, $method, true);
            exit;
        } else {
            header('Content-Type: application/json');
            $res = call_ollama($url_destino, $requestData, $method, false);
            json_decode($res);
            if (json_last_error() !== JSON_ERROR_NONE) {
                echo json_encode(["error" => "INVALID_JSON", "raw" => substr($res,0,200)]);
            } else {
                echo $res;
            }
            exit;
        }
    }
}
header('Content-Type: application/javascript; charset=utf-8'); 
?>

(function() {
    // ===================== INICIO =====================
    // [FIX 9.7] BUSCAR NOME, IDADE E DADOS COMPLETOS VIA SQL (BACKGROUND)
    // ==========================================
    // 1. PRIMEIRO: Cria a base do contexto (lê a URL e cria os campos vazios/null)
    window.PAGE_CTX = (() => {
        const p = new URLSearchParams(window.location.search);
        const toInt = (v) => v ? parseInt(v, 10) : null;
        
        // Lê id_profissional_criador do TD, com fallback para o usuário logado
        const tdCriador = document.getElementById('profissional_criador');
        const idCriador = tdCriador
            ? toInt(tdCriador.getAttribute('id_profissional_criador'))
            : <?php echo ((isset($row_login_atual['id']) && !empty($row_login_atual['id'])) ? $row_login_atual['id'] : 'null'); ?>;

        return {
            // ==========================================
            // 🔗 CONTEXTO DA PÁGINA E SESSÃO (URL/DOM)
            // ==========================================
            id_profissional_atual:   <?php echo ((isset($row_login_atual['id']) && !empty($row_login_atual['id'])) ? $row_login_atual['id'] : 'null'); ?>,
            id_profissional_criador: idCriador,
            id_paciente:    toInt(p.get('id_paciente')),
            id_membro:      toInt(p.get('id_membro')),
            id_atendimento: toInt(p.get('id_atendimento')),
            id_receita:     toInt(p.get('id_receita')),

            // ==========================================
            // 👤 DADOS DA TABELA "MEMBROS" (Para cache/SQL)
            // ==========================================
            id_hospitais_participa: null,
            id_hospital_atual: null,
            nome: null,
            classificacao: null,
            ultimo_tipo_consulta: null,
            id_profissao_cbo: null,
            nome_carimbo: null,
            registro_conselho: null,
            prontuario: null,
            area: null,
            atendimento_internamento: null,
            codigos_pesquisas_array: null,
            codigo_sus: null,
            usuario: null,
            senha: null,
            facebook_id_user: null,
            google_id_user: null,
            link_lattes: null,
            descricao: null,
            token_usuario_memed: null,
            usuario_laudos_cerpe: null,
            senha_laudos_cerpe: null,
            usuario_sisreg: null,
            senha_sisreg: null,
            cod_estabelecimento_cadsus: null,
            usuario_cadsus: null,
            senha_cadsus: null,
            id_criador: null,
            id_editor: null,
            datetime_cadastro: null,
            datetime_atualizacao: null,
            foto_usar: null,
            data_nascimento: null,
            cpf: null,
            rg: null,
            estadocivil: null,
            sexo: null,
            raca: null,
            profissao: null,
            naturalidade: null,
            endereco: null,
            endereco_bairro: null,
            endereco_cidade: null,
            endereco_estado: null,
            endereco_pais: null,
            endereco_cep: null,
            latitude: null,
            longitude: null,
            falecido: null,
            telefone1: null,
            telefone2: null,
            indicado_por: null,
            email: null,
            mae_nome: null,
            mae_data_nascimento: null,
            mae_profissao: null,
            pai_nome: null,
            pai_data_nascimento: null,
            pai_profissao: null,
            telefone1pais: null,
            telefone2pais: null,
            observacoes: null,
            id_convenio: null,
            convenio_matricula: null,
            convenio_titular: null,
            convenio_validade: null,
            foto: null,
            foto_link: null,
            requisicoes_feitas_conhecidos: null,
            requisicoes_feitas_amigos: null,
            requisicoes_feitas_bons_amigos: null,
            requisicoes_recebidas_conhecidos: null,
            requisicoes_recebidas_amigos: null,
            requisicoes_recebidas_bons_amigos: null,
            conhecidos: null,
            amigos: null,
            bons_amigos: null,
            ultimo_ip: null,
            data_ultima_visita: null,
            hora_ultima_visita: null,
            barra_progresso_tipo: null,
            unidades_unidas: null,
            ver: null,
            incluir: null,
            editar: null,
            excluir: null,
            timestamp_conferido_laudos_cerpe: null,
            atendimento_cabecalho: null,
            certificado_x509: null,
            assinatura_imagem: null,
            link_ultima_pesquisa_chatgpt: null
        };
    })();

    // 2. SEGUNDO: Define a função que vai buscar os dados e preencher o contexto
    // ==========================================
    // BUSCAR DADOS COMPLETOS VIA SQL (BACKGROUND)
    // ==========================================
    async function loadPatientDemographics() {
        console.groupCollapsed(`%c${FILE_PREFIX} 🚦 [SQL Interno] Iniciando loadPatientDemographics...`, 'color: #9c27b0; font-weight: bold');
        try {
            const params = new URLSearchParams(window.location.search);
            const targetId = params.get('id_paciente') || params.get('id_membro');
            
            if (!targetId) return;

            console.log(`🔍 [SQL Interno] Buscando dados do membro ID ${targetId} para o PAGE_CTX...`);
            
            const query = `SELECT * FROM membros WHERE id = ${parseInt(targetId)} LIMIT 1`;
            
            console.log("⏳ [DEBUG SQL] A disparar o Fetch para o backend...");
            
            const res = await fetch("<?php echo $_SERVER['PHP_SELF']; ?>?action=execute_sql", {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ query: query }) // MUDOU PARA SINGULAR E SEM OS COLCHETES!
            });
            
            const rawText = await res.text();
            
            if (!rawText || rawText.trim() === "") {
                throw new Error("O servidor devolveu vazio.");
            }

            const data = JSON.parse(rawText);
            
            if (data && data.success && data.data && data.data.length > 0) {
                const paciente = data.data[0];
                
                if (typeof window.PAGE_CTX !== 'undefined') {
                    for (const key in paciente) {
                        window.PAGE_CTX[key] = paciente[key];
                    }
                    console.log(`🧠 [PAGE_CTX] Contexto global atualizado com os dados do(a) paciente!`);
                }
                
                const dataNasc = paciente.data_nascimento || paciente.nascimento; 
                const nomeStr = paciente.nome;
                
                if (dataNasc && nomeStr && dataNasc !== '0000-00-00') {
                    const birthDate = new Date(dataNasc);
                    const today = new Date();
                    let age = today.getFullYear() - birthDate.getFullYear();
                    const m = today.getMonth() - birthDate.getMonth();
                    if (m < 0 || (m === 0 && today.getDate() < birthDate.getDate())) {
                        age--;
                    }
                    
                    const partesNome = nomeStr.trim().split(' ');
                    const nomeCurto = partesNome.length > 1 ? `${partesNome[0]} ${partesNome[1]}` : partesNome[0];
                    const tituloFinal = `${nomeCurto} (${age} anos)`;
                    
                    const titleEl = document.getElementById('ow-chat-title');
                    if (titleEl) titleEl.innerText = tituloFinal;
                    localStorage.setItem('ow_cached_paciente_' + targetId, tituloFinal);
                }
            } else {
                console.log("⚠️ [DEBUG SQL] O formato falhou:", data);
            }
        } catch (e) {
            console.error("❌ [SQL Interno] ERRO FATAL:", e);
        }
        
        console.groupEnd();
    }

    // 3. TERCEIRO: O Gatilho Automático que chama a função
    document.addEventListener('DOMContentLoaded', () => {
        const params = new URLSearchParams(window.location.search);
        const targetId = params.get('id_paciente') || params.get('id_membro');
        const titleEl = document.getElementById('ow-chat-title');
        
        // Tenta versão cache rápida para a interface
        if (targetId && titleEl) {
            const cached = localStorage.getItem('ow_cached_paciente_' + targetId);
            if (cached) {
                titleEl.innerText = cached;
            }
        }
        
        // Dispara a busca SQL logo após carregar a página
        setTimeout(loadPatientDemographics, 800);
    });

    // ==========================================
    // [FIX 9.7] BUSCAR NOME, IDADE E DADOS COMPLETOS VIA SQL (BACKGROUND)
    // ===================== FIM =====================
    
    
    const DEFAULT_SYS_PROMPT = `<?php echo str_replace('`', '\`', $default_system_prompt); ?>`;

    const PROXY_URL = "<?php echo $_SERVER['PHP_SELF']; ?>?action=proxy";
    const FILE_PREFIX = "[<?php echo $_SERVER['PHP_SELF']; ?>]"; 
    
    // [FIX 7.3] REMOÇÃO DE SEPARADORES (=== / ---) PARA EVITAR CONFUSÃO DO MODELO
    const USER_SEP = "### PERGUNTA ###"; 
    
    const PREFIX = 'chatgpt_integracao_';
    const KEY_MODEL = PREFIX + 'selected_model';
    const KEY_STREAM = PREFIX + 'stream_enabled';
    const KEY_CONTEXT = PREFIX + 'context_prefs'; 
    const KEY_HIST_PREFIX = PREFIX + 'hist_';
    const DIRECT_TOOL_MODEL = 'Execução de search_queries e sql_queries';
    const DIRECT_TOOL_MODEL_LABEL = '🧩 Execução de search_queries e sql_queries';
    const DIRECT_TOOL_INTRO_MD = [
        '## 🧩 Modo direto: Execução de `search_queries` e `sql_queries`',
        '',
        'Neste modo, suas próximas mensagens **não vão para a LLM**.',
        'Elas serão interpretadas diretamente pelo PHP para executar:',
        '',
        '- `sql_queries` via banco de dados',
        '- `search_queries` via pesquisa web',
        '',
        '### Formato correto',
        '',
        '- Envie **um único JSON por mensagem**.',
        '- Use **ou** `sql_queries` **ou** `search_queries`.',
        '- **Nunca misture os dois** no mesmo JSON.',
        '',
        '### Exemplo SQL',
        '',
        '```json',
        '{',
        '  "sql_queries": [',
        '    {',
        '      "query": "SELECT id, nome FROM membros ORDER BY id DESC LIMIT 5",',
        '      "reason": "Listar os últimos membros cadastrados"',
        '    }',
        '  ]',
        '}',
        '```',
        '',
        '### Exemplo search',
        '',
        '```json',
        '{',
        '  "search_queries": [',
        '    {',
        '      "query": "Risperidona autism children site:pubmed.ncbi.nlm.nih.gov",',
        '      "reason": "Buscar evidências em crianças com TEA"',
        '    }',
        '  ]',
        '}',
        '```',
    ].join('\n');

    const URL_ID = btoa(window.location.search); //Captura o GET da URL e usa como um ID/KEY do chat.
    const HISTORYKEY   = KEY_HIST_PREFIX + URL_ID; // ← já existe, não duplicar
    const MAX_RETRIES  = 3;
    
    
    window.PROF_CTX = {}; //Objeto global para os dados do Profissional (inicia vazio)
    
    // Adicionamos os metadados de sessão ao state
    // ═══════════════════════════════════════════════════════════════
    // SESSION MANAGER — fonte de verdade única, sobrevive a reloads
    // ═══════════════════════════════════════════════════════════════
    const KEY_SESSION = PREFIX + '_session';

    const Session = {
        // --- getters com fallback localStorage ---
        get chatId()    { return state.currentChatId  || this._ls().chatId  || null; },
        get chatUrl()   { return state.currentChatUrl || this._ls().url     || null; },
        get question()  { return currentUserQuestion  || localStorage.getItem(PREFIX + '_lastQ') || ''; },

        // --- setters que persistem imediatamente ---
        setQuestion(q) {
            currentUserQuestion = q;
            if (q) localStorage.setItem(PREFIX + '_lastQ', q);
        },
        setChat(id, url, title) {
            if (id)    { state.currentChatId    = id;    }
            if (url)   { state.currentChatUrl   = url;   }
            if (title) { state.currentChatTitle = title; }
            saveLocal();
        },
        clearChat() {
            state.currentChatId = state.currentChatUrl = state.currentChatTitle = null;
            currentUserQuestion = '';
            localStorage.removeItem(PREFIX + '_lastQ');
            saveLocal();
        },

        // --- detecção centralizada de modo ---
        isChatGPT() {
            const id  = this.chatId;
            const url = this.chatUrl;
            return !!(id && url && url.includes('chatgpt.com'));
        },
        effectiveModel() {
            const sel = document.getElementById('ow-model-sel')?.value || '';
            return (sel === 'ChatGPT Simulator' || this.isChatGPT()) ? 'ChatGPT Simulator' : sel;
        },

        // --- helper interno ---
        _ls() {
            try { return JSON.parse(localStorage.getItem(HISTORYKEY) || '{}'); } catch(e) { return {}; }
        }
    };

    function isDirectToolMode() {
        return document.getElementById('ow-model-sel')?.value === DIRECT_TOOL_MODEL;
    }

    function isDirectToolMode() {
        return document.getElementById('ow-model-sel')?.value === DIRECT_TOOL_MODEL;
    }

    function isDirectToolMode() {
        return document.getElementById('ow-model-sel')?.value === DIRECT_TOOL_MODEL;
    }

    let state = { messages: [], currentChatId: null, currentChatTitle: null, currentChatUrl: null };
    let currentUserQuestion = '';
    let currentAbortController = null;
    
    let _currentUserQuestion = ''; // Fonte de verdade da pergunta atual
    
    // Análise clínica pré-carregada em background
    let analiseAtendimentoCtx = null;

    let recognition = null;
    let isRecording = false;
    let hasSpeechMatch = false; 
    let manualStop = false; 

    // [FIX 8.1] PREFIXO UNIFICADO PARA MICROFONE (CONFORME SOLICITADO)
    const MIC_PREFIX = `🎤 ${FILE_PREFIX} [MICROFONE]`;

    const css = `
        div.qtip { z-index: 2147483647 !important; }
        #ow-widget { position: fixed; bottom: 20px; right: 20px; z-index: 99999; font-family: -apple-system, sans-serif; }
        #ow-toggle-btn { width: 60px; height: 60px; background: #212121; border-radius: 50%; color: #fff; border:none; cursor:pointer; font-size:24px; box-shadow:0 4px 15px rgba(0,0,0,0.3); transition: transform 0.2s; }
        #ow-toggle-btn:hover { transform: scale(1.05); }
        #ow-window { position: absolute; bottom: 80px; right: 0; width: 420px; height: 650px; background: #fff; border-radius: 12px; box-shadow: 0 5px 30px rgba(0,0,0,0.15); display: none; flex-direction: column; border: 1px solid #ddd; overflow: hidden; transition: width 0.3s, height 0.3s; z-index: 100000; }
        #ow-window.maximized { width: 90vw !important; height: 90vh !important; bottom: 5vh !important; right: 5vw !important; }
        #ow-backdrop { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background: rgba(0,0,0,0.5); z-index: 99998; display: none; backdrop-filter: blur(2px); }
        #ow-backdrop.active { display: block; }
        #ow-header { padding: 15px; background: #f9f9f9; border-bottom: 1px solid #eee; flex-shrink: 0; }
        
        
        #ow-messages {
            flex: 1;
            min-height: 0;           /* ← crítico: permite que flex-child seja scrollável */
            padding: 20px;
            overflow-y: auto;
            overflow-x: hidden;
            display: flex;
            flex-direction: column;
            gap: 15px;
            scroll-behavior: smooth;
            /* padding-bottom removido — não é mais necessário */
        }

        #ow-input-area {
            flex-shrink: 0;          /* ← ocupa espaço real no flex, não sobrepõe */
            padding: 15px;
            border-top: 1px solid #eee;
            display: flex;
            gap: 10px;
            align-items: flex-end;
            background: #fff;
            box-sizing: border-box;
            z-index: 10;
            /* position/bottom/left/width removidos */
        }
        #ow-input { flex: 1; padding: 12px; border: 1px solid #ddd; border-radius: 8px; resize: none; height: 45px; font-size: 14px; outline: none; }
        #ow-send { background: #212121; color: #fff; border: none; padding: 0 20px; border-radius: 8px; cursor: pointer; height: 45px; font-weight: 600; }
        #ow-send.stop-mode { background: #d32f2f; }
        
        #ow-mic { width: 45px; height: 45px; background: #f0f0f0; border: 1px solid #ccc; border-radius: 8px; cursor: pointer; font-size: 20px; display: flex; align-items: center; justify-content: center; transition: all 0.2s; }
        #ow-mic.recording { background: #ffebee; border-color: #ef5350; color: #d32f2f; animation: pulseRed 1.5s infinite; }
        
        /* === ANÁLISE PRÉVIA DA LLM EXPOSTA NO CHAT — DESIGN SYSTEM = INICIO === */
        #ow-analise-previa{font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;border:1px solid #e2e8f0;border-radius:14px;background:#fff;box-shadow:0 8px 24px rgba(0,0,0,0.08);margin:10px 0;overflow:visible;width:100%;box-sizing:border-box}
        #ow-analise-previa .ia-header{display:flex;align-items:flex-start;justify-content:space-between;padding:14px 16px;border-bottom:1px solid #e2e8f0;background:#f8fafc;gap:8px;flex-wrap:wrap}
        #ow-analise-previa .ia-title{font-weight:700;font-size:16px;color:#0f172a}
        #ow-analise-previa .ia-actions{display:flex;gap:6px;flex-wrap:wrap}
        #ow-analise-previa .ia-btn{font-size:13px;padding:5px 8px;border:1px solid #cbd5e1;border-radius:6px;background:#fff;cursor:pointer}
        #ow-analise-previa .ia-btn:hover{background:#f1f5f9}
        #ow-analise-previa .ia-resumo{padding:14px 16px;background:#f8fafc;border-bottom:1px solid #e2e8f0}
        #ow-analise-previa .ia-resumo-text{font-size:14px;line-height:1.6;color:#1e293b}
        #ow-analise-previa .ia-tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
        #ow-analise-previa .ia-tag{background:#e2e8f0;border-radius:999px;font-size:12px;padding:3px 8px;font-weight:600}
        #ow-analise-previa .ia-section{padding:12px 16px;border-bottom:1px solid #e2e8f0}
        #ow-analise-previa .ia-section-title{font-size:14px;font-weight:700;margin-bottom:8px;color:#0f172a}
        #ow-analise-previa .ia-section-sub{font-size:12px;font-weight:800;color:#64748b;text-transform:uppercase;letter-spacing:.04em;margin:8px 0 5px}
        #ow-analise-previa .ia-list{padding-left:16px;margin:0}
        #ow-analise-previa .ia-list li{font-size:13px;margin-bottom:6px;color:#334155}
        #ow-analise-previa .ia-conduta{border:1px solid #e2e8f0;border-radius:8px;margin-bottom:8px;background:#fff}
        #ow-analise-previa .ia-conduta-header{width:100%;border:0;background:none;text-align:left;padding:10px 12px;cursor:pointer;display:flex;justify-content:space-between;align-items:center;font-size:13px;font-weight:600}
        #ow-analise-previa .ia-toggle{font-size:14px;color:#64748b;transition:transform .2s;flex-shrink:0}
        #ow-analise-previa .ia-conduta.is-open .ia-toggle{transform:rotate(180deg)}
        #ow-analise-previa .ia-conduta-body{display:none;padding:10px 12px;border-top:1px dashed #e2e8f0;font-size:13px;color:#334155}
        #ow-analise-previa .ia-conduta.is-open .ia-conduta-body{display:block}
        #ow-analise-previa .ia-ref{margin-top:6px;font-size:12px;color:#64748b;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
        #ow-analise-previa .ia-mini-btn{font-size:12px;padding:3px 6px;border:1px solid #cbd5e1;border-radius:5px;background:#fff;cursor:pointer}
        #ow-analise-previa .ia-mini-btn:hover{background:#f1f5f9}
        #ow-analise-previa .ia-timeline{padding:10px 16px}
        #ow-analise-previa .ia-time-item{margin-bottom:8px}
        #ow-analise-previa .ia-time-date{font-size:12px;font-weight:700;color:#2563eb}
        #ow-analise-previa .ia-time-title{font-size:13px;font-weight:600}
        #ow-analise-previa .ia-time-text{font-size:12px;color:#475569}
        #ow-analise-previa .ia-seguimento{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:10px 13px;margin-top:6px}
        #ow-analise-previa .ia-seguimento-header{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:5px}
        #ow-analise-previa .ia-seguimento-data{font-size:13px;font-weight:800;color:#14532d}
        #ow-analise-previa .ia-seguimento-intervalo{font-size:12px;color:#166534;font-weight:600}
        #ow-analise-previa .ia-seguimento-motivo{font-size:12px;color:#14532d;margin-top:3px}
        #ow-analise-previa .ia-seguimento-base{font-size:11px;color:#166534;font-style:italic;margin-top:3px}
        #ow-analise-previa .ia-seguimento-params{font-size:12px;color:#14532d;margin-top:5px}
        #ow-analise-previa .ia-seguimento-params ul{margin:3px 0 0 0;padding-left:16px}
        #ow-analise-previa .ia-seguimento-params li{margin-bottom:3px}
        #ow-analise-previa .ia-prio{font-size:11px;font-weight:700;padding:2px 9px;border-radius:999px;color:#fff}
        #iap-toast{position:fixed;bottom:20px;right:20px;background:#0f172a;color:#fff;padding:8px 12px;border-radius:8px;font-size:13px;opacity:0;transform:translateY(10px);transition:0.2s;z-index:9999}
        #iap-toast.show{opacity:1;transform:translateY(0)}
        /* === ANÁLISE PRÉVIA DA LLM EXPOSTA NO CHAT — DESIGN SYSTEM = FIM === */
        
        @keyframes pulseRed { 0% { box-shadow: 0 0 0 0 rgba(211, 47, 47, 0.4); } 70% { box-shadow: 0 0 0 10px rgba(211, 47, 47, 0); } 100% { box-shadow: 0 0 0 0 rgba(211, 47, 47, 0); } }

        .thinking-wrapper { margin-bottom: 12px; font-size: 13px; border-left: 3px solid #ccc; padding-left: 12px; background: #fafafa; }
        .thinking-header { cursor: pointer; color: #777; font-style: italic; padding: 5px 0; display: flex; align-items: center; gap: 6px; }
        .thinking-content { display: none; color: #666; white-space: pre-wrap; padding-bottom: 8px; }
        .thinking-content.open { display: block; }
        .msg { max-width: 85%; padding: 10px 14px; border-radius: 12px; font-size: 14px; line-height: 1.5; word-wrap: break-word; box-sizing: border-box; }
        .msg-user { background: #e8f0fe; color: #1a1a1a; align-self: flex-end; border-bottom-right-radius: 2px; }

        /* ── Wrapper de linha da mensagem do usuário ── */
        .msg-user-row {
            display: flex;
            align-items: flex-end;
            justify-content: flex-end;
            gap: 6px;
            width: 100%;
        }
        /* Botão copiar — invisível até hover na linha */
        .msg-user-row .ow-user-copy-btn {
            opacity: 0;
            transition: opacity .15s;
            flex-shrink: 0;
            background: none;
            border: none;
            cursor: pointer;
            color: #5f6368;
            padding: 5px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            margin-bottom: 2px;
        }
        .msg-user-row:hover .ow-user-copy-btn { opacity: 1; }
        .msg-user-row .ow-user-copy-btn:hover { background: rgba(0,0,0,0.07); }
        /* Colapso de mensagens longas */
        .msg-user.ow-collapsed {
            max-height: 96px;
            overflow: hidden;
            -webkit-mask-image: linear-gradient(to bottom, black 55%, transparent 100%);
            mask-image: linear-gradient(to bottom, black 55%, transparent 100%);
        }
        .ow-user-expand-btn {
            align-self: flex-end;
            background: none;
            border: none;
            cursor: pointer;
            font-size: 12px;
            color: #1a73e8;
            padding: 0 4px 2px;
            margin-right: 2px;
            display: flex;
            align-items: center;
            gap: 3px;
        }
        .ow-user-expand-btn:hover { text-decoration: underline; }
        .msg-ai h1,.msg-ai h2,.msg-ai h3,.msg-ai h4,.msg-ai h5,.msg-ai h6{margin:6px 0 2px 0;font-weight:bold;line-height:1.3;color:#111}
        .msg-ai h1{font-size:1.4em}.msg-ai h2{font-size:1.25em}.msg-ai h3{font-size:1.1em}
        .msg-ai h4,.msg-ai h5,.msg-ai h6{font-size:1em}
        .msg-ai ul,.msg-ai ol{margin:4px 0;padding-left:20px}
        .msg-ai li{margin:2px 0}
        .msg-ai hr{border:none;border-top:1px solid #ccc;margin:8px 0}
        .msg-ai em{font-style:italic}
        
        
        pre {
            background: #f8f9fa;
            color: #202124;
            padding: 16px;
            border-radius: 0 0 8px 8px;
            overflow-x: hidden;
            border: 1px solid #e0e0e0;
            border-top: none;
            margin: 0;
            white-space: pre-wrap;
            word-break: break-word;
            overflow-wrap: break-word;
            font-size: 13px;
            line-height: 1.6;
            max-width: 100%;
            box-sizing: border-box;
        }
        .ow-code-wrapper {
            border-radius: 8px;
            overflow: visible;  /* NÃO hidden — não quebra position:sticky do filho */
            margin: 8px 0;
        }
        code {
            font-family: 'SFMono-Regular', Consolas, Menlo, monospace;
            background: #f1f3f4;
            color: #c2185b;
            padding: 2px 5px;
            border-radius: 4px;
            font-size: 12px;
        }
        pre code {
            background: none;
            padding: 0;
            color: #202124;
            font-size: 13px;
        }
        strong { font-weight: bold; }
        
        
        
        .cursor-blink::after { content: '▋'; animation: blink 1s step-start infinite; color: #888; }
        @keyframes blink { 50% { opacity: 0; } }
        .ctx-pill { display: flex; align-items: center; gap: 4px; background: #fff; border: 1px solid #ccc; padding: 2px 8px; border-radius: 12px; cursor: pointer; font-size: 11px; }
        .ow-stream-box { display: flex; align-items: center; gap: 6px; margin-top: 8px; font-size: 11px; color: #666; }
        #ow-sidebar { position: absolute; top: 0; left: 0; width: 0; height: 100%; background: #ffffff; z-index: 100; transition: width 0.3s; overflow: hidden; color: #333; box-shadow: 2px 0 5px rgba(0,0,0,0.1); border-right: 1px solid #eee; }
        #ow-sidebar.open { width: 90%; }
        .sb-content { padding: 20px; width: 100%; box-sizing: border-box; display: flex; flex-direction: column; height: 100%; }
        .sb-title { font-size: 16px; font-weight: bold; margin-bottom: 15px; border-bottom: 1px solid #eee; padding-bottom: 10px; display: flex; justify-content: space-between; align-items: center; }
        .sb-input { width: 100%; padding: 10px; margin-bottom: 10px; border-radius: 4px; border: 1px solid #ddd; background: #f9f9f9; color: #333; box-sizing: border-box;}
        .sb-textarea { width: 100%; padding: 10px; margin-bottom: 10px; border-radius: 4px; border: 1px solid #ddd; background: #f9f9f9; color: #333; box-sizing: border-box; min-height: 100px; resize: vertical; font-family: monospace; font-size: 12px; }
        .sb-btn { width: 100%; padding: 10px; background: #212121; color: white; border: none; border-radius: 4px; cursor: pointer; font-weight: bold; margin-bottom: 10px; }
        .sb-btn:disabled { background: #aaa; cursor: not-allowed; }
        .sb-btn-sec { background: #f0f0f0; color: #333; border: 1px solid #ccc; }
        .sb-btn-sec:hover { background: #e0e0e0; }
        .sb-menu-item { padding: 15px; border-bottom: 1px solid #eee; cursor: pointer; display: flex; align-items: center; gap: 10px; transition: background 0.1s; }
        .sb-menu-item:hover { background: #f9f9f9; }
        .sb-progress-wrap { margin-top: 15px; background: #eee; height: 10px; border-radius: 5px; overflow: hidden; display: none; }
        .sb-progress-bar { width: 0%; height: 100%; background: #4caf50; transition: width 0.2s; }
        .sb-status { margin-top: 5px; font-size: 11px; color: #666; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        #ow-menu-toggle { cursor: pointer; font-size: 20px; margin-right: 10px; background: none; border: none; color: #333; }
        .sb-close-btn { background: none; border: none; font-size: 20px; cursor: pointer; color: #666; }
        .sb-view { display: none; animation: fadeIn 0.3s; }
        .sb-view.active { display: block; }
        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
    `;
    const stTag = document.createElement("style"); stTag.innerHTML = css; document.head.appendChild(stTag);

    const widget = document.createElement('div');
    widget.id = 'ow-widget';
    widget.innerHTML = `
        <div id="ow-backdrop"></div>
        <div id="ow-window">
            <div id="ow-sidebar">
                <div id="sb-view-menu" class="sb-view active sb-content">
                    <div class="sb-title">
                        <span>Menu Ollama</span>
                        <button id="sb-btn-close-main" class="sb-close-btn">×</button>
                    </div>
                    <?php if ($user_can_edit_system): ?>
                    <div class="sb-menu-item" onclick="switchSidebarView('install')">
                        <span>📥</span> <span>Instalar Modelos</span>
                    </div>
                    <?php endif; ?>
                    <div class="sb-menu-item" onclick="switchSidebarView('prompts')">
                        <span>✏️</span> <span>Personalizar IA</span>
                    </div>
                </div>

                <div id="sb-view-install" class="sb-view sb-content">
                    <div class="sb-title">
                        <button class="sb-close-btn" onclick="switchSidebarView('menu')">←</button>
                        <span>Instalar Modelo</span>
                        <button id="sb-btn-close-install" class="sb-close-btn">×</button>
                    </div>
                    
                    <div style="background:#fff3cd; color:#856404; padding:8px; border-radius:4px; font-size:11px; margin-bottom:10px; border:1px solid #ffeeba; line-height:1.4;">
                        <strong>⚠️ Requisito GGUF:</strong> O Ollama só aceita modelos convertidos para o formato <b>.GGUF</b>.<br><br>
                        Links oficiais (ex: <i>mistralai/Mistral-7B</i>) costumam falhar. Use versões quantizadas como:<br>
                        - <i>TheBloke/NomeDoModelo-GGUF</i><br>
                        - <i>MaziyarPanahi/NomeDoModelo-GGUF</i>
                    </div>

                    <p style="font-size:12px; color:#666; margin-bottom:5px;">Link do HuggingFace (GGUF):</p>
                    <input type="text" id="sb-model-url" class="sb-input" placeholder="https://huggingface.co/TheBloke/...">
                    
                    <button id="sb-btn-install" class="sb-btn">Baixar e Instalar</button>
                    <div class="sb-progress-wrap" id="sb-prog-wrap">
                        <div class="sb-progress-bar" id="sb-prog-bar"></div>
                    </div>
                    <div class="sb-status" id="sb-status"></div>
                </div>

                <div id="sb-view-prompts" class="sb-view sb-content">
                    <div class="sb-title">
                        <button class="sb-close-btn" onclick="switchSidebarView('menu')">←</button>
                        <span>Prompts</span>
                        <button id="sb-btn-close-prompts" class="sb-close-btn">×</button>
                    </div>
                    
                    <div style="flex:1; overflow-y:auto;">
                        <p style="font-size:12px; font-weight:bold; margin-bottom:5px;">Suas Preferências (User Prompt):</p>
                        <p style="font-size:10px; color:#666; margin-bottom:5px;">Ex: "Responda sempre formalmente", "Seja breve".</p>
                        <textarea id="sb-user-prompt" class="sb-textarea" placeholder="Digite suas instruções aqui..."></textarea>
                        <button id="sb-save-user-prompt" class="sb-btn sb-btn-sec">Salvar Preferências</button>
                        
                        <hr style="border:0; border-top:1px solid #eee; margin:15px 0;">

                        <?php if ($user_can_edit_system): ?>
                        <div id="sb-admin-area">
                            <p style="font-size:12px; font-weight:bold; margin-bottom:5px; color:#d32f2f;">⚙️ Cérebro da IA (System Prompt):</p>
                            <p style="font-size:10px; color:#666; margin-bottom:5px;">Este prompt define a personalidade base e as regras SQL. Cuidado ao editar.</p>
                            <textarea id="sb-system-prompt" class="sb-textarea" style="height:200px;"></textarea>
                            <button id="sb-save-system-prompt" class="sb-btn sb-btn-sec" style="border-color:#d32f2f; color:#d32f2f;">Salvar Prompt do Sistema</button>
                            <button id="sb-reset-system-prompt" class="sb-btn sb-btn-sec" style="font-size:10px;">Restaurar Padrão</button>
                        </div>
                        <?php endif; ?>
                    </div>
                </div>
            </div>

            <div id="ow-header">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                    <div style="display:flex; align-items:center;">
                        <button id="ow-menu-toggle">☰</button>
                        <div style="font-weight:bold; font-size:14px;">IA da Conexão Vida</div>
                    </div>
                    <div id="ow-chat-title" style="font-size:11px; color:#aaa; font-weight:bold; max-width:180px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="REF: ${URL_ID}">REF: ${URL_ID}</div>
                    <div>
                        <button id="ow-btn-new" style="background:none; border:none; cursor:pointer; font-size:18px;" title="Limpar histórico local">🗑️</button>
                        <button id="ow-btn-max" style="background:none; border:none; cursor:pointer; font-size:18px;" title="Expandir/Restaurar">🗖</button>
                        <button id="ow-btn-close" style="background:none; border:none; cursor:pointer; font-size:18px;">×</button>
                    </div>
                </div>
                <select id="ow-model-sel" style="width:100%; padding:4px; font-size:12px; border:1px solid #ddd; border-radius:4px;">
                    <option value="">Inicializando...</option>
                </select>
                <div class="ow-stream-box">
                    <input type="checkbox" id="ow-stream-check">
                    <label for="ow-stream-check">Streaming (Resposta em tempo real)</label>
                </div>
                <div id="ow-context-area" style="display:flex; flex-wrap:wrap; gap:5px; margin-top:8px; border-top:1px solid #eee; padding-top:8px;"></div>
            </div>
            <div id="ow-body" style="flex:1; display:flex; flex-direction:column; position:relative; overflow:hidden;">
                <div id="ow-messages"></div>
                <div id="ow-input-area">
                    <button id="ow-mic" title="Ditado Clínico (Clique para falar)">🎤</button>
                    <textarea id="ow-input" placeholder="Pergunte algo..."></textarea>
                    <button id="ow-send">Enviar</button>
                </div>
            </div>
        </div>
        <button id="ow-toggle-btn">💬</button>
    `;
    

    // --- COOKIE HELPERS ---
    function setCookie(name, value, days) {
        let expires = "";
        if (days) {
            const date = new Date();
            date.setTime(date.getTime() + (days*24*60*60*1000));
            expires = "; expires=" + date.toUTCString();
        }
        document.cookie = name + "=" + (encodeURIComponent(value) || "")  + expires + "; path=/";
    }
    function getCookie(name) {
        const nameEQ = name + "=";
        const ca = document.cookie.split(';');
        for(let i=0;i < ca.length;i++) {
            let c = ca[i];
            while (c.charAt(0)==' ') c = c.substring(1,c.length);
            if (c.indexOf(nameEQ) == 0) return decodeURIComponent(c.substring(nameEQ.length,c.length));
        }
        return null;
    }




    //Função para carregar os dados do Profissional via SQL
    async function loadProfessionalData() {
        try {
            const toInt = (v) => v ? parseInt(v, 10) : null;
            
            // Captura o ID conforme solicitado: do DOM ou fallback do login atual
            const tdCriador = document.getElementById('profissional_criador');
            const idToFetch = tdCriador
                ? toInt(tdCriador.getAttribute('id_profissional_criador'))
                : <?php echo ((isset($row_login_atual['id']) && !empty($row_login_atual['id'])) ? $row_login_atual['id'] : 'null'); ?>;

            if (!idToFetch) return;

            console.log(`🚀 [PROF_SQL] Buscando dados do profissional ID ${idToFetch}...`);
            
            const query = `SELECT * FROM membros WHERE id = ${idToFetch} LIMIT 1`;
            
            const res = await fetch("<?php echo $_SERVER['PHP_SELF']; ?>?action=execute_sql", {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ query: query }) 
            });
            
            const data = await res.json();
            
            if (data && data.success && data.data && data.data.length > 0) {
                window.PROF_CTX = data.data[0];
                console.log("🧠 [PROF_SQL] PROF_CTX atualizado com sucesso!");
            }
        } catch (e) {
            console.error("❌ [PROF_SQL] Erro:", e);
        }
    }

    // No seu DOMContentLoaded, adicione a chamada:
    document.addEventListener('DOMContentLoaded', () => {
        setTimeout(loadProfessionalData, 500); // Carrega o profissional um pouco antes do paciente
    });

    async function initPrompts() {
        try {
            const res = await fetch(`<?php echo $_SERVER['PHP_SELF']; ?>?action=get_prompt`);
            const d   = await res.json();
            if (!d.success) throw new Error(d.error);

            const userEl = document.getElementById('sb-user-prompt');
            if (userEl && d.user_prompt !== null) userEl.value = d.user_prompt;

            <?php if ($user_can_edit_system): ?>
            const sysEl = document.getElementById('sb-system-prompt');
            if (sysEl) sysEl.value = d.system_prompt !== null ? d.system_prompt : DEFAULT_SYS_PROMPT;
            <?php endif; ?>
        } catch(e) {
            console.warn('initPrompts: erro ao carregar do banco:', e);
        }
    }


    function updateTitleUI() {
        const el = document.getElementById('ow-chat-title');
        if (el) {
            el.innerText = state.currentChatTitle ? state.currentChatTitle : `REF: ${URL_ID}`;
            el.title = state.currentChatTitle ? state.currentChatTitle : `REF: ${URL_ID}`;
        }
    }

    // Tornamos a função global anexando-a ao 'window' para permitir debug no Console
    window.buildPageContextBlock = function() {
        // Regra de Omissão (mantida conforme sua solicitação anterior)
        const isOmitted = (k, v) => {
            if (!isNaN(k)) return true; // Omite índices numéricos
            const keyLower = String(k).toLowerCase();
            if (keyLower.includes('usuario') || keyLower.includes('user') || keyLower.includes('senha') || keyLower.includes('token')) return true;
            if (['ver', 'incluir', 'editar', 'excluir', 'acesso'].includes(keyLower)) return true;
            if (keyLower.includes('blob') || keyLower.includes('base64') || keyLower.includes('certificado') || keyLower.includes('assinatura') || keyLower.includes('foto')) return true;
            if (String(v).length > 400 || String(v).includes('-----BEGIN')) return true;
            return false;
        };

        // --- 1. BLOCO DO PROFISSIONAL (vindo do PROF_CTX) ---
        const cleanProf = {};
        let hasProfData = false;
        
        for (const k in window.PROF_CTX) {
            const v = window.PROF_CTX[k];
            if (v !== null && v !== undefined && v !== "" && v !== "0000-00-00") {
                if (!isOmitted(k, v)) {
                    cleanProf[k] = v;
                    hasProfData = true;
                }
            }
        }
        
        const profBlock = hasProfData 
            ? `[DADOS DO PROFISSIONAL]\n${JSON.stringify(cleanProf, null, 2)}` 
            : "";

        // --- 2. BLOCO DO PACIENTE (vindo do PAGE_CTX) ---
        const cleanCtx = {};
        let hasPatientData = false;

        if (window.PAGE_CTX) {
            for (const key in window.PAGE_CTX) {
                const val = window.PAGE_CTX[key];
                if (val !== null && val !== undefined && val !== "" && val !== "0000-00-00") {
                    if (!isOmitted(key, val)) {
                        cleanCtx[key] = val;
                        hasPatientData = true;
                    }
                }
            }
        }

        const patientBlock = hasPatientData 
            ? `[DADOS DO PACIENTE]\n${JSON.stringify(cleanCtx, null, 2)}` 
            : "";

        // --- 3. MONTAGEM FINAL ---
        const parts = [];
        if (profBlock) parts.push(profBlock);
        if (patientBlock) parts.push(patientBlock);

        return parts.length > 0 ? parts.join("\n\n") : null;
    };


    // ===================== INICIO =====================
    // UI DE BOTÕES PARA BLOCOS SQL
    // ==========================================
    function _attachSQLButtons(el, sqlQueries) {
        // Guarda real: só a presença física da barra (sobrevive a innerHTML replacements)
        if (el.querySelector('.ow-sql-actions-bar') || (el.previousElementSibling && el.previousElementSibling.classList.contains('ow-sql-actions-bar'))) return;

        // Cria wrapper externo para isolar overflow do <pre> da barra sticky
        const wrapper = document.createElement('div');
        wrapper.className = el.tagName.toLowerCase() === 'pre' ? 'ow-code-wrapper' : '';
        if (el.tagName.toLowerCase() !== 'pre') {
            wrapper.style.cssText = 'display:block; background:#f8f9fa; border:1px solid #e0e0e0; border-radius:8px; margin-top:10px; overflow:hidden;';
            el.style.fontFamily   = 'monospace';
            el.style.whiteSpace   = 'pre-wrap';
            el.style.padding      = '15px';
            el.style.overflowX    = 'auto';
        }
        el.parentNode.insertBefore(wrapper, el);
        wrapper.appendChild(el);

        const actionBar = document.createElement('div');
        actionBar.className  = 'ow-sql-actions-bar';
        actionBar.style.cssText = `
            position: sticky; top: -20px; z-index: 10;
            height: 38px; background: #f1f3f4;
            border-bottom: 1px solid #e0e0e0;
            border-radius: 8px 8px 0 0;
            display: flex; justify-content: flex-end; align-items: center;
            padding: 0 10px; gap: 8px;
        `;

        // ── Botão COPIAR ──────────────────────────────────────────────────────────
        const btnCopy = document.createElement('button');
        btnCopy.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" style="vertical-align:middle"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg> <span style="font-size:12px;font-weight:600;vertical-align:middle">Copiar SQL</span>`;
        btnCopy.style.cssText = 'background:none;border:none;color:#5f6368;cursor:pointer;padding:4px 8px;border-radius:4px;display:flex;align-items:center;gap:6px;transition:background .2s';
        btnCopy.onmouseover = () => btnCopy.style.background = 'rgba(0,0,0,0.08)';
        btnCopy.onmouseout  = () => btnCopy.style.background = 'none';
        btnCopy.onclick = () => {
            const sql = sqlQueries.map(q => (q.query || q) + ';').join('\n\n');
            navigator.clipboard.writeText(sql).then(() => {
                const orig = btnCopy.innerHTML;
                btnCopy.innerHTML = `<span style="font-size:12px;color:#0d652d;font-weight:bold">✅ Copiado!</span>`;
                setTimeout(() => btnCopy.innerHTML = orig, 2000);
            });
        };

        // ── Botão EXECUTAR ────────────────────────────────────────────────────────
        const execHTML = `<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" style="vertical-align:middle"><path d="M8 5v14l11-7z"/></svg> <span style="font-size:12px;font-weight:600;vertical-align:middle">Executar</span>`;
        const btnExec  = document.createElement('button');
        btnExec.innerHTML     = execHTML;
        btnExec.style.cssText = 'background:rgba(0,188,212,.1);border:1px solid rgba(0,188,212,.4);color:#00838f;cursor:pointer;padding:4px 10px;border-radius:4px;display:flex;align-items:center;gap:4px;transition:all .2s';
        btnExec.onmouseover = () => btnExec.style.background = 'rgba(0,188,212,.2)';
        btnExec.onmouseout  = () => btnExec.style.background = 'rgba(0,188,212,.1)';

        btnExec.onclick = async () => {
            btnExec.disabled = true;
            btnExec.innerHTML = `<span style="font-size:12px;color:#ff9800;font-weight:bold">⏳ Executando...</span>`;

            // UI de resultado
            let resultUI = el.nextElementSibling;
            if (!resultUI || !resultUI.classList.contains('manual-sql-result')) {
                resultUI = document.createElement('div');
                resultUI.className = 'manual-sql-result';
                resultUI.id = 'manual-sql-' + Date.now();
                el.parentNode.insertBefore(resultUI, el.nextSibling);
            }

            // ── Pergunta: Session é a fonte de verdade ──────────────────────
            const lastUserEl = [...document.querySelectorAll('.msg-user')].pop();
            // ─── RECUPERA PERGUNTA ORIGINAL (busca retroativa) ───────────────────────
            // NUNCA usar data.messages — pode já estar com o contexto SQL sobrescrito.
            // A fonte confiável é state.messages (histórico completo original).
            const _findOriginalQuestion = () => {
                // 1. Memória volátil (sessão atual, não recarregou)
                if (currentUserQuestion && currentUserQuestion !== 'Reexecução Manual')
                    return currentUserQuestion;

                // 2. localStorage via Session
                const sq = Session.question;
                if (sq && sq !== 'Reexecução Manual') return sq;

                // 3. state.messages: busca reversa ignorando mensagens de contexto SQL
                const msgs = (typeof state !== 'undefined' ? state.messages : []) || [];
                for (let i = msgs.length - 1; i >= 0; i--) {
                    const m = msgs[i];
                    if (m.role !== 'user') continue;
                    const c = m.content || '';

                    // Mensagem de contexto SQL — tenta extrair o que vem após [FIM_TEXTO_COLADO]
                    if (c.includes('[INICIO_TEXTO_COLADO]')) {
                        const parts = c.split('[FIM_TEXTO_COLADO]');
                        if (parts.length > 1) {
                            const after = parts[parts.length - 1].trim();
                            // Só aceita se for uma pergunta real, não o placeholder
                            if (after && after !== 'Reexecução Manual' && !after.includes('RESULTADOS DAS CONSULTAS SQL')) {
                                return after;
                            }
                        }
                        continue; // era placeholder, tenta mensagens anteriores
                    }

                    // Mensagem normal sem contexto SQL
                    const plain = c.trim();
                    if (plain && !plain.includes('RESULTADOS DAS CONSULTAS SQL')) return plain;
                }

                // 4. Último recurso: DOM (ignora texto de placeholder)
                const userEls = [...document.querySelectorAll('.msg-user')];
                for (let i = userEls.length - 1; i >= 0; i--) {
                    const t = userEls[i].innerText?.trim();
                    if (t && t !== 'Reexecução Manual') return t;
                }

                return '';
            };

            const originalQuestion = _findOriginalQuestion();

            // ⚠️ Só sobrescreve Session se encontrou algo real
            if (originalQuestion && originalQuestion !== 'Reexecução Manual') {
                Session.setQuestion(originalQuestion);
            }

            // ── Executa SQL e monta sqlResultContext ─────────────────────────
            const cleanJson = JSON.stringify({ sql_queries: sqlQueries });
            const fakeUi    = { mID: resultUI.id, tID: null };
            const sqlResultContext = await detectAndExecuteSQL(cleanJson, originalQuestion, '', fakeUi);
            if (!sqlResultContext) {
                btnExec.disabled = false;
                btnExec.innerHTML = execHTML;
                return;
            }

            // ── Detecta modo e restaura state se necessário ──────────────────
            const isChatGPTMode  = Session.isChatGPT();
            const effectiveModel = Session.effectiveModel();
            const useStream      = document.getElementById('ow-stream-check').checked;

            // Restaura state.currentChatId/Url caso tenha vindo null (timing de reload)
            if (isChatGPTMode && !state.currentChatId) {
                state.currentChatId  = Session.chatId;
                state.currentChatUrl = Session.chatUrl;
            }

            // ── Monta payload limpo para a LLM ───────────────────────────────
            const messagesForLLM = [{ role: 'user', content: sqlResultContext }];
            if (!isChatGPTMode) {
                const sysProm = state.messages.find(m => m.role === 'system');
                if (sysProm) messagesForLLM.unshift(sysProm);
            }

            // ── Chama LLM ────────────────────────────────────────────────────
            const uiNew = addAiMarkup();
            currentAbortController = new AbortController();
            let fullC = '';

            try {
                await apiCallStream(PROXY_URL, 'POST', {
                    model:    effectiveModel,
                    messages: messagesForLLM,
                    stream:   useStream,
                    chat_id:  isChatGPTMode ? Session.chatId  : null,
                    url:      isChatGPTMode ? Session.chatUrl : null
                }, chunk => {
                    let c = '';
                    if      (chunk.type === 'markdown' || chunk.type === 'html') { fullC = chunk.content; c = fullC; }
                    else if (chunk.type === 'status') {
                        const tEl = document.getElementById(uiNew.tID);
                        if (tEl) { tEl.parentElement.style.display = 'block'; tEl.innerText = chunk.content; }
                        return;
                    }
                    else if (chunk.choices?.[0]?.delta?.content) { c = chunk.choices[0].delta.content; fullC += c; }
                    else if (chunk.type === 'finish') {
                        const fd = chunk.content;
                        Session.setChat(fd.chat_id, fd.url, null);
                        return;
                    }
                    if (c) {
                        const mEl = document.getElementById(uiNew.mID);
                        if (mEl) mEl.innerHTML = formatMarkdown(fullC);
                        scroll();
                    }
                }, currentAbortController.signal);

                const mEl = document.getElementById(uiNew.mID);
                if (mEl) { mEl.classList.remove('cursor-blink'); mEl.innerHTML = formatMarkdown(fullC); }
                if (fullC) {
                    state.messages.push({ role: 'assistant', content: fullC });
                    saveLocal();
                    saveChatMetaToDatabase();
                }
                if (resultUI?.parentNode) resultUI.remove();

            } catch (err) {
                if (err.name !== 'AbortError') {
                    const mEl = document.getElementById(uiNew.mID);
                    if (mEl) mEl.innerText = 'Erro ao chamar LLM: ' + err.message;
                }
            }

            btnExec.disabled = false;
            btnExec.innerHTML = `<span style="font-size:12px;color:#0d652d;font-weight:bold">✅ Concluído</span>`;
            setTimeout(() => { btnExec.innerHTML = execHTML; }, 3000);
        };

        actionBar.appendChild(btnCopy);
        actionBar.appendChild(btnExec);
        wrapper.insertBefore(actionBar, wrapper.firstChild);
    }

    function injectSQLButtons() {
        const container = document.getElementById('ow-messages') || document.body;

        // [FIX BUG 1] Adiciona 'code' — JSON em fenced blocks fica em <pre><code>
        const elements = container.querySelectorAll('pre, code, p, div, span');

        elements.forEach(el => {
            // [FIX BUG 2] Guarda pela presença FÍSICA da barra, não por classe
            // (classe sobrevive a innerHTML replacement, barra não)
            if (el.querySelector('.ow-sql-actions-bar')) return;

            if (!el.textContent || !el.textContent.includes('"sql_queries"')) return;

            // [FIX BUG 1] Para <code> dentro de <pre>: opera no <pre>
            // (só <pre> tem o contexto CSS necessário para position:absolute)
            let target = el;
            if (el.tagName === 'CODE' && el.parentElement?.tagName === 'PRE') {
                target = el.parentElement;
                if (target.querySelector('.ow-sql-actions-bar')) return;
            }

            // Deepest-element check (evita injetar na div wrapper)
            let isDeepest = true;
            for (const child of el.children) {
                if (child.classList?.contains('ow-sql-actions-bar')) continue;
                if (child.tagName === 'BR') continue; // <br> não conta
                if (child.textContent?.includes('"sql_queries"')) {
                    isDeepest = false;
                    break;
                }
            }
            if (!isDeepest) return;

            // Evita processar containers gigantes (wrappers de página)
            if (el.textContent.length > 5000) return;

            const sqlQueries = extractSQLFromResponse(el.textContent);
            if (sqlQueries && sqlQueries.length > 0) {
                _attachSQLButtons(target, sqlQueries);
            }
        });
    }

    // Observador automático melhorado (Debounce para não pesar na performance)
    function initSQLUIObserver() {
        const chatBox  = document.getElementById('ow-messages') || document.body;

        const observer = new MutationObserver(() => {
            clearTimeout(window._sqlUiTimeout);
            window._sqlUiTimeout = setTimeout(injectSQLButtons, 300);
        });

        observer.observe(chatBox, { childList: true, subtree: true, characterData: true });

        setTimeout(injectSQLButtons, 500);
        setTimeout(injectSQLButtons, 1500);
    }

    document.addEventListener('DOMContentLoaded', initSQLUIObserver);
    
    // ==========================================
    // UI DE BOTÕES PARA BLOCOS SQL
    // ===================== FIM =====================
    
    
    // ===================== INICIO =====================
    // UI DE BOTÃO COPIAR PARA BLOCOS RAW / JSON
    // ==========================================

    function _attachRawCopyButton(el, rawText, label) {
        if (el.querySelector('.ow-raw-actions-bar') || (el.previousElementSibling && el.previousElementSibling.classList.contains('ow-raw-actions-bar'))) return;

        // Cria wrapper externo para isolar overflow do <pre> da barra sticky
        const wrapper = document.createElement('div');
        wrapper.className = el.tagName.toLowerCase() === 'pre' ? 'ow-code-wrapper' : '';
        if (el.tagName.toLowerCase() !== 'pre') {
            wrapper.style.cssText = 'display:block; background:#f8f9fa; border:1px solid #e0e0e0; border-radius:8px; margin-top:10px; overflow:hidden;';
            el.style.fontFamily   = 'monospace';
            el.style.whiteSpace   = 'pre-wrap';
            el.style.padding      = '15px';
            el.style.overflowX    = 'auto';
        }
        el.parentNode.insertBefore(wrapper, el);
        wrapper.appendChild(el);

        const actionBar = document.createElement('div');
        actionBar.className     = 'ow-raw-actions-bar';
        actionBar.style.cssText = `
            position: sticky; top: -20px; z-index: 10;
            height: 36px; background: #f1f3f4;
            border-bottom: 1px solid #e0e0e0;
            border-radius: 8px 8px 0 0;
            display: flex; justify-content: space-between; align-items: center;
            padding: 0 10px;
        `;

        // ── Label do tipo de bloco (ex: "json", "raw") ──────────────────────────
        const typeLabel = document.createElement('span');
        typeLabel.textContent   = label || 'raw';
        typeLabel.style.cssText = `
            font-size: 11px; font-weight: 600; color: #8b949e;
            font-family: monospace; letter-spacing: .5px; text-transform: lowercase;
        `;

        // ── Botão COPIAR ────────────────────────────────────────────────────────
        const btnCopy = document.createElement('button');
        const iconSVG = `<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" style="vertical-align:middle">
            <path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/>
        </svg>`;
        const defaultHTML = `${iconSVG}<span style="font-size:12px;font-weight:600;vertical-align:middle;margin-left:5px">Copiar</span>`;

        btnCopy.innerHTML     = defaultHTML;
        btnCopy.style.cssText = `
            background: none; border: none; color: #5f6368; cursor: pointer;
            padding: 3px 8px; border-radius: 4px;
            display: flex; align-items: center;
            transition: background .2s, color .2s;
        `;
        btnCopy.onmouseover = () => btnCopy.style.background = 'rgba(0,0,0,0.07)';
        btnCopy.onmouseout  = () => btnCopy.style.background = 'none';
        btnCopy.onclick = () => {
            navigator.clipboard.writeText(rawText).then(() => {
                btnCopy.innerHTML = `<span style="font-size:12px;color:#0d652d;font-weight:bold">✅ Copiado!</span>`;
                setTimeout(() => { btnCopy.innerHTML = defaultHTML; }, 2000);
            }).catch(() => {
                btnCopy.innerHTML = `<span style="font-size:12px;color:#c62828;font-weight:bold">❌ Erro</span>`;
                setTimeout(() => { btnCopy.innerHTML = defaultHTML; }, 2000);
            });
        };

        actionBar.appendChild(typeLabel);
        actionBar.appendChild(btnCopy);
        wrapper.insertBefore(actionBar, wrapper.firstChild);
    }

    function injectRawButtons() {
        const container = document.getElementById('ow-messages') || document.body;
        const elements  = container.querySelectorAll('pre, code');

        elements.forEach(el => {
            // Já tem barra SQL ou RAW — ignora
            if (el.querySelector('.ow-sql-actions-bar') || el.querySelector('.ow-raw-actions-bar')) return;

            // Sobe para o <pre> pai quando for <code> dentro de <pre>
            let target = el;
            if (el.tagName === 'CODE' && el.parentElement?.tagName === 'PRE') {
                target = el.parentElement;
                if (target.querySelector('.ow-sql-actions-bar') || target.querySelector('.ow-raw-actions-bar')) return;
            }

            const text = el.textContent?.trim();
            if (!text || text.length < 10 || text.length > 50000) return;

            // Bloco SQL ou pesquisa já tratado pelos respectivos injectButtons — pula
            if (text.includes('"sql_queries"')) return;
            if (text.includes('"search_queries"') || text.includes('"pesquisa_query"')) return;

            // Detecta JSON válido ou bloco raw relevante
            let isJSON  = false;
            let label   = 'raw';
            let content = text;

            try {
                const parsed = JSON.parse(text);
                if (typeof parsed === 'object' && parsed !== null) {
                    isJSON  = true;
                    label   = 'json';
                    // Pretty-print para facilitar leitura ao copiar
                    content = JSON.stringify(parsed, null, 2);
                }
            } catch (_) {
                // Não é JSON — verifica se parece um bloco de código com conteúdo útil
                // (pelo menos 2 linhas ou estrutura chave:valor)
                const lines = text.split('\n').filter(l => l.trim());
                if (lines.length < 2) return;
            }

            // Deepest-element check: evita injetar no wrapper externo
            let isDeepest = true;
            for (const child of el.children) {
                if (child.classList?.contains('ow-raw-actions-bar') ||
                    child.classList?.contains('ow-sql-actions-bar')) continue;
                if (child.tagName === 'BR') continue;
                if (child.textContent?.trim().length > 5) {
                    isDeepest = false;
                    break;
                }
            }
            if (!isDeepest) return;

            _attachRawCopyButton(target, content, label);
        });
    }

    function initRawUIObserver() {
        const chatBox = document.getElementById('ow-messages') || document.body;

        const observer = new MutationObserver(() => {
            clearTimeout(window._rawUiTimeout);
            window._rawUiTimeout = setTimeout(injectRawButtons, 300);
        });

        observer.observe(chatBox, { childList: true, subtree: true, characterData: true });

        // Disparos iniciais para conteúdo já renderizado
        setTimeout(injectRawButtons, 500);
        setTimeout(injectRawButtons, 1500);
    }

    document.addEventListener('DOMContentLoaded', initRawUIObserver);
    
    // ==========================================
    // UI DE BOTÃO COPIAR PARA BLOCOS RAW / JSON
    // ===================== FIM =====================
    
    
    // Helper interno: formatação inline (bold, italic, code)
    function _inlineFmt(text) {
        const ic = [];
        text = text.replace(/`([^`]+)`/g, (_, c) => { ic.push(`<code>${c}</code>`); return `\x00IC${ic.length-1}\x00`; });
        text = text.replace(/\*\*\*([\s\S]*?)\*\*\*/g, '<strong><em>$1</em></strong>');
        text = text.replace(/\*\*([\s\S]*?)\*\*/g, '<strong>$1</strong>');
        text = text.replace(/__([\s\S]*?)__/g, '<strong>$1</strong>');
        text = text.replace(/\*([\s\S]*?)\*/g, '<em>$1</em>');
        text = text.replace(/_([\s\S]*?)_/g, '<em>$1</em>');
        return text.replace(/\x00IC(\d+)\x00/g, (_, i) => ic[i]);
    }

    function formatMarkdown(text) {
        if (!text) return '';

        // ── Prioridade: marked.js (confiável, full CommonMark + GFM) ───────────
        if (typeof marked !== 'undefined') {
            try {
                return marked.parse(text, { breaks: true, gfm: true });
            } catch(e) {
                console.warn('[formatMarkdown] marked.js erro, usando fallback:', e.message);
            }
        }

        // ── Fallback: renderizador interno (quando marked.js ainda não carregou) ─
        let clean = text
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');

        const codeBlocks = [];
        clean = clean.replace(/```(\w*)\r?\n?([\s\S]*?)```/g, (_, lang, code) => {
            const idx = codeBlocks.length;
            codeBlocks.push(`<pre><code class="lang-${lang}">${code.trim()}</code></pre>`);
            return `\x00CODE${idx}\x00`;
        });

        const inlineCodes = [];
        clean = clean.replace(/`([^`]+)`/g, (_, c) => {
            const idx = inlineCodes.length;
            inlineCodes.push(`<code>${c}</code>`);
            return `\x00INLINE${idx}\x00`;
        });

        const lines = clean.split('\n');
        const result = [];
        let inList = false;

        // Formatação inline: **bold**, *italic*, `code`, [link](url)
        function applyInline(text) {
            return text
                .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
                .replace(/\*(.+?)\*/g, '<em>$1</em>')
                .replace(/_(.+?)_/g, '<em>$1</em>')
                .replace(/`([^`]+)`/g, '<code>$1</code>')
                .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
        }

        for (let i = 0; i < lines.length; i++) {
            let line = lines[i];
            if (/^-{3,}$/.test(line)) { if (inList) { result.push('</ul>'); inList = false; } result.push('<hr>'); continue; }
            const hMatch = line.match(/^(#{1,6})\s+(.*)/);
            if (hMatch) { if (inList) { result.push('</ul>'); inList = false; } const level = hMatch[1].length; result.push(`<h${level} style="margin:.4em 0 .2em">${applyInline(hMatch[2])}</h${level}>`); continue; }
            const liMatch = line.match(/^[-*]\s+(.*)/);
            if (liMatch) { if (!inList) { result.push('<ul style="margin:.3em 0 .3em 1.2em;padding:0">'); inList = true; } result.push(`<li>${applyInline(liMatch[1])}</li>`); continue; }
            const olMatch = line.match(/^\d+\.\s+(.*)/);
            if (olMatch) { if (!inList) { result.push('<ol style="margin:.3em 0 .3em 1.2em;padding:0">'); inList = true; } result.push(`<li>${applyInline(olMatch[1])}</li>`); continue; }
            if (inList && line.trim() !== '') { result.push('</ul>'); inList = false; }
            if (/^>/.test(line)) { result.push(`<blockquote style="border-left:3px solid #aaa;margin:.3em 0;padding:.2em .6em;color:#555">${applyInline(line.replace(/^>\s?/, ''))}</blockquote>`); continue; }
            if (line.trim() === '') { if (inList) { result.push('</ul>'); inList = false; } result.push('<br>'); continue; }
            result.push(applyInline(line) + '<br>');
        }
        if (inList) result.push('</ul>');

        let out = result.join('');
        out = out.replace(/\x00CODE(\d+)\x00/g, (_, i) => codeBlocks[i]);
        out = out.replace(/\x00INLINE(\d+)\x00/g, (_, i) => inlineCodes[i]);
        return out;
    }
    
    function normalizeSQL(sql) {
        return sql.replace(/\s+/g, ' ').trim();
    }

    function parseAnalisePreviaRow(row) {
        const jp = (col) => { try { return JSON.parse(row[col] || '[]'); } catch(e) { return []; } };
        return {
            status:             row.status,
            inicio_atendimento: row.datetime_atendimento_inicio || null,
            analisado_em:       row.datetime_analise_concluida,
            resumo_texto:       row.resumo_texto  || '',
            gravidade_clinica:  row.gravidade_clinica || null,
            seguimento_sugerido: {
                retorno_estimado: row.seguimento_retorno_estimado || null,
                observacao:       row.seguimento_observacao       || ''
            },
            diagnosticos_citados:               jp('diagnosticos_citados'),
            pontos_chave:                       jp('pontos_chave'),
            mudancas_relevantes:                jp('mudancas_relevantes'),
            eventos_comportamentais:            jp('eventos_comportamentais'),
            sinais_nucleares:                   jp('sinais_nucleares'),
            terapias_referidas:                 jp('terapias_referidas'),
            exames_citados:                     jp('exames_citados'),
            pendencias_clinicas:                jp('pendencias_clinicas'),
            condutas_registradas_no_prontuario: jp('condutas_no_prontuario'),
            medicacoes_em_uso:                  jp('medicacoes_em_uso'),
            medicacoes_iniciadas:               jp('medicacoes_iniciadas'),
            medicacoes_suspensas:               jp('medicacoes_suspensas'),
            condutas_especificas_sugeridas:     jp('condutas_especificas_sugeridas'),
            condutas_gerais_sugeridas:          jp('condutas_gerais_sugeridas'),
            mensagens_acompanhamento:           (() => {
                const v = row.mensagens_acompanhamento;
                if (!v) return null;
                try { return JSON.parse(v); } catch(e) { return null; }
            })(),
            dados_json: (() => { const v=row.dados_json; if(!v) return null; try{return JSON.parse(v);}catch(e){return null;} })(),
            idade_paciente: (() => {
                try {
                    const dj = row.dados_json ? JSON.parse(row.dados_json) : null;
                    if (dj?.identificacao_paciente?.idade_paciente?.valor!=null) return dj.identificacao_paciente.idade_paciente;
                    if (dj?.identificacao_paciente?.idade!=null) return {valor:dj.identificacao_paciente.idade,unidade:'anos'};
                    if (dj?.idade_paciente?.valor!=null) return dj.idade_paciente;
                } catch(e) {}
                return {valor:null,unidade:null};
            })()
        };
    }

    function applyAnalisePrevia(analise, row, sourceLabel) {
        console.log('%c✅ Carregada com sucesso!', 'color: #4caf50; font-weight: bold');
        console.log('%cOrigem:    ' + sourceLabel,                                   'color: #7b1fa2');
        console.log('%cStatus:    ' + row.status,                                   'color: #4caf50');
        console.log('%cAnalisado: ' + row.datetime_analise_concluida,               'color: #4caf50');
        console.log('%cResumo:    ' + (analise.resumo_texto?.substring(0, 120) ?? ''), 'color: #666');
        console.log(
            `%c${analise.pontos_chave.length} pontos-chave | ` +
            `${analise.condutas_especificas_sugeridas.length} condutas específicas | ` +
            `${analise.condutas_gerais_sugeridas.length} condutas gerais | ` +
            `${analise.medicacoes_em_uso.length} meds em uso`,
            'color: #1565c0'
        );

        analiseAtendimentoCtx = JSON.stringify({ analise_clinica_previa: analise }, null, 2);
        renderAnalisePrevia(true);

        if (!window.__analiseObserver) {
            const owMessages = document.getElementById('ow-messages');
            if (owMessages) {
                window.__analiseObserver = new MutationObserver(() => {
                    if (!analiseAtendimentoCtx) return;
                    if (document.getElementById('ow-analise-previa')) return;
                    window.__analiseObserver.disconnect();
                    renderAnalisePrevia();
                    window.__analiseObserver.observe(owMessages, { childList: true });
                });
                window.__analiseObserver.observe(owMessages, { childList: true });
            }
        }
    }

    function notifyAnalisePreviaPendente(status, scopeLabel = 'atendimento') {
        const statusNorm = String(status || '').trim().toLowerCase();
        let msg = `Análise prévia do ${scopeLabel} ainda não está disponível.`;
        if (statusNorm === 'pendente') {
            msg = `Análise prévia do ${scopeLabel} ainda está pendente de processamento.`;
        } else if (statusNorm === 'processando') {
            msg = `Análise prévia do ${scopeLabel} ainda está sendo processada.`;
        }
        if (typeof window.apToast === 'function') {
            window.apToast(msg);
        }
    }

    async function fetchAnaliseAtendimento(idAtendimento) {
        console.groupCollapsed(`%c${FILE_PREFIX} 🧠 Análise Prévia — id_atendimento=${idAtendimento}`, 'color: #9c27b0; font-weight: bold');

        if (!idAtendimento) {
            console.warn('❌ id nulo/zero — abortando.');
            console.groupEnd();
            return;
        }

        try {
            console.log('%c📡 Buscando via execute_sql...', 'color: #1565c0');

            const res = await fetch("<?php echo $_SERVER['PHP_SELF']; ?>?action=execute_sql", {
                method:  'POST',
                headers: { 'Content-Type': 'application/json; charset=utf-8' },
                body:    JSON.stringify({
                    query: normalizeSQL(`
                        SELECT
                                status,
                                datetime_atendimento_inicio,
                                datetime_analise_concluida,
                                resumo_texto,
                                gravidade_clinica,
                                dados_json,
                                seguimento_retorno_estimado,
                                seguimento_observacao,
                                diagnosticos_citados,
                                pontos_chave,
                                mudancas_relevantes,
                                eventos_comportamentais,
                                sinais_nucleares,
                                terapias_referidas,
                                exames_citados,
                                pendencias_clinicas,
                                condutas_no_prontuario,
                                medicacoes_em_uso,
                                medicacoes_iniciadas,
                                medicacoes_suspensas,
                                condutas_especificas_sugeridas,
                                condutas_gerais_sugeridas,
                                mensagens_acompanhamento
                            FROM chatgpt_atendimentos_analise
                            WHERE id_atendimento = ${idAtendimento}
                            LIMIT 1
                    `),
                    reason: 'Busca análise prévia do atendimento'
                })
            });

            const data = await res.json();

            if (!data?.success || !data.data?.length) {
                // diagnóstico: mostra o que o PHP realmente retornou
                console.warn('⚠️  Resposta do execute_sql:', data);
                if (!data?.success) {
                    console.error('❌ PHP retornou erro:', data?.error ?? 'sem mensagem');
                } else {
                    console.log('%cℹ️  Nenhuma análise encontrada no banco.', 'color: #9e9e9e');
                }
                console.groupEnd();
                return;
            }

            const row = data.data[0];

            if (row.status !== 'concluido') {
                console.log(`%cℹ️  Análise existe mas status='${row.status}' — ignorando.`, 'color: #ff9800');
                if (row.status === 'pendente' || row.status === 'processando') {
                    notifyAnalisePreviaPendente(row.status, 'atendimento');
                }
                console.groupEnd();
                return;
            }
            const analise = parseAnalisePreviaRow(row);
            applyAnalisePrevia(analise, row, 'atendimento');

        } catch (e) {
            console.error('🚨 Falha ao buscar análise:', e.message);
        }

        console.groupEnd();
    }

    async function fetchAnalisePacienteCompilada(idPaciente) {
        console.groupCollapsed(`%c${FILE_PREFIX} 🧬 Síntese do Paciente — id_paciente=${idPaciente}`, 'color: #7b1fa2; font-weight: bold');

        if (!idPaciente) {
            console.warn('❌ id_paciente nulo/zero — abortando síntese compilada.');
            console.groupEnd();
            return;
        }

        try {
            const res = await fetch("<?php echo $_SERVER['PHP_SELF']; ?>?action=execute_sql", {
                method:  'POST',
                headers: { 'Content-Type': 'application/json; charset=utf-8' },
                body:    JSON.stringify({
                    query: normalizeSQL(`
                        SELECT
                            status,
                            datetime_analise_concluida,
                            resumo_texto,
                            gravidade_clinica,
                            dados_json,
                            seguimento_retorno_estimado,
                            seguimento_observacao,
                            diagnosticos_citados,
                            pontos_chave,
                            mudancas_relevantes,
                            eventos_comportamentais,
                            sinais_nucleares,
                            terapias_referidas,
                            exames_citados,
                            pendencias_clinicas,
                            condutas_no_prontuario,
                            medicacoes_em_uso,
                            medicacoes_iniciadas,
                            medicacoes_suspensas,
                            condutas_especificas_sugeridas,
                            condutas_gerais_sugeridas,
                            mensagens_acompanhamento
                        FROM chatgpt_atendimentos_analise
                        WHERE id_paciente = ${idPaciente}
                          AND id_atendimento IS NULL
                          AND id_criador IS NULL
                        ORDER BY datetime_analise_concluida DESC, id DESC
                        LIMIT 1
                    `),
                    reason: 'Busca síntese longitudinal compilada do paciente'
                })
            });

            const data = await res.json();
            if (!data?.success || !data.data?.length) {
                console.log('%cℹ️  Nenhuma síntese compilada do paciente encontrada.', 'color: #9e9e9e');
                console.groupEnd();
                return;
            }

            const row = data.data[0];
            if (row.status !== 'concluido') {
                console.log(`%cℹ️  Síntese compilada existe mas status='${row.status}' — ignorando.`, 'color: #ff9800');
                console.groupEnd();
                return;
            }

            const analise = parseAnalisePreviaRow(row);
            applyAnalisePrevia(analise, row, 'paciente_compilado');
        } catch (e) {
            console.error('🚨 Falha ao buscar síntese compilada do paciente:', e.message);
        }

        console.groupEnd();
    }
    
    // ==========================================
    // FUNÇÃO: SALVAR METADADOS NO MYSQL
    // ==========================================
    function saveChatMetaToDatabase() {
        // Só prossegue se tivermos um ID válido na memória global
        if (typeof state === 'undefined' || !state.currentChatId) return;

        const payload = {
            id_chatgpt:     state.currentChatId,
            url_chatgpt:    state.currentChatUrl || '',
            url_atual:      window.location.href,
            id_paciente:    PAGE_CTX.id_paciente    || null,
            id_membro:      PAGE_CTX.id_membro      || null,
            id_atendimento: PAGE_CTX.id_atendimento || null,
            id_receita:     PAGE_CTX.id_receita     || null
        };

        fetch(`<?php echo $_SERVER['PHP_SELF']; ?>?action=save_chat_meta`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        })
        .then(res => res.json())
        .then(data => {
            if (data.success) {
                console.log(payload);
                // Atualiza o título com o valor gerado pelo servidor
                if (data.titulo) {
                    state.currentChatTitle = data.titulo;
                    const titleEl = document.getElementById('ow-chat-title');
                    if (titleEl) { titleEl.innerText = data.titulo; titleEl.title = data.titulo; }
                    saveLocal();
                }
                console.groupCollapsed(`%c${FILE_PREFIX} 💾 [MySQL] Chat [${payload.id_chatgpt}] persistido no banco de dados!`, "color: #4caf50; font-weight: bold;");
                console.log(`SQL: ${data.sql}`);
                console.groupEnd();
            } else {
                console.warn(`%c${FILE_PREFIX} ⚠️ [MySQL] Falha ao persistir chat:`, "color: #ff9800;", data.error);
            }
        })
        .catch(err => console.error("Erro de rede ao salvar chat no DB:", err));
    }
    
    
    
    function saveLocal() {
        localStorage.setItem(HISTORYKEY, JSON.stringify({
            messages:  state.messages.slice(-30),
            chatId:    state.currentChatId,
            title:     state.currentChatTitle,
            url:       state.currentChatUrl,
            lastQ:     currentUserQuestion   // ← NOVO: persiste a pergunta
        }));
    }
    
    async function loadLocal() {
        // 1. Carrega os dados locais primeiro para exibir as mensagens de imediato na UI
        const saved = localStorage.getItem(HISTORYKEY);
        if (saved) {
            try {
                const parsed = JSON.parse(saved);
                if (Array.isArray(parsed)) {
                    state.messages = parsed;
                    // formato legado: chatId não estava salvo — tenta recuperar do MySQL abaixo
                } else {
                    state.messages       = parsed.messages      || [];
                    state.currentChatId  = parsed.chatId        || null;
                    state.currentChatTitle = parsed.title       || null;
                    state.currentChatUrl = parsed.url           || null;
                    // Restaura pergunta persistida
                    if (parsed.lastQ) currentUserQuestion = parsed.lastQ;
                }
            } catch(e) {}
        }
        
        renderChatMessages();

        // ------------------------------------------------------------------
        // 👉 NOVO: 2. Buscar metadados oficias do MySQL antes do Sync Remoto
        // ------------------------------------------------------------------
        try {
            console.log(`%c🔍 ${FILE_PREFIX} [MySQL][loadLocal] Procurando histórico de chat para esta página...`, "color: #9c27b0; font-weight: bold;");
            

            const metaRes = await fetch(`<?php echo $_SERVER['PHP_SELF']; ?>?action=get_chat_meta`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    id_paciente:    PAGE_CTX.id_paciente    || null,
                    id_membro:      PAGE_CTX.id_membro      || null,
                    id_atendimento: PAGE_CTX.id_atendimento || null,
                    id_receita:     PAGE_CTX.id_receita     || null
                })
            });
            
            const metaData = await metaRes.json();
            
            if (metaData && metaData.success && metaData.chat) {
                // Se a BD tem dados, eles têm prioridade sobre o LocalStorage!
                state.currentChatId = metaData.chat.id_chatgpt;
                state.currentChatUrl = metaData.chat.url_chatgpt;
                
                if (metaData.chat.titulo) {
                    state.currentChatTitle = metaData.chat.titulo;
                    const titleEl = document.getElementById('ow-chat-title');
                    if (titleEl) titleEl.innerText = state.currentChatTitle;
                }
                
                saveLocal(); // Sincroniza o novo estado no localStorage do navegador
                console.log(`%c✅ ${FILE_PREFIX} [MySQL] [loadLocal]Chat recuperado: ${state.currentChatId}`, "color: #4caf50; font-weight: bold;");
            } else {
                console.log(`%cℹ️ ${FILE_PREFIX} [MySQL][loadLocal] Nenhum histórico de chat encontrado para esta URL específica.`, "color: #9e9e9e;");
            }
        } catch (e) {
            console.warn("[MySQL][loadLocal] Erro ao consultar a base de dados:", e);
        }

        // ------------------------------------------------------------------
        // 3. Sync Simulator: Se temos um ID (do MySQL ou LocalStorage), busca remoto
        // ------------------------------------------------------------------
        const isChatGPTUrl = state.currentChatUrl && state.currentChatUrl.includes("chatgpt.com");
        if (state.currentChatId && isChatGPTUrl) {
            
            console.log(`%c☁️ ${FILE_PREFIX} [SYNC] Sincronizando histórico com a nuvem... (ID: ${state.currentChatId})`, "color: #2196f3; font-weight: bold;");

            try {
                // Indicador visual de Sincronização na interface
                const box = document.getElementById('ow-messages');
                const syncMsg = document.createElement('div');
                syncMsg.id = 'ow-sync-indicator';
                syncMsg.innerHTML = '<div style="text-align:center; font-size:11px; color:#aaa; margin-top: 10px;">🔄 Atualizando histórico com a nuvem...</div>';
                box.appendChild(syncMsg);
                scroll(true);

                const startTime = Date.now();

                // Faz a requisição ao endpoint PHP (Remote Sync)
                const res = await fetch("<?php echo $_SERVER['PHP_SELF']; ?>?action=sync_simulator", {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ chat_id: state.currentChatId, url: state.currentChatUrl })
                });
                
                const data = await res.json();
                const duration = ((Date.now() - startTime) / 1000).toFixed(1);
                
                if (data && data.success && data.chat && Array.isArray(data.chat.messages)) {
                    const msgArray = data.chat.messages;
                    
                    // Título gerido pelo servidor — não sobrescrever com título do sync
                    if (data.chat.url) state.currentChatUrl = data.chat.url;
                    
                    console.groupCollapsed(`%c☁️ ${FILE_PREFIX} [SYNC] ✅ Sucesso (${duration}s)`, "color: #4caf50; font-weight: bold; background: #e8f5e9; padding: 4px 8px; border-radius: 4px;");
                    console.log(`URL: ${state.currentChatUrl}`);
                    console.log(`Título: ${state.currentChatTitle}`);
                    console.log(`${msgArray.length} mensagens recuperadas.`);
                    console.log('%c📦 Mensagens recuperadas (JSON):', 'color: #1565c0; font-weight: bold;');  // ✅ NOVO
                    console.log(JSON.parse(JSON.stringify(msgArray)));                                          // ✅ NOVO — objeto expansível no DevTools
                    console.groupEnd();
                    
                    state.messages = msgArray; 
                    saveLocal();
                    renderChatMessages();
                } else if (data && data.error) {
                    console.groupCollapsed(`%c☁️ ${FILE_PREFIX} [SYNC] ⚠️ Erro do Servidor`, "color: #ff9800; font-weight: bold; background: #fff3e0; padding: 4px 8px; border-radius: 4px;");
                    console.warn("Detalhe do Erro:", data.error);
                    console.groupEnd();

                    // Limpa histórico se o chat não existir mais no servidor Python/OpenAI
                    if (data.error === "chat_not_found" || data.error.includes("não encontrado")) {
                        console.error(`%c🗑️ ${FILE_PREFIX} [SYNC] Chat inexistente na nuvem. Limpando contexto...`, "color: #f44336; font-weight: bold;");
                        state.messages = [];
                        state.currentChatId = null;
                        state.currentChatTitle = null;
                        state.currentChatUrl = null;
                        saveLocal();
                        renderChatMessages();
                    }
                    
                    if (document.getElementById('ow-sync-indicator')) document.getElementById('ow-sync-indicator').remove();
                }
                
            } catch (e) {
                console.groupCollapsed(`%c☁️ ${FILE_PREFIX} [SYNC] ❌ Falha de Conexão`, "color: #f44336; font-weight: bold; background: #ffebee; padding: 4px 8px; border-radius: 4px;");
                console.error("Motivo:", e);
                console.groupEnd();
                if (document.getElementById('ow-sync-indicator')) document.getElementById('ow-sync-indicator').remove();
            }
        }
    }
    
    function renderChatMessages() {
        updateTitleUI();
        const box = document.getElementById('ow-messages');
        box.innerHTML = '';
        state.messages.forEach(m => {
            if (m.role === 'assistant') {
                if (m.content.includes('</think>')) {
                    const parts = m.content.split('</think>');
                    const ui = addAiMarkup();
                    document.getElementById(ui.tID).parentElement.style.display = 'block';
                    document.getElementById(ui.tID).innerText = parts[0].replace('<think>','').trim();
                    document.getElementById(ui.mID).innerHTML = formatMarkdown(parts[1].trim());
                    document.getElementById(ui.mID).classList.remove('cursor-blink');
                } else if (m.content.includes(('<div>').slice(0, -1)) && m.content.includes('class=')) { 
                    // Simulador em HTML puro
                    const ui = addAiMarkup();
                    document.getElementById(ui.mID).innerHTML = m.content;
                    document.getElementById(ui.mID).classList.remove('cursor-blink');
                } else {
                    addSimpleMsg('ai', m.content.trim());
                }
            } else if (m.role === 'user') {
                let display = m.content;

                // 1. Remove bloco de contexto (original e versão markdownify-escaped \[FIM\_TEXTO\_COLADO\])
                for (const endToken of ['[FIM_TEXTO_COLADO]', '\\[FIM\\_TEXTO\\_COLADO\\]']) {
                    const idx = display.indexOf(endToken);
                    if (idx !== -1) {
                        display = display.slice(idx + endToken.length);
                        break;
                    }
                }

                // 2. Remove separador USER_SEP (fallback para mensagens locais sem bloco de contexto)
                if (display.includes(USER_SEP)) {
                    display = display.split(USER_SEP).pop();
                }

                addSimpleMsg('user', display.trim());
            }
        });
        scroll(true);
    }
    // ===================== INICIO =====================
    // FUNÇÃO: RENDERIZAR ANÁLISE PRÉVIA DE LLM QUANTO AO PRONTUÁRIO
    // ==========================================
    function renderAnalisePrevia(com_log = false) {
        const log  = (...args) => { if (com_log) console.log(...args); };
        const warn = (...args) => { if (com_log) console.warn(...args); };

        log('%c🎨 renderAnalisePrevia() iniciado', 'color:#9c27b0;font-weight:bold');

        if (!analiseAtendimentoCtx) {
            warn('⛔ abortou: analiseAtendimentoCtx é null/undefined');
            return;
        }
        log('%c✅ analiseAtendimentoCtx existe', 'color:#4caf50', typeof analiseAtendimentoCtx);

        let ctx;
        try {
            ctx = typeof analiseAtendimentoCtx === 'string'
                ? JSON.parse(analiseAtendimentoCtx)
                : analiseAtendimentoCtx;
            log('%c✅ JSON parseado com sucesso', 'color:#4caf50');
        } catch(e) {
            warn('⛔ abortou: falha ao parsear JSON —', e.message);
            return;
        }

        if (!ctx?.analise_clinica_previa) {
            warn('⛔ abortou: ctx.analise_clinica_previa é null/undefined');
            warn('chaves do ctx:', Object.keys(ctx || {}));
            return;
        }

        const a = ctx.analise_clinica_previa;
        log('%c✅ analise_clinica_previa carregado', 'color:#4caf50');
        log('  resumo_texto:         ', a.resumo_texto?.substring(0, 80));
        log('  gravidade_clinica:    ', a.gravidade_clinica);
        log('  idade_paciente:       ', a.idade_paciente);
        log('  diagnosticos_citados: ', a.diagnosticos_citados?.length ?? 'ausente');
        log('  pontos_chave:         ', a.pontos_chave?.length ?? 'ausente');
        log('  medicacoes_em_uso:    ', a.medicacoes_em_uso?.length ?? 'ausente');
        log('  condutas_especificas: ', a.condutas_especificas_sugeridas?.length ?? 'ausente');
        log('  condutas_gerais:      ', a.condutas_gerais_sugeridas?.length ?? 'ausente');

        const container = document.getElementById('ow-messages');
        if (!container) {
            warn('⛔ abortou: #ow-messages não encontrado no DOM');
            return;
        }
        log('%c✅ #ow-messages encontrado', 'color:#4caf50');

        // ── helpers ───────────────────────────────────────────────────
        const esc       = s => (s ?? '').toString()
            .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/'/g,'&#39;');
        // Extrai texto de um item que pode ser string (V7) ou objeto (V16)
        const toStr = i => {
            if (!i || typeof i === 'string') return i || '';
            // V16 object: usa 'in' para detectar campo mesmo quando vazio (evita JSON.stringify)
            // _pick: retorna primeiro valor nao-vazio encontrado; '' se nenhum for util
            const _pick = (...keys) => { for (const k of keys) { const v = i[k]; if (v !== null && v !== undefined && String(v).trim()) return String(v); } return ''; };
            const picked = _pick('diagnostico','descricao','evento','terapia','exame','pendencia','conduta','nome','texto','medicacao');
            // Se nenhum campo util: retorna '' (sera filtrado por pills/listItems)
            if (!picked) return '';
            return picked;
        };
        const listItems = arr => arr?.length ? arr.map(i => toStr(i)).filter(Boolean).map(s => `<li>${esc(s)}</li>`).join('') : '';
        const pills     = arr => arr?.length ? arr.map(i => toStr(i)).filter(Boolean).map(s => `<span class="ia-tag">${esc(s)}</span>`).join('') : '';

        // Wrapper padrão de seção — DEVE ser usado em todas as seções para que
        // apToggleSection e apToggleAll funcionem corretamente.
        const section = (titulo, conteudo) => `
            <div class="ia-section">
                <div class="ia-section-title ia-section-toggle" onclick="apToggleSection(this)" style="cursor:pointer;display:flex;justify-content:space-between;align-items:center">
                    <span>${titulo}</span><span class="ia-toggle" style="font-size:12px;color:#94a3b8;transition:transform .2s">⌄</span>
                </div>
                <div class="ia-section-body">${conteudo}</div>
            </div>`;

        const sectionCollapsed = (titulo, conteudo) => `
            <div class="ia-section is-collapsed">
                <div class="ia-section-title ia-section-toggle" onclick="apToggleSection(this)" style="cursor:pointer;display:flex;justify-content:space-between;align-items:center">
                    <span>${titulo}</span><span class="ia-toggle" style="font-size:12px;color:#94a3b8;transition:transform .2s;transform:rotate(-90deg)">⌄</span>
                </div>
                <div class="ia-section-body" style="display:none">${conteudo}</div>
            </div>`;

        // ── gravidade badge ───────────────────────────────────────────
        const gravMap   = { leve:'#16a34a', moderada:'#b45309', grave:'#dc2626', alta:'#dc2626', urgente:'#dc2626' };
        // gravidade_clinica pode ser string simples (V7) ou JSON {nivel,score_estimado,justificativa} (V16)
        const gravObj = (() => {
            const raw = a.gravidade_clinica;
            if (!raw) return null;
            if (typeof raw === 'object') return raw;
            try { const p = JSON.parse(raw); return typeof p === 'object' ? p : null; } catch(e) { return null; }
        })();
        const gravRaw = gravObj?.nivel
            || (typeof a.gravidade_clinica === 'string' ? a.gravidade_clinica : null)
            || a.score_gravidade_neurodesenvolvimento?.classificacao
            || a.analise_risco_clinico?.risco_urgencia || null;
        const gravKey   = gravRaw ? Object.keys(gravMap).find(k => gravRaw.toLowerCase().includes(k)) : null;
        const gravColor = gravKey ? gravMap[gravKey] : '#64748b';
        // gravLabel: extrai nivel de gravidade sem nunca exibir JSON bruto
        const gravLabel = (() => {
            // 1. objeto V16 com .nivel
            if (gravObj?.nivel) return gravObj.nivel;
            // 2. string simples V7 (ex: 'moderada', 'grave')
            const raw = a.gravidade_clinica;
            if (typeof raw === 'string' && !raw.trim().startsWith('{') && raw.trim()) return raw.trim();
            // 3. fallbacks V16
            if (a.analise_risco_clinico?.risco_urgencia) return a.analise_risco_clinico.risco_urgencia;
            if (a.score_gravidade_neurodesenvolvimento?.classificacao) return a.score_gravidade_neurodesenvolvimento.classificacao;
            // 4. tenta extrair do dados_json caso as colunas acima estejam vazias
            try {
                const dj = (typeof a.dados_json === 'string') ? JSON.parse(a.dados_json) : a.dados_json;
                return dj?.gravidade_clinica?.nivel || dj?.analise_risco_clinico?.risco_urgencia || '';
            } catch(e) { return ''; }
        })();
        const gravBadge = gravLabel
            ? `<span class="ia-tag" style="background:${gravColor};color:#fff;border:none;font-weight:700">⚡ ${esc(gravLabel)}</span>` : '';

        const idadeTxt = a.idade_paciente?.valor != null
            ? `${a.idade_paciente.valor} ${a.idade_paciente.unidade || ''}`.trim()
            : a.identificacao_paciente?.idade != null
            ? `${a.identificacao_paciente.idade} anos` : '';

        // ── tags automáticas ──────────────────────────────────────────
        const tagsKeywords = ['TEA','autismo','nível 3','deficiência intelectual','fala funcional',
            'agressividade','insônia','sono','risperidona','clonidina','melatonina',
            'regressão','estereotipias','TDAH','epilepsia'];
        const txt  = (a.resumo_texto + ' ' + (a.pontos_chave || []).join(' ')).toLowerCase();
        const tags = tagsKeywords.filter(t => txt.includes(t.toLowerCase()));
        log('  tags detectadas:', tags);

        // ── medicações ────────────────────────────────────────────────
        const medRow = (m, label, labelColor) => {
            // Suporte a V7 {nome,dose,posologia,...} e V16 {medicacao,dose,indicacao/motivo_inicio/motivo_suspensao}
            const nome   = m.nome      || m.medicacao  || '';
            const dose   = m.dose      || '';
            const posol  = m.posologia || '';
            const obs    = m.observacao|| m.indicacao  || m.motivo_inicio || '';
            const motivo = m.motivo    || m.motivo_suspensao || '';
            const desde  = m.desde     || m.data_relativa   || m.periodo || '';
            return `
            <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:8px 11px;margin-bottom:7px">
                <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
                    <strong style="font-size:13px">${esc(nome)}</strong>
                    <span style="font-size:12px;font-weight:700;padding:2px 8px;border-radius:999px;background:${labelColor};color:#fff">${label}</span>
                </div>
                ${dose   ? `<div style="font-size:13px;color:#334155;margin-top:3px">💊 ${esc(dose)}${posol ? ' · '+esc(posol) : ''}</div>` : ''}
                ${desde  ? `<div style="font-size:12px;color:#64748b">📅 ${esc(desde)}</div>` : ''}
                ${motivo ? `<div style="font-size:12px;color:#dc2626">🚫 ${esc(motivo)}</div>` : ''}
                ${obs    ? `<div style="font-size:12px;color:#64748b;font-style:italic">ℹ️ ${esc(obs)}</div>` : ''}
            </div>`;
        };

        const temMeds    = a.medicacoes_em_uso?.length || a.medicacoes_iniciadas?.length || a.medicacoes_suspensas?.length;
        const medsInner  = !temMeds ? '' : `
            ${a.medicacoes_em_uso?.length    ? `<div class="ia-section-sub">Em uso contínuo</div>${a.medicacoes_em_uso.map(m=>medRow(m,'em uso','#1976d2')).join('')}`    : ''}
            ${a.medicacoes_iniciadas?.length ? `<div class="ia-section-sub" style="color:#16a34a">Iniciadas</div>${a.medicacoes_iniciadas.map(m=>medRow(m,'nova','#16a34a')).join('')}` : ''}
            ${a.medicacoes_suspensas?.length ? `<div class="ia-section-sub" style="color:#dc2626">Suspensas</div>${a.medicacoes_suspensas.map(m=>medRow(m,'suspensa','#dc2626')).join('')}` : ''}`;

        // ── condutas específicas ──────────────────────────────────────
        const condEspInner = (a.condutas_especificas_sugeridas || []).map((c, i) => {
            const refTxt  = esc([c.referencia, c.fonte].filter(Boolean).join(' | '));
            const refCopy = esc([c.referencia, c.fonte].filter(Boolean).join(' | '));
            return `
            <div class="ia-conduta${i === 0 ? ' is-open' : ''}">
                <button class="ia-conduta-header" onclick="apToggleConduta(this)">
                    <span>${esc(c.conduta)}</span><span class="ia-toggle">⌄</span>
                </button>
                <div class="ia-conduta-body">
                    ${c.justificativa ? `<div style="font-size:12px;margin-bottom:6px">${esc(c.justificativa)}</div>` : ''}
                    ${refTxt ? `<div class="ia-ref"><span>${refTxt}</span>
                        <button class="ia-mini-btn" onclick="apCopiarRef(event,'${refCopy}')">copiar</button>
                    </div>` : ''}
                </div>
            </div>`;
        }).join('');
        
        // ── seguimento ────────────────────────────────────────────────
        const seg     = a.seguimento_sugerido || {};
        const segData = (() => {
            const raw = seg.retorno_estimado;
            if (!raw) return {};
            if (typeof raw === 'object') return raw;
            try { return JSON.parse(raw); } catch(e) { return {}; }
        })();
        const prioMap   = { baixo:'#16a34a', moderado:'#b45309', alto:'#dc2626' };
        const prioColor = prioMap[(segData.nivel_prioridade||'').toLowerCase()] || '#64748b';
        const prioBadge = segData.nivel_prioridade
            ? `<span class="ia-prio" style="background:${prioColor}">${esc(segData.nivel_prioridade)}</span>`
            : '';
        const segHtml = (segData.intervalo_estimado || segData.data_estimada || segData.motivo_clinico) ? `
            <div class="ia-seguimento">
                <div class="ia-seguimento-header">
                    ${segData.data_estimada      ? `<span class="ia-seguimento-data">📅 Retorno: ${esc(segData.data_estimada)}</span>` : ''}
                    ${segData.intervalo_estimado ? `<span class="ia-seguimento-intervalo">(${esc(segData.intervalo_estimado)})</span>` : ''}
                    ${prioBadge}
                </div>
                ${segData.motivo_clinico ? `<div class="ia-seguimento-motivo">🎯 ${esc(segData.motivo_clinico)}</div>` : ''}
                ${segData.base_clinica   ? `<div class="ia-seguimento-base">📖 ${esc(segData.base_clinica)}</div>` : ''}
                ${segData.parametros_a_avaliar?.length ? `<div class="ia-seguimento-params"><strong>Parâmetros:</strong><ul>${segData.parametros_a_avaliar.map(p=>`<li>${esc(p)}</li>`).join('')}</ul></div>` : ''}
                ${seg.observacao ? `<div class="ia-seguimento-base">${esc(seg.observacao)}</div>` : ''}
            </div>` : '';
        const pendSeg = `
            ${a.pendencias_clinicas?.length ? `<ul class="ia-list">${listItems(a.pendencias_clinicas)}</ul>` : ''}
            ${segHtml}`;
        // ── mudanças + eventos ────────────────────────────────────────
        const mudEvInner = `
            ${a.mudancas_relevantes?.length     ? `<div class="ia-section-sub">Mudanças relevantes</div><ul class="ia-list">${listItems(a.mudancas_relevantes)}</ul>`     : ''}
            ${a.eventos_comportamentais?.length ? `<div class="ia-section-sub">Eventos comportamentais</div><ul class="ia-list">${listItems(a.eventos_comportamentais)}</ul>` : ''}`;
        // ── terapias + exames ─────────────────────────────────────────
        const examePills = a.exames_citados?.length ? pills(a.exames_citados) : '';
        const exameHtml  = examePills
            ? `<div class="ia-tags">${examePills}</div>`
            : `<span style="font-size:12px;color:#94a3b8;font-style:italic">Nenhum exame solicitado nesta consulta.</span>`;
        const terapExInner = `
            ${a.terapias_referidas?.length ? `<div class="ia-section-sub">Terapias</div><div class="ia-tags">${pills(a.terapias_referidas)}</div>` : ''}
            <div class="ia-section-sub">Exames</div>${exameHtml}`;
        const analisadoEm = a.analisado_em
            ? new Date(a.analisado_em).toLocaleString('pt-BR', {dateStyle:'short', timeStyle:'short'}) : '';
            
        // ── mensagens de acompanhamento ──────────────────
        const msgAcomp = (() => {
            let raw = a.mensagens_acompanhamento;
            if (!raw) return null;
            if (typeof raw === 'string') { try { raw = JSON.parse(raw); } catch(e) { return null; } }
            if (typeof raw !== 'object' || Array.isArray(raw)) return null;
            return raw;
        })();

        const msgBox = (label, icone, texto) => !texto ? '' : `
            <div style="margin-bottom:12px">
                <div class="ia-section-sub">${icone} ${label}</div>
                <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:10px 12px;font-size:13px;color:#334155;white-space:pre-wrap;line-height:1.5">${esc(texto)}</div>
                <button onclick="navigator.clipboard.writeText(this.dataset.txt).then(()=>apToast('Copiado!'))"
                    data-txt="${esc(texto)}"
                    style="margin-top:4px;background:none;border:1px solid #e2e8f0;border-radius:5px;padding:3px 10px;font-size:11px;cursor:pointer;color:#64748b">
                    📋 Copiar mensagem
                </button>
            </div>`;

        const mensagensHtml = msgAcomp ? (
            msgBox('1 semana após a consulta', '📅', msgAcomp.mensagem_1_semana) +
            msgBox('1 mês após a consulta', '📆', msgAcomp.mensagem_1_mes) +
            msgBox('Pré-retorno', '🔔', msgAcomp.mensagem_pre_retorno)
        ) : '';

        // ── monta HTML ────────────────────────────────────────────────
        const html = `
        <div id="ow-analise-previa">
            <div class="ia-header">
                <div>
                    <div class="ia-title">🧠 Análise clínica prévia${idadeTxt ? ' · ' + idadeTxt : ''}</div>
                    <div style="font-size:11px;color:#94a3b8">Gerada por IA · Revisar antes de validar</div>
                </div>
                <div class="ia-actions">
                    <button class="ia-btn" onclick="apToggleAll(true)">Expandir</button>
                    <button class="ia-btn" onclick="apToggleAll(false)">Recolher</button>
                    <button class="ia-btn" onclick="apCopiarResumo()">Copiar resumo</button>
                </div>
            </div>

            <div class="ia-resumo">
                <div id="iap-resumo-texto" class="ia-resumo-text">${esc(a.resumo_texto || a.resumo_clinico_objetivo || '')}</div>
                <div class="ia-tags" style="margin-top:8px">
                    ${gravBadge}
                    ${(a.diagnosticos_citados||[]).map(d => {
                        if (typeof d === 'string') return `<span class="ia-tag">${esc(d)}</span>`;
                        const label = esc(d.diagnostico||'');
                        const cid   = d.cid10_sugerido ? ` <small style="opacity:.7">${esc(d.cid10_sugerido)}</small>` : '';
                        return `<span class="ia-tag">${label}${cid}</span>`;
                    }).join('')}
                    ${tags.map(t=>`<span class="ia-tag">${esc(t)}</span>`).join('')}
                </div>
            </div>

            ${a.pontos_chave?.length                                   ? section('📌 Pontos-chave',            `<ul class="ia-list">${listItems(a.pontos_chave)}</ul>`) : ''}
            ${a.sinais_nucleares?.length                               ? section('🔍 Sinais nucleares',         `<div class="ia-tags">${pills(a.sinais_nucleares)}</div>`) : ''}
            ${a.mudancas_relevantes?.length||a.eventos_comportamentais?.length ? section('🔄 Mudanças e eventos', mudEvInner) : ''}
            ${temMeds                                                  ? section('💊 Medicações',               medsInner) : ''}
            ${condEspInner                                             ? section('📋 Condutas com evidência',   condEspInner) : ''}
            ${a.condutas_gerais_sugeridas?.length                      ? section('💡 Condutas gerais',          `<ul class="ia-list">${listItems(a.condutas_gerais_sugeridas)}</ul>`) : ''}
            ${a.condutas_registradas_no_prontuario?.length             ? section('📝 Condutas no prontuário',   `<ul class="ia-list">${listItems(a.condutas_registradas_no_prontuario)}</ul>`) : ''}
            ${a.pendencias_clinicas?.length||segHtml                   ? section('⏳ Pendências',               pendSeg) : ''}
            ${section('🔬 Terapias e exames', terapExInner)}
            ${(() => {
                const acs = a.alertas_clinicos;
                if (!acs?.length) return '';
                const nivelCor = {baixo:'#16a34a', moderado:'#b45309', alto:'#dc2626', moderada:'#b45309', grave:'#dc2626'};
                const rows = acs.map(al => {
                    const txt  = typeof al === 'string' ? al : (al.descricao || al.tipo || JSON.stringify(al));
                    const niv  = typeof al === 'object' ? (al.nivel||'').toLowerCase() : '';
                    const cor  = nivelCor[niv] || '#64748b';
                    const badge = niv ? `<span style="font-size:10px;padding:1px 6px;border-radius:999px;background:${cor};color:#fff;margin-left:6px">${esc(niv)}</span>` : '';
                    return `<li>${esc(txt)}${badge}</li>`;
                }).join('');
                return sectionCollapsed('🚨 Alertas clínicos', `<ul class="ia-list">${rows}</ul>`);
            })()}
            ${(() => {
                const tl = a.timeline_clinica;
                if (!tl?.length) return '';
                const rows = tl.map(t => `<li><strong>${esc(t.idade||t.data||'')}</strong> — ${esc(t.evento||t.descricao||'')}</li>`).join('');
                return sectionCollapsed('🕐 Timeline clínica', `<ul class="ia-list">${rows}</ul>`);
            })()}
            ${mensagensHtml ? sectionCollapsed('📲 Mensagens de acompanhamento', mensagensHtml) : ''}

            <div style="font-size:10px;color:#94a3b8;text-align:right;margin-top:8px;padding-top:8px;border-top:1px solid #f1f5f9">
                ${analisadoEm ? `Analisado em ${analisadoEm}` : ''}
            </div>
        </div>`;

        log('%c✅ HTML montado, injetando no DOM...', 'color:#4caf50', `${html.length} chars`);
        
        
        // ── Remove instância anterior antes de injetar ────────────────
        const anterior = document.getElementById('ow-analise-previa');
        if (anterior) anterior.remove();

        container.insertAdjacentHTML('afterbegin', html);
        

        const inserted = document.getElementById('ow-analise-previa');
        if (inserted) {
            log('%c✅ #ow-analise-previa inserido com sucesso!', 'color:#4caf50;font-weight:bold');
        } else {
            warn('⛔ insertAdjacentHTML executou mas #ow-analise-previa NÃO encontrado após inserção');
        }

        container.scrollTop = 0;
    }

    // ── Funções auxiliares globais do card ──────────────────────────
    window.apToast = function(msg) {
        let t = document.getElementById('iap-toast');
        if (!t) { t = document.createElement('div'); t.id = 'iap-toast'; document.body.appendChild(t); }
        t.textContent = msg;
        t.classList.add('show');
        clearTimeout(window.__iapToastTimer);
        window.__iapToastTimer = setTimeout(() => t.classList.remove('show'), 1600);
    };

    window.apToggleConduta = function(btn) {
        const card = btn.closest('.ia-conduta');
        if (card) card.classList.toggle('is-open');
    };

    window.apToggleSection = function(titleEl) {
        const sec = titleEl.closest('.ia-section');
        if (!sec) return;
        sec.classList.toggle('is-collapsed');
        const body  = sec.querySelector('.ia-section-body');
        const arrow = titleEl.querySelector('.ia-toggle');
        if (body)  body.style.display    = sec.classList.contains('is-collapsed') ? 'none' : '';
        if (arrow) arrow.style.transform = sec.classList.contains('is-collapsed') ? 'rotate(-90deg)' : '';
    };

    window.apToggleAll = function(open) {
        document.querySelectorAll('#ow-analise-previa .ia-conduta').forEach(c =>
            c.classList.toggle('is-open', open));
        document.querySelectorAll('#ow-analise-previa .ia-section').forEach(sec => {
            const body  = sec.querySelector('.ia-section-body');
            const arrow = sec.querySelector('.ia-section-toggle .ia-toggle');
            if (open) {
                sec.classList.remove('is-collapsed');
                if (body)  body.style.display    = '';
                if (arrow) arrow.style.transform = '';
            } else {
                sec.classList.add('is-collapsed');
                if (body)  body.style.display    = 'none';
                if (arrow) arrow.style.transform = 'rotate(-90deg)';
            }
        });
    };

    window.apCopiarResumo = function() {
        const el = document.getElementById('iap-resumo-texto');
        if (!el) return;
        navigator.clipboard.writeText(el.innerText.trim()).then(() => apToast('Resumo copiado'));
    };

    window.apCopiarRef = function(event, texto) {
        if (event) event.stopPropagation();
        navigator.clipboard.writeText(texto).then(() => apToast('Referência copiada'));
    };
    // ==========================================
    // FUNÇÃO: RENDERIZAR ANÁLISE PRÉVIA DE LLM QUANTO AO PRONTUÁRIO
    // ===================== FIM =====================
    
    function detectContexts() {
        // Se a página ainda não terminou de carregar, aguarda o evento 'load'
        if (document.readyState !== 'complete') {
            window.addEventListener('load', detectContexts);
            return;
        }

        // Adiciona um pequeno delay extra para garantir que o CKEditor tenha tempo de instanciar
        setTimeout(() => {
            const ct = [];
            console.groupCollapsed(`%c🔧 ${FILE_PREFIX} [SYSTEM] Verificando contextos disponíveis para envio ao ChatJS (LLM)`, "color: #e67e22; font-weight: bold;");
            
            // Busca dinâmica para lidar com IDs gerados como [1000000]
            const txtEvolucao = document.querySelector('textarea[name="atendimento_consulta_conteudo"]');
            
            let ckeditorInstanceName = null;
            if (window.CKEDITOR && window.CKEDITOR.instances) {
                // Varre todas as instâncias e encontra a que contém o nome base
                ckeditorInstanceName = Object.keys(window.CKEDITOR.instances).find(key => key.includes('atendimento_consulta_conteudo'));
            }

            if (document.getElementById('evolucao_conteudo') || txtEvolucao || ckeditorInstanceName) {
                // Monta os sources dinamicamente para garantir que a função de extração ache o texto depois
                const sourcesArray = ['evolucao_conteudo'];
                if (ckeditorInstanceName) sourcesArray.push(ckeditorInstanceName);
                if (txtEvolucao && txtEvolucao.id) sourcesArray.push(txtEvolucao.id);
                sourcesArray.push('atendimento_consulta_conteudo'); // Fallback de segurança
                
                ct.push({ id: 'c1', label: 'Evolução', sources: sourcesArray });
                
                // 👉 CORREÇÃO AQUI: Uso seguro das variáveis para evitar erro de 'null'
                const nomeInstanciaDetectada = ckeditorInstanceName || (txtEvolucao ? txtEvolucao.id : 'evolucao_conteudo');
                console.log(`Detectou prontuário/evolução disponível (Instância: ${nomeInstanciaDetectada}) - disponibilizado(a).`);
            }
            else {
                console.log("Não detectou prontuário/evolução disponível.");
            }
            
            if (document.getElementById('receita_receita')) {
                ct.push({ id: 'c2', label: 'Receita', sources: ['receita_receita'] });
                console.log("Detectou receita/laudo disponível - disponibilizada(o) para envio à LLM.");
            }
            else {
                console.log("Não detectou receita/laudo disponível.");
            }
            
            const container = document.getElementById('ow-context-area'); 
            if (container) {
                container.innerHTML = '';
                const savedPrefs = JSON.parse(localStorage.getItem(KEY_CONTEXT) || '{}');
                ct.forEach(c => { 
                    const l = document.createElement('label'); l.className = 'ctx-pill';
                    const checkbox = document.createElement('input'); checkbox.type = 'checkbox';
                    checkbox.dataset.sources = JSON.stringify(c.sources);
                    checkbox.checked = (savedPrefs[c.id] !== undefined) ? savedPrefs[c.id] : true;
                    checkbox.onchange = () => { savedPrefs[c.id] = checkbox.checked; localStorage.setItem(KEY_CONTEXT, JSON.stringify(savedPrefs)); };
                    l.appendChild(checkbox); l.appendChild(document.createTextNode(' ' + c.label)); container.appendChild(l); 
                });
            }
            console.groupEnd();
        }, 500); // 500ms de segurança para o CKEditor
    }

    function scroll(f = false) {
        const b = document.getElementById('ow-messages');
        if (!b) return;
        const doScroll = () => { b.scrollTop = b.scrollHeight; };
        if (f) {
            requestAnimationFrame(() => requestAnimationFrame(doScroll));
        } else if (b.scrollHeight - b.scrollTop - b.clientHeight < 150) {
            doScroll();
        }
    }
    function addSimpleMsg(role, txt) { 
        const b = document.getElementById('ow-messages');

        if (role === 'user') {
            // ── Wrapper de linha ──────────────────────────────────────────────
            const row = document.createElement('div');
            row.className = 'msg-user-row';

            // Botão copiar (ícone clipboard)
            const copyBtn = document.createElement('button');
            copyBtn.className = 'ow-user-copy-btn';
            copyBtn.title = 'Copiar mensagem';
            copyBtn.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
                <path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/>
            </svg>`;
            copyBtn.onclick = () => {
                navigator.clipboard.writeText(txt).then(() => {
                    const orig = copyBtn.innerHTML;
                    copyBtn.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" fill="#0d652d"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>`;
                    setTimeout(() => { copyBtn.innerHTML = orig; }, 2000);
                });
            };

            // Bolha da mensagem
            const d = document.createElement('div');
            d.className = 'msg msg-user';
            d.innerText = txt;

            // Colapso para mensagens longas (> 5 linhas ≈ 96px)
            // Avalia após render com rAF
            row.appendChild(copyBtn);
            row.appendChild(d);
            b.appendChild(row);

            requestAnimationFrame(() => {
                if (d.scrollHeight > 100) {
                    d.classList.add('ow-collapsed');

                    const expandBtn = document.createElement('button');
                    expandBtn.className = 'ow-user-expand-btn';
                    expandBtn.innerHTML = `<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M7 10l5 5 5-5z"/></svg><span>Ver mais</span>`;

                    let expanded = false;
                    expandBtn.onclick = () => {
                        expanded = !expanded;
                        if (expanded) {
                            d.classList.remove('ow-collapsed');
                            d.style.webkitMaskImage = 'none';
                            d.style.maskImage = 'none';
                            expandBtn.innerHTML = `<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M7 14l5-5 5 5z"/></svg><span>Ver menos</span>`;
                        } else {
                            d.classList.add('ow-collapsed');
                            d.style.webkitMaskImage = '';
                            d.style.maskImage = '';
                            expandBtn.innerHTML = `<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M7 10l5 5 5-5z"/></svg><span>Ver mais</span>`;
                        }
                    };

                    // Insere o botão de expansão após a linha
                    row.parentNode.insertBefore(expandBtn, row.nextSibling);
                }
            });

            return;
        }

        // ── Mensagem da IA (inalterada) ──────────────────────────────────────
        const d = document.createElement('div');
        d.className = `msg msg-${role}`;
        d.innerHTML = formatMarkdown(txt);
        b.appendChild(d);
    }
    function addAiMarkup() {
        const b = document.getElementById('ow-messages');
        const wrap = document.createElement('div'); wrap.className = 'msg msg-ai'; wrap.style.background='transparent'; wrap.style.padding='0';
        const tID = 't'+Date.now(), mID = 'm'+Date.now(), hID = 'h'+Date.now();
        wrap.innerHTML = `
            <div class="thinking-wrapper" style="display:none;">
                <div id="${hID}" class="thinking-header" onclick="const c=this.nextElementSibling; c.classList.toggle('open');"><span>▼</span> <span>Raciocínio</span></div>
                <div id="${tID}" class="thinking-content open"></div>
            </div>
            <div id="${mID}" class="cursor-blink"></div>`;
        b.appendChild(wrap); return { tID, mID, hID };
    }

    function appendAssistantMarkdown(markdown) {
        const safeMd = String(markdown || '').trim();
        if (!safeMd) return;
        addSimpleMsg('assistant', safeMd);
        state.messages.push({ role: 'assistant', content: safeMd });
        saveLocal();
        scroll(true);
    }

    function ensureDirectToolIntro(force = false) {
        if (!isDirectToolMode()) return;
        const lastMsg = state.messages[state.messages.length - 1];
        if (!force && lastMsg?.role === 'assistant' && lastMsg.content === DIRECT_TOOL_INTRO_MD) return;
        appendAssistantMarkdown(DIRECT_TOOL_INTRO_MD);
    }

    function formatDirectToolError(userTxt = '') {
        const trimmed = String(userTxt || '').trim();
        return [
            '⚠️ **Formato inválido para o modo direto.**',
            '',
            'Envie **um único JSON válido** contendo **ou** `sql_queries` **ou** `search_queries`.',
            'Não misture os dois no mesmo payload.',
            '',
            '### Exemplo SQL',
            '```json',
            '{',
            '  "sql_queries": [',
            '    {',
            '      "query": "SELECT id, nome FROM membros ORDER BY id DESC LIMIT 5",',
            '      "reason": "Listar os últimos membros cadastrados"',
            '    }',
            '  ]',
            '}',
            '```',
            '',
            '### Exemplo search',
            '```json',
            '{',
            '  "search_queries": [',
            '    {',
            '      "query": "Risperidona autism children site:pubmed.ncbi.nlm.nih.gov",',
            '      "reason": "Buscar evidências em crianças com TEA"',
            '    }',
            '  ]',
            '}',
            '```',
            trimmed ? `\n**Mensagem recebida:**\n\`\`\`\n${trimmed}\n\`\`\`` : '',
        ].join('\n');
    }

    function parseDirectToolRequest(userTxt) {
        const sqlQueries = extractSQLFromResponse(userTxt);
        const searchQueries = extractSearchFromResponse(userTxt, false);
        const hasSql = Array.isArray(sqlQueries) && sqlQueries.length > 0;
        const hasSearch = Array.isArray(searchQueries) && searchQueries.length > 0;
        if (hasSql && hasSearch) {
            return { ok: false, error: 'Payload misto: envie apenas sql_queries ou apenas search_queries.' };
        }
        if (hasSql) return { ok: true, type: 'sql', queries: sqlQueries };
        if (hasSearch) return { ok: true, type: 'search', queries: searchQueries };
        return { ok: false, error: 'Nenhum bloco válido de sql_queries/search_queries encontrado.' };
    }

    function formatDirectSqlResults(sqlResults) {
        const lines = ['## 🐬 Resultado da execução SQL', ''];
        sqlResults.forEach((result, index) => {
            lines.push(`### Query ${index + 1}`);
            lines.push(`- **SQL:** \`${result.query || '(sem query)'}\``);
            if (result.reason) lines.push(`- **Motivo:** ${result.reason}`);
            if (result.success === false || result.error) {
                lines.push(`- **Status:** ❌ Erro`);
                lines.push(`- **Detalhe:** ${result.error || 'Falha não especificada'}`);
                lines.push('');
                return;
            }
            const count = Array.isArray(result.data) ? result.data.length : (result.affected_rows ?? 0);
            lines.push(`- **Status:** ✅ Sucesso`);
            lines.push(`- **Registros:** ${count}`);
            if (Array.isArray(result.data) && result.data.length > 0) {
                lines.push('');
                lines.push('```json');
                lines.push(JSON.stringify(result.data.slice(0, 20), null, 2));
                lines.push('```');
            }
            lines.push('');
        });
        return lines.join('\n').trim();
    }

    function escapeDirectResultText(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/'/g, '&#39;');
    }

    function buildDirectResultBoxHtml(title, plainText, accentColor = '#475569') {
        const safeText = String(plainText || '').trim();
        return `
            <div style="border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;background:#ffffff;box-shadow:0 1px 2px rgba(15,23,42,.05);">
                <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:10px 12px;background:${accentColor}12;border-bottom:1px solid #e2e8f0;">
                    <strong style="font-size:13px;color:${accentColor};">${title}</strong>
                    <button onclick="navigator.clipboard.writeText(this.dataset.txt).then(()=>apToast('Resultado copiado!'))"
                        data-txt="${escapeDirectResultText(safeText)}"
                        style="background:#fff;border:1px solid #cbd5e1;border-radius:6px;padding:5px 10px;font-size:11px;cursor:pointer;color:#334155;white-space:nowrap;">
                        📋 Copiar resultado
                    </button>
                </div>
                <div style="padding:12px;">
                    <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px;font-family:SFMono-Regular,Consolas,Menlo,monospace;font-size:12px;line-height:1.6;color:#1e293b;white-space:pre-wrap;max-height:360px;overflow:auto;">${escapeDirectResultText(safeText)}</div>
                </div>
            </div>`;
    }

    function formatDirectSearchResults(searchData, queries) {
        const lines = ['## 🔍 Resultado da pesquisa web', ''];
        const results = Array.isArray(searchData?.results) ? searchData.results : [];
        queries.forEach((q, index) => {
            const result = results[index] || {};
            lines.push(`### Pesquisa ${index + 1}`);
            lines.push(`- **Query:** \`${q.query || q}\``);
            if (q.reason) lines.push(`- **Motivo:** ${q.reason}`);
            if (result.success === false) {
                lines.push(`- **Status:** ❌ ${result.error || 'Erro na pesquisa'}`);
                lines.push('');
                return;
            }
            const items = Array.isArray(result.results) ? result.results : [];
            lines.push(`- **Status:** ✅ ${items.length} resultado(s)`);
            lines.push('');
            if (!items.length) {
                lines.push('_Nenhum resultado encontrado._');
                lines.push('');
                return;
            }
            items.slice(0, 10).forEach((item, itemIndex) => {
                lines.push(`${itemIndex + 1}. **${item.title || 'Sem título'}**`);
                if (item.url) lines.push(`   - URL: ${item.url}`);
                if (item.snippet) lines.push(`   - Resumo: ${item.snippet}`);
            });
            lines.push('');
        });
        return lines.join('\n').trim();
    }

    async function handleDirectToolModeMessage(userTxt) {
        const parsed = parseDirectToolRequest(userTxt);
        if (!parsed.ok) {
            appendAssistantMarkdown(formatDirectToolError(userTxt));
            return false;
        }

        const ui = addAiMarkup();
        const mEl = document.getElementById(ui.mID);
        const btn = document.getElementById('ow-send');
        try {
            if (parsed.type === 'sql') {
                if (mEl) {
                    mEl.innerHTML = `<div style="padding:14px;border-left:4px solid #00bcd4;background:#e0f7fa;border-radius:8px;">🐬 Executando ${parsed.queries.length} consulta(s) SQL...</div>`;
                }
                const sqlResults = await Promise.all(parsed.queries.map(async (q, index) => {
                    const query = q.query || q;
                    const reason = q.reason || `Query #${index + 1}`;
                    const result = await executeSQLQuery(query, reason);
                    return { ...result, query, reason };
                }));
                const markdown = formatDirectSqlResults(sqlResults);
                if (mEl) {
                    mEl.classList.remove('cursor-blink');
                    mEl.innerHTML = buildDirectResultBoxHtml('🐬 Resultado da execução SQL', markdown, '#0891b2');
                }
                state.messages.push({ role: 'assistant', content: markdown });
                saveLocal();
                return true;
            }

            if (mEl) {
                mEl.innerHTML = `<div style="padding:14px;border-left:4px solid #4caf50;background:#e8f5e9;border-radius:8px;">🔍 Executando ${parsed.queries.length} pesquisa(s) web...</div>`;
            }
            const searchData = await executeWebSearch(parsed.queries);
            const markdown = formatDirectSearchResults(searchData, parsed.queries);
            if (mEl) {
                mEl.classList.remove('cursor-blink');
                mEl.innerHTML = buildDirectResultBoxHtml('🔍 Resultado da pesquisa web', markdown, '#16a34a');
            }
            state.messages.push({ role: 'assistant', content: markdown });
            saveLocal();
            return true;
        } catch (e) {
            const markdown = `❌ **Erro ao executar ${parsed.type === 'sql' ? 'sql_queries' : 'search_queries'}:**\n\n\`${e.message}\``;
            if (mEl) {
                mEl.classList.remove('cursor-blink');
                mEl.innerHTML = formatMarkdown(markdown);
            }
            state.messages.push({ role: 'assistant', content: markdown });
            saveLocal();
            return false;
        } finally {
            if (btn) {
                btn.innerText = 'Enviar';
                btn.classList.remove('stop-mode');
            }
            scroll(true);
        }
    }

    async function init() {
        console.groupCollapsed(`%c🔧 ${FILE_PREFIX} [SYSTEM] Inicialização ChatJS`, "color: #e67e22; font-weight: bold;");
        const streamPref = localStorage.getItem(KEY_STREAM);
        document.getElementById('ow-stream-check').checked = streamPref === 'false' ? false : true;

        // ── Título imediato baseado nos parâmetros GET da página ──────────────
        // (será sobrescrito pelo título completo com nome do paciente após save_chat_meta)
        (() => {
            const ctx = window.PAGE_CTX || {};

            // Extrai nome do paciente do document.title
            // Formato: "Seção - Nome Paciente - [SubSeção -] Conexão Vida"
            // O nome é sempre o segundo segmento (índice 1)
            let nome_paciente = null;
            const titleParts = document.title.split(' - ');
            if (titleParts.length >= 3) {
                nome_paciente = titleParts[1].trim() || null;
            }

            // Extrai idade do td "Data de nascimento" (ex: "13/04/2016 <> 9 anos, 10 meses e 29 dias")
            let idade_paciente = null;
            document.querySelectorAll('td').forEach(td => {
                if (idade_paciente) return;
                const txt = td.textContent || '';
                const match = txt.match(/<>\s*(.+?\banos\b.+)/);
                if (match) idade_paciente = match[1].trim();
            });

            const nome_com_idade = nome_paciente
                ? (idade_paciente ? `${nome_paciente} - ${idade_paciente}` : nome_paciente)
                : null;

            const id_pac_ctx = ctx.id_paciente || ctx.id_membro || null; // id_paciente tem prioridade
            let titulo = null;
            if      (ctx.id_atendimento) titulo = `ConexaoVida IA${nome_com_idade ? ' - ' + nome_com_idade : ''} - Atend. ${ctx.id_atendimento}`;
            else if (ctx.id_receita)     titulo = `ConexaoVida IA${nome_com_idade ? ' - ' + nome_com_idade : ''} - Receita/Laudo ${ctx.id_receita}`;
            else if (id_pac_ctx)         titulo = `ConexaoVida IA${nome_com_idade ? ' - ' + nome_com_idade : ''}`;
            else                         titulo = 'ConexaoVida IA - Geral';

            state.currentChatTitle = titulo;
            const el = document.getElementById('ow-chat-title');
            if (el) { el.innerText = titulo; el.title = titulo; }
        })();
        
        const _idAtendAnalise = window.PAGE_CTX?.id_atendimento ?? null;
        const _idReceitaAnalise = window.PAGE_CTX?.id_receita ?? null;
        const _idPacienteAnalise = window.PAGE_CTX?.id_paciente ?? window.PAGE_CTX?.id_membro ?? null;
        if (_idAtendAnalise) {
            fetchAnaliseAtendimento(_idAtendAnalise);
        } else if (!_idReceitaAnalise && _idPacienteAnalise) {
            fetchAnalisePacienteCompilada(_idPacienteAnalise);
        }
        
        if (typeof detectContexts === 'function') detectContexts();
        if (typeof loadLocal === 'function') loadLocal();
        if (typeof initPrompts === 'function') initPrompts(); 
        
        // SETUP DO MICROFONE
        if ('webkitSpeechRecognition' in window) {
            console.groupCollapsed(`${MIC_PREFIX} ▶️ Setup Inicial`);
            console.log("Status: API Detectada");
            
            recognition = new webkitSpeechRecognition();
            recognition.continuous = true; 
            recognition.interimResults = true;
            recognition.lang = 'pt-BR';

            recognition.onstart = function() {
                isRecording = true;
                hasSpeechMatch = false; 
                if(document.getElementById('ow-mic')){document.getElementById('ow-mic').classList.add('recording');}
                console.groupCollapsed(`${MIC_PREFIX} ▶️ Iniciando Gravação (Ouvindo...)`);
                console.log("Status: Ativo");
                console.groupEnd();
            };

            recognition.onend = function() {
                if (isRecording && !manualStop) {
                    console.groupCollapsed(`${MIC_PREFIX} 🔄 Reiniciando (Keep-Alive)...`);
                    try { recognition.start(); } catch(e) {}
                    console.groupEnd();
                    return; 
                }

                isRecording = false;
                document.getElementById('ow-mic').classList.remove('recording');
                
                if (!hasSpeechMatch) {
                    console.groupCollapsed(`${MIC_PREFIX} ⚠️ Gravação Encerrada (Sem Áudio)`);
                    console.warn("Nenhum texto foi retornado pela API.");
                    alert("⚠️ NENHUM ÁUDIO DETECTADO\n\nTente falar mais próximo ao microfone.");
                    console.groupEnd();
                } else {
                    console.groupCollapsed(`${MIC_PREFIX} ⏹️ Gravação Encerrada (Sucesso)`);
                    console.log("Status: Parado");
                    console.groupEnd();
                }
            };
            
            recognition.onerror = function(event) {
                console.groupCollapsed(`${MIC_PREFIX} ⚠️ Erro Detectado`);
                console.error("Código de Erro:", event.error);
                
                if (event.error === 'not-allowed' || event.error === 'service-not-allowed' || event.error === 'audio-capture') {
                    if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
                        console.groupCollapsed("🛠️ Diagnóstico de Hardware (Detalhes)");
                        navigator.mediaDevices.enumerateDevices().then(devices => {
                            const hasMic = devices.some(device => device.kind === 'audioinput');
                            if (!hasMic) alert("⚠️ NENHUM MICROFONE DETECTADO!\n\nNenhum dispositivo de entrada encontrado.");
                            else alert("⚠️ ACESSO AO MICROFONE BLOQUEADO!\n\nVerifique as permissões do navegador.");
                        }).catch(err => console.warn("Falha:", err));
                        console.groupEnd();
                    } else { alert("⚠️ Erro de permissão do microfone."); }
                }
                isRecording = false;
                document.getElementById('ow-mic').classList.remove('recording');
                console.groupEnd();
            };

            recognition.onresult = function(event) {
                let finalTranscript = '';
                for (let i = event.resultIndex; i < event.results.length; ++i) {
                    if (event.results[i].isFinal) finalTranscript += event.results[i][0].transcript;
                }
                if (finalTranscript) {
                    hasSpeechMatch = true; 
                    const inp = document.getElementById('ow-input');
                    if (inp.value.indexOf('[DITADO CLINICO]') === -1) inp.value = "[DITADO CLINICO]: " + inp.value;
                    inp.value += finalTranscript + " ";
                    inp.scrollTop = inp.scrollHeight;
                    
                    console.groupCollapsed(`${MIC_PREFIX} 📝 Texto Detectado`);
                    console.log("Transcript:", finalTranscript);
                    console.groupEnd();
                }
            };
            console.groupEnd();
        } else {
            document.getElementById('ow-mic').style.display = 'none';
            console.warn(`${FILE_PREFIX} ⚠️ Web Speech API não suportada neste navegador.`);
        }

        // BUSCA DE MODELOS (OLLAMA + SIMULADOR)
        const sel = document.getElementById('ow-model-sel');
        sel.innerHTML = '<option value="">Buscando modelos...</option>';
        try {
            // 1. Testa os modelos do Ollama Local
            let ollamaModels = [];
            try {
                const r = await fetch(PROXY_URL, { method: 'POST', body: JSON.stringify({endpoint:'/api/tags', method:'GET'}) });
                const rawText = await r.text();
                const res = JSON.parse(rawText);
                if (res && res.models && Array.isArray(res.models)) ollamaModels = res.models;
            } catch(e) { console.warn("Ollama offline ou indisponível:", e); }

            // 2. Testa o servidor remoto (ChatGPT Simulator)
            let isSimulatorOnline = false;
            try {
                const simRes = await fetch("<?php echo $_SERVER['PHP_SELF']; ?>?action=ping_simulator");
                const simData = await simRes.json();
                isSimulatorOnline = simData.online === true;
            } catch(e) { console.warn("Simulador offline:", e); }

            // 3. Monta a lista final combinada
            let finalModels = [];
            
            // Adiciona o Simulador no topo
            if (isSimulatorOnline) {
                finalModels.push({ name: 'ChatGPT Simulator', displayName: '✨ ChatGPT Simulator' });
            } else {
                finalModels.push({ name: 'ChatGPT Simulator (Offline)', displayName: '❌ ChatGPT Simulator (Offline)', disabled: true });
            }
            finalModels.push({ name: DIRECT_TOOL_MODEL, displayName: DIRECT_TOOL_MODEL_LABEL });
            
            // Adiciona os modelos locais
            finalModels = finalModels.concat(ollamaModels);

            // 4. Constrói o HTML do Select
            if (finalModels.length > 0) {
                sel.innerHTML = '';
                
                let savedModel = localStorage.getItem(KEY_MODEL);
                
                // Trata a seleção padrão: se o simulador caiu, muda para o primeiro do Ollama (se houver)
                if (savedModel === 'ChatGPT Simulator' && !isSimulatorOnline && ollamaModels.length > 0) {
                    savedModel = ollamaModels[0].name;
                }
                if (!savedModel) {
                    savedModel = isSimulatorOnline ? 'ChatGPT Simulator' : (ollamaModels.length > 0 ? ollamaModels[0].name : '');
                }
                
                localStorage.setItem(KEY_MODEL, savedModel);

                finalModels.forEach(m => {
                    const opt = document.createElement('option'); 
                    opt.value = m.name; 
                    opt.innerText = m.displayName || m.name; 
                    if (m.disabled) opt.disabled = true; // Desativa opções offline
                    if (savedModel === m.name && !m.disabled) opt.selected = true;
                    sel.appendChild(opt);
                });
                console.log(`${FILE_PREFIX} ✅ ${finalModels.length} modelos processados (Simulador: ${isSimulatorOnline ? 'ON' : 'OFF'}, Ollama: ${ollamaModels.length}).`);
            } else { 
                sel.innerHTML = '<option value="">Todos os Servidores Offline</option>'; 
            }
            
            sel.onchange = () => {
                if(!sel.options[sel.selectedIndex].disabled) {
                    localStorage.setItem(KEY_MODEL, sel.value);
                    updateInputPlaceholderForModel();
                    if (isDirectToolMode()) {
                        ensureDirectToolIntro(true);
                    }
                }
            };
            updateInputPlaceholderForModel();
            if (isDirectToolMode()) {
                ensureDirectToolIntro();
            }
        } catch(e) { 
            sel.innerHTML = '<option value="">Erro Crítico ao buscar modelos</option>'; 
            console.error(e);
        } 
        console.groupEnd();
    }

    function updateInputPlaceholderForModel() {
        const input = document.getElementById('ow-input');
        if (!input) return;
        input.placeholder = isDirectToolMode()
            ? 'Cole um JSON com sql_queries ou search_queries...'
            : 'Pergunte algo...';
    }
    
    // [FIX 7.7] HANDLER SEGURO DO CLIQUE
    if(document.getElementById('ow-mic')){
        document.getElementById('ow-mic').onclick = function() {
            if (!recognition) {
                alert("Seu navegador não suporta reconhecimento de voz.");
                return;
            }
            if (isRecording) {
                manualStop = true; // [FIX 8.5] Sinaliza parada intencional
                recognition.stop();
            } else {
                try {
                    manualStop = false;
                    recognition.start();
                } catch(e) {
                    console.error(`${FILE_PREFIX} Erro ao tentar iniciar gravação:`, e);
                }
            }
        };
    }

    async function apiCallStream(endpoint, method, data, onChunk, signal, retryCount = 0) {
        const isStream = data.stream === true;
        const reqId = Date.now().toString().slice(-4);
        console.groupCollapsed(`%c🚀 ${FILE_PREFIX}[REQ-${reqId}] Interaction (${isStream ? 'Stream' : 'Static'})`, "color: #007acc; font-weight: bold;");
        if(data.messages) {
           const lastMsg = data.messages[data.messages.length - 1].content.split(USER_SEP).pop().trim();
           console.log(`%c${FILE_PREFIX} ❓ QUESTION:`, "color: #1e88e5; font-weight: bold;", lastMsg);
        }
        console.log(`%c${FILE_PREFIX} 📦 JSON REAL ENVIADO À LLM (${PROXY_URL}):`, "color: #ef6c00; font-weight: bold;", data);
        
        try {
            const response = await fetch(PROXY_URL, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ endpoint, method, data }), signal });
            
            // [FIX 9.1] Verifica status HTTP antes de processar
            if (!response.ok) {
                const statusText = response.statusText || 'Unknown Error';
                console.error(`${FILE_PREFIX} ❌ HTTP ${response.status}: ${statusText}`);
                console.groupEnd();
                
                if (response.status === 503) {
                    throw new Error('503 Service Unavailable');
                } else if (response.status === 502) {
                    throw new Error('502 Bad Gateway');
                } else if (response.status === 504) {
                    throw new Error('504 Gateway Timeout');
                } else if (response.status >= 500) {
                    throw new Error(`${response.status} ${statusText}`);
                }
            }
            
            if (!isStream) {
                const raw = await response.text();
                let res;
                try { res = JSON.parse(raw); } catch(e) { 
                    // [FIX 9.1] DETECÇÃO DE ERRO 503 - LANÇA EXCEÇÃO PARA RECOVERY
                    if (raw.trim().startsWith('<') || raw.includes('503') || raw.includes('Service Unavailable')) {
                        console.error(`${FILE_PREFIX} ❌ ERRO HTML/503 DETECTADO:`, raw);
                        console.groupEnd();
                        throw new Error('503 Service Unavailable');
                    }
                    
                    console.error(`${FILE_PREFIX} Erro JSON Response:`, raw === "" ? "[VAZIO]" : raw); 
                    console.groupEnd();
                    throw new Error("Erro JSON: " + (raw === "" ? "Resposta Vazia" : raw.substring(0,50)));
                }
                
                if (res.debug_sql_data) {
                    console.groupCollapsed(`%c${FILE_PREFIX} 🐬 SQL EXECUTADO (Static Mode)`, "color: #00d2ff; font-weight: bold;");
                    console.dir(res.debug_sql_data);
                    console.groupEnd();
                }
                
                //const reply = res.choices?.[0]?.message?.content || "(Sem conteúdo)";
                let reply = "";
                // PARSER HÍBRIDO: Aceita Ollama (choices) ou Simulator (type/content)
                if (res.choices && res.choices[0].message) {
                    reply = res.choices?.[0]?.message?.content || "(Sem conteúdo)";
                } else if (res.type === 'html' || res.type === 'status') {
                    reply = res.content || "";
                } else if (res.html) { // Caso stream: false do simulador
                    reply = res.html;
                }
                
                console.log(`%c${FILE_PREFIX} 🤖 RESPONSE:`, "color: #2e7d32; font-weight: bold;", reply);
                console.log(`%c${FILE_PREFIX} 📥 JSON REAL RECEBIDO:`, "color: #6a1b9a; font-weight: bold;", res);
                onChunk(res); console.groupEnd(); return true;
            }

            console.groupCollapsed(`%c${FILE_PREFIX} 🌊 Stream Flow`, "color: #9b59b6;");
            const reader = response.body.getReader();
            const decoder = new TextDecoder("utf-8");
            let buffer = ''; let fullText = '';
            
            let isT = false; 

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                const chunk = decoder.decode(value, { stream: true });
                // -----------------------------------------------------
                // 👉 ADICIONA ESTE LOG AQUI PARA VER O TEXTO BRUTO QUE CHEGA
                // -----------------------------------------------------
                console.log(`%c🔎 CHUNK BRUTO RECEBIDO:`, "color: #ff9800; font-weight: bold;", chunk);
                buffer += chunk;
                const lines = buffer.split('\n');
                buffer = lines.pop();
                for (const line of lines) {
                    const l = line.trim();
                    if (l === 'data: [DONE]') continue;
                    if (l.startsWith('data: ')) {
                        try {
                            const json = JSON.parse(l.substring(6));
                            if (json.debug_sql_data) { console.log(`%c${FILE_PREFIX} 🐬 SQL RESULTADO (Do PHP):`, "color: #00d2ff; font-weight: bold;", json.debug_sql_data); continue; }
                            
                            if (json.js_log) {
                                console.group(`%c${FILE_PREFIX} 📤 ${json.js_log.label}`, "color: #4caf50; background: #e8f5e9; padding: 2px 4px; border-radius: 3px; font-weight: bold;");
                                console.dir(json.js_log.data);
                                console.groupEnd();
                                continue;
                            }

                            const txt = json.choices?.[0]?.delta?.content || "";
                            
                            if (txt.includes('<think>')) isT = true;
                            if (txt.includes('</think>')) {
                                isT = false;
                                const openThink = document.querySelector('.thinking-content.open');
                                if(openThink) openThink.classList.remove('open');
                            }
                            if (!isT && txt.trim().length > 0) {
                                const openThink = document.querySelector('.thinking-content.open');
                                if(openThink) openThink.classList.remove('open');
                            }

                            fullText += txt; onChunk(json);
                        } catch (e) {
                        }
                    } else if (l.startsWith('{')) { 
                        try { 
                            const jsonObj = JSON.parse(l);
                            
                            // 👉 Captura o chat_id e guarda no estado GLOBAL da aplicação
                            if (jsonObj.type === 'chat_id' && jsonObj.content) {
                                data.chat_id = jsonObj.content;
                                if (typeof state !== 'undefined') state.currentChatId = jsonObj.content;
                                console.log(`%c📌 CHAT_ID GUARDADO: ${data.chat_id}`, "color: #e91e63; font-weight: bold;");
                            }
                    
                            // 👉 Captura o evento "finish" que traz URL e Título
                            if (jsonObj.type === 'finish' && jsonObj.content && jsonObj.content.chat_id) {
                                // 1. Atualiza imediatamente o pacote de dados atual
                                data.chat_id = jsonObj.content.chat_id;
                                if (jsonObj.content.url) data.url = jsonObj.content.url; 
                                
                                // 2. Atualiza o estado global da aplicação
                                if (typeof state !== 'undefined') {
                                    state.currentChatId = jsonObj.content.chat_id;
                                    if (jsonObj.content.url) state.currentChatUrl = jsonObj.content.url;
                                    
                                    // Título gerido pelo servidor (save_chat_meta) — não sobrescrever com título da LLM
                                    
                                    // Força a gravação no localStorage para não perder o contexto
                                    if (typeof saveLocal === 'function') saveLocal();
                                    if (typeof saveChatMetaToDatabase === 'function') saveChatMetaToDatabase();
                                }
                                
                                console.log(`%c🔗 CONTEXTO SALVO: ID [${data.chat_id}] | Título [${state.currentChatTitle || 'N/A'}]`, "color: #9c27b0; font-weight: bold;");
                            }

                    
                            // Intercetar e exibir erros da LLM
                            if (jsonObj.type === 'error' && jsonObj.content) {
                                const errorText = `❌ **Erro do Servidor/LLM:**\n\`\`\`\n${jsonObj.content}\n\`\`\``;
                                console.error(`%c❌ ERRO DA LLM INTERCETADO:`, "color: #f44336; font-weight: bold;", jsonObj.content);
                                fullText = errorText;
                                jsonObj.type = 'markdown';
                                jsonObj.content = errorText;
                            }
                    
                            onChunk(jsonObj); 
                    
                            // Atualiza o texto acumulado
                            if (jsonObj.type === 'markdown' || jsonObj.type === 'html') {
                                fullText = jsonObj.content || fullText;
                            }
                        } catch(e) {} 
                    }
                }
            }
            if (fullText.trim() === "" && document.querySelector('.thinking-content.open')) {
                onChunk({choices:[{delta:{content:"\n\n⚠️ *A LLM recebeu os dados, mas não gerou resposta de texto.* Verifique o console (F12) para ver o que foi enviado."}}]});
            }
            console.groupEnd(); 
            console.log(`%c${FILE_PREFIX} 🤖 FINAL RESPONSE:`, "color: #2e7d32; font-weight: bold;", fullText); 
            console.groupEnd(); 
            
            
            // -----------------------------------------------------
            // LOOP DO AGENTE (DETECÇÃO E EXECUÇÃO SQL)
            // -----------------------------------------------------
            const isChatGPTMode = (data.model === 'ChatGPT Simulator') || Session.isChatGPT();

            // Extrai a pergunta real de uma mensagem user (mesma lógica do LOG no topo)
            const _extractQ = (c) => {
                if (!c) return null;
                // ✅ Método principal: USER_SEP — idêntico ao split do log ❓ QUESTION
                if (c.includes(USER_SEP)) {
                    const q = c.split(USER_SEP).pop().trim();
                    if (q && q !== 'Reexecução Manual') return q;
                }
                // ✅ Método 2: após [FIM_TEXTO_COLADO] — remove USER_SEP residual se houver
                if (c.includes('[FIM_TEXTO_COLADO]')) {
                    let q = c.split('[FIM_TEXTO_COLADO]').pop().trim();
                    if (q.startsWith(USER_SEP)) q = q.slice(USER_SEP.length).trim();
                    if (q && q !== 'Reexecução Manual') return q;
                }
                // ✅ Método 3: mensagem simples sem bloco de contexto SQL
                if (!c.includes('[INICIO_TEXTO_COLADO]') && !c.includes('RESULTADOS DAS CONSULTAS SQL')) {
                    const q = c.trim();
                    if (q && q !== 'Reexecução Manual') return q;
                }
                return null;
            };

            const originalQuestion = currentUserQuestion || Session.question || (() => {
                // 1. state.messages — histórico completo, nunca sobrescrito pelo loop SQL
                const stateMsgs = (typeof state !== 'undefined' ? state.messages : []) || [];
                for (let i = stateMsgs.length - 1; i >= 0; i--) {
                    if (stateMsgs[i].role !== 'user') continue;
                    const q = _extractQ(stateMsgs[i].content);
                    if (q) return q;
                }
                // 2. data.messages — fallback (pode estar sobrescrito no modo ChatGPT)
                const msgs = data.messages || [];
                for (let i = msgs.length - 1; i >= 0; i--) {
                    if (msgs[i].role !== 'user') continue;
                    const q = _extractQ(msgs[i].content);
                    if (q) return q;
                }
                return '';
            })();

            console.groupCollapsed(
                `%c🎯 originalQuestion (${originalQuestion.length} chars)`,
                'color: #e91e63; font-weight: bold;'
            );
            console.log(originalQuestion);
            console.groupEnd();

            const sqlResultContext = await detectAndExecuteSQL(fullText, originalQuestion, "", null);

            if (sqlResultContext !== false) {
                onChunk({ type: 'status', content: '🧠 Analisando os resultados do banco de dados...' });

                if (typeof state !== 'undefined') {
                    if (state.currentChatId) data.chat_id = state.currentChatId;
                    if (state.currentChatUrl) data.url    = state.currentChatUrl;
                }

                if (isChatGPTMode) {
                    data.messages = [{ role: 'user', content: sqlResultContext }];
                } else {
                    data.messages.push({ role: 'assistant', content: fullText });
                    data.messages.push({ role: 'system',    content: sqlResultContext });
                }

                console.log(`%c🔄 LOOP AGENTE: [${isChatGPTMode ? 'ChatGPT — msg única' : 'Ollama — histórico completo'}]`, "color: #3f51b5; font-weight: bold;");
                console.log(`   ID: ${data.chat_id} | URL: ${data.url}`);

                return await apiCallStream(endpoint, method, data, onChunk, signal, retryCount);
            }
            // -----------------------------------------------------

            return true;
                } catch (err) { 
            if (err.name === 'AbortError') {
                 console.groupEnd(); 
                 return false; 
            }
            if (err.name !== 'AbortError') { console.error(`${FILE_PREFIX} ❌ Erro:`, err); onChunk({ error: err.message }); } 

            // 👉 NOVO: Lógica de Auto-Retry para recuperar de falhas de rede / 503
            const isRecoverableError = err.message.includes('503') || err.message.includes('502') || err.message.includes('504') || err.message.includes('fetch');
            
            // Verifica se é um erro recuperável, se temos o chat_id guardado e se não excedemos o limite
            if (isRecoverableError && data.chat_id && retryCount < MAX_RETRIES) {
                const isSimulatorModel = (data.model === 'ChatGPT Simulator') && data.chat_id;

                if (isSimulatorModel) {
                    // Simulator: NÃO reenvia — faz poll no /api/sync para buscar a resposta em andamento
                    console.warn(`%c⏳ [Simulator] Timeout. Verificando resposta pendente...`, 'color:#ff9800;font-weight:bold;');
                    onChunk({ type: 'status', content: '⏳ Reconectando ao servidor...' });

                    await new Promise(r => setTimeout(r, 4000));

                    try {
                        const pollRes = await fetch('?action=sync_simulator', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ chat_id: data.chat_id, url: data.url })
                        });
                        const pollData = await pollRes.json();

                        if (pollData?.success && Array.isArray(pollData?.chat?.messages)) {
                            const lastAI = [...pollData.chat.messages].reverse()
                                              .find(m => m.role === 'assistant');

                            if (lastAI?.content) {
                                console.log(`%c✅ [Simulator] Resposta recuperada via poll!`, 'color:#4caf50;font-weight:bold;');
                                onChunk({ type: 'markdown', content: lastAI.content });
                                onChunk({ type: 'finish',   content: {
                                    chat_id: data.chat_id,
                                    url:     pollData.chat.url  || data.url,
                                    title:   pollData.chat.title || ''
                                }});
                                return true; // encerra sem duplicar
                            }
                        }
                    } catch(pollErr) {
                        console.warn('[Simulator] Poll falhou:', pollErr);
                    }

                    // Python ainda não terminou — avisa o usuário sem reenviar
                    onChunk({ type: 'markdown', content:
                        `⚠️ **A conexão caiu enquanto o ChatGPT processava.**\n\n` +
                        `Aguarde alguns segundos e recarregue o chat com **🔄 Novo** para ver a resposta.`
                    });
                    return false;
                }

                // Ollama: retry normal (sem risco de duplicar mensagem)
                onChunk({ type: 'status', content: `Reconectando... (${retryCount + 1}/${MAX_RETRIES})` });
                await new Promise(r => setTimeout(r, 3000));
                return await apiCallStream(endpoint, method, data, onChunk, signal, retryCount + 1);
            }

            // Se não for possível recuperar, mostra o erro final
            console.error(`${FILE_PREFIX} ❌ Erro:`, err); 
            onChunk({ error: err.message }); 
            console.groupEnd(); 
            return false; 
        }
    }

    async function installModel(retryCount = 0) {
        const inp = document.getElementById('sb-model-url');
        const btn = document.getElementById('sb-btn-install');
        const pBar = document.getElementById('sb-prog-bar');
        const pWrap = document.getElementById('sb-prog-wrap');
        const status = document.getElementById('sb-status');

        let rawUrl = inp.value.trim();
        if (!rawUrl) return;
        if (rawUrl.includes('huggingface.co/')) { rawUrl = rawUrl.replace(/^https?:\/\//, '').replace('huggingface.co/', 'hf.co/'); }

        if (retryCount === 0) {
            btn.disabled = true; 
            pWrap.style.display = 'block'; 
            pBar.style.width = '0%'; 
            status.innerText = "Iniciando download...";
            console.groupCollapsed(`%c⬇️ ${FILE_PREFIX} [INSTALL] Installing Model: ${rawUrl}`, "color: #e91e63; font-weight: bold;");
        }

        let isSuccess = false;
        await apiCallStream('/api/pull', 'POST', { name: rawUrl, stream: true }, (chunk) => {
            if (chunk.status) {
                let msg = chunk.status;
                if (chunk.total && chunk.completed) { const pct = Math.round((chunk.completed / chunk.total) * 100); pBar.style.width = pct + '%'; msg += ` (${pct}%)`; }
                status.innerText = msg;
                if (chunk.status === 'success') { 
                    isSuccess = true;
                    status.innerText = "Instalação concluída!"; 
                    pBar.style.background = '#4caf50'; 
                    console.log(`${FILE_PREFIX} ✅ Instalação Concluída`, "color: #4caf50; font-weight:bold;"); 
                    setTimeout(() => { alert("Modelo instalado com sucesso!"); location.reload(); }, 1000); 
                    btn.disabled = false;
                }
            }
            if (chunk.error) { status.innerText = "Erro: " + chunk.error; pBar.style.background = '#f44336'; console.error(`${FILE_PREFIX} Erro na instalação:`, chunk.error); }
        });
        
        if (!isSuccess && status.innerText !== "Instalação concluída!") {
             if (retryCount < 10) {
                 const next = retryCount + 1;
                 status.innerText = `⚠️ Conexão instável. Retomando download (Tentativa ${next}/10)...`;
                 pBar.style.background = '#ff9800';
                 console.warn(`${FILE_PREFIX} Instalação caiu. Tentando novamente (${next}/10)...`);
                 
                 setTimeout(() => installModel(next), 3000);
                 return;
             }
             
             status.innerText = "❌ Falha após várias tentativas. Verifique sua rede.";
             pBar.style.background = '#f44336';
             btn.disabled = false;
             console.groupEnd();
        } else if (isSuccess) {
            console.groupEnd();
        }
    }

    // ==========================================
    // [FIX 9.3] SISTEMA DE DETECÇÃO E EXECUÇÃO AUTOMÁTICA DE SQL
    // ==========================================
    
    function extractSQLFromResponse(text) {
        if (!text || typeof text !== 'string') return null;

        // [FIX] markdownify (Python) escapa underscores: sql_queries → sql\_queries
        // Normaliza antes de qualquer verificação ou parse
        text = text.replace(/\\_/g, '_').replace(/\\\*/g, '*');

        if (text.includes('resultado do SQL executado') || text.includes('execute o seguinte comando')) {
            console.log(`${FILE_PREFIX} 🐬 SQL já foi executado pelo backend PHP, pulando frontend execution`);
            return null;
        }

        if (!text.includes('"sql_queries"')) {
            return null;
        }
        
        function sanitizeJSON(jsonStr) {
            return jsonStr
                .replace(/[\u2018\u2019]/g, "'")  
                .replace(/[\u201C\u201D]/g, '"')  
                .replace(/[\u00A0]/g, " ") // Corrige espaços invisíveis que quebram o Parse
                .trim();
        }
        
        try {
            // 1. Tenta primeiro encontrar um bloco Markdown (Padrão seguro)
            const markdownMatch = text.match(/```(?:json)?\s*(\{[\s\S]*?"sql_queries"[\s\S]*?\})\s*```/);
            if (markdownMatch) {
                const json = JSON.parse(sanitizeJSON(markdownMatch[1]));
                if (json.sql_queries && Array.isArray(json.sql_queries) && json.sql_queries.length > 0) {
                    console.log(`${FILE_PREFIX} 🐬 SQL extraído (Markdown)`);
                    return json.sql_queries;
                }
            }
            
            // 2. Se não for Markdown, varre o texto à procura do bloco JSON equilibrado
            let startIndex = text.indexOf('{');
            while (startIndex !== -1) {
                let candidate = text.substring(startIndex);
                let closeIndex = findMatchingBrace(candidate);
                
                if (closeIndex > 0) {
                    let jsonStr = candidate.substring(0, closeIndex + 1);
                    if (jsonStr.includes('"sql_queries"')) {
                        try {
                            const json = JSON.parse(sanitizeJSON(jsonStr));
                            if (json.sql_queries && Array.isArray(json.sql_queries)) {
                                console.log(`${FILE_PREFIX} 🐬 SQL extraído (Inline)`);
                                return json.sql_queries;
                            }
                        } catch (e) {
                            console.warn(`${FILE_PREFIX} ⚠️ Encontrou um bloco, mas falhou ao extrair:`, e.message);
                        }
                    }
                }
                // Se falhou, procura a próxima chave aberta
                startIndex = text.indexOf('{', startIndex + 1);
            }
        } catch(e) {
            console.log(`%c${FILE_PREFIX} ℹ️ Ignorado: Texto continha "sql_queries", mas não formava um JSON válido.`, "color: #9e9e9e;");
        }
        
        return null; 
    }
    
    // Função auxiliar para encontrar a chave de fechamento correspondente
    function findMatchingBrace(str) {
        let depth = 0;
        let inString = false;
        let escapeNext = false;
        
        for (let i = 0; i < str.length; i++) {
            const char = str[i];
            
            if (escapeNext) {
                escapeNext = false;
                continue;
            }
            
            if (char === '\\') {
                escapeNext = true;
                continue;
            }
            
            if (char === '"' && !escapeNext) {
                inString = !inString;
                continue;
            }
            
            if (inString) continue;
            
            if (char === '{') {
                depth++;
            } else if (char === '}') {
                depth--;
                if (depth === 0) {
                    return i;
                }
            }
        }
        
        return -1;
    }

    // ==========================================
    // PESQUISA WEB VIA BROWSER.PY (search_queries)
    // ==========================================

    function extractSearchFromResponse(text, autoExecMode) {
        if (!text || typeof text !== 'string') return null;
        text = text.replace(/\\_/g, '_').replace(/\\\*/g, '*');

        // Detecta ambos os formatos: search_queries (correto) e pesquisa_query (legado/LLM inventado)
        const hasSearchQueries  = text.includes('"search_queries"');
        const hasPesquisaQuery  = text.includes('"pesquisa_query"');
        if (!hasSearchQueries && !hasPesquisaQuery) return null;

        if (autoExecMode) {
            const stripped = text.trim();
            const isJsonOnly     = /^\{[\s\S]*\}$/.test(stripped);
            const isMarkdownOnly = /^```(?:json)?\s*\{[\s\S]*\}\s*```$/.test(stripped);
            if (!isJsonOnly && !isMarkdownOnly) return null;
        }

        function sanitize(s) {
            return s.replace(/[\u2018\u2019]/g,"'").replace(/[\u201C\u201D]/g,'"').replace(/[\u00A0]/g,' ').trim();
        }

        function decodeLooseJsonString(value) {
            let v = sanitize(String(value || ''));
            v = v.replace(/,$/, '').trim();
            while ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) {
                v = v.slice(1, -1).trim();
            }
            return v
                .replace(/\\"/g, '"')
                .replace(/\\n/g, '\n')
                .replace(/\\t/g, '\t')
                .replace(/\\\\/g, '\\')
                .trim();
        }

        // Normaliza: converte pesquisa_query (string) → search_queries (array de objetos)
        function normalizeToArray(j) {
            // Formato correto: search_queries é array
            if (j.search_queries && Array.isArray(j.search_queries) && j.search_queries.length > 0) {
                return j.search_queries;
            }
            // Formato legado: pesquisa_query é string única
            if (j.pesquisa_query && typeof j.pesquisa_query === 'string') {
                return [{ query: j.pesquisa_query, reason: 'Pesquisa solicitada' }];
            }
            // Formato legado: pesquisa_query é array de strings
            if (j.pesquisa_query && Array.isArray(j.pesquisa_query)) {
                return j.pesquisa_query.map(q => typeof q === 'string' ? { query: q, reason: 'Pesquisa solicitada' } : q);
            }
            return null;
        }

        function extractLoosePairs(rawText) {
            const compact = sanitize(String(rawText || ''))
                .replace(/```(?:json)?/gi, '')
                .replace(/```/g, '')
                .replace(/\r/g, '');

            const matches = [...compact.matchAll(/{[\s\S]*?"query"\s*:\s*([\s\S]*?)(?:,\s*"reason"\s*:\s*([\s\S]*?))?\s*}/gi)];
            if (matches.length > 0) {
                const parsed = matches
                    .map(([, rawQuery, rawReason]) => ({
                        query:  decodeLooseJsonString(rawQuery),
                        reason: decodeLooseJsonString(rawReason || 'Pesquisa solicitada'),
                    }))
                    .filter(item => item.query);
                if (parsed.length > 0) return parsed;
            }

            const legacy = compact.match(/"pesquisa_query"\s*:\s*([\s\S]*?)(?=,\s*"[a-z_]+\"\s*:|\s*}\s*$)/i);
            if (legacy?.[1]) {
                const query = decodeLooseJsonString(legacy[1]);
                if (query) return [{ query, reason: 'Pesquisa solicitada' }];
            }

            return null;
        }

        function extractLoosePairs(rawText) {
            const compact = sanitize(String(rawText || ''))
                .replace(/```(?:json)?/gi, '')
                .replace(/```/g, '')
                .replace(/\r/g, '');

            const matches = [...compact.matchAll(/{[\s\S]*?"query"\s*:\s*([\s\S]*?)(?:,\s*"reason"\s*:\s*([\s\S]*?))?\s*}/gi)];
            if (matches.length > 0) {
                const parsed = matches
                    .map(([, rawQuery, rawReason]) => ({
                        query:  decodeLooseJsonString(rawQuery),
                        reason: decodeLooseJsonString(rawReason || 'Pesquisa solicitada'),
                    }))
                    .filter(item => item.query);
                if (parsed.length > 0) return parsed;
            }

            const legacy = compact.match(/"pesquisa_query"\s*:\s*([\s\S]*?)(?=,\s*"[a-z_]+\"\s*:|\s*}\s*$)/i);
            if (legacy?.[1]) {
                const query = decodeLooseJsonString(legacy[1]);
                if (query) return [{ query, reason: 'Pesquisa solicitada' }];
            }

            return null;
        }

        function extractLoosePairs(rawText) {
            const compact = sanitize(String(rawText || ''))
                .replace(/```(?:json)?/gi, '')
                .replace(/```/g, '')
                .replace(/\r/g, '');

            const matches = [...compact.matchAll(/{[\s\S]*?"query"\s*:\s*([\s\S]*?)(?:,\s*"reason"\s*:\s*([\s\S]*?))?\s*}/gi)];
            if (matches.length > 0) {
                const parsed = matches
                    .map(([, rawQuery, rawReason]) => ({
                        query:  decodeLooseJsonString(rawQuery),
                        reason: decodeLooseJsonString(rawReason || 'Pesquisa solicitada'),
                    }))
                    .filter(item => item.query);
                if (parsed.length > 0) return parsed;
            }

            const legacy = compact.match(/"pesquisa_query"\s*:\s*([\s\S]*?)(?=,\s*"[a-z_]+\"\s*:|\s*}\s*$)/i);
            if (legacy?.[1]) {
                const query = decodeLooseJsonString(legacy[1]);
                if (query) return [{ query, reason: 'Pesquisa solicitada' }];
            }

            return null;
        }

        function extractLoosePairs(rawText) {
            const compact = sanitize(String(rawText || ''))
                .replace(/```(?:json)?/gi, '')
                .replace(/```/g, '')
                .replace(/\r/g, '');

            const matches = [...compact.matchAll(/{[\s\S]*?"query"\s*:\s*([\s\S]*?)(?:,\s*"reason"\s*:\s*([\s\S]*?))?\s*}/gi)];
            if (matches.length > 0) {
                const parsed = matches
                    .map(([, rawQuery, rawReason]) => ({
                        query:  decodeLooseJsonString(rawQuery),
                        reason: decodeLooseJsonString(rawReason || 'Pesquisa solicitada'),
                    }))
                    .filter(item => item.query);
                if (parsed.length > 0) return parsed;
            }

            const legacy = compact.match(/"pesquisa_query"\s*:\s*([\s\S]*?)(?=,\s*"[a-z_]+\"\s*:|\s*}\s*$)/i);
            if (legacy?.[1]) {
                const query = decodeLooseJsonString(legacy[1]);
                if (query) return [{ query, reason: 'Pesquisa solicitada' }];
            }

            return null;
        }

        function extractLoosePairs(rawText) {
            const compact = sanitize(String(rawText || ''))
                .replace(/```(?:json)?/gi, '')
                .replace(/```/g, '')
                .replace(/\r/g, '');

            const matches = [...compact.matchAll(/{[\s\S]*?"query"\s*:\s*([\s\S]*?)(?:,\s*"reason"\s*:\s*([\s\S]*?))?\s*}/gi)];
            if (matches.length > 0) {
                const parsed = matches
                    .map(([, rawQuery, rawReason]) => ({
                        query:  decodeLooseJsonString(rawQuery),
                        reason: decodeLooseJsonString(rawReason || 'Pesquisa solicitada'),
                    }))
                    .filter(item => item.query);
                if (parsed.length > 0) return parsed;
            }

            const legacy = compact.match(/"pesquisa_query"\s*:\s*([\s\S]*?)(?=,\s*"[a-z_]+\"\s*:|\s*}\s*$)/i);
            if (legacy?.[1]) {
                const query = decodeLooseJsonString(legacy[1]);
                if (query) return [{ query, reason: 'Pesquisa solicitada' }];
            }

            return null;
        }

        // Regex para ambos os formatos
        const keyPattern = hasSearchQueries ? 'search_queries' : 'pesquisa_query';

        try {
            const mdMatch = text.match(new RegExp('```(?:json)?\\s*(\\{[\\s\\S]*?"' + keyPattern + '"[\\s\\S]*?\\})\\s*```'));
            if (mdMatch) {
                const j = JSON.parse(sanitize(mdMatch[1]));
                const result = normalizeToArray(j);
                if (result) return result;
            }
            let start = text.indexOf('{');
            while (start !== -1) {
                const candidate  = text.substring(start);
                const closeIndex = findMatchingBrace(candidate);
                if (closeIndex > 0) {
                    const jsonStr = candidate.substring(0, closeIndex + 1);
                    if (jsonStr.includes('"' + keyPattern + '"')) {
                        try {
                            const j = JSON.parse(sanitize(jsonStr));
                            const result = normalizeToArray(j);
                            if (result) return result;
                        } catch (_) {}
                    }
                }
                start = text.indexOf('{', start + 1);
            }
        } catch (_) {}

        const looseResult = extractLoosePairs(text);
        if (looseResult && looseResult.length > 0) return looseResult;
        return null;
    }

    async function executeWebSearch(queries) {
        const queryStrings = queries.map(q => q.query || q);
        const res = await fetch(`<?php echo $_SERVER['PHP_SELF']; ?>?action=web_search`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ queries: queryStrings })
        });
        return await res.json();
    }

    function formatSearchResultsForLLM(searchData, originalQuestion) {
        const sanitizePastedText = value => String(value || '')
            .replaceAll('[INICIO_TEXTO_COLADO]', '')
            .replaceAll('[FIM_TEXTO_COLADO]', '')
            .trim();

        const results = searchData.results || [];
        const sections = results.map((r, i) => {
            const lines = [`**Pesquisa ${i + 1}**: ${sanitizePastedText(r.query)}`, ''];

            if (!r.success) {
                lines.push(`**Erro**: ${sanitizePastedText(r.error)}`);
                return lines.join('\n');
            }

            const items = r.results || [];
            if (items.length === 0) {
                lines.push('Nenhum resultado encontrado.');
                return lines.join('\n');
            }

            items.forEach((item, j) => {
                lines.push(`**Resultado ${j + 1}**: ${sanitizePastedText(item.title)}`);
                lines.push(`URL: ${sanitizePastedText(item.url)}`);
                if (item.snippet) lines.push(`Resumo: ${sanitizePastedText(item.snippet)}`);
                lines.push('');
            });

            return lines.join('\n').trimEnd();
        });

        const pastedBlock = [
            '### 🔍 RESULTADOS DA PESQUISA WEB ###',
            '⚠️ IMPORTANTE: Responda APENAS em Português do Brasil.',
            'Você solicitou pesquisas na web. Aqui estão os resultados:',
            '⚠️ Ao responder, toda fonte citada deve exibir a URL explícita (link markdown ou URL completa).',
            '⚠️ Nunca escreva apenas "Fonte: Doctoralia" ou "Fonte: Instituto X" se o URL estiver presente abaixo.',
            '',
            sections.join('\n\n---\n\n')
        ].join('\n');

        return `[INICIO_TEXTO_COLADO]\n${pastedBlock}\n[FIM_TEXTO_COLADO]\n\n[INICIO_TEXTO_COLADO]\nCom base nesses resultados, responda à **pergunta**:\n${sanitizePastedText(originalQuestion)}\n\nNa resposta final:\n- mostre as fontes com URL explícita\n- prefira links markdown clicáveis quando possível\n- se algum resultado não tiver URL, diga claramente que veio de item sem URL fornecida\n[FIM_TEXTO_COLADO]`;
    }

    async function detectAndExecuteSearch(responseText, originalQuestion, ui, depth = 0) {
        const MAX_SEARCH_CHAIN_DEPTH = 3;
        if (responseText.includes('<') && responseText.includes('>')) {
            responseText = responseText.replace(/<br\s*\/?>/gi, '\n').replace(/<\/p>/gi, '\n').replace(/<[^>]+>/g, '');
        }

        const searchQueries = extractSearchFromResponse(responseText, true);
        if (!searchQueries || searchQueries.length === 0) return false;
        if (depth >= MAX_SEARCH_CHAIN_DEPTH) {
            console.warn(`${FILE_PREFIX} Limite de encadeamento de pesquisas atingido (${MAX_SEARCH_CHAIN_DEPTH}).`);
            return false;
        }

        const _sendBtn = document.getElementById('ow-send');
        if (_sendBtn) { _sendBtn.disabled = true; _sendBtn.classList.add('stop-mode'); }

        console.groupCollapsed(`%c${FILE_PREFIX} 🔍 Pesquisa web detectada na resposta`, "color: #4caf50; font-weight: bold; background: #e8f5e9; padding: 4px 8px; border-radius: 4px;");
        console.log(`Queries: ${searchQueries.length}`);
        searchQueries.forEach((q, i) => console.log(`[${i+1}] ${q.query} — ${q.reason}`));
        console.groupEnd();

        if (ui && ui.mID && document.getElementById(ui.mID)) {
            document.getElementById(ui.mID).innerHTML = `
                <div style="background:linear-gradient(135deg,#e8f5e9 0%,#c8e6c9 100%);border-left:4px solid #4caf50;padding:20px;border-radius:8px;margin:10px 0;">
                    <div style="display:flex;align-items:center;gap:15px;margin-bottom:15px;">
                        <div style="border:4px solid #f3f3f3;border-top:4px solid #4caf50;border-radius:50%;width:30px;height:30px;animation:spin 1s linear infinite;"></div>
                        <div>
                            <strong style="font-size:16px;color:#2e7d32;">🔍 Pesquisando na Web</strong>
                            <div style="font-size:12px;color:#666;margin-top:4px;">${searchQueries.length} pesquisa(s) em andamento</div>
                        </div>
                    </div>
                    <div style="background:rgba(255,255,255,.7);padding:12px;border-radius:6px;">
                        ${searchQueries.map((q,i) => `
                            <div style="margin-bottom:10px;padding:10px;background:white;border-radius:4px;border-left:3px solid #4caf50;">
                                <div style="font-size:13px;font-weight:500;color:#2e7d32;margin-bottom:5px;">Pesquisa ${i+1}: ${q.reason||q.query}</div>
                                <div style="font-family:monospace;font-size:11px;color:#666;background:#f5f5f5;padding:6px;border-radius:3px;">${q.query}</div>
                                <div id="search-status-${i}" style="font-size:12px;color:#ff9800;margin-top:5px;">⏳ Aguardando...</div>
                            </div>`).join('')}
                    </div>
                </div>`;
        }

        try {
            const searchData = await executeWebSearch(searchQueries);
            searchQueries.forEach((_, i) => {
                const el = document.getElementById(`search-status-${i}`);
                if (el) {
                    const r = (searchData.results || [])[i];
                    el.innerHTML = r && r.success
                        ? `<span style="color:#4caf50;">✅ ${(r.results||[]).length} resultado(s)</span>`
                        : `<span style="color:#f44336;">❌ ${r?.error||'Sem resultado'}</span>`;
                }
            });

            const contextMsg = formatSearchResultsForLLM(searchData, originalQuestion);
            state.messages.push({ role: 'user', content: contextMsg });

            // Nova chamada à LLM com os resultados
            const uiNew = addAiMarkup();
            let fullC = '';
            await apiCallStream(PROXY_URL, 'POST', {
                model:    document.getElementById('ow-model-sel')?.value || '',
                messages: state.messages,
                stream:   true,
                chat_id:  Session.chatId  || null,
                url:      Session.chatUrl || null
            }, chunk => {
                let c = '';
                if (chunk.type === 'markdown' || chunk.type === 'html') { fullC = chunk.content; c = fullC; }
                else if (chunk.choices?.[0]?.delta?.content) { c = chunk.choices[0].delta.content; fullC += c; }
                else if (chunk.type === 'finish') { const fd = chunk.content||{}; Session.setChat(fd.chat_id, fd.url, null); return; }
                if (c) { const mEl = document.getElementById(uiNew.mID); if (mEl) mEl.innerHTML = formatMarkdown(fullC); scroll(); }
            }, currentAbortController?.signal);

            const mEl = document.getElementById(uiNew.mID);
            if (mEl) { mEl.classList.remove('cursor-blink'); mEl.innerHTML = formatMarkdown(fullC); }
            if (typeof injectSearchButtons === 'function') setTimeout(() => injectSearchButtons(), 0);

            if (fullC) {
                const chainedSearchDetected = await detectAndExecuteSearch(fullC, originalQuestion, uiNew, depth + 1);
                if (chainedSearchDetected) return true;

                state.messages.push({ role: 'assistant', content: fullC });
                saveLocal();
                saveChatMetaToDatabase();
            }

        } catch(e) {
            console.error('Erro na pesquisa web:', e);
            if (ui && ui.mID) {
                const mEl = document.getElementById(ui.mID);
                if (mEl) mEl.innerText = 'Erro ao pesquisar na web: ' + e.message;
            }
        }

        if (_sendBtn) { _sendBtn.disabled = false; _sendBtn.classList.remove('stop-mode'); }
        return true;
    }

    function _attachSearchButton(el, searchQueries) {
        const messageEl = el.closest('.msg-ai');
        if (messageEl?.querySelector('.ow-search-actions-bar')) return;

        // Guarda real: evita reempacotar o mesmo bloco após novos MutationObserver events
        if (el.querySelector('.ow-search-actions-bar') || (el.previousElementSibling && el.previousElementSibling.classList.contains('ow-search-actions-bar'))) {
            return;
        }

        // Compatibilidade com wrappers já existentes de outras UIs
        if (el.parentElement?.classList?.contains('ow-code-wrapper') && el.parentElement.querySelector('.ow-search-actions-bar')) {
            return;
        }

        const wrapper = document.createElement('div');
        wrapper.className = el.tagName.toLowerCase() === 'pre' ? 'ow-code-wrapper' : '';
        if (el.tagName.toLowerCase() !== 'pre') {
            wrapper.style.cssText = 'display:block; background:#f8f9fa; border:1px solid #e0e0e0; border-radius:8px; margin-top:10px; overflow:hidden;';
            el.style.fontFamily = 'monospace';
            el.style.whiteSpace = 'pre-wrap';
            el.style.padding = '15px';
            el.style.overflowX = 'auto';
        }

        el.parentNode.insertBefore(wrapper, el);
        wrapper.appendChild(el);

        const actionBar = document.createElement('div');
        actionBar.className = 'ow-search-actions-bar';
        actionBar.style.cssText = `position:sticky;top:-20px;z-index:10;height:38px;background:#f1f3f4;
            border-bottom:1px solid #e0e0e0;border-radius:8px 8px 0 0;
            display:flex;justify-content:flex-end;align-items:center;padding:0 10px;gap:8px;`;

        const execHTML = `<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" style="vertical-align:middle"><path d="M8 5v14l11-7z"/></svg> <span style="font-size:12px;font-weight:600;vertical-align:middle">Pesquisar</span>`;
        const btnExec = document.createElement('button');
        btnExec.innerHTML = execHTML;
        btnExec.style.cssText = 'background:rgba(76,175,80,.1);border:1px solid rgba(76,175,80,.4);color:#2e7d32;cursor:pointer;padding:4px 10px;border-radius:4px;display:flex;align-items:center;gap:4px;transition:all .2s;';

        btnExec.onclick = async () => {
            btnExec.disabled = true;
            btnExec.innerHTML = `<span style="font-size:12px;color:#ff9800;font-weight:bold">⏳ Pesquisando...</span>`;
            const _owSend = document.getElementById('ow-send');
            if (_owSend) { _owSend.disabled = true; _owSend.classList.add('stop-mode'); }

            try {
                const searchData = await executeWebSearch(searchQueries);
                const lastUserMsg = [...state.messages].reverse().find(m => m.role === 'user')?.content || '';
                const contextMsg  = formatSearchResultsForLLM(searchData, lastUserMsg);
                state.messages.push({ role: 'user', content: contextMsg });

                const uiNew = addAiMarkup();
                let fullC = '';
                await apiCallStream(PROXY_URL, 'POST', {
                    model:    document.getElementById('ow-model-sel')?.value || '',
                    messages: state.messages,
                    stream:   true,
                    chat_id:  Session.chatId  || null,
                    url:      Session.chatUrl || null
                }, chunk => {
                    let c = '';
                    if (chunk.type === 'markdown' || chunk.type === 'html') { fullC = chunk.content; c = fullC; }
                    else if (chunk.choices?.[0]?.delta?.content) { c = chunk.choices[0].delta.content; fullC += c; }
                    else if (chunk.type === 'finish') { const fd = chunk.content||{}; Session.setChat(fd.chat_id, fd.url, null); return; }
                    if (c) { const mEl = document.getElementById(uiNew.mID); if (mEl) mEl.innerHTML = formatMarkdown(fullC); scroll(); }
                }, currentAbortController?.signal);

                const mEl = document.getElementById(uiNew.mID);
                if (mEl) { mEl.classList.remove('cursor-blink'); mEl.innerHTML = formatMarkdown(fullC); }
                if (typeof injectSearchButtons === 'function') setTimeout(() => injectSearchButtons(), 0);
                if (fullC) {
                    const chainedSearchDetected = await detectAndExecuteSearch(fullC, lastUserMsg, uiNew, 1);
                    if (!chainedSearchDetected) {
                        state.messages.push({ role: 'assistant', content: fullC });
                        saveLocal();
                    }
                }

            } catch(e) {
                console.error('Erro pesquisa manual:', e);
            }

            btnExec.disabled = false;
            btnExec.innerHTML = `<span style="font-size:12px;color:#2e7d32;font-weight:bold">✅ Concluído</span>`;
            setTimeout(() => { btnExec.innerHTML = execHTML; }, 3000);
            if (_owSend) { _owSend.disabled = false; _owSend.classList.remove('stop-mode'); }
        };

        actionBar.appendChild(btnExec);
        wrapper.insertBefore(actionBar, wrapper.firstChild);
    }

    function injectSearchButtons() {
        const container = document.getElementById('ow-messages') || document.body;
        container.querySelectorAll('pre, code, p, div, span').forEach(el => {
            if (el.closest('.msg-ai')?.querySelector('.ow-search-actions-bar')) return;
            if (el.querySelector('.ow-search-actions-bar')) return;
            const elText = el.textContent || '';
            if (!elText.includes('"search_queries"') && !elText.includes('"pesquisa_query"')) return;

            let target = el;
            if (el.tagName === 'CODE' && el.parentElement?.tagName === 'PRE') {
                target = el.parentElement;
                if (target.querySelector('.ow-search-actions-bar')) return;
            }

            // Só opera no elemento mais profundo
            let isDeepest = true;
            for (const child of el.children) {
                if (child.classList?.contains('ow-search-actions-bar')) continue;
                if (child.tagName === 'BR') continue;
                if (child.textContent?.includes('"search_queries"') || child.textContent?.includes('"pesquisa_query"')) { isDeepest = false; break; }
            }
            if (!isDeepest) return;
            if (el.textContent.length > 5000) return;

            const searchQueries = extractSearchFromResponse(el.textContent, false);
            if (!searchQueries || searchQueries.length === 0) return;
            _attachSearchButton(target, searchQueries);
        });
    }

    function initSearchUIObserver() {
        const chatBox = document.getElementById('ow-messages') || document.body;
        const observer = new MutationObserver(() => {
            clearTimeout(window._searchUiTimeout);
            window._searchUiTimeout = setTimeout(injectSearchButtons, 300);
        });
        observer.observe(chatBox, { childList: true, subtree: true, characterData: true });
        setTimeout(injectSearchButtons, 500);
        setTimeout(injectSearchButtons, 1500);
    }
    document.addEventListener('DOMContentLoaded', initSearchUIObserver);

    // ==========================================
    // FIM — PESQUISA WEB
    // ==========================================

    async function executeSQLQuery(query, reason) {
        try {
            const response = await fetch('<?php echo $_SERVER['PHP_SELF']; ?>?action=execute_sql', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ query, reason })
            });
            
            const result = await response.json();
            return result;
        } catch (error) {
            return {
                success: false,
                error: error.message,
                query: query,
                reason: reason
            };
        }
    }
    
    // SUBSTITUIR a função compactResult inteira:
    function compactResult(data) {
        if (!data || data.length === 0) return "(sem dados)";
        const keys = Object.keys(data[0]);
        // Para DESCRIBE: formato legível compacto
        const isDescribe = keys.includes('Field') && keys.includes('Type');
        if (isDescribe) {
            return data.map(r =>
                `${r.Field} ${r.Type}${r.Key ? ' [' + r.Key + ']' : ''}`
            ).join('\n');
        }
        // NUNCA truncar — a LLM pediu esses dados para analisar
        return JSON.stringify(data, null, 2);
    }
    function formatSQLResultsForLLM(sqlResults, originalQuestion, originalContext) {
        const sanitizePastedText = value => String(value || '')
            .replaceAll('[INICIO_TEXTO_COLADO]', '')
            .replaceAll('[FIM_TEXTO_COLADO]', '')
            .trim();

        let formattedText = '[INICIO_TEXTO_COLADO]\n\n';

        // Se já há contexto anterior (round de DESCRIBE), inclui sem repetir o header
        if (originalContext && originalContext.trim() !== '') {
            // Remove delimitadores caso o originalContext já seja um bloco anterior
            let cleanCtx = sanitizePastedText(originalContext);

            // Remove o rodapé "Com base nesses resultados..." do contexto anterior
            // para não ficar duplicado (será adicionado no fim deste bloco)
            const rodapeMarker = 'Com base nesses resultados do banco de dados, responda à **pergunta**:';
            const rodapeIdx = cleanCtx.lastIndexOf(rodapeMarker);
            if (rodapeIdx !== -1) cleanCtx = cleanCtx.substring(0, rodapeIdx).trimEnd();

            formattedText += cleanCtx + '\n\n---\n\n';
        }

        // Header apenas UMA VEZ por bloco — não repete se já há contexto anterior
        if (!originalContext || originalContext.trim() === '') {
            formattedText += '### 🔍 RESULTADOS DAS CONSULTAS SQL ###\n\n';
            formattedText += '⚠️ IMPORTANTE: Responda APENAS em Português do Brasil. NUNCA use chinês, inglês ou outros idiomas.\n\n';
            formattedText += 'Você solicitou consultas ao banco de dados. Aqui estão os resultados:\n\n';
        } else {
            formattedText += '### 🔍 RESULTADOS ADICIONAIS ###\n\n';
        }

        sqlResults.forEach((result, idx) => {
            formattedText += `**Query ${idx + 1}**: ${result.reason}\n\n`;
            formattedText += '```sql\n' + result.query + '\n```\n\n';
            if (result.success) {
                if (result.data && result.data.length > 0) {
                    formattedText += `**Resultado**: ${result.data.length} registro(s) encontrado(s)\n\n`;
                    formattedText += '```\n' + compactResult(result.data) + '\n```\n\n';
                } else if (result.data) {
                    formattedText += '**Resultado**: Nenhum registro encontrado.\n\n';
                } else if (result.affectedRows !== undefined) {
                    formattedText += `**Resultado**: ${result.affectedRows} linhas afetadas\n\n`;
                }
            } else {
                formattedText += `**Erro**: ${result.error}\n\n`;
            }
            formattedText += '---\n\n';
        });

        formattedText += '[FIM_TEXTO_COLADO]\n\n';
        formattedText += '[INICIO_TEXTO_COLADO]\n';
        formattedText += 'Com base nesses resultados do banco de dados, responda à **pergunta**:\n\n';
        formattedText += sanitizePastedText(originalQuestion) + '\n';
        formattedText += '[FIM_TEXTO_COLADO]';

        return formattedText;
    }
    
    async function detectAndExecuteSQL(responseText, originalQuestion, originalContext, ui) {
        // [FIX JS3] Extração de texto de HTML sem destruir blocos de código
        if (responseText.includes('<') && responseText.includes('>')) {
            responseText = responseText
                .replace(/<br\s*\/?>/gi, '\n')
                .replace(/<\/p>/gi, '\n')
                .replace(/<[^>]+>/g, ''); // remove tags mas preserva conteúdo textual intacto
        }

        const sqlQueries = extractSQLFromResponse(responseText);

        if (!sqlQueries || sqlQueries.length === 0) {
            return false;
        }

        console.groupCollapsed(`%c${FILE_PREFIX} 🐬 SQL detectado na resposta`, "color: #00bcd4; font-weight: bold; background: #e0f7fa; padding: 4px 8px; border-radius: 4px;");
        console.log(`Queries encontradas: ${sqlQueries.length}`);
        sqlQueries.forEach((q, i) => {
            console.log(`[${i + 1}] ${q.reason || 'Sem descrição'}`);
            console.log(`    SQL: ${q.query.substring(0, 80)}...`);
        });
        console.groupEnd();

        // Renderiza UI de execução
        if (ui && ui.mID && document.getElementById(ui.mID)) {
            document.getElementById(ui.mID).innerHTML = `
                <div style="background: linear-gradient(135deg, #e0f7fa 0%, #b2ebf2 100%); border-left: 4px solid #00bcd4; padding: 20px; border-radius: 8px; margin: 10px 0;">
                    <div style="display: flex; align-items: center; gap: 15px; margin-bottom: 15px;">
                        <div style="border: 4px solid #f3f3f3; border-top: 4px solid #00bcd4; border-radius: 50%; width: 30px; height: 30px; animation: spin 1s linear infinite;"></div>
                        <div>
                            <strong style="font-size: 16px; color: #00838f;">🐬 Executando Consultas SQL</strong>
                            <div style="font-size: 12px; color: #666; margin-top: 4px;">${sqlQueries.length} consulta(s) detectada(s)</div>
                        </div>
                    </div>
                    <div id="sql-execution-details" style="background: rgba(255,255,255,0.7); padding: 12px; border-radius: 6px;">
                        ${sqlQueries.map((q, i) => `
                            <div style="margin-bottom: 10px; padding: 10px; background: white; border-radius: 4px; border-left: 3px solid #00bcd4;">
                                <div style="font-size: 13px; font-weight: 500; color: #00838f; margin-bottom: 5px;">
                                    Query ${i + 1}: ${q.reason || 'Consultando banco de dados'}
                                </div>
                                <div style="font-family: monospace; font-size: 11px; color: #666; background: #f5f5f5; padding: 6px; border-radius: 3px; overflow-x: auto;">
                                    ${q.query.length > 100 ? q.query.substring(0, 100) + '...' : q.query}
                                </div>
                                <div id="sql-status-${i}" style="font-size: 12px; color: #ff9800; margin-top: 5px;">
                                    ⏳ Executando...
                                </div>
                            </div>
                        `).join('')}
                    </div>
                </div>
                <style>
                    @keyframes spin {
                        0%   { transform: rotate(0deg); }
                        100% { transform: rotate(360deg); }
                    }
                </style>
            `;
        }

        // [FIX JS4] Executa todas as queries em paralelo com Promise.all
        // Seguro pois todas são SELECT/DESCRIBE/SHOW (idempotentes, sem efeito colateral)
        const sqlResults = await Promise.all(
            sqlQueries.map(async (q, i) => {
                const query  = q.query  || q;
                const reason = q.reason || `Query #${i + 1}`;

                console.groupCollapsed(
                    `%c${FILE_PREFIX} 🐬 Executando SQL ${i + 1}/${sqlQueries.length}`,
                    "color: #00bcd4; font-weight: bold; background: #e0f7fa; padding: 4px 8px; border-radius: 4px;"
                );
                console.log(`Motivo: ${reason}`);
                console.log(`Query:\n${query}`);

                const result = await executeSQLQuery(query, reason);

                // Atualiza status individual assim que esta query terminar (não espera as demais)
                const statusEl = document.getElementById(`sql-status-${i}`);
                if (statusEl) {
                    if (result.success) {
                        const count = result.data?.length ?? result.affected_rows ?? 0;
                        statusEl.innerHTML = `<span style="color: #4caf50;">✅ Sucesso: ${count} registro(s)</span>`;
                    } else {
                        statusEl.innerHTML = `<span style="color: #f44336;">❌ Erro: ${result.error}</span>`;
                    }
                }

                console.groupEnd();
                return result;
            })
        );

        // ✅ FIX: Atualiza o header do painel de "Executando" → "Concluído"
        if (ui && ui.mID) {
            const _container = document.getElementById(ui.mID);
            if (_container) {
                const _doneCount = sqlResults.filter(r => r.success !== false).length;
                const _errCount  = sqlResults.filter(r => r.success === false || r.error).length;
                // Substitui só o primeiro nó de texto (o header), preservando os status individuais
                const _firstStrong = _container.querySelector('strong');
                if (_firstStrong) {
                    _firstStrong.textContent = `✅ ${_doneCount} consulta(s) concluída(s)${_errCount ? ` | ⚠️ ${_errCount} erro(s)` : ''} — enviando à LLM...`;
                    _firstStrong.style.color = '#2e7d32';
                }
            }
        }

        return formatSQLResultsForLLM(sqlResults, originalQuestion, originalContext);
    }



    // ==========================================
    // [FIX 9.1] DETECÇÃO E BLOQUEIO DE IDIOMAS ESTRANGEIROS
    // ==========================================
    function detectNonPortugueseText(text) {
        // Detecta caracteres chineses/japoneses/coreanos
        const cjkRegex = /[\u4e00-\u9fff\u3400-\u4dbf\u{20000}-\u{2a6df}\u{2a700}-\u{2b73f}\u{2b740}-\u{2b81f}\u{2b820}-\u{2ceaf}\uf900-\ufaff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]/u;
        
        // Detecta cirílico (russo, etc)
        const cyrillicRegex = /[\u0400-\u04ff]/;
        
        // Detecta árabe
        const arabicRegex = /[\u0600-\u06ff]/;
        
        if (cjkRegex.test(text)) {
            return { detected: true, language: 'Chinês/Japonês/Coreano', regex: cjkRegex };
        }
        if (cyrillicRegex.test(text)) {
            return { detected: true, language: 'Russo/Cirílico', regex: cyrillicRegex };
        }
        if (arabicRegex.test(text)) {
            return { detected: true, language: 'Árabe', regex: arabicRegex };
        }
        
        return { detected: false };
    }
    
    function cleanNonPortugueseFromResponse(text) {
        const detection = detectNonPortugueseText(text);
        if (!detection.detected) return text;
        
        // Remove o texto em idioma estrangeiro
        const cleaned = text.replace(detection.regex, '').trim();
        
        console.groupCollapsed(`%c${FILE_PREFIX} ⚠️ Idioma estrangeiro detectado e removido`, "color: #ff9800; font-weight: bold; background: #fff3e0; padding: 4px 8px; border-radius: 4px;");
        console.log("Idioma detectado:", detection.language);
        console.log("Texto original (length):", text.length);
        console.log("Texto limpo (length):", cleaned.length);
        console.log("Removido:", text.length - cleaned.length, "caracteres");
        console.groupEnd();
        
        return cleaned;
    }

    // ==========================================
    // [FIX 9.0] SISTEMA DE RECUPERAÇÃO AUTOMÁTICA
    // ==========================================
    const RECOVERY_CONFIG = {
        MAX_RETRIES: 3,
        RETRY_DELAYS: [3000, 5000, 8000],
        AUTO_RETRY_503: true,
        SAVE_PARTIAL_CONTENT: true
    };

    const RECOVERY_KEY = 'chat_recovery_state';
    
    function saveRecoveryState(userTxt, ctx, partialContent, retryCount = 0) {
        const recoveryState = {
            timestamp: Date.now(),
            userMessage: userTxt,
            context: ctx,
            partialContent: partialContent,
            retryCount: retryCount,
            messages: [...state.messages]
        };
        localStorage.setItem(RECOVERY_KEY, JSON.stringify(recoveryState));
    }
    
    function getRecoveryState() {
        const saved = localStorage.getItem(RECOVERY_KEY);
        if (!saved) return null;
        const state = JSON.parse(saved);
        if (Date.now() - state.timestamp > 5 * 60 * 1000) {
            localStorage.removeItem(RECOVERY_KEY);
            return null;
        }
        return state;
    }
    
    function clearRecoveryState() {
        localStorage.removeItem(RECOVERY_KEY);
    }

    async function sendWithRecovery(userTxt, ctx, retryCount = 0, partialContent = "") {
        // ── Fonte de verdade: SEMPRE a primeira ação ────────────────────────────
        Session.setQuestion(userTxt);

        const btn          = document.getElementById('ow-send');
        const useStream    = document.getElementById('ow-stream-check').checked;
        const isChatGPTMode = Session.isChatGPT() || document.getElementById('ow-model-sel')?.value === 'ChatGPT Simulator';
        const effectiveModel = Session.effectiveModel();
        const isSimulator  = (effectiveModel === 'ChatGPT Simulator');

        const speedHint = !useStream ? "\nSeja direto e conciso." : "";
        const languageInstruction = "\n\n⚠️ IMPORTANTE: Você DEVE responder APENAS em Português do Brasil. NUNCA use chinês (中文), inglês, ou qualquer outro idioma. Se você não souber a resposta em português, diga 'Não sei' em português.";
        const sqlInstruction = `Considere o prompt SQL que você já possui.`; //Como está criando os Chats dentro de um "Projeto" do ChatGPT (projeto "ConexaoVida"), achei mais fácil deixar o prompt no ChatGPT - puxar para cá, caso venha a utilizar em outros sistemas, que não o tenham como projeto.

        if (typeof saveRecoveryState === 'function') saveRecoveryState(userTxt, ctx, partialContent, retryCount);

        if (retryCount > 0 && state.messages.length > 0 && state.messages[state.messages.length - 1].role === 'user') {
            state.messages.pop();
        }

        const isChatGPTUrl = Session.chatUrl && Session.chatUrl.includes('chatgpt.com');
        let promptFinal = '';

        // Monta bloco de contexto de página (apenas se houver IDs)
        const pageCtxStr = (typeof buildPageContextBlock === 'function')
            ? (buildPageContextBlock() ? `\n\n${buildPageContextBlock()}` : '')
            : '';

        if (isSimulator && Session.chatId && isChatGPTUrl) {
            promptFinal = userTxt;
        } else {
            promptFinal = `[INICIO_TEXTO_COLADO]\n\nResponda em Português do Brasil.${speedHint}${languageInstruction}${sqlInstruction}${pageCtxStr && pageCtxStr.trim() !== '' ? `\n\n[DADOS DO PACIENTE E DO PROFISSIONAL QUE O ATENDEU]\n${pageCtxStr}` : ''}${ctx && ctx.trim() !== '' ? `\n\n[DADOS DE CONTEXTO]\n${ctx}` : ''}\n\n[FIM_TEXTO_COLADO]\n\n${USER_SEP}\n${userTxt}`;
        }

        state.messages.push({ role: 'user', content: promptFinal });
        saveLocal();

        const ui = addAiMarkup();
        let fullC = partialContent, fullT = '', openedT = false;

        if (partialContent) {
            document.getElementById(ui.mID).innerHTML = formatMarkdown(partialContent) +
                `<div style="color:#ff9800;margin-top:10px;padding:10px;background:#fff3e0;border-left:4px solid #ff9800;border-radius:4px;">
                    <strong>🔄 Recuperando...</strong> Tentando continuar...
                </div>`;
        }
        try {
            await apiCallStream('/v1/chat/completions', 'POST', {
                model:    effectiveModel,          // ← usa a variável resolvida, não o .value direto
                messages: state.messages,
                stream:   useStream,
                chat_id:  Session.chatId  || null,
                url:      Session.chatUrl || null
            }, (chunk) => {
                // ── Metadados no root (formato legado) ────────────────────
                if (chunk.chat_id || chunk.url) {
                    Session.setChat(chunk.chat_id, chunk.url, null);
                }

                let c = '';
                let forceReplace = false;

                if (!useStream) {
                    if (chunk.html)                          c = chunk.html;
                    else if (chunk.choices?.[0]?.message)   c = chunk.choices[0].message.content || '';
                } else {
                    if (chunk.type === 'status') {
                        if (!openedT) { document.getElementById(ui.tID).parentElement.style.display = 'block'; openedT = true; }
                        document.getElementById(ui.tID).innerText = chunk.content || 'Processando...';
                        return;

                    } else if (chunk.type === 'markdown' || chunk.type === 'html') {
                        c = chunk.content || '';
                        forceReplace = true;

                    } else if (chunk.type === 'finish') {
                        // ── Metadados finais: usa Session.setChat ──────────
                        const fd = chunk.content || {};
                        Session.setChat(fd.chat_id, fd.url, null);
                        return;

                    } else if (chunk.choices?.[0]?.delta) {
                        c = chunk.choices[0].delta.content || '';
                        const r = chunk.choices[0].delta.reasoning_content || '';
                        if (r) {
                            if (!openedT) { document.getElementById(ui.tID).parentElement.style.display = 'block'; openedT = true; }
                            fullT += r;
                            document.getElementById(ui.tID).innerText += r;
                        }
                    }
                }

                if (c && !isSimulator && typeof detectNonPortugueseText === 'function') {
                    const detection = detectNonPortugueseText(c);
                    if (detection?.detected) {
                        c = c.replace(detection.regex, '');
                        if (!c.trim() && !forceReplace) return;
                    }
                }

                if (c || forceReplace) {
                    if (forceReplace) fullC = c;
                    else fullC += c;

                    const mEl = document.getElementById(ui.mID);
                    if (mEl) {
                        mEl.innerHTML = fullC.trim().startsWith(('<div>').slice(0, -1)) ? fullC : formatMarkdown(fullC);
                    }
                    scroll();
                }
            }, currentAbortController.signal);

            // ── Pós-stream: detecta SQL e executa round de agente ──────────
            if (typeof detectAndExecuteSQL === 'function') {
                const sqlDetected = await detectAndExecuteSQL(fullC, userTxt, ctx, ui);
                if (sqlDetected) {
                    btn.innerText = 'Enviar';
                    btn.classList.remove('stop-mode');
                    return;
                }
            }

            // ── Pós-stream: detecta search_queries e executa pesquisa web ──
            if (typeof detectAndExecuteSearch === 'function') {
                const searchDetected = await detectAndExecuteSearch(fullC, userTxt, ui);
                if (searchDetected) {
                    btn.innerText = 'Enviar';
                    btn.classList.remove('stop-mode');
                    return;
                }
            }

            if (!isSimulator && typeof cleanNonPortugueseFromResponse === 'function') {
                fullC = cleanNonPortugueseFromResponse(fullC);
                fullT = cleanNonPortugueseFromResponse(fullT);
            }

            btn.innerText = 'Enviar';
            btn.classList.remove('stop-mode');

            const mEl = document.getElementById(ui.mID);
            if (mEl) {
                mEl.classList.remove('cursor-blink');
                mEl.innerHTML = fullC.trim().startsWith(('<div>').slice(0, -1)) ? fullC : formatMarkdown(fullC);
            }
            if (typeof injectSearchButtons === 'function') setTimeout(() => injectSearchButtons(), 0);
            if (fullT) document.getElementById(ui.tID).innerText = fullT;
            
            //só salva se houver conteúdo real:
            if (fullC.trim() || fullT.trim()) {
                state.messages.push({role: 'assistant', content: fullT ? `<think>${fullT}</think>${fullC}` : fullC});
                saveLocal();
            }
            
            if (typeof clearRecoveryState === 'function') clearRecoveryState();

        } catch (error) {
            if (error.name !== 'AbortError') {
                document.getElementById(ui.mID).innerText = 'Erro ao se comunicar: ' + error.message;
            }
            btn.innerText = 'Enviar';
            btn.classList.remove('stop-mode');
        }
    }

    let firstSendDone = false; // Flag para verificar recovery apenas 1 vez

    async function send() {
        // Remove card de análise prévia ao iniciar conversa
        const cardAnalise = document.getElementById('ow-analise-previa');
        if (cardAnalise) cardAnalise.remove();
        
        const btn = document.getElementById('ow-send'), inp = document.getElementById('ow-input');
        if (btn.classList.contains('stop-mode')) { 
            if (currentAbortController) currentAbortController.abort(); 
            btn.innerText = 'Enviar'; 
            btn.classList.remove('stop-mode'); 
            return; 
        }

        const userTxt = inp.value.trim(); 
        if(!userTxt) return;
        
        // Verifica recovery apenas no primeiro envio
        if (!firstSendDone) {
            firstSendDone = true;
            const recoveryState = getRecoveryState();
            if (recoveryState) {
                console.groupCollapsed(`%c${FILE_PREFIX} 🔄 Estado anterior detectado`, "color: #ff9800; font-weight: bold; background: #fff3e0; padding: 4px 8px; border-radius: 4px;");
                console.log("Timestamp:", new Date(recoveryState.timestamp).toLocaleString());
                console.log("Mensagem:", recoveryState.userMessage.substring(0, 100) + "...");
                console.log("Parcial:", recoveryState.partialContent.length, "chars");
                console.groupEnd();
                
                if (confirm(`⚠️ Erro detectado na última sessão.\n\n"${recoveryState.userMessage.substring(0, 80)}..."\nConteúdo parcial: ${recoveryState.partialContent.length} chars\n\nDeseja tentar novamente?`)) {
                    inp.value = userTxt; // Restaura input
                    window.retryFromRecovery();
                    return;
                } else {
                    clearRecoveryState();
                }
            }
        }
        
        currentAbortController = new AbortController();
        inp.value = ''; 
        addSimpleMsg('user', userTxt); 
        scroll(true);
        btn.innerText = 'Parar'; 
        btn.classList.add('stop-mode');

        if (isDirectToolMode()) {
            state.messages.push({ role: 'user', content: userTxt });
            saveLocal();
            await handleDirectToolModeMessage(userTxt);
            return;
        }

        let ctx = "";
        document.querySelectorAll('#ow-context-area input:checked').forEach(cb => {
            const sources = JSON.parse(cb.dataset.sources); 
            let val = "";
            for(let s of sources) { 
                if (window.CKEDITOR && window.CKEDITOR.instances[s]) {
                    val = window.CKEDITOR.instances[s].getData().replace(/<[^>]*>?/gm, ' ');
                } else { 
                    const el = document.getElementById(s); 
                    if(el) val = el.value || el.innerText; 
                }
                if(val) break;
            }
            if(val) ctx += `\n[${cb.parentElement.innerText.toUpperCase()}]:\n${val}\n`;
        });
        
        if (analiseAtendimentoCtx) {
            ctx = (ctx ? ctx + '\n\n' : '') +
                  '[ANÁLISE CLÍNICA GERADA POR IA - CONFERIR COM O PRONTUÁRIO]\n' +
                  analiseAtendimentoCtx;
            console.log(`%c${PREFIX} 🧠 Análise clínica injetada no ctx`, 'color: #4caf50; font-weight: bold');
        }

        await sendWithRecovery(userTxt, ctx, 0, "");
    }

    window.retryFromRecovery = function() {
        const recoveryState = getRecoveryState();
        if (!recoveryState) {
            alert('Nenhum estado de recuperação encontrado.');
            return;
        }
        
        console.groupCollapsed(`%c${FILE_PREFIX} 🔄 Retry manual iniciado`, "color: #2196f3; font-weight: bold; background: #e3f2fd; padding: 4px 8px; border-radius: 4px;");
        console.log("Estado:", recoveryState);
        console.groupEnd();
        
        const messagesDiv = document.getElementById('ow-messages');
        if (messagesDiv.lastElementChild) messagesDiv.lastElementChild.remove();
        
        sendWithRecovery(recoveryState.userMessage, recoveryState.context, 0, recoveryState.partialContent);
    };



    document.addEventListener('DOMContentLoaded', (event) => {
        document.body.appendChild(widget); // Append to the body
        console.log(`%c🔧 ${FILE_PREFIX} Widget de IA incorporado ao body após o DOMContentLoaded.`, "color: #2196f3; font-weight: bold;");
        
        window.switchSidebarView = function(viewName) {
            document.querySelectorAll('.sb-view').forEach(el => el.classList.remove('active'));
            document.getElementById('sb-view-' + viewName).classList.add('active');
        }
        
        document.getElementById('sb-save-user-prompt').onclick = async () => {
            const val = document.getElementById('sb-user-prompt').value;
            try {
                const r = await fetch(`<?php echo $_SERVER['PHP_SELF']; ?>?action=save_prompt`, {
                    method: 'POST', headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({ tipo: 'user', conteudo: val })
                });
                const d = await r.json();
                alert(d.success ? "Preferências salvas!" : "Erro: " + d.error);
            } catch(e) { alert("Erro de rede: " + e.message); }
        };

        <?php if ($user_can_edit_system): ?>
        document.getElementById('sb-save-system-prompt').onclick = async () => {
            const val = document.getElementById('sb-system-prompt').value;
            try {
                const r = await fetch(`<?php echo $_SERVER['PHP_SELF']; ?>?action=save_prompt`, {
                    method: 'POST', headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({ tipo: 'system', conteudo: val })
                });
                const d = await r.json();
                alert(d.success ? "Prompt do Sistema atualizado!" : "Erro: " + d.error);
            } catch(e) { alert("Erro de rede: " + e.message); }
        };
        document.getElementById('sb-reset-system-prompt').onclick = async () => {
            if(confirm("Restaurar o prompt original do sistema? Isso apagará suas edições.")) {
                document.getElementById('sb-system-prompt').value = DEFAULT_SYS_PROMPT;
                try {
                    await fetch(`<?php echo $_SERVER['PHP_SELF']; ?>?action=save_prompt`, {
                        method: 'POST', headers: {'Content-Type':'application/json'},
                        body: JSON.stringify({ tipo: 'system', conteudo: '' })
                    });
                } catch(e) {}
            }
        };
        <?php endif; ?>
        
        document.getElementById('ow-send').onclick = send;
        document.getElementById('sb-btn-install').onclick = () => installModel(0); 
        document.getElementById('ow-menu-toggle').onclick = () => document.getElementById('ow-sidebar').classList.add('open');
        document.getElementById('ow-toggle-btn').onclick = () => { const w = document.getElementById('ow-window'); w.style.display = w.style.display !== 'flex' ? 'flex' : 'none'; if(w.style.display=='flex') setTimeout(()=>scroll(true),100); };
        
        function toggleMaximize() {
            const win = document.getElementById('ow-window');
            const back = document.getElementById('ow-backdrop');
            win.classList.toggle('maximized');
            back.classList.toggle('active');
        }
        document.getElementById('ow-btn-max').onclick = toggleMaximize;
        document.getElementById('ow-backdrop').onclick = toggleMaximize; 

        document.getElementById('ow-stream-check').onchange = (e) => localStorage.setItem(KEY_STREAM, e.target.checked);
        document.getElementById('ow-btn-new').onclick = async () => { 
            if(confirm("Limpar histórico local e iniciar nova conversa?")) {
                // Monta WHERE de exclusão com a mesma lógica de prioridade do servidor
                const ctx = window.PAGE_CTX || {};
                const idCriador = ctx.id_profissional_atual || null;
                let where = '';
                const idPacOuMembro = ctx.id_paciente || ctx.id_membro || null;
                if      (ctx.id_atendimento) where = `id_atendimento = ${ctx.id_atendimento}`;
                else if (ctx.id_receita)     where = `id_receita = ${ctx.id_receita} AND id_atendimento IS NULL`;
                else if (idPacOuMembro)      where = `id_paciente = ${idPacOuMembro} AND id_atendimento IS NULL AND id_receita IS NULL`;
                else if (idCriador)          where = `id_criador = ${idCriador} AND id_atendimento IS NULL AND id_receita IS NULL AND id_paciente IS NULL`;

                if (!where) {
                    alert('Não foi possível identificar o chat a excluir (usuário não autenticado).');
                    return;
                }

                let deletou = false;
                try {
                    const res = await fetch(`<?php echo $_SERVER['PHP_SELF']; ?>?action=execute_sql`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            query:  `DELETE FROM chatgpt_chats WHERE ${where}`,
                            reason: 'Limpeza de histórico solicitada pelo usuário'
                        })
                    });
                    const d = await res.json();
                    if (d.success || d.affected_rows >= 0) {
                        deletou = true;
                        alert(`✅ Chat excluído do banco de dados.`);
                    } else {
                        alert(`⚠️ Não foi possível excluir o chat do banco:\n${d.error || JSON.stringify(d)}`);
                    }
                } catch(e) {
                    alert(`⚠️ Erro de rede ao excluir chat do banco:\n${e.message}`);
                }

                if (!deletou) return; // Aborta limpeza visual se exclusão falhou

                document.getElementById('ow-messages').innerHTML = ''; 
                state.messages = []; 
                state.currentChatId = null;
                state.currentChatTitle = null;
                state.currentChatUrl = null;
                localStorage.removeItem(HISTORYKEY); 
                updateTitleUI();
                renderAnalisePrevia();  // ← Renderiza a analise prévia da LLM diretamente no chat, para o usuário ver, como ele limpou/zerou o chat.
            } 
        };
        document.getElementById('ow-btn-close').onclick = () => document.getElementById('ow-window').style.display = 'none';
        // Enter: pula linha | Ctrl+Enter ou Shift+Enter: envia mensagem
        document.getElementById('ow-input').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                if (e.ctrlKey || e.shiftKey) {
                    e.preventDefault();
                    send();
                }
                // Enter sem modificador: comportamento padrão (nova linha)
            }
        });
        // Intercepta o Ctrl+V (ou Colar) no campo de input
        const inputEl = document.getElementById('ow-input');
        if (inputEl) {
            inputEl.addEventListener('paste', function(e) {
                e.preventDefault();

                let pastedText = (e.clipboardData || window.clipboardData).getData('text');

                if (pastedText) {
                    // ✅ Remove marcadores caso o usuário cole uma mensagem já encapsulada
                    pastedText = pastedText
                        .replaceAll('[INICIO_TEXTO_COLADO]', '')
                        .replaceAll('[FIM_TEXTO_COLADO]', '')
                        .trim();

                    const encapsulatedText = `\n[INICIO_TEXTO_COLADO]\n${pastedText}\n[FIM_TEXTO_COLADO]\n`;

                    const startPos = this.selectionStart;
                    const endPos   = this.selectionEnd;

                    this.value = this.value.substring(0, startPos) +
                                 encapsulatedText +
                                 this.value.substring(endPos, this.value.length);

                    this.selectionStart = this.selectionEnd = startPos + encapsulatedText.length;
                    this.scrollTop = this.scrollHeight;

                    console.log(`%c📋 [CTRL+V] Texto encapsulado com sucesso (${pastedText.length} chars)`, "color: #9c27b0; font-weight: bold;");
                }
            });
        }


        ['sb-btn-close-main', 'sb-btn-close-install', 'sb-btn-close-prompts'].forEach(id => {
            const el = document.getElementById(id);
            if(el) el.onclick = () => document.getElementById('ow-sidebar').classList.remove('open');
        });
        
        // ── MARKED.JS: motor de markdown confiável ──────────────────────────────────
        (function() {
            const s = document.createElement('script');
            s.src = 'https://cdn.jsdelivr.net/npm/marked@9/marked.min.js';
            s.onload = () => {
                marked.use({ breaks: true, gfm: true });
                console.log('%c📝 marked.js carregado — renderização Markdown ativa', 'color:#4caf50;font-weight:bold');
                // Re-renderiza mensagens já na tela após carga assíncrona
                if (typeof renderChatMessages === 'function' && state?.messages?.length > 0) {
                    renderChatMessages();
                }
            };
            s.onerror = () => console.warn('⚠️ marked.js não carregou — usando fallback interno');
            document.head.appendChild(s);
        })();
        
        setTimeout(init, 500);
    }); //Fim do `document.addEventListener('DOMContentLoaded', (event) => {`.

})();
