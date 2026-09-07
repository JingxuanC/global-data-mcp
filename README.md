# Global Data MCP

美股 + 宏观舆情工具集的独立 MCP（Model Context Protocol）服务。从
[Athena](https://github.com/JingxuanC/Athena) 的 py-sidecar 中抽取，
让任何 MCP 客户端（Claude Desktop、Kimi Code、Cursor、自研 Agent）
都能直接获取美股行情/财报、宏观指标、社交媒体舆情与预测市场数据。

## 工具清单（14 个）

### 美股（yfinance）

| 工具 | 说明 |
|------|------|
| `get_stock_data` | 美股 OHLCV 日线历史（CSV：Date,Open,High,Low,Close,Volume） |
| `get_fundamentals` | 公司基本面：市值、PE、EPS、营收、利润率、ROE 等 20+ 字段 |
| `get_balance_sheet` | 资产负债表（季度/年度） |
| `get_cashflow` | 现金流量表（季度/年度） |
| `get_income_statement` | 利润表（季度/年度） |
| `get_insider_transactions` | 内部人（高管/大股东）买卖记录 |
| `get_news` | 个股相关新闻（标题 + 发布时间） |

### 宏观 / 舆情

| 工具 | 说明 | 数据源 |
|------|------|--------|
| `get_macro_indicators` | FRED 宏观指标：联邦基金利率、CPI、GDP、失业率、国债收益率、VIX 等（支持别名） | FRED（需 `FRED_API_KEY`） |
| `get_global_news` | 宏观市场新闻（SPY/QQQ/DIA 聚合，支持 topic 过滤） | yfinance |
| `get_a_global_news` | 7x24 全球财经快讯（标题 + 摘要 + 时间） | 东方财富 |
| `get_prediction_markets` | Polymarket 预测市场概率（如 "Fed rate cut"、"recession 2026"） | Polymarket gamma API |
| `get_reddit_sentiment` | Reddit 舆情：wallstreetbets/stocks/investing 三板块提及个股的帖子 | Reddit RSS（零依赖） |
| `get_stocktwits_sentiment` | StockTwits 消息流，含 Bullish/Bearish 标签统计 | StockTwits API（零依赖） |
| `get_indicators` | 技术指标：rsi/macd/boll/atr/sma/ema/vwma/mfi 等 13 个 | stockstats + yfinance |

所有工具均为秒级 HTTP 数据获取，同步调用，带本地 JSON 缓存
（`DATA_CACHE_DIR`，默认 TTL 300s）。

## 快速开始

```bash
pip install -r requirements.txt
python3 server.py --port 50058
```

验证：

```bash
curl http://127.0.0.1:50058/health
curl http://127.0.0.1:50058/tools
# MCP initialize
curl -X POST http://127.0.0.1:50058/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}'
# 真实调用
curl -X POST http://127.0.0.1:50058/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"get_stock_data",
                 "arguments":{"symbol":"AAPL","start_date":"2026-08-01","end_date":"2026-09-01"}}}'
```

接入 MCP 客户端（以 Claude Desktop / Kimi Code 为例）：

```yaml
# mcp 配置
global-data:
  url: http://127.0.0.1:50058/mcp
```

## Docker 部署

无需本地 Python 环境，一条命令起服务：

```bash
docker compose up -d        # 构建镜像 + 启动容器（首次构建约 3-5 分钟）
docker compose ps           # 查看状态
docker compose logs -f      # 跟踪日志
```

验证：

```bash
curl http://127.0.0.1:50058/health
curl http://127.0.0.1:50058/tools   # 应返回 14 个工具
```

license 鉴权（可选）：在 `docker-compose.yml` 中取消 license 相关注释，
把宿主机 `licenses.json` 挂进容器并设置 `MCP_LICENSE_FILE`：

```yaml
environment:
  MCP_LICENSE_FILE: /app/licenses/licenses.json
volumes:
  - ./licenses.json:/app/licenses/licenses.json:ro
```

其他环境变量（`FRED_API_KEY` / `YAHOO_PROXY` / `MCP_WORKERS` 等）同样在
compose 文件的 `environment` 段配置，改完 `docker compose up -d` 生效。
注意容器内访问宿主机代理用 `host.docker.internal`（如
`YAHOO_PROXY=socks5://host.docker.internal:1097`）。

## 环境变量

| 变量 | 说明 |
|------|------|
| `YAHOO_PROXY` | yfinance 专用代理（如 `socks5://127.0.0.1:1097`）。仅作用于美股请求（显式 session 挂载），不污染全局 socket——国内源（东财快讯）保持直连 |
| `FRED_API_KEY` | FRED 宏观数据 API key，`get_macro_indicators` 必需。[免费申请](https://fred.stlouisfed.org/docs/api/api_key.html) |
| `DATA_CACHE_DIR` | 工具结果缓存目录（默认 `./.data_cache`） |
| `DATA_CACHE_TTL` | 缓存秒数（默认 300） |

## 附带：yahoo_quotes.py

Yahoo 美股实时行情子进程脚本（Go 行情网关以子进程方式调用）：

```bash
echo '["AAPL","NVDA"]' | python3 yahoo_quotes.py
# 输出 JSON: [{symbol, name, price, prev_close, pct, volume, high, low, turnover}, ...]
```

自带 60s 缓存（`.yahoo_cache.json`）防 Yahoo 限流；`YAHOO_PROXY` 同样生效。

## 鉴权与额度（可选）

默认开放模式（本地/内网）。设置环境变量后强制 license key 鉴权：

```bash
export MCP_LICENSE_FILE=/path/to/licenses.json
python3 server.py --port 50058
# 客户端请求头：X-License-Key: <key>
```

license JSON 格式与额度语义见 `mcp_gateway.py` docstring。`GET /quota` 查余量。

## 端点一览

```
GET  /health        健康检查
GET  /tools         工具 JSON schema 列表
POST /mcp           MCP JSON-RPC（initialize / tools/list / tools/call）
GET  /quota         license 额度余量（鉴权模式）
GET  /metrics       Prometheus 指标（文本格式，不鉴权）
```

## 可观察性 / Observability

`GET /metrics` 输出 Prometheus 文本格式（`text/plain; version=0.0.4`），
不要求鉴权（内网抓取惯例；只含工具名级聚合，不泄露 license key）。

指标：

| 指标 | 类型 | 说明 |
|------|------|------|
| `mcp_tool_calls_total{tool,status}` | counter | 调用计数；status ∈ `ok` / `error` / `rejected_license` / `rejected_quota` / `queued` |
| `mcp_tool_latency_seconds_sum{tool}` / `mcp_tool_latency_seconds_count{tool}` | counter | 延迟总和与样本数，相除即平均延迟 |
| `mcp_uptime_seconds` | gauge | 进程启动至今秒数 |

Prometheus scrape 配置示例：

```yaml
scrape_configs:
  - job_name: global-data-mcp
    metrics_path: /metrics
    static_configs:
      - targets: ["127.0.0.1:50058"]
```

## 数据源说明

- **yfinance**：Yahoo Finance 非官方接口，有频率限制；生产建议挂代理 + 依赖内置缓存
- **FRED**：圣路易斯联储官方 API，免费 key，稳定
- **Reddit / StockTwits**：公开 RSS/API，无需 key，stdlib urllib 直连
- **Polymarket**：gamma 公开搜索 API，无需 key
- **东方财富快讯**：内置全局节流（≥1s 间隔 + 随机抖动 + 会话复用重试）防封 IP

## 致谢

本项目主体来自 [Athena](https://github.com/JingxuanC/Athena) ——
local-first、AI-native 的多 Agent 量化交易系统。

## License

MIT
