from __future__ import annotations

from urllib.parse import urljoin, urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse


app = FastAPI(title="SupportFlow recording relay", docs_url=None, redoc_url=None)
ALLOWED_HOSTS = {"vats671653.megapbx.ru"}
MAX_REDIRECTS = 3


def validate_recording_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
    ):
        raise ValueError("Recording URL is outside the allowlist")
    return value


async def _close(response: httpx.Response, client: httpx.AsyncClient) -> None:
    await response.aclose()
    await client.aclose()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/recording")
async def recording(
    url: str = Query(min_length=12, max_length=4096),
    range_header: str = Header(default="", alias="Range"),
) -> StreamingResponse:
    try:
        current = validate_recording_url(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=20))
    response: httpx.Response | None = None
    try:
        for _ in range(MAX_REDIRECTS + 1):
            request = client.build_request(
                "GET", current, headers={"Range": range_header} if range_header else None
            )
            response = await client.send(request, stream=True)
            if response.status_code not in {301, 302, 303, 307, 308}:
                break
            location = response.headers.get("location", "")
            await response.aclose()
            current = validate_recording_url(urljoin(current, location))
        else:
            await client.aclose()
            raise HTTPException(status_code=502, detail="Too many recording redirects")
        if response is None:
            raise HTTPException(status_code=502, detail="Recording server did not respond")
        if response.status_code >= 400:
            status = response.status_code
            await response.aclose()
            await client.aclose()
            raise HTTPException(status_code=502, detail=f"Recording server returned {status}")
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail="Recording server is unavailable") from exc

    headers = {
        key: value for key, value in response.headers.items()
        if key.casefold() in {
            "content-type", "content-length", "content-range",
            "accept-ranges", "content-disposition",
        }
    }
    return StreamingResponse(
        response.aiter_bytes(), status_code=response.status_code,
        headers=headers,
        background=BackgroundTask(_close, response, client),
    )
