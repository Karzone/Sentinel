"""Phase 6: the read-only dashboard.

The chart tests assert *spec properties* — one y-axis, a legend, stable hues,
endpoint labels — rather than "a chart object was returned". A test that passes
with the chart rendering nothing is a defect, so each one reaches into the Vega
spec for the thing it claims to check.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import re
import sqlite3
from decimal import Decimal

import pandas as pd
import pytest

from sentinel.dashboard import (
    auth, charts, components as ui, palette as pal, queries, views,
)
from sentinel.evals import signal_quality
from sentinel.domain import IdeaClass
from sentinel.portfolio import Ledger
from sentinel.storage import repo


# ---------------------------------------------------------------- palette


class TestPalette:
    def test_both_modes_carry_the_validated_slots(self):
        """These exact hexes were run through the data-viz validator in both
        modes. Changing one means re-running it, not editing this list."""
        assert pal.LIGHT.series == ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")
        assert pal.DARK.series == ("#3987e5", "#d95926", "#199e70", "#c98500")

    def test_dark_is_a_selected_palette_not_an_inversion(self):
        # Every slot differs from its light counterpart because each was stepped
        # for the dark surface; an automatic flip would ship unchecked colours.
        assert all(a != b for a, b in zip(pal.LIGHT.series, pal.DARK.series))
        assert pal.LIGHT.surface != pal.DARK.surface

    def test_a_ninth_hue_is_refused_rather_than_generated(self):
        """A generated hue is indistinguishable from an existing slot under CVD."""
        with pytest.raises(IndexError, match="fold the tail"):
            pal.LIGHT.slot(len(pal.LIGHT.series))

    def test_status_colours_are_identical_across_modes_and_distinct_from_series(self):
        assert pal.LIGHT.status == pal.DARK.status
        for mode in (pal.LIGHT, pal.DARK):
            assert not set(mode.status.values()) & set(mode.series)

    def test_the_ordinal_ramp_is_a_single_hue_light_to_dark(self):
        assert len(set(pal.LIGHT.ordinal)) == len(pal.LIGHT.ordinal)
        assert pal.LIGHT.ordinal[0] != pal.LIGHT.ordinal[-1]

    def test_gridlines_are_solid_never_dashed(self):
        """A dashed grid reads as 'threshold' when it is just a grid."""
        for mode in ("light", "dark"):
            assert pal.theme_config(mode)["config"]["axis"]["gridDash"] == []

    def test_the_legend_never_shares_a_corner_with_the_axis_title_or_labels(self):
        """Two collisions were caught in screenshots: a top-left legend sat under
        the horizontal y-axis title, and a top-right one sat over the endpoint
        labels. The bottom is the only edge neither occupies."""
        for mode in ("light", "dark"):
            config = pal.theme_config(mode)["config"]
            assert config["legend"]["orient"] == "bottom"
            assert config["axisY"]["titleAlign"] == "left"

    def test_an_unknown_mode_is_refused(self):
        with pytest.raises(ValueError, match="light"):
            pal.get("sepia")


# ---------------------------------------------------------------- read-only


class TestReadOnly:
    def test_the_dashboard_connection_refuses_writes(self, tmp_path):
        """Enforced by SQLite, not by anyone remembering. A future page that
        grows a button still cannot write."""
        from sentinel.storage import init_db

        path = tmp_path / "ro.sqlite"
        init_db(path).close()
        conn = queries.read_only_connect(path)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO audit(ts, run_id, event) VALUES('x','y','z')")

    def test_a_missing_database_fails_loudly(self, tmp_path):
        """mode=ro will not create the file, so this cannot silently serve an
        empty database that looks like 'no data yet'."""
        with pytest.raises(FileNotFoundError, match="sentinel init"):
            queries.read_only_connect(tmp_path / "absent.sqlite")

    def test_a_seeded_database_is_flagged_as_fabricated(self, conn, tmp_path):
        conn.execute("INSERT INTO schema_meta(key, value) VALUES('demo_data','true')")
        assert queries.is_demo_database(conn) is True

    def test_a_normal_database_is_not_flagged(self, conn):
        assert queries.is_demo_database(conn) is False


# ---------------------------------------------------------------- queries


@pytest.fixture()
def populated(conn, config):
    """A small but real account: bars, an equity curve, one open position."""
    from sentinel.data import ingest as ingest_mod

    ingest_mod.ingest(conn, config, ["DEMO1.LSE", "DEMO2.LSE"], history_days=500)
    ledger = Ledger(Decimal("10000"))
    bars = repo.get_bars(conn, "DEMO1.LSE")
    entry = bars[-40].adjusted_close
    ledger.open(ticker="DEMO1.LSE", idea_id="i-1", idea_class=IdeaClass.LONG_TERM,
                sector="consumer", shares=5, price=entry, date=bars[-40].date,
                stop=entry * Decimal("0.9"))
    for bar in bars[-40:]:
        point = ledger.mark_to_market(bar.date, {"DEMO1.LSE": bar.adjusted_close})
        repo.save_equity_point(conn, bar.date, point.nav_gbp, point.cash_gbp,
                               point.high_water_gbp)
    for position in ledger.positions.values():
        repo.save_position(conn, position)
    return conn


class TestQueries:
    def test_the_snapshot_reports_nav_cash_and_drawdown(self, populated, config):
        snapshot = queries.portfolio_snapshot(populated, config)
        assert snapshot.nav > 0
        assert snapshot.open_positions == 1
        assert snapshot.drawdown >= 0

    def test_the_equity_frame_carries_a_drawdown_column(self, populated):
        frame = queries.equity_frame(populated)
        assert len(frame) == 40
        assert (frame["drawdown"] <= 0).all()

    def test_benchmarks_share_one_axis_by_indexing_to_a_common_base(self, populated, config):
        """Every series starts from the same capital, which is what lets four of
        them share a single y-axis instead of needing a second one."""
        frame = queries.benchmark_frame(populated, config)
        first = frame.sort_values("date").groupby("series").head(1)
        assert first["value"].nunique() == 1

    def test_an_uningested_benchmark_is_absent_rather_than_flat_lined(self, populated, config):
        """A flat line reads as 'the index did nothing', which is a different
        claim from 'we have no data'."""
        series = set(queries.benchmark_frame(populated, config)["series"])
        assert not any("VWRP" in s for s in series)
        assert any("cash" in s for s in series)   # B3 needs no vendor

    def test_positions_carry_distance_to_stop(self, populated):
        frame = queries.positions_frame(populated)
        assert len(frame) == 1
        assert 0 <= frame["to_stop"].iloc[0] <= 1

    def test_sector_weights_are_measured_against_the_configured_cap(self, populated, config):
        frame = queries.sector_frame(populated, config)
        assert not frame.empty
        assert frame["cap"].iloc[0] == pytest.approx(0.30)

    def test_freshness_uses_the_same_thresholds_as_the_quality_layer(self, populated):
        frame = queries.freshness_frame(populated)
        assert set(frame["status"]) <= {"good", "warning", "critical"}

    def test_a_catalyst_whose_horizon_has_not_elapsed_is_not_scored(self, conn, config):
        """Scoring a 90-day call after 20 days biases the hit rate toward
        whatever the last three weeks did."""
        assert queries.catalyst_calls(conn) == []

    def test_empty_inputs_give_empty_frames_not_exceptions(self, conn, config):
        assert queries.equity_frame(conn).empty
        assert queries.positions_frame(conn).empty
        assert queries.sector_frame(conn, config).empty
        assert queries.ideas_frame(conn).empty
        assert queries.quality_history_frame(conn).empty


# ---------------------------------------------------------------- charts


def equity_fixture() -> pd.DataFrame:
    rows = []
    for day in range(1, 15):
        for index, name in enumerate(["Sentinel (paper)", "B1 · global index", "B3 · cash"]):
            rows.append({"date": pd.Timestamp(2026, 1, day), "series": name,
                         "value": 10000 + day * (index + 1) * 10, "role": "x"})
    return pd.DataFrame(rows)


def _values(spec: dict, node: dict) -> list[dict]:
    """Resolve a node's data whether Altair inlined it or named it.

    Altair 6 hoists repeated frames into a top-level `datasets` map, so a test
    that only looks for `data.values` passes or fails depending on how the chart
    happened to be assembled rather than on what it contains.
    """
    data = node.get("data", {})
    if "values" in data:
        return data["values"]
    name = data.get("name")
    return spec.get("datasets", {}).get(name, [])


def _layers(spec: dict) -> list[dict]:
    return spec.get("layer", [spec])


def _y_fields(spec: dict) -> set[str]:
    """Every distinct y encoding in a spec, however deeply layered."""
    found: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            encoding = node.get("encoding", {})
            y = encoding.get("y")
            if isinstance(y, dict) and "field" in y:
                found.add(y["field"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(spec)
    return found


class TestCharts:
    def test_the_equity_chart_has_one_y_scale_not_two(self):
        """Dual axes are the single most common charting mistake: the alignment
        of the two scales is arbitrary, so the chart invents a correlation."""
        spec = charts.equity_vs_benchmarks(equity_fixture(), "light").to_dict()
        assert _y_fields(spec) == {"value"}
        assert "resolve" not in spec or spec.get("resolve", {}).get("scale", {}).get("y") != "independent"

    def test_the_equity_chart_carries_a_legend_for_multiple_series(self):
        spec = charts.equity_vs_benchmarks(equity_fixture(), "light").to_dict()
        colours = [layer["encoding"]["color"] for layer in spec["layer"]
                   if "color" in layer.get("encoding", {})]
        assert any(c.get("legend") is not None for c in colours)

    def test_it_labels_endpoints_only_never_every_point(self):
        """A number beside every point is chaos and goes unread."""
        frame = equity_fixture()
        spec = charts.equity_vs_benchmarks(frame, "light").to_dict()
        text_layers = [layer for layer in _layers(spec)
                       if str(layer.get("mark", {}).get("type", layer.get("mark"))) == "text"]
        assert text_layers
        labelled = len(_values(spec, text_layers[0]))
        assert labelled == frame["series"].nunique()   # one per series, not per point
        assert labelled < len(frame)

    def test_filtering_a_series_out_does_not_repaint_the_survivors(self):
        """Colour follows the entity, never its row number. A reader who learned
        'cash is yellow' must keep that when a series is removed."""
        full = charts._series_scale(
            ["Sentinel (paper)", "B1", "B2", "B3"], "light", strategy="Sentinel (paper)"
        ).to_dict()
        filtered = charts._series_scale(
            ["Sentinel (paper)", "B1", "B3"], "light", strategy="Sentinel (paper)"
        ).to_dict()
        full_map = dict(zip(full["domain"], full["range"]))
        filtered_map = dict(zip(filtered["domain"], filtered["range"]))
        assert filtered_map["Sentinel (paper)"] == full_map["Sentinel (paper)"]
        assert filtered_map["B1"] == full_map["B1"]

    def test_the_strategy_always_takes_slot_one_whatever_the_input_order(self):
        scale = charts._series_scale(["B1", "B2", "Sentinel (paper)"], "light",
                                     strategy="Sentinel (paper)").to_dict()
        assert scale["domain"][0] == "Sentinel (paper)"
        assert scale["range"][0] == pal.LIGHT.slot(0)

    def test_the_drawdown_chart_marks_the_kill_switch_threshold(self):
        frame = pd.DataFrame([
            {"date": pd.Timestamp(2026, 1, d), "nav": 10000 - d * 100,
             "high_water": 10000, "drawdown": -d / 100}
            for d in range(1, 12)
        ])
        spec = charts.drawdown_area(frame, "light", kill_pct=0.15).to_dict()
        rules = [layer for layer in _layers(spec)
                 if str(layer.get("mark", {}).get("type", "")) == "rule"]
        assert rules
        assert any(_values(spec, rule) == [{"limit": -0.15}] for rule in rules)

    def test_a_sector_over_the_cap_takes_the_reserved_critical_colour(self):
        frame = pd.DataFrame([{"sector": "tech", "weight": 0.4, "cap": 0.3, "over": True},
                              {"sector": "consumer", "weight": 0.1, "cap": 0.3, "over": False}])
        spec = charts.sector_vs_limit(frame, "light").to_dict()
        encoded = str(spec["layer"][0]["encoding"]["color"])
        assert pal.LIGHT.status["critical"] in encoded

    def test_module_scores_diverge_from_neutral_fifty(self):
        """On a plain 0-100 bar chart, 51 and 92 sit at the same end and look
        like neighbours."""
        frame = pd.DataFrame([
            {"module": "technical", "score": 72.0, "delta": 22.0, "confidence": 1.0, "version": "v1"},
            {"module": "fundamental", "score": 31.0, "delta": -19.0, "confidence": 1.0, "version": "v1"},
        ])
        spec = charts.module_scores(frame, "light").to_dict()
        x = spec["layer"][0]["encoding"]["x"]
        assert x["field"] == "delta"
        assert x["scale"]["domain"] == [-50, 50]

    def test_the_hit_rate_chart_shows_the_interval_and_the_coin_flip_baseline(self):
        frame = pd.DataFrame([{"label": "Catalyst direction", "rate": 0.6, "low": 0.4,
                               "high": 0.8, "n": 40, "verdict": "…", "significant": False}])
        spec = charts.hit_rate_interval(frame, "light").to_dict()
        assert any(_values(spec, layer) == [{"coin": 0.5}] for layer in _layers(spec))
        assert any("x2" in layer.get("encoding", {}) for layer in _layers(spec))

    def test_an_insignificant_hit_rate_is_drawn_in_muted_ink_not_the_accent(self):
        """The colour should not imply a result the interval does not support."""
        base = {"label": "x", "rate": 0.6, "low": 0.4, "high": 0.8, "n": 40, "verdict": ""}
        weak = charts.hit_rate_interval(
            pd.DataFrame([{**base, "significant": False}]), "light").to_dict()
        strong = charts.hit_rate_interval(
            pd.DataFrame([{**base, "significant": True}]), "light").to_dict()
        assert pal.LIGHT.ink_muted in str(weak)
        assert pal.LIGHT.slot(0) in str(strong)

    def test_ordered_categories_use_the_ordinal_ramp_not_categorical_hues(self):
        frame = pd.DataFrame([
            {"conviction": "low", "mean_return": 0.01, "samples": 3, "win_rate": 0.5},
            {"conviction": "high", "mean_return": 0.09, "samples": 4, "win_rate": 0.7},
        ])
        spec = charts.ordinal_bars(frame, "light", field="conviction", value="mean_return",
                                   order=["low", "medium", "high"], value_title="Mean return").to_dict()
        ramp = spec["layer"][1]["encoding"]["color"]["scale"]["range"]
        assert ramp == [pal.LIGHT.ordinal[0], pal.LIGHT.ordinal[1]]

    def test_severity_uses_the_status_palette_not_series_colours(self):
        frame = pd.DataFrame([
            {"as_of": pd.Timestamp(2026, 1, 2), "severity": "warn", "count": 3},
            {"as_of": pd.Timestamp(2026, 1, 2), "severity": "critical", "count": 1},
        ])
        spec = charts.severity_history(frame, "light").to_dict()
        colours = spec["encoding"]["color"]["scale"]["range"]
        assert pal.LIGHT.status["critical"] in colours
        assert pal.LIGHT.status["warning"] in colours

    def test_stacked_segments_are_separated_by_a_surface_gap_not_a_border(self):
        frame = pd.DataFrame([{"as_of": pd.Timestamp(2026, 1, 2), "severity": "warn", "count": 3}])
        mark = charts.severity_history(frame, "light").to_dict()["mark"]
        assert mark["stroke"] == pal.LIGHT.surface
        assert mark["strokeWidth"] == pal.SURFACE_GAP

    @pytest.mark.parametrize("builder", [
        lambda m: charts.equity_vs_benchmarks(pd.DataFrame(), m),
        lambda m: charts.drawdown_area(pd.DataFrame(), m),
        lambda m: charts.sector_vs_limit(pd.DataFrame(), m),
        lambda m: charts.module_scores(pd.DataFrame(), m),
        lambda m: charts.hit_rate_interval(pd.DataFrame(), m),
        lambda m: charts.severity_history(pd.DataFrame(), m),
    ])
    def test_every_chart_renders_a_reason_rather_than_an_empty_grid(self, builder):
        """An empty grid looks like 'the values are all zero', which is a
        different claim from 'we have not measured this yet'."""
        spec = builder("light").to_dict()
        assert spec["mark"]["type"] == "text"
        assert _values(spec, spec)[0]["message"]

    def test_charts_build_in_both_modes(self):
        for mode in ("light", "dark"):
            assert charts.equity_vs_benchmarks(equity_fixture(), mode).to_dict()


# ---------------------------------------------------------------- auth


class TestAuthPolicy:
    def test_no_password_off_localhost_refuses_to_serve(self):
        """Fail-closed. A banner nobody reads is not an access control."""
        decision = auth.decide(configured_password=None, submitted=None, is_local=False)
        assert decision.outcome == "refused"
        assert not decision.may_render

    def test_no_password_on_localhost_serves_with_a_notice(self):
        decision = auth.decide(configured_password=None, submitted=None, is_local=True)
        assert decision.may_render
        assert "before exposing this" in decision.message

    def test_a_correct_password_grants_access(self):
        assert auth.decide(configured_password="hunter2", submitted="hunter2",
                           is_local=False).may_render

    def test_a_wrong_password_is_rejected(self):
        decision = auth.decide(configured_password="hunter2", submitted="hunter3",
                               is_local=False)
        assert decision.outcome == "rejected" and not decision.may_render

    def test_an_empty_submission_prompts_rather_than_rejecting(self):
        assert auth.decide(configured_password="hunter2", submitted="",
                           is_local=False).outcome == "prompt"

    def test_a_password_does_not_shortcut_just_because_the_session_is_local(self):
        """Setting a password must not become advisory on localhost."""
        assert not auth.decide(configured_password="hunter2", submitted="wrong",
                               is_local=True).may_render


# ---------------------------------------------------------------- components


class TestComponents:
    def test_a_status_badge_carries_an_icon_and_a_word_not_just_colour(self):
        """Survives a colourblind reader, greyscale print and forced-colors."""
        markup = ui.badge("critical")
        assert ui.STATUS_ICONS["critical"] in markup
        assert "Breach" in markup

    def test_badge_text_is_escaped(self):
        assert "<script>" not in ui.badge("good", "<script>alert(1)</script>")

    def test_tiles_use_proportional_figures_for_large_numbers(self):
        """tabular-nums makes 121 look loose at display sizes."""
        markup = ui.tile("NAV", "£10,000.00", mode="light", hero=True)
        assert "sx-hero" in markup
        assert "tabular" not in markup

    def test_the_shell_hides_the_deploy_button_but_keeps_the_toolbar(self):
        """The toolbar holds Streamlit's theme setting, which is the single
        light/dark control — hiding it took the theme switcher with it."""
        css = ui.shell_css("light")
        assert 'data-testid="stAppDeployButton"' in css
        assert 'data-testid="stToolbar"] , ' not in css
        assert '[data-testid="stToolbar"], [data-testid="stDecoration"]' not in css

    def test_each_mode_paints_its_own_surfaces(self):
        assert pal.LIGHT.plane in ui.shell_css("light")
        assert pal.DARK.plane in ui.shell_css("dark")

    def test_drawdown_status_escalates_toward_the_limit(self):
        assert ui.status_for_drawdown(0.01, 0.15) == "good"
        assert ui.status_for_drawdown(0.10, 0.15) == "warning"
        assert ui.status_for_drawdown(0.16, 0.15) == "critical"


# ---------------------------------------------------------------- money guard


class TestNumpyDecimalCoercion:
    def test_numpy_floats_coerce_exactly(self):
        """np.float64 SUBCLASSES float, so repr() on it yields
        'np.float64(1.0)' and Decimal rejects it outright. Every pandas path
        into the money layer hits this."""
        import numpy as np

        from sentinel.money import dec

        assert dec(np.float64(10000.0)) == Decimal("10000.0")
        assert dec(pd.Series([0.1]).iloc[0]) == Decimal("0.1")

    def test_numpy_integers_coerce_exactly(self):
        import numpy as np

        from sentinel.money import dec

        assert dec(np.int64(42)) == Decimal("42")

    def test_a_bool_is_never_a_monetary_amount(self):
        from sentinel.money import dec

        with pytest.raises(TypeError):
            dec(True)


class TestShellCssDoesNotBreakIcons:
    def test_the_font_family_is_not_forced_onto_every_span(self):
        """A blanket `.stApp span { font-family }` overrides Streamlit's Material
        Symbols icon font, and the ligature then renders as its literal source
        text — every expander chevron read "arrow_right" over its own label."""
        import re

        # Strip comments first: the explanation of this very bug quotes the
        # offending selector, and a naive substring check matches the comment.
        rules = re.sub(r"/\*.*?\*/", "", ui.shell_css("light"), flags=re.S)
        assert ".stApp span" not in rules
        assert '[data-testid="stIconMaterial"]' in rules

    def test_the_icon_font_is_restored_explicitly(self):
        assert 'font-family: "Material Symbols Rounded"' in ui.shell_css("dark")


class TestOrdinalRampDepth:
    def test_there_is_a_step_for_every_materiality_bucket(self):
        """Materiality has five buckets. With a three-step ramp the top three
        clamped to the same shade, so the chart said 3, 4 and 5 were identical."""
        assert len(pal.LIGHT.ordinal) >= 5
        assert len(pal.DARK.ordinal) >= 5

    def test_five_buckets_get_five_distinct_shades(self):
        frame = pd.DataFrame([
            {"bucket": str(n), "mean_abs_move": n / 100, "samples": 3} for n in range(1, 6)
        ])
        spec = charts.ordinal_bars(frame, "light", field="bucket", value="mean_abs_move",
                                   order=["1", "2", "3", "4", "5"],
                                   value_title="Mean absolute move").to_dict()
        ramp = spec["layer"][1]["encoding"]["color"]["scale"]["range"]
        assert len(set(ramp)) == 5

    def test_the_hit_rate_axis_does_not_crowd_its_ticks(self):
        """Twenty-one ticks at 5% collided — "95%" ran into "100%"."""
        frame = pd.DataFrame([{"label": "x", "rate": 0.5, "low": 0.3, "high": 0.7,
                               "n": 20, "verdict": "", "significant": False}])
        spec = charts.hit_rate_interval(frame, "light").to_dict()
        axes = [layer["encoding"]["x"].get("axis", {}) for layer in _layers(spec)
                if "x" in layer.get("encoding", {})]
        assert any(axis.get("values") == [0, 0.25, 0.5, 0.75, 1.0] for axis in axes)

    def test_the_header_band_is_themed(self):
        """Unthemed, Streamlit's header left a white strip across every dark page."""
        assert '[data-testid="stHeader"]' in ui.shell_css("dark")


