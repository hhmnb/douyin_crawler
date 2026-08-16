import os
import re
import sys
import time
import json
import subprocess
import urllib.parse
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import tkinter as tk
from tkinter import scrolledtext, messagebox
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


# ==================== 1. 标准输出重定向安全引擎 ====================
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


# ==================== 2. 链接清洗 ====================
def extract_best_url(raw_text):
    priority_domains = [
        r'https?://www\.iesdouyin\.com/share/video/\d{19}',
        r'https?://www\.douyin\.com/video/\d{19}',
        r'https?://v\.douyin\.com/[A-Za-z0-9_-]+/?',
    ]
    for pat in priority_domains:
        m = re.search(pat, raw_text)
        if m:
            return m.group(0).rstrip('/')
    urls = re.findall(r'https?://[^\s]+', raw_text)
    return urls[-1] if urls else raw_text.strip()


# ==================== 3. 核心解析 ====================
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


def parse_universal_data(html_text):
    match = re.search(r'id="__UNIVERSAL_DATA__"[^>]*>([\s\S]*?)</script>', html_text)
    if match:
        try:
            raw = urllib.parse.unquote(match.group(1))
            return json.loads(raw)
        except:
            pass
    return None


def extract_media_from_json(data):
    result = {"video_url": None, "audio_url": None, "images": []}

    def _search(obj):
        if isinstance(obj, dict):
            video = obj.get("video") or obj.get("video_info")
            if isinstance(video, dict):
                play_addr = video.get("play_addr") or video.get("play_addr_h264")
                if isinstance(play_addr, dict):
                    urls = play_addr.get("url_list")
                    if urls and isinstance(urls, list):
                        result["video_url"] = clean_url(urls[0])
                elif isinstance(play_addr, list) and play_addr:
                    result["video_url"] = clean_url(play_addr[0])

            music = obj.get("music") or obj.get("music_info")
            if isinstance(music, dict):
                play_url = music.get("play_url") or music.get("play_info")
                if isinstance(play_url, dict):
                    urls = play_url.get("url_list")
                    if urls and isinstance(urls, list):
                        result["audio_url"] = clean_url(urls[0])

            images = obj.get("images")
            if isinstance(images, list):
                for img in images:
                    if isinstance(img, dict):
                        url_list = img.get("url_list")
                        if url_list and isinstance(url_list, list):
                            result["images"].append(clean_url(url_list[0]))
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
        render_match = re.search(r'id="RENDER_DATA"[^>]*>([\s\S]*?)</script>', html_text)
        if render_match:
            try:
                data = json.loads(urllib.parse.unquote(render_match.group(1).strip()))
                print(f"💡 [{source_name}] 成功解密 RENDER_DATA")
            except:
                pass
    if not data:
        router_match = re.search(r'id="_ROUTER_DATA"[^>]*>([\s\S]*?)</script>', html_text)
        if router_match:
            try:
                data = json.loads(router_match.group(1).strip())
                print(f"💡 [{source_name}] 成功解密 _ROUTER_DATA")
            except:
                pass

    if data:
        res = extract_media_from_json(data)
        if res and (res['video_url'] or res['audio_url'] or res['images']):
            res['type'] = 'image' if (not res['video_url'] and res['images']) else 'video'
            return res

    video_matches = re.findall(r'play_addr.*?url_list.*?["\'](https?:[^"\']+)["\']', html_text)
    if not video_matches:
        video_matches = re.findall(r'["\'](https?://aweme\.snssdk\.com/aweme/v1/play/[^"\']+)["\']', html_text)
    if not video_matches:
        video_matches = re.findall(r'["\'](https?://[^"\']+video/play[^"\']+)["\']', html_text)
    video_url = clean_url(video_matches[0]) if video_matches else None

    audio_matches = re.findall(r'music.*?play_url.*?url_list.*?["\'](https?:[^"\']+)["\']', html_text)
    audio_url = clean_url(audio_matches[0]) if audio_matches else None

    image_matches = re.findall(r'["\'](https?://[^"\']+?\.(?:jpg|jpeg|png|webp|gif)\?[^"\']*)["\']', html_text)
    images = [clean_url(img) for img in image_matches]

    if video_url or audio_url or images:
        return {
            "type": "image" if (not video_url and images) else "video",
            "video_url": video_url,
            "audio_url": audio_url,
            "images": images
        }
    return None


