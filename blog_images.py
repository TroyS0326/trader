import os
from datetime import datetime

from werkzeug.utils import secure_filename

import config


def _allowed_blog_image_filename(filename: str) -> bool:
    if not filename:
        return False
    if "/" in filename or "\\" in filename:
        return False
    lowered = filename.lower().strip()
    return lowered.endswith(".webp")


def save_blog_featured_image(file_storage, slug: str) -> dict:
    filename = (getattr(file_storage, "filename", "") or "").strip()
    if not _allowed_blog_image_filename(filename):
        return {"ok": False, "url": None, "filename": None, "error": "Only .webp images are allowed."}

    safe_name = secure_filename(filename)
    if not safe_name or not safe_name.lower().endswith(".webp"):
        return {"ok": False, "url": None, "filename": None, "error": "Invalid image filename."}

    max_bytes = int(getattr(config, "BLOG_IMAGE_MAX_BYTES", 3 * 1024 * 1024))
    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > max_bytes:
        return {"ok": False, "url": None, "filename": None, "error": f"Image exceeds max size of {max_bytes} bytes."}

    slug_base = secure_filename((slug or "blog-post").strip().lower()) or "blog-post"
    now = datetime.utcnow()
    upload_root = os.path.abspath(getattr(config, "BLOG_IMAGE_UPLOAD_DIR"))
    year_month_dir = os.path.join(upload_root, now.strftime("%Y"), now.strftime("%m"))
    os.makedirs(year_month_dir, exist_ok=True)

    candidate = f"{slug_base}-featured.webp"
    save_path = os.path.abspath(os.path.join(year_month_dir, candidate))
    if not save_path.startswith(upload_root):
        return {"ok": False, "url": None, "filename": None, "error": "Invalid upload path."}
    if os.path.exists(save_path):
        candidate = f"{slug_base}-featured-{int(now.timestamp())}.webp"
        save_path = os.path.abspath(os.path.join(year_month_dir, candidate))

    if not save_path.startswith(upload_root):
        return {"ok": False, "url": None, "filename": None, "error": "Invalid upload path."}

    file_storage.save(save_path)

    url_prefix = (getattr(config, "BLOG_IMAGE_URL_PREFIX", "/static/blog/uploads") or "/static/blog/uploads").rstrip("/")
    rel_dir = f"{now.strftime('%Y')}/{now.strftime('%m')}"
    image_url = f"{url_prefix}/{rel_dir}/{candidate}"
    return {"ok": True, "url": image_url, "filename": candidate, "error": None}
