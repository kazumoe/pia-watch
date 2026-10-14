import os
import datetime as dt

import requests
from playwright.sync_api import sync_playwright

URL = "https://ticket.pia.jp/sp/ticketInformation.do?eventCd=2632756&rlsCd=001"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

SOLD_OUT_WORDS = ["予定枚数終了", "受付終了", "販売終了", "完売", "発売前", "受付前", "取扱いがありません"]
ON_SALE_WORDS = ["購入手続きへ", "申し込む", "申込む", "購入する", "残りわずか", "空席あり", "発売中", "受付中"]


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

    on = [w for w in ON_SALE_WORDS if w in text]
    off = [w for w in SOLD_OUT_WORDS if w in text]
    now = dt.datetime.utcnow() + dt.timedelta(hours=9)
    print(f"[{now:%Y-%m-%d %H:%M} JST] 販売中ワード={on} 終了ワード={off}")
    print("---- ページ文言（先頭1000字） ----")
    print(text[:1000])

    if on:
        notify("チケットぴあ 販売中かも！", f"検出: {', '.join(on)}")
    elif not off:
        notify("チケットぴあ 判定不能", "ページ文言を確認してください", priority="low")


if __name__ == "__main__":
    main()
