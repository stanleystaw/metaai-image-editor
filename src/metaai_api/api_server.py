"""FastAPI server for Meta AI — exposes the SDK as a REST API.

Endpoints match the original metaai-api repo:
    GET  /healthz              — health check
    POST /chat                 — send a chat message
    POST /image                — generate image from text
    POST /video                — generate video (returns 501 — not available)
    POST /video/async          — async video (returns 501)
    GET  /video/jobs/{job_id}  — job status (returns 404)
    POST /video/extend         — extend video (returns 501)
    POST /upload               — upload image (not yet supported via browser)
    GET  /conversations        — list conversations
    POST /media                — fetch media by card ID
    POST /reset                — reset browser session

Run:
    META_AI_DATR=... META_AI_ECTO_1_SESS=... python -m metaai_api.api_server
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import __version__
from .main import MetaAI
from .exceptions import (
    AuthenticationError, BrowserNotInstalledError, GenerationError,
    MetaAIError, TimeoutError as MetaAITimeoutError,
)

logger = logging.getLogger(__name__)

# ---- Pydantic models (matching original repo) ----

class ChatRequest(BaseModel):
    message: str
    stream: bool = False
    new_conversation: bool = False
    media_ids: Optional[list] = None
    attachment_metadata: Optional[dict] = None

class ImageRequest(BaseModel):
    prompt: str
    new_conversation: bool = False
    media_ids: Optional[list] = None
    attachment_metadata: Optional[dict] = None
    orientation: Optional[str] = None
    num_images: int = Field(1, ge=1, le=4)

class VideoRequest(BaseModel):
    prompt: str
    media_ids: Optional[list] = None
    attachment_metadata: Optional[dict] = None
    auto_poll: bool = True
    max_poll_attempts: int = Field(15, ge=1, le=60)
    poll_wait_seconds: int = Field(3, ge=1, le=30)
    orientation: Optional[str] = None
    wait_before_poll: int = Field(10, ge=0, le=60)
    max_attempts: int = Field(30, ge=1, le=60)

class VideoExtendRequest(BaseModel):
    media_id: str
    source_media_url: Optional[str] = None
    conversation_id: Optional[str] = None
    auto_poll: bool = True
    max_poll_attempts: int = Field(15, ge=1, le=60)
    poll_wait_seconds: int = Field(3, ge=1, le=30)

class FetchMediaRequest(BaseModel):
    card_id: str
    card_type: str = Field("IMAGE_CARD", pattern="^(IMAGE_CARD|VIDEO_CARD)$")

class HealthResponse(BaseModel):
    status: str; version: str; browser_ready: bool


MAX_UPLOAD_BYTES = int(os.getenv("META_AI_MAX_UPLOAD_BYTES", str(15 * 1024 * 1024)))
UPLOAD_DIR = Path(os.getenv("META_AI_UPLOAD_DIR", "/tmp/metaai-uploads"))
ALLOWED_IMAGE_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}


def _upload_path(upload_id: str) -> Path:
    """Resolve an opaque upload id without permitting path traversal."""
    try:
        normalized = str(uuid.UUID(upload_id))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid upload_id")
    matches = list(UPLOAD_DIR.glob(f"{normalized}.*"))
    if not matches:
        raise HTTPException(status_code=404, detail="Upload not found or expired")
    return matches[0]


async def _save_upload(image: UploadFile) -> tuple[str, Path]:
    content_type = (image.content_type or "").lower()
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=415, detail="Only PNG, JPEG and WebP are supported")

    data = await image.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded image is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Image exceeds the configured upload limit")

    signatures_ok = {
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": data.startswith(b"\xff\xd8\xff"),
        "image/webp": len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    }
    if not signatures_ok[content_type]:
        raise HTTPException(status_code=415, detail="File content does not match its image MIME type")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    upload_id = str(uuid.uuid4())
    path = UPLOAD_DIR / f"{upload_id}{ALLOWED_IMAGE_TYPES[content_type]}"
    path.write_bytes(data)
    return upload_id, path


# ---- Client manager ----

class ClientManager:
    def __init__(self):
        self._client: Optional[MetaAI] = None
        self._lock = threading.Lock()
        # Playwright's sync objects must be created and used on the same thread.
        # A single-worker executor also serializes edits on the shared browser page.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="metaai-browser")

    def get_client(self) -> MetaAI:
        with self._lock:
            if self._client is None:
                logger.info("Initializing MetaAI client...")
                self._client = MetaAI()
                self._client._get_browser()
                logger.info("MetaAI client ready")
            return self._client

    def _run(self, operation):
        return operation(self.get_client())

    async def run(self, operation):
        """Run a client operation on Playwright's dedicated thread."""
        future = self._executor.submit(self._run, operation)
        return await asyncio.wrap_future(future)

    def reset(self):
        """Queue browser cleanup on its owning thread."""
        def do_reset():
            with self._lock:
                if self._client:
                    try:
                        self._client.close()
                    except Exception:
                        pass
                    self._client = None
        return self._executor.submit(do_reset)

    def shutdown(self):
        try:
            self.reset().result(timeout=20)
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)

