import os
import re
import datetime as dt

import requests
from playwright.sync_api import sync_playwright

URL = "https://ticket.pia.jp/sp/ticketInformation.do?eventCd=2632756&rlsCd=001"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# 説明文に含まれる「予定枚数終了」は判定から除外
NOTE_PHRASES = ["予定枚数終了次第", "予定枚数終了まで"]
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


def try_click(page, label):
    """指定の文言のボタン/リンクがあれば押す。押した先が新しいタブならそのタブを返す"""
    loc = page.get_by_text(label, exact=False)
    if loc.count() == 0:
        return page
    before = len(page.context.pages)
    try:
        loc.first.click(timeout=5000)
    except Exception as e:
        print(f"「{label}」を押せませんでした: {e}")
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


def check_agree(page):
    """「上記の内容について同意する」にチェックを入れる"""
    label = "上記の内容について同意する"
    try:
        page.get_by_label(label).check(timeout=3000)
        print("同意にチェックしました")
        return
    except Exception:
        pass
    try:
        page.get_by_text(label).first.click(timeout=3000)
        print("同意の文言をタップしました")
        return
    except Exception:
        pass
    try:
        page.locator("input[type=checkbox]").first.check(timeout=3000, force=True)
        print("チェックボックスにチェックしました")
    except Exception as e:
        print(f"同意にチェックできませんでした: {e}")
    page.wait_for_timeout(1000)


def main():
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
        if not any(w in text for w in BUSY_WORDS):
            # 案内ページ → 確認画面を閉じる → 同意にチェック → 公演一覧へ進む
            page = try_click(page, "申し込みへ進む")
            check_agree(page)
            page = try_click(page, "お申し込み画面へ")
            text = page.inner_text("body")
        final_url = page.url
        browser.close()

    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))
    print(f"[{now:%Y-%m-%d %H:%M} JST] {final_url}")

    if any(w in text for w in BUSY_WORDS):
        notify("チケットぴあ アクセス集中中", "アクセスが集中しています。販売中の可能性があります")
        return

    cleaned = text
    for ph in NOTE_PHRASES:
        cleaned = cleaned.replace(ph, "")

    perf = len(re.findall(r"開演", cleaned))           # 公演数
    sold = cleaned.count("予定枚数終了")                # 売り切れバッジ数（全体＋公演ごと）
    on = [w for w in ON_SALE_WORDS if w in cleaned]
    print(f"公演数={perf} 予定枚数終了バッジ={sold} 販売中ワード={on}")
    print("---- ページ文言（先頭1500字） ----")
    print(text[:1500])

    if perf == 0:
        print("公演一覧を読み取れませんでした（通知はしません）")
        return

    # 全公演売り切れなら バッジ数 = 公演数 + 全体バッジ1つ。1つでも減れば販売中の可能性
    if sold <= perf or on:
        notify("チケットぴあ 販売中かも！",
               f"売り切れ表示 {sold} / 公演 {perf}件。販売中の公演がある可能性があります")
    else:
        print("全公演 予定枚数終了")


if __name__ == "__main__":
    main()
