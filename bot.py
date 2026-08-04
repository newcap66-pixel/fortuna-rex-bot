import asyncio
import json
import os
import re
import logging
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import quote

import pytz
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, FSInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
CHANNEL_ID = os.getenv("CHANNEL_ID", "")          # ID канала/группы для рассылки, напр. -1001234567890
DAILY_HOUR = int(os.getenv("DAILY_HOUR", "12"))   # Час рассылки (в TIMEZONE)
DAILY_MINUTE = int(os.getenv("DAILY_MINUTE", "0"))
TIMEZONE = os.getenv("TIMEZONE", "America/Sao_Paulo")

# ── Оффер A360 (единый источник правды для всех ссылок) ───────────────────────
# Базовый клик-линк maglead → A360 #2093. Метки в формате Scaleo:
#   click_id → aff_click_id, зона → sub_id1  (НЕ sub1/sub2!)
A360_BASE = os.getenv(
    "A360_BASE",
    "https://maglead.buzz/click?o=2093&a=13335&link_id=8717"
)
# Пакет приветствия A360 (подтверждён на экране регистрации)
BONUS_TEXT_SHORT = "até 8250 BRL + 150 FS"

BASE_DIR = Path(__file__).parent
SLOTS_FILE = BASE_DIR / "slots.json"
STATS_FILE = BASE_DIR / "stats.json"
DAILY_FILE = BASE_DIR / "daily.json"    # хранит текущий слот дня
ZONES_FILE = BASE_DIR / "zones.json"    # хранит зону по каждому Telegram user_id

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── Трекинг: чистка меток и сборка ссылки оффера ──────────────────────────────

def clean(value: str) -> str:
    """
    Фикс доллара (как в ленде колеса):
    - срезает ведущий '$' у значения ($1108... → 1108...)
    - отсеивает нераскрытые макросы вида ${...} → пустая строка
    Возвращает безопасную для URL строку.
    """
    if not value:
        return ""
    v = str(value).strip()
    # нераскрытый макрос целиком, напр. "${ZONE_ID}" или "${CLICK_ID}"
    if v.startswith("${") and v.endswith("}"):
        return ""
    # ведущий '$' (Propeller иногда прилетает как "$1108...")
    if v.startswith("$"):
        v = v[1:]
    # выкидываем любые оставшиеся ${...} внутри строки
    v = re.sub(r"\$\{[^}]*\}", "", v)
    return v.strip()


def build_offer_url(base_url: str, zone: str = "", click_id: str = "") -> str:
    """
    Достраивает клик-линк A360 метками:
      sub_id1      = зона (из deep-link ?start=ZONE_ID)
      aff_click_id = click_id (используем telegram user_id как суррогат)
    Обе метки прогоняются через clean(). Пустые не добавляются.
    Если у слота уже свой полный URL — просто дополняем недостающими метками.
    """
    zone = clean(zone)
    click_id = clean(click_id)

    params = []
    if "aff_click_id=" not in base_url and click_id:
        params.append(f"aff_click_id={quote(click_id)}")
    if "sub_id1=" not in base_url and zone:
        params.append(f"sub_id1={quote(zone)}")

    if not params:
        return base_url
    sep = "&" if "?" in base_url else "?"
    return base_url + sep + "&".join(params)

# ── Данные: слоты / статистика / слот дня / зоны ──────────────────────────────

def load_slots() -> list[dict]:
    if SLOTS_FILE.exists():
        return json.loads(SLOTS_FILE.read_text(encoding="utf-8"))
    return []

def save_slots(data: list[dict]):
    SLOTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def load_stats() -> dict:
    if STATS_FILE.exists():
        return json.loads(STATS_FILE.read_text(encoding="utf-8"))
    return {}

def save_stats(data: dict):
    STATS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def bump_stat(slot_id: str):
    stats = load_stats()
    stats[slot_id] = stats.get(slot_id, 0) + 1
    save_stats(stats)

def load_daily() -> dict:
    if DAILY_FILE.exists():
        return json.loads(DAILY_FILE.read_text(encoding="utf-8"))
    return {}

def save_daily(data: dict):
    DAILY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def load_zones() -> dict:
    if ZONES_FILE.exists():
        return json.loads(ZONES_FILE.read_text(encoding="utf-8"))
    return {}

