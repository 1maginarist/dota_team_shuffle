import asyncio
import random

from aiogram.methods import DeleteMessage, EditMessageText, SendMessage

from bot import captain_draft as cd
from bot.balancer import Player, assign_roles
from bot.db import CaptainDraft
from test_flow import GROUP_ID, Harness

# uid → (ММР, роли). 1 — организатор, 11 и 12 — лишние онлайн.
ROSTER = {
    1: (4000, "2"), 2: (6000, "1"), 3: (5500, "2"), 4: (3000, "3"), 5: (3500, "4"),
    6: (2800, "5"), 7: (4200, "1"), 8: (3900, "3"), 9: (3100, "45"), 10: (2500, "54"),
    11: (5000, "1"), 12: (4500, "2"),
}


def make_draft(**kwargs):
    base = dict(id=1, chat_id=1, message_id=1, owner_id=1, stage="picking", selected=list(range(1, 11)),
                show_all=False, captains=[1, 2], picks=[], dm_messages={})
    base.update(kwargs)
    return CaptainDraft(**base)


def test_pick_order_is_immortal_draft():
    assert cd.PICK_ORDER == (0, 1, 1, 0, 0, 1, 1, 0)
    assert cd.PICK_ORDER.count(0) == cd.PICK_ORDER.count(1) == 4


def test_turns_and_teams():
    draft = make_draft()
    assert cd.current_captain(draft) == 1 and cd.picks_in_a_row(draft) == 1
    draft.picks = [3]
    assert cd.current_captain(draft) == 2 and cd.picks_in_a_row(draft) == 2
    draft.picks = [3, 4, 5, 6]
    assert cd.current_captain(draft) == 1 and cd.picks_in_a_row(draft) == 1
    assert cd.team(draft, 0) == [1, 3, 6] and cd.team(draft, 1) == [2, 4, 5]
    assert cd.pool(draft) == [7, 8, 9, 10]
    draft.picks = [3, 4, 5, 6, 7, 8, 9, 10]
    draft.stage = "done"
    assert cd.current_captain(draft) is None
    assert len(cd.team(draft, 0)) == len(cd.team(draft, 1)) == 5


def test_coin_flip_keeps_both_captains():
    for seed in range(10):
        assert sorted(cd.flip_coin([5, 9], random.Random(seed))) == [5, 9]


def test_assign_roles_respects_priorities():
    players = [Player(i, f"p{i}", 3000, (pos,)) for i, pos in zip(range(5), (3, 1, 5, 2, 4))]
    team, penalty = assign_roles(players)
    assert penalty == 0
    assert all(slot.rank == 0 for slot in team.slots)