def fetch_video_via_requests(video_id):
    share_url = f"https://www.iesdouyin.com/share/video/{video_id}/"
    headers_mobile = {
        'User-Agent': 'Mozilla/5.0 (Linux; Android 14; K) AppleWebKit/537.36',
        'Accept': 'text/html,application/xhtml+xml',
    }
    try:
        resp = requests.get(share_url, headers=headers_mobile, timeout=12)
        res = try_parse_html(resp.text, "移动端H5")
        if res:
            return res
    except:
        pass

    pc_url = f"https://www.douyin.com/video/{video_id}"
    headers_pc = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }
    try:
        resp = requests.get(pc_url, headers=headers_pc, timeout=12)
        res = try_parse_html(resp.text, "PC端")
        if res:
            return res
    except:
        pass

    return None


# ==================== 从浏览器提取 Cookies ====================
def get_cookies_from_browser():
    if not HAS_PLAYWRIGHT:
        return None
    user_data_dir = os.path.join(os.getcwd(), "douyin_browser_data")
    cookie_file = os.path.join(os.getcwd(), "douyin_cookies.txt")
    print("🍪 从浏览器提取 cookies...")
    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=False,
                viewport={"width": 1280, "height": 720},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                args=["--disable-blink-features=AutomationControlled"]
            )
        except:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
        page = context.new_page()
        try:
            page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30000)
            print("👆 若未登录，请在浏览器窗口手动登录抖音，程序将自动检测...")
            for _ in range(10):
                time.sleep(2)
                cookies = context.cookies()
                if any(c.get('name') == 'sessionid' for c in cookies):
                    print("✅ 检测到登录状态，提取 cookies...")
                    break
            else:
                print("⚠️ 未检测到 sessionid，可能未登录，将尝试提取当前 cookies...")

            cookies = context.cookies()
            with open(cookie_file, 'w', encoding='utf-8') as f:
                f.write("# Netscape HTTP Cookie File\n")
                for c in cookies:
                    domain = c.get('domain', '')
                    if not domain.startswith('.'):
                        domain = '.' + domain
                    secure = 'TRUE' if c.get('secure', False) else 'FALSE'
                    expires_raw = c.get('expires', 0)
                    if expires_raw is None or expires_raw < 0:
                        expires = '0'
                    else:
                        expires = str(int(expires_raw))
                    path = c.get('path', '/')
                    name = c.get('name', '')
                    value = c.get('value', '')
                    if not name or not value:
                        continue
                    f.write(f"{domain}\tTRUE\t{path}\t{secure}\t{expires}\t{name}\t{value}\n")
            print(f"✅ Cookies 已保存到 {cookie_file}")
            return cookie_file
        except Exception as e:
            print(f"⚠️ 提取 cookies 失败: {e}")
            return None
        finally:
            context.close()


# ==================== yt-dlp 直接下载 ====================
def download_with_ytdlp(video_id, output_path, cookies_file=None):
    if not HAS_YTDLP:
        print("❌ 未安装 yt-dlp，无法使用此方式。")
        return False
    url = f"https://www.douyin.com/video/{video_id}"
    ydl_opts = {
        'outtmpl': str(output_path),
        'quiet': True,
        'no_warnings': True,
        'format': 'best',
        'merge_output_format': 'mp4',
    }
    if cookies_file and os.path.exists(cookies_file):
        ydl_opts['cookiefile'] = cookies_file
        print("🍪 使用 cookies 文件进行下载...")
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        print(f"✅ yt-dlp 下载成功: {output_path}")
        return True
    except Exception as e:
        print(f"⚠️ yt-dlp 下载失败: {e}")
        return False


