# DEPENDENCIES
import re
import time
import httpx
import base64
import structlog
import chainlit as cl
from pathlib import Path
from config.settings  import settings
from config.constants import DOMAIN_TABLE_MAP


# Setup Logging
logger      = structlog.get_logger()


# Get backend URL
BACKEND_URL = settings.backend_url


# Helpers
def _format_number(value) -> str:
    try:
        f = float(value)

        if (f == int(f)):
            return f"{int(f):,}"

        return f"{f:,.2f}"

    except (TypeError, ValueError):
        return str(value)


def _build_answer_md(result: dict) -> str:
    """
    Render the LLM answer block with clean BI-grade formatting

    Shows:
    ------
    - Domain DB badge (single-domain or cross-DB dual-badge)
    - Execution time
    - Row count
    - Warning block if errors present
    """
    answer            = result.get("answer", "No answer generated.")
    sql_list          = result.get("sql_executed", [])
    exec_ms           = result.get("execution_time_ms", 0)
    errors            = result.get("errors", [])

    # Uses actual data length, not regex on LLM text
    row_count         = len(result.get("data", []))
    row_hint          = f"  ·  **{row_count:,} rows**" if row_count > 0 else ""

    exec_s            = exec_ms / 1000

    # Cross-DB badge vs single-domain badge
    is_cross_db       = result.get("is_cross_db", False)
    databases_queried = result.get("databases_queried", [])

    if (is_cross_db and (len(databases_queried) >= 2)):
        db_badge = f"`🔗 {databases_queried[0].upper()} + {databases_queried[1].upper()} DB`  "

    elif sql_list:
        db_badge  = ""
        sql_upper = sql_list[0].upper()

        for db_type, tables in DOMAIN_TABLE_MAP.items():
            if any(t in sql_upper for t in tables):
                db_badge = f"`{db_type.value.upper()} DB`  "
                break

    else:
        db_badge = ""

    header        = f"{db_badge}⏱ `{exec_s:.2f}s`{row_hint}"
    warning_block = ""

    if errors:
        warning_block = f"\n\n> ⚠️ **Note:** Query completed with warnings — {errors[-1]}"

    return (f"{header}\n\n"
            f"---\n\n"
            f"{answer}"
            f"{warning_block}"
           )


def _build_sql_md(sql_list: list) -> str:
    if not sql_list:
        return ""

    sql_text = "\n\n".join(sql_list)

    return f"```sql\n{sql_text}\n```"


def _decode_b64_image(b64_str: str) -> bytes | None:
    try:
        if "," in b64_str:
            b64_str = b64_str.split(",", 1)[1]

        return base64.b64decode(b64_str)

    except Exception as e:
        logger.error("Failed to decode base64 image", 
                     error = str(e),
                    )
        return None


# Chat start
@cl.on_chat_start
async def start():
    cl.user_session.set("session_id", "")
    cl.user_session.set("last_result", None)

    await cl.Message(content = ("## LocalGenBI · Autonomous BI Platform\n\n"
                                "Ask questions about your business data in plain English. "
                                "I'll query the right database, generate SQL, and give you insights "
                                "— with charts and downloadable exports.\n\n"
                                "**Available databases:** Sales · Finance · Health · IoT\n\n"
                                "---\n\n"
                                "> 💡 After any result, use the **download buttons** below the response to export as JSON, CSV, or PNG chart.\n"
                                "> 🔄 Type `/clear` at any time to reset conversation history."
                               )
                    ).send()

    # Resume session history
    session_id = cl.user_session.get("session_id", "")

    if session_id and settings.session_history_enabled:
        try:
            async with httpx.AsyncClient(timeout = 10.0) as client:
                resp = await client.get(f"{BACKEND_URL}/api/sessions/{session_id}/history",
                                        params = {"last_n": 3},
                                       )

            if (resp.status_code == 200):
                hist   = resp.json()

                if (hist.get("turns", 0) > 0):
                    last_q = hist["history"][0].get("query", "")

                    await cl.Message(content = (f"_Resuming session — **{hist['turns']}** previous "
                                                f"{'query' if hist['turns'] == 1 else 'queries'} in history. "
                                                f"Last question: \"{last_q[:80]}{'…' if len(last_q) > 80 else ''}\"_"
                                               )
                                    ).send()

        except Exception:
            # Non-fatal — history resume is best-effort
            pass


