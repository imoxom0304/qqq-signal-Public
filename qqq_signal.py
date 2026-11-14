#!/usr/bin/env python3
"""④ レバナス増額シグナル判定 (QQQの終値と200日線)

ルール
  発動: QQQの終値が200日線を割ったら 3万円/日 に増額
  解除: QQQの終値が5営業日連続で200日線を上回ったら 1.5万円/日 に戻す

出力
  status.json  ... 表示ページ(CodePen)が読むファイル
  ntfy通知     ... 環境変数 NTFY_TOPIC があればiPhoneへ通知

標準ライブラリだけで動きます。
"""
import argparse
import csv
import io
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

TICKER = "QQQ"
MA_DAYS = 200
RELEASE_DAYS = 5
NORMAL_MAN = 1.5   # 万円/日
BOOST_MAN = 3.0    # 万円/日
STALE_DAYS = 5     # 最新の終値がこれより古ければ「データが古い」として知らせる
JST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (compatible; qqq-signal/1.0)"}


# ---------- データ取得 (上から順に試す) ----------

def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def fetch_yahoo():
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{TICKER}"
           "?range=2y&interval=1d&events=history")
    res = json.loads(http_get(url))["chart"]["result"][0]
    out = []
    for ts, close in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
        if close is None:
            continue
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        out.append((day, float(close)))
    return out


def fetch_stooq():
    txt = http_get(f"https://stooq.com/q/d/l/?s={TICKER.lower()}.us&i=d")
    rows = csv.DictReader(io.StringIO(txt))
    return [(r["Date"], float(r["Close"])) for r in rows
            if r.get("Close") not in (None, "", "null")]


def fetch_twelvedata():
    key = os.environ.get("TWELVE_DATA_KEY")
    if not key:
        raise RuntimeError("TWELVE_DATA_KEY が未設定")
    url = ("https://api.twelvedata.com/time_series"
           f"?symbol={TICKER}&interval=1day&outputsize=800&apikey={key}")
    values = json.loads(http_get(url))["values"]
    return sorted((v["datetime"], float(v["close"])) for v in values)


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return [(r["date"], float(r["close"])) for r in csv.DictReader(f)]


def fetch_series():
    errors = []
    for name, fn in (("yahoo", fetch_yahoo), ("stooq", fetch_stooq),
                     ("twelvedata", fetch_twelvedata)):
        try:
            series = clean(fn())
            if len(series) >= MA_DAYS + 30:
                return name, series
            errors.append(f"{name}: データ不足({len(series)}件)")
        except Exception as e:  # どの取得元も失敗しうるので次へ
            errors.append(f"{name}: {type(e).__name__}: {e}")
    raise RuntimeError(" / ".join(errors))


def clean(series):
    """日付で並べ、重複日は後勝ちにする。"""
    return sorted(dict(series).items())


# ---------- 判定 ----------

def compute(series):
    """各日の状態を返す。state は 'normal'(通常) か 'boost'(増額中)。
    event は 'trigger'(その日に発動) / 'release'(その日に解除) / None。"""
    closes = [c for _, c in series]
    rows = []
    state, above = None, 0
    for i in range(MA_DAYS - 1, len(series)):
        day, close = series[i]
        ma = sum(closes[i - MA_DAYS + 1:i + 1]) / MA_DAYS  # 当日の終値を含む200日平均
        event = None
        if state is None:                      # 最初の日だけ、位置で決める
            state = "boost" if close < ma else "normal"
        elif close < ma:                       # 割ったら発動(すでに発動中なら継続)
            if state == "normal":
                event = "trigger"
            state, above = "boost", 0
        elif close > ma:
            if state == "boost":
                above += 1
                if above >= RELEASE_DAYS:      # 5営業日連続で上回ったら解除
                    state, above, event = "normal", 0, "release"
            else:
                above = 0
        else:                                  # ちょうど同値の日は連続を切る
            above = 0
        rows.append({"date": day, "close": close, "ma200": ma,
                     "state": state, "above": above, "event": event})
    return rows


