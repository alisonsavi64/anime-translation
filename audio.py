import sys
import ctypes
import re

try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

import tkinter as tk
import tkinter.messagebox
import threading
import queue
import time
import numpy as np
from difflib import SequenceMatcher
from datetime import datetime
from pathlib import Path

import os

DEEPL_API_KEY     = os.environ.get('DEEPL_API_KEY', '')       # opcional: deepl.com (500k chars/mês grátis)
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')   # opcional: console.anthropic.com
CLAUDE_MODEL      = 'claude-haiku-4-5-20251001'                # rápido o bastante para tempo real

try:
    import pyaudiowpatch as pyaudio
    from faster_whisper import WhisperModel
    from deep_translator import GoogleTranslator
    import pykakasi
except ImportError as e:
    root = tk.Tk()
    root.withdraw()
    tkinter.messagebox.showerror("Dependência faltando",
        f"{e}\n\nRode: pip install -r requirements_audio.txt")
    sys.exit(1)

# DeepL e Claude são opcionais — importados separadamente para não quebrar se ausentes
try:
    from deep_translator import DeepL as _DeepL
    DeepL = _DeepL
except ImportError:
    DeepL = None

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None

CHUNK           = 1024
CONTEXT_S       = 6.0
PROCESS_EVERY_S = 0.5
SENTENCE_END = re.compile(r'(.+?[。.!?！？]+)')
MAX_CHARS = 45
SILENCE_SEC = 1.2

# Frases que o Whisper alucina quando o áudio é ambíguo (música, pausas, ruído)
HALLUCINATIONS = {
    'ご視聴ありがとうございました',
    'ご視聴ありがとう',
    'チャンネル登録',
    'ご覧いただきありがとうございました',
    'どうもありがとうございました',
    'ありがとうございました',
    'ご清聴ありがとうございました',
    'また次回',
    'お疲れ様でした',
}

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
        self.win.geometry('920x420+20+20')
        self.history = []
        self.max_history = 5
        self._pending_result = None

        frame = tk.Frame(self.win, bg='#0d0d1a', padx=14, pady=8)
        frame.pack(fill='both', expand=True)

        _txt_common = dict(
            bg='#0d0d1a', relief='flat', bd=0,
            highlightthickness=0, wrap='word',
            state='disabled', cursor='xterm',
            selectbackground='#2a4080',
            selectforeground='#ffffff',
            exportselection=True,
            padx=0, pady=2,
        )

        # Histórico (últimas 5 frases) — selecionável
        self.history_text = tk.Text(frame, fg='#666688',
                                    font=('Segoe UI', 8), height=8,
                                    **_txt_common)
        self.history_text.pack(fill='x')

        # Frase atual — selecionável
        self.current_text = tk.Text(frame, fg='#ffffff',
                                    font=('Segoe UI', 12, 'bold'), height=4,
                                    **_txt_common)
        self.current_text.pack(fill='x')

        self.dbg_var = tk.StringVar(value='')
        self._dbg_lbl = tk.Label(frame, textvariable=self.dbg_var,
                                  fg='#444466', bg='#0d0d1a',
                                  font=('Segoe UI', 8), anchor='w')
        self._dbg_lbl.pack(fill='x')

        # Drag: win + dbg label (Text widgets ficam com seleção)
        for w in (self.win, self._dbg_lbl):
            w.bind('<ButtonPress-1>', self._drag_start)
            w.bind('<B1-Motion>',     self._drag_move)

        # Ctrl+A seleciona tudo no widget focado
        for w in (self.history_text, self.current_text):
            w.bind('<Control-a>', self._select_all)

        self._dx = self._dy = 0

    def _select_all(self, event):
        event.widget.tag_add('sel', '1.0', 'end')
        return 'break'

    def _drag_start(self, event):
        self._dx, self._dy = event.x, event.y

    def _drag_move(self, event):
        x = self.win.winfo_x() + event.x - self._dx
        y = self.win.winfo_y() + event.y - self._dy
        self.win.geometry(f'+{x}+{y}')

    def _archive_pending(self):
        if self._pending_result:
            old_romaji, old_pt = self._pending_result
            self.history.append(f"{old_romaji}\n{old_pt}")
            if len(self.history) > self.max_history:
                self.history.pop(0)
            self._pending_result = None

    def set_result(self, jp: str, romaji: str, pt: str):
        self._archive_pending()
        self._pending_result = (romaji, pt)
        self._render(current_romaji=romaji, current_pt=pt)

    def set_partial(self, jp: str, romaji: str):
        # Quando nova fala começa, arquiva a frase anterior no histórico
        self._archive_pending()
        self._render(current_romaji=romaji, current_pt="…")

    def set_status(self, msg: str):
        self.dbg_var.set(msg)

    def set_error(self, msg: str):
        self.dbg_var.set(f'⚠ {msg}')

    def _set_text(self, widget: tk.Text, content: str):
        widget.config(state='normal')
        widget.delete('1.0', 'end')
        widget.insert('end', content)
        widget.config(state='disabled')

    def _render(self, current_romaji: str, current_pt: str):
        self._set_text(self.history_text, "\n\n".join(self.history))
        self._set_text(self.current_text,
                       f"{current_romaji}\n{current_pt}")

