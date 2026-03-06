import os
import time
import random
import logging
from datetime import datetime, timedelta, timezone

import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.remote.remote_connection import RemoteConnection
from selenium.common.exceptions import TimeoutException

# -----------------------------
# 基本設定
# -----------------------------
JST = timezone(timedelta(hours=9))

target_minute = 20
tolerance_minutes = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)

def safe_log(msg: str):
    logging.info(msg)
    print(msg, flush=True)

RemoteConnection.set_timeout = lambda *_: None

# -----------------------------
# 環境変数
# -----------------------------
LOGIN_URL = os.getenv("LOGIN_URL", "https://www.d-round.co.jp/adams/").strip()
STAFF_ID = os.getenv("STAFF_ID", "").strip()
PASSWORD = os.getenv("PASSWORD", "").strip()
TENANT_TEXT = os.getenv("TENANT_TEXT", "C").strip()

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_USER_ID = os.getenv("LINE_USER_ID", "").strip()

CHROME_BIN = os.getenv("CHROME_BIN", "/usr/bin/chromium").strip()
CHROMEDRIVER_PATH = os.getenv("CHROMEDRIVER_PATH", "/usr/bin/chromedriver").strip()


# -----------------------------
# 共通時刻関数
# -----------------------------
def now_jst() -> datetime:
    return datetime.now(JST)


# -----------------------------
# LINE通知
# -----------------------------
def send_line_message(text: str) -> bool:
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID:
        safe_log("LINE通知スキップ：LINE_CHANNEL_ACCESS_TOKEN / LINE_USER_ID 未設定")
        return False

    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "to": LINE_USER_ID,
        "messages": [{"type": "text", "text": text[:2000]}],
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        if 200 <= r.status_code < 300:
            safe_log("LINE通知送信OK")
            return True
        safe_log(f"LINE通知失敗：status={r.status_code} body={r.text}")
        return False
    except Exception as e:
        safe_log(f"LINE通知例外：{e}")
        return False


# -----------------------------
# 表示用時間（0〜6 → 24〜30）
# -----------------------------
def display_hour(dt: datetime) -> int:
    return dt.hour + 24 if dt.hour < 7 else dt.hour


# -----------------------------
# 夜勤時間帯生成（23〜30）
# -----------------------------
def create_night_hours():
    now = now_jst()
    base_date = now.date() - timedelta(days=1) if now.hour < 7 else now.date()

    hours = []
    for h in range(23, 31):
        if h >= 24:
            dt = datetime.combine(
                base_date + timedelta(days=1),
                datetime.min.time(),
                tzinfo=JST
            ).replace(hour=h - 24, minute=target_minute)
        else:
            dt = datetime.combine(
                base_date,
                datetime.min.time(),
                tzinfo=JST
            ).replace(hour=h, minute=target_minute)
        hours.append((h, dt))
    return hours


# -----------------------------
# ブラウザ起動
# -----------------------------
def start_browser(hour: int):
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-features=VizDisplayCompositor")
    options.add_argument("--window-size=1200,900")

    # Cloud Run Debian の chromium パス
    options.binary_location = CHROME_BIN

    # ★ 重要：driver を明示
    service = Service(
        executable_path=CHROMEDRIVER_PATH,
        log_output=f"/tmp/chromedriver_{hour}.log",
    )

    driver = webdriver.Chrome(service=service, options=options)
    return driver


# -----------------------------
# ログイン＋C選択
# -----------------------------
def login_and_select_C(driver, timeout=60):
    if not STAFF_ID or not PASSWORD:
        raise RuntimeError("環境変数 STAFF_ID / PASSWORD が未設定です")

    driver.get(LOGIN_URL)
    safe_log("ログインページにアクセス")

    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.NAME, "staff_id"))
    )

    driver.find_element(By.NAME, "staff_id").send_keys(STAFF_ID)
    driver.find_element(By.NAME, "password").send_keys(PASSWORD)
    driver.find_element(By.NAME, "send").click()
    safe_log("ログイン送信完了")

    WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.XPATH, f"//option[contains(text(),'{TENANT_TEXT}')]"))
    ).click()
    safe_log(f"プルダウンから {TENANT_TEXT} を選択")

    WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.XPATH, "//input[@value='決定']"))
    ).click()
    safe_log("決定ボタンをクリック")


# -----------------------------
# 完了画面判定
# -----------------------------
def is_report_completed(driver, timeout=60):
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located(
                (By.XPATH, "//a[@href='/adams/logout.php' and text()='終了する']")
            )
        )
        return True
    except Exception:
        links = driver.find_elements(By.XPATH, "//a[@href='/adams/logout.php' and text()='終了する']")
        return len(links) > 0


