#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# 依赖：os/re/sys/time 基础库，requests 取出口 IP，seleniumbase 驱动浏览器。
# 旧登录方式相关的 json / subprocess / urllib 及 datetime 均已随代码删除。
import os, re, sys, time, requests
from seleniumbase import SB

# 环境变量配置(可以直接私库在双引号里填写)
EMAIL         = os.environ.get("EMAIL") or ""           # 登录邮箱,同时也是通知用的账户标识
PASSWORD      = os.environ.get("PASSWORD") or ""        # bot-hosting 账号密码,唯一登录方式(密码不过期)
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID") or ""      # TG chat id,不填写不通知，需和bot token一起填写生效
TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN") or ""    # TG bot token 

# 登录前提校验。
# 2026-10-21 上游「Add Password login; Remove Discord token login」，注释写明
# 「密码不过期，唯一登录方式」。2026-11-13 本仓库实测 SESSION_TOKEN 与 Discord OAuth
# 同时失效 —— 不是 token 过期，是站点把这两个入口拆了。
# 故彻底移除两种旧登录方式，不再维护，只留 EMAIL + PASSWORD。
if not (EMAIL and PASSWORD):
    print("❌ 未配置 EMAIL 或 PASSWORD，无法登录，脚本终止。")
    print("   EMAIL 必须是 bot-hosting 的登录邮箱；PASSWORD 是账户密码。")
    sys.exit(1)

# 记录本次登录方式（用于通知）
_LOGIN_METHOD = "Password"

# 站点基础配置
BASE_URL = "https://bot-hosting.net"

# 账单页路径：站点在 2026-11-12 前后把 /a/billings 迁到 /dash/billings
# （上游同日的提交「页面调整，及时同步」同步改了这一行）。
# 迁移期常见新旧并存，故两个都试、谁通算谁 —— 只硬编码一个，下次再改版又要瞎一次。
BILLINGS_PATHS = ("/dash/billings", "/a/billings")

# 发送tg通知
def send_telegram_message(message: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram 未配置，跳过通知")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": message}, timeout=10)
        print("✅ Telegram 通知已发送")
    except Exception as e:
        print(f"❌ Telegram 发送失败: {e}")

# 通知格式
def format_notification(status: str, extra: str = "", error: str = "", expiry_date: str = "") -> str:
    local_time = time.gmtime(time.time() + 8 * 3600)
    now = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    if '@' in EMAIL:
        name, domain = EMAIL.split('@', 1)
        if len(name) > 4:
            masked_email = f"{name[:2]}****{name[-2:]}@{domain}"
        else:
            masked_email = f"{name}@{domain}"
    else:
        masked_email = EMAIL[:2] + '****' 
    
    lines = [
        "🇫🇮 Bot-hosting 续期通知",
        "",
        f"{status}",
        f"👤 登录账户: {masked_email}",
    ]
    lines.append(f"🔐 登录方式: {_LOGIN_METHOD}")
    if expiry_date:
        lines.append(f"📅 到期时间: {expiry_date}")
    if extra:
        lines.append(extra)
    if error:
        lines.append(f"⚠️ 错误信息: {error}")
    lines.append(f"⏱️ 登录时间: {now}")
    return "\n".join(lines)

# 等待Turnstile验证通过
def wait_for_turnstile_pass(sb, timeout=30):
    start = time.time()
    cf_indicators = ["verify you are human", "确认您是真人", "troubleshoot", "just a moment"]
    while time.time() - start < timeout:
        page_lower = sb.get_page_source().lower()
        if not any(x in page_lower for x in cf_indicators):
            print("✅ Turnstile 验证已通过")
            # sb.save_screenshot("turnstile_passed.png")
            return True
        sb.sleep(1)
    print("❌ Turnstile 验证超时未通过")
    return False
    
# 获取当前出口ip
def get_current_ip(proxy_server: str = "") -> str:
    proxies = None
    if proxy_server:
        proxies = {"http": proxy_server, "https": proxy_server}
    response = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
    response.raise_for_status()
    return response.text.strip()

