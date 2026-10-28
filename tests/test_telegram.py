import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from telegram.error import TelegramError
from test_engine import profile

from chess_agents.config import Settings
from chess_agents.telegram_bot import Bot


def update(number=1, user=123, chat="private", text="/prep opponent white"):
    acknowledgement = NS(message_id=9, edit_text=AsyncMock())
    message = NS(text=text, reply_text=AsyncMock(return_value=acknowledgement))
    query = NS(data="confirm:bad", answer=AsyncMock())
    return NS(
        update_id=number,
        effective_user=NS(id=user),
        effective_chat=NS(type=chat),
        effective_message=message,
        callback_query=query,
    ), acknowledgement


@pytest.mark.parametrize("user,chat", [(999, "private"), (123, "group"), (123, "supergroup")])
async def test_unauthorized_commands_callbacks_do_nothing(store, user, chat):
    service = NS(run=AsyncMock())
    bot = Bot(Settings(allowed_user=123), store, service=service, profile_builder=AsyncMock())
    value, _ = update(user=user, chat=chat)
    await bot.handle(value, None)
    await bot.callback(value, None)
    service.run.assert_not_called()
    value.effective_message.reply_text.assert_not_called()
    value.callback_query.answer.assert_not_called()
    assert store.connection.execute("SELECT COUNT(*) FROM updates").fetchone()[0] == 0


async def test_duplicate_update_delivery_failure_preserves_result(store):
    store.save_profile(profile())

    async def run(*_args, **_kwargs):
        run_id = store.start_run("prep", {}, 200)
        result = {"text": "Persisted result", "run_id": run_id}
        store.finish(run_id, result, "partial", [])
        return result

    service = NS(run=AsyncMock(side_effect=run))
    bot = Bot(Settings(allowed_user=123), store, service=service)
    value, acknowledgement = update()
    acknowledgement.edit_text.side_effect = TelegramError("delivery failed")
    await bot.handle(value, None)
    await bot.handle(value, None)
    assert service.run.await_count == 1
    assert store.last()["delivery"] == "failed"
    assert store.last()["result"]["text"] == "Persisted result"


@pytest.mark.parametrize(
    "text",
    [
        "/prep",
        "/prep opponent purple",
        "/prep https://bad white",
        "/setup 8/8/8",
        "FEN board",
        None,
    ],
)
async def test_malformed_spends_nothing(store, text):
    service, build = NS(run=AsyncMock()), AsyncMock()
    bot = Bot(Settings(allowed_user=123), store, service=service, profile_builder=build)
    value, _ = update(text=text)
    await bot.handle(value, None)
    service.run.assert_not_called()
    build.assert_not_called()


async def test_stale_callbacks_and_busy_remain_responsive(store):
    store.save_profile(profile())
    bot = Bot(Settings(allowed_user=123), store, service=NS(run=AsyncMock()))
    value, _ = update()
    await bot.callback(value, None)
    assert "No draft" in value.callback_query.answer.call_args.args[0]
    bot.busy = True
    value, _ = update(number=2)
    await asyncio.wait_for(bot.handle(value, None), 0.1)
    assert "Busy" in value.effective_message.reply_text.call_args.args[0]
    bot.service.run.assert_not_called()
