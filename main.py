import asyncio
import base64
import io
import json
import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import aiohttp
from PIL import Image

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as MessageImage, Reply
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.media_utils import MediaResolver


IMAGE_EXTENSIONS = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}
MAX_REFERENCE_DATA_URL = 20 * 1024 * 1024
MAX_REFERENCE_BYTES = (MAX_REFERENCE_DATA_URL - len("data:image/webp;base64,")) // 4 * 3
MAX_TOTAL_REFERENCE_BYTES = 24 * 1024 * 1024
MAX_HISTORY_BYTES = 64 * 1024 * 1024
HISTORY_TTL = 3600


class ReferenceTooLarge(ValueError):
    pass


def is_integer(value: object) -> bool:
    return type(value) in (int, float) and value % 1 == 0


def image_format(raw: bytes) -> str:
    with Image.open(io.BytesIO(raw)) as image:
        if image.format not in IMAGE_EXTENSIONS:
            raise ValueError("Unsupported image format")
        image.verify()
        return image.format


def read_reference_file(path: Path) -> bytes:
    with path.open("rb") as file:
        raw = file.read(MAX_REFERENCE_BYTES + 1)
    if len(raw) > MAX_REFERENCE_BYTES:
        raise ReferenceTooLarge
    return raw


async def read_reference(reference: MessageImage) -> bytes | bytearray:
    source = reference.url or reference.file
    if not source:
        raise ValueError("Missing reference image")
    if source.startswith(("http://", "https://")):
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.get(source) as response:
                response.raise_for_status()
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    if len(raw) + len(chunk) > MAX_REFERENCE_BYTES:
                        raise ReferenceTooLarge
                    raw.extend(chunk)
                return raw

    # Bound inline data before AstrBot decodes it into a temporary file.
    if len(source) > MAX_REFERENCE_DATA_URL:
        raise ReferenceTooLarge
    async with MediaResolver(source, media_type="image").as_path() as resolved:
        return await asyncio.to_thread(read_reference_file, resolved.path)


@dataclass(frozen=True)
class ImageJob:
    event: AstrMessageEvent
    prompt: str
    count: int
    references: tuple[MessageImage | bytes, ...]
    options: dict[str, str]
    history_key: tuple[str, str]


@dataclass(frozen=True)
class ImageBatch:
    images: tuple[bytes, ...]
    options: dict[str, str]
    created_at: float