# 时间格式化
def format_countdown(countdown_str: str) -> str:
    try:
        h, m, _ = countdown_str.split(':')
        h = int(h)
        m = int(m)
        if h > 0:
            return f"{h}h{m}min"
        else:
            return f"{m}min"
    except:
        return countdown_str

# 判断是否为「前序刚续期成功」：倒计时落在 23~24 小时区间
# 原理：兜底触发只比主触发晚 20 分钟。若本次跑起来看到倒计时是 23:4x:xx，
#       说明不到 1 小时前已有人成功续期 —— 那就是前序（主触发）已经干成了。
#       此时应静默退出：不推送通知、不重复操作，也不标记失败。
def is_recently_renewed(countdown_str: str, hours_threshold: int = 23) -> bool:
    try:
        h = int(str(countdown_str).split(':')[0])
        return hours_threshold <= h <= 24
    except Exception:
        return False


# ── 日期处理 ──
# 站点 2026-11-12 前后调整了页面，日期写法可能变成 <13 Nov 2026>、<2026-11-13> 等。
# 旧正则只认 \d{2} 的 YYYY/MM/DD 与 MM/DD/YYYY，格式一变就提取不到，
# 进而把「续期成功」误判成「结果未知」。这是继登录方式之后最容易中的暗坑。
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def normalize_date(date_str: str) -> str:
    """把各种日期写法统一成 YYYY/MM/DD；认不出的原样返回。"""
    s = (date_str or "").strip()
    # 13 Nov 2026 / 13 November 2026
    m = re.match(r"(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})", s)
    if m:
        mon = _MONTHS.get(m.group(2)[:3].lower())
        if mon:
            return f"{m.group(3)}/{mon:02d}/{int(m.group(1)):02d}"
    # 11/13/2026、2026/11/13、2026-11-13
    parts = re.split(r"[/-]", re.sub(r"\s+", "", s))
    if len(parts) == 3:
        try:
            if len(parts[0]) == 4:          # YYYY/MM/DD
                return f"{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}"
            if len(parts[2]) == 4:          # MM/DD/YYYY
                return f"{parts[2]}/{int(parts[0]):02d}/{int(parts[1]):02d}"
        except ValueError:
            pass
    return s


# 注意：上游 2026-11-12 那版正则只写了 \d{4}-\d{2}-\d{2}（ISO 横线版），
# 漏掉了 \d{4}/\d{1,2}/\d{1,2}（斜杠版）。而页面与 TG 通知实际用的就是 2026/11/13 这种写法，
# 照搬会直接读不到日期 —— 这里用 [-/] 把两种分隔符一并覆盖。
DATE_RE = (r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}"
           r"|\d{1,2}/\d{1,2}/\d{4}"
           r"|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})")

# 倒计时文案：新版页面可能写成 "Renewal cooldown"，小时也可能只有一位
CD_RE = r"(?:Renew in|Renewal cooldown)\s+(\d{1,2}:\d{2}:\d{2})"


def extract_expiry_date(page_source: str) -> str:
    for pattern in (r"[Ee]xpires\s*[:\-]?\s*" + DATE_RE, DATE_RE):
        match = re.search(pattern, page_source)
        if match:
            return normalize_date(match.group(1))
    return None


def get_expiry_from_dom(sb) -> str:
    """优先从 st-meta 元素取到期日 —— 站点用它渲染，比全文正则抗改版。"""
    for selector in ("span.st-meta", ".st-meta", '[class*="st-meta"]'):
        try:
            if sb.is_element_present(selector):
                match = re.search(DATE_RE, sb.get_text(selector))
                if match:
                    return normalize_date(match.group(1))
        except Exception:
            pass
    return None


def open_billings(sb) -> str:
    """打开账单页：优先新路径 /dash/billings，失败回落 /a/billings，返回最终 URL。"""
    last_url = ""
    for path in BILLINGS_PATHS:
        try:
            url = BASE_URL + path
            print(f"🌐 访问 {url} ...")
            sb.open(url)
            sb.wait_for_ready_state_complete()
            sb.sleep(3)
            last_url = sb.get_current_url()
            if "billings" in last_url and "/login" not in last_url and "error=" not in last_url:
                return last_url
            print(f"⚠️ {path} 未落到账单页，当前URL: {last_url}")
        except Exception as e:
            print(f"⚠️ 访问 {path} 异常: {e}")
    return last_url

