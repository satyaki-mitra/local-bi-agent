# DEPENDENCIES
import sys
import uvicorn
import traceback
from pathlib import Path
from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


# Add project root to Python path
sys.path.insert(0, str(Path(__file__).parent))

# Import the existing FastAPI app from your backend
from backend.main import app
from config.settings import settings


# Path to frontend directory and index.html
FRONTEND_DIR = Path(__file__).parent / "frontend"
INDEX_HTML   = FRONTEND_DIR / "index.html"


class ExceptionLoggingMiddleware(BaseHTTPMiddleware):
    """
    Catch any exception from the backend and print the full traceback
    """
    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        
        except Exception as e:
            print("\n❌ EXCEPTION CAUGHT IN BACKEND:")
            traceback.print_exc()
            print(f"Exception type: {type(e).__name__}")
            print(f"Exception args: {e.args}")
            
            # Return a 500 with details (the frontend will display this)
            return JSONResponse(status_code = 500,
                                content     = {"detail" : f"Internal server error: {str(e)}", 
                                               "type"   : type(e).__name__,
                                              }
                               )


class FrontendMiddleware(BaseHTTPMiddleware):
    """
    Serve frontend HTML at the root path
    """
    async def dispatch(self, request: Request, call_next):
        if (request.url.path == "/"):
            if not INDEX_HTML.exists():
                return HTMLResponse(content    = f"""
                                                      <html>
                                                          <body style="background:#0D1117;color:#E6EDF3;font-family:sans-serif;padding:40px;">
                                                              <h1>❌ Frontend file not found</h1>
                                                              <p>Expected at: {INDEX_HTML}</p>
                                                          </body>
                                                      </html>
                                                  """,
                                    status_code = 404,
                                   )

            return FileResponse(INDEX_HTML)

        return await call_next(request)


# Add middlewares: frontend first, then exception logging
app.add_middleware(FrontendMiddleware)
app.add_middleware(ExceptionLoggingMiddleware)



# Start the Web-application
if __name__ == "__main__":
    host   = settings.fastapi_host
    port   = settings.fastapi_port
    reload = settings.fastapi_reload

    print(f"\n🚀 LocalGenBI unified server (with debug logging)")
    print(f"📁 Frontend: {INDEX_HTML}")
    print(f"🌐 URL: http://{host if (host != '0.0.0.0') else 'localhost'}:{port}")
    print(f"📡 API:  http://{host if (host != '0.0.0.0') else 'localhost'}:{port}/api/...\n")
    print("⚠️  Make sure the standalone backend is NOT running on the same port.\n")

    uvicorn.run("backend.main:app",  
                host   = host,
                port   = port,
                reload = reload,
               )