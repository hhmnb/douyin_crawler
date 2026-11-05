import os
import re
import sys
import time
import json
import shutil
import subprocess
import urllib.parse
import tempfile
from pathlib import Path
from datetime import datetime
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import tkinter as tk
from tkinter import scrolledtext, messagebox, filedialog
import requests

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False

# ==================== 全局配置 ====================
USE_HEADLESS = True
MAX_RETRY = 3
DOWNLOAD_THREADS = 16
AUDIO_WAIT_AFTER_VIDEO = 6.0
BATCH_CONCURRENCY = 2
# 探测阶段最多探测多少个候选，避免长视频浪费流量
MAX_PROBE_CANDIDATES = 4
# 头部探测时下载多少字节用于 ffprobe 判断流类型（3MB 足够覆盖 moov atom）
PROBE_HEAD_BYTES = 3 * 1024 * 1024

URL_BLACKLIST = [
    "uuu_265.mp4", "/obj/douyin-pc-web/uuu_", "ies/douyin_web/media/",
    "effectcdn", "byteeffecttos", "ies.fe.effect",
    "/logo-", "chat_fly.", "icons-color",
]

FFMPEG_COMMON_PATHS = [
    r"C:\ffmpeg\bin",
    r"C:\Program Files\ffmpeg-master-latest-win64-gpl-shared\bin",
    r"C:\Program Files\ffmpeg\bin",
    r"C:\Program Files (x86)\ffmpeg\bin",
    os.path.expanduser(r"~\ffmpeg\bin"),
]

HISTORY_FILE = "download_history.json"

MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

# DASH 音频 URL 特征
DASH_AUDIO_STRONG = ["mime_type=audio", "audio_mp4", "media-audio", "audio-und"]
DASH_AUDIO_WEAK = ["-audio-", ".m4a", ".mp3", ".aac", ".opus", "/audio/"]
BGM_DOMAINS = ["douyinstatic.com", "ies-music"]


# ==================== 1. 输出重定向 ====================
class StdoutRedirector:
    def __init__(self, text_widget, root_widget):
        self.text_widget = text_widget
        self.root_widget = root_widget
        self.original_stdout = sys.stdout

    def write(self, string):
        try:
            if self.text_widget.winfo_exists():
                self.root_widget.after_idle(self._safe_write, string)
            else:
                self.original_stdout.write(string)
        except Exception:
            try:
                self.original_stdout.write(string)
            except Exception:
                pass

    def _safe_write(self, string):
        try:
            if self.text_widget.winfo_exists():
                self.text_widget.insert(tk.END, string)
                self.text_widget.see(tk.END)
        except Exception:
            pass

    def flush(self):
        pass


# ==================== 2. 工具函数 ====================
def extract_best_url(raw_text):
    patterns = [
        r'https?://www\.iesdouyin\.com/share/video/\d{19}',
        r'https?://www\.douyin\.com/video/\d{19}',
        r'https?://v\.douyin\.com/[A-Za-z0-9_-]+/?',
        r'https?://www\.douyin\.com/note/\d{19}',
        r'https?://www\.iesdouyin\.com/share/note/\d{19}',
    ]
    for pat in patterns:
        m = re.search(pat, raw_text)
        if m:
            return m.group(0).rstrip('/')
    urls = re.findall(r'https?://[^\s]+', raw_text)
    return urls[-1] if urls else raw_text.strip()


def extract_all_urls(raw_text):
    full_urls = []
    for m in re.finditer(r'https?://[^\s]+', raw_text):
        u = m.group(0)
        if re.match(r'https?://(?:v\.douyin\.com/[A-Za-z0-9_-]+'
                    r'|www\.douyin\.com/(?:video|note)/\d{19}'
                    r'|www\.iesdouyin\.com/share/(?:video|note)/\d{19})', u):
            full_urls.append(u.rstrip('/'))
    seen = set()
    result = []
    for u in full_urls:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


def is_blacklisted(url):
    lower = url.lower()
    return any(bad.lower() in lower for bad in URL_BLACKLIST)


def is_bgm_url(url):
    lower = url.lower()
    return any(d in lower for d in BGM_DOMAINS)


def clean_url(url_str):
    try:
        if '\\u' in url_str:
            url_str = url_str.encode('utf-8').decode('unicode_escape')
    except:
        pass
    url_str = url_str.replace(r'\/', '/').replace('\\/', '/').replace('&amp;', '&')
    if url_str.startswith("//"):
        url_str = "https:" + url_str
    return url_str


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return []


