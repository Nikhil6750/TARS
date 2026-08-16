from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

pytestmark = pytest.mark.acceptance

TRANSCRIPT = "What setups require my attention?"
ASSISTANT_RESPONSE = "Nothing currently requires your attention."
AUDIO_SENTINEL = b"TARS-CERT-AUDIO-BYTES"
SILENT_WAV = (
    b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
    b"\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
)


def assistant_message() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "message_id": str(uuid4()),
        "conversation_id": str(uuid4()),
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "role": "assistant",
        "content": ASSISTANT_RESPONSE,
        "input_mode": "text",
        "audio_ref": None,
        "related_event_id": None,
        "intent": "attention_summary",
        "providers": {"stt": None, "assistant": "deterministic", "tts": None},
        "error": None,
    }


def test_push_to_talk_uses_recorded_audio_stt_text_and_backend_tts() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    captured: dict[str, list[Any]] = {"stt": [], "assistant": [], "tts": []}

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(
            headless=True,
            args=[
                "--use-fake-device-for-media-stream",
                "--use-fake-ui-for-media-stream",
            ],
        )
        context = browser.new_context()
        context.add_init_script(
            """
            window.__speechSynthesisCalls = 0;
            window.__backendAudioPlayCalls = 0;
            const originalSpeechSynthesis = window.speechSynthesis;
            if (originalSpeechSynthesis) {
              originalSpeechSynthesis.speak = () => { window.__speechSynthesisCalls += 1; };
            }
            HTMLMediaElement.prototype.play = function () {
              window.__backendAudioPlayCalls += 1;
              return Promise.resolve();
            };
            class CertificationMediaRecorder {
              constructor(stream) {
                this.stream = stream;
                this.state = 'inactive';
                this.mimeType = 'audio/wav';
                this.ondataavailable = null;
                this.onstop = null;
              }
              start() { this.state = 'recording'; }
              stop() {
                this.state = 'inactive';
                const bytes = new TextEncoder().encode('TARS-CERT-AUDIO-BYTES');
                const data = new Blob([bytes], { type: 'audio/wav' });
                this.ondataavailable?.({ data });
                this.onstop?.();
              }
            }
            window.MediaRecorder = CertificationMediaRecorder;
            """
        )

        def route_api(route: Any, request: Any) -> None:
            path = request.url.split("?", 1)[0]
            if path.endswith("/api/v1/voice/transcribe"):
                captured["stt"].append(
                    {
                        "body": request.post_data_buffer,
                        "content_type": request.headers.get("content-type", ""),
                    }
                )
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"text": TRANSCRIPT, "language": "en"}),
                )
            elif path.endswith("/api/v1/assistant/query"):
                captured["assistant"].append(json.loads(request.post_data or "{}"))
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(assistant_message()),
                )
            elif path.endswith("/api/v1/voice/synthesize"):
                captured["tts"].append(json.loads(request.post_data or "{}"))
                route.fulfill(status=200, content_type="audio/wav", body=SILENT_WAV)
            else:
                route.continue_()

        context.route("**/api/v1/**", route_api)
        page = context.new_page()
        try:
            page.goto(
                os.environ["TARS_FRONTEND_URL"],
                wait_until="domcontentloaded",
                timeout=10_000,
            )
            ptt = page.get_by_title("Push to Talk").first
            ptt.click()
            page.get_by_title("Release to Send").first.click(timeout=5_000)

            page.wait_for_function("window.__backendAudioPlayCalls > 0", timeout=8_000)

            assert len(captured["stt"]) == 1
            assert captured["stt"][0]["body"]
            assert AUDIO_SENTINEL in captured["stt"][0]["body"]
            assert "multipart/form-data" in captured["stt"][0]["content_type"]

            assert captured["assistant"] == [
                {"text": TRANSCRIPT, "conversation_id": "conv_voice_session"}
            ]
            assert captured["assistant"][0]["text"] != "Show active setups"
            assert captured["tts"] == [{"text": ASSISTANT_RESPONSE}]
            assert page.evaluate("window.__speechSynthesisCalls") == 0
        finally:
            context.close()
            browser.close()
