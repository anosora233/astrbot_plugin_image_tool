import asyncio
import base64
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import aiohttp
from PIL import Image

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register("astrbot_plugin_image_tool", "Codex", "Generate images through the Images API", "0.1.0")
class ImageTool(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.task: asyncio.Task | None = None

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
        if self.task is not None and not self.task.done():
            return "已有生图任务正在处理，请勿自动重试。"
        self.task = asyncio.create_task(self._generate_and_send(event, prompt))
        return "生图任务已提交，完成后会自动发送图片。请勿重复提交。"

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
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
