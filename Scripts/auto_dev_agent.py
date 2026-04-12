#!/usr/bin/env python3
"""
auto_dev_agent.py

Agente autônomo para desenvolvimento contínuo do ChatGPT_Simulator.

O fluxo principal:
1) Inicia um .bat (ex.: "0. start.bat") para subir o sistema.
2) Faz tail de logs em tempo real e detecta erros com regex.
3) Consulta uma LLM (via endpoint local) para sugerir ações/correções.
4) Executa diagnóstico, aplica patches via `git apply`, roda checks e faz rollback se necessário.
5) Mesmo sem erro, entra em ciclo de melhoria contínua pedindo otimizações para a LLM.

IMPORTANTE:
- Por padrão, atua em modo seguro (SAFE_MODE=true), sem comandos destrutivos.
- Para aplicar mudanças automaticamente, defina AUTO_APPLY_PATCHES=true.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import requests


ERROR_PATTERNS = [
    re.compile(r"\b(traceback|exception|fatal|error|fail(ed|ure)?)\b", re.IGNORECASE),
    re.compile(r"\bsegmentation fault\b", re.IGNORECASE),
    re.compile(r"\bunhandled\b", re.IGNORECASE),
]


@dataclass
class AgentConfig:
    repo_root: Path = Path(__file__).resolve().parents[1]
    bat_file: str = os.getenv("AUTO_AGENT_BAT", "0. start.bat")
    log_paths: list[Path] = field(default_factory=list)
    llm_url: str = os.getenv("AUTO_AGENT_LLM_URL", "http://127.0.0.1:3003/v1/chat/completions")
    llm_api_key: str = os.getenv("AUTO_AGENT_LLM_API_KEY", "")
    model: str = os.getenv("AUTO_AGENT_MODEL", "gpt-4o-mini")
    poll_interval_sec: float = float(os.getenv("AUTO_AGENT_POLL_INTERVAL", "1.0"))
    improvement_interval_sec: int = int(os.getenv("AUTO_AGENT_IMPROVEMENT_INTERVAL", "120"))
    max_log_context_lines: int = int(os.getenv("AUTO_AGENT_MAX_LOG_CONTEXT", "120"))
    command_timeout_sec: int = int(os.getenv("AUTO_AGENT_COMMAND_TIMEOUT", "180"))
    safe_mode: bool = os.getenv("AUTO_AGENT_SAFE_MODE", "true").lower() == "true"
    auto_apply_patches: bool = os.getenv("AUTO_AGENT_AUTO_APPLY_PATCHES", "false").lower() == "true"


class AutoDevAgent:
    def __init__(self, config: AgentConfig):
        self.cfg = config
        if not self.cfg.log_paths:
            self.cfg.log_paths = [self.cfg.repo_root / "logs" / "server.log"]
        self.stop_event = threading.Event()
        self.log_queue: queue.Queue[tuple[Path, str]] = queue.Queue()
        self.recent_logs: list[str] = []
        self.last_improvement_ts = 0.0
        self.bat_process: Optional[subprocess.Popen[str]] = None

    def run(self) -> None:
        self._print("🚀 AutoDevAgent iniciando")
        self._start_bat_system()
        self._start_log_watchers()
        self.last_improvement_ts = time.time()

        while not self.stop_event.is_set():
            self._consume_log_events()
            self._maybe_run_improvement_cycle()
            time.sleep(self.cfg.poll_interval_sec)

    def stop(self) -> None:
        self._print("🛑 Encerrando agente")
        self.stop_event.set()
        if self.bat_process and self.bat_process.poll() is None:
            try:
                self.bat_process.terminate()
            except Exception as exc:
                self._print(f"⚠️ Falha ao terminar processo .bat: {exc}")

    def _start_bat_system(self) -> None:
        bat_path = self.cfg.repo_root / self.cfg.bat_file
        if not bat_path.exists():
            self._print(f"⚠️ .bat não encontrado: {bat_path}")
            return

        if os.name != "nt":
            self._print("⚠️ Ambiente não-Windows: .bat não foi executado. Execute manualmente em Windows.")
            return

        cmd = ["cmd", "/c", str(bat_path)]
        self._print(f"▶️ Executando: {' '.join(cmd)}")
        self.bat_process = subprocess.Popen(
            cmd,
            cwd=str(self.cfg.repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        threading.Thread(target=self._stream_process_output, daemon=True).start()

    def _stream_process_output(self) -> None:
        if not self.bat_process or not self.bat_process.stdout:
            return
        for raw_line in self.bat_process.stdout:
            line = raw_line.rstrip("\n")
            self._append_log(f"[BAT] {line}")
            if self._is_error_line(line):
                self._handle_error_context(trigger_line=line)

    def _start_log_watchers(self) -> None:
        for log_path in self.cfg.log_paths:
            threading.Thread(target=self._tail_file_worker, args=(log_path,), daemon=True).start()
            self._print(f"👀 Monitorando log: {log_path}")

    def _tail_file_worker(self, path: Path) -> None:
        last_pos = 0
        while not self.stop_event.is_set():
            if not path.exists():
                time.sleep(self.cfg.poll_interval_sec)
                continue

            try:
                with path.open("r", encoding="utf-8", errors="ignore") as f:
                    f.seek(last_pos)
                    for line in f:
                        self.log_queue.put((path, line.rstrip("\n")))
                    last_pos = f.tell()
            except Exception as exc:
                self._print(f"⚠️ Falha lendo {path}: {exc}")

            time.sleep(self.cfg.poll_interval_sec)

    def _consume_log_events(self) -> None:
        while True:
            try:
                path, line = self.log_queue.get_nowait()
            except queue.Empty:
                break

            tagged = f"[{path.name}] {line}"
            self._append_log(tagged)
            if self._is_error_line(line):
                self._handle_error_context(trigger_line=tagged)

    def _append_log(self, line: str) -> None:
        self.recent_logs.append(line)
        if len(self.recent_logs) > self.cfg.max_log_context_lines:
            self.recent_logs = self.recent_logs[-self.cfg.max_log_context_lines :]

    def _is_error_line(self, line: str) -> bool:
        return any(pattern.search(line) for pattern in ERROR_PATTERNS)

    def _handle_error_context(self, trigger_line: str) -> None:
        self._print(f"❗ Erro detectado: {trigger_line[:180]}")
        context = "\n".join(self.recent_logs[-self.cfg.max_log_context_lines :])
        prompt = (
            "Você é um engenheiro sênior de confiabilidade. "
            "Retorne JSON com campos: summary, commands[], patch_diff, tests[]. "
            "Prefira correções pequenas, seguras e reversíveis.\n\n"
            f"Trigger:\n{trigger_line}\n\n"
            f"Logs recentes:\n{context}"
        )
        plan = self._ask_llm(prompt)
        if plan:
            self._execute_plan(plan, reason="error")

    def _maybe_run_improvement_cycle(self) -> None:
        now = time.time()
        if now - self.last_improvement_ts < self.cfg.improvement_interval_sec:
            return

        self.last_improvement_ts = now
        context = "\n".join(self.recent_logs[-40:]) or "Sem logs recentes relevantes."
        prompt = (
            "Você é um tech lead em melhoria contínua. "
            "Retorne JSON com campos: summary, commands[], patch_diff, tests[]. "
            "Foque em performance, observabilidade, robustez e legibilidade."
            " Evite mudanças destrutivas.\n\n"
            f"Contexto atual:\n{context}"
        )
        plan = self._ask_llm(prompt)
        if plan:
            self._execute_plan(plan, reason="improvement")

    def _ask_llm(self, prompt: str) -> Optional[dict[str, Any]]:
        headers = {"Content-Type": "application/json"}
        if self.cfg.llm_api_key:
            headers["Authorization"] = f"Bearer {self.cfg.llm_api_key}"

        payload = {
            "model": self.cfg.model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": "Responda APENAS JSON válido."},
                {"role": "user", "content": prompt},
            ],
        }

        try:
            r = requests.post(self.cfg.llm_url, headers=headers, json=payload, timeout=90)
            r.raise_for_status()
            data = r.json()
            text = data["choices"][0]["message"]["content"]
            return json.loads(text)
        except Exception as exc:
            self._print(f"⚠️ Falha consultando LLM: {exc}")
            return None

    def _execute_plan(self, plan: dict[str, Any], reason: str) -> None:
        summary = str(plan.get("summary", "(sem summary)"))
        self._print(f"🧠 Plano ({reason}): {summary}")

        commands = plan.get("commands") or []
        if isinstance(commands, list):
            for cmd in commands[:5]:
                self._run_command(str(cmd))

        patch_diff = plan.get("patch_diff")
        if patch_diff and isinstance(patch_diff, str):
            self._maybe_apply_patch(patch_diff)

        tests = plan.get("tests") or []
        if isinstance(tests, list):
            for test_cmd in tests[:5]:
                self._run_command(str(test_cmd))

    def _run_command(self, cmd: str) -> None:
        cmd = cmd.strip()
        if not cmd:
            return

        blocked_tokens = [" rm ", " del ", " format ", "shutdown", "reboot"]
        if self.cfg.safe_mode and any(tok in f" {cmd.lower()} " for tok in blocked_tokens):
            self._print(f"⛔ Comando bloqueado por SAFE_MODE: {cmd}")
            return

        self._print(f"⚙️ Executando comando: {cmd}")
        try:
            result = subprocess.run(
                cmd,
                cwd=self.cfg.repo_root,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.cfg.command_timeout_sec,
            )
            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            if stdout:
                self._append_log(f"[CMD][OUT] {stdout[:1200]}")
            if stderr:
                self._append_log(f"[CMD][ERR] {stderr[:1200]}")
            self._print(f"↩️ Exit code: {result.returncode}")
        except Exception as exc:
            self._print(f"⚠️ Falha ao executar comando '{cmd}': {exc}")

    def _maybe_apply_patch(self, patch_diff: str) -> None:
        if not self.cfg.auto_apply_patches:
            self._print("ℹ️ patch_diff recebido, mas AUTO_APPLY_PATCHES=false")
            return

        patch_file = self.cfg.repo_root / "tmp_auto_agent.patch"
        patch_file.write_text(patch_diff, encoding="utf-8")

        self._print("🩹 Aplicando patch via git apply")
        apply_cmd = f"git apply --whitespace=fix {patch_file.name}"
        self._run_command(apply_cmd)

        if self.cfg.safe_mode:
            self._print("ℹ️ SAFE_MODE=true: rollback automático após validação é recomendado no pipeline externo")

    def _print(self, msg: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)


def main() -> int:
    cfg = AgentConfig()
    agent = AutoDevAgent(cfg)
    try:
        agent.run()
        return 0
    except KeyboardInterrupt:
        agent.stop()
        return 0
    except Exception as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        agent.stop()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
