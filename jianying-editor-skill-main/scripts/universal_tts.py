import asyncio
import json
import os
import re
import ssl
import subprocess
from typing import Optional, Tuple

import websockets
from utils.config import CONFIG


def get_jy_local_config() -> Tuple[str, str]:
    import sys as _sys

    defaults = ("1053764930506284", "2314914062247833")

    if _sys.platform == "darwin":
        # ---- macOS ----
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(
                home, "Library", "Containers", "com.lemon.lvpro", "Data",
                "Library", "Application Support", "JianyingPro", "User Data",
            ),
            os.path.join(home, "Library", "Application Support", "JianyingPro", "User Data"),
        ]
        jy_user_data = next((p for p in candidates if os.path.exists(p)), None)
        if not jy_user_data:
            return defaults
    else:
        # ---- Windows ----
        local_app_data = os.getenv("LOCALAPPDATA")
        if not local_app_data:
            return defaults
        jy_user_data = os.path.join(local_app_data, "JianyingPro", "User Data")

    cfg = {"device_id": defaults[0], "iid": defaults[1]}

    ttnet_path = os.path.join(jy_user_data, "TTNet", "tt_net_config.config")
    if os.path.exists(ttnet_path):
        try:
            with open(ttnet_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            m = re.search(r"device_id\&#\*(\d+)", content)
            if m:
                cfg["device_id"] = m.group(1)
        except Exception:
            pass

    log_dir = os.path.join(jy_user_data, "Log")
    if os.path.exists(log_dir):
        logs = sorted(
            [os.path.join(log_dir, x) for x in os.listdir(log_dir) if x.endswith(".log")],
            key=os.path.getmtime,
            reverse=True,
        )
        for p in logs[:5]:
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    chunk = f.read(1_000_000)
                m = re.search(r"iid=(\d+)", chunk)
                if m:
                    cfg["iid"] = m.group(1)
                    break
            except Exception:
                continue

    return cfg["device_id"], cfg["iid"]


APP_KEY = "IZjhUeAYwP"
APP_ID = "3704"


def _ffmpeg_exe() -> Optional[str]:
    """定位可用的 ffmpeg：优先系统 PATH，其次 imageio-ffmpeg 自带的二进制。"""
    import shutil

    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _remux_audio(path: str) -> str:
    """把流式拼接出来的链式 ogg 重新封装为单一连续流。

    SAMI 对长文本会分多个 ogg 流返回，拼接后 MediaInfo 只认第一个流的时长。
    重新解码封装成 wav 后时长才正确。失败时原样返回，不影响主流程。
    """
    exe = _ffmpeg_exe()
    if not exe or not os.path.exists(path):
        return path

    fixed = os.path.splitext(path)[0] + "_fixed.wav"
    try:
        proc = subprocess.run(
            [exe, "-y", "-loglevel", "error", "-i", path, "-c:a", "pcm_s16le", fixed],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        if proc.returncode == 0 and os.path.exists(fixed) and os.path.getsize(fixed) > 0:
            return fixed
        print(f"[!] Remux failed, using raw file: {proc.stderr.decode('utf-8', 'ignore')[:120]}", flush=True)
    except Exception as e:
        print(f"[!] Remux exception, using raw file: {e}", flush=True)
    return path


def _build_ssl_context() -> ssl.SSLContext:
    if CONFIG.tts_insecure_ssl:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        print("[!] WARNING: TLS verification disabled by JY_TTS_INSECURE_SSL=1", flush=True)
        return ctx
    return ssl.create_default_context()


# SAMI TTS 文本长度上限（火山引擎官方文档，错误码 40402003 TTSExceededTextLimit）：
#   流式 WebSocket 接口：2000 个 UTF-8 字符
#   非流式 HTTP 接口：  1000 个 UTF-8 字符
# 本文件用的是流式 WebSocket，因此单次请求可以传几百个汉字，
# 不需要把文案切成 25 字的小段（那样会让配音听起来一顿一顿）。
SAMI_WS_TEXT_LIMIT = 2000


async def _run_sami_tts(text: str, speaker: str, output_file: str, dev_id: str, iid: str):
    if len(text) > SAMI_WS_TEXT_LIMIT:
        return False, (
            f"Text too long for SAMI streaming TTS: {len(text)} > {SAMI_WS_TEXT_LIMIT} chars"
        )
    ws_url = f"wss://sami.bytedance.com/internal/api/v2/ws?device_id={dev_id}&iid={iid}"
    headers = {
        "User-Agent": f"JianyingPro/5.9.0.11632 (Windows 10.0.19045; app_id:3704; device_id:{dev_id})"
    }
    ssl_context = _build_ssl_context()

    try:
        async with websockets.connect(
            ws_url, additional_headers=headers, ssl=ssl_context, open_timeout=20
        ) as ws:
            task_id = f"ai_gen_{os.urandom(4).hex()}"
            start_msg = {
                "app_id": APP_ID,
                "appkey": APP_KEY,
                "event": "StartTask",
                "namespace": "TTS",
                "task_id": task_id,
                "message_id": task_id + "_0",
                "payload": json.dumps(
                    {
                        "text": text,
                        "speaker": speaker,
                        "audio_config": {
                            "format": "ogg_opus",
                            "sample_rate": 24000,
                            "bit_rate": 64000,
                        },
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
            await ws.send(json.dumps(start_msg, ensure_ascii=False, separators=(",", ":")))
            await ws.send(
                json.dumps({"appkey": APP_KEY, "event": "FinishTask", "namespace": "TTS"})
            )

            audio_data = bytearray()
            while True:
                try:
                    resp_raw = await asyncio.wait_for(ws.recv(), timeout=15)
                except asyncio.TimeoutError:
                    return False, "SAMI Timeout"

                if isinstance(resp_raw, str):
                    resp = json.loads(resp_raw)
                    event = resp.get("event")
                    if event == "TaskFailed":
                        return (
                            False,
                            f"SAMI Error: {resp.get('status_text')} (Code: {resp.get('status_code')})",
                        )
                    if event == "TaskFinished":
                        break
                else:
                    audio_data.extend(resp_raw)

            if audio_data:
                with open(output_file, "wb") as f:
                    f.write(audio_data)
                # 🔥 SAMI 是流式返回：长文本会分成多个独立的 ogg 流，
                # 直接拼接得到的是"链式 ogg"(chained ogg)。这种文件能播放，
                # 但 MediaInfo 只会读到**第一个流**的时长（例如 30 秒的音频只报 10 秒），
                # 导致上层 AudioMaterial 拿到错误时长 / 片段重叠 / 判定失败。
                # 这里统一重新封装成单一连续流，让时长可被正确解析。
                fixed = _remux_audio(output_file)
                return True, fixed
            return False, "No audio"
    except Exception as e:
        return False, str(e)


async def _run_edge_tts(text: str, output_file: str, voice: str = "zh-CN-YunxiNeural"):
    try:
        import edge_tts

        actual_path = output_file
        if output_file.endswith(".ogg") and not output_file.endswith(".mp3"):
            actual_path = output_file + ".mp3"

        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(actual_path)
        return True, actual_path
    except Exception as e:
        return False, f"Edge-TTS Error: {str(e)}"


async def generate_voice_with_meta(
    text: str,
    output_path: str,
    speaker: str = "zh_male_huoli",
    *,
    backend: Optional[str] = None,
    allow_fallback: bool = True,
    sami_retries: int = 2,
) -> Tuple[Optional[str], Optional[str]]:
    """
    backend: None | "sami" | "edge"
    allow_fallback: when True, SAMI failure may fallback to edge.
    returns: (audio_path, backend_used)
    """
    dev_id, iid = get_jy_local_config()
    print(f"[*] Intelligent TTS Trace: speaker={speaker}, dev={dev_id}, iid={iid}", flush=True)

    force_sami = backend == "sami"
    force_edge = backend == "edge"

    if not force_edge:
        for i in range(max(1, int(sami_retries))):
            ok, res = await _run_sami_tts(text, speaker, output_path, dev_id, iid)
            if ok:
                print(f"[+] SAMI Success: {res}", flush=True)
                return res, "sami"
            print(
                f"[!] SAMI Failed (attempt {i + 1}/{max(1, int(sami_retries))}): {res}", flush=True
            )
            if i + 1 < max(1, int(sami_retries)):
                await asyncio.sleep(0.35)

        if force_sami or not allow_fallback:
            return None, None

    voice = "zh-CN-YunxiNeural" if "male" in speaker else "zh-CN-XiaoxiaoNeural"
    ok_edge, res_edge = await _run_edge_tts(text, output_path, voice)
    if ok_edge:
        print(f"[+] Edge-TTS Success: {res_edge}", flush=True)
        return res_edge, "edge"
    return None, None


async def generate_voice(
    text: str,
    output_path: str,
    speaker: str = "zh_male_huoli",
    *,
    backend: Optional[str] = None,
    allow_fallback: bool = True,
    sami_retries: int = 2,
):
    path, _backend_used = await generate_voice_with_meta(
        text,
        output_path,
        speaker,
        backend=backend,
        allow_fallback=allow_fallback,
        sami_retries=sami_retries,
    )
    return path


if __name__ == "__main__":
    asyncio.run(generate_voice("测试智能配音系统集成成功。", "test.ogg"))
