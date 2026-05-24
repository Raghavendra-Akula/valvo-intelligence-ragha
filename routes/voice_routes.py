"""
Voice Routes — Valvo Intelligence
- /api/voice/stt — audio → text (Gemini Flash with trading context, Sarvam fallback)
- /api/voice/tts — text → audio (Gemini 2.5 Flash TTS, voice: Kore)
- /api/voice/command — LEGACY: full audio loop (not called by frontend)
"""

from flask import Blueprint, request, jsonify, Response, stream_with_context
import requests
import base64
import os
import io
from extensions import limiter

voice_bp = Blueprint("voice_bp", __name__)

SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY")
if not SARVAM_API_KEY:
    import logging
    logging.getLogger(__name__).warning("SARVAM_API_KEY env var not set — voice features will fail")
SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"
SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"

# ── Gemini TTS config ──
GEMINI_API_KEY = os.environ.get("api_key", "").strip()  # Same key used by Valvo AI gateway
GEMINI_TTS_MODEL = "gemini-2.5-flash-preview-tts"
GEMINI_TTS_VOICE = "Kore"  # Clear, professional voice
GEMINI_TTS_STYLE = "Read clearly and professionally:"

# ── Gemini STT config ──
GEMINI_STT_MODEL = "gemini-2.5-flash"

# Trading context prompt for accurate transcription
_STT_CONTEXT_PROMPT = """\
Transcribe this audio exactly. The speaker is an Indian equity trader using a trading platform.

CONTEXT for accurate transcription:
- Stock symbols: RELIANCE, TCS, INFY, HDFCBANK, TATAMOTORS, BAJFINANCE, SBIN, ITC, etc.
- Trading terms: stop loss, trailing stop, breakout, 52-week high, R-multiple, position sizing, entry price, exit price, P&L, profit and loss, win rate, profit factor
- Indian financial terms: Nifty, Sensex, FY (financial year), lakh, crore, STCG, LTCG, NSE, BSE
- FY references: "FY 24-25", "FY 2024-25", "last year", "this year", "all years"
- Page names: screener, scanner, journal, positions, watchlist, dashboard, analytics
- Actions: create position, close position, record sell, update stop loss

Rules:
1. Output ONLY the transcribed text, nothing else.
2. Fix obvious mishearings using the context above (e.g., "reliance" → "RELIANCE", "nifty fifty" → "Nifty 50").
3. Keep numbers as spoken ("fifty two week" → "52 week", "four percent" → "4%").
4. Preserve the user's intent — don't rephrase.
"""


@voice_bp.route("/api/voice/stt", methods=["POST"])
@limiter.limit("20 per minute")
def speech_to_text():
    """Convert audio to text using Gemini Flash (with trading context) or Sarvam fallback."""
    try:
        # Accept audio file upload
        if "audio" not in request.files:
            return jsonify({"error": "No audio file provided"}), 400

        audio_file = request.files["audio"]
        audio_bytes = audio_file.read()

        if len(audio_bytes) < 100:
            return jsonify({"error": "Audio too short"}), 400

        content_type = audio_file.content_type or "audio/webm"

        # Try Gemini STT first (better accent handling + trading context)
        if GEMINI_API_KEY:
            try:
                transcript = _gemini_stt(audio_bytes, content_type)
                if transcript:
                    return jsonify({
                        "transcript": transcript,
                        "text": transcript,
                        "provider": "gemini",
                        "language": "en-IN",
                    })
            except Exception as e:
                print(f"[voice] Gemini STT failed, falling back to Sarvam: {e}")

        # Fallback to Sarvam STT
        if SARVAM_API_KEY:
            try:
                transcript = _sarvam_stt(audio_bytes, content_type)
                if transcript:
                    return jsonify({
                        "transcript": transcript,
                        "text": transcript,
                        "provider": "sarvam",
                        "language": "en-IN",
                    })
            except Exception as e:
                print(f"[voice] Sarvam STT also failed: {e}")

        return jsonify({"error": "Could not transcribe audio"}), 400

    except Exception as e:
        print(f"[voice] STT error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Internal error"}), 500