def append_history(entry):
    history = load_history()
    history.append(entry)
    try:
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(history[-500:], f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 写历史失败: {e}")


# ==================== 3. 无Cookie分享页提取 ====================
def _search_uri_in_json(obj):
    if isinstance(obj, dict):
        uri = obj.get("uri")
        if isinstance(uri, str) and uri.startswith("v0"):
            return uri
        for v in obj.values():
            r = _search_uri_in_json(v)
            if r:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = _search_uri_in_json(item)
            if r:
                return r
    return None


def fetch_via_aweme_snssdk(uri):
    if not uri:
        return None
    api_url = f"https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}&ratio=1080p&line=0"
    headers = {'User-Agent': MOBILE_UA}
    try:
        resp = requests.get(api_url, headers=headers, allow_redirects=False, timeout=12)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get('Location')
            if location:
                print(f"   ✅ aweme.snssdk.com 返回 302 直链")
                return clean_url(location)
        if resp.status_code == 200:
            m = re.search(r'https?://[^\s"\'<>]+', resp.text)
            if m:
                return clean_url(m.group(0))
    except Exception as e:
        print(f"   ⚠️ aweme.snssdk.com 失败: {e}")
    return None


def _extract_audio_from_html(html):
    m = re.search(r'"music"\s*:\s*\{[\s\S]*?"play_url"\s*:\s*\{[\s\S]*?"url_list"\s*:\s*\[\s*"([^"]+)"', html)
    if m:
        url = clean_url(m.group(1))
        if ".mp3" in url or "music" in url or "douyinstatic" in url:
            return url
    return None


def fetch_video_via_share_page(video_id):
    """方案1：分享页 + aweme.snssdk.com 直连。分享页取到的音频是 BGM。"""
    print("📱 【无Cookie】分享页解析...")
    share_url = f"https://www.iesdouyin.com/share/video/{video_id}/"
    headers = {'User-Agent': MOBILE_UA}
    try:
        resp = requests.get(share_url, headers=headers, timeout=15)
        html = resp.text
        audio_url = _extract_audio_from_html(html)

        uri = None
        m = re.search(r'"uri"\s*:\s*"(v0[0-9a-f]+)"', html)
        if m:
            uri = m.group(1)
        if not uri:
            for marker in ['_ROUTER_DATA', 'RENDER_DATA']:
                mm = re.search(rf'id="{marker}"[^>]*>([\s\S]*?)</script>', html)
                if mm:
                    try:
                        raw = mm.group(1).strip()
                        if marker == 'RENDER_DATA':
                            raw = urllib.parse.unquote(raw)
                        data = json.loads(raw)
                        uri = _search_uri_in_json(data)
                        if uri:
                            break
                    except:
                        pass

        if uri:
            print(f"   🎯 uri: {uri[:40]}...")
            aweme_url = fetch_via_aweme_snssdk(uri)
            if aweme_url:
                return {
                    "video_candidates": [aweme_url],
                    "original_audio_candidates": [],
                    "bgm_candidates": [audio_url] if audio_url else [],
                    "source": "share_page"
                }
    except Exception as e:
        print(f"   ⚠️ 分享页流程失败: {e}")
    return None


# ==================== 4. 解析核心 ====================
def parse_universal_data(html_text):
    match = re.search(r'id="__UNIVERSAL_DATA__"[^>]*>([\s\S]*?)</script>', html_text)
    if match:
        try:
            return json.loads(urllib.parse.unquote(match.group(1)))
        except:
            pass
    return None


def _extract_original_audio_from_bitrate(video_obj):
    if not isinstance(video_obj, dict):
        return None
    bit_rates = video_obj.get("bit_rate")
    if not isinstance(bit_rates, list):
        return None
    for br in bit_rates:
        if not isinstance(br, dict):
            continue
        play_addr = br.get("play_addr")
        if not isinstance(play_addr, dict):
            continue
        urls = play_addr.get("url_list")
        if not urls or not isinstance(urls, list):
            continue
        for u in urls:
            u_lower = u.lower()
            if any(k in u_lower for k in DASH_AUDIO_STRONG):
                return clean_url(u)
    return None


def extract_media_from_json(data):
    result = {"video_url": None, "audio_url": None, "original_audio_url": None, "images": []}

    def _search(obj):
        if isinstance(obj, dict):
            video = obj.get("video") or obj.get("video_info")
            if isinstance(video, dict):
                pa = video.get("play_addr") or video.get("play_addr_h264")
                if isinstance(pa, dict):
                    urls = pa.get("url_list")
                    if urls and isinstance(urls, list):
                        result["video_url"] = clean_url(urls[0])
                elif isinstance(pa, list) and pa:
                    result["video_url"] = clean_url(pa[0])
                if not result["original_audio_url"]:
                    orig = _extract_original_audio_from_bitrate(video)
                    if orig:
                        result["original_audio_url"] = orig
            music = obj.get("music") or obj.get("music_info")
            if isinstance(music, dict):
                pu = music.get("play_url") or music.get("play_info")
                if isinstance(pu, dict):
                    urls = pu.get("url_list")
                    if urls and isinstance(urls, list):
                        result["audio_url"] = clean_url(urls[0])
            images = obj.get("images")
            if isinstance(images, list):
                for img in images:
                    if isinstance(img, dict):
                        ul = img.get("url_list")
                        if ul and isinstance(ul, list):
                            result["images"].append(clean_url(ul[0]))
            for v in obj.values():
                _search(v)
        elif isinstance(obj, list):
            for item in obj:
                _search(item)

    _search(data)
    return result


def try_parse_html(html_text, source_name=""):
    data = parse_universal_data(html_text)
    if not data:
        for marker in ['RENDER_DATA', '_ROUTER_DATA']:
            m = re.search(rf'id="{marker}"[^>]*>([\s\S]*?)</script>', html_text)
            if m:
                try:
                    data = json.loads(urllib.parse.unquote(m.group(1).strip()))
                    print(f"💡 [{source_name}] {marker}")
                    break
                except:
                    pass
    if data:
        res = extract_media_from_json(data)
        if res and (res['video_url'] or res['audio_url'] or res['images']):
            res['type'] = 'image' if (not res['video_url'] and res['images']) else 'video'
            return res
    return None


def fetch_video_via_requests(video_id):
    for url, ua, name in [
        (f"https://www.iesdouyin.com/share/video/{video_id}/", MOBILE_UA, "移动H5"),
        (f"https://www.douyin.com/video/{video_id}",
         'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36', "PC"),
    ]:
        try:
            resp = requests.get(url, headers={'User-Agent': ua,
                             'Accept': 'text/html,application/xhtml+xml'}, timeout=12)
            res = try_parse_html(resp.text, name)
            if res:
                return res
        except:
            pass
    return None


# ==================== 5. Cookies ====================
def get_cookies_from_browser():
    if not HAS_PLAYWRIGHT:
        return None
    user_data_dir = os.path.join(os.getcwd(), "douyin_browser_data")
    cookie_file = os.path.join(os.getcwd(), "douyin_cookies.txt")
    print("🍪 提取 cookies...")
    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=user_data_dir, headless=USE_HEADLESS,
                viewport={"width": 1280, "height": 720},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                args=["--disable-blink-features=AutomationControlled"]
            )
        except:
            browser = p.chromium.launch(headless=USE_HEADLESS)
            context = browser.new_context(viewport={"width": 1280, "height": 720})
        page = context.new_page()
        try:
            page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30000)
            if not USE_HEADLESS:
                print("👆 请手动登录...")
            for _ in range(10):
                time.sleep(2)
                if any(c.get('name') == 'sessionid' for c in context.cookies()):
                    print("✅ 已登录")
                    break
            cookies = context.cookies()
            with open(cookie_file, 'w', encoding='utf-8') as f:
                f.write("# Netscape HTTP Cookie File\n")
                for c in cookies:
                    domain = c.get('domain', '')
                    if not domain.startswith('.'):
                        domain = '.' + domain
                    secure = 'TRUE' if c.get('secure', False) else 'FALSE'
                    ex = c.get('expires', 0)
                    expires = '0' if (ex is None or ex < 0) else str(int(ex))
                    n, v = c.get('name', ''), c.get('value', '')
                    if not n or not v:
                        continue
                    f.write(f"{domain}\tTRUE\t{c.get('path', '/')}\t{secure}\t{expires}\t{n}\t{v}\n")
            print(f"✅ Cookies: {cookie_file}")
            return cookie_file
        except Exception as e:
            print(f"⚠️ cookies 失败: {e}")
            return None
        finally:
            context.close()


# ==================== 6. yt-dlp ====================
def fetch_video_via_ytdlp(video_id, cookies_file=None):
    if not HAS_YTDLP:
        return None
    url = f"https://www.douyin.com/video/{video_id}"
    ydl_opts = {'quiet': True, 'no_warnings': True, 'skip_download': True}
    if cookies_file and os.path.exists(cookies_file):
        ydl_opts['cookiefile'] = cookies_file
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get('formats', [])
            best_v, best_a = None, None
            for fmt in formats:
                h = fmt.get('height', 0) or 0
                if fmt.get('vcodec') != 'none' and fmt.get('acodec') == 'none':
                    if not best_v or h > (best_v.get('height', 0) or 0):
                        best_v = fmt
                elif fmt.get('acodec') != 'none' and fmt.get('vcodec') == 'none':
                    if not best_a or (fmt.get('abr', 0) or 0) > (best_a.get('abr', 0) or 0):
                        best_a = fmt
                elif fmt.get('vcodec') != 'none' and fmt.get('acodec') != 'none':
                    if not best_v or h > (best_v.get('height', 0) or 0):
                        best_v = fmt
                        best_a = None
            if best_v:
                return {"video_url": best_v.get('url'),
                        "audio_url": best_a.get('url') if best_a else None}
            if formats:
                return {"video_url": formats[-1].get('url'), "audio_url": None}
    except Exception as e:
        print(f"⚠️ yt-dlp: {e}")
    return None