# ==================== Playwright 全自动提取（备选方案） ====================
def fetch_video_via_browser(video_id):
    if not HAS_PLAYWRIGHT:
        print("❌ 未安装 Playwright，请执行：pip install playwright && playwright install chromium")
        return None

    user_data_dir = os.path.join(os.getcwd(), "douyin_browser_data")
    pc_url = f"https://www.douyin.com/video/{video_id}"
    video_url = None
    audio_url = None
    network_candidates = []
    audio_candidates = []
    media_playlist = []

    print("🌐 启动浏览器（持久化登录）...")
    browser = None
    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=False,
                viewport={"width": 1280, "height": 720},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                args=["--disable-blink-features=AutomationControlled"]
            )
        except:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )

        page = context.new_page()

        def on_response(response):
            url = response.url
            lower_url = url.lower()
            content_type = response.headers.get("content-type", "").lower()

            if any(ext in content_type for ext in [
                "text/html", "text/css", "application/javascript",
                "image/jpeg", "image/png", "image/webp", "image/gif",
                "application/json", "application/xml", "text/plain"
            ]):
                return

            is_media = False
            type_label = "unknown"

            if ".mp4" in lower_url:
                is_media = True
                type_label = "mp4"
            elif ".m4s" in lower_url:
                is_media = True
                type_label = "m4s"
            elif ".m4a" in lower_url or ".mp3" in lower_url:
                is_media = True
                type_label = "audio"
            elif ".mpd" in lower_url or ".m3u8" in lower_url:
                is_media = True
                type_label = "playlist"
            elif any(ct in content_type for ct in [
                "video", "audio", "application/octet-stream",
                "application/vnd.apple.mpegurl", "application/dash+xml"
            ]):
                is_media = True
                if "mpegurl" in content_type or "dash+xml" in content_type:
                    type_label = "playlist"
                elif "audio" in content_type:
                    type_label = "audio"
                else:
                    type_label = "media"
            elif any(k in lower_url for k in [
                "aweme/v1/play", "video_id", "play_addr", "douyinvod",
                "v3-dy", "aweme.snssdk.com/aweme/v1/play",
                "byteicdn", "bytegecko", "douyinstatic", "douyinpic",
                "audio", "music", "song", "sound"
            ]):
                is_media = True
                if any(k in lower_url for k in ["audio", "music", "song", "sound"]):
                    type_label = "audio"
                else:
                    type_label = "media"

            if is_media:
                cl = response.headers.get("content-length")
                size = int(cl) if cl and cl.isdigit() else 0
                if type_label == "playlist":
                    media_playlist.append(url)
                    print(f"   [播放列表] {url[:120]}...")
                elif type_label in ("audio", "m4a", "mp3"):
                    audio_candidates.append((size, url, type_label))
                    print(f"   [音频候选] {url[:120]}... size={size//1024}KB, type={type_label}")
                else:
                    network_candidates.append((size, url, type_label))
                    print(f"   [媒体候选] {url[:120]}... size={size//1024}KB, type={type_label}")

        page.on("response", on_response)

        def check_playing():
            return page.evaluate("""
                () => {
                    const videos = document.querySelectorAll('video');
                    for (const v of videos) {
                        if (!v.paused && v.readyState >= 2 && v.currentTime > 0) return true;
                    }
                    return false;
                }
            """)

        try:
            page.goto(pc_url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_selector("video", timeout=60000)
                print("✅ 检测到视频播放器")
            except:
                print("❌ 超时未检测到 video 元素，可能未完成登录验证")
                context.close()
                if browser:
                    browser.close()
                return None

            print("🎬 开始自动播放循环（最多 5 次）...")
            played = False
            for i in range(5):
                print(f"   尝试 {i+1}/5")
                try:
                    main_video = page.locator("video").first
                    box = main_video.bounding_box()
                    if box:
                        x = box['x'] + box['width'] // 2
                        y = box['y'] + box['height'] // 2
                        page.mouse.click(x, y)
                        print("     已点击视频中心")
                    else:
                        main_video.click()
                        print("     点击了 video 元素")
                except Exception as e:
                    print(f"     点击失败: {e}")

                try:
                    page.evaluate("""
                        () => {
                            const videos = document.querySelectorAll('video');
                            videos.forEach(v => {
                                if (v.paused) v.play();
                            });
                        }
                    """)
                    print("     调用所有 video 的 play()")
                except:
                    pass

                time.sleep(2)
                if check_playing():
                    played = True
                    print("✅ 视频已成功播放")
                    break
                else:
                    print("     当前仍未播放，继续尝试...")

            if not played:
                print("❌ 多次自动播放尝试失败，请手动点击浏览器页面中的视频区域以播放，程序将等待...")
                for _ in range(20):
                    time.sleep(1)
                    if check_playing():
                        played = True
                        print("✅ 用户已手动播放")
                        break
                if not played:
                    print("❌ 用户未播放，放弃。")
                    context.close()
                    if browser:
                        browser.close()
                    return None

            print("⏳ 等待视频地址出现（最多 30 秒）...")
            for i in range(30):
                try:
                    src = page.evaluate(
                        "() => { const v = document.querySelector('video'); return v ? (v.currentSrc || v.src) : null; }"
                    )
                    if src and src.startswith("http") and not src.startswith("blob:"):
                        if not any(k in src.lower() for k in ["effect", "sticker", "byteeffect"]):
                            video_url = src
                            print(f"🎯 从 video 元素获取: {video_url[:100]}...")
                            break
                except:
                    pass

                if media_playlist:
                    video_url = media_playlist[0]
                    print(f"🎼 使用播放列表下载: {video_url[:100]}...")
                    break

                if network_candidates:
                    preferred = []
                    for cand in network_candidates:
                        size, url, typ = cand
                        if typ == "mp4" and any(k in url.lower() for k in [
                            "aweme/v1/play", "douyinvod", "v3-dy", "video", "play"
                        ]):
                            preferred.append(cand)
                    if preferred:
                        preferred.sort(reverse=True, key=lambda x: x[0])
                        video_url = preferred[0][1]
                        print(f"🌐 从优选视频候选中选取: {video_url[:100]}... (size={preferred[0][0]//1024}KB)")
                        break
                    large_candidates = [c for c in network_candidates if c[2] in ("mp4", "media") and c[0] > 500*1024]
                    if large_candidates:
                        large_candidates.sort(reverse=True, key=lambda x: x[0])
                        video_url = large_candidates[0][1]
                        print(f"🌐 从较大候选中选取: {video_url[:100]}... (size={large_candidates[0][0]//1024}KB)")
                        break
                    network_candidates.sort(reverse=True, key=lambda x: x[0])
                    video_url = network_candidates[0][1]
                    print(f"⚠️ 未找到理想视频，使用最大候选: {video_url[:100]}... (size={network_candidates[0][0]//1024}KB)")
                    break

                if i % 10 == 0 and i > 0:
                    print(f"   ⏳ 已等待 {i} 秒，候选总数: {len(network_candidates)}，播放列表: {len(media_playlist)}")

                time.sleep(1)

            if not video_url and media_playlist:
                video_url = media_playlist[0]
                print(f"🎼 使用播放列表下载: {video_url[:100]}...")

            if audio_candidates:
                audio_candidates.sort(reverse=True, key=lambda x: x[0])
                audio_url = audio_candidates[0][1]
                print(f"🎵 从音频候选中选取: {audio_url[:100]}... (size={audio_candidates[0][0]//1024}KB)")

        except Exception as e:
            print(f"⚠️ 页面异常: {e}")
        finally:
            context.close()
            if browser:
                browser.close()

    if video_url:
        print(f"✅ 最终视频地址: {video_url[:100]}...")
        return {"video_url": video_url, "audio_url": audio_url}
    else:
        print("❌ 未能获取视频地址，请确认视频已开始播放。")
        return None


# ==================== 视频 ID 提取 ====================
def get_video_id(input_url):
    try:
        resp = requests.get(input_url, allow_redirects=True, timeout=12)
        final_url = resp.url
        m = re.search(r'video/(\d{19})', final_url)
        if m:
            return m.group(1)
        oembed = f"https://www.douyin.com/oembed?url={input_url}"
        r = requests.get(oembed, timeout=10)
        if r.status_code == 200:
            vid = r.json().get('video_id')
            if vid:
                return str(vid)
    except:
        pass
    return None


def fetch_media(video_id):
    # 1. requests
    print("📡 尝试 requests 解析...")
    res = fetch_video_via_requests(video_id)
    if res:
        return res

    # 2. yt-dlp（无 cookies）
    if HAS_YTDLP:
        print("🚀 requests 解析失败，尝试 yt-dlp 提取...")
        yt_res = fetch_video_via_ytdlp(video_id)
        if yt_res:
            return {"type": "video", "video_url": yt_res.get("video_url"),
                    "audio_url": yt_res.get("audio_url"), "images": []}
    else:
        print("ℹ️ 未安装 yt-dlp，跳过此方式。可执行 `pip install yt-dlp` 安装。")

    # 3. cookies + yt-dlp
    cookie_file = os.path.join(os.getcwd(), "douyin_cookies.txt")
    if not os.path.exists(cookie_file) and HAS_PLAYWRIGHT:
        print("🍪 未找到 cookies 文件，尝试从浏览器提取...")
        cookie_file = get_cookies_from_browser()
        if cookie_file and HAS_YTDLP:
            print("🚀 使用刚提取的 cookies 重新尝试 yt-dlp...")
            yt_res = fetch_video_via_ytdlp(video_id, cookies_file=cookie_file)
            if yt_res:
                return {"type": "video", "video_url": yt_res.get("video_url"),
                        "audio_url": yt_res.get("audio_url"), "images": []}

    # 4. 浏览器自动化
    print("🚀 启用浏览器自动化...")
    browser_res = fetch_video_via_browser(video_id)
    if browser_res:
        return {"type": "video", "video_url": browser_res.get("video_url"),
                "audio_url": browser_res.get("audio_url"), "images": []}

    print("❌ 所有方式均无法获取视频地址")
    return None


# ==================== 6. 下载引擎（直链 + 播放列表 + 音视频合并） ====================
def mt_download_file(url, headers, output_path, num_threads=4):
    session = requests.Session()
    download_headers = headers.copy()
    if 'Host' in download_headers:
        del download_headers['Host']

    total_size = 0
    try:
        head_res = session.head(url, headers=download_headers, timeout=10, allow_redirects=True)
        total_size = int(head_res.headers.get('content-length', 0))
    except:
        try:
            with session.get(url, headers=download_headers, stream=True, timeout=10) as r:
                total_size = int(r.headers.get('content-length', 0))
        except:
            total_size = 0

    def fallback_single_thread(reason_msg):
        print(f"⚠️ {reason_msg}")
        print("🔄 正在启用[安全单线程防风控流]重新拉取资源...")
        for i in range(num_threads):
            tp = f"{output_path}.part{i}"
            if os.path.exists(tp):
                os.remove(tp)
        try:
            with session.get(url, headers=download_headers, stream=True, timeout=30) as r:
                r.raise_for_status()
                with open(output_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1024*1024):
                        if chunk:
                            f.write(chunk)
            print("✨ 极速单线程下载已安全完成！")
            return True
        except Exception as ex:
            print(f"❌ 终极单线程下载也失败了: {ex}")
            return False

    if total_size <= 500 * 1024:
        return fallback_single_thread("资源较小或长度未知")

    test_headers = download_headers.copy()
    test_headers['Range'] = 'bytes=0-10'
    try:
        test_res = session.get(url, headers=test_headers, timeout=8)
        if test_res.status_code != 206:
            return fallback_single_thread("检测到 CDN 服务器不支持分块传输 (未返回 206)")
    except Exception as e:
        return fallback_single_thread(f"CDN 探测失败或超时 ({e})")

    print(f"📊 媒体资源总大小: {total_size // 1024} KB，已开启 {num_threads} 线程并发暴风加速...")

    part_size = total_size // num_threads
    futures = []
    progress_lock = threading.Lock()
    thread_progress = [0] * num_threads

    def download_range(start, end, part_num, temp_path):
        range_headers = download_headers.copy()
        range_headers['Range'] = f'bytes={start}-{end}'
        for attempt in range(3):
            try:
                with progress_lock:
                    thread_progress[part_num] = 0
                with session.get(url, headers=range_headers, stream=True, timeout=12) as r:
                    if r.status_code == 206:
                        with open(temp_path, 'wb') as f:
                            for chunk in r.iter_content(chunk_size=64*1024):
                                if chunk:
                                    f.write(chunk)
                                    with progress_lock:
                                        thread_progress[part_num] += len(chunk)
                                        current_total = sum(thread_progress)
                                        if current_total % (1024 * 1024) < 64 * 1024 or current_total == total_size:
                                            percent = (current_total / total_size) * 100
                                            percent = min(percent, 100.0)
                                            print(f" 🚀 并发暴风加速中... 🚀 整体进度: {percent:.1f}% ({current_total//1024}KB / {total_size//1024}KB)")
                        return True
            except:
                time.sleep(1.5)
        return False

    temp_files = []
    success = False
    try:
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            for i in range(num_threads):
                start = i * part_size
                end = total_size - 1 if i == num_threads - 1 else (start + part_size - 1)
                temp_path = f"{output_path}.part{i}"
                temp_files.append(temp_path)
                futures.append(executor.submit(download_range, start, end, i, temp_path))
            results = [f.result() for f in as_completed(futures)]

        if all(results):
            with open(output_path, 'wb') as total_file:
                for temp_path in temp_files:
                    with open(temp_path, 'rb') as pf:
                        total_file.write(pf.read())
            success = True
            print(" 🚀 并发暴风加速中... 🚀 整体进度: 100.0% 已完成！")
        else:
            raise RuntimeError("部分分片下载失败")
    except Exception as mte:
        return fallback_single_thread(f"多线程下载失败 ({mte})")
    finally:
        for temp_path in temp_files:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except:
                    pass
        if not success and os.path.exists(output_path):
            try:
                os.remove(output_path)
            except:
                pass
    return success


def download_with_ffmpeg(playlist_url, output_path):
    if not check_ffmpeg_available():
        print("❌ 未安装 ffmpeg，无法下载播放列表。请安装 ffmpeg 后重试。")
        return False
    print(f"🎼 使用 ffmpeg 下载播放列表: {playlist_url[:100]}...")
    cmd = [
        "ffmpeg",
        "-y",
        "-i", playlist_url,
        "-c", "copy",
        str(output_path)
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        print(f"✅ ffmpeg 下载成功: {output_path}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ ffmpeg 下载失败: {e.stderr}")
        return False
    except Exception as e:
        print(f"❌ 调用 ffmpeg 时发生错误: {e}")
        return False


def has_audio_stream(file_path):
    if not check_ffmpeg_available():
        return True
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=codec_type",
        "-of", "csv=p=0",
        str(file_path)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return "audio" in result.stdout.lower()
    except:
        return True


def merge_audio_video(video_path, audio_path, output_path):
    if not check_ffmpeg_available():
        print("⚠️ 未安装 ffmpeg，无法合并音频，视频将保持无声。")
        return False
    print(f"🔗 合并音视频...")
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-c:v", "copy",
        "-c:a", "aac",
        "-map", "0:v:0",
        "-map", "1:a:0",
        str(output_path)
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        print(f"✅ 合并完成: {output_path}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ 合并失败: {e.stderr}")
        return False
    except Exception as e:
        print(f"❌ 调用 ffmpeg 时发生错误: {e}")
        return False


def download_video_smart(url, headers, output_path, audio_url=None):
    if ".m3u8" in url.lower() or ".mpd" in url.lower():
        success = download_with_ffmpeg(url, output_path)
    else:
        success = mt_download_file(url, headers, output_path, num_threads=16)

    if not success:
        return False

    if has_audio_stream(output_path):
        print("✅ 视频已包含音轨，无需合并。")
        return True

    if audio_url:
        audio_temp = output_path.with_suffix('.audio_tmp.m4a')
        print("🎵 下载独立音频...")
        audio_success = mt_download_file(audio_url, headers, audio_temp, num_threads=8)
        if audio_success:
            final_output = output_path.with_name(output_path.stem + "_merged.mp4")
            if merge_audio_video(output_path, audio_temp, final_output):
                os.replace(final_output, output_path)
                print("✅ 视频已合并音频。")
                try:
                    os.remove(audio_temp)
                except:
                    pass
                return True
            else:
                print("⚠️ 音频合并失败，视频可能无声。")
                try:
                    os.remove(audio_temp)
                except:
                    pass
                return True
        else:
            print("⚠️ 独立音频下载失败，视频可能无声。")
            return True
    else:
        print("⚠️ 未捕获到独立音频，视频可能无声。")
    return True


# ==================== 7. ffmpeg 音频分离工具 ====================
def check_ffmpeg_available():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except:
        return False


def extract_audio_from_video(video_path, audio_output_path):
    cmd = [
        "ffmpeg",
        "-i", str(video_path),
        "-vn",
        "-acodec", "libmp3lame",
        "-q:a", "2",
        "-y",
        str(audio_output_path)
    ]
    try:
        print("🎧 正在从视频中分离音频...")
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        print(f"✅ 音频分离成功 -> {audio_output_path}")
    except subprocess.CalledProcessError as e:
        print(f"❌ 音频分离失败: {e.stderr}")
    except Exception as e:
        print(f"❌ 调用 ffmpeg 时发生错误: {e}")


# ==================== 8. 业务调度控制台 ====================
def core_parse_and_download(raw_input, get_video, get_audio, extract_audio, btn_widget):
    output_dir = Path("downloaded_assets")
    output_dir.mkdir(exist_ok=True)

    target_url = extract_best_url(raw_input)
    print(f"🔗 解析地址: {target_url}")

    video_id = get_video_id(target_url)
    if not video_id:
        print("❌ 无法获取视频 ID")
        btn_widget.after(0, lambda: btn_widget.config(state=tk.NORMAL))
        return

    print(f"✅ 视频 ID: {video_id}")

    # 优先使用 yt-dlp 直接下载完整视频（含音频）
    cookie_file = os.path.join(os.getcwd(), "douyin_cookies.txt")
    if HAS_YTDLP and os.path.exists(cookie_file):
        timestamp = int(time.time())
        v_path = output_dir / f"real_video_{timestamp}.mp4"
        print("🚀 尝试使用 yt-dlp 直接下载完整视频...")
        if download_with_ytdlp(video_id, v_path, cookie_file):
            print(f"✨ yt-dlp 下载成功: {v_path.resolve()}")
            if extract_audio and check_ffmpeg_available():
                audio_from_video = output_dir / f"extracted_audio_{timestamp}.mp3"
                extract_audio_from_video(v_path, audio_from_video)
            print("🏁 [100% 自动绕过机制已执行完毕]\n" + "=" * 30)
            btn_widget.after(0, lambda: btn_widget.config(state=tk.NORMAL))
            return
        else:
            print("⚠️ yt-dlp 下载失败，回退到浏览器自动化...")

    # 原有流程
    res_data = fetch_media(video_id)
    if not res_data:
        print("❌ 解析失败，首次使用请先在浏览器窗口中完成登录验证。")
        btn_widget.after(0, lambda: btn_widget.config(state=tk.NORMAL))
        return

    timestamp = int(time.time())
    download_headers = {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1',
        'Referer': 'https://www.douyin.com/',
    }

    if get_video:
        if res_data.get("video_url"):
            video_url = res_data["video_url"].replace("playwm", "play")
            audio_url = res_data.get("audio_url")
            print("📥 正在下载无水印视频...")
            v_path = output_dir / f"real_video_{timestamp}.mp4"
            try:
                download_video_smart(video_url, download_headers, v_path, audio_url=audio_url)
                print(f"✨ 视频下载成功: {v_path.resolve()}")
            except Exception as e:
                print(f"❌ 下载视频失败: {e}")
                v_path = None

            if v_path and v_path.exists() and extract_audio and check_ffmpeg_available():
                audio_from_video = output_dir / f"extracted_audio_{timestamp}.mp3"
                extract_audio_from_video(v_path, audio_from_video)
            elif v_path and v_path.exists() and extract_audio:
                print("⚠️ 未检测到 ffmpeg，无法自动分离音频。请安装 ffmpeg 后重试。")

        elif res_data.get("images"):
            print(f"📸 检测到图集作品，共 {len(res_data['images'])} 张图片，开始下载...")
            for idx, img_url in enumerate(res_data["images"], 1):
                ext = img_url.split('?')[0].split('.')[-1] if '.' in img_url.split('?')[0] else 'jpg'
                img_name = f"real_image_{timestamp}_{idx}.{ext}"
                img_path = output_dir / img_name
                try:
                    print(f"  [{idx}/{len(res_data['images'])}] 下载: {img_url[:80]}...")
                    with requests.get(img_url, headers=download_headers, stream=True, timeout=30) as r:
                        r.raise_for_status()
                        with open(img_path, 'wb') as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                if chunk:
                                    f.write(chunk)
                    print(f"  ✅ 图片保存至: {img_path.resolve()}")
                except Exception as e:
                    print(f"  ❌ 下载第{idx}张图片失败: {e}")
            print("📸 图集下载全部完成！")
        else:
            print("ℹ️ 该作品未携带视频/图片直链，或无法被提取。")
    else:
        print("ℹ️ 您未勾选【提取无水印资产】，已跳过视频/图片下载。")

    if get_audio:
        audio_url = res_data.get("audio_url")
        if audio_url:
            print("📥 正在下载平台提供的独立 BGM...")
            a_path = output_dir / f"real_bgm_{timestamp}.mp3"
            try:
                mt_download_file(audio_url, download_headers, a_path, num_threads=8)
                print(f"✨ BGM 下载成功: {a_path.resolve()}")
            except Exception as e:
                print(f"❌ BGM 下载异常: {e}")
        else:
            print("ℹ️ 该作品未提供独立 BGM 链接。")

    print("🏁 [100% 自动绕过机制已执行完毕]\n" + "=" * 30)
    btn_widget.after(0, lambda: btn_widget.config(state=tk.NORMAL))


# ==================== 9. GUI 逻辑 ====================
def start_download_thread():
    url = url_input.get().strip()
    if not url:
        messagebox.showwarning("提示", "请先输入分享链接！")
        return

    get_video = video_var.get()
    get_audio = audio_var.get()
    extract_audio_flag = extract_audio_var.get()

    if not get_video and not get_audio:
        video_var.set(True)
        get_video = True
        messagebox.showinfo("提示", "已自动为您勾选【提取无水印资产】")

    download_btn.config(state=tk.DISABLED)
    log_box.delete(1.0, tk.END)

    task_thread = threading.Thread(
        target=core_parse_and_download,
        args=(url, get_video, get_audio, extract_audio_flag, download_btn)
    )
    task_thread.daemon = True
    task_thread.start()


def on_closing():
    sys.stdout = sys.__stdout__
    root.destroy()


# ==================== 10. 主界面 ====================
if __name__ == "__main__":
    root = tk.Tk()
    root.title("短视频多资产提取工具 (支持Playwright全自动)")
    root.geometry("700x620")
    root.minsize(600, 500)

    root.protocol("WM_DELETE_WINDOW", on_closing)

    BG_DEEP_BLUE = "#0A192F"
    BG_CONTAINER = "#172A45"
    TEXT_WHITE = "#E6F1FF"
    TEXT_CYAN = "#64FFDA"
    BTN_BLUE = "#0052CC"

    root.configure(bg=BG_DEEP_BLUE)
    root.rowconfigure(5, weight=1)
    root.columnconfigure(0, weight=1)

    title_label = tk.Label(root, text="短视频逆向极速解析引擎 (浏览器全自动)", font=("Helvetica", 14, "bold"),
                           bg=BG_DEEP_BLUE, fg=TEXT_CYAN)
    title_label.grid(row=0, column=0, pady=15, padx=30, sticky="w")

    input_frame = tk.Frame(root, bg=BG_DEEP_BLUE)
    input_frame.grid(row=1, column=0, sticky="we", padx=30, pady=5)
    input_frame.columnconfigure(1, weight=1)

    input_label = tk.Label(input_frame, text="视频链接:", font=("Microsoft YaHei", 10), bg=BG_DEEP_BLUE, fg=TEXT_WHITE)
    input_label.grid(row=0, column=0, padx=(0, 5))

    url_input = tk.Entry(input_frame, font=("Microsoft YaHei", 10), bg=BG_CONTAINER, fg=TEXT_WHITE,
                         insertbackground=TEXT_WHITE, relief=tk.FLAT)
    url_input.grid(row=0, column=1, sticky="we", ipady=4)
    url_input.focus()

    control_frame = tk.Frame(root, bg=BG_DEEP_BLUE)
    control_frame.grid(row=2, column=0, sticky="we", padx=30, pady=10)

    video_var = tk.BooleanVar(value=True)
    audio_var = tk.BooleanVar(value=False)
    extract_audio_var = tk.BooleanVar(value=False)

    video_cb = tk.Checkbutton(control_frame, text="提取无水印资产 (视频/图片)", variable=video_var,
                              font=("Microsoft YaHei", 10), bg=BG_DEEP_BLUE, fg=TEXT_CYAN,
                              selectcolor=BG_CONTAINER, activebackground=BG_DEEP_BLUE, activeforeground=TEXT_CYAN)
    video_cb.grid(row=0, column=0, padx=10, pady=5)

    audio_cb = tk.Checkbutton(control_frame, text="提取平台独立 BGM (.mp3)", variable=audio_var,
                              font=("Microsoft YaHei", 10), bg=BG_DEEP_BLUE, fg=TEXT_CYAN,
                              selectcolor=BG_CONTAINER, activebackground=BG_DEEP_BLUE, activeforeground=TEXT_CYAN)
    audio_cb.grid(row=0, column=1, padx=10, pady=5)

    extract_audio_cb = tk.Checkbutton(control_frame, text="从下载的视频中分离音频 (需ffmpeg)", variable=extract_audio_var,
                                      font=("Microsoft YaHei", 10), bg=BG_DEEP_BLUE, fg=TEXT_CYAN,
                                      selectcolor=BG_CONTAINER, activebackground=BG_DEEP_BLUE, activeforeground=TEXT_CYAN)
    extract_audio_cb.grid(row=1, column=0, padx=10, pady=5, columnspan=2)

    btn_container = tk.Frame(root, bg=BG_DEEP_BLUE)
    btn_container.grid(row=3, column=0, sticky="we", pady=(5, 15))
    btn_container.columnconfigure(0, weight=1)

    download_btn = tk.Button(btn_container, text="🚀 直接解析复杂口令并下载", font=("Microsoft YaHei", 10, "bold"),
                             bg=BTN_BLUE, fg=TEXT_WHITE, activebackground="#0040A3", activeforeground=TEXT_WHITE,
                             relief=tk.FLAT, command=start_download_thread)
    download_btn.grid(row=0, column=0, ipadx=40, ipady=6)

    log_frame = tk.Frame(root, bg=BG_DEEP_BLUE)
    log_frame.grid(row=5, column=0, sticky="nsew", padx=30, pady=(5, 20))
    log_frame.rowconfigure(1, weight=1)
    log_frame.columnconfigure(0, weight=1)

    log_label = tk.Label(log_frame, text="实时控制台输出日志:", font=("Microsoft YaHei", 9), bg=BG_DEEP_BLUE, fg=TEXT_WHITE)
    log_label.grid(row=0, column=0, sticky="w", pady=(0, 5))

    log_box = scrolledtext.ScrolledText(log_frame, font=("Consolas", 10), bg=BG_CONTAINER, fg=TEXT_WHITE, relief=tk.FLAT)
    log_box.grid(row=1, column=0, sticky="nsew")

    sys.stdout = StdoutRedirector(log_box, root)

    root.mainloop()