def save_zones(data: dict):
    ZONES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def set_user_zone(user_id: int, zone: str):
    """Запоминаем зону, с которой пришёл юзер (для подстановки в sub_id1)."""
    zone = clean(zone)
    if not zone:
        return
    zones = load_zones()
    zones[str(user_id)] = zone
    save_zones(zones)

def get_user_zone(user_id: int) -> str:
    return load_zones().get(str(user_id), "")

def get_daily_slot() -> dict | None:
    daily = load_daily()
    slot_id = daily.get("slot_id")
    if not slot_id:
        slots = load_slots()
        popular = [s for s in slots if "Populares" in s.get("category", "")]
        return popular[0] if popular else (slots[0] if slots else None)
    slots = load_slots()
    return next((s for s in slots if s["id"] == slot_id), None)

def resolve_photo(image_value: str):
    if not image_value:
        return None
    local_path = (BASE_DIR / image_value).resolve()
    if local_path.exists() and local_path.is_file():
        return FSInputFile(local_path)
    return image_value

def offer_url_for(slot: dict, user_id: int) -> str:
    """Готовая ссылка оффера для конкретного юзера: база слота + зона юзера + user_id."""
    base = slot.get("url") or A360_BASE
    return build_offer_url(base, zone=get_user_zone(user_id), click_id=str(user_id))

# ── FSM ───────────────────────────────────────────────────────────────────────

class AddSlot(StatesGroup):
    name     = State()
    category = State()
    image    = State()
    url      = State()

class SetImage(StatesGroup):
    waiting  = State()

class SearchSlot(StatesGroup):
    waiting  = State()

# ── Клавиатуры ────────────────────────────────────────────────────────────────

PAGE_SIZE = 5
CATEGORY_ORDER = ["🔥 Mais Populares", "⭐ Clássicos", "🆕 Novidades"]

def ordered_categories(cats: set[str]) -> list[str]:
    known = [c for c in CATEGORY_ORDER if c in cats]
    unknown = sorted(c for c in cats if c not in CATEGORY_ORDER)
    return known + unknown

