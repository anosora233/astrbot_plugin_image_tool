import asyncio
import base64
import io
import json
from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory

import aiohttp
from PIL import Image

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register("astrbot_plugin_image_tool", "Codex", "Generate images through the Images API", "0.2.0")
class ImageTool(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.max_concurrent = max(1, int(config.get("max_concurrent", 1)))
        self.max_pending = max(0, int(config.get("max_pending", 3)))
        self.workers: set[asyncio.Task[None]] = set()
        self.pending: deque[tuple[AstrMessageEvent, str]] = deque()
        self.stopping = False

    @filter.llm_tool(name="generate_image")
    async def generate(self, event: AstrMessageEvent, prompt: str) -> str:
        """Generate one image when the user explicitly asks to draw or create one.

        Args:
            prompt(string): Describe the image to generate.
        """
        prompt = prompt.strip()
        if not prompt:
            return "缺少图片描述。"
        if not self.config["api_key"].strip():
            return "生图服务尚未配置 API Key。"
        if self.stopping:
            return "生图工具正在停止，请勿自动重试。"
        if len(self.workers) >= self.max_concurrent:
            if len(self.pending) >= self.max_pending:
                return "生图队列已满，请勿自动重试。"
            self.pending.append((event, prompt))
            return "生图任务已排队，完成后会自动发送图片。请勿重复提交。"
        self.workers.add(asyncio.create_task(self._process_queue(event, prompt)))
        return "生图任务已提交，完成后会自动发送图片。请勿重复提交。"

    async def _process_queue(self, event: AstrMessageEvent, prompt: str) -> None:
        try:
            await self._generate_and_send(event, prompt)
            while self.pending:
                event, prompt = self.pending.popleft()
                await self._generate_and_send(event, prompt)
        finally:
            worker = asyncio.current_task()
            if worker is not None:
                self.workers.discard(worker)

    async def _generate_and_send(self, event: AstrMessageEvent, prompt: str) -> None:
        status = None
        request_id = None
        fields = ()
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
                async with session.post(
                    self.config["base_url"].rstrip("/") + "/images/generations",
                    headers={"Authorization": "Bearer " + self.config["api_key"].strip()},
                    json={"model": self.config["model"], "prompt": prompt, "n": 1},
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
                    if not isinstance(images, list) or len(images) != 1:
                        raise ValueError("Expected one image")
                    raw = base64.b64decode(images[0]["b64_json"], validate=True)

            with Image.open(io.BytesIO(raw)) as image:
                extension = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}.get(image.format)
                if extension is None:
                    raise ValueError("Unsupported image format")
                image.verify()
            with TemporaryDirectory() as directory:
                path = Path(directory) / f"image.{extension}"
                path.write_bytes(raw)
                await event.send(event.image_result(str(path)))
        except Exception as error:
            logger.warning(
                "Image request failed: status=%s request_id=%r fields=%s error=%s",
                status, request_id, fields, type(error).__name__,
            )
            try:
                await event.send(event.plain_result("图片未能生成或发送，请勿自动重试。"))
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
