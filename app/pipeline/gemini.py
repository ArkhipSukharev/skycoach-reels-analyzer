import base64
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from openai import APIError as OpenAIAPIError
from openai import APITimeoutError as OpenAITimeoutError
from openai import OpenAI
from pydantic import BaseModel, Field

from app.config import settings
from app.errors import PipelineError
from app.pipeline.media import Frame

log = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parent.parent / "assets"

# Optional Google SDK path (ProxyAPI / direct Google).
try:
    from google import genai
    from google.genai import errors as genai_errors
    from google.genai import types

    _HAS_GENAI = True
except ImportError:  # pragma: no cover
    genai = None  # type: ignore
    genai_errors = None  # type: ignore
    types = None  # type: ignore
    _HAS_GENAI = False


class FrameObservation(BaseModel):
    frame: int = Field(description="Номер кадра из подписи 'Frame N'")
    logo_visible: bool = Field(description="Виден ли логотип, название или баннер Skycoach")
    box_2d: list[int] = Field(default_factory=list, description="[ymin, xmin, ymax, xmax] в шкале 0-1000, пусто если не виден")
    logo_cut_off: bool = Field(default=False, description="Логотип обрезан краем кадра")
    logo_partially_hidden: bool = Field(default=False, description="Логотип частично перекрыт другими элементами видео")
    skycoach_text: str = Field(default="", description="Текст на экране, связанный со Skycoach: название, сайт, промокод")
    cta_text: str = Field(default="", description="Текстовый призыв к действию на экране, если есть")


class FramesResult(BaseModel):
    frames: list[FrameObservation]


class SpokenMention(BaseModel):
    t: float | None = Field(default=None, description="Секунда ролика")
    quote: str


class FinalResult(BaseModel):
    speech_present: bool
    transcript: str = Field(description="Расшифровка речи на языке оригинала, до ~3000 символов; пусто если речи нет")
    spoken_mentions: list[SpokenMention] = Field(default_factory=list)
    voice_cta: bool = Field(description="Есть ли голосовой призыв воспользоваться Skycoach")
    voice_cta_quote: str = ""
    caption_mentions_skycoach: bool
    text_cta: bool = Field(description="Есть ли текстовый призыв/промокод Skycoach на экране или в описании")
    promo_codes: list[str] = Field(default_factory=list)
    integration_class: int = Field(description="0, 1 или 2")
    class_reason: str = Field(description="Обоснование класса на русском, 1-2 предложения")
    summary: str = Field(description="Краткий вывод для менеджера на русском, 1-3 предложения")


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    models: set[str] = field(default_factory=set)
    cost: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add_tokens(self, model: str, tokens_in: int, tokens_out: int, cost_rub: float | None = None) -> None:
        if cost_rub is None:
            if model == settings.gemini_cheap_model:
                price_in, price_out = settings.gemini_cheap_price_input_per_m, settings.gemini_cheap_price_output_per_m
            else:
                price_in, price_out = settings.gemini_price_input_per_m, settings.gemini_price_output_per_m
            cost_rub = tokens_in / 1e6 * price_in + tokens_out / 1e6 * price_out
        with self.lock:
            self.calls += 1
            self.input_tokens += tokens_in
            self.output_tokens += tokens_out
            self.cost = round(self.cost + float(cost_rub), 5)
            self.models.add(model)

    def add_genai(self, model: str, response) -> None:
        meta = response.usage_metadata
        if meta is None:
            return
        tokens_in = meta.prompt_token_count or 0
        tokens_out = (meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0)
        self.add_tokens(model, tokens_in, tokens_out)

    def add_openai(self, model: str, response) -> None:
        usage = response.usage
        if usage is None:
            self.add_tokens(model, 0, 0, 0.0)
            return
        tokens_in = usage.prompt_tokens or 0
        tokens_out = usage.completion_tokens or 0
        cost_rub = getattr(usage, "cost_rub", None)
        if cost_rub is None:
            extra = getattr(usage, "model_extra", None) or {}
            cost_rub = extra.get("cost_rub")
        self.add_tokens(model, tokens_in, tokens_out, float(cost_rub) if cost_rub is not None else None)


