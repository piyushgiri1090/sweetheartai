import atexit
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


PORT = int(os.environ.get("PORT", "3000"))
MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
OLLAMA_CHAT_URL = "http://127.0.0.1:11434/api/chat"
MAX_BODY_BYTES = 100_000
MAX_MUSIC_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_VOICE_SAMPLE_BYTES = 25 * 1024 * 1024
SONG_GENERATOR_PORT = 8001
SONG_GENERATOR_URL = f"http://127.0.0.1:{SONG_GENERATOR_PORT}"
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
LOCAL_APP_DATA = Path(
    os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
)
MUSIC_DIR = LOCAL_APP_DATA / "Sweetheart" / "Music"
MUSIC_INDEX_PATH = MUSIC_DIR / "library.json"
_music_lock = threading.Lock()
SONG_GENERATOR_DIR = LOCAL_APP_DATA / "Sweetheart" / "SongGenerator"
SONG_GENERATOR_ENGINE_DIR = SONG_GENERATOR_DIR / "engine"
SONG_GENERATOR_PYTHON = SONG_GENERATOR_DIR / "venv" / "Scripts" / "python.exe"
SONG_GENERATOR_LOG_PATH = SONG_GENERATOR_DIR / "api.log"
_song_generator_lock = threading.Lock()
_song_job_lock = threading.Lock()
_song_jobs = {}
_song_generator_process = None
_song_generator_log = None
VOICE_SAMPLE_DIR = LOCAL_APP_DATA / "Sweetheart" / "Voice"
VOICE_SAMPLE_PATH = VOICE_SAMPLE_DIR / "custom-voice-sample.audio"
VOICE_SAMPLE_INFO_PATH = VOICE_SAMPLE_DIR / "custom-voice-sample.json"
_voice_sample_lock = threading.Lock()
INDEX_PATH = RESOURCE_DIR / "public" / "index.html"
ICON_PATH = RESOURCE_DIR / "assets" / "sweetheart.svg"
PERSONAL_ADAPTER_PATH = APP_DIR / "models" / "personal-hindi-lora"
PERSONAL_MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"
_personal_model = None
_personal_tokenizer = None
_personal_model_lock = threading.Lock()
_logger = logging.getLogger(__name__)
SYSTEM_PROMPT = (
    "You are a helpful personal assistant. Always answer in clear, natural Hindi "
    "written in Devanagari, even if the user asks in another language. Be concise "
    "but useful, with natural Indian Hindi phrasing. Your name is Sweetheart. If "
    "asked your name, say Sweetheart. If you do not know something, say so "
    "honestly. You were developed by Piyush and Nitish; if asked who developed "
    "or created you, credit both Piyush and Nitish. Speak in a warm, friendly, "
    "playful feminine conversational style, like a helpful person rather than a "
    "formal bot. Use light, clever humor or a small joke when it fits naturally; "
    "never force jokes, and stay kind and serious for sensitive, urgent, or "
    "distressing topics. Prefer concise, speakable sentences with natural pauses "
    "over robotic lists and stiff wording. Help with "
    "smartphones using accurate general knowledge: explain processors and chipsets, "
    "RAM and storage, displays, camera sensors and lenses, battery capacity and "
    "charging, Android/iOS, updates and security, 4G/5G bands, SIM/eSIM, Wi-Fi, "
    "Bluetooth, NFC, durability ratings, thermals, and repair/support. Explain "
    "trade-offs in plain language and compare phones against the user's budget and "
    "needs. Keep these facts straight: megapixels count image pixels, not camera "
    "quality; OIS physically compensates for camera shake, while EIS stabilizes "
    "video digitally; optical zoom uses the lens and digital zoom crops/enlarges "
    "the image; mAh is battery capacity, not a guarantee of screen time; charging "
    "speed depends on the phone, charger, protocol, and temperature; refresh rate "
    "is measured in Hz; RAM helps keep apps available, while storage holds files "
    "and apps. More RAM, charging watts, benchmark scores, or 5G branding alone "
    "do not prove a phone is better. Use clear, correct Devanagari Hindi and define "
    "technical terms simply. Do not invent model specifications, current prices, "
    "offers, or update promises. You have no live product or price lookup: say when "
    "details may be outdated and ask for the exact model/variant or a listing to "
    "compare."
)


