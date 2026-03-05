# DEPENDENCIES
import structlog
from aiohttp import web
from config.settings import settings
from mcp_servers.base_server import BaseMCPServer


logger = structlog.get_logger()

server = None


class IoTMCPServer(BaseMCPServer):
    """
    MCP server for IoT database
    """
    def __init__(self):
        super().__init__(host        = settings.db_iot_host,
                         port        = settings.db_iot_port,
                         database    = settings.db_iot_name,
                         user        = settings.db_iot_user,
                         password    = settings.db_iot_password.get_secret_value(),
                         server_name = "iot-mcp",
                        )


async def handle_mcp_request(request):
    try:
        data   = await request.json()
        method = data.get("method")
        params = data.get("params", {})

        result = await server.handle_request(method, params)

        return web.json_response(result)

    except Exception as e:
        logger.error("Unhandled MCP error", server = "iot-mcp", error = str(e))

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
                              "server" : "iot-mcp",
                            })


async def on_startup(app):
    """
    Initialize server on app startup
    """
    global server
    server = IoTMCPServer()

    await server.connect()
    
    logger.info("IoT MCP server initialized")


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
                port = settings.mcp_iot_port,
               )


if __name__ == "__main__":
    main()