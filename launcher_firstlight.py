import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
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
HTTP_USER_AGENT = "FirstLightLauncher/2"


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
        self.updating = False
        self.last_exit_code = None

        os.makedirs(self.user_dir, exist_ok=True)
        os.makedirs(self.runtime_dir, exist_ok=True)

        self.game_dir = tk.StringVar()
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

    def runtime_description(self):
        aliases = os.path.isfile(os.path.join(self.runtime_dir, "import_aliases.tsv"))
        native = os.path.isfile(os.path.join(self.runtime_dir, "firstlight_runtime.dll"))
        return (
            "Hotfixes locais: aliases externos %s; módulo nativo %s. "
            "O botão Atualizar port substitui somente esses arquivos, sem apagar jogo, saves, cache ou configuração."
            % ("ativo" if aliases else "ausente", "ativo" if native else "ausente")
        )

    def load_config(self):
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            value = data.get("game_dir")
            if isinstance(value, str):
                self.game_dir.set(value)
        except (OSError, ValueError, TypeError):
            pass

    def save_config(self):
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump({"game_dir": self.game_dir.get().strip()}, f, indent=2)
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
        self.log.configure(state="normal")
        self.log.insert("end", text)
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
            self.process = subprocess.Popen(
                [bootstrap, game, "--console"],
                cwd=self.base,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=flags,
            )
        except OSError as exc:
            self.finish_launch(None, "Não foi possível iniciar FirstLightBootstrap.exe: %s" % exc)
            return

        self.reader = threading.Thread(target=self.read_output, daemon=True)
        self.reader.start()

    def read_output(self):
        try:
            if self.process.stdout:
                for line in self.process.stdout:
                    self.events.put(("line", line))
            code = self.process.wait()
            self.events.put(("done", code))
        except Exception as exc:
            self.events.put(("reader_error", str(exc)))

    def http_bytes(self, url):
        request = urllib.request.Request(url, headers={"User-Agent": HTTP_USER_AGENT, "Cache-Control": "no-cache"})
        with urllib.request.urlopen(request, timeout=20) as response:
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
            manifest_data = self.http_bytes(UPDATE_MANIFEST_URL)
            manifest = json.loads(manifest_data.decode("utf-8"))
            if manifest.get("schema") != 1 or not isinstance(manifest.get("files"), list):
                raise RuntimeError("manifesto de atualização incompatível")

            changed = []
            unchanged = []
            skipped = []
            for item in manifest["files"]:
                rel = item.get("path")
                url = item.get("url")
                required = bool(item.get("required", False))
                if not isinstance(rel, str) or not rel or not isinstance(url, str) or not url:
                    continue
                self.events.put(("update_status", "Baixando " + rel + "..."))
                try:
                    data = self.http_bytes(url)
                except urllib.error.HTTPError as exc:
                    if not required and exc.code == 404:
                        skipped.append(rel)
                        continue
                    raise
                expected = item.get("sha256")
                if expected and hashlib.sha256(data).hexdigest().lower() != str(expected).lower():
                    raise RuntimeError("SHA-256 inválido para " + rel)

                destination = os.path.abspath(os.path.join(self.base, rel.replace("/", os.sep)))
                base_prefix = os.path.abspath(self.base) + os.sep
                if not destination.startswith(base_prefix):
                    raise RuntimeError("caminho inválido no manifesto: " + rel)

                old = None
                try:
                    with open(destination, "rb") as f:
                        old = f.read()
                except OSError:
                    pass
                if old == data:
                    unchanged.append(rel)
                    continue
                self.atomic_replace(destination, data)
                changed.append(rel)

            state = {
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "channel": manifest.get("channel"),
                "changed": changed,
                "unchanged": unchanged,
                "skipped": skipped,
            }
            with open(self.update_state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            self.events.put(("update_done", state))
        except Exception as exc:
            self.events.put(("update_error", str(exc)))

    def poll_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "line":
                    self.append_log(payload)
                elif kind == "done":
                    self.finish_launch(payload)
                elif kind == "reader_error":
                    self.finish_launch(None, "Falha ao capturar o log: " + payload)
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
        self.root.after(50, self.poll_events)

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

    def stop_game(self):
        if not self.process or self.process.poll() is not None:
            return
        if messagebox.askyesno("Encerrar", "Deseja encerrar a execução atual?"):
            try:
                self.process.terminate()
            except OSError:
                pass

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
            try:
                self.process.terminate()
            except OSError:
                pass
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
