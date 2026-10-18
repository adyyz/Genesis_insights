"""Container web entry point; native development keeps its loopback npm command."""
import argparse
import json
import os

from .config import Settings


def server_options(settings, port):
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("PORT must be a whole number from 1 to 65535")
    return {"app": "backend.api:app", "host": "0.0.0.0" if settings.mode == "cloud" else "127.0.0.1", "port": int(port)}


def main():
    parser = argparse.ArgumentParser(description="Serve the container web API and built frontend.")
    parser.add_argument("--check", action="store_true", help="Validate startup settings without starting a server or connecting to services.")
    parser.add_argument("--worker", action="store_true", help="Serve only the private task delivery endpoint.")
    args = parser.parse_args()
    settings = Settings.load()
    options = server_options(settings, os.getenv("PORT", "8080"))
    if args.worker:
        if not settings.worker_configured(): raise ValueError("Private worker requires cloud audience and task invoker identity")
        options["app"] = "backend.worker:app"
    if args.check:
        print(json.dumps({**options, "mode": settings.mode, "live_ai_enabled": settings.live_ai,
            "local_gemini_enabled": settings.local_gemini_enabled}))
        return
    import uvicorn
    uvicorn.run(**options, access_log=False)


if __name__ == "__main__": main()
