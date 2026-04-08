# =============================
# 夜勤報告スクリプト 単発実行版（環境変数対応・Cloud Run Job向け）
#  - checkin   : 出勤だけ実行（即時）
#  - report XX : 指定時刻の勤務状況報告だけ実行（0〜6分ランダム待機）
#  - checkout  : 退勤だけ実行（0〜6分ランダム待機）
#  - ログイン後、不定期の「会社からのお知らせ」が出たら閉じる
#  - 例外時も「実は成功済み / 終了済み」なら完了扱いにする
#  - Cloud Run では option直クリックではなく Select でテナント選択
# =============================

import os
import sys
import time
import logging
import random

import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.remote.remote_connection import RemoteConnection
from selenium.common.exceptions import TimeoutException

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

# ランダム待機用
ENABLE_RANDOM_DELAY = os.getenv("ENABLE_RANDOM_DELAY", "1").strip() == "1"
RANDOM_DELAY_MAX_MINUTES = int(os.getenv("RANDOM_DELAY_MAX_MINUTES", "6").strip())

# -----------------------------
# 基本設定
# -----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)

RemoteConnection.set_timeout = lambda *_: None

def safe_log(msg: str):
    logging.info(msg)
    print(msg, flush=True)

# -----------------------------
# ランダム待機
# -----------------------------
def random_delay_if_enabled(label: str):
    if not ENABLE_RANDOM_DELAY:
        safe_log(f"{label}：ランダム待機なし")
        return

    if RANDOM_DELAY_MAX_MINUTES <= 0:
        safe_log(f"{label}：RANDOM_DELAY_MAX_MINUTES <= 0 のため待機なし")
        return

    wait_sec = random.randint(0, RANDOM_DELAY_MAX_MINUTES * 60)
    m, s = divmod(wait_sec, 60)
    safe_log(f"{label}：ランダム待機 {m}分{s}秒")
    time.sleep(wait_sec)

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
        safe_log(f"LINE通知例外：{type(e).__name__}: {e}")
        return False

# -----------------------------
# 共通ブラウザ起動関数
# -----------------------------
def start_browser(log_tag):
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-features=VizDisplayCompositor")
    options.add_argument("--window-size=1400,1100")

    if CHROME_BIN:
        options.binary_location = CHROME_BIN

    service = Service(
        executable_path=CHROMEDRIVER_PATH,
        log_output=f"/tmp/chromedriver_{log_tag}.log",
    )

    driver = webdriver.Chrome(service=service, options=options)
    return driver

# -----------------------------
# デバッグ
# -----------------------------
def dump_debug_info(driver, prefix=""):
    if not driver:
        return

    try:
        safe_log(f"{prefix}現在URL: {driver.current_url}")
    except Exception as e:
        safe_log(f"{prefix}現在URL取得失敗: {type(e).__name__}: {e}")

    try:
        safe_log(f"{prefix}タイトル: {driver.title}")
    except Exception as e:
        safe_log(f"{prefix}タイトル取得失敗: {type(e).__name__}: {e}")

    try:
        src = driver.page_source[:2500].replace("\n", " ").replace("\r", " ")
        safe_log(f"{prefix}page_source(head): {src}")
    except Exception as e:
        safe_log(f"{prefix}page_source取得失敗: {type(e).__name__}: {e}")

# -----------------------------
# 共通待機＆クリック
# -----------------------------
def wait_and_click(driver, xpath, label, timeout=60):
    elem = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.XPATH, xpath))
    )
    elem.click()
    safe_log(f"{label} をクリック")

def wait_until_any(driver, xpaths, label, timeout=60):
    end = time.time() + timeout
    last_error = None

    while time.time() < end:
        for xp in xpaths:
            try:
                WebDriverWait(driver, 2).until(
                    EC.element_to_be_clickable((By.XPATH, xp))
                )
                safe_log(f"{label} を確認")
                return xp
            except Exception as e:
                last_error = e
                continue
        time.sleep(0.5)

    raise TimeoutException(f"{label} が見つかりません last_error={last_error}")