def categories_kb() -> InlineKeyboardMarkup:
    slots = load_slots()
    cats = ordered_categories({s["category"] for s in slots})
    buttons = [[InlineKeyboardButton(text=c, callback_data=f"cat:{c}:0")] for c in cats]
    buttons.append([InlineKeyboardButton(text="🔥 Slot do Dia", callback_data="daily")])
    buttons.append([InlineKeyboardButton(text=f"🎁 Bônus {BONUS_TEXT_SHORT}", callback_data="bonus")])
    buttons.append([InlineKeyboardButton(text="🔍 Buscar jogo", callback_data="search")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def slots_kb(category: str, page: int) -> InlineKeyboardMarkup:
    slots = [s for s in load_slots() if s["category"] == category]
    total = len(slots)
    chunk = slots[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    buttons = []
    for s in chunk:
        buttons.append([InlineKeyboardButton(
            text=f"🎰 {s['name']}",
            callback_data=f"slot:{s['id']}"
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"cat:{category}:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{max(1, -(-total // PAGE_SIZE))}", callback_data="noop"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"cat:{category}:{page + 1}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="🏠 Menu principal", callback_data="menu_new")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def slot_kb(slot: dict, user_id: int) -> InlineKeyboardMarkup:
    url = offer_url_for(slot, user_id)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🎮 Jogar {slot['name']} agora!", url=url)],
        [InlineKeyboardButton(text=f"🎁 Bônus {BONUS_TEXT_SHORT}", url=url)],
        [InlineKeyboardButton(text="◀️ Voltar", callback_data=f"cat:{slot['category']}:0")],
        [InlineKeyboardButton(text="🏠 Menu principal", callback_data="menu_new")],
    ])

def daily_slot_kb(slot: dict, user_id: int) -> InlineKeyboardMarkup:
    url = offer_url_for(slot, user_id)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Jogar agora!", url=url)],
        [InlineKeyboardButton(text=f"🎁 Resgatar bônus ({BONUS_TEXT_SHORT})", url=url)],
        [InlineKeyboardButton(text="🏠 Menu principal", callback_data="menu_new")],
    ])

# ── Текст и отправка слота дня ────────────────────────────────────────────────

def daily_caption(slot: dict) -> str:
    tz = pytz.timezone(TIMEZONE)
    hoje = datetime.now(tz).strftime("%d/%m/%Y")
    return (
        f"🔥 <b>SLOT DO DIA — {hoje}</b>\n\n"
        f"🎰 <b>{slot['name']}</b>\n"
        f"📂 {slot['category']}\n\n"
        f"{slot.get('description', '')}\n\n"
        f"⚡ Jogue agora e aproveite os maiores multiplicadores do dia!"
    )

async def send_daily_slot(bot: Bot, chat_id: str | int):
    slot = get_daily_slot()
    if not slot:
        log.warning("Slot do dia não encontrado")
        return
    caption = daily_caption(slot)
    photo = resolve_photo(slot.get("image"))
    # В канал зону юзера не знаем → ссылка идёт без sub_id1 (общий трафик канала)
    kb = daily_slot_kb(slot, user_id=0)
    try:
        if photo:
            await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption,
                                 reply_markup=kb, parse_mode="HTML")
        else:
            await bot.send_message(chat_id=chat_id, text=caption,
                                   reply_markup=kb, parse_mode="HTML")
        log.info(f"Slot do dia enviado para {chat_id}: {slot['name']}")
    except Exception as e:
        log.error(f"Erro ao enviar slot do dia: {e}")

# ── Авторассылка ──────────────────────────────────────────────────────────────

async def daily_scheduler(bot: Bot):
    tz = pytz.timezone(TIMEZONE)
    log.info(f"Scheduler iniciado — envio diário às {DAILY_HOUR:02d}:{DAILY_MINUTE:02d} {TIMEZONE}")
    while True:
        now = datetime.now(tz)
        target = now.replace(hour=DAILY_HOUR, minute=DAILY_MINUTE, second=0, microsecond=0)
        if now >= target:
            # уже прошло сегодня — ждём до завтра (timedelta, чтобы не падать 28/30/31 числа)
            target = target + timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        log.info(f"Próximo envio em {wait_seconds/3600:.1f} horas")
        await asyncio.sleep(wait_seconds)

        if CHANNEL_ID:
            await send_daily_slot(bot, CHANNEL_ID)
        else:
            log.warning("CHANNEL_ID não configurado — slot do dia não enviado")

# ── Хэндлеры ─────────────────────────────────────────────────────────────────

dp = Dispatcher(storage=MemoryStorage())

@dp.message(Command("start"))
async def cmd_start(msg: Message, command: CommandObject):
    # Ловим deep-link: t.me/FortunaRex_bot?start=ZONE_ID → запоминаем зону
    if command.args:
        set_user_zone(msg.from_user.id, command.args)
        log.info(f"Zona capturada p/ user {msg.from_user.id}: {clean(command.args)}")

    text = (
        "🎰 <b>Bem-vindo ao Fortuna Rex!</b>\n\n"
        "🔥 Os melhores slots com os maiores multiplicadores\n"
        f"💰 Bônus de boas-vindas: <b>{BONUS_TEXT_SHORT}</b>\n\n"
        "👇 Escolha uma categoria:"
    )
    banner = resolve_photo("images/banner_start.jpg")
    if banner:
        await msg.answer_photo(photo=banner, caption=text, reply_markup=categories_kb(), parse_mode="HTML")
    else:
        await msg.answer(text, reply_markup=categories_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "menu_new")
async def cb_menu_new(cb: CallbackQuery):
    await cb.message.answer(
        "🎰 <b>Fortuna Rex — Catálogo</b>\n\n👇 Escolha uma categoria:",
        reply_markup=categories_kb(),
        parse_mode="HTML"
    )
    await cb.answer()

@dp.callback_query(F.data == "daily")
async def cb_daily(cb: CallbackQuery):
    slot = get_daily_slot()
    if not slot:
        await cb.answer("Slot do dia não disponível", show_alert=True)
        return
    caption = daily_caption(slot)
    photo = resolve_photo(slot.get("image"))
    kb = daily_slot_kb(slot, cb.from_user.id)
    if photo:
        await cb.message.answer_photo(photo=photo, caption=caption, reply_markup=kb, parse_mode="HTML")
    else:
        await cb.message.answer(caption, reply_markup=kb, parse_mode="HTML")
    await cb.answer()

@dp.callback_query(F.data == "bonus")
async def cb_bonus(cb: CallbackQuery):
    url = build_offer_url(A360_BASE, zone=get_user_zone(cb.from_user.id), click_id=str(cb.from_user.id))
    text = (
        f"🎁 <b>Pacote de Boas-vindas — {BONUS_TEXT_SHORT}</b>\n\n"
        "✅ Bônus no primeiro depósito\n"
        "✅ 150 rodadas grátis (FS)\n"
        "✅ Cassino + A360 Exclusives\n\n"
        "💰 Cadastro rápido em 1 clique — país Brasil, moeda BRL\n"
        "⚡ Saque via Pix"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Resgatar bônus agora", url=url)],
        [InlineKeyboardButton(text="🏠 Menu principal", callback_data="menu_new")],
    ])
    banner = resolve_photo("images/banner_bonus.jpg")
    if banner:
        await cb.message.answer_photo(photo=banner, caption=text, reply_markup=kb, parse_mode="HTML")
    else:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()

