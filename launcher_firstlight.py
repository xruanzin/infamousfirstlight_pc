import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import shutil
import zipfile
import urllib.error
import urllib.request
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText


APP_TITLE = "inFAMOUS First Light PC Launcher"
CREATE_NO_WINDOW = 0x08000000
UPDATE_MANIFEST_URL = (
    "https://raw.githubusercontent.com/xruanzin/infamousfirstlight_pc/"
    "firstlight-bootstrap/runtime/update_manifest.json"
)
HTTP_USER_AGENT = "FirstLightLauncher/3.1"
# Rolling launcher channel enabled.
MAX_LIVE_LOG_CHARS = 350_000
MAX_EVENTS_PER_TICK = 80
POLL_BUDGET_SECONDS = 0.015

BACKEND_D3D12 = "DirectX 12 (Mesa DZN)"
BACKEND_VULKAN = "Vulkan nativo (AMD)"


def portable_root():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


class FirstLightLauncher:
    def __init__(self, root):
        self.root = root
        self.base = portable_root()
        self.user_dir = os.path.join(self.base, "user")
        self.runtime_dir = os.path.join(self.base, "runtime")
        self.config_path = os.path.join(self.user_dir, "launcher.json")
        self.update_state_path = os.path.join(self.user_dir, "launcher_update.json")
        self.diagnosis_path = os.path.join(self.user_dir, "last_run_diagnosis.json")
        self.log_path = os.path.join(self.user_dir, "last_run.log")
        self.events = queue.Queue()
        self.process = None
        self.reader = None
        self.waiter = None
        self.process_generation = 0
        self.finished_generation = -1
        self.updating = False
        self.last_exit_code = None

        os.makedirs(self.user_dir, exist_ok=True)
        os.makedirs(self.runtime_dir, exist_ok=True)

        self.game_dir = tk.StringVar()
        self.backend = tk.StringVar(value=BACKEND_D3D12)
        self.status = tk.StringVar(value="Pronto.")
        self.reason = tk.StringVar(value="Nenhum jogo executado nesta sessão.")
        self.runtime_status = tk.StringVar(value=self.runtime_description())

        root.title(APP_TITLE)
        root.minsize(900, 620)
        root.geometry("1020x730")
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.build_ui()
        self.load_config()
        self.show_previous_diagnosis()
        self.root.after(50, self.poll_events)

    def build_ui(self):
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text="inFAMOUS First Light",
            font=("Segoe UI Semibold", 18),
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text="Selecione a pasta do jogo uma vez. O launcher salva o caminho, inicia o port, mostra o log e aplica hotfixes na própria pasta.",
        ).pack(anchor="w", pady=(2, 12))

        path_frame = ttk.LabelFrame(outer, text="Diretório do jogo", padding=10)
        path_frame.pack(fill="x")

        self.path_entry = ttk.Entry(path_frame, textvariable=self.game_dir)
        self.path_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(path_frame, text="Procurar...", command=self.browse_game).pack(side="left", padx=(8, 0))

        backend_frame = ttk.LabelFrame(outer, text="Backend gráfico", padding=10)
        backend_frame.pack(fill="x", pady=(10, 0))
        ttk.Label(
            backend_frame,
            text="DirectX 12 usa Mesa DZN (Vulkan → D3D12) e evita o ICD amdvlk64.dll.",
        ).pack(side="left")
        self.backend_combo = ttk.Combobox(
            backend_frame,
            textvariable=self.backend,
            values=(BACKEND_D3D12, BACKEND_VULKAN),
            state="readonly",
            width=30,
        )
        self.backend_combo.pack(side="right")
        self.backend_combo.bind("<<ComboboxSelected>>", lambda _event: self.save_config())

        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=10)

        self.start_button = ttk.Button(controls, text="Iniciar jogo", command=self.start_game)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Encerrar", command=self.stop_game, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        self.update_button = ttk.Button(controls, text="Atualizar port", command=self.update_port)
        self.update_button.pack(side="left", padx=(18, 0))
        ttk.Button(controls, text="Abrir pasta de logs", command=self.open_logs).pack(side="left", padx=(18, 0))
        ttk.Button(controls, text="Copiar relatório", command=self.copy_report).pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="Salvar relatório...", command=self.save_report).pack(side="left", padx=(8, 0))

        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill="x")

        runtime_box = ttk.LabelFrame(outer, text="Atualização modular", padding=10)
        runtime_box.pack(fill="x", pady=(10, 0))
        ttk.Label(runtime_box, textvariable=self.runtime_status, wraplength=930, justify="left").pack(anchor="w")

        info = ttk.LabelFrame(outer, text="Diagnóstico", padding=10)
        info.pack(fill="x", pady=(10, 10))
        ttk.Label(info, textvariable=self.status, font=("Segoe UI Semibold", 10)).pack(anchor="w")
        ttk.Label(info, textvariable=self.reason, wraplength=930, justify="left").pack(anchor="w", pady=(4, 0))

        log_frame = ttk.LabelFrame(outer, text="Log ao vivo", padding=6)
        log_frame.pack(fill="both", expand=True)
        self.log = ScrolledText(log_frame, wrap="word", font=("Consolas", 9), state="disabled")
        self.log.pack(fill="both", expand=True)

    def dzn_manifest_path(self):
        return os.path.join(self.runtime_dir, "dzn", "dzn_icd.x86_64.json")

    def dzn_ready(self):
        dzn_dir = os.path.join(self.runtime_dir, "dzn")
        return (
            os.path.isfile(self.dzn_manifest_path())
            and os.path.isfile(os.path.join(dzn_dir, "vulkan_dzn.dll"))
        )

    def runtime_description(self):
        aliases = os.path.isfile(os.path.join(self.runtime_dir, "import_aliases.tsv"))
        native = os.path.isfile(os.path.join(self.runtime_dir, "firstlight_runtime.dll"))
        dzn = self.dzn_ready()
        return (
            "Hotfixes locais: aliases externos %s; módulo nativo %s; DirectX 12/DZN %s. "
            "O botão Atualizar port substitui somente arquivos do port, sem apagar jogo, saves, cache ou configuração."
            % (
                "ativo" if aliases else "ausente",
                "ativo" if native else "ausente",
                "disponível" if dzn else "ausente",
            )
        )

    def load_config(self):
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            value = data.get("game_dir")
            if isinstance(value, str):
                self.game_dir.set(value)
            backend = data.get("backend")
            if backend in (BACKEND_D3D12, BACKEND_VULKAN):
                self.backend.set(backend)
        except (OSError, ValueError, TypeError):
            pass

    def save_config(self):
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "game_dir": self.game_dir.get().strip(),
                        "backend": self.backend.get(),
                    },
                    f,
                    indent=2,
                )
        except OSError:
            pass

    def browse_game(self):
        initial = self.game_dir.get().strip()
        if not os.path.isdir(initial):
            initial = os.path.expanduser("~/Downloads")
        chosen = filedialog.askdirectory(title="Selecione a pasta extraída de inFAMOUS First Light", initialdir=initial)
        if chosen:
            self.game_dir.set(os.path.normpath(chosen))
            self.save_config()
            self.status.set("Diretório salvo.")

    def bootstrap_env(self):
        # Read the external compatibility profile in the launcher too. This lets a launcher
        # hotfix inject new FL_*/BB_* defaults into an older FirstLightBootstrap.exe, because
        # that bootstrap preserves values already present in its environment.
        env = os.environ.copy()
        path = os.path.join(self.runtime_dir, "firstlight_config.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            values = data.get("env_defaults", {})
            if data.get("schema") == 1 and isinstance(values, dict):
                for key, value in values.items():
                    if isinstance(key, str) and isinstance(value, (str, int, float, bool)):
                        env.setdefault(key, str(value))
        except (OSError, ValueError, TypeError):
            pass

        if self.backend.get() == BACKEND_D3D12:
            dzn_dir = os.path.join(self.runtime_dir, "dzn")
            manifest = os.path.abspath(self.dzn_manifest_path())
            # VK_DRIVER_FILES overrides normal Vulkan ICD discovery. With only the DZN
            # manifest listed, amdvlk64.dll is not selected by the Vulkan loader.
            env["VK_DRIVER_FILES"] = manifest
            env["VK_ICD_FILENAMES"] = manifest
            env["PATH"] = dzn_dir + os.pathsep + env.get("PATH", "")
            env["FL_GPU_BACKEND"] = "dzn-d3d12"
            env.setdefault("MESA_D3D12_DEFAULT_ADAPTER_NAME", "AMD Radeon")

            # Keep third-party Vulkan overlays/injectors out of the DZN chain. RTSS/MSI
            # Afterburner, capture tools and other implicit layers can hook vkCreateInstance
            # before DZN and crash even though the AMD Vulkan ICD is no longer loaded.
            # This is the Vulkan loader's documented global implicit-layer filter.
            env["VK_LOADER_LAYERS_DISABLE"] = "~implicit~"
            env["VK_LOADER_DEBUG"] = "error,warn,driver"
            env.pop("VK_LOADER_LAYERS_ENABLE", None)
            env.pop("VK_LOADER_LAYERS_ALLOW", None)
            env.pop("VK_INSTANCE_LAYERS", None)
        else:
            # Do not inherit an override created by a previous launcher invocation.
            env.pop("VK_DRIVER_FILES", None)
            env.pop("VK_ICD_FILENAMES", None)
            env.pop("VK_LOADER_LAYERS_DISABLE", None)
            env["FL_GPU_BACKEND"] = "native-vulkan"
        return env

    def validate_game(self, path):
        if not path or not os.path.isdir(path):
            return "Selecione uma pasta válida do jogo."
        required = [
            "eboot.bin",
            os.path.join("sce_sys", "param.sfo"),
            os.path.join("sce_module", "libc.prx"),
            os.path.join("sce_module", "libSceFios2.prx"),
        ]
        missing = [item for item in required if not os.path.isfile(os.path.join(path, item))]
        if missing:
            return "A pasta não parece ser o dump correto. Faltando: " + ", ".join(missing)
        return None

    def append_log(self, text):
        # GPU tracing can produce very large physical lines. Keeping an unbounded Tk Text
        # widget and draining the whole queue in one UI callback made the launcher appear
        # frozen after a driver crash. The complete log is already written by the bootstrap;
        # the launcher only needs a useful live tail.
        if not text:
            return
        if len(text) > 80_000:
            text = "[... trecho muito grande omitido da visualização; o arquivo de log mantém tudo ...]\n" + text[-80_000:]
        self.log.configure(state="normal")
        self.log.insert("end", text)
        try:
            count = int(self.log.count("1.0", "end-1c", "chars")[0])
        except (tk.TclError, TypeError, ValueError):
            count = 0
        if count > MAX_LIVE_LOG_CHARS:
            trim = count - MAX_LIVE_LOG_CHARS
            self.log.delete("1.0", "1.0 + %d chars" % trim)
            self.log.insert("1.0", "[... início removido da visualização; log completo salvo em user\\last_run.log ...]\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def set_busy(self, busy):
        if busy:
            self.start_button.configure(state="disabled")
            self.update_button.configure(state="disabled")
            self.progress.start(10)
        else:
            self.start_button.configure(state="normal")
            self.update_button.configure(state="normal")
            self.progress.stop()

    def start_game(self):
        if self.updating or (self.process and self.process.poll() is None):
            return

        game = os.path.abspath(self.game_dir.get().strip().strip('"'))
        error = self.validate_game(game)
        if error:
            messagebox.showerror("Diretório inválido", error)
            self.status.set("Não iniciado.")
            self.reason.set(error)
            return

        if self.backend.get() == BACKEND_D3D12 and not self.dzn_ready():
            messagebox.showerror(
                "Backend DirectX 12 não instalado",
                "O Mesa DZN não está instalado nesta pasta. Clique em 'Atualizar port' e tente novamente.",
            )
            self.status.set("DirectX 12/DZN ausente.")
            self.reason.set("Atualize o port para baixar runtime\\dzn.")
            return

        bootstrap = os.path.join(self.base, "FirstLightBootstrap.exe")
        if not os.path.isfile(bootstrap):
            messagebox.showerror(
                "Arquivo ausente",
                "FirstLightBootstrap.exe não foi encontrado ao lado do launcher. Extraia o pacote portátil completo.",
            )
            return

        self.game_dir.set(game)
        self.save_config()
        self.clear_log()
        self.last_exit_code = None
        self.status.set("Iniciando o jogo...")
        self.reason.set("O log será analisado automaticamente se o jogo parar ou fechar.")
        self.set_busy(True)
        self.stop_button.configure(state="normal")

        flags = CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                [bootstrap, game, "--console"],
                cwd=self.base,
                env=self.bootstrap_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=flags,
            )
            self.process = process
            self.process_generation += 1
            generation = self.process_generation
        except OSError as exc:
            self.finish_launch(None, "Não foi possível iniciar FirstLightBootstrap.exe: %s" % exc)
            return

        # Output and process lifetime are deliberately independent. A crashed GPU/driver can
        # leave an inherited pipe handle open even after the bootstrap process is gone; waiting
        # for EOF before noticing process exit used to leave the launcher stuck.
        self.reader = threading.Thread(target=self.read_output, args=(process, generation), daemon=True)
        self.waiter = threading.Thread(target=self.wait_process, args=(process, generation), daemon=True)
        self.reader.start()
        self.waiter.start()

    def read_output(self, process, generation):
        try:
            if process.stdout:
                for line in process.stdout:
                    self.events.put(("line", (generation, line)))
        except Exception as exc:
            self.events.put(("reader_error", (generation, str(exc))))

    def wait_process(self, process, generation):
        try:
            code = process.wait()
            self.events.put(("done", (generation, code)))
        except Exception as exc:
            self.events.put(("reader_error", (generation, str(exc))))

    def cache_bust_url(self, url):
        separator = "&" if "?" in url else "?"
        return "%s%snocache=%d" % (url, separator, time.time_ns())

    def http_bytes(self, url, cache_bust=False):
        if cache_bust:
            url = self.cache_bust_url(url)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": HTTP_USER_AGENT,
                "Cache-Control": "no-cache, no-store, max-age=0",
                "Pragma": "no-cache",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()

    def atomic_replace(self, path, data):
        folder = os.path.dirname(path)
        os.makedirs(folder, exist_ok=True)
        temp = path + ".new"
        with open(temp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)

    def install_zip_bundle(self, archive_path, extract_rel):
        target = os.path.abspath(os.path.join(self.base, extract_rel.replace("/", os.sep)))
        base_prefix = os.path.abspath(self.base) + os.sep
        if not target.startswith(base_prefix):
            raise RuntimeError("destino de bundle inválido: " + extract_rel)
        temp = target + ".new"
        shutil.rmtree(temp, ignore_errors=True)
        os.makedirs(temp, exist_ok=True)
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                for member in zf.infolist():
                    member_target = os.path.abspath(os.path.join(temp, member.filename))
                    temp_prefix = os.path.abspath(temp) + os.sep
                    if member_target != os.path.abspath(temp) and not member_target.startswith(temp_prefix):
                        raise RuntimeError("arquivo inseguro no bundle: " + member.filename)
                zf.extractall(temp)
            if os.path.isdir(target):
                shutil.rmtree(target)
            os.replace(temp, target)
        except Exception:
            shutil.rmtree(temp, ignore_errors=True)
            raise

    def update_port(self):
        if self.updating or (self.process and self.process.poll() is None):
            messagebox.showinfo("Atualizar port", "Encerre o jogo antes de aplicar uma atualização.")
            return
        self.updating = True
        self.set_busy(True)
        self.status.set("Verificando hotfixes...")
        self.reason.set("Somente os arquivos modificados serão gravados na pasta atual.")
        threading.Thread(target=self.update_worker, daemon=True).start()

    def update_worker(self):
        try:
            manifest_data = self.http_bytes(UPDATE_MANIFEST_URL, cache_bust=True)
            manifest = json.loads(manifest_data.decode("utf-8"))
            if manifest.get("schema") != 1 or not isinstance(manifest.get("files"), list):
                raise RuntimeError("manifesto de atualização incompatível")

            changed = []
            unchanged = []
            skipped = []
            pending_launcher = None
            for item in manifest["files"]:
                rel = item.get("path")
                url = item.get("url")
                required = bool(item.get("required", False))
                kind = item.get("kind")
                extract_to = item.get("extract_to")
                if not isinstance(rel, str) or not rel or not isinstance(url, str) or not url:
                    continue
                self.events.put(("update_status", "Baixando " + rel + "..."))
                try:
                    data = self.http_bytes(url, cache_bust=True)
                except urllib.error.HTTPError as exc:
                    if not required and exc.code == 404:
                        skipped.append(rel)
                        continue
                    raise
                expected = item.get("sha256")
                remote_sha256 = hashlib.sha256(data).hexdigest().lower()
                if expected and remote_sha256 != str(expected).lower():
                    raise RuntimeError(
                        "SHA-256 inválido para %s (manifesto=%s baixado=%s)"
                        % (rel, str(expected).lower(), remote_sha256)
                    )

                destination = os.path.abspath(os.path.join(self.base, rel.replace("/", os.sep)))
                base_prefix = os.path.abspath(self.base) + os.sep
                if not destination.startswith(base_prefix):
                    raise RuntimeError("caminho inválido no manifesto: " + rel)

                old = None
                local_sha256 = None
                try:
                    with open(destination, "rb") as f:
                        old = f.read()
                    local_sha256 = hashlib.sha256(old).hexdigest().lower()
                except OSError:
                    pass
                if local_sha256 == remote_sha256:
                    if kind == "zip_bundle" and isinstance(extract_to, str) and extract_to:
                        bundle_marker = os.path.join(
                            self.base, extract_to.replace("/", os.sep), "dzn_icd.x86_64.json"
                        )
                        if not os.path.isfile(bundle_marker):
                            self.install_zip_bundle(destination, extract_to)
                    unchanged.append(rel)
                    continue

                current_exe = os.path.normcase(os.path.abspath(sys.executable))
                is_self = (
                    getattr(sys, "frozen", False)
                    and os.path.normcase(destination) == current_exe
                )
                if is_self:
                    stage_dir = os.path.join(self.user_dir, ".launcher-update")
                    os.makedirs(stage_dir, exist_ok=True)
                    staged = os.path.join(stage_dir, os.path.basename(destination))
                    self.atomic_replace(staged, data)
                    pending_launcher = {"staged": staged, "target": destination}
                    changed.append(rel + " (reinício pendente)")
                else:
                    self.atomic_replace(destination, data)
                    with open(destination, "rb") as f:
                        installed_sha256 = hashlib.sha256(f.read()).hexdigest().lower()
                    if installed_sha256 != remote_sha256:
                        raise RuntimeError(
                            "falha ao verificar arquivo instalado: %s (esperado=%s instalado=%s)"
                            % (rel, remote_sha256, installed_sha256)
                        )
                    if kind == "zip_bundle" and isinstance(extract_to, str) and extract_to:
                        self.install_zip_bundle(destination, extract_to)
                    changed.append(rel)

            state = {
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "channel": manifest.get("channel"),
                "changed": changed,
                "unchanged": unchanged,
                "skipped": skipped,
                "pending_launcher": pending_launcher,
            }
            with open(self.update_state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            self.events.put(("update_done", state))
        except Exception as exc:
            self.events.put(("update_error", str(exc)))

    def poll_events(self):
        # Never drain an unlimited trace queue inside one Tk callback. This keeps buttons,
        # repainting and crash diagnosis responsive even when Vulkan emits hundreds of KB.
        started = time.monotonic()
        handled = 0
        chunks = []
        try:
            while handled < MAX_EVENTS_PER_TICK and time.monotonic() - started < POLL_BUDGET_SECONDS:
                kind, payload = self.events.get_nowait()
                handled += 1
                if kind == "line":
                    generation, line = payload
                    if generation == self.process_generation:
                        chunks.append(line)
                else:
                    if chunks:
                        self.append_log("".join(chunks))
                        chunks = []
                    if kind == "done":
                        generation, code = payload
                        if generation == self.process_generation and generation != self.finished_generation:
                            self.finished_generation = generation
                            self.finish_launch(code)
                    elif kind == "reader_error":
                        generation, error = payload
                        if generation == self.process_generation and generation != self.finished_generation:
                            self.finished_generation = generation
                            self.finish_launch(None, "Falha ao capturar o log: " + error)
                    elif kind == "update_status":
                        self.status.set(payload)
                    elif kind == "update_done":
                        self.finish_update(payload)
                    elif kind == "update_error":
                        self.updating = False
                        self.set_busy(False)
                        self.status.set("Falha ao atualizar.")
                        self.reason.set(payload)
                        messagebox.showerror("Atualizar port", payload)
        except queue.Empty:
            pass
        if chunks:
            self.append_log("".join(chunks))
        self.root.after(25 if not self.events.empty() else 60, self.poll_events)

    def finish_update(self, state):
        self.updating = False
        self.set_busy(False)
        changed = state.get("changed") or []
        skipped = state.get("skipped") or []
        if changed:
            self.status.set("Port atualizado.")
            self.reason.set(
                "%d arquivo(s) substituído(s) diretamente na pasta atual: %s"
                % (len(changed), ", ".join(changed))
            )
        else:
            self.status.set("Port já está atualizado.")
            self.reason.set("Nenhum arquivo precisou ser substituído.")
        if skipped:
            self.reason.set(self.reason.get() + " Opcional indisponível: " + ", ".join(skipped))
        self.runtime_status.set(self.runtime_description())
        pending = state.get("pending_launcher")
        if pending:
            self.status.set("Launcher atualizado.")
            self.reason.set("A nova versão foi baixada. O launcher será reiniciado para substituir o EXE em uso.")
            self.schedule_self_update(pending["staged"], pending["target"])

    def schedule_self_update(self, staged, target):
        if os.name != "nt":
            self.reason.set("Atualização do launcher preparada; feche e substitua o executável pela versão em " + staged)
            return
        helper = os.path.join(self.user_dir, "_finish_launcher_update.cmd")
        # cmd retries until Windows releases the mapped executable, then restarts the exact path.
        script = (
            "@echo off\r\n"
            "setlocal\r\n"
            ":retry\r\n"
            ">nul 2>&1 ping 127.0.0.1 -n 2\r\n"
            "copy /y \"" + staged + "\" \"" + target + "\" >nul 2>&1\r\n"
            "if errorlevel 1 goto retry\r\n"
            "del /q \"" + staged + "\" >nul 2>&1\r\n"
            "start \"\" \"" + target + "\"\r\n"
            "del /q \"%~f0\"\r\n"
        )
        try:
            with open(helper, "w", encoding="utf-8", newline="") as f:
                f.write(script)
            subprocess.Popen(
                ["cmd.exe", "/c", helper],
                cwd=self.base,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
            self.root.after(150, self.root.destroy)
        except OSError as exc:
            self.status.set("Launcher baixado, mas não reiniciado.")
            self.reason.set("Feche o launcher e substitua manualmente pelo arquivo preparado. Erro: %s" % exc)

    def read_diagnosis(self):
        try:
            with open(self.diagnosis_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError, TypeError):
            return None

    def diagnosis_summary(self, code, data):
        if isinstance(data, dict):
            stop = data.get("stop") or {}
            kind = stop.get("kind") or ""
            detail = stop.get("detail") or ""
            next_action = data.get("next_action") or ""
            if kind == "unsupported_import":
                text = "O jogo não abriu porque ainda falta implementar a função PS4: %s." % (detail or "desconhecida")
            elif kind == "driver_fault":
                text = "O jogo fechou dentro do driver Vulkan da GPU: %s." % (detail or "driver desconhecido")
                last_upload = data.get("last_gpu_upload")
                last_submit = data.get("last_gpu_submit")
                if last_upload:
                    text += " Último upload rastreado: " + str(last_upload) + "."
                if last_submit:
                    text += " Último estágio Vulkan: " + str(last_submit) + "."
            elif kind == "windows_heap_corruption":
                text = "O Windows encerrou o processo por corrupção de heap (0xC0000374)."
                last_upload = data.get("last_gpu_upload")
                if last_upload:
                    text += " Última operação GPU rastreada: " + str(last_upload) + "."
            elif kind == "windows_access_violation":
                text = "O Windows encerrou o processo por violação de acesso (0xC0000005)."
            elif kind:
                text = "O jogo parou em '%s'%s." % (kind, (" (" + str(detail) + ")") if detail else "")
            elif code == 0:
                text = "O processo terminou normalmente."
            else:
                text = "O processo terminou com código %s." % code
            if next_action:
                text += " Próximo passo: " + str(next_action)
            return text

        if code == 0:
            return "O processo terminou normalmente."
        if code is None:
            return "O processo terminou inesperadamente e não gerou diagnóstico."
        return "O processo encerrou com código %s e não gerou um diagnóstico estruturado." % code

    def finish_launch(self, code, forced_reason=None):
        self.last_exit_code = code
        self.set_busy(False)
        self.stop_button.configure(state="disabled")
        self.process = None

        if forced_reason:
            summary = forced_reason
            self.status.set("Falha ao iniciar.")
        else:
            data = self.read_diagnosis()
            summary = self.diagnosis_summary(code, data)
            if code == 0:
                self.status.set("Jogo encerrado.")
            elif isinstance(data, dict) and (data.get("stop") or {}).get("kind") == "unsupported_import":
                self.status.set("Inicialização incompleta.")
            else:
                self.status.set("O jogo parou ou fechou.")

        self.reason.set(summary)
        self.append_log("\n\n=== DIAGNÓSTICO DO LAUNCHER ===\n" + summary + "\n")
        if os.path.isfile(self.log_path):
            self.append_log("Log salvo em: " + self.log_path + "\n")
        if os.path.isfile(self.diagnosis_path):
            self.append_log("Diagnóstico salvo em: " + self.diagnosis_path + "\n")

    def show_previous_diagnosis(self):
        data = self.read_diagnosis()
        if data:
            self.reason.set("Última execução: " + self.diagnosis_summary(data.get("exit_code"), data))

    def terminate_process_tree(self, process):
        if not process or process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.Popen(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                )
            else:
                process.terminate()
        except OSError:
            try:
                process.terminate()
            except OSError:
                pass

    def stop_game(self):
        process = self.process
        if not process or process.poll() is not None:
            return
        if messagebox.askyesno("Encerrar", "Deseja encerrar a execução atual?"):
            self.terminate_process_tree(process)

    def open_logs(self):
        os.makedirs(self.user_dir, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(self.user_dir)
            else:
                subprocess.Popen(["xdg-open", self.user_dir])
        except OSError as exc:
            messagebox.showerror("Logs", str(exc))

    def report_text(self):
        parts = [
            APP_TITLE,
            "Gerado: " + time.strftime("%Y-%m-%d %H:%M:%S"),
            "Jogo: " + self.game_dir.get().strip(),
            "Backend: " + self.backend.get(),
            "Exit code: " + str(self.last_exit_code),
            "Status: " + self.status.get(),
            "Motivo: " + self.reason.get(),
            "Runtime: " + self.runtime_status.get(),
            "",
        ]
        try:
            with open(self.update_state_path, "r", encoding="utf-8") as f:
                update_state = json.load(f)
            parts.extend(["=== launcher_update.json ===", json.dumps(update_state, ensure_ascii=False, indent=2), ""])
        except (OSError, ValueError, TypeError):
            pass
        data = self.read_diagnosis()
        if data:
            parts.extend(["=== last_run_diagnosis.json ===", json.dumps(data, ensure_ascii=False, indent=2), ""])
        parts.extend(["=== Log exibido ===", self.log.get("1.0", "end-1c")])
        return "\n".join(parts)

    def copy_report(self):
        text = self.report_text()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()
        self.status.set("Relatório copiado. Você pode colá-lo no ChatGPT.")

    def save_report(self):
        path = filedialog.asksaveasfilename(
            title="Salvar relatório",
            defaultextension=".txt",
            filetypes=[("Arquivo de texto", "*.txt"), ("Todos os arquivos", "*.*")],
            initialfile="firstlight-report.txt",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.report_text())
            self.status.set("Relatório salvo.")
        except OSError as exc:
            messagebox.showerror("Salvar relatório", str(exc))

    def on_close(self):
        if self.process and self.process.poll() is None:
            if not messagebox.askyesno("Fechar launcher", "O jogo ainda está em execução. Encerrar e fechar?"):
                return
            self.terminate_process_tree(self.process)
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        root.iconname("First Light")
    except tk.TclError:
        pass
    FirstLightLauncher(root)
    root.mainloop()


if __name__ == "__main__":
    main()
