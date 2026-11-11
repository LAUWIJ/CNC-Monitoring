import time

from machine_monitor.store import Store


def _store(tmp_path):
    st = Store(str(tmp_path / "d.db"))
    st.register_machine("m1", "M1", "sim")
    return st


def _cycle(st, start, end, result="COMPLETED", program="P1", parts=1):
    st.db.execute("INSERT INTO cycles(machine_id,program,start_ts,end_ts,duration_s,run_s,pause_s,alarm_s,result,parts) "
                  "VALUES ('m1',?,?,?,?,?,0,0,?,?)", (program, start, end, end - start, end - start, result, parts))
    st.db.commit()


def test_summary_matches_live_calculation(tmp_path):
    st = _store(tmp_path)
    d0 = Store.day_start(time.time()) - 5 * 86400           # 5 days ago, local midnight
    t, states = d0, ["RUNNING", "IDLE", "PAUSED", "RUNNING", "IDLE"]
    k = 0
    while t < time.time() - 60:                              # a state change every 47 minutes for 5 days
        st.add_state("m1", states[k % 5], t, "P1")
        if states[k % 5] == "RUNNING":
            _cycle(st, t, t + 2000, "ABORTED" if k % 7 == 0 else "COMPLETED")
        t += 2820
        k += 1
    a, b = d0 + 3 * 3600, time.time()                        # starts mid-day, ends now (partial both ends)
    live = st.utilization(a, b)["m1"]
    before = st.summary(a, b)["m1"]
    assert st.refresh_daily() >= 4                            # complete days only, today is not summarised
    after = st.summary(a, b)["m1"]
    for s in live:
        assert abs(live[s] - before["utilization"][s]) < 1e-6
        assert abs(live[s] - after["utilization"][s]) < 1e-6
    assert before["parts"] == after["parts"] > 0 and before["aborted"] == after["aborted"] > 0
    assert st.refresh_daily() == 0                            # nothing new


def test_daily_rows_and_invalidate(tmp_path):
    st = _store(tmp_path)
    d0 = Store.day_start(time.time()) - 3 * 86400
    st.add_state("m1", "RUNNING", d0 + 3600, "P1")
    st.add_state("m1", "IDLE", d0 + 7200, "P1")
    _cycle(st, d0 + 3600, d0 + 7000)
    st.refresh_daily()
    days = st.daily(d0, Store.next_day(d0))["m1"]
    day = days[Store.day_key(d0)]
    assert day["states"]["RUNNING"] == 3600 and day["parts"] == 1
    st.invalidate_daily(d0)
    assert st.db.execute("SELECT COUNT(*) FROM daily_done").fetchone()[0] == 0
    assert st.refresh_daily() >= 3


def test_day_bounds_are_local_midnights():
    t = time.mktime((2026, 3, 29, 12, 0, 0, 0, 0, -1))      # DST change day in Europe (23 h)
    s = Store.day_start(t)
    e = Store.next_day(s)
    assert time.localtime(s)[3:6] == (0, 0, 0) and time.localtime(e)[3:6] == (0, 0, 0)
    assert time.localtime(e).tm_mday == 30
