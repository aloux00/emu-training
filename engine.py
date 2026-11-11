import calendar
from datetime import date, timedelta

import rules as rules_mod

GRAD_TYPE = "研究生汇报"   # 特殊课型：只有汇报人 + 题目，无老师/评审/指导


# ---------- 日期工具 ----------

def weekday_days(year, month, weekday, max_count=31):
    """当月所有指定星期几的日期（weekday: 0=周一 … 6=周日）。"""
    days = []
    cur = date(year, month, 1)
    while cur.month == month:
        if cur.weekday() == weekday:
            days.append(cur)
        cur += timedelta(days=1)
    return days[:max_count]


def get_tuesdays(year, month, max_count=31):
    """兼容旧接口：当月周二列表。"""
    return weekday_days(year, month, 1, max_count)


def _wd_set(weekdays):
    """把讲课日参数归一化为星期几集合（支持 int / list / tuple）。"""
    if isinstance(weekdays, int):
        weekdays = (weekdays,)
    return {int(w) for w in (weekdays or (1,))}


def weekdays_days(year, month, weekdays, max_count=31):
    """当月所有命中讲课日（可多选星期几）的日期，按日期升序。"""
    wd = _wd_set(weekdays)
    days = []
    cur = date(year, month, 1)
    while cur.month == month:
        if cur.weekday() in wd:
            days.append(cur)
        cur += timedelta(days=1)
    return days[:max_count]


def teaching_days(year, month, count=4, holidays=None, weekdays=(1,), policy="same_weekday"):
    """收集讲课日：先取本月命中多选星期几的日期，排除节假日；不足则按策略向后顺延，直至攒够 count 个。"""
    hol = set(holidays or [])
    wd = _wd_set(weekdays)
    days = []
    cur = date(year, month, 1)
    while cur.month == month and len(days) < count:
        if cur.weekday() in wd and cur.isoformat() not in hol:
            days.append(cur)
        cur += timedelta(days=1)

    if policy == "none":
        return days[:count]

    guard = 0
    while len(days) < count and guard < 400:
        guard += 1
        if cur.isoformat() not in hol:
            if policy == "next_workday":
                if cur.weekday() < 5:      # 周一~周五
                    days.append(cur)
            elif cur.weekday() in wd:      # same_weekday：顺延到下一个讲课日
                days.append(cur)
        cur += timedelta(days=1)
    return days[:count]


def count_skipped(year, month, holidays, weekdays=(1,)):
    """统计当月落在讲课日（可多选）上的节假日个数（用于生成后提示）。"""
    hol = set(holidays or [])
    wd = _wd_set(weekdays)
    return len([d for d in weekdays_days(year, month, wd) if d.isoformat() in hol])


def _parse_leave(leave_str):
    if not leave_str:
        return set()
    return {d.strip() for d in leave_str.split(",") if d.strip()}


# ---------- 选人 ----------

def _choose(pool_ids, ptr, covered_set, eligible_set):
    """在候选池(pool_ids, 固定顺序)中按全局轮转指针 ptr 选择一人。
      ① 轮转均匀：从 ptr 起向前扫描，选第一个 eligible 的人（环形），选后 ptr 前移。
      ② 覆盖偏置：优先选"尚未覆盖该课型"的人，保证人人逐步覆盖三种课型。
    返回 (选中的 id, 新的 ptr, 三人中是否有可用的)。"""
    n = len(pool_ids)
    if not eligible_set or n == 0:
        return None, ptr
    default = None
    uncovered_pick = None
    for step in range(n):
        idx = (ptr + step) % n
        pid = pool_ids[idx]
        if pid not in eligible_set:
            continue
        if default is None:
            default = pid
        if pid not in covered_set and uncovered_pick is None:
            uncovered_pick = pid
        if default is not None and uncovered_pick is not None:
            break
    chosen = uncovered_pick if uncovered_pick is not None else default
    pos = pool_ids.index(chosen)
    new_ptr = (pos + 1) % n
    return chosen, new_ptr


# ---------- 主流程 ----------

