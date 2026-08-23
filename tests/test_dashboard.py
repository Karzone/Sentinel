"""Phase 6: the read-only dashboard.

The chart tests assert *spec properties* — one y-axis, a legend, stable hues,
endpoint labels — rather than "a chart object was returned". A test that passes
with the chart rendering nothing is a defect, so each one reaches into the Vega
spec for the thing it claims to check.
"""

from __future__ import annotations

import datetime as dt
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
