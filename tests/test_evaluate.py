#!/usr/bin/env python3
"""状态机：只根据探针快照判级，不碰真网。策略用显式 Policy，不依赖本机 config.toml。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from watch import Policy, Result, Snapshot, evaluate, should_notify

DIRECT_IPS = ("203.0.113.10", "203.0.113.11")  # RFC 5737 文档用地址
POLICY = Policy(direct_ips=DIRECT_IPS, uplink_interface="en0")
DAILY_IP = "198.51.100.7"
HOMEBB_IP = "192.0.2.44"


def base(**kwargs) -> Snapshot:
    data = dict(
        clash_up=True,
        ai_group_type="Selector",
        ai_group_all=("RESIP-Claude",),
        ai_group_now="RESIP-Claude",
        tun_exclude=tuple(f"{ip}/32" for ip in DIRECT_IPS),
        direct_ifaces={ip: "en0" for ip in DIRECT_IPS},
        daily_ip=DAILY_IP,
        homebb_ip=HOMEBB_IP,
        ai_via_daily="fail_closed",
        ai_chain="Claude-Only",
        homebb_type="Fallback",
        homebb_all=("RESIP-A", "RESIP-B"),
        homebb_member_types=("Vless", "Vless"),
    )
    data.update(kwargs)
    return Snapshot(**data)


def ev(snap: Snapshot) -> Result:
    return evaluate(snap, POLICY)


class EvaluateTest(unittest.TestCase):
    def test_ok_when_homebb_and_daily_differ(self):
        r = ev(base())
        self.assertEqual((r.code, r.level), ("ok", "ok"))

    def test_leak_when_homebb_port_exits_same_ip_as_daily(self):
        r = ev(base(homebb_ip=DAILY_IP))
        self.assertEqual((r.code, r.level), ("leak_homebb_is_daily", "crit"))

    def test_leak_when_ai_chain_has_no_ai_group(self):
        r = ev(base(homebb_ip=None, ai_via_daily="ok", ai_chain="Proxies"))
        self.assertEqual((r.code, r.level), ("leak_ai_via_daily", "crit"))

    def test_fail_closed_homebb_down_is_warn_not_ok(self):
        r = ev(base(homebb_ip=None, ai_via_daily="fail_closed", ai_chain="Claude-Only"))
        self.assertEqual((r.code, r.level), ("homebb_down", "warn"))

    def test_clash_dead_beats_other_signals(self):
        r = ev(base(clash_up=False, homebb_ip=DAILY_IP))
        self.assertEqual(r.code, "clash_dead")

    def test_deadchain_broken_if_ai_group_has_extra_member(self):
        r = ev(base(ai_group_all=("RESIP-Claude", "DIRECT")))
        self.assertEqual((r.code, r.level), ("deadchain_broken", "crit"))

    def test_missing_ai_group_is_core_reloading_not_broken(self):
        r = ev(base(ai_group_type="", ai_group_all=(), ai_group_now=""))
        self.assertEqual((r.code, r.level), ("core_reloading", "warn"))

    def test_deadchain_broken_if_ai_group_not_selector(self):
        r = ev(base(ai_group_type="Fallback"))
        self.assertEqual(r.code, "deadchain_broken")

    def test_homebb_group_must_be_fallback(self):
        self.assertEqual(ev(base(homebb_type="Selector")).code, "deadchain_broken")

    def test_homebb_group_members_must_be_exact(self):
        r = ev(base(homebb_all=("RESIP-A", "RESIP-B", "DIRECT"), homebb_member_types=("Vless", "Vless", "Direct")))
        self.assertEqual(r.code, "deadchain_broken")
        r = ev(base(homebb_all=("RESIP-A",), homebb_member_types=("Vless",)))
        self.assertEqual(r.code, "deadchain_broken")

    def test_homebb_members_must_match_type(self):
        r = ev(base(homebb_member_types=("Vless", "Socks5")))
        self.assertEqual((r.code, r.level), ("deadchain_broken", "crit"))

    def test_direct_route_missing_when_ip_not_excluded(self):
        r = ev(base(tun_exclude=(f"{DIRECT_IPS[0]}/32",)))
        self.assertEqual(r.code, "direct_route_missing")
        self.assertIn(DIRECT_IPS[1], r.detail)

    def test_direct_route_missing_when_wrong_iface(self):
        r = ev(base(direct_ifaces={DIRECT_IPS[0]: "en0", DIRECT_IPS[1]: "utun1024"}))
        self.assertEqual(r.code, "direct_route_missing")

    def test_no_direct_ips_means_no_route_check(self):
        r = evaluate(base(tun_exclude=(), direct_ifaces={}), Policy(direct_ips=()))
        self.assertEqual(r.code, "ok")

    def test_daily_down(self):
        r = ev(base(daily_ip=None))
        self.assertEqual((r.code, r.level), ("daily_down", "crit"))

    def test_config_tampered_is_crit_and_beats_leak_checks(self):
        r = ev(base(lock_ok=False, lock_detail="Merge.yaml 内容变了", homebb_ip=DAILY_IP))
        self.assertEqual((r.code, r.level), ("config_tampered", "crit"))
        self.assertIn("Merge.yaml", r.detail)

    def test_provider_mode_homebb_members_unfixed(self):
        pol = Policy(homebb_members=(), homebb_member_type="")
        ok = base(homebb_all=("[home] node-x", "[home] node-y"), homebb_member_types=("Vless", "Socks5"),
                  tun_exclude=(), direct_ifaces={})
        self.assertEqual(evaluate(ok, pol).code, "ok")
        empty = base(homebb_all=(), homebb_member_types=(), tun_exclude=(), direct_ifaces={})
        self.assertEqual(evaluate(empty, pol).code, "deadchain_broken")

    def test_homebb_group_type_configurable(self):
        pol = Policy(homebb_group_type="URLTest", direct_ips=())
        snap = base(homebb_type="URLTest", tun_exclude=(), direct_ifaces={})
        self.assertEqual(evaluate(snap, pol).code, "ok")

    def test_policy_names_are_respected(self):
        pol = Policy(ai_group="AI", homebb_group="HOME", homebb_members=("H1",), homebb_member_type="Trojan")
        snap = base(ai_group_all=("HOME",), ai_chain="AI", homebb_all=("H1",), homebb_member_types=("Trojan",),
                    tun_exclude=(), direct_ifaces={})
        self.assertEqual(evaluate(snap, pol).code, "ok")


class NotifyTest(unittest.TestCase):
    R = 1800
    D = ("daily_down",)

    def notify(self, result, prev, last, now, alerted=None):
        return should_notify(result, prev, last, now, alerted_code=alerted, realert_secs=self.R, debounce=self.D)

    def test_changed_only_when_code_flips(self):
        r = Result("homebb_down", "warn")
        self.assertTrue(r.should_alert("ok"))
        self.assertFalse(r.should_alert("homebb_down"))

    def test_notify_on_flip(self):
        self.assertTrue(self.notify(Result("homebb_down", "warn"), "ok", 0.0, 1000.0))
        self.assertTrue(self.notify(Result("ok", "ok"), "homebb_down", 0.0, 1000.0))

    def test_no_repeat_when_ok(self):
        self.assertFalse(self.notify(Result("ok", "ok"), "ok", 0.0, 10**9))

    def test_repeat_only_after_realert_window(self):
        r = Result("homebb_down", "warn")
        self.assertFalse(self.notify(r, "homebb_down", 1000.0, 1000.0 + self.R - 1))
        self.assertTrue(self.notify(r, "homebb_down", 1000.0, 1000.0 + self.R))

    def test_debounced_first_sighting_is_observed_not_alerted(self):
        self.assertFalse(self.notify(Result("daily_down", "crit"), "ok", 0.0, 1000.0, alerted="ok"))

    def test_debounced_second_consecutive_round_alerts(self):
        self.assertTrue(self.notify(Result("daily_down", "crit"), "daily_down", 0.0, 1000.0, alerted="ok"))

    def test_blip_recovery_is_silent_if_never_alerted(self):
        self.assertFalse(self.notify(Result("ok", "ok"), "daily_down", 0.0, 1000.0, alerted="ok"))

    def test_recovery_notifies_after_real_alert(self):
        self.assertTrue(self.notify(Result("ok", "ok"), "daily_down", 0.0, 1000.0, alerted="daily_down"))

    def test_default_debounce_covers_reload_class(self):
        from config import Settings
        d = Settings().alert.debounce_codes
        for code in ("daily_down", "core_reloading", "clash_dead", "deadchain_broken"):
            self.assertIn(code, d)
        self.assertNotIn("config_tampered", d)
        self.assertNotIn("leak_ai_via_daily", d)

    def test_non_debounced_code_alerts_immediately(self):
        self.assertTrue(self.notify(Result("config_tampered", "crit"), "ok", 0.0, 1000.0, alerted="ok"))


if __name__ == "__main__":
    unittest.main()


class DailyDownDetailTest(unittest.TestCase):
    """日常出口断了的时候，要能一眼看出是本机上行断还是订阅线路断。"""

    def test_says_upstream_when_homebb_also_down(self):
        r = ev(base(daily_ip=None, homebb_ip=None))
        self.assertEqual(r.code, "daily_down")
        self.assertIn("上行", r.detail)

    def test_says_subscription_when_homebb_alive(self):
        r = ev(base(daily_ip=None, homebb_ip=HOMEBB_IP))
        self.assertEqual(r.code, "daily_down")
        self.assertIn("订阅线路", r.detail)
