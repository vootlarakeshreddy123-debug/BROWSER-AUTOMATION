"""Smoke tests for the dataset read APIs."""
from fastapi.testclient import TestClient
from main import app


def test_dataset_stats_and_listing():
    with TestClient(app) as client:
        stats = client.get('/api/dataset/stats')
        assert stats.status_code == 200
        assert stats.json()['total_tasks'] >= 200
        listing = client.get('/api/dataset/tasks', params={'category': 'recovery', 'limit': 3})
        assert listing.status_code == 200
        assert len(listing.json()['items']) == 3
        assert listing.json()['total'] > 0
        assert all(x['category'] == 'recovery' for x in listing.json()['items'])


def test_dataset_task_lookup_and_retrieval():
    with TestClient(app) as client:
        item = client.get('/api/dataset/tasks/NEXA-DATA-0001')
        assert item.status_code == 200
        assert item.json()['task_id'] == 'NEXA-DATA-0001'
        missing = client.get('/api/dataset/tasks/not-a-real-task')
        assert missing.status_code == 404
        matches = client.get('/api/dataset/retrieve', params={'q': 'Python tutorials YouTube', 'limit': 3})
        assert matches.status_code == 200
        assert len(matches.json()['matches']) <= 3
        assert 'not verified workflows' in matches.json()['note']


def test_dataset_api_rejects_invalid_pagination():
    with TestClient(app) as client:
        assert client.get('/api/dataset/tasks', params={'limit': 999}).status_code == 400
        assert client.get('/api/dataset/tasks', params={'offset': -1}).status_code == 400
        assert client.get('/api/dataset/retrieve', params={'q': 'test', 'limit': 99}).status_code == 400
