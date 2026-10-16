import os

from app import app
from scheduler import start_scheduler


if os.getenv("START_SCHEDULER", "true").casefold() == "true":
    start_scheduler(app)

