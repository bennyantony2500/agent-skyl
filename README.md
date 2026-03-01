# Skylark Business Intelligence Agent — MCP Edition

A conversational BI agent using the **Model Context Protocol (MCP)** to integrate with Monday.com.

## Architecture

```
Browser <-> FastAPI (back/main.py) <-> Groq API (Llama 3.3 70B)  ← LLM decides which tools to call <-> MCP Client (back/mcp_client.py) <-> JSON-RPC over stdio (MCP protocol) <-> MCP Server (mc_server/monday_mcp_server.py) <-> HTTPS / GraphQL <-> Monday.com API
```



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

### 1. Get API keys 

- **Monday.com**: Profile → Developers → My Access Tokens
- **Groq**: console.groq.com (free, no credit card)
- **Board IDs**: from your Monday.com board URLs





### 2. Run

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