FRAMES_PROMPT = """Ты анализируешь кадры из рекламного ролика Instagram Reels.
Задача: найти на кадрах бренд Skycoach (маркетплейс игровых услуг, сайт skycoach.gg).
Первые два изображения — эталоны: словесный логотип SKYCOACH (буква O стилизована молнией) и значок —
белая молния в фиолетовом круге. Бренд может быть в виде баннера, плашки, наложенного логотипа,
надписи skycoach.gg, промокода, водяного знака или предмета в кадре.

Для КАЖДОГО пронумерованного кадра верни объект:
- logo_visible: true только если на кадре действительно виден логотип, название или баннер Skycoach.
  Не путай с логотипами игр и других брендов. Если сомневаешься — false.
- box_2d: рамка всего брендированного элемента (баннер целиком, а не одна буква) в формате
  [ymin, xmin, ymax, xmax], координаты нормированы 0-1000 относительно кадра.
- logo_cut_off: элемент обрезан краем кадра (часть логотипа не помещается).
- logo_partially_hidden: элемент частично закрыт другими объектами видео.
- skycoach_text: читаемый текст бренда на кадре (например, "skycoach.gg", "code SKY10"), иначе "".
- cta_text: текстовый призыв к действию, если он есть ("переходи по ссылке", "скидка 10%"), иначе "".
Верни ровно по одному объекту на каждый кадр, номер кадра бери из подписи.
Ответ — только JSON по схеме."""


FINAL_PROMPT = """Ты помогаешь менеджеру Skycoach (маркетплейс игровых услуг: бустинг, коучинг, прокачка в играх,
сайт skycoach.gg) проверять рекламные интеграции блогеров в Instagram Reels.

Данные о ролике:
{context}

{audio_note}

Сделай:
1. Расшифруй речь (если она есть) и найди упоминания Skycoach голосом с таймкодами.
   Учитывай искажённые варианты: "скайкоуч", "sky coach", "скай коуч".
2. Определи, есть ли голосовой призыв воспользоваться Skycoach (перейти, купить, промокод, скидка).
3. Определи, упоминается ли Skycoach в описании ролика, и есть ли текстовый призыв или промокод
   (в описании или в тексте на экране из данных выше).
4. Определи класс интеграции:
   0 — про Skycoach в ролике ничего нет;
   1 — Skycoach упоминается (логотип в кадре, название голосом или текстом), но продукт не рекламируется;
   2 — продукт Skycoach рекламируется: призыв, описание услуги, промокод, ссылка с предложением.
   Логотип/баннер, который постоянно висит в кадре без призыва и описания услуги, — это класс 1.
5. Обоснования и вывод пиши по-русски, коротко и по фактам, со ссылкой на секунды и цифры.
Ответ — только JSON по схеме."""


def _use_openai_compat() -> bool:
    key = (settings.gemini_api_key or "").lower()
    base = (settings.gemini_base_url or "").lower()
    return key.startswith("sk-aitunnel") or "aitunnel" in base or settings.llm_api_style == "openai"


def _openai_base_url() -> str:
    base = (settings.gemini_base_url or "https://api.aitunnel.ru/v1").rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base + "/"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _json_schema(model: type[BaseModel]) -> dict:
    schema = model.model_json_schema()
    # OpenAI/AI Tunnel strict schemas dislike some pydantic extras; keep usable subset.
    return {
        "name": model.__name__,
        "strict": False,
        "schema": schema,
    }