class TestBarMarkSpec:
    def test_bars_are_capped_in_pixels_not_as_a_share_of_the_band(self):
        """A 0.6 band share made a single-category bar a ~60px slab. The mark
        spec caps bars at 24px and lets the leftover band be air."""
        frame = pd.DataFrame([{"check": "has_invalidation", "count": 9}])
        spec = charts.counts_bar(frame, "light", field="check").to_dict()
        assert spec["layer"][0]["mark"]["height"] == pal.BAR_MAX_WIDTH
        assert pal.BAR_MAX_WIDTH <= 24

    def test_a_one_row_chart_is_not_stretched_to_a_fixed_minimum(self):
        one = charts.counts_bar(pd.DataFrame([{"check": "a", "count": 1}]),
                                "light", field="check").to_dict()
        five = charts.counts_bar(
            pd.DataFrame([{"check": c, "count": 1} for c in "abcde"]),
            "light", field="check").to_dict()
        assert one["height"] < five["height"]


# ---------------------------------------------------------------- evals page


class _Recorder:
    """A stand-in for the ``st`` module that records what a view rendered.

    The evals-page bug lived between two widgets on one screen, so it could only
    be caught by rendering the page and reading both numbers back. A test that
    called the query functions directly would have passed throughout.
    """

    def __init__(self) -> None:
        self.html: list[str] = []
        self.captions: list[str] = []

    # widgets the evals view touches
    def markdown(self, body, **_kw) -> None:
        self.html.append(str(body))

    def caption(self, body, **_kw) -> None:
        self.captions.append(str(body))

    def divider(self) -> None:
        pass

    def columns(self, spec, **_kw) -> list["_Recorder"]:
        count = spec if isinstance(spec, int) else len(spec)
        return [self] * count

    def expander(self, *_a, **_kw) -> "_Recorder":
        return self

    def altair_chart(self, *_a, **_kw) -> None:
        pass

    def dataframe(self, *_a, **_kw) -> None:
        pass

    def __enter__(self) -> "_Recorder":
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    # readers
    def tile_value(self, label: str) -> str:
        for block in self.html:
            if f">{label}<" in block:
                return re.search(r'class="sx-tile-value">([^<]*)<', block).group(1)
        raise AssertionError(f"no tile labelled {label!r} was rendered")

    def tile_delta(self, label: str) -> str:
        for block in self.html:
            if f">{label}<" in block:
                match = re.search(r'class="sx-tile-delta"[^>]*>([^<]*)<', block)
                return match.group(1) if match else ""
        raise AssertionError(f"no tile labelled {label!r} was rendered")


