import asyncio
import logging

from amqtt.broker import Broker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

CONFIG = {
    "listeners": {
        "default": {
            "type": "tcp",
            "bind": "0.0.0.0:1883",
        }
    },
    "sys_interval": 10,
    "auth": {"allow-anonymous": True},
    "topic-check": {"enabled": False},
}

async def main() -> None:
    broker = Broker(CONFIG)
    await broker.start()
    try:
        await asyncio.Event().wait()
    finally:
        await broker.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
