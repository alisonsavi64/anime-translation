import sys
import ctypes

try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

import tkinter as tk
import threading
import queue
import time
import re
import numpy as np
from difflib import SequenceMatcher

try:
    import mss
    from PIL import Image
    import easyocr
    from deep_translator import GoogleTranslator
    import pykakasi
except ImportError as e:
    root = tk.Tk()
    root.withdraw()
    tk.messagebox.showerror("Dependência faltando",
        f"{e}\n\nRode: pip install -r requirements.txt")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Region Selector
# ---------------------------------------------------------------------------

class RegionSelector:
    def __init__(self, master, callback):
        self.callback = callback
        self.start_x = self.start_y = 0
        self.rect = None

        self.top = tk.Toplevel(master)
        self.top.attributes('-fullscreen', True)
        self.top.attributes('-alpha', 0.35)
        self.top.attributes('-topmost', True)
        self.top.configure(bg='black')

        self.canvas = tk.Canvas(self.top, cursor='cross', bg='black',
                                highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)

        tk.Label(self.canvas,
                 text='Arraste para selecionar a área da legenda japonesa  |  ESC para cancelar',
                 fg='white', bg='black', font=('Segoe UI', 14)).place(
                     relx=0.5, rely=0.06, anchor='center')

        self.canvas.bind('<ButtonPress-1>', self._on_press)
        self.canvas.bind('<B1-Motion>', self._on_drag)
        self.canvas.bind('<ButtonRelease-1>', self._on_release)
        self.top.bind('<Escape>', lambda _: self.top.destroy())

        master.wait_window(self.top)

    def _on_press(self, event):
        self.start_x, self.start_y = event.x, event.y
        if self.rect:
            self.canvas.delete(self.rect)

    def _on_drag(self, event):
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(
            self.start_x, self.start_y, event.x, event.y,
            outline='#00ff88', width=2, fill='')
        w = abs(event.x - self.start_x)
        h = abs(event.y - self.start_y)
        self.canvas.delete('dim')
        self.canvas.create_text(
            event.x + 8, event.y + 8,
            text=f'{w}×{h}', fill='#00ff88',
            font=('Segoe UI', 10), anchor='nw', tags='dim')

    def _on_release(self, event):
        x1 = min(self.start_x, event.x)
        y1 = min(self.start_y, event.y)
        x2 = max(self.start_x, event.x)
        y2 = max(self.start_y, event.y)
        self.top.destroy()
        if x2 - x1 > 20 and y2 - y1 > 10:
            self.callback({'top': y1, 'left': x1,
                           'width': x2 - x1, 'height': y2 - y1})


# ---------------------------------------------------------------------------
# Overlay
# ---------------------------------------------------------------------------

class Overlay:
    def __init__(self, master):
        self.win = tk.Toplevel(master)
        self.win.overrideredirect(True)
        self.win.attributes('-topmost', True)
        self.win.attributes('-alpha', 0.92)
        self.win.configure(bg='#0d0d1a')
        self.win.geometry('920x400+20+20')
        self.history = []
        self.max_history = 5
        self._pending_result = None

        frame = tk.Frame(self.win, bg='#0d0d1a', padx=14, pady=8)
        frame.pack(fill='both', expand=True)

        self.history_var = tk.StringVar(value='')
        self.current_var = tk.StringVar(value='Aguardando legenda…')
        self.dbg_var     = tk.StringVar(value='')

        tk.Label(frame, textvariable=self.history_var,
                 fg='#666688', bg='#0d0d1a',
                 font=('Segoe UI', 8),
                 wraplength=892, justify='left', anchor='w').pack(fill='x')

        tk.Label(frame, textvariable=self.current_var,
                 fg='#ffffff', bg='#0d0d1a',
                 font=('Segoe UI', 12, 'bold'),
                 wraplength=892, justify='left', anchor='w').pack(fill='x')

        tk.Label(frame, textvariable=self.dbg_var,
                 fg='#444466', bg='#0d0d1a',
                 font=('Segoe UI', 8),
                 wraplength=892, justify='left', anchor='w').pack(fill='x')

        for w in (self.win, frame):
            w.bind('<ButtonPress-1>', self._drag_start)
            w.bind('<B1-Motion>', self._drag_move)

        self._dx = self._dy = 0

    def _drag_start(self, event):
        self._dx, self._dy = event.x, event.y

    def _drag_move(self, event):
        x = self.win.winfo_x() + event.x - self._dx
        y = self.win.winfo_y() + event.y - self._dy
        self.win.geometry(f'+{x}+{y}')

    def _archive_pending(self):
        if self._pending_result:
            jp, romaji, pt = self._pending_result
            self.history.append(f"JP: {jp}\nRomaji: {romaji}\nPT: {pt}")
            if len(self.history) > self.max_history:
                self.history.pop(0)
            self._pending_result = None

    def set_result(self, jp: str, romaji: str, pt: str):
        self._archive_pending()
        self._pending_result = (jp, romaji, pt)
        self._render(jp, romaji, pt)

    def set_partial(self, jp: str, romaji: str):
        self._archive_pending()
        self._render(jp, romaji, '…')

    def set_status(self, msg: str):
        self.dbg_var.set(msg)

    def set_error(self, msg: str):
        self.dbg_var.set(f'⚠ {msg}')

    def _render(self, jp: str, romaji: str, pt: str):
        self.history_var.set('\n\n'.join(self.history))
        self.current_var.set(f'JP: {jp}\nRomaji: {romaji}\nPT: {pt}')