def download_with_ytdlp(video_id, output_path, cookies_file=None):
    if not HAS_YTDLP:
        return False
    url = f"https://www.douyin.com/video/{video_id}"
    ydl_opts = {
        'outtmpl': str(output_path), 'quiet': True, 'no_warnings': True,
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
    }
    if cookies_file and os.path.exists(cookies_file):
        ydl_opts['cookiefile'] = cookies_file
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return True
    except Exception as e:
        print(f"⚠️ yt-dlp: {e}")
        return False


# ==================== 7. 浏览器自动化 ====================
def fetch_video_via_browser(video_id):
    if not HAS_PLAYWRIGHT:
        print("❌ 未安装 Playwright")
        return None

    user_data_dir = os.path.join(os.getcwd(), "douyin_browser_data")
    pc_url = f"https://www.douyin.com/video/{video_id}"

    api_videos = []
    api_audios = []
    network_candidates = []
    orig_audio_candidates = []
    bgm_candidates = []
    media_playlist = []

    print("🌐 启动浏览器...")
    browser = None
    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=user_data_dir, headless=USE_HEADLESS,
                viewport={"width": 1280, "height": 720},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                args=["--disable-blink-features=AutomationControlled"]
            )
        except:
            browser = p.chromium.launch(headless=USE_HEADLESS)
            context = browser.new_context(viewport={"width": 1280, "height": 720})

        page = context.new_page()

        def on_response(response):
            url = response.url
            lower_url = url.lower()
            content_type = response.headers.get("content-type", "").lower()

            if "application/json" in content_type:
                if any(k in lower_url for k in ["aweme/detail", "aweme/v1/web", "aweme/v2/web"]):
                    try:
                        data = response.json()
                        info = extract_media_from_json(data)
                        v = info.get("video_url")
                        a = info.get("audio_url")
                        oa = info.get("original_audio_url")
                        if v:
                            api_videos.append((v, a))
                        if a and a not in api_audios:
                            api_audios.append(a)
                        if oa and oa not in orig_audio_candidates:
                            orig_audio_candidates.append(oa)
                    except Exception:
                        pass
                return

            if is_blacklisted(url):
                return
            if any(ext in content_type for ext in [
                "text/html", "text/css", "application/javascript",
                "image/jpeg", "image/png", "image/webp", "image/gif",
                "application/xml", "text/plain"
            ]):
                return

            strong_audio = any(k in lower_url for k in DASH_AUDIO_STRONG)
            weak_audio = any(k in lower_url for k in DASH_AUDIO_WEAK)
            url_is_video = any(k in lower_url for k in [
                "/video/", "media-video", "-video-", ".mp4", ".m4s",
                "avc1", "hvc1", "hevc",
            ])
            url_is_douyinvod = "douyinvod.com" in lower_url

            type_label = None
            if "mpegurl" in content_type or "dash+xml" in content_type:
                type_label = "playlist"
            elif strong_audio:
                type_label = "audio"
            elif "audio" in content_type and "video" not in content_type:
                type_label = "audio"
            elif url_is_douyinvod and weak_audio:
                type_label = "audio"
            elif "video" in content_type:
                type_label = "video"
            elif weak_audio:
                type_label = "audio"
            elif url_is_video:
                type_label = "video"
            elif url_is_douyinvod:
                type_label = "video"
            else:
                return

            cl = response.headers.get("content-length")
            size = int(cl) if cl and cl.isdigit() else 0

            if type_label == "playlist":
                media_playlist.append(url)
            elif type_label == "audio":
                if is_bgm_url(url):
                    bgm_candidates.append((size, url))
                else:
                    orig_audio_candidates.append((size, url))
            else:
                network_candidates.append((size, url, type_label))

        page.on("response", on_response)

        def check_playing():
            try:
                return page.evaluate("""
                    () => {
                        const vs = document.querySelectorAll('video');
                        for (const v of vs) {
                            if (!v.paused && v.readyState >= 2 && v.currentTime > 0) return true;
                        }
                        return false;
                    }
                """)
            except:
                return False

        try:
            page.goto(pc_url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_selector("video", timeout=40000)
                print("✅ 检测到视频播放器")
            except:
                print("❌ 未检测到 video")
                context.close()
                if browser:
                    browser.close()
                return None

            played = False
            first_pick_at = None
            for i in range(MAX_RETRY):
                try:
                    mv = page.locator("video").first
                    box = mv.bounding_box()
                    if box:
                        page.mouse.click(box['x'] + box['width'] // 2, box['y'] + box['height'] // 2)
                    else:
                        mv.click()
                except:
                    pass
                try:
                    page.evaluate("""
                        () => { document.querySelectorAll('video').forEach(v => { if (v.paused) v.play(); }); }
                    """)
                except:
                    pass
                time.sleep(1.5)
                if check_playing():
                    played = True
                    print("✅ 视频已播放")
                    break
                if (network_candidates or api_videos) and not first_pick_at:
                    first_pick_at = time.time()

            if not first_pick_at and (network_candidates or api_videos):
                first_pick_at = time.time()
            if first_pick_at:
                ws = time.time()
                while time.time() - ws < AUDIO_WAIT_AFTER_VIDEO:
                    if orig_audio_candidates or bgm_candidates:
                        print("✅ 已捕获音频，提前结束等待")
                        break
                    time.sleep(0.5)

            video_list = []
            seen = set()

            def add_v(u):
                if u and u not in seen:
                    seen.add(u)
                    video_list.append(u)

            sorted_net = sorted([c for c in network_candidates if c[0] > 50 * 1024], key=lambda x: -x[0])
            for s, u, k in sorted_net:
                if "douyinvod.com" in u and "web-prime" not in u:
                    add_v(u)
            for v, _ in api_videos:
                if "web-prime" not in v:
                    add_v(v)
            for s, u, k in sorted_net:
                if "douyinvod.com" in u:
                    add_v(u)
            for v, _ in api_videos:
                add_v(v)
            for u in media_playlist:
                add_v(u)
            for s, u, k in sorted_net:
                add_v(u)

            orig_list = []
            aseen = set()
            for u in orig_audio_candidates:
                if isinstance(u, tuple):
                    u = u[1]
                if u and u not in aseen and not is_bgm_url(u):
                    aseen.add(u)
                    orig_list.append(u)

            api_bgm = []
            for a in api_audios:
                if is_bgm_url(a):
                    api_bgm.append(a)
                elif a not in aseen:
                    aseen.add(a)
                    orig_list.append(a)

            bgm_list = []
            bseen = set()
            for u in api_bgm:
                if u not in bseen:
                    bseen.add(u)
                    bgm_list.append(u)

            orig_sorted = sorted([c for c in orig_audio_candidates if isinstance(c, tuple)],
                                 key=lambda x: -x[0])
            for s, u in orig_sorted:
                if u not in aseen and not is_bgm_url(u):
                    aseen.add(u)
                    orig_list.append(u)

            bgm_sorted = sorted(bgm_candidates, key=lambda x: -x[0])
            for s, u in bgm_sorted:
                if u not in bseen:
                    bseen.add(u)
                    bgm_list.append(u)

            if not video_list:
                try:
                    src = page.evaluate(
                        "() => { const v = document.querySelector('video'); return v ? (v.currentSrc || v.src) : null; }"
                    )
                    if src and src.startswith("http") and "douyinvod.com" in src.lower() and not is_blacklisted(src):
                        video_list.append(src)
                except:
                    pass

            print(f"⏳ 视频候选: {len(video_list)} 个，原声音频候选: {len(orig_list)} 个，BGM候选: {len(bgm_list)} 个")
            for v in video_list[:5]:
                print(f"   📹 {v[:100]}...")
            for a in orig_list[:3]:
                print(f"   🎵[原声] {a[:100]}...")
            for a in bgm_list[:2]:
                print(f"   🎵[BGM] {a[:100]}...")

        except Exception as e:
            print(f"⚠️ 页面异常: {e}")
            video_list, orig_list, bgm_list = [], [], []
        finally:
            context.close()
            if browser:
                browser.close()

    if not video_list:
        return None
    return {"video_candidates": video_list,
            "original_audio_candidates": orig_list,
            "bgm_candidates": bgm_list,
            "source": "browser"}


# ==================== 8. 视频 ID 提取 ====================
def get_video_id(input_url):
    try:
        resp = requests.get(input_url, allow_redirects=True, timeout=12)
        m = re.search(r'(?:video|note)/(\d{19})', resp.url)
        if m:
            return m.group(1)
        r = requests.get(f"https://www.douyin.com/oembed?url={input_url}", timeout=10)
        if r.status_code == 200:
            vid = r.json().get('video_id')
            if vid:
                return str(vid)
    except:
        pass
    return None


# ==================== 9. 提取链 ====================
def fetch_media(video_id):
    print("\n📱 方案1：分享页无Cookie直连...")
    result = fetch_video_via_share_page(video_id)
    if result and result.get("video_candidates"):
        print("✅ 方案1 成功！")
        return result
    print("❌ 方案1 失败")

    print("\n📄 方案2：分享页 JSON 解析...")
    res = fetch_video_via_requests(video_id)
    if res and res.get("video_url"):
        print("✅ 方案2 成功！")
        bgm = []
        orig = []
        if res.get("original_audio_url"):
            orig.append(res.get("original_audio_url"))
        if res.get("audio_url"):
            bgm.append(res.get("audio_url"))
        return {
            "video_candidates": [res.get("video_url")],
            "original_audio_candidates": orig,
            "bgm_candidates": bgm,
            "source": "share_json"
        }
    print("❌ 方案2 失败")

    cookie_file = os.path.join(os.getcwd(), "douyin_cookies.txt")
    if HAS_YTDLP and os.path.exists(cookie_file):
        print("\n🚀 方案3：yt-dlp 提取（使用 cookies）...")
        yt = fetch_video_via_ytdlp(video_id, cookies_file=cookie_file)
        if yt and yt.get("video_url"):
            print("✅ 方案3 成功！")
            return {
                "video_candidates": [yt.get("video_url")],
                "original_audio_candidates": [yt.get("audio_url")] if yt.get("audio_url") else [],
                "bgm_candidates": [],
                "source": "yt-dlp"
            }
        print("❌ 方案3 失败（yt-dlp 签名墙）")
    else:
        print("\nℹ️ 方案3 跳过（无 yt-dlp 或无 cookies）")

    print("\n🌐 方案4：浏览器自动化（最后兜底）...")
    browser_res = fetch_video_via_browser(video_id)
    if browser_res and browser_res.get("video_candidates"):
        print("✅ 方案4 成功！")
        return browser_res
    print("❌ 方案4 失败")

    print("\n❌ 所有提取方案均失败")
    return None


# ==================== 10. 下载引擎 ====================
def get_remote_size(session, url, headers):
    try:
        h = headers.copy()
        h['Range'] = 'bytes=0-1'
        r = session.get(url, headers=h, timeout=10, stream=True)
        cr = r.headers.get('content-range', '')
        if '/' in cr:
            try:
                size = int(cr.split('/')[-1]); r.close(); return size
            except:
                pass
        cl = r.headers.get('content-length', '0'); r.close()
        if cl.isdigit():
            n = int(cl)
            if n > 2:
                return n
    except:
        pass
    try:
        hr = session.head(url, headers=headers, timeout=10, allow_redirects=True)
        cl = hr.headers.get('content-length', '0')
        if cl.isdigit():
            return int(cl)
    except:
        pass
    return 0


def mt_download_file(url, headers, output_path, num_threads=4):
    session = requests.Session()
    dh = headers.copy()
    if 'Host' in dh:
        del dh['Host']

    total = get_remote_size(session, url, dh)

    def single(reason):
        print(f"⚠️ {reason} → 单线程...")
        for i in range(num_threads):
            tp = f"{output_path}.part{i}"
            if os.path.exists(tp):
                os.remove(tp)
        try:
            with session.get(url, headers=dh, stream=True, timeout=30) as r:
                r.raise_for_status()
                with open(output_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1024*1024):
                        if chunk:
                            f.write(chunk)
            return True
        except Exception as ex:
            print(f"❌ 单线程失败: {ex}")
            return False

    if total <= 500 * 1024:
        return single("资源小/长度未知")

    th = dh.copy(); th['Range'] = 'bytes=0-10'
    try:
        tr = session.get(url, headers=th, timeout=8)
        if tr.status_code != 206:
            return single("不支持分块")
    except Exception as e:
        return single(f"CDN探测失败 ({e})")

    print(f"📊 {total // 1024} KB，{num_threads} 线程...")

    part = total // num_threads
    lock = threading.Lock()
    progress = [0] * num_threads
    last_pct = -1

    def dl_range(start, end, idx, tp):
        nonlocal last_pct
        rh = dh.copy(); rh['Range'] = f'bytes={start}-{end}'
        for _ in range(3):
            try:
                with lock:
                    progress[idx] = 0
                with session.get(url, headers=rh, stream=True, timeout=15) as r:
                    if r.status_code == 206:
                        with open(tp, 'wb') as f:
                            for ch in r.iter_content(chunk_size=128*1024):
                                if ch:
                                    f.write(ch)
                                    with lock:
                                        progress[idx] += len(ch)
                                        cur = sum(progress)
                                        p = int((cur / total) * 100)
                                        if p != last_pct and p % 10 == 0:
                                            last_pct = p
                                            print(f" 🚀 {p}%")
                        return True
            except:
                time.sleep(1.0)
        return False

    temps = []
    ok = False
    try:
        with ThreadPoolExecutor(max_workers=num_threads) as ex:
            futs = []
            for i in range(num_threads):
                s = i * part
                e = total - 1 if i == num_threads - 1 else (s + part - 1)
                tp = f"{output_path}.part{i}"
                temps.append(tp)
                futs.append(ex.submit(dl_range, s, e, i, tp))
            results = [f.result() for f in as_completed(futs)]
        if all(results):
            with open(output_path, 'wb') as out:
                for tp in temps:
                    with open(tp, 'rb') as pf:
                        out.write(pf.read())
            ok = True
            print(" 🚀 完成！")
        else:
            raise RuntimeError("分片失败")
    except Exception as mte:
        return single(f"多线程失败 ({mte})")
    finally:
        for tp in temps:
            if os.path.exists(tp):
                try:
                    os.remove(tp)
                except:
                    pass
        if not ok and os.path.exists(output_path):
            try:
                os.remove(output_path)
            except:
                pass
    return ok


def download_with_ffmpeg(playlist_url, output_path):
    if not check_ffmpeg_available():
        return False
    print(f"🎼 ffmpeg: {playlist_url[:100]}...")
    try:
        subprocess.run(["ffmpeg", "-y", "-i", playlist_url, "-c", "copy", str(output_path)],
                       check=True, capture_output=True, text=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ ffmpeg: {e.stderr[:300]}")
        return False


_ffmpeg_cache = None

def check_ffmpeg_available():
    global _ffmpeg_cache
    if _ffmpeg_cache is not None:
        return _ffmpeg_cache
    if shutil.which("ffmpeg"):
        _ffmpeg_cache = True
        return True
    for path in FFMPEG_COMMON_PATHS:
        if os.path.exists(os.path.join(path, "ffmpeg.exe")):
            os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
            print(f"💡 ffmpeg: {path}")
            _ffmpeg_cache = True
            return True
    _ffmpeg_cache = False
    return False


def probe_media_info(file_path):
    """探测媒体流信息。返回 {'has_video': bool, 'has_audio': bool} 或 None"""
    if not check_ffmpeg_available():
        return None
    try:
        r = subprocess.run([
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type",
            "-of", "json", str(file_path)
        ], capture_output=True, text=True, check=True, timeout=15)
        try:
            data = json.loads(r.stdout)
        except Exception:
            return None
        streams = data.get("streams", [])
        has_v = any(s.get("codec_type") == "video" for s in streams)
        has_a = any(s.get("codec_type") == "audio" for s in streams)
        return {"has_video": has_v, "has_audio": has_a}
    except FileNotFoundError:
        try:
            r = subprocess.run(
                ["ffmpeg", "-i", str(file_path)],
                capture_output=True, text=True, timeout=15
            )
            stderr = r.stderr
            return {"has_video": "Video:" in stderr,
                    "has_audio": "Audio:" in stderr}
        except Exception:
            return None
    except Exception:
        return None


def probe_remote_streams(url, headers, probe_bytes=PROBE_HEAD_BYTES):
    """
    只下载 URL 的前 probe_bytes 字节，用 ffprobe 探测流类型。
    返回 {'has_video': bool, 'has_audio': bool} 或 None。
    长视频秒级探测，避免全量下载后再判断。
    """
    if not check_ffmpeg_available():
        return None
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix='.head.mp4')
        os.close(fd)
        h = headers.copy()
        h['Range'] = f'bytes=0-{probe_bytes - 1}'
        with requests.get(url, headers=h, stream=True, timeout=15) as r:
            if r.status_code not in (200, 206):
                return None
            if r.status_code == 200:
                print("   ⚠️ CDN 忽略 Range，可能读取超过探测量的数据")
            with open(tmp, 'wb') as f:
                downloaded = 0
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    if downloaded >= probe_bytes:
                        break
        if os.path.getsize(tmp) < 8 * 1024:
            return None
        return probe_media_info(tmp)
    except Exception as e:
        print(f"   ⚠️ 头部探测失败: {e}")
        return None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except:
                pass


def has_audio_stream(file_path):
    info = probe_media_info(file_path)
    return bool(info and info.get("has_audio"))


def merge_audio_video(video_path, audio_path, output_path):
    if not check_ffmpeg_available():
        return False
    print("🔗 合并音视频...")
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", str(video_path), "-i", str(audio_path),
            "-c:v", "copy", "-c:a", "copy",
            "-map", "0:v:0", "-map", "1:a:0", "-shortest", str(output_path)
        ], check=True, capture_output=True, text=True, timeout=180)
        return True
    except subprocess.CalledProcessError:
        print("⚠️ -c:a copy 合并失败，尝试 AAC 重编码...")
        try:
            subprocess.run([
                "ffmpeg", "-y", "-i", str(video_path), "-i", str(audio_path),
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-map", "0:v:0", "-map", "1:a:0", "-shortest", str(output_path)
            ], check=True, capture_output=True, text=True, timeout=180)
            return True
        except subprocess.CalledProcessError as e2:
            print(f"❌ 合并失败: {e2.stderr[:500]}")
            return False
    except Exception as e:
        print(f"❌ 合并异常: {e}")
        return False