class TestEvalsPage:
    """The page whose whole job is honest measurement must not overstate itself."""

    CALLS = [
        signal_quality.DirectionalCall("DEMO1.LSE", "long", 0.05),
        signal_quality.DirectionalCall("DEMO2.LSE", "long", -0.02),
        signal_quality.DirectionalCall("DEMO3.US", "avoid", -0.04),
        # Abstentions. They make no directional claim, so the verdict excludes
        # them — and so must the tile that counts toward the same gate.
        signal_quality.DirectionalCall("DEMO4.US", "flat", 0.01),
        signal_quality.DirectionalCall("DEMO5.US", "flat", -0.01),
    ]

    @pytest.fixture()
    def rendered(self, conn, config, monkeypatch) -> _Recorder:
        monkeypatch.setattr(views.queries, "catalyst_calls", lambda *_a, **_kw: self.CALLS)
        recorder = _Recorder()
        views.evals(recorder, views.Context(conn=conn, config=config, mode="light"))
        return recorder

    def test_the_tile_counts_what_the_verdict_counts(self, rendered):
        """The tile read 5 against a 100-sample gate the verdict had seen 3 of."""
        verdicts = [c for c in rendered.captions if "calls [" in c]
        assert verdicts, "the hit-rate verdict was never rendered"
        counted_by_the_verdict = re.search(r"on (\d+) calls", verdicts[0]).group(1)
        assert rendered.tile_value("Scoreable catalyst calls") == counted_by_the_verdict
        assert counted_by_the_verdict == "3"

    def test_the_excluded_abstentions_are_named_rather_than_silently_dropped(self, rendered):
        """Two of five vanishing with no explanation is how a reader concludes
        the tile is broken — or worse, does not notice."""
        assert "2 flat not scored" in rendered.tile_delta("Scoreable catalyst calls")

    def test_the_kill_criteria_count_the_same_calls_as_the_tile(self, rendered):
        """Three widgets on one page, one definition of a sample."""
        gate = [h for h in rendered.html if "samples needed for a verdict" in h]
        assert gate, "the catalyst kill criterion was never rendered"
        assert re.search(r"has (\d+) of the 100", gate[0]).group(1) == "3"