# Message router
@cl.on_message
async def handle_message(message: cl.Message):
    content = message.content.strip()

    # /clear command
    if (content.lower() == "/clear"):
        session_id = cl.user_session.get("session_id", "")

        if session_id and settings.session_history_enabled:
            try:
                async with httpx.AsyncClient(timeout = 10.0) as client:
                    await client.delete(f"{BACKEND_URL}/api/sessions/{session_id}/history")

            except Exception as e:
                logger.warning("History clear failed", error = str(e))

        cl.user_session.set("last_result", None)
        await cl.Message(content = "🗑 Conversation history cleared. Starting fresh.").send()
        return

    if content.startswith("/download"):
        await _handle_legacy_download(content)
        return

    await _process_query(message)


# Query pipeline
async def _process_query(message: cl.Message):
    session_id = cl.user_session.get("session_id", "")
    thinking   = cl.Message(content = "")

    await thinking.send()

    async with cl.Step(name = "🧠  Reasoning", show_input = False) as step:
        step.output = ("Analyzing query → selecting database → "
                       "fetching schema → generating SQL → executing → "
                       "building insights…"
                      )

    try:
        async with httpx.AsyncClient(timeout = 300.0) as client:
            response = await client.post(f"{BACKEND_URL}/api/query",
                                         json = {"query"      : message.content,
                                                 "session_id" : session_id,
                                                },
                                        )

        if (response.status_code != 200):
            try:
                detail = response.json().get("detail") or response.text

            except ValueError:
                detail = response.text

            await thinking.remove()
            await cl.Message(content = (f"### ❌ Backend Error `{response.status_code}`\n\n"
                                        f"```\n{detail}\n```"
                                       )
                            ).send()

            logger.error("Backend HTTP error",
                         status = response.status_code,
                         detail = detail,
                        )
            return

        result = response.json()
        cl.user_session.set("session_id", result.get("session_id", ""))
        cl.user_session.set("last_result", result)

        await thinking.remove()
        await _render_response(result)

    except httpx.TimeoutException:
        await thinking.remove()
        await cl.Message(content = ("### ⏱ Timeout\n\n"
                                    "The model took too long to respond. "
                                    "Try a more specific query or check that Ollama is running."
                                   )
                        ).send()

    except Exception as e:
        await thinking.remove()
        logger.error("Frontend processing error", error = str(e))
        await cl.Message(content = f"### ❌ Unexpected Error\n\n```\n{e}\n```").send()


# Response renderer
async def _render_response(result: dict):
    sql_list        = result.get("sql_executed", [])
    visualization   = result.get("visualization")
    answer_md       = _build_answer_md(result)
    elements        = list()
    chart_available = False

    if (visualization and visualization.get("base64_image")):
        image_bytes = _decode_b64_image(visualization["base64_image"])

        if image_bytes:
            elements.append(cl.Image(name    = "📊 Chart",
                                     content = image_bytes,
                                     display = "inline",
                                     size    = "large",
                                    )
                           )
            chart_available = True

    await cl.Message(content  = answer_md,
                     elements = elements,
                    ).send()

    if sql_list:
        await cl.Message(content  = f"**🛠 Generated SQL**\n\n{_build_sql_md(sql_list)}",
                         author   = "SQL Engine",
                         language = "sql",
                        ).send()

    actions = _build_download_actions(result, chart_available)

    if actions:
        await cl.Message(content = "**📥 Export Results**",
                         actions = actions,
                        ).send()


def _build_download_actions(result: dict, chart_available: bool) -> list:
    """
    Build Chainlit Action buttons for export: Chainlit uses 'payload' (not 'value') for passing
    structured data to action callbacks as of Chainlit ≥ 1.0
    """
    actions    = list()
    has_data   = bool(result.get("sql_executed"))
    session_id = result.get("session_id", "")

    if has_data:
        actions.append(cl.Action(name        = "download_json",
                                 payload     = {"session_id": session_id},
                                 label       = "⬇ JSON",
                                 description = "Full result with SQL & reasoning trace",
                                )
                      )

        actions.append(cl.Action(name        = "download_csv",
                                 payload     = {"session_id": session_id},
                                 label       = "⬇ CSV",
                                 description = "Tabular data as spreadsheet",
                                )
                      )

    if chart_available:
        actions.append(cl.Action(name        = "download_png",
                                 payload     = {"session_id": session_id},
                                 label       = "⬇ Chart PNG",
                                 description = "Download visualization image",
                                )
                      )

    if has_data:
        actions.append(cl.Action(name        = "download_analysis",
                                 payload     = {"session_id": session_id},
                                 label       = "⬇ Analysis Report",
                                 description = "Statistical summary report",
                                )
                      )

    return actions


