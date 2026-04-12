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


# ==============================
# Configuração
# ==============================
ROOT_DIR = Path(__file__).resolve().parents[1]
LOGS_DIR = ROOT_DIR / "logs"
_log_ts = datetime.now().strftime("%d_%m_%Y-%H_%M_%S")
AGENT_LOG = LOGS_DIR / f"auto_dev_agent-{_log_ts}.log"

def _env(name_new: str, default: str, legacy_name: str | None = None) -> str:
    """Lê variável nova, com fallback opcional para nome legado."""
    if name_new in os.environ:
        return os.environ[name_new]
    if legacy_name and legacy_name in os.environ:
        return os.environ[legacy_name]
    return default


SIMULATOR_URL = _env("AUTODEV_AGENT_SIMULATOR_URL", "http://127.0.0.1:3003/v1/chat/completions", "AUTON_AGENT_SIMULATOR_URL")
SIMULATOR_MODEL = _env("AUTODEV_AGENT_MODEL", "ChatGPT Simulator", "AUTON_AGENT_MODEL")
API_KEY = _env("AUTODEV_AGENT_API_KEY", "", "AUTON_AGENT_API_KEY")

# janelas de ciclo
SLEEP_BETWEEN_CYCLES = int(_env("AUTODEV_AGENT_CYCLE_SEC", "60", "AUTON_AGENT_CYCLE_SEC"))
SUGGESTION_INTERVAL_SEC = int(_env("AUTODEV_AGENT_SUGGESTION_SEC", "300", "AUTON_AGENT_SUGGESTION_SEC"))
MAX_CONTEXT_CHARS = int(_env("AUTODEV_AGENT_CONTEXT_CHARS", "12000", "AUTON_AGENT_CONTEXT_CHARS"))

# segurança de patch
MAX_PATCH_BYTES = int(_env("AUTODEV_AGENT_MAX_PATCH_BYTES", "120000", "AUTON_AGENT_MAX_PATCH_BYTES"))
ALLOW_UNSAFE_AUTOFIX = _env("AUTODEV_AGENT_UNSAFE", "0", "AUTON_AGENT_UNSAFE") == "1"

# monitoramento
ERROR_PATTERNS = [
    r"\btraceback\b",
    r"\bexception\b",
    r"\berror\b",
    r"\bfalha\b",
    r"\bfatal\b",
    r"\bsegmentation fault\b",
]

WARN_PATTERNS = [
    r"\bwarning\b",
    r"\btimeout\b",
    r"\brate limit\b",
    r"\bthrottle\b",
    r"\bretry\b",
]

START_COMMANDS = [
    ["cmd", "/c", "0. start.bat"],
    ["cmd", "/c", "1. start_apenas_analisador_prontuarios.bat"],
]


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


def start_bats_if_needed() -> list[subprocess.Popen]:
    """Inicia os .bat declarados, se estiver em ambiente Windows.

    Em Linux/macOS, registra aviso e segue somente com monitoramento/sugestões.
    """
    procs: list[subprocess.Popen] = []
    if not is_windows():
        log("⚠️ Ambiente não-Windows detectado; start dos .bat foi pulado.")
        return procs

    for cmd in START_COMMANDS:
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_CONSOLE,  # type: ignore[attr-defined]
            )
            procs.append(proc)
            log(f"✅ Processo iniciado: {' '.join(cmd)} (pid={proc.pid})")
        except Exception as exc:
            log(f"❌ Falha ao iniciar {' '.join(cmd)}: {exc}")
    return procs


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
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    return headers


def ask_llm_for_actions(context: dict) -> Optional[dict]:
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

    try:
        resp = requests.post(SIMULATOR_URL, headers=_llm_headers(), json=body, timeout=180)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("JSON de resposta não é objeto")
        return parsed
    except Exception as exc:
        log(f"❌ Falha ao consultar LLM: {exc}")
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
    code, out = run_shell("python -m py_compile Scripts/*.py", timeout=240)
    checks["py_compile"] = {"exit_code": code, "output": out[-2500:]}

    code, out = run_shell("git status --short", timeout=60)
    checks["git_status"] = {"exit_code": code, "output": out[-2500:]}
    return checks


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
    changed = "M" in (checks.get("git_status", {}).get("output", "")) or "A" in (checks.get("git_status", {}).get("output", ""))
    log(
        "ciclo concluído | "
        f"errors={err_count} warnings={warn_count} "
        f"actions={len(results)} changed={changed}"
    )


def main() -> None:
    log("🚀 AutoDevAgent iniciando")
    log(f"📄 Log: {AGENT_LOG}")
    start_bats_if_needed()

    last_suggestion_ts = 0.0
    while True:
        context = collect_runtime_context()
        checks = quick_checks()

        has_error = any(i.get("level") == "error" for i in context.get("incidents", []))
        time_for_suggestion = (time.time() - last_suggestion_ts) >= SUGGESTION_INTERVAL_SEC

        results: list[dict] = []
        if has_error or time_for_suggestion:
            plan = ask_llm_for_actions(context)
            if plan:
                results = execute_actions(plan)
                last_suggestion_ts = time.time()

        summarize_cycle(context, checks, results)
        time.sleep(max(10, SLEEP_BETWEEN_CYCLES))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("🛑 Encerrado por KeyboardInterrupt")
    except Exception as exc:
        log(f"❌ Erro fatal no auto_dev_agent: {exc}", level=logging.ERROR)
        raise