@dp.callback_query(F.data.startswith("cat:"))
async def cb_category(cb: CallbackQuery):
    _, category, page_str = cb.data.split(":", 2)
    page = int(page_str)
    slots = [s for s in load_slots() if s["category"] == category]
    if not slots:
        await cb.answer("Nenhum slot nesta categoria", show_alert=True)
        return
    text = f"📂 <b>{category}</b>\n🎮 {len(slots)} jogos disponíveis\n\n👇 Escolha um jogo:"
    await cb.message.answer(text, reply_markup=slots_kb(category, page), parse_mode="HTML")
    await cb.answer()

@dp.callback_query(F.data.startswith("slot:"))
async def cb_slot(cb: CallbackQuery):
    slot_id = cb.data.split(":", 1)[1]
    slots = load_slots()
    slot = next((s for s in slots if s["id"] == slot_id), None)
    if not slot:
        await cb.answer("Slot não encontrado", show_alert=True)
        return
    bump_stat(slot_id)
    caption = (
        f"🎰 <b>{slot['name']}</b>\n"
        f"📂 {slot['category']}\n\n"
        f"{slot.get('description', '')}\n\n"
        f"🎁 Bônus de boas-vindas: <b>{BONUS_TEXT_SHORT}</b>"
    )
    kb = slot_kb(slot, cb.from_user.id)
    photo = resolve_photo(slot.get("image"))
    if not photo:
        await cb.message.answer(caption, reply_markup=kb, parse_mode="HTML")
    else:
        try:
            await cb.message.answer_photo(photo=photo, caption=caption, reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            log.warning(f"Falha ao enviar imagem {slot_id}: {e}")
            await cb.message.answer(f"⚠️ Imagem não disponível\n\n{caption}", reply_markup=kb, parse_mode="HTML")
    await cb.answer()

@dp.callback_query(F.data == "search")
async def cb_search_prompt(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SearchSlot.waiting)
    await cb.message.answer("🔍 Digite o nome do slot que procura:")
    await cb.answer()

@dp.message(StateFilter(SearchSlot.waiting))
async def handle_search(msg: Message, state: FSMContext):
    query = (msg.text or "").lower()
    results = [s for s in load_slots() if query in s["name"].lower()]
    await state.clear()
    if not results:
        await msg.answer(
            "❌ Nenhum resultado encontrado.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Menu principal", callback_data="menu_new")]
            ])
        )
        return
    buttons = [[InlineKeyboardButton(text=f"🎰 {s['name']}", callback_data=f"slot:{s['id']}")] for s in results[:10]]
    buttons.append([InlineKeyboardButton(text="🏠 Menu principal", callback_data="menu_new")])
    await msg.answer(f"🔍 Encontrado: <b>{len(results)}</b> jogo(s)",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@dp.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()

# ── Админ ─────────────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

@dp.message(Command("setdaily"))
async def cmd_setdaily(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    slots = load_slots()
    if not slots:
        await msg.answer("Nenhum slot cadastrado.")
        return
    daily = load_daily()
    current_id = daily.get("slot_id", "")
    buttons = []
    for s in slots:
        mark = "✅ " if s["id"] == current_id else ""
        buttons.append([InlineKeyboardButton(text=f"{mark}{s['name']}", callback_data=f"daily_set:{s['id']}")])
    await msg.answer("🔥 <b>Escolha o Slot do Dia:</b>\n\n✅ = atual",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@dp.callback_query(F.data.startswith("daily_set:"))
async def cb_daily_set(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Sem acesso", show_alert=True)
        return
    slot_id = cb.data.split(":", 1)[1]
    slots = load_slots()
    slot = next((s for s in slots if s["id"] == slot_id), None)
    if not slot:
        await cb.answer("Slot não encontrado", show_alert=True)
        return
    save_daily({"slot_id": slot_id, "updated": datetime.now().isoformat()})
    await cb.message.edit_text(f"✅ Slot do dia definido: <b>{slot['name']}</b>", parse_mode="HTML")
    await cb.answer()

@dp.message(Command("senddaily"))
async def cmd_senddaily(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    target = CHANNEL_ID if CHANNEL_ID else msg.chat.id
    await send_daily_slot(msg.bot, target)
    if CHANNEL_ID:
        await msg.answer("✅ Slot do dia enviado para o canal!")
    else:
        await msg.answer("⚠️ CHANNEL_ID não configurado — enviado aqui como teste")

@dp.message(Command("stats"))
async def cmd_stats(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    stats = load_stats()
    slots = load_slots()
    if not stats:
        await msg.answer("📊 Ainda sem cliques.")
        return
    lines = ["📊 <b>Estatísticas de cliques:</b>\n"]
    sorted_slots = sorted(slots, key=lambda s: stats.get(s["id"], 0), reverse=True)
    for slot in sorted_slots:
        count = stats.get(slot["id"], 0)
        bar = "▓" * min(count, 10) + "░" * (10 - min(count, 10))
        lines.append(f"{bar} <b>{slot['name']}</b>: {count} cliques")
    await msg.answer("\n".join(lines), parse_mode="HTML")

@dp.message(Command("zones"))
async def cmd_zones(msg: Message):
    """Сводка по зонам: сколько юзеров пришло с каждой зоны Propeller."""
    if not is_admin(msg.from_user.id):
        return
    zones = load_zones()
    if not zones:
        await msg.answer("📊 Ainda sem zonas registradas.")
        return
    counts: dict[str, int] = {}
    for z in zones.values():
        counts[z] = counts.get(z, 0) + 1
    lines = ["📊 <b>Usuários por zona (sub_id1):</b>\n"]
    for zone, cnt in sorted(counts.items(), key=lambda kv: kv[1], reverse=True):
        lines.append(f"• <code>{zone}</code>: {cnt} usuário(s)")
    lines.append(f"\n👥 Total: {len(zones)} usuário(s)")
    await msg.answer("\n".join(lines), parse_mode="HTML")

@dp.message(Command("addslot"))
async def cmd_addslot(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await state.set_state(AddSlot.name)
    await msg.answer("➕ <b>Adicionar slot</b>\n\nPasso 1/4 — Digite o <b>nome</b> do slot:", parse_mode="HTML")

@dp.message(AddSlot.name)
async def add_name(msg: Message, state: FSMContext):
    await state.update_data(name=msg.text)
    await state.set_state(AddSlot.category)
    slots = load_slots()
    cats = ordered_categories({s["category"] for s in slots})
    hint = "\n\nCategorias existentes:\n" + "\n".join(f"• {c}" for c in cats) if cats else ""
    await msg.answer(f"Passo 2/4 — Digite a <b>categoria</b>:{hint}", parse_mode="HTML")

@dp.message(AddSlot.category)
async def add_category(msg: Message, state: FSMContext):
    await state.update_data(category=msg.text)
    await state.set_state(AddSlot.image)
    await msg.answer("Passo 3/4 — Envie a <b>foto</b> do slot:", parse_mode="HTML")

@dp.message(AddSlot.image)
async def add_image(msg: Message, state: FSMContext):
    if msg.photo:
        image = msg.photo[-1].file_id
        await state.update_data(image=image)
        await state.set_state(AddSlot.url)
        await msg.answer(
            "✅ Foto salva!\n\nPasso 4/4 — Cole o <b>link de afiliado</b> "
            "(ou envie <code>-</code> para usar o link padrão A360):",
            parse_mode="HTML"
        )
    else:
        await msg.answer("❌ Envie uma <b>foto</b>:", parse_mode="HTML")

@dp.message(AddSlot.url)
async def add_url(msg: Message, state: FSMContext):
    text = (msg.text or "").strip()
    if text == "-":
        url = A360_BASE
    elif text.startswith("http"):
        url = text
    else:
        await msg.answer("❌ O link deve começar com https:// (ou envie <code>-</code> para o padrão A360)",
                         parse_mode="HTML")
        return
    data = await state.get_data()
    await state.clear()
    slots = load_slots()
    slot_id = f"slot_{len(slots) + 1}_{data['name'].lower().replace(' ', '_')[:20]}"
    slots.append({
        "id": slot_id, "name": data["name"], "category": data["category"],
        "image": data["image"], "url": url, "description": ""
    })
    save_slots(slots)
    await msg.answer(f"✅ Slot <b>{data['name']}</b> adicionado!", parse_mode="HTML")

@dp.message(Command("setimage"))
async def cmd_setimage(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    slots = load_slots()
    buttons = [[InlineKeyboardButton(text=s["name"], callback_data=f"setimg:{s['id']}")] for s in slots]
    await msg.answer("🖼 Escolha o slot para atualizar a imagem:",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("setimg:"))
async def cb_setimg(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("Sem acesso", show_alert=True)
        return
    slot_id = cb.data.split(":", 1)[1]
    await state.set_state(SetImage.waiting)
    await state.update_data(slot_id=slot_id)
    await cb.message.answer("📸 Envie a nova foto:")
    await cb.answer()

@dp.message(SetImage.waiting)
async def set_image_photo(msg: Message, state: FSMContext):
    if not msg.photo:
        await msg.answer("❌ Envie uma foto:")
        return
    data = await state.get_data()
    file_id = msg.photo[-1].file_id
    slots = load_slots()
    for s in slots:
        if s["id"] == data["slot_id"]:
            s["image"] = file_id
            break
    save_slots(slots)
    await state.clear()
    await msg.answer("✅ Imagem atualizada!")

@dp.message(Command("removeslot"))
async def cmd_removeslot(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    slots = load_slots()
    buttons = [[InlineKeyboardButton(text=f"❌ {s['name']}", callback_data=f"remove:{s['id']}")] for s in slots]
    await msg.answer("Escolha o slot para remover:",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("remove:"))
async def cb_remove(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Sem acesso", show_alert=True)
        return
    slot_id = cb.data.split(":", 1)[1]
    slots = [s for s in load_slots() if s["id"] != slot_id]
    save_slots(slots)
    await cb.message.edit_text("✅ Slot removido.")
    await cb.answer()

@dp.message(Command("listslots"))
async def cmd_listslots(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    slots = load_slots()
    lines = [f"• [{s['category']}] <b>{s['name']}</b>" for s in slots]
    await msg.answer("\n".join(lines) or "Vazio", parse_mode="HTML")

@dp.message(Command("help"))
async def cmd_help(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    await msg.answer(
        "🛠 <b>Comandos de admin:</b>\n\n"
        "/setdaily — выбрать слот дня\n"
        "/senddaily — отправить слот дня сейчас (тест)\n"
        "/stats — статистика кликов\n"
        "/zones — статистика по зонам (sub_id1)\n"
        "/addslot — добавить слот\n"
        "/removeslot — удалить слот\n"
        "/setimage — обновить картинку слота\n"
        "/listslots — список всех слотов",
        parse_mode="HTML"
    )

# ── /invite — готовые посты для продвижения ──────────────────────────────────

CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/+72lKgWkkgFc3MDc6")
BOT_LINK = os.getenv("BOT_LINK", "https://t.me/FortunaRex_bot")

@dp.message(Command("invite"))
async def cmd_invite(msg: Message):
    slot = get_daily_slot()
    slot_name = slot["name"] if slot else "Gates of Olympus"

    posts = [
        f"🔥 <b>Пост 1 — Интрига</b>\n\n"
        f"<code>🎰 {slot_name} pagou x500 agora!\n\n"
        f"Canal gratuito com os slots mais quentes do dia 👇\n"
        f"{CHANNEL_LINK}</code>",

        f"💰 <b>Пост 2 — Бонус</b>\n\n"
        f"<code>💰 Bônus de boas-vindas {BONUS_TEXT_SHORT}!\n\n"
        f"✅ Bônus no 1º depósito\n"
        f"✅ 150 rodadas grátis\n"
        f"✅ Saque via Pix\n\n"
        f"Slots do dia no canal 👇\n"
        f"{CHANNEL_LINK}</code>",

        f"🎮 <b>Пост 3 — Слот дня</b>\n\n"
        f"<code>🔥 SLOT DO DIA: {slot_name}\n\n"
        f"📊 RTP alto | Multiplicadores explosivos\n"
        f"⚡ Jogue agora e ganhe grande!\n\n"
        f"🎁 + Bônus exclusivo para novos jogadores\n"
        f"{CHANNEL_LINK}</code>",

        f"⚡ <b>Пост 4 — Срочность</b>\n\n"
        f"<code>⚠️ Hoje é o dia certo para jogar {slot_name}!\n\n"
        f"🔴 Alta volatilidade = grandes prêmios\n"
        f"💎 Canal VIP gratuito com dicas diárias\n\n"
        f"Entrar agora 👇\n"
        f"{CHANNEL_LINK}</code>",
    ]

    await msg.answer(
        "📢 <b>Готовые посты для продвижения</b>\n\n"
        "Скопируйте и разместите в Telegram группах.\n"
        "Текст внутри <code>серых блоков</code> — готов к публикации:",
        parse_mode="HTML"
    )
    for post in posts:
        await msg.answer(post, parse_mode="HTML")

    await msg.answer(
        "💡 <b>Советы по размещению:</b>\n\n"
        "• Лучшее время: 19:00–22:00 по Бразилии\n"
        "• Не спамьте в одну группу чаще раза в день\n"
        "• Меняйте посты — не всегда один и тот же\n"
        "• Группы: ищите 'slots brasil', 'cassino', 'gates of olympus'",
        parse_mode="HTML"
    )

# ── Авто-приветствие новых подписчиков канала ─────────────────────────────────

@dp.chat_member()
async def on_new_member(event, bot: Bot):
    """Срабатывает когда кто-то вступает в канал/группу"""
    if CHANNEL_ID and str(event.chat.id) != str(CHANNEL_ID):
        return

    new_status = event.new_chat_member.status
    old_status = event.old_chat_member.status
    user = event.new_chat_member.user

    if user.is_bot:
        return

    if old_status in ("left", "kicked") and new_status == "member":
        name = user.first_name or "Amigo"
        # Зона юзера в канале неизвестна → ссылка без sub_id1, click_id = user_id
        url = build_offer_url(A360_BASE, zone="", click_id=str(user.id))
        text = (
            f"🎰 Bem-vindo, <b>{name}</b>!\n\n"
            f"Aqui você encontra todo dia:\n"
            f"🔥 Slot do dia com os maiores multiplicadores\n"
            f"💰 Bônus de boas-vindas {BONUS_TEXT_SHORT}\n"
            f"📊 Dicas de RTP e volatilidade\n\n"
            f"👇 Acesse o catálogo completo:"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎮 Ver todos os slots", url=BOT_LINK)],
            [InlineKeyboardButton(text=f"🎁 Pegar bônus ({BONUS_TEXT_SHORT})", url=url)],
        ])
        try:
            await bot.send_message(chat_id=event.chat.id, text=text, reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            log.warning(f"Não foi possível enviar boas-vindas: {e}")

# ── Запуск ────────────────────────────────────────────────────────────────────

async def main():
    bot = Bot(token=BOT_TOKEN)
    asyncio.create_task(daily_scheduler(bot))
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "chat_member"])

if __name__ == "__main__":
    asyncio.run(main())
