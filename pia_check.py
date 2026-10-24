import os
import re
import time
import random
import datetime as dt

import requests
from playwright.sync_api import sync_playwright

URL = "https://ticket.pia.jp/sp/ticketInformation.do?eventCd=2632756&rlsCd=001"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
MORNING = os.environ.get("MORNING", "") == "true"  # 毎朝10時の実行かどうか

INTERVAL_SEC = 60           # 確認の間隔（1分＋0〜30秒のランダム）
NOTIFY_COOLDOWN_SEC = 300   # 同じ通知を繰り返さない時間（5分）
RUN_MAX_SEC = 50 * 60       # 1回の実行で見張る最大時間（50分）
END_TIME = (19, 10)         # 日本時間19:10を過ぎたら終了

# 公演ごとの状態表示（「開演」の行のすぐ下に出る文言）で判定する
SOLD_OUT_WORDS = ["予定枚数終了", "販売終了", "受付終了", "完売", "売切", "取扱いなし"]
ON_SALE_WORDS = ["発売中", "販売中", "受付中", "残りわずか", "空席あり", "購入可能"]
BUSY_WORDS = ["アクセスが集中しております", "The website is too busy"]


_last_sent = {}


def notify(title, body, priority="high"):
    now = time.time()
    if now - _last_sent.get(title, 0) < NOTIFY_COOLDOWN_SEC:
        print(f"通知（{NOTIFY_COOLDOWN_SEC // 60}分以内に送信済みのため省略）: {title}")
        return
    _last_sent[title] = now
    print(f"通知: {title} / {body}")
    if not NTFY_TOPIC:
        print("NTFY_TOPIC 未設定のため送信しません")
        return
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=f"{body}\n{URL}".encode("utf-8"),
        headers={"Title": title.encode("utf-8"), "Priority": priority, "Click": URL},
        timeout=10,
    )


def check_agree(page):
    """「上記の内容について同意する」にチェックを入れる（画面外に隠れたチェックボックス対応）"""
    try:
        checked = page.evaluate("""() => {
            const cb = document.querySelector('input[name="dst-agreement"]')
                    || document.querySelector('input[type="checkbox"]');
            if (!cb) return null;
            if (!cb.checked) cb.click();
            return cb.checked;
        }""")
        print(f"同意チェック: {checked}")
    except Exception as e:
        print(f"同意にチェックできませんでした: {e}")
    page.wait_for_timeout(1000)


def dump_buttons(page, title):
    """調査用：ページ内のボタン・リンクの一覧をログに出す"""
    try:
        items = page.evaluate("""() => {
            const els = Array.from(document.querySelectorAll(
                'a, button, input[type=button], input[type=submit], [role=button], [onclick]'));
            return els.map(e => ({
                tag: e.tagName.toLowerCase(),
                text: (e.innerText || e.value || '').trim().replace(/\\s+/g, ' ').slice(0, 40),
                href: (e.getAttribute('href') || '').slice(0, 80),
                cls: (e.className || '').toString().slice(0, 40),
                vis: !!(e.offsetWidth || e.offsetHeight),
            })).filter(x => x.text || x.href);
        }""")
        print(f"---- ボタン一覧（{title}） ----")
        for it in items[:80]:
            print(f"  [{'表示' if it['vis'] else '非表示'}] <{it['tag']} class='{it['cls']}'> {it['text']}  {it['href']}")
    except Exception as e:
        print(f"ボタン一覧を取得できませんでした: {e}")


def click_proceed(page):
    """確認画面の「〜進む」ボタンを押す（表記ゆれ対応）"""
    before = len(page.context.pages)
    try:
        label = page.evaluate("""() => {
            const els = Array.from(document.querySelectorAll(
                'a, button, input[type=button], input[type=submit], [role=button]'));
            const t = el => (el.innerText || el.value || '').trim();
            const hits = els.filter(e => t(e).includes('進む') && t(e).length <= 20
                                       && (e.offsetWidth || e.offsetHeight));
            if (!hits.length) return null;
            hits[0].click();
            return t(hits[0]);
        }""")
    except Exception as e:
        print(f"「進む」ボタンを押せませんでした: {e}")
        return page
    if not label:
        print("表示中の「進む」ボタンはありませんでした")
        return page
    page.wait_for_timeout(1500)
    pages = page.context.pages
    target = pages[-1] if len(pages) > before else page
    try:
        target.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:
        pass
    target.wait_for_timeout(2000)
    print(f"「{label}」を押しました → {target.url}")
    return target


def click_apply_button(page):
    """「お申し込み画面へ」ボタン（dst-btn）を通常のタップ操作で押し、画面が切り替わるのを待つ"""
    btn = page.locator("button.dst-btn").first
    try:
        print(f"お申し込みボタンの状態: disabled={btn.get_attribute('disabled')}")
        before_url = page.url
        before = len(page.context.pages)
        btn.scroll_into_view_if_needed(timeout=5000)
        btn.click(timeout=10_000)
        try:
            page.wait_for_url(lambda u: u != before_url, timeout=15_000)
        except Exception:
            pass
        pages = page.context.pages
        target = pages[-1] if len(pages) > before else page
        try:
            target.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        target.wait_for_timeout(2000)
        print(f"「お申し込み画面へ」を押しました → {target.url}")
        return target
    except Exception as e:
        print(f"「お申し込み画面へ」を押せませんでした: {e}")
        return page


