"""A failed owner releases waiters without a retry stampede or failure cache."""
import asyncio

import httpx
import pytest

import nutrigenomics.explorer_performance as performance


@pytest.mark.parametrize('status', ['query_timeout', 'source_unavailable', 'source_changed'])
def test_failed_singleflight_is_shared_as_failure_without_recomputation(monkeypatch, status):
    cache = performance.ByteCache()
    monkeypatch.setattr(performance, 'CACHE', cache)
    monkeypatch.setattr(performance, 'source_paths', list)
    calls = []

    async def failing_app(scope, receive, send):
        calls.append(1)
        # Yield so all four requests join this same in-flight computation.
        await asyncio.sleep(0.03)
        raise performance.QueryUnavailable(status)

    middleware = performance.ExplorerReadMiddleware(failing_app)

    async def batch():
        transport = httpx.ASGITransport(app=middleware)
        async with httpx.AsyncClient(transport=transport, base_url='http://fixture') as client:
            responses = await asyncio.gather(*[
                client.get('/api/v1/explorer/graph?seed_id=identical') for _ in range(4)
            ])
            assert len(calls) == 1
            assert all(response.status_code == 503 for response in responses)
            assert all(response.json()['detail']['code'] == status for response in responses)
            assert not cache.items and not cache.flights
            # An independent later request can try again; the failure is not cached.
            later = await client.get('/api/v1/explorer/graph?seed_id=identical')
            assert later.status_code == 503 and len(calls) == 2
            assert not cache.items and not cache.flights

    asyncio.run(batch())
    assert all(middleware.slots.acquire(blocking=False) for _ in range(4))
    for _ in range(4):
        middleware.slots.release()


def test_failure_future_is_explicit_and_does_not_allocate_cached_bytes():
    cache = performance.ByteCache()
    owner, owns = cache.claim('same-request')
    follower, waits = cache.claim('same-request')
    assert owns and not waits and owner is follower
    cache.finish('same-request', failure='source_unavailable')
    result = follower.result()
    assert isinstance(result, performance.FlightFailure)
    assert result.status == 'source_unavailable'
    assert not cache.items and cache.bytes == 0 and not cache.flights
