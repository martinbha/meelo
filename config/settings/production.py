import os

from .base import *  # noqa: F403


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} must be set in production")
    return value


SECRET_KEY = required_env("DJANGO_SECRET_KEY")
DEBUG = False
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")  # noqa: F405
if not ALLOWED_HOSTS:
    raise RuntimeError("DJANGO_ALLOWED_HOSTS must contain at least one host in production")