def extract_audio_track(video_path, audio_output_path):
    """从带音轨的视频中抽取音频到 m4a"""
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", str(video_path), "-vn",
            "-c:a", "copy", str(audio_output_path)
        ], check=True, capture_output=True, text=True, timeout=180)
        return os.path.exists(audio_output_path) and os.path.getsize(audio_output_path) > 1024
    except subprocess.CalledProcessError:
        try:
            subprocess.run([
                "ffmpeg", "-y", "-i", str(video_path), "-vn",
                "-c:a", "aac", "-b:a", "192k", str(audio_output_path)
            ], check=True, capture_output=True, text=True, timeout=180)
            return os.path.exists(audio_output_path) and os.path.getsize(audio_output_path) > 1024
        except Exception as e:
            print(f"⚠️ 抽取音频失败: {e}")
            return False
    except Exception as e:
        print(f"⚠️ 抽取音频异常: {e}")
        return False


def _try_merge_and_replace(video_path, audio_path, label=""):
    """尝试把 audio_path 合并到 video_path 上，成功则原地替换 video_path。"""
    tmp_merged = video_path.with_name(video_path.stem + "_merged_tmp.mp4")
    if merge_audio_video(video_path, audio_path, tmp_merged):
        try:
            os.replace(tmp_merged, video_path)
        except Exception as e:
            print(f"⚠️ 替换失败: {e}")
            try:
                os.remove(tmp_merged)
            except:
                pass
            return False
        print(f"✅ 已合并{label}音频")
        return True
    if os.path.exists(tmp_merged):
        try:
            os.remove(tmp_merged)
        except:
            pass
    return False


