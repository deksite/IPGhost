import os, re, json, asyncio
from urllib.parse import urlsplit, urlunsplit, urlencode
from fastapi import FastAPI, Request, Response
import httpx

FLARESOLVERR_URL = os.getenv("FLARESOLVERR_URL", "http://flaresolverr:8191/v1")
TARGET_ORIGIN    = os.getenv("TARGET_ORIGIN", "https://www.comprasparaguai.com.br").rstrip("/")
UPSTREAM_PROXY   = os.getenv("UPSTREAM_PROXY_URL", "").strip() or None
BLOCK_PHRASES    = os.getenv("BLOCK_PHRASES", "checking your browser|cf-browser-verification|just a moment|Access denied|403 Forbidden")
REQUEST_TIMEOUT  = int(os.getenv("REQUEST_TIMEOUT", "90"))

BLOCK_REGEX = re.compile(BLOCK_PHRASES, re.IGNORECASE)

app = FastAPI(title="Flare Direct Proxy", version="1.0")

# guardamos uma única sessão para o domínio
SESSION_ID: str | None = None

async def flaresolverr(cmd: dict) -> dict:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        r = await client.post(FLARESOLVERR_URL, json=cmd)
        r.raise_for_status()
        return r.json()

async def create_session() -> str:
    global SESSION_ID
    # você pode pedir um nome fixo; FlareSolverr retorna o id real
    payload = {"cmd": "sessions.create"}
    if UPSTREAM_PROXY:
        payload["proxy"] = {"url": UPSTREAM_PROXY}
    data = await flaresolverr(payload)
    SESSION_ID = data.get("session")
    return SESSION_ID

async def destroy_session():
    global SESSION_ID
    if SESSION_ID:
        try:
            await flaresolverr({"cmd": "sessions.destroy", "session": SESSION_ID})
        except Exception:
            pass
    SESSION_ID = None

async def ensure_session():
    if not SESSION_ID:
        await create_session()

async def solve_get(url: str, headers: dict | None = None) -> dict:
    await ensure_session()
    req = {
        "cmd": "request.get",
        "session": SESSION_ID,
        "url": url,
        "maxTimeout": REQUEST_TIMEOUT * 1000,
        "headers": headers or {},
        "returnOnlyCookies": False
    }
    if UPSTREAM_PROXY:
        req["proxy"] = {"url": UPSTREAM_PROXY}
    return await flaresolverr(req)

def is_blocked(status: int, body: str) -> bool:
    if status >= 400:
        return True
    if BLOCK_REGEX.search(body or ""):
        return True
    return False

def build_target_url(path_qs: str) -> str:
    # path_qs já vem com / e query; apenas reanexa à origem
    if path_qs.startswith("/"):
        return TARGET_ORIGIN + path_qs
    return TARGET_ORIGIN + "/" + path_qs

def forward_headers(req: Request) -> dict:
    # repassa alguns headers úteis (UA pode ser ignorado; FlareSolverr gerencia)
    out = {}
    for h in ["accept", "accept-language", "user-agent"]:
        if h in req.headers:
            out[h] = req.headers[h]
    return out

@app.get("/{full_path:path}")
async def passthrough_get(full_path: str, request: Request):
    # reconstrói a query string original
    qs = request.url.query
    path_qs = f"/{full_path}" + (f"?{qs}" if qs else "")
    target_url = build_target_url(path_qs)

    # 1ª tentativa
    res1 = await solve_get(target_url, headers=forward_headers(request))
    status1 = res1.get("status", 0)
    solution = res1.get("solution", {}) if isinstance(res1, dict) else {}
    body1 = solution.get("response", "")
    status_code1 = solution.get("status", 200)
    hdrs1 = solution.get("headers", {})

    if is_blocked(status_code1, body1):
        # renova sessão e tenta novamente
        await destroy_session()
        await ensure_session()
        res2 = await solve_get(target_url, headers=forward_headers(request))
        solution2 = res2.get("solution", {})
        body2 = solution2.get("response", "")
        status_code2 = solution2.get("status", 200)
        hdrs2 = solution2.get("headers", {})

        # devolve a 2ª resposta (bloqueada ou não)
        return Response(
            content=body2.encode("utf-8", "ignore"),
            status_code=status_code2,
            headers=_filter_out_hop_by_hop(hdrs2),
            media_type=solution2.get("headers", {}).get("content-type", "text/html; charset=utf-8")
        )

    # ok na 1ª tentativa
    return Response(
        content=body1.encode("utf-8", "ignore"),
        status_code=status_code1,
        headers=_filter_out_hop_by_hop(hdrs1),
        media_type=solution.get("headers", {}).get("content-type", "text/html; charset=utf-8")
    )

def _filter_out_hop_by_hop(h: dict) -> dict:
    """Remove headers hop-by-hop e coisas que podem conflitar na resposta."""
    if not isinstance(h, dict):
        return {}
    drop = {"transfer-encoding", "content-encoding", "content-length", "connection", "keep-alive", "proxy-authenticate",
            "proxy-authorization", "te", "trailers", "upgrade"}
    return {k: v for k, v in h.items() if k.lower() not in drop}
