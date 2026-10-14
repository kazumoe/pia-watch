import os
import re
import datetime as dt

import requests
from playwright.sync_api import sync_playwright

URL = "https://ticket.pia.jp/sp/ticketInformation.do?eventCd=2632756&rlsCd=001"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# 注意事項の定型文（判定から除外する）
NOTE_PHRASES = ["予定枚数終了次第", "受付終了となります"]
SOLD_OUT_WORDS = ["予定枚数終了", "受付終了", "販売終了", "完売", "発売前", "受付前", "取扱いがありません"]
ON_SALE_WORDS = ["購入手続きへ", "残りわずか", "空席あり", "発売中", "受付中"]
BUSY_WORDS = ["アクセスが集中しております", "The website is too busy"]


def notify(title, body, priority="high"):
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


def context(text, word, width=30):
    return [text[max(0, m.start() - width): m.end() + width].replace("\n", " ")
            for m in re.finditer(re.escape(word), text)]


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"),
            locale="ja-JP",
        )
        page.goto(URL, wait_until="networkidle", timeout=60_000)
        page.wait_for_timeout(2000)
        text = page.inner_text("body")
        browser.close()

    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))
    print(f"[{now:%Y-%m-%d %H:%M} JST]")

    if any(w in text for w in BUSY_WORDS):
        print("混雑ページのため今回はスキップ")
        return

    cleaned = text
    for ph in NOTE_PHRASES:
        cleaned = cleaned.replace(ph, "")

    on = [w for w in ON_SALE_WORDS if w in cleaned]
    off = [w for w in SOLD_OUT_WORDS if w in cleaned]
    print(f"販売中ワード={on} 終了ワード={off}")
    for w in on + off:
        for c in context(cleaned, w):
            print(f"  [{w}] …{c}…")

    if on:
        notify("チケットぴあ 販売中かも！", f"検出: {', '.join(on)}")
    elif not off:
        notify("チケットぴあ 状態が変わったかも", "売り切れ表示が見つかりません。ページを確認してください")


if __name__ == "__main__":
    main()