def test_full_draft_in_group():
    async def scenario():
        h = Harness()
        await h.start()
        try:
            await run(h)
        finally:
            await h.db.close()

    async def run(h):
        for uid, (mmr, roles) in ROSTER.items():
            await h.register(uid, mmr, roles)

        def press(uid, action, user_id=0, chat_id=GROUP_ID):
            return h.press(uid, cd.CaptainDraftCb(draft_id=1, action=action, user_id=user_id), chat_id=chat_id)

        async def draft():
            return await h.db.get_captain_draft(1)

        # Шаг 1: участники
        await h.text(1, "/draft", chat_id=GROUP_ID)
        assert "шаг 1/3" in h.last_text(GROUP_ID) and "1/10" in h.last_text(GROUP_ID)
        await press(5, "all")
        assert "Этим управляет" in h.last_alert()
        await press(1, "all")
        d = await draft()
        assert len(d.selected) == 10 and d.selected[0] == 1
        extra = next(uid for uid in ROSTER if uid not in d.selected)
        await press(1, "toggle", extra)
        assert "Уже отмечено 10" in h.last_alert()
        # Заменим одного участника на «лишнего», чтобы точно знать состав
        await press(1, "toggle", d.selected[-1])
        await press(1, "toggle", extra)
        d = await draft()
        assert extra in d.selected and len(d.selected) == 10
        await press(1, "next")

        # Шаг 2: капитаны
        assert "шаг 2/3" in h.last_text(GROUP_ID)
        await press(1, "start")
        assert h.last_alert() == "Выбери двух капитанов."
        await press(1, "top2")
        d = await draft()
        players = await h.db.get_players(d.selected)
        expected = sorted(d.selected, key=lambda uid: -players[uid].mmr)[:2]
        assert sorted(d.captains) == sorted(expected)

        before = len(h.session.calls)
        await press(1, "start")
        d = await draft()
        assert d.stage == "picking" and sorted(d.captains) == sorted(expected)
        first, second = d.captains
        dms = [c for c in h.session.calls[before:] if isinstance(c, SendMessage)]
        # Живая доска в личке у всех 10 участников, у капитанов — со своим статусом
        assert sorted(m.chat_id for m in dms) == sorted(d.selected)
        assert "Твой ход" in next(m.text for m in dms if m.chat_id == first)
        assert "соперник" in next(m.text for m in dms if m.chat_id == second)
        watchers = [m for m in dms if m.chat_id not in d.captains]
        assert len(watchers) == 8 and all("Ты участвуешь в Immortal Draft" in m.text for m in watchers)
        assert all(m.reply_markup is None for m in watchers)
        assert "пик 1/8" in h.last_text(GROUP_ID) and "Свободные игроки" in h.last_text(GROUP_ID)

        # Чужой капитан не может пикнуть не в свой ход
        pool = cd.pool(d)
        await press(second, "pick", pool[0], chat_id=second)
        assert "Сейчас выбирает" in h.last_alert()

        # Пик 1 — первый капитан из лички; ход переходит ко второму → новое сообщение ему
        before = len(h.session.calls)
        await press(first, "pick", pool[0], chat_id=first)
        new = h.session.calls[before:]
        assert any(isinstance(c, SendMessage) and c.chat_id == second and "Твой ход" in c.text for c in new)
        assert any(isinstance(c, DeleteMessage) and c.chat_id == second for c in new)

        # Участники видят пик у себя в личке (правка, без нового уведомления)
        watcher = next(uid for uid in d.selected if uid not in d.captains and uid != pool[0])
        edits = [c for c in new if isinstance(c, EditMessageText) and c.chat_id == watcher]
        assert edits and "пик 2/8" in edits[-1].text

        # Уже забранного игрока взять нельзя
        await press(second, "pick", pool[0], chat_id=second)
        assert "уже забрали" in h.last_alert()

        # Пик 2 (второй капитан), потом отмена пика организатором
        await press(second, "pick", pool[1], chat_id=second)
        assert len((await draft()).picks) == 2
        await press(1, "undo")
        assert (await draft()).picks == [pool[0]]

        # Доводим драфт до конца; один пик делает организатор за капитана
        while (d := await draft()).stage == "picking":
            captain = cd.current_captain(d)
            who = 1 if len(d.picks) == 5 else captain
            await press(who, "pick", cd.pool(d)[0], chat_id=GROUP_ID)
        d = await draft()
        assert d.stage == "done" and len(d.picks) == 8
        assert len(cd.team(d, 0)) == len(cd.team(d, 1)) == 5
        final = h.last_text(GROUP_ID)
        assert "составы готовы" in final and "Разница" in final
        for team_index in (0, 1):
            lineup = cd.final_lineup(d, await h.db.get_players(d.selected), h.config.weights)
            assert sorted(s.position for s in lineup.teams[team_index].slots if s.player) == [1, 2, 3, 4, 5]

        # Утверждаем — всем 10 в личку команда и позиция
        before = len(h.session.calls)
        await press(1, "ok")
        assert "Состав утверждён" in h.last_text(GROUP_ID)
        after = h.session.calls[before:]
        personal = [c for c in after if isinstance(c, SendMessage) and "Ты за" in c.text]
        assert sorted(m.chat_id for m in personal) == sorted(d.selected)
        assert all("составы готовы" in m.text and "Состав утверждён" in m.text for m in personal)
        # Живые доски убраны, чтобы у каждого осталось одно итоговое сообщение
        assert sorted(c.chat_id for c in after if isinstance(c, DeleteMessage)) == sorted(d.selected)
        await press(1, "cancel")
        assert "уже закончен" in h.last_alert()

    asyncio.run(scenario())


def test_cancel_and_organizer_in_private_chat():
    """Драфт в личке организатора: если он капитан, лишних сообщений ему не шлём."""
    async def scenario():
        h = Harness()
        await h.start()
        try:
            for uid, (mmr, roles) in ROSTER.items():
                await h.register(uid, mmr, roles)
            await h.text(1, "/draft")
            for action in ("all", "next"):
                await h.press(1, cd.CaptainDraftCb(draft_id=1, action=action))
            for uid in (1, 2):
                await h.press(1, cd.CaptainDraftCb(draft_id=1, action="cap", user_id=uid))
            before = len(h.session.calls)
            await h.press(1, cd.CaptainDraftCb(draft_id=1, action="start"))
            d = await h.db.get_captain_draft(1)
            dms = [c for c in h.session.calls[before:] if isinstance(c, SendMessage)]
            # Всем участникам, кроме организатора: у него доска и так в личке
            assert sorted(m.chat_id for m in dms) == sorted(uid for uid in d.selected if uid != 1)
            await h.press(1, cd.CaptainDraftCb(draft_id=1, action="cancel"))
            assert (await h.db.get_captain_draft(1)).stage == "cancelled"
            assert "Драфт отменён" in h.last_text(1)
            for uid in d.selected:
                if uid == 1:
                    continue
                edits = [c for c in h.session.calls if isinstance(c, EditMessageText) and c.chat_id == uid]
                assert edits and "Драфт отменён" in edits[-1].text
            assert d.captains and sorted(d.captains) == [1, 2]
        finally:
            await h.db.close()

    asyncio.run(scenario())