def build_status(rows, source):
    last = rows[-1]
    gap = (last["close"] / last["ma200"] - 1) * 100

    # いまの状態が始まった日
    since = last["date"]
    for r in reversed(rows):
        if r["state"] != last["state"]:
            break
        since = r["date"]

    if last["event"] == "release":
        label, daily = "通常に戻す", NORMAL_MAN
        title = f"🔵 通常に戻す {NORMAL_MAN:g}万円/日"
        body = (f"レバナスの積立を {NORMAL_MAN:g}万円/日 に戻す。"
                f"QQQは{RELEASE_DAYS}営業日連続で200日線の上。")
        priority = 4
    elif last["state"] == "boost":
        label, daily = "発動中", BOOST_MAN
        title = f"🔴 発動中 {BOOST_MAN:g}万円/日"
        if last["event"] == "trigger":
            body = f"本日発動。レバナスの積立を {BOOST_MAN:g}万円/日 に変更する。"
            priority = 4
        else:
            body = f"継続中（{since}〜）。"
            priority = 3
        if last["close"] > last["ma200"]:
            body += f"解除まであと{RELEASE_DAYS - last['above']}営業日（{last['above']}日連続で上）。"
        else:
            body += "200日線の下。"
    else:
        label, daily = "通常", NORMAL_MAN
        title = f"🟢 通常 {NORMAL_MAN:g}万円/日"
        body = "変更なし。"
        priority = 3

    body += (f" QQQ {last['close']:.2f} / 200日線 {last['ma200']:.2f}"
             f"（{gap:+.1f}%）・米国{last['date'][5:].replace('-', '/')}終値")

    recent = [{"date": r["date"], "close": round(r["close"], 2),
               "ma200": round(r["ma200"], 2), "state": r["state"]}
              for r in rows[-10:]]
    return {
        "label": label,
        "state": last["state"],
        "event": last["event"],
        "daily_man": daily,
        "title": title,
        "message": body,
        "priority": priority,
        "data_date": last["date"],
        "close": round(last["close"], 2),
        "ma200": round(last["ma200"], 2),
        "gap_pct": round(gap, 2),
        "above_count": last["above"],
        "days_to_release": (RELEASE_DAYS - last["above"]
                            if last["state"] == "boost" and last["close"] > last["ma200"]
                            else None),
        "since": since,
        "source": source,
        "recent": recent,
        "updated_at": datetime.now(JST).isoformat(timespec="seconds"),
    }


# ---------- 通知 ----------

def notify(title, message, priority=3, dry_run=False):
    topic = os.environ.get("NTFY_TOPIC")
    payload = {"topic": topic, "title": title, "message": message, "priority": priority}
    if dry_run or not topic:
        print("[通知(送信せず)]", json.dumps(payload, ensure_ascii=False))
        return
    req = urllib.request.Request(
        "https://ntfy.sh/", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=30).read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="status.json")
    ap.add_argument("--csv", help="テスト用: date,close のCSVを読む(取得しない)")
    ap.add_argument("--dry-run", action="store_true", help="通知を送らず表示だけ")
    args = ap.parse_args()

    try:
        if args.csv:
            source, series = "csv", clean(read_csv(args.csv))
        else:
            source, series = fetch_series()
        if len(series) < MA_DAYS + 30:
            raise RuntimeError(f"データ不足({len(series)}件)")
        status = build_status(compute(series), source)

        age = (datetime.now(JST).date()
               - datetime.strptime(status["data_date"], "%Y-%m-%d").date()).days
        if not args.csv and age > STALE_DAYS:
            raise RuntimeError(f"最新の終値が{age}日前({status['data_date']})のまま更新されていない")
    except Exception as e:
        notify("⚠️ 取得失敗", f"{type(e).__name__}: {e}", priority=4, dry_run=args.dry_run)
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: status[k] for k in
                      ("label", "state", "data_date", "close", "ma200", "gap_pct")},
                     ensure_ascii=False))
    notify(status["title"], status["message"], status["priority"], dry_run=args.dry_run)


if __name__ == "__main__":
    main()