class TestStreamlitExecutionMode:
    """A `pages` directory beside run.py changes how Streamlit runs the app.

    PagesManager computes `uses_pages_directory` once per process from whether
    that folder exists; when it is true the script runner calls `_mpa_v1()`
    instead of exec'ing the script, and v1's navigation — built from `pages/*.py`
    — cannot resolve a deep-linked URL, so it emits "Page not found" before
    falling through to our script. `st.navigation` then clears the flag for the
    rest of the process, so exactly the FIRST session after each server start is
    affected: the correct page renders under a modal that swallows clicks.
    """

    def test_no_pages_directory_sits_beside_the_entry_script(self):
        from sentinel.dashboard import run as run_module

        pages = pathlib.Path(run_module.__file__).parent / "pages"
        assert not pages.exists(), (
            f"{pages} puts Streamlit into v1 multi-page mode. This app builds "
            "its pages in views.PAGES; delete the directory."
        )

    def test_the_launcher_warns_when_one_appears(self, tmp_path):
        """The directory that caused this was empty and untracked, so no test
        over committed files could have seen it — git cannot store an empty
        directory. The launcher can, because it knows the real path."""
        from sentinel.cli import pages_directory_warning

        script = tmp_path / "run.py"
        script.write_text("")
        assert pages_directory_warning(script) is None

        (tmp_path / "pages").mkdir()          # empty is enough
        warning = pages_directory_warning(script)
        assert warning is not None
        assert "v1 multi-page app" in warning


class TestTunnelIsNotALocalSession:
    """A loopback bind implies "nobody else can reach this" only until a tunnel
    republishes it. `auth.decide` grants unprotected access on exactly that
    inference, so `cloudflared --url http://localhost:8501` in front of a plain
    `sentinel dashboard` would serve the whole portfolio, password-free, to
    anyone with the URL — under a sidebar notice reading "Running locally".
    """

    def test_loopback_alone_still_counts_as_local(self):
        from sentinel.cli import serves_as_local

        for address in ("localhost", "127.0.0.1", "::1"):
            assert serves_as_local(address, tunnel=False) is True

    def test_a_tunnel_revokes_that_however_local_the_bind_looks(self):
        from sentinel.cli import serves_as_local

        for address in ("localhost", "127.0.0.1", "::1"):
            assert serves_as_local(address, tunnel=True) is False

    def test_a_public_bind_was_never_local_with_or_without_a_tunnel(self):
        from sentinel.cli import serves_as_local

        assert serves_as_local("0.0.0.0", tunnel=False) is False
        assert serves_as_local("0.0.0.0", tunnel=True) is False

    def test_the_gate_then_refuses_to_serve_without_a_password(self):
        """The two halves joined up: --tunnel makes is_local False, and a
        non-local session with no password is refused, not warned."""
        from sentinel.cli import serves_as_local

        is_local = serves_as_local("127.0.0.1", tunnel=True)
        decision = auth.decide(
            configured_password=None, submitted=None, is_local=is_local
        )
        assert decision.outcome == "refused"
        assert not decision.may_render

    def test_a_password_is_demanded_rather_than_assumed_valid(self):
        from sentinel.cli import serves_as_local

        is_local = serves_as_local("127.0.0.1", tunnel=True)
        assert auth.decide(configured_password="s3cret", submitted=None,
                           is_local=is_local).outcome == "prompt"
        assert auth.decide(configured_password="s3cret", submitted="wrong",
                           is_local=is_local).outcome == "rejected"
        assert auth.decide(configured_password="s3cret", submitted="s3cret",
                           is_local=is_local).outcome == "granted"


