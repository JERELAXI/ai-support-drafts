from fastapi import FastAPI

app = FastAPI(title="Helpdesk mock")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "OK"}
