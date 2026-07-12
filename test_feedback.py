"""Contracts for announcement feedback buttons: per-feature 👍/👎 + free-text ideas."""
from types import SimpleNamespace

import bot
from test_feature_matrix import run, use_database


def _ctx():
    return SimpleNamespace(user_data={})


class FakeQuery:
    def __init__(self, data, user_id=101):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.answers = []
        self.replies = []
        self.message = SimpleNamespace(reply_text=self._reply)

    async def answer(self, text=None, **kw):
        self.answers.append(text)

    async def _reply(self, text, **kw):
        self.replies.append(text)


def _cb_update(data, user_id=101):
    q = FakeQuery(data, user_id)
    upd = SimpleNamespace(
        callback_query=q, message=None,
        effective_user=SimpleNamespace(id=user_id),
    )
    return upd, q


def _text_update(text, user_id=101):
    replies = []

    async def _reply(t, **kw):
        replies.append(t)

    msg = SimpleNamespace(text=text, reply_text=_reply)
    upd = SimpleNamespace(
        callback_query=None, message=msg,
        effective_user=SimpleNamespace(id=user_id),
    )
    return upd, replies


def test_reactions_recorded_and_upsert(monkeypatch, tmp_path):
    use_database(monkeypatch, tmp_path)
    ctx = _ctx()
    for data in ('fb:import:up', 'fb:declaration:down'):
        upd, q = _cb_update(data, 101)
        run(bot.handle_callback(upd, ctx))
        assert q.answers  # a confirmation toast was shown
    # Re-tapping import replaces up -> down (one current vote per feature).
    upd, q = _cb_update('fb:import:down', 101)
    run(bot.handle_callback(upd, ctx))

    summary = run(bot.db.get_feedback_summary())
    assert summary['tally']['import'] == {'up': 0, 'down': 1}
    assert summary['tally']['declaration'] == {'up': 0, 'down': 1}
    assert len(summary['reactions']) == 2  # upsert => 2 rows, not 3


def test_comment_flow_captures_free_text(monkeypatch, tmp_path):
    use_database(monkeypatch, tmp_path)
    ctx = _ctx()
    upd, q = _cb_update('fb:comment', 202)
    run(bot.handle_callback(upd, ctx))
    assert ctx.user_data.get('waiting_for') == 'feedback_comment'
    assert q.replies  # prompted for the comment

    upd, replies = _text_update('Хочу імпорт з Приватбанку!', 202)
    run(bot.handle_text_transaction(upd, ctx))
    assert ctx.user_data.get('waiting_for') is None
    assert replies  # user was thanked

    summary = run(bot.db.get_feedback_summary())
    assert any('Приватбанку' in c['comment'] for c in summary['comments'])
    assert summary['comments'][0]['user_id'] == '202'


def test_unknown_fb_callback_is_safe(monkeypatch, tmp_path):
    use_database(monkeypatch, tmp_path)
    ctx = _ctx()
    upd, q = _cb_update('fb:garbage', 303)
    run(bot.handle_callback(upd, ctx))  # must not raise
    assert q.answers  # still acknowledged the tap
