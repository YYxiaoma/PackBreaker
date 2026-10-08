import asyncio

from backend.app.application.unpack_driver import UnpackDriver


class _FakeDiscoveryService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.fail_for: str | None = None

    def list_due_execution_ids(self, *, limit: int = 20) -> tuple[str, ...]:
        assert limit == 2
        return ("execution-1", "execution-2")

    def discover_next_page(self, execution_id: str, *, limit: int = 100) -> object:
        self.calls.append((execution_id, limit))
        if execution_id == self.fail_for:
            raise RuntimeError("synthetic failure")
        return object()


class _FakeMonitorService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_due_definition_ids(self, *, limit: int = 20) -> tuple[str, ...]:
        assert limit == 2
        return ("definition-1",)

    async def trigger_due_definition(self, definition_id: str) -> object:
        self.calls.append(definition_id)
        return object()


class _FakeMatchService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def list_matching_execution_ids(self, *, limit: int = 20) -> tuple[str, ...]:
        assert limit == 2
        return ("matching-1",)

    async def match_next_batch(self, execution_id: str, *, limit: int = 20) -> object:
        self.calls.append((execution_id, limit))
        return object()


class _FakeContentVerificationService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def list_verifiable_execution_ids(self, *, limit: int = 20) -> tuple[str, ...]:
        assert limit == 2
        return ("verify-1",)

    async def verify_next_batch(self, execution_id: str, *, limit: int = 5) -> object:
        self.calls.append((execution_id, limit))
        return object()


class _FakeAuxiliaryService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def list_auxiliary_execution_ids(self, *, limit: int = 20) -> tuple[str, ...]:
        assert limit == 2
        return ("auxiliary-1",)

    async def advance_next_batch(self, execution_id: str, *, limit: int = 2) -> object:
        self.calls.append((execution_id, limit))
        return object()


class _FakeExecutionPlanService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def list_plannable_execution_ids(self, *, limit: int = 20) -> tuple[str, ...]:
        assert limit == 2
        return ("plan-1",)

    async def plan_next_batch(self, execution_id: str, *, limit: int = 5) -> object:
        self.calls.append((execution_id, limit))
        return object()


class _FakeMaterializationService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def list_materializable_execution_ids(self, *, limit: int = 20) -> tuple[str, ...]:
        assert limit == 2
        return ("materialize-1",)

    def materialize_next_batch(self, execution_id: str, *, limit: int = 5) -> object:
        self.calls.append((execution_id, limit))
        return object()


class _FakeSeedingService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def list_seedable_execution_ids(self, *, limit: int = 20) -> tuple[str, ...]:
        assert limit == 2
        return ("seed-1",)

    async def advance_next_batch(self, execution_id: str, *, limit: int = 5) -> object:
        self.calls.append((execution_id, limit))
        return object()


def test_unpack_driver_advances_due_executions_in_bounded_batches() -> None:
    service = _FakeDiscoveryService()
    driver = UnpackDriver(
        service,
        interval_seconds=10,
        execution_limit=2,
        discovery_batch_size=50,
    )

    advanced = asyncio.run(driver.run_once())

    assert advanced == 2
    assert service.calls == [("execution-1", 50), ("execution-2", 50)]
    assert driver.state.ticks_started == 1
    assert driver.state.ticks_completed == 1


def test_unpack_driver_triggers_due_monitor_definitions_before_discovery() -> None:
    discovery = _FakeDiscoveryService()
    monitor = _FakeMonitorService()
    driver = UnpackDriver(
        discovery,
        monitor,
        interval_seconds=10,
        execution_limit=2,
        discovery_batch_size=50,
    )

    advanced = asyncio.run(driver.run_once())

    assert advanced == 3
    assert monitor.calls == ["definition-1"]
    assert discovery.calls == [("execution-1", 50), ("execution-2", 50)]


def test_unpack_driver_advances_matching_after_discovery() -> None:
    discovery = _FakeDiscoveryService()
    matching = _FakeMatchService()
    driver = UnpackDriver(
        discovery,
        None,
        matching,
        interval_seconds=10,
        execution_limit=2,
        discovery_batch_size=50,
        match_batch_size=7,
    )

    advanced = asyncio.run(driver.run_once())

    assert advanced == 3
    assert matching.calls == [("matching-1", 7)]


def test_unpack_driver_advances_content_verification_after_matching() -> None:
    discovery = _FakeDiscoveryService()
    matching = _FakeMatchService()
    content = _FakeContentVerificationService()
    driver = UnpackDriver(
        discovery,
        None,
        matching,
        content,
        interval_seconds=10,
        execution_limit=2,
        discovery_batch_size=50,
        match_batch_size=7,
        content_verification_batch_size=3,
    )

    advanced = asyncio.run(driver.run_once())

    assert advanced == 4
    assert matching.calls == [("matching-1", 7)]
    assert content.calls == [("verify-1", 3)]


def test_unpack_driver_advances_auxiliary_staging_after_content_verification() -> None:
    discovery = _FakeDiscoveryService()
    matching = _FakeMatchService()
    content = _FakeContentVerificationService()
    auxiliary = _FakeAuxiliaryService()
    driver = UnpackDriver(
        discovery,
        None,
        matching,
        content,
        auxiliary,
        interval_seconds=10,
        execution_limit=2,
        discovery_batch_size=50,
        match_batch_size=7,
        content_verification_batch_size=3,
        auxiliary_batch_size=1,
    )

    advanced = asyncio.run(driver.run_once())

    assert advanced == 5
    assert content.calls == [("verify-1", 3)]
    assert auxiliary.calls == [("auxiliary-1", 1)]


def test_unpack_driver_advances_plan_materialization_and_seeding_in_order() -> None:
    discovery = _FakeDiscoveryService()
    matching = _FakeMatchService()
    content = _FakeContentVerificationService()
    auxiliary = _FakeAuxiliaryService()
    planning = _FakeExecutionPlanService()
    materialization = _FakeMaterializationService()
    seeding = _FakeSeedingService()
    driver = UnpackDriver(
        discovery,
        None,
        matching,
        content,
        auxiliary,
        planning,
        materialization,
        seeding,
        interval_seconds=10,
        execution_limit=2,
        discovery_batch_size=50,
        match_batch_size=7,
        content_verification_batch_size=3,
        auxiliary_batch_size=1,
        execution_plan_batch_size=4,
        materialization_batch_size=5,
        seeding_batch_size=6,
    )

    advanced = asyncio.run(driver.run_once())

    assert advanced == 8
    assert planning.calls == [("plan-1", 4)]
    assert materialization.calls == [("materialize-1", 5)]
    assert seeding.calls == [("seed-1", 6)]


def test_unpack_driver_isolates_single_execution_failure() -> None:
    service = _FakeDiscoveryService()
    service.fail_for = "execution-1"
    driver = UnpackDriver(
        service,
        interval_seconds=10,
        execution_limit=2,
        discovery_batch_size=50,
    )

    advanced = asyncio.run(driver.run_once())

    assert advanced == 1
    assert service.calls == [("execution-1", 50), ("execution-2", 50)]
    assert driver.state.ticks_completed == 1
