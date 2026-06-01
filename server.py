"""
freee人事労務 MCP サーバー
勤怠の参照・打刻を Claude Desktop から操作できるようにする
"""

import calendar
import json
import os
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import jpholiday
from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv()

# ── 定数 ────────────────────────────────────────────
CLIENT_ID = os.getenv("FREEE_CLIENT_ID")
CLIENT_SECRET = os.getenv("FREEE_CLIENT_SECRET")
REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"
TOKEN_FILE = Path(__file__).parent / ".tokens.json"

AUTH_URL = "https://accounts.secure.freee.co.jp/public_api/authorize"
TOKEN_URL = "https://accounts.secure.freee.co.jp/public_api/token"
HR_BASE = "https://api.freee.co.jp/hr"

mcp = FastMCP("freee-hr")


# ── トークン管理 ─────────────────────────────────────
def load_tokens() -> dict | None:
    if TOKEN_FILE.exists():
        return json.loads(TOKEN_FILE.read_text())
    return None


def save_tokens(tokens: dict) -> None:
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2))


def get_auth_url() -> str:
    return (
        f"{AUTH_URL}"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=read+write"
        f"&prompt=select_company"
    )


async def exchange_code(code: str) -> dict:
    """認可コード → アクセストークン"""
    async with httpx.AsyncClient() as client:
        resp = await client.post(TOKEN_URL, data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "redirect_uri": REDIRECT_URI,
        })
        resp.raise_for_status()
        return resp.json()


async def refresh_access_token(refresh_token: str) -> dict:
    """リフレッシュトークンでアクセストークンを更新"""
    async with httpx.AsyncClient() as client:
        resp = await client.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": refresh_token,
        })
        resp.raise_for_status()
        tokens = resp.json()
        save_tokens(tokens)
        return tokens


async def get_valid_token() -> str:
    """有効なアクセストークンを返す。期限切れなら自動更新。"""
    tokens = load_tokens()
    if tokens is None:
        raise RuntimeError("未認証です。まず authorize ツールを実行してください。")

    try:
        tokens = await refresh_access_token(tokens["refresh_token"])
    except Exception:
        raise RuntimeError("トークンの更新に失敗しました。再度 authorize を実行してください。")

    return tokens["access_token"]


async def hr_get(path: str, params: dict = None) -> dict:
    token = await get_valid_token()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{HR_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
        )
        if not resp.is_success:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")
        return resp.json()