# 主流程
def main():
    print("#" * 25)
    print("   Bot-hosting 自动续期")
    print("#" * 25)

    IS_PROXY = os.environ.get("IS_PROXY", "false").lower() == "true"
    PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"
    HEADLESS = os.environ.get("HEADLESS", "false").lower() == "true" 

    # locale_code 固定 en-US：脚本所有选择器（Renew free plan / Renew in）都是英文文案，
    # 站点会按浏览器语言切换界面（弹窗中文版出现过「续期 4 天」）。
    # 不锁语言，等于把成败押在 runner 的默认 locale 上 —— 上游同样锁了 en-US。
    sb_kwargs = {"uc": True, "headless": HEADLESS, "locale_code": "en-US"}

    if IS_PROXY:
        print(f"🔗 挂载代理: {PROXY_SERVER}")
        sb_kwargs["proxy"] = PROXY_SERVER
    else:
        print("🍭 未使用代理，直连访问")

    with SB(**sb_kwargs) as sb:
        try:
            ip = get_current_ip(PROXY_SERVER if IS_PROXY else "")
            print(f"📍 当前出口IP: {ip}")
        except Exception as e:
            print(f"⚠️ 获取出口 IP 失败: {e}")

        login_ok = False

        # ── 账号密码登录（唯一方式）──
        # bot-hosting 已废弃 SESSION_TOKEN / Discord OAuth 两种入口，密码是唯一稳定路径。
        # 流程与上游 eooce 2026-10-21 的实装一致：
        #   /login → 点「Continue with email」→ 填邮箱密码 → 过 Turnstile → 提交 → 验证 /a/billings
        if EMAIL and PASSWORD:
            print("🚀 启动浏览器...")
            try:
                print("🌐 访问 https://bot-hosting.net/login")
                sb.open("https://bot-hosting.net/login")
                sb.wait_for_ready_state_complete()
                sb.sleep(3)

                # 首屏是登录方式选择页，先点「Continue with email」展开表单
                sb.click('button[type="submit"]')
                sb.sleep(12)

                print("📧 填写邮箱...")
                sb.input('input[type="email"]', EMAIL)
                sb.sleep(1)
                print("🔑 填写密码...")
                sb.input('input[type="password"]', PASSWORD)
                sb.sleep(3)

                # 登录页同样有 Turnstile
                if not wait_for_turnstile_pass(sb, timeout=25):
                    print("⚠️ 登录页 Turnstile 未通过，尝试兜底点击...")
                    try:
                        sb.uc_gui_click_captcha()
                        sb.sleep(4)
                    except Exception as e:
                        print(f"⚠️ 兜底点击失败: {e}")

                sb.sleep(1)
                sb.click('button[type="submit"]')
                print("🖱️ 提交登录按钮")
                sb.sleep(6)
                sb.wait_for_ready_state_complete()

                current_url = open_billings(sb)
                current_title = sb.get_title()
                print(f"📝 当前URL: {current_url}, Title: {current_title}")

                if ("billings" in current_url and "/login" not in current_url
                        and "error=" not in current_url):
                    login_ok = True
                    print("✅ Password 登录成功, 当前已到达账单页")
                else:
                    print(f"❌ Password 登录后仍未到达账单页，当前URL: {current_url}")
            except Exception as e:
                print(f"❌ Password 登录过程异常: {type(e).__name__}: {str(e)[:150]}")

        if not login_ok:
            send_telegram_message(format_notification("❌ 登录失败", error="Password 登录失败"))
            return

        # 提取当前到期日期（优先 DOM 的 st-meta，比全文正则抗改版）
        sb.sleep(2)
        page_source = sb.get_page_source()
        current_expiry = get_expiry_from_dom(sb) or extract_expiry_date(page_source)
        if current_expiry:
            print(f"📅 当前到期日期: {current_expiry}")
        else:
            print("⚠️ 未能提取当前到期日期")

        # 寻找外部续期按钮
        outer_renew_selector = None
        countdown_text = None

        # 页面级倒计时优先判定：
        # 冷却期内页面上一定存在 "Renew in HH:MM:SS" 或 "Renewal cooldown HH:MM:SS"。
        # 只要出现，就说明还没到可续期时间，直接走「未到时间」分支 ——
        # 避免被宽泛选择器误匹配到别的元素，把冷却期内的按钮当成可点。
        page_html = sb.get_page_source()
        page_cd = re.search(CD_RE, page_html)
        if page_cd:
            countdown_text = page_cd.group(1)
            print(f"⏳ 页面显示倒计时 {countdown_text}，判定为未到续期时间")

        # 选择器顺序：精确的排前面。
        # 旧的 [class*="renew"] / button:contains("Renew") 过于宽泛，容易命中非续期元素，
        # 上游 2026-11-12 已把它们删掉，这里同步收紧。
        possible_selectors = [
            'button:contains("Renew free plan")',
            'type:button("Renew free plan")',
            'button:contains("Renew")',
            'a:contains("Renew")',
        ]

        # 已从页面文本里读到倒计时，就不再找按钮，直接走「未到时间」分支
        for selector in ([] if countdown_text else possible_selectors):
            try:
                if sb.is_element_visible(selector):
                    button_text = sb.get_text(selector)
                    m_cd = re.search(CD_RE, button_text)
                    if m_cd:
                        countdown_text = m_cd.group(1)
                        break
                    elif "Renew" in button_text and "in" not in button_text.lower():
                        outer_renew_selector = selector
                        print(f"✅ 续期按钮可用: '{button_text}'")
                        break
            except Exception:
                pass

        # 点击外部续期按钮等待弹窗
        if outer_renew_selector:
            print("🔄 点击外部续期按钮，等待验证窗口...")
            try:
                sb.sleep(2)
                sb.click(outer_renew_selector)
                sb.sleep(15)  # 等待模态框加载，可能因网络因素加载慢
            except Exception as e:
                print(f"❌ 点击外部按钮失败: {e}")
                send_telegram_message(format_notification("❌ 续期失败", error="点击外部续期按钮出错"))
                return

            # 处理弹窗中的 Turnstile
            print("🔒 检测弹窗中的 Turnstile 验证...")
            turnstile_passed = False
            for attempt in range(1, 4):
                try:
                    sb.uc_gui_click_captcha()
                    time.sleep(12)
                except Exception as e:
                    print(f"⚠️ 点击 Turnstile 出错: {e}")

                if wait_for_turnstile_pass(sb, timeout=20):
                    turnstile_passed = True
                    break
                else:
                    print(f"⏳ 第 {attempt} 次未通过，重试点击...")

            if not turnstile_passed:
                print("❌ Turnstile 验证最终未通过，脚本退出")
                send_telegram_message(format_notification("❌ 续期失败", error="Turnstile 验证未通过"))
                return

            # ── 点击续期按钮（2026-11-09 改版适配）──
            # 站点在 11-07 → 11-09 之间改版：弹窗改为中文「续期 4 天」，
            # 并把流程拆成「1 人机验证 → 通过后按钮才可用」两步。
            #
            # 本地原来用 button:contains("Renew for 4 days") 匹配文案，改版后页面
            # 仍残留含该英文的元素，click 不报错、日志打印「已点击」，但点击实际无效
            # —— 60 秒页面纹丝不动，后台到期日不动。11-09 两次失败皆出于此。
            #
            # 上游 eooce 的 11-09 运行（#187）成功，其做法是不看文案，用
            #     div[role="dialog"] button.btn-primary
            # 按「弹窗 + 主按钮」这个结构特征定位，语言和文案怎么改都不受影响。
            # 本地改为同样的主方案，文案匹配仅作兜底。
            #
            # 点击后必须校验后果：弹窗关闭 = 请求已发出；还在 = 点空了，重试。
            # 可见性判断用 getBoundingClientRect，不能用 offsetParent ——
            # 弹窗是 position:fixed，其子元素 offsetParent 恒为 null，会全部误判为隐藏。
            print("⏳ 等待续期按钮可用并点击...")
            time.sleep(5)

            DIALOG_BTN_SELECTORS = [
                'div[role="dialog"] button.btn-primary',   # 上游同款，首选
                'div[role="dialog"] .btn-primary',
                '[role="dialog"] button.btn-primary',
            ]

            JS_VIS_HELPER = """
                const vis = el => {
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) return false;
                    const s = getComputedStyle(el);
                    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
                };
                const re = /(续期\\s*4\\s*天|Renew\\s*(for\\s*)?4\\s*days)/i;
            """
            JS_LIST_BUTTONS = JS_VIS_HELPER + """
                const btns = [...document.querySelectorAll('button')];
                return btns.filter(vis).map(b => (b.textContent || '').trim()).filter(t => t.length > 0);
            """
            JS_CLICK_BY_TEXT = JS_VIS_HELPER + """
                const btns = [...document.querySelectorAll('button')];
                const target = btns.find(b => vis(b) && !b.disabled && re.test(b.textContent || ''));
                let result = 'NOT_FOUND';
                if (target) {
                    target.click();
                    result = 'CLICKED:' + (target.textContent || '').trim();
                }
                return result;
            """
            # 只查「弹窗内」的按钮是否还在，绝不能全页扫描 ——
            # 2026-11-09 实测页面上有两个 "Renew for 4 days"（弹窗一个、底部套餐区一个），
            # 全页扫描必然命中底部那个，导致弹窗已关闭也被判成「仍在」，白跑两轮重试。
            JS_DIALOG_BTN_EXISTS = JS_VIS_HELPER + """
                const dlg = document.querySelector('div[role="dialog"], [role="dialog"]');
                let exists = false;
                if (dlg) {
                    exists = [...dlg.querySelectorAll('button.btn-primary')].some(vis)
                          || [...dlg.querySelectorAll('button')].some(b => vis(b) && re.test(b.textContent || ''));
                }
                return exists;
            """

            try:
                visible_btns = sb.execute_script(JS_LIST_BUTTONS)
                print(f"🔎 当前可见按钮共 {len(visible_btns)} 个：{visible_btns}")
            except Exception as e:
                print(f"⚠️ 枚举按钮失败: {e}")

            modal_button_clicked = False
            for rnd in range(1, 4):
                # 主方案：CSS 定位弹窗主按钮，不看文案
                hit = False
                for sel in DIALOG_BTN_SELECTORS:
                    try:
                        if sb.is_element_visible(sel):
                            sb.click(sel, timeout=5)
                            print(f"🖱️ 第 {rnd} 轮点击弹窗主按钮：{sel}")
                            hit = True
                            break
                    except Exception:
                        continue

                # 兜底：按中英文文案用 JS 触发
                if not hit:
                    try:
                        ret = sb.execute_script(JS_CLICK_BY_TEXT)
                        print(f"🖱️ 第 {rnd} 轮改用文案匹配点击 → {ret}")
                    except Exception as e:
                        print(f"⚠️ 第 {rnd} 轮文案点击异常: {e}")

                time.sleep(6)

                # 先看到期日变没变：变了就是真成功，不必纠结弹窗关没关。
                # （2026-11-09 实测：点击其实已生效，但弹窗关闭有延迟，只看弹窗会误判。）
                try:
                    now_expiry = get_expiry_from_dom(sb) or extract_expiry_date(sb.get_page_source())
                    if now_expiry and current_expiry and now_expiry != current_expiry:
                        modal_button_clicked = True
                        print(f"✅ 到期日已更新为 {now_expiry}，续期已生效")
                        break
                except Exception:
                    pass

                try:
                    still_there = sb.execute_script(JS_DIALOG_BTN_EXISTS)
                except Exception:
                    still_there = True

                if not still_there:
                    modal_button_clicked = True
                    print("✅ 弹窗已关闭，续期请求已发出")
                    break
                print(f"⚠️ 第 {rnd} 轮点击后弹窗仍在，判定未生效，重试")

            if not modal_button_clicked:
                print("❌ 三轮点击后续期按钮仍在，判定点击未生效")
                try:
                    sb.save_screenshot("renew_click_failed.png")
                    print("📸 已保存现场截图: renew_click_failed.png")
                except Exception as e:
                    print(f"⚠️ 截图失败: {e}")

            print("⏳ 等待新的过期时间...")

            # 续期确认：点击后 bot-hosting 服务端可能延迟刷新，原先只等 6 秒就读页面，
            # 经常误判为「结果未知」—— 官网其实已续成功，日志却报未成功。
            # 改为最多轮询 60 秒：每 10 秒重新加载一次账单页再读，读到新倒计时/新到期日才算确认。
            CONFIRM_TIMEOUT = 60
            CONFIRM_INTERVAL = 10

            sb.sleep(6)

            # 提取新的到期日期和倒计时
            new_page_text = sb.get_page_source()
            new_expiry = get_expiry_from_dom(sb) or extract_expiry_date(new_page_text)
            new_match = re.search(CD_RE, new_page_text)

            waited = 0
            while (not new_match) and (not new_expiry or new_expiry == current_expiry) and waited < CONFIRM_TIMEOUT:
                sb.sleep(CONFIRM_INTERVAL)
                waited += CONFIRM_INTERVAL
                print(f"   …已等 {waited} 秒未读到新的到期信息，刷新账单页确认")
                try:
                    # 用 refresh 而不是重新 open：等同用户按 F5，
                    # 且不会因站点路径迁移（/a → /dash）而撞 404。
                    sb.refresh()
                    sb.wait_for_ready_state_complete()
                    sb.sleep(2)
                except Exception as e:
                    print(f"   ⚠️ 刷新失败，改走直连账单页: {e}")
                    open_billings(sb)
                new_page_text = sb.get_page_source()
                new_expiry = get_expiry_from_dom(sb) or extract_expiry_date(new_page_text)
                new_match = re.search(CD_RE, new_page_text)

            if new_match:
                new_countdown = new_match.group(1)
                print(f"✅ 续期成功！新的倒计时: {new_countdown}")
                if new_expiry:
                    print(f"📅 新的到期日期: {new_expiry}")
                send_telegram_message(
                    format_notification(
                        "✅ 续期成功",
                        extra=f"⏱️ 可续期时间: {format_countdown(new_countdown)}后",
                        expiry_date=new_expiry or "（未获取到）"
                    )
                )
            else:
                if new_expiry and new_expiry != current_expiry:
                    print(f"✅ 续期成功，到期日期已更新为: {new_expiry}")
                    send_telegram_message(
                        format_notification(
                            "✅ 续期成功",
                            extra="到期日期已更新",
                            expiry_date=new_expiry
                        )
                    )
                else:
                    print("⚠️ 续期结果未知，到期日期未变化，请手动检查")
                    send_telegram_message(
                        format_notification(
                            "⚠️ 续期可能未成功",
                            extra="请登录后台检查",
                            expiry_date=current_expiry or "（未获取到）"
                        )
                    )

        else:
            if countdown_text:
                friendly = format_countdown(countdown_text)
                if is_recently_renewed(countdown_text):
                    # 倒计时 23~24 小时 → 不到 1 小时前刚续期过（例如月末 31→1 边界连跑两天）
                    # 静默处理：不推送通知、不重复操作
                    print(f"✅ 前序已续期完成（倒计时 {countdown_text}），本次无需操作，不发通知")
                else:
                    print(f"⏳ 未到续期时间，倒计时: {countdown_text} ({friendly})")
                    send_telegram_message(
                        format_notification(
                            "⏳ 未到续期时间",
                            extra=f"⏱️ 可续期时间: {friendly}后",
                            expiry_date=current_expiry or "（未获取到）"
                        )
                    )
            else:
                print("ℹ️ 未找到续期按钮或倒计时，状态未知")
                send_telegram_message(
                    format_notification(
                        "ℹ️ 无需续期",
                        extra="当前状态未知，请手动检查",
                        expiry_date=current_expiry or "（未获取到）"
                    )
                )

        print("🏁 脚本执行完毕")

if __name__ == "__main__":
    main()