def has_list(page):
    try:
        return "開演" in page.inner_text("body")
    except Exception:
        return False


RETRY = 2                   # 混雑ページだったときの再試行回数
RETRY_WAIT_SEC = (20, 45)   # 再試行までの待ち時間（秒）をこの範囲でランダムに
START_JITTER_SEC = (0, 30)  # 開始時にこの範囲でランダムに待ってからアクセス


def is_busy(text, url):
    return "sorry.pia.jp" in url or any(w in text for w in BUSY_WORDS)


def fetch_once():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"),
            locale="ja-JP",
        )
        page = context.new_page()
        page.goto(URL, wait_until="networkidle", timeout=60_000)
        page.wait_for_timeout(2000)

        text = page.inner_text("body")
        if not is_busy(text, page.url):
            # 案内ページ → 同意にチェック → 確認画面の「申し込みへ進む」 → 公演一覧
            page = click_proceed(page)
            check_agree(page)
            if not has_list(page):
                page = click_apply_button(page)
            if not has_list(page):
                # 確認画面がもう一度出た場合
                page = click_proceed(page)
            if not has_list(page):
                dump_buttons(page, "お申し込み画面へ を押した後")
            text = page.inner_text("body")
        final_url = page.url
        browser.close()
    return text, final_url


def parse_statuses(text):
    """公演ごとに（日時, 状態表示, 判定）を返す。判定は 売り切れ / 販売中 / 不明"""
    lines = [l.strip() for l in text.splitlines()]
    result = []
    for i, line in enumerate(lines):
        if "開演" not in line:
            continue
        date = next((lines[j] for j in range(i - 1, max(i - 3, -1), -1) if lines[j]), "")
        status = next((lines[j] for j in range(i + 1, min(i + 4, len(lines))) if lines[j]), "")
        when = f"{date} {line.split('開演')[0].strip()}"
        if any(w in status for w in SOLD_OUT_WORDS):
            kind = "売り切れ"
        elif any(w in status for w in ON_SALE_WORDS):
            kind = "販売中"
        elif "(" in status and any(c in status for c in "都道府県"):
            # 状態表示がなく、すぐ会場名が来る → 売り切れ表示が消えた＝販売中の可能性
            status, kind = "（表示なし）", "販売中"
        else:
            kind = "不明"
        result.append((when, status, kind))
    return result


def check_once(morning):
    for attempt in range(RETRY + 1):
        text, final_url = fetch_once()
        now = dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))
        print(f"[{now:%Y-%m-%d %H:%M:%S} JST] {attempt + 1}回目 {final_url}")
        if not is_busy(text, final_url):
            break
        if attempt < RETRY:
            wait = random.randint(*RETRY_WAIT_SEC)
            print(f"混雑ページのため {wait}秒後に再試行します")
            time.sleep(wait)
    else:
        notify("チケットぴあ アクセス集中中",
               f"{RETRY + 1}回続けて混雑ページでした。販売中の可能性があります")
        return

    statuses = parse_statuses(text)
    perf = len(statuses)
    print(f"公演数={perf}")
    for when, st, kind in statuses:
        print(f"  {when} → 「{st}」（{kind}）")

    if perf == 0:
        print("公演一覧を読み取れませんでした")
        print("---- ページ文言（先頭1500字） ----")
        print(text[:1500])
        if morning:
            notify("監視中", "公演一覧を読み取れませんでした。ページを確認してください", priority="low")
        return

    on_sale = [f"{w} {st}" for w, st, k in statuses if k == "販売中"]
    unknown = [f"{w} {st}" for w, st, k in statuses if k == "不明"]

    if on_sale:
        notify("チケットぴあ 販売中かも！", "販売中の公演: " + " / ".join(on_sale))
    elif unknown:
        # 売り切れでも販売中でもない新しい表示 → 念のため控えめに知らせる
        notify("チケットぴあ 表示が変わりました", "見慣れない表示: " + " / ".join(unknown), priority="default")
    else:
        print("全公演 売り切れ表示")
        if morning:
            notify("監視中", f"全{perf}公演 売り切れ表示（{statuses[0][1]}）", priority="low")

def jst_now():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))


def main():
    started = time.time()
    jitter = random.uniform(*START_JITTER_SEC)
    print(f"{jitter:.3f}秒待ってからアクセスします")
    time.sleep(jitter)

    first = True
    while True:
        print(f"===== 確認開始 {jst_now():%H:%M:%S} JST =====")
        try:
            check_once(MORNING and first)
        except Exception as e:
            print(f"確認中にエラーが発生しました: {e}")
        first = False

        wait = INTERVAL_SEC + random.uniform(*START_JITTER_SEC)
        next_at = jst_now() + dt.timedelta(seconds=wait)
        end_at = jst_now().replace(hour=END_TIME[0], minute=END_TIME[1], second=59, microsecond=0)
        if time.time() - started + wait > RUN_MAX_SEC or next_at > end_at:
            print("この回の見張りを終了します（次の自動実行に引き継ぎます）")
            break
        print(f"次の確認は {next_at:%H:%M:%S} JST ごろです")
        time.sleep(wait)

if __name__ == "__main__":
    main()