async def hr_put(path: str, body: dict) -> dict:
    token = await get_valid_token()
    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{HR_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
        if not resp.is_success:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")
        return resp.json()


# ── ヘルパー ─────────────────────────────────────────
def _add_minutes(time_str: str, minutes: int) -> str:
    """HH:MM 形式の時刻に分を加算して HH:MM を返す"""
    h, m = map(int, time_str.split(":"))
    total = h * 60 + m + minutes
    return f"{total // 60:02d}:{total % 60:02d}"


def _is_paid_holiday(wr: dict) -> bool:
    """
    有給休暇取得済みかどうかを判定する。

    判定ロジック（2段階）:
    1. paid_holiday フィールド（float: 1.0=全休, 0.5=半休）
    2. paid_holidays 配列の days/mins いずれかが 0 より大きい
    また時間単位有給（half_paid_holiday_mins）も考慮する。
    """
    # ── アプローチ1: paid_holiday / half_paid_holiday_mins で判定 ──
    paid_holiday = wr.get("paid_holiday") or 0          # float: 1.0=全休, 0.5=半休
    half_paid_holiday_mins = wr.get("half_paid_holiday_mins") or 0  # 時間単位有給（分）

    if paid_holiday > 0 or half_paid_holiday_mins > 0:
        return True

    # ── アプローチ2: paid_holidays 配列で判定（より厳密） ──
    paid_holidays = wr.get("paid_holidays") or []
    if any(ph.get("days", 0) > 0 or ph.get("mins", 0) > 0 for ph in paid_holidays):
        return True

    return False


# ── MCP ツール ───────────────────────────────────────

@mcp.tool()
def get_auth_link() -> str:
    """
    freee認可URLを返す。
    初回セットアップ時に実行し、表示されたURLをブラウザで開いて認可コードを取得する。
    """
    url = get_auth_url()
    webbrowser.open(url)
    return (
        f"ブラウザを開きました。freeeにログインして「許可する」を押してください。\n"
        f"認可コードが表示されたら、次に set_auth_code ツールにそのコードを渡してください。\n\n"
        f"認可URL: {url}"
    )


@mcp.tool()
async def set_auth_code(code: str) -> str:
    """
    認可コードをアクセストークンに交換して保存する。
    get_auth_link の後にブラウザに表示されたコードを渡す。

    Args:
        code: ブラウザに表示された認可コード
    """
    try:
        tokens = await exchange_code(code)
        save_tokens(tokens)
        return "✅ 認証完了！トークンを保存しました。freee人事労務に接続できます。"
    except Exception as e:
        return f"❌ 認証失敗: {e}"


@mcp.tool()
async def get_me() -> str:
    """
    ログインユーザーの情報（company_id, employee_id）を取得する。
    他のツールで必要になる employee_id を確認するために使う。
    """
    data = await hr_get("/api/v1/users/me")
    companies = data.get("companies", [])
    lines = ["【ログインユーザー情報】"]
    for c in companies:
        lines.append(f"  事業所: {c.get('name')} (company_id={c.get('id')})")
        lines.append(f"  従業員ID: {c.get('employee_id')}")
        lines.append(f"  権限: {c.get('role')}")
    return "\n".join(lines)


@mcp.tool()
async def get_work_record(employee_id: int, company_id: int, target_date: str) -> str:
    """
    指定日の勤怠情報を取得する。

    Args:
        employee_id: 従業員ID（get_me で確認）
        company_id: 事業所ID（get_me で確認）
        target_date: 対象日（例: 2026-05-15）
    """
    data = await hr_get(
        f"/api/v1/employees/{employee_id}/work_records/{target_date}",
        params={"company_id": company_id},
    )
    wr = data.get("work_record", data)
    clock_in = wr.get("clock_in_at", "未打刻")
    clock_out = wr.get("clock_out_at", "未打刻")
    return (
        f"【{target_date} の勤怠】\n"
        f"  出勤: {clock_in}\n"
        f"  退勤: {clock_out}\n"
        f"  勤務時間: {wr.get('total_work_mins', 0)} 分\n"
        f"  残業時間: {wr.get('overtime_work_mins', 0)} 分\n\n"
        f"【RAW レスポンス（フィールド確認用）】\n"
        f"{json.dumps(data, ensure_ascii=False, indent=2)}"
    )


@mcp.tool()
async def update_work_record(
    employee_id: int,
    company_id: int,
    target_date: str,
    clock_in_at: str,
    clock_out_at: str,
    break_mins: int = 60,
) -> str:
    """
    勤怠を入力・更新する。

    Args:
        employee_id: 従業員ID
        company_id: 事業所ID
        target_date: 対象日（例: 2026-05-15）
        clock_in_at: 出勤時刻（例: 09:00）
        clock_out_at: 退勤時刻（例: 18:00）
        break_mins: 休憩時間（分）デフォルト60分
    """
    # freee API の要求フォーマット: "YYYY-MM-DD HH:MM:SS"（Tなし・タイムゾーンなし）
    clock_in = f"{target_date} {clock_in_at}:00"
    clock_out = f"{target_date} {clock_out_at}:00"

    break_start = f"{target_date} 12:00:00"
    break_end = f"{target_date} {_add_minutes('12:00', break_mins)}:00"

    body = {
        "company_id": company_id,
        "work_record_segments": [
            {
                "clock_in_at": clock_in,
                "clock_out_at": clock_out,
            }
        ]
    }

    await hr_put(
        f"/api/v1/employees/{employee_id}/work_records/{target_date}",
        body,
    )
    return (
        f"✅ {target_date} の勤怠を更新しました\n"
        f"  出勤: {clock_in_at}\n"
        f"  退勤: {clock_out_at}\n"
        f"  休憩: {break_mins}分"
    )


@mcp.tool()
async def get_work_records_month(
    employee_id: int,
    company_id: int,
    year: int,
    month: int,
) -> str:
    """
    指定月の勤怠一覧を取得する。

    Args:
        employee_id: 従業員ID
        company_id: 事業所ID
        year: 年（例: 2026）
        month: 月（例: 5）
    """
    data = await hr_get(
        f"/api/v1/employees/{employee_id}/work_record_summaries/{year}/{month}",
        params={"company_id": company_id},
    )
    summary = data.get("work_record_summary", data)
    total = summary.get("total_work_mins", 0)
    overtime = summary.get("total_overtime_work_mins", 0)
    return (
        f"【{year}年{month}月 勤怠サマリー】\n"
        f"  総勤務時間: {total // 60}時間{total % 60}分\n"
        f"  総残業時間: {overtime // 60}時間{overtime % 60}分"
    )


@mcp.tool()
async def check_monthly_attendance(
    employee_id: int,
    company_id: int,
    year: int,
    month: int,
) -> str:
    """
    指定月の営業日ごとに勤怠を確認し、未打刻・未申請の日を警告する。

    Args:
        employee_id: 従業員ID
        company_id: 事業所ID
        year: 年（例: 2026）
        month: 月（例: 5）
    """
    num_days = calendar.monthrange(year, month)[1]
    business_days = [
        date(year, month, d)
        for d in range(1, num_days + 1)
        if date(year, month, d).weekday() < 5
        and not jpholiday.is_holiday(date(year, month, d))
    ]

    warnings: list[str] = []
    ok_count = 0
    paid_count = 0
    errors: list[str] = []

    for day in business_days:
        date_str = day.isoformat()
        try:
            data = await hr_get(
                f"/api/v1/employees/{employee_id}/work_records/{date_str}",
                params={"company_id": company_id},
            )
            wr = data.get("work_record", data)
            clock_in = wr.get("clock_in_at")

            # ── 修正箇所: 正しいフィールド名で有給判定 ──
            is_paid = _is_paid_holiday(wr)

            if clock_in is None and not is_paid:
                warnings.append(date_str)
            elif is_paid:
                paid_count += 1
            else:
                ok_count += 1

        except Exception as e:
            errors.append(f"{date_str}: {e}")

    lines = [f"【{year}年{month}月 勤怠チェック結果】"]
    lines.append(f"  営業日数: {len(business_days)} 日")
    lines.append(f"  打刻あり: {ok_count} 日")
    lines.append(f"  有給休暇: {paid_count} 日")
    lines.append(f"  ⚠️ 未打刻（要確認）: {len(warnings)} 日")

    if warnings:
        lines.append("\n【未打刻日一覧】")
        for d in warnings:
            lines.append(f"  ⚠️ {d}")

    if errors:
        lines.append("\n【取得エラー】")
        for e in errors:
            lines.append(f"  ❌ {e}")

    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
