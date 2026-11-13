# Image Tool for AstrBot

Requires AstrBot 4.29.4 or later.

Copy this directory into AstrBot's `data/plugins/`, load the plugin and configure the endpoint:

| Endpoint | Base URL | API key |
| --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` (default) | OpenAI API key |
| OpenAI-compatible proxy | `https://proxy.example.com/v1` | Proxy client API key |

The default model is `gpt-image-2.6-flare`. For a compatible endpoint, choose a model it exposes through `POST /images/generations` and verify it returns `data[0].b64_json`.

Enable `generate_image` in AstrBot's tool settings and use a chat model with tool calling enabled. Ask the bot to draw an image in natural language. The tool acknowledges the request immediately and sends the image to the originating conversation when ready.

- **Edit reference images.** Attach images or quote a message containing them, then ask the bot to make a change, such as “Use the character from the first image and the outfit from the second.” Select up to 16 references, including a previous generated image if needed. The endpoint must support JSON `POST /images/edits` with base64 data URLs in `images`. PNG, JPEG and WebP references are supported, with a 20 MiB data URL limit per image and a 24 MiB limit on the combined decoded reference bytes.
- **Generate multiple images.** Ask for up to five images from the same prompt, with or without a reference. The default is one. The service must support the requested `n` and return that many images. The 24 MiB response limit applies to the whole batch.
- **Choose output settings.** Ask for a square, landscape or portrait image, or an exact `WIDTHxHEIGHT` size. Presets are 1024x1024, 1536x1024 and 1024x1536. Custom dimensions must be multiples of 16, at most 3840 per edge, with an aspect ratio between 1:3 and 3:1 and 655,360–8,294,400 total pixels. Quality options are `auto`, `low`, `medium`, `high`, `xhigh` and `max`. Background options are `auto`, `opaque` and `transparent`. Output formats are PNG, JPEG and WebP. Transparent output defaults to PNG and can also use WebP. Available options and sizes depend on the configured model and endpoint.
- **Continue editing.** After receiving images, ask “Make it darker” or “Edit the second image.” The tool can select an image from your most recently delivered batch in the same conversation and inherit its explicitly requested output settings. Only that latest batch is available, for up to one hour. A shared 64 MiB in-memory cache evicts the oldest batches when full. Reloading the plugin or restarting AstrBot clears the cache. If a batch expires or is evicted, resend or quote the image. Queued edits retain the reference selected when submitted.

The tool exposes `size`, `quality`, `background` and `output_format` as optional per-request settings. Omitted settings use service defaults for new requests and inherit explicitly requested settings for edits of a previous result. For message references, use `reference_indices` with `[1]` for one image, `[1, 2]` for several in order, or `[]` for none. Positions start at 1, counting current-message images before quoted-message images. `previous_image_index` selects from the latest delivered batch, with zero meaning no previous image. The model should select references only when the user requests them.

`max_concurrent` sets how many jobs can run at once (default 1). `max_pending` sets how many jobs can wait in the shared queue (default 3). Set it to 0 to reject new jobs when all workers are busy. Waiting jobs start in submission order and can finish out of order when concurrency exceeds 1. Reloading the plugin or restarting AstrBot discards pending requests.

The tool accepts PNG, JPEG and WebP and uses a temporary file for delivery. It does not retry failed requests. The plugin requires AstrBot, aiohttp and Pillow in its runtime.
