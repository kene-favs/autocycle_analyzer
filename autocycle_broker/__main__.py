"""
Entry point so the broker can be started with:
  python -m autocycle_broker

from the autocycle-analyzer/ directory.
"""
import uvicorn
from . import config

uvicorn.run(
    'autocycle_broker.broker:app',
    host    = '0.0.0.0',
    port    = config.BROKER_PORT,
    reload  = False,
    workers = 1,
)
