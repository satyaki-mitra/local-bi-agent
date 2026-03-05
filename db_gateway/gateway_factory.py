# DEPENDENCIES
import sys
import structlog
from aiohttp import web
from config.settings import settings
from db_gateway.base_server import BaseDbServer


# Setup Logging
logger = structlog.get_logger()


_DOMAIN_CONFIG: dict = {"health"  : lambda: dict(host        = settings.db_health_host,
                                                 port        = settings.db_health_port,
                                                 database    = settings.db_health_name,
                                                 user        = settings.db_health_user,
                                                 password    = settings.db_health_password.get_secret_value(),
                                                 server_name = "health-gateway",
                                                 http_port   = settings.gateway_health_port,
                                                ),
                        "finance" : lambda: dict(host        = settings.db_finance_host,
                                                 port        = settings.db_finance_port,
                                                 database    = settings.db_finance_name,
                                                 user        = settings.db_finance_user,
                                                 password    = settings.db_finance_password.get_secret_value(),
                                                 server_name = "finance-gateway",
                                                 http_port   = settings.gateway_finance_port,
                                                ),
                        "sales"   : lambda: dict(host        = settings.db_sales_host,
                                                 port        = settings.db_sales_port,
                                                 database    = settings.db_sales_name,
                                                 user        = settings.db_sales_user,
                                                 password    = settings.db_sales_password.get_secret_value(),
                                                 server_name = "sales-gateway",
                                                 http_port   = settings.gateway_sales_port,
                                                ),
                        "iot"     : lambda: dict(host        = settings.db_iot_host,
                                                 port        = settings.db_iot_port,
                                                 database    = settings.db_iot_name,
                                                 user        = settings.db_iot_user,
                                                 password    = settings.db_iot_password.get_secret_value(),
                                                 server_name = "iot-gateway",
                                                 http_port   = settings.gateway_iot_port,
                                                ),
                       }

VALID_DOMAINS        = set(_DOMAIN_CONFIG.keys())



def create_gateway_app(domain: str) -> tuple[web.Application, int]:
    """
    Build and return an aiohttp Application + HTTP port for the given domain

    Returns:
    --------
        (app, http_port) : Caller is responsible for running the app

    Raises:
    -------
        ValueError       : If domain is not in VALID_DOMAINS
    """
    if domain not in _DOMAIN_CONFIG:
        raise ValueError(f"Unknown domain: '{domain}'. Valid domains: {sorted(VALID_DOMAINS)}")

    cfg    = _DOMAIN_CONFIG[domain]()
    server = BaseDbServer(host        = cfg["host"],
                          port        = cfg["port"],
                          database    = cfg["database"],
                          user        = cfg["user"],
                          password    = cfg["password"],
                          server_name = cfg["server_name"],
                         )


    async def handle_request(request: web.Request) -> web.Response:
        """
        Main gateway endpoint: returns a generic error message; full detail goes to structured logs only
        """
        try:
            data   = await request.json()
            method = data.get("method", "")
            params = data.get("params", {})

            if not method:
                return web.json_response({"success" : False,
                                          "error"   : "Missing 'method' in request body",
                                         },
                                         status = 400,
                                        )

            result = await server.handle_request(method, params)

            return web.json_response(result)

        except Exception as e:
            logger.error("Unhandled gateway error",
                         domain = domain,
                         error  = str(e),
                        )

            # Never exposes internal error detail to the caller
            return web.json_response({"success" : False,
                                      "error"   : "Internal gateway error",
                                     },
                                     status = 500,
                                    )


    async def health_check(request: web.Request) -> web.Response:
        """
        Liveness probe — used by Docker Compose healthcheck and orchestrator
        """
        pool_ok = False

        if server.pool is not None:
            try:
                # get_size() > 0 means the pool was successfully created and has connections
                pool_ok = server.pool.get_size() > 0
                
            except Exception:
                pool_ok = False

        return web.json_response({"status"   : "healthy" if pool_ok else "degraded",
                                  "server"   : cfg["server_name"],
                                  "domain"   : domain,
                                  "database" : cfg["database"],
                                  "pool"     : "connected" if pool_ok else "disconnected",
                                })


    async def on_startup(app: web.Application) -> None:
        await server.connect()
        logger.info("Gateway started",
                    domain = domain,
                    server = cfg["server_name"],
                   )


    async def on_cleanup(app: web.Application) -> None:
        await server.disconnect()
        logger.info("Gateway stopped",
                    domain = domain,
                   )


    # App assembly
    app = web.Application()
    app.router.add_post("/gateway", handle_request)
    app.router.add_get("/health",   health_check)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    return app, cfg["http_port"]


# CLI entry point
def main() -> None:
    """
    Launch a single domain gateway from the command line

    Usage: python -m db_gateway.gateway_factory <domain>
    """
    if ((len(sys.argv) != 2) or (sys.argv[1] not in VALID_DOMAINS)):
        print(f"Usage: python -m db_gateway.gateway_factory <domain>\n Valid domains: {sorted(VALID_DOMAINS)}",
              file = sys.stderr,
             )

        sys.exit(1)

    domain         = sys.argv[1]
    app, http_port = create_gateway_app(domain)

    logger.info("Launching gateway",
                domain = domain,
                port   = http_port,
               )

    web.run_app(app,
                host = "0.0.0.0",
                port = http_port,
               )


if __name__ == "__main__":
    main()