class TestAcceptedMeansRulesAndRisk:
    """"Accepted" is the word this whole system turns on: it is what "ready to
    buy" means. It must mean cleared the rules layer AND approved by the risk
    layer — the layer the spec says no signal may override.

    The trap is that it is not computable from the stored idea. score_universe
    persists an idea BEFORE assess() runs, and assess returns its verdicts to the
    caller without writing them back (ideas are append-only, so it could not).
    Idea.risk is therefore always None on anything read back, which makes
    Idea.accepted always False. The audit trail is the only surviving record.
    """

    @pytest.fixture()
    def scored(self, conn, config):
        from sentinel.storage import audit

        idea_ids = {}
        for ticker, rules_ok, risk_ok in (
            ("PASS.US", True, True),        # the only one that is ready to buy
            ("RISKED.US", True, False),     # rules cleared, RISK REFUSED
            ("RULED.US", False, True),      # rules refused
        ):
            item = _idea(ticker, rejected=() if rules_ok else ("R6",))
            repo.save_idea(conn, item)
            idea_ids[ticker] = item.id
            audit.record(
                conn,
                audit.AuditEvent.RISK_APPROVED if risk_ok
                else audit.AuditEvent.RISK_CHECK_FAILED,
                ticker=ticker,
                payload={"idea_id": item.id,
                         **({} if risk_ok else {"reasons": ["max single position 10%"]})},
            )
        return idea_ids

    def test_an_idea_the_risk_layer_refused_is_not_accepted(self, conn, config, scored):
        """This is the regression. `not rejected_by_rules` called it accepted."""
        frame = queries.ideas_frame(conn).set_index("ticker")
        assert frame.loc["RISKED.US", "rules_ok"], "the rules layer did clear it"
        assert bool(frame.loc["RISKED.US", "risk_ok"]) is False
        assert not frame.loc["RISKED.US", "accepted"], (
            "an idea the risk layer refused was reported as accepted"
        )

    def test_only_clearing_both_layers_counts(self, conn, config, scored):
        frame = queries.ideas_frame(conn).set_index("ticker")
        assert frame.loc["PASS.US", "accepted"]
        assert not frame.loc["RULED.US", "accepted"]

    def test_the_search_verdict_names_the_layer_that_refused(self, conn, config, scored):
        verdict = queries.verdict_for(conn, "RISKED.US")
        assert verdict.stance == "AVOID"
        assert not verdict.ready_to_buy
        assert any("risk layer" in b for b in verdict.blockers), verdict.blockers
        assert any("max single position" in b for b in verdict.blockers), verdict.blockers

    def test_a_ticker_with_no_idea_gets_no_opinion_rather_than_a_guess(self, conn, config):
        verdict = queries.verdict_for(conn, "UNKNOWN.US")
        assert verdict.stance == "NOT SCORED"
        assert not verdict.ready_to_buy
        assert verdict.composite is None

    def test_the_verdict_applies_no_threshold_of_its_own(self, conn, config):
        """A low composite that cleared both layers is still a BUY. If this page
        second-guessed the pipeline it would become a competing opinion, and the
        brief and the dashboard could disagree about the same ticker."""
        from sentinel.storage import audit

        item = _idea("LOWSCORE.US", score=Decimal("31"))
        repo.save_idea(conn, item)
        audit.record(conn, audit.AuditEvent.RISK_APPROVED, ticker="LOWSCORE.US",
                     payload={"idea_id": item.id})
        verdict = queries.verdict_for(conn, "LOWSCORE.US")
        assert verdict.stance == "BUY"
        assert verdict.composite == pytest.approx(31.0)


def _idea(ticker: str, *, rejected: tuple[str, ...] = (), score: Decimal = Decimal("60")):
    from sentinel.domain.enums import Conviction, Direction
    from sentinel.domain.models import Idea, Signal

    return Idea(
        id=f"idea-{ticker}", created_at=dt.datetime(2026, 8, 20, tzinfo=dt.UTC),
        as_of=dt.date(2026, 8, 20), ticker=ticker,
        idea_class=IdeaClass.LONG_TERM, conviction=Conviction.MEDIUM,
        direction=Direction.LONG,
        signals=(Signal(module="technical", module_version="test-1", ticker=ticker,
                        as_of=dt.date(2026, 8, 20), score=score,
                        confidence=Decimal("0.8"), notes="fixture"),),
        composite_score=score, rejected_by_rules=rejected,
    )


class TestSmaCrosses:
    """Buy/sell *events* on the price chart — golden/death crosses, plotted
    where they happened. Deliberately not a prediction: the verdict banner is
    the system's opinion; these markers only report what the averages did."""

    def _frame(self, closes):
        import pandas as pd
        base = dt.date(2024, 1, 1)
        return pd.DataFrame({
            "date": [base + dt.timedelta(days=i) for i in range(len(closes))],
            "close": closes,
        })

    def test_a_cross_is_found_where_the_fast_average_overtakes(self):
        # 250 days: long decline (SMA50 below SMA200) then a strong recovery —
        # the recovery must produce exactly one golden cross.
        closes = [200 - i * 0.5 for i in range(220)] + [90 + i * 8 for i in range(80)]
        crosses = queries.sma_crosses(self._frame(closes))
        golden = crosses[crosses["kind"] == "golden"]
        assert len(golden) == 1
        assert "Golden cross" in golden.iloc[0]["label"]

    def test_a_flat_series_has_no_crosses(self):
        crosses = queries.sma_crosses(self._frame([100.0] * 300))
        assert crosses.empty

    def test_too_little_history_yields_none_rather_than_noise(self):
        """With under 200 bars there is no SMA200; inventing crosses from a
        partial window would be a signal fabricated from missing data."""
        closes = [200 - i * 0.5 for i in range(150)]
        assert queries.sma_crosses(self._frame(closes)).empty

    def test_the_marker_carries_the_price_it_sits_at(self):
        closes = [200 - i * 0.5 for i in range(220)] + [90 + i * 8 for i in range(80)]
        frame = self._frame(closes)
        crosses = queries.sma_crosses(frame)
        row = crosses.iloc[0]
        at = frame[frame["date"] == row["date"]]["close"].iloc[0]
        assert row["close"] == at, "marker must sit on the close, not on an average"

    def test_the_chart_accepts_and_layers_the_markers(self):
        import pandas as pd
        closes = [200 - i * 0.5 for i in range(220)] + [90 + i * 8 for i in range(80)]
        frame = self._frame(closes)
        chart = charts.price_history(frame, "light", crosses=queries.sma_crosses(frame))
        spec = chart.to_dict()
        assert "layer" in spec, "markers must be layered onto the price chart"

    def test_without_markers_the_chart_is_unchanged(self):
        import pandas as pd
        frame = self._frame([100.0 + i for i in range(260)])
        empty = queries.sma_crosses(self._frame([100.0] * 300))
        with_none = charts.price_history(frame, "light")
        with_empty = charts.price_history(frame, "light", crosses=empty)
        assert with_none.to_dict() == with_empty.to_dict()


class TestNewsFrame:
    """The news was captured at every ingest and scored by the sentiment
    module — and shown to nobody. This is the read side."""

    def test_stored_headlines_come_back_newest_first(self, conn):
        from sentinel.domain.models import NewsItem
        now = dt.datetime.now(dt.UTC)
        repo.save_news(conn, [
            NewsItem(ticker="NVDA.US", published_at=now - dt.timedelta(days=2),
                     headline="Older", source="Reuters", url="https://x.test/1"),
            NewsItem(ticker="NVDA.US", published_at=now - dt.timedelta(days=1),
                     headline="Newer", source="FT", url="https://x.test/2"),
        ])
        frame = queries.news_frame(conn, "NVDA.US")
        assert frame["headline"].tolist() == ["Newer", "Older"]
        assert set(frame.columns) == {"published", "headline", "source", "url"}

    def test_stale_news_is_outside_the_window(self, conn):
        from sentinel.domain.models import NewsItem
        repo.save_news(conn, [NewsItem(
            ticker="NVDA.US",
            published_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=40),
            headline="Ancient", source="", url="")])
        assert queries.news_frame(conn, "NVDA.US", days=14).empty

    def test_another_tickers_news_does_not_bleed_in(self, conn):
        from sentinel.domain.models import NewsItem
        repo.save_news(conn, [NewsItem(
            ticker="AMD.US", published_at=dt.datetime.now(dt.UTC),
            headline="About AMD", source="", url="")])
        assert queries.news_frame(conn, "NVDA.US").empty


