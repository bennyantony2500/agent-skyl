# Monday.com BI Agent — MCP Edition

A conversational BI agent using the **Model Context Protocol (MCP)** to integrate with Monday.com.

## Architecture

```
Browser
  ↕ HTTP
FastAPI (back/main.py)
  ↕ Groq API (Llama 3.3 70B)  ← LLM decides which tools to call
  ↕ MCP Client (back/mcp_client.py)
  ↕ JSON-RPC over stdio (MCP protocol)
MCP Server (mc_server/monday_mcp_server.py)
  ↕ HTTPS / GraphQL
Monday.com API
```

## What is MCP?

**Model Context Protocol** is an open standard (by Anthropic) for connecting AI models to external tools and data sources. Instead of writing tool integrations directly in your app, you:

1. Run an **MCP Server** — a process that exposes tools (like `get_work_orders`)
2. Connect to it from your app via an **MCP Client** using JSON-RPC 2.0
3. The LLM calls tools through this standardized protocol

This makes tool integrations **reusable** — any MCP-compatible AI app can use your Monday.com MCP server.

## MCP Transport: stdio

This implementation uses **stdio transport** — the MCP server runs as a child process, and communication happens via stdin/stdout:

```
FastAPI process
  └── spawns → monday_mcp_server.py (child process)
                    stdin ← JSON-RPC requests
                    stdout → JSON-RPC responses
```

Each tool call flow:
1. Groq returns a tool_call (e.g. `get_work_orders`)
2. FastAPI → MCP Client sends `{"method": "tools/call", "params": {"name": "get_work_orders", ...}}`
3. MCP Server receives it, calls Monday.com GraphQL API live
4. MCP Server responds with `{"result": {"content": [{"type": "text", "text": "...data..."}]}}`
5. FastAPI feeds result back to Groq to generate the final answer

## Project Structure

```
agent-skyl/
├── mc_server/
│   └── monday_mcp_server.py   # MCP server: exposes Monday tools over stdio
├── back/
│   ├── main.py                # FastAPI + Groq agentic loop
│   └── mcp_client.py          # MCP stdio client
├── front/
│   └── index.html             # Chat UI
├── requirements.txt
├── Dockerfile
└── README.md
```

## Quick Start

### 1. Get API keys (all free)

- **Monday.com**: Profile → Developers → My Access Tokens
- **Groq**: console.groq.com (free, no credit card)
- **Board IDs**: from your Monday.com board URLs

### 2. Import data to Monday.com

```bash
pip install openpyxl httpx
MONDAY_API_KEY=your_key python scripts/import_to_monday.py \
  --work-orders Work_Order_Tracker_Data.xlsx \
  --deals Deal_funnel_Data.xlsx
```

### 3. Run

```bash
pip install -r requirements.txt

cd back
MONDAY_API_KEY=xxx \
WORK_ORDERS_BOARD_ID=yyy \
DEALS_BOARD_ID=zzz \
GROQ_API_KEY=gsk_... \
uvicorn main:app --port 8000
```

Open http://localhost:8000

### Docker

```bash
docker build -t monday-bi-mcp .
docker run -p 8000:8000 \
  -e MONDAY_API_KEY=xxx \
  -e WORK_ORDERS_BOARD_ID=yyy \
  -e DEALS_BOARD_ID=zzz \
  -e GROQ_API_KEY=gsk_... \
  monday-bi-mcp
```

## MCP Protocol Details

The server implements these MCP methods:

| Method | Description |
|--------|-------------|
| `initialize` | Handshake, exchange capabilities |
| `notifications/initialized` | Client confirms ready |
| `tools/list` | Returns available tool schemas |
| `tools/call` | Executes a tool with arguments |
| `ping` | Health check |

Tools exposed:

| Tool | Description |
|------|-------------|
| `get_work_orders` | Fetch all work orders from Monday.com with optional sector/status filters |
| `get_deals` | Fetch all pipeline deals with optional sector/status/stage filters |

## No MCP Library Needed

This uses a **hand-rolled MCP implementation** (pure Python, stdlib only) instead of the `mcp` pip package. This means:
- No extra dependency
- Works with any Python 3.10+
- Fully transparent — you can read every line of the protocol
