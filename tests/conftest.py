import pytest

from bot.handlers import captains, mvp, organizer, profile


@pytest.fixture(autouse=True)
def detach_routers():
    """aiogram не даёт подключить роутер ко второму диспетчеру — отцепляем после теста."""
    yield
    for router in (profile.router, organizer.router, captains.router, mvp.router, profile.fallback):
        router._parent_router = None
