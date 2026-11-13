#!/usr/bin/env python
"""Excalidraw 构建产物后处理：把官方 Web 应用改造成可嵌入 TOS 的内网离线版。

处理项：
  1. 去除 SimpleAnalytics 统计脚本（数据外发）
  2. 绝对路径 /assets/ -> 相对路径，以适配 TOS 子路径 /excalidraw/
  3. 去掉指向外网 CDN 的字体 preload（字体已本地打包）
  4. 移除 PWA service worker 注册（iframe 内会造成旧版本缓存）
  5. 剥离 source map
  6. 注入 TOS 适配样式（去掉多余外边距/滚动条）
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

BUILD = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("build")


def log(msg: str) -> None:
    print(f"  [postprocess] {msg}")


def strip_sourcemaps() -> int:
    removed = 0
    for p in BUILD.rglob("*.map"):
        p.unlink()
        removed += 1
    # 去掉 js/css 末尾的 sourceMappingURL 注释
    for pattern in ("*.js", "*.css"):
        for p in BUILD.rglob(pattern):
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            cleaned = re.sub(r"\n?//# sourceMappingURL=\S+\s*$", "", text)
            cleaned = re.sub(r"\n?/\*# sourceMappingURL=\S+\s*\*/", "", cleaned)
            if cleaned != text:
                p.write_text(cleaned, encoding="utf-8")
    return removed


def remove_pwa() -> None:
    for name in ("sw.js", "sw.js.map", "workbox-3631b453.js", "service-worker.js"):
        p = BUILD / name
        if p.exists():
            p.unlink()
    # 其他 workbox 版本号可能变化，按前缀清理
    for p in BUILD.glob("workbox-*.js*"):
        p.unlink()
    # 移除 manifest 引用（PWA 安装对 iframe 内嵌应用无意义）
    manifest = BUILD / "manifest.webmanifest"
    if manifest.exists():
        manifest.unlink()


def patch_index() -> None:
    index = BUILD / "index.html"
    html = index.read_text(encoding="utf-8", errors="ignore")
    original_len = len(html)

    # 1) 移除 SimpleAnalytics 脚本块（包含 iframe 分支逻辑）
    html = re.sub(
        r"<script>\s*//\s*need to load this script dynamically.*?</script>",
        "",
        html,
        flags=re.S,
    )
    # 兜底：直接删掉任何 simpleanalytics 引用
    html = re.sub(
        r"<script[^>]*simpleanalyticscdn[^>]*>.*?</script>",
        "",
        html,
        flags=re.S,
    )

    # 2) 绝对路径 -> 相对路径（保留协议相对 //host 和 http(s):// 外链）
    html = re.sub(r'(href|src)="/(?!/)', r'\1="./', html)
    html = re.sub(r"href='/(?!/)", "href='./", html)
    html = re.sub(r"src='/(?!/)", "src='./", html)

    # 3) 去掉外网 CDN 字体 preload
    html = re.sub(
        r'<link rel="preload" href="https://excalidraw\.nyc3\.cdn\.digitaloceanspaces\.com[^"]*"[^>]*/?>',
        "",
        html,
    )
    # 3.1) 去掉 Google Fonts 预连接（未实际使用，且内网不可达会拖慢首屏）
    html = re.sub(r'<link rel="preconnect" href="https://fonts\.(googleapis|gstatic)\.com"[^>]*/?>', "", html)

    # 3.2) 资产基址必须用「当前页面目录的绝对 URL」。
    # Excalidraw 的 normalizeBaseUrl() 会把 "./" 这类相对值强行拼到 location.origin
    # （站点根），在 TOS 的 /excalidraw/ 子路径下会导致字体 404 并使应用崩溃。
    # 因此这里在运行时按 location 计算真实的目录基址。
    html = re.sub(
        r'window\.EXCALIDRAW_ASSET_PATH = \[[^\]]*\];',
        "window.EXCALIDRAW_ASSET_PATH = "
        "new URL('.', window.location.href).href;",
        html,
    )

    # 4) 移除 manifest 链接
    html = re.sub(r'<link rel="manifest" href="[^"]*"[^>]*/?>', "", html)

    # 5) 移除 sitemap 链接（CSP/内网无用）
    html = re.sub(r'<link rel="sitemap"[^>]*/?>', "", html)

    # 幂等保护：TOS 适配样式已注入过就不再重复注入
    if 'id="tos-chrome-css"' in html:
        index.write_text(html, encoding="utf-8")
        log(f"index.html 已含 TOS 适配样式，跳过重复注入（{len(html)} bytes）")
        return

    # 6) Excalidraw+ 自动跳转（会把用户导到 app.excalidraw.com，内网禁用）
    html = re.sub(
        r"<script>// Redirect Excalidraw\+ users.*?</script>",
        "",
        html,
        flags=re.S,
    )

    # 7) 注入 TOS 适配样式 + 屏蔽外部跳转
    inject = (
        "<style>"
        "html,body{margin:0;padding:0;width:100%;height:100%;overflow:hidden}"
        "body{background:#fff}html.dark body{background:#121212}"
        ".excalidraw--mobile .App-bottom-bar{bottom:0}"
        "#root{height:100vh}"
        "</style>"
        # ---- TOS 桌面适配（见 patch_tos_chrome）----
        "<style id=\"tos-chrome-css\">"
        "html.tos-embedded body{overflow:hidden}"
        "html.tos-embedded #root{"
        "  --tos-titlebar-h:41px;"
        "  height:100vh;"
        "  box-sizing:border-box;"
        "  padding-top:var(--tos-titlebar-h);"
        "}"
        # Excalidraw 的 UI 层是绝对定位于 #root 的，需跟着收窄可用高度
        "html.tos-embedded .excalidraw{"
        "  height:calc(100vh - var(--tos-titlebar-h))!important;"
        "}"
        "</style>"
        "<script>"
        "window.__TOS_APP__=true;"
        # 仅在 iframe 内（即 TOS 桌面里）才启用顶部让位，独立打开浏览器时不加
        "try{"
        "  if(window.self!==window.top){"
        "    document.documentElement.classList.add('tos-embedded');"
        "  }"
        "}catch(e){}"
        "</script>"
    )
    html = html.replace("</head>", inject + "</head>")

    index.write_text(html, encoding="utf-8")
    log(f"index.html {original_len} -> {len(html)} bytes")


def patch_service_worker_registration() -> None:
    """移除 js 里的 service worker 注册调用，避免 iframe 内缓存旧版本。"""
    hits = 0
    for p in BUILD.rglob("*.js"):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "serviceWorker" not in text:
            continue
        new = re.sub(r"serviceWorker\.register\([^)]*\)", "Promise.resolve()", text)
        if new != text:
            p.write_text(new, encoding="utf-8")
            hits += 1
    log(f"service worker 注册已在 {hits} 个 js 文件中断开")


def patch_self_embedding_guard() -> None:
    """放行 TOS 桌面嵌入。

    上游 Excaildraw 把「父页面与自身同源」一律判定为自我嵌套（isSelfEmbedding），
    渲染成 "I'm not a pretzel!"。但 TOS 的 iframe 应用天然是**同源**嵌入
    （宿主页与 /<appid>/ 同源），必然被误伤，应用在真机上完全打不开。

    这里只放宽到「父页面路径本身不是本应用」即放行，
    真正的无限自嵌（Excalidraw 嵌 Excalidraw）仍然会被拦住。
    """
    original = (
        'let FK=!1;if(window.self!==window.top)try{const e=new URL(document.referrer),'
        't=new URL(window.location.href);e.origin===t.origin&&(FK=!0)}catch{}'
    )
    replacement = (
        'let FK=!1;if(window.self!==window.top)try{'
        'const e=new URL(document.referrer),t=new URL(window.location.href);'
        # 仅当父页面也落在本应用自身路径下时才视为自我嵌套
        'e.origin===t.origin&&e.pathname.indexOf("/excalidraw/")===0&&(FK=!0)'
        '}catch{}'
    )

    patched = 0
    already = 0
    for p in BUILD.rglob("*.js"):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if replacement.split("const")[1][:60] in text:
            already += 1
            continue
        if original not in text:
            continue
        p.write_text(text.replace(original, replacement), encoding="utf-8")
        patched += 1

    if patched:
        log(f"自我嵌套守卫已放行 TOS 嵌入（{patched} 个文件）")
    elif already:
        log(f"自我嵌套守卫此前已打过补丁（{already} 个文件），跳过")
    else:
        log("警告：未找到自我嵌套守卫，可能上游代码已变更，请人工核查")


def strip_bom() -> None:
    """去掉 HTML/JSON/CSS 的 UTF-8 BOM。

    Windows 下用 PowerShell 改文件很容易带上 BOM，浏览器虽能容忍，
    但会让响应首字节变成乱码，也让外网探测类工具误判编码。
    """
    removed = 0
    for pattern in ("*.html", "*.json", "*.webmanifest", "*.css"):
        for p in BUILD.rglob(pattern):
            try:
                raw = p.read_bytes()
            except OSError:
                continue
            if raw.startswith(b"\xef\xbb\xbf"):
                p.write_bytes(raw[3:])
                removed += 1
    log(f"已剥离 {removed} 个文件的 UTF-8 BOM")


def main() -> int:
    if not BUILD.is_dir():
        print(f"构建产物目录不存在: {BUILD}", file=sys.stderr)
        return 1

    log(f"目标目录: {BUILD.resolve()}")
    n = strip_sourcemaps()
    log(f"已剥离 {n} 个 source map")
    remove_pwa()
    log("已移除 PWA service worker / manifest")
    patch_index()
    patch_service_worker_registration()
    patch_self_embedding_guard()
    strip_bom()

    files = [p for p in BUILD.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    log(f"完成：{len(files)} 个文件，{total / 1024 / 1024:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