class TestMemoAbsenceReason:
    """The detail page used to show ONE static sentence ('Without an LLM
    configured …') for every memo-less idea — a guess. The pipeline has three
    distinct reasons and records enough in the audit trail to tell them apart;
    two of them mean the opposite of "not configured"."""

    def _idea(self, score, ticker="NVDA.US"):
        from sentinel.analysis import synthesis
        from sentinel.domain.models import Signal

        return synthesis.build_idea(
            ticker,
            [Signal(module="technical", module_version="test-1", ticker=ticker,
                    as_of=dt.date(2026, 8, 23), score=Decimal(score))],
            dt.date(2026, 8, 23),
        )

    def test_a_memo_carrying_idea_needs_no_reason(self, conn):
        idea = self._idea(70)
        assert idea.memo is None  # build_idea without memo
        # (the None-memo branch is the subject; a memo'd idea short-circuits)
        from sentinel.domain.models import IdeaMemo
        memod = idea.model_copy(update={"memo": IdeaMemo(
            ticker="NVDA.US", thesis="t", bull_case="b", bear_case="b",
            invalidation="price < 100", idea_class="long_term",
            conviction="medium", horizon_days=90)})
        assert queries.memo_absence_reason(conn, memod) is None

    def test_below_the_bar_is_named_as_below_the_bar(self, conn):
        reason = queries.memo_absence_reason(conn, self._idea(38))
        assert "below the" in reason and "50" in reason
        assert "ANTHROPIC_API_KEY" not in reason, (
            "an idea the pipeline never asks the LLM about must not tell the "
            "user to configure the LLM")

    def test_a_recorded_llm_failure_is_reported_as_the_failure_it_was(self, conn):
        from sentinel.storage import audit
        audit.record(conn, audit.AuditEvent.LLM_SCHEMA_FAILURE, ticker="NVDA.US",
                     payload={"error": "401 authentication_error"},
                     at=dt.datetime(2026, 8, 23, 9, 0, tzinfo=dt.UTC))
        reason = queries.memo_absence_reason(conn, self._idea(70))
        assert "FAILED" in reason and "401 authentication_error" in reason

    def test_another_days_failure_does_not_explain_this_run(self, conn):
        from sentinel.storage import audit
        audit.record(conn, audit.AuditEvent.LLM_SCHEMA_FAILURE, ticker="NVDA.US",
                     payload={"error": "old"},
                     at=dt.datetime(2026, 7, 1, 9, 0, tzinfo=dt.UTC))
        reason = queries.memo_absence_reason(conn, self._idea(70))
        assert "old" not in reason
        assert "no LLM was configured" in reason

    def test_no_failure_and_over_the_bar_means_no_key(self, conn):
        reason = queries.memo_absence_reason(conn, self._idea(70))
        assert "ANTHROPIC_API_KEY" in reason and "re-run" in reason


class TestAnyTickerSearch:
    """"SOFI doesn't return anything": search only listed tickers past ingests
    happened to cover, with no path from 'not here' to 'here'."""

    def test_a_bare_symbol_is_read_as_a_us_listing(self):
        assert queries.normalize_ticker("sofi") == "SOFI.US"
        assert queries.normalize_ticker(" SOFI ") == "SOFI.US"

    def test_an_explicit_suffix_is_respected(self):
        """Currency inference hangs off the suffix — force-appending .US to
        VOD.LSE would price a GBP stock in dollars."""
        assert queries.normalize_ticker("vod.lse") == "VOD.LSE"

    def test_empty_input_stays_empty(self):
        assert queries.normalize_ticker("   ") == ""


class TestReportsListing:
    def test_newest_first_and_markdown_only(self, tmp_path):
        (tmp_path / "2026-08-20.md").write_text("old")
        (tmp_path / "weekly-2026-08-24.md").write_text("newer")
        (tmp_path / "2026-08-24.md").write_text("new")
        (tmp_path / "readout-2026-08-24.html").write_text("<html>")
        names = [p.name for p in queries.list_reports(tmp_path)]
        assert names == ["weekly-2026-08-24.md", "2026-08-24.md", "2026-08-20.md"]

    def test_a_missing_directory_is_empty_not_a_crash(self, tmp_path):
        assert queries.list_reports(tmp_path / "nope") == []


class TestConvictionBoard:
    """"Show me the strong buys" — with the spec's constraint intact: a signal
    never appears without its justification, and a high score that failed a
    layer is not a buy at any threshold."""

    def _idea(self, ticker, score, *, rejected=()):
        from sentinel.analysis import synthesis
        from sentinel.domain.models import Signal

        built = synthesis.build_idea(
            ticker,
            [Signal(module="technical", module_version="test-1", ticker=ticker,
                    as_of=dt.date.today(), score=Decimal(score))],
            dt.date.today(),
        )
        return built.model_copy(update={"rejected_by_rules": tuple(rejected)})

    def _approve(self, conn, idea):
        from sentinel.storage import audit
        audit.record(conn, audit.AuditEvent.RISK_APPROVED, ticker=idea.ticker,
                     payload={"idea_id": idea.id, "shares": 1})

    def _refuse(self, conn, idea):
        from sentinel.storage import audit
        audit.record(conn, audit.AuditEvent.RISK_CHECK_FAILED, ticker=idea.ticker,
                     payload={"idea_id": idea.id, "reasons": ["x"]})

    def test_only_accepted_ideas_qualify_however_high_the_score(self, conn):
        rules_reject = self._idea("A.US", 95, rejected=("R1: too good",))
        risk_reject = self._idea("B.US", 93)
        clean = self._idea("C.US", 85)
        for idea in (rules_reject, risk_reject, clean):
            repo.save_idea(conn, idea)
        self._refuse(conn, risk_reject)
        self._approve(conn, clean)
        self._approve(conn, rules_reject)  # risk said yes; rules already said no

        qualifying, _ = queries.conviction_board(conn, min_score=80)
        assert [i.ticker for i in qualifying] == ["C.US"], (
            "a 95 that failed a layer outranked an 85 that passed everything")

    def test_the_bar_is_a_bar(self, conn):
        low, high = self._idea("A.US", 79), self._idea("B.US", 80)
        for idea in (low, high):
            repo.save_idea(conn, idea)
            self._approve(conn, idea)
        qualifying, near = queries.conviction_board(conn, min_score=80)
        assert [i.ticker for i in qualifying] == ["B.US"]
        assert near is not None and near.ticker == "A.US", (
            "the empty-state near-miss must name the best idea UNDER the bar")

    def test_sorted_best_first(self, conn):
        for ticker, score in (("A.US", 81), ("B.US", 92), ("C.US", 85)):
            idea = self._idea(ticker, score)
            repo.save_idea(conn, idea)
            self._approve(conn, idea)
        qualifying, _ = queries.conviction_board(conn, min_score=80)
        assert [i.ticker for i in qualifying] == ["B.US", "C.US", "A.US"]

    def test_only_the_latest_idea_per_ticker_counts(self, conn):
        import datetime as dtm
        from sentinel.analysis import synthesis
        from sentinel.domain.models import Signal

        def at(day, score):
            return synthesis.build_idea(
                "A.US",
                [Signal(module="technical", module_version="test-1", ticker="A.US",
                        as_of=day, score=Decimal(score))],
                day,
            )
        stale = at(dtm.date.today() - dtm.timedelta(days=3), 95)
        fresh = at(dtm.date.today(), 60)
        for idea in (stale, fresh):
            repo.save_idea(conn, idea)
            self._approve(conn, idea)
        qualifying, near = queries.conviction_board(conn, min_score=80)
        assert qualifying == [], (
            "a stale 95 must not outlive the fresh 60 that superseded it")
        assert near is not None and near.composite_score == Decimal("60")

    def test_an_empty_database_yields_nothing_and_no_near_miss(self, conn):
        qualifying, near = queries.conviction_board(conn, min_score=80)
        assert qualifying == [] and near is None