client_manager = ClientManager()


# ---- Auth ----

def verify_api_key(authorization: Optional[str] = Header(None)):
    expected_key = os.getenv("META_AI_API_KEY")
    if not expected_key:
        return True
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header.")
    token = authorization[7:] if authorization.startswith("Bearer ") else authorization
    if token != expected_key:
        raise HTTPException(status_code=401, detail="Invalid API key.")
    return True


def handle_error(e: Exception, timeout: int = 60):
    if isinstance(e, AuthenticationError):
        client_manager.reset()
        raise HTTPException(status_code=401, detail="Meta AI cookies expired.")
    if isinstance(e, MetaAITimeoutError):
        raise HTTPException(status_code=504, detail=f"Timed out after {timeout}s")
    if isinstance(e, GenerationError):
        raise HTTPException(status_code=502, detail=str(e))
    if isinstance(e, BrowserNotInstalledError):
        raise HTTPException(status_code=500, detail=str(e))
    raise HTTPException(status_code=500, detail=str(e))


# ---- App ----

app = FastAPI(
    title="MetaAI API",
    description="Unofficial API server for Meta AI — image generation, chat, and more.",
    version=__version__,
    docs_url="/docs",
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


@app.get("/healthz", response_model=HealthResponse)
async def health():
    """Health check endpoint."""
    return HealthResponse(status="ok", version=__version__,
                          browser_ready=client_manager._client is not None)


@app.post("/chat")
async def chat(req: ChatRequest, _=Depends(verify_api_key)):
    """Send a chat message to Meta AI."""
    start = time.time()
    try:
        result = await client_manager.run(lambda client: client.prompt(
            req.message,
            stream=req.stream,
            new_conversation=req.new_conversation,
            media_ids=req.media_ids,
            attachment_metadata=req.attachment_metadata,
        ))
        return {
            "success": True,
            "message": result.get("message", ""),
            "conversation_id": result.get("conversation_id"),
            "elapsed_seconds": round(time.time()-start, 1),
        }
    except Exception as e:
        handle_error(e, 60)


@app.post("/image")
async def generate_image(req: ImageRequest, _=Depends(verify_api_key)):
    """Generate an image from a text prompt."""
    start = time.time()
    try:
        result = await client_manager.run(lambda client: client.generate_image_new(
            req.prompt,
            orientation=req.orientation or "VERTICAL",
            num_images=req.num_images,
            media_ids=req.media_ids,
            attachment_metadata=req.attachment_metadata,
            timeout=120,
        ))
        return {
            "success": result["success"],
            "image_urls": result.get("image_urls", []),
            "conversation_id": result.get("conversation_id"),
            "prompt": result.get("prompt", ""),
            "elapsed_seconds": round(time.time()-start, 1),
        }
    except Exception as e:
        handle_error(e, 120)


@app.post("/video")
async def generate_video(req: VideoRequest, _=Depends(verify_api_key)):
    """Video generation is NOT available on Meta AI. Returns 501."""
    raise HTTPException(status_code=501,
        detail="Video generation is not available on Meta AI. "
               "Meta AI responds: 'I can't generate videos right now.'")


@app.post("/video/async")
async def generate_video_async(req: VideoRequest, _=Depends(verify_api_key)):
    """Video generation is NOT available on Meta AI. Returns 501."""
    raise HTTPException(status_code=501,
        detail="Video generation is not available on Meta AI.")


@app.get("/video/jobs/{job_id}")
async def video_job_status(job_id: str):
    """Video generation is NOT available. Returns 404."""
    raise HTTPException(status_code=404, detail="No video jobs — video generation is not available.")


@app.post("/video/extend")
async def video_extend(req: VideoExtendRequest, _=Depends(verify_api_key)):
    """Video generation is NOT available on Meta AI. Returns 501."""
    raise HTTPException(status_code=501,
        detail="Video extend is not available on Meta AI.")


@app.post("/upload")
async def upload_image(
    image: UploadFile = File(...),
    _=Depends(verify_api_key),
):
    """Stage an image for a later /edit call.

    Files live in Render's ephemeral /tmp storage and disappear on restart.
    Prefer sending the image directly to /edit when possible.
    """
    upload_id, path = await _save_upload(image)
    return {
        "success": True,
        "upload_id": upload_id,
        "filename": image.filename,
        "content_type": image.content_type,
        "size": path.stat().st_size,
    }


@app.post("/edit")
async def edit_image(
    prompt: str = Form(...),
    image: Optional[UploadFile] = File(None),
    upload_id: Optional[str] = Form(None),
    timeout: int = Form(180, ge=30, le=300),
    _=Depends(verify_api_key),
):
    """Edit an image with a natural-language prompt using Meta AI.

    Supply either `image` directly or an `upload_id` returned by /upload.
    """
    if bool(image) == bool(upload_id):
        raise HTTPException(
            status_code=400,
            detail="Supply exactly one of image or upload_id",
        )

    temporary = False
    if image is not None:
        _, path = await _save_upload(image)
        temporary = True
    else:
        path = _upload_path(upload_id or "")

    start = time.time()
    try:
        result = await client_manager.run(
            lambda client: client.edit_image(str(path), prompt, timeout=timeout)
        )
        return {
            "success": True,
            "image_urls": result.get("image_urls", []),
            "conversation_id": result.get("conversation_id"),
            "prompt": prompt,
            "elapsed_seconds": round(time.time() - start, 1),
        }
    except Exception as exc:
        handle_error(exc, timeout)
    finally:
        if temporary:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


@app.post("/v1/images/edits")
async def openai_compatible_edit(
    prompt: str = Form(...),
    image: UploadFile = File(...),
    _=Depends(verify_api_key),
):
    """OpenAI-shaped multipart image-edit endpoint.

    The response contains temporary Meta CDN URLs rather than b64_json.
    """
    _, path = await _save_upload(image)
    try:
        result = await client_manager.run(
            lambda client: client.edit_image(str(path), prompt, timeout=180)
        )
        return {
            "created": int(time.time()),
            "data": [{"url": url} for url in result.get("image_urls", [])],
        }
    except Exception as exc:
        handle_error(exc, 180)
    finally:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


@app.get("/conversations")
async def list_conversations(_=Depends(verify_api_key)):
    """List all conversations."""
    try:
        convs = await client_manager.run(lambda client: client.list_conversations())
        return {"success": True, "conversations": convs}
    except Exception as e:
        handle_error(e, 10)


@app.post("/media")
async def fetch_media(req: FetchMediaRequest, _=Depends(verify_api_key)):
    """Fetch media URLs by card ID."""
    try:
        data, urls = await client_manager.run(lambda client: (
            (lambda payload: (payload, client.generation_api.extract_media_urls(payload)))(
                client.fetch_card_media(req.card_id, req.card_type)
            )
        ))
        return {
            "success": True,
            "urls": urls,
            "card_id": req.card_id,
            "card_type": req.card_type,
        }
    except Exception as e:
        handle_error(e, 30)


@app.post("/reset")
async def reset_client(_=Depends(verify_api_key)):
    """Reset the browser session."""
    await asyncio.wrap_future(client_manager.reset())
    return {"status": "reset", "message": "Browser will restart on next request"}


@app.on_event("shutdown")
def shutdown():
    client_manager.shutdown()


def main():
    import uvicorn
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    has_single_cookie = bool(os.getenv("META_AI_COOKIE"))
    has_split_cookies = bool(os.getenv("META_AI_DATR") and os.getenv("META_AI_ECTO_1_SESS"))
    if not has_single_cookie and not has_split_cookies:
        print("ERROR: Set META_AI_COOKIE to your complete meta.ai Cookie header.")
        print("Alternatively set META_AI_DATR and META_AI_ECTO_1_SESS separately.")
        raise SystemExit(1)
    print(f"Starting MetaAI API server on {host}:{port}")
    print(f"  Docs: http://localhost:{port}/docs")
    print(f"  API key: {'enabled' if os.getenv('META_AI_API_KEY') else 'disabled'}")
    uvicorn.run(app, host=host, port=port)

if __name__ == "__main__":
    main()