# -----------------------------
# お知らせが出ていたら閉じる
# -----------------------------
def close_notice_if_present(driver, timeout=3):
    notice_close_xpath = "//input[@name='send' and @type='submit' and @value='閉じる']"

    try:
        elem = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.XPATH, notice_close_xpath))
        )
        elem.click()
        safe_log("会社からのお知らせを閉じる")
        time.sleep(1)
        return True
    except Exception:
        safe_log("会社からのお知らせなし")
        return False

# -----------------------------
# テナント選択
# -----------------------------
def select_tenant(driver, timeout=60):
    try:
        select_elem = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.NAME, "order_id"))
        )
        safe_log("テナント選択プルダウンを確認")
    except Exception as e:
        safe_log(f"テナント選択プルダウン待ちで失敗: {type(e).__name__}: {e}")
        dump_debug_info(driver, prefix="テナント選択失敗時 ")
        raise

    try:
        select_box = Select(select_elem)
        matched = False

        for i, opt in enumerate(select_box.options):
            text = opt.text.strip()
            safe_log(f"テナント候補[{i}]: {text}")
            if TENANT_TEXT in text:
                select_box.select_by_visible_text(opt.text)
                safe_log(f"プルダウンから{TENANT_TEXT} を選択")
                matched = True
                break

        if not matched:
            raise RuntimeError(f"TENANT_TEXT='{TENANT_TEXT}' を含む選択肢が見つかりません")
    except Exception as e:
        safe_log(f"テナント選択で失敗: {type(e).__name__}: {e}")
        dump_debug_info(driver, prefix="テナント選択失敗時 ")
        raise

# -----------------------------
# ログイン＋テナント選択＋決定
# -----------------------------
def login_and_select_tenant(driver, timeout=60):
    if not STAFF_ID or not PASSWORD:
        raise RuntimeError("環境変数 STAFF_ID / PASSWORD が未設定です")

    driver.get(LOGIN_URL)
    safe_log("ログインページにアクセス")

    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.NAME, "staff_id"))
    )

    staff = driver.find_element(By.NAME, "staff_id")
    password = driver.find_element(By.NAME, "password")

    staff.clear()
    password.clear()

    staff.send_keys(STAFF_ID)
    password.send_keys(PASSWORD)

    wait_and_click(driver, "//input[@name='send']", "ログイン送信ボタン", timeout=timeout)

    close_notice_if_present(driver, timeout=3)

    select_tenant(driver, timeout=timeout)

    wait_and_click(driver, "//input[@value='決定']", "決定ボタン", timeout=timeout)

    try:
        wait_until_any(
            driver,
            [
                "//input[@value='出勤']",
                "//input[@value='退勤']",
                "//input[contains(@value,'勤務状況報告')]",
            ],
            "決定後の主要ボタン",
            timeout=timeout
        )
    except Exception as e:
        safe_log(f"決定後の主要ボタン待ちで失敗: {type(e).__name__}: {e}")
        dump_debug_info(driver, prefix="決定後失敗時 ")
        raise

# -----------------------------
# 成功/終了済み画面判定
# -----------------------------
def get_page_text(driver):
    try:
        return driver.page_source
    except Exception:
        return ""

def get_current_url(driver):
    try:
        return driver.current_url
    except Exception:
        return ""

def is_report_completed(driver, timeout=60):
    try:
        wait_until_any(
            driver,
            [
                "//a[@href='/adams/logout.php' and text()='終了する']",
                "//a[text()='終了する']",
            ],
            "終了する画面",
            timeout=timeout
        )
        return True
    except Exception:
        return False

def is_effectively_completed(driver, mode):
    url = get_current_url(driver)
    page = get_page_text(driver)

    if "/adams/logout.php" in page and "終了する" in page:
        if "勤務状況報告が完了しました" in page:
            return True
        if "この時間の報告は終了しています" in page:
            return True
        if mode in ("出勤", "退勤"):
            return True

    if "report_thanks" in url:
        return True

    if "勤務状況報告が完了しました" in page:
        return True

    if "この時間の報告は終了しています" in page:
        return True

    return False

