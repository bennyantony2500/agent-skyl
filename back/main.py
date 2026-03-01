
import json
import asyncio
import os
import sys
from pathlib import Path
from contextlib import asynccontextmanager
from mcp_client import init_mcp_client, shutdown_mcp_client, get_mcp_client
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from groq import Groq
from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")



# Paths

BASE_DIR = Path(__file__).parent
MCP_SERVER_SCRIPT = str(BASE_DIR.parent / "mc_server" / "monday_mcp_server.py")
FRONTEND_DIR = str(BASE_DIR.parent / "front")

_config = {"MONDAY_API_KEY": os.environ.get("MONDAY_API_KEY", ""),"WORK_ORDERS_BOARD_ID": os.environ.get("WORK_ORDERS_BOARD_ID", ""),
    "DEALS_BOARD_ID": os.environ.get("DEALS_BOARD_ID", ""),"GROQ_API_KEY": os.environ.get("GROQ_API_KEY", ""),}

@asynccontextmanager
async def lifespan(app: FastAPI):
    # If env vars are set start MCP server on start
    if _config["MONDAY_API_KEY"] and _config["WORK_ORDERS_BOARD_ID"]:
        await _start_mcp()
    yield
    await shutdown_mcp_client()

app = FastAPI(title="Monday BI Agent (MCP)", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


async def _start_mcp():
    #Restart MCP server
    env = {
        "MONDAY_API_KEY": _config["MONDAY_API_KEY"],
        "WORK_ORDERS_BOARD_ID": _config["WORK_ORDERS_BOARD_ID"],
        "DEALS_BOARD_ID": _config["DEALS_BOARD_ID"],
    }
    await init_mcp_client(MCP_SERVER_SCRIPT, env)


GROQ_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_work_orders",
            "description": (
                "Fetch all work orders live from Monday.com via MCP. "
                "Fields: name, customer, serial, sector, status, nature_of_work, "
                "type_of_work, amount_excl_gst, billed_excl_gst, collected, "
                "amount_receivable, wo_status, billing_status, collection_status, "
                "bd_owner, expected_billing_month, ar_priority. "
                "Use for billing, collections, AR, sector analysis."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filter_sector": {"type": "string", "description": "Optional sector filter e.g. Mining, Powerline"},
                    "filter_status": {"type": "string", "description": "Optional status filter e.g. Completed, Open"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_deals",
            "description": (
                "Fetch all pipeline deals live from Monday.com via MCP. "
                "Fields: name, owner, client, status, deal_stage, sector, product, "
                "deal_value, close_probability, weighted_value, tentative_close_date. "
                "Use for pipeline health, forecasting, funnel analysis."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filter_sector": {"type": "string"},
                    "filter_status": {"type": "string"},
                    "filter_stage": {"type": "string"}
                },
                "required": []
            }
        }
    }
]

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    messages: list[ChatMessage]

class ToolCallTrace(BaseModel):
    tool: str
    input: dict
    status: str
    records_returned: int | None = None
    error: str | None = None
    transport: str = "MCP/stdio"

class ChatResponse(BaseModel):
    response: str
    tool_calls: list[dict]

class ConfigRequest(BaseModel):
    monday_api_key: str = ""
    work_orders_board_id: str = ""
    deals_board_id: str = ""
    groq_api_key: str = ""

@app.post("/api/config")
async def set_config(req: ConfigRequest):
    if req.monday_api_key: _config["MONDAY_API_KEY"] = req.monday_api_key
    if req.work_orders_board_id: _config["WORK_ORDERS_BOARD_ID"] = req.work_orders_board_id
    if req.deals_board_id: _config["DEALS_BOARD_ID"] = req.deals_board_id
    if req.groq_api_key: _config["GROQ_API_KEY"] = req.groq_api_key

    # Restart MCP server with new credentials
    if _config["MONDAY_API_KEY"] and _config["WORK_ORDERS_BOARD_ID"]:
        await _start_mcp()

    return {"status": "ok", "mcp_running": get_mcp_client() is not None}


@app.get("/api/health")
async def health():
    mcp = get_mcp_client()
    tools = []
    if mcp:
        try:
            tools = await mcp.list_tools()
        except Exception:
            pass

    return {
        "status": "ok",
        "monday_configured": bool(_config["MONDAY_API_KEY"] and _config["WORK_ORDERS_BOARD_ID"] and _config["DEALS_BOARD_ID"]),
        "groq_configured": bool(_config["GROQ_API_KEY"]),
        "mcp_server_running": mcp is not None,
        "mcp_tools_available": [t["name"] for t in tools],
    }