class TestTodayStatus:
    """The landing page's one question: "what do I do now?" — the status dict
    behind it, asserted in both fresh and stale states."""

    def test_an_empty_database_says_begin_at_the_beginning(self, conn, tmp_path):
        status = queries.today_status(conn, tmp_path)
        assert status["tickers"] == 0
        assert status["last_bar"] is None
        assert status["brief_today"] is False
        assert status["open_positions"] == 0

    def test_fresh_data_and_todays_brief_are_recognised(self, conn, tmp_path):
        from decimal import Decimal as D
        from sentinel.domain.models import Bar

        today = dt.date.today()
        repo.save_bars(conn, [Bar(ticker="A.US", date=today, open=D("1"),
                                  high=D("1"), low=D("1"), close=D("1"),
                                  adjusted_close=D("1"), volume=1,
                                  currency="USD")], source="t")
        (tmp_path / f"{today.isoformat()}.md").write_text("# brief")
        status = queries.today_status(conn, tmp_path)
        assert status["last_bar"] == today
        assert status["data_age_days"] == 0
        assert status["brief_today"] is True

    def test_yesterdays_brief_does_not_count_as_todays(self, conn, tmp_path):
        yesterday = dt.date.today() - dt.timedelta(days=1)
        (tmp_path / f"{yesterday.isoformat()}.md").write_text("# old")
        assert queries.today_status(conn, tmp_path)["brief_today"] is False

    def test_a_position_below_its_stop_is_counted_as_trouble(self, conn, tmp_path):
        from decimal import Decimal as D
        from sentinel.domain.models import Bar, Position

        today = dt.date.today()
        repo.save_bars(conn, [Bar(ticker="A.US", date=today, open=D("50"),
                                  high=D("50"), low=D("50"), close=D("50"),
                                  adjusted_close=D("50"), volume=1,
                                  currency="USD")], source="t")
        repo.save_position(conn, Position(
            ticker="A.US", idea_id="manual", idea_class="long_term",
            sector="tech", opened_on=today, shares=1, entry=D("80"),
            stop=D("60")))
        status = queries.today_status(conn, tmp_path)
        assert status["open_positions"] == 1
        assert status["positions_below_stop"] == 1, (
            "mark 50 under stop 60 is the brief's Action-needed case — the "
            "landing page must not show a calm zero")


class TestFavourites:
    """The watchlist behind "Your favourites" — stored in the database, so it
    survives restarts and travels with the data."""

    def test_add_list_remove_round_trip(self, conn):
        repo.add_favourite(conn, "sofi.us")
        repo.add_favourite(conn, "NVDA.US")
        assert repo.list_favourites(conn) == ["NVDA.US", "SOFI.US"], (
            "stored upper-cased and listed sorted")
        repo.add_favourite(conn, "SOFI.US")  # starring twice is idempotent
        assert len(repo.list_favourites(conn)) == 2
        repo.remove_favourite(conn, "NVDA.US")
        assert repo.list_favourites(conn) == ["SOFI.US"]

    def test_overview_carries_price_move_and_score(self, conn):
        from decimal import Decimal as D
        from sentinel.domain.models import Bar

        today = dt.date.today()
        repo.add_favourite(conn, "A.US")
        repo.save_bars(conn, [
            Bar(ticker="A.US", date=today - dt.timedelta(days=1), open=D("100"),
                high=D("100"), low=D("100"), close=D("100"),
                adjusted_close=D("100"), volume=1, currency="USD"),
            Bar(ticker="A.US", date=today, open=D("110"), high=D("110"),
                low=D("110"), close=D("110"), adjusted_close=D("110"),
                volume=1, currency="USD"),
        ], source="t")
        rows = queries.favourites_overview(conn)
        assert len(rows) == 1
        row = rows[0]
        assert row["last_close"] == 110.0
        assert abs(row["change_1d"] - 0.10) < 1e-9
        assert row["score"] is None and row["accepted"] is False

    def test_a_pre_watchlist_database_degrades_to_empty_not_a_crash(self, tmp_path):
        """The dashboard connection is read-only and cannot migrate, so a
        database created before the watchlist table must not take the landing
        page down."""
        import sqlite3 as s
        old_db = tmp_path / "old.sqlite"
        c = s.connect(old_db)
        c.row_factory = s.Row
        assert queries.favourites_overview(c) == []


class TestCompanyName:
    def test_round_trips_through_storage_and_falls_back_to_none(self, conn):
        from decimal import Decimal as D
        from sentinel.domain.models import Fundamentals

        repo.save_fundamentals(conn, [Fundamentals(
            ticker="ANET.US", as_of=dt.date(2026, 8, 1), currency="USD",
            company_name="Arista Networks", revenue_ttm=D("1"))], source="t")
        assert queries.company_name(conn, "ANET.US") == "Arista Networks"
        assert queries.company_name(conn, "NOPE.US") is None


class TestCandlestick:
    """The candle view: real OHLC geometry, CVD-safe direction colours, one
    y-axis, and the honest empty state."""

    def _frame(self, days=30):
        start = dt.date(2026, 6, 1)
        rows = []
        for i in range(days):
            base = 100 + i * 0.5
            rows.append({"date": start + dt.timedelta(days=i),
                         "open": base, "high": base + 2, "low": base - 2,
                         "close": base + (1 if i % 2 == 0 else -1),
                         "volume": 1_000 + i})
        return pd.DataFrame(rows)

    def test_wicks_span_low_to_high_and_bodies_open_to_close(self):
        spec = charts.candlestick(self._frame(), "light").to_dict()
        layers = spec["layer"]
        wick, body = layers[0], layers[1]
        assert wick["mark"]["type"] == "rule"
        assert wick["encoding"]["y"]["field"] == "low"
        assert wick["encoding"]["y2"]["field"] == "high"
        assert body["mark"]["type"] == "bar"
        assert body["encoding"]["y"]["field"] == "open"
        assert body["encoding"]["y2"]["field"] == "close"

    def test_direction_wears_the_diverging_poles_not_green_red(self):
        """Up/down is polarity, and green/red is the classic CVD trap — the
        bodies must take the validated diverging pair instead."""
        p = pal.get("light")
        spec = charts.candlestick(self._frame(), "light").to_dict()
        body_scale = spec["layer"][1]["encoding"]["color"]["scale"]
        assert set(body_scale["range"]) == {p.diverging[0], p.diverging[2]}

    def test_the_averages_derive_from_the_close_so_one_axis_holds(self):
        frame = self._frame(days=60)
        spec = charts.candlestick(frame, "light", sma=(20, 50)).to_dict()
        lines = spec["layer"][2]
        assert lines["mark"]["type"] == "line"
        domain = lines["encoding"]["color"]["scale"]["domain"]
        assert domain == ["SMA 20", "SMA 50"]
        # Independent scales: the SMA legend must not swallow the candle
        # direction encoding, nor repaint candles with series hues.
        assert spec["resolve"]["scale"]["color"] == "independent"

    def test_volume_rides_the_tooltip_never_a_second_axis(self):
        spec = charts.candlestick(self._frame(), "light").to_dict()
        tips = {t["field"] for t in spec["layer"][1]["encoding"]["tooltip"]}
        assert "volume" in tips
        for layer in spec["layer"]:
            enc = layer.get("encoding", {})
            assert enc.get("y", {}).get("field") != "volume"

    def test_empty_history_renders_the_reason(self):
        spec = charts.candlestick(pd.DataFrame(), "light").to_dict()
        assert spec["mark"]["type"] == "text"