def download_video_multi_candidates(video_candidates, orig_audio_candidates,
                                     bgm_candidates, headers, output_path):
    """
    候选探测式下载（头部探测版）：
    1. 对每个候选 URL 先 Range 下载头部 PROBE_HEAD_BYTES 字节，ffprobe 判定流类型：
       - video+audio → 命中，全量下载此候选作为最终视频
       - video only → 只记 URL，暂不全量下载（等确认无 mixed 流再回头下）
       - audio only → 全量下载保存为音频源
       最多探测 MAX_PROBE_CANDIDATES 个候选
    2. 找不到 mixed 流时，回头全量下载第一个纯视频 URL 作为无声基准
    3. 找音频优先级：探测得到的原声 > orig_audio_candidates > 探测得到的BGM > bgm_candidates
    4. 所有探测产生的临时文件通过 finally 统一清理，异常也不会残留
    """
    if not video_candidates:
        return False

    mixed_path = None
    video_only_url = None        # 纯视频候选 URL（不全量下载，只记 URL）
    audio_only_sources = []      # [(path, url, is_bgm)]
    all_probe_files = []

    seen_cand = set()
    ordered_cands = []
    for u in video_candidates:
        if u and u not in seen_cand:
            seen_cand.add(u)
            ordered_cands.append(u)

    try:
        # ====== 阶段1：头部探测每个候选 ======
        probed = 0
        for idx, u in enumerate(ordered_cands, 1):
            if mixed_path:
                break
            if probed >= MAX_PROBE_CANDIDATES:
                print(f"⚠️ 已探测 {probed} 个候选，达到上限 {MAX_PROBE_CANDIDATES}，停止探测")
                break
            probed += 1

            print(f"\n🔎 头部探测 [{idx}/{len(ordered_cands)}]: {u[:90]}...")
            info = probe_remote_streams(u, headers)
            if not info:
                print(f"⚠️ 候选 {idx} 探测失败，跳过")
                continue

            has_v, has_a = info["has_video"], info["has_audio"]
            print(f"   → 视频流: {has_v}, 音频流: {has_a}")

            if has_v and has_a:
                # 命中 mixed 流：全量下载
                print(f"✅ 候选 {idx} 自带音轨，全量下载...")
                tmp = output_path.with_name(output_path.stem + f'.probe{idx}.mp4')
                all_probe_files.append(tmp)
                if ".m3u8" in u.lower() or ".mpd" in u.lower():
                    ok = download_with_ffmpeg(u, tmp)
                else:
                    ok = mt_download_file(u, headers, tmp, num_threads=DOWNLOAD_THREADS)
                if ok and os.path.exists(tmp) and os.path.getsize(tmp) > 10 * 1024:
                    mixed_path = tmp
                else:
                    print(f"⚠️ 候选 {idx} 全量下载失败")
                break

            elif has_v and not has_a:
                if video_only_url is None:
                    video_only_url = u
                    print(f"   纯视频流，暂存 URL（稍后如无 mixed 再全量下载）")

            elif has_a and not has_v:
                # 纯音频流：全量下载，作为音频源
                print(f"   纯音频流，全量下载...")
                tmp = output_path.with_name(output_path.stem + f'.probe{idx}.m4a')
                all_probe_files.append(tmp)
                if mt_download_file(u, headers, tmp, num_threads=8):
                    is_bgm = is_bgm_url(u)
                    audio_only_sources.append((tmp, u, is_bgm))
                    print(f"   已保存为{'BGM' if is_bgm else '原声'}音频源")
                else:
                    print(f"⚠️ 候选 {idx} 音频下载失败")

        # ====== 情况A：找到 mixed 流 ======
        if mixed_path:
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
                os.replace(mixed_path, output_path)
                mixed_path = None
            except Exception as e:
                print(f"❌ 替换失败: {e}")
                return False
            return True

        if not video_only_url:
            print("❌ 未找到可用的视频流")
            return False

        # ====== 情况B：无 mixed 流，回头全量下载纯视频 ======
        print(f"\n📥 没有混合流，全量下载纯视频: {video_only_url[:90]}...")
        tmp_v = output_path.with_name(output_path.stem + '.video_only.mp4')
        all_probe_files.append(tmp_v)
        if ".m3u8" in video_only_url.lower() or ".mpd" in video_only_url.lower():
            ok = download_with_ffmpeg(video_only_url, tmp_v)
        else:
            ok = mt_download_file(video_only_url, headers, tmp_v, num_threads=DOWNLOAD_THREADS)

        if not ok or not os.path.exists(tmp_v) or os.path.getsize(tmp_v) < 10 * 1024:
            print("❌ 纯视频下载失败")
            return False

        try:
            if os.path.exists(output_path):
                os.remove(output_path)
            os.replace(tmp_v, output_path)
        except Exception as e:
            print(f"❌ 替换失败: {e}")
            return False

        # 优先级1：探测得到的原声
        for p, u, is_bgm in audio_only_sources:
            if not is_bgm:
                if _try_merge_and_replace(output_path, p, label="原声"):
                    return True

        # 优先级2：orig_audio_candidates
        for i, u in enumerate(orig_audio_candidates, 1):
            print(f"\n🎵 原声音频 [{i}/{len(orig_audio_candidates)}]: {u[:90]}...")
            at = output_path.with_name(output_path.stem + f'.orig{i}.m4a')
            if mt_download_file(u, headers, at, num_threads=8):
                a_info = probe_media_info(at)
                if a_info and (a_info.get("has_audio") or a_info.get("has_video")):
                    if _try_merge_and_replace(output_path, at, label="原声"):
                        try: os.remove(at)
                        except: pass
                        return True
                if os.path.exists(at):
                    try: os.remove(at)
                    except: pass
            else:
                print(f"⚠️ 原声音频 {i} 下载失败")

        # 优先级3：探测得到的 BGM
        for p, u, is_bgm in audio_only_sources:
            if is_bgm:
                if _try_merge_and_replace(output_path, p, label="BGM"):
                    return True

        # 优先级4：bgm_candidates
        for i, u in enumerate(bgm_candidates, 1):
            print(f"\n🎵 BGM 候选 [{i}/{len(bgm_candidates)}]: {u[:90]}...")
            at = output_path.with_name(output_path.stem + f'.bgm{i}.m4a')
            if mt_download_file(u, headers, at, num_threads=8):
                if _try_merge_and_replace(output_path, at, label="BGM"):
                    try: os.remove(at)
                    except: pass
                    return True
                if os.path.exists(at):
                    try: os.remove(at)
                    except: pass

        print("⚠️ 所有音频候选尝试完毕，保留无声视频")
        return True

    finally:
        for p in all_probe_files:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except:
                    pass


