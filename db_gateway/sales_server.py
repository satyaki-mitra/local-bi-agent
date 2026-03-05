# DEPENDENCIES
import structlog
from aiohttp import web
from config.settings import settings
from mcp_servers.base_server import BaseMCPServer

logger = structlog.get_logger()

server = None


class SalesMCPServer(BaseMCPServer):
    """
    MCP server for Sales database
    """
    def __init__(self):
        super().__init__(host        = settings.db_sales_host,
                         port        = settings.db_sales_port,
                         database    = settings.db_sales_name,
                         user        = settings.db_sales_user,
                         password    = settings.db_sales_password.get_secret_value(),
                         server_name = "sales-mcp",
                        )


async def handle_mcp_request(request):
    try:
        data   = await request.json()
        method = data.get("method")
        params = data.get("params", {})

        result = await server.handle_request(method, params)

        return web.json_response(result)

    except Exception as e:
        logger.error("Unhandled MCP error", server = "sales-mcp", error = str(e))

        return web.json_response({"success" : False,
                                  "error"   : str(e),
                                 },
                                 status = 500,
                                )


async def health_check(request):
    """
    Health check endpoint
    """
    return web.json_response({"status" : "healthy", 
                              "server" : "sales-mcp",
                            })


async def on_startup(app):
    """
    Initialize server on app startup
    """
    global server
    
    server = SalesMCPServer()
    
    await server.connect()
    
    logger.info("Sales MCP server initialized")


async def on_cleanup(app):
    """
    Cleanup on shutdown
    """
    if server:
        await server.disconnect()


def main():
    """
    Main entry point
    """
    app = web.Application()

    app.router.add_post("/mcp", handle_mcp_request)
    app.router.add_get("/health", health_check)
    
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    
    web.run_app(app,
                host = "0.0.0.0",
                port = settings.mcp_sales_port,
               )


if __name__ == "__main__":
    main()