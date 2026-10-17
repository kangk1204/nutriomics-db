"""Saving a meal must not mutate the indexed omics snapshot or use stale food values."""
import sqlite3

from sqlalchemy import func, select

from nutrigenomics.db import get_session_factory
from nutrigenomics.models import CompositionMeasurement, MealRecord
from test_bridge_api import client as client, session as session  # noqa: F401


def test_recording_uses_frozen_calculation_and_separate_history(client, session, tmp_path, monkeypatch):
    storage = tmp_path / 'meal-history.sqlite3'
    with sqlite3.connect(session.get_bind().url.database) as src, sqlite3.connect(storage) as dst:
        src.backup(dst)
    url = 'sqlite:///' + storage.as_posix()
    monkeypatch.setenv('NUTRIGENOMICS_MEAL_STORAGE_DATABASE_URL', url)
    # A stale stored composition must not replace the frozen research reference.
    with get_session_factory(url)() as db:
        db.scalar(select(CompositionMeasurement)).amount_per_100g = 99
        db.commit()
    response = client.post('/api/v1/meals', json={'items': [{'food_id': 1, 'amount_g': 150}],
        'consumed_at': '2026-01-01T12:00:00+09:00', 'note': 'Synthetic storage isolation test'})
    assert response.status_code == 201
    assert response.json()['analysis']['components'][0]['amount'] == 15
    assert session.scalar(select(func.count()).select_from(MealRecord)) == 0
    with get_session_factory(url)() as db:
        assert db.scalar(select(func.count()).select_from(MealRecord)) == 1
    assert client.get('/api/v1/meals').json()['count'] == 1
    assert client.get('/api/v1/meals/totals?start=2026-01-01&end=2026-01-01').json()['meal_count'] == 1
    assert client.get('/api/v1/export/meals').json()['count'] == 1
