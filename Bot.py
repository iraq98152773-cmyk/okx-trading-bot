import os
import time
import json
import hmac
import base64
import hashlib
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone

import requests


# =========================
# إعدادات البوت
# =========================

BASE_URL = os.getenv("OKX_BASE_URL", "https://www.okx.com")

API_KEY = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

# true = تداول حقيقي
LIVE_TRADING = os.getenv("LIVE_TRADING", "false").lower() == "true"

# العملة والسوق
INST_ID = os.getenv("INST_ID", "BTC-USDT")

# استراتيجية بسيطة: EMA 9 / EMA 21
FAST_EMA = 9
SLOW_EMA = 21

# نسبة الرصيد المستخدمة في الصفقة
# لا يستخدم كامل الرصيد تلقائياً
TRADE_ALLOCATION = Decimal("0.20")

# أقل فترة بين الصفقات
COOLDOWN_SECONDS = 300


# =========================
# أدوات OKX
# =========================

def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sign(ts, method, path, body=""):
    message = ts + method.upper() + path + body

    mac = hmac.new(
        SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256
    )

    return base64.b64encode(mac.digest()).decode()


def private_request(method, path, body=None, params=None):
    if not API_KEY or not SECRET_KEY or not PASSPHRASE:
        raise RuntimeError("مفاتيح OKX غير موجودة.")

    body_text = json.dumps(body, separators=(",", ":")) if body else ""

    ts = timestamp()

    headers = {
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": sign(ts, method, path + (
            "?" + requests.compat.urlencode(params)
            if params else ""
        ), body_text),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json",
    }

    url = BASE_URL + path

    response = requests.request(
        method,
        url,
        headers=headers,
        params=params,
        data=body_text,
        timeout=20
    )

    response.raise_for_status()

    result = response.json()

    if result.get("code") != "0":
        raise RuntimeError(
            f"OKX Error {result.get('code')}: {result.get('msg')}"
        )

    return result


def public_request(path, params=None):
    response = requests.get(
        BASE_URL + path,
        params=params,
        timeout=20
    )

    response.raise_for_status()

    result = response.json()

    if result.get("code") != "0":
        raise RuntimeError(
            f"OKX Error {result.get('code')}: {result.get('msg')}"
        )

    return result


# =========================
# السوق
# =========================

def get_candles():
    result = public_request(
        "/api/v5/market/candles",
        {
            "instId": INST_ID,
            "bar": "5m",
            "limit": "100"
        }
    )

    candles = result["data"]

    # OKX ترجع الأحدث أولاً
    candles.reverse()

    return [
        {
            "close": Decimal(c[4]),
            "confirm": c[8]
        }
        for c in candles
    ]


def get_price():
    result = public_request(
        "/api/v5/market/ticker",
        {"instId": INST_ID}
    )

    return Decimal(result["data"][0]["last"])


# =========================
# EMA
# =========================

def ema(values, period):
    if len(values) < period:
        return None

    multiplier = Decimal("2") / Decimal(period + 1)

    value = sum(values[:period]) / Decimal(period)

    for price in values[period:]:
        value = (price - value) * multiplier + value

    return value


def get_signal():
    candles = get_candles()

    # نستخدم فقط الشموع المغلقة
    closes = [
        x["close"]
        for x in candles
        if x["confirm"] == "1"
    ]

    if len(closes) < SLOW_EMA + 2:
        return "WAIT", None, None

    previous = closes[:-1]

    fast_now = ema(closes, FAST_EMA)
    slow_now = ema(closes, SLOW_EMA)

    fast_prev = ema(previous, FAST_EMA)
    slow_prev = ema(previous, SLOW_EMA)

    if None in (fast_now, slow_now, fast_prev, slow_prev):
        return "WAIT", fast_now, slow_now

    # تقاطع صاعد
    if fast_prev <= slow_prev and fast_now > slow_now:
        return "BUY", fast_now, slow_now

    # تقاطع هابط
    if fast_prev >= slow_prev and fast_now < slow_now:
        return "SELL", fast_now, slow_now

    return "HOLD", fast_now, slow_now


# =========================
# الرصيد
# =========================

def get_balance(currency):
    result = private_request(
        "GET",
        "/api/v5/account/balance",
        params={"ccy": currency}
    )

    details = result["data"][0]["details"]

    for item in details:
        if item["ccy"] == currency:
            return Decimal(item["availBal"])

    return Decimal("0")


# =========================
# معلومات السوق
# =========================

def get_instrument():
    result = public_request(
        "/api/v5/public/instruments",
        {
            "instType": "SPOT",
            "instId": INST_ID
        }
    )

    return result["data"][0]


def normalize_size(size, lot_size):
    size = Decimal(size)
    lot_size = Decimal(lot_size)

    units = (size / lot_size).to_integral_value(
        rounding=ROUND_DOWN
    )

    return units * lot_size


# =========================
# تنفيذ الأوامر
# =========================

def place_market_order(side, size):

    if not LIVE_TRADING:
        print(
            f"[DEMO] كان سيتم تنفيذ {side.upper()} "
            f"{size} {INST_ID}"
        )
        return

    body = {
        "instId": INST_ID,
        "tdMode": "cash",
        "side": side,
        "ordType": "market",
        "sz": str(size)
    }

    result = private_request(
        "POST",
        "/api/v5/trade/order",
        body=body
    )

    print("ORDER:", json.dumps(result, indent=2))


# =========================
# منطق التداول
# =========================

def trade():

    signal, fast, slow = get_signal()

    price = get_price()

    print()
    print("==============================")
    print("OKX Trading Bot")
    print("==============================")
    print("السوق:", INST_ID)
    print("السعر:", price)
    print("الإشارة:", signal)

    if fast:
        print("EMA", FAST_EMA, ":", fast)

    if slow:
        print("EMA", SLOW_EMA, ":", slow)

    print("Live Trading:", LIVE_TRADING)

    if signal == "WAIT" or signal == "HOLD":
        print("لا توجد صفقة حالياً.")
        return

    instrument = get_instrument()
    lot_size = Decimal(instrument["lotSz"])
    min_size = Decimal(instrument["minSz"])

    # شراء
    if signal == "BUY":

        usdt = get_balance("USDT")

        amount = usdt * TRADE_ALLOCATION

        if amount <= Decimal("1"):
            print("رصيد USDT غير كافٍ للشراء.")
            return

        size = amount / price
        size = normalize_size(size, lot_size)

        if size < min_size:
            print("حجم الصفقة أقل من الحد الأدنى.")
            return

        print(
            f"شراء {size} BTC تقريباً "
            f"باستخدام {amount} USDT"
        )

        place_market_order("buy", size)

    # بيع
    elif signal == "SELL":

        btc = get_balance("BTC")

        if btc <= Decimal("0"):
            print("لا يوجد BTC للبيع.")
            return

        size = normalize_size(
            btc * TRADE_ALLOCATION,
            lot_size
        )

        if size < min_size:
            print("حجم البيع أقل من الحد الأدنى.")
            return

        print(f"بيع {size} BTC")

        place_market_order("sell", size)


# =========================
# التشغيل المستمر
# =========================

def main():

    print("بدأ تشغيل وكيل التداول...")
    print("LIVE_TRADING =", LIVE_TRADING)

    if LIVE_TRADING:
        print("⚠️ التداول الحقيقي مفعّل.")
    else:
        print("وضع الاختبار مفعّل - لن يتم إرسال أوامر حقيقية.")

    while True:

        try:
            trade()

        except Exception as e:
            print("ERROR:", str(e))

        time.sleep(COOLDOWN_SECONDS)


if __name__ == "__main__":
    main()