# -----------------------------
# デバッグログ
# -----------------------------
def dump_debug_info(driver, prefix=""):
    try:
        safe_log(f"{prefix}現在URL: {driver.current_url}")
    except Exception:
        pass

    try:
        safe_log(f"{prefix}タイトル: {driver.title}")
    except Exception:
        pass

    try:
        src = driver.page_source[:1000].replace("\n", " ").replace("\r", " ")
        safe_log(f"{prefix}page_source(head): {src}")
    except Exception:
        pass


# -----------------------------
# 出勤 / 退勤 / 夜勤報告
# -----------------------------
def perform_action(hour, mode, retry=3, timeout=120):
    disp = display_hour(now_jst())

    for attempt in range(1, retry + 1):
        driver = None
        try:
            driver = start_browser(hour)
            login_and_select_C(driver, timeout=timeout)

            if mode == "出勤":
                xpath_button = "//input[@value='出勤']"
            elif mode == "退勤":
                xpath_button = "//input[@value='退勤']"
            else:
                xpath_button = "//input[contains(@value,'勤務状況報告')]"

            dump_debug_info(driver, prefix=f"{disp}時：{mode}前 ")

            WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.XPATH, xpath_button))
            ).click()
            safe_log(f"{disp}時：{mode}ボタンをクリック")

            WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.XPATH, "//input[@value='内容確認']"))
            ).click()
            safe_log("内容確認ボタンをクリック")

            WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.XPATH, "//input[@value='報告']"))
            ).click()
            safe_log(f"{disp}時：{mode}報告ボタン押下")

            if is_report_completed(driver, timeout):
                safe_log(f"{disp}時：{mode}完了（終了するリンク確認）")
                if mode in ("出勤", "退勤"):
                    send_line_message(f"【夜勤】{disp}時：{mode} 完了")
                return True

            raise Exception("完了画面が確認できません")

        except TimeoutException as e:
            safe_log(f"{disp}時：{mode}でタイムアウト (試行{attempt}/{retry})")
            dump_debug_info(driver, prefix=f"{disp}時：{mode}失敗時 ")
            if attempt == retry:
                send_line_message(f"【夜勤】{disp}時：{mode} 失敗（最終）\nTimeoutException")
                return False
            safe_log("5秒後に再試行...")
            time.sleep(5)

        except Exception as e:
            safe_log(f"{disp}時：{mode}でエラー (試行{attempt}/{retry}): {type(e).__name__}: {e}")
            dump_debug_info(driver, prefix=f"{disp}時：{mode}失敗時 ")
            if attempt == retry:
                send_line_message(f"【夜勤】{disp}時：{mode} 失敗（最終）\n{type(e).__name__}: {e}")
                return False
            safe_log("5秒後に再試行...")
            time.sleep(5)

        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass

    return False


# -----------------------------
# メイン処理
# -----------------------------
def run_night_work():
    safe_log("====== 夜勤処理開始 ======")
    send_line_message("【夜勤】処理開始")

    perform_action(100, "出勤")

    night_hours = create_night_hours()
    now = now_jst()

    current_hour = None
    for h, t in night_hours:
        if (t - timedelta(minutes=tolerance_minutes)) <= now <= t.replace(minute=59):
            current_hour = h
            break

    if current_hour is None:
        for h, t in night_hours:
            if now < t:
                wait_sec = max((t - now).total_seconds(), 0)
                m, s = divmod(int(wait_sec), 60)
                safe_log(f"次の報告まで待機 {m}分{s}秒 (目標 {t})")
                time.sleep(wait_sec)
                current_hour = h
                break

    if current_hour is None:
        safe_log("夜勤時間帯の算出に失敗")
        send_line_message("【夜勤】夜勤時間帯の算出に失敗")
        return

    perform_action(current_hour, "勤務状況報告")

    for h, t in night_hours:
        if h <= current_hour:
            continue

        offset = random.randint(-tolerance_minutes, tolerance_minutes)
        wait_sec = max((t - now_jst()).total_seconds() + offset * 60, 0)
        m, s = divmod(int(wait_sec), 60)
        safe_log(f"{h-1}時報告後、次まで待機 {m}分{s}秒")
        time.sleep(wait_sec)

        perform_action(h, "勤務状況報告")

        if h == 30:
            safe_log("30時報告完了。退勤待機へ移行")
            break

    now = now_jst()
    target = datetime.combine(
        now.date(),
        datetime.min.time(),
        tzinfo=JST
    ).replace(hour=9, minute=5)

    if now.hour >= 10:
        target += timedelta(days=1)

    offset = random.randint(-tolerance_minutes, tolerance_minutes)
    target += timedelta(minutes=offset)

    wait_sec = max((target - now_jst()).total_seconds(), 0)
    m, s = divmod(int(wait_sec), 60)
    safe_log(f"退勤まで待機 {m}分{s}秒 (予定 {target})")
    time.sleep(wait_sec)

    perform_action(200, "退勤")

    safe_log("====== 全処理完了 ======")
    send_line_message("【夜勤】全処理完了")


if __name__ == "__main__":
    run_night_work()
