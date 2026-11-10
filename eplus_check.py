import os
import re
import sys
import time
import random
import datetime as dt

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# Spotify On Stage Tokyo 2026 一般発売（先着）の申込みページ
URL = ("https://atom.eplus.jp/sys/main.jsp?prm=U=82:P2=301520:P5=0001:P3=0005:P21=001:P7=2:P6=127"
       ":P1=0402:P0=GGWC01:P55=//eplus.jp%2Fsf%2Fdetail%2F3015200001%3FP1%2a0402%26P59%2a1%26P6%2a127")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

INTERVAL_SEC = 60           # 確認の間隔（1分＋0〜30秒のランダム）
NOTIFY_COOLDOWN_SEC = 300   # 同じ通知を繰り返さない時間（5分）
TROUBLE_COOLDOWN_SEC = 3600 # 「読み取れない」「拒否された」などの不調通知は1時間に1回まで
END_DATE = "2026/12/20"     # 監視の終了日（ワークフロー側で自動停止）
RUN_MAX_SEC = 50 * 60       # 1回の実行で見張る最大時間（50分）。24時間監視のため終了時刻はなし

RETRY = 2                   # 混雑ページだったときの再試行回数
RETRY_WAIT_SEC = (20, 45)   # 再試行までの待ち時間（秒）をこの範囲でランダムに
START_JITTER_SEC = (0, 30)  # 開始時・各確認の間にこの範囲でランダムに待つ

# e+ の空席表示（凡例：○ 空席あり　△ 残りわずか　× 予定枚数終了　－ 扱いなし）
ON_SALE_MARKS = {"○": "空席あり", "◯": "空席あり", "〇": "空席あり", "△": "残りわずか"}
SOLD_OUT_MARKS = {"×": "予定枚数終了", "✕": "予定枚数終了", "Ｘ": "予定枚数終了"}
NONE_MARKS = {"－": "扱いなし", "-": "扱いなし", "ー": "扱いなし", "―": "扱いなし"}
BUSY_WORDS = ["アクセスが集中", "混み合って", "しばらく時間をおいて", "ただいま大変混雑"]
BLOCKED_WORDS = ["Access Denied", "Reference #"]


_last_sent = {}


def notify(title, body, priority="high", cooldown=NOTIFY_COOLDOWN_SEC):
    now = time.time()
    if now - _last_sent.get(title, 0) < cooldown:
        print(f"通知（{cooldown // 60}分以内に送信済みのため省略）: {title}")
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


def jst_now():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))


def is_busy(text):
    return any(w in text for w in BUSY_WORDS)


def is_blocked(text):
    return any(w in text for w in BLOCKED_WORDS)