# ==================== 11. 音频分离 ====================
def extract_audio_from_video(video_path, audio_output_path):
    try:
        subprocess.run([
            "ffmpeg", "-i", str(video_path), "-vn",
            "-c:a", "aac", "-b:a", "192k",
            "-y", str(audio_output_path)
        ], check=True, capture_output=True, text=True, timeout=120)
        print(f"✅ 音频分离: {audio_output_path}")
    except subprocess.CalledProcessError as e:
        print(f"❌ 分离失败: {e.stderr[:300]}")
    except Exception as e:
        print(f"❌ ffmpeg: {e}")


# ==================== 12. 单视频处理 ====================
def process_one_url(target_url, output_dir, get_video, get_audio, extract_audio, tag=""):
    video_id = get_video_id(target_url)
    if not video_id:
        print(f"{tag} ❌ 无法获取视频 ID")
        return False, "无法获取 ID"

    print(f"{tag} ✅ 视频 ID: {video_id}")
    timestamp = int(time.time() * 1000)
    download_headers = {
        'User-Agent': MOBILE_UA,
        'Referer': 'https://www.douyin.com/',
    }

    res_data = fetch_media(video_id)

    if not res_data and HAS_YTDLP:
        cookie_file = os.path.join(os.getcwd(), "douyin_cookies.txt")
        if os.path.exists(cookie_file):
            v_path = output_dir / f"real_video_{video_id}_{timestamp}.mp4"
            print(f"{tag} 🚀 yt-dlp 直接下载...")
            if download_with_ytdlp(video_id, v_path, cookie_file):
                print(f"{tag} ✨ yt-dlp 成功: {v_path.resolve()}")
                if extract_audio and check_ffmpeg_available():
                    extract_audio_from_video(
                        v_path,
                        output_dir / f"extracted_audio_{video_id}_{timestamp}.m4a"
                    )
                append_history({
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "video_id": video_id, "file": str(v_path), "method": "yt-dlp"
                })
                return True, str(v_path)

    if not res_data:
        print(f"{tag} ❌ 所有解析方式失败")
        return False, "解析失败"

    video_candidates = list(dict.fromkeys([v for v in res_data.get("video_candidates", []) if v]))
    orig_audios = list(dict.fromkeys([a for a in res_data.get("original_audio_candidates", []) if a]))
    bgm_audios = list(dict.fromkeys([a for a in res_data.get("bgm_candidates", []) if a]))

    if not orig_audios and not bgm_audios:
        legacy = list(dict.fromkeys([a for a in res_data.get("audio_candidates", []) if a]))
        if res_data.get("audio_is_bgm", False):
            bgm_audios = legacy
        else:
            orig_audios = legacy

    if get_video and video_candidates:
        print(f"{tag} 📥 视频候选 {len(video_candidates)}，原声音频候选 {len(orig_audios)}，BGM 候选 {len(bgm_audios)}")
        v_path = output_dir / f"real_video_{video_id}_{timestamp}.mp4"
        try:
            ok = download_video_multi_candidates(
                video_candidates, orig_audios, bgm_audios,
                download_headers, v_path
            )
            if ok and v_path.exists() and v_path.stat().st_size > 1024:
                size_mb = v_path.stat().st_size / 1024 / 1024
                print(f"{tag} ✨ 视频下载成功: {v_path.resolve()} ({size_mb:.1f} MB)")
                if extract_audio and check_ffmpeg_available():
                    extract_audio_from_video(
                        v_path,
                        output_dir / f"extracted_audio_{video_id}_{timestamp}.m4a"
                    )
                append_history({
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "video_id": video_id, "file": str(v_path),
                    "method": res_data.get("source", "unknown")
                })
                return True, str(v_path)
            else:
                print(f"{tag} ❌ 视频下载失败或过小")
                return False, "下载失败"
        except Exception as e:
            print(f"{tag} ❌ 异常: {e}")
            return False, str(e)

    if get_audio and (orig_audios or bgm_audios):
        print(f"{tag} 📥 仅下载音频...")
        candidate = (orig_audios or bgm_audios)[0]
        a_path = output_dir / f"real_audio_{video_id}_{timestamp}.m4a"
        if mt_download_file(candidate, download_headers, a_path, num_threads=8):
            return True, str(a_path)

    return False, "无候选"


