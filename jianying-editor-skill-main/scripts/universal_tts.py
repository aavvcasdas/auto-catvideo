import asyncio
import json
import os
import re
import ssl
import struct
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


def ogg_total_duration_us(path: str) -> int:
    """纯 Python 解析 Ogg 页，返回总时长(微秒)。无需 ffmpeg。

    为什么需要它：SAMI 是流式接口，长文本会分多个独立的 ogg 流返回，
    客户端把字节直接拼接后得到「链式 ogg」(chained ogg)。这种文件能正常
    播放，但 MediaInfo 只统计第一个流，30 秒的音频会被报成 10 秒，
    导致时间轴错乱、片段重叠、配音判定失败。

    Opus 的 granulepos 以 48kHz 计。链式流里每一段都从 0 重新计数，
    所以遇到 granulepos 回退(或新的 OpusHead)就说明进入下一段，需要累加。
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return 0

    total_samples = 0
    last = {}       # serial -> 上一次见到的 granulepos
    pre_skip = {}   # serial -> 编码器预跳过的采样数
    i, n = 0, len(data)

    while True:
        i = data.find(b"OggS", i)
        if i < 0 or i + 27 > n:
            break
        granule = struct.unpack_from("<q", data, i + 6)[0]
        serial = struct.unpack_from("<I", data, i + 14)[0]
        nsegs = data[i + 26]
        if i + 27 + nsegs > n:
            break
        seg_table = data[i + 27:i + 27 + nsegs]
        body = i + 27 + nsegs

        if data[body:body + 8] == b"OpusHead":
            # 新的一段开始，先结算上一段
            if serial in last:
                total_samples += max(0, last.pop(serial) - pre_skip.get(serial, 0))
            if body + 12 <= n:
                pre_skip[serial] = struct.unpack_from("<H", data, body + 10)[0]

        if granule >= 0:
            prev = last.get(serial)
            if prev is not None and granule < prev:
                # granulepos 回退 = 进入链式流的下一段
                total_samples += max(0, prev - pre_skip.get(serial, 0))
            last[serial] = granule

        i = body + sum(seg_table)

    for serial, g in last.items():
        total_samples += max(0, g - pre_skip.get(serial, 0))

    return int(total_samples / 48000 * 1_000_000)


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
                return True, output_file
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