def _gemini_stt(audio_bytes: bytes, content_type: str) -> str | None:
    """Transcribe audio using Gemini Flash with trading domain context."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY, http_options={"timeout": 30})

    # Gemini accepts inline audio data
    audio_part = types.Part(
        inline_data=types.Blob(
            mime_type=content_type,
            data=audio_bytes,
        )
    )

    response = client.models.generate_content(
        model=GEMINI_STT_MODEL,
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(text=_STT_CONTEXT_PROMPT),
                    audio_part,
                ],
            )
        ],
        config=types.GenerateContentConfig(
            max_output_tokens=256,
            temperature=0.1,
        ),
    )

    if response.candidates:
        for part in response.candidates[0].content.parts:
            if hasattr(part, "text") and part.text:
                transcript = part.text.strip().strip('"').strip("'").strip()
                if transcript:
                    print(f"[voice] Gemini STT: '{transcript}'")
                    return transcript
    return None


def _sarvam_stt(audio_bytes: bytes, content_type: str) -> str | None:
    """Transcribe audio using Sarvam Saaras v3 (fallback)."""
    headers = {"api-subscription-key": SARVAM_API_KEY}
    files = {"file": ("audio.webm", io.BytesIO(audio_bytes), content_type)}
    data = {
        "language_code": "en-IN",
        "model": "saaras:v3",
        "with_timestamps": "false",
    }

    resp = requests.post(SARVAM_STT_URL, headers=headers, files=files, data=data, timeout=15)
    if resp.status_code == 200:
        transcript = resp.json().get("transcript", "").strip()
        if transcript:
            print(f"[voice] Sarvam STT: '{transcript}'")
            return transcript
    return None


def _query_valvo_ai(message: str, page_context: str = "", *, load_history: bool = True, persist_history: bool = True):
    """Call V7 engine with voice=True for short responses."""
    from services.valvo_ai_v7.engine import ValvoAIV7Engine
    engine = ValvoAIV7Engine()
    return engine.query(
        message=message,
        page_context=page_context or "voice",
        voice=True,
        load_history=load_history,
        persist_history=persist_history,
    )


@voice_bp.route("/api/voice/test", methods=["GET"])
def voice_test():
    """Quick test — confirms voice routes are loaded."""
    return jsonify({
        "status": "voice_routes_loaded",
        "tts_provider": "gemini",
        "tts_model": GEMINI_TTS_MODEL,
        "tts_voice": GEMINI_TTS_VOICE,
        "gemini_key_set": bool(GEMINI_API_KEY),
        "stt_provider": "sarvam (iOS fallback)",
        "sarvam_key_set": bool(SARVAM_API_KEY),
    })


def _pcm_to_wav_base64(pcm_bytes: bytes, sample_rate: int = 24000, channels: int = 1, sample_width: int = 2) -> str:
    """Convert raw PCM bytes to a WAV file and return as base64 string."""
    import wave

    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    wav_buffer.seek(0)
    return base64.b64encode(wav_buffer.read()).decode("utf-8")


@voice_bp.route("/api/voice/tts", methods=["POST"])
@limiter.limit("20 per minute")
def text_to_speech():
    """Convert text to audio using Gemini 2.5 Flash TTS."""
    debug = {}
    try:
        data = request.json or {}
        text = data.get("text", "").strip()
        debug["step"] = "parse_input"
        debug["text_len"] = len(text)

        if not text:
            return jsonify({"error": "No text provided"}), 400

        if len(text) > 400:
            text = text[:400]

        if not GEMINI_API_KEY:
            debug["step"] = "no_api_key"
            return jsonify({"error": "Gemini API key not configured (env: api_key)"}), 500

        # Language selection — Gemini supports en-IN, hi-IN, etc.
        language = data.get("language", "en").lower()
        lang_code = "hi-IN" if language == "hi" else "en-IN"

        # Voice override (optional — defaults to Kore)
        voice = data.get("speaker", GEMINI_TTS_VOICE)

        # Build the prompt: style instruction + actual text
        prompt = f"{GEMINI_TTS_STYLE}\n\n{text}"

        debug["step"] = "calling_gemini"
        print(f"🎤 TTS calling Gemini ({GEMINI_TTS_MODEL}, voice={voice}): {len(text)} chars")

        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=GEMINI_API_KEY)
            response = client.models.generate_content(
                model=GEMINI_TTS_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        language_code=lang_code,
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=voice,
                            )
                        ),
                    ),
                ),
            )
        except Exception as ge:
            debug["step"] = "gemini_error"
            debug["error_type"] = type(ge).__name__
            print(f"❌ Gemini TTS error: {type(ge).__name__}: {ge}")
            return jsonify({"error": f"Gemini TTS failed: {str(ge)[:300]}", "debug": debug}), 502

        # Extract audio from response
        debug["step"] = "parse_response"
        try:
            part = response.candidates[0].content.parts[0]
            if not hasattr(part, "inline_data") or not part.inline_data:
                debug["step"] = "no_inline_data"
                return jsonify({"error": "Gemini returned no audio data", "debug": debug}), 500

            # Gemini returns base64-encoded raw PCM (16-bit, 24kHz, mono)
            pcm_b64 = part.inline_data.data
            if isinstance(pcm_b64, str):
                pcm_bytes = base64.b64decode(pcm_b64)
            elif isinstance(pcm_b64, bytes):
                pcm_bytes = pcm_b64
            else:
                pcm_bytes = bytes(pcm_b64)

        except (IndexError, AttributeError) as pe:
            debug["step"] = "parse_fail"
            print(f"❌ Gemini TTS parse error: {pe}")
            return jsonify({"error": f"Failed to extract audio: {pe}", "debug": debug}), 500

        # Convert raw PCM → WAV → base64 (frontend expects WAV)
        debug["step"] = "encode_wav"
        wav_b64 = _pcm_to_wav_base64(pcm_bytes, sample_rate=24000, channels=1, sample_width=2)

        debug["step"] = "success"
        debug["pcm_bytes"] = len(pcm_bytes)
        debug["wav_b64_len"] = len(wav_b64)
        print(f"✅ TTS success: {len(pcm_bytes)} PCM bytes → {len(wav_b64)} base64 WAV chars")

        return jsonify({
            "audio": wav_b64,
            "format": "wav",
        })

    except Exception as e:
        print(f"❌ TTS outer error: {e}")
        import traceback
        traceback.print_exc()
        debug["outer_error"] = str(e)
        print(f"[voice_bp] error: {e}")
        return jsonify({"error": "Internal error", "debug": debug}), 500


@voice_bp.route("/api/voice/tts/stream", methods=["POST"])
@limiter.limit("20 per minute")
def text_to_speech_stream():
    """Stream TTS audio chunks via SSE for instant playback."""
    import json as _json

    data = request.json or {}
    text = data.get("text", "").strip()

    if not text:
        return jsonify({"error": "No text provided"}), 400
    if len(text) > 800:
        text = text[:800]
    if not GEMINI_API_KEY:
        return jsonify({"error": "Gemini API key not configured"}), 500

    language = data.get("language", "en").lower()
    lang_code = "hi-IN" if language == "hi" else "en-IN"
    voice = data.get("speaker", GEMINI_TTS_VOICE)
    prompt = f"{GEMINI_TTS_STYLE}\n\n{text}"

    def generate():
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=GEMINI_API_KEY)
            chunk_idx = 0

            for chunk in client.models.generate_content_stream(
                model=GEMINI_TTS_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        language_code=lang_code,
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=voice,
                            )
                        ),
                    ),
                ),
            ):
                if not chunk.candidates:
                    continue
                for part in chunk.candidates[0].content.parts:
                    if not hasattr(part, "inline_data") or not part.inline_data:
                        continue
                    pcm_data = part.inline_data.data
                    if isinstance(pcm_data, bytes):
                        b64 = base64.b64encode(pcm_data).decode("utf-8")
                    elif isinstance(pcm_data, str):
                        b64 = pcm_data  # already base64
                    else:
                        b64 = base64.b64encode(bytes(pcm_data)).decode("utf-8")

                    yield f"data: {_json.dumps({'audio': b64, 'idx': chunk_idx})}\n\n"
                    chunk_idx += 1

            yield f"data: {_json.dumps({'done': True, 'chunks': chunk_idx})}\n\n"

        except Exception as e:
            print(f"❌ TTS stream error: {e}")
            import traceback
            traceback.print_exc()
            yield f"data: {_json.dumps({'error': str(e)[:300]})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@voice_bp.route("/api/voice/command", methods=["POST"])
@limiter.limit("10 per minute")
def voice_command():
    """Full voice loop: audio → STT → Valvo AI → TTS → audio response."""
    try:
        if "audio" not in request.files:
            return jsonify({"error": "No audio file provided"}), 400

        audio_file = request.files["audio"]
        audio_bytes = audio_file.read()
        page_context = request.form.get("page_context", "")

        # Step 1: STT
        stt_headers = {"api-subscription-key": SARVAM_API_KEY}
        stt_files = {"file": ("audio.webm", io.BytesIO(audio_bytes), audio_file.content_type or "audio/webm")}
        stt_data = {"language_code": "en-IN", "model": "saaras:v3", "with_timestamps": "false"}

        stt_resp = requests.post(SARVAM_STT_URL, headers=stt_headers, files=stt_files, data=stt_data, timeout=15)
        if stt_resp.status_code != 200:
            return jsonify({"error": f"STT failed: {stt_resp.text}"}), 500

        transcript = stt_resp.json().get("transcript", "")
        if not transcript:
            return jsonify({"error": "Could not understand audio", "step": "stt"}), 400

        # Step 2: Call Valvo AI v3 (Gemini Flash Lite default)
        try:
            chat_data = _query_valvo_ai(transcript, page_context=page_context)
        except Exception as chat_err:
            print(f"⚠️ Internal Valvo AI call failed: {chat_err}")
            chat_data = {"response": f"Heard: '{transcript}'. Processing failed."}

        ai_text = chat_data.get("response", "Sorry, I couldn't process that.")

        # For TTS: cap at 300 chars, strip markdown
        tts_text = ai_text[:300].replace("**", "").replace("*", "").replace("#", "").replace("`", "").strip()

        # Step 3: TTS (skip if response is very short action confirmation)
        tts_headers = {"api-subscription-key": SARVAM_API_KEY, "Content-Type": "application/json"}
        tts_payload = {
            "inputs": [tts_text],
            "target_language_code": "en-IN",
            "speaker": "meera",
            "pitch": 0,
            "pace": 1.15,
            "loudness": 1.5,
            "speech_sample_rate": 22050,
            "enable_preprocessing": True,
            "model": "bulbul:v2",
        }

        tts_resp = requests.post(SARVAM_TTS_URL, headers=tts_headers, json=tts_payload, timeout=30)
        audio_b64 = ""
        print(f"🔊 TTS response: status={tts_resp.status_code}")
        if tts_resp.status_code == 200:
            tts_data = tts_resp.json()
            audios = tts_data.get("audios", [])
            if audios:
                audio_b64 = audios[0]

        return jsonify({
            "transcript": transcript,
            "response": ai_text,
            "audio": audio_b64,
            "model": chat_data.get("model", ""),
            "tool_calls": chat_data.get("tool_calls"),
        })

    except Exception as e:
        print(f"❌ Voice command error: {e}")
        import traceback
        traceback.print_exc()
        print(f"[voice_bp] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@voice_bp.route("/api/voice/morning-briefing", methods=["GET"])
@limiter.limit("10 per minute")
def morning_briefing():
    """Auto-generate morning briefing — short text summary for dashboard banner."""
    try:
        # Call Valvo AI v3 with explicit short instruction
        try:
            chat_data = _query_valvo_ai(
                "Morning briefing in 2-3 short lines only. Format: positions count, total P&L, any alerts. No tables, no markdown, no headers.",
                page_context="dashboard",
                load_history=False,
                persist_history=False,
            )
        except Exception as e:
            return jsonify({"error": f"Briefing failed: {e}"}), 500

        briefing_text = chat_data.get("response", "")

        # Aggressive markdown cleanup — strip everything
        import re
        briefing_text = re.sub(r'\*\*([^*]+)\*\*', r'\1', briefing_text)  # Bold
        briefing_text = re.sub(r'\*([^*]+)\*', r'\1', briefing_text)       # Italic
        briefing_text = re.sub(r'#{1,3}\s*', '', briefing_text)             # Headers
        briefing_text = re.sub(r'---+', '', briefing_text)                  # HR
        briefing_text = re.sub(r'\|[^\n]*\|', '', briefing_text)            # Table rows
        briefing_text = re.sub(r'^[-•]\s*', '', briefing_text, flags=re.MULTILINE)  # Bullets
        briefing_text = re.sub(r'[📊🎯⚠❌✅🔴🟢🟡💰📈📉]\s*', '', briefing_text)  # Emojis
        briefing_text = re.sub(r'\n{2,}', '\n', briefing_text)              # Extra newlines
        briefing_text = briefing_text.strip()
        # Truncate to ~400 chars
        if len(briefing_text) > 400:
            briefing_text = briefing_text[:400].rsplit('.', 1)[0] + '.'

        return jsonify({
            "briefing": briefing_text,
            "category": chat_data.get("category", ""),
        })

    except Exception as e:
        print(f"❌ Morning briefing error: {e}")
        return jsonify({"error": "Internal error"}), 500
