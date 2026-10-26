"""
main.py - VolumeSniper
Entry point. Setup logging → chạy Scanner.
"""
import asyncio
import logging
import sys
import os
from logging.handlers import RotatingFileHandler

from . import config


def setup_logging():
    os.makedirs("logs", exist_ok=True)

    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")

    handlers = [
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler("logs/volume_sniper.log", maxBytes=50*1024*1024, backupCount=5, encoding="utf-8"),
    ]
    for h in handlers:
        h.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    for h in handlers:
        root.addHandler(h)

    # Tắt noise từ thư viện ngoài
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def main():
    setup_logging()
    logger = logging.getLogger("main")
    logger.info("=" * 50)
    logger.info("  VolumeSniper - Binance Futures Volume Spike Bot")
    logger.info("=" * 50)

    # Validate config
    if config.TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.error("❌ Chưa cấu hình TELEGRAM_BOT_TOKEN trong config.py")
        sys.exit(1)
    if config.TELEGRAM_CHAT_ID == "YOUR_CHAT_ID_HERE":
        logger.error("❌ Chưa cấu hình TELEGRAM_CHAT_ID trong config.py")
        sys.exit(1)

    from .scanner import Scanner
    scanner = Scanner()

    try:
        await scanner.run()
    except asyncio.CancelledError:
        logger.info("🛑 Bot bị ngắt (Cancelled)")
    except KeyboardInterrupt:
        logger.info("🛑 Dừng bot (Ctrl+C)")
    except Exception as e:
        logger.exception(f"❌ Lỗi nghiêm trọng: {e}")
        sys.exit(1)
    finally:
        # Đảm bảo đóng tất cả kết nối aiohttp
        await scanner.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
