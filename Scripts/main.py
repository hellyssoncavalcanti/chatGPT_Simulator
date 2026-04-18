# =============================================================================
# main.py — Ponto de entrada do ChatGPT Simulator
# =============================================================================
#
# RESPONSABILIDADE:
#   Inicializa todos os componentes do sistema em threads separadas e sobe
#   os servidores Flask. Também garante que a .venv exista/esteja saudável,
#   instala dependências ausentes e prepara o Chromium do Playwright antes
#   de importar os módulos principais.
# =============================================================================
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPTS_DIR)
VENV_DIR = os.path.join(BASE_DIR, ".venv")
VENV_PYVENV_CFG = os.path.join(VENV_DIR, "pyvenv.cfg")
IS_WINDOWS = os.name == "nt"
VENV_PYTHON = os.path.join(VENV_DIR, "Scripts" if IS_WINDOWS else "bin", "python.exe" if IS_WINDOWS else "python")
CORE_DEPENDENCIES = [
    ("flask", "flask"),
    ("flask-cors", "flask_cors"),
    ("playwright", "playwright"),
    ("markdownify", "markdownify"),
    ("requests", "requests"),
    ("pystray", "pystray"),
    ("pillow", "PIL"),
    ("cryptography", "cryptography"),
]
REPAIR_FLAG = "--repair-venv"
SKIP_BOOTSTRAP_FLAG = "--skip-bootstrap"


def _terminate_previous_same_server_instances(script_name: str) -> None:
    """Fecha processos antigos do mesmo servidor, incluindo a janela CMD/.bat anterior."""
    if os.name != "nt":
        return

    current_pid = os.getpid()
    escaped_script = re.escape(script_name).replace("'", "''")
    extra_shell_tokens = {
        "main.py": ["0. start.bat", "0.start.bat", "start.bat"],
    }
    shell_tokens = [script_name, *extra_shell_tokens.get(script_name.lower(), [])]
    shell_regex = "|".join(re.escape(token) for token in shell_tokens).replace("'", "''")

    ps_cmd_shells = (
        f"$self={current_pid}; "
        "$selfParent=(Get-CimInstance Win32_Process -Filter \"ProcessId = $self\" | Select-Object -ExpandProperty ParentProcessId); "
        "Get-CimInstance Win32_Process "
        "| Where-Object { "
        "($_.Name -match '^(cmd|powershell|pwsh)\\.exe$') -and "
        "($_.ProcessId -ne $selfParent) -and "
        "($_.CommandLine -match '(?i)(" + shell_regex + ")') "
        "} "
        "| Select-Object -ExpandProperty ProcessId"
    )

    ps_cmd_python = (
        f"$self={current_pid}; "
        "Get-CimInstance Win32_Process "
        "| Where-Object { "
        "($_.Name -match 'python|py') -and "
        "($_.ProcessId -ne $self) -and "
        "($_.CommandLine -match '(?i)" + escaped_script + "') "
        "} "
        "| Select-Object -ExpandProperty ProcessId"
    )
    ps_cmd_ancestors = (
        f"$p={current_pid}; "
        "while ($p -and $p -ne 0) { "
        "  $proc=Get-CimInstance Win32_Process -Filter (\"ProcessId = \" + $p); "
        "  if (-not $proc) { break }; "
        "  $pp=$proc.ParentProcessId; "
        "  if ($pp -and $pp -ne 0) { Write-Output $pp }; "
        "  $p=$pp "
        "}"
    )

    try:
        ancestors_proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd_ancestors],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
        protected_pids = {current_pid}
        protected_pids.update(
            int(pid_txt.strip())
            for pid_txt in (ancestors_proc.stdout or "").splitlines()
            if pid_txt.strip().isdigit()
        )

        shell_proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd_shells],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
        shell_targets = {
            int(pid_txt.strip())
            for pid_txt in (shell_proc.stdout or "").splitlines()
            if pid_txt.strip().isdigit()
        }
        shell_killed = 0
        for pid in sorted(shell_targets):
            if pid in protected_pids:
                continue
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            shell_killed += 1
            print(f"[BOOT] Janela CMD anterior do servidor foi finalizada (PID {pid}) para {script_name}.")

        py_proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd_python],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
        py_targets = {
            int(pid_txt.strip())
            for pid_txt in (py_proc.stdout or "").splitlines()
            if pid_txt.strip().isdigit()
        }
        py_killed = 0
        for pid in sorted(py_targets):
            if pid in protected_pids:
                continue
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            py_killed += 1
            print(f"[BOOT] Processo Python anterior finalizado (PID {pid}) para {script_name}.")

        print(
            f"[BOOT] Substituição de instâncias de {script_name}: "
            f"shells_encontrados={len(shell_targets)} shells_finalizados={shell_killed} "
            f"python_encontrados={len(py_targets)} python_finalizados={py_killed} "
            f"pids_protegidos={len(protected_pids)}"
        )
    except Exception as exc:
        print(f"[BOOT] Aviso: não foi possível substituir instâncias anteriores de {script_name}: {exc}")

