"""Файловая точка проверки лабораторного приложения."""

from .main import get_status


def check():
    status = get_status()
    return status.get("status") == "ok"
