"""Resource lifecycle regressions for the isolated mail worker."""

from types import SimpleNamespace

import pytest

import evals.mail_task_worker as mail_task_worker
from evals.mail_task_worker import _mail_resource_scope


class FakeService:
    def __init__(self, error=None):
        self.error = error
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1
        if self.error is not None:
            raise self.error


class FakeClient:
    def __init__(self, error=None):
        self.error = error
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1
        if self.error is not None:
            raise self.error


class FakeProvider:
    def __init__(self, error=None):
        self._client = FakeClient(error)


async def test_prepare_failure_closes_service_created_before_prepare():
    service = FakeService()
    prepare_error = RuntimeError("prepare failed")

    async def prepare():
        raise prepare_error

    with pytest.raises(RuntimeError) as caught:
        async with _mail_resource_scope(service):
            await prepare()

    assert caught.value is prepare_error
    assert service.close_calls == 1


async def test_execute_task_closes_service_when_prepare_fails(monkeypatch, tmp_path):
    service_error = OSError("service close failed")
    service = FakeService(service_error)
    prepare_error = RuntimeError("prepare failed")

    async def prepare():
        raise prepare_error

    monkeypatch.setattr(
        mail_task_worker,
        "_fixture_service",
        lambda *args, **kwargs: (service, object(), prepare),
    )
    monkeypatch.setattr("nanobot.config.loader.set_config_path", lambda path: None)

    with pytest.raises(RuntimeError) as caught:
        await mail_task_worker._execute_task(
            SimpleNamespace(),
            {
                "mail_fixture": {"mails": []},
                "case_id": "V085",
                "runtime": str(tmp_path / "runtime"),
                "workspace": str(tmp_path / "workspace"),
                "input": "question",
                "mode": "D",
                "trial_id": "trial",
            },
            object(),
        )

    assert caught.value is prepare_error
    assert caught.value.__cause__ is service_error
    assert service.close_calls == 1


async def test_early_return_closes_provider_and_service():
    service = FakeService()
    provider = FakeProvider()

    async def task():
        async with _mail_resource_scope(service) as resources:
            resources["provider"] = provider
            return "worker declined"

    assert await task() == "worker declined"
    assert provider._client.close_calls == 1
    assert service.close_calls == 1


async def test_task_error_survives_provider_close_failure_and_service_is_closed():
    provider_error = RuntimeError("provider close failed")
    provider = FakeProvider(provider_error)
    service = FakeService()
    task_error = ValueError("task failed")

    with pytest.raises(ValueError) as caught:
        async with _mail_resource_scope(service) as resources:
            resources["provider"] = provider
            raise task_error

    assert caught.value is task_error
    assert caught.value.__cause__ is provider_error
    assert provider._client.close_calls == 1
    assert service.close_calls == 1


async def test_both_close_failures_keep_task_and_cleanup_exception_chain():
    provider_error = RuntimeError("provider close failed")
    service_error = OSError("service close failed")
    provider = FakeProvider(provider_error)
    service = FakeService(service_error)
    task_error = ValueError("task failed")

    with pytest.raises(ValueError) as caught:
        async with _mail_resource_scope(service) as resources:
            resources["provider"] = provider
            raise task_error

    assert caught.value is task_error
    assert caught.value.__cause__ is service_error
    assert service_error.__cause__ is provider_error
    assert provider._client.close_calls == 1
    assert service.close_calls == 1


async def test_close_failure_propagates_when_task_succeeded():
    provider_error = RuntimeError("provider close failed")
    service_error = OSError("service close failed")
    provider = FakeProvider(provider_error)
    service = FakeService(service_error)

    with pytest.raises(OSError) as caught:
        async with _mail_resource_scope(service) as resources:
            resources["provider"] = provider

    assert caught.value is service_error
    assert service_error.__cause__ is provider_error
    assert provider._client.close_calls == 1
    assert service.close_calls == 1
