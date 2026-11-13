import asyncio
import base64
import io
import json
from collections import deque
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


class ReferenceTooLarge(ValueError):
    pass


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
    reference: MessageImage | None


@register("astrbot_plugin_image_tool", "Codex", "Generate images through the Images API", "0.3.0")
class ImageTool(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.max_concurrent = max(1, int(config.get("max_concurrent", 1)))
        self.max_pending = max(0, int(config.get("max_pending", 3)))
        self.workers: set[asyncio.Task[None]] = set()
        self.pending: deque[ImageJob] = deque()
        self.stopping = False

    @filter.llm_tool(name="generate_image")
    async def generate(
        self, event: AstrMessageEvent, prompt: str, count: int | float = 1,
        reference_index: int | float = 0,
    ) -> str:
        """Generate or edit images when the user explicitly asks.

        Args:
            prompt(string): Describe the image to generate.
            count(number): Number of output images, an integer from 1 to 5. Use 1 unless the user asks for more.
            reference_index(number): Use 0 for text-only generation. For an explicit request to use a reference, select one image by its 1-based position, counting current message images first and then quoted message images.
        """
        prompt = prompt.strip()
        if not prompt:
            return "缺少图片描述。"
        if type(count) not in (int, float) or not 1 <= count <= 5 or count % 1:
            return "生成张数须为 1 到 5 的整数。"
        if type(reference_index) not in (int, float) or reference_index < 0 or reference_index % 1:
            return "参考图编号须为非负整数。"
        if not self.config["api_key"].strip():
            return "生图服务尚未配置 API Key。"
        if self.stopping:
            return "生图工具正在停止，请勿自动重试。"
        reference = None
        if reference_index:
            parts = event.get_messages()
            references = [part for part in parts if isinstance(part, MessageImage)]
            for part in parts:
                if isinstance(part, Reply):
                    references.extend(item for item in part.chain or [] if isinstance(item, MessageImage))
            if reference_index > len(references):
                return "找不到指定的参考图，请让用户重新发送图片或引用含图消息。"
            reference = references[int(reference_index) - 1]
        job = ImageJob(event, prompt, int(count), reference)
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
            if job.reference is not None:
                failure_message = "参考图无法读取或格式不受支持，请重新发送 PNG、JPEG 或 WebP 图片。"
                raw_reference = await asyncio.wait_for(read_reference(job.reference), timeout=30)
                mime = Image.MIME[image_format(raw_reference)]
                encoded = base64.b64encode(raw_reference).decode("ascii")
                endpoint = "/images/edits"
                payload["images"] = [{"image_url": f"data:{mime};base64,{encoded}"}]
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
                for index, item in enumerate(images, start=1):
                    raw = base64.b64decode(item["b64_json"], validate=True)
                    extension = IMAGE_EXTENSIONS[image_format(raw)]
                    path = Path(directory) / f"image-{index}.{extension}"
                    path.write_bytes(raw)
                    paths.append(str(path))
                if job.count == 1:
                    await event.send(event.image_result(paths[0]))
                else:
                    await event.send(event.chain_result([MessageImage.fromFileSystem(path) for path in paths]))
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
