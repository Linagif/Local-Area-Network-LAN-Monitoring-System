from fastapi import FastAPI

app = FastAPI(
    title="LAN Monitoring System API",
    description="Monitor registered devices on a local network: "
                "connectivity, response times and history.",
    version="0.1.0",
)


@app.get("/health", tags=["System"])
def health() -> dict:
    """Liveness check: returns ok if the API process is running."""
    return {"status": "ok"}