@register("astrbot_plugin_image_tool", "Codex", "Generate images through the Images API", "0.4.0")
class ImageTool(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.max_concurrent = max(1, int(config.get("max_concurrent", 1)))
        self.max_pending = max(0, int(config.get("max_pending", 3)))
        self.workers: set[asyncio.Task[None]] = set()
        self.pending: deque[ImageJob] = deque()
        self.stopping = False
        self.history: OrderedDict[tuple[str, str], ImageBatch] = OrderedDict()

    def _prune_history(self) -> None:
        now = time.monotonic()
        for key, batch in tuple(self.history.items()):
            if now - batch.created_at >= HISTORY_TTL:
                del self.history[key]
        total = sum(len(raw) for batch in self.history.values() for raw in batch.images)
        while total > MAX_HISTORY_BYTES:
            _, batch = self.history.popitem(last=False)
            total -= sum(map(len, batch.images))

    @filter.llm_tool(name="generate_image")
    async def generate(
        self, event: AstrMessageEvent, prompt: str, count: int | float = 1,
        reference_index: int | float = 0,
        reference_indices: list | None = None, previous_image_index: int | float = 0,
        size: str = "", quality: str = "", background: str = "", output_format: str = "",
    ) -> str:
        """Generate or edit images when the user explicitly asks.

        Args:
            prompt(string): Describe the image to generate.
            count(number): Number of output images, an integer from 1 to 5. Use 1 unless the user asks for more.
            reference_indices(array[number]): For an explicit request to use references, select up to 16 images by their 1-based positions, counting current-message images first and then quoted-message images. Use [1] for one reference or [1, 2] for two. Omit or use [] for no message references.
            previous_image_index(number): For follow-up edits without resending, select a 1-based image from this user's most recently delivered batch in this conversation. Use 1 for "make it darker", or 2 for "edit the second image". Use 0 for a new image. Only the latest batch is available for up to one hour. Can be combined with current references, with the previous image first.
            size(string): Output size. Use auto, square, landscape, portrait, or WIDTHxHEIGHT such as 1536x1024. Omit or use an empty string for service defaults, or to inherit settings when editing a previous result.
            quality(string): Output quality. Use auto, low, medium, high, xhigh, or max when requested. Omit or use an empty string for defaults or inherited settings.
            background(string): Use transparent, opaque, or auto. Omit or use an empty string for defaults or inherited settings. Transparent output requires PNG or WebP.
            output_format(string): Use png, jpeg, or webp. Omit or use an empty string for defaults or inherited settings.
        """
        prompt = prompt.strip()
        if not prompt:
            return "缺少图片描述。"
        if not is_integer(count) or not 1 <= count <= 5:
            return "生成张数须为 1 到 5 的整数。"
        # Keep old direct calls compatible. AstrBot exposes only docstring parameters.
        if not is_integer(reference_index) or reference_index < 0:
            return "参考图编号须为非负整数。"
        if not is_integer(previous_image_index) or previous_image_index < 0:
            return "历史图片编号须为非负整数。"
        if reference_indices is None:
            reference_indices = []
        if not isinstance(reference_indices, list) or any(
            not is_integer(index) or index < 1 for index in reference_indices
        ):
            return "参考图编号列表须由正整数构成。"
        if reference_index and reference_indices:
            return "请使用单个参考图编号或参考图编号列表中的一种。"
        indices = [int(index) for index in reference_indices]
        if reference_index:
            indices = [int(reference_index)]
        if len(indices) != len(set(indices)) or len(indices) + bool(previous_image_index) > 16:
            return "每次最多使用 16 张参考图，编号不能重复。"
        if not self.config["api_key"].strip():
            return "生图服务尚未配置 API Key。"
        if self.stopping:
            return "生图工具正在停止，请勿自动重试。"
        self._prune_history()
        history_key = (event.unified_msg_origin, event.get_sender_id())
        selected = []
        options = {}
        if previous_image_index:
            batch = self.history.get(history_key)
            if batch is None or previous_image_index > len(batch.images):
                return "找不到最近生成的指定图片，请重新发送或引用要修改的图片。"
            selected.append(batch.images[int(previous_image_index) - 1])
            options.update(batch.options)
        if indices:
            parts = event.get_messages()
            references = [part for part in parts if isinstance(part, MessageImage)]
            for part in parts:
                if isinstance(part, Reply):
                    references.extend(item for item in part.chain or [] if isinstance(item, MessageImage))
            if any(index > len(references) for index in indices):
                return "找不到指定的参考图，请让用户重新发送图片或引用含图消息。"
            selected.extend(references[index - 1] for index in indices)
        aliases = {"square": "1024x1024", "landscape": "1536x1024", "portrait": "1024x1536"}
        if not all(isinstance(value, str) for value in (size, quality, background, output_format)):
            return "尺寸、画质、背景和格式须为字符串。"
        size = aliases.get(size, size)
        if size and size != "auto":
            match = re.fullmatch(r"([1-9][0-9]{0,3})x([1-9][0-9]{0,3})", size)
            if match is None:
                return "尺寸须为 auto、square、landscape、portrait 或 WIDTHxHEIGHT。"
            width, height = map(int, match.groups())
            if (
                width % 16 or height % 16 or max(width, height) > 3840
                or max(width, height) > 3 * min(width, height)
                or not 655360 <= width * height <= 8294400
            ):
                return "尺寸边长须为 16 的倍数且不超过 3840，比例须在 1:3 到 3:1 之间，总像素须在 655360 到 8294400 之间。"
        if quality not in ("", "auto", "low", "medium", "high", "xhigh", "max"):
            return "不支持的画质选项。"
        if background not in ("", "auto", "opaque", "transparent"):
            return "不支持的背景选项。"
        if output_format not in ("", "png", "jpeg", "webp"):
            return "输出格式须为 png、jpeg 或 webp。"
        options.update({key: value for key, value in {
            "size": size, "quality": quality, "background": background,
            "output_format": output_format,
        }.items() if value})
        if options.get("background") == "transparent":
            if output_format == "jpeg":
                return "透明背景请使用 PNG 或 WebP 格式。"
            if options.get("output_format") not in ("png", "webp"):
                options["output_format"] = "png"
        job = ImageJob(event, prompt, int(count), tuple(selected), options, history_key)
        if len(self.workers) >= self.max_concurrent:
            if len(self.pending) >= self.max_pending:
                return "生图队列已满，请勿自动重试。"
            self.pending.append(job)
            return "生图任务已排队，完成后会自动发送图片。请勿重复提交。"
        self.workers.add(asyncio.create_task(self._process_queue(job)))
        return "生图任务已提交，完成后会自动发送图片。请勿重复提交。"

    async def _process_queue(self, job: ImageJob) -> None:
        try:
            await self._generate_and_send(job)
            while self.pending:
                await self._generate_and_send(self.pending.popleft())
        finally:
            worker = asyncio.current_task()
            if worker is not None:
                self.workers.discard(worker)

    async def _generate_and_send(self, job: ImageJob) -> None:
        event = job.event
        status = None
        request_id = None
        fields = ()
        failure_message = "图片未能生成或发送，请勿自动重试。"
        try:
            endpoint = "/images/generations"
            payload = {"model": self.config["model"], "prompt": job.prompt, "n": job.count}
            payload.update(job.options)
            if job.references:
                failure_message = "参考图无法读取或格式不受支持，请重新发送 PNG、JPEG 或 WebP 图片。"
                encoded_references = []
                total_reference_bytes = 0
                for reference in job.references:
                    raw_reference = reference
                    if not isinstance(reference, bytes):
                        raw_reference = await asyncio.wait_for(read_reference(reference), timeout=30)
                    total_reference_bytes += len(raw_reference)
                    if len(raw_reference) > MAX_REFERENCE_BYTES or total_reference_bytes > MAX_TOTAL_REFERENCE_BYTES:
                        raise ReferenceTooLarge
                    mime = Image.MIME[image_format(raw_reference)]
                    encoded = base64.b64encode(raw_reference).decode("ascii")
                    encoded_references.append({"image_url": f"data:{mime};base64,{encoded}"})
                endpoint = "/images/edits"
                payload["images"] = encoded_references
                failure_message = "图片未能生成或发送，请勿自动重试。"
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
                async with session.post(
                    self.config["base_url"].rstrip("/") + endpoint,
                    headers={"Authorization": "Bearer " + self.config["api_key"].strip()},
                    json=payload,
                    allow_redirects=False,
                ) as response:
                    status = response.status
                    request_id = response.headers.get("x-request-id")
                    if status != 200:
                        raise ValueError("Image request failed")
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        body.extend(chunk)
                        if len(body) > 24 * 1024 * 1024:
                            raise ValueError("Image response too large")
                    result = json.loads(body)
                    if not isinstance(result, dict):
                        raise ValueError("Unexpected response")
                    fields = tuple(key for key in ("data", "error", "output") if key in result)
                    images = result.get("data")
                    if not isinstance(images, list) or len(images) != job.count:
                        raise ValueError("Unexpected image count")

            with TemporaryDirectory() as directory:
                paths = []
                generated = []
                for index, item in enumerate(images, start=1):
                    raw = base64.b64decode(item["b64_json"], validate=True)
                    extension = IMAGE_EXTENSIONS[image_format(raw)]
                    generated.append(raw)
                    path = Path(directory) / f"image-{index}.{extension}"
                    path.write_bytes(raw)
                    paths.append(str(path))
                if job.count == 1:
                    await event.send(event.image_result(paths[0]))
                else:
                    await event.send(event.chain_result([MessageImage.fromFileSystem(path) for path in paths]))
                self.history.pop(job.history_key, None)
                self.history[job.history_key] = ImageBatch(
                    tuple(generated), dict(job.options), time.monotonic()
                )
                self._prune_history()
        except Exception as error:
            if isinstance(error, ReferenceTooLarge):
                failure_message = "参考图过大，请压缩后重新发送。"
            logger.warning(
                "Image request failed: status=%s request_id=%r fields=%s error=%s",
                status, request_id, fields, type(error).__name__,
            )
            try:
                await event.send(event.plain_result(failure_message))
            except Exception:
                logger.warning("Image failure notification could not be sent")

    async def terminate(self) -> None:
        self.stopping = True
        self.pending.clear()
        workers = tuple(self.workers)
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self.workers.clear()
        self.history.clear()
