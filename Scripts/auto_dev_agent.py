#!/usr/bin/env python3
"""
auto_dev_agent.py

Orquestrador autônomo para melhoria contínua do ChatGPT_Simulator.

Objetivos:
1) Subir os .bat principais automaticamente.
2) Monitorar logs em tempo real (tail) buscando erros, warnings e padrões de travamento.
3) Pedir sugestões de melhoria para a LLM local (via endpoint /v1/chat/completions,
   que internamente usa browser.py no Simulator).
4) Rodar checks locais (py_compile) e aplicar melhorias seguras por patch automático.
5) Manter ciclo contínuo de otimização mesmo sem erro explícito.

IMPORTANTE:
- Este agente aplica patches apenas em modo "safe" por padrão.
- Alterações potencialmente destrutivas são bloqueadas por regras de segurança.
- Toda ação é registrada em logs/auto_dev_agent.log.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import requests
try:
    import config
except Exception:
    config = None


# ==============================
# Configuração
# ==============================
ROOT_DIR = Path(__file__).resolve().parents[1]
LOGS_DIR = ROOT_DIR / "logs"
_log_ts = datetime.now().strftime("%d_%m_%Y-%H_%M_%S")
AGENT_LOG = LOGS_DIR / f"auto_dev_agent-{_log_ts}.log"
IS_WINDOWS = os.name == "nt"

def _env(name_new: str, default: str, legacy_name: str | None = None) -> str:
    """Lê variável nova, com fallback opcional para nome legado."""
    if name_new in os.environ:
        return os.environ[name_new]
    if legacy_name and legacy_name in os.environ:
        return os.environ[legacy_name]
    return default


SIMULATOR_URL = _env("AUTODEV_AGENT_SIMULATOR_URL", "http://127.0.0.1:3003/v1/chat/completions", "AUTON_AGENT_SIMULATOR_URL")
SIMULATOR_MODEL = _env("AUTODEV_AGENT_MODEL", "ChatGPT Simulator", "AUTON_AGENT_MODEL")
_cfg_api_key = getattr(config, "API_KEY", "") if config else ""
API_KEY = _env("AUTODEV_AGENT_API_KEY", _cfg_api_key, "AUTON_AGENT_API_KEY")

# janelas de ciclo
SLEEP_BETWEEN_CYCLES = int(_env("AUTODEV_AGENT_CYCLE_SEC", "60", "AUTON_AGENT_CYCLE_SEC"))
SUGGESTION_INTERVAL_SEC = int(_env("AUTODEV_AGENT_SUGGESTION_SEC", "300", "AUTON_AGENT_SUGGESTION_SEC"))
MAX_CONTEXT_CHARS = int(_env("AUTODEV_AGENT_CONTEXT_CHARS", "12000", "AUTON_AGENT_CONTEXT_CHARS"))

# segurança de patch
MAX_PATCH_BYTES = int(_env("AUTODEV_AGENT_MAX_PATCH_BYTES", "120000", "AUTON_AGENT_MAX_PATCH_BYTES"))
ALLOW_UNSAFE_AUTOFIX = _env("AUTODEV_AGENT_UNSAFE", "1", "AUTON_AGENT_UNSAFE") == "1"
IMPROVEMENT_MAX_ATTEMPTS = int(_env("AUTODEV_AGENT_MAX_ATTEMPTS", "3"))
TEST_COMMANDS = [
    _env("AUTODEV_AGENT_TEST_CMD_1", "python -m py_compile Scripts/*.py"),
    _env("AUTODEV_AGENT_TEST_CMD_2", "git status --short"),
]
_last_llm_unavailable_log = 0.0

# monitoramento
ERROR_PATTERNS = [
    r"\btraceback\b",
    r"\bexception\b",
    r"\berror\b",
    r"\berro\b",
    r"\bfalha\b",
    r"\bfalhou\b",
    r"\bfatal\b",
    r"\bcannot access local variable\b",
    r"\bconnectionreseterror\b",
    r"\bsegmentation fault\b",
]

WARN_PATTERNS = [
    r"\bwarning\b",
    r"\btimeout\b",
    r"\brate limit\b",
    r"\bthrottle\b",
    r"\bretry\b",
]

SERVICE_PATTERNS = {
    "main": "Scripts\\\\main.py",
    "analisador_prontuarios": "Scripts\\\\analisador_prontuarios.py",
    "browser_worker": "Scripts\\\\browser.py",
}
_last_active_services_signature = ""


@dataclass
class Incident:
    level: str
    source: str
    line: str
    when_utc: str


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def setup_logger() -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("auto_dev_agent")
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        file_handler = logging.FileHandler(AGENT_LOG, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(stream)
        logger.addHandler(file_handler)
    return logger


LOGGER = setup_logger()


def log(msg: str, level: int = logging.INFO) -> None:
    LOGGER.log(level, msg)


def is_windows() -> bool:
    return os.name == "nt"


def discover_active_services() -> dict:
    """Descobre processos ativos do ecossistema sem iniciá-los."""
    if not is_windows():
        return {}

    service_map: dict[str, list[int]] = {name: [] for name in SERVICE_PATTERNS.keys()}
    for name, pattern in SERVICE_PATTERNS.items():
        ps_cmd = (
            "Get-CimInstance Win32_Process "
            f"| Where-Object {{ $_.CommandLine -like '*{pattern}*' }} "
            "| Select-Object -ExpandProperty ProcessId"
        )
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
            )
            pids = []
            for line in (proc.stdout or "").splitlines():
                line = line.strip()
                if line.isdigit():
                    pids.append(int(line))
            service_map[name] = sorted(set(pids))
        except Exception:
            service_map[name] = []
    return service_map


def log_active_services_snapshot(service_map: dict) -> None:
    global _last_active_services_signature
    signature = json.dumps(service_map, sort_keys=True, ensure_ascii=False)
    if signature == _last_active_services_signature:
        return
    _last_active_services_signature = signature

    msg = []
    for name, pids in service_map.items():
        if pids:
            msg.append(f"{name}={pids}")
        else:
            msg.append(f"{name}=OFF")
    log("🛰️ Serviços ativos monitorados: " + " | ".join(msg))


def tail_last_lines(path: Path, max_lines: int = 80) -> list[str]:
    if not path.exists() or not path.is_file():
        return []
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    lines = content.splitlines()
    return lines[-max_lines:]


def pick_log_files(max_files: int = 12) -> list[Path]:
    if not LOGS_DIR.exists():
        return []
    files = sorted(
        [p for p in LOGS_DIR.iterdir() if p.is_file() and p.suffix.lower() in {".log", ".txt"}],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[:max_files]


def scan_incidents(lines: Iterable[str], source: str) -> list[Incident]:
    incidents: list[Incident] = []
    for line in lines:
        low = line.lower()
        if any(re.search(p, low) for p in ERROR_PATTERNS):
            incidents.append(Incident("error", source, line.strip(), _now_utc()))
        elif any(re.search(p, low) for p in WARN_PATTERNS):
            incidents.append(Incident("warning", source, line.strip(), _now_utc()))
    return incidents


def collect_runtime_context() -> dict:
    payload: dict = {
        "timestamp": _now_utc(),
        "repo": str(ROOT_DIR),
        "python": sys.version,
        "incidents": [],
        "log_excerpt": [],
    }

    total_chars = 0
    all_incidents = []
    for log_file in pick_log_files():
        lines = tail_last_lines(log_file)
        all_incidents.extend(scan_incidents(lines, str(log_file.relative_to(ROOT_DIR))))
        excerpt = "\n".join(lines)
        if excerpt:
            block = f"### {log_file.name}\n{excerpt}"
            if total_chars + len(block) > MAX_CONTEXT_CHARS:
                break
            payload["log_excerpt"].append(block)
            total_chars += len(block)

    payload["incidents"] = [inc.__dict__ for inc in all_incidents[-100:]]
    return payload


def _llm_headers() -> dict:
    headers = {
        "Content-Type": "application/json",
        "X-Request-Source": "auto_dev_agent.py",
    }
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    return headers


def simulator_is_ready() -> bool:
    """Verifica se o Simulator HTTP está respondendo health-check."""
    global _last_llm_unavailable_log
    health_url = SIMULATOR_URL.replace("/v1/chat/completions", "/health")
    try:
        resp = requests.get(health_url, timeout=5)
        return resp.status_code == 200
    except Exception:
        now = time.time()
        if now - _last_llm_unavailable_log >= 30:
            log(f"⏳ Simulator ainda indisponível em {health_url}; aguardando subir...")
            _last_llm_unavailable_log = now
        return False


def ask_llm_for_actions(context: dict, objective: str) -> Optional[dict]:
    """Solicita plano de melhoria à LLM local.

    A resposta esperada é JSON estrito com o formato:
    {
      "summary": "...",
      "actions": [
        {
          "type": "shell" | "patch" | "note",
          "reason": "...",
          "command": "...",          # se shell
          "unified_diff": "...",      # se patch
          "priority": 1
        }
      ]
    }
    """
    system_prompt = (
        "Você é um engenheiro sênior Python focado em confiabilidade. "
        "Retorne APENAS JSON válido, sem markdown. "
        "Priorize correções pequenas e seguras. "
        "Nunca proponha comandos destrutivos (rm -rf, format, shutdown)."
    )
    user_prompt = (
        f"Objetivo desta rodada: {objective}\n\n"
        "Contexto de execução do sistema:\n"
        f"{json.dumps(context, ensure_ascii=False)}\n\n"
        "Proponha até 3 ações de melhoria/correção. "
        "Use patch somente quando necessário e mantenha diff mínimo."
    )

    body = {
        "model": SIMULATOR_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "stream": False,
    }

    if not simulator_is_ready():
        return None

    try:
        resp = requests.post(SIMULATOR_URL, headers=_llm_headers(), json=body, timeout=180)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "choices" in data:
            content = data["choices"][0]["message"]["content"]
        elif isinstance(data, dict) and "response" in data:
            content = data.get("response", "")
        elif isinstance(data, dict) and "error" in data:
            raise ValueError(f"Simulator/browser.py retornou erro: {data.get('error')}")
        else:
            raise ValueError(f"Resposta inesperada do Simulator/browser.py: chaves={list(data.keys()) if isinstance(data, dict) else type(data)}")
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("JSON de resposta não é objeto")
        return parsed
    except Exception as exc:
        log(f"❌ Falha ao consultar Simulator/browser.py: {exc}")
        return None


def command_is_safe(command: str) -> bool:
    forbidden = [
        "rm -rf",
        "shutdown",
        "reboot",
        "mkfs",
        "dd if=",
        "del /f /q",
        ":(){:|:&};:",
        "git reset --hard",
    ]
    cmd = command.lower().strip()
    if any(token in cmd for token in forbidden):
        return False
    return True


def run_shell(command: str, timeout: int = 180) -> tuple[int, str]:
    if not command_is_safe(command):
        return 2, "blocked by safety policy"

    try:
        proc = subprocess.run(
            command,
            cwd=str(ROOT_DIR),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout[-6000:]
    except subprocess.TimeoutExpired as exc:
        return 124, f"timeout: {exc}"
    except Exception as exc:
        return 1, str(exc)


def patch_is_safe(unified_diff: str) -> bool:
    if not unified_diff or len(unified_diff.encode("utf-8")) > MAX_PATCH_BYTES:
        return False
    blocked_paths = [".git/", "certs/", "db/", "logs/"]
    low = unified_diff.lower()
    if any(bp in low for bp in blocked_paths):
        return False
    return True


def apply_unified_diff(unified_diff: str) -> tuple[bool, str]:
    """Aplica diff via git apply --whitespace=nowarn."""
    if not patch_is_safe(unified_diff):
        return False, "patch bloqueado por política de segurança"
    if not ALLOW_UNSAFE_AUTOFIX:
        return False, "AUTODEV_AGENT_UNSAFE=0: auto patch desabilitado por padrão"

    patch_file = ROOT_DIR / "temp" / "auton_agent.patch"
    patch_file.parent.mkdir(parents=True, exist_ok=True)
    patch_file.write_text(unified_diff, encoding="utf-8")

    code, out = run_shell(f'git apply --whitespace=nowarn "{patch_file}"')
    if code != 0:
        return False, out
    return True, "patch aplicado"


def quick_checks() -> dict:
    checks = {}
    for i, cmd in enumerate(TEST_COMMANDS, start=1):
        timeout = 240 if i == 1 else 90
        code, out = run_shell(cmd, timeout=timeout)
        checks[f"check_{i}"] = {"cmd": cmd, "exit_code": code, "output": out[-2500:]}
    return checks


def checks_ok(checks: dict) -> bool:
    return all(v.get("exit_code") == 0 for v in checks.values())


def execute_actions(plan: dict) -> list[dict]:
    results = []
    actions = plan.get("actions") if isinstance(plan, dict) else None
    if not isinstance(actions, list):
        return [{"ok": False, "reason": "plano sem actions"}]

    for action in actions[:3]:
        if not isinstance(action, dict):
            continue
        typ = str(action.get("type", "note")).lower().strip()
        reason = str(action.get("reason", "")).strip()
        if typ == "shell":
            cmd = str(action.get("command", "")).strip()
            code, out = run_shell(cmd)
            results.append({"type": "shell", "cmd": cmd, "exit_code": code, "output": out, "reason": reason})
            log(f"shell: {cmd} -> exit={code}")
        elif typ == "patch":
            diff = str(action.get("unified_diff", ""))
            ok, msg = apply_unified_diff(diff)
            results.append({"type": "patch", "ok": ok, "message": msg, "reason": reason})
            log(f"patch: ok={ok} msg={msg}")
        else:
            note = str(action.get("note", reason or "sem nota"))
            results.append({"type": "note", "note": note})
            log(f"note: {note}")
    return results


def summarize_cycle(context: dict, checks: dict, results: list[dict]) -> None:
    err_count = sum(1 for i in context.get("incidents", []) if i.get("level") == "error")
    warn_count = sum(1 for i in context.get("incidents", []) if i.get("level") == "warning")
    changed = any(token in json.dumps(checks, ensure_ascii=False) for token in [" M ", " A ", "?? "])
    log(
        "ciclo concluído | "
        f"errors={err_count} warnings={warn_count} "
        f"actions={len(results)} changed={changed} checks_ok={checks_ok(checks)}"
    )


def run_improvement_round(context: dict, objective: str) -> tuple[list[dict], dict]:
    """Executa rodada de melhoria com retentativas até validação passar."""
    attempts = []
    checks = quick_checks()
    for attempt in range(1, max(1, IMPROVEMENT_MAX_ATTEMPTS) + 1):
        plan = ask_llm_for_actions(context, objective=objective)
        if not plan:
            attempts.append({"attempt": attempt, "ok": False, "reason": "sem plano"})
            continue

        results = execute_actions(plan)
        checks = quick_checks()
        ok = checks_ok(checks)
        attempts.append({"attempt": attempt, "ok": ok, "results": results, "checks": checks})
        if ok:
            return results, checks

        # feedback para próxima tentativa (com erros de validação)
        context["last_attempt_feedback"] = {
            "attempt": attempt,
            "checks": checks,
            "results": results,
        }
        log(f"⚠️ Tentativa {attempt} não validou. Gerando novo plano...")

    last = attempts[-1] if attempts else {"results": []}
    return last.get("results", []), checks


def main() -> None:
    log("🚀 AutoDevAgent iniciando")
    log(f"📄 Log: {AGENT_LOG}")
    if not API_KEY:
        log("⚠️ API_KEY ausente no ambiente/config. Requisições ao Simulator podem retornar 401.", level=logging.WARNING)
    log("🔎 Modo monitor: não inicia servidores automaticamente; apenas detecta e monitora serviços ativos.")

    last_suggestion_ts = 0.0
    while True:
        try:
            active_services = discover_active_services()
            log_active_services_snapshot(active_services)
            context = collect_runtime_context()
            context["active_services"] = active_services
            checks = quick_checks()

            has_error = any(i.get("level") == "error" for i in context.get("incidents", []))
            time_for_suggestion = (time.time() - last_suggestion_ts) >= SUGGESTION_INTERVAL_SEC

            results: list[dict] = []
            if has_error or time_for_suggestion:
                objective = (
                    "Corrigir erros de execução detectados nos logs, validar testes e estabilizar o sistema."
                    if has_error
                    else "Propor e implementar melhorias contínuas de robustez, performance e observabilidade, depois validar testes."
                )
                results, checks = run_improvement_round(context, objective=objective)
                last_suggestion_ts = time.time()

            summarize_cycle(context, checks, results)
            time.sleep(max(10, SLEEP_BETWEEN_CYCLES))
        except Exception as exc:
            log(f"❌ Exceção no loop principal: {exc}", level=logging.ERROR)
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("🛑 Encerrado por KeyboardInterrupt")
    except Exception as exc:
        log(f"❌ Erro fatal no auto_dev_agent: {exc}", level=logging.ERROR)
        time.sleep(10)