# -----------------------------
# 単発処理
# -----------------------------
def perform_action(mode, report_hour=None, retry=3, timeout=60):
    if mode == "出勤":
        log_tag = "checkin"
        disp = "出勤"
        xpath_button = "//input[@value='出勤']"
        safe_log("出勤前：ランダム待機なし")

    elif mode == "退勤":
        log_tag = "checkout"
        disp = "退勤"
        xpath_button = "//input[@value='退勤']"
        random_delay_if_enabled("退勤前")

    else:
        if report_hour is None:
            raise ValueError("勤務状況報告には report_hour が必要です")
        log_tag = f"report_{report_hour}"
        disp = f"{report_hour}時：勤務状況報告"
        xpath_button = "//input[contains(@value,'勤務状況報告')]"
        random_delay_if_enabled(f"{report_hour}時報告前")

    for attempt in range(1, retry + 1):
        driver = None
        try:
            driver = start_browser(log_tag)
            login_and_select_tenant(driver, timeout=timeout)

            wait_and_click(driver, xpath_button, f"{disp}ボタン", timeout=timeout)

            wait_until_any(
                driver,
                ["//input[@value='内容確認']"],
                "内容確認ボタン表示",
                timeout=timeout
            )
            wait_and_click(driver, "//input[@value='内容確認']", "内容確認ボタン", timeout=timeout)

            wait_until_any(
                driver,
                ["//input[@value='報告']"],
                "報告ボタン表示",
                timeout=timeout
            )
            wait_and_click(driver, "//input[@value='報告']", f"{disp}報告ボタン", timeout=timeout)

            if is_report_completed(driver, timeout=timeout):
                safe_log(f"{disp}完了（終了するリンク確認）")
                if mode in ("出勤", "退勤"):
                    send_line_message(f"【夜勤】{disp} 完了")
                return True

            if is_effectively_completed(driver, mode):
                safe_log(f"{disp}完了（事後判定で成功/終了済み扱い）")
                if mode in ("出勤", "退勤"):
                    send_line_message(f"【夜勤】{disp} 完了")
                return True

            raise Exception("終了する画面が確認できません")

        except Exception as e:
            safe_log(f"{disp}でエラー (試行{attempt}/{retry}): {type(e).__name__}: {e}")
            dump_debug_info(driver, prefix=f"{disp}失敗時 ")

            try:
                if driver and is_effectively_completed(driver, mode):
                    safe_log(f"{disp}は事後判定で成功/終了済みのため再試行せず完了扱い")
                    if mode in ("出勤", "退勤"):
                        send_line_message(f"【夜勤】{disp} 完了")
                    return True
            except Exception:
                pass

            if attempt == retry:
                if mode in ("出勤", "退勤"):
                    send_line_message(f"【夜勤】{disp} 失敗")
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
# ラッパー関数
# -----------------------------
def run_checkin():
    return perform_action("出勤")

def run_report(hour):
    return perform_action("勤務状況報告", report_hour=hour)

def run_checkout():
    return perform_action("退勤")

# -----------------------------
# CLI
# -----------------------------
def print_usage():
    print("使い方:")
    print("  python main.py checkin")
    print("  python main.py report 28")
    print("  python main.py checkout")

def main():
    if len(sys.argv) < 2:
        print_usage()
        sys.exit(1)

    command = sys.argv[1].strip().lower()

    if command == "checkin":
        safe_log("====== 出勤単発実行開始 ======")
        ok = run_checkin()
        safe_log("====== 出勤単発実行終了 ======")
        sys.exit(0 if ok else 2)

    elif command == "report":
        if len(sys.argv) < 3:
            print("report には時刻指定が必要です。例: python main.py report 28")
            sys.exit(1)

        try:
            hour = int(sys.argv[2])
        except ValueError:
            print("時刻は整数で指定してください。例: 23, 28, 30")
            sys.exit(1)

        if hour < 23 or hour > 30:
            print("勤務状況報告の時刻は 23〜30 で指定してください。")
            sys.exit(1)

        safe_log(f"====== {hour}時 勤務状況報告単発実行開始 ======")
        ok = run_report(hour)
        safe_log(f"====== {hour}時 勤務状況報告単発実行終了 ======")
        sys.exit(0 if ok else 2)

    elif command == "checkout":
        safe_log("====== 退勤単発実行開始 ======")
        ok = run_checkout()
        safe_log("====== 退勤単発実行終了 ======")
        sys.exit(0 if ok else 2)

    else:
        print_usage()
        sys.exit(1)

if __name__ == "__main__":
    main()
