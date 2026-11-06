# Image Tool for AstrBot

Copy this directory into AstrBot's `data/plugins/`, load the plugin and configure the endpoint:

| Endpoint | Base URL | API key |
| --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` (default) | OpenAI API key |
| OpenAI-compatible proxy | `https://proxy.example.com/v1` | Proxy client API key |

The default model is `gpt-image-2.6-flare`. For a compatible endpoint, choose a model it exposes through `POST /images/generations` and verify it returns `data[0].b64_json`.

Enable `generate_image` in AstrBot's tool settings and use a chat model with tool calling enabled. Ask the bot to draw an image in natural language. The tool acknowledges the request immediately and sends the image to the originating conversation when ready.

One generation can run at a time. The tool accepts PNG, JPEG and WebP and uses a temporary file for delivery. It does not retry failed requests. The plugin requires AstrBot, aiohttp and Pillow in its runtime.