# ---------------------------------------------------------------------------
# Audio Worker
# ---------------------------------------------------------------------------

class AudioWorker(threading.Thread):

    def __init__(self, model_size: str, result_queue: queue.Queue):
        super().__init__(daemon=True)
        self.model_size    = model_size
        self.result_queue  = result_queue
        self._stop         = threading.Event()
        self._commit_ready = threading.Event()
        self._translating  = False
        self._cache: dict[str, tuple[str, str]] = {}

        self._claude = None
        if ANTHROPIC_API_KEY and Anthropic is not None:
            self._claude = Anthropic(api_key=ANTHROPIC_API_KEY)

        if DEEPL_API_KEY and DeepL is not None:
            self._fallback_translator = DeepL(api_key=DEEPL_API_KEY, source='ja', target='pt-BR')
            self._fallback_name       = 'DeepL'
        else:
            self._fallback_translator = GoogleTranslator(source='ja', target='pt')
            self._fallback_name       = 'Google'

        self._translator_name = f'Claude ({self._fallback_name} fallback)' if self._claude \
            else self._fallback_name
        self._kks              = pykakasi.kakasi()
        self._committed: list[str]       = []
        self._translate_queue: list[str] = []
        self._audio_chunks: list[np.ndarray] = []
        self._audio_lock   = threading.Lock()
        self._total_samples = 0

    def stop(self):
        self._stop.set()
        self._commit_ready.set()

    @staticmethod
    def _resample(audio: np.ndarray, from_rate: int) -> np.ndarray:
        if from_rate == 16000:
            return audio
        new_len = int(len(audio) * 16000 / from_rate)
        return np.interp(
            np.linspace(0, len(audio), new_len),
            np.arange(len(audio)),
            audio
        ).astype(np.float32)

    def _to_romaji(self, japanese: str) -> str:
        tokens = self._kks.convert(japanese)
        return ' '.join(t['hepburn'] for t in tokens if t['hepburn']).strip()

    _CLAUDE_SYSTEM_PROMPT = (
        "Você traduz falas de diálogo de anime do japonês para português brasileiro "
        "coloquial, para uma legenda ao vivo. Regras: "
        "1) Responda APENAS com a tradução, sem aspas, explicações ou notas. "
        "2) Traduza de forma natural, do jeito que um brasileiro realmente falaria, "
        "não literal. "
        "3) Preserve honoríficos (san, kun, chan, sama, senpai, sensei) junto ao nome, "
        "sem traduzir (ex: 'Sasuke-kun'). "
        "4) Use o contexto das falas anteriores (se houver) só para manter a "
        "coerência de tom e pronomes — não as traduza de novo."
    )

    def _claude_translate(self, jp_text: str, context_lines: list) -> str:
        user_content = jp_text
        if context_lines:
            ctx = '\n'.join(context_lines[-3:])
            user_content = f"Contexto (falas anteriores, não traduzir):\n{ctx}\n\nTraduzir:\n{jp_text}"
        resp = self._claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=200,
            system=self._CLAUDE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        return ''.join(b.text for b in resp.content if hasattr(b, 'text')).strip()

    def _fallback_translate(self, jp_text: str, context_jp: str) -> str:
        if context_jp:
            combined   = f"{context_jp}\n{jp_text}"
            translated = self._fallback_translator.translate(combined) or ''
            return translated.split('\n')[-1].strip() or translated
        return self._fallback_translator.translate(jp_text) or ''

    def _translate_async(self, jp_text: str):
        if jp_text in self._cache:
            romaji, pt = self._cache[jp_text]
            self.result_queue.put(('result', jp_text, romaji, pt))
            self._process_translate_queue()
            return

        romaji = self._to_romaji(jp_text)
        self.result_queue.put(('partial', jp_text, romaji))
        self._translating  = True
        context_lines       = list(self._committed[-4:-1]) if len(self._committed) > 1 else []
        context_jp          = self._committed[-1] if self._committed else ''

        def _do():
            pt = None
            if self._claude is not None:
                try:
                    pt = self._claude_translate(jp_text, context_lines)
                except Exception as e:
                    self.result_queue.put(('status', f'Claude indisponível ({e}), usando {self._fallback_name}'))
            if not pt:
                try:
                    pt = self._fallback_translate(jp_text, context_jp)
                except Exception as e:
                    self.result_queue.put(('error', f'Tradução: {e}'))
                    self._translating = False
                    self._process_translate_queue()
                    return
            self._cache[jp_text] = (romaji, pt)
            self.result_queue.put(('result', jp_text, romaji, pt))
            self._translating = False
            self._process_translate_queue()

        threading.Thread(target=_do, daemon=True).start()

    def _process_translate_queue(self):
        if self._translate_queue and not self._translating:
            self._translate_async(self._translate_queue.pop(0))

    def _extract_sentences(self, text: str):
        sentences = SENTENCE_END.findall(text)
        remainder = SENTENCE_END.sub('', text)
        return sentences, remainder

    def _is_hallucination(self, text: str) -> bool:
        stripped = re.sub(r'[。.!?！？\s]+$', '', text.strip())
        for phrase in HALLUCINATIONS:
            if SequenceMatcher(None, stripped, phrase).ratio() > 0.85:
                return True
        return False

    def _commit(self, jp: str):
        jp = jp.strip()
        if not jp or len(jp) < 4:
            return
        if self._is_hallucination(jp):
            self.result_queue.put(('skip', f'[ALUCINAÇÃO] {jp}'))
            return
        for prev in self._committed:
            if SequenceMatcher(None, jp, prev).ratio() > 0.7:
                self.result_queue.put(('skip', f'[DEDUP] {jp}'))
                return
            if len(jp) < len(prev) and (prev.endswith(jp) or jp in prev):
                self.result_queue.put(('skip', f'[DEDUP-SUFFIX] {jp}'))
                return
        self._committed.append(jp)
        if len(self._committed) > 8:
            self._committed.pop(0)
        self._translate_queue.append(jp)
        self._process_translate_queue()

    def _get_snapshot(self) -> np.ndarray:
        with self._audio_lock:
            if not self._audio_chunks:
                return np.array([], dtype=np.float32)
            return np.concatenate(self._audio_chunks)

    def _trim_buffer(self, trim_samples: int):
        with self._audio_lock:
            if not self._audio_chunks:
                return
            all_audio = np.concatenate(self._audio_chunks)
            kept = all_audio[min(trim_samples, len(all_audio)):]
            self._audio_chunks  = [kept] if len(kept) else []
            self._total_samples = len(kept)

    def _find_loopback(self, p):
        wasapi_info  = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_name = p.get_device_info_by_index(
            wasapi_info["defaultOutputDevice"])["name"]
        best = None
        for i in range(p.get_device_count()):
            dev = p.get_device_info_by_index(i)
            if (dev.get("hostApi") == wasapi_info["index"] and
                    dev.get("isLoopbackDevice", False) and
                    dev["maxInputChannels"] > 0):
                best = dev
                if default_name in dev["name"]:
                    return dev
        return best

    def run(self):
        import torch

        # --- 1. Load Whisper ---
        self.result_queue.put(('status', f'Carregando Whisper {self.model_size}…'))
        try:
            device    = 'cuda' if torch.cuda.is_available() else 'cpu'
            compute   = 'float16' if device == 'cuda' else 'int8'
            gpu_label = torch.cuda.get_device_name(0) if device == 'cuda' else 'CPU'
            model     = WhisperModel(self.model_size, device=device, compute_type=compute)
            self.result_queue.put(('status',
                f'Whisper {self.model_size} — {gpu_label} | {self._translator_name} ✓'))
        except Exception as e:
            self.result_queue.put(('error', f'Erro Whisper: {e}'))
            return

        # --- 2. Load Silero-VAD ---
        vad_lock = threading.Lock()
        try:
            from silero_vad import load_silero_vad, VADIterator
            vad_model    = load_silero_vad()
            vad_iterator = VADIterator(vad_model, threshold=0.5, sampling_rate=16000,
                                       min_silence_duration_ms=550, speech_pad_ms=250)
            use_silero = True
            self.result_queue.put(('status', 'Silero-VAD carregado ✓'))
        except Exception as e:
            use_silero   = False
            vad_iterator = None
            self.result_queue.put(('status', f'VAD por energia (silero-vad ausente: {e})'))

        # --- 3. Open WASAPI loopback ---
        try:
            p        = pyaudio.PyAudio()
            loopback = self._find_loopback(p)
            if not loopback:
                self.result_queue.put(('error',
                    'Nenhum dispositivo loopback WASAPI encontrado.'))
                p.terminate()
                return
            sys_rate = int(loopback["defaultSampleRate"])
            channels = loopback["maxInputChannels"]
            stream   = p.open(
                format=pyaudio.paFloat32,
                channels=channels,
                rate=sys_rate,
                frames_per_buffer=CHUNK,
                input=True,
                input_device_index=loopback["index"],
            )
            self.result_queue.put(('status',
                f'Capturando: {loopback["name"]} ({sys_rate} Hz) ✓'))
        except Exception as e:
            self.result_queue.put(('error', f'Erro áudio: {e}'))
            return

        # --- 4. Capture thread ---
        VAD_CHUNK     = 512           # silero-vad needs exactly 512 samples @ 16kHz
        MAX_BUF_SAMP  = int(25 * 16000)   # 25s safety cap

        # energy-based VAD fallback: trigger after ~600ms of silence
        ENERGY_THRESH  = 0.008
        ENERGY_FRAMES  = max(1, int(0.6 * sys_rate / CHUNK))

        vad_buf            = np.array([], dtype=np.float32)
        energy_silence_cnt = 0

        def _capture():
            nonlocal vad_buf, energy_silence_cnt

            while not self._stop.is_set():
                try:
                    data = stream.read(CHUNK, exception_on_overflow=False)
                except Exception:
                    continue

                raw = np.frombuffer(data, dtype=np.float32)
                if channels > 1:
                    raw = raw.reshape(-1, channels).mean(axis=1)
                chunk_16k = self._resample(raw, sys_rate)

                with self._audio_lock:
                    self._audio_chunks.append(chunk_16k)
                    self._total_samples += len(chunk_16k)
                    if self._total_samples > MAX_BUF_SAMP:
                        excess = self._total_samples - MAX_BUF_SAMP
                        while self._audio_chunks and excess > 0:
                            c = self._audio_chunks[0]
                            if len(c) <= excess:
                                excess              -= len(c)
                                self._total_samples -= len(c)
                                self._audio_chunks.pop(0)
                            else:
                                self._audio_chunks[0] = c[excess:]
                                self._total_samples  -= excess
                                break

                if use_silero:
                    vad_buf = np.concatenate([vad_buf, chunk_16k])
                    while len(vad_buf) >= VAD_CHUNK:
                        vad_chunk = torch.from_numpy(vad_buf[:VAD_CHUNK])
                        vad_buf   = vad_buf[VAD_CHUNK:]
                        with vad_lock:
                            result = vad_iterator(vad_chunk)
                        if result and 'end' in result:
                            self._commit_ready.set()
                else:
                    rms = float(np.sqrt(np.mean(chunk_16k ** 2)))
                    if rms < ENERGY_THRESH:
                        energy_silence_cnt += 1
                        if energy_silence_cnt >= ENERGY_FRAMES:
                            self._commit_ready.set()
                            energy_silence_cnt = 0
                    else:
                        energy_silence_cnt = 0

        threading.Thread(target=_capture, daemon=True).start()

        # --- 5. Transcribe loop ---
        PREVIEW_SAMPLES       = int(8 * 16000)   # last 8s for partial previews
        FORCE_COMMIT_SAMPLES  = int(7 * 16000)   # force commit if buffer > 7s without VAD
        PARTIAL_TIMEOUT       = 0.4
        last_partial_txt      = ''

        whisper_params = dict(
            language='ja',
            beam_size=5,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters={'min_silence_duration_ms': 500},
            no_speech_threshold=0.6,
            condition_on_previous_text=False,
            repetition_penalty=1.3,
            no_repeat_ngram_size=3,
            suppress_blank=True,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
        )

        try:
            while not self._stop.is_set():
                triggered = self._commit_ready.wait(timeout=PARTIAL_TIMEOUT)
                self._commit_ready.clear()

                with self._audio_lock:
                    total = self._total_samples
                if total < int(0.3 * 16000):
                    continue

                initial_prompt = (''.join(self._committed[-3:])) or None

                if triggered:
                    # VAD detected end-of-speech → commit full buffer
                    audio = self._get_snapshot()
                    if len(audio) == 0:
                        continue
                    try:
                        segs, _ = model.transcribe(audio,
                                                   initial_prompt=initial_prompt,
                                                   **whisper_params)
                        segs = list(segs)
                    except Exception as e:
                        self.result_queue.put(('error', f'Transcrição: {e}'))
                        continue

                    if segs:
                        full_text = ''.join(s.text.strip() for s in segs)
                        sentences, remainder = self._extract_sentences(full_text)
                        for s in sentences:
                            self._commit(s.strip())
                        if remainder.strip():
                            self._commit(remainder.strip())
                        last_end = max(s.end for s in segs)
                        self._trim_buffer(int(last_end * 16000))

                    if use_silero:
                        with vad_lock:
                            vad_iterator.reset_states()

                else:
                    with self._audio_lock:
                        total = self._total_samples

                    if total >= FORCE_COMMIT_SAMPLES:
                        # Buffer growing without VAD trigger (continuous speech) —
                        # force commit any complete sentences found in the full buffer
                        audio = self._get_snapshot()
                        if len(audio) == 0:
                            continue
                        try:
                            segs, _ = model.transcribe(audio,
                                                       initial_prompt=initial_prompt,
                                                       **whisper_params)
                            segs = list(segs)
                        except Exception:
                            continue

                        if segs:
                            full_text = ''.join(s.text.strip() for s in segs)
                            sentences, remainder = self._extract_sentences(full_text)
                            for s in sentences:
                                self._commit(s.strip())
                            if sentences:
                                last_end = max(s.end for s in segs)
                                self._trim_buffer(int(last_end * 16000))
                            elif full_text.strip():
                                # Whisper não pontuou nada (comum em japonês) — sem isso o
                                # buffer só cresceria até o teto de 25s e perderia o início
                                # da fala. Committa o texto inteiro como uma frase só.
                                self._commit(full_text.strip())
                                last_end = max(s.end for s in segs)
                                self._trim_buffer(int(last_end * 16000))
                            display = remainder.strip()
                            if display and display != last_partial_txt:
                                last_partial_txt = display
                                romaji = self._to_romaji(display)
                                self.result_queue.put(('partial', display, romaji))

                    else:
                        # Partial preview — only last 8s to keep latency low
                        with self._audio_lock:
                            if not self._audio_chunks:
                                continue
                            preview = np.concatenate(self._audio_chunks)[-PREVIEW_SAMPLES:]
                        if len(preview) < int(0.3 * 16000):
                            continue
                        try:
                            segs, _ = model.transcribe(preview,
                                                       initial_prompt=initial_prompt,
                                                       **whisper_params)
                            segs = list(segs)
                        except Exception:
                            continue

                        if segs:
                            full_text = ''.join(s.text.strip() for s in segs)
                            if full_text and full_text != last_partial_txt:
                                last_partial_txt = full_text
                                _, remainder = self._extract_sentences(full_text)
                                display = remainder.strip() or full_text
                                if display:
                                    romaji = self._to_romaji(display)
                                    self.result_queue.put(('partial', display, romaji))

        finally:
            stream.stop_stream()
            stream.close()
            p.terminate()


