import os
import re
import sys
import json
import asyncio
import httpx
from typing import Optional
MCP_PROTOCOL_VERSION = "2024-11-05"
MONDAY_API_URL = "https://api.monday.com/v2"
MONDAY_API_KEY = os.environ.get("MONDAY_API_KEY", "")
WORK_ORDERS_BOARD_ID = os.environ.get("WORK_ORDERS_BOARD_ID", "")
DEALS_BOARD_ID = os.environ.get("DEALS_BOARD_ID", "")

async def monday_query(gql: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            MONDAY_API_URL,
            json={"query": gql},
            headers={
                "Authorization": MONDAY_API_KEY,
                "Content-Type": "application/json",
                "API-Version": "2024-01",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise ValueError(f"Monday API error: {data['errors']}")
        return data

async def get_column_map(board_id: str) -> dict:
    gql = f"""{{
      boards(ids: [{board_id}]) {{
        columns {{ id title }}
      }}
    }}"""
    data = await monday_query(gql)
    boards = data["data"]["boards"]
    if not boards:
        raise ValueError(f"Board ID {board_id} not found or API key has no access to it. Check your Board ID and Monday.com API key permissions.")
    cols = boards[0]["columns"]
    return {c["id"]: c["title"] for c in cols}

async def get_board_items(board_id: str) -> list[dict]:
    col_map = await get_column_map(board_id)

    all_items = []
    cursor = None
    while True:
        cursor_arg = f', cursor: "{cursor}"' if cursor else ""
        gql = f"""{{
          boards(ids: [{board_id}]) {{
            items_page(limit: 500{cursor_arg}) {{
              cursor
              items {{
                id name
                column_values {{ id text value }}
              }}
            }}
          }}
        }}"""
        data = await monday_query(gql)
        boards = data["data"]["boards"]
        if not boards:
            raise ValueError(f"Board ID {board_id} not found or API key has no access to it.")
        page = boards[0]["items_page"]
        for item in page["items"]:
            for cv in item["column_values"]:
                cv["title"] = col_map.get(cv["id"], cv["id"])
        all_items.extend(page["items"])
        cursor = page.get("cursor")
        if not cursor:
            break
    return all_items

def normalize_item(item: dict) -> dict:
    row = {"id": item["id"], "name": item["name"]}
    for col in item.get("column_values", []):
        key = (col["title"] or col["id"]).strip()
        value = col["text"]
        if not value:
            raw = col.get("value")
            if raw and raw != "null":
                try:
                    parsed = json.loads(raw)
                    value = parsed.get("text") or parsed.get("name") or str(parsed) if isinstance(parsed, dict) else str(parsed)
                except Exception:
                    value = ""
        row[key] = value or ""
    return row

def nc(val) -> Optional[float]:
    if not val: return None
    try: return float(re.sub(r"[₹,\s]", "", str(val)))
    except: return None

def np_(val) -> Optional[float]:
    if not val: return None
    m = {"high": 0.8, "medium": 0.5, "low": 0.2}
    l = val.strip().lower()
    if l in m: return m[l]
    try:
        f = float(re.sub(r"[%\s]", "", l))
        return f / 100 if f > 1 else f
    except: return None


async def tool_get_work_orders(arguments: dict) -> dict:
    if not WORK_ORDERS_BOARD_ID:
        return {"error": "WORK_ORDERS_BOARD_ID not set"}

    raw = await get_board_items(WORK_ORDERS_BOARD_ID)
    records, issues = [], []

    for item in raw:
        flat = normalize_item(item)
        def g(*keys):
            for k in keys:
                for fk, fv in flat.items():
                    if k.lower() in fk.lower(): return fv
            return ""

        ae = nc(g("Amount in Rupees (Excl"))
        if not ae: issues.append(f"Missing amount: {item['name']}")

        rec = {
            "name": item["name"], "customer": g("Customer Name"), "serial": g("Serial"),
            "sector": g("Sector"), "status": g("Execution Status"),
            "nature_of_work": g("Nature of Work"), "type_of_work": g("Type of Work"),
            "amount_excl_gst": ae, "amount_incl_gst": nc(g("Amount in Rupees (Incl")),
            "billed_excl_gst": nc(g("Billed Value in Rupees (Excl")),
            "billed_incl_gst": nc(g("Billed Value in Rupees (Incl")),
            "collected": nc(g("Collected Amount")),
            "amount_to_bill": nc(g("Amount to be billed in Rs. (Exl")),
            "amount_receivable": nc(g("Amount Receivable")),
            "wo_status": g("WO Status"), "billing_status": g("Billing Status"),
            "collection_status": g("Collection status"), "invoice_status": g("Invoice Status"),
            "bd_owner": g("BD/KAM"), "probable_start": g("Probable Start"),
            "probable_end": g("Probable End"), "expected_billing_month": g("Expected Billing"),
            "ar_priority": g("AR Priority"),
        }

        fs = arguments.get("filter_sector", "").lower()
        fst = arguments.get("filter_status", "").lower()
        if fs and fs not in (rec["sector"] or "").lower(): continue
        if fst and fst not in (rec["status"] or "").lower(): continue
        records.append(rec)

    return {"total_fetched": len(raw), "total_returned": len(records),
            "data_quality_notes": issues[:10], "records": records}


async def tool_get_deals(arguments: dict) -> dict:
    if not DEALS_BOARD_ID:
        return {"error": "DEALS_BOARD_ID not set"}

    raw = await get_board_items(DEALS_BOARD_ID)
    records, issues = [], []

    for item in raw:
        flat = normalize_item(item)
        def g(*keys):
            for k in keys:
                for fk, fv in flat.items():
                    if k.lower() in fk.lower(): return fv
            return ""

        dv = nc(g("deal value", "Masked Deal"))
        prob = np_(g("Closure Probability", "probability"))
        if not dv: issues.append(f"Missing value: {item['name']}")

        rec = {
            "name": item["name"], "owner": g("Owner"), "client": g("Client"),
            "status": g("Deal Status"), "deal_stage": g("Deal Stage"),
            "sector": g("Sector", "service"), "product": g("Product"),
            "deal_value": dv, "close_probability": prob,
            "close_probability_label": g("Closure Probability"),
            "tentative_close_date": g("Tentative Close", "close date"),
            "actual_close_date": g("Close Date (A)"), "created_date": g("Created Date"),
            "weighted_value": (dv * prob) if (dv and prob) else None,
        }

        fs = arguments.get("filter_sector", "").lower()
        fst = arguments.get("filter_status", "").lower()
        fsg = arguments.get("filter_stage", "").lower()
        if fs and fs not in (rec["sector"] or "").lower(): continue
        if fst and fst not in (rec["status"] or "").lower(): continue
        if fsg and fsg not in (rec["deal_stage"] or "").lower(): continue
        records.append(rec)

    return {"total_fetched": len(raw), "total_returned": len(records),
            "data_quality_notes": issues[:10], "records": records}


# MCP tool registry 

MCP_TOOLS = [
    {
        "name": "get_work_orders",
        "description": "Fetch ALL work orders live from Monday.com. Fields: name, customer, serial, sector, status, nature_of_work, type_of_work, amount_excl_gst, billed_excl_gst, collected, amount_receivable, wo_status, billing_status, collection_status, bd_owner, expected_billing_month. Use for billing, collections, sector analysis.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filter_sector": {"type": "string", "description": "Optional sector filter e.g. Mining, Powerline"},
                "filter_status": {"type": "string", "description": "Optional status filter e.g. Completed, Open"}
            },
            "required": []
        }
    },
    {
        "name": "get_deals",
        "description": "Fetch ALL pipeline deals live from Monday.com. Fields: name, owner, client, status, deal_stage, sector, product, deal_value, close_probability, weighted_value, tentative_close_date. Use for pipeline health, forecasting.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filter_sector": {"type": "string"},
                "filter_status": {"type": "string"},
                "filter_stage": {"type": "string"}
            },
            "required": []
        }
    }
]



