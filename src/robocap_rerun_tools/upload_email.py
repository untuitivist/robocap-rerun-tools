"""End-of-run summaries for manually serialized batch uploads."""

import os
import smtplib
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from time import monotonic

from dotenv import dotenv_values


@dataclass
class BatchSummary:
    root: str
    target: str
    started: float = field(default_factory=monotonic)
    discovered: int = 0
    skipped: int = 0
    new: int = 0
    replaced: int = 0
    failed: int = 0
    hours: float = 0
    unknown_durations: int = 0
    excluded: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    last_output: str = ""

    def text(self):
        elapsed = int(monotonic() - self.started)
        return (
            f"批量上传总结\n时间：{datetime.now().astimezone().isoformat(timespec='seconds')}\n"
            f"根目录：{self.root}\n目标数据集：{self.target}\n"
            f"扫描 Session 数：{self.discovered}\n"
            f"上传成功 Session 数：{self.new + self.replaced}（新增 {self.new}，替换 {self.replaced}）\n"
            f"远端完整跳过：{self.skipped}\n上传/准备失败：{self.failed}\n"
            f"本地排除：{len(self.excluded)}\n"
            f"成功上传数据时长：{self.hours:.4f} hour（每 Session 按单个视频时长计算）\n"
            f"成功项中时长未知：{self.unknown_durations}\n"
            f"总耗时：{elapsed // 3600:02d}:{elapsed % 3600 // 60:02d}:{elapsed % 60:02d}\n\n"
            "错误报告：\n" + ("\n".join(self.errors) or "无")
            + "\n\n本地排除详情：\n" + ("\n".join(self.excluded) or "无")
            + "\n\n结束日志：\n" + self.last_output[-12000:]
        )


def send_batch_summary(summary: BatchSummary, env_path: Path) -> str:
    values = dotenv_values(env_path, interpolate=False) if env_path.is_file() else {}

    def setting(key, default=""):
        return str(os.environ.get(key) or values.get(key) or default).strip()

    sender = setting("UPLOAD_EMAIL_SENDER")
    password = setting("UPLOAD_EMAIL_AUTH_CODE")
    receiver = setting("UPLOAD_EMAIL_RECEIVER", sender)
    if not sender or not password or not receiver:
        return "总结邮件未发送：请在 .env 配置 UPLOAD_EMAIL_SENDER / UPLOAD_EMAIL_AUTH_CODE / UPLOAD_EMAIL_RECEIVER。"
    body = summary.text()
    message = EmailMessage()
    message["From"] = sender
    message["To"] = receiver
    message["Subject"] = (
        f"批量上传结束 | 成功 {summary.new + summary.replaced} Session | "
        f"{summary.hours:.2f} hour | 失败 {summary.failed} | "
        f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S}"
    )
    message.set_content(body)
    message.add_attachment(body.encode("utf-8"), maintype="text", subtype="plain",
                           filename="batch-upload-report.txt")
    try:
        with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30) as smtp:
            smtp.login(sender, password)
            smtp.send_message(message)
    except Exception as exc:
        return f"总结邮件发送失败：{type(exc).__name__}（上传结果不受影响）。"
    return f"总结邮件已发送至 {receiver}。"
