import asyncio
import base64
import importlib.util
import io
import json
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image


class Attachment:
    def __init__(self, raw):
        self.raw = raw

    fromFileSystem = staticmethod(Path)


class Reply:
    def __init__(self, chain):
        self.chain = chain


class Event:
    def __init__(self, parts=(), origin="test:chat", sender="user", fail_send=False):
        self.parts = parts
        self.unified_msg_origin = origin
        self.sender = sender
        self.sent = []
        self.paths = []
        self.fail_send = fail_send

    def get_messages(self):
        return self.parts

    def get_sender_id(self):
        return self.sender

    def image_result(self, path):
        return [Path(path)]

    def chain_result(self, paths):
        return paths

    def plain_result(self, text):
        return text

    async def send(self, result):
        if self.fail_send and isinstance(result, list):
            raise OSError("Mock delivery failure")
        if isinstance(result, list):
            self.paths.extend(result)
            result = [path.read_bytes() for path in result]
        self.sent.append(result)


class Star:
    def __init__(self, context):
        pass


def decorator(*args, **kwargs):
    return lambda target: target


def load_plugin():
    modules = {
        "astrbot.api": types.SimpleNamespace(AstrBotConfig=dict, logger=logging.getLogger("test")),
        "astrbot.api.event": types.SimpleNamespace(
            AstrMessageEvent=Event, filter=types.SimpleNamespace(llm_tool=decorator)
        ),
        "astrbot.api.star": types.SimpleNamespace(Context=object, Star=Star, register=decorator),
        "astrbot.api.message_components": types.SimpleNamespace(Image=Attachment, Reply=Reply),
        "astrbot.core.utils.media_utils": types.SimpleNamespace(MediaResolver=object),
    }
    spec = importlib.util.spec_from_file_location(
        "image_tool_under_test", Path(__file__).resolve().parents[1] / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules | {spec.name: module}):
        spec.loader.exec_module(module)
    return module


plugin = load_plugin()


def sample(color, fmt="PNG"):
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(buffer, format=fmt)
    return buffer.getvalue()


class ImageToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.images = [sample("red"), sample("blue", "JPEG"), sample("green", "WEBP")]
        self.requests = []
        self.gate = asyncio.Event()
        owner = self

        class Session:
            def __init__(self, timeout):
                owner.assertEqual(timeout.total, 180)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def post(self, url, *, json, headers, allow_redirects):
                owner.assertFalse(allow_redirects)
                owner.requests.append((url, json))
                return Response(json)

        class Response:
            status = 200
            headers = {}

            def __init__(self, payload):
                self.payload = payload
                self.content = self

            async def __aenter__(self):
                if self.payload["prompt"] == "hold":
                    await owner.gate.wait()
                return self

            async def __aexit__(self, *args):
                pass

            async def iter_chunked(self, size):
                images = [
                    {"b64_json": base64.b64encode(owner.images[index % 3]).decode()}
                    for index in range(self.payload["n"])
                ]
                if self.payload["prompt"] == "bad":
                    images[-1]["b64_json"] = "!!!"
                yield json.dumps({"data": images}).encode()

        async def read_reference(reference):
            return reference.raw

        self.patches = [
            patch.object(plugin.aiohttp, "ClientSession", Session),
            patch.object(plugin, "read_reference", read_reference),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.tool = plugin.ImageTool(
            None, dict(api_key="mock", base_url="https://example.test/v1", model="mock")
        )
        self.addAsyncCleanup(self.tool.terminate)

    async def drain(self):
        await asyncio.gather(*self.tool.workers)

    async def test_legacy_and_output_settings(self):
        event = Event()
        await self.tool.generate(event, "plain")
        await self.drain()
        self.assertEqual(self.requests[0][1], dict(model="mock", prompt="plain", n=1))
        await self.tool.generate(
            event, "styled", 2.0, size="portrait", quality="high",
            background="transparent", output_format="webp",
        )
        await self.drain()
        self.assertEqual(
            self.requests[-1][1],
            dict(model="mock", prompt="styled", n=2, size="1024x1536",
                 quality="high", background="transparent", output_format="webp"),
        )
        self.assertEqual(event.sent[-1], self.images[:2])
        self.assertTrue(all(not path.exists() for path in event.paths))
        await self.tool.generate(event, "transparent", background="transparent")
        await self.drain()
        self.assertEqual(self.requests[-1][1]["output_format"], "png")

    async def test_reference_order_and_legacy_selector(self):
        first, second = self.images[:2]
        event = Event([Reply([Attachment(second)]), Attachment(first)])
        await self.tool.generate(event, "combine", reference_indices=[2.0, 1.0])
        await self.drain()
        url, payload = self.requests[-1]
        self.assertTrue(url.endswith("/images/edits"))
        self.assertEqual(payload["images"], [
            {"image_url": "data:image/jpeg;base64," + base64.b64encode(second).decode()},
            {"image_url": "data:image/png;base64," + base64.b64encode(first).decode()},
        ])
        await self.tool.generate(event, "single list", reference_indices=[1])
        await self.drain()
        single_reference = self.requests[-1][1]["images"]
        await self.tool.generate(event, "legacy", reference_index=1.0)
        await self.drain()
        self.assertEqual(self.requests[-1][1]["images"], single_reference)

    async def test_previous_result_and_inheritance(self):
        event = Event()
        await self.tool.generate(event, "start", 2, size="landscape", quality="high")
        await self.drain()
        await self.tool.generate(event, "hold")
        await self.tool.generate(
            event, "followup", previous_image_index=2, quality="low"
        )
        # Simulate eviction while the edit waits. Its selected reference must survive.
        self.tool.history.clear()
        self.gate.set()
        await self.drain()
        payload = self.requests[-1][1]
        self.assertEqual(payload["images"], [
            {"image_url": "data:image/jpeg;base64," + base64.b64encode(self.images[1]).decode()}
        ])
        self.assertEqual(payload["size"], "1536x1024")
        self.assertEqual(payload["quality"], "low")
        await self.tool.generate(event, "again", previous_image_index=1)
        await self.drain()
        self.assertEqual(self.requests[-1][1]["quality"], "low")
        combined = Event([Attachment(self.images[1])])
        await self.tool.generate(
            combined, "previous plus attachment", previous_image_index=1,
            reference_indices=[1],
        )
        await self.drain()
        self.assertEqual(len(self.requests[-1][1]["images"]), 2)

    async def test_history_isolation_expiry_and_eviction(self):
        event = Event()
        await self.tool.generate(event, "initial")
        await self.drain()
        for other in (Event(sender="another"), Event(origin="test:other")):
            receipt = await self.tool.generate(other, "edit", previous_image_index=1)
            self.assertIn("找不到", receipt)
        with patch.object(plugin.time, "monotonic", return_value=plugin.time.monotonic() + 3601):
            receipt = await self.tool.generate(event, "expired", previous_image_index=1)
            self.assertIn("找不到", receipt)
        await self.tool.generate(event, "new")
        await self.drain()
        with patch.object(plugin, "MAX_HISTORY_BYTES", len(self.images[0])):
            other = Event(sender="another")
            await self.tool.generate(other, "newer")
            await self.drain()
            self.assertNotIn((event.unified_msg_origin, event.sender), self.tool.history)
            self.assertIn((other.unified_msg_origin, other.sender), self.tool.history)

    async def test_invalid_inputs_never_request_images(self):
        event = Event([Attachment(self.images[0])])
        options = [
            dict(size="1000x1000"), dict(size="4096x1024"), dict(size="128x128"),
            dict(size=[]), dict(quality="ultra"), dict(output_format="gif"),
            dict(background="transparent", output_format="jpeg"),
            dict(count=1.5), dict(reference_indices=[True]),
            dict(reference_indices=[1, 1]), dict(reference_indices=[1] * 17),
            dict(reference_indices=[2]), dict(reference_index=1, reference_indices=[1]),
            dict(previous_image_index=float("nan")),
        ]
        for option in options:
            with self.subTest(option=option):
                await self.tool.generate(event, "invalid", **option)
                self.assertFalse(self.tool.workers)
        self.assertEqual(self.requests, [])

    async def test_failed_batch_does_not_replace_history(self):
        event = Event()
        await self.tool.generate(event, "good")
        await self.drain()
        original = dict(self.tool.history)
        await self.tool.generate(event, "bad", 2)
        await self.drain()
        self.assertEqual(dict(self.tool.history), original)
        failed = Event(fail_send=True)
        await self.tool.generate(failed, "send fails")
        await self.drain()
        self.assertEqual(dict(self.tool.history), original)
        self.assertEqual(len(event.paths), 1)

    async def test_reference_budget_and_queue_cleanup(self):
        event = Event([Attachment(self.images[0]), Attachment(self.images[1])])
        with patch.object(plugin, "MAX_TOTAL_REFERENCE_BYTES", len(self.images[0])):
            await self.tool.generate(event, "too large", reference_indices=[1, 2])
            await self.drain()
        self.assertEqual(self.requests, [])
        self.tool.max_pending = 1
        await self.tool.generate(event, "hold")
        await self.tool.generate(event, "waiting")
        receipt = await self.tool.generate(event, "full")
        self.assertIn("已满", receipt)
        await asyncio.sleep(0)
        self.gate.set()
        await self.drain()
        self.assertEqual([payload["prompt"] for _, payload in self.requests], ["hold", "waiting"])
        self.gate.clear()
        await self.tool.generate(event, "hold")
        await self.tool.generate(event, "discard")
        await asyncio.sleep(0)
        await self.tool.terminate()
        self.assertFalse(self.tool.pending)
        self.assertFalse(self.tool.workers)
        self.assertFalse(self.tool.history)

    async def test_zero_waiting_limit_and_concurrent_completion(self):
        self.tool.max_concurrent = 2
        self.tool.max_pending = 0
        first = Event(sender="first")
        second = Event(sender="second")
        await self.tool.generate(first, "hold")
        await self.tool.generate(second, "fast")
        receipt = await self.tool.generate(first, "rejected")
        self.assertIn("已满", receipt)
        await asyncio.sleep(0)
        self.assertTrue(second.sent)
        self.assertFalse(first.sent)
        self.gate.set()
        await self.drain()
        self.assertEqual([payload["prompt"] for _, payload in self.requests], ["hold", "fast"])
        self.assertTrue(first.sent)


if __name__ == "__main__":
    unittest.main()
