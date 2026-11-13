# Image Tool for AstrBot

Requires AstrBot 4.29.4 or later.

Copy this directory into AstrBot's `data/plugins/`, load the plugin and configure the endpoint:

| Endpoint | Base URL | API key |
| --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` (default) | OpenAI API key |
| OpenAI-compatible proxy | `https://proxy.example.com/v1` | Proxy client API key |

The default model is `gpt-image-2.6-flare`. For a compatible endpoint, choose a model it exposes through `POST /images/generations` and verify it returns `data[0].b64_json`.

Enable `generate_image` in AstrBot's tool settings and use a chat model with tool calling enabled. Ask the bot to draw an image in natural language. The tool acknowledges the request immediately and sends the image to the originating conversation when ready.

- **Edit a reference image.** Attach an image or quote a message containing one, then ask the bot to make a change, such as “Turn this photo into a watercolor.” The tool uses one selected image from the current or quoted message. The endpoint must support JSON `POST /images/edits` with a base64 data URL in `images`. PNG, JPEG and WebP reference images are supported, with a 20 MiB data URL limit.
- **Generate multiple images.** Ask for up to five images from the same prompt, with or without a reference. The default is one. The service must support the requested `n` and return that many images. The 24 MiB response limit applies to the whole batch.

`max_concurrent` sets how many jobs can run at once (default 1). `max_pending` sets how many jobs can wait in the shared queue (default 3). Set it to 0 to reject new jobs when all workers are busy. Waiting jobs start in submission order and can finish out of order when concurrency exceeds 1. Reloading the plugin or restarting AstrBot discards pending requests.

The tool accepts PNG, JPEG and WebP and uses a temporary file for delivery. It does not retry failed requests. The plugin requires AstrBot, aiohttp and Pillow in its runtime.