def generate_plan(year, month, teachers, students, class_types, history=None,
                  holidays=None, rotation=None, rules=None, graduates=None):
    """按规则生成某月课表。rules 缺省时使用 rules.DEFAULT_RULES（= 系统原有内定行为）。

    graduates：研究生名册，用于课型「研究生汇报」（只排汇报人 + 题目）。
    """
    r = rules_mod.merge_rules(rules)

    weekdays = r.get("weekdays") or [1]
    day_count = int(r["days_per_month"])
    slots_per_day = int(r["slots_per_day"])

    # ---- 一、讲课日 ----
    days = teaching_days(year, month, count=day_count, holidays=holidays,
                         weekdays=weekdays, policy=r["holiday_policy"])

    # ---- 二、课型编排 ----
    ct_map = {c["name"]: c for c in class_types}
    fallback_ct = class_types[0]

    def resolve(name):
        return ct_map.get(name) or fallback_ct

    seqs = r.get("slot_types") or []
    slots = []
    for wi, d in enumerate(days, start=1):
        for s in range(1, slots_per_day + 1):
            seq = seqs[s - 1] if s - 1 < len(seqs) else [fallback_ct["name"]]
            if not seq:
                seq = [fallback_ct["name"]]
            cname = seq[(wi - 1) % len(seq)]
            slots.append((wi, d, s, resolve(cname)))

    # ---- 三、候选池 ----
    t_pool = sorted([t for t in teachers if t.get("can_teach")], key=lambda x: x["id"])
    r_pool = sorted([t for t in teachers if t.get("can_review")], key=lambda x: x["id"])
    m_pool = sorted([t for t in teachers if t.get("can_mentor", True)], key=lambda x: x["id"])
    g_pool = sorted(graduates or [], key=lambda x: x["id"])
    g_by_id = {g["id"]: g for g in g_pool}
    g_ids = [g["id"] for g in g_pool]
    reader_src = r.get("reader_source", "student")
    if reader_src == "teacher":
        s_pool = list(t_pool)
    else:
        s_pool = sorted(students, key=lambda x: x["id"])

    t_ids = [t["id"] for t in t_pool]
    r_ids = [r["id"] for r in r_pool]
    m_ids = [t["id"] for t in m_pool]
    s_ids = [t["id"] for t in s_pool]
    if not t_ids:
        raise ValueError("没有可讲课的老师，请先在教师管理中勾选「可讲课」")
    if not r_ids:
        raise ValueError("没有可评审的老师，请先在教师管理中勾选「可评审」")
    if reader_src != "teacher" and not s_ids:
        raise ValueError("学生名册为空，无法分配主阅；或把「主阅来源」改为教师")
    by_id = {("t", x["id"]): x for x in t_pool}
    by_id.update({("r", x["id"]): x for x in r_pool})
    by_id.update({("m", x["id"]): x for x in m_pool})
    by_id.update({("s", x["id"]): x for x in s_pool})

    # ---- 四、轮转状态（跨月连续）----
    rot = rotation or {}
    teacher_ptr = int(rot.get("teacher", {}).get("ptr", 0)) % (len(t_ids) or 1)
    review_ptr = int(rot.get("review", {}).get("ptr", 0)) % (len(r_ids) or 1)
    mentor_ptr = int(rot.get("mentor", {}).get("ptr", 0)) % (len(m_ids) or 1)
    grad_ptr = int(rot.get("graduate", {}).get("ptr", 0)) % (len(g_ids) or 1)
    student_ptr = int(rot.get("student", {}).get("ptr", 0)) % (len(s_ids) or 1)

    cb = r.get("coverage_bias") or {}
    empty = set()
    teacher_cov = {c["name"]: (set(rot.get("teacher", {}).get("covered", {}).get(c["name"], [])) if cb.get("teacher", True) else set()) for c in class_types}
    review_cov = {c["name"]: (set(rot.get("review", {}).get("covered", {}).get(c["name"], [])) if cb.get("review", True) else set()) for c in class_types}
    student_cov = {c["name"]: (set(rot.get("student", {}).get("covered", {}).get(c["name"], [])) if cb.get("student", False) else set()) for c in class_types}
    mentor_cov = {c["name"]: (set(rot.get("mentor", {}).get("covered", {}).get(c["name"], [])) if cb.get("mentor", True) else set()) for c in class_types}

    # ---- 五、本月计数（每月上限）----
    limits = r.get("monthly_limit") or {}
    used = {"teacher": {}, "review": {}, "student": {}, "mentor": {}}

    def within(role, pid):
        lim = int(limits.get(role, 1) or 0)
        if lim <= 0:
            return True
        return used[role].get(pid, 0) < lim

    def bump(role, pid):
        used[role][pid] = used[role].get(pid, 0) + 1

    respect_leave = bool(r.get("respect_leave", True))

    def eligible(pool, role, ds, exclude=(), relax_limit=False):
        ids = set()
        for p in pool:
            pid = p["id"]
            if pid in exclude:
                continue
            if respect_leave and ds in _parse_leave(p.get("leave_dates", "")):
                continue
            if not relax_limit and not within(role, pid):
                continue
            ids.add(pid)
        return ids

    def pick(role, pool, ids, ds, cname, exclude=(), cov=None):
        """返回 (id, new_ptr)。逐级放宽：正常 → 放宽每月上限 → 忽略全部限制。"""
        e = eligible(pool, role, ds, exclude=exclude)
        if not e:
            e = eligible(pool, role, ds, exclude=exclude, relax_limit=True)
        if not e:
            e = set(ids)
        covered = (cov or {}).get(cname, empty)
        chosen, new_ptr = _choose(ids, _ptr[role], covered, e)
        _ptr[role] = new_ptr
        return chosen

    _ptr = {"teacher": teacher_ptr, "review": review_ptr, "student": student_ptr,
            "mentor": mentor_ptr, "graduate": grad_ptr}

    # ---- 六、逐场分配 ----
    result = []
    for (wi, d, slot, ctype) in slots:
        ds = d.isoformat()
        cname = ctype["name"]
        need_distinct = bool(r.get("distinct_roles", True))

        # 研究生汇报：只排汇报人（研究生名册轮转），无老师/评审/指导
        if cname == GRAD_TYPE:
            presenter_id = 0
            if g_ids:
                for _ in range(len(g_ids)):
                    cand = g_ids[_ptr["graduate"] % len(g_ids)]
                    _ptr["graduate"] = (_ptr["graduate"] + 1) % len(g_ids)
                    gd = g_by_id.get(cand) or {}
                    if respect_leave and ds in _parse_leave(gd.get("leave_dates", "")):
                        continue
                    presenter_id = cand
                    break
            result.append({
                "week_index": wi,
                "date": ds, "slot": slot,
                "class_type_id": ctype["id"], "class_type_name": cname,
                "teacher_id": 0, "main_reader_id": 0,
                "reviewer_id": 0, "mentor_id": 0,
                "presenter_id": presenter_id,
            })
            continue

        # 老师
        chosen_t = pick("teacher", t_pool, t_ids, ds, cname)
        bump("teacher", chosen_t)
        teacher_cov.setdefault(cname, set()).add(chosen_t)

        # 评审
        exclude_r = set()
        if need_distinct and chosen_t is not None:
            exclude_r.add(chosen_t)
        if r.get("reviewer_avoid_teacher_in_month", True):
            exclude_r |= {pid for pid, n in used["teacher"].items() if n > 0} - ({chosen_t} if chosen_t else set())
            exclude_r.add(chosen_t)
        chosen_r = pick("review", r_pool, r_ids, ds, cname, exclude=exclude_r)
        bump("review", chosen_r)
        review_cov.setdefault(cname, set()).add(chosen_r)

        # 主阅
        if cname == "小讲课" and r.get("small_lecture_reader_is_teacher", True):
            main_reader_id = chosen_t
        else:
            exclude_s = set()
            if need_distinct:
                exclude_s |= {chosen_t, chosen_r}
            chosen_s = pick("student", s_pool, s_ids, ds, cname, exclude=exclude_s, cov=student_cov)
            bump("student", chosen_s)
            student_cov.setdefault(cname, set()).add(chosen_s)
            main_reader_id = chosen_s

        # 指导老师（可选）：从「可指导」教师池轮转，避开本节老师/评审/主阅
        mentor_id = 0
        if r.get("mentor_enabled", True) and m_ids:
            exclude_m = set()
            if need_distinct:
                if chosen_t:
                    exclude_m.add(chosen_t)
                if chosen_r:
                    exclude_m.add(chosen_r)
            if main_reader_id and main_reader_id in set(m_ids):
                exclude_m.add(main_reader_id)
            chosen_m = pick("mentor", m_pool, m_ids, ds, cname, exclude=exclude_m, cov=mentor_cov)
            if chosen_m and chosen_m not in exclude_m:
                bump("mentor", chosen_m)
                mentor_cov.setdefault(cname, set()).add(chosen_m)
                mentor_id = chosen_m

        result.append({
            "week_index": wi,
            "date": ds, "slot": slot,
            "class_type_id": ctype["id"], "class_type_name": cname,
            "teacher_id": chosen_t, "main_reader_id": main_reader_id,
            "reviewer_id": chosen_r, "mentor_id": mentor_id,
        })

    # ---- 七、回传轮转状态 ----
    def _dump_cov(cov):
        return {k: sorted(v) for k, v in cov.items()}

    rotation_out = {
        "teacher": {"ptr": _ptr["teacher"], "covered": _dump_cov(teacher_cov)},
        "review": {"ptr": _ptr["review"], "covered": _dump_cov(review_cov)},
        "student": {"ptr": _ptr["student"], "covered": _dump_cov(student_cov)},
        "mentor": {"ptr": _ptr["mentor"], "covered": _dump_cov(mentor_cov)},
        "graduate": {"ptr": _ptr["graduate"], "covered": {}},
    }
    return result, rotation_out


