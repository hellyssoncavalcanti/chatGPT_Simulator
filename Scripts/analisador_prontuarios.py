#!/usr/bin/env python3
"""
auto_dev_agent.py
Orquestrador autônomo e auto-reparável para o ChatGPT_Simulator.

Atua como um "Desenvolvedor Virtual":
1. Escaneia a pasta de logs em busca de erros recentes.
2. Se achar erros: Envia o stacktrace para o LLM local pedindo o patch de correção.
3. Se não achar erros: Pede sugestões de melhorias de performance.
4. Aplica os códigos sugeridos automaticamente.
"""

import os
import time
import glob
import re
import json
import requests
import logging
from datetime import datetime

# =====================================================================
# CONFIGURAÇÕES DO AGENTE
# =====================================================================
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(ROOT_DIR, "logs")
SIMULATOR_URL = os.environ.get("AUTODEV_AGENT_SIMULATOR_URL", "http://127.0.0.1:3003/v1/chat/completions")

CYCLE_SEC = 60              # Tempo entre verificações (segundos)
SUGGESTION_INTERVAL = 600   # Se ficar 10 min sem erros, pede uma melhoria (segundos)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | AUTODEV 🤖 | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler()]
)

# =====================================================================
# 1. LEITURA E ANÁLISE DE LOGS
# =====================================================================
def get_recent_errors(tail_lines=100):
    """Busca os arquivos de log mais recentes e extrai linhas com Erro."""
    if not os.path.exists(LOGS_DIR):
        return None

    list_of_files = glob.glob(os.path.join(LOGS_DIR, '*.log'))
    if not list_of_files:
        return None

    latest_file = max(list_of_files, key=os.path.getctime)
    
    with open(latest_file, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()[-tail_lines:]

    errors = []
    capture = False
    for line in lines:
        if "ERROR" in line or "[ERRO]" in line or "Exception" in line or "Traceback" in line:
            capture = True
        
        # Se capturou um erro, pega as linhas seguintes também (para pegar o Traceback completo)
        if capture:
            errors.append(line.strip())
            if len(errors) > 20: # Limita o tamanho do stacktrace
                break

    return "\n".join(errors) if errors else None


# =====================================================================
# 2. COMUNICAÇÃO COM O LLM LOCAL (Simulador)
# =====================================================================
def ask_llm_for_solution(prompt_text):
    """Envia o contexto para o ChatGPT Simulator local e retorna a resposta."""
    logging.info("Enviando requisição para a LLM local...")
    
    payload = {
        "model": "gpt-4o", # O simulador ignora, mas precisa estar presente
        "messages": [
            {
                "role": "system",
                "content": (
                    "Você é um Desenvolvedor Python Sênior autônomo operando um sistema. "
                    "Sua missão é consertar erros ou sugerir melhorias. "
                    "REGRA ESTRITA: Sempre que for sugerir a alteração de um código, "
                    "você DEVE iniciar o bloco de código com um comentário contendo o caminho do arquivo "
                    "(exemplo: `# FILE: Scripts/server.py`) seguido pelo código completo atualizado."
                )
            },
            {"role": "user", "content": prompt_text}
        ]
    }

    try:
        response = requests.post(SIMULATOR_URL, json=payload, timeout=300)
        response.raise_for_status()
        data = response.json()
        return data['choices'][0]['message']['content']
    except Exception as e:
        logging.error(f"Falha ao contatar a LLM: {e}")
        return None


# =====================================================================
# 3. EXTRAÇÃO E APLICAÇÃO DE PATCHES (Auto-Cura)
# =====================================================================
def apply_code_patches(llm_response):
    """Analisa a resposta do LLM, busca blocos de código e aplica as edições."""
    if not llm_response:
        return False

    # Solução técnica: constrói a regex somando as crases para não quebrar o visualizador de markdown do chat!
    bt = "`" * 3
    pattern_str = fr"{bt}(?:python)?\s*#\s*FILE:\s*([^\n]+)\n(.*?)\n{bt}"
    pattern = re.compile(pattern_str, re.DOTALL | re.IGNORECASE)
    
    matches = pattern.findall(llm_response)

    if not matches:
        logging.info("Nenhuma modificação de código acionável detectada na resposta da LLM.")
        return False

    applied_any = False
    for file_path, new_code in matches:
        target_path = os.path.join(ROOT_DIR, file_path.strip())
        
        logging.info(f"LLM sugeriu modificações para: {target_path}")
        
        if os.path.exists(target_path):
            # Backup de segurança antes de sobrescrever
            backup_path = f"{target_path}.bak_{int(time.time())}"
            os.rename(target_path, backup_path)
            
            with open(target_path, 'w', encoding='utf-8') as f:
                f.write(new_code.strip() + "\n")
            
            logging.info(f"✅ Patch aplicado com sucesso em {file_path}! (Backup criado)")
            applied_any = True
        else:
            logging.warning(f"⚠️ O arquivo {file_path} não foi encontrado. Ignorando correção.")

    return applied_any


# =====================================================================
# 4. LOOP PRINCIPAL (A Mente do Agente)
# =====================================================================
def main():
    logging.info("Agente Autônomo Iniciado. Monitorando logs e arquitetura...")
    last_suggestion_ts = time.time()

    while True:
        try:
            errors = get_recent_errors(tail_lines=150)
            time_since_last_suggestion = time.time() - last_suggestion_ts
            
            # --- CÁLCULO DE ESTRATÉGIA ---
            if errors:
                logging.warning("🚨 Erros detectados! Solicitando correção emergencial à LLM.")
                prompt = (
                    "O sistema crashou ou gerou os seguintes erros nos logs:\n\n"
                    f"{errors}\n\n"
                    "Analise esse stacktrace, identifique a falha e reescreva o arquivo defeituoso. "
                    "Lembre-se de colocar `# FILE: nome_do_arquivo.py` dentro do bloco de código para que eu aplique automaticamente."
                )
                
                response = ask_llm_for_solution(prompt)
                
                if apply_code_patches(response):
                    logging.info("🔄 Correção aplicada! Sugerindo restart dos serviços.")
                    # Opcional: Adicione aqui uma lógica para reiniciar o .bat
                
                last_suggestion_ts = time.time() # Reseta o timer

            elif time_since_last_suggestion >= SUGGESTION_INTERVAL:
                logging.info("💤 Sistema estável há um tempo. Solicitando melhorias e otimizações à LLM.")
                prompt = (
                    "Os logs do sistema estão limpos e não há erros. "
                    "Com base no que você sabe sobre minha arquitetura (um servidor Flask que interage com Playwright via filas), "
                    "escolha um arquivo (como browser.py, server.py ou storage.py) e crie uma melhoria de performance, "
                    "tratamento de exceções ou refatoração.\n"
                    "Forneça o arquivo reescrito e lembre-se do `# FILE: nome_do_arquivo.py`."
                )
                
                response = ask_llm_for_solution(prompt)
                apply_code_patches(response)
                last_suggestion_ts = time.time()

            else:
                logging.info("Nenhum erro encontrado no último ciclo. Aguardando...")

            # Descansa até o próximo ciclo
            time.sleep(CYCLE_SEC)

        except Exception as exc:
            logging.error(f"Erro fatal no próprio Agente: {exc}")
            time.sleep(10)

if __name__ == "__main__":
    main()