# ---------------------------------------------------------------------------
# Control Panel
# ---------------------------------------------------------------------------

class App:
    BG     = '#1a1a2e'
    ACCENT = '#e94560'
    BTN_BG = '#16213e'
    MODELS = ['large-v3', 'medium', 'small', 'base', 'tiny']

    def __init__(self):
        self.root = tk.Tk()
        self.root.title('Anime Translator — Áudio')
        self.root.geometry('400x215')
        self.root.resizable(False, False)
        self.root.configure(bg=self.BG)

        self.worker  = None
        self.overlay = None
        self.q       = queue.Queue()
        self._model  = tk.StringVar(value='large-v3')
        self._log    = None  # arquivo de log da sessão atual

        self._build_ui()
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)
        self.root.after(100, self._poll)
        self.root.mainloop()

    def _on_close(self):
        self._stop()
        self.root.destroy()

    def _btn(self, parent, text, cmd, **kw):
        return tk.Button(parent, text=text, command=cmd,
                         bg=self.BTN_BG, fg='white',
                         font=('Segoe UI', 10), relief='flat',
                         padx=10, pady=6, cursor='hand2',
                         activebackground='#0f3460',
                         activeforeground='white', **kw)

    def _build_ui(self):
        tk.Label(self.root, text='Anime Translator — Áudio',
                 font=('Segoe UI', 13, 'bold'),
                 bg=self.BG, fg=self.ACCENT).pack(pady=(16, 2))

        tk.Label(self.root,
                 text='Fala em PT/EN (Discord) é ignorada automaticamente',
                 font=('Segoe UI', 8), bg=self.BG, fg='#555577').pack()

        # Model selector
        row = tk.Frame(self.root, bg=self.BG)
        row.pack(pady=(12, 0))
        tk.Label(row, text='Modelo Whisper:', bg=self.BG, fg='white',
                 font=('Segoe UI', 10)).pack(side='left', padx=(0, 8))
        menu = tk.OptionMenu(row, self._model, *self.MODELS)
        menu.config(bg=self.BTN_BG, fg='white', font=('Segoe UI', 10),
                    relief='flat', highlightthickness=0, activebackground='#0f3460')
        menu['menu'].config(bg=self.BTN_BG, fg='white')
        menu.pack(side='left')

        self.status = tk.StringVar(value='Escolha o modelo e clique Iniciar')
        tk.Label(self.root, textvariable=self.status,
                 bg=self.BG, fg='white', font=('Segoe UI', 10),
                 wraplength=380).pack(pady=(10, 0))

        self.debug = tk.StringVar(value='')
        tk.Label(self.root, textvariable=self.debug,
                 bg=self.BG, fg='#555577', font=('Segoe UI', 8),
                 wraplength=380).pack(pady=(2, 0))

        btn_row = tk.Frame(self.root, bg=self.BG)
        btn_row.pack(pady=10)

        self.b_start = self._btn(btn_row, 'Iniciar', self._start)
        self.b_start.grid(row=0, column=0, padx=6)

        self.b_stop = self._btn(btn_row, 'Parar', self._stop, state='disabled')
        self.b_stop.grid(row=0, column=1, padx=6)

    def _open_log(self):
        logs_dir = Path(__file__).parent / 'logs'
        logs_dir.mkdir(exist_ok=True)
        filename = logs_dir / f"session_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.txt"
        self._log = open(filename, 'w', encoding='utf-8')
        self._log.write(f"# Sessão iniciada: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n")
        self._log.write(f"# Modelo: {self._model.get()}\n\n")
        self._log.flush()

    def _log_result(self, jp: str, romaji: str, pt: str):
        if self._log:
            ts = datetime.now().strftime('%H:%M:%S')
            self._log.write(f"[{ts}]\nJP: {jp}\nRomaji: {romaji}\nPT: {pt}\n\n")
            self._log.flush()

    def _close_log(self):
        if self._log:
            self._log.write(f"# Sessão encerrada: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n")
            self._log.close()
            self._log = None

    def _start(self):
        if self.overlay is None:
            self.overlay = Overlay(self.root)
        self._open_log()
        self.worker = AudioWorker(self._model.get(), self.q)
        self.worker.start()
        self.status.set('Carregando…')
        self.b_start.config(state='disabled')
        self.b_stop.config(state='normal')

    def _stop(self):
        if self.worker:
            self.worker.stop()
            self.worker = None
        self._close_log()
        self.status.set('Parado.')
        self.b_start.config(state='normal')
        self.b_stop.config(state='disabled')

    def _poll(self):
        try:
            while True:
                msg  = self.q.get_nowait()
                kind = msg[0]
                if kind == 'result':
                    _, jp, romaji, pt = msg
                    if self.overlay:
                        self.overlay.set_result(jp, romaji, pt)
                    self._log_result(jp, romaji, pt)
                    self.status.set('✓ traduzido')
                    self.debug.set('')
                elif kind == 'partial':
                    _, jp, romaji = msg
                    if self.overlay:
                        self.overlay.set_partial(jp, romaji)
                    self.status.set('traduzindo PT…')
                elif kind == 'status':
                    txt = msg[1]
                    if self.overlay:
                        self.overlay.set_status(txt)
                    self.status.set(txt)
                elif kind == 'skip':
                    if self._log:
                        self._log.write(f"{msg[1]}\n")
                        self._log.flush()
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