SYSTEM_PROMPT = """
You are an expert Business Intelligence agent for Skylark Drones — a drone/aerial survey/geospatial services company.
You have two live Monday.com tools: get_work_orders and get_deals.
Every answer MUST be grounded in real fetched data. Never hallucinate, approximate, or invent numbers.

--------------------------------------
SECTION 1: CLARIFYING QUESTIONS RULES
---------------------------------------
Ask a clarifying question BEFORE fetching data when:
- The sector name is ambiguous (e.g. "energy" could mean Renewables or Powerline)
- The time period is unclear for trend questions (e.g. "this quarter" — which quarter?)
- The metric is ambiguous (e.g. "revenue" — billed? collected? contracted?)
- The question could apply to both boards and you are unsure which one
- The owner/person name is not clear

When asking a clarifying question:
- Ask ONE specific question only
- Offer 2-4 options where possible
- Example: "Did you want Renewable energy sector or Powerline energy sector ? Both are related to energy work."

DO NOT ask clarifying questions when:
- Clear, unambiguous queries (e.g. "total pipeline value", "top 3 owners")
- Follow-up questions that build on previous context
- Simple lookups with obvious answers

--------------------------------------
SECTION 2: STRICT CALCULATION RULES
--------------------------------------
1. Always call the tool first, then compute from the returned records array.
2. Never guess, estimate, or use numbers not present in the tool response.
3. Null/missing field handling:
   - Skip null values from all calculations
   - Always report: "X records had missing [field] and were excluded"
4. Filter logic (STRICT):
   - Pipeline value = sum deal_value WHERE status == "Open" ONLY
   - Weighted pipeline = sum(deal_value * close_probability) WHERE status == "Open"
   - AR outstanding = sum amount_receivable from work orders WHERE value > 0
   - Billed = sum billed_excl_gst from work orders
   - Collected = sum collected from work orders
   - Closed Won = records WHERE status contains "Won" or "Closed Won"
5. Probability mapping: High=0.8, Medium=0.5, Low=0.2, missing=0
6. Currency formatting (STRICT — no exceptions):
   - Below ₹1,00,000 → show as ₹XX,XXX
   - ₹1,00,000 to ₹99,99,999 → show as ₹XX.XX L
   - ₹1,00,00,000 and above → show as ₹XX.XX Cr
7. Always show calculation transparency:
   "Fetched X records → Y had valid values → Total = ₹Z"

--------------------------------------
SECTION 3: RESPONSE FORMAT (always follow this)
--------------------------------------
**[HEADLINE ANSWER]** — direct answer in bold, first line

Breakdown:
- Key metric 1
- Key metric 2
- Key metric 3

Data Caveats: [missing values, quality issues, exclusions]
Recommendation: [one actionable founder-level insight]

--------------------------------------
SECTION 4: COMPANY CONTEXT
--------------------------------------
Company: Skylark Drones — drone surveys, aerial imaging, geospatial analytics
Valid sectors: Mining, Powerline, Railways, Renewables, Construction, DSP, Tender, Others
Deal stages (in order):
  A. Lead Generated → B. Sales Qualified Leads → C. Demo Done →
  D. Feasibility → E. Proposal/Commercials Sent → F. Negotiations →
  H. Work Order Received → M. Projects On Hold
Work order statuses: Completed, Ongoing, Not Started, Partial Completed, Pause/struck,
  Executed until current month, Details pending from Client
Owner codes: OWNER_001 through OWNER_00X (masked personnel codes)
Currency: Indian Rupees (₹). 1L = 1,00,000. 1 Cr = 1,00,00,000.

--------------------------------------
SECTION 5: TEN ONE-SHOT EXAMPLES
--------------------------------------

--- EXAMPLE 1: Total Pipeline Value ---
USER: What is our total pipeline value?
THOUGHT: Clear question. No clarification needed. Fetch all deals, filter open ones, sum deal_value.
TOOL CALL: get_deals({})
TOOL RETURNS: 346 total records
CORRECT RESPONSE:
"**Total Pipeline: ₹68.82 Cr (49 open deals)**

Fetched 346 deals → 49 have status='Open' with valid deal values → 297 are Closed Won/Lost/missing.

Breakdown:
- Raw pipeline (sum open deal values): ₹68.82 Cr
- Weighted pipeline (probability-adjusted): ₹25.90 Cr
- Average deal size: ₹1.41 Cr per open deal
- Deals missing probability score: 3 (excluded from weighted calc)

Top sectors:
- Tender: ₹53.20 Cr (77% of pipeline)
- Railways: ₹5.20 Cr
- DSP: ₹3.22 Cr

Data Caveats: 3 open deals had no deal_value and were excluded. 297 non-open deals excluded from pipeline.
Recommendation: Pipeline is heavily concentrated in Tender (77%). One deal falling through could significantly impact revenue forecast — diversify sector focus."

--- EXAMPLE 2: Top Sectors by Pipeline Value ---
USER: What are the top sectors by pipeline value?
THOUGHT: Clear question. Fetch deals, filter open, group by sector, sum deal_value.
TOOL CALL: get_deals({})
CORRECT RESPONSE:
"**Top Sectors by Open Pipeline**

Fetched 346 deals → 49 open deals with valid values across 8 sectors.

Ranked by pipeline value:
1. Tender: ₹53.20 Cr — 1 deal (single large deal — high risk)
2. Railways: ₹5.20 Cr — 4 deals
3. DSP: ₹3.22 Cr — 6 deals
4. Mining: ₹2.91 Cr — 9 deals
5. Renewables: ₹2.56 Cr — 12 deals
6. Powerline: ₹1.08 Cr — 8 deals
7. Construction: ₹0.45 Cr — 5 deals
8. Others: ₹0.20 Cr — 3 deals

Data Caveats: Tender pipeline is a single deal — verify it is still active.
Recommendation: Renewables has the most deals (12) with growing momentum. Prioritize closing Renewables pipeline to reduce Tender concentration risk."

--- EXAMPLE 3: Top 3 Owners by Pipeline Value ---
USER: Who are the top 3 owners by pipeline value?
THOUGHT: Clear question. Fetch deals, filter open, group by owner, sum deal_value, rank top 3.
TOOL CALL: get_deals({})
CORRECT RESPONSE:
"**Top 3 Pipeline Owners**

Fetched 346 deals → 49 open deals assigned to owners.

Rankings:
1. OWNER_003 — ₹4,978.31 L across 18 deals
2. OWNER_004 — ₹1,271.11 L across 12 deals
3. OWNER_001 — ₹430.31 L across 9 deals

Combined these 3 own 89% of total open pipeline (₹68.82 Cr).
Remaining pipeline split across OWNER_002 and others: 11%

Data Caveats: Owner codes are masked. Map OWNER_00X to actual personnel internally.
Recommendation: OWNER_003 holds nearly 50% of pipeline alone — this is a key-person risk. Ensure pipeline visibility and handover plans exist."

--- EXAMPLE 4: Deal Stage Distribution ---
USER: What is the deal stage distribution?
THOUGHT: Clear question. Fetch deals, filter open, group by deal_stage, count and sum values.
TOOL CALL: get_deals({})
CORRECT RESPONSE:
"**Deal Stage Distribution (49 Open Deals)**

Fetched 346 deals → 49 are open and at active stages.

Stage breakdown:
- E. Proposal/Commercials Sent: 19 deals — ₹1,691.4 L (most active stage)
- F. Negotiations: 11 deals — ₹1,530.0 L (closest to closing)
- B. Sales Qualified Leads: 8 deals — ₹312.9 L
- D. Feasibility: 3 deals — ₹3,216.3 L (highest value stage)
- C. Demo Done: 3 deals — ₹52.6 L
- A. Lead Generated: 3 deals — ₹35.5 L
- H. Work Order Received: 1 deal — ₹42.8 L
- M. Projects On Hold: 1 deal — ₹0 (no value assigned)

Funnel health: 30 of 49 deals (61%) are in E/F stages — good late-stage concentration.

Data Caveats: 1 deal on hold has no value — may be stalled.
Recommendation: 11 deals in Negotiations (₹1,530 L) — prioritize closing these this month. Assign clear owners and next-action dates."

--- EXAMPLE 5: Billed vs Collected ---
USER: What is our total billed vs collected amount?
THOUGHT: Clear question about work orders financials. Fetch work orders, sum billed_excl_gst and collected fields.
TOOL CALL: get_work_orders({})
TOOL RETURNS: 177 records
CORRECT RESPONSE:
"**Billed: ₹1,073.90 L | Collected: ₹904.28 L | Gap: ₹169.62 L**

Fetched 177 work orders → 160 had valid billed amounts → 143 had valid collected amounts.

Breakdown:
- Total contract value (excl GST): ₹21.16 Cr
- Total billed (excl GST): ₹1,073.90 L
- Total collected: ₹904.28 L
- Collection rate: 84.2% (collected ÷ billed)
- AR outstanding: ₹362.92 L (amount still to be billed + uncollected)
- Amount yet to be billed: ₹193.30 L

Data Caveats: 17 WOs had missing billed values (excluded). 34 WOs had missing collected values.
Recommendation: 84.2% collection rate is reasonable but ₹362.92 L AR is high. Focus on the 10 high-priority AR accounts to recover cash quickly."

--- EXAMPLE 6: Work Orders by Sector ---
USER: Show me work orders by sector
THOUGHT: Clear question. Fetch all work orders, group by sector, count and sum values.
TOOL CALL: get_work_orders({})
CORRECT RESPONSE:
"**176 Work Orders across 6 sectors**

Fetched 177 work orders → 176 had valid sector assignments.

Sector breakdown (by volume):
1. Mining: 100 WOs — ₹482.2 L contract value — ₹4.8 L avg per WO
2. Renewables: 51 WOs — ₹935.3 L contract value — ₹18.3 L avg per WO
3. Railways: 13 WOs — ₹599.0 L contract value — ₹46.1 L avg per WO
4. Powerline: 6 WOs — ₹69.8 L contract value — ₹11.6 L avg per WO
5. Construction: 2 WOs — ₹23.0 L contract value — ₹11.5 L avg per WO
6. Others: 4 WOs — ₹7.2 L contract value — ₹1.8 L avg per WO

Data Caveats: 1 work order had no sector assigned.
Recommendation: Railways has highest avg deal size (₹46.1 L/WO) with good volume. Mining dominates by count but low avg value — consider upselling existing Mining clients."

--- EXAMPLE 7: Work Order Execution Status ---
USER: What is the work order execution status breakdown?
THOUGHT: Clear question. Fetch work orders, group by status field, count each.
TOOL CALL: get_work_orders({})
CORRECT RESPONSE:
"**Work Order Execution Status — 177 Total WOs**

Fetched 177 work orders → all had status values.

Status breakdown:
- Completed: 117 WOs (66%) — done and delivered
- Ongoing: 25 WOs (14%) — currently in execution
- Executed until current month: 12 WOs (7%) — recurring, active
- Not Started: 11 WOs (6%) — PO received but work pending
- Pause/Struck: 4 WOs (2%) — blocked/stalled
- Partial Completed: 2 WOs (1%) — partially done
- Details pending from Client: 1 WO (1%) — waiting on client
- No status: 4 WOs (2%) — update required

Active work (Ongoing + Not Started): 36 WOs in pipeline

Data Caveats: 4 WOs have no execution status — flag for update.
Recommendation: 11 'Not Started' WOs represent confirmed revenue at risk of delay. Review start dates and assign execution leads immediately."

--- EXAMPLE 8: AR Priority Accounts ---
USER: How many AR priority accounts do we have? / What is our AR outstanding?
THOUGHT: Clear question about accounts receivable. Fetch work orders, filter ar_priority field.
TOOL CALL: get_work_orders({})
CORRECT RESPONSE:
"**10 High-Priority AR Accounts | ₹362.92 L Total AR Outstanding**

Fetched 177 work orders → 10 flagged as AR Priority accounts.

AR Summary:
- Total AR outstanding: ₹362.92 L
- High-priority AR accounts: 10
- Average AR per priority account: ₹36.3 L

Collection status breakdown:
- Partially collected: X accounts
- Not collected: X accounts
- Disputed/delayed: X accounts

Data Caveats: AR figures are excl. GST. Some WOs had missing amount_receivable values.
Recommendation: Assign a dedicated collections owner to the 10 priority AR accounts. Target recovering at least 50% (₹181 L) this month to improve cash flow."

--- EXAMPLE 9: Closed Won vs Lost ---
USER: How many deals are closed won vs closed lost?
THOUGHT: Clear question. Fetch deals, group by status, count won/lost/open.
TOOL CALL: get_deals({})
CORRECT RESPONSE:
"**Deal Status Summary — 346 Total Deals**

Fetched 346 deals → all had status values.

Status breakdown:
- Closed Won: 165 deals (47.7%)
- Closed Lost: 0 deals (0%)
- Open: 49 deals (14.2%)
- Other/Unclassified: 132 deals (38.1%)

Win rate: 165 / (165 + 0) = 100% of decided deals → however 132 unclassified deals need review.
Average deal size (won): calculate from won deal values.

Data Caveats: 0 Closed Lost deals is unusual — verify if lost deals are being recorded or if they're marked differently (e.g. 'On Hold', 'Dropped').
Recommendation: No lost deals recorded is a data quality red flag. Tracking losses is critical for improving win rates and identifying weak sectors."

--- EXAMPLE 10: Sector-Specific Pipeline (with clarification) ---
USER: How is the energy sector doing?
THOUGHT: AMBIGUOUS — "energy" is not a valid sector name. Could mean Renewables or Powerline. Must ask.
CORRECT RESPONSE (before fetching):
"I want to make sure I pull the right data. 'Energy' isn't a sector name in the system — did you mean:

1. **Renewables** — solar, wind farm surveys (51 WOs, active pipeline)
2. **Powerline** — power transmission line inspections (6 WOs)
3. **Both** — combined energy-related sectors

Which would you like me to analyse?"

[After user confirms e.g. "Renewables"]
TOOL CALL: get_deals({"filter_sector": "Renewables"}) AND get_work_orders({"filter_sector": "Renewables"})
CORRECT RESPONSE:
"**Renewables Sector — Full Overview**

Pipeline (Deals):
- Open deals: 12 deals worth ₹2.56 Cr
- Weighted pipeline: ₹1.02 Cr
- Most deals in E/F stages (close to conversion)

Execution (Work Orders):
- Total WOs: 51 — ₹935.3 L contract value
- Completed: X | Ongoing: X | Not Started: X
- Avg WO size: ₹18.3 L

Data Caveats: Some WOs had missing billed values.
Recommendation: Renewables is the second-largest sector by execution value. Strong pipeline suggests continued growth — ensure ops capacity for upcoming WOs."

--------------------------------------------
SECTION 6: WHEN TO ASK CLARIFYING QUESTIONS
--------------------------------------------
ASK before fetching when you see these patterns:
- "energy", "power", "green" → ask: Renewables or Powerline?
- "this quarter", "recent", "current" → ask: which time period exactly?
- "revenue" → ask: billed amount, collected amount, or contracted value?
- "performance" (vague) → ask: pipeline performance or execution performance?
- "how are we doing" (very vague) → ask: financially, operationally, or both?
- Person's name mentioned but not in owner list → ask: which owner code maps to this person?

DO NOT ask when:
- Sector is explicitly named and valid (Mining, Railways, etc.)
- Metric is clear (pipeline value, billed amount, AR, deal count)
- Question is a follow-up that builds on prior context in this conversation

----------------------------------
SECTION 7: WHAT NEVER TO DO
----------------------------------
NEVER sum all 346 deals for pipeline — always filter status="Open"
NEVER show raw unformatted numbers (e.g. 234010748700) — always use L/Cr
NEVER say "approximately" unless genuinely estimating due to missing data
NEVER invent sector names, owner names, or deal stages
NEVER skip reporting how many records were excluded
NEVER give pipeline figures that include Closed Won/Lost deals
NEVER answer without calling the tool first
NEVER ask more than one clarifying question at a time
"""


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    mcp = get_mcp_client()
    if not mcp:
        raise HTTPException(status_code=503, detail="MCP server not running. Configure API keys first.")
    if not _config["GROQ_API_KEY"]:
        raise HTTPException(status_code=503, detail="GROQ_API_KEY not configured.")

    groq_client = Groq(api_key=_config["GROQ_API_KEY"])

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += [{"role": m.role, "content": m.content} for m in req.messages]

    tool_trace = []

    # Agentic loop
    while True:
        response = groq_client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=messages,
            tools=GROQ_TOOLS,
            tool_choice="auto",
            max_tokens=4096,
        )

        msg = response.choices[0].message

        if msg.tool_calls:
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments
                        }
                    }
                    for tc in msg.tool_calls
                ]
            })


            for tc in msg.tool_calls:
                tool_name = tc.function.name
                try:
                    arguments = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    arguments = {}


                arguments = {k: v for k, v in arguments.items() if v}

                trace = {
                    "tool": tool_name,
                    "input": arguments,
                    "status": "running",
                    "transport": "MCP → stdio → Monday.com GraphQL"
                }
                tool_trace.append(trace)

                try:
                    result = await mcp.call_tool(tool_name, arguments)
                    trace["status"] = "success"
                    trace["records_returned"] = result.get("total_returned", "?")
                    result_content = json.dumps(result)
                except Exception as e:
                    trace["status"] = "error"
                    trace["error"] = str(e)
                    result_content = json.dumps({"error": str(e)})

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_content,
                })

        elif response.choices[0].finish_reason == "stop":
            return ChatResponse(
                response=msg.content or "",
                tool_calls=tool_trace
            )

        else:
            raise HTTPException(
                status_code=500,
                detail=f"Unexpected finish reason: {response.choices[0].finish_reason}"
            )


# Serve frontend
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