# ==================== 13. 批量调度 ====================
def core_batch_download(urls, output_dir, get_video, get_audio, extract_audio, btn_widget):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'=' * 60}")
    print(f"📋 批量任务: {len(urls)} 个链接")
    print(f"📁 保存目录: {output_dir.resolve()}")
    print(f"⚡ 并发数: {BATCH_CONCURRENCY}")
    print(f"{'=' * 60}\n")

    if check_ffmpeg_available():
        print("✅ ffmpeg 已就绪\n")
    else:
        print("⚠️ 未检测到 ffmpeg\n")

    results = [None] * len(urls)
    success_count = [0]
    counter_lock = threading.Lock()
    sem = threading.Semaphore(BATCH_CONCURRENCY)

    def worker(idx, url):
        with sem:
            tag = f"[{idx + 1}/{len(urls)}]"
            print(f"\n{tag} ▶️ 开始处理: {url}")
            try:
                target_url = extract_best_url(url)
                ok, info = process_one_url(target_url, output_dir, get_video, get_audio,
                                            extract_audio, tag=tag)
                results[idx] = (ok, info)
                with counter_lock:
                    if ok:
                        success_count[0] += 1
            except Exception as e:
                print(f"{tag} ❌ 异常: {e}")
                results[idx] = (False, str(e))

    threads = []
    for i, url in enumerate(urls):
        t = threading.Thread(target=worker, args=(i, url))
        t.daemon = True
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    print(f"\n{'=' * 60}")
    print(f"🏁 批量任务完成：成功 {success_count[0]}/{len(urls)}")
    print(f"{'=' * 60}\n")
    for i, r in enumerate(results):
        if r is None:
            continue
        ok, info = r
        mark = "✅" if ok else "❌"
        print(f"  {mark} [{i + 1}] {urls[i][:60]}... → {info if ok else '失败: ' + info}")

    btn_widget.after(0, lambda: btn_widget.config(state=tk.NORMAL))


# ==================== 14. GUI ====================
def start_download_thread():
    raw = url_input.get("1.0", tk.END).strip()
    if not raw:
        messagebox.showwarning("提示", "请先输入分享链接！")
        return

    urls = extract_all_urls(raw)
    if not urls:
        urls = [extract_best_url(raw)]

    if not urls:
        messagebox.showwarning("提示", "未识别到有效链接")
        return

    get_video = video_var.get()
    get_audio = audio_var.get()
    extract_audio_flag = extract_audio_var.get()
    if not get_video and not get_audio:
        video_var.set(True)
        get_video = True

    out_dir = output_dir_var.get().strip() or "downloaded_assets"

    download_btn.config(state=tk.DISABLED)
    log_box.delete(1.0, tk.END)

    task = threading.Thread(
        target=core_batch_download,
        args=(urls, out_dir, get_video, get_audio, extract_audio_flag, download_btn)
    )
    task.daemon = True
    task.start()