def fetch_once():
    """ページを開いてHTMLと本文テキストを返す"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            # この受付はスマチケ限定のため、スマホとしてアクセスする
            user_agent=("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"),
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )
        page = context.new_page()
        page.goto(URL, wait_until="networkidle", timeout=60_000)
        page.wait_for_timeout(2000)
        html = page.content()
        text = page.inner_text("body")
        final_url = page.url
        browser.close()
    return html, text, final_url


def parse_statuses(html):
    """空席状況の表から（公演日時, 席種, 記号, 判定）の一覧を返す。判定は 販売中 / 売り切れ / 扱いなし / 不明"""
    soup = BeautifulSoup(html, "html.parser")
    result = []
    for box in soup.select("div.seat-info"):
        table = box.find("table")
        if not table:
            continue
        rows = table.find_all("tr")
        if not rows:
            continue
        seats = [s.get_text(strip=True) for s in rows[0].select("span.event-seat")]
        for tr in rows[1:]:
            date_el = tr.select_one(".ticketDate") or tr.find("th")
            if not date_el:
                continue
            when = re.sub(r"\s+", " ", date_el.get_text(" ", strip=True)).replace("：", ":")
            # 備考欄を除いた各セル（空席時に記号がリンクになっても、セルの書き方が変わっても拾えるように）
            cells = [td for td in tr.find_all("td", recursive=False)
                     if not td.select_one(".event-note") and "event-note" not in (td.get("class") or [])]
            for i, td in enumerate(cells):
                mark = td.get_text(strip=True)
                seat = seats[i] if i < len(seats) else f"席種{i + 1}"
                if any(m in mark for m in ON_SALE_MARKS):
                    kind = "販売中"
                elif any(m in mark for m in SOLD_OUT_MARKS):
                    kind = "売り切れ"
                elif mark in NONE_MARKS:
                    kind = "扱いなし"
                else:
                    kind = "不明"
                result.append((when, seat, mark, kind))
    return result


def check_once():
    for attempt in range(RETRY + 1):
        html, text, final_url = fetch_once()
        print(f"[{jst_now():%Y-%m-%d %H:%M:%S} JST] {attempt + 1}回目 {final_url}")
        if is_blocked(text):
            print("アクセスを拒否されました（bot対策の可能性）")
            notify("e+ アクセス拒否", "e+にアクセスを拒否されました。監視できていない可能性があります",
                   priority="low", cooldown=TROUBLE_COOLDOWN_SEC)
            return
        if not is_busy(text):
            break
        if attempt < RETRY:
            wait = random.randint(*RETRY_WAIT_SEC)
            print(f"混雑ページのため {wait}秒後に再試行します")
            time.sleep(wait)
    else:
        notify("e+ アクセス集中中", f"{RETRY + 1}回続けて混雑ページでした。販売中の可能性があります")
        return

    statuses = parse_statuses(html)
    print(f"枠数={len(statuses)}")
    for when, seat, mark, kind in statuses:
        print(f"  {when} {seat} → 「{mark}」（{kind}）")

    if not statuses:
        print("空席状況の表を読み取れませんでした")
        print("---- ページ文言（先頭1500字） ----")
        print(text[:1500])
        notify("e+ 表を読み取れません", "空席状況の表を読み取れませんでした。ページを確認してください",
               priority="low", cooldown=TROUBLE_COOLDOWN_SEC)
        return

    on_sale = [f"{w} {s} {ON_SALE_MARKS.get(m, m)}" for w, s, m, k in statuses if k == "販売中"]
    unknown = [f"{w} {s}「{m}」" for w, s, m, k in statuses if k == "不明"]

    if on_sale:
        notify("e+ 空席が出ました！", "空席: " + " / ".join(on_sale), priority="urgent")
    elif unknown:
        # ○△×－以外の表示 → 念のため控えめに知らせる
        notify("e+ 表示が変わりました", "見慣れない表示: " + " / ".join(unknown), priority="default")
    else:
        print("全席種 売り切れ表示")


def main():
    started = time.time()
    jitter = random.uniform(*START_JITTER_SEC)
    print(f"{jitter:.3f}秒待ってからアクセスします")
    time.sleep(jitter)

    while True:
        print(f"===== 確認開始 {jst_now():%H:%M:%S} JST =====")
        try:
            check_once()
        except Exception as e:
            print(f"確認中にエラーが発生しました: {e}")

        wait = INTERVAL_SEC + random.uniform(*START_JITTER_SEC)
        next_at = jst_now() + dt.timedelta(seconds=wait)
        if time.time() - started + wait > RUN_MAX_SEC:
            print("この回の見張りを終了します（次の自動実行に引き継ぎます）")
            break
        print(f"次の確認は {next_at:%H:%M:%S} JST ごろです")
        time.sleep(wait)


def morning_message():
    """毎朝10時の「本日も監視します」通知（ページにはアクセスしない）"""
    notify("本日もe+を監視します",
           f"Spotify On Stage Tokyo 2026 の空席を24時間・約1分半おきに確認します（{END_DATE}まで）。"
           "空席が出たら最優先で通知します。",
           priority="default")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "morning":
        morning_message()
    else:
        main()
