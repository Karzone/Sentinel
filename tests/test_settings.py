"""The Settings write seam: a closed field set, edited as text, validated whole.

The properties worth pinning are the negative ones — what this module CANNOT do.
A settings page that could be persuaded to touch a risk limit would quietly
undo the rule that limits change only by editing the tracked file.
"""

from __future__ import annotations

import inspect
import tomllib

import pytest

from sentinel import settings as cfg
from sentinel.config import STARTER_CONFIG, Config, _decimalise, load_config


def _cfg(text: str) -> Config:
    return Config.model_validate(_decimalise(tomllib.loads(text)))


class TestParseWatchlist:
    def test_splits_uppercases_and_dedupes_in_order(self):
        got = cfg.parse_watchlist("nvda.us, AMD.US\nvwrp.lse; nvda.us")
        assert got == ("NVDA.US", "AMD.US", "VWRP.LSE")

    def test_a_bare_symbol_is_refused_with_the_form_to_use(self):
        with pytest.raises(cfg.SettingsError, match="SYMBOL.EXCHANGE"):
            cfg.parse_watchlist("NVDA")

    def test_injection_shaped_input_is_refused(self):
        with pytest.raises(cfg.SettingsError):
            cfg.parse_watchlist('NVDA.US"]\n[risk]\nmax_single_position_pct = 100')

    def test_the_cap_is_enforced_because_every_ticker_costs_llm_calls(self):
        many = [f"T{i}.US" for i in range(cfg.MAX_WATCHLIST + 1)]
        with pytest.raises(cfg.SettingsError, match="limit"):
            cfg.parse_watchlist(many)


class TestTheClosedFieldSet:
    def test_apply_changes_has_no_parameter_that_reaches_a_risk_limit(self):
        """The whole guarantee, as a signature. Adding a parameter here is a
        policy change and must come with a spec change, so this list is pinned."""
        params = set(inspect.signature(cfg.apply_changes).parameters) - {"text"}
        assert params == {"watchlist", "ntfy_topic", "email_to", "autopilot_enabled",
                          "daily_at", "weekly_at", "universe"}

    def test_every_edit_leaves_the_risk_block_byte_identical(self):
        before = STARTER_CONFIG
        after = cfg.apply_changes(
            before, watchlist=["NVDA.US"], ntfy_topic="a-long-unguessable-topic",
            email_to="me@example.com", autopilot_enabled=False, daily_at="06:30",
            weekly_at="19:00", universe="demo")

        def block(text: str) -> str:
            start = text.index("[risk]")
            return text[start:text.index("[data]")]

        assert block(after) == block(before)
        assert _cfg(after).risk == _cfg(before).risk
        assert _cfg(after).satellite_capital_gbp == _cfg(before).satellite_capital_gbp

    def test_comments_survive_an_edit(self):
        after = cfg.apply_changes(STARTER_CONFIG, watchlist=["NVDA.US"])
        assert "These are hard limits. Nothing" in after


class TestEdits:
    def test_watchlist_round_trips_through_the_real_loader(self, tmp_path):
        path = tmp_path / "sentinel.toml"
        path.write_text(STARTER_CONFIG, encoding="utf-8")
        assert cfg.save(path, watchlist=["nvda.us", "vwrp.lse"])
        assert load_config(path).watchlist == ("NVDA.US", "VWRP.LSE")

    def test_an_existing_multiline_watchlist_is_replaced_whole(self):
        text = STARTER_CONFIG.replace(
            "[risk]", 'watchlist = [\n  "OLD1.US",\n  "OLD2.US",\n]\n\n[risk]', 1)
        assert _cfg(text).watchlist == ("OLD1.US", "OLD2.US")
        after = cfg.apply_changes(text, watchlist=["NEW.US"])
        assert _cfg(after).watchlist == ("NEW.US",)
        assert "OLD1" not in after and "OLD2" not in after

    def test_an_inline_comment_on_a_schedule_line_is_kept(self):
        after = cfg.apply_changes(STARTER_CONFIG, daily_at="06:45")
        line = next(ln for ln in after.splitlines() if ln.startswith("daily_at"))
        assert '"06:45"' in line and "Mon-Fri" in line

    def test_a_missing_section_is_created(self):
        bare = STARTER_CONFIG[:STARTER_CONFIG.index("[autopilot]")] + \
            STARTER_CONFIG[STARTER_CONFIG.index("[paths]"):]
        assert "[autopilot]" not in bare
        after = cfg.apply_changes(bare, daily_at="06:00")
        assert _cfg(after).autopilot.daily_at == "06:00"

    def test_editing_twice_with_the_same_value_changes_nothing(self):
        once = cfg.apply_changes(STARTER_CONFIG, watchlist=["NVDA.US"], universe="demo")
        twice = cfg.apply_changes(once, watchlist=["NVDA.US"], universe="demo")
        assert once == twice

    def test_none_means_leave_alone(self):
        assert cfg.apply_changes(STARTER_CONFIG) == STARTER_CONFIG


class TestRejections:
    @pytest.mark.parametrize("bad", ["25:00", "7:00", "07:60", "noon", ""])
    def test_a_bad_clock_time_is_refused(self, bad):
        with pytest.raises(cfg.SettingsError, match="HH:MM"):
            cfg.apply_changes(STARTER_CONFIG, daily_at=bad)

    def test_an_unknown_universe_is_refused_and_named(self):
        with pytest.raises(cfg.SettingsError, match="unknown universe 'nope'"):
            cfg.apply_changes(STARTER_CONFIG, universe="nope")

    def test_a_short_ntfy_topic_is_refused_because_it_is_the_only_credential(self):
        with pytest.raises(cfg.SettingsError, match="12"):
            cfg.apply_changes(STARTER_CONFIG, ntfy_topic="sentinel")

    def test_an_email_without_an_at_is_refused(self):
        with pytest.raises(cfg.SettingsError, match="email"):
            cfg.apply_changes(STARTER_CONFIG, email_to="not-an-address")

    def test_a_rejected_save_leaves_the_file_byte_identical(self, tmp_path):
        path = tmp_path / "sentinel.toml"
        path.write_text(STARTER_CONFIG, encoding="utf-8")
        with pytest.raises(cfg.SettingsError):
            cfg.save(path, watchlist=["NVDA.US"], daily_at="99:99")
        assert path.read_text(encoding="utf-8") == STARTER_CONFIG
        assert not path.with_suffix(".toml.bak").exists()


class TestDisk:
    def test_a_save_keeps_one_backup_and_an_append_only_log(self, tmp_path):
        path = tmp_path / "sentinel.toml"
        path.write_text(STARTER_CONFIG, encoding="utf-8")
        cfg.save(path, watchlist=["NVDA.US"])
        cfg.save(path, watchlist=["AMD.US"])
        backup = path.with_suffix(".toml.bak").read_text(encoding="utf-8")
        assert _cfg(backup).watchlist == ("NVDA.US",)        # the version before the last edit
        log = (tmp_path / "settings-changes.log").read_text().splitlines()
        assert len(log) == 2 and all("watchlist updated" in line for line in log)

    def test_a_no_op_save_writes_nothing(self, tmp_path):
        path = tmp_path / "sentinel.toml"
        path.write_text(STARTER_CONFIG, encoding="utf-8")
        assert cfg.save(path, daily_at="07:00") == []
        assert not (tmp_path / "settings-changes.log").exists()
