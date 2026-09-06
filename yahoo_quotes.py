#!/usr/bin/env python3
"""Yahoo Finance US stock quotes — called by Go gateway as subprocess.
Usage: echo '["AAPL","NVDA","MSFT"]' | python3 yahoo_quotes.py
Output: JSON array of {symbol, name, price, prev_close, pct, volume, high, low, turnover}
"""
import sys, json, time, os

# Cache file to avoid hitting rate limits on repeated calls
CACHE_FILE = os.path.join(os.path.dirname(__file__), ".yahoo_cache.json")
CACHE_TTL = 60  # seconds


def load_cache():
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        if time.time() - data.get("ts", 0) < CACHE_TTL:
            return data.get("quotes", {})
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def save_cache(quotes):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump({"ts": time.time(), "quotes": quotes}, f)
    except OSError:
        pass


def _make_session(proxy):
    """新版 yfinance（1.x / 0.2.5x+）强制 curl_cffi session（浏览器指纹），
    旧版用 requests session + 全局 set_session。"""
    try:
        from curl_cffi import requests as _cr
        session = _cr.Session(impersonate="chrome")
    except ImportError:
        import requests
        session = requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session


def setup_proxy():
    """Yahoo Finance 专用代理：显式挂到 session，避免全局 env 代理
    触发 PySocks IPv6 问题（PySocks 不支持 IPv6 目标地址）。"""
    proxy = os.environ.get("YAHOO_PROXY", "")
    try:
        import yfinance as yf
        if hasattr(yf, "set_session"):
            # 旧版 yfinance：全局挂 session
            yf.set_session(_make_session(proxy))
    except ImportError:
        pass


def main():
    setup_proxy()
    symbols = json.load(sys.stdin) if not sys.stdin.isatty() else sys.argv[1:]
    if not symbols:
        print("[]")
        return

    cache = load_cache()
    results = []
    need_fetch = []

    for sym in symbols:
        s = sym.upper().strip()
        if s in cache:
            results.append(cache[s])
        else:
            need_fetch.append(s)

    if need_fetch:
        try:
            import yfinance as yf
            proxy = os.environ.get("YAHOO_PROXY", "")
            for sym in need_fetch:
                try:
                    if hasattr(yf, "set_session"):
                        t = yf.Ticker(sym)
                    else:
                        t = yf.Ticker(sym, session=_make_session(proxy))
                    info = t.info
                    price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("regularMarketOpen", 0)
                    prev_close = info.get("previousClose") or info.get("regularMarketPreviousClose", 0)
                    pct = ((price - prev_close) / prev_close * 100) if prev_close else 0
                    name = info.get("shortName") or info.get("longName", sym)
                    volume = info.get("volume") or info.get("regularMarketVolume", 0)
                    high = info.get("dayHigh") or info.get("regularMarketDayHigh", price)
                    low = info.get("dayLow") or info.get("regularMarketDayLow", price)

                    q = {
                        "symbol": sym,
                        "name": name,
                        "price": price,
                        "prev_close": prev_close,
                        "pct": round(pct, 2),
                        "volume": volume,
                        "high": high,
                        "low": low,
                        "turnover": volume * price if price and volume else 0,
                    }
                    results.append(q)
                    cache[sym] = q
                except Exception as e:
                    # Return stale cache entry if available, else skip
                    if sym in cache:
                        results.append(cache[sym])
                    time.sleep(0.5)
        except ImportError:
            pass

    save_cache(cache)
    print(json.dumps(results))


if __name__ == "__main__":
    main()