class GeminiAnalyzer:
    def __init__(self) -> None:
        if not settings.gemini_api_key:
            raise PipelineError("config_error", "Не задан GEMINI_API_KEY в .env — анализ видео невозможен.")
        self.usage = Usage()
        self._openai = None
        self._genai = None
        self._ref_og = (ASSETS / "skycoach_og.jpg").read_bytes()
        self._ref_icon = (ASSETS / "skycoach_icon.png").read_bytes()
        if _use_openai_compat():
            # Жёсткий таймаут: иначе AI Tunnel может держать сокет минутами без ответа.
            timeout = httpx.Timeout(connect=15.0, read=120.0, write=60.0, pool=15.0)
            self._openai = OpenAI(
                api_key=settings.gemini_api_key,
                base_url=_openai_base_url(),
                timeout=timeout,
                max_retries=0,
            )
            log.info("LLM via OpenAI-compatible API: %s", _openai_base_url())
        else:
            if not _HAS_GENAI:
                raise PipelineError("config_error", "google-genai не установлен, а LLM_API_STYLE не openai.")
            http_options = types.HttpOptions(base_url=settings.gemini_base_url) if settings.gemini_base_url else None
            self._genai = genai.Client(api_key=settings.gemini_api_key, http_options=http_options)
            self._references = [
                types.Part.from_bytes(data=self._ref_og, mime_type="image/jpeg"),
                types.Part.from_bytes(data=self._ref_icon, mime_type="image/png"),
            ]

    def detect_frames(self, frames: list[Frame], models: list[str] | None = None) -> dict[int, FrameObservation]:
        size = settings.frame_batch_size
        batches = [frames[start:start + size] for start in range(0, len(frames), size)]
        observations: dict[int, FrameObservation] = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            for batch_result in pool.map(lambda batch: self._detect_batch(batch, models), batches):
                observations.update(batch_result)
        return observations

    def _detect_batch(self, batch: list[Frame], models: list[str] | None) -> dict[int, FrameObservation]:
        if self._openai is not None:
            content: list[dict] = [{"type": "text", "text": FRAMES_PROMPT}]
            content.append({"type": "text", "text": "Эталон 1:"})
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_b64(self._ref_og)}"}})
            content.append({"type": "text", "text": "Эталон 2:"})
            content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_b64(self._ref_icon)}"}})
            for frame in batch:
                content.append({"type": "text", "text": f"Frame {frame.index} (t={frame.t:.1f}s)"})
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{_b64(frame.path.read_bytes())}"},
                })
            result: FramesResult = self._generate_openai(content, FramesResult, models, max_tokens=16000)
        else:
            contents: list = [FRAMES_PROMPT, "Эталон 1:", self._references[0], "Эталон 2:", self._references[1]]
            for frame in batch:
                contents.append(f"Frame {frame.index} (t={frame.t:.1f}s)")
                contents.append(types.Part.from_bytes(data=frame.path.read_bytes(), mime_type="image/jpeg"))
            result = self._generate_genai(contents, FramesResult, models)
        valid = {frame.index for frame in batch}
        return {obs.frame: obs for obs in result.frames if obs.frame in valid}

    def final_analysis(self, context: str, audio: Path | None, audio_note: str) -> FinalResult:
        prompt = FINAL_PROMPT.format(context=context, audio_note=audio_note)
        if self._openai is not None:
            content: list[dict] = [{"type": "text", "text": prompt}]
            if audio is not None:
                if not audio.exists():
                    log.warning("Audio file missing (%s), classify without speech", audio)
                    content[0] = {
                        "type": "text",
                        "text": FINAL_PROMPT.format(
                            context=context,
                            audio_note="Аудиофайл недоступен — анализ речи пропущен, опирайся на описание и текст на экране.",
                        ),
                    }
                else:
                    fmt = "mp3" if audio.suffix.lower() == ".mp3" else "wav"
                    content.append({
                        "type": "input_audio",
                        "input_audio": {"data": _b64(audio.read_bytes()), "format": fmt},
                    })
            return self._generate_openai(content, FinalResult, None, max_tokens=8000)
        contents: list = [prompt]
        if audio is not None and audio.exists():
            contents.append(types.Part.from_bytes(data=audio.read_bytes(), mime_type="audio/mp3"))
        return self._generate_genai(contents, FinalResult, None)

    def _openai_create(self, model: str, content: list[dict], max_tokens: int, response_format: dict):
        # Gemini 3.x через AI Tunnel всегда думает; low + большой max_tokens,
        # иначе finish_reason=length и content=None (все токены уходят в reasoning).
        kwargs = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": response_format,
            "extra_body": {"reasoning": {"effort": "low", "exclude": True}},
        }
        return self._openai.chat.completions.create(**kwargs)

    def _generate_openai(self, content: list[dict], schema: type[BaseModel], models: list[str] | None, max_tokens: int):
        models = models or default_models()
        available = [m for m in models if _exhausted_until.get(m, 0) < time.time()]
        if not available:
            raise QUOTA_ERROR
        last_error: Exception | None = None
        schema_format = {"type": "json_schema", "json_schema": _json_schema(schema)}
        object_format = {"type": "json_object"}
        for model in available:
            use_format = schema_format
            for attempt in range(3):
                try:
                    with _slots:
                        response = self._openai_create(model, content, max_tokens, use_format)
                    self.usage.add_openai(model, response)
                    choice = response.choices[0]
                    raw = choice.message.content or ""
                    if not raw.strip():
                        finish = choice.finish_reason
                        log.warning(
                            "AI Tunnel %s empty content (finish=%s), attempt %s — bump max_tokens",
                            model, finish, attempt + 1,
                        )
                        max_tokens = min(max_tokens * 2, 65536)
                        time.sleep(1)
                        continue
                    try:
                        return schema.model_validate_json(raw)
                    except Exception:
                        return schema.model_validate(json.loads(raw))
                except OpenAITimeoutError as exc:
                    last_error = exc
                    log.warning("AI Tunnel %s timeout, attempt %s", model, attempt + 1)
                    time.sleep(2)
                    continue
                except OpenAIAPIError as exc:
                    last_error = exc
                    code = getattr(exc, "status_code", None) or 0
                    message = str(exc)
                    if code in (401, 403):
                        raise PipelineError("config_error", "AI Tunnel отклонил ключ. Проверьте GEMINI_API_KEY.")
                    if code == 404:
                        log.warning("Model %s not available on AI Tunnel, trying next", model)
                        break
                    if code == 402:
                        raise PipelineError(
                            "config_error",
                            "Недостаточно средств на балансе AI Tunnel (или слишком большой max_tokens).",
                        )
                    if code in (429, 500, 502, 503, 504):
                        delay = _retry_delay(message, default=4 * (attempt + 1))
                        log.warning("AI Tunnel %s returned %s, attempt %s, sleep %.0fs", model, code, attempt + 1, delay)
                        time.sleep(delay)
                        continue
                    if use_format is schema_format and (
                        "json_schema" in message.lower() or "response_format" in message.lower()
                    ):
                        log.warning("AI Tunnel %s json_schema unsupported, fallback to json_object", model)
                        use_format = object_format
                        continue
                    if "reasoning" in message.lower() or "thinking" in message.lower():
                        # некоторые модели не принимают effort=low — повторим без extra_body
                        try:
                            with _slots:
                                response = self._openai.chat.completions.create(
                                    model=model,
                                    messages=[{"role": "user", "content": content}],
                                    temperature=0,
                                    max_tokens=max_tokens,
                                    response_format=use_format,
                                )
                            self.usage.add_openai(model, response)
                            raw = response.choices[0].message.content or ""
                            if not raw.strip():
                                max_tokens = min(max_tokens * 2, 65536)
                                continue
                            return schema.model_validate_json(raw)
                        except Exception as fallback_exc:
                            last_error = fallback_exc
                            time.sleep(2)
                            continue
                    raise PipelineError("analysis_error", f"AI Tunnel вернул ошибку: {exc}"[:400])
                except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
                    last_error = exc
                    log.warning("AI Tunnel %s parse/network error: %s, attempt %s", model, exc, attempt + 1)
                    time.sleep(4 * (attempt + 1))
                except PipelineError as exc:
                    last_error = exc
                    time.sleep(2)
        raise PipelineError("analysis_error", "Модель перегружена или недоступна через AI Tunnel.", retryable=True) from last_error

    def _generate_genai(self, contents: list, schema: type[BaseModel], models: list[str] | None = None):
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0,
            media_resolution=settings.gemini_media_resolution or None,
            thinking_config=types.ThinkingConfig(thinking_level="low"),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        models = models or default_models()
        available = [m for m in models if _exhausted_until.get(m, 0) < time.time()]
        if not available:
            raise QUOTA_ERROR
        last_error: Exception | None = None
        for model in available:
            for attempt in range(3):
                try:
                    with _slots:
                        response = self._genai.models.generate_content(model=model, contents=contents, config=config)
                    self.usage.add_genai(model, response)
                    if response.parsed is None:
                        raise PipelineError("analysis_error", "Модель вернула ответ не в том формате.", retryable=True)
                    return response.parsed
                except genai_errors.APIError as exc:
                    last_error = exc
                    code = getattr(exc, "code", None) or 0
                    message = str(exc)
                    if code in (401, 403):
                        raise PipelineError("config_error", "Gemini отклонил ключ. Проверьте GEMINI_API_KEY.")
                    if code == 404:
                        log.warning("Gemini model %s not available, trying next", model)
                        break
                    if code == 429 and "PerDay" in message:
                        log.warning("Gemini %s daily quota exhausted, switching model", model)
                        _exhausted_until[model] = time.time() + 3600
                        break
                    if code in (429, 500, 502, 503, 504):
                        delay = _retry_delay(message, default=4 * (attempt + 1))
                        log.warning("Gemini %s returned %s, attempt %s, sleep %.0fs", model, code, attempt + 1, delay)
                        time.sleep(delay)
                        continue
                    raise PipelineError("analysis_error", f"Gemini вернул ошибку: {exc}"[:400])
                except httpx.HTTPError as exc:
                    last_error = exc
                    log.warning("Gemini %s network error: %s, attempt %s", model, exc, attempt + 1)
                    time.sleep(4 * (attempt + 1))
                except PipelineError as exc:
                    last_error = exc
                    time.sleep(2)
        if all(_exhausted_until.get(m, 0) >= time.time() for m in models):
            raise QUOTA_ERROR
        raise PipelineError("analysis_error", "Gemini перегружен или недоступен, попробуем позже.", retryable=True) from last_error


def default_models() -> list[str]:
    return [settings.gemini_model] + [m.strip() for m in settings.gemini_fallback_models.split(",") if m.strip()]


_slots = threading.BoundedSemaphore(max(1, settings.gemini_max_concurrency))
_exhausted_until: dict[str, float] = {}
QUOTA_ERROR = PipelineError(
    "quota_exceeded",
    "Исчерпана квота или баланс LLM. Проверьте кабинет AI Tunnel / Gemini и повторите позже.",
    retryable=True,
)


def _retry_delay(message: str, default: float) -> float:
    match = re.search(r"retry in ([\d.]+)s", message)
    return min(float(match.group(1)) + 1, 60) if match else default