# ---------------------------------------------------------------------------
# Capture + OCR + Translate worker
# ---------------------------------------------------------------------------

class CaptureWorker(threading.Thread):

    # Mantém kanji/kana/fullwidth + ASCII + pontuação japonesa; remove lixo de OCR
    _JUNK = re.compile(r'[^　-鿿＀-￯゠-ヿ぀-ゟ'
                       r'A-Za-z0-9\s。、！？!?,.\-]')

    def __init__(self, region: dict, result_queue: queue.Queue):
        super().__init__(daemon=True)
        self.region       = region
        self.result_queue = result_queue
        self._stop        = threading.Event()
        self._last_text   = ''
        self._pending_text  = ''
        self._stable_count  = 0
        self._translating   = False
        self._cache: dict[str, tuple[str, str]] = {}  # jp → (romaji, pt)
        self._translator    = GoogleTranslator(source='ja', target='pt')
        self._kks           = pykakasi.kakasi()

    def stop(self):
        self._stop.set()

    def _to_romaji(self, japanese: str) -> str:
        tokens = self._kks.convert(japanese)
        return ' '.join(t['hepburn'] for t in tokens if t['hepburn']).strip()

    def _clean(self, parts: list) -> str:
        # Ordena por linha (y) depois por coluna (x) para ler na ordem certa
        parts = sorted(parts, key=lambda p: (p[0][0][1], p[0][0][0]))
        words = [p[1].strip() for p in parts if p[2] > 0.5 and p[1].strip()]
        text = ''.join(words)           # japonês não usa espaço entre tokens
        text = self._JUNK.sub('', text)
        return text.strip()

    @staticmethod
    def _similar(a: str, b: str) -> bool:
        return SequenceMatcher(None, a, b).ratio() > 0.82

    def _translate_async(self, jp_text: str):
        if jp_text in self._cache:
            romaji, pt = self._cache[jp_text]
            self.result_queue.put(('result', jp_text, romaji, pt))
            return

        romaji = self._to_romaji(jp_text)
        self.result_queue.put(('partial', jp_text, romaji))  # preview instantâneo
        self._translating = True

        def _do():
            try:
                pt = self._translator.translate(jp_text) or ''
                self._cache[jp_text] = (romaji, pt)
                self.result_queue.put(('result', jp_text, romaji, pt))
            except Exception as e:
                self.result_queue.put(('error', f'Tradução: {e}'))
            finally:
                self._translating = False

        threading.Thread(target=_do, daemon=True).start()

    def run(self):
        self.result_queue.put(('status', 'Carregando EasyOCR japonês… (primeira vez ~30s)'))
        try:
            import torch
            gpu = torch.cuda.is_available()
            reader = easyocr.Reader(['ja'], gpu=gpu, verbose=False)
            label = torch.cuda.get_device_name(0) if gpu else 'CPU'
            self.result_queue.put(('status', f'EasyOCR pronto — {label} ✓'))
        except Exception as e:
            reader = easyocr.Reader(['ja'], gpu=False, verbose=False)
            self.result_queue.put(('status', f'EasyOCR pronto — CPU ({e})'))

        with mss.mss() as sct:
            while not self._stop.is_set():
                try:
                    shot = sct.grab(self.region)
                    img  = Image.frombytes('RGB', shot.size, shot.bgra, 'raw', 'BGRX')
                    arr  = np.array(img)

                    results = reader.readtext(arr, paragraph=False)
                    text    = self._clean(results)

                    if not text or len(text) < 2:
                        self.result_queue.put(('status', 'OCR: sem legenda na região'))
                        self._pending_text  = ''
                        self._stable_count  = 0
                    elif self._similar(text, self._last_text):
                        pass  # mesma legenda, já traduzida
                    elif self._similar(text, self._pending_text):
                        self._stable_count += 1
                        if self._stable_count >= 2 and not self._translating:
                            self._last_text    = text
                            self._pending_text = ''
                            self._stable_count = 0
                            self._translate_async(text)
                    else:
                        # Novo texto: dispara tradução especulativa no 1º frame
                        self._pending_text = text
                        self._stable_count = 1
                        self.result_queue.put(('status', f'Lendo: "{text[:50]}"'))
                        if not self._translating:
                            self._translate_async(text)

                    time.sleep(0.25)

                except Exception as e:
                    self.result_queue.put(('error', f'Captura: {e}'))
                    time.sleep(1)


