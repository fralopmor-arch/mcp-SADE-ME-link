from mcp_servers.energy_data import server_data2 as server_data


def test_entsoe_style_labels_are_mapped_for_kpis() -> None:
    analytics = server_data._build_generation_analytics(
        "2026-02-20",
        {
            "Solar": [100.0] * 24,
            "Wind Onshore": [200.0] * 24,
            "Hydro Water Reservoir": [50.0] * 24,
            "Fossil Gas": [100.0] * 24,
            "Fossil Hard coal": [50.0] * 24,
            "Waste": [20.0] * 24,
            "Nuclear": [80.0] * 24,
        },
    )

    indicators = analytics["indicators"]

    assert indicators["_has_exactly_24_values"] == 1
    assert analytics["mix_mw"]["photovoltaic solar"] == 100.0
    assert analytics["mix_mw"]["wind"] == 200.0
    assert analytics["mix_mw"]["hydroelectric"] == 50.0
    assert analytics["mix_mw"]["combined cycle"] == 100.0
    assert analytics["mix_mw"]["coal"] == 50.0

    assert indicators["renewable_share"] == 0.5833
    assert indicators["gas_dependency"] == 0.1667
    assert indicators["thermal_share"] == 0.25
