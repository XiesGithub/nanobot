"""Manual WebUI QA with fictional local mail, no mailbox or generative provider.

Run explicitly: python -m tests.knowledge.mail.preview --root tmp/mail-ui-preview
Only loopback is bound. The fixed password is for this disposable fixture only.
"""

import argparse
import asyncio
from pathlib import Path

from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket import WebSocketChannel, WebSocketConfig
from nanobot.config.schema import MailKnowledgeConfig, MailOwnerBinding
from nanobot.knowledge.mail.agent_bridge import MailTurnExtension
from nanobot.knowledge.mail.contracts import AccessContext, ArchivePart, MailQuery
from nanobot.knowledge.mail.service import MailService
from tests.knowledge.mail.test_retrieval import ingest
from tests.knowledge.mail.test_store import envelope


async def run(root: Path, port: int):
    service = MailService(root / "mail", MailKnowledgeConfig(enabled=True, semantic_enabled=False,
        owner_bindings=[MailOwnerBinding(binding_id="preview-owner", principal_id="webui-owner",
            channel="websocket", scopes={"read", "manage"})]), worker_enabled=False)
    await service.start()
    MailTurnExtension(service)
    channel = WebSocketChannel(WebSocketConfig(enabled=True, host="127.0.0.1", port=port,
        token_issue_secret="fictional-preview-only", mail_owner_bootstrap=True), MessageBus(),
        static_dist_path=Path("nanobot/web/dist"), mail_service=service)
    mail = envelope(parts=[ArchivePart(part_id="2", filename="扫描合同.pdf", mime="application/pdf",
        content=None, error_code="needs_ocr")])
    mail.subject = "虚构示例：秋季入职安排"
    mail.body = "报到日期为 2026 年 9 月 28 日，地点为上海办公室。\n> 旧邮件曾建议 9 月 21 日，此安排已被替换。"
    ingest(service.store, mail)
    security = service.access.authenticate("websocket", "webui-owner", private_chat=True)
    chat_id = "fictional-review"
    key = channel._mail_access.session_key(security, chat_id)
    with service.store.connect(write=True) as db:
        db.execute("INSERT OR IGNORE INTO chat_bindings VALUES('websocket',?,?,?,?)",
                   (chat_id, security.binding_id, security.binding_id, key))
    page = await service.search(AccessContext.from_security(security, key), MailQuery(query="报到日期"))
    session = service.private_sessions.get_or_create(key)
    session.metadata.update(binding_id="preview-owner", title="虚构邮件来源演示")
    session.messages = []
    session.add_message("user", "虚构示例中的报到日期是什么？")
    session.add_message("assistant", "示例邮件写明报到日期为 2026 年 9 月 28 日。[1]\n\n这是本地界面测试数据，没有调用生成模型。",
                        citations=[item.model_dump(mode="json") for item in page.matches])
    service.private_sessions.save(session)
    print(f"Fictional-only preview: http://127.0.0.1:{port}/webui/")
    try:
        await channel.start()
    finally:
        await channel.stop()
        await service.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    asyncio.run(run(args.root, args.port))