# ---------------------------------------------------------------------------
# Control Panel
# ---------------------------------------------------------------------------

class App:
    BG     = '#1a1a2e'
    ACCENT = '#e94560'
    BTN_BG = '#16213e'

    def __init__(self):
        self.root = tk.Tk()
        self.root.title('Anime Translator — Legenda JP')
        self.root.geometry('360x185')
        self.root.resizable(False, False)
        self.root.configure(bg=self.BG)

        self.region  = None
        self.worker  = None
        self.overlay = None
        self.q       = queue.Queue()

        self._build_ui()
        self.root.after(100, self._poll)
        self.root.mainloop()

    def _btn(self, parent, text, cmd, **kw):
        return tk.Button(parent, text=text, command=cmd,
                         bg=self.BTN_BG, fg='white',
                         font=('Segoe UI', 10), relief='flat',
                         padx=10, pady=6, cursor='hand2',
                         activebackground='#0f3460',
                         activeforeground='white', **kw)

    def _build_ui(self):
        tk.Label(self.root, text='Anime Translator — Legenda JP',
                 font=('Segoe UI', 13, 'bold'),
                 bg=self.BG, fg=self.ACCENT).pack(pady=(16, 4))

        self.status = tk.StringVar(value='1. Selecione a região da legenda japonesa')
        tk.Label(self.root, textvariable=self.status,
                 bg=self.BG, fg='white', font=('Segoe UI', 10),
                 wraplength=340).pack()

        self.debug = tk.StringVar(value='')
        tk.Label(self.root, textvariable=self.debug,
                 bg=self.BG, fg='#666699', font=('Segoe UI', 8),
                 wraplength=340).pack(pady=(2, 0))

        btn_row = tk.Frame(self.root, bg=self.BG)
        btn_row.pack(pady=12)

        self.b_sel   = self._btn(btn_row, 'Selecionar Região', self._select)
        self.b_sel.grid(row=0, column=0, padx=4)

        self.b_start = self._btn(btn_row, 'Iniciar', self._start, state='disabled')
        self.b_start.grid(row=0, column=1, padx=4)

        self.b_stop  = self._btn(btn_row, 'Parar', self._stop, state='disabled')
        self.b_stop.grid(row=0, column=2, padx=4)

    def _select(self):
        self.root.after(50, self._open_selector)

    def _open_selector(self):
        RegionSelector(self.root, self._on_region)

    def _on_region(self, region):
        self.region = region
        w, h = region['width'], region['height']
        self.status.set(f'Região: {w}×{h} px  ✓')
        self.debug.set(f'top={region["top"]} left={region["left"]}')
        self.b_start.config(state='normal')

    def _start(self):
        if not self.region:
            return
        if self.overlay is None:
            self.overlay = Overlay(self.root)
        self.worker = CaptureWorker(self.region, self.q)
        self.worker.start()
        self.status.set('Monitorando legenda japonesa…')
        self.debug.set('')
        self.b_start.config(state='disabled')
        self.b_stop.config(state='normal')
        self.b_sel.config(state='disabled')

    def _stop(self):
        if self.worker:
            self.worker.stop()
            self.worker = None
        self.status.set('Parado.')
        self.b_start.config(state='normal')
        self.b_stop.config(state='disabled')
        self.b_sel.config(state='normal')

    def _poll(self):
        try:
            while True:
                msg  = self.q.get_nowait()
                kind = msg[0]
                if kind == 'result':
                    _, jp, romaji, pt = msg
                    if self.overlay:
                        self.overlay.set_result(jp, romaji, pt)
                    self.status.set('✓ traduzido')
                    self.debug.set('')
                elif kind == 'partial':
                    _, jp, romaji = msg
                    if self.overlay:
                        self.overlay.set_partial(jp, romaji)
                    self.status.set('traduzindo…')
                elif kind == 'status':
                    txt = msg[1]
                    if self.overlay:
                        self.overlay.set_status(txt)
                    self.debug.set(txt)
                elif kind == 'error':
                    err = msg[1]
                    if self.overlay:
                        self.overlay.set_error(err)
                    self.debug.set(f'⚠ {err}')
        except queue.Empty:
            pass
        self.root.after(100, self._poll)


# ---------------------------------------------------------------------------

if __name__ == '__main__':
    App()