def _song_api_request(path, method="GET", payload=None, timeout=30):
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(
        f"{SONG_GENERATOR_URL}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        try:
            error_result = json.loads(error.read().decode("utf-8"))
            message = error_result.get("error")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            message = None
        raise RuntimeError(
            str(message or f"Song generator returned HTTP {error.code}.")
        ) from error
    except (URLError, OSError, TimeoutError) as error:
        raise RuntimeError("Local song generator API is not responding.") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Local song generator returned an invalid response.") from error

    if not isinstance(result, dict):
        raise RuntimeError("Local song generator returned an invalid response.")
    if result.get("error"):
        raise RuntimeError(str(result["error"]))
    if result.get("code", 200) != 200:
        raise RuntimeError("Local song generator could not complete the request.")
    return result.get("data")


def _song_generator_health():
    try:
        data = _song_api_request("/health", timeout=3)
    except RuntimeError as error:
        if "not responding" in str(error):
            return None
        raise RuntimeError(
            f"Port {SONG_GENERATOR_PORT} is occupied by a different service."
        ) from error
    if not isinstance(data, dict) or data.get("service") != "ACE-Step API":
        raise RuntimeError(
            f"Port {SONG_GENERATOR_PORT} is occupied by a different service."
        )
    return data


def _start_song_generator():
    global _song_generator_process, _song_generator_log

    if not SONG_GENERATOR_PYTHON.is_file() or not (
        SONG_GENERATOR_ENGINE_DIR / "acestep" / "api_server.py"
    ).is_file():
        raise RuntimeError(
            "Local song generation is not installed yet. Follow the ACE-Step "
            "installation steps in README.md, then restart Sweetheart."
        )

    health = _song_generator_health()
    if health is not None:
        return health

    with _song_generator_lock:
        health = _song_generator_health()
        if health is not None:
            return health

        if _song_generator_process is not None and _song_generator_process.poll() is not None:
            _song_generator_process = None

        SONG_GENERATOR_DIR.mkdir(parents=True, exist_ok=True)
        _song_generator_log = SONG_GENERATOR_LOG_PATH.open("a", encoding="utf-8")
        environment = os.environ.copy()
        environment.pop("ACESTEP_API_KEY", None)
        environment.update(
            {
                "ACESTEP_API_HOST": "127.0.0.1",
                "ACESTEP_API_PORT": str(SONG_GENERATOR_PORT),
                "ACESTEP_CONFIG_PATH": "acestep-v15-turbo",
                "ACESTEP_DEVICE": "cpu",
                "ACESTEP_INIT_LLM": "false",
                "ACESTEP_NO_INIT": "true",
                "ACESTEP_USE_FLASH_ATTENTION": "false",
                "ACESTEP_COMPILE_MODEL": "false",
                "ACESTEP_DOWNLOAD_SOURCE": "huggingface",
            }
        )
        startupinfo = None
        creationflags = 0
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = subprocess.CREATE_NO_WINDOW
        try:
            _song_generator_process = subprocess.Popen(
                [
                    str(SONG_GENERATOR_PYTHON),
                    "-m",
                    "acestep.api_server",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(SONG_GENERATOR_PORT),
                    "--no-init",
                ],
                cwd=SONG_GENERATOR_ENGINE_DIR,
                env=environment,
                stdout=_song_generator_log,
                stderr=subprocess.STDOUT,
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
        except OSError as error:
            _song_generator_log.close()
            _song_generator_log = None
            _logger.exception("Could not start the local song generator.")
            raise RuntimeError("Local song generator could not be started.") from error

        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if _song_generator_process.poll() is not None:
                break
            try:
                health = _song_generator_health()
            except RuntimeError:
                health = None
            if health is not None:
                return health
            time.sleep(0.5)

        if _song_generator_process.poll() is None:
            raise RuntimeError(
                "Local song generator did not start in time. Check "
                f"{SONG_GENERATOR_LOG_PATH} for details."
            )
        if _song_generator_log is not None:
            _song_generator_log.flush()
        try:
            log_tail = SONG_GENERATOR_LOG_PATH.read_text(
                encoding="utf-8",
                errors="replace",
            )[-3000:]
        except OSError:
            log_tail = ""
        raise RuntimeError(
            "Local song generator stopped during startup. Check "
            f"{SONG_GENERATOR_LOG_PATH} for details. {log_tail[-1000:]}"
        )


def _set_song_job(job_id, **updates):
    with _song_job_lock:
        job = _song_jobs.get(job_id)
        if job is not None:
            job.update(updates)


def _generate_song_lyrics(prompt):
    request_body = json.dumps(
        {
            "model": MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Write short, original, singable Hindi song lyrics in "
                        "Devanagari. Do not copy existing songs or imitate any "
                        "real singer. Return only a JSON object with string "
                        "fields title and lyrics. Include simple verse and "
                        "chorus labels, and keep the lyrics under 24 lines."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.85, "num_predict": 900},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        OLLAMA_CHAT_URL,
        data=request_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=180) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        try:
            error_result = json.loads(error.read().decode("utf-8"))
            message = error_result.get("error")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            message = None
        raise RuntimeError(
            str(message or f"Ollama returned HTTP {error.code} while writing lyrics.")
        ) from error
    except (URLError, OSError, TimeoutError) as error:
        raise RuntimeError(
            "Lyrics could not be created. Start Ollama and make sure "
            f"`ollama pull {MODEL}` has completed."
        ) from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Ollama returned an invalid lyrics response.") from error

    message = result.get("message", {}) if isinstance(result, dict) else {}
    content = message.get("content", "") if isinstance(message, dict) else ""
    try:
        song = json.loads(content)
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("Ollama did not return valid song lyrics.") from error
    if (
        not isinstance(song, dict)
        or not isinstance(song.get("title"), str)
        or not isinstance(song.get("lyrics"), str)
        or not song["lyrics"].strip()
    ):
        raise RuntimeError("Ollama returned empty or invalid song lyrics.")
    return song["title"].strip()[:200], song["lyrics"].strip()


def _save_generated_music_track(title, prompt, lyrics, duration, audio):
    track_id = str(uuid.uuid4())
    track = {
        "id": track_id,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "title": title[:200],
        "artist": "Sweetheart AI",
        "album": "",
        "genre": "AI-generated",
        "language": "Hindi",
        "composer": "Sweetheart AI",
        "lyricist": "Sweetheart AI",
        "label": "Sweetheart",
        "mood": "",
        "notes": f"AI से बनाया गया गाना। Prompt: {prompt[:1800]}",
        "filename": f"{track_id}.mp3",
        "release_year": datetime.now().year,
        "track_number": None,
        "duration_seconds": duration,
        "audio_content_type": "audio/mpeg",
        "generation_prompt": prompt,
        "lyrics": lyrics,
        "has_audio": True,
    }
    audio_path = MUSIC_DIR / f"{track_id}.audio"
    temporary_audio_path = MUSIC_DIR / f"{track_id}.tmp"
    try:
        with _music_lock:
            MUSIC_DIR.mkdir(parents=True, exist_ok=True)
            temporary_audio_path.write_bytes(audio)
            temporary_audio_path.replace(audio_path)
            try:
                library = json.loads(
                    MUSIC_INDEX_PATH.read_text(encoding="utf-8")
                ) if MUSIC_INDEX_PATH.exists() else []
                if not isinstance(library, list) or any(
                    not isinstance(item, dict) for item in library
                ):
                    raise OSError("The music library has an invalid format.")
                library.append(track)
                temporary_index_path = MUSIC_INDEX_PATH.with_suffix(".tmp")
                temporary_index_path.write_text(
                    json.dumps(library, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                temporary_index_path.replace(MUSIC_INDEX_PATH)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                if audio_path.exists():
                    audio_path.unlink()
                raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Generated audio could not be saved to the music library.") from error
    return track


def _run_song_generation(job_id, payload):
    try:
        _set_song_job(job_id, status="running", message="गीत के बोल तैयार हो रहे हैं…")
        title = payload["title"]
        lyrics = payload["lyrics"]
        if not lyrics:
            generated_title, lyrics = _generate_song_lyrics(payload["prompt"])
            if not title:
                title = generated_title

        singer = {
            "male": "warm deep male vocals",
            "female": "warm expressive female vocals",
            "duet": "warm male and female duet vocals",
        }[payload["singer"]]
        music_prompt = (
            f"Original Hindi song, {singer}, natural expressive singing, "
            f"{payload['prompt']}"
        )
        _set_song_job(
            job_id,
            message="Local song model शुरू हो रहा है; पहली बार model download हो सकता है…",
        )
        health = _start_song_generator()
        if not health.get("models_initialized"):
            _set_song_job(
                job_id,
                message="संगीत model तैयार हो रहा है; पहली बार लगभग 10 GB download हो सकता है…",
            )
            _song_api_request(
                "/v1/init",
                method="POST",
                payload={
                    "model": "acestep-v15-turbo",
                    "init_llm": False,
                },
                timeout=6 * 60 * 60,
            )

        _set_song_job(job_id, message="गीत गाया और संगीत बनाया जा रहा है… CPU पर इसमें समय लग सकता है।")
        created = _song_api_request(
            "/release_task",
            method="POST",
            payload={
                "prompt": music_prompt,
                "lyrics": lyrics,
                "thinking": False,
                "vocal_language": "hi",
                "audio_format": "mp3",
                "model": "acestep-v15-turbo",
                "audio_duration": payload["duration"],
                "inference_steps": 8,
                "bpm": 95,
                "key_scale": "C Major",
                "time_signature": "4/4",
                "use_cot_caption": False,
                "use_cot_language": False,
                "use_format": False,
            },
            timeout=60,
        )
        task_id = created.get("task_id") if isinstance(created, dict) else None
        if not isinstance(task_id, str) or not task_id:
            raise RuntimeError("Song generator did not return a generation task ID.")

        deadline = time.monotonic() + 3600
        result = None
        while time.monotonic() < deadline:
            time.sleep(5)
            query = _song_api_request(
                "/query_result",
                method="POST",
                payload={"task_id_list": [task_id]},
                timeout=30,
            )
            tasks = query if isinstance(query, list) else []
            current = next(
                (item for item in tasks if isinstance(item, dict) and item.get("task_id") == task_id),
                None,
            )
            if current is None:
                raise RuntimeError("Song generator lost the active generation task.")
            if current.get("status") == 2:
                raise RuntimeError(str(current.get("error") or "Song generation failed."))
            if current.get("status") == 1:
                raw_result = current.get("result")
                try:
                    parsed_result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
                except json.JSONDecodeError as error:
                    raise RuntimeError("Song generator returned invalid audio details.") from error
                if isinstance(parsed_result, list) and parsed_result:
                    result = parsed_result[0]
                elif isinstance(parsed_result, dict):
                    result = parsed_result
                break

        if result is None:
            raise RuntimeError("Song generation timed out after one hour.")
        audio_url = result.get("file") if isinstance(result, dict) else None
        if not isinstance(audio_url, str):
            raise RuntimeError("Song generator did not return an audio file.")
        parsed_audio_url = urlsplit(audio_url)
        if parsed_audio_url.path != "/v1/audio" or not parsed_audio_url.query:
            raise RuntimeError("Song generator returned an invalid audio URL.")

        _set_song_job(job_id, message="गाना local music library में save हो रहा है…")
        audio_request = Request(
            f"{SONG_GENERATOR_URL}{parsed_audio_url.path}?{parsed_audio_url.query}"
        )
        try:
            with urlopen(audio_request, timeout=120) as response:
                audio = response.read(MAX_MUSIC_UPLOAD_BYTES + 1)
        except (HTTPError, URLError, OSError, TimeoutError) as error:
            raise RuntimeError("Generated audio could not be downloaded from the local model.") from error
        if not audio or len(audio) > MAX_MUSIC_UPLOAD_BYTES:
            raise RuntimeError("Generated audio is empty or larger than the 50 MB library limit.")

        track = _save_generated_music_track(
            title or payload["prompt"][:100] or "Sweetheart का नया गाना",
            payload["prompt"],
            lyrics,
            payload["duration"],
            audio,
        )
        _set_song_job(
            job_id,
            status="completed",
            message="गाना तैयार है और आपकी local music library में save हो गया।",
            track=track,
        )
    except Exception as error:
        _logger.exception("Local song generation failed for job %s.", job_id)
        _set_song_job(job_id, status="failed", message=str(error), error=str(error))


def _stop_song_generator():
    global _song_generator_process, _song_generator_log

    process = _song_generator_process
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    if _song_generator_log is not None:
        _song_generator_log.close()
        _song_generator_log = None


atexit.register(_stop_song_generator)


def generate_personal_answer(messages):
    global _personal_model, _personal_tokenizer

    with _personal_model_lock:
        if _personal_model is None:
            import torch
            from peft import PeftModel
            from transformers import AutoModelForCausalLM, AutoTokenizer

            _personal_tokenizer = AutoTokenizer.from_pretrained(
                PERSONAL_MODEL_ID,
                local_files_only=True,
            )
            base_model = AutoModelForCausalLM.from_pretrained(
                PERSONAL_MODEL_ID,
                local_files_only=True,
                torch_dtype=torch.float32,
            )
            _personal_model = PeftModel.from_pretrained(
                base_model,
                PERSONAL_ADAPTER_PATH,
                local_files_only=True,
            )
            _personal_model.eval()

        prompt = _personal_tokenizer.apply_chat_template(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                *messages,
            ],
            tokenize=False,
            add_generation_prompt=True,
        )
        inputs = _personal_tokenizer(
            prompt,
            return_tensors="pt",
            truncation=True,
            max_length=1024,
        )
        output = _personal_model.generate(
            **inputs,
            max_new_tokens=256,
            do_sample=True,
            temperature=0.7,
            pad_token_id=_personal_tokenizer.eos_token_id,
        )
        generated_tokens = output[0, inputs["input_ids"].shape[1] :]
        return _personal_tokenizer.decode(
            generated_tokens,
            skip_special_tokens=True,
        ).strip()


class HindiAIHandler(BaseHTTPRequestHandler):
    server_version = "HindiPersonalAI/1.0"

    def send_json(self, status_code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        request_path = urlsplit(self.path).path
        if request_path == "/api/voice-sample":
            self.handle_voice_sample_info()
            return
        if request_path == "/api/voice-sample/audio":
            self.handle_voice_sample_audio()
            return
        if request_path == "/api/music":
            self.handle_music_list()
            return
        if request_path == "/api/song-generator":
            self.handle_song_generator_status()
            return
        generation_match = re.fullmatch(
            r"/api/music/generate/([0-9a-f-]+)",
            request_path,
        )
        if generation_match:
            self.handle_song_generation_status(generation_match.group(1))
            return
        audio_match = re.fullmatch(r"/api/music/([0-9a-f-]+)/audio", request_path)
        if audio_match:
            self.handle_music_audio(audio_match.group(1))
            return
        if request_path == "/":
            asset_path = INDEX_PATH
            content_type = "text/html; charset=utf-8"
        elif request_path == "/assets/sweetheart.svg":
            asset_path = ICON_PATH
            content_type = "image/svg+xml"
        else:
            self.send_json(404, {"error": "Not found."})
            return

        try:
            page = asset_path.read_bytes()
        except OSError:
            self.send_json(500, {"error": "The app resource could not be loaded."})
            return

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(page)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if content_type.startswith("text/html"):
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(page)

    def do_POST(self):
        request_path = urlsplit(self.path).path
        if request_path == "/api/open-voice-settings":
            self.handle_open_voice_settings()
            return
        if request_path == "/api/music":
            self.handle_music_create()
            return
        if request_path == "/api/music/generate":
            self.handle_song_generation_create()
            return
        if request_path != "/api/chat":
            self.send_json(404, {"error": "Not found."})
            return
        self.handle_chat()

    def do_PUT(self):
        if urlsplit(self.path).path == "/api/voice-sample":
            self.handle_voice_sample_upload()
            return
        audio_match = re.fullmatch(
            r"/api/music/([0-9a-f-]+)/audio",
            urlsplit(self.path).path,
        )
        if not audio_match:
            self.send_json(404, {"error": "Not found."})
            return
        self.handle_music_audio_upload(audio_match.group(1))

    def do_DELETE(self):
        if urlsplit(self.path).path == "/api/voice-sample":
            self.handle_voice_sample_delete()
            return
        track_match = re.fullmatch(r"/api/music/([0-9a-f-]+)", urlsplit(self.path).path)
        if not track_match:
            self.send_json(404, {"error": "Not found."})
            return
        self.handle_music_delete(track_match.group(1))

    def read_music_library(self):
        if not MUSIC_INDEX_PATH.exists():
            return []
        try:
            library = json.loads(MUSIC_INDEX_PATH.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OSError("The music library could not be read.") from error
        if not isinstance(library, list) or any(
            not isinstance(track, dict) for track in library
        ):
            raise OSError("The music library has an invalid format.")
        return library

    def write_music_library(self, library):
        MUSIC_DIR.mkdir(parents=True, exist_ok=True)
        temporary_path = MUSIC_INDEX_PATH.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(library, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(MUSIC_INDEX_PATH)

    def handle_music_list(self):
        try:
            with _music_lock:
                library = self.read_music_library()
        except OSError as error:
            self.log_error(f"Music library error: {error}")
            self.send_json(500, {"error": "म्यूज़िक लाइब्रेरी पढ़ी नहीं जा सकी।"})
            return
        for track in library:
            track_id = self.parse_music_track_id(track.get("id", ""))
            track["has_audio"] = (
                track_id is not None
                and (MUSIC_DIR / f"{track_id}.audio").is_file()
            )
        library.sort(
            key=lambda track: str(track.get("created_at", "")),
            reverse=True,
        )
        self.send_json(200, {"tracks": library})

    def handle_song_generator_status(self):
        installed = SONG_GENERATOR_PYTHON.is_file() and (
            SONG_GENERATOR_ENGINE_DIR / "acestep" / "api_server.py"
        ).is_file()
        if not installed:
            self.send_json(
                200,
                {
                    "installed": False,
                    "running": False,
                    "models_loaded": False,
                    "message": "Local song generator is not installed.",
                },
            )
            return
        try:
            health = _song_generator_health()
        except RuntimeError as error:
            self.send_json(503, {"error": str(error)})
            return
        self.send_json(
            200,
            {
                "installed": True,
                "running": health is not None,
                "models_loaded": bool(health and health.get("models_initialized")),
                "message": (
                    "ACE-Step is ready."
                    if health and health.get("models_initialized")
                    else "ACE-Step is installed and will start when you generate a song."
                ),
            },
        )

    def handle_song_generation_create(self):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "कृपया सही Content-Length भेजें।"})
            return
        if content_length <= 0 or content_length > MAX_BODY_BYTES:
            self.send_json(413, {"error": "गीत का अनुरोध खाली या बहुत बड़ा है।"})
            self.close_connection = content_length > MAX_BODY_BYTES
            return
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(400, {"error": "कृपया सही JSON अनुरोध भेजें।"})
            return
        if not isinstance(payload, dict):
            self.send_json(400, {"error": "गीत का अनुरोध सही नहीं है।"})
            return

        prompt = payload.get("prompt")
        title = payload.get("title", "")
        lyrics = payload.get("lyrics", "")
        singer = payload.get("singer", "male")
        duration = payload.get("duration", 30)
        if not isinstance(prompt, str) or not 5 <= len(prompt.strip()) <= 1200:
            self.send_json(400, {"error": "गीत का prompt 5 से 1,200 अक्षरों का रखें।"})
            return
        if not isinstance(title, str) or len(title) > 200:
            self.send_json(400, {"error": "गीत का नाम 200 अक्षरों से छोटा रखें।"})
            return
        if not isinstance(lyrics, str) or len(lyrics) > 8000:
            self.send_json(400, {"error": "गीत के बोल 8,000 अक्षरों से छोटे रखें।"})
            return
        if singer not in ("male", "female", "duet"):
            self.send_json(400, {"error": "कृपया उपलब्ध vocal style चुनें।"})
            return
        if (
            isinstance(duration, bool)
            or not isinstance(duration, int)
            or not 10 <= duration <= 60
        ):
            self.send_json(400, {"error": "गीत की अवधि 10 से 60 सेकंड चुनें।"})
            return

        job_id = str(uuid.uuid4())
        job = {
            "id": job_id,
            "status": "queued",
            "message": "गीत तैयार करने का अनुरोध कतार में है…",
            "created_at": time.monotonic(),
        }
        with _song_job_lock:
            expired_jobs = [
                existing_id
                for existing_id, existing_job in _song_jobs.items()
                if time.monotonic() - existing_job["created_at"] > 3600
                and existing_job["status"] in ("completed", "failed")
            ]
            for existing_id in expired_jobs:
                del _song_jobs[existing_id]
            _song_jobs[job_id] = job

        generation_payload = {
            "prompt": prompt.strip(),
            "title": title.strip(),
            "lyrics": lyrics.strip(),
            "singer": singer,
            "duration": duration,
        }
        threading.Thread(
            target=_run_song_generation,
            args=(job_id, generation_payload),
            daemon=True,
        ).start()
        self.send_json(
            202,
            {"job_id": job_id, "status": "queued"},
        )

    def handle_song_generation_status(self, job_id):
        try:
            job_id = str(uuid.UUID(job_id))
        except ValueError:
            self.send_json(400, {"error": "गीत की पहचान सही नहीं है।"})
            return
        with _song_job_lock:
            job = _song_jobs.get(job_id)
            result = dict(job) if job is not None else None
        if result is None:
            self.send_json(404, {"error": "गीत बनाने का यह अनुरोध नहीं मिला।"})
            return
        result.pop("created_at", None)
        self.send_json(200, result)

    def handle_music_create(self):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "कृपया सही Content-Length भेजें।"})
            return
        if content_length <= 0 or content_length > MAX_BODY_BYTES:
            self.send_json(413, {"error": "म्यूज़िक की जानकारी खाली या बहुत बड़ी है।"})
            self.close_connection = content_length > MAX_BODY_BYTES
            return
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(400, {"error": "कृपया सही JSON जानकारी भेजें।"})
            return
        if not isinstance(payload, dict):
            self.send_json(400, {"error": "म्यूज़िक की जानकारी सही नहीं है।"})
            return

        string_fields = {
            "title": 200,
            "artist": 200,
            "album": 200,
            "genre": 100,
            "language": 80,
            "composer": 200,
            "lyricist": 200,
            "label": 200,
            "mood": 100,
            "notes": 2000,
            "filename": 255,
        }
        details = {}
        for field, max_length in string_fields.items():
            value = payload.get(field, "")
            if not isinstance(value, str) or len(value) > max_length:
                self.send_json(400, {"error": f"‘{field}’ की जानकारी सही नहीं है।"})
                return
            details[field] = value.strip()
        if not details["title"] or not details["artist"]:
            self.send_json(400, {"error": "गाने का नाम और कलाकार भरना ज़रूरी है।"})
            return

        for field, minimum, maximum in (
            ("release_year", 1880, 2100),
            ("track_number", 1, 9999),
            ("duration_seconds", 0, 86400),
        ):
            value = payload.get(field)
            if value is None or value == "":
                details[field] = None
            elif (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                self.send_json(400, {"error": f"‘{field}’ की जानकारी सही नहीं है।"})
                return
            else:
                details[field] = value

        track = {
            "id": str(uuid.uuid4()),
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            **details,
        }
        try:
            with _music_lock:
                library = self.read_music_library()
                library.append(track)
                self.write_music_library(library)
        except OSError as error:
            self.log_error(f"Could not save music details: {error}")
            self.send_json(500, {"error": "म्यूज़िक की जानकारी save नहीं हो सकी।"})
            return
        track["has_audio"] = False
        self.send_json(201, {"track": track})

    def parse_music_track_id(self, track_id):
        try:
            return str(uuid.UUID(track_id))
        except ValueError:
            return None

    def handle_music_audio_upload(self, track_id):
        track_id = self.parse_music_track_id(track_id)
        if track_id is None:
            self.send_json(400, {"error": "म्यूज़िक पहचान सही नहीं है।"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "कृपया सही Content-Length भेजें।"})
            return
        if content_length <= 0 or content_length > MAX_MUSIC_UPLOAD_BYTES:
            self.send_json(
                413,
                {"error": "ऑडियो फ़ाइल खाली है या 50 MB से बड़ी है।"},
            )
            self.close_connection = content_length > MAX_MUSIC_UPLOAD_BYTES
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if not re.fullmatch(r"audio/[A-Za-z0-9.+-]+", content_type):
            self.send_json(415, {"error": "कृपया मान्य audio फ़ाइल चुनें।"})
            return
        audio = self.rfile.read(content_length)
        if len(audio) != content_length:
            self.send_json(400, {"error": "ऑडियो फ़ाइल पूरी तरह upload नहीं हुई।"})
            return

        try:
            with _music_lock:
                library = self.read_music_library()
                track = next(
                    (item for item in library if item.get("id") == track_id),
                    None,
                )
                if track is None:
                    self.send_json(404, {"error": "यह गाना लाइब्रेरी में नहीं है।"})
                    return
                MUSIC_DIR.mkdir(parents=True, exist_ok=True)
                audio_path = MUSIC_DIR / f"{track_id}.audio"
                temporary_path = MUSIC_DIR / f"{track_id}.tmp"
                temporary_path.write_bytes(audio)
                temporary_path.replace(audio_path)
                track["audio_content_type"] = content_type
                self.write_music_library(library)
        except OSError as error:
            self.log_error(f"Could not save music audio: {error}")
            self.send_json(500, {"error": "ऑडियो फ़ाइल local app data में save नहीं हुई।"})
            return
        self.send_json(200, {"saved": True})

    def handle_music_audio(self, track_id):
        track_id = self.parse_music_track_id(track_id)
        if track_id is None:
            self.send_json(400, {"error": "म्यूज़िक पहचान सही नहीं है।"})
            return
        try:
            with _music_lock:
                library = self.read_music_library()
                track = next(
                    (item for item in library if item.get("id") == track_id),
                    None,
                )
                audio_path = MUSIC_DIR / f"{track_id}.audio"
                if track is None or not audio_path.is_file():
                    self.send_json(404, {"error": "इस गाने की audio फ़ाइल नहीं मिली।"})
                    return
                audio = audio_path.read_bytes()
                content_type = track.get("audio_content_type", "audio/mpeg")
                if not isinstance(content_type, str) or not re.fullmatch(
                    r"audio/[A-Za-z0-9.+-]+",
                    content_type,
                ):
                    self.send_json(500, {"error": "ऑडियो फ़ाइल का format सही नहीं है।"})
                    return
        except OSError as error:
            self.log_error(f"Could not read music audio: {error}")
            self.send_json(500, {"error": "ऑडियो फ़ाइल नहीं पढ़ी जा सकी।"})
            return

        start, end = 0, len(audio) - 1
        status_code = 200
        range_header = self.headers.get("Range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match or (not match.group(1) and not match.group(2)):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(audio)}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else end
            else:
                suffix_length = int(match.group(2))
                start = max(0, len(audio) - suffix_length)
            if start >= len(audio) or end < start:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(audio)}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, len(audio) - 1)
            status_code = 206
        content = audio[start : end + 1]
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(content)))
        self.send_header(
            "Content-Disposition",
            "inline; filename*=UTF-8''Sweetheart-audio",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        if status_code == 206:
            self.send_header(
                "Content-Range",
                f"bytes {start}-{end}/{len(audio)}",
            )
        self.end_headers()
        self.wfile.write(content)

    def handle_music_delete(self, track_id):
        track_id = self.parse_music_track_id(track_id)
        if track_id is None:
            self.send_json(400, {"error": "म्यूज़िक पहचान सही नहीं है।"})
            return
        try:
            with _music_lock:
                library = self.read_music_library()
                filtered = [item for item in library if item.get("id") != track_id]
                if len(filtered) == len(library):
                    self.send_json(404, {"error": "यह गाना लाइब्रेरी में नहीं है।"})
                    return
                self.write_music_library(filtered)
                audio_path = MUSIC_DIR / f"{track_id}.audio"
                if audio_path.exists():
                    audio_path.unlink()
        except OSError as error:
            self.log_error(f"Could not delete music track: {error}")
            self.send_json(500, {"error": "गाना लाइब्रेरी से हटाया नहीं जा सका।"})
            return
        self.send_json(200, {"deleted": True})

    def handle_open_voice_settings(self):
        if sys.platform != "win32":
            self.send_json(
                501,
                {"error": "Speech voice install settings are available on Windows only."},
            )
            return
        try:
            os.startfile("ms-settings:speech")
        except OSError as error:
            self.log_error(f"Could not open Windows speech settings: {error}")
            self.send_json(
                500,
                {"error": "Windows speech settings नहीं खुलीं। Settings manually खोलें।"},
            )
            return
        self.send_json(
            200,
            {"message": "Windows Speech settings खुल गईं। वहाँ voice add करके वापस आएँ।"},
        )

    def handle_voice_sample_info(self):
        try:
            with _voice_sample_lock:
                if not VOICE_SAMPLE_PATH.is_file() or not VOICE_SAMPLE_INFO_PATH.is_file():
                    self.send_json(200, {"exists": False})
                    return
                metadata = json.loads(VOICE_SAMPLE_INFO_PATH.read_text(encoding="utf-8"))
                if not isinstance(metadata, dict):
                    raise OSError("The voice sample metadata has an invalid format.")
                metadata["exists"] = True
                metadata["size_bytes"] = VOICE_SAMPLE_PATH.stat().st_size
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            self.log_error(f"Could not read personal voice sample: {error}")
            self.send_json(500, {"error": "अपना voice sample नहीं पढ़ा जा सका।"})
            return
        self.send_json(200, metadata)

    def handle_voice_sample_upload(self):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "कृपया सही Content-Length भेजें।"})
            return
        if content_length <= 0 or content_length > MAX_VOICE_SAMPLE_BYTES:
            self.send_json(413, {"error": "Voice recording खाली है या 25 MB से बड़ी है।"})
            self.close_connection = content_length > MAX_VOICE_SAMPLE_BYTES
            return

        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if not re.fullmatch(r"audio/[A-Za-z0-9.+-]+", content_type):
            self.send_json(415, {"error": "कृपया मान्य audio recording चुनें।"})
            return
        filename = self.headers.get("X-Voice-Sample-Filename", "voice-sample")
        filename = Path(filename.replace("\\", "/")).name[:255]
        audio = self.rfile.read(content_length)
        if len(audio) != content_length:
            self.send_json(400, {"error": "Voice recording पूरी तरह save नहीं हुई।"})
            return

        metadata = {
            "filename": filename or "voice-sample",
            "content_type": content_type,
        }
        try:
            with _voice_sample_lock:
                VOICE_SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
                temporary_audio = VOICE_SAMPLE_PATH.with_suffix(".audio.tmp")
                temporary_info = VOICE_SAMPLE_INFO_PATH.with_suffix(".json.tmp")
                temporary_audio.write_bytes(audio)
                temporary_info.write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                temporary_audio.replace(VOICE_SAMPLE_PATH)
                temporary_info.replace(VOICE_SAMPLE_INFO_PATH)
        except OSError as error:
            self.log_error(f"Could not save personal voice sample: {error}")
            self.send_json(500, {"error": "Voice recording local app data में save नहीं हुई।"})
            return
        self.send_json(201, {"saved": True, **metadata, "size_bytes": len(audio)})

    def handle_voice_sample_audio(self):
        try:
            with _voice_sample_lock:
                if not VOICE_SAMPLE_PATH.is_file() or not VOICE_SAMPLE_INFO_PATH.is_file():
                    self.send_json(404, {"error": "अपनी voice recording अभी save नहीं है।"})
                    return
                metadata = json.loads(VOICE_SAMPLE_INFO_PATH.read_text(encoding="utf-8"))
                if not isinstance(metadata, dict):
                    raise OSError("The voice sample metadata has an invalid format.")
                content_type = metadata.get("content_type", "")
                if not isinstance(content_type, str) or not re.fullmatch(
                    r"audio/[A-Za-z0-9.+-]+",
                    content_type,
                ):
                    raise OSError("The voice sample content type is invalid.")
                audio = VOICE_SAMPLE_PATH.read_bytes()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            self.log_error(f"Could not read personal voice sample: {error}")
            self.send_json(500, {"error": "Voice recording नहीं पढ़ी जा सकी।"})
            return

        start, end = 0, len(audio) - 1
        status_code = 200
        range_header = self.headers.get("Range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match or (not match.group(1) and not match.group(2)):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(audio)}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else end
            else:
                suffix_length = int(match.group(2))
                start = max(0, len(audio) - suffix_length)
            if start >= len(audio) or end < start:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(audio)}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, len(audio) - 1)
            status_code = 206
        content = audio[start : end + 1]
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if status_code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(audio)}")
        self.end_headers()
        self.wfile.write(content)

    def handle_voice_sample_delete(self):
        try:
            with _voice_sample_lock:
                for path in (VOICE_SAMPLE_PATH, VOICE_SAMPLE_INFO_PATH):
                    if path.exists():
                        path.unlink()
        except OSError as error:
            self.log_error(f"Could not delete personal voice sample: {error}")
            self.send_json(500, {"error": "अपनी voice recording हटाई नहीं जा सकी।"})
            return
        self.send_json(200, {"deleted": True})

    def handle_chat(self):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "Please send a valid Content-Length."})
            return

        if content_length > MAX_BODY_BYTES:
            self.send_json(413, {"error": "Request is too large."})
            self.close_connection = True
            return

        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(400, {"error": "Please send valid JSON."})
            return

        if not isinstance(payload, dict):
            self.send_json(400, {"error": "Add a question before sending."})
            return

        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            self.send_json(400, {"error": "Add a question before sending."})
            return

        messages = messages[-20:]
        for message in messages:
            if (
                not isinstance(message, dict)
                or message.get("role") not in ("user", "assistant")
                or not isinstance(message.get("content"), str)
                or len(message["content"]) > 10_000
            ):
                self.send_json(
                    400,
                    {"error": "The chat contains an invalid or overly long message."},
                )
                return

        if messages[-1]["role"] != "user":
            self.send_json(400, {"error": "Your latest message must be a question."})
            return

        if PERSONAL_ADAPTER_PATH.is_dir() and not getattr(sys, "frozen", False):
            try:
                answer = generate_personal_answer(messages)
            except ImportError:
                self.send_json(
                    503,
                    {
                        "error": (
                            "Fine-tuned AI packages are missing. Install them with "
                            "`python -m pip install -r requirements-training.txt`."
                        )
                    },
                )
                return
            except (OSError, RuntimeError, ValueError) as error:
                self.log_error(f"Fine-tuned model error: {error}")
                self.send_json(
                    503,
                    {
                        "error": (
                            "The fine-tuned model could not be loaded. Check the "
                            "model download and training output, then restart the app."
                        )
                    },
                )
                return

            if not answer:
                self.send_json(502, {"error": "The fine-tuned model returned an empty answer."})
                return

            self.send_json(200, {"answer": answer})
            return

        request_body = json.dumps(
            {
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    *messages,
                ],
                "stream": False,
                "options": {"temperature": 0.7},
            }
        ).encode("utf-8")
        request = Request(
            OLLAMA_CHAT_URL,
            data=request_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urlopen(request, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            try:
                error_result = json.loads(error.read().decode("utf-8"))
                message = error_result.get("error")
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                message = None
            self.send_json(
                503 if error.code == 404 else 502,
                {
                    "error": (
                        f"Ollama error: {message}"
                        if message
                        else "Ollama could not answer. Please try again."
                    )
                },
            )
            return
        except (URLError, OSError):
            self.send_json(
                503,
                {
                    "error": (
                        "Ollama is not running on this computer. Install Ollama, run "
                        f"`ollama pull {MODEL}`, and try again."
                    )
                },
            )
            return
        except TimeoutError:
            self.send_json(504, {"error": "The answer took too long. Please try again."})
            return
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(502, {"error": "Ollama returned an invalid response."})
            return

        message = result.get("message", {})
        answer = message.get("content", "") if isinstance(message, dict) else ""
        if not isinstance(answer, str) or not answer.strip():
            self.send_json(
                502,
                {"error": "Ollama returned an empty answer. Please try again."},
            )
            return

        self.send_json(200, {"answer": answer.strip()})


class HindiAIHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    with HindiAIHTTPServer(("127.0.0.1", PORT), HindiAIHandler) as server:
        print(f"Hindi Personal AI is running at http://localhost:{PORT}")
        server.serve_forever()
