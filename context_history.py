"""保存插件实际发出的图文，供用户引用时重新注入对应聊天流。"""

from dataclasses import asdict, dataclass
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image, ImageOps

import base64
import json
import sqlite3


MAX_CONTEXT_RECORDS = 200
MAX_CONTEXT_BYTES = 128 * 1024 * 1024
MAX_CONTEXT_BATCH_CHARS = 4 * 1024 * 1024


def encode_image_preview(data: bytes) -> str:
    """保留已发送图片的可读预览，限制长期存储和单帧 RPC 的体积。"""

    with Image.open(BytesIO(data)) as source:
        source.seek(0)
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((1024, 1024))
        output = BytesIO()
        image.save(output, format="JPEG", quality=80)
    return base64.b64encode(output.getvalue()).decode("ascii")


@dataclass
class SentContext:
    stream_id: str
    message_id: str
    sent_at: str
    summary: str
    full_text: str
    images: List[Dict[str, str]]

    def batches(self, *, full: bool, quoted_by: str = "") -> List[List[Dict[str, Any]]]:
        """消息身份直接写入可见正文；大转发按图片拆帧，避免超过 SDK 单帧上限。"""

        prefix = (
            f'<message msg_id="{escape(self.message_id, quote=True)}" '
            f'time="{escape(self.sent_at, quote=True)}" '
            'user="机器人（推文转发）" is_self_message="true">\n'
        )
        if quoted_by:
            prefix += f"[用户消息 {quoted_by} 引用了此消息，以下为机器人此前实际转发的内容]\n"
        text = prefix + (self.full_text if full else self.summary)
        batches: List[List[Dict[str, Any]]] = [[{"type": "text", "content": text}]]
        size = len(text.encode("utf-8"))
        for image in self.images:
            description = image.get("description", "")
            if size + len(image["content"]) > MAX_CONTEXT_BATCH_CHARS:
                batches.append([{"type": "text", "content": prefix + "[同一消息的后续图片]"}])
                size = len(prefix.encode("utf-8")) + 100
            batches[-1].extend(
                [
                    {"type": "text", "content": image["label"] + ("" if description else "（画面尚未识别）")},
                    {"type": "image", "binary_data_base64": image["content"], "description": description},
                ]
            )
            size += len(image["content"]) + len(image["label"].encode("utf-8"))
        return batches


class ContextHistory:
    """以真实会话和 QQ 消息 ID 为联合键，保存最近 200 条、至多 128 MiB 记录。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS sent_context ("
            "stream_id TEXT NOT NULL, message_id TEXT NOT NULL, payload TEXT NOT NULL, "
            "PRIMARY KEY (stream_id, message_id))"
        )
        return connection

    def save(self, record: SentContext) -> None:
        payload = json.dumps(asdict(record), ensure_ascii=False)
        if len(payload.encode("utf-8")) > MAX_CONTEXT_BYTES:
            raise ValueError("单条推文上下文超过 128 MiB，无法持久化")
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    "INSERT OR REPLACE INTO sent_context VALUES (?, ?, ?)",
                    (record.stream_id, record.message_id, payload),
                )
                rows = connection.execute(
                    "SELECT rowid, length(CAST(payload AS BLOB)) FROM sent_context ORDER BY rowid DESC"
                ).fetchall()
                total = 0
                for index, (row_id, size) in enumerate(rows):
                    total += size
                    if index >= MAX_CONTEXT_RECORDS or total > MAX_CONTEXT_BYTES:
                        connection.execute("DELETE FROM sent_context WHERE rowid = ?", (row_id,))
        finally:
            connection.close()

    def get(self, stream_id: str, message_id: str) -> Optional[SentContext]:
        if not self.path.exists():
            return None
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT payload FROM sent_context WHERE stream_id = ? AND message_id = ?",
                (stream_id, message_id),
            ).fetchone()
            return SentContext(**json.loads(row[0])) if row else None
        finally:
            connection.close()
