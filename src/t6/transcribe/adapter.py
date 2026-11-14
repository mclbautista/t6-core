"""ElevenLabs Scribe adapter. Install with ``pip install 't6[transcribe]'``."""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:
    import httpx
    from elevenlabs.core.api_error import ApiError
except ImportError as error:
    raise ImportError("Install 't6[transcribe]' to use the transcription adapter") from error


def transcribe(
    wav: str | Path,
    client: Any,
    *,
    language: str = "fil",
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Transcribe one WAV using a caller-configured ElevenLabs client.

    The caller constructs the client with its API key. SDK retries are disabled
    so this adapter controls the total number of attempts.
    """
    for attempt in range(3):
        try:
            with open(wav, "rb") as audio:
                response = client.speech_to_text.convert(
                    file=audio,
                    model_id="scribe_v3",
                    language_code=language,
                    secondary_language_codes=["eng"],
                    diarize=True,
                    tag_audio_events=False,
                    timestamps_granularity="word",
                    request_options={"max_retries": 0},
                )
            result: dict[str, Any] = response.model_dump(mode="json")
            return result
        except TypeError as error:
            if "unexpected keyword argument" in str(error):
                raise RuntimeError(
                    "ElevenLabs SDK does not support the required parameters; "
                    "upgrade 't6[transcribe]' (elevenlabs >= 2.78.0)"
                ) from error
            raise
        except (ApiError, httpx.TransportError) as error:
            if isinstance(error, ApiError) and not (
                error.status_code == 429
                or (error.status_code is not None and 500 <= error.status_code < 600)
            ):
                raise
            if attempt == 2:
                raise
            sleep(10 * (attempt + 1))
    raise AssertionError("unreachable")
