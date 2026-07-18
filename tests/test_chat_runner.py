import chat_runner


def test_parse_intent_summary_and_raw_context() -> None:
    assert chat_runner.parse_intent("please summary now") == "summary"
    assert chat_runner.parse_intent("show data") == "raw context"


def test_run_chat_action_summary_success(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_context(period: str, location: str) -> dict:
        calls.append("context")
        return {"period": period, "location": location, "demand": [1, 2, 3]}

    async def fake_summary(period: str, location: str) -> dict:
        calls.append("summary")
        return {"summary": f"ok {period} {location}"}

    monkeypatch.setattr(chat_runner, "get_daily_energy_context", fake_context)
    monkeypatch.setattr(chat_runner, "summarize_daily_energy_context", fake_summary)

    result = chat_runner.asyncio.run(
        chat_runner.run_chat_action("summary this", "2026-01-10", "Madrid,ES")
    )

    assert calls == ["context", "summary"]
    assert result["intent"] == "summary"
    assert result["summary"] == "ok 2026-01-10 Madrid,ES"


def test_run_chat_action_returns_plain_error_dict(monkeypatch) -> None:
    async def fake_context(period: str, location: str) -> dict:
        raise RuntimeError("boom")

    monkeypatch.setattr(chat_runner, "get_daily_energy_context", fake_context)

    result = chat_runner.asyncio.run(
        chat_runner.run_chat_action("raw", "2026-01-10", "Madrid,ES")
    )

    assert result == {"error": "boom"}


def test_run_chat_action_normalizes_non_dict_result(monkeypatch) -> None:
    async def fake_context(period: str, location: str) -> list:
        return [1, 2, 3]

    monkeypatch.setattr(chat_runner, "get_daily_energy_context", fake_context)

    result = chat_runner.asyncio.run(
        chat_runner.run_chat_action("raw", "2026-01-10", "Madrid,ES")
    )

    assert result == {"error": "get_daily_energy_context must return dict"}


def test_run_chat_action_raw_context_keeps_generation_mix(monkeypatch) -> None:
    async def fake_context(period: str, location: str) -> dict:
        return {
            "period": period,
            "location": location,
            "demand": [1, 2, 3],
            "weather": {"temperature": [10.0] * 24},
            "generation_mix": {
                "requested_date": period,
                "effective_date": period,
                "data_staleness_days": 0,
                "mix_mw": {"wind": 1200.0},
            },
            "partial_failure": False,
            "errors": {},
        }

    monkeypatch.setattr(chat_runner, "get_daily_energy_context", fake_context)

    result = chat_runner.asyncio.run(
        chat_runner.run_chat_action("show full context", "2026-01-10", "Madrid,ES")
    )

    assert result["intent"] == "raw context"
    assert result["context"]["generation_mix"]["requested_date"] == "2026-01-10"
    assert result["context"]["generation_mix"]["effective_date"] == "2026-01-10"
    assert result["context"]["generation_mix"]["data_staleness_days"] == 0


def test_run_smoke_flow_maps_all_expected_paths(monkeypatch) -> None:
    async def fake_consumption(period: str) -> dict:
        return {"period": period, "consumption": [100] * 24}

    async def fake_weather(location: str, period: str) -> dict:
        return {
            "location": location,
            "period": period,
            "temperature": [10.0] * 24,
            "wind_speed": [3.0] * 24,
            "solar_irradiance": [0.0] * 24,
        }

    async def fake_generation(period: str) -> dict:
        return {
            "requested_date": period,
            "effective_date": period,
            "data_staleness_days": 0,
            "mix_mw": {"wind": 1000.0},
        }

    async def fake_run_chat_action(prompt: str, period: str, location: str) -> dict:
        if "summary" in prompt:
            return {"summary": "ok"}
        return {
            "intent": "raw context",
            "period": period,
            "location": location,
            "context": {"period": period},
        }

    monkeypatch.setattr(chat_runner, "load_consumption_data", fake_consumption)
    monkeypatch.setattr(chat_runner, "load_weather_data", fake_weather)
    monkeypatch.setattr(chat_runner, "get_generation_mix", fake_generation)
    monkeypatch.setattr(chat_runner, "run_chat_action", fake_run_chat_action)

    result = chat_runner.asyncio.run(
        chat_runner.run_smoke_flow("2026-01-10", "Madrid,ES")
    )

    rows = result["mapping"]
    assert len(rows) == 5
    assert [row["tool_or_function"] for row in rows] == [
        "load_consumption_data",
        "load_weather_data",
        "get_generation_mix",
        "get_daily_energy_context",
        "summarize_daily_energy_context",
    ]
    assert all(row["observed_result"] == "pass" for row in rows)


def test_run_smoke_flow_uses_binary_fail(monkeypatch) -> None:
    async def fake_consumption(period: str) -> dict:
        return {"period": period, "consumption": [100] * 24}

    async def fake_weather(location: str, period: str) -> dict:
        return {"error": "weather unavailable"}

    async def fake_generation(period: str) -> dict:
        return {
            "requested_date": period,
            "effective_date": period,
            "data_staleness_days": 0,
            "mix_mw": {"wind": 1000.0},
        }

    async def fake_run_chat_action(prompt: str, period: str, location: str) -> dict:
        return {"intent": "raw context", "context": {"period": period}}

    monkeypatch.setattr(chat_runner, "load_consumption_data", fake_consumption)
    monkeypatch.setattr(chat_runner, "load_weather_data", fake_weather)
    monkeypatch.setattr(chat_runner, "get_generation_mix", fake_generation)
    monkeypatch.setattr(chat_runner, "run_chat_action", fake_run_chat_action)

    result = chat_runner.asyncio.run(
        chat_runner.run_smoke_flow("2026-01-10", "Madrid,ES")
    )

    rows = result["mapping"]
    weather_row = next(
        row for row in rows if row["tool_or_function"] == "load_weather_data"
    )
    assert weather_row["observed_result"] == "fail"