def write_response(obj: dict):
    line = json.dumps(obj) + "\n"
    sys.stdout.buffer.write(line.encode("utf-8"))
    sys.stdout.buffer.flush()

def log(msg: str):
    sys.stderr.write(f"[MCP] {msg}\n")
    sys.stderr.flush()

def make_response(req_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}

def make_error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}




async def handle_request(request: dict) -> Optional[dict]:
    method = request.get("method", "")
    req_id = request.get("id")  
    params = request.get("params", {})

    log(f"← {method} (id={req_id})")

    if req_id is None:
        return None

    if method == "initialize":
        return make_response(req_id, {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "monday-bi-mcp-server", "version": "1.0.0"}
        })

    elif method == "tools/list":
        return make_response(req_id, {"tools": MCP_TOOLS})

    elif method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        log(f"  calling tool: {tool_name} args={arguments}")

        try:
            if tool_name == "get_work_orders":
                result = await tool_get_work_orders(arguments)
            elif tool_name == "get_deals":
                result = await tool_get_deals(arguments)
            else:
                return make_error(req_id, -32601, f"Unknown tool: {tool_name}")

            log(f"  → {result.get('total_returned', '?')} records returned")
            return make_response(req_id, {
                "content": [{"type": "text", "text": json.dumps(result)}]
            })

        except Exception as e:
            log(f"  ERROR: {e}")
            return make_response(req_id, {
                "content": [{"type": "text", "text": json.dumps({"error": str(e)})}],
                "isError": True
            })

    elif method == "ping":
        return make_response(req_id, {})

    else:
        return make_error(req_id, -32601, f"Method not found: {method}")


async def main():

    log("MCP server starting...")
    loop = asyncio.get_event_loop()

    while True:
        try:
            raw = await loop.run_in_executor(None, sys.stdin.buffer.readline)

            if not raw:
                log("stdin closed, shutting down")
                break

            line = raw.decode("utf-8").strip()
            if not line:
                continue

            try:
                request = json.loads(line)
            except json.JSONDecodeError as e:
                log(f"JSON parse error: {e}")
                write_response(make_error(None, -32700, f"Parse error: {e}"))
                continue

            response = await handle_request(request)

            if response is not None:
                log(f"→ responding to id={response.get('id')}")
                write_response(response)

        except Exception as e:
            log(f"Fatal error in main loop: {e}")
            break

    log("MCP server stopped")


if __name__ == "__main__":
    asyncio.run(main())