# Action handlers
@cl.action_callback("download_json")
async def on_download_json(action: cl.Action):
    await _trigger_export("json", action.payload.get("session_id"))


@cl.action_callback("download_csv")
async def on_download_csv(action: cl.Action):
    await _trigger_export("csv", action.payload.get("session_id"))


@cl.action_callback("download_png")
async def on_download_png(action: cl.Action):
    await _trigger_export("png", action.payload.get("session_id"))


@cl.action_callback("download_analysis")
async def on_download_analysis(action: cl.Action):
    await _trigger_export("analysis", action.payload.get("session_id"))


async def _trigger_export(format_type: str, session_id: str):
    """
    Call the FastAPI export endpoint and serve the file as a Chainlit attachment.
    """
    last_result = cl.user_session.get("last_result")

    if not last_result:
        await cl.Message(content = "❌ No result in session. Run a query first.").send()
        return

    export_payload = {"query"           : last_result.get("query", ""),
                      "answer"          : last_result.get("answer", ""),
                      "data"            : last_result.get("data", []),
                      "sql_queries"     : last_result.get("sql_executed", []),
                      "reasoning_trace" : last_result.get("reasoning_trace", []),
                     }

    status_msg = cl.Message(content = f"⏳ Generating **{format_type.upper()}** export…")

    await status_msg.send()

    try:
        async with httpx.AsyncClient(timeout = 60.0) as client:
            response = await client.post(f"{BACKEND_URL}/api/export/{format_type}",
                                         json = export_payload,
                                        )

        if (response.status_code != 200):
            await status_msg.remove()
            await cl.Message(content = f"❌ Export failed `{response.status_code}`: {response.text}").send()
            return

        cd_header = response.headers.get("content-disposition", "")
        filename  = (cd_header.split("filename=")[-1].strip('"') if "filename=" in cd_header else f"export_{format_type}_{int(time.time())}.{format_type}")

        temp_dir  = Path(settings.chainlit_temp_export_dir)

        temp_dir.mkdir(parents = True, exist_ok = True)

        temp_path = temp_dir / filename

        with open(temp_path, "wb") as f:
            f.write(response.content)

        await status_msg.remove()
        await cl.Message(content  = f"✅ **{format_type.upper()} ready** — `{filename}`",
                         elements = [cl.File(name    = filename,
                                             path    = str(temp_path),
                                             display = "inline",
                                            )
                                    ],
                        ).send()

        logger.info("Export served",
                    format   = format_type,
                    filename = filename,
                   )

    except httpx.TimeoutException:
        await status_msg.remove()
        await cl.Message(content = "❌ Export timed out. Try a smaller dataset.").send()

    except Exception as e:
        await status_msg.remove()
        logger.error("Export error", format = format_type, error = str(e))
        await cl.Message(content = f"❌ Export error: `{e}`").send()


async def _handle_legacy_download(content: str) -> None:
    """
    Handle /download <format> text commands as a fallback to the action buttons: supported: 
    
    - /download json 
    - /download csv 
    - /download png 
    - /download analysis 
    - /download txt
    """
    parts       = content.strip().split()
    valid_fmts  = {"json", "csv", "png", "analysis", "txt"}

    if (len(parts) < 2) or (parts[1].lower() not in valid_fmts):
        await cl.Message(content = (f"❓ Usage: `/download <format>`\n\n"
                                    f"Supported formats: {', '.join(sorted(valid_fmts))}"
                                   )
                        ).send()
        return

    format_type = parts[1].lower()
    session_id  = cl.user_session.get("session_id", "")

    await _trigger_export(format_type, session_id)


# Chat end
@cl.on_chat_end
async def end():
    cl.user_session.set("last_result", None)
    logger.info("Chat session ended")