if __name__ == "__main__":
    teachers = [{"id": i, "can_teach": True, "can_review": True, "can_mentor": True,
                 "leave_dates": ""} for i in range(1, 19)]
    students = [{"id": i, "leave_dates": ""} for i in range(100, 118)]
    graduates = [{"id": i, "leave_dates": ""} for i in range(200, 206)]
    cts = [{"id": 1, "name": "小讲课"}, {"id": 2, "name": "病例讨论"},
           {"id": 3, "name": "教学阅片"}, {"id": 4, "name": "研究生汇报"}]

    # 默认规则：每月 4 天 × 3 节（第3节研究生汇报），老师/评审每人每月至多 1 次
    rotation = None
    for (yy, mm) in [(2026, 11), (2026, 12), (2027, 1), (2027, 2)]:
        plan, rotation = generate_plan(yy, mm, teachers, students, cts,
                                       rotation=rotation, graduates=graduates)
        tcount, rcount = {}, {}
        for p in plan:
            if p["class_type_name"] == "研究生汇报":
                assert p["teacher_id"] == 0 and p["reviewer_id"] == 0, p
                assert p["presenter_id"] in {g["id"] for g in graduates}, p
                continue
            assert p["teacher_id"] != p["reviewer_id"], p
            if p["class_type_name"] == "小讲课":
                assert p["teacher_id"] == p["main_reader_id"], p
            else:
                assert p["main_reader_id"] >= 100, p
            assert p["mentor_id"] not in (0, p["teacher_id"], p["reviewer_id"]), p
            tcount[p["teacher_id"]] = tcount.get(p["teacher_id"], 0) + 1
            rcount[p["reviewer_id"]] = rcount.get(p["reviewer_id"], 0) + 1
        assert max(tcount.values()) <= 1, ("老师月度重复", tcount)
        assert max(rcount.values()) <= 1, ("评审月度重复", rcount)
        assert len(plan) == 12, len(plan)
        print(f"{yy}-{mm}: {len(plan)}节 老师 {sorted(tcount)} 评审 {sorted(rcount)}")
    print("默认规则 PASS（12节/月、每人每月至多1次、三角色互异、自动安排指导）")

    # 自定义规则：周三 + 每月2天 + 每天1节 + 每月上限2次 + 评审不限
    custom = rules_mod.merge_rules({
        "weekdays": [2], "days_per_month": 2, "slots_per_day": 1,
        "slot_types": [["病例讨论", "教学阅片"]],
        "monthly_limit": {"teacher": 2, "review": 0, "student": 0},
    })
    plan2, _ = generate_plan(2026, 11, teachers, students, cts, rules=custom)
    print("自定义规则:", [(p["week_index"], p["date"], p["class_type_name"]) for p in plan2])
    assert len(plan2) == 2
    assert all(date.fromisoformat(p["date"]).weekday() == 2 for p in plan2)

    # 多选讲课日：周二 + 周四，每月 4 天 → 应在两种星期几上取日
    custom_wd = rules_mod.merge_rules({"weekdays": [1, 3], "days_per_month": 4})
    plan_wd, _ = generate_plan(2026, 11, teachers, students, cts, rules=custom_wd)
    wd_hit = {date.fromisoformat(p["date"]).weekday() for p in plan_wd}
    print("多选讲课日(周二+周四):", [p["date"] for p in plan_wd])
    assert len(plan_wd) == 12                     # 4 天 × 每天 3 节
    assert len({p["date"] for p in plan_wd}) == 4  # 取满 4 个讲课日
    assert wd_hit <= {1, 3} and wd_hit, wd_hit

    # 旧版单选兼容：weekday: 3 → 自动映射 weekdays=[3]
    legacy = rules_mod.merge_rules({"weekday": 3})
    assert legacy["weekdays"] == [3], legacy["weekdays"]
    print("旧版 weekday 兼容映射 PASS")

    # 节假日策略：顺延工作日
    hol = {weekdays_days(2026, 11, [2])[0].isoformat()}
    custom2 = rules_mod.merge_rules({"weekdays": [2], "holiday_policy": "next_workday"})
    plan3, _ = generate_plan(2026, 11, teachers, students, cts, holidays=hol, rules=custom2)
    print("节假日顺延工作日:", [p["date"] for p in plan3][:4])
    assert hol & {p["date"] for p in plan3} == set()
    print("自定义规则 + 节假日策略 PASS")