# ─────────────────────────────────────────────────────────────
# CAPTURA CONFIGURAÇÃO DE DEBUG (que é estabelecida no arquivo "config.py").
# ─────────────────────────────────────────────────────────────
# Verifica se config já foi importado; se não, importa
if 'config' not in sys.modules:
    import config

# Tenta importar DEBUG_LOG do módulo config já carregado
try:
    DEBUG_LOG = config.DEBUG_LOG
except AttributeError:
    DEBUG_LOG = False  # fallback se a variável não existir no config
    print("⚠️ DEBUG_LOG não encontrado no config.py. Usando False como padrão.")


def _same_path(path_a: str, path_b: str) -> bool:
    return os.path.normcase(os.path.abspath(path_a)) == os.path.normcase(os.path.abspath(path_b))


def _current_python_is_venv() -> bool:
    try:
        return _same_path(sys.executable, VENV_PYTHON)
    except Exception:
        return False


def _venv_is_healthy() -> bool:
    return all(
        os.path.exists(path)
        for path in (VENV_DIR, VENV_PYVENV_CFG, VENV_PYTHON)
    )


def _resolve_bootstrap_python_cmd() -> list:
    candidates = []

    base_executable = getattr(sys, "_base_executable", None)
    if base_executable and os.path.exists(base_executable):
        candidates.append([base_executable])

    if sys.executable and os.path.exists(sys.executable) and not _current_python_is_venv():
        candidates.append([sys.executable])

    py_launcher = shutil.which("py") if IS_WINDOWS else None
    if py_launcher:
        candidates.append([py_launcher, "-3"])

    python_cmd = shutil.which("python3") or shutil.which("python")
    if python_cmd:
        candidates.append([python_cmd])

    seen = set()
    for candidate in candidates:
        key = tuple(candidate)
        if key in seen:
            continue
        seen.add(key)
        try:
            proc = subprocess.run(
                candidate + ["-c", "import sys; print(sys.executable)"],
                cwd=BASE_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            resolved = proc.stdout.strip()
            if resolved:
                return [resolved]
            return candidate
        except Exception:
            continue

    raise RuntimeError(
        "Nenhum interpretador Python base foi encontrado para recriar a .venv. "
        "Instale o Python 3 e garanta que `python`, `python3` ou `py -3` estejam disponíveis no PATH."
    )


def _run_checked(cmd: list, description: str, quiet: bool = False):
    print(f"[BOOT] {description}...")
    stdout = subprocess.DEVNULL if quiet else None
    stderr = subprocess.DEVNULL if quiet else None
    subprocess.check_call(cmd, cwd=BASE_DIR, stdout=stdout, stderr=stderr)


def _recreate_venv(base_python_cmd: list):
    print("[BOOT] Ambiente virtual ausente/corrompido. Recriando .venv...")
    subprocess.check_call(base_python_cmd + ["-m", "venv", VENV_DIR, "--clear"], cwd=BASE_DIR)


def _missing_dependencies(python_cmd: list) -> list:
    missing = []
    for package, import_name in CORE_DEPENDENCIES:
        try:
            proc = subprocess.run(
                python_cmd + ["-c", f"import importlib.util, sys; sys.exit(0 if importlib.util.find_spec({import_name!r}) else 1)"],
                cwd=BASE_DIR,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if proc.returncode != 0:
                missing.append(package)
        except Exception:
            missing.append(package)
    return missing


def _ensure_dependencies(python_cmd: list):
    missing = _missing_dependencies(python_cmd)
    if missing:
        _run_checked(
            python_cmd + ["-m", "pip", "install", "--upgrade", "pip"],
            "Atualizando pip da .venv",
            quiet=True,
        )
        _run_checked(
            python_cmd + ["-m", "pip", "install", "--upgrade", *missing],
            f"Instalando/atualizando dependências ({', '.join(missing)})",
        )
    else:
        print("[BOOT] Dependências Python já estão instaladas.")


def _ensure_playwright_browser(python_cmd: list):
    try:
        subprocess.run(
            python_cmd + ["-m", "playwright", "install", "chromium"],
            cwd=BASE_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        print("[BOOT] Chromium do Playwright verificado.")
    except Exception:
        print("[BOOT] Aviso: não foi possível validar/instalar o Chromium automaticamente.")


def _relaunch_into_venv(argv: list):
    cmd = [VENV_PYTHON, os.path.abspath(__file__), SKIP_BOOTSTRAP_FLAG, *argv]
    print("[BOOT] Reiniciando o ChatGPT Simulator dentro da .venv...")
    raise SystemExit(subprocess.call(cmd, cwd=BASE_DIR))


def ensure_runtime_environment():
    argv = [arg for arg in sys.argv[1:] if arg not in {REPAIR_FLAG, SKIP_BOOTSTRAP_FLAG}]
    if SKIP_BOOTSTRAP_FLAG in sys.argv:
        return argv

    current_is_venv = _current_python_is_venv()
    venv_is_healthy = _venv_is_healthy()

    if REPAIR_FLAG in sys.argv:
        base_python_cmd = _resolve_bootstrap_python_cmd()
        _recreate_venv(base_python_cmd)
        _ensure_dependencies([VENV_PYTHON])
        _ensure_playwright_browser([VENV_PYTHON])
        _relaunch_into_venv(argv)

    if not venv_is_healthy:
        if current_is_venv:
            base_python_cmd = _resolve_bootstrap_python_cmd()
            print("[BOOT] .venv inválida detectada durante a inicialização. Delegando reparo ao Python base...")
            raise SystemExit(
                subprocess.call(base_python_cmd + [os.path.abspath(__file__), REPAIR_FLAG, *argv], cwd=BASE_DIR)
            )

        base_python_cmd = _resolve_bootstrap_python_cmd()
        _recreate_venv(base_python_cmd)
        current_is_venv = False

    _ensure_dependencies([VENV_PYTHON])
    _ensure_playwright_browser([VENV_PYTHON])

    if not current_is_venv:
        _relaunch_into_venv(argv)

    return argv


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def start_browser_thread(browser_module):
    try:
        browser_module.browser_loop()
    except Exception as exc:
        print(f"[ERRO] Falha no browser worker: {exc}")


def start_http_server(config_module, server_module):
    http_port = config_module.PORT + 1
    try:
        server_module.app.run(host="0.0.0.0", port=http_port, debug=False, use_reloader=False)
    except Exception as exc:
        print(f"[ERRO] Falha ao iniciar HTTP na porta {http_port}: {exc}")


def _wait_for_port(host: str, port: int, timeout: int = 180, interval: float = 0.5) -> tuple[bool, float]:
    try:
        timeout = max(1, int(timeout))
    except Exception:
        timeout = 180
    try:
        interval = max(0.1, float(interval))
    except Exception:
        interval = 0.5
    started_at = time.perf_counter()
    deadline = started_at + timeout
    while True:
        now = time.perf_counter()
        if now >= deadline:
            break
        try:
            connect_timeout = min(2.0, max(0.2, deadline - now))
            with socket.create_connection((host, port), timeout=connect_timeout):
                return True, (time.perf_counter() - started_at)
        except OSError:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))
    return False, (time.perf_counter() - started_at)


def open_urls_when_server_is_ready(port: int, urls: list, startup_timeout: int = 180):
    def _worker():
        is_ready, waited_seconds = _wait_for_port("127.0.0.1", port, timeout=startup_timeout)
        if not is_ready:
            print(
                f"[BOOT] Aviso: servidor HTTPS na porta {port} não ficou pronto após "
                f"{waited_seconds:.1f}s; navegador não será aberto automaticamente."
            )
            return

        print(f"[BOOT] Servidor HTTPS na porta {port} ficou pronto após {waited_seconds:.1f}s.")
        time.sleep(1.0)
        normalized_urls = []
        seen = set()
        for raw_url in (urls or []):
            url = str(raw_url or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            normalized_urls.append(url)

        if not normalized_urls:
            print("[BOOT] Aviso: nenhuma URL válida para abrir automaticamente.")
            return

        for url in normalized_urls:
            try:
                webbrowser.open_new(url)
            except Exception as exc:
                print(f"[BOOT] Aviso: falha ao abrir {url}: {exc}")

    t_open = threading.Thread(target=_worker, daemon=True)
    t_open.start()


if __name__ == "__main__":
    _terminate_previous_same_server_instances("main.py")
    cleaned_argv = ensure_runtime_environment()
    sys.argv = [sys.argv[0], *cleaned_argv]

    sys.path.append(SCRIPTS_DIR)

    import config
    import server
    import browser
    import utils
    from shared import browser_queue

    os.system('cls' if os.name == 'nt' else 'clear')

    print(f"\n=== CHATGPT SIMULATOR v{config.VERSION} (Async Tabs) ===")
    print("[INFO] Inicializando sistema...")
    print(f"[INFO] Processo atual: PID={os.getpid()} PPID={os.getppid()}")

    utils.ensure_certificates()

    t_browser = threading.Thread(target=start_browser_thread, args=(browser,), name="browser-worker")
    t_browser.daemon = True
    t_browser.start()
    print(f"[INFO] Thread iniciada: {t_browser.name}")

    t_http = threading.Thread(target=start_http_server, args=(config, server), name="http-server")
    t_http.daemon = True
    t_http.start()
    print(f"[INFO] Thread iniciada: {t_http.name}")

    time.sleep(0.5)
    print(f"[INFO] Status thread {t_browser.name}: {'alive' if t_browser.is_alive() else 'dead'}")
    print(f"[INFO] Status thread {t_http.name}: {'alive' if t_http.is_alive() else 'dead'}")
    if not t_browser.is_alive():
        print(f"[WARN] Thread {t_browser.name} encerrou logo após a inicialização.")
    if not t_http.is_alive():
        print(f"[WARN] Thread {t_http.name} encerrou logo após a inicialização.")

    utils.setup_frontend()

    local_ip = get_local_ip()
    local_https_url = f"https://localhost:{config.PORT}"
    print("\n[SERVIDOR ONLINE]")
    print(f" 🔒 HTTPS (Seguro):   {local_https_url}")
    print(f" 🌍 HTTP (Remoto):    http://{local_ip}:{config.PORT + 1}")
    print("\n[ADMIN] User: admin | Pass: 32713091")
    print("--------------------------------------------------\n")

    open_urls_when_server_is_ready(config.PORT, [local_https_url])

    try:
        ssl_context = (config.CERT_FILE, config.KEY_FILE)
        server.app.run(host="0.0.0.0", port=config.PORT, debug=False, use_reloader=False, ssl_context=ssl_context)
    except KeyboardInterrupt:
        print("\n[INFO] Encerramento solicitado pelo usuário.")
    except Exception as e:
        print(f"[ERRO] Falha ao iniciar HTTPS: {e}")
    finally:
        try:
            browser_queue.put({'action': 'STOP'})
            t_browser.join(timeout=8)
            print("[INFO] Sinal de parada enviado ao browser worker.")
        except Exception as e:
            print(f"[WARN] Falha ao sinalizar parada do browser worker: {e}")
