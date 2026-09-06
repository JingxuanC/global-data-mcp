"""tools.py — 美股 + 宏观舆情 MCP 的工具注册表（14 个工具）。

从 Athena py-sidecar server.py 摘出美股（yfinance）与宏观/舆情子集，
独立成项目后不再依赖 A 股工具与 factor/grpc 域。重依赖（yfinance/pandas/
stockstats）全部惰性导入，未装也可启动服务。

数据源：
  - yfinance        美股行情 / 财报 / 内部人交易 / 个股新闻 / 全球新闻
  - FRED            宏观指标（需 FRED_API_KEY）
  - stockstats      技术指标（基于 yfinance 日线）
  - Reddit RSS      舆情（stdlib urllib，零依赖）
  - StockTwits      舆情（stdlib urllib，零依赖）
  - Polymarket      预测市场概率
  - Eastmoney       7x24 全球快讯
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("global-data-mcp")

# ── Proxy setup (before any network imports) ──
# 注意：不要全局替换 socket（socks.socksocket）——PySocks 不支持 IPv6 目标地址，
# 且会污染所有连接，导致国内行情源（东财）失败。
# 代理只应作用于 yfinance（美股），通过显式 session 挂载。
_proxy = os.environ.get("YAHOO_PROXY", "")
if _proxy and not _proxy.startswith(("socks", "http")):
    # 兜底：非标准协议（如裸 host:port）按 socks5 处理
    _proxy = "socks5://" + _proxy

# Lazy imports — yfinance only needed for US stock tools
_yf = None
_yf_session = None


def _get_yf_session():
    """yfinance 专用 session：显式挂 YAHOO_PROXY，不污染全局 socket。
    仅美股请求走代理；国内源（em_get）保持直连。
    新版 yfinance（1.x / 0.2.5x+）强制 curl_cffi session（浏览器指纹），
    旧版用 requests session + 全局 set_session。"""
    global _yf_session
    if _yf_session is None:
        try:
            from curl_cffi import requests as _cr
            _yf_session = _cr.Session(impersonate="chrome")
        except ImportError:
            import requests as _r
            _yf_session = _r.Session()
        _yf_session.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
        if _proxy:
            _yf_session.proxies = {"http": _proxy, "https": _proxy}
    return _yf_session


def _get_yf():
    global _yf
    if _yf is None:
        import yfinance as yf
        if hasattr(yf, "set_session"):
            # 旧版 yfinance：全局挂 session
            yf.set_session(_get_yf_session())
        _yf = yf
    return _yf


def _ticker(symbol: str):
    """构造 yfinance Ticker。旧版走全局 set_session；
    新版（无 set_session）按实例传 curl_cffi session。"""
    yf = _get_yf()
    if hasattr(yf, "set_session"):
        return yf.Ticker(symbol.upper())
    return yf.Ticker(symbol.upper(), session=_get_yf_session())


# ── Eastmoney anti-blocking: global throttle + session reuse ──
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_EM_SESSION = None
_EM_MIN_INTERVAL = 1.0
_em_last_call = [0.0]


def _get_em_session():
    global _EM_SESSION
    if _EM_SESSION is None:
        import requests as _r
        _EM_SESSION = _r.Session()
        _EM_SESSION.headers.update({"User-Agent": _UA})
        try:
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
            _adapter = HTTPAdapter(max_retries=Retry(
                total=3, connect=3, backoff_factor=0.6,
                status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"]))
            _EM_SESSION.mount("https://", _adapter)
            _EM_SESSION.mount("http://", _adapter)
        except Exception:
            pass
    return _EM_SESSION


def em_get(url: str, params: dict = None, headers: dict = None, timeout: int = 15,
           method: str = "GET", **kwargs):
    """Eastmoney unified request: auto throttle + session reuse + default UA.
    All eastmoney.com APIs must go through this to avoid IP ban.
    method: "GET" (default) or "POST"."""
    import time as _time
    import random as _random
    wait = _EM_MIN_INTERVAL - (_time.time() - _em_last_call[0])
    if wait > 0:
        _time.sleep(wait + _random.uniform(0.1, 0.5))
    try:
        if method.upper() == "POST":
            return _get_em_session().post(url, params=params, headers=headers,
                                          timeout=timeout, **kwargs)
        return _get_em_session().get(url, params=params, headers=headers, timeout=timeout, **kwargs)
    finally:
        _em_last_call[0] = _time.time()


# ── Cache ──
CACHE_DIR = Path(os.environ.get("DATA_CACHE_DIR", Path(__file__).parent / ".data_cache"))
CACHE_TTL = int(os.environ.get("DATA_CACHE_TTL", "300"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_path(tool_name: str, *args) -> Path:
    key = tool_name + "_" + "_".join(str(a).replace("/", "_")[:40] for a in args)
    return CACHE_DIR / f"{key}.json"


def _cache_get(tool_name: str, *args) -> Optional[Any]:
    p = _cache_path(tool_name, *args)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        if time.time() - data.get("ts", 0) < CACHE_TTL:
            return data.get("value")
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _cache_set(tool_name: str, value: Any, *args) -> None:
    p = _cache_path(tool_name, *args)
    try:
        p.write_text(json.dumps({"ts": time.time(), "value": value}))
    except OSError:
        pass


# ── Toolkit interface ──
class ToolDef:
    def __init__(self, name: str, description: str, inputSchema: dict):
        self.name = name
        self.description = description
        self.inputSchema = inputSchema

    def to_dict(self):
        return {"name": self.name, "description": self.description, "inputSchema": self.inputSchema}


TOOLS: dict[str, ToolDef] = {}
HANDLERS: dict[str, callable] = {}


def tool(name: str, description: str, properties: dict, required: Optional[list] = None):
    """Decorator to register a tool."""
    def deco(fn):
        TOOLS[name] = ToolDef(name, description, {
            "type": "object",
            "properties": properties,
            "required": required or list(properties.keys()),
        })
        HANDLERS[name] = fn
        return fn
    return deco


# ═══════════════════════════════════════════════════════════════
# 1. Stock data (OHLCV)
# ═══════════════════════════════════════════════════════════════

@tool("get_stock_data", "Get OHLCV price history for a US stock. "
      "Returns CSV with Date,Open,High,Low,Close,Volume columns.",
      {"symbol": {"type": "string", "description": "Ticker symbol (e.g. AAPL, NVDA)"},
       "start_date": {"type": "string", "description": "Start date YYYY-MM-DD"},
       "end_date": {"type": "string", "description": "End date YYYY-MM-DD"}})
def get_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    cached = _cache_get("get_stock_data", symbol, start_date, end_date)
    if cached:
        return cached
    try:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        end_inclusive = (end_dt + timedelta(days=1)).strftime("%Y-%m-%d")
        t = _ticker(symbol)
        data = t.history(start=start_date, end=end_inclusive)
        if data.empty:
            result = f"NO_DATA: {symbol} — no price data between {start_date} and {end_date}"
        else:
            if data.index.tz is not None:
                data.index = data.index.tz_localize(None)
            for col in ["Open", "High", "Low", "Close"]:
                if col in data.columns:
                    data[col] = data[col].round(2)
            csv_str = data.to_csv()
            result = (f"# {symbol} from {start_date} to {end_date}\n"
                      f"# Records: {len(data)}\n\n{csv_str}")
        _cache_set("get_stock_data", result, symbol, start_date, end_date)
        return result
    except Exception as e:
        return f"ERROR: {e}"


# ═══════════════════════════════════════════════════════════════
# 2. Fundamentals
# ═══════════════════════════════════════════════════════════════

@tool("get_fundamentals", "Get company fundamentals: market cap, PE, EPS, revenue, margins, ROE, etc.",
      {"symbol": {"type": "string", "description": "Ticker symbol (e.g. AAPL)"}})
def get_fundamentals(symbol: str) -> str:
    cached = _cache_get("get_fundamentals", symbol)
    if cached:
        return cached
    try:
        t = _ticker(symbol)
        info = t.info
        if not info or not info.get("longName"):
            result = f"NO_DATA: {symbol} — no fundamentals returned"
        else:
            fields = [
                ("Name", info.get("longName")),
                ("Sector", info.get("sector")), ("Industry", info.get("industry")),
                ("Market Cap", info.get("marketCap")),
                ("PE Ratio (TTM)", info.get("trailingPE")),
                ("Forward PE", info.get("forwardPE")),
                ("PEG Ratio", info.get("pegRatio")),
                ("Price to Book", info.get("priceToBook")),
                ("EPS (TTM)", info.get("trailingEps")),
                ("Dividend Yield", info.get("dividendYield")),
                ("Beta", info.get("beta")),
                ("52W High", info.get("fiftyTwoWeekHigh")),
                ("52W Low", info.get("fiftyTwoWeekLow")),
                ("50D Avg", info.get("fiftyDayAverage")),
                ("200D Avg", info.get("twoHundredDayAverage")),
                ("Revenue", info.get("totalRevenue")),
                ("Gross Profit", info.get("grossProfits")),
                ("EBITDA", info.get("ebitda")),
                ("Net Income", info.get("netIncomeToCommon")),
                ("Profit Margin", info.get("profitMargins")),
                ("Operating Margin", info.get("operatingMargins")),
                ("ROE", info.get("returnOnEquity")),
                ("ROA", info.get("returnOnAssets")),
                ("Debt/Equity", info.get("debtToEquity")),
                ("Current Ratio", info.get("currentRatio")),
                ("Book Value", info.get("bookValue")),
                ("Free Cash Flow", info.get("freeCashflow")),
            ]
            lines = [f"# Fundamentals: {symbol}"]
            for label, val in fields:
                if val is not None:
                    lines.append(f"{label}: {val}")
            result = "\n".join(lines)
        _cache_set("get_fundamentals", result, symbol)
        return result
    except Exception as e:
        return f"ERROR: {e}"


# ═══════════════════════════════════════════════════════════════
# 3-5. Financial statements
# ═══════════════════════════════════════════════════════════════

def _financial_stmt(symbol: str, freq: str, stmt_type: str, fetcher) -> str:
    cached = _cache_get(f"get_{stmt_type}", symbol, freq)
    if cached:
        return cached
    try:
        t = _ticker(symbol)
        data = fetcher(t)
        if data is None or data.empty:
            result = f"NO_DATA: {symbol} — no {stmt_type} data"
        else:
            result = f"# {stmt_type.title()} ({freq}): {symbol}\n\n" + data.to_csv()
        _cache_set(f"get_{stmt_type}", result, symbol, freq)
        return result
    except Exception as e:
        return f"ERROR: {e}"


@tool("get_balance_sheet", "Get balance sheet (assets, liabilities, equity).",
      {"symbol": {"type": "string", "description": "Ticker symbol"},
       "freq": {"type": "string", "description": "'quarterly' or 'annual'"}})
def get_balance_sheet(symbol: str, freq: str = "quarterly") -> str:
    return _financial_stmt(symbol, freq, "balance_sheet",
                           lambda t: t.quarterly_balance_sheet if freq == "quarterly" else t.balance_sheet)


@tool("get_cashflow", "Get cash flow statement.",
      {"symbol": {"type": "string", "description": "Ticker symbol"},
       "freq": {"type": "string", "description": "'quarterly' or 'annual'"}})
def get_cashflow(symbol: str, freq: str = "quarterly") -> str:
    return _financial_stmt(symbol, freq, "cashflow",
                           lambda t: t.quarterly_cashflow if freq == "quarterly" else t.cashflow)


@tool("get_income_statement", "Get income statement (revenue, costs, profit).",
      {"symbol": {"type": "string", "description": "Ticker symbol"},
       "freq": {"type": "string", "description": "'quarterly' or 'annual'"}})
def get_income_statement(symbol: str, freq: str = "quarterly") -> str:
    return _financial_stmt(symbol, freq, "income_statement",
                           lambda t: t.quarterly_income_stmt if freq == "quarterly" else t.income_stmt)


# ═══════════════════════════════════════════════════════════════
# 6. Insider transactions
# ═══════════════════════════════════════════════════════════════

@tool("get_insider_transactions", "Get recent insider buying/selling activity.",
      {"symbol": {"type": "string", "description": "Ticker symbol"}})
def get_insider_transactions(symbol: str) -> str:
    cached = _cache_get("get_insider_transactions", symbol)
    if cached:
        return cached
    try:
        t = _ticker(symbol)
        data = t.insider_transactions
        if data is None or data.empty:
            result = f"No insider transactions reported for {symbol}"
        else:
            result = f"# Insider Transactions: {symbol}\n\n" + data.to_csv()
        _cache_set("get_insider_transactions", result, symbol)
        return result
    except Exception as e:
        return f"ERROR: {e}"


# ═══════════════════════════════════════════════════════════════
# 7-8. News
# ═══════════════════════════════════════════════════════════════

@tool("get_news", "Get recent news articles mentioning a specific ticker.",
      {"symbol": {"type": "string", "description": "Ticker symbol (e.g. AAPL)"},
       "limit": {"type": "integer", "description": "Max articles (default 10)"}})
def get_news(symbol: str, limit: int = 10) -> str:
    cached = _cache_get("get_news", symbol, str(limit))
    if cached:
        return cached
    try:
        t = _ticker(symbol)
        news = t.news
        if not news:
            result = f"No recent news for {symbol}"
        else:
            lines = [f"# News: {symbol}"]
            for i, n in enumerate(news[:limit]):
                title = (n.get("content", {}).get("title", "") or n.get("title", ""))
                pub = n.get("content", {}).get("pubDate", "") or ""
                lines.append(f"{i+1}. [{pub}] {title}")
            result = "\n".join(lines)
        _cache_set("get_news", result, symbol, str(limit))
        return result
    except Exception as e:
        return f"ERROR: {e}"


@tool("get_global_news", "Get macro market news from major sources.",
      {"topic": {"type": "string", "description": "Topic keyword (e.g. 'Fed', 'inflation', 'tech')"},
       "limit": {"type": "integer", "description": "Max articles (default 10)"}})
def get_global_news(topic: str = "", limit: int = 10) -> str:
    cached = _cache_get("get_global_news", topic, str(limit))
    if cached:
        return cached
    try:
        # yfinance doesn't have a global news endpoint. Use a generic ticker's news.
        tickers = ["SPY", "QQQ", "DIA"]
        all_news = []
        for tk in tickers:
            try:
                t = _ticker(tk)
                all_news.extend(t.news or [])
            except Exception:
                continue
        if not all_news:
            result = "No global news available"
        else:
            # Filter by topic if specified
            filtered = all_news
            if topic:
                tlow = topic.lower()
                filtered = [n for n in all_news if tlow in str(n).lower()]
            lines = [f"# Global News" + (f" (topic: {topic})" if topic else "")]
            for i, n in enumerate(filtered[:limit]):
                content = n.get("content", {})
                title = content.get("title", "") or n.get("title", "")
                pub = content.get("pubDate", "") or ""
                source = content.get("provider", {}).get("displayName", "")
                lines.append(f"{i+1}. [{pub}] {title} ({source})")
            result = "\n".join(lines)
        _cache_set("get_global_news", result, topic, str(limit))
        return result
    except Exception as e:
        return f"ERROR: {e}"


# ═══════════════════════════════════════════════════════════════
# 9. Technical indicators (stockstats)
# ═══════════════════════════════════════════════════════════════

INDICATOR_INFO = {
    "rsi": "RSI (14): Momentum oscillator, 0-100. >70 overbought, <30 oversold.",
    "macd": "MACD: Trend-following momentum. Crossover with signal line = trade signal.",
    "macds": "MACD Signal: 9-period EMA of MACD. Crossovers trigger signals.",
    "macdh": "MACD Histogram: MACD - Signal. Positive = bullish momentum.",
    "close_50_sma": "50-day SMA: Medium-term trend. Price above = uptrend.",
    "close_200_sma": "200-day SMA: Long-term trend benchmark.",
    "close_10_ema": "10-day EMA: Responsive short-term average.",
    "boll": "Bollinger Middle (20 SMA): Basis for Bollinger Bands.",
    "boll_ub": "Bollinger Upper: ~2 std dev above. Potential overbought.",
    "boll_lb": "Bollinger Lower: ~2 std dev below. Potential oversold.",
    "atr": "ATR (14): Average True Range. Measures volatility for stop-loss sizing.",
    "vwma": "VWMA: Volume-weighted moving average. Confirms trends with volume.",
    "mfi": "MFI (14): Money Flow Index. Price + volume momentum. >80 overbought, <20 oversold.",
}


@tool("get_indicators", "Get technical indicator values. Pass comma-separated indicator names.",
      {"symbol": {"type": "string", "description": "Ticker symbol"},
       "indicator": {"type": "string",
                     "description": "Comma-separated indicator names: rsi,macd,boll,atr,close_50_sma,close_200_sma,close_10_ema,macds,macdh,boll_ub,boll_lb,vwma,mfi"},
       "lookback_days": {"type": "integer", "description": "Days to look back (default 60)"}})
def get_indicators(symbol: str, indicator: str, lookback_days: int = 60) -> str:
    cached = _cache_get("get_indicators", symbol, indicator, str(lookback_days))
    if cached:
        return cached
    try:
        from stockstats import wrap

        inds = [i.strip() for i in indicator.split(",") if i.strip()]
        unknown = [i for i in inds if i not in INDICATOR_INFO]
        if unknown:
            return f"ERROR: Unknown indicators: {unknown}. Available: {list(INDICATOR_INFO.keys())}"

        end = datetime.now()
        start = end - timedelta(days=lookback_days + 30)
        t = _ticker(symbol)
        data = t.history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
        if data.empty:
            return f"NO_DATA: {symbol}"

        df = wrap(data)
        df["Date"] = df.index.strftime("%Y-%m-%d")

        lines = [f"# Indicators: {symbol} (last {lookback_days}d)"]
        for ind in inds:
            lines.append(f"\n## {ind} — {INDICATOR_INFO.get(ind, '')}")
            try:
                vals = df[ind].dropna()
                recent = vals.tail(5)
                for idx, val in recent.items():
                    if isinstance(idx, int):
                        date_str = df.loc[idx, "Date"]
                    elif hasattr(idx, "strftime"):
                        date_str = idx.strftime("%Y-%m-%d")
                    else:
                        date_str = str(idx)
                    lines.append(f"  {date_str}: {val:.4f}" if isinstance(val, float) else f"  {val}")
            except Exception as e:
                lines.append(f"  (unavailable: {e})")

        result = "\n".join(lines)
        _cache_set("get_indicators", result, symbol, indicator, str(lookback_days))
        return result
    except ImportError:
        return "ERROR: stockstats not installed. Run: pip install stockstats"
    except Exception as e:
        return f"ERROR: {e}"


# ═══════════════════════════════════════════════════════════════
# 10. FRED macro
# ═══════════════════════════════════════════════════════════════

FRED_SERIES = {
    "fed_funds_rate": "FEDFUNDS", "federal_funds_rate": "FEDFUNDS",
    "10y_treasury": "DGS10", "2y_treasury": "DGS2", "30y_treasury": "DGS30",
    "10y_2y_spread": "T10Y2Y", "yield_curve": "T10Y2Y",
    "cpi": "CPIAUCSL", "core_cpi": "CPILFESL",
    "pce": "PCEPI", "core_pce": "PCEPILFE",
    "inflation_expectations": "T10YIE",
    "real_gdp": "GDPC1", "gdp": "GDP", "industrial_production": "INDPRO",
    "unemployment_rate": "UNRATE", "unemployment": "UNRATE",
    "nonfarm_payrolls": "PAYEMS", "initial_claims": "ICSA",
    "m2": "M2SL", "money_supply": "M2SL",
    "vix": "VIXCLS", "dollar_index": "DTWEXBGS",
    "consumer_sentiment": "UMCSENT",
    "housing_starts": "HOUST", "retail_sales": "RSAFS",
}


@tool("get_macro_indicators", "Get FRED macroeconomic data (Fed rate, CPI, GDP, unemployment, etc.). "
      "Use indicator aliases: fed_funds_rate, cpi, unemployment_rate, gdp, 10y_treasury, vix, etc.",
      {"indicator": {"type": "string", "description": "Indicator alias or FRED series ID (e.g. 'fed_funds_rate', 'cpi', 'unemployment')"},
       "lookback_days": {"type": "integer", "description": "Days to look back (default 365)"}})
def get_macro_indicators(indicator: str, lookback_days: int = 365) -> str:
    cached = _cache_get("get_macro_indicators", indicator, str(lookback_days))
    if cached:
        return cached

    api_key = os.environ.get("FRED_API_KEY", "")
    if not api_key:
        return ("FRED unavailable: FRED_API_KEY not set. "
                "Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html")

    key = indicator.strip().lower().replace(" ", "_").replace("-", "_")
    series_id = FRED_SERIES.get(key, indicator.strip().upper())
    if not series_id or len(series_id) > 30 or any(c.isspace() for c in series_id):
        return f"ERROR: '{indicator}' is not a known macro alias. Available: {list(FRED_SERIES.keys())}"

    try:
        import requests
        end_dt = datetime.now()
        start_date = (end_dt - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        end_date = end_dt.strftime("%Y-%m-%d")

        # Get series metadata
        meta = requests.get(
            "https://api.stlouisfed.org/fred/series",
            params={"series_id": series_id, "api_key": api_key, "file_type": "json"},
            timeout=15,
        ).json()
        info = (meta.get("seriess") or [{}])[0]
        title = info.get("title", series_id)
        units = info.get("units_short", "")

        # Get observations
        obs = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": series_id, "api_key": api_key, "file_type": "json",
                "observation_start": start_date, "observation_end": end_date,
                "sort_order": "desc", "limit": 60,
            },
            timeout=15,
        ).json()
        observations = obs.get("observations", [])

        points = [(o["date"], o["value"]) for o in observations if o.get("value") not in (".", None, "")]
        if not points:
            result = f"# FRED: {title} ({series_id})\nNo observations in window."
        else:
            latest_date, latest_val = points[0]
            lines = [
                f"# FRED: {title} ({series_id})",
                f"Units: {units}",
                f"Latest: {latest_val} ({latest_date})",
                f"Window: {start_date} to {end_date}",
                "",
                "| Date | Value |",
                "|------|-------|",
            ]
            for d, v in points[:40]:
                lines.append(f"| {d} | {v} |")
            result = "\n".join(lines)

        _cache_set("get_macro_indicators", result, indicator, str(lookback_days))
        return result
    except ImportError:
        return "ERROR: requests not installed"
    except Exception as e:
        return f"ERROR: {e}"


# ═══════════════════════════════════════════════════════════════
# 11. Reddit
# ═══════════════════════════════════════════════════════════════

@tool("get_reddit_sentiment", "Get recent Reddit posts mentioning a ticker across finance subreddits.",
      {"symbol": {"type": "string", "description": "Ticker symbol (e.g. AAPL, NVDA)"},
       "limit": {"type": "integer", "description": "Max posts per subreddit (default 5)"}})
def get_reddit_sentiment(symbol: str, limit: int = 5) -> str:
    cached = _cache_get("get_reddit_sentiment", symbol, str(limit))
    if cached:
        return cached
    try:
        import html
        import re
        import xml.etree.ElementTree as ET
        from urllib.parse import urlencode
        from urllib.request import Request, urlopen

        subs = ("wallstreetbets", "stocks", "investing")
        blocks = []
        total = 0
        ua = "global-data-mcp/1.0"

        for sub in subs:
            try:
                qs = urlencode({"q": symbol.upper(), "restrict_sr": "on", "sort": "new", "t": "week", "limit": limit})
                url = f"https://www.reddit.com/r/{sub}/search.rss?{qs}"
                req = Request(url, headers={"User-Agent": ua})
                with urlopen(req, timeout=10) as resp:
                    root = ET.fromstring(resp.read())
                ns = {"atom": "http://www.w3.org/2005/Atom"}
                entries = root.findall("atom:entry", ns)[:limit]
                if not entries:
                    blocks.append(f"r/{sub}: no posts found")
                    continue
                lines = [f"r/{sub} — {len(entries)} posts:"]
                for e in entries:
                    title_el = e.find("atom:title", ns)
                    title = (title_el.text or "") if title_el is not None else ""
                    content_el = e.find("atom:content", ns)
                    text = content_el.text if content_el is not None else ""
                    if "<!-- SC_OFF -->" in text:
                        text = text.split("<!-- SC_OFF -->")[1].split("<!-- SC_ON -->")[0]
                    text = re.sub(r"<[^>]+>", " ", text)
                    text = " ".join(html.unescape(text).split())[:200]
                    lines.append(f"  {title}" + (f"\n    {text}" if text else ""))
                blocks.append("\n".join(lines))
                total += len(entries)
            except Exception as e:
                blocks.append(f"r/{sub}: unavailable ({e})")

        if total == 0:
            result = f"No Reddit posts found for {symbol.upper()}"
        else:
            result = f"# Reddit: {symbol.upper()}\n\n" + "\n\n".join(blocks)

        _cache_set("get_reddit_sentiment", result, symbol, str(limit))
        return result
    except Exception as e:
        return f"ERROR: {e}"


# ═══════════════════════════════════════════════════════════════
# 12. StockTwits
# ═══════════════════════════════════════════════════════════════

@tool("get_stocktwits_sentiment", "Get StockTwits messages for a ticker with bullish/bearish labels.",
      {"symbol": {"type": "string", "description": "Ticker symbol"},
       "limit": {"type": "integer", "description": "Max messages (default 30)"}})
def get_stocktwits_sentiment(symbol: str, limit: int = 30) -> str:
    cached = _cache_get("get_stocktwits_sentiment", symbol, str(limit))
    if cached:
        return cached
    try:
        import json as _json
        from urllib.request import Request, urlopen

        url = f"https://api.stocktwits.com/api/2/streams/symbol/{symbol.upper()}.json"
        req = Request(url, headers={"User-Agent": "global-data-mcp/1.0", "Accept": "application/json"})
        with urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read())

        messages = data.get("messages", []) if isinstance(data, dict) else []
        if not messages:
            result = f"No StockTwits messages for ${symbol.upper()}"
        else:
            bullish = bearish = unlabeled = 0
            lines = []
            for m in messages[:limit]:
                sentiment = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
                body = (m.get("body") or "").replace("\n", " ").strip()[:280]
                user = (m.get("user") or {}).get("username", "?")
                if sentiment == "Bullish":
                    bullish += 1
                    tag = "Bullish"
                elif sentiment == "Bearish":
                    bearish += 1
                    tag = "Bearish"
                else:
                    unlabeled += 1
                    tag = "-"
                lines.append(f"[{tag}] @{user}: {body}")
            total = bullish + bearish + unlabeled
            header = (f"# StockTwits: {symbol.upper()}\n"
                      f"Bullish: {bullish} | Bearish: {bearish} | Unlabeled: {unlabeled} | Total: {total}\n")
            result = header + "\n".join(lines)

        _cache_set("get_stocktwits_sentiment", result, symbol, str(limit))
        return result
    except Exception as e:
        return f"ERROR: {e}"


# ═══════════════════════════════════════════════════════════════
# 13. Polymarket
# ═══════════════════════════════════════════════════════════════

@tool("get_prediction_markets", "Get Polymarket prediction market probabilities for an event topic.",
      {"topic": {"type": "string", "description": "Event topic (e.g. 'Fed rate cut', 'recession 2026', 'US election')"},
       "limit": {"type": "integer", "description": "Max markets (default 6)"}})
def get_prediction_markets(topic: str, limit: int = 6) -> str:
    cached = _cache_get("get_prediction_markets", topic, str(limit))
    if cached:
        return cached
    try:
        import requests as _requests

        data = _requests.get(
            "https://gamma-api.polymarket.com/public-search",
            params={"q": topic, "limit_per_type": 20},
            timeout=15,
        ).json()

        now = datetime.now()
        candidates = []
        for event in data.get("events", []):
            for m in event.get("markets", []):
                if m.get("closed"):
                    continue
                prices = m.get("outcomePrices")
                if isinstance(prices, str):
                    try:
                        prices = json.loads(prices)
                    except json.JSONDecodeError:
                        continue
                if not prices:
                    continue
                outcomes = m.get("outcomes")
                if isinstance(outcomes, str):
                    try:
                        outcomes = json.loads(outcomes)
                    except json.JSONDecodeError:
                        outcomes = []
                candidates.append(m)

        candidates.sort(key=lambda m: m.get("volumeNum") or 0, reverse=True)

        if not candidates:
            result = f"# Polymarket: '{topic}'\nNo matching open markets found."
        else:
            lines = [f"# Polymarket: '{topic}'\n"]
            for m in candidates[:limit]:
                prices = m.get("outcomePrices", [])
                outcomes = m.get("outcomes", [])
                if isinstance(prices, str):
                    try:
                        prices = json.loads(prices)
                    except json.JSONDecodeError:
                        prices = []
                try:
                    prob = float(prices[0])
                except (ValueError, IndexError, TypeError):
                    continue
                label = outcomes[0] if outcomes else "Yes"
                vol = m.get("volumeNum") or 0
                end_date = (m.get("endDate") or "")[:10]
                question = m.get("question", m.get("title", "?"))
                wk = m.get("oneWeekPriceChange")
                wk_str = f", 1w {wk * 100:+.1f}pp" if isinstance(wk, (int, float)) and wk else ""
                lines.append(f"- **{question}** — {label} {prob:.0%} (${vol:,.0f} vol, {end_date}{wk_str})")
            result = "\n".join(lines)

        _cache_set("get_prediction_markets", result, topic, str(limit))
        return result
    except ImportError:
        return "ERROR: requests not installed"
    except Exception as e:
        return f"ERROR: {e}"


# ═══════════════════════════════════════════════════════════════
# 14. Eastmoney 7x24 全球快讯
# ═══════════════════════════════════════════════════════════════

@tool("get_a_global_news", "Get 7x24 global financial news from Eastmoney (全球资讯). Returns title, summary, time.",
      {"page_size": {"type": "integer", "description": "Articles to fetch (default 50)"}})
def get_a_global_news(page_size: int = 50) -> str:
    import json as _json, uuid as _uuid
    cached = _cache_get("get_a_global_news", str(page_size))
    if cached:
        return cached
    try:
        url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
        params = {
            "client": "web", "biz": "web_724",
            "fastColumn": "102", "sortEnd": "",
            "pageSize": str(page_size),
            "req_trace": str(_uuid.uuid4()),
        }
        headers = {"User-Agent": _UA, "Referer": "https://kuaixun.eastmoney.com/"}
        r = em_get(url, params=params, headers=headers, timeout=10)
        d = r.json()
        rows = []
        for item in d.get("data", {}).get("fastNewsList", []):
            rows.append({
                "title": item.get("title", ""),
                "summary": (item.get("summary") or "")[:200],
                "time": item.get("showTime", ""),
            })
        result = _json.dumps(rows, ensure_ascii=False)
        _cache_set("get_a_global_news", result, str(page_size))
        return result
    except Exception as e:
        return '{"error":"%s"}' % str(e)