class TestScoreLeaders:
    """The Today leaderboard: magnitude on one hue, sorted best first, with
    the notable-70 rule the digest already uses."""

    def _frame(self):
        return pd.DataFrame([
            {"ticker": "B.US", "name": "Bravo", "score": 92.0,
             "conviction": "high", "as_of": dt.date(2026, 8, 21)},
            {"ticker": "A.US", "name": "Alpha", "score": 81.0,
             "conviction": "medium", "as_of": dt.date(2026, 8, 21)},
            {"ticker": "C.US", "name": "Charlie", "score": 64.0,
             "conviction": "low", "as_of": dt.date(2026, 8, 21)},
        ])

    def test_sorted_best_first_on_a_full_0_100_scale(self):
        spec = charts.score_leaders(self._frame(), "light").to_dict()
        bars = spec["layer"][0]
        assert bars["encoding"]["y"]["sort"] == ["B.US", "A.US", "C.US"]
        assert bars["encoding"]["x"]["scale"]["domain"] == [0, 100]

    def test_one_hue_not_categorical(self):
        """One measure on one scale: identity is the y label's job, so a
        per-ticker hue would burn the colour channel restating it."""
        spec = charts.score_leaders(self._frame(), "light").to_dict()
        assert "color" not in spec["layer"][0]["encoding"]
        assert spec["layer"][0]["mark"]["color"] == pal.get("light").slot(0)

    def test_carries_the_notable_bar_as_a_rule(self):
        spec = charts.score_leaders(self._frame(), "light", bar=70).to_dict()
        rules = [l for l in spec["layer"] if l["mark"].get("type") == "rule"]
        assert rules and rules[0]["encoding"]["x"]["field"] == "bar"
        assert any(rows == [{"bar": 70}] for rows in spec["datasets"].values())

    def test_the_tooltip_names_the_company(self):
        spec = charts.score_leaders(self._frame(), "light").to_dict()
        tips = {t["field"] for t in spec["layer"][0]["encoding"]["tooltip"]}
        assert {"name", "score", "conviction"} <= tips

    def test_empty_says_what_to_do(self):
        spec = charts.score_leaders(pd.DataFrame(), "light").to_dict()
        assert spec["mark"]["type"] == "text"


class TestOhlcAndLeaderQueries:
    def test_ohlc_frame_carries_the_raw_prints(self, populated):
        frame = queries.ohlc_frame(populated, "DEMO1.LSE", days=30)
        assert list(frame.columns) == ["date", "open", "high", "low", "close", "volume"]
        assert len(frame) == 30
        assert (frame["high"] >= frame["low"]).all()

    def test_ohlc_frame_is_empty_with_named_columns_for_a_stranger(self, conn):
        frame = queries.ohlc_frame(conn, "NOPE.US")
        assert frame.empty and "open" in frame.columns

    def test_top_ideas_frame_is_the_conviction_board_in_miniature(self, conn):
        """Same gate as the board: an unapproved 95 must not lead the Today
        chart that an approved 85 earned."""
        helper = TestConvictionBoard()
        strong_unapproved = helper._idea("X.US", 95)
        approved = helper._idea("Y.US", 85)
        for idea in (strong_unapproved, approved):
            repo.save_idea(conn, idea)
        helper._approve(conn, approved)

        frame = queries.top_ideas_frame(conn, limit=5)
        assert frame["ticker"].tolist() == ["Y.US"]
        assert frame["score"].iloc[0] == 85.0
        # Name falls back to the ticker when fundamentals never carried one.
        assert frame["name"].iloc[0] == "Y.US"

    def test_top_ideas_frame_caps_at_the_limit_best_first(self, conn):
        helper = TestConvictionBoard()
        for ticker, score in (("A.US", 61), ("B.US", 92), ("C.US", 85),
                              ("D.US", 70), ("E.US", 66), ("F.US", 88)):
            idea = helper._idea(ticker, score)
            repo.save_idea(conn, idea)
            helper._approve(conn, idea)
        frame = queries.top_ideas_frame(conn, limit=5)
        assert frame["ticker"].tolist() == ["B.US", "F.US", "C.US", "D.US", "E.US"]


class TestSearchByName:
    """"should also be able to search by stock name not just ticker": the
    option labels carry both spellings, and free text is classified as
    ticker-shaped or name-shaped."""

    def test_options_carry_ticker_and_name_and_map_back_to_the_ticker(self, conn):
        from decimal import Decimal as D
        from sentinel.domain.models import Bar, Fundamentals

        day = dt.date(2026, 8, 21)
        repo.save_bars(conn, [Bar(ticker="RKLB.US", date=day, open=D("40"),
                                  high=D("41"), low=D("39"), close=D("40.5"),
                                  adjusted_close=D("40.5"), volume=1,
                                  currency="USD")], source="t")
        repo.save_bars(conn, [Bar(ticker="NONAME.US", date=day, open=D("1"),
                                  high=D("1"), low=D("1"), close=D("1"),
                                  adjusted_close=D("1"), volume=1,
                                  currency="USD")], source="t")
        repo.save_fundamentals(conn, [Fundamentals(
            ticker="RKLB.US", as_of=day, currency="USD",
            company_name="Rocket Lab USA Inc", revenue_ttm=D("1"))], source="t")

        options = queries.search_options(conn)
        assert options["RKLB.US — Rocket Lab USA Inc"] == "RKLB.US"
        # A stock with no stored name is listed by ticker, not hidden.
        assert options["NONAME.US"] == "NONAME.US"

    def test_ticker_shaped_vs_name_shaped(self):
        for text in ("SOFI", "sofi", "brk.b", "VOD.LSE", "RKLB"):
            assert queries.looks_like_ticker(text), text
        for text in ("Rocket Lab", "NVIDIA", "rocket lab usa", ""):
            assert not queries.looks_like_ticker(text), text


class TestCandlestickLevels:
    """The trade plan drawn on the chart: stop solid critical, targets dashed
    good, every rule labelled — a status colour never carries meaning alone."""

    def _levels(self):
        return pd.DataFrame([
            {"label": "Entry 100.00", "value": 100.0, "kind": "entry"},
            {"label": "Stop 94.00", "value": 94.0, "kind": "stop"},
            {"label": "Target 1R 106.00", "value": 106.0, "kind": "target"},
            {"label": "Target 2R 112.00", "value": 112.0, "kind": "target"},
        ])

    def _ohlc(self):
        helper = TestCandlestick()
        return helper._frame(days=30)

    def test_levels_add_rules_in_status_colours_with_labels(self):
        p = pal.get("light")
        spec = charts.candlestick(self._ohlc(), "light",
                                  levels=self._levels()).to_dict()
        assert len(spec["layer"]) == 5
        rules = spec["layer"][3]
        assert rules["mark"]["type"] == "rule"
        scale = rules["encoding"]["color"]["scale"]
        mapping = dict(zip(scale["domain"], scale["range"]))
        assert mapping["stop"] == p.status["critical"]
        assert mapping["target"] == p.status["good"]
        # Dashed = aspiration (targets); solid = hard limit (stop).
        dash = dict(zip(rules["encoding"]["strokeDash"]["scale"]["domain"],
                        rules["encoding"]["strokeDash"]["scale"]["range"]))
        assert dash["target"] != [1, 0] and dash["stop"] == [1, 0]
        labels = spec["layer"][4]
        assert labels["mark"]["type"] == "text"
        assert labels["encoding"]["text"]["field"] == "label"

    def test_no_levels_means_no_extra_layers(self):
        spec = charts.candlestick(self._ohlc(), "light").to_dict()
        assert len(spec["layer"]) == 3
        spec = charts.candlestick(self._ohlc(), "light",
                                  levels=pd.DataFrame()).to_dict()
        assert len(spec["layer"]) == 3


class TestChartCarriesTheStockName:
    """"we have no idea what stock that is": a chart seen without its page
    header (a scroll position, a screenshot) must name its own subject."""

    def test_candlestick_title_names_the_stock(self):
        frame = TestCandlestick()._frame(days=20)
        spec = charts.candlestick(frame, "light",
                                  title="NVIDIA Corporation (NVDA.US)").to_dict()
        assert spec["title"]["text"] == "NVIDIA Corporation (NVDA.US)"

    def test_price_history_title_names_the_stock(self):
        frame = pd.DataFrame([
            {"date": dt.date(2026, 8, 1) + dt.timedelta(days=i),
             "close": 100.0 + i, "volume": 1} for i in range(10)
        ])
        spec = charts.price_history(frame, "light",
                                    title="Rocket Lab USA (RKLB.US)").to_dict()
        assert spec["title"]["text"] == "Rocket Lab USA (RKLB.US)"

    def test_no_title_stays_untitled(self):
        frame = TestCandlestick()._frame(days=20)
        assert "title" not in charts.candlestick(frame, "light").to_dict()
