import asyncio
import logging
import signal
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from chess_agents.config import Settings, username
from chess_agents.profile import build_profile, confirm, require_profile, set_line
from chess_agents.report import MAX_REPORT, notation
from chess_agents.storage import Store
from chess_agents.workflow import Preparation

HELP = (
    "Private pre-game preparation from completed rated standard rapid games only.\n"
    "/setup <my_username> — build a draft profile\n"
    "/profile — review Familiar / Learning / Exclude, then Confirm\n"
    "/prep <opponent_username> <white|black> — your color\n"
    "/last — retrieve the saved result without generation\n"
    "No live games, ongoing boards, FENs, screenshots, or move-by-move advice."
)


class Bot:
    def __init__(
        self, settings: Settings, store: Store, *, service=None, profile_builder=build_profile
    ):
        self.settings, self.store = settings, store
        self.service = service or Preparation(store, settings)
        self.profile_builder = profile_builder
        self.busy = False
        self.jobs = set()

    def authorized(self, update: Update) -> bool:
        return (
            update.effective_user is not None
            and update.effective_chat is not None
            and update.effective_user.id == self.settings.allowed_user
            and update.effective_chat.type == "private"
        )

    async def send(self, message, text: str, **kwargs):
        async with asyncio.timeout(10):
            return await message.reply_text(text, parse_mode=None, **kwargs)

    async def review(self, message) -> None:
        profile = self.store.profile("draft")
        if profile is None:
            previous = self.store.profile("confirmed")
            if previous is None:
                await self.send(message, "Build a profile with /setup first.")
                return
            profile = previous.model_copy(deep=True)
            profile.id, profile.status = uuid.uuid4().hex, "draft"
            self.store.save_profile(profile)
        lines = [f"Review profile for {profile.username}; played does not mean familiar."]
        keyboard = []
        for index, line in enumerate(profile.lines, 1):
            lines.append(
                f"{index}. {line.color} · {len(line.games)} games · {line.choice}"
                f"{' (reviewed)' if line.reviewed else ' (unreviewed)'}\n{notation(line.moves)}\n{line.summary[:150]}"
            )
            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"{index}: {label}", callback_data=f"line:{profile.id}:{line.id}:{code}"
                    )
                    for label, code in (("Familiar", "F"), ("Learning", "L"), ("Exclude", "E"))
                ]
            )
        lines.extend(profile.limitations[:3])
        keyboard.append(
            [InlineKeyboardButton("Confirm profile", callback_data=f"confirm:{profile.id}")]
        )
        text = "\n".join(lines)
        if len(text) > MAX_REPORT:
            raise ValueError("Profile review exceeds message limit.")
        await self.send(message, text, reply_markup=InlineKeyboardMarkup(keyboard))

    async def handle(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        task = asyncio.current_task()
        self.jobs.add(task)
        try:
            await self.dispatch(update, _context)
        finally:
            self.jobs.discard(task)

    async def dispatch(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        if not self.store.claim_update(update.update_id):
            return
        message = update.effective_message
        text = message.text or ""
        parts = text.split()
        command = parts[0].split("@")[0] if parts else ""
        args = parts[1:]
        try:
            if command in ("/start", "/help") and not args:
                await self.send(message, HELP)
            elif command == "/last" and not args:
                last = self.store.last()
                await self.send(
                    message, last["result"]["text"] if last else "No saved preparation."
                )
            elif command == "/profile" and not args:
                if self.busy:
                    await self.send(message, "Busy with another job; review when it finishes.")
                    return
                await self.review(message)
            elif command == "/setup" and len(args) == 1:
                player = username(args[0])
                if self.busy:
                    await self.send(message, "Busy with another job; try again when it finishes.")
                    return
                self.busy = True
                try:
                    await self.send(message, "Building a reviewable profile from completed games.")
                    await self.profile_builder(self.store, self.settings, player)
                    await self.review(message)
                finally:
                    self.busy = False
            elif command == "/prep" and len(args) == 2:
                player = username(args[0])
                if args[1] not in ("white", "black"):
                    raise ValueError("Use /prep <username> <white|black>; no board inputs.")
                require_profile(self.store)
                if self.busy:
                    await self.send(message, "Busy with another job; try again when it finishes.")
                    return
                accepted = time.monotonic()
                self.busy = True
                try:
                    acknowledgement = None
                    try:
                        acknowledgement = await self.send(
                            message, "Preparing; verified result will replace this message."
                        )
                    except (TelegramError, TimeoutError):
                        pass
                    result = await self.service.run(
                        player, args[1], update_id=update.update_id, accepted_at=accepted
                    )
                    try:
                        async with asyncio.timeout(
                            max(0.001, min(10, accepted + self.settings.seconds - time.monotonic()))
                        ):
                            if acknowledgement is not None:
                                await acknowledgement.edit_text(result["text"], parse_mode=None)
                            else:
                                await message.reply_text(result["text"], parse_mode=None)
                        self.store.delivery(
                            result["run_id"],
                            "delivered",
                            acknowledgement.message_id if acknowledgement else None,
                        )
                    except (TelegramError, TimeoutError):
                        self.store.delivery(
                            result["run_id"],
                            "failed",
                            acknowledgement.message_id if acknowledgement else None,
                        )
                finally:
                    self.busy = False
            else:
                await self.send(message, HELP)
        except ValueError as error:
            await self.send(message, str(error)[:MAX_REPORT])

    async def callback(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        if not self.store.claim_update(update.update_id):
            return
        query = update.callback_query
        if query is None or not isinstance(query.data, str):
            return
        try:
            if self.busy:
                raise ValueError("Busy; review when the current job finishes.")
            parts = query.data.split(":")
            if len(parts) == 4 and parts[0] == "line" and parts[3] in ("F", "L", "E"):
                set_line(
                    self.store,
                    parts[2],
                    {"F": "familiar", "L": "learning", "E": "exclude"}[parts[3]],
                    parts[1],
                )
                answer = "Choice saved. Review other lines or confirm."
            elif len(parts) == 2 and parts[0] == "confirm":
                confirm(self.store, parts[1])
                answer = "Profile confirmed. /prep <opponent> <white|black> is ready."
            else:
                raise ValueError("Invalid review action; open /profile again.")
            await query.answer(answer)
        except ValueError as error:
            await query.answer(str(error)[:180])


async def poll(application, bot: Bot) -> None:
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, stopped.set)
    try:
        async with application:
            await application.start()
            try:
                await application.updater.start_polling(
                    drop_pending_updates=True, allowed_updates=["message", "callback_query"]
                )
                await stopped.wait()
            finally:
                for task in list(bot.jobs):
                    task.cancel()
                if bot.jobs:
                    await asyncio.gather(*list(bot.jobs), return_exceptions=True)
                if application.updater.running:
                    await application.updater.stop()
                await application.stop()
    finally:
        for name in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(name)


def run_bot(settings: Settings) -> None:
    settings.require_bot()
    store = Store(settings.db)
    store.interrupt_running()
    bot = Bot(settings, store)
    application = Application.builder().token(settings.telegram_token).concurrent_updates(8).build()
    application.add_handler(CallbackQueryHandler(bot.callback))
    application.add_handler(MessageHandler(filters.ALL, bot.handle))
    # HTTP clients can include bot-token paths in INFO logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    async def error_handler(_update, context):
        logging.getLogger(__name__).error(
            "Telegram operation failed: %s", type(context.error).__name__
        )

    application.add_error_handler(error_handler)
    try:
        asyncio.run(poll(application, bot))
    except TelegramError:
        raise ValueError(
            "Telegram polling unavailable; check bot configuration/connectivity."
        ) from None
    finally:
        store.close()
