# ElevenLabs Scribe transcript fact check

Checked 2026-11-14 against public ElevenLabs documentation and the official Python SDK; no transcription requests were made. The API and prices may change.

## Response shape

For recorded audio, use `model_id="scribe_v3"` with the [create transcript endpoint](https://elevenlabs.io/docs/api-reference/speech-to-text/convert). The single-channel response contains `language_code`, `language_probability`, `text`, and `words`. Each `words` entry has `text`, `start` and `end` in seconds (timestamps can be null), `type`, `speaker_id` (nullable), and `logprob`: a log probability from negative infinity to zero, with values nearer zero indicating greater confidence. Optional character and channel details can also appear. See the [SDK word model](https://github.com/elevenlabs/elevenlabs-python/blob/v2.78.0/src/elevenlabs/types/speech_to_text_word_response_model.py).

Types are `word`, `spacing`, and `audio_event`; spacing is an explicit entry where applicable and audio events represent non-speech sounds. The [documented example](https://elevenlabs.io/docs/api-reference/speech-to-text/get) attaches punctuation to word text (`world!`) and represents a space as a separate `spacing` entry; it does not establish a universal punctuation-token rule. With diarization, ordinary IDs look like `speaker_0`, `speaker_1`, etc.; they are labels, not names. The optional speaker-role feature can instead return `agent` and `customer`. See the [overview](https://elevenlabs.io/docs/overview/capabilities/speech-to-text) and [API reference](https://elevenlabs.io/docs/api-reference/speech-to-text/convert).

Illustrative synthetic response, based on that documented shape (not an actual API result):

```json
{
  "language_code": "en",
  "language_probability": 0.98,
  "text": "Hello, there! Hi!",
  "words": [
    {"text": "Hello,", "start": 0.0, "end": 0.4, "type": "word", "speaker_id": "speaker_0", "logprob": -0.12},
    {"text": " ", "start": 0.4, "end": 0.4, "type": "spacing", "speaker_id": "speaker_0", "logprob": 0.0},
    {"text": "there!", "start": 0.4, "end": 0.8, "type": "word", "speaker_id": "speaker_0", "logprob": -0.08},
    {"text": " ", "start": 0.8, "end": 0.8, "type": "spacing", "speaker_id": "speaker_0", "logprob": 0.0},
    {"text": "Hi!", "start": 1.0, "end": 1.3, "type": "word", "speaker_id": "speaker_1", "logprob": -0.10}
  ]
}
```

## Parameters and Python SDK

The [API reference](https://elevenlabs.io/docs/api-reference/speech-to-text/convert) defines these parameters. Version numbers below are the first stable tagged SDK versions whose generated `speech_to_text.convert` signature contains each parameter, determined by inspecting the official SDK's release tags; they are not claims about when the server first supported the feature.

| Parameter | Meaning | First typed SDK version |
| --- | --- | --- |
| `language_code` | Optional ISO 639-1 or 639-3 hint; null means automatic detection. Filipino is `fil`. | [1.50.7](https://github.com/elevenlabs/elevenlabs-python/blob/1.50.7/src/elevenlabs/speech_to_text/client.py) |
| `secondary_language_codes` | List of additional language hints, e.g. `["eng"]`; supported by `scribe_v3`. | [2.78.0](https://github.com/elevenlabs/elevenlabs-python/blob/v2.78.0/src/elevenlabs/speech_to_text/client.py) |
| `diarize` | Annotate speakers; default false. | [1.52.0](https://github.com/elevenlabs/elevenlabs-python/blob/v1.52.0/src/elevenlabs/speech_to_text/client.py) |
| `tag_audio_events` | Include non-speech tags such as laughter; default true. | [1.50.7](https://github.com/elevenlabs/elevenlabs-python/blob/1.50.7/src/elevenlabs/speech_to_text/client.py) |
| `additional_formats` | Request additional transcript export formats, not a replacement for the normal result. | [1.56.0](https://github.com/elevenlabs/elevenlabs-python/blob/v1.56.0/src/elevenlabs/speech_to_text/client.py) |

The [language list](https://elevenlabs.io/docs/overview/capabilities/speech-to-text) identifies Filipino as `fil`; the [models page](https://elevenlabs.io/docs/overview/models/) confirms secondary languages are a Scribe v3 batch feature. SDK version support and model availability are separate: an old signature accepting a parameter does not imply it supported `scribe_v3` at the time.

The [batch language guide](https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/batch/number-and-language-options) still says the SDK has no typed argument and shows a `request_options.additional_body_parameters` workaround. That guidance conflicts with the 2.78.0 SDK source linked above; check the installed version before choosing an approach.

## Size, duration, and asynchronous results

The current [transcription overview](https://elevenlabs.io/docs/overview/capabilities/speech-to-text) states **5 GB and 15 hours** per standard request, and 15 hours combined across channels in multichannel mode. WAV is accepted. However, ElevenLabs' [integration guide](https://elevenlabs.io/insights/speech-to-text-api-integration) says **3 GB and 10 hours** in standard mode and one hour for multichannel; its [product pricing page](https://elevenlabs.io/speech-to-text) also lists 3 GB. These official pages conflict, so treat the applicable limit as unconfirmed and do not assume the larger limit without verifying it for the intended account and endpoint. Files exceeding either applicable limit need splitting or conversion; size and duration both matter.

Multi-hour WAVs do not categorically require webhooks: batch has a synchronous response, and `webhook=true` is an optional asynchronous delivery mode. For long jobs it avoids keeping the request open. The [API reference](https://elevenlabs.io/docs/api-reference/speech-to-text/convert) says the initial webhook request returns early without the transcript; the [webhook guide](https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/batch/webhooks) describes a later HTTPS POST containing `data.transcription` and request metadata, or a `webhook_error`. Verify the webhook signature. If delivery fails, the guide says to retrieve the result using the returned `transcription_id`, except in zero-retention mode, where it is not stored.

## Concurrency, errors, and price

The [STT concurrency table](https://elevenlabs.io/docs/help-center/product/core-capabilities/speech-to-text/how-many-speech-to-text-requests-can-i-make-and-can-i-increase-it) lists Free 8, Starter 12, Creator 20, Pro 40, Scale and Business 60, and elevated Enterprise limits. This is concurrency, not a published fixed requests-per-minute quota. The [overview](https://elevenlabs.io/docs/overview/capabilities/speech-to-text) says batch files over eight minutes consume multiple slots, up to four per file.

The [error reference](https://elevenlabs.io/docs/eleven-api/resources/errors) says 429 can mean rate or concurrency limit: use exponential backoff for the former and wait for current requests to finish for the latter. It identifies 500 as server error and service unavailability as temporary. ElevenLabs' [retry guidance](https://elevenlabs.io/blog/text-to-speech-api-integration) recommends backoff for 5xx and 429; it is written for TTS, so applying that general policy to STT is an inference. Ordinary invalid/authentication/payment 4xx need correction rather than blind retries. The error reference also lists a 409 `concurrent_modification` where retrying with the latest version is appropriate, so “never retry any other 4xx” is not a universal ElevenLabs rule. Webhook delivery retries are a separate policy, including 408 when enabled.

The [API pricing page](https://elevenlabs.io/pricing/api) lists Scribe v3 at **$0.20 per audio hour**. At that base rate 36 hours would be $7.20, before taxes, add-ons, minimums, or account-specific billing; the reported roughly $11 cannot be reconciled from duration alone. The [API reference](https://elevenlabs.io/docs/api-reference/speech-to-text/convert) notes, for example, extra charges for keyterms and independently billed multichannel audio.

## Differences or qualifications for the old script

1. The script's `secondary_language_codes=["eng"]` and Scribe v3-only claim are supported by the current batch API reference. Direct typed SDK support starts at 2.78.0; older SDKs cannot be assumed to accept that keyword.
2. Retrying 429 and 5xx is a reasonable general policy, but 429 needs different handling for rate versus concurrency limits; the blanket exclusion of all other 4xx is not universally valid (409 concurrent modification is a documented exception).
3. The observed $11 for 36 hours is above the currently published $7.20 base calculation; do not treat it as the current base hourly rate or infer a cause without billing details.
4. Current official size and duration figures conflict across pages. Neither a single presumed cap nor a requirement to use webhooks for all multi-hour WAVs is established.