def browse_dir():
    d = filedialog.askdirectory(title="选择保存目录")
    if d:
        output_dir_var.set(d)


def show_history():
    h = load_history()
    if not h:
        messagebox.showinfo("历史记录", "暂无记录")
        return
    lines = [f"{e.get('time', '')} | {e.get('video_id', '')} | {e.get('method', '')} | {e.get('file', '')}"
             for e in h[-30:]]
    messagebox.showinfo("最近 30 条下载记录", "\n".join(lines))


def on_closing():
    sys.stdout = sys.__stdout__
    root.destroy()


# ==================== 15. 主界面 ====================
if __name__ == "__main__":
    root = tk.Tk()
    root.title("短视频批量下载器 v3.4")
    root.geometry("820x740")
    root.minsize(720, 620)
    root.protocol("WM_DELETE_WINDOW", on_closing)

    BG_DEEP_BLUE = "#0A192F"
    BG_CONTAINER = "#172A45"
    TEXT_WHITE = "#E6F1FF"
    TEXT_CYAN = "#64FFDA"
    BTN_BLUE = "#0052CC"

    root.configure(bg=BG_DEEP_BLUE)
    root.rowconfigure(5, weight=1)
    root.columnconfigure(0, weight=1)

    tk.Label(root, text="短视频批量逆向解析引擎 v3.4",
             font=("Helvetica", 15, "bold"), bg=BG_DEEP_BLUE, fg=TEXT_CYAN
             ).grid(row=0, column=0, pady=12, padx=30, sticky="w")

    input_frame = tk.Frame(root, bg=BG_DEEP_BLUE)
    input_frame.grid(row=1, column=0, sticky="we", padx=30, pady=5)
    input_frame.columnconfigure(1, weight=1)
    tk.Label(input_frame, text="视频链接（支持多行批量）:",
             font=("Microsoft YaHei", 10), bg=BG_DEEP_BLUE, fg=TEXT_WHITE
             ).grid(row=0, column=0, padx=(0, 5), sticky="nw")

    url_input = tk.Text(input_frame, font=("Microsoft YaHei", 10), height=4,
                        bg=BG_CONTAINER, fg=TEXT_WHITE, insertbackground=TEXT_WHITE,
                        relief=tk.FLAT, wrap=tk.WORD)
    url_input.grid(row=0, column=1, sticky="we")
    url_input.focus()

    dir_frame = tk.Frame(root, bg=BG_DEEP_BLUE)
    dir_frame.grid(row=2, column=0, sticky="we", padx=30, pady=5)
    dir_frame.columnconfigure(1, weight=1)
    tk.Label(dir_frame, text="保存目录:", font=("Microsoft YaHei", 10),
             bg=BG_DEEP_BLUE, fg=TEXT_WHITE).grid(row=0, column=0, padx=(0, 5))
    output_dir_var = tk.StringVar(value=str(Path.cwd() / "downloaded_assets"))
    tk.Entry(dir_frame, textvariable=output_dir_var, font=("Microsoft YaHei", 10),
             bg=BG_CONTAINER, fg=TEXT_WHITE, insertbackground=TEXT_WHITE,
             relief=tk.FLAT).grid(row=0, column=1, sticky="we", ipady=4)
    tk.Button(dir_frame, text="浏览", font=("Microsoft YaHei", 9),
              bg=BG_CONTAINER, fg=TEXT_CYAN, relief=tk.FLAT,
              command=browse_dir).grid(row=0, column=2, padx=(5, 0))
    tk.Button(dir_frame, text="📜 历史记录", font=("Microsoft YaHei", 9),
              bg=BG_CONTAINER, fg=TEXT_CYAN, relief=tk.FLAT,
              command=show_history).grid(row=0, column=3, padx=(5, 0))

    control_frame = tk.Frame(root, bg=BG_DEEP_BLUE)
    control_frame.grid(row=3, column=0, sticky="we", padx=30, pady=8)

    video_var = tk.BooleanVar(value=True)
    audio_var = tk.BooleanVar(value=False)
    extract_audio_var = tk.BooleanVar(value=False)

    tk.Checkbutton(control_frame, text="提取无水印视频", variable=video_var,
                   font=("Microsoft YaHei", 10), bg=BG_DEEP_BLUE, fg=TEXT_CYAN,
                   selectcolor=BG_CONTAINER, activebackground=BG_DEEP_BLUE,
                   activeforeground=TEXT_CYAN).grid(row=0, column=0, padx=10, pady=3)
    tk.Checkbutton(control_frame, text="提取独立音频（BGM）", variable=audio_var,
                   font=("Microsoft YaHei", 10), bg=BG_DEEP_BLUE, fg=TEXT_CYAN,
                   selectcolor=BG_CONTAINER, activebackground=BG_DEEP_BLUE,
                   activeforeground=TEXT_CYAN).grid(row=0, column=1, padx=10, pady=3)
    tk.Checkbutton(control_frame, text="从视频分离音频", variable=extract_audio_var,
                   font=("Microsoft YaHei", 10), bg=BG_DEEP_BLUE, fg=TEXT_CYAN,
                   selectcolor=BG_CONTAINER, activebackground=BG_DEEP_BLUE,
                   activeforeground=TEXT_CYAN).grid(row=0, column=2, padx=10, pady=3)

    btn_container = tk.Frame(root, bg=BG_DEEP_BLUE)
    btn_container.grid(row=4, column=0, sticky="we", pady=(5, 10))
    btn_container.columnconfigure(0, weight=1)

    download_btn = tk.Button(btn_container, text="🚀 一键批量解析并下载",
                             font=("Microsoft YaHei", 11, "bold"),
                             bg=BTN_BLUE, fg=TEXT_WHITE,
                             activebackground="#0040A3", activeforeground=TEXT_WHITE,
                             relief=tk.FLAT, command=start_download_thread)
    download_btn.grid(row=0, column=0, ipadx=40, ipady=8, padx=30, sticky="we")

    log_frame = tk.Frame(root, bg=BG_DEEP_BLUE)
    log_frame.grid(row=5, column=0, sticky="nsew", padx=30, pady=(5, 20))
    log_frame.rowconfigure(1, weight=1)
    log_frame.columnconfigure(0, weight=1)

    tk.Label(log_frame, text="实时控制台输出日志:",
             font=("Microsoft YaHei", 9), bg=BG_DEEP_BLUE, fg=TEXT_WHITE
             ).grid(row=0, column=0, sticky="w", pady=(0, 5))

    log_box = scrolledtext.ScrolledText(log_frame, font=("Consolas", 10),
                                        bg=BG_CONTAINER, fg=TEXT_WHITE, relief=tk.FLAT)
    log_box.grid(row=1, column=0, sticky="nsew")

    sys.stdout = StdoutRedirector(log_box, root)
    root.